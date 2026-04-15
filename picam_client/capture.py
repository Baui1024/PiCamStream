"""Camera frame capture with pluggable backends (picamera2 or V4L2/GStreamer)."""

from __future__ import annotations

import threading
import time
from abc import ABC, abstractmethod
from typing import Any, Optional

import cv2
from loguru import logger

from .config import (
    CAMERA_BACKEND,
    CAMERA_RESOLUTION,
    STREAM_RESOLUTION,
    CAMERA_FPS,
    JPEG_QUALITY,
    CAMERA_ROTATION,
    CAMERA_HFLIP,
    CAMERA_VFLIP,
    V4L2_DEVICE,
    V4L2_FORMAT,
)


class FrameBuffer:
    """Thread-safe single-frame buffer for encoded JPEG output."""

    def __init__(self):
        self.frame: Optional[bytes] = None
        self._frame_id: int = 0
        self.condition = threading.Condition()

    def update(self, data: bytes) -> None:
        with self.condition:
            self.frame = data
            self._frame_id += 1
            self.condition.notify_all()

    def wait_for_frame(self, timeout: float = 2.0) -> Optional[bytes]:
        """Block until a NEW frame is available (skips stale frames)."""
        with self.condition:
            seen_id = self._frame_id
            while self._frame_id == seen_id:
                if not self.condition.wait(timeout=timeout):
                    return None
            return self.frame


# =============================================================================
# Abstract Camera Backend
# =============================================================================


class CameraBackend(ABC):
    """Abstract interface for camera capture backends."""

    def __init__(self, buffer: FrameBuffer):
        self._buffer = buffer
        self._running = False
        self._settings_lock = threading.Lock()
        self._jpeg_quality = JPEG_QUALITY

    @abstractmethod
    def start(self) -> None:
        """Initialize camera and begin capturing."""
        pass

    @abstractmethod
    def stop(self) -> None:
        """Stop camera capture and release resources."""
        pass

    @abstractmethod
    def get_settings(self) -> dict[str, Any]:
        """Get current runtime settings."""
        pass

    @abstractmethod
    def update_settings(self, settings: dict[str, Any]) -> dict[str, Any]:
        """Update runtime settings. Returns the updated settings."""
        pass

    @abstractmethod
    def reset_settings(self) -> dict[str, Any]:
        """Reset all settings to defaults."""
        pass


# =============================================================================
# Picamera2 Backend (for standard Pi cameras)
# =============================================================================


class PicamBackend(CameraBackend):
    """Picamera2-based capture for standard Raspberry Pi cameras."""

    def __init__(self, buffer: FrameBuffer):
        super().__init__(buffer)
        self._picam = None
        self._capture_thread: Optional[threading.Thread] = None

    def start(self) -> None:
        """Initialize picamera2 and begin capturing."""
        from picamera2 import Picamera2

        self._picam = Picamera2()
        width, height = CAMERA_RESOLUTION

        config = self._picam.create_video_configuration(
            main={"size": CAMERA_RESOLUTION, "format": "RGB888"},
            transform=self._build_transform(),
        )
        self._picam.configure(config)
        self._picam.start()

        self._running = True
        self._capture_thread = threading.Thread(
            target=self._capture_loop, daemon=True
        )
        self._capture_thread.start()

        logger.info(
            f"PicamBackend started (MJPEG): {width}x{height} @ {CAMERA_FPS}fps, "
            f"JPEG quality={self._jpeg_quality}"
        )

    def _capture_loop(self) -> None:
        """Continuously capture and encode frames."""
        while self._running:
            try:
                with self._settings_lock:
                    jpeg_quality = self._jpeg_quality

                encode_params = [cv2.IMWRITE_JPEG_QUALITY, jpeg_quality]
                frame = self._picam.capture_array("main")
                frame = cv2.cvtColor(frame, cv2.COLOR_RGB2BGR)

                success, jpeg_data = cv2.imencode(".jpg", frame, encode_params)
                if success:
                    self._buffer.update(jpeg_data.tobytes())

            except Exception as e:
                if self._running:
                    logger.error(f"Capture error: {e}")
                    time.sleep(0.1)

    def get_settings(self) -> dict[str, Any]:
        with self._settings_lock:
            return {"jpeg_quality": self._jpeg_quality}

    def update_settings(self, settings: dict[str, Any]) -> dict[str, Any]:
        with self._settings_lock:
            if "jpeg_quality" in settings:
                value = int(settings["jpeg_quality"])
                if 1 <= value <= 100:
                    self._jpeg_quality = value
        return self.get_settings()

    def reset_settings(self) -> dict[str, Any]:
        with self._settings_lock:
            self._jpeg_quality = JPEG_QUALITY
        return self.get_settings()

    def stop(self) -> None:
        """Stop camera capture and release resources."""
        self._running = False
        if self._capture_thread:
            self._capture_thread.join(timeout=2.0)
            self._capture_thread = None
        if self._picam:
            self._picam.stop()
            self._picam.close()
            self._picam = None
        logger.info("PicamBackend stopped")

    @staticmethod
    def _build_transform():
        """Build libcamera Transform from config."""
        from libcamera import Transform

        hflip = CAMERA_HFLIP
        vflip = CAMERA_VFLIP
        transpose = False

        if CAMERA_ROTATION == 90:
            transpose = True
            hflip, vflip = not vflip, hflip
        elif CAMERA_ROTATION == 180:
            hflip = not hflip
            vflip = not vflip
        elif CAMERA_ROTATION == 270:
            transpose = True
            hflip, vflip = vflip, not hflip

        return Transform(hflip=hflip, vflip=vflip, transpose=transpose)


# =============================================================================
# V4L2/GStreamer Backend (for IMX462/VEYE cameras)
# =============================================================================

# Try to import GStreamer Python bindings for MJPEG pipeline
_gst_available = False
try:
    import gi
    gi.require_version('Gst', '1.0')
    gi.require_version('GstApp', '1.0')
    from gi.repository import Gst, GstApp, GLib
    Gst.init(None)
    _gst_available = True
    logger.debug("GStreamer Python bindings (PyGObject) loaded successfully")
except (ImportError, ValueError) as e:
    logger.warning(f"GStreamer Python bindings not available: {e}. Will use OpenCV fallback.")
    Gst = None
    GstApp = None
    GLib = None


class V4L2Backend(CameraBackend):
    """GStreamer-based capture for V4L2 cameras (IMX462/VEYE).
    
    ISP settings are persisted to JSON and managed via I2C.
    """

    def __init__(self, buffer: FrameBuffer):
        super().__init__(buffer)
        self._cap: Optional[cv2.VideoCapture] = None
        self._pipeline = None
        self._appsink = None
        self._capture_thread: Optional[threading.Thread] = None
        self._isp_settings: dict[str, str] = {}

    def start(self) -> None:
        """Initialize ISP settings and GStreamer pipeline."""
        from . import isp_settings

        # Load settings from JSON or query camera on first run
        self._isp_settings = isp_settings.load_or_init()

        width, height = CAMERA_RESOLUTION
        self._start_mjpeg(width, height)

    def _start_mjpeg(self, width: int, height: int) -> None:
        """Start MJPEG capture using PyGObject GStreamer (more reliable than OpenCV)."""
        if _gst_available:
            out_w, out_h = STREAM_RESOLUTION if STREAM_RESOLUTION else (width, height)
            
            if (out_w, out_h) != (width, height):
                scale_elements = (
                    f"videoscale method=nearest-neighbour ! "
                    f"video/x-raw,width={out_w},height={out_h} ! "
                )
            else:
                scale_elements = ""
            
            pipeline_str = (
                f"v4l2src device={V4L2_DEVICE} ! "
                f"video/x-raw,format={V4L2_FORMAT},width={width},height={height},framerate={CAMERA_FPS}/1 ! "
                f"queue max-size-buffers=1 leaky=downstream ! "
                f"{scale_elements}"
                f"videoconvert n-threads=4 ! "
                f"jpegenc quality={self._jpeg_quality} idct-method=ifast ! "
                f"appsink name=sink emit-signals=false drop=true sync=false max-buffers=1"
            )
            logger.info(f"V4L2Backend MJPEG pipeline (PyGObject): {pipeline_str}")
            
            try:
                self._pipeline = Gst.parse_launch(pipeline_str)
                self._appsink = self._pipeline.get_by_name("sink")
                
                ret = self._pipeline.set_state(Gst.State.PLAYING)
                if ret == Gst.StateChangeReturn.FAILURE:
                    raise RuntimeError("Pipeline failed to start")
                
                self._running = True
                self._capture_thread = threading.Thread(
                    target=self._capture_loop_mjpeg_gst, daemon=True
                )
                self._capture_thread.start()
                
                logger.info(
                    f"V4L2Backend started (MJPEG via PyGObject): {width}x{height} → {out_w}x{out_h} @ {CAMERA_FPS}fps, "
                    f"JPEG quality={self._jpeg_quality}, device={V4L2_DEVICE}"
                )
                return
            except Exception as e:
                logger.warning(f"PyGObject MJPEG failed: {e}, trying OpenCV")
                if self._pipeline:
                    self._pipeline.set_state(Gst.State.NULL)
                    self._pipeline = None
        
        # Fallback to OpenCV GStreamer backend
        pipeline = (
            f"v4l2src device={V4L2_DEVICE} ! "
            f"video/x-raw,format={V4L2_FORMAT},width={width},height={height},framerate={CAMERA_FPS}/1 ! "
            f"queue max-size-buffers=2 leaky=downstream ! "
            f"videoconvert ! "
            f"video/x-raw,format=BGR ! "
            f"appsink drop=true sync=false max-buffers=2"
        )
        logger.info(f"V4L2Backend MJPEG pipeline (OpenCV): {pipeline}")

        self._cap = cv2.VideoCapture(pipeline, cv2.CAP_GSTREAMER)
        if not self._cap.isOpened():
            raise RuntimeError(f"Failed to open GStreamer pipeline: {pipeline}")

        self._running = True
        self._capture_thread = threading.Thread(
            target=self._capture_loop_mjpeg, daemon=True
        )
        self._capture_thread.start()

        logger.info(
            f"V4L2Backend started (MJPEG via OpenCV): {width}x{height} @ {CAMERA_FPS}fps, "
            f"JPEG quality={self._jpeg_quality}, device={V4L2_DEVICE}"
        )

    def _capture_loop_mjpeg_gst(self) -> None:
        """Capture JPEG frames via PyGObject GStreamer."""
        logger.info("MJPEG capture thread started (PyGObject)")
        frame_count = 0
        while self._running:
            try:
                sample = self._appsink.try_pull_sample(Gst.SECOND // 10)
                if sample is None:
                    continue
                
                buf = sample.get_buffer()
                success, map_info = buf.map(Gst.MapFlags.READ)
                if success:
                    data = bytes(map_info.data)
                    self._buffer.update(data)
                    buf.unmap(map_info)
                    
                    frame_count += 1
                    if frame_count <= 3:
                        logger.info(f"MJPEG frame {frame_count}: {len(data)} bytes")
                    elif frame_count % 100 == 0:
                        logger.info(f"MJPEG frames captured: {frame_count}")
                        
            except Exception as e:
                if self._running:
                    logger.error(f"MJPEG capture error: {e}")
                    time.sleep(0.1)
        
        logger.info("MJPEG capture thread stopped")

    def _capture_loop_mjpeg(self) -> None:
        """Capture BGR frames, encode to JPEG."""
        while self._running:
            try:
                with self._settings_lock:
                    jpeg_quality = self._jpeg_quality

                ret, frame = self._cap.read()
                if ret and frame is not None:
                    encode_params = [cv2.IMWRITE_JPEG_QUALITY, jpeg_quality]
                    success, jpeg_data = cv2.imencode(".jpg", frame, encode_params)
                    if success:
                        self._buffer.update(jpeg_data.tobytes())
                else:
                    time.sleep(0.01)
            except Exception as e:
                if self._running:
                    logger.error(f"MJPEG capture error: {e}")
                    time.sleep(0.1)

    # -- ISP settings via I2C --

    def get_settings(self) -> dict[str, Any]:
        with self._settings_lock:
            return {
                "jpeg_quality": self._jpeg_quality,
                **self._isp_settings,
            }

    def update_settings(self, settings: dict[str, Any]) -> dict[str, Any]:
        from . import isp_settings

        with self._settings_lock:
            if "jpeg_quality" in settings:
                value = int(settings["jpeg_quality"])
                if 1 <= value <= 100:
                    self._jpeg_quality = value

            # Update ISP params via I2C
            for param, value in settings.items():
                if param in isp_settings.ISP_PARAMS:
                    isp_settings.update_param(
                        self._isp_settings, param, str(value)
                    )

        return self.get_settings()

    def reset_settings(self) -> dict[str, Any]:
        from . import isp_settings

        with self._settings_lock:
            self._jpeg_quality = JPEG_QUALITY
            # Re-query camera for current hardware defaults
            self._isp_settings = isp_settings.query_camera()
            isp_settings.save(self._isp_settings)

        return self.get_settings()

    def stop(self) -> None:
        """Stop GStreamer pipeline and release resources."""
        self._running = False

        if self._capture_thread:
            self._capture_thread.join(timeout=2.0)
            self._capture_thread = None

        if self._pipeline:
            self._pipeline.set_state(Gst.State.NULL)
            self._pipeline = None
            self._appsink = None
        
        if self._cap:
            self._cap.release()
            self._cap = None

        logger.info("V4L2Backend stopped")


# =============================================================================
# Camera Facade (selects backend based on config)
# =============================================================================


class Camera:
    """Camera facade that delegates to the configured backend."""

    def __init__(self):
        self._buffer = FrameBuffer()
        self._backend: Optional[CameraBackend] = None

    def start(self) -> None:
        """Initialize the configured camera backend and begin capturing."""
        if CAMERA_BACKEND == "v4l2":
            self._backend = V4L2Backend(self._buffer)
        else:
            self._backend = PicamBackend(self._buffer)

        self._backend.start()

    def stop(self) -> None:
        """Stop camera capture and release resources."""
        if self._backend:
            self._backend.stop()
            self._backend = None

    def get_frame(self, timeout: float = 2.0) -> Optional[bytes]:
        """Get the latest encoded frame (blocks until available)."""
        if not self._backend or not self._backend._running:
            return None
        return self._buffer.wait_for_frame(timeout=timeout)

    def get_settings(self) -> dict[str, Any]:
        """Get current runtime settings."""
        if self._backend:
            return self._backend.get_settings()
        return {}

    def update_settings(self, settings: dict[str, Any]) -> dict[str, Any]:
        """Update runtime settings."""
        if self._backend:
            return self._backend.update_settings(settings)
        return {}

    def reset_settings(self) -> dict[str, Any]:
        """Reset all settings to config file defaults."""
        if self._backend:
            return self._backend.reset_settings()
        return {}