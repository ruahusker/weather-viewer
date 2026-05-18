#!/bin/bash

# Check if the lock file process is still running
LOCK_PID=$(cat /home/main/weather-viewer/data/scheduler_unified.lock 2>/dev/null)

if [ -n "$LOCK_PID" ]; then
    if ps -p $LOCK_PID >/dev/null 2>&1; then
        echo "Process $LOCK_PID is still running - not removing lock"
    else
        echo "Process $LOCK_PID is not running - removing stale lock"
        rm -f /home/main/weather-viewer/data/scheduler_unified.lock
        echo "Lock file removed"
    fi
else
    echo "No lock file found"
fi

# Check systemd service status
systemctl status weather-pipeline --no-pager 2>&1 | head -10

# Try to restart if not running
if systemctl is-active --quiet weather-pipeline; then
    echo "Pipeline is already running"
else
    echo "Restarting weather-pipeline service"
    systemctl restart weather-pipeline
fi