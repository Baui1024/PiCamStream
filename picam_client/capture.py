"""Pi camera frame capture using picamera2."""

import threading
import time
from typing import Any, Optional

import cv2
import numpy as np
from loguru import logger
from picamera2 import Picamera2

from .config import (
    CAMERA_RESOLUTION,
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
    ENCODE_FORMAT,
    H264_BITRATE,
    H264_KEYFRAME_PERIOD,
)


class FrameBuffer:
    """Thread-safe single-frame buffer for encoded output (JPEG or H.264)."""

    def __init__(self):
        self.frame: Optional[bytes] = None
        self._latest_keyframe: Optional[bytes] = None
        self.condition = threading.Condition()

    def update(self, data: bytes, keyframe: bool = True) -> None:
        with self.condition:
            self.frame = data
            if keyframe:
                self._latest_keyframe = data
            self.condition.notify_all()

    def wait_for_frame(self, timeout: float = 2.0) -> Optional[bytes]:
        """Block until a new frame is available."""
        with self.condition:
            self.condition.wait(timeout=timeout)
            return self.frame

    def get_latest_keyframe(self) -> Optional[bytes]:
        """Get latest keyframe (H.264) or latest frame (JPEG)."""
        with self.condition:
            return self._latest_keyframe


if ENCODE_FORMAT == "h264":
    from picamera2.encoders import H264Encoder
    from picamera2.outputs import Output as _PicamOutput

    class H264Output(_PicamOutput):
        """Feeds H.264 access units from the hardware encoder into a FrameBuffer."""

        def __init__(self, buffer: FrameBuffer):
            super().__init__()
            self._buffer = buffer

        def outputframe(self, frame, keyframe=False, timestamp=None, packet=None, audio=None):
            self._buffer.update(bytes(frame), keyframe=keyframe)


class Camera:
    """Wraps picamera2 to produce encoded frames (JPEG or H.264 hardware)."""

    def __init__(self):
        self._picam: Optional[Picamera2] = None
        self._buffer = FrameBuffer()
        self._running = False
        self._capture_thread: Optional[threading.Thread] = None
        self._settings_lock = threading.Lock()
        
        # Runtime-adjustable settings (initialized from config)
        self._ir_mode = IR_NIGHT_MODE
        self._clahe_enabled = IR_CLAHE_ENABLED
        self._ae_enable = CAMERA_AE_ENABLE
        self._exposure_time = CAMERA_EXPOSURE_TIME
        self._analogue_gain = CAMERA_ANALOGUE_GAIN
        self._jpeg_quality = JPEG_QUALITY
        self._encoder = None
        self._h264_output = None

    def start(self) -> None:
        """Initialize camera and begin capturing."""
        self._picam = Picamera2()
        width, height = CAMERA_RESOLUTION

        if ENCODE_FORMAT == "h264":
            config = self._picam.create_video_configuration(
                main={"size": CAMERA_RESOLUTION},
                transform=self._build_transform(),
            )
            self._picam.configure(config)

            self._encoder = H264Encoder(
                bitrate=H264_BITRATE,
                iperiod=H264_KEYFRAME_PERIOD,
            )
            self._h264_output = H264Output(self._buffer)
            self._picam.start_recording(self._encoder, self._h264_output)
            self._apply_exposure_settings()

            self._running = True
            logger.info(
                f"Camera started (H.264 HW): {width}x{height} @ {CAMERA_FPS}fps, "
                f"bitrate={H264_BITRATE // 1000}kbps, "
                f"keyframe every {H264_KEYFRAME_PERIOD} frames"
            )
        else:
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
                f"Camera started (MJPEG): {width}x{height} @ {CAMERA_FPS}fps, "
                f"JPEG quality={self._jpeg_quality}, {mode_str}"
            )

    def _apply_exposure_settings(self) -> None:
        """Apply current exposure settings to camera."""
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
            logger.info(
                f"Manual exposure: {self._exposure_time}us, gain={self._analogue_gain}"
            )

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
            if exposure_changed and self._picam:
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
            
            if self._picam:
                self._apply_exposure_settings()
                
        return self.get_settings()

    def _capture_loop(self) -> None:
        """Continuously capture, process, and encode frames."""
        while self._running:
            try:
                # Get current settings (thread-safe)
                with self._settings_lock:
                    ir_mode = self._ir_mode
                    clahe_enabled = self._clahe_enabled
                    jpeg_quality = self._jpeg_quality
                
                encode_params = [cv2.IMWRITE_JPEG_QUALITY, jpeg_quality]
                
                # Capture raw frame (RGB888)
                frame = self._picam.capture_array("main")
                
                # Apply IR night mode processing
                frame = self._process_ir_mode(frame, ir_mode, clahe_enabled)
                
                # Encode to JPEG
                success, jpeg_data = cv2.imencode(".jpg", frame, encode_params)
                if success:
                    self._buffer.update(jpeg_data.tobytes())
                    
            except Exception as e:
                if self._running:
                    logger.error(f"Capture error: {e}")
                    time.sleep(0.1)

    @staticmethod
    def _process_ir_mode(
        frame: np.ndarray, ir_mode: str, clahe_enabled: bool
    ) -> np.ndarray:
        """Apply IR night vision processing based on settings."""
        if ir_mode == "off":
            # Convert RGB to BGR for cv2.imencode
            return cv2.cvtColor(frame, cv2.COLOR_RGB2BGR)
        
        elif ir_mode == "blue_channel":
            # Extract blue channel (best for IR with OV5647 Bayer filter)
            # RGB888 format: frame[:,:,2] is blue
            blue = frame[:, :, 2]
            # Apply histogram equalization for better contrast
            blue = cv2.equalizeHist(blue)
            return blue
        
        elif ir_mode == "grayscale":
            # Standard grayscale conversion
            gray = cv2.cvtColor(frame, cv2.COLOR_RGB2GRAY)
            if clahe_enabled:
                clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
                gray = clahe.apply(gray)
            return gray
        
        else:
            logger.warning(f"Unknown IR mode: {ir_mode}, using grayscale")
            return cv2.cvtColor(frame, cv2.COLOR_RGB2GRAY)

    def stop(self) -> None:
        """Stop camera capture and release resources."""
        self._running = False
        if ENCODE_FORMAT == "h264":
            if self._picam:
                self._picam.stop_recording()
                self._picam.close()
                self._picam = None
        else:
            if self._capture_thread:
                self._capture_thread.join(timeout=2.0)
                self._capture_thread = None
            if self._picam:
                self._picam.stop()
                self._picam.close()
                self._picam = None
        logger.info("Camera stopped")

    def get_frame(self, timeout: float = 2.0) -> Optional[bytes]:
        """Get the latest encoded frame (blocks until available)."""
        if not self._running:
            return None
        return self._buffer.wait_for_frame(timeout=timeout)

    def get_latest_keyframe(self) -> Optional[bytes]:
        """Get latest H.264 keyframe for new client initialization."""
        return self._buffer.get_latest_keyframe()

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