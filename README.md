# PiCam Stream

Raspberry Pi camera streaming client for the inference server. Captures frames
via `picamera2` and serves them over TCP (with optional TLS) using the same
length-prefixed JPEG protocol the ESP32 firmware uses.

## Requirements

- Raspberry Pi Zero 2 W (or any Pi with camera port)
- Raspberry Pi Camera Module (v2/v3 or compatible)
- Python 3.11+ (pre-installed on Raspberry Pi OS)
- `picamera2` (pre-installed on Raspberry Pi OS)

## Setup

```bash
# On the Raspberry Pi:
cd PiCamStream
pip install .
```

## Usage

```bash
python main.py
```

The inference server connects to this Pi the same way it connects to the ESP32 —
just update `ESP32_HOST` in the inference server's `config.py` to point at the
Pi's IP address.

## TLS (encrypted streaming)

Generate a self-signed certificate:

```bash
mkdir -p certs
openssl req -x509 -newkey rsa:2048 \
  -keyout certs/key.pem -out certs/cert.pem \
  -days 365 -nodes -subj "/CN=picam"
```

Then in `picam_client/config.py`, set:

```python
TLS_ENABLED = True
```

On the inference server side, update `TCPReceiver` to wrap the connection in
`ssl.create_default_context()` with the Pi's cert as the CA (or disable
verification for self-signed certs during development).

## Mutual TLS (mTLS)

For full mutual authentication, also generate a client cert for the inference
server and set `TLS_REQUIRE_CLIENT_CERT = True` in the Pi's config.

## Configuration

All settings are in `picam_client/config.py`:

| Setting | Default | Description |
|---------|---------|-------------|
| `CAMERA_RESOLUTION` | `(640, 480)` | Capture resolution |
| `CAMERA_FPS` | `30` | Frame rate cap |
| `JPEG_QUALITY` | `80` | JPEG compression (1-100) |
| `STREAM_PORT` | `8081` | TCP port for inference server |
| `TLS_ENABLED` | `False` | Enable TLS encryption |
