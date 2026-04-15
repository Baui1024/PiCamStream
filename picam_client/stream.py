"""TCP stream server — sends length-prefixed JPEG frames to connected clients.

Protocol (matches the existing inference server's TCPReceiver):
    [4-byte big-endian uint32 length][JPEG payload]

Optionally wraps the socket in TLS for encrypted transport.
"""

import asyncio
import socket
import ssl
import struct
from pathlib import Path
from typing import Optional

from loguru import logger

from .capture import Camera
from .config import (
    STREAM_HOST,
    STREAM_PORT,
    TLS_ENABLED,
    TLS_CERT_FILE,
    TLS_KEY_FILE,
    TLS_REQUIRE_CLIENT_CERT,
    TLS_CA_FILE,
    CAMERA_FPS,
)


class StreamServer:
    """Async TCP server that pushes camera frames to every connected client."""

    def __init__(self, camera: Camera):
        self._camera = camera
        self._server: Optional[asyncio.AbstractServer] = None
        self._clients: set[asyncio.StreamWriter] = set()
        self._running = False

    async def start(self) -> None:
        """Start listening for inference-server connections."""
        ssl_ctx = self._build_ssl_context() if TLS_ENABLED else None

        self._server = await asyncio.start_server(
            self._handle_client,
            host=STREAM_HOST,
            port=STREAM_PORT,
            ssl=ssl_ctx,
        )
        self._running = True

        proto = "TLS" if TLS_ENABLED else "TCP"
        logger.info(f"Stream server listening on {proto} {STREAM_HOST}:{STREAM_PORT}")

    async def stop(self) -> None:
        """Shut down the server and disconnect all clients."""
        self._running = False

        for writer in list(self._clients):
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass
        self._clients.clear()

        if self._server:
            self._server.close()
            await self._server.wait_closed()
        logger.info("Stream server stopped")

    async def broadcast_loop(self) -> None:
        """Continuously capture frames and push to all connected clients."""
        frame_interval = 1.0 / CAMERA_FPS
        frame_count = 0
        slow_clients: set[asyncio.StreamWriter] = set()  # Track backed-up clients

        while self._running:
            if not self._clients:
                # No clients — sleep briefly and retry
                await asyncio.sleep(0.1)
                continue

            jpeg = await asyncio.get_event_loop().run_in_executor(
                None, self._camera.get_frame
            )
            if jpeg is None:
                await asyncio.sleep(0.05)
                continue

            header = struct.pack(">I", len(jpeg))
            payload = header + jpeg

            frame_count += 1
            if frame_count <= 50 or frame_count % 200 == 0:
                logger.info(
                    f"Frame #{frame_count}: {len(jpeg)} bytes → "
                    f"{len(self._clients)} client(s)"
                )

            # Send to every connected client; skip slow clients until they catch up
            disconnected: list[asyncio.StreamWriter] = []
            for writer in list(self._clients):
                # Skip clients that are backed up
                if writer in slow_clients:
                    # Check if they've caught up (drain succeeds instantly)
                    try:
                        await asyncio.wait_for(writer.drain(), timeout=0.001)
                        slow_clients.discard(writer)
                        logger.debug("Slow client recovered")
                    except asyncio.TimeoutError:
                        continue  # Still backed up, skip this frame
                    except Exception:
                        disconnected.append(writer)
                        continue

                try:
                    writer.write(payload)
                    await asyncio.wait_for(writer.drain(), timeout=0.05)  # Tighter timeout
                except asyncio.TimeoutError:
                    # Mark as slow - will skip frames until caught up
                    slow_clients.add(writer)
                    logger.debug("Client backing up, skipping frames")
                except Exception:
                    disconnected.append(writer)

            for writer in disconnected:
                self._clients.discard(writer)
                try:
                    writer.close()
                except Exception:
                    pass
                logger.warning(f"Client disconnected (dropped). Active: {len(self._clients)}")

            await asyncio.sleep(frame_interval)

    # --- internal -----------------------------------------------------------

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        addr = writer.get_extra_info("peername")
        logger.info(f"Client connected: {addr}")

        # Disable Nagle's algorithm for lower latency
        sock = writer.get_extra_info("socket")
        if sock:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            # Limit send buffer to ~2 frames worth to prevent latency buildup
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 131072)

        self._clients.add(writer)

        try:
            # Keep the handler alive until the client disconnects
            while self._running:
                data = await reader.read(1024)
                if not data:
                    break
        except Exception:
            pass
        finally:
            self._clients.discard(writer)
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass
            logger.info(f"Client disconnected: {addr}. Active: {len(self._clients)}")

    @staticmethod
    def _build_ssl_context() -> ssl.SSLContext:
        """Create a server-side TLS context."""
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2

        cert = Path(TLS_CERT_FILE)
        key = Path(TLS_KEY_FILE)
        if not cert.exists() or not key.exists():
            raise FileNotFoundError(
                f"TLS enabled but cert/key not found: {cert}, {key}\n"
                "Generate with: openssl req -x509 -newkey rsa:2048 "
                "-keyout key.pem -out cert.pem -days 365 -nodes -subj '/CN=picam'"
            )
        ctx.load_cert_chain(certfile=str(cert), keyfile=str(key))

        if TLS_REQUIRE_CLIENT_CERT:
            ca = Path(TLS_CA_FILE)
            if not ca.exists():
                raise FileNotFoundError(f"mTLS enabled but CA file not found: {ca}")
            ctx.verify_mode = ssl.CERT_REQUIRED
            ctx.load_verify_locations(cafile=str(ca))
            logger.info("mTLS enabled — client certificate required")
        else:
            ctx.verify_mode = ssl.CERT_NONE

        logger.info(f"TLS context loaded (cert={cert}, key={key})")
        return ctx
