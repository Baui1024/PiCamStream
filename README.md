# PiCamStream

Raspberry Pi camera streaming client for the [InferenceServer](https://github.com/Baui1024/InferenceServer).
Captures frames from a Pi camera and streams them over TCP (with optional TLS) as length-prefixed JPEGs.

Supports two camera backends:

- **picamera2** — Standard Raspberry Pi Camera Module v2/v3
- **V4L2 + GStreamer** — VEYE/IMX462 cameras with direct I2C ISP control

## Hardware Requirements

- Raspberry Pi (Zero 2 W, 3, 4, or 5)
- Supported camera module:
  - Pi Camera v2 or v3 (picamera2 backend), or
  - VEYE IMX462 (V4L2 backend — installed by `install.sh`)
- Raspberry Pi OS (Bookworm or later)

## Quick Start

### 1. Clone the repository

```bash
git clone https://github.com/Baui1024/PiCamStream.git
cd PiCamStream
```

### 2. Run the install script

The install script sets up everything: system packages, GStreamer, Python
dependencies, camera drivers (VEYE/IMX462), and the systemd service.

```bash
chmod +x install.sh
./install.sh
```

> The script will ask if you are using USB WiFi (to disable the onboard radio).
> It reboots the Pi automatically when finished.

**Expect to run it twice.** The first run does a full `apt` upgrade, which
usually installs a newer kernel. The VEYE camera driver must be compiled
against the kernel that is *actually booted*, so if the kernel changed the
script stops and offers to reboot. Run it again after the reboot — your
answers are remembered, so it will not ask the questions a second time.

After the final reboot, PiCamStream starts automatically as a systemd service.

### 3. Add the camera in the Inference Server

Open the InferenceServer web UI and add a new camera pointing to this Pi's
IP address on port **8081**.

## Running Manually (Development)

If you prefer to run without the systemd service:

```bash
cd PiCamStream
python main.py
```

## What the Install Script Does

| Step | Details |
|------|---------|
| System update | `apt update && apt full-upgrade` |
| I2C | Enables I2C bus (needed for VEYE ISP control) |
| GStreamer | Installs full GStreamer stack + Python bindings |
| picamera2 | Installs libcamera dependencies |
| Python packages | picamera2, loguru, websockets, smbus2, opencv, numpy |
| VEYE driver | Clones, compiles, and installs the V4L2 kernel module + device tree overlay |
| Kernel pin | Holds the kernel packages so an upgrade cannot orphan the compiled driver |
| systemd service | Installs and enables `picamstream.service` (via `install_service.sh`) |

## Kernel Notes

The VEYE/IMX462 driver is an out-of-tree kernel module. It is compiled against
`/lib/modules/$(uname -r)/build` and installed into `/lib/modules/$(uname -r)/`,
so it is valid **only for the exact kernel version it was built on**.

Three consequences:

1. **Headers must match the running kernel, flavour included.** Raspberry Pi OS
   ships one flavour per model and bitness — a Pi Zero 2 W runs `v7` on 32-bit
   and `v8` on 64-bit. `linux-headers-rpi-v8` is installable on 32-bit systems
   too, so installing it on a `v7` kernel yields headers that can never match
   `uname -r`. The script derives the flavour from `uname -r` and installs the
   matching package.

2. **The kernel must not be newer than VEYE's driver source.** The
   [VEYE repo](https://github.com/veyeimaging/raspberrypi_v4l2) publishes one
   source tree per kernel series (`rpi-6.1.y`, `rpi-6.6.y`, `rpi-6.12.y`, …).
   Raspberry Pi OS Trixie now ships **6.18**, which VEYE has no source for yet.
   The script falls back to the newest tree it can find and attempts the build
   anyway — the `rpi-6.12.y` sources compile cleanly against 6.18.39 headers
   (verified 2026-09, warnings only). If a future kernel does break the build,
   run the Pi on a supported kernel instead: pin an older
   `linux-image-rpi-<flavour>` from the archive, or flash Bookworm (6.12/6.6).

3. **Kernel upgrades break the camera.** After a successful build the script
   runs `apt-mark hold` on `linux-image-rpi-<flavour>` and
   `linux-headers-rpi-<flavour>`. To take kernel updates again:

   ```bash
   sudo apt-mark unhold linux-image-rpi-v8 linux-headers-rpi-v8   # your flavour
   sudo apt full-upgrade && sudo reboot
   ./install.sh   # rebuild the driver for the new kernel
   ```

### Troubleshooting

```bash
uname -r                                  # running kernel + flavour
ls -d /lib/modules/$(uname -r)/build      # headers present?
ls /lib/modules/*/kernel/drivers/media/i2c/veyecam2m.ko   # driver installed for which kernel?
dmesg | grep -i veye                      # driver probe messages
v4l2-ctl --list-devices                   # camera detected?
```

If `/lib/modules/$(uname -r)/build` is missing, the kernel you booted has no
headers installed — reboot into the newest kernel and re-run `install.sh`.

## systemd Service

The service is installed automatically by `install.sh`. It can also be
installed or reinstalled independently:

```bash
chmod +x install_service.sh
./install_service.sh
```

### Useful commands

```bash
sudo systemctl start picamstream       # start now
sudo systemctl stop picamstream        # stop
sudo systemctl restart picamstream     # restart
sudo systemctl status picamstream      # check status
```

### Logs

Logs go to the systemd journal. No separate log files are needed.

```bash
journalctl -u picamstream -f             # live tail
journalctl -u picamstream --since today  # today's logs
journalctl -u picamstream -b             # since last boot
```

## Configuration

All settings are in `picam_client/config.py`. Edit before running or restart
the service after changes (`sudo systemctl restart picamstream`).

### Camera Backend

| Setting | Default | Description |
|---------|---------|-------------|
| `CAMERA_BACKEND` | `"v4l2"` | `"picam"` for Pi Camera v2/v3, `"v4l2"` for VEYE/IMX462 |

### V4L2 / VEYE Settings (V4L2 backend only)

| Setting | Default | Description |
|---------|---------|-------------|
| `V4L2_DEVICE` | `"/dev/video0"` | V4L2 device path |
| `V4L2_FORMAT` | `"UYVY"` | Raw pixel format from sensor |
| `V4L2_I2C_BUS` | `10` | I2C bus number for ISP control |
| `V4L2_I2C_ADDR` | `0x3B` | I2C device address |

### Image Settings

| Setting | Default | Description |
|---------|---------|-------------|
| `CAMERA_RESOLUTION` | `(1920, 1080)` | Sensor capture resolution |
| `STREAM_RESOLUTION` | `(1136, 640)` | Scaled output resolution (reduces CPU load) |
| `CAMERA_FPS` | `30` | Frame rate cap |
| `JPEG_QUALITY` | `80` | JPEG compression quality (1–100) |
| `CAMERA_ROTATION` | `180` | Rotation in degrees (0, 90, 180, 270) |
| `CAMERA_HFLIP` | `False` | Horizontal flip |
| `CAMERA_VFLIP` | `False` | Vertical flip |

### Network

| Setting | Default | Description |
|---------|---------|-------------|
| `STREAM_HOST` | `"0.0.0.0"` | TCP bind address |
| `STREAM_PORT` | `8081` | TCP port for frame streaming |
| `SETTINGS_WS_PORT` | `8082` | WebSocket port for runtime camera control |

### TLS (Optional)

| Setting | Default | Description |
|---------|---------|-------------|
| `TLS_ENABLED` | `False` | Enable TLS encryption |
| `TLS_CERT_FILE` | `"certs/cert.pem"` | Path to TLS certificate |
| `TLS_KEY_FILE` | `"certs/key.pem"` | Path to TLS private key |
| `TLS_REQUIRE_CLIENT_CERT` | `False` | Require mTLS client certificate |
| `TLS_CA_FILE` | `"certs/ca.pem"` | CA for client cert verification |

To enable TLS, generate a self-signed certificate:

```bash
mkdir -p certs
openssl req -x509 -newkey rsa:2048 \
  -keyout certs/key.pem -out certs/cert.pem \
  -days 365 -nodes -subj "/CN=picam"
```

Then set `TLS_ENABLED = True` in `config.py` and restart the service.

## Streaming Protocol

Frames are sent over TCP as:

```
[4-byte big-endian uint32 length][JPEG payload]
```

The InferenceServer's `RPiTLSReceiver` connects to this stream on port 8081.

## Runtime Camera Control

The settings WebSocket server (port 8082) accepts JSON messages for live
adjustments without restarting the service:

```json
{"type": "get"}
{"type": "set", "data": {"jpeg_quality": 70, "brightness": 128}}
{"type": "reset"}
```

ISP parameters (VEYE backend): `daynightmode`, `mshutter`, `agc`, `denoise`,
`brightness`, `contrast`, `saturation`, `sharppen`, `wdrmode`, `lowlight`, `wbmode`.

Changes are persisted to `isp_settings.json` and reapplied on startup.

## Project Structure

```
PiCamStream/
├── main.py                  # Entry point
├── install.sh               # Full system setup (packages, drivers, service)
├── install_service.sh       # systemd service installer (called by install.sh)
├── pyproject.toml           # Python project metadata & dependencies
└── picam_client/
    ├── config.py            # All configuration constants
    ├── capture.py           # Camera backends (PicamBackend, V4L2Backend)
    ├── stream.py            # TCP/TLS frame streaming server
    ├── settings_server.py   # WebSocket server for runtime settings
    └── isp_settings.py      # VEYE ISP parameter persistence (I2C)
```
