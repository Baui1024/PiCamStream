"""Configuration for Pi camera streaming client."""

# =============================================================================
# Camera Settings
# =============================================================================

# Resolution (width, height)
CAMERA_RESOLUTION = (1920, 1080)

# Framerate cap
CAMERA_FPS = 30

# JPEG quality (1-100, higher = better quality, larger frames)
JPEG_QUALITY = 80

# Encoding format: "mjpeg" (CPU JPEG encoding) or "h264" (hardware H.264)
# H.264 uses the Pi's hardware encoder for much lower CPU usage at 1080p30
# Note: IR night mode processing is only available in MJPEG mode
ENCODE_FORMAT = "h264"

# H.264 encoder bitrate in bits per second (only used when ENCODE_FORMAT = "h264")
H264_BITRATE = 5_000_000

# H.264 keyframe interval in frames (only used when ENCODE_FORMAT = "h264")
# Lower = faster client sync but slightly larger stream
H264_KEYFRAME_PERIOD = 30

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
STREAM_HOST = "192.168.178.173"

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
