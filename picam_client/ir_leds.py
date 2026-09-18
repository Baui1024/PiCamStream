"""IR LED illuminator dimming via sysfs PWM (AL8860 CTRL pin).

Hardware chain: GPIO12 (PWM0, ALT0) -> AL8860 hysteretic buck LED driver ->
4x SFH 4726BS A01 in series, 940 nm, from a 24 V rail.

Requires ``dtoverlay=pwm,pin=12,func=4`` in /boot/firmware/config.txt (added by
install.sh). Verify after reboot with ``pinctrl get 12``, which should report
``a0``. install.sh also installs a udev rule giving the ``gpio`` group write
access to /sys/class/pwm so the service does not need to run as root.

The AL8860 dims on its CTRL pin and wants a PWM frequency below 500 Hz; above
that both the usable dimming range and the accuracy degrade. LED current is
linear in duty cycle, with I_OUT_NOM = 0.1 / Rs set by the sense resistor
(see IR_LED_SENSE_RESISTOR_OHM). Linearity is only specified from 1% duty
upwards, so anything below that is treated as off.

Keep the duty cycle high where you can: the illuminator is PWM-modulated and
the sensor has a rolling shutter, so low duty amplifies the residual banding.
See the README for the exposure/PWM relationship.

Use as a context manager::

    with IRIlluminator() as ir:
        ir.set_brightness(40)
"""

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from loguru import logger

from .config import (
    IR_PWM_CHIP,
    IR_PWM_CHANNEL,
    IR_PWM_PERIOD_NS,
    IR_LED_MAX_DUTY_PCT,
    IR_LED_DEFAULT_BRIGHTNESS,
    IR_LED_SENSE_RESISTOR_OHM,
    IR_AUTO_DEFAULT_MODE,
)

_SYSFS_ROOT = Path("/sys/class/pwm")

# Brightness survives restarts, the same way the ISP settings do.
SETTINGS_FILE = Path(__file__).parent.parent / "ir_settings.json"

# AL8860 datasheet: keep PWM dimming below 500 Hz. 500 Hz == 2 ms period, and
# a *longer* period is always safe, so this is a floor on period_ns.
_MIN_PERIOD_NS = 2_000_000

# Below 1% duty the AL8860 is outside its specified linear range — the output
# may misbehave rather than simply being dim, so we snap it to hard off.
_MIN_LINEAR_DUTY_PCT = 1.0

# Full-scale LED current at 100% duty: AL8860 regulates 100 mV across Rs.
_I_OUT_NOM_MA = 100.0 / IR_LED_SENSE_RESISTOR_OHM

# How long to wait for the kernel to create the channel directory and for udev
# to apply group permissions to it after export.
_EXPORT_TIMEOUT_S = 2.0


@dataclass
class IRState:
    """Current illuminator state."""

    brightness_pct: float   # what the caller asked for, 0-100
    duty_pct: float         # actual PWM duty after the power-budget cap
    duty_cycle_ns: int
    period_ns: int
    enabled: bool

    @property
    def frequency_hz(self) -> float:
        return 1e9 / self.period_ns if self.period_ns else 0.0

    @property
    def current_ma(self) -> float:
        """Approximate LED current — linear in duty cycle."""
        return self.duty_pct / 100.0 * _I_OUT_NOM_MA

    def as_dict(self) -> dict:
        return {
            "brightness_pct": round(self.brightness_pct, 1),
            "duty_pct": round(self.duty_pct, 2),
            "duty_cycle_ns": self.duty_cycle_ns,
            "period_ns": self.period_ns,
            "frequency_hz": round(self.frequency_hz, 1),
            "current_ma": round(self.current_ma, 1),
            "enabled": self.enabled,
        }


class IRIlluminator:
    """PWM dimming control for the IR illuminator board.

    ``brightness`` is always 0-100 from the caller's point of view. It is
    scaled internally to ``max_duty_pct`` so the public API stays intuitive
    while the hardware power ceiling is respected.
    """

    def __init__(self, chip: str = IR_PWM_CHIP,
                 channel: int = IR_PWM_CHANNEL,
                 period_ns: int = IR_PWM_PERIOD_NS,
                 max_duty_pct: float = IR_LED_MAX_DUTY_PCT,
                 allow_above_spec: bool = False):
        if period_ns < _MIN_PERIOD_NS:
            if not allow_above_spec:
                raise ValueError(
                    f"period_ns {period_ns} is faster than 500 Hz — the AL8860 "
                    f"needs >= {_MIN_PERIOD_NS} ns"
                )
            # Deliberate override, e.g. testing whether a faster carrier cures
            # rolling-shutter banding. Costs low-end dimming range.
            logger.warning(
                f"IR PWM at {1e9 / period_ns:.0f} Hz is above the AL8860's "
                f"500 Hz recommendation — dimming accuracy and low-end range "
                f"will suffer"
            )
        if not 0 < max_duty_pct <= 100:
            raise ValueError(f"max_duty_pct must be 0..100, got {max_duty_pct}")

        self._chip_name = chip
        self._channel = channel
        self._period_ns = period_ns
        self._max_duty_pct = float(max_duty_pct)
        self._chip_path: Optional[Path] = None
        self._pwm_path: Optional[Path] = None
        self._brightness = 0.0
        self._duty_ns = 0
        self._enabled = False

    def __enter__(self):
        return self.open()

    def __exit__(self, *exc):
        self.close()

    def open(self) -> "IRIlluminator":
        """Claim the PWM channel and bring it up at 0% brightness."""
        self._chip_path = self._resolve_chip()
        self._pwm_path = self._chip_path / f"pwm{self._channel}"
        self._export()
        # Safe ordering for the initial setup: duty to 0 first so the new
        # period can never be smaller than a duty left over from a previous
        # run, then the period, then enable.
        self._write("duty_cycle", 0)
        self._write("period", self._period_ns)
        self._write("enable", 1)
        self._enabled = True
        self._duty_ns = 0
        self._brightness = 0.0
        # Spell out the current ceiling: it is the one line that would reveal
        # this config being run on a board with a different sense resistor.
        logger.info(
            f"IR illuminator ready on {self._chip_name}/pwm{self._channel}: "
            f"{1e9 / self._period_ns:.0f} Hz, Rs={IR_LED_SENSE_RESISTOR_OHM:g} ohm "
            f"({_I_OUT_NOM_MA:.0f} mA at full duty), capped at "
            f"{self._max_duty_pct:g}% duty = {_I_OUT_NOM_MA * self._max_duty_pct / 100:.0f} mA"
        )
        return self

    def close(self) -> None:
        """Turn the illuminator off and release the PWM channel."""
        if self._pwm_path is None:
            return
        # Leave a defined off-state behind rather than relying on disable
        # alone to pull CTRL low.
        try:
            self._write("duty_cycle", 0)
            self._write("enable", 0)
        except Exception as e:
            logger.warning(f"IR illuminator: failed to turn off cleanly: {e}")
        self._enabled = False
        self._duty_ns = 0
        self._unexport()
        self._pwm_path = None
        self._chip_path = None

    # -- sysfs plumbing --

    def _resolve_chip(self) -> Path:
        """Locate the PWM chip, falling back to whatever the kernel exposes.

        Chip numbering is not stable across kernels and overlay combinations,
        so a configured name that no longer exists should not be fatal while a
        single unambiguous chip is present.
        """
        configured = _SYSFS_ROOT / self._chip_name
        if configured.is_dir():
            return configured

        if not _SYSFS_ROOT.is_dir():
            raise RuntimeError(
                "/sys/class/pwm does not exist — no PWM driver is loaded. "
                "Add 'dtoverlay=pwm,pin=12,func=4' to /boot/firmware/config.txt "
                "and reboot."
            )

        chips = sorted(p for p in _SYSFS_ROOT.iterdir() if p.name.startswith("pwmchip"))
        if not chips:
            raise RuntimeError(
                f"No pwmchip found under {_SYSFS_ROOT}. Add "
                "'dtoverlay=pwm,pin=12,func=4' to /boot/firmware/config.txt "
                "and reboot, then check 'pinctrl get 12' reports 'a0'."
            )
        chosen = chips[0]
        logger.warning(
            f"IR illuminator: {self._chip_name} not found, using {chosen.name} "
            f"instead (available: {', '.join(c.name for c in chips)})"
        )
        self._chip_name = chosen.name
        return chosen

    def _export(self) -> None:
        """Export the channel, tolerating one that is already exported."""
        assert self._chip_path is not None and self._pwm_path is not None
        if not self._pwm_path.is_dir():
            try:
                (self._chip_path / "export").write_text(str(self._channel))
            except PermissionError as e:
                raise self._permission_error(self._chip_path / "export") from e
            except OSError as e:
                # EBUSY means someone exported it between the check and here.
                if not self._pwm_path.is_dir():
                    raise RuntimeError(
                        f"Failed to export {self._chip_name}/pwm{self._channel}: {e}"
                    ) from e

        # The directory and its udev-applied permissions both appear
        # asynchronously after the export write.
        deadline = time.monotonic() + _EXPORT_TIMEOUT_S
        while time.monotonic() < deadline:
            duty = self._pwm_path / "duty_cycle"
            if duty.exists():
                try:
                    with open(duty, "w"):
                        return
                except PermissionError:
                    pass  # udev has not applied the group rule yet
                except OSError:
                    return  # exists and opened differently — let the write fail loudly
            time.sleep(0.02)

        if not self._pwm_path.is_dir():
            raise RuntimeError(
                f"{self._pwm_path} never appeared after export — "
                f"is channel {self._channel} valid for {self._chip_name}?"
            )
        raise self._permission_error(self._pwm_path / "duty_cycle")

    def _permission_error(self, path: Path) -> RuntimeError:
        return RuntimeError(
            f"No write access to {path}. Either run as root or install the udev "
            f"rule that grants the 'gpio' group access to /sys/class/pwm "
            f"(install.sh does this), then make sure this process is in that group."
        )

    def _write(self, name: str, value: int) -> None:
        assert self._pwm_path is not None
        path = self._pwm_path / name
        try:
            path.write_text(str(value))
        except PermissionError as e:
            raise self._permission_error(path) from e
        except OSError as e:
            raise RuntimeError(f"Failed writing {value} to {path}: {e}") from e

    def _read_int(self, name: str) -> int:
        assert self._pwm_path is not None
        try:
            return int((self._pwm_path / name).read_text().strip())
        except (OSError, ValueError):
            return 0

    def _unexport(self) -> None:
        if not self._chip_path or not self._pwm_path or not self._pwm_path.is_dir():
            return
        try:
            (self._chip_path / "unexport").write_text(str(self._channel))
        except OSError as e:
            logger.debug(f"IR illuminator: unexport failed (harmless): {e}")

    # -- control --

    @property
    def max_duty_pct(self) -> float:
        return self._max_duty_pct

    @property
    def min_brightness_pct(self) -> float:
        """Smallest brightness that still produces light.

        The power-budget cap squeezes the bottom of the range: with a 50% cap,
        a requested 1% is only 0.5% actual duty, which is below the AL8860's
        specified linear range and therefore off.
        """
        return _MIN_LINEAR_DUTY_PCT / self._max_duty_pct * 100.0

    @property
    def brightness(self) -> float:
        return self._brightness

    def set_brightness(self, pct: float, persist: bool = False) -> IRState:
        """Set illuminator brightness, 0-100. Returns the resulting state."""
        try:
            pct = float(pct)
        except (TypeError, ValueError) as e:
            raise ValueError(f"brightness must be a number, got {pct!r}") from e
        if pct != pct:  # NaN
            raise ValueError("brightness must be a number, got nan")

        clamped = min(100.0, max(0.0, pct))
        if clamped != pct:
            logger.warning(f"IR brightness {pct} out of range, clamped to {clamped}")

        duty_pct = clamped / 100.0 * self._max_duty_pct
        if duty_pct < _MIN_LINEAR_DUTY_PCT:
            if duty_pct > 0:
                logger.debug(
                    f"IR brightness {clamped:g}% -> {duty_pct:.2f}% duty is below the "
                    f"{_MIN_LINEAR_DUTY_PCT:g}% linear floor, turning off instead"
                )
            # Report 0 rather than the request, so state() and anything built
            # on it never claim the LEDs are lit when they are not.
            duty_pct = 0.0
            clamped = 0.0

        duty_ns = round(duty_pct / 100.0 * self._period_ns)
        # The period never changes at runtime, so duty can be written directly;
        # the kernel only rejects duty_cycle > period.
        self._write("duty_cycle", duty_ns)
        self._duty_ns = duty_ns
        self._brightness = clamped
        logger.debug(f"IR brightness {clamped:g}% -> {duty_pct:.2f}% duty ({duty_ns} ns)")
        if persist:
            save_brightness(clamped)
        return self.state()

    def off(self) -> IRState:
        """Turn the illuminator off, leaving the channel configured."""
        return self.set_brightness(0)

    def state(self) -> IRState:
        duty_pct = self._duty_ns / self._period_ns * 100.0 if self._period_ns else 0.0
        return IRState(
            brightness_pct=self._brightness,
            duty_pct=duty_pct,
            duty_cycle_ns=self._duty_ns,
            period_ns=self._period_ns,
            enabled=self._enabled,
        )


def load_settings(path: Path = SETTINGS_FILE) -> dict:
    """Persisted IR state, falling back to config defaults. Never raises.

    Tolerates the older single-key file: missing keys just take their default.
    """
    settings = {
        "brightness_pct": float(IR_LED_DEFAULT_BRIGHTNESS),
        "mode": IR_AUTO_DEFAULT_MODE,
    }
    if path.exists():
        try:
            with open(path) as f:
                stored = json.load(f)
            if "brightness_pct" in stored:
                settings["brightness_pct"] = min(
                    100.0, max(0.0, float(stored["brightness_pct"])))
            if stored.get("mode") in ("auto", "manual"):
                settings["mode"] = stored["mode"]
        except Exception as e:
            logger.error(f"Failed to load {path}: {e}")
    return settings


def save_settings(path: Path = SETTINGS_FILE, **fields) -> None:
    """Merge fields into the persisted state. Failures only log."""
    settings = load_settings(path)
    settings.update(fields)
    try:
        with open(path, "w") as f:
            json.dump({"brightness_pct": round(float(settings["brightness_pct"]), 1),
                       "mode": settings["mode"]}, f, indent=2)
        logger.debug(f"Saved IR settings to {path}")
    except Exception as e:
        logger.error(f"Failed to save {path}: {e}")


def load_brightness(path: Path = SETTINGS_FILE) -> float:
    """Last persisted brightness, or the config default if there is none."""
    return load_settings(path)["brightness_pct"]


def save_brightness(pct: float, path: Path = SETTINGS_FILE) -> None:
    """Persist brightness so it survives a service restart."""
    save_settings(path, brightness_pct=pct)


if __name__ == "__main__":
    import sys

    logger.remove()
    logger.add(sys.stderr, format="<green>{time:HH:mm:ss}</green> | {message}",
               level="DEBUG" if "-v" in sys.argv else "INFO")

    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    if "--hz" in sys.argv:
        args = [a for a in args if a != sys.argv[sys.argv.index("--hz") + 1]]

    # --hz N overrides the configured PWM frequency for this run only, so a
    # frequency sweep against the live camera does not need a config edit.
    period_ns = IR_PWM_PERIOD_NS
    if "--hz" in sys.argv:
        hz = float(sys.argv[sys.argv.index("--hz") + 1])
        period_ns = round(1e9 / hz)

    with IRIlluminator(period_ns=period_ns, allow_above_spec=True) as ir:
        if args:
            st = ir.set_brightness(float(args[0]))
            logger.info(
                f"brightness {st.brightness_pct:g}% -> {st.duty_pct:.2f}% duty, "
                f"~{st.current_ma:.0f} mA"
            )
            logger.info("Holding — Ctrl-C to turn off and exit")
            try:
                while True:
                    time.sleep(1)
            except KeyboardInterrupt:
                print()
        else:
            logger.info(f"Ramping 0 -> 100 -> 0 (cap {ir.max_duty_pct:g}% duty, "
                        f"floor {ir.min_brightness_pct:.0f}%)")
            try:
                for pct in list(range(0, 101, 5)) + list(range(100, -1, -5)):
                    st = ir.set_brightness(pct)
                    print(f"\r{pct:3d}% -> {st.duty_pct:5.2f}% duty, "
                          f"~{st.current_ma:5.1f} mA", end="", flush=True)
                    time.sleep(0.1)
                print()
            except KeyboardInterrupt:
                print()
        logger.info("Off")
