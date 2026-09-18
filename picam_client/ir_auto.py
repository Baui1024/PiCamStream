"""Automatic day/night switching for the IR illuminator.

Brightness is only ever 0 or 100 — never anything between. That is a policy,
not an optimisation. At both endpoints the PWM pin is static (low or high), so
the LEDs are unmodulated and rolling-shutter banding is impossible at any
exposure. Any intermediate duty brings modulation back, and the image is then
only stripe-free when the exposure happens to be a whole number of PWM
periods. Measured on hardware at 10% brightness in PAL at 500 Hz: 1/100 s
(5.0 periods) is clean, 1/200 s (2.5) and 1/400 s (1.25) both stripe.

If you are tempted to add intermediate levels, read the banding section of the
README first. Image brightness is the camera's auto-exposure's job; it does it
better than a loop here would, and it cannot produce stripes.

Threading: everything runs on the event loop except ISP writes, which go to
the shared I2C executor. That is why there are no locks in this module.
"""

import asyncio
import time
from dataclasses import dataclass
from concurrent.futures import Executor
from typing import Optional, TYPE_CHECKING

from loguru import logger

from . import ir_leds
from .config import (
    IR_AUTO_INTERVAL_S,
    IR_AUTO_NIGHT_LUX,
    IR_AUTO_DAY_LUX,
    IR_AUTO_DAY_LUX_WITH_IR,
    IR_AUTO_DAY_MAX_IR_RATIO,
    IR_AUTO_PHASE_DEBOUNCE_S,
    IR_AUTO_NIGHT_DEBOUNCE_S,
    IR_AUTO_LUX_MAX_AGE_S,
    IR_AUTO_STARTUP_GRACE_S,
    IR_AUTO_UNKNOWN_HOLD_S,
    IR_AUTO_FAILSAFE_PCT,
    IR_AUTO_MANAGE_DAYNIGHT,
    IR_AUTO_MANAGE_SHUTTER_CAP,
    IR_AUTO_NIGHT_SHUTTER_MAX_US,
    IR_PWM_PERIOD_NS,
)

if TYPE_CHECKING:
    from .capture import Camera
    from .light_monitor import LightMonitor

DAY = "day"
NIGHT = "night"
UNKNOWN = "unknown"

# daynightmode register values (VEYE): colour, black & white, external trigger.
_DNM_COLOUR = 0xFF
_DNM_BW = 0xFE
_DNM_EXTERNAL = 0xFC

# Half-line time in microseconds by video format, for auto_shutter_max.
# Confirmed on hardware: PAL reads 2250 -> 2250 * 17.778 = 40000 us = 1/25 s,
# exactly one frame, which is the register's natural maximum.
_HALF_LINE_US = {0: 17.778, 1: 14.815}  # 0 = PAL, 1 = NTSC


def _fmt_lux(lux: float) -> str:
    """Human-readable lux across the sensor's five-decade range."""
    if lux >= 100:
        return f"{lux:.0f}"
    if lux >= 1:
        return f"{lux:.1f}"
    return f"{lux:.3f}"


@dataclass
class IRAutoState:
    """Controller state, published as telemetry."""

    mode: str            # "auto" | "manual"
    phase: str           # "day" | "night" | "unknown"
    brightness_pct: float
    full_duty: bool      # False => "on" is PWM-modulated, banding is possible
    manages_daynight: bool
    lux: Optional[float]
    ir_ratio: Optional[float]
    reason: str

    def as_dict(self) -> dict:
        return {
            "available": True,
            "mode": self.mode,
            "phase": self.phase,
            "brightness_pct": round(self.brightness_pct, 1),
            "full_duty": self.full_duty,
            "manages_daynight": self.manages_daynight,
            "lux": round(self.lux, 3) if self.lux is not None else None,
            "ir_ratio": round(self.ir_ratio, 3) if self.ir_ratio is not None else None,
            "reason": self.reason,
        }


class IRDayNightController:
    """Switches the illuminator and the IR-cut filter on ambient light."""

    def __init__(self, camera: "Camera", light: "LightMonitor",
                 executor: Optional[Executor] = None):
        self._camera = camera
        self._light = light
        self._executor = executor

        self.mode: str = ir_leds.load_settings()["mode"]
        self.phase: str = UNKNOWN
        self._reason = "starting"
        self._task: Optional[asyncio.Task] = None
        self._started = time.monotonic()

        # Debounce bookkeeping for the pending phase change.
        self._candidate: Optional[str] = None
        self._candidate_since = 0.0

        # Set once we know whether "on" is actually full duty, and whether the
        # ISP is driving the IR-cut filter itself.
        self._full_duty = True
        self._manages_daynight = IR_AUTO_MANAGE_DAYNIGHT
        self._day_shutter_cap: Optional[int] = None
        self._failsafe_done = False

    # -- lifecycle --

    async def start(self) -> None:
        self._check_full_duty()
        self._check_daynight_ownership()
        logger.info(
            f"IR auto-switching {'enabled' if self.mode == 'auto' else 'in manual mode'}: "
            f"night below {IR_AUTO_NIGHT_LUX:g} lx, day above {IR_AUTO_DAY_LUX:g} lx "
            f"({IR_AUTO_DAY_LUX_WITH_IR:g} lx while lit), "
            f"{IR_AUTO_PHASE_DEBOUNCE_S:g}s debounce")
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        # Persist the operator's choice, not the auto-derived brightness.
        ir_leds.save_settings(mode=self.mode)

    def _check_full_duty(self) -> None:
        """Warn if "on" would be PWM-modulated rather than static.

        The binary policy only guarantees no banding while brightness 100 maps
        to 100% duty. A board with the original 0.13 ohm sense resistor needs
        IR_LED_MAX_DUTY_PCT = 13, and then "on" is a 13% duty cycle — modulated,
        and the exposure constraint is back.
        """
        ill = getattr(self._camera, "illuminator", None)
        if ill is None:
            return
        if ill.max_duty_pct < 100.0:
            self._full_duty = False
            logger.warning(
                f"IR auto: IR_LED_MAX_DUTY_PCT is {ill.max_duty_pct:g}, so the "
                f"'on' state is a {ill.max_duty_pct:g}% duty cycle rather than a "
                f"static level. Banding is possible unless the exposure is a whole "
                f"number of {IR_PWM_PERIOD_NS / 1000:.0f} us PWM periods "
                f"(PAL 1/25, 1/50 or 1/100). Set IR_LED_MAX_DUTY_PCT = 100 for the "
                f"guaranteed stripe-free path.")

    def _check_daynight_ownership(self) -> None:
        """Stand down from filter control if the ISP is doing it itself."""
        if not self._manages_daynight:
            return
        current = self._camera.get_isp_value("daynightmode")
        if current == _DNM_EXTERNAL:
            self._manages_daynight = False
            logger.info(
                "IR auto: daynightmode is 0xfc (external trigger), so the ISP is "
                "driving the IR-cut filter from its own pin — not touching it.")

    # -- external control (called from the settings websocket handler) --

    def set_mode(self, mode: str) -> None:
        mode = str(mode).lower()
        if mode not in ("auto", "manual"):
            logger.warning(f"IR auto: ignoring unknown mode {mode!r}")
            return
        if mode == self.mode:
            return
        self.mode = mode
        ir_leds.save_settings(mode=mode)
        if mode == "auto":
            # Re-arm: the next classification applies with no debounce, because
            # "auto" should take effect now, not in a minute.
            self.phase = UNKNOWN
            self._candidate = None
            self._failsafe_done = False
        logger.info(f"IR auto: mode -> {mode}")

    def note_manual_brightness(self, pct: float) -> None:
        """A brightness arrived from the UI: that means stop deciding for me."""
        if self.mode != "manual":
            self.set_mode("manual")
        logger.info(f"IR auto: manual brightness {pct:g}% — auto switching paused")

    def current_brightness(self) -> float:
        ill = getattr(self._camera, "illuminator", None)
        return float(ill.brightness) if ill is not None else 0.0

    def snapshot(self) -> Optional[dict]:
        ill = getattr(self._camera, "illuminator", None)
        if ill is None:
            return {"available": False, "reason": "illuminator_unavailable"}
        reading = self._light.latest()
        return IRAutoState(
            mode=self.mode,
            phase=self.phase,
            brightness_pct=self.current_brightness(),
            full_duty=self._full_duty,
            manages_daynight=self._manages_daynight,
            lux=self._light.corrected_lux(),
            ir_ratio=reading.ir_ratio if reading is not None else None,
            reason=self._reason,
        ).as_dict()

    # -- decision --

    def classify(self, lux: Optional[float], ir_ratio: Optional[float],
                 ir_on: bool, now: float) -> str:
        """Decide the phase. Pure apart from the debounce carried on self.

        `now` is a parameter so this can be tested without sleeping.
        """
        if lux is None:
            self._candidate = None
            return UNKNOWN

        if self.phase == UNKNOWN:
            # First classification after start or after re-arming auto: apply
            # at once. Debouncing here would leave the camera blind for a
            # minute after a restart at night.
            phase = NIGHT if lux < IR_AUTO_NIGHT_LUX else DAY
            self._reason = f"{phase}, {_fmt_lux(lux)} lx"
            return phase

        interlocked = False
        if self.phase == NIGHT:
            day_lux = IR_AUTO_DAY_LUX_WITH_IR if ir_on else IR_AUTO_DAY_LUX
            bright_enough = lux > day_lux
            # Never call it day while the light we can see is our own IR.
            ir_dominant = ir_ratio is not None and ir_ratio > IR_AUTO_DAY_MAX_IR_RATIO
            interlocked = bright_enough and ir_dominant
            wants = DAY if (bright_enough and not ir_dominant) else NIGHT
        else:
            wants = NIGHT if lux < IR_AUTO_NIGHT_LUX else DAY

        if wants == self.phase:
            self._candidate = None
            # Say *why* it is still night at 500 lx, rather than reporting a
            # bare lux value that reads like a fault.
            self._reason = (
                f"night held: {_fmt_lux(lux)} lx but IR ratio {ir_ratio:.2f} "
                f"says the light is ours" if interlocked
                else f"{self.phase}, {_fmt_lux(lux)} lx")
            return self.phase

        if self._candidate != wants:
            self._candidate = wants
            self._candidate_since = now
            self._reason = f"{wants} pending, {_fmt_lux(lux)} lx"
            return self.phase

        # Quick into night (a blind camera is the worse failure), slow into day.
        required = IR_AUTO_NIGHT_DEBOUNCE_S if wants == NIGHT else IR_AUTO_PHASE_DEBOUNCE_S
        held = now - self._candidate_since
        if held >= required:
            self._reason = f"{wants}, {_fmt_lux(lux)} lx"
            return wants

        self._reason = (f"{wants} pending {held:.0f}/{required:.0f}s, "
                        f"{_fmt_lux(lux)} lx")
        return self.phase

    # -- actuation --

    def _set_ir(self, pct: float) -> None:
        """Binary only. Anything else is a bug, not a feature."""
        if pct not in (0.0, 100.0):
            raise ValueError(f"IR auto is binary; refusing brightness {pct!r}")
        ill = getattr(self._camera, "illuminator", None)
        if ill is None:
            return
        if abs(ill.brightness - pct) < 0.05:
            return  # already there; skip the sysfs write
        # persist=False: the loop must not rewrite ir_settings.json at loop rate.
        ill.set_brightness(pct, persist=False)

    def _night_shutter_cap_hex(self) -> Optional[str]:
        video_format = self._camera.get_isp_value("videoformat")
        if video_format not in _HALF_LINE_US:
            return None
        raw = round(IR_AUTO_NIGHT_SHUTTER_MAX_US / _HALF_LINE_US[video_format])
        return f"0x{max(1, min(0xFFFF, raw)):04x}"

    async def _apply_phase(self, phase: str) -> None:
        previous, self.phase = self.phase, phase
        if self.mode != "auto":
            return

        changes: dict[str, str] = {}
        if phase == NIGHT:
            # Light first: the ISP write moves the IR-cut filter and takes a few
            # hundred milliseconds, so having the LEDs already lit means the
            # image is never black through the transition.
            self._set_ir(100.0)
            if self._manages_daynight:
                changes["daynightmode"] = f"0x{_DNM_BW:02x}"
            if IR_AUTO_MANAGE_SHUTTER_CAP:
                cap = self._night_shutter_cap_hex()
                if cap:
                    changes["auto_shutter_max"] = cap
        elif phase == DAY:
            self._set_ir(0.0)
            if self._manages_daynight:
                changes["daynightmode"] = f"0x{_DNM_COLOUR:02x}"
            if IR_AUTO_MANAGE_SHUTTER_CAP and self._day_shutter_cap is not None:
                changes["auto_shutter_max"] = f"0x{self._day_shutter_cap:04x}"

        if changes:
            await self._apply_isp(changes)

        logger.info(f"IR auto: {previous} -> {phase} ({self._reason})")

    async def _apply_isp(self, changes: dict[str, str]) -> None:
        """Write ISP params off the event loop — this is hundreds of ms of I2C."""
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(
                self._executor, self._camera.apply_isp, changes)
        except Exception as e:
            logger.warning(f"IR auto: ISP update {changes} failed: {e}")

    # -- loop --

    async def _run(self) -> None:
        while True:
            try:
                await self._step()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"IR auto: step failed: {e}")
            await asyncio.sleep(IR_AUTO_INTERVAL_S)

    async def _step(self) -> None:
        now = time.monotonic()
        reading = self._light.latest()
        age = self._light.age_s()
        usable = reading is not None and age <= IR_AUTO_LUX_MAX_AGE_S

        lux = self._light.corrected_lux() if usable else None
        ir_ratio = reading.ir_ratio if (usable and reading is not None) else None
        ir_on = self.current_brightness() > 0

        # Remember the operator's daytime exposure ceiling, but only while it
        # is actually day: capturing it at night would bake the night cap in as
        # the daytime value after a restart.
        if IR_AUTO_MANAGE_SHUTTER_CAP and self.phase == DAY:
            cap = self._camera.get_isp_value("auto_shutter_max")
            if cap:
                self._day_shutter_cap = cap

        phase = self.classify(lux, ir_ratio, ir_on, now)

        if phase == UNKNOWN:
            self._handle_unknown(now)
            return

        self._failsafe_done = False
        if phase != self.phase:
            await self._apply_phase(phase)
        elif self.mode == "auto":
            # Converge rather than only acting on edges. The phase can be
            # correct while the hardware is not — after a failsafe switched the
            # LEDs off, or if anything else moved the brightness underneath us.
            # _set_ir skips the write when it is already there, so this is free.
            self._set_ir(100.0 if phase == NIGHT else 0.0)

    def _handle_unknown(self, now: float) -> None:
        """No usable reading: hold, then fail safe.

        Holding is the only defensible default. Turning off would blind the
        camera whenever I2C hiccups at night; turning on would light the LEDs
        all day if the sensor dies at noon.
        """
        if self._failsafe_done or self.mode != "auto":
            return
        started = self._started
        grace = (IR_AUTO_STARTUP_GRACE_S if self.phase == UNKNOWN
                 else IR_AUTO_UNKNOWN_HOLD_S)
        elapsed = now - started if self.phase == UNKNOWN else self._light.age_s()
        if elapsed < grace:
            self._reason = f"no light reading for {elapsed:.0f}s, holding"
            return

        logger.warning(
            f"IR auto: no usable light reading for {elapsed:.0f}s — "
            f"falling back to {IR_AUTO_FAILSAFE_PCT:g}%")
        try:
            self._set_ir(float(IR_AUTO_FAILSAFE_PCT))
        except ValueError:
            logger.error(
                f"IR_AUTO_FAILSAFE_PCT must be 0 or 100, not {IR_AUTO_FAILSAFE_PCT}")
        self._reason = "failsafe: no light reading"
        self._failsafe_done = True
