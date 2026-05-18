#!/bin/bash
# Daily restic backup of the full server: weather site, bitcoin/ckpool,
# openclaw, and all system configs needed for migration.
#
# Captures: application code, nginx configs (all sites), systemd units
# (including override .d/ dirs), crontabs (wx + bitcoin), herbie config,
# /var/www/html, restic password, bitcoin configs/dashboard/ckpool,
# and openclaw home directory.

set -euo pipefail

# This script must run as root (sudo) to read all users' files.
if [ "$(id -u)" -ne 0 ]; then
    exec sudo "$0" "$@"
fi

RESTIC=/home/main/.local/bin/restic
REPO=/home/main/backups/weather-viewer
export RESTIC_PASSWORD_FILE=/home/main/.config/restic/password.txt

SOURCE=/home/main/weather-viewer

# --- Dump crontabs so they get backed up as files ---
CRONTAB_DUMP="$SOURCE/data/crontab.bak"
crontab -u wx -l > "$CRONTAB_DUMP" 2>/dev/null || true
CRONTAB_BITCOIN="$SOURCE/data/crontab-bitcoin.bak"
crontab -u bitcoin -l > "$CRONTAB_BITCOIN" 2>/dev/null || true

echo "$(date '+%Y-%m-%d %H:%M:%S') Starting backup..."

if [ ! -d "$REPO/data" ]; then
    "$RESTIC" init --repo "$REPO"
fi

$RESTIC backup \
    "$SOURCE" \
    /etc/systemd/system/weather-pipeline.service \
    /etc/systemd/system/weather-pipeline.service.d \
    /etc/systemd/system/weather-pipeline-lowpri.service \
    /etc/systemd/system/weather-pipeline-lowpri.service.d \
    /etc/systemd/system/weather-radar.service \
    /etc/systemd/system/weather-satellite.service \
    /etc/systemd/system/weather-web.service \
    /etc/systemd/system/weather-web.service.d \
    /etc/nginx/nginx.conf \
    /etc/nginx/sites-available/default \
    /etc/nginx/sites-available/weather \
    /etc/nginx/sites-available/nebraskastormchasers \
    /home/main/.config/herbie \
    /home/main/.config/restic/password.txt \
    /var/www/html \
    /home/bitcoin/dashboard.py \
    /home/bitcoin/start-dashboard.sh \
    /home/bitcoin/setup-nginx-bitcoin.sh \
    /home/bitcoin/trading \
    /home/bitcoin/ckpool \
    /home/bitcoin/.bitcoin/bitcoin.conf \
    /home/bitcoin/.bashrc \
    /home/bitcoin/.profile \
    /etc/ckpool \
    /etc/systemd/system/ckpool.service \
    /home/openclaw/molty \
    /home/openclaw/.openclaw \
    /home/openclaw/.web-monitor \
    /home/openclaw/.bashrc \
    /home/openclaw/.profile \
    /home/openclaw/.zshrc \
    /home/openclaw/.npmrc \
    --repo "$REPO" \
    --exclude "$SOURCE/data/grib_cache" \
    --exclude "$SOURCE/data/radar_cache" \
    --exclude "$SOURCE/data/satellite_cache" \
    --exclude "$SOURCE/data/rendered" \
    --exclude "$SOURCE/data/regrid_cache" \
    --exclude "$SOURCE/data/test_level2_renders" \
    --exclude "$SOURCE/data/lowpri_queue" \
    --exclude "$SOURCE/data/logs" \
    --exclude "$SOURCE/data/worker_status" \
    --exclude "$SOURCE/data/radar_worker_status" \
    --exclude "$SOURCE/data/satellite_worker_status" \
    --exclude "$SOURCE/logs" \
    --exclude "$SOURCE/data/pipeline_state.json" \
    --exclude "$SOURCE/data/radar_state.json" \
    --exclude "$SOURCE/data/satellite_state.json" \
    --exclude "$SOURCE/data/recent_completions.json" \
    --exclude "$SOURCE/data/satellite_worker_status.json" \
    --exclude "$SOURCE/data/scheduler_unified.lock" \
    --exclude "$SOURCE/.venv" \
    --exclude "*.png" \
    --exclude "*.grib2" \
    --exclude "*.idx" \
    --exclude "__pycache__" \
    --exclude "*.pyc" \
    --exclude "nohup.out" \
    --exclude "*.lock"

# Retention: keep 7 daily, 4 weekly, 3 monthly
$RESTIC forget --repo "$REPO" \
    --keep-daily 7 \
    --keep-weekly 4 \
    --keep-monthly 3 \
    --prune

# Keep repo owned by wx so non-root restic commands still work
chown -R wx:wx "$REPO"

echo "$(date '+%Y-%m-%d %H:%M:%S') Backup complete"
$RESTIC snapshots --repo "$REPO" --latest 3
