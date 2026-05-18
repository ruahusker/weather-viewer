#!/usr/bin/env bash
# setup.sh — One-shot setup for the weather-viewer on Ubuntu
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_USER="${SUDO_USER:-wx}"

echo "=== Weather Viewer Setup ==="
echo "App directory: $SCRIPT_DIR"
echo "Running as user context: $APP_USER"

# ---------------------------------------------------------------------------
# 1. System dependencies
# ---------------------------------------------------------------------------
echo "[1/7] Installing system packages..."
apt-get update -qq
apt-get install -y --no-install-recommends \
    libgeos-dev libproj-dev libeccodes-dev \
    proj-data proj-bin \
    python3-pip python3-venv \
    nginx

# ---------------------------------------------------------------------------
# 2. Python dependencies
# ---------------------------------------------------------------------------
echo "[2/7] Installing Python packages..."
pip3 install --break-system-packages -r "$SCRIPT_DIR/requirements.txt"

# ---------------------------------------------------------------------------
# 3. Pre-download Cartopy Natural Earth shapefiles
# ---------------------------------------------------------------------------
echo "[3/7] Pre-downloading Cartopy shapefiles..."
python3 - <<'PYEOF'
import cartopy.io.shapereader as shpreader
# Download all needed feature sets
for resolution in ('110m', '50m'):
    for category, name in [
        ('cultural', 'admin_0_countries'),
        ('cultural', 'admin_1_states_provinces'),
        ('physical', 'coastline'),
        ('physical', 'lakes'),
    ]:
        try:
            shpreader.natural_earth(resolution=resolution, category=category, name=name)
            print(f"  OK: {resolution}/{category}/{name}")
        except Exception as e:
            print(f"  WARN: {resolution}/{category}/{name}: {e}")
PYEOF

# ---------------------------------------------------------------------------
# 4. Create data directories
# ---------------------------------------------------------------------------
echo "[4/7] Creating data directories..."
DATA_DIR="$SCRIPT_DIR/data"
mkdir -p "$DATA_DIR/grib_cache/hrrr"
mkdir -p "$DATA_DIR/rendered/hrrr"
mkdir -p "$SCRIPT_DIR/logs"

# Set permissions so www-data (nginx) can read rendered images
chmod -R o+rX "$DATA_DIR/rendered"
chown -R "$APP_USER:$APP_USER" "$DATA_DIR" "$SCRIPT_DIR/logs"

# ---------------------------------------------------------------------------
# 5. Configure herbie
# ---------------------------------------------------------------------------
echo "[5/7] Configuring herbie..."
HERBIE_CFG_DIR="/home/$APP_USER/.config/herbie"
mkdir -p "$HERBIE_CFG_DIR"
cat > "$HERBIE_CFG_DIR/config.toml" <<TOML
[default]
save_dir = "$DATA_DIR/grib_cache"
overwrite = false
priority = ["aws", "nomads"]
verbose = false
TOML
chown -R "$APP_USER:$APP_USER" "$HERBIE_CFG_DIR"
echo "  Written: $HERBIE_CFG_DIR/config.toml"

# ---------------------------------------------------------------------------
# 6. Install systemd services
# ---------------------------------------------------------------------------
echo "[6/7] Installing systemd services..."
cp "$SCRIPT_DIR/systemd/weather-pipeline.service" /etc/systemd/system/
cp "$SCRIPT_DIR/systemd/weather-satellite.service" /etc/systemd/system/
cp "$SCRIPT_DIR/systemd/weather-web.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable weather-pipeline.service weather-satellite.service weather-web.service
echo "  Services enabled (not yet started)"

# ---------------------------------------------------------------------------
# 7. Configure nginx
# ---------------------------------------------------------------------------
echo "[7/7] Configuring nginx..."
cp "$SCRIPT_DIR/nginx/weather.conf" /etc/nginx/sites-available/weather
ln -sf /etc/nginx/sites-available/weather /etc/nginx/sites-enabled/weather
nginx -t && nginx -s reload
echo "  nginx configured and reloaded"

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------
echo ""
echo "=== Setup complete ==="
echo ""
echo "To start the services:"
echo "  sudo systemctl start weather-pipeline weather-satellite weather-web"
echo ""
echo "To check status:"
echo "  sudo systemctl status weather-pipeline weather-satellite weather-web"
echo "  tail -f $SCRIPT_DIR/logs/pipeline.log"
echo ""
echo "The web UI will be available at http://localhost/ once a run is processed."
