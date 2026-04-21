#!/bin/bash
set -e

# =============================================================================
# Install PiCamStream as a systemd service
# =============================================================================

SERVICE_NAME="picamstream"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
RUN_USER="$(whoami)"

echo "Installing ${SERVICE_NAME} systemd service..."
echo "  Working directory: ${SCRIPT_DIR}"
echo "  User: ${RUN_USER}"

# Resolve python path (prefer system python3)
PYTHON_BIN=$(command -v python3)

# Build supplementary groups based on what exists on this system
GROUPS=""
for g in video i2c gpio; do
    if getent group "$g" > /dev/null 2>&1; then
        GROUPS="${GROUPS:+${GROUPS} }${g}"
    fi
done

# Write the unit file
sudo tee "$SERVICE_FILE" > /dev/null <<EOF
[Unit]
Description=PiCamStream Camera Service
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${RUN_USER}
WorkingDirectory=${SCRIPT_DIR}
ExecStart=${PYTHON_BIN} main.py
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal
SyslogIdentifier=${SERVICE_NAME}

# Hardware access
SupplementaryGroups=${GROUPS}

# Hardening
NoNewPrivileges=true
ProtectSystem=strict
ReadWritePaths=${SCRIPT_DIR}
ProtectHome=read-only

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE_NAME"

echo ""
echo "Service installed and enabled."
echo "It will start automatically on next boot."
echo ""
echo "Useful commands:"
echo "  sudo systemctl start ${SERVICE_NAME}      # start now"
echo "  sudo systemctl status ${SERVICE_NAME}      # check status"
echo "  journalctl -u ${SERVICE_NAME} -f           # live logs"
echo "  journalctl -u ${SERVICE_NAME} --since today # today's logs"
