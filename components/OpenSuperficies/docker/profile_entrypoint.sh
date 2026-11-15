#!/usr/bin/env bash
# [CANONICAL]: /home/user/OpenSuperficies/docker/profile_entrypoint.sh
# Mitad de PERFIL del PID 1 de una capsula Topologia B: cuarentena, layout,
# escritor unico, purga de locks y eviction de cache. U-02 la compone con la pila
# grafica para formar entrypoint.sh. Aqui NO vive nada grafico.
#
#   /data/profile  <- bind mount de <base>/perfil
#
# Invariantes:
#   * exactamente un escritor por user-data-dir, siempre;
#   * los Singleton* de un SIGKILL/OOM se purgan ANTES de arrancar;
#   * las caches se evictan mientras la identidad de la sesion sobrevive;
#   * si el flock falla se sale con 69 sin tocar un byte del perfil;
#   * 0 huerfanos tras el teardown.
set -Eeuo pipefail

PROFILE_ROOT="${PROFILE_ROOT:-/data/profile}"
PROFILE_MODE="${PROFILE_MODE:-persistent}"
CACHE_BUDGET_MB="${CACHE_BUDGET_MB:-512}"
LOCKDIR="$PROFILE_ROOT/.orchestrator"

log() { printf '[perfil %s] %s\n' "$(date -Is)" "$*" >&2; }

# --- 1. Cuarentena: reafirmada aunque ya venga del ENV de la imagen -----------
export DBUS_SESSION_BUS_ADDRESS="disabled:" DBUS_SYSTEM_BUS_ADDRESS="disabled:"
export XDG_RUNTIME_DIR="/run/newton-tenant" XDG_CURRENT_DESKTOP="" XDG_SESSION_TYPE="x11"
export GTK_USE_PORTAL=0 GDK_BACKEND=x11 QT_QPA_PLATFORM=xcb NO_AT_BRIDGE=1 GIO_USE_VFS=local
mkdir -p "$XDG_RUNTIME_DIR" && chmod 0700 "$XDG_RUNTIME_DIR" || true

# --- 2. Layout del perfil ----------------------------------------------------
mkdir -p "$PROFILE_ROOT/persistent" "$PROFILE_ROOT/scratch" "$LOCKDIR"
if [[ "$PROFILE_MODE" == "ephemeral" ]]; then
  RUN_ID="${RUN_ID:-$(cat /proc/sys/kernel/random/uuid)}"
  USER_DATA_DIR="$PROFILE_ROOT/scratch/$RUN_ID"
  mkdir -p "$USER_DATA_DIR"
else
  USER_DATA_DIR="$PROFILE_ROOT/persistent"
fi
export USER_DATA_DIR

# --- 3. Escritor unico: la garantia real, no el SingletonLock de Chromium -----
# R-23 / CIR-1: el candado va al DIRECTORIO EFECTIVO. El persistente sigue
# tomando la raiz en exclusiva (dos persistentes siguen dando 69, B-09); el
# efimero toma su propio scratch y NO retiene la raiz durante la corrida.
if [[ "$PROFILE_MODE" == "ephemeral" ]]; then
  LOCKFILE="$USER_DATA_DIR/.lock"
else
  LOCKFILE="$LOCKDIR/profile.flock"
fi
exec 9>"$LOCKFILE"
if ! flock -n 9; then
  log "FATAL: candado $LOCKFILE tomado por otro contenedor vivo (modo $PROFILE_MODE)"
  exit 69
fi
if [[ "$PROFILE_MODE" == "ephemeral" ]]; then
  # El trap nace DESPUES del candado: si otro contenedor sostiene este scratch,
  # salir con 69 no puede borrarle el directorio en vuelo.
  trap 'rm -rf --one-file-system "$USER_DATA_DIR"' EXIT
  if [[ -n "${SEED_FROM_PERSISTENT:-}" ]]; then
    # Copiar Cookies de un Chromium vivo es copiar SQLite en vuelo: la siembra
    # pide la raiz en COMPARTIDO con espera corta y, si un persistente la tiene
    # en exclusiva, se rechaza (R-23 punto 3; sembrar de un perfil vivo = R-23-bis).
    exec 8>"$LOCKDIR/profile.flock"
    if ! flock -s -w 5 8; then
      log "FATAL: no se puede sembrar: el perfil persistente lo sostiene un contenedor vivo (candado de la raiz en exclusiva): candado $LOCKDIR/profile.flock no concedido en compartido tras 5 s (modo ephemeral con SEED_FROM_PERSISTENT)"
      exit 69
    fi
    for rel in "Default/Cookies" "Default/Cookies-journal" "Default/Local Storage" "Local State"; do
      src="$PROFILE_ROOT/persistent/$rel"; [[ -e "$src" ]] || continue
      mkdir -p "$USER_DATA_DIR/$(dirname "$rel")"; cp -a "$src" "$USER_DATA_DIR/$rel"
    done
    exec 8>&-
    log "perfil sembrado desde persistent/ (solo cookies y estado de sesion) bajo candado compartido de $LOCKDIR/profile.flock, ya soltado"
  fi
else
  echo "$$" > "$LOCKDIR/last-pid" || true
fi
log "candado $LOCKFILE tomado en exclusiva (modo $PROFILE_MODE)"

# --- 4. Purga de locks residuales (SIGKILL / OOM) ----------------------------
purge_locks() {
  local d="$1" removed=0
  for f in SingletonLock SingletonSocket SingletonCookie; do
    if [[ -L "$d/$f" || -e "$d/$f" ]]; then rm -f "$d/$f" && removed=1; fi
  done
  # "$d/.lock" es el candado vivo del efimero (R-23), no un residuo: GNU find
  # casa '*.lock' con '.lock' y borrarlo dejaria el flock sobre un inodo huerfano.
  find "$d" -maxdepth 4 -type f \( -name 'LOCK' -o -name 'lockfile' -o -name '*.lock' \) \
       ! -path "$d/.lock" -delete 2>/dev/null || true
  for lsf in "$d/Local State" "$d/Default/Preferences"; do
    [[ -f "$lsf" ]] || continue
    python3 - "$lsf" <<'PY' 2>/dev/null || true
import json,sys
p=sys.argv[1]
try:
    d=json.load(open(p))
except Exception:
    sys.exit(0)
prof=d.get("profile",{})
prof["exit_type"]="Normal"; prof["exited_cleanly"]=True
d["profile"]=prof
json.dump(d,open(p,"w"))
PY
  done
  [[ $removed -eq 1 ]] && log "purgados Singleton* residuales en $d"
  return 0
}
purge_locks "$USER_DATA_DIR"

# --- 5. Eviction selectiva de cache ------------------------------------------
evict_cache() {
  local d="$1" used
  used=$(du -sm "$d" 2>/dev/null | cut -f1 || echo 0)
  if (( used > CACHE_BUDGET_MB )); then
    log "perfil en ${used}MB > ${CACHE_BUDGET_MB}MB: evictando caches"
    rm -rf --one-file-system \
      "$d/Default/Cache" "$d/Default/Code Cache" \
      "$d/Default/Service Worker/CacheStorage" "$d/Default/Service Worker/ScriptCache" \
      "$d/Default/GPUCache" "$d/GrShaderCache" "$d/ShaderCache" \
      "$d/GraphiteDawnCache" "$d/component_crx_cache" \
      "$d/Default/DawnGraphiteCache" "$d/Default/DawnWebGPUCache" \
      "$d/Default/optimization_guide_model_and_features_store" \
      2>/dev/null || true
  fi
}
evict_cache "$USER_DATA_DIR"

# --- 6. Supervision del hijo y teardown --------------------------------------
CHILD_PID=""; CHILD_PGID=""
teardown() {
  if [[ -n "$CHILD_PGID" ]]; then
    kill -TERM "-${CHILD_PGID}" 2>/dev/null || true
    for _ in $(seq 1 30); do kill -0 "$CHILD_PID" 2>/dev/null || break; sleep 0.5; done
    kill -KILL "-${CHILD_PGID}" 2>/dev/null || true
  fi
  purge_locks "$USER_DATA_DIR"
  evict_cache "$USER_DATA_DIR"
  log "teardown completo"
}
trap teardown TERM INT

if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
  if [[ "$#" -gt 0 ]]; then
    setsid "$@" &
    CHILD_PID=$!
    CHILD_PGID=$(ps -o pgid= -p "$CHILD_PID" 2>/dev/null | tr -d ' ' || true)
    log "hijo pid=${CHILD_PID} pgid=${CHILD_PGID} perfil=${USER_DATA_DIR}"
    while kill -0 "$CHILD_PID" 2>/dev/null; do sleep 2; done
    teardown
  else
    log "sin hijo: preparacion de perfil terminada en ${USER_DATA_DIR}"
    teardown
  fi
  exit 0
fi
log "perfil listo y cerrojo tomado en ${USER_DATA_DIR} (modo sourceado)"
