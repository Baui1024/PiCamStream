"""Periodic telemetry push to the inference server.

Collects cached snapshots from registered subsystems and sends them as one
``{"type": "telemetry"}`` message on the settings WebSocket. The TCP frame
stream is deliberately untouched — telemetry changes on a ~1 Hz timescale and
has no business in the per-frame hot path.

The inference server ignores WebSocket message types it does not recognise, so
this is non-breaking against an older server.

Sources must be cheap and synchronous: they return already-cached state, never
perform I/O. A source returning None is omitted from the payload, which is how
"this subsystem is not fitted" is distinguished from "it is fitted but
currently failing" (a dict with ``available: false``).
"""

import asyncio
import time
from typing import Callable, Optional, TYPE_CHECKING

from loguru import logger

from .config import TELEMETRY_INTERVAL_S

if TYPE_CHECKING:
    from .settings_server import SettingsServer

# Source of a telemetry sub-object. Returning None omits the key entirely.
TelemetrySource = Callable[[], Optional[dict]]


class TelemetryHub:
    """Assembles and pushes periodic telemetry."""

    def __init__(self, settings_server: "SettingsServer",
                 interval_s: float = TELEMETRY_INTERVAL_S):
        self._settings_server = settings_server
        self._interval_s = interval_s
        self._sources: dict[str, TelemetrySource] = {}
        self._started = time.monotonic()

    def register(self, name: str, source: TelemetrySource) -> None:
        """Add a named telemetry source."""
        self._sources[name] = source

    def snapshot(self) -> dict:
        """Build one telemetry payload from all registered sources."""
        payload: dict = {
            "ts": time.time(),
            "uptime_s": round(time.monotonic() - self._started, 1),
        }
        for name, source in self._sources.items():
            try:
                value = source()
            except Exception as e:
                # One broken subsystem must not silence the others.
                logger.debug(f"Telemetry source {name!r} failed: {e}")
                continue
            if value is not None:
                payload[name] = value
        return payload

    async def run(self) -> None:
        """Push telemetry forever. Only CancelledError escapes."""
        logger.info(
            f"Telemetry publishing every {self._interval_s:g}s "
            f"({', '.join(self._sources) or 'no sources'})")
        while True:
            try:
                await self._settings_server.broadcast({
                    "type": "telemetry",
                    "data": self.snapshot(),
                })
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.debug(f"Telemetry push failed: {e}")
            await asyncio.sleep(self._interval_s)
