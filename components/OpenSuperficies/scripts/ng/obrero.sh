#!/usr/bin/env bash
# Plan v3.5 (A-28..A-34) instrument: the ONLY way the metaorchestrator dispatches workers.
# It pins the caged launcher, the models, the ceilings and the receipt format, so the MO never types a raw model command.
#
#   obrero.sh gemini   --unidad U-NN --sobre <envelope.xml|md> --salida <out.json> [--tope-s 1800] [--rol constructor|auditor]
#   obrero.sh deepseek --encargo <task.txt>|--texto '<task>' --salida <out.json> [--permiso sintesis|lectura|newton] [--esquema <schema.json>]
#   obrero.sh enjambre --encargos <tareas.txt>|--dir-encargos <dir> --dir <outdir> [--k 6] [--permiso lectura]
#   obrero.sh gate     --dir <outdir> [--n <expected>]   |   --f <w.json>     # mechanical verdict over worker output
#   obrero.sh estado   --dir <outdir>          # receipts of a batch: rc, seconds, tokens, output path
#
# Hard ceilings (A-31): at most 6 concurrent DeepSeek workers (--k is clamped); Gemini builders are not capped by this
# instrument but each one runs in its own Newton cage of the `agy-obrero` row. Every launch is journalled under
# _estado/obreros/registro.jsonl (one O_APPEND line each) so the audit trail exists without reading the worker output.
set -euo pipefail
export LC_ALL=C
R=/home/user/OpenSuperficies
AGY=/home/user/.local/bin/shims/agy-obrero
CAT=${NEWTON_RAIZ:-/home/user/Documentos/newton-shell}/scripts/agent
resolver() { command -v "$1" 2>/dev/null || { [[ -x "$CAT/$1" ]] && printf '%s' "$CAT/$1"; }; }
DS=$(resolver scr-obrero-deepseek); ENJ=$(resolver scr-obrero-enjambre); GATE=$(resolver scr-obrero-gate)
MODELO_GEMINI=${OBRERO_MODELO_GEMINI:-gemini-3.8-flash-high}
REG=$R/_estado/obreros/registro.jsonl
K_MAX_DEEPSEEK=6

die() { printf '{"ok":false,"rc":%d,"causa":"%s"}\n' "$1" "$2" >&2; exit "$1"; }
reg() { # one atomic line per launch
  mkdir -p "$(dirname "$REG")"
  python3 -B -c 'import json,sys,os,fcntl,datetime
l=json.dumps({"ts":datetime.datetime.now().astimezone().isoformat(timespec="seconds"),"clase":sys.argv[1],"modelo":sys.argv[2],"unidad":sys.argv[3],"rol":sys.argv[4],"salida":sys.argv[5],"rc":int(sys.argv[6]),"segundos":float(sys.argv[7])},ensure_ascii=False)+"\n"
b=l.encode()
assert len(b)<=4096
fd=os.open(sys.argv[8],os.O_WRONLY|os.O_CREAT|os.O_APPEND,0o644)
fcntl.flock(fd,fcntl.LOCK_EX); os.write(fd,b); os.fsync(fd); os.close(fd)' "$@" "$REG"
}

parada() { "$R/scripts/ng/campana.sh" parada >/dev/null || die 3 parada_activa; }

cmd_gemini() {
  local unidad="" sobre="" salida="" tope=${OBRERO_TOPE_GEMINI_S:-3600} rol=constructor
  while [[ $# -gt 0 ]]; do case "$1" in
    --unidad) unidad=$2; shift 2;; --sobre) sobre=$2; shift 2;; --salida) salida=$2; shift 2;;
    --tope-s) tope=$2; shift 2;; --rol) rol=$2; shift 2;; *) die 2 "arg_desconocido:$1";; esac; done
  [[ -n $unidad && -r $sobre && -n $salida ]] || die 2 argumentos_incompletos
  [[ -x $AGY ]] || die 127 "binario_ausente:agy-obrero"
  parada
  mkdir -p "$(dirname "$salida")"
  local t0 rc; t0=$(date +%s)
  set +e
  ( cd "$R" && timeout "${tope}s" "$AGY" -p "$(cat "$sobre")" \
      --model "$MODELO_GEMINI" --dangerously-skip-permissions \
      --output-format json --print-timeout "${tope}s" --add-dir "$R" ) > "$salida" 2> "${salida%.json}.err"
  rc=$?
  set -e
  reg gemini "$MODELO_GEMINI" "$unidad" "$rol" "$salida" "$rc" "$(( $(date +%s) - t0 ))"
  jq -c '{ok:(.status=="SUCCESS"),status,duration_seconds,num_turns,usage,denied:(.denied_actions|length)}' "$salida" 2>/dev/null \
    || printf '{"ok":false,"rc":%d,"salida":"%s"}\n' "$rc" "$salida"
  return $rc
}

cmd_deepseek() {
  local args=() salida="" unidad="${OBRERO_UNIDAD:-}"
  while [[ $# -gt 0 ]]; do case "$1" in
    --salida) salida=$2; args+=(--salida "$2"); shift 2;;
    --unidad) unidad=$2; shift 2;;
    *) args+=("$1"); shift;; esac; done
  [[ -n $salida ]] || die 2 salida_ausente
  [[ -n $DS && -x $DS ]] || die 127 "binario_ausente:scr-obrero-deepseek"
  parada
  local t0 rc; t0=$(date +%s)
  # the piece defaults to 45 s and 12 tool calls and that is where workers were dying; a wall stops a hang,
  # it does not ration work (operator rule 2026-09-18). Callers may still override with their own flags.
  local extra=()
  [[ " ${args[*]} " == *" --tope-wall "* ]] || extra+=(--tope-wall "${OBRERO_TOPE_WALL:-720s}")
  [[ " ${args[*]} " == *" --tope-tools "* ]] || extra+=(--tope-tools "${OBRERO_TOPE_TOOLS:-40}")
  set +e; ( cd "$R" && $DS "${args[@]}" "${extra[@]}" --eco ); rc=$?; set -e
  reg deepseek deepseek-v4.1-flash "${unidad:-n/a}" auditor "$salida" "$rc" "$(( $(date +%s) - t0 ))"
  return $rc
}

cmd_enjambre() {
  local fuente=() dir="" k=$K_MAX_DEEPSEEK permiso=lectura tope_wall=${OBRERO_TOPE_WALL:-720s} tope_tools=${OBRERO_TOPE_TOOLS:-40}
  while [[ $# -gt 0 ]]; do case "$1" in
    --encargos) fuente=(--encargos "$2"); shift 2;; --dir-encargos) fuente=(--dir-encargos "$2"); shift 2;;
    --dir) dir=$2; shift 2;; --k) k=$2; shift 2;;
    --permiso) permiso=$2; shift 2;; --tope-wall) tope_wall=$2; shift 2;; --tope-tools) tope_tools=$2; shift 2;; *) die 2 "arg_desconocido:$1";; esac; done
  [[ ${#fuente[@]} -eq 2 && -n $dir ]] || die 2 argumentos_incompletos
  [[ $k =~ ^[0-9]+$ ]] || die 2 k_invalida
  (( k > K_MAX_DEEPSEEK )) && k=$K_MAX_DEEPSEEK   # A-31: never more than 6 concurrent DeepSeek workers
  [[ -n $ENJ && -x $ENJ ]] || die 127 "binario_ausente:scr-obrero-enjambre"
  parada
  local t0 rc; t0=$(date +%s)
  set +e; ( cd "$R" && $ENJ "${fuente[@]}" --dir "$dir" --k "$k" --permiso "$permiso" --tope-wall "$tope_wall" --tope-tools "$tope_tools" ); rc=$?; set -e
  reg enjambre deepseek-v4.1-flash n/a auditor "$dir" "$rc" "$(( $(date +%s) - t0 ))"
  return $rc
}

cmd_estado() {
  local dir=""
  while [[ $# -gt 0 ]]; do case "$1" in --dir) dir=$2; shift 2;; *) die 2 "arg_desconocido:$1";; esac; done
  [[ -d $dir ]] || die 2 dir_ausente
  python3 -B -c 'import json,sys,glob,os
out=[]
for f in sorted(glob.glob(os.path.join(sys.argv[1],"*.json"))):
    try: d=json.load(open(f))
    except Exception: out.append({"f":os.path.basename(f),"ok":False,"causa":"json_ilegible"}); continue
    if isinstance(d,list): d=d[-1] if d else {}
    out.append({"f":os.path.basename(f),"ok":d.get("status")=="SUCCESS" or d.get("ok") is True,
                "status":d.get("status"),"segundos":d.get("duration_seconds"),
                "tokens":(d.get("usage") or {}).get("total_tokens")})
print(json.dumps({"n":len(out),"obreros":out},ensure_ascii=False))' "$dir"
}

cmd_gate() {
  [[ -n $GATE && -x $GATE ]] || die 127 "binario_ausente:scr-obrero-gate"
  ( cd "$R" && "$GATE" "$@" )
}

[[ $# -ge 1 ]] || die 2 "uso: obrero.sh {gemini|deepseek|enjambre|gate|estado} ..."
sub=$1; shift
case "$sub" in
  gemini)   cmd_gemini "$@" ;;
  deepseek) cmd_deepseek "$@" ;;
  enjambre) cmd_enjambre "$@" ;;
  estado)   cmd_estado "$@" ;;
  gate)     cmd_gate "$@" ;;
  *) die 2 "subcomando_desconocido:$sub" ;;
esac
