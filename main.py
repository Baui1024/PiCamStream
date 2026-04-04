"""Raspberry Pi camera streaming client.

Captures frames via picamera2 and serves them over TCP (optionally TLS)
using the same length-prefixed JPEG protocol the inference server expects.
"""

import asyncio
import signal
import sys

from loguru import logger

from picam_client.capture import Camera
from picam_client.stream import StreamServer
from picam_client.settings_server import SettingsServer
from picam_client.config import (
    LOG_LEVEL,
    TLS_ENABLED,
    STREAM_HOST,
    STREAM_PORT,
    SETTINGS_WS_PORT,
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

    # Run broadcast loop until shutdown
    broadcast_task = asyncio.create_task(server.broadcast_loop())

    try:
        await stop.wait()
    except KeyboardInterrupt:
        pass

    broadcast_task.cancel()
    try:
        await broadcast_task
    except asyncio.CancelledError:
        pass

    await settings_server.stop()
    await server.stop()
    camera.stop()
    logger.info("Shutdown complete")


if __name__ == "__main__":
    asyncio.run(main())
