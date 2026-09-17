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
| IR illuminator | Enables PWM0 on GPIO12 and a udev rule for non-root `/sys/class/pwm` access |
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

`ir_brightness` (0–100) drives the IR illuminator on the same channel:

```json
{"type": "set", "data": {"ir_brightness": 60}}
```

Changes are persisted to `isp_settings.json` (ISP) and `ir_settings.json`
(illuminator) and reapplied on startup.

## IR Illuminator

Four 940 nm SFH 4726BS LEDs in series, driven by an AL8860 hysteretic buck
whose CTRL pin is dimmed by PWM0 on GPIO12. `install.sh` adds
`dtoverlay=pwm,pin=12,func=4` and a udev rule that gives the `gpio` group
access to `/sys/class/pwm`, so the service dims the LEDs without running as
root.

| Setting | Default | Description |
|---------|---------|-------------|
| `IR_LED_ENABLED` | `True` | Set `False` on boards with no illuminator fitted |
| `IR_PWM_CHIP` | `"pwmchip0"` | Falls back to whatever chip exists if this one doesn't |
| `IR_PWM_CHANNEL` | `0` | |
| `IR_PWM_PERIOD_NS` | `2_000_000` | 500 Hz. The AL8860 needs < 500 Hz — never go shorter |
| `IR_LED_SENSE_RESISTOR_OHM` | `1.0` | **Must match the fitted part, see below** |
| `IR_LED_MAX_DUTY_PCT` | `100` | Duty ceiling; brightness 0–100 is scaled into it |
| `IR_LED_DEFAULT_BRIGHTNESS` | `0` | Used until something is persisted |

**Sense resistor.** `I_OUT_NOM = 0.1 / Rs` sets full-scale LED current, and
the two config values above must match the board:

| Rs | Full-duty current | Full-duty power | `IR_LED_MAX_DUTY_PCT` |
|---|---|---|---|
| 1.0 Ω | 100 mA | ~1 W | `100` |
| 0.13 Ω (original) | 760 mA | ~12 W — over the 10 W budget | `13` |

Running the 1 Ω config on a board still fitted with 0.13 Ω would allow ~12 W
and an unverified LED solder-point temperature (junction max 145 °C, Rth(j-sp)
1.6–1.9 K/W). The illuminator logs its ceiling on startup, so check the
journal after swapping boards:

```
IR illuminator ready on pwmchip0/pwm0: 500 Hz, Rs=1 ohm (100 mA at full duty),
capped at 100% duty = 100 mA
```

The AL8860 is only specified as linear from 1% duty, so requests below that
are reported and driven as off. With the cap at 100 the whole 1–100 range is
usable; a lower cap raises that floor proportionally (a 50% cap makes 2% the
minimum).

### Banding and shutter speed

**Short version: run the illuminator at 100% and there is no banding at any
shutter speed.** At full duty the CTRL pin is held high, the LED current is
constant, and there is nothing for the rolling shutter to beat against —
verified banding-free down to 1/120 s. Size the sense resistor so the
brightness you want falls at or near full duty and this whole section stops
mattering.

The rest applies when dimming below 100%.

The illuminator is PWM-dimmed and the sensor has a rolling shutter, so rows
integrate over different time windows. Unless the exposure is a whole number
of PWM periods, rows collect different amounts of light and the image shows
horizontal stripes — worse at higher IR output, because AE shortens the
shutter as the scene brightens.

At 500 Hz (2 ms period) in **PAL**, these are exact and stripe-free:

| `mshutter` | PAL exposure | PWM periods |
|---|---|---|
| `0x41` | 1/25 s (40 ms) | 20 |
| `0x42` | 1/50 s (20 ms) | 10 |
| `0x43` | 1/100 s (10 ms) | 5 |

NTSC has no exact match at 500 Hz (1/30 s = 16.67 periods), which is why
stripes there also *roll* — 500/30 is not an integer, so the pattern shifts
every frame. Use PAL, or move the PWM to 240 Hz for the NTSC family.

A faint residual remains even at exact multiples: the sensor and the Pi run on
independent clocks, so the exposure is only exact to a few hundred ppm. The
relative ripple is roughly `clock_error / duty_cycle` — independent of PWM
frequency and exposure time, so raising the PWM frequency does not help. Only
a higher duty cycle does, which is why the sense resistor is sized for the
brightness actually needed rather than dimming hard from a much larger
full-scale current.

Manual control without the service:

```bash
python -m picam_client.ir_leds 40     # 40%, holds until Ctrl-C
python -m picam_client.ir_leds        # ramp 0 -> 100 -> 0
pinctrl get 12                        # should report a0
```

## Project Structure

```
PiCamStream/
├── main.py                  # Entry point
├── install.sh               # Full system setup (packages, drivers, service)
├── install_service.sh       # systemd service installer (called by install.sh)
├── pyproject.toml           # Python project metadata & dependencies
└── picam_client/
    ├── config.py            # All configuration constants
    ├── capture.py           # Camera backends (PicamBackend, V4L2Backend) + illuminator
    ├── stream.py            # TCP/TLS frame streaming server
    ├── settings_server.py   # WebSocket server for runtime settings
    ├── isp_settings.py      # VEYE ISP parameter persistence (I2C)
    ├── light_sensor.py      # TSL27721 ambient light sensor (I2C)
    └── ir_leds.py           # IR illuminator PWM dimming (AL8860 on GPIO12)
```
