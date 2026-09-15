"""TSL27721 ambient light sensor control via I2C (smbus2).

The TSL27721 sits on the custom nightvision board and measures ambient light
so the IR illuminators can be driven only when they are actually needed.

The part has two photodiodes: channel 0 is visible + IR, channel 1 is IR only.
Lux is derived from the pair, and the IR-only channel doubles as a way to tell
whether the scene is lit by daylight or by our own IR LEDs.

Use as a context manager::

    with TSL27721() as als:
        r = als.read()
        print(r.lux, r.ch0, r.ch1)
"""

import time
from dataclasses import dataclass
from typing import Optional

from loguru import logger

try:
    from smbus2 import SMBus, i2c_msg
    _smbus_available = True
except ImportError:
    _smbus_available = False
    SMBus = None
    i2c_msg = None

from .config import (
    LIGHT_SENSOR_I2C_BUS,
    LIGHT_SENSOR_I2C_ADDR,
    LIGHT_SENSOR_GLASS_ATTENUATION,
)

# ---------------------------------------------------------------------------
# Register map
# ---------------------------------------------------------------------------

# Every access is prefixed with a command byte: bit 7 set, bits 6:5 select the
# transaction type (0b01 = auto-increment, needed to read 16-bit data pairs).
_CMD = 0x80
_CMD_AUTO_INC = 0xA0

_REG_ENABLE = 0x00
_REG_ATIME = 0x01
_REG_WTIME = 0x03
_REG_PERS = 0x0C
_REG_CONFIG = 0x0D
_REG_CONTROL = 0x0F
_REG_ID = 0x12
_REG_STATUS = 0x13
_REG_C0DATA = 0x14  # 0x14/0x15 = ch0 low/high, 0x16/0x17 = ch1 low/high

# ENABLE register bits
_EN_PON = 0x01   # power on
_EN_AEN = 0x02   # ALS enable
_EN_WEN = 0x08   # wait timer enable

# STATUS register bits
_ST_AVALID = 0x01  # ALS integration cycle completed

# Device ID: the TSL2772/TSL27721/TSL27723 family reports 0x3x.
_ID_MASK = 0xF0
_ID_TSL2772_FAMILY = 0x30

# AGAIN field (CONTROL bits 1:0) -> gain multiplier
_GAIN_BITS = {1: 0x00, 8: 0x01, 16: 0x02, 120: 0x03}

# One integration cycle in milliseconds (2.73 ms per the datasheet).
_CYCLE_MS = 2.73

# Lux coefficients for the TSL2772 family, open air (datasheet "Lux Equation").
# GA is applied separately from config so it can be calibrated per enclosure.
_COEF_B = 1.85
_COEF_C = 0.80
_COEF_D = 1.25
_DEVICE_FACTOR = 52.0

# Auto-range ladder, ordered from least to most sensitive. Each entry is
# (integration cycles, gain). Spans direct sunlight down to overcast moonlight.
_RANGE_LADDER: list[tuple[int, int]] = [
    (1, 1),      # 2.7 ms,  1x   — full sun
    (19, 1),     # 51.9 ms, 1x
    (19, 8),     # 51.9 ms, 8x   — indoors / overcast
    (37, 16),    # 101 ms,  16x
    (37, 120),   # 101 ms,  120x — dusk
    (64, 120),   # 175 ms,  120x
    (256, 120),  # 699 ms,  120x — near darkness
]

# Fractions of full scale that trigger a range change.
_SATURATED_AT = 0.90
_TOO_DIM_AT = 0.10


@dataclass
class LightReading:
    """One ambient light measurement."""

    lux: float
    ch0: int              # visible + IR counts
    ch1: int              # IR only counts
    gain: int             # ALS gain in effect (1, 8, 16 or 120)
    integration_ms: float  # integration time in effect
    saturated: bool       # channel hit full scale — lux is a lower bound

    @property
    def ir_ratio(self) -> float:
        """ch1/ch0 — high means IR-dominant light (our own illuminators)."""
        return self.ch1 / self.ch0 if self.ch0 else 0.0

    def as_dict(self) -> dict:
        return {
            "lux": round(self.lux, 3),
            "ch0": self.ch0,
            "ch1": self.ch1,
            "gain": self.gain,
            "integration_ms": round(self.integration_ms, 2),
            "saturated": self.saturated,
            "ir_ratio": round(self.ir_ratio, 3),
        }


class TSL27721:
    """Direct I2C control for the TSL27721 ambient light sensor."""

    def __init__(self, bus_num: int = LIGHT_SENSOR_I2C_BUS,
                 addr: int = LIGHT_SENSOR_I2C_ADDR,
                 glass_attenuation: float = LIGHT_SENSOR_GLASS_ATTENUATION):
        self._bus_num = bus_num
        self._addr = addr
        self._ga = glass_attenuation
        self._bus: Optional[SMBus] = None
        self._cycles = 0
        self._gain = 0

    def __enter__(self):
        if not _smbus_available:
            raise RuntimeError("smbus2 is not installed")
        self._bus = SMBus(self._bus_num)
        try:
            self._verify_id()
            # Power on, then give the oscillator time to settle before the
            # ALS engine is allowed to start integrating.
            self._write_reg(_REG_ENABLE, _EN_PON)
            time.sleep(0.003)
            self._write_reg(_REG_CONFIG, 0x00)  # no wait-long, no gain divider
            # Start in the middle of the ladder; auto-ranging converges from here.
            self.configure(cycles=19, gain=8)
        except Exception:
            self._bus.close()
            self._bus = None
            raise
        return self

    def __exit__(self, *exc):
        try:
            self._write_reg(_REG_ENABLE, 0x00)  # power down
        except Exception:
            pass
        if self._bus:
            self._bus.close()
            self._bus = None

    # -- low-level helpers --

    def _write_reg(self, reg: int, val: int) -> None:
        """Write one 8-bit register."""
        msg = i2c_msg.write(self._addr, [_CMD | reg, val])
        self._bus.i2c_rdwr(msg)

    def _read_reg(self, reg: int) -> int:
        """Read one 8-bit register."""
        wr = i2c_msg.write(self._addr, [_CMD | reg])
        rd = i2c_msg.read(self._addr, 1)
        self._bus.i2c_rdwr(wr, rd)
        return list(rd)[0]

    def _read_block(self, reg: int, length: int) -> list[int]:
        """Read consecutive registers in one transaction (auto-increment).

        The data registers are double-buffered per integration cycle, so the
        ch0/ch1 pair must be read in a single burst to stay coherent.
        """
        wr = i2c_msg.write(self._addr, [_CMD_AUTO_INC | reg])
        rd = i2c_msg.read(self._addr, length)
        self._bus.i2c_rdwr(wr, rd)
        return list(rd)

    def _verify_id(self) -> None:
        try:
            dev_id = self._read_reg(_REG_ID)
        except OSError as e:
            raise RuntimeError(
                f"No response from 0x{self._addr:02x} on i2c-{self._bus_num} "
                f"({e.strerror}) — check wiring, pull-ups and sensor power"
            ) from e
        if (dev_id & _ID_MASK) != _ID_TSL2772_FAMILY:
            raise RuntimeError(
                f"Unexpected device ID 0x{dev_id:02x} at "
                f"0x{self._addr:02x} on i2c-{self._bus_num} — not a TSL27721"
            )
        logger.debug(f"TSL27721 found at 0x{self._addr:02x} (ID 0x{dev_id:02x})")

    # -- configuration --

    @property
    def bus_num(self) -> int:
        return self._bus_num

    @property
    def addr(self) -> int:
        return self._addr

    @property
    def integration_ms(self) -> float:
        return self._cycles * _CYCLE_MS

    @property
    def gain(self) -> int:
        return self._gain

    @property
    def full_scale(self) -> int:
        """Maximum count for the current integration time."""
        return min(65535, 1024 * self._cycles)

    def configure(self, cycles: int, gain: int) -> None:
        """Set integration time (in 2.73 ms cycles, 1-256) and ALS gain.

        Disables the ALS engine while reconfiguring so the next reading is
        taken entirely under the new settings rather than straddling both.
        """
        if not 1 <= cycles <= 256:
            raise ValueError(f"cycles must be 1..256, got {cycles}")
        if gain not in _GAIN_BITS:
            raise ValueError(f"gain must be one of {sorted(_GAIN_BITS)}, got {gain}")

        self._write_reg(_REG_ENABLE, _EN_PON)  # AEN off
        # ATIME counts down: 256 cycles is encoded as 0x00.
        self._write_reg(_REG_ATIME, (256 - cycles) & 0xFF)
        self._write_reg(_REG_CONTROL, _GAIN_BITS[gain])
        self._write_reg(_REG_ENABLE, _EN_PON | _EN_AEN)

        self._cycles = cycles
        self._gain = gain
        logger.debug(f"TSL27721 configured: {self.integration_ms:.1f} ms, {gain}x")

    # -- measurement --

    def _wait_for_data(self, timeout: float = 1.5) -> bool:
        """Block until the ALS engine reports a completed integration cycle."""
        # A cycle is already in flight; sleep through it before polling.
        time.sleep(self.integration_ms / 1000.0 + 0.003)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._read_reg(_REG_STATUS) & _ST_AVALID:
                return True
            time.sleep(0.002)
        logger.warning("TSL27721: timed out waiting for ALS data")
        return False

    def read_raw(self) -> tuple[int, int]:
        """Read the raw (ch0, ch1) counts from a completed integration cycle."""
        self._wait_for_data()
        data = self._read_block(_REG_C0DATA, 4)
        ch0 = data[0] | (data[1] << 8)
        ch1 = data[2] | (data[3] << 8)
        return ch0, ch1

    def compute_lux(self, ch0: int, ch1: int) -> float:
        """Convert raw counts to lux using the datasheet equation."""
        cpl = (self.integration_ms * self._gain) / (self._ga * _DEVICE_FACTOR)
        if cpl <= 0:
            return 0.0
        lux1 = (ch0 - _COEF_B * ch1) / cpl
        lux2 = (_COEF_C * ch0 - _COEF_D * ch1) / cpl
        return max(lux1, lux2, 0.0)

    def read(self, auto_range: bool = True) -> LightReading:
        """Take one measurement, re-ranging the sensor as needed.

        With auto_range the sensor walks the gain/integration ladder until the
        reading sits in a usable part of the ADC span, which is what lets the
        same call work in daylight and in near darkness.
        """
        ch0, ch1 = self.read_raw()

        if auto_range:
            idx = self._ladder_index()
            for _ in range(len(_RANGE_LADDER)):
                fs = self.full_scale
                if ch0 >= fs * _SATURATED_AT and idx > 0:
                    idx -= 1  # too bright — less sensitive
                elif ch0 < fs * _TOO_DIM_AT and idx < len(_RANGE_LADDER) - 1:
                    idx += 1  # too dim — more sensitive
                else:
                    break
                cycles, gain = _RANGE_LADDER[idx]
                self.configure(cycles, gain)
                ch0, ch1 = self.read_raw()

        saturated = ch0 >= self.full_scale or ch1 >= self.full_scale
        if saturated:
            logger.debug("TSL27721: reading saturated, lux is a lower bound")

        return LightReading(
            lux=self.compute_lux(ch0, ch1),
            ch0=ch0,
            ch1=ch1,
            gain=self._gain,
            integration_ms=self.integration_ms,
            saturated=saturated,
        )

    def _ladder_index(self) -> int:
        """Closest ladder position to the current settings."""
        current = (self._cycles, self._gain)
        if current in _RANGE_LADDER:
            return _RANGE_LADDER.index(current)
        # Fall back to the rung with the nearest overall sensitivity.
        target = self._cycles * self._gain
        return min(
            range(len(_RANGE_LADDER)),
            key=lambda i: abs(_RANGE_LADDER[i][0] * _RANGE_LADDER[i][1] - target),
        )


def read_light() -> Optional[LightReading]:
    """One-shot convenience read. Returns None if the sensor is unreachable."""
    try:
        with TSL27721() as als:
            return als.read()
    except Exception as e:
        logger.warning(f"Light sensor read failed: {e}")
        return None


if __name__ == "__main__":
    import sys

    logger.remove()
    logger.add(sys.stderr, format="<green>{time:HH:mm:ss}</green> | {message}",
               level="DEBUG" if "-v" in sys.argv else "INFO")

    with TSL27721() as als:
        logger.info(f"TSL27721 on i2c-{als.bus_num} at 0x{als.addr:02x}")
        print(f"{'lux':>12}  {'ch0':>6}  {'ch1':>6}  {'gain':>5}  "
              f"{'int(ms)':>8}  {'ir':>5}")
        try:
            while True:
                r = als.read()
                flag = " SAT" if r.saturated else ""
                print(f"{r.lux:12.3f}  {r.ch0:6d}  {r.ch1:6d}  {r.gain:4d}x  "
                      f"{r.integration_ms:8.1f}  {r.ir_ratio:5.2f}{flag}")
                time.sleep(0.5)
        except KeyboardInterrupt:
            print()
            logger.info("Stopped")
