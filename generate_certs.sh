#!/bin/bash
set -e

# =============================================================================
# Generate self-signed TLS certificates for PiCamStream
# =============================================================================

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CERT_DIR="${SCRIPT_DIR}/certs"

echo "Generating TLS certificates in ${CERT_DIR}..."

mkdir -p "$CERT_DIR"

# Get the Pi's hostname for the certificate CN
HOSTNAME=$(hostname)

openssl req -x509 -newkey rsa:2048 \
    -keyout "${CERT_DIR}/key.pem" \
    -out "${CERT_DIR}/cert.pem" \
    -days 3650 \
    -nodes \
    -subj "/CN=${HOSTNAME}"

# Restrict key file permissions
chmod 600 "${CERT_DIR}/key.pem"
chmod 644 "${CERT_DIR}/cert.pem"

echo ""
echo "Certificates generated:"
echo "  Certificate: ${CERT_DIR}/cert.pem"
echo "  Private key: ${CERT_DIR}/key.pem"
echo "  Valid for:   10 years"
echo "  CN:          ${HOSTNAME}"
