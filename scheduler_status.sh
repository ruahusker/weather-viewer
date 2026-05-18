#!/bin/bash
# Quick status check of the unified scheduler

echo "=== Unified Scheduler Status ==="
echo ""

PATTERN="[p]ipeline.unified_scheduler"
MAIN_PID=$(pgrep -of "$PATTERN" 2>/dev/null)
if [ -z "$MAIN_PID" ]; then
    echo "  Status: NOT RUNNING"
    echo ""
else
    TOTAL_CPU=0
    TOTAL_MEM=0
    TOTAL_RSS_KB=0

    # Scheduler (main process)
    S_CPU=$(ps -p $MAIN_PID -o %cpu= 2>/dev/null | tr -d ' ')
    S_MEM=$(ps -p $MAIN_PID -o %mem= 2>/dev/null | tr -d ' ')
    S_RSS=$(ps -p $MAIN_PID -o rss= 2>/dev/null | tr -d ' ')
    S_RSS_MB=$(awk "BEGIN{printf \"%.0f\", ${S_RSS:-0}/1024}")
    S_STATUS="Idle"
    if awk "BEGIN{exit(${S_CPU:-0} <= 20.0)}"; then
        S_STATUS="Scheduling"
    fi
    TOTAL_CPU=$(awk "BEGIN{printf \"%.1f\", $TOTAL_CPU + ${S_CPU:-0}}")
    TOTAL_MEM=$(awk "BEGIN{printf \"%.1f\", $TOTAL_MEM + ${S_MEM:-0}}")
    TOTAL_RSS_KB=$((TOTAL_RSS_KB + ${S_RSS:-0}))

    # Render workers (child processes)
    RENDER_PIDS=$(pgrep -P $MAIN_PID 2>/dev/null)
    WORKER_COUNT=0
    if [ -n "$RENDER_PIDS" ]; then
        WORKER_COUNT=$(echo "$RENDER_PIDS" | wc -l)
    fi

    echo "Processes ($((WORKER_COUNT + 1))):"
    echo "  PID $MAIN_PID - CPU: ${S_CPU}% MEM: ${S_MEM}% (${S_RSS_MB}MB) [Scheduler — $S_STATUS]"

    if [ -n "$RENDER_PIDS" ]; then
        for pid in $RENDER_PIDS; do
            CPU=$(ps -p $pid -o %cpu= 2>/dev/null | tr -d ' ')
            MEM=$(ps -p $pid -o %mem= 2>/dev/null | tr -d ' ')
            RSS=$(ps -p $pid -o rss= 2>/dev/null | tr -d ' ')
            RSS_MB=$(awk "BEGIN{printf \"%.0f\", ${RSS:-0}/1024}")
            echo "  PID $pid - CPU: ${CPU}% MEM: ${MEM}% (${RSS_MB}MB) [Render Worker]"
            TOTAL_CPU=$(awk "BEGIN{printf \"%.1f\", $TOTAL_CPU + ${CPU:-0}}")
            TOTAL_MEM=$(awk "BEGIN{printf \"%.1f\", $TOTAL_MEM + ${MEM:-0}}")
            TOTAL_RSS_KB=$((TOTAL_RSS_KB + ${RSS:-0}))
        done
    fi

    TOTAL_RSS_MB=$(awk "BEGIN{printf \"%.0f\", $TOTAL_RSS_KB/1024}")
    echo "  ─────────────────────────────────────────────────"
    echo "  Totals     - CPU: ${TOTAL_CPU}% MEM: ${TOTAL_MEM}% (${TOTAL_RSS_MB}MB)"
    echo ""

    # Pipeline status
    STATE_FILE="/home/main/weather-viewer/data/pipeline_state.json"
    if [ -f "$STATE_FILE" ]; then
        RAW_STATUS=$(python3 -c "import json; d=json.load(open('$STATE_FILE')); print(d.get('status','unknown'))" 2>/dev/null)
        if [ "$RAW_STATUS" = "processing" ]; then
            MODEL=$(python3 -c "import json; d=json.load(open('$STATE_FILE')); print(d.get('model','').upper())" 2>/dev/null)
            RUN=$(python3 -c "import json; d=json.load(open('$STATE_FILE')); print(d.get('current_run',''))" 2>/dev/null)
            DONE=$(python3 -c "import json; d=json.load(open('$STATE_FILE')); print(len(d.get('completed_fxx',[])))" 2>/dev/null)
            TOTAL_FXX=$(python3 -c "import json; d=json.load(open('$STATE_FILE')); print(d.get('total_fxx','?'))" 2>/dev/null)
            QUEUE_LAYOUT=$(python3 -c "import json; d=json.load(open('$STATE_FILE')); print(d.get('queue_layout_text',''))" 2>/dev/null)
            QUEUE_ACTIVITY=$(python3 -c "import json; d=json.load(open('$STATE_FILE')); print(d.get('queue_activity_text',''))" 2>/dev/null)
            echo "Status: Processing $MODEL $RUN ($DONE/$TOTAL_FXX fxx)"
            [ -n "$QUEUE_LAYOUT" ] && echo "Queues: $QUEUE_LAYOUT"
            [ -n "$QUEUE_ACTIVITY" ] && echo "Slots:  $QUEUE_ACTIVITY"
        elif [ "$RAW_STATUS" = "idle" ] && awk "BEGIN{exit(${TOTAL_CPU:-0} <= 20.0)}"; then
            echo "Status: Processing"
        else
            echo "Status: Idle"
        fi
    else
        echo "Status: Unknown (no state file)"
    fi
    echo ""
fi

echo "Recent Log:"
tail -5 /home/main/weather-viewer/logs/pipeline.log 2>/dev/null | while read line; do
    echo "  ${line:0:120}"
done
