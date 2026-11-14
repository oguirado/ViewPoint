#!/usr/bin/env bash
set -euo pipefail
DISPLAY_NUM=100
XPRA_SOCKET=/srfview/xpra.sock
# Only the explicit operator-login mode enables input.
XPRA_READONLY=yes
if [ "${SRF_LOGIN:-0}" = 1 ]; then XPRA_READONLY=no; fi
PIDFILE="/run/newton-tenant/xpra-layer.pid"
LOGFILE="/run/newton-tenant/xpra-layer.log"

start() {
  # Bind mounts may share a filesystem: inspect mountinfo, not os.path.ismount.
  python3 -I - <<'VIEW_MOUNT'
import os
import sys
try:
    with open('/proc/self/mountinfo') as mounts:
        mounted = any(line.split()[4] == '/srfview' for line in mounts)
    valid = (mounted and os.path.isdir('/srfview') and not os.path.islink('/srfview')
             and os.access('/srfview', os.W_OK | os.X_OK))
except OSError:
    valid = False
if not valid:
    print('REFUSED: view_mount_not_writable', file=sys.stderr)
    sys.exit(4)
VIEW_MOUNT
  # The system config (/etc/xpra/conf.d/60_server.conf) starts "/etc/X11/Xsession true", which pops an
  # xmessage dialog in the capsule; a --start flag only appends to that list, a user config replaces it.
  mkdir -p /run/user/10001/xpra-conf
  printf 'start = /bin/true\n' > /run/user/10001/xpra-conf/xpra.conf
  XPRA_USER_CONF_DIRS=/run/user/10001/xpra-conf xpra start-desktop ":${DISPLAY_NUM}" \
    --use-display \
    --bind="${XPRA_SOCKET}" \
    --socket-permissions=666 \
    \
    --socket-dirs=/run/user/10001/xpra --socket-dir=/run/user/10001/xpra \
    --html=on \
    --readonly="${XPRA_READONLY}" \
    --mmap=no \
    --bandwidth-limit=0 \
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
    if [[ -S "$XPRA_SOCKET" ]] && python3 -I - "$XPRA_SOCKET" <<'VIEW_HTTP'
import socket
import sys
import time
try:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
        deadline = time.monotonic() + 1
        conn.settimeout(1)
        conn.connect(sys.argv[1])
        conn.sendall(b'GET / HTTP/1.0\r\nHost: localhost\r\nConnection: close\r\n\r\n')
        status = bytearray()
        while len(status) < 1024 and not status.endswith(b'\r\n'):
            conn.settimeout(max(0.001, deadline - time.monotonic()))
            if time.monotonic() >= deadline:
                raise TimeoutError()
            part = conn.recv(1)
            if not part:
                break
            status.extend(part)
        fields = bytes(status).split()
        valid = (status.endswith(b'\r\n') and len(fields) >= 2
                 and fields[0] in (b'HTTP/1.0', b'HTTP/1.1') and fields[1] == b'200')
except OSError:
    valid = False
sys.exit(0 if valid else 3)
VIEW_HTTP
    then return 0; fi
    sleep 0.1
  done
  echo "xpra-layer: the unix-socket HTML5 endpoint did not become ready" >&2
  stop
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

# Normalize unexpected shell/tool failures to the public runtime error code.
trap 'rc=$?; if [ "$rc" -ne 0 ] && { [ "$rc" -lt 2 ] || [ "$rc" -gt 7 ]; }; then exit 4; fi' EXIT
trap 'stop; exit 4' TERM INT
case "${1:-start}" in
  start)  start ;;
  stop)   stop ;;
  restart) stop; start ;;
  *) echo "uso: xpra-layer.sh {start|stop|restart}" >&2; exit 2 ;;
esac
