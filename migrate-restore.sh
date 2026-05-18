#!/bin/bash
# migrate-restore.sh — Restore full server on a fresh Ubuntu instance.
#
# Restores: weather site (wx), bitcoin/ckpool (bitcoin), openclaw user.
#
# Prerequisites on the NEW server:
#   1. Ubuntu 22.04+ with sudo access
#   2. User "wx" created:  sudo adduser wx && sudo usermod -aG sudo wx
#   3. restic installed:   sudo apt install restic
#      (or copy /home/main/.local/bin/restic from old server)
#   4. The backup repo copied to /home/main/backups/weather-viewer/
#      e.g.: rsync -avz oldserver:/home/main/backups/weather-viewer/ /home/main/backups/weather-viewer/
#   5. The restic password file at /home/main/.config/restic/password.txt
#      (it's included in the backup, but you need it to unlock the backup —
#       copy it manually first: scp oldserver:/home/main/.config/restic/password.txt ~/.config/restic/)
#
# Usage:
#   sudo bash migrate-restore.sh
#
# After restore, update DNS for vibeprojects.us and nebraskastormchasers.com
# to point to the new server's IP, then run certbot to get new SSL certs.

set -euo pipefail

RESTIC="${RESTIC:-restic}"
REPO=/home/main/backups/weather-viewer
export RESTIC_PASSWORD_FILE=/home/main/.config/restic/password.txt
APP_USER=wx

if [ "$(id -u)" -ne 0 ]; then
    echo "ERROR: Run this script as root (sudo bash $0)"
    exit 1
fi

if [ ! -f "$RESTIC_PASSWORD_FILE" ]; then
    echo "ERROR: Restic password file not found at $RESTIC_PASSWORD_FILE"
    echo "Copy it from the old server first:"
    echo "  mkdir -p /home/$APP_USER/.config/restic"
    echo "  scp oldserver:/home/$APP_USER/.config/restic/password.txt $RESTIC_PASSWORD_FILE"
    exit 1
fi

if [ ! -d "$REPO/data" ]; then
    echo "ERROR: Backup repo not found at $REPO"
    echo "Copy it from the old server first:"
    echo "  rsync -avz oldserver:$REPO/ $REPO/"
    exit 1
fi

echo "=== Full Server Migration Restore ==="
echo ""

# -------------------------------------------------------------------------
# 0. Create users if they don't exist
# -------------------------------------------------------------------------
echo "[0/10] Creating users..."
for u in wx bitcoin openclaw; do
    if ! id "$u" &>/dev/null; then
        adduser --disabled-password --gecos "" "$u"
        echo "  Created user: $u"
    else
        echo "  User $u already exists"
    fi
done

# Show available snapshots
echo ""
echo "Available snapshots:"
$RESTIC snapshots --repo "$REPO" --latest 5
echo ""
read -rp "Press Enter to restore the latest snapshot, or Ctrl-C to cancel..."

# -------------------------------------------------------------------------
# 1. System dependencies
# -------------------------------------------------------------------------
echo ""
echo "[1/10] Installing system packages..."
apt-get update -qq
apt-get install -y --no-install-recommends \
    libgeos-dev libproj-dev libeccodes-dev \
    proj-data proj-bin \
    python3-pip python3-venv \
    nginx certbot python3-certbot-nginx \
    goaccess npm

# -------------------------------------------------------------------------
# 2. Restore files from restic
# -------------------------------------------------------------------------
echo ""
echo "[2/10] Restoring files from backup..."
$RESTIC restore latest --repo "$REPO" --target /
echo "  Files restored."

# -------------------------------------------------------------------------
# 3. Fix ownership
# -------------------------------------------------------------------------
echo ""
echo "[3/10] Fixing file ownership..."
chown -R "$APP_USER:$APP_USER" "/home/$APP_USER/weather-viewer"
chown -R "$APP_USER:$APP_USER" "/home/$APP_USER/.config"
chown -R "$APP_USER:$APP_USER" "/home/$APP_USER/backups"

# Bitcoin user ownership
chown -R bitcoin:bitcoin /home/bitcoin/ 2>/dev/null || true
chmod 600 /home/bitcoin/.bitcoin/bitcoin.conf 2>/dev/null || true

# Openclaw user ownership
chown -R openclaw:openclaw /home/openclaw/ 2>/dev/null || true

# Make rendered directory readable by nginx
DATA_DIR="/home/$APP_USER/weather-viewer/data"
mkdir -p "$DATA_DIR/grib_cache" "$DATA_DIR/rendered" "$DATA_DIR/radar_cache" \
         "$DATA_DIR/satellite_cache" "$DATA_DIR/regrid_cache" "$DATA_DIR/logs" \
         "$DATA_DIR/worker_status" "$DATA_DIR/radar_worker_status" \
         "$DATA_DIR/satellite_worker_status" "$DATA_DIR/lowpri_queue" \
         "/home/$APP_USER/weather-viewer/logs"
chown -R "$APP_USER:$APP_USER" "$DATA_DIR"
chmod -R o+rX "$DATA_DIR/rendered"

# Create model-specific cache subdirectories
for model in hrrr nam3km rap nam nam12km gfs ecmwf ifs href rrfs aifs gdps gefs rtma; do
    mkdir -p "$DATA_DIR/grib_cache/$model" "$DATA_DIR/rendered/$model"
done
mkdir -p "$DATA_DIR/rendered/satellite" "$DATA_DIR/rendered/radar" "$DATA_DIR/rendered/home"
chown -R "$APP_USER:$APP_USER" "$DATA_DIR"

# -------------------------------------------------------------------------
# 4. Rebuild Python virtual environment
# -------------------------------------------------------------------------
echo ""
echo "[4/10] Rebuilding Python virtual environment..."
VENV_DIR="/home/$APP_USER/weather-viewer/.venv"
sudo -u "$APP_USER" python3 -m venv "$VENV_DIR"
sudo -u "$APP_USER" "$VENV_DIR/bin/pip" install --upgrade pip
sudo -u "$APP_USER" "$VENV_DIR/bin/pip" install -r "/home/$APP_USER/weather-viewer/requirements.txt"

# Pre-download Cartopy shapefiles
echo "  Downloading Cartopy shapefiles..."
sudo -u "$APP_USER" "$VENV_DIR/bin/python" - <<'PYEOF'
import cartopy.io.shapereader as shpreader
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

# -------------------------------------------------------------------------
# 5. Install systemd services
# -------------------------------------------------------------------------
echo ""
echo "[5/10] Configuring systemd services..."
systemctl daemon-reload
systemctl enable weather-pipeline.service weather-radar.service \
    weather-satellite.service weather-web.service
# Enable ckpool if the service file was restored
if [ -f /etc/systemd/system/ckpool.service ]; then
    systemctl enable ckpool.service
    echo "  ckpool service enabled."
fi
echo "  Weather services enabled."

# -------------------------------------------------------------------------
# 6. Configure nginx
# -------------------------------------------------------------------------
echo ""
echo "[6/10] Configuring nginx..."
# Enable site configs
ln -sf /etc/nginx/sites-available/weather /etc/nginx/sites-enabled/weather
ln -sf /etc/nginx/sites-available/nebraskastormchasers /etc/nginx/sites-enabled/nebraskastormchasers

# Remove default site if it conflicts
rm -f /etc/nginx/sites-enabled/default

# Test and reload — SSL certs won't exist yet, so temporarily comment out SSL
# lines or just start with HTTP. We'll get certs in step 7.
echo "  NOTE: nginx may fail if SSL certs don't exist yet."
echo "  SSL will be configured after certbot runs in step 7."
nginx -t 2>/dev/null && nginx -s reload 2>/dev/null || echo "  (nginx not reloaded — will fix after certbot)"

# -------------------------------------------------------------------------
# 7. SSL certificates
# -------------------------------------------------------------------------
echo ""
echo "[7/10] SSL certificates..."
echo ""
echo "  IMPORTANT: Before running certbot, update DNS records for both domains"
echo "  to point to this server's IP address."
echo ""
echo "  Once DNS is updated, run:"
echo "    sudo certbot --nginx -d vibeprojects.us -d www.vibeprojects.us"
echo "    sudo certbot --nginx -d nebraskastormchasers.com -d www.nebraskastormchasers.com"
echo ""

# -------------------------------------------------------------------------
# 8. Restore crontab
# -------------------------------------------------------------------------
echo ""
echo "[8/10] Restoring crontabs..."
CRONTAB_BAK="/home/$APP_USER/weather-viewer/data/crontab.bak"
if [ -f "$CRONTAB_BAK" ]; then
    sudo -u "$APP_USER" crontab "$CRONTAB_BAK"
    echo "  wx crontab restored."
else
    echo "  WARNING: No wx crontab backup found."
fi
CRONTAB_BITCOIN="/home/$APP_USER/weather-viewer/data/crontab-bitcoin.bak"
if [ -f "$CRONTAB_BITCOIN" ] && [ -s "$CRONTAB_BITCOIN" ]; then
    sudo -u bitcoin crontab "$CRONTAB_BITCOIN"
    echo "  bitcoin crontab restored."
fi

# -------------------------------------------------------------------------
# 9. Bitcoin / ckpool setup
# -------------------------------------------------------------------------
echo ""
echo "[9/10] Bitcoin / ckpool notes..."
echo ""
echo "  The ckpool config and bitcoin dashboard have been restored."
echo "  You will need to install bitcoind (Bitcoin Knots) separately:"
echo "    - Download from https://bitcoinknots.org/"
echo "    - The bitcoin.conf at /home/bitcoin/.bitcoin/bitcoin.conf is restored"
echo "    - Blockchain will re-sync (pruned mode, ~19GB)"
echo ""
echo "  To install ckpool from source:"
echo "    cd /home/bitcoin/ckpool && ./configure && make && sudo make install"
echo ""

# -------------------------------------------------------------------------
# 10. Openclaw setup
# -------------------------------------------------------------------------
echo ""
echo "[10/10] Openclaw..."
echo "  Openclaw home directory restored to /home/openclaw/"
echo "  npm packages will need reinstalling if any node projects are present."

# -------------------------------------------------------------------------
# Done
# -------------------------------------------------------------------------
echo ""
echo "=========================================="
echo "  Restore complete!"
echo "=========================================="
echo ""
echo "Next steps:"
echo "  1. Update DNS for vibeprojects.us and nebraskastormchasers.com"
echo "  2. Run certbot for SSL (commands shown above)"
echo "  3. Install Bitcoin Knots if needed (see step 9 output)"
echo "  4. Start all services:"
echo "       sudo systemctl start weather-web weather-pipeline weather-radar weather-satellite"
echo "       sudo systemctl start ckpool  # after bitcoind is synced"
echo "  5. Start bitcoin dashboard:"
echo "       sudo -u bitcoin bash /home/bitcoin/start-dashboard.sh &"
echo "  6. Verify:"
echo "       sudo systemctl status weather-web weather-pipeline weather-radar weather-satellite"
echo "       curl -s http://localhost:8000/  # FastAPI health check"
echo "       tail -f /home/$APP_USER/weather-viewer/logs/pipeline.log"
echo ""
echo "  Weather data caches (GRIB, radar, satellite, rendered images) will"
echo "  rebuild automatically once the pipelines start. ~1 hour for first cycle."
echo "  Bitcoin blockchain will re-sync in pruned mode (~19GB, may take hours)."
echo ""
