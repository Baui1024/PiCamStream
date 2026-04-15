#!/bin/bash
set -e

# =============================================================================
# User prompts
# =============================================================================
read -rp "Are you using USB WiFi? (y/n): " USE_USB_WIFI

# =============================================================================
# Fix corrupted dpkg state if needed
# =============================================================================
if ! sudo dpkg --audit > /dev/null 2>&1; then
    echo "Repairing corrupted dpkg state..."
    sudo rm -rf /var/lib/dpkg/updates
    sudo mkdir -p /var/lib/dpkg/updates
    sudo dpkg --configure -a
fi

sudo apt update -y
sudo apt full-upgrade -y

# Kernel headers (package name differs across Pi OS versions)
if apt-cache show raspberrypi-kernel-headers > /dev/null 2>&1; then
    sudo apt install -y raspberrypi-kernel raspberrypi-kernel-headers
elif apt-cache show linux-headers-rpi-v8 > /dev/null 2>&1; then
    sudo apt install -y linux-headers-rpi-v8
else
    sudo apt install -y linux-headers-$(uname -r) || sudo apt install -y linux-headers-arm64
fi

# =============================================================================
# Enable I2C
# =============================================================================
if command -v raspi-config > /dev/null 2>&1; then
    sudo raspi-config nonint do_i2c 0
else
    # Plain Debian: enable i2c via config.txt and dtparam
    CONFIG_TXT=""
    if [ -f /boot/firmware/config.txt ]; then
        CONFIG_TXT="/boot/firmware/config.txt"
    elif [ -f /boot/config.txt ]; then
        CONFIG_TXT="/boot/config.txt"
    fi
    if [ -n "$CONFIG_TXT" ] && ! grep -q "^dtparam=i2c_arm=on" "$CONFIG_TXT"; then
        echo "dtparam=i2c_arm=on" | sudo tee -a "$CONFIG_TXT"
    fi
fi
sudo modprobe i2c-dev
if ! grep -q "^i2c-dev" /etc/modules; then
    echo "i2c-dev" | sudo tee -a /etc/modules
fi

# =============================================================================
# Common dependencies
# =============================================================================
sudo apt install -y libcap-dev python3-dev

# =============================================================================
# GStreamer (required for V4L2/IMX462 camera backend)
# =============================================================================
sudo apt install -y \
    gstreamer1.0-tools \
    gstreamer1.0-plugins-base \
    gstreamer1.0-plugins-good \
    gstreamer1.0-plugins-bad \
    gstreamer1.0-plugins-ugly \
    libgstreamer1.0-dev \
    libgstreamer-plugins-base1.0-dev \
    libx264-dev \
    libjpeg-dev \
    v4l-utils \
    python3-gi \
    gir1.2-gstreamer-1.0 \
    gir1.2-gst-plugins-base-1.0

# =============================================================================
# Picamera2 dependencies (optional, for standard Pi cameras)
# =============================================================================
sudo apt install -y python3-libcamera python3-kms++ || true

# =============================================================================
# Python packages (system-wide)
# =============================================================================
sudo apt install -y python3-pip python3-numpy python3-opencv
pip install --break-system-packages picamera2 loguru websockets

# =============================================================================
# VEYE/IMX462 camera driver (build from source)
# =============================================================================

# Build dependencies
sudo apt install -y git bc bison flex libssl-dev make

# Verify the build directory exists; abort early if not
if [ ! -d "/lib/modules/$(uname -r)/build" ]; then
    echo ""
    echo "============================================================"
    echo "  Kernel was upgraded but the old kernel is still running."
    echo "  Please reboot the Pi and run this script again."
    echo "============================================================"
    echo ""
    exit 1
fi

# Clone driver repo
VEYE_DIR="$HOME/raspberrypi_v4l2"
if [ -d "$VEYE_DIR" ]; then
    git -C "$VEYE_DIR" pull
else
    git clone https://github.com/veyeimaging/raspberrypi_v4l2.git "$VEYE_DIR"
fi

# Detect kernel version and select matching source directories
KVER=$(uname -r)                         # e.g. 6.6.51+rpt-rpi-v8
KMAJMIN=$(echo "$KVER" | grep -oP '^\d+\.\d+')  # e.g. 6.6

# Map kernel version to driver source folder
case "$KMAJMIN" in
    6.12) DRV_DIR="rpi-6.12.y" ; DTS_DIR="rpi-6.12.y" ;;
    6.6)  DRV_DIR="rpi-6.6.y"  ; DTS_DIR="rpi-6.6.y"  ;;
    6.1)  DRV_DIR="rpi-6.1.y"  ; DTS_DIR="rpi-6.1.y-bookworm" ;;
    5.15) DRV_DIR="rpi-5.15_all"; DTS_DIR="rpi-5.15.y" ;;
    5.10) DRV_DIR="rpi-5.x_all" ; DTS_DIR="rpi-5.10.y" ;;
    5.4)  DRV_DIR="rpi-5.x_all" ; DTS_DIR="rpi-5.4_all" ;;
    *)    echo "ERROR: Unsupported kernel version $KMAJMIN"; exit 1 ;;
esac

echo "Kernel $KVER → driver=$DRV_DIR, dts=$DTS_DIR"

# Compile drivers
cd "$VEYE_DIR/driver_source/cam_drv_src/$DRV_DIR"
make clean || true
make

# Install driver modules
MOD_DIR="/lib/modules/$KVER/kernel/drivers/media/i2c"
sudo mkdir -p "$MOD_DIR"
sudo cp *.ko "$MOD_DIR/"
sudo depmod -a

# Compile device tree overlays
cd "$VEYE_DIR/driver_source/dts/$DTS_DIR"
chmod +x build_dtbo.sh
./build_dtbo.sh

# Install dtbo files
if [ -d /boot/firmware/overlays ]; then
    OVERLAY_DIR="/boot/firmware/overlays"
else
    OVERLAY_DIR="/boot/overlays"
fi
sudo cp *.dtbo "$OVERLAY_DIR/"

# Enable veyecam2m overlay in boot config
CONFIG_TXT=""
if [ -f /boot/firmware/config.txt ]; then
    CONFIG_TXT="/boot/firmware/config.txt"
elif [ -f /boot/config.txt ]; then
    CONFIG_TXT="/boot/config.txt"
fi
if [ -n "$CONFIG_TXT" ] && ! grep -q "^dtoverlay=veyecam2m" "$CONFIG_TXT"; then
    echo "dtoverlay=veyecam2m" | sudo tee -a "$CONFIG_TXT"
fi

# Disable onboard WiFi if using USB WiFi
if [[ "$USE_USB_WIFI" =~ ^[Yy] ]]; then
    if [ -n "$CONFIG_TXT" ] && ! grep -q "^dtoverlay=disable-wifi" "$CONFIG_TXT"; then
        echo "dtoverlay=disable-wifi" | sudo tee -a "$CONFIG_TXT"
    fi
fi

cd "$OLDPWD"

echo "Installation complete. Rebooting in 5 seconds..."
sleep 5
sudo reboot