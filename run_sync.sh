#!/bin/sh
# Cron wrapper: runs the mirror sync and keeps its log from growing unbounded
# on the Pi's SD card. All output goes to the log, so cron stays silent.
set -u

DIR="$(dirname "$(readlink -f "$0")")"
LOG="$DIR/sync.log"

/usr/bin/python3 "$DIR/sync_mirrors.py" >>"$LOG" 2>&1
status=$?

if [ -f "$LOG" ] && [ "$(wc -l <"$LOG")" -gt 2000 ]; then
    tail -n 1000 "$LOG" >"$LOG.tmp" && mv "$LOG.tmp" "$LOG"
fi

exit $status
