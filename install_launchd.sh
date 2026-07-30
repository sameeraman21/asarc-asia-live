#!/usr/bin/env bash
# Persistent ASARC live launcher via launchd (survives Cursor shell exit)
set -e
ROOT="$(cd "$(dirname "$0")" && pwd)"
LABEL="com.sam.goldbacktest.asarc-live"
PLIST="$HOME/Library/LaunchAgents/${LABEL}.plist"
PY="$(command -v python3)"
if [ -x "$ROOT/.venv/bin/python3" ]; then PY="$ROOT/.venv/bin/python3"; fi
LOT="${2:-0.01}"
cmd="${1:-help}"

mkdir -p "$ROOT/logs" "$ROOT/decisions" "$ROOT/outcomes" "$HOME/Library/LaunchAgents"

write_plist() {
  cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>${LABEL}</string>
  <key>WorkingDirectory</key><string>${ROOT}</string>
  <key>ProgramArguments</key>
  <array>
    <string>${PY}</string>
    <string>-u</string>
    <string>${ROOT}/asarc_live_bot.py</string>
    <string>--mode</string><string>live</string>
    <string>--lot</string><string>${LOT}</string>
    <string>--confirm-live</string>
    <string>--poll</string><string>20</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>${ROOT}/logs/stdout.log</string>
  <key>StandardErrorPath</key><string>${ROOT}/logs/stdout.log</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PYTHONUNBUFFERED</key><string>1</string>
  </dict>
</dict>
</plist>
EOF
}

case "$cmd" in
  start|live)
    launchctl bootout "gui/$(id -u)/${LABEL}" 2>/dev/null || true
    launchctl unload "$PLIST" 2>/dev/null || true
    : > "$ROOT/logs/latest.log"
    write_plist
    launchctl bootstrap "gui/$(id -u)" "$PLIST" 2>/dev/null || launchctl load "$PLIST"
    sleep 3
    if pgrep -f 'asarc_live_bot.py --mode live' >/dev/null; then
      pid=$(pgrep -f 'asarc_live_bot.py --mode live' | head -1)
      echo "$pid" > "$ROOT/bot.pid"
      echo "ASARC LIVE via launchd pid=$pid lot=$LOT"
      echo "plist=$PLIST"
    else
      echo "FAILED to start via launchd — check $ROOT/logs/stdout.log"
      exit 1
    fi
    ;;
  stop)
    launchctl bootout "gui/$(id -u)/${LABEL}" 2>/dev/null || true
    launchctl unload "$PLIST" 2>/dev/null || true
    pkill -f 'asarc_live_bot.py' 2>/dev/null || true
    rm -f "$ROOT/bot.pid"
    echo "ASARC LIVE stopped (launchd unloaded)"
    ;;
  status)
    if pgrep -f 'asarc_live_bot.py --mode live' >/dev/null; then
      pid=$(pgrep -f 'asarc_live_bot.py --mode live' | head -1)
      echo "$pid" > "$ROOT/bot.pid"
      echo "RUNNING pid=$pid (launchd)"
      tail -8 "$ROOT/logs/latest.log" 2>/dev/null || true
    else
      echo "STOPPED"
    fi
    ;;
  *)
    echo "Usage: $0 start|live [lot] | stop | status"
    ;;
esac
