#!/bin/zsh
# Daily catch-up runner for the $0-API results harvester.
# Fires at login AND daily at 12:45; the stamp guard makes it once-per-day:
# skips if a successful harvest ran <20h ago, so a missed (powered-off) day
# is made up at next boot without double-running on ordinary reboots.
STAMP="$HOME/estate-art-scanner/logs/.price-harvest-last"
if pgrep -f "wallhunter.results_harvest" > /dev/null; then
  exit 0  # already harvesting right now
fi
if [ -f "$STAMP" ]; then
  last=$(stat -f %m "$STAMP")
  now=$(date +%s)
  if (( now - last < 72000 )); then
    exit 0  # ran successfully in the last 20h
  fi
fi
cd "$HOME/estate-art-scanner" || exit 1
/usr/bin/caffeinate -i ./venv/bin/python3 -m wallhunter.results_harvest --limit 150 \
  && touch "$STAMP"
