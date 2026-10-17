#!/usr/bin/env bash
# Install reviewed overrides without replacing the host's service identities.
set -euo pipefail
WX_PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
WX_BACKUP_DIR="${WX_PROJECT_DIR}/data/performance/service-backup-$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p -- "${WX_BACKUP_DIR}"

for WX_SERVICE in weather-pipeline weather-radar weather-satellite weather-web weather-surface; do
    WX_DROPIN_DIR="/etc/systemd/system/${WX_SERVICE}.service.d"
    sudo -n systemctl cat "${WX_SERVICE}" > "${WX_BACKUP_DIR}/${WX_SERVICE}.before"
    if [[ -f "${WX_DROPIN_DIR}/95-performance.conf" ]]; then
        cp -- "${WX_DROPIN_DIR}/95-performance.conf" "${WX_BACKUP_DIR}/${WX_SERVICE}.95-performance.conf"
    fi
    sudo -n install -d -m 0755 -- "${WX_DROPIN_DIR}"
    sudo -n install -m 0644 -- "${WX_PROJECT_DIR}/systemd/performance/${WX_SERVICE}.conf" "${WX_DROPIN_DIR}/95-performance.conf"
done

sudo -n systemctl daemon-reload
for WX_SERVICE in weather-pipeline weather-radar weather-satellite weather-web weather-surface; do
    sudo -n systemctl restart "${WX_SERVICE}"
    sudo -n systemctl is-active "${WX_SERVICE}"
done
printf 'Service configuration backup: %s\n' "${WX_BACKUP_DIR}"
