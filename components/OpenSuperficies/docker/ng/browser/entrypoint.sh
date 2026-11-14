#!/usr/bin/env bash
set -Eeuo pipefail

DISPLAY_NUM="${DISPLAY_NUM:-100}"
GEOMETRY="${GEOMETRY:-1280x900x24}"
WM="${WM:-openbox}"
START_URL="${START_URL:-about:blank}"
CDP_PORT="${CDP_PORT:-9222}"
RELAY_PORT="${RELAY_PORT:-9220}"
XPRA_BIND_PORT="${XPRA_BIND_PORT:-10000}"
XPRA_READONLY="${XPRA_READONLY:-yes}"

log() { printf '[entrypoint %s] %s\n' "$(date -Is)" "$*" >&2; }

# --- 1-5. Profile and quarantine layout (U-01) ------------------------------
. /usr/local/bin/profile_entrypoint.sh

# --- 6. Virtual display -----------------------------------------------------
Xvfb ":${DISPLAY_NUM}" -screen 0 "${GEOMETRY}" \
  -nolisten tcp -nolisten unix \
  -dpi 96 -ac +extension GLX +extension RANDR +render -noreset &
XVFB_PID=$!
for _ in $(seq 1 100); do
  xdpyinfo -display ":${DISPLAY_NUM}" >/dev/null 2>&1 && break
  sleep 0.05
done
xdpyinfo -display ":${DISPLAY_NUM}" >/dev/null 2>&1 || { log "FATAL: :${DISPLAY_NUM} does not accept connections"; exit 3; }
log "Xvfb ready on :${DISPLAY_NUM} (${GEOMETRY}) pid=${XVFB_PID}"

xsetroot -solid '#101014' 2>/dev/null || true
"${WM}" & WM_PID=$!

# --- 7. Chromium setsid (loopback-only on 127.0.0.1) ------------------------
CHROME_LOG="/tmp/chromium.log"
CACHE_BUDGET_MB="${CACHE_BUDGET_MB:-512}"
WINDOW_SIZE="${WINDOW_SIZE:-1280,900}"

setsid chromium \
  --user-data-dir="${USER_DATA_DIR}" \
  --remote-debugging-address=127.0.0.1 \
  --remote-debugging-port="${CDP_PORT}" \
  --no-sandbox \
  --no-first-run \
  --no-default-browser-check \
  --disable-infobars \
  --disable-session-crashed-bubble \
  --use-gl=angle \
  --use-angle=gl \
  --ignore-gpu-blocklist \
  --disable-gpu-compositing \
  --enable-features=NetworkService,NetworkServiceInProcess \
  --disable-features=Translate,MediaRouter,OptimizationHints,OptimizationGuideModelDownloading,UseXdgDesktopPortal \
  --window-size="${WINDOW_SIZE}" \
  --window-position=0,0 \
  --start-maximized \
  --password-store=basic \
  --use-mock-keychain \
  --disable-breakpad \
  --disable-component-update \
  --disable-background-networking \
  --disable-background-timer-throttling \
  --disable-backgrounding-occluded-windows \
  --disable-renderer-backgrounding \
  --disable-sync \
  --metrics-recording-only \
  --disk-cache-dir=/tmp/chromium-cache \
  --disk-cache-size=$(( CACHE_BUDGET_MB * 1024 * 1024 / 4 )) \
  "${START_URL}" >"$CHROME_LOG" 2>&1 &
CHROME_SH_PID=$!

CHROME_PID=""
for _ in $(seq 1 100); do
  CHROME_PID=$(pgrep -n -f "remote-debugging-port=${CDP_PORT}" || true)
  [[ -n "$CHROME_PID" ]] && break
  sleep 0.1
done
[[ -n "$CHROME_PID" ]] || { log "FATAL: Chromium did not appear in /proc"; exit 3; }
CHROME_PGID=$(ps -o pgid= -p "$CHROME_PID" | tr -d ' ')
log "chromium pid=${CHROME_PID} pgid=${CHROME_PGID} cdp=127.0.0.1:${CDP_PORT} profile=${USER_DATA_DIR}"

for _ in $(seq 1 100); do
  curl -s "http://127.0.0.1:${CDP_PORT}/json/version" >/dev/null 2>&1 && break
  sleep 0.1
done

# --- 7b. In-container static Go relay (srf-relay) with bearer auth ----------
RELAY_TOKEN="${SRF_RELAY_TOKEN:-${RELAY_TOKEN:-${SRF_TOKEN:-}}}"
[[ -n "$RELAY_TOKEN" ]] || { log "FATAL: SRF_RELAY_TOKEN is required (fail-closed)"; exit 1; }
export SRF_RELAY_TOKEN="$RELAY_TOKEN"
RELAY_LOG="/tmp/srf-relay.log"
/usr/local/bin/srf-relay \
  --listen "0.0.0.0:${RELAY_PORT}" \
  --target "127.0.0.1:${CDP_PORT}" \
  --public "127.0.0.1:${RELAY_PORT}" >"$RELAY_LOG" 2>&1 &
RELAY_PID=$!

for _ in $(seq 1 100); do
  curl -s -f -H "Authorization: Bearer ${RELAY_TOKEN}" "http://127.0.0.1:${RELAY_PORT}/json/version" >/dev/null 2>&1 && break
  sleep 0.1
done
log "srf-relay ready on 0.0.0.0:${RELAY_PORT} pid=${RELAY_PID} target=127.0.0.1:${CDP_PORT}"

# --- 8. Capa xpra -----------------------------------------------------------
XPRA_READONLY="$XPRA_READONLY" DISPLAY_NUM="$DISPLAY_NUM" XPRA_BIND_PORT="$XPRA_BIND_PORT" \
  /usr/local/bin/xpra-layer.sh start
log "xpra layer up on 0.0.0.0:${XPRA_BIND_PORT} readonly=${XPRA_READONLY}"

# --- 9. Teardown ------------------------------------------------------------
shutdown() {
  log "shutting down"
  /usr/local/bin/xpra-layer.sh stop || true
  kill -TERM "$RELAY_PID" 2>/dev/null || true
  kill -TERM "-${CHROME_PGID}" 2>/dev/null || true
  for _ in $(seq 1 30); do kill -0 "$CHROME_PID" 2>/dev/null || break; sleep 0.5; done
  kill -KILL "-${CHROME_PGID}" 2>/dev/null || true
  kill -KILL "$RELAY_PID" 2>/dev/null || true
  kill -TERM "$WM_PID" "$XVFB_PID" 2>/dev/null || true
  sleep 0.3
  kill -KILL "$WM_PID" "$XVFB_PID" 2>/dev/null || true
  purge_locks "$USER_DATA_DIR"
  evict_cache "$USER_DATA_DIR"
  log "shutdown complete"
}
trap shutdown TERM INT

while kill -0 "$CHROME_PID" 2>/dev/null; do sleep 2; done
log "chromium terminated; closing capsule"
shutdown
exit 0
