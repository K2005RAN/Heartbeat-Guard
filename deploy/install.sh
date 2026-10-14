#!/usr/bin/env bash
# deploy/install.sh - Idempotent installer for MayaCardio Bedside Device
# Target: Raspberry Pi OS 64-bit (ARM64)
# Python 3.11+ Standard Library ONLY (zero pip/compilation dependencies).

set -euo pipefail

echo "======================================================================"
echo "INSTALLING MAYACARDIO BEDSIDE PIPELINE"
echo "======================================================================"

if [[ $EUID -ne 0 ]]; then
   echo "ERROR: This script must be run as root (use: sudo ./deploy/install.sh)"
   exit 1
fi

APP_DIR="/opt/mayacardio"
CONF_DIR="/etc/mayacardio"
DATA_DIR="/var/lib/mayacardio"
LOG_DIR="/var/log/mayacardio"
BIN_PATH="/usr/local/bin/pipeline"

echo "--> Step 1: Installing base system dependencies (Python 3.11+, SQLite, curl)..."
if command -v apt-get &> /dev/null; then
    apt-get update -qq || true
    apt-get install -y -qq python3 sqlite3 curl
fi

echo "--> Step 2: Creating persistent directories..."
mkdir -p "$APP_DIR" "$CONF_DIR" "$DATA_DIR" "$LOG_DIR"
chmod 755 "$DATA_DIR"

echo "--> Step 3: Copying pipeline codebase to $APP_DIR..."
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cp -r "$SCRIPT_DIR/pipeline" "$APP_DIR/"
cp -r "$SCRIPT_DIR/tools" "$APP_DIR/" 2>/dev/null || true

echo "--> Step 4: Installing CLI binary wrapper at $BIN_PATH..."
cat << 'EOF' > "$BIN_PATH"
#!/usr/bin/env bash
export PYTHONPATH="/opt/mayacardio:${PYTHONPATH:-}"
export MAYACARDIO_DB_PATH="${MAYACARDIO_DB_PATH:-/var/lib/mayacardio/pipeline_data.db}"
export MAYACARDIO_CLOUD_URL="${MAYACARDIO_CLOUD_URL:-http://127.0.0.1:8000}"
export MAYACARDIO_AUTH_TOKEN="${MAYACARDIO_AUTH_TOKEN:-mayacardio_secret_token_2026}"
exec python3 -m pipeline.cli "$@"
EOF
chmod +x "$BIN_PATH"

echo "--> Step 5: Installing default configuration..."
if [[ ! -f "$CONF_DIR/config.toml" ]]; then
    cp "$SCRIPT_DIR/deploy/config.example.toml" "$CONF_DIR/config.toml"
    chmod 644 "$CONF_DIR/config.toml"
fi

echo "--> Step 6: Installing systemd service unit..."
cp "$SCRIPT_DIR/deploy/mayacardio-pipeline.service" /etc/systemd/system/
chmod 644 /etc/systemd/system/mayacardio-pipeline.service

if [[ -f "$SCRIPT_DIR/deploy/mayacardio.logrotate" ]]; then
    cp "$SCRIPT_DIR/deploy/mayacardio.logrotate" /etc/logrotate.d/mayacardio
    chmod 644 /etc/logrotate.d/mayacardio
fi

echo "--> Step 7: Reloading systemd..."
systemctl daemon-reload
systemctl enable mayacardio-pipeline.service

echo "======================================================================"
echo "INSTALLATION COMPLETE!"
echo ""
echo "CLI command 'pipeline' is now available on PATH:"
echo "  pipeline status"
echo "  pipeline export --session <ID> --from device|cloud"
echo "  pipeline results --session <ID>"
echo "  pipeline verify --session <ID>"
echo ""
echo "To start the background daemon immediately:"
echo "  sudo systemctl start mayacardio-pipeline"
echo "To check daemon status and logs:"
echo "  sudo systemctl status mayacardio-pipeline"
echo "  journalctl -u mayacardio-pipeline -f"
echo "======================================================================"
