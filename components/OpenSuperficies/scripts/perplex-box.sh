#!/usr/bin/env bash
# [CANONICAL]: /home/user/OpenSuperficies/scripts/perplex-box.sh
# Protocolo de sitio sobre una superficie aislada ya viva. Delega el ciclo de
# vida en docker-xpra-surface.sh y la semantica en cdp_runner.py.
set -euo pipefail
export LC_ALL=C

RAIZ="/home/user/OpenSuperficies"
SURFACE="${RAIZ}/scripts/docker-xpra-surface.sh"
RUNNER="${RAIZ}/motor/cdp_runner.py"
ADMISION_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/newton-surface"
SLOTS_PINTANDO_MAX="${SLOTS_PINTANDO_MAX:-4}"     # 4 x --cpus=5 = 20 nucleos

ACCION="${1:-ayuda}"; NOMBRE="${2:-perplexity_pro}"
ENVF="/tmp/xpra-surface-${NOMBRE}/surface.env"
mkdir -p "${ADMISION_DIR}"

cargar() { [ -f "${ENVF}" ] || { echo "superficie '${NOMBRE}' no activa" >&2; exit 1; }; . "${ENVF}"; }

# Control de admision (Q5): token de PINTADO por flock. Los residentes ociosos
# (0,1-0,3 nucleos) no consumen token; solo consume quien va a hacer pintar.
tomar_token() {
  local i
  for i in $(seq 1 "${SLOTS_PINTANDO_MAX}"); do
    exec {TOKEN_FD}>"${ADMISION_DIR}/paint.${i}.lock"
    if flock -n "${TOKEN_FD}"; then echo "token de pintado ${i}/${SLOTS_PINTANDO_MAX}" >&2; return 0; fi
    eval "exec ${TOKEN_FD}>&-"
  done
  echo "los ${SLOTS_PINTANDO_MAX} slots de pintado estan ocupados; esperando..." >&2
  exec {TOKEN_FD}>"${ADMISION_DIR}/paint.1.lock"; flock "${TOKEN_FD}"
}

case "${ACCION}" in
  adjuntar)
    FICHERO="${3:?ruta del fichero en el host}"
    cargar
    BASE="$(basename "${FICHERO}")"
    cp -f "${FICHERO}" "${DIR_UPLOADS}/${BASE}"
    # La ruta que viaja a CDP es la DEL NAVEGADOR, no la del host.
    python3 "${RUNNER}" adjuntar --puerto "${CDP_PORT}" \
      --fichero "/uploads/${BASE}" --nombre "${BASE}"
    ;;

  consultar)
    PROMPT="${3:?prompt}"
    DESTINO="${4:-}"
    ADJUNTO="${ADJUNTO:-}"
    cargar
    RUN_ID="$(date -u +%Y%m%dT%H%M%SZ)-$$"
    # Q4: default en staging, NUNCA dentro de un output/ de campana OpenFermat.
    [ -n "${DESTINO}" ] || DESTINO="${DIR_COSECHA}/${RUN_ID}.md"
    ARG_ADJ=()
    if [ -n "${ADJUNTO}" ]; then
      BASE="$(basename "${ADJUNTO}")"
      cp -f "${ADJUNTO}" "${DIR_UPLOADS}/${BASE}"
      ARG_ADJ=(--adjunto "/uploads/${BASE}" --nombre "${BASE}")
    fi
    tomar_token
    set +e
    python3 "${RUNNER}" consultar --puerto "${CDP_PORT}" \
      --prompt "${PROMPT}" --destino "${DESTINO}" "${ARG_ADJ[@]}" \
      --budget-s "${BUDGET_S:-900}"
    RC=$?
    set -e
    case "${RC}" in
      0) echo "OK  -> ${DESTINO}" ;;
      4) echo "PARCIAL (el gate de fin no vio el final) -> ${DESTINO}" >&2 ;;
      6) echo "AMBIGUO: pudo enviarse y COBRARSE. Sellado needs_human. NO reintentar." >&2 ;;
      7) echo "REQUIERE HUMANO. Toma el mando y resuelvelo a mano:" >&2
         echo "  ${SURFACE} intervenir ${NOMBRE}" >&2 ;;
    esac
    exit "${RC}"
    ;;

  cosechar)
    cargar; ls -la "${DIR_COSECHA}" ;;

  *)
    cat >&2 <<AYUDA
uso: $0 <accion> [instancia] [args]
  adjuntar  <inst> <fichero>                    stagea en /uploads y ejecuta G-ATTACH
  consultar <inst> <prompt> [destino]           envia, vigila el stream y cosecha
                                                  ADJUNTO=/ruta  BUDGET_S=900
  cosechar  <inst>                              lista las cosechas de la instancia
AYUDA
    exit 2 ;;
esac
