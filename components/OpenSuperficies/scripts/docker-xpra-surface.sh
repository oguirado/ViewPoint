#!/usr/bin/env bash
# [CANONICAL]: /home/user/OpenSuperficies/scripts/docker-xpra-surface.sh
# Operador universal de superficies graficas aisladas · Topologia B autonoma.
# Ejecutable desde cualquier terminal: bash, zsh, ssh, CI. No requiere GUI.
set -euo pipefail
export LC_ALL=C

RAIZ="/home/user/OpenSuperficies"
IMAGEN="opensuperficies-browser-arm64:1.0.0"
ACCION="${1:-ayuda}"
NOMBRE="${2:-default}"

DIR_BASE="${RAIZ}/staging/superficies/${NOMBRE}"
DIR_UPLOADS="${DIR_BASE}/uploads"
DIR_PERFIL="${DIR_BASE}/perfil"
DIR_COSECHA="${DIR_BASE}/cosecha"
DIR_RUN="/tmp/xpra-surface-${NOMBRE}"
ENVF="${DIR_RUN}/surface.env"
CONTENEDOR="newton-srf-${NOMBRE}"
RED="newton-srf-${NOMBRE}"

mkdir -p "${DIR_UPLOADS}" "${DIR_PERFIL}" "${DIR_COSECHA}" "${DIR_RUN}"

morir() { echo "error: $*" >&2; exit "${2:-1}"; }

buscar_puertos_libres() {
  # Par (xpra, cdp) libre: 10020..10099 <-> 9220..9299. Comprobado en el host,
  # no en el contenedor: quien colisiona es la publicacion del puerto.
  local p cdp
  for p in $(seq 10020 10099); do
    cdp=$((p - 10000 + 9220))
    if [ -z "$(ss -Htanl "sport = :${p}" 2>/dev/null)" ] &&
       [ -z "$(ss -Htanl "sport = :${cdp}" 2>/dev/null)" ]; then
      echo "${p} ${cdp}"; return 0
    fi
  done
  morir "no hay pares de puertos libres en 10020..10099" 3
}

asegurar_imagen() {
  docker image inspect "${IMAGEN}" >/dev/null 2>&1 && return 0
  echo "construyendo ${IMAGEN} (Debian 12 arm64)..."
  docker build --platform linux/arm64 -t "${IMAGEN}" \
    -f "${RAIZ}/docker/Dockerfile.browser-arm64" "${RAIZ}/docker/"
}

cargar_estado() {
  [ -f "${ENVF}" ] || morir "la superficie '${NOMBRE}' no esta activa" 1
  # shellcheck disable=SC1090
  . "${ENVF}"
}

url_visor() {
  printf 'http://127.0.0.1:%s/?floating_menu=yes&clipboard=no&printing=no&file_transfer=no&sound=no&video=no&reconnect=yes&steal=yes&autohide=yes\n' "$1"
}

case "${ACCION}" in
  iniciar)
    URL="${3:-https://www.perplexity.ai}"
    ANCHO="${4:-1280}"; ALTO="${5:-900}"
    [ -f "${ENVF}" ] && { echo "la superficie '${NOMBRE}' ya esta iniciada:"; cat "${ENVF}"; exit 0; }
    asegurar_imagen
    read -r XPRA_PORT CDP_PORT <<< "$(buscar_puertos_libres)"

    # R-05: red propia por instancia. Un contenedor del bridge por defecto NO
    # alcanza a un contenedor de una red definida por el usuario (medido).
    docker network create "${RED}" >/dev/null 2>&1 || true

    docker rm -f "${CONTENEDOR}" >/dev/null 2>&1 || true
    docker run -d \
      --name "${CONTENEDOR}" \
      --network "${RED}" \
      --shm-size=2gb \
      --memory=4g --memory-swap=4g \
      --cpus=5 \
      --pids-limit=512 \
      --security-opt no-new-privileges \
      --cap-drop=ALL \
      --user "$(id -u):0" \
      -p "127.0.0.1:${XPRA_PORT}:10000" \
      -p "127.0.0.1:${CDP_PORT}:9222" \
      -v "${DIR_PERFIL}:/data/profile:rw" \
      -v "${DIR_UPLOADS}:/uploads:rw" \
      -v "${DIR_COSECHA}:/cosecha:rw" \
      --tmpfs /tmp:rw,nosuid,nodev,size=512m \
      -e DISPLAY_NUM=100 \
      -e GEOMETRY="${ANCHO}x${ALTO}x24" \
      -e WM=openbox \
      -e PROFILE_MODE="${PROFILE_MODE:-persistent}" \
      -e SEED_FROM_PERSISTENT="${SEED_FROM_PERSISTENT:-}" \
      -e XPRA_READONLY=yes \
      -e START_URL="${URL}" \
      "${IMAGEN}" > "${DIR_RUN}/docker.cid"

    # Puente CDP interno (D-U04-04 / R-05): Chromium 152 hardcodea 127.0.0.1:9222.
    # El puerto publicado (-p) conecta a eth0; enrutamos eth0:9222 -> 127.0.0.1:9222.
    # Requisitos D-U04-04: bind estricto a eth0 (jamas 0.0.0.0), techo de conexiones
    # y tiempo de espera de inactividad, y fallo ruidoso (no mudo).
    docker exec -d "${CONTENEDOR}" sh -c 'exec python3 -c '\''
import socket, threading, fcntl, struct

MAX_CONNS = 16
TIMEOUT = 60.0
sem = threading.Semaphore(MAX_CONNS)

def fwd(s, d):
    try:
        s.settimeout(TIMEOUT)
        d.settimeout(TIMEOUT)
        while True:
            b = s.recv(4096)
            if not b: break
            d.sendall(b)
    except Exception: pass
    finally:
        for x in (s, d):
            try: x.close()
            except Exception: pass

def handle(c):
    try:
        cdp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        cdp.settimeout(10.0)
        cdp.connect(("127.0.0.1", 9222))
        t1 = threading.Thread(target=fwd, args=(c, cdp), daemon=True)
        t2 = threading.Thread(target=fwd, args=(cdp, c), daemon=True)
        t1.start()
        t2.start()
        t1.join()
        t2.join()
    except Exception:
        try: c.close()
        except Exception: pass
    finally:
        sem.release()

sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
ip = socket.inet_ntoa(fcntl.ioctl(sock.fileno(), 0x8915, struct.pack("256s", b"eth0"))[20:24])
sock.close()

srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
srv.bind((ip, 9222))
srv.listen(MAX_CONNS)

while True:
    c, _ = srv.accept()
    if not sem.acquire(blocking=False):
        c.close()
        continue
    threading.Thread(target=handle, args=(c,), daemon=True).start()
'\'' >/tmp/cdp-relay.log 2>&1' || morir "fallo al iniciar puente CDP interno D-U04-04" 3

    # Espera a que el endpoint CDP publique el GUID. 69 = perfil ya tomado.
    for _ in $(seq 1 120); do
      curl -fsS --max-time 1 "http://127.0.0.1:${CDP_PORT}/json/version" >/dev/null 2>&1 && break
      if [ "$(docker inspect -f '{{.State.ExitCode}}' "${CONTENEDOR}" 2>/dev/null)" = "69" ]; then
        docker logs --tail 5 "${CONTENEDOR}" >&2
        morir "el perfil de '${NOMBRE}' ya lo sostiene otro contenedor vivo" 5
      fi
      sleep 0.5
    done
    curl -fsS --max-time 2 "http://127.0.0.1:${CDP_PORT}/json/version" >/dev/null 2>&1 \
      || { docker logs --tail 30 "${CONTENEDOR}" >&2; morir "CDP no respondio en 60 s" 3; }

    cat > "${ENVF}" <<ENVEOF
NOMBRE=${NOMBRE}
CONTENEDOR=${CONTENEDOR}
RED=${RED}
XPRA_PORT=${XPRA_PORT}
CDP_PORT=${CDP_PORT}
ANCHO=${ANCHO}
ALTO=${ALTO}
DIR_BASE=${DIR_BASE}
DIR_UPLOADS=${DIR_UPLOADS}
DIR_PERFIL=${DIR_PERFIL}
DIR_COSECHA=${DIR_COSECHA}
READONLY=yes
ENVEOF

    echo "=========================================================="
    echo " Superficie autonoma lista: '${NOMBRE}'  [BLINDADA]"
    echo " Visor:  $(url_visor "${XPRA_PORT}")"
    echo " CDP:    127.0.0.1:${CDP_PORT}"
    echo " Adjuntos: ${DIR_UPLOADS}  ->  /uploads"
    echo " Cosecha:  ${DIR_COSECHA}  ->  /cosecha"
    echo " Tomar el mando: $0 intervenir ${NOMBRE}"
    echo "=========================================================="
    ;;

  intervenir|blindar)
    cargar_estado
    RO=yes; [ "${ACCION}" = "intervenir" ] && RO=no
    # R-02/R-08: readonly es del SERVIDOR y no hay comando de control en 3.1.5.
    # Se recicla SOLO la capa xpra: Xvfb y Chromium no se enteran.
    docker exec -e XPRA_READONLY="${RO}" "${CONTENEDOR}" \
      /usr/local/bin/xpra-layer.sh readonly "${RO}"
    sed -i "s/^READONLY=.*/READONLY=${RO}/" "${ENVF}"
    if [ "${RO}" = "no" ]; then
      echo "MANDO CEDIDO AL OPERADOR — la automatizacion debe estar parada."
      echo "  $(url_visor "${XPRA_PORT}")"
      echo "Al terminar:  $0 blindar ${NOMBRE}"
    else
      echo "SUPERFICIE BLINDADA — el visor vuelve a ser solo pixeles."
    fi
    ;;

  estado)
    if [ -f "${ENVF}" ]; then
      cargar_estado
      cat "${ENVF}"
      echo "SALUD_CONTENEDOR=$(docker inspect -f '{{.State.Status}}' "${CONTENEDOR}" 2>/dev/null || echo ausente)"
      echo "CDP_VERSION=$(curl -fsS --max-time 2 "http://127.0.0.1:${CDP_PORT}/json/version" 2>/dev/null | head -c 120 || echo inalcanzable)"
      echo "URL_VISOR=$(url_visor "${XPRA_PORT}")"
      python3 "${RAIZ}/motor/cdp_runner.py" estado --puerto "${CDP_PORT}" 2>/dev/null || true
    else
      echo "la superficie '${NOMBRE}' no esta activa"; exit 1
    fi
    ;;

  verificar)
    cargar_estado
    exec "${RAIZ}/docker/verify-quarantine.sh" "${CONTENEDOR}" "${XPRA_PORT}"
    ;;

  purgar)
    # Limpieza post-crash sin contenedor vivo: suelta locks residuales.
    rm -f "${DIR_PERFIL}/.orchestrator/profile.flock" 2>/dev/null || true
    find "${DIR_PERFIL}" -maxdepth 3 -name 'Singleton*' -delete 2>/dev/null || true
    echo "locks residuales purgados en ${DIR_PERFIL}"
    ;;

  detener)
    [ -f "${ENVF}" ] || { echo "la superficie '${NOMBRE}' no esta activa"; exit 0; }
    cargar_estado
    docker stop -t 15 "${CONTENEDOR}" >/dev/null 2>&1 || true
    docker rm -f "${CONTENEDOR}" >/dev/null 2>&1 || true
    docker network rm "${RED}" >/dev/null 2>&1 || true
    rm -f "${ENVF}" "${DIR_RUN}/docker.cid"
    HUERFANOS=$(ps -eo args= | grep -c "[n]ewton-srf-${NOMBRE}" || true)
    echo "superficie '${NOMBRE}' destruida · procesos residuales: ${HUERFANOS}"
    ;;

  *)
    cat >&2 <<AYUDA
uso: $0 <accion> <instancia> [args]

  iniciar <inst> [url] [ancho] [alto]   levanta la capsula BLINDADA
  intervenir <inst>                     cede el mando al operador (readonly=no)
  blindar <inst>                        recupera el blindaje (readonly=yes)
  estado <inst>                         salud, puertos, URL del visor, sesion
  verificar <inst>                      auditoria de cuarentena de 14 puertas
  purgar <inst>                         limpia locks residuales sin contenedor
  detener <inst>                        teardown limpio, red incluida

  Variables: PROFILE_MODE=persistent|ephemeral  SEED_FROM_PERSISTENT=1
  'verificar' exige la linea base:  BEFORE_TREE=/tmp/display1-baseline.txt
                                    AW_BASELINE=\$(DISPLAY=:1 xdotool getactivewindow)
  adjuntar y consultar viven en perplex-box.sh (protocolo de dominio)
AYUDA
    exit 2 ;;
esac
