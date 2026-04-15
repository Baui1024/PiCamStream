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

# I2C bus and device address for VEYE ISP control
V4L2_I2C_BUS = 10
V4L2_I2C_ADDR = 0x3B

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
