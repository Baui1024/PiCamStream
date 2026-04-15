"""Configuration for Pi camera streaming client."""

# =============================================================================
# Camera Backend Selection
# =============================================================================

# Backend: "picam" (picamera2 for standard Pi cameras) or "v4l2" (GStreamer for IMX462/VEYE)
CAMERA_BACKEND = "v4l2"

# =============================================================================
# V4L2/GStreamer Settings (only used when CAMERA_BACKEND = "v4l2")
# =============================================================================

# V4L2 device path
V4L2_DEVICE = "/dev/video0"

# Raw pixel format from sensor (IMX462 outputs UYVY)
V4L2_FORMAT = "UYVY"

# Path to VEYE I2C control script (for ISP settings)
# Clone from: https://github.com/veyeimaging/raspberrypi.git
V4L2_I2C_SCRIPT = "~/raspberrypi_v4l2/i2c_cmd/veye_mipi_i2c.sh"

# =============================================================================
# Camera Settings
# =============================================================================

# Resolution (width, height) - sensor capture resolution
CAMERA_RESOLUTION = (1920, 1080)

# Stream/output resolution (width, height) - scaled before encoding
# Set to None to use CAMERA_RESOLUTION (no scaling)
# Lower resolution reduces CPU load for MJPEG encoding
STREAM_RESOLUTION = (1136, 640)

# Framerate cap
CAMERA_FPS = 30

# JPEG quality (1-100, higher = better quality, larger frames)
JPEG_QUALITY = 80

# Camera rotation (0, 90, 180, 270)
CAMERA_ROTATION = 180

# Horizontal / vertical flip
CAMERA_HFLIP = False
CAMERA_VFLIP = False

# IR Night Vision mode - converts to grayscale for cleaner IR LED illumination
# Options: "off", "grayscale", "blue_channel"
#   - "off": Normal RGB output
#   - "grayscale": Recommended - averages channels to reduce noise
#   - "blue_channel": Noisier - uses only blue channel (where IR signal is strongest
#                     on OV5647, but single-channel amplifies sensor noise)
IR_NIGHT_MODE = "grayscale"

# CLAHE (Contrast Limited Adaptive Histogram Equalization)
# Improves local contrast in night mode, but costs ~10 FPS on Pi 4
IR_CLAHE_ENABLED = True

# =============================================================================
# Exposure Settings
# =============================================================================

# Auto exposure - set False for manual control (recommended for IR night vision)
CAMERA_AE_ENABLE = False

# Manual exposure time in microseconds (only used when AE disabled)
# Lower = darker but less hotspot bloom, Higher = brighter but more saturation
# Typical range: 1000 (1ms) - 100000 (100ms)
CAMERA_EXPOSURE_TIME = 30000

# Analogue gain (only used when AE disabled)
# Higher = brighter but more noise. Typical range: 1.0 - 16.0
CAMERA_ANALOGUE_GAIN = 4.0

# =============================================================================
# TCP Stream Server
# =============================================================================

# Bind address (0.0.0.0 = all interfaces)
STREAM_HOST = "0.0.0.0"

# Port the inference server connects to
STREAM_PORT = 8081

# =============================================================================
# Settings WebSocket Server
# =============================================================================

# WebSocket server for runtime settings adjustment
SETTINGS_WS_HOST = "0.0.0.0"
SETTINGS_WS_PORT = 8082

# =============================================================================
# TLS / Encryption
# =============================================================================

# Enable TLS (requires cert + key files)
TLS_ENABLED = False

# Path to TLS certificate and private key (PEM format)
# Generate self-signed pair:
#   openssl req -x509 -newkey rsa:2048 -keyout key.pem -out cert.pem \
#     -days 365 -nodes -subj "/CN=picam"
TLS_CERT_FILE = "certs/cert.pem"
TLS_KEY_FILE = "certs/key.pem"

# Require the inference server to present a client certificate (mTLS)
TLS_REQUIRE_CLIENT_CERT = False
TLS_CA_FILE = "certs/ca.pem"  # CA that signed the client cert

# =============================================================================
# Logging
# =============================================================================

LOG_LEVEL = "INFO"
