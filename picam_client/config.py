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

# =============================================================================
# TSL27721 Ambient Light Sensor (custom nightvision board)
# =============================================================================

# I2C bus and address of the TSL27721 (fixed address, 0x39)
LIGHT_SENSOR_I2C_BUS = 1
LIGHT_SENSOR_I2C_ADDR = 0x39

# Glass attenuation factor: 1.0 = bare sensor in open air. If the sensor sits
# behind a window/dome, raise this by the window's transmission loss
# (e.g. 40% transmission -> GA = 2.5). Pure scale factor on the lux output;
# calibrate against a reference meter once the board is in its enclosure.
LIGHT_SENSOR_GLASS_ATTENUATION = 1.0

# =============================================================================
# IR LED Illuminator (AL8860 dimmer on GPIO12 / PWM0)
# =============================================================================

# Needs "dtoverlay=pwm,pin=12,func=4" in /boot/firmware/config.txt (install.sh
# adds it). Check with "pinctrl get 12" — it should report "a0".
IR_PWM_CHIP = "pwmchip0"
IR_PWM_CHANNEL = 0

# PWM period in nanoseconds. The AL8860 wants dimming below 500 Hz; at 500 Hz
# accuracy is better than 1% across the whole 1-100% range. Longer periods are
# fine (5000000 = 200 Hz) and give slightly better low-end resolution — do not
# go shorter than 2000000.
IR_PWM_PERIOD_NS = 2_000_000  # 500 Hz

# AL8860 sense resistor in ohms. Sets full-scale LED current:
#   I_OUT_NOM = 0.1 / Rs
# 1.0 ohm -> 100 mA. Only used to report estimated current, but keep it
# accurate: it is the one place the firmware knows which board it is on.
IR_LED_SENSE_RESISTOR_OHM = 1.0

# Hard ceiling on PWM duty cycle, as a percentage. Brightness is exposed as
# 0-100 to callers and scaled into 0..IR_LED_MAX_DUTY_PCT internally.
#
# 100% is safe with Rs = 1 ohm: 4 LEDs x ~2.6 V x 0.1 A is about 1 W, against
# a 10 W system budget.
#
# !! If this board still has the original Rs = 0.13 ohm (760 mA), set this back
# !! to 13. At full duty that board draws ~12 W, over budget, and the LED
# !! solder-point temperature has never been verified at sustained full output.
IR_LED_MAX_DUTY_PCT = 100

# Set False to leave the PWM channel alone entirely (e.g. boards without the
# illuminator fitted).
IR_LED_ENABLED = True

# Brightness (0-100) used on first start, before anything has been persisted
# to ir_settings.json.
#
# 100 is the banding-free operating point: at full duty the CTRL pin is held
# high, so there is no modulation for the rolling shutter to beat against and
# any exposure works (verified down to 1/120 s). Dimming below 100 brings the
# modulation back, and with it the constraint that the exposure must be a
# whole number of PWM periods — see the README.
IR_LED_DEFAULT_BRIGHTNESS = 0

# =============================================================================
# Light sensor polling and telemetry
# =============================================================================

# Set False on boards with no ambient light sensor fitted.
LIGHT_SENSOR_ENABLED = True

# Gap *between* sensor reads, not the resulting rate. A read blocks for one
# integration period, which is up to ~0.7 s on the most sensitive rung, so in
# near-darkness the effective rate bottoms out around one reading per 2.7 s.
LIGHT_SENSOR_POLL_INTERVAL_S = 2.0

# Backoff before reopening the bus after an I2C failure.
LIGHT_SENSOR_RETRY_INTERVAL_S = 30.0

# Lux our own IR illuminator adds to the sensor per 1% of brightness.
# compute_lux() already subtracts a weighted ch1, so 940 nm cancels to first
# order, but its coefficients are fitted for broadband illuminants and a
# residual remains. Measure per enclosure — see the README calibration steps.
LIGHT_SENSOR_IR_LUX_PER_PCT = 0.0

# How often the camera pushes telemetry to the inference server. Matches the
# server's own 2 s stats broadcast so nothing waits for the following tick.
TELEMETRY_INTERVAL_S = 2.0
