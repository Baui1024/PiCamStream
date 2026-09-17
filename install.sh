#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ANSWER_FILE="${SCRIPT_DIR}/.install_answers"

# =============================================================================
# Helpers
# =============================================================================
pkg_exists() { apt-cache show "$1" > /dev/null 2>&1; }
has_build_dir() { [ -d "/lib/modules/$1/build" ]; }

# Newest kernel installed on this system that has usable headers
newest_kernel_with_headers() {
    local m
    for m in /lib/modules/*; do
        [ -d "$m/build" ] && basename "$m"
    done | sort -V | tail -n 1
}

# =============================================================================
# User prompts (answers are remembered so a re-run after reboot is silent)
# =============================================================================
if [ -f "$ANSWER_FILE" ]; then
    # shellcheck disable=SC1090
    source "$ANSWER_FILE"
    echo "Using saved answers from ${ANSWER_FILE} (delete it to change them)."
    echo "  USB WiFi: $USE_USB_WIFI   TLS: $USE_TLS"
else
    read -rp "Are you using USB WiFi? (y/n): " USE_USB_WIFI
    read -rp "Enable TLS encrypted streaming? (y/n): " USE_TLS
    printf 'USE_USB_WIFI=%q\nUSE_TLS=%q\n' "$USE_USB_WIFI" "$USE_TLS" > "$ANSWER_FILE"
fi

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

# =============================================================================
# Kernel headers for the RUNNING kernel
#
# The VEYE driver is an out-of-tree kernel module: it must be compiled against
# /lib/modules/$(uname -r)/build, i.e. the headers of the kernel that is booted
# right now -- not just "some" kernel headers package.
#
# Raspberry Pi OS ships one kernel flavour per model/bitness:
#   v6   -> Pi 1 / Zero / Zero W            (armel)
#   v7   -> Pi 2 / 3 / Zero 2 W, 32-bit     (armhf)   <-- Pi Zero 2 W default
#   v7l  -> Pi 4 / 400, 32-bit              (armhf)
#   v8   -> Pi 3 and newer, 64-bit          (arm64)
#   2712 -> Pi 5                            (arm64)
# linux-headers-rpi-v8 is installable on armhf too, so hardcoding it on a v7
# kernel silently gives headers that can never match uname -r.
# =============================================================================
KVER="$(uname -r)"          # e.g. 6.12.47+rpt-rpi-v7
KFLAVOR="${KVER##*-}"       # e.g. v7

echo "Running kernel: ${KVER} (flavour: ${KFLAVOR})"

if ! has_build_dir "$KVER"; then
    if pkg_exists "linux-headers-${KVER}"; then
        # Exact match for the running kernel -> no reboot required
        sudo apt install -y "linux-headers-${KVER}"
    elif pkg_exists "linux-headers-rpi-${KFLAVOR}"; then
        # Metapackage: pulls headers (and image) for the NEWEST kernel of this
        # flavour, which may be newer than the one currently booted.
        sudo apt install -y "linux-image-rpi-${KFLAVOR}" "linux-headers-rpi-${KFLAVOR}"
    elif pkg_exists raspberrypi-kernel-headers; then
        # Bullseye and older
        sudo apt install -y raspberrypi-kernel raspberrypi-kernel-headers
    else
        echo "ERROR: no kernel headers package found for ${KVER} (flavour ${KFLAVOR})." >&2
        echo "       Try: sudo apt install linux-headers-rpi-${KFLAVOR}" >&2
        exit 1
    fi
fi

# =============================================================================
# Reboot gate
# =============================================================================
if ! has_build_dir "$KVER"; then
    AVAILABLE="$(newest_kernel_with_headers)"
    echo ""
    echo "============================================================"
    echo "  Cannot build the camera driver for the running kernel."
    echo ""
    echo "    running kernel : ${KVER}"
    echo "    headers found  : ${AVAILABLE:-none}"
    echo ""
    if [ -n "$AVAILABLE" ] && [ "${AVAILABLE##*-}" != "$KFLAVOR" ]; then
        echo "  The installed headers are for a different kernel FLAVOUR."
        echo "  This Pi runs ${KFLAVOR}, so it needs linux-headers-rpi-${KFLAVOR}"
        echo "  and linux-image-rpi-${KFLAVOR}."
    else
        echo "  The kernel was upgraded, but the old kernel is still running."
    fi
    echo ""
    echo "  Reboot, then run this script again:"
    echo "      sudo reboot"
    echo "      ${SCRIPT_DIR}/install.sh"
    echo "  (your answers are remembered, it will not ask again)"
    echo "============================================================"
    echo ""
    read -rp "Reboot now? (y/n): " DO_REBOOT
    if [[ "$DO_REBOOT" =~ ^[Yy] ]]; then
        sudo reboot
    fi
    exit 1
fi

echo "Kernel headers OK: /lib/modules/${KVER}/build"

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
pip install --break-system-packages picamera2 loguru websockets smbus2

# =============================================================================
# VEYE/IMX462 camera driver (built from source against the running kernel)
# =============================================================================

# Build dependencies (build-essential = gcc, device-tree-compiler = dtc)
sudo apt install -y git bc bison flex libssl-dev make build-essential device-tree-compiler

# Clone driver repo
VEYE_DIR="$HOME/raspberrypi_v4l2"
if [ -d "$VEYE_DIR" ]; then
    git -C "$VEYE_DIR" pull
else
    git clone https://github.com/veyeimaging/raspberrypi_v4l2.git "$VEYE_DIR"
fi

# Map kernel version to driver source folder
KREST="${KVER#*.}"
KMAJMIN="${KVER%%.*}.${KREST%%.*}"   # 6.12.47+rpt-rpi-v7 -> 6.12

# Newest rpi-<maj>.<min>.y directory in $1 that is <= $2. Used when the running
# kernel is newer than anything VEYE ships source for.
newest_src_dir_upto() {
    local base="$1" want="$2" best="" d v
    for d in "$base"/rpi-*.y; do
        [ -d "$d" ] || continue
        v="$(basename "$d")"; v="${v#rpi-}"; v="${v%.y}"
        case "$v" in *[!0-9.]*) continue ;; esac   # skip rpi-6.1.y-bookworm etc.
        [ "$(printf '%s\n%s\n' "$v" "$want" | sort -V | head -n 1)" = "$v" ] || continue
        if [ -z "$best" ] || [ "$(printf '%s\n%s\n' "$best" "$v" | sort -V | tail -n 1)" = "$v" ]; then
            best="$v"
        fi
    done
    [ -n "$best" ] && echo "rpi-${best}.y"
}

case "$KMAJMIN" in
    6.12) DRV_DIR="rpi-6.12.y" ; DTS_DIR="rpi-6.12.y" ;;
    6.6)  DRV_DIR="rpi-6.6.y"  ; DTS_DIR="rpi-6.6.y"  ;;
    6.1)  DRV_DIR="rpi-6.1.y"  ; DTS_DIR="rpi-6.1.y-bookworm" ;;
    5.15) DRV_DIR="rpi-5.15_all"; DTS_DIR="rpi-5.15.y" ;;
    5.10) DRV_DIR="rpi-5.x_all" ; DTS_DIR="rpi-5.10.y" ;;
    5.4)  DRV_DIR="rpi-5.x_all" ; DTS_DIR="rpi-5.4_all" ;;
    *)
        # No exact match. Fall back to the newest older source tree and try it;
        # the in-kernel V4L2 API changes between releases, so this may not build.
        DRV_DIR="$(newest_src_dir_upto "$VEYE_DIR/driver_source/cam_drv_src" "$KMAJMIN")"
        DTS_DIR="$(newest_src_dir_upto "$VEYE_DIR/driver_source/dts" "$KMAJMIN")"
        if [ -z "$DRV_DIR" ] || [ -z "$DTS_DIR" ]; then
            echo "ERROR: kernel $KMAJMIN is older than any VEYE driver source." >&2
            exit 1
        fi
        echo ""
        echo "============================================================"
        echo "  WARNING: kernel $KMAJMIN is newer than any driver source"
        echo "  published by VEYE (newest available: ${DRV_DIR#rpi-})."
        echo ""
        echo "  Falling back to $DRV_DIR. If the V4L2 kernel API changed in"
        echo "  between, the build below will fail. In that case, run the"
        echo "  camera on a kernel VEYE supports:"
        echo ""
        echo "      apt list -a linux-image-rpi-${KFLAVOR}   # older versions still in the archive?"
        echo "      sudo apt install linux-image-rpi-${KFLAVOR}=<older-version>"
        echo "      sudo apt-mark hold linux-image-rpi-${KFLAVOR}"
        echo "      sudo reboot && ${SCRIPT_DIR}/install.sh"
        echo ""
        echo "  Or check https://github.com/veyeimaging/raspberrypi_v4l2 for"
        echo "  a newer source tree."
        echo "============================================================"
        echo ""
        ;;
esac

echo "Kernel $KVER -> driver=$DRV_DIR, dts=$DTS_DIR"

if [ ! -d "$VEYE_DIR/driver_source/cam_drv_src/$DRV_DIR" ]; then
    echo "ERROR: $VEYE_DIR/driver_source/cam_drv_src/$DRV_DIR does not exist." >&2
    echo "       The VEYE repo has no driver source for kernel $KMAJMIN." >&2
    exit 1
fi

# Compile drivers
cd "$VEYE_DIR/driver_source/cam_drv_src/$DRV_DIR"
make clean || true
make

# Install driver modules
MOD_DIR="/lib/modules/$KVER/kernel/drivers/media/i2c"
sudo mkdir -p "$MOD_DIR"
sudo cp ./*.ko "$MOD_DIR/"
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
sudo cp ./*.dtbo "$OVERLAY_DIR/"

# The modules are installed under /lib/modules/$KVER only. A later kernel
# upgrade would leave the camera without a driver until this script is re-run,
# so pin the kernel. Undo with:
#   sudo apt-mark unhold linux-image-rpi-<flavour> linux-headers-rpi-<flavour>
if pkg_exists "linux-headers-rpi-${KFLAVOR}"; then
    sudo apt-mark hold "linux-image-rpi-${KFLAVOR}" "linux-headers-rpi-${KFLAVOR}" || true
    echo "Kernel packages held at ${KVER} (driver modules are built for this version)."
fi

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

# VEYE I2C control tools no longer needed at runtime (smbus2 replaces them)
# but keep the repo around for manual debugging with veye_mipi_i2c.sh

# =============================================================================
# IR illuminator PWM (GPIO12 -> AL8860 CTRL)
# =============================================================================

# Route GPIO12 to PWM0 (ALT0). Verify after reboot with "pinctrl get 12" -> a0.
if [ -n "$CONFIG_TXT" ] && ! grep -q "^dtoverlay=pwm,pin=12" "$CONFIG_TXT"; then
    echo "dtoverlay=pwm,pin=12,func=4" | sudo tee -a "$CONFIG_TXT"
fi

# Sysfs PWM is root-only by default. Hand /sys/class/pwm to the gpio group so
# picamstream can dim the illuminator without running privileged —
# install_service.sh already puts the service user in that group.
PWM_RULES="/etc/udev/rules.d/99-picamstream-pwm.rules"
if [ ! -f "$PWM_RULES" ]; then
    sudo tee "$PWM_RULES" > /dev/null <<'EOF'
# Give the gpio group access to sysfs PWM so PiCamStream can drive the IR LEDs.
SUBSYSTEM=="pwm*", PROGRAM="/bin/sh -c '\
    chown -R root:gpio /sys/class/pwm && chmod -R 770 /sys/class/pwm ; \
    chown -R root:gpio /sys/devices/platform/soc/*.pwm/pwm/pwmchip* && \
    chmod -R 770 /sys/devices/platform/soc/*.pwm/pwm/pwmchip* \
'"
EOF
    sudo udevadm control --reload-rules || true
    echo "Installed udev rule for sysfs PWM access: $PWM_RULES"
fi

if ! getent group gpio > /dev/null 2>&1; then
    echo "WARNING: no 'gpio' group on this system — the IR LED PWM udev rule"
    echo "         will not grant access. Create the group and add $USER to it."
fi

# Disable onboard WiFi if using USB WiFi
if [[ "$USE_USB_WIFI" =~ ^[Yy] ]]; then
    if [ -n "$CONFIG_TXT" ] && ! grep -q "^dtoverlay=disable-wifi" "$CONFIG_TXT"; then
        echo "dtoverlay=disable-wifi" | sudo tee -a "$CONFIG_TXT"
    fi
fi

cd "$SCRIPT_DIR"

# =============================================================================
# TLS setup (if requested)
# =============================================================================
CONFIG_PY="${SCRIPT_DIR}/picam_client/config.py"
if [[ "$USE_TLS" =~ ^[Yy] ]]; then
    bash "${SCRIPT_DIR}/generate_certs.sh"
    sed -i 's/^TLS_ENABLED = .*/TLS_ENABLED = True/' "$CONFIG_PY"
    echo "TLS enabled in picam_client/config.py"
    echo "  -> set \"use_tls\": true for this camera in the InferenceServer's cameras.json"
else
    sed -i 's/^TLS_ENABLED = .*/TLS_ENABLED = False/' "$CONFIG_PY"
    echo "TLS disabled in picam_client/config.py"
    echo "  -> set \"use_tls\": false for this camera in the InferenceServer's cameras.json"
fi

# Install systemd service
bash "${SCRIPT_DIR}/install_service.sh"

echo "Installation complete. Rebooting in 5 seconds..."
sleep 5
sudo reboot
