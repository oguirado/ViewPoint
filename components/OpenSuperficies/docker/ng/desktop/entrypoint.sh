#!/usr/bin/env bash
set -Eeuo pipefail

DISPLAY_NUM="${DISPLAY_NUM:-100}"
GEOMETRY="${GEOMETRY:-1280x900x24}"
WM="${WM:-openbox}"
XPRA_BIND_PORT="${XPRA_BIND_PORT:-10000}"
XPRA_READONLY="${XPRA_READONLY:-no}"

log() { printf '[desktop-entrypoint %s] %s\n' "$(date -Is)" "$*" >&2; }

# Capture preflight must finish before any daemon or framebuffer can start.
[[ "${XVFB_CURSOR:-off}" != on ]] || { printf '%s\n' cursor_forbidden_ui >&2; exit 4; }
[[ "$(stat -f -c %T /run/vqa/fb 2>/dev/null)" == tmpfs ]] || { printf '%s\n' fb_not_tmpfs >&2; exit 4; }
[[ -x "${SRF_FBREAD_BIN:-/usr/local/libexec/srf-fbread}" ]] || { printf '%s\n' fbread_missing >&2; exit 4; }

export DBUS_SESSION_BUS_ADDRESS="unix:path=/run/newton-tenant/bus"
export DBUS_SYSTEM_BUS_ADDRESS="disabled:"
export XDG_RUNTIME_DIR="/run/newton-tenant"
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
export LP_NUM_THREADS=4
export HOME=/home/tenant

mkdir -p "$XDG_RUNTIME_DIR" && chmod 0700 "$XDG_RUNTIME_DIR" || true

# --- 1. Private D-Bus Session Daemon ----------------------------------------
rm -f /run/newton-tenant/bus
dbus-daemon --session --address="unix:path=/run/newton-tenant/bus" --nofork --nopidfile &
DBUS_PID=$!

for _ in $(seq 1 100); do
  [[ -S /run/newton-tenant/bus ]] && break
  sleep 0.05
done
[[ -S /run/newton-tenant/bus ]] || { log "FATAL: private D-Bus socket not created"; exit 3; }
log "private D-Bus daemon ready (pid=${DBUS_PID})"

# --- 2. Virtual Display (Xvfb) and Window Manager (Openbox) -----------------
Xvfb ":${DISPLAY_NUM}" -screen 0 "${GEOMETRY}" \
  -fbdir /run/vqa/fb -nocursor -nolisten tcp -nolisten unix \
  -dpi 96 -ac +extension GLX +extension RANDR +render -noreset &
XVFB_PID=$!

for _ in $(seq 1 100); do
  xdpyinfo -display ":${DISPLAY_NUM}" >/dev/null 2>&1 && break
  sleep 0.05
done
xdpyinfo -display ":${DISPLAY_NUM}" >/dev/null 2>&1 || { log "FATAL: :${DISPLAY_NUM} does not accept connections"; exit 3; }
log "Xvfb ready on :${DISPLAY_NUM} (${GEOMETRY}) pid=${XVFB_PID}"

xsetroot -solid '#101014' 2>/dev/null || true
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
XPRA_READONLY="$XPRA_READONLY" DISPLAY_NUM="$DISPLAY_NUM" XPRA_BIND_PORT="$XPRA_BIND_PORT" \
  /usr/local/bin/xpra-layer.sh start
log "xpra layer up on 0.0.0.0:${XPRA_BIND_PORT} readonly=${XPRA_READONLY}"

# --- 5. Lifecycle and Teardown ----------------------------------------------
shutdown() {
  log "shutting down desktop capsule"
  /usr/local/bin/xpra-layer.sh stop || true
  kill -TERM "$WM_PID" 2>/dev/null || true
  [[ -n "$ATSPI_PID" ]] && kill -TERM "$ATSPI_PID" 2>/dev/null || true
  kill -TERM "$DBUS_PID" 2>/dev/null || true
  kill -TERM "$XVFB_PID" 2>/dev/null || true
  sleep 0.3
  kill -KILL "$WM_PID" "$XVFB_PID" "$DBUS_PID" 2>/dev/null || true
  [[ -n "$ATSPI_PID" ]] && kill -KILL "$ATSPI_PID" 2>/dev/null || true
  rm -f /run/newton-tenant/bus
  log "shutdown complete"
}
trap shutdown TERM INT

while kill -0 "$WM_PID" 2>/dev/null && kill -0 "$XVFB_PID" 2>/dev/null && kill -0 "$DBUS_PID" 2>/dev/null; do
  sleep 1
done
log "core processes exited; triggering shutdown"
shutdown
exit 0
