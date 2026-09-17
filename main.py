"""Raspberry Pi camera streaming client.

Captures frames via picamera2 and serves them over TCP (optionally TLS)
using the same length-prefixed JPEG protocol the inference server expects.
"""

import asyncio
import signal
import sys
from concurrent.futures import ThreadPoolExecutor

from loguru import logger

from picam_client.capture import Camera
from picam_client.stream import StreamServer
from picam_client.settings_server import SettingsServer
from picam_client.light_monitor import LightMonitor
from picam_client.telemetry import TelemetryHub
from picam_client.config import (
    LOG_LEVEL,
    TLS_ENABLED,
    STREAM_HOST,
    STREAM_PORT,
    SETTINGS_WS_PORT,
    LIGHT_SENSOR_ENABLED,
)

# Configure loguru
logger.remove()
logger.add(
    sys.stderr,
    format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | {message}",
    level=LOG_LEVEL,
)


async def main() -> None:
    proto = "TLS" if TLS_ENABLED else "TCP"
    logger.info(f"PiCam Stream — {proto} on {STREAM_HOST}:{STREAM_PORT}")
    logger.info(f"Settings WebSocket on port {SETTINGS_WS_PORT}")

    camera = Camera()
    camera.start()

    server = StreamServer(camera)
    await server.start()


    settings_server = SettingsServer(camera)
    await settings_server.start()

    # One worker for every blocking I2C call, so sensor reads and ISP access
    # are serialised and never land on the event loop or the frame path.
    i2c_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="picam-i2c")

    light = LightMonitor(executor=i2c_pool)
    if LIGHT_SENSOR_ENABLED:
        await light.start()

    telemetry = TelemetryHub(settings_server)
    telemetry.register("light", light.snapshot)

    # Graceful shutdown
    loop = asyncio.get_event_loop()
    stop = asyncio.Event()

    def _signal():
        logger.info("Shutdown signal received")
        stop.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _signal)
        except NotImplementedError:
            pass  # Windows

    # Run broadcast + telemetry loops until shutdown
    broadcast_task = asyncio.create_task(server.broadcast_loop())
    telemetry_task = asyncio.create_task(telemetry.run())

    try:
        await stop.wait()
    except KeyboardInterrupt:
        pass

    # Order matters: the telemetry task must stop before the settings server it
    # writes to, and the light monitor before the executor it dispatches onto.
    for task in (broadcast_task, telemetry_task):
        task.cancel()
    await asyncio.gather(broadcast_task, telemetry_task, return_exceptions=True)

    await light.stop()
    await settings_server.stop()
    await server.stop()
    camera.stop()
    i2c_pool.shutdown(wait=True)
    logger.info("Shutdown complete")


if __name__ == "__main__":
    asyncio.run(main())
