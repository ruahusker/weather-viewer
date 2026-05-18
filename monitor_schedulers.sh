#!/bin/bash
# Monitor and auto-restart the unified weather pipeline scheduler

BASE_DIR="/home/main/weather-viewer"
LOG_DIR="$BASE_DIR/logs"
DATA_DIR="$BASE_DIR/data"

check_process() {
    pgrep -f "$1" > /dev/null 2>&1
}

process_count() {
    pgrep -f "$1" 2>/dev/null | wc -l
}

PATTERN="[p]ipeline.unified_scheduler"

echo "=== $(date): Checking unified scheduler ==="

COUNT=$(process_count "$PATTERN")
if [ "$COUNT" -eq 0 ]; then
    echo "Unified scheduler not running, restarting systemd service..."
    sudo /bin/systemctl restart weather-pipeline
    sleep 5
    if check_process "$PATTERN"; then
        echo "Started successfully"
    else
        echo "FAILED to start unified scheduler"
    fi
elif [ "$COUNT" -gt 1 ]; then
    echo "Found $COUNT instances, killing duplicates..."
    pgrep -f "$PATTERN" | tail -n +2 | xargs -r kill -9 2>/dev/null
    sleep 2
else
    echo "Running OK (PID: $(pgrep -f "$PATTERN" | head -1))"
fi

echo "=== Check complete ==="
echo ""
