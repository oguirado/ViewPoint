#!/usr/bin/env bash
set -euo pipefail
DISPLAY_NUM="${DISPLAY_NUM:-100}"
XPRA_BIND_PORT="${XPRA_BIND_PORT:-10000}"
XPRA_READONLY="${XPRA_READONLY:-yes}"
PIDFILE="/tmp/xpra-layer.pid"
LOGFILE="/tmp/xpra-layer.log"

start() {
  xpra start-desktop ":${DISPLAY_NUM}" \
    --use-display \
    --bind-tcp=0.0.0.0:"${XPRA_BIND_PORT}" \
    --html=on \
    --readonly="${XPRA_READONLY}" \
    --mmap=no \
    --encodings=rgb,jpeg,scroll \
    --encoding=auto \
    --min-quality=80 \
    --min-speed=90 \
    --speed=100 \
    --compress=1 \
    --compressors=lz4 \
    --auto-refresh-delay=0.15 \
    --bandwidth-limit=0 \
    --video-scaling=off \
    --pixel-depth=24 \
    --dpi=96 \
    --resize-display=no \
    --speaker=no --microphone=no --pulseaudio=no --av-sync=no \
    --printing=no --file-transfer=no --open-files=no --open-url=no \
    --webcam=no --notifications=no --bell=no --clipboard=no --mdns=no \
    --dbus-launch= --dbus-proxy=no --dbus-control=no \
    --start-new-commands=no --sharing=no --systemd-run=no \
    --exit-with-children=no \
    --daemon=no >"$LOGFILE" 2>&1 &
  echo $! > "$PIDFILE"
  for _ in $(seq 1 100); do
    curl -fsS --max-time 1 "http://127.0.0.1:${XPRA_BIND_PORT}/" >/dev/null 2>&1 && return 0
    sleep 0.1
  done
  echo "xpra-layer: el endpoint HTML5 no respondio en 10 s" >&2
  return 3
}

stop() {
  [[ -f "$PIDFILE" ]] || return 0
  local p; p=$(cat "$PIDFILE")
  kill -TERM "$p" 2>/dev/null || true
  for _ in $(seq 1 40); do kill -0 "$p" 2>/dev/null || break; sleep 0.25; done
  kill -KILL "$p" 2>/dev/null || true
  rm -f "$PIDFILE"
}

case "${1:-start}" in
  start)  start ;;
  stop)   stop ;;
  readonly) XPRA_READONLY="${2:?yes|no}"; stop; sleep 0.2; start ;;
  *) echo "uso: xpra-layer.sh {start|stop|readonly yes|no}" >&2; exit 2 ;;
esac
