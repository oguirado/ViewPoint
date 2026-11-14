#!/usr/bin/env bash
# [CANONICAL]: /home/user/OpenSuperficies/scripts/consultar-perplexity.sh
# Bridge operacional e interfaz CLI de Perplexity para Claude Sonnet 5 y agentes
# de OpenFresnel / OpenSuperficies / Newton-Shell bajo mutex estricto (flock).
# Conforme a docs/GUIA-SUPERFICIE-PERPLEXITY-COMPARTIDA.md y ESTRATEGIA_OPERACION_DOCKER_XPRA_PERPLEXITY.md
set -euo pipefail
export LC_ALL=C

RAIZ_SUPERFICIES="/home/user/OpenSuperficies"
PERPLEX_BOX="${RAIZ_SUPERFICIES}/scripts/perplex-box.sh"
DOCKER_SURFACE="${RAIZ_SUPERFICIES}/scripts/docker-xpra-surface.sh"
INSTANCIA="perplexity"
CONTENEDOR="newton-srf-${INSTANCIA}"

RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/newton-surface"
LOCK_FILE="${RUNTIME_DIR}/perplexity.campana.lock"
DEFAULT_LOCK_TIMEOUT=1200
DEFAULT_BUDGET_S=900

# Codigos de retorno estandarizados
RC_OK=0
RC_LIMPIO=1
RC_USO=2
RC_SUSTRATO=3
RC_PARCIAL=4
RC_OCUPADA=5
RC_AMBIGUO=6
RC_HUMANO=7
RC_ABORTADA=8
RC_NOAUTH=9
RC_LOCK_TIMEOUT=75

FD_LOCK=200
LOCK_ACQUIRED=0

liberar_lock() {
  if [ "${LOCK_ACQUIRED}" -eq 1 ]; then
    flock -u "${FD_LOCK}" 2>/dev/null || true
    eval "exec ${FD_LOCK}>&-" 2>/dev/null || true
    LOCK_ACQUIRED=0
  fi
}
trap liberar_lock EXIT INT TERM HUP

imprimir_uso() {
  cat <<AYUDA
Uso: $0 [OPCIONES] "<prompt>" [destino.md]
     $0 {estado|--check}
     $0 cosechar

Bridge CLI de Perplexity Pro / Deep Research con exclusion mutua estricta.

Argumentos posicionales:
  prompt                Texto de la consulta en lenguaje natural (ingles recomendado).
  destino               Ruta absoluta o relativa del archivo markdown destino.
                        (Por defecto: staging/superficies/perplexity/cosecha/<id>.md)

Opciones:
  -q, --query TEXT      Texto de la consulta (alternativa a argumento posicional).
  -o, --destino PATH    Ruta de destino del informe cosechado (.md).
  -a, --adjunto PATH    Ruta de archivo local en el host a adjuntar (stageado en /uploads).
  -b, --budget SECONDS  Presupuesto maximo de streaming CDP en segundos (def: ${DEFAULT_BUDGET_S}s).
  -t, --timeout SECONDS Timeout de espera de adquisicion del lock flock (def: ${DEFAULT_LOCK_TIMEOUT}s).
  --format FORMAT       Formato de salida a stdout:
                          json   (defecto: objeto estructurado con metadatos y estado)
                          path   (solo la ruta absoluta al markdown cosechado)
                          cat    (vuelca el contenido cosechado a stdout)
  --check, estado       Comprueba salud del contenedor, conectividad CDP y sesion activa.
  cosechar              Lista las cosechas existentes en el directorio de staging.
  -h, --help            Muestra esta ayuda y termina.

Codigos de salida (RC):
  0   OK: Respuesta completa generada y cosechada con todos los gates verdes.
  1   LIMPIO: Fallo previo al envio (ej. fallo en G-ATTACH). No se consumio cuota.
  2   USO: Parametros de invocacion invalidos.
  3   SUSTRATO: Contenedor no activo, puerto CDP caido o entorno corrupto.
  4   PARCIAL: Stream no vio token de fin de generacion; archivo contiene respuesta parcial.
  6   AMBIGUO: G-SUBMIT no confirmado; pudo enviarse y cobrarse. ALERTA HUMANA. NO REINTENTAR.
  7   HUMANO: Desafio interactivo (Turnstile/Captcha) o muro de login. REQUIERE OPERADOR.
  75  LOCK_TIMEOUT: El recurso estuvo ocupado durante mas de ${DEFAULT_LOCK_TIMEOUT}s.
AYUDA
}

emitir_json_error() {
  local rc="$1"
  local status="$2"
  local mensaje="$3"
  local accion="$4"
  local extra="${5:-}"

  python3 -c '
import json, sys
data = {
    "status": sys.argv[2],
    "rc": int(sys.argv[1]),
    "error": sys.argv[3],
    "action_required": sys.argv[4]
}
extra = sys.argv[5]
if extra:
    try:
        data.update(json.loads(extra))
    except Exception:
        data["extra_raw"] = extra
print(json.dumps(data, indent=2, ensure_ascii=False))
' "${rc}" "${status}" "${mensaje}" "${accion}" "${extra}"
}

verificar_preflight_contenedor() {
  if [ ! -x "${DOCKER_SURFACE}" ] || [ ! -x "${PERPLEX_BOX}" ]; then
    emitir_json_error "${RC_SUSTRATO}" "SUBSTRATE_ERROR" \
      "Scripts de soporte no ejecutables en ${RAIZ_SUPERFICIES}" \
      "VERIFY_PERMISSIONS" "" >&2
    exit "${RC_SUSTRATO}"
  fi

  local estado_docker
  estado_docker="$(docker inspect -f '{{.State.Status}}' "${CONTENEDOR}" 2>/dev/null || echo "ausente")"
  if [ "${estado_docker}" != "running" ]; then
    emitir_json_error "${RC_SUSTRATO}" "CONTAINER_INACTIVE" \
      "El contenedor '\''${CONTENEDOR}'\'' no esta activo (estado: ${estado_docker})." \
      "OPERATOR_START_CONTAINER" \
      "{\"command_to_start\": \"${DOCKER_SURFACE} iniciar ${INSTANCIA} https://www.perplexity.ai/ 1280 900\"}" >&2
    exit "${RC_SUSTRATO}"
  fi

  local envf="/tmp/xpra-surface-${INSTANCIA}/surface.env"
  if [ ! -f "${envf}" ]; then
    emitir_json_error "${RC_SUSTRATO}" "CONTAINER_ENV_MISSING" \
      "Archivo de variables '\''${envf}'\'' no encontrado." \
      "OPERATOR_INSPECT_CONTAINER" "" >&2
    exit "${RC_SUSTRATO}"
  fi
}

ejecutar_estado() {
  verificar_preflight_contenedor
  local salida rc
  set +e
  salida="$("${DOCKER_SURFACE}" estado "${INSTANCIA}" 2>&1)"
  rc=$?
  set -e

  python3 -c '
import sys, json, re

raw = sys.argv[1]
rc = int(sys.argv[2])

resultado = {
    "status": "OK" if rc == 0 else "ERROR",
    "rc": rc,
    "container": "newton-srf-perplexity",
    "raw_output": raw
}

# Parse env lines
for line in raw.splitlines():
    if "=" in line and not line.startswith("{"):
        k, v = line.split("=", 1)
        resultado[k.strip().lower()] = v.strip()

# Parse JSON state line from cdp_runner if present
match = re.search(r"\{.*\"autenticado\".*\}", raw)
if match:
    try:
        cdp_json = json.loads(match.group(0))
        resultado["cdp_state"] = cdp_json
        resultado["authenticated"] = cdp_json.get("autenticado", False)
        resultado["challenge_active"] = cdp_json.get("desafio", False)
    except Exception:
        pass

if resultado.get("authenticated") is False:
    resultado["status"] = "HUMAN_REQUIRED"
    resultado["action_required"] = "OPERATOR_LOGIN_REQUIRED"
elif resultado.get("challenge_active") is True:
    resultado["status"] = "HUMAN_REQUIRED"
    resultado["action_required"] = "OPERATOR_SOLVE_CHALLENGE"
else:
    resultado["action_required"] = "READY_FOR_QUERIES"

print(json.dumps(resultado, indent=2, ensure_ascii=False))
' "${salida}" "${rc}"

  exit "${rc}"
}

# ==============================================================================
# Parseo de Argumentos CLI
# ==============================================================================
QUERY=""
DESTINO=""
ADJUNTO=""
BUDGET_S="${DEFAULT_BUDGET_S}"
LOCK_TIMEOUT="${DEFAULT_LOCK_TIMEOUT}"
FORMATO="json"

while [ $# -gt 0 ]; do
  case "$1" in
    estado|--check)
      ejecutar_estado
      ;;
    cosechar)
      verificar_preflight_contenedor
      exec "${PERPLEX_BOX}" cosechar "${INSTANCIA}"
      ;;
    -q|--query)
      QUERY="${2:?Falta texto para --query}"; shift 2 ;;
    -o|--destino)
      DESTINO="${2:?Falta ruta para --destino}"; shift 2 ;;
    -a|--adjunto)
      ADJUNTO="${2:?Falta ruta para --adjunto}"; shift 2 ;;
    -b|--budget)
      BUDGET_S="${2:?Falta valor numerico para --budget}"; shift 2 ;;
    -t|--timeout)
      LOCK_TIMEOUT="${2:?Falta valor numerico para --timeout}"; shift 2 ;;
    --format)
      FORMATO="${2:?Falta formato (json|path|cat)}"; shift 2 ;;
    -h|--help)
      imprimir_uso; exit 0 ;;
    --)
      shift; break ;;
    -*)
      echo "Opcion desconocida: $1" >&2
      imprimir_uso >&2
      exit "${RC_USO}"
      ;;
    *)
      if [ -z "${QUERY}" ]; then
        QUERY="$1"
      elif [ -z "${DESTINO}" ]; then
        DESTINO="$1"
      else
        echo "Argumento posicional sobrante: $1" >&2
        imprimir_uso >&2
        exit "${RC_USO}"
      fi
      shift
      ;;
  esac
done

if [ -z "${QUERY}" ]; then
  echo "Error: Se requiere especificar una consulta." >&2
  imprimir_uso >&2
  exit "${RC_USO}"
fi

# Validar adjunto si se especifico
if [ -n "${ADJUNTO}" ]; then
  if [ ! -f "${ADJUNTO}" ]; then
    emitir_json_error "${RC_USO}" "ATTACHMENT_NOT_FOUND" \
      "El archivo adjunto especificado no existe: ${ADJUNTO}" \
      "CHECK_ATTACHMENT_PATH" "" >&2
    exit "${RC_USO}"
  fi
  ADJUNTO="$(realpath "${ADJUNTO}")"
fi

# Validar destino si se especifico
if [ -n "${DESTINO}" ]; then
  DESTINO_DIR="$(dirname "${DESTINO}")"
  mkdir -p "${DESTINO_DIR}"
  DESTINO="$(realpath -m "${DESTINO}")"
fi

# ==============================================================================
# Fase 1: Preflight del Contenedor (Rapido, antes de bloquear)
# ==============================================================================
verificar_preflight_contenedor

# ==============================================================================
# Fase 2: Adquisicion del Mutex de Campana (flock -w 1200)
# ==============================================================================
mkdir -p "${RUNTIME_DIR}"
eval "exec ${FD_LOCK}>'${LOCK_FILE}'"

T_INICIO=$(date +%s%N)
if ! flock -w "${LOCK_TIMEOUT}" "${FD_LOCK}"; then
  T_FIN=$(date +%s%N)
  ELAPSED=$(( (T_FIN - T_INICIO) / 1000000000 ))
  emitir_json_error "${RC_LOCK_TIMEOUT}" "LOCK_TIMEOUT" \
    "No se pudo adquirir el lock '${LOCK_FILE}' tras ${ELAPSED}s (limite: ${LOCK_TIMEOUT}s). Superficie ocupada por otra campana." \
    "BACKOFF_OR_FALLBACK" \
    "{\"lock_file\": \"${LOCK_FILE}\", \"elapsed_s\": ${ELAPSED}, \"timeout_s\": ${LOCK_TIMEOUT}}" >&2
  exit "${RC_LOCK_TIMEOUT}"
fi
LOCK_ACQUIRED=1

# ==============================================================================
# Fase 3: Ejecucion de la Consulta dentro del Lock
# ==============================================================================
TMP_OUT="$(mktemp /tmp/perplex_out_XXXXXX.log)"
TMP_ERR="$(mktemp /tmp/perplex_err_XXXXXX.log)"
trap 'liberar_lock; rm -f "${TMP_OUT}" "${TMP_ERR}"' EXIT INT TERM HUP

ENV_CALL=()
[ -n "${ADJUNTO}" ] && ENV_CALL+=(ADJUNTO="${ADJUNTO}")
ENV_CALL+=(BUDGET_S="${BUDGET_S}")

set +e
env "${ENV_CALL[@]}" "${PERPLEX_BOX}" consultar "${INSTANCIA}" "${QUERY}" ${DESTINO:+"${DESTINO}"} >"${TMP_OUT}" 2>"${TMP_ERR}"
RC_CALL=$?
set -e

T_FIN=$(date +%s%N)
ELAPSED_TOTAL=$(awk "BEGIN {printf \"%.2f\", ($T_FIN - $T_INICIO) / 1000000000}")

RAW_OUT="$(cat "${TMP_OUT}" 2>/dev/null || true)"
RAW_ERR="$(cat "${TMP_ERR}" 2>/dev/null || true)"

# ==============================================================================
# Fase 4: Analisis de Veredicto y Emision de Resultados
# ==============================================================================
python3 -c '
import sys, json, os, re

rc = int(sys.argv[1])
formato = sys.argv[2]
elapsed = float(sys.argv[3])
raw_out = sys.argv[4]
raw_err = sys.argv[5]
query = sys.argv[6]
adjunto = sys.argv[7]

# Extraer JSON de cdp_runner si existe en stdout
info_cdp = {}
for line in raw_out.splitlines():
    line = line.strip()
    if line.startswith("{") and line.endswith("}"):
        try:
            parsed = json.loads(line)
            if "gate_cosecha" in parsed or "destino" in parsed:
                info_cdp = parsed
                break
        except Exception:
            pass

# Localizar ruta de destino
destino = info_cdp.get("destino", "")
if not destino:
    m = re.search(r"(?:OK|PARCIAL.*?)\s*->\s*(\S+)", raw_out + "\n" + raw_err)
    if m:
        destino = m.group(1).strip()

# Obtener metricas del archivo destino si existe
file_stats = {}
if destino and os.path.isfile(destino):
    try:
        size = os.path.getsize(destino)
        with open(destino, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
        lines = len(content.splitlines())
        chars = len(content)
        file_stats = {
            "size_bytes": size,
            "chars": chars,
            "lines": lines
        }
    except Exception as e:
        file_stats["read_error"] = str(e)

# Mapeo de estados y acciones segun codigo de salida
status_map = {
    0: ("OK", "READ_OUTPUT", "Consulta completada y cosechada con exito."),
    1: ("CLEAN_FAILURE", "RETRY_ALLOWED", "Fallo limpio antes del envio; nada se envio ni cobro."),
    2: ("USAGE_ERROR", "FIX_ARGUMENTS", "Error de uso o argumentos invalidos."),
    3: ("SUBSTRATE_ERROR", "INSPECT_INFRASTRUCTURE", "Fallo de sustrato (CDP inalcanzable o adaptador invalido)."),
    4: ("PARCIAL", "INSPECT_PARTIAL_OR_RETRY", "El gate de fin no vio el token final; respuesta parcial escrita."),
    5: ("INSTANCE_BUSY", "WAIT_AND_RETRY", "La instancia o perfil esta ocupada por otro proceso."),
    6: ("AMBIGUOUS", "HALT_ALERT_HUMAN_DO_NOT_RETRY", "CRITICO: Pudo enviarse y cobrarse. Sellado needs_human. NO REINTENTAR."),
    7: ("HUMAN_REQUIRED", "HALT_ALERT_HUMAN_INTERVENE", "Desafio interactivo o autenticacion requerida. El operador debe intervenir.")
}

status, action, default_msg = status_map.get(rc, ("UNKNOWN_ERROR", "INSPECT_LOGS", f"Error desconocido con codigo {rc}"))

# Enriquecer informe JSON
payload = {
    "status": status,
    "rc": rc,
    "elapsed_seconds": elapsed,
    "destination": destino,
    "action_required": action,
    "message": default_msg,
    "query": query[:200] + ("..." if len(query) > 200 else ""),
    "attachment": adjunto if adjunto else None,
    "file_metrics": file_stats,
    "cdp_report": info_cdp
}

if rc == 6:
    payload["warning"] = "DO NOT RETRY UNDER ANY CIRCUMSTANCES. Query might be billed."
    payload["intervene_command"] = "/home/user/OpenSuperficies/scripts/docker-xpra-surface.sh intervenir perplexity"

if rc == 7:
    payload["intervene_command"] = "/home/user/OpenSuperficies/scripts/docker-xpra-surface.sh intervenir perplexity"
    payload["viewer_url"] = "http://127.0.0.1:10020/?floating_menu=yes&clipboard=no&read-only=no"

if rc != 0 and raw_err:
    payload["stderr_summary"] = raw_err.strip().splitlines()[-5:]

# Renderizar segun formato
if formato == "json":
    print(json.dumps(payload, indent=2, ensure_ascii=False))
elif formato == "path":
    if destino:
        print(destino)
    else:
        print(json.dumps(payload, ensure_ascii=False), file=sys.stderr)
        sys.exit(rc if rc != 0 else 1)
elif formato == "cat":
    if destino and os.path.isfile(destino):
        with open(destino, "r", encoding="utf-8", errors="replace") as f:
            sys.stdout.write(f.read())
    else:
        print(f"Error: Archivo destino no disponible para cat (rc={rc})", file=sys.stderr)
        if raw_err:
            print(raw_err, file=sys.stderr)
        sys.exit(rc if rc != 0 else 1)
' "${RC_CALL}" "${FORMATO}" "${ELAPSED_TOTAL}" "${RAW_OUT}" "${RAW_ERR}" "${QUERY}" "${ADJUNTO}"

exit "${RC_CALL}"
