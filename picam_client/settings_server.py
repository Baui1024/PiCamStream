"""WebSocket server for runtime camera settings adjustment."""

import asyncio
import json
from typing import TYPE_CHECKING, Any

from loguru import logger

from .config import SETTINGS_WS_HOST, SETTINGS_WS_PORT

# Try to import websockets
_websockets_available = False
try:
    import websockets
    from websockets.server import serve, WebSocketServerProtocol
    _websockets_available = True
    logger.debug("websockets module loaded successfully")
except ImportError as e:
    logger.error(f"Failed to import websockets: {e}")
    logger.error("Install with: pip install websockets")
    websockets = None  # type: ignore
    WebSocketServerProtocol = Any  # type: ignore
    serve = None  # type: ignore

if TYPE_CHECKING:
    from .capture import Camera


class SettingsServer:
    """WebSocket server that allows real-time adjustment of camera settings."""

    def __init__(self, camera: "Camera"):
        self._camera = camera
        self._server = None
        self._clients: set = set()

    async def start(self) -> None:
        """Start the WebSocket settings server."""
        if not _websockets_available:
            logger.error("WebSocket settings server DISABLED — websockets not installed")
            return

        try:
            self._server = await serve(
                self._handle_client,
                SETTINGS_WS_HOST,
                SETTINGS_WS_PORT,
            )
            logger.info(
                f"Settings WebSocket server listening on ws://{SETTINGS_WS_HOST}:{SETTINGS_WS_PORT}"
            )
        except Exception as e:
            logger.error(f"Failed to start WebSocket server: {e}")

    async def stop(self) -> None:
        """Stop the WebSocket server."""
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            logger.info("Settings server stopped")

    async def _handle_client(self, websocket) -> None:
        """Handle a connected settings client."""
        self._clients.add(websocket)
        client_addr = websocket.remote_address
        logger.info(f"Settings client connected: {client_addr}")

        try:
            # Send current settings on connect
            await websocket.send(json.dumps({
                "type": "settings",
                "data": self._camera.get_settings(),
            }))

            async for message in websocket:
                await self._process_message(websocket, message)

        except websockets.exceptions.ConnectionClosed:
            pass
        except Exception as e:
            logger.error(f"Settings client error: {e}")
        finally:
            self._clients.discard(websocket)
            logger.info(f"Settings client disconnected: {client_addr}")

    async def _process_message(self, websocket, message: str) -> None:
        """Process an incoming settings message."""
        try:
            data = json.loads(message)
            msg_type = data.get("type")

            if msg_type == "get":
                # Return current settings
                await websocket.send(json.dumps({
                    "type": "settings",
                    "data": self._camera.get_settings(),
                }))

            elif msg_type == "set":
                # Update settings
                settings = data.get("data", {})
                result = self._camera.update_settings(settings)
                
                # Broadcast updated settings to all clients
                response = json.dumps({
                    "type": "settings",
                    "data": self._camera.get_settings(),
                })
                await asyncio.gather(
                    *[client.send(response) for client in self._clients],
                    return_exceptions=True,
                )
                
                logger.info(f"Settings updated: {settings}")

            elif msg_type == "reset":
                # Reset to config file defaults
                self._camera.reset_settings()
                response = json.dumps({
                    "type": "settings",
                    "data": self._camera.get_settings(),
                })
                await asyncio.gather(
                    *[client.send(response) for client in self._clients],
                    return_exceptions=True,
                )
                logger.info("Settings reset to defaults")

            else:
                await websocket.send(json.dumps({
                    "type": "error",
                    "message": f"Unknown message type: {msg_type}",
                }))

        except json.JSONDecodeError:
            await websocket.send(json.dumps({
                "type": "error",
                "message": "Invalid JSON",
            }))
        except Exception as e:
            logger.error(f"Error processing settings message: {e}")
            await websocket.send(json.dumps({
                "type": "error",
                "message": str(e),
            }))
