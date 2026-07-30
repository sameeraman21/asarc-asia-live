#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"
cmd="${1:-help}"

case "$cmd" in
  setup)
    python3 -m pip install --user -r requirements.txt || true
    python3 -m venv .venv 2>/dev/null || true
    .venv/bin/pip install -q -r requirements.txt
    echo "Setup OK"
    ;;
  shadow)
    # Shadow can use nohup; prefer launchd-style only for live
    PY=python3
    [ -x .venv/bin/python3 ] && PY=.venv/bin/python3
    mkdir -p logs decisions outcomes
    if pgrep -f 'asarc_live_bot.py --mode shadow' >/dev/null; then
      echo "Already running shadow"; exit 0
    fi
    : > logs/latest.log
    nohup $PY -u asarc_live_bot.py --mode shadow --poll 20 >> logs/stdout.log 2>&1 &
    echo $! > bot.pid
    disown $! 2>/dev/null || true
    echo "ASARC SHADOW started pid=$(cat bot.pid)"
    ;;
  live)
    LOT="${2:-0.01}"
    exec ./install_launchd.sh live "$LOT"
    ;;
  stop)
    ./install_launchd.sh stop
    pkill -f 'asarc_live_bot.py --mode shadow' 2>/dev/null || true
    rm -f bot.pid
    ;;
  status)
    exec ./install_launchd.sh status
    ;;
  log) tail -f logs/latest.log ;;
  *)
    echo "ASARC Absolute-Best Production (launchd-backed live)"
    echo "  ./run.sh setup | shadow | live 0.01 | status | stop | log"
    ;;
esac
