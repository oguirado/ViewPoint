#!/usr/bin/env bash
# sondas-cuarentena.sh - Gestor del arnes de sondas para verificacion de cuarentena
set -euo pipefail
export LC_ALL=C

ACCION="${1:-ayuda}"
IMAGEN="opensuperficies-browser-arm64:1.0.0"
RED="newton-srf-probe"
CTR_PROBE="srf-probe"
CTR_VENENO="srf-probe-poison"
XPRA_PORT=10099
CDP_PORT=9299

esperar_puerto() {
  local p="$1"
  for _ in $(seq 1 60); do
    if curl -fsS --max-time 1 "http://127.0.0.1:${p}/" >/dev/null 2>&1; then
      return 0
    fi
    sleep 0.1
  done
  echo "aviso: el puerto ${p} no respondio en el tiempo de espera" >&2
  return 1
}

case "$ACCION" in
  arrancar)
    docker network create "${RED}" >/dev/null 2>&1 || true
    docker rm -f "${CTR_PROBE}" >/dev/null 2>&1 || true
    sleep 0.2
    docker run -d \
      --name "${CTR_PROBE}" \
      --network "${RED}" \
      --shm-size=2gb \
      --security-opt no-new-privileges \
      --cap-drop=ALL \
      --user "$(id -u):0" \
      -p "127.0.0.1:${XPRA_PORT}:10000" \
      -p "127.0.0.1:${CDP_PORT}:9222" \
      --tmpfs /tmp:rw,nosuid,nodev,size=512m \
      -e DISPLAY_NUM=100 \
      -e XPRA_READONLY=yes \
      "${IMAGEN}" >/dev/null
    esperar_puerto "${XPRA_PORT}" || true
    ;;
  arrancar-veneno)
    docker network create "${RED}" >/dev/null 2>&1 || true
    docker rm -f "${CTR_VENENO}" >/dev/null 2>&1 || true
    sleep 0.2
    docker run -d \
      --name "${CTR_VENENO}" \
      --network "${RED}" \
      --shm-size=2gb \
      --security-opt no-new-privileges \
      --cap-drop=ALL \
      --user "$(id -u):0" \
      -p "127.0.0.1:${XPRA_PORT}:10000" \
      -p "127.0.0.1:${CDP_PORT}:9222" \
      -v /tmp/.X11-unix:/tmp/.X11-unix:ro \
      --tmpfs /tmp:rw,nosuid,nodev,size=512m \
      -e DISPLAY_NUM=100 \
      -e XPRA_READONLY=yes \
      "${IMAGEN}" >/dev/null
    esperar_puerto "${XPRA_PORT}" || true
    ;;
  parar)
    docker rm -f "${CTR_PROBE}" "${CTR_VENENO}" >/dev/null 2>&1 || true
    for _ in $(seq 1 20); do
      docker network rm "${RED}" >/dev/null 2>&1 && break || sleep 0.1
    done
    ;;
  *)
    echo "uso: $0 {arrancar|arrancar-veneno|parar}" >&2
    exit 2
    ;;
esac
