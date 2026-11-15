#!/usr/bin/env bash
set -euo pipefail
URL="${1:-about:blank}"
USER_DATA_DIR="${USER_DATA_DIR:-/data/profile/persistent}"
CDP_PORT="${CDP_PORT:-9222}"
CACHE_BUDGET_MB="${CACHE_BUDGET_MB:-512}"
WINDOW_SIZE="${WINDOW_SIZE:-1280,900}"

exec chromium \
  --user-data-dir="${USER_DATA_DIR}" \
  --remote-debugging-address=0.0.0.0 \
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
  "${URL}"
