#!/bin/bash
# Watchdog for the unified pipeline scheduler.
# Detects stuck processes (no log progress while CPU is active) and restarts.
# Run via cron every 5 minutes:
#   */5 * * * * /home/main/weather-viewer/watchdog.sh >> /home/main/weather-viewer/logs/watchdog.log 2>&1

LOG_FILE="/home/main/weather-viewer/logs/pipeline.log"
WATCHDOG_LOG="/home/main/weather-viewer/logs/watchdog.log"
STATE_FILE="/home/main/weather-viewer/data/pipeline_state.json"
STALL_MINUTES=10
PATTERN="[p]ipeline.unified_scheduler"

now=$(date +%s)
ts() { date -u "+%Y-%m-%d %H:%M:%SZ"; }

# --- 1. Is the process running at all? ---
MAIN_PID=$(pgrep -of "$PATTERN" 2>/dev/null)
if [ -z "$MAIN_PID" ]; then
    echo "$(ts) [WATCHDOG] Pipeline not running — starting"
    rm -f /home/main/weather-viewer/data/scheduler_unified.lock
    sudo /bin/systemctl restart weather-pipeline
    sleep 5
    if pgrep -f "$PATTERN" > /dev/null 2>&1; then
        echo "$(ts) [WATCHDOG] Pipeline started successfully"
    else
        echo "$(ts) [WATCHDOG] ERROR: Failed to start pipeline"
    fi
    exit 0
fi

# --- 2. Is it stuck? Check last log activity vs CPU usage ---

# Get last log modification time
if [ ! -f "$LOG_FILE" ]; then
    echo "$(ts) [WATCHDOG] No log file found, skipping stall check"
    exit 0
fi

last_mod=$(stat -c %Y "$LOG_FILE" 2>/dev/null)
age_seconds=$((now - last_mod))
age_minutes=$((age_seconds / 60))

# Get total CPU of all pipeline processes
ALL_PIDS=$(pgrep -f "$PATTERN" 2>/dev/null | tr '\n' ',')
ALL_PIDS=${ALL_PIDS%,}  # trim trailing comma
if [ -z "$ALL_PIDS" ]; then
    exit 0
fi
TOTAL_CPU=$(ps -p "$ALL_PIDS" --no-headers -o %cpu= 2>/dev/null | awk '{s+=$1}END{printf "%.0f", s}')

# Stall = no log output for STALL_MINUTES but CPU > 20% (workers are spinning)
if [ "$age_minutes" -ge "$STALL_MINUTES" ] && [ "${TOTAL_CPU:-0}" -gt 20 ]; then
    echo "$(ts) [WATCHDOG] STALL DETECTED — no log output for ${age_minutes}m, CPU at ${TOTAL_CPU}%"
    echo "$(ts) [WATCHDOG] Last 3 log lines:"
    tail -3 "$LOG_FILE" | while read line; do echo "  $line"; done
    echo "$(ts) [WATCHDOG] Restarting pipeline..."
    sudo /bin/systemctl restart weather-pipeline
    sleep 5
    if pgrep -f "$PATTERN" > /dev/null 2>&1; then
        echo "$(ts) [WATCHDOG] Pipeline restarted successfully"
    else
        echo "$(ts) [WATCHDOG] ERROR: Failed to restart pipeline"
    fi
    exit 0
fi

# --- 3. Also catch zombie state: no log output, no CPU (fully dead but PID exists) ---
if [ "$age_minutes" -ge "$STALL_MINUTES" ] && [ "${TOTAL_CPU:-0}" -le 1 ]; then
    # Check if pipeline_state says idle — that's fine, it's just waiting
    if [ -f "$STATE_FILE" ]; then
        status=$(python3 -c "import json; print(json.load(open('$STATE_FILE')).get('status',''))" 2>/dev/null)
        if [ "$status" = "idle" ]; then
            # Genuinely idle, no work to do — not stuck
            exit 0
        fi
    fi
    echo "$(ts) [WATCHDOG] ZOMBIE DETECTED — no log for ${age_minutes}m, CPU at ${TOTAL_CPU}%, state not idle"
    echo "$(ts) [WATCHDOG] Restarting pipeline..."
    sudo /bin/systemctl restart weather-pipeline
    sleep 5
    if pgrep -f "$PATTERN" > /dev/null 2>&1; then
        echo "$(ts) [WATCHDOG] Pipeline restarted successfully"
    else
        echo "$(ts) [WATCHDOG] ERROR: Failed to restart pipeline"
    fi
    exit 0
fi

# All good
exit 0
