#!/usr/bin/env bash
set -Eeuo pipefail

DISPLAY_NUM="${DISPLAY_NUM:-100}"
GEOMETRY="${GEOMETRY:-1280x900x24}"
WM="${WM:-openbox}"
START_URL="${START_URL:-about:blank}"
CDP_PORT="${CDP_PORT:-9222}"
XPRA_BIND_PORT="${XPRA_BIND_PORT:-10000}"
XPRA_READONLY="${XPRA_READONLY:-yes}"

log() { printf '[entrypoint %s] %s\n' "$(date -Is)" "$*" >&2; }

# --- Composicion con la mitad de perfil (U-01) -------------------------------
# Prepara cuarentena, layout del perfil, toma el flock advisory en fd 9,
# ejecuta purga de locks residuales y eviction selectiva de cache.
. /usr/local/bin/profile_entrypoint.sh

# --- 6. Display virtual ------------------------------------------------------
Xvfb ":${DISPLAY_NUM}" -screen 0 "${GEOMETRY}" \
  -nolisten tcp -nolisten unix \
  -dpi 96 -ac +extension GLX +extension RANDR +render -noreset &
XVFB_PID=$!
for _ in $(seq 1 100); do
  xdpyinfo -display ":${DISPLAY_NUM}" >/dev/null 2>&1 && break
  sleep 0.05
done
xdpyinfo -display ":${DISPLAY_NUM}" >/dev/null 2>&1 || { log "FATAL: :${DISPLAY_NUM} no acepta conexiones"; exit 3; }
log "Xvfb listo en :${DISPLAY_NUM} (${GEOMETRY}) pid=${XVFB_PID}"

xsetroot -solid '#101014' 2>/dev/null || true
"${WM}" & WM_PID=$!

# --- 7. Chromium setsid con resolucion por proc ------------------------------
CHROME_LOG="/tmp/chromium.log"
USER_DATA_DIR="$USER_DATA_DIR" CDP_PORT="$CDP_PORT" \
  setsid /usr/local/bin/launch-chromium.sh "${START_URL}" >"$CHROME_LOG" 2>&1 &
CHROME_SH_PID=$!

CHROME_PID=""
for _ in $(seq 1 100); do
  CHROME_PID=$(pgrep -n -f "remote-debugging-port=${CDP_PORT}" || true)
  [[ -n "$CHROME_PID" ]] && break
  sleep 0.1
done
[[ -n "$CHROME_PID" ]] || { log "FATAL: Chromium no aparecio en /proc"; exit 3; }
CHROME_PGID=$(ps -o pgid= -p "$CHROME_PID" | tr -d ' ')
log "chromium pid=${CHROME_PID} pgid=${CHROME_PGID} cdp=0.0.0.0:${CDP_PORT} perfil=${USER_DATA_DIR}"

# --- 8. Capa xpra ------------------------------------------------------------
XPRA_READONLY="$XPRA_READONLY" DISPLAY_NUM="$DISPLAY_NUM" XPRA_BIND_PORT="$XPRA_BIND_PORT" \
  /usr/local/bin/xpra-layer.sh start
log "capa xpra arriba en 0.0.0.0:${XPRA_BIND_PORT} readonly=${XPRA_READONLY}"

# --- 9. Teardown -------------------------------------------------------------
shutdown() {
  log "apagando"
  /usr/local/bin/xpra-layer.sh stop || true
  kill -TERM "-${CHROME_PGID}" 2>/dev/null || true
  for _ in $(seq 1 30); do kill -0 "$CHROME_PID" 2>/dev/null || break; sleep 0.5; done
  kill -KILL "-${CHROME_PGID}" 2>/dev/null || true
  kill -TERM "$WM_PID" "$XVFB_PID" 2>/dev/null || true
  sleep 0.3
  kill -KILL "$WM_PID" "$XVFB_PID" 2>/dev/null || true
  purge_locks "$USER_DATA_DIR"
  evict_cache "$USER_DATA_DIR"
  log "apagado completo"
}
trap shutdown TERM INT

while kill -0 "$CHROME_PID" 2>/dev/null; do sleep 2; done
log "chromium termino; cerrando capsula"
shutdown
exit 0
