"""VEYE ISP control via I2C (smbus2) and settings persistence to JSON."""

import json
import time
from pathlib import Path
from typing import Optional

from loguru import logger

try:
    from smbus2 import SMBus, i2c_msg
    _smbus_available = True
except ImportError:
    _smbus_available = False
    SMBus = None
    i2c_msg = None

from .config import V4L2_I2C_BUS, V4L2_I2C_ADDR

# All ISP parameters we manage (persisted to JSON)
ISP_PARAMS = [
    "daynightmode",
    "mshutter",
    "agc",
    "denoise",
    "brightness",
    "contrast",
    "saturation",
    "sharppen",
    "wdrmode",
    "lowlight",
    "wbmode",
    # --- added params ---
    "videoformat",
    "mirrormode",
    "ircutdir",
    "irtrigger",
    "cameramode",
    "nodf",
    "wdrtargetbr",
    "wdrbtargetbr",
    "aespeed_agc",
    "aespeed_shutter",
    "mwbgain_rgain",
    "mwbgain_bgain",
]

# Read-only params (queried but never written back on apply)
_READ_ONLY_PARAMS = {"awbgain_rgain", "awbgain_bgain"}

SETTINGS_FILE = Path(__file__).parent.parent / "isp_settings.json"

# Simple indirect register map: param → (page, offset)
_INDIRECT_REGS: dict[str, tuple[int, int]] = {
    "wdrmode":      (0xDB, 0x32),
    "denoise":      (0xD8, 0x9B),
    "mshutter":     (0xDA, 0x66),
    "contrast":     (0x49, 0x5B),
    "wbmode":       (0xDA, 0x34),
    "lowlight":     (0xDA, 0x64),
    "videoformat":  (0xDE, 0xC2),
    "mirrormode":   (0xDE, 0x57),
    "wdrtargetbr":  (0xDA, 0xC1),
    "wdrbtargetbr": (0xDA, 0xCA),
    "aespeed_agc":      (0xDA, 0x18),
    "aespeed_shutter":  (0xDA, 0x1B),
    "mwbgain_rgain":    (0xDA, 0x2E),
    "mwbgain_bgain":    (0xDA, 0x29),
}

# Direct register map: param → 16-bit register address
_DIRECT_REGS: dict[str, int] = {
    "daynightmode": 0x02,
    "ircutdir":     0x16,
    "irtrigger":    0x15,
    "cameramode":   0x1A,
    "nodf":         0x1B,
}


# ---------------------------------------------------------------------------
# I2C control class
# ---------------------------------------------------------------------------

class VeyeISPControl:
    """Direct I2C control for VEYE ISP cameras (IMX462/IMX327).

    Use as a context manager::

        with VeyeISPControl() as isp:
            val = isp.read("brightness")
            isp.write("brightness", 0x80)
    """

    def __init__(self, bus_num: int = V4L2_I2C_BUS, addr: int = V4L2_I2C_ADDR):
        self._bus_num = bus_num
        self._addr = addr
        self._bus: Optional[SMBus] = None

    def __enter__(self):
        if not _smbus_available:
            raise RuntimeError("smbus2 is not installed")
        self._bus = SMBus(self._bus_num)
        # Enable I2C transfer on the ISP
        self._write_reg(0x07, 0xFE)
        time.sleep(0.01)
        return self

    def __exit__(self, *exc):
        try:
            # Disable I2C transfer
            self._write_reg(0x07, 0xFF)
        except Exception:
            pass
        if self._bus:
            self._bus.close()
            self._bus = None

    # -- low-level helpers --

    def _write_reg(self, reg: int, val: int) -> None:
        """Raw I2C write: 16-bit register address + 8-bit data."""
        msg = i2c_msg.write(self._addr, [reg >> 8, reg & 0xFF, val])
        self._bus.i2c_rdwr(msg)

    def _read_reg(self, reg: int) -> int:
        """Raw I2C read: write 16-bit register address, then read 1 byte."""
        wr = i2c_msg.write(self._addr, [reg >> 8, reg & 0xFF])
        rd = i2c_msg.read(self._addr, 1)
        self._bus.i2c_rdwr(wr, rd)
        return list(rd)[0]

    def _indirect_read(self, page: int, offset: int) -> int:
        self._write_reg(0x10, page)
        time.sleep(0.005)
        self._write_reg(0x11, offset)
        time.sleep(0.005)
        self._write_reg(0x13, 0x01)
        time.sleep(0.01)
        return self._read_reg(0x14)

    def _indirect_write(self, page: int, offset: int, value: int) -> None:
        self._write_reg(0x10, page)
        time.sleep(0.005)
        self._write_reg(0x11, offset)
        time.sleep(0.005)
        self._write_reg(0x12, value)
        time.sleep(0.005)
        self._write_reg(0x13, 0x00)
        time.sleep(0.005)

    def _get_video_format(self) -> int:
        """0 = PAL (50 Hz), 1 = NTSC (60 Hz)."""
        return self._indirect_read(0xDE, 0xC2)

    # -- public read / write --

    def read(self, param: str) -> int:
        """Read a single ISP parameter. Returns the raw byte value."""
        # Direct register params
        if param in _DIRECT_REGS:
            return self._read_reg(_DIRECT_REGS[param])

        if param == "agc":
            return self._indirect_read(0xDA, 0x67) & 0x0F

        if param == "brightness":
            offset = 0x65 if self._get_video_format() == 1 else 0x1A
            return self._indirect_read(0xDA, offset)

        if param == "saturation":
            return self._indirect_read(0xD8, 0x7A)

        if param == "sharppen":
            # Return the sharpening strength (upper nibble of value register)
            return self._indirect_read(0xD9, 0x52) >> 4

        # Read-only AWB gain
        if param == "awbgain_rgain":
            return self._indirect_read(0x5E, 0x0B)
        if param == "awbgain_bgain":
            return self._indirect_read(0x5E, 0x0F)

        if param in _INDIRECT_REGS:
            page, offset = _INDIRECT_REGS[param]
            return self._indirect_read(page, offset)

        raise ValueError(f"Unknown ISP param: {param}")

    def write(self, param: str, value: int) -> None:
        """Write a single ISP parameter."""
        # Direct register params
        if param in _DIRECT_REGS:
            self._write_reg(_DIRECT_REGS[param], value)
            return

        if param == "agc":
            # Read-modify-write: preserve upper nibble (exposure mode flag)
            current = self._indirect_read(0xDA, 0x67) & 0xF0
            self._indirect_write(0xDA, 0x67, current | (value & 0x0F))
            return

        if param == "brightness":
            offset = 0x65 if self._get_video_format() == 1 else 0x1A
            self._indirect_write(0xDA, offset, value)
            return

        if param == "saturation":
            # ISP uses two registers for saturation
            self._indirect_write(0xD8, 0x7A, value)
            self._indirect_write(0xD8, 0x7B, value)
            return

        if param == "sharppen":
            # Always enable sharpening; write strength to value register
            self._indirect_write(0xD9, 0x5D, 0x01)
            self._indirect_write(0xD9, 0x52, (value << 4) | 0x03)
            return

        if param == "lowlight":
            # Enable/disable lowlight mode via auxiliary registers
            if value == 0:
                self._indirect_write(0xDA, 0x6D, 0xA5)
                self._indirect_write(0xDA, 0x66, 0x40)
            else:
                self._indirect_write(0xDA, 0x6D, 0xA4)
                self._indirect_write(0xDA, 0x66, 0x41)
            time.sleep(0.01)
            self._indirect_write(0xDA, 0x64, value)
            return

        if param in _INDIRECT_REGS:
            page, offset = _INDIRECT_REGS[param]
            self._indirect_write(page, offset, value)
            return

        raise ValueError(f"Unknown ISP param: {param}")

    def capture(self) -> None:
        """Trigger a single frame capture (only valid in capture cameramode)."""
        self._write_reg(0x1C, 0x01)


# ---------------------------------------------------------------------------
# Hex string helpers  (JSON stores "0xfe", Python works with int)
# ---------------------------------------------------------------------------

def _to_hex(val: int) -> str:
    return f"0x{val:02x}"


def _from_hex(val: str) -> int:
    return int(val, 16)


# ---------------------------------------------------------------------------
# Query / apply / persist
# ---------------------------------------------------------------------------

def query_camera() -> dict[str, str]:
    """Read all ISP parameters from the camera. Returns hex-string dict."""
    settings: dict[str, str] = {}
    all_params = list(ISP_PARAMS) + list(_READ_ONLY_PARAMS)
    with VeyeISPControl() as isp:
        for param in all_params:
            try:
                val = isp.read(param)
                settings[param] = _to_hex(val)
                logger.debug(f"Queried {param} = {settings[param]}")
            except Exception as e:
                logger.warning(f"Could not query {param}: {e}")
    return settings


def apply_settings(settings: dict[str, str]) -> None:
    """Write all settings to the camera."""
    with VeyeISPControl() as isp:
        for param, hex_val in settings.items():
            if param not in ISP_PARAMS or param in _READ_ONLY_PARAMS:
                continue
            try:
                isp.write(param, _from_hex(hex_val))
                logger.debug(f"Applied {param} = {hex_val}")
            except Exception as e:
                logger.warning(f"Failed to apply {param} = {hex_val}: {e}")


def load_or_init(path: Path = SETTINGS_FILE) -> dict[str, str]:
    """Load from JSON and apply, or query camera and create the JSON."""
    if path.exists():
        try:
            with open(path) as f:
                settings = json.load(f)
            logger.info(f"Loaded ISP settings from {path}")
            apply_settings(settings)
            return settings
        except Exception as e:
            logger.error(f"Failed to load {path}: {e}")

    logger.info("No ISP settings file found, querying camera...")
    settings = query_camera()
    save(settings, path)
    return settings


def save(settings: dict[str, str], path: Path = SETTINGS_FILE) -> None:
    """Persist settings dict to JSON."""
    try:
        with open(path, "w") as f:
            json.dump(settings, f, indent=2)
        logger.debug(f"Saved ISP settings to {path}")
    except Exception as e:
        logger.error(f"Failed to save {path}: {e}")


def update_param(settings: dict[str, str], param: str, hex_val: str,
                 path: Path = SETTINGS_FILE) -> bool:
    """Update one ISP param: write to camera via I2C and persist to JSON."""
    if param not in ISP_PARAMS:
        logger.warning(f"Unknown or read-only ISP param: {param}")
        return False
    try:
        with VeyeISPControl() as isp:
            isp.write(param, _from_hex(hex_val))
        settings[param] = hex_val
        save(settings, path)
        return True
    except Exception as e:
        logger.error(f"Failed to update {param}: {e}")
        return False
