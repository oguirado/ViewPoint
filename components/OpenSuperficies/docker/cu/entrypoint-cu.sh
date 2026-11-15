#!/usr/bin/env bash
set -Eeuo pipefail
umask 027  # every file this capsule creates (Xvfb fb, chunks) has no world bits; docker exec uses exec-umask.sh

DISPLAY_NUM="${DISPLAY_NUM:-100}"
GEOMETRY="${GEOMETRY:-1280x1064x24}"
WM="${WM:-openbox}"
XPRA_BIND_PORT="${XPRA_BIND_PORT:-10000}"
XPRA_READONLY="${XPRA_READONLY:-no}"
# CU capsule variant (os-cu-0 L1): XPRA_MODE=off by default, takeover only over UDS (ER-004).
XPRA_MODE="${XPRA_MODE:-off}"
FB_DIR="${FB_DIR:-/run/vqa/fb}"  # preflight probe path; Xvfb -fbdir stays the fixed bind mount
TENANT_DIR="${TENANT_DIR:-/run/newton-tenant}"
CAPSULE_UID=10001
VQA_ISO="${VQA_ISO:-}"
XVFB_CURSOR="${XVFB_CURSOR:-off}"  # on only for WL-7 (pointer path); default keeps -nocursor

log() { printf '[desktop-entrypoint %s] %s\n' "$(date -Is)" "$*" >&2; }

# --- 0. Preflight (before any daemon): exit 4 with a distinct token, never a silent chmod/true ------------
[[ -n "$VQA_ISO" ]] || { log "FATAL: iso_missing: VQA_ISO (32 hex) is required"; exit 2; }
[[ "$XVFB_CURSOR" == "on" || "$XVFB_CURSOR" == "off" ]] || { log "FATAL: xvfb_cursor_invalid: XVFB_CURSOR must be on|off"; exit 2; }
[[ "$(id -u)" == "$CAPSULE_UID" ]] || { log "FATAL: uid_mismatch: expected uid ${CAPSULE_UID}, got $(id -u) (run as the image user)"; exit 4; }
[[ -d "$TENANT_DIR" && -O "$TENANT_DIR" && -w "$TENANT_DIR" ]] || { log "FATAL: tenant_not_owned: ${TENANT_DIR} is not a writable directory owned by uid ${CAPSULE_UID}"; exit 4; }
[[ -d "$FB_DIR" ]] && : >"$FB_DIR/.probe" 2>/dev/null && rm -f "$FB_DIR/.probe" || { log "FATAL: fb_not_writable: ${FB_DIR}"; exit 4; }

export DBUS_SESSION_BUS_ADDRESS="unix:path=${TENANT_DIR}/bus"
export DBUS_SYSTEM_BUS_ADDRESS="disabled:"
export XDG_RUNTIME_DIR="$TENANT_DIR"
export XDG_CURRENT_DESKTOP=""
export XDG_SESSION_TYPE="x11"
export GTK_USE_PORTAL=0
export GDK_BACKEND=x11
export QT_QPA_PLATFORM=xcb
export NO_AT_BRIDGE=0
export GIO_USE_VFS=local
export DISPLAY=":${DISPLAY_NUM}"
export LIBGL_ALWAYS_SOFTWARE=1
export GALLIUM_DRIVER=llvmpipe
export LP_NUM_THREADS=2
export HOME=/home/tenant

# --- 1. Private D-Bus Session Daemon ----------------------------------------
rm -f "$TENANT_DIR/bus"
dbus-daemon --session --address="unix:path=${TENANT_DIR}/bus" --nofork --nopidfile &
DBUS_PID=$!

for _ in $(seq 1 100); do
  [[ -S "$TENANT_DIR/bus" ]] && break
  sleep 0.05
done
[[ -S "$TENANT_DIR/bus" ]] || { log "FATAL: private D-Bus socket not created"; exit 3; }
log "private D-Bus daemon ready (pid=${DBUS_PID})"

# --- 2. Virtual Display (Xvfb) and Window Manager (Openbox) -----------------
CURSOR_ARGS=(-nocursor)
[[ "$XVFB_CURSOR" == "on" ]] && CURSOR_ARGS=()
Xvfb ":${DISPLAY_NUM}" -screen 0 "${GEOMETRY}" \
  -fbdir /run/vqa/fb +extension XTEST -nolisten tcp -nolisten unix "${CURSOR_ARGS[@]}" \
  -dpi 96 -ac +extension GLX +extension RANDR +render -noreset &
XVFB_PID=$!

for _ in $(seq 1 100); do
  xdpyinfo -display ":${DISPLAY_NUM}" >/dev/null 2>&1 && break
  sleep 0.05
done
xdpyinfo -display ":${DISPLAY_NUM}" >/dev/null 2>&1 || { log "FATAL: :${DISPLAY_NUM} does not accept connections"; exit 3; }
log "Xvfb ready on :${DISPLAY_NUM} (${GEOMETRY}) pid=${XVFB_PID}"

xsetroot -solid '#101014' 2>/dev/null || true
# isolation id pinned on the root window (checked by the host guard, rc 5 iso_mismatch)
xprop -display ":${DISPLAY_NUM}" -root -f _OPENNEWTON_ISOLATION_ID 8s -set _OPENNEWTON_ISOLATION_ID "$VQA_ISO" \
  || { log "FATAL: cannot pin _OPENNEWTON_ISOLATION_ID"; exit 3; }
"${WM}" &
WM_PID=$!
log "window manager ${WM} started (pid=${WM_PID})"

# --- 3. AT-SPI2 Accessibility Perception Bus --------------------------------
ATSPI_PID=""
if [[ -x /usr/libexec/at-spi-bus-launcher ]]; then
  /usr/libexec/at-spi-bus-launcher &
  ATSPI_PID=$!
  log "at-spi-bus-launcher started (pid=${ATSPI_PID})"
elif [[ -x /usr/lib/at-spi2-core/at-spi-bus-launcher ]]; then
  /usr/lib/at-spi2-core/at-spi-bus-launcher &
  ATSPI_PID=$!
  log "at-spi-bus-launcher started (pid=${ATSPI_PID})"
fi

# --- 4. Xpra Layer ----------------------------------------------------------
if [[ "$XPRA_MODE" != "off" ]]; then
  XPRA_READONLY="$XPRA_READONLY" DISPLAY_NUM="$DISPLAY_NUM" XPRA_BIND_PORT="$XPRA_BIND_PORT" \
    /usr/local/bin/xpra-layer.sh start
  log "xpra layer up on 0.0.0.0:${XPRA_BIND_PORT} readonly=${XPRA_READONLY}"
else
  log "xpra layer off (XPRA_MODE=off)"
fi

# --- 5. Lifecycle and Teardown ----------------------------------------------
shutdown() {
  log "shutting down desktop capsule"
  [[ "$XPRA_MODE" != "off" ]] && { /usr/local/bin/xpra-layer.sh stop || true; }
  kill -TERM "$WM_PID" 2>/dev/null || true
  [[ -n "$ATSPI_PID" ]] && kill -TERM "$ATSPI_PID" 2>/dev/null || true
  kill -TERM "$DBUS_PID" 2>/dev/null || true
  kill -TERM "$XVFB_PID" 2>/dev/null || true
  sleep 0.3
  kill -KILL "$WM_PID" "$XVFB_PID" "$DBUS_PID" 2>/dev/null || true
  [[ -n "$ATSPI_PID" ]] && kill -KILL "$ATSPI_PID" 2>/dev/null || true
  rm -f "$TENANT_DIR/bus"
  log "shutdown complete"
}
trap shutdown TERM INT

while kill -0 "$WM_PID" 2>/dev/null && kill -0 "$XVFB_PID" 2>/dev/null && kill -0 "$DBUS_PID" 2>/dev/null; do
  sleep 1
done
log "core processes exited; triggering shutdown"
shutdown
exit 0
