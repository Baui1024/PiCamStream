"""Background polling of the TSL27721 ambient light sensor.

Owns one long-lived sensor instance for the life of the process and publishes
the most recent reading. Two details drive the design:

- ``TSL27721.read()`` is synchronous and blocks for at least one integration
  period — up to ~700 ms on the most sensitive rung — so every read goes
  through an executor rather than the event loop.
- ``light_sensor.read_light()`` is deliberately not used. It reopens the bus
  and restarts the auto-range ladder from the middle on every call, which is
  right for a one-shot CLI read and wrong for a loop: in darkness it would
  re-walk several rungs, each costing a full integration.

Use from asyncio::

    monitor = LightMonitor(executor=pool)
    await monitor.start()
    ...
    reading = monitor.latest()
    await monitor.stop()
"""

import asyncio
import math
import time
from concurrent.futures import Executor
from typing import Callable, Optional

from loguru import logger

from .config import (
    LIGHT_SENSOR_POLL_INTERVAL_S,
    LIGHT_SENSOR_RETRY_INTERVAL_S,
    LIGHT_SENSOR_IR_LUX_PER_PCT,
)
from .light_sensor import LightReading, TSL27721

# A reading older than this many poll intervals is reported as unavailable
# rather than as a stale-but-plausible number.
_STALE_INTERVALS = 3


class LightMonitor:
    """Polls the ambient light sensor and caches the latest reading."""

    def __init__(self,
                 interval_s: float = LIGHT_SENSOR_POLL_INTERVAL_S,
                 retry_s: float = LIGHT_SENSOR_RETRY_INTERVAL_S,
                 executor: Optional[Executor] = None,
                 sensor_factory: Callable[[], TSL27721] = TSL27721):
        self._interval_s = interval_s
        self._retry_s = retry_s
        self._executor = executor
        # Injectable so the monitor can be exercised without hardware.
        self._sensor_factory = sensor_factory

        self._sensor: Optional[TSL27721] = None
        self._task: Optional[asyncio.Task] = None
        self._reading: Optional[LightReading] = None
        self._reading_ts: float = 0.0
        self._failure_logged = False
        self._opened_once = False

        # Set by whoever owns the illuminator, so lux can be corrected for the
        # IR we are adding ourselves. Kept as a hook rather than a reference so
        # this module does not depend on the IR controller.
        self.ir_brightness_source: Callable[[], float] = lambda: 0.0

    # -- lifecycle --

    async def start(self) -> None:
        """Open the sensor and begin polling. Never fatal."""
        if self._task is not None:
            return
        await self._open()
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        """Stop polling and release the sensor."""
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        await self._close()

    # -- state --

    def latest(self) -> Optional[LightReading]:
        """Most recent reading, or None if there has never been one."""
        return self._reading

    def age_s(self) -> float:
        """Seconds since the last successful read, or inf if never."""
        if not self._reading_ts:
            return math.inf
        return time.monotonic() - self._reading_ts

    @property
    def available(self) -> bool:
        """True when the sensor is open and its reading is recent."""
        return (self._sensor is not None
                and self._reading is not None
                and self.age_s() <= self._interval_s * _STALE_INTERVALS)

    def corrected_lux(self, reading: Optional[LightReading] = None) -> Optional[float]:
        """Lux with our own IR illumination subtracted.

        The sensor's two-diode lux equation already cancels IR to first order,
        but its coefficients are fitted for broadband light, not a 940 nm LED,
        so a calibrated residual is removed here. Applied at this layer on
        purpose: LightReading stays raw sensor truth.
        """
        reading = reading if reading is not None else self._reading
        if reading is None:
            return None
        try:
            ir_pct = float(self.ir_brightness_source())
        except Exception:
            ir_pct = 0.0
        return max(0.0, reading.lux - LIGHT_SENSOR_IR_LUX_PER_PCT * ir_pct)

    def snapshot(self) -> Optional[dict]:
        """Telemetry payload, or None if the monitor was never started."""
        if self._task is None and self._reading is None and self._sensor is None:
            return None

        if self._reading is None:
            return {"available": False, "reason": "no_reading"}

        data = self._reading.as_dict()
        data["lux_raw"] = data["lux"]
        corrected = self.corrected_lux()
        data["lux"] = round(corrected, 3) if corrected is not None else data["lux"]
        data["ir_compensation"] = round(data["lux_raw"] - data["lux"], 3)
        data["age_s"] = round(self.age_s(), 1)
        data["available"] = self.available
        if not self.available:
            data["reason"] = "sensor_unavailable" if self._sensor is None else "stale"
        return data

    # -- internals --

    async def _open(self) -> bool:
        """Open the sensor in the executor. Returns success."""
        loop = asyncio.get_running_loop()
        try:
            sensor = await loop.run_in_executor(
                self._executor, self._enter_sensor)
        except Exception as e:
            # One warning per outage, then quiet: this retries forever.
            if not self._failure_logged:
                logger.warning(f"Light sensor unavailable: {e}")
                self._failure_logged = True
            else:
                logger.debug(f"Light sensor still unavailable: {e}")
            self._sensor = None
            return False

        self._sensor = sensor
        # Deliberately does NOT clear _failure_logged: a sensor that opens but
        # then fails every read would otherwise warn on every retry cycle.
        # Only a successful read counts as recovery.
        message = (f"Light sensor ready on i2c-{sensor.bus_num} at "
                   f"0x{sensor.addr:02x}, polling every {self._interval_s:g}s")
        if self._opened_once:
            logger.debug(message)  # reopen after a failure — not news
        else:
            logger.info(message)
            self._opened_once = True
        return True

    def _enter_sensor(self) -> TSL27721:
        """Blocking: construct and open the sensor. Runs in the executor."""
        return self._sensor_factory().__enter__()

    async def _close(self) -> None:
        sensor, self._sensor = self._sensor, None
        if sensor is None:
            return
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(self._executor, sensor.__exit__, None, None, None)
        except Exception as e:
            logger.debug(f"Light sensor close failed: {e}")

    async def _run(self) -> None:
        """Poll forever. Only CancelledError escapes."""
        loop = asyncio.get_running_loop()
        while True:
            if self._sensor is None:
                await asyncio.sleep(self._retry_s)
                await self._open()
                continue

            try:
                reading = await loop.run_in_executor(self._executor, self._sensor.read)
                self._reading = reading
                self._reading_ts = time.monotonic()
                if self._failure_logged:
                    logger.info("Light sensor recovered")
                    self._failure_logged = False
            except asyncio.CancelledError:
                raise
            except Exception as e:
                if not self._failure_logged:
                    logger.warning(f"Light sensor read failed: {e}")
                    self._failure_logged = True
                else:
                    logger.debug(f"Light sensor read failed: {e}")
                # Drop the handle so the next iteration reopens the bus.
                await self._close()
                continue

            await asyncio.sleep(self._interval_s)
