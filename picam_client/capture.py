"""Camera frame capture with pluggable backends (picamera2 or V4L2/GStreamer)."""

from __future__ import annotations

import os
import subprocess
import threading
import time
from abc import ABC, abstractmethod
from typing import Any, Optional

import cv2
import numpy as np
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
    IR_NIGHT_MODE,
    IR_CLAHE_ENABLED,
    CAMERA_AE_ENABLE,
    CAMERA_EXPOSURE_TIME,
    CAMERA_ANALOGUE_GAIN,
    V4L2_DEVICE,
    V4L2_FORMAT,
    V4L2_I2C_SCRIPT,
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

        # Runtime-adjustable settings (initialized from config)
        self._ir_mode = IR_NIGHT_MODE
        self._clahe_enabled = IR_CLAHE_ENABLED
        self._ae_enable = CAMERA_AE_ENABLE
        self._exposure_time = CAMERA_EXPOSURE_TIME
        self._analogue_gain = CAMERA_ANALOGUE_GAIN
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
    def _apply_exposure_settings(self) -> None:
        """Apply current exposure settings to camera hardware."""
        pass

    def get_settings(self) -> dict[str, Any]:
        """Get current runtime settings."""
        with self._settings_lock:
            return {
                "ir_mode": self._ir_mode,
                "clahe_enabled": self._clahe_enabled,
                "ae_enable": self._ae_enable,
                "exposure_time": self._exposure_time,
                "analogue_gain": self._analogue_gain,
                "jpeg_quality": self._jpeg_quality,
            }

    def update_settings(self, settings: dict[str, Any]) -> dict[str, Any]:
        """Update runtime settings. Returns the updated settings."""
        with self._settings_lock:
            exposure_changed = False

            if "ir_mode" in settings:
                value = settings["ir_mode"]
                if value in ("off", "grayscale", "blue_channel"):
                    self._ir_mode = value

            if "clahe_enabled" in settings:
                self._clahe_enabled = bool(settings["clahe_enabled"])

            if "ae_enable" in settings:
                self._ae_enable = bool(settings["ae_enable"])
                exposure_changed = True

            if "exposure_time" in settings:
                value = int(settings["exposure_time"])
                if 100 <= value <= 200000:
                    self._exposure_time = value
                    exposure_changed = True

            if "analogue_gain" in settings:
                value = float(settings["analogue_gain"])
                if 1.0 <= value <= 16.0:
                    self._analogue_gain = value
                    exposure_changed = True

            if "jpeg_quality" in settings:
                value = int(settings["jpeg_quality"])
                if 1 <= value <= 100:
                    self._jpeg_quality = value

            # Apply exposure changes to camera hardware
            if exposure_changed:
                self._apply_exposure_settings()

        return self.get_settings()

    def reset_settings(self) -> dict[str, Any]:
        """Reset all settings to config file defaults."""
        with self._settings_lock:
            self._ir_mode = IR_NIGHT_MODE
            self._clahe_enabled = IR_CLAHE_ENABLED
            self._ae_enable = CAMERA_AE_ENABLE
            self._exposure_time = CAMERA_EXPOSURE_TIME
            self._analogue_gain = CAMERA_ANALOGUE_GAIN
            self._jpeg_quality = JPEG_QUALITY
            self._apply_exposure_settings()

        return self.get_settings()


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
        self._apply_exposure_settings()

        self._running = True
        self._capture_thread = threading.Thread(
            target=self._capture_loop, daemon=True
        )
        self._capture_thread.start()

        mode_str = f"IR mode={self._ir_mode}" if self._ir_mode != "off" else "RGB"
        logger.info(
            f"PicamBackend started (MJPEG): {width}x{height} @ {CAMERA_FPS}fps, "
            f"JPEG quality={self._jpeg_quality}, {mode_str}"
        )

    def _apply_exposure_settings(self) -> None:
        """Apply exposure settings via picamera2 controls."""
        if not self._picam:
            return

        if self._ae_enable:
            self._picam.set_controls({"AeEnable": True})
            logger.info("Auto exposure enabled")
        else:
            self._picam.set_controls({
                "AeEnable": False,
                "ExposureTime": self._exposure_time,
                "AnalogueGain": self._analogue_gain,
            })
            logger.info(f"Manual exposure: {self._exposure_time}us, gain={self._analogue_gain}")

    def _capture_loop(self) -> None:
        """Continuously capture, process, and encode frames (MJPEG mode)."""
        while self._running:
            try:
                with self._settings_lock:
                    ir_mode = self._ir_mode
                    clahe_enabled = self._clahe_enabled
                    jpeg_quality = self._jpeg_quality

                encode_params = [cv2.IMWRITE_JPEG_QUALITY, jpeg_quality]
                frame = self._picam.capture_array("main")
                frame = self._process_ir_mode(frame, ir_mode, clahe_enabled)

                success, jpeg_data = cv2.imencode(".jpg", frame, encode_params)
                if success:
                    self._buffer.update(jpeg_data.tobytes())

            except Exception as e:
                if self._running:
                    logger.error(f"Capture error: {e}")
                    time.sleep(0.1)

    @staticmethod
    def _process_ir_mode(frame: np.ndarray, ir_mode: str, clahe_enabled: bool) -> np.ndarray:
        """Apply IR night vision processing."""
        if ir_mode == "off":
            return cv2.cvtColor(frame, cv2.COLOR_RGB2BGR)
        elif ir_mode == "blue_channel":
            blue = frame[:, :, 2]
            return cv2.equalizeHist(blue)
        elif ir_mode == "grayscale":
            gray = cv2.cvtColor(frame, cv2.COLOR_RGB2GRAY)
            if clahe_enabled:
                clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
                gray = clahe.apply(gray)
            return gray
        else:
            return cv2.cvtColor(frame, cv2.COLOR_RGB2GRAY)

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
    
    Captures frames via GStreamer MJPEG pipeline (PyGObject preferred, OpenCV fallback).
    """

    def __init__(self, buffer: FrameBuffer):
        super().__init__(buffer)
        self._cap: Optional[cv2.VideoCapture] = None
        self._pipeline = None
        self._appsink = None
        self._capture_thread: Optional[threading.Thread] = None
        self._i2c_script = V4L2_I2C_SCRIPT.replace("~", "/home/admin")

    def start(self) -> None:
        """Initialize GStreamer pipeline and begin capturing."""
        width, height = CAMERA_RESOLUTION

        # Set camera to B&W mode (IR-CUT always open) for person detection
        self._run_i2c_command("daynightmode", "0xFE")

        self._start_mjpeg(width, height)

    def _start_mjpeg(self, width: int, height: int) -> None:
        """Start MJPEG capture using PyGObject GStreamer (more reliable than OpenCV)."""
        if _gst_available:
            # Use PyGObject for MJPEG - more reliable than OpenCV's GStreamer backend
            # Optionally scale down to reduce JPEG encoding CPU load
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

    def _apply_exposure_settings(self) -> None:
        """Apply exposure settings via VEYE I2C script."""
        if self._ae_enable:
            # Auto exposure: mshutter=0x40
            self._run_i2c_command("mshutter", "0x40")
            logger.info("V4L2Backend: Auto exposure enabled")
        else:
            # Manual exposure - map microseconds to VEYE shutter values
            # VEYE uses predefined shutter speeds, not direct microseconds
            # 0x41=1/30, 0x42=1/60, 0x43=1/120, 0x44=1/240, etc.
            shutter_val = self._map_exposure_to_veye(self._exposure_time)
            self._run_i2c_command("mshutter", shutter_val)
            # AGC for gain control
            agc_val = hex(min(15, max(0, int(self._analogue_gain))))
            self._run_i2c_command("agc", agc_val)
            logger.info(f"V4L2Backend: Manual exposure shutter={shutter_val}, agc={agc_val}")

    @staticmethod
    def _map_exposure_to_veye(exposure_us: int) -> str:
        """Map microseconds exposure to VEYE mshutter value."""
        # VEYE shutter values for NTSC (30fps base)
        # exposure_us -> approximate match
        if exposure_us >= 33333:  # >= 1/30s
            return "0x41"
        elif exposure_us >= 16666:  # >= 1/60s
            return "0x42"
        elif exposure_us >= 8333:  # >= 1/120s
            return "0x43"
        elif exposure_us >= 4166:  # >= 1/240s
            return "0x44"
        elif exposure_us >= 2083:  # >= 1/480s
            return "0x45"
        elif exposure_us >= 1000:  # >= 1/1000s
            return "0x46"
        elif exposure_us >= 500:  # >= 1/2000s
            return "0x47"
        elif exposure_us >= 200:  # >= 1/5000s
            return "0x48"
        elif exposure_us >= 100:  # >= 1/10000s
            return "0x49"
        else:
            return "0x4A"  # 1/50000s

    def _run_i2c_command(self, param: str, value: str) -> bool:
        """Run VEYE I2C control script command."""
        try:
            cmd = f"{self._i2c_script} -w -f {param} -p1 {value}"
            script_dir = os.path.dirname(self._i2c_script)
            result = subprocess.run(
                cmd,
                shell=True,
                capture_output=True,
                text=True,
                timeout=5,
                cwd=script_dir,
            )
            if result.returncode != 0:
                logger.warning(f"I2C command failed: {cmd} -> {result.stderr}")
                return False
            logger.debug(f"I2C command success: {param}={value}")
            return True
        except subprocess.TimeoutExpired:
            logger.error(f"I2C command timeout: {param}={value}")
            return False
        except Exception as e:
            logger.error(f"I2C command error: {e}")
            return False

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