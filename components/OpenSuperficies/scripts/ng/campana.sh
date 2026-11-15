#!/usr/bin/env bash
# SCRIPT_HEADER:BEGIN
# {"id":"srf-ng-campana","version":"1.0.0","tipo":"obrero","plazo_ms":15000,"plan_id":"opensuperficies-nextgen","fase":"FASE-0"}
# SCRIPT_HEADER:END
#
# Published campaign instrument for OpenSuperficies Next-Gen. Every figure a
# campaign agent reports about stop signal, baseline, checkpoint, residues,
# Plaza lines or journal turns MUST come from this instrument, never typed.
#
#   campana.sh parada                      rc 0 LIBRE | rc 3 PARADA (fail-closed)
#   campana.sh linea-base                  rc 0 v1 frozen set intact | rc 1 drift
#   campana.sh checkpoint                  prints {"checkpoint":sha256,...}
#   campana.sh residuos                    rc 0 zero residues | rc 1 residues found
#   campana.sh apendar SLUG TYPE PAYLOAD   Plaza append under flock, <= 4096 B
#   campana.sh turno PAYLOAD               LOG-CAMPANA.md append under flock, contiguous
#   campana.sh sidecar FILE...             writes FILE.sha256 and verifies it
#   campana.sh estado                      one JSON line with all of the above
#   campana.sh auditar                     Plaza ids/types/size + journal contiguity (turns >= 62 <= 4096 B)
#   campana.sh verificar-sello             cold re-verification of SELLO-FASE-0-NEXTGEN.json
#
# Exit codes: 0 ok · 1 check failed · 2 usage/ABI · 3 PARADA · 126 containment.
set -euo pipefail
export LC_ALL=C

REPO=${SRF_REPO:-/home/user/OpenSuperficies}
SALA=${SRF_SALA:-/home/user/.openfresnel/plaza/sala-opensuperficies-nextgen}
APENDAR=${SRF_APENDAR:-/home/user/.openfresnel/plaza/apendar.py}
DIARIO=${SRF_DIARIO:-$REPO/LOG-CAMPANA.md}
IA_SLICE=${SRF_IA_SLICE:-/sys/fs/cgroup/user.slice/user-$(id -u).slice/user@$(id -u).service/ia.slice}
PIPE_BUF=4096

die() { printf '{"ok":false,"rc":%s,"causa":"%s"}\n' "$1" "$2"; exit "$1"; }

leer_parada() {  # prints LIBRE | PARADA | AUSENTE for one file
  local f=$1
  [[ -f $f ]] || { echo AUSENTE; return; }
  local t; t=$(awk 'NR==1{print $1}' "$f")
  [[ $t == LIBRE ]] && echo LIBRE || echo PARADA
}

cmd_parada() {
  local r p e
  r=$(leer_parada "$REPO/PARADA.txt"); p=$(leer_parada "$SALA/PARADA.txt")
  if [[ $r == LIBRE && $p == LIBRE ]]; then e=LIBRE; else e=PARADA; fi
  printf '{"repo":"%s","plaza":"%s","efectivo":"%s"}\n' "$r" "$p" "$e"
  [[ $e == LIBRE ]] || exit 3
}

cmd_linea_base() {
  local fallos=0 enlace
  cd "$REPO"
  fallos=$(sha256sum -c contexto/LINEA-BASE-V1.sha256 2>/dev/null | grep -vc ': OK$' || true)
  enlace=$(readlink scripts/srf 2>/dev/null || echo AUSENTE)
  [[ $enlace == docker-xpra-surface.sh ]] || fallos=$((fallos + 1))
  local pyc; pyc=$(find motor scripts -name '*.pyc' -o -name '__pycache__' 2>/dev/null | wc -l)
  fallos=$((fallos + pyc))
  printf '{"ok":%s,"fallos":%d,"enlace_srf":"%s","pycache":%d,"manifiesto":"contexto/LINEA-BASE-V1.sha256"}\n' \
    "$([[ $fallos -eq 0 ]] && echo true || echo false)" "$fallos" "$enlace" "$pyc"
  [[ $fallos -eq 0 ]] || exit 1
}

CHECKPOINT_RAICES=(go.mod go.sum Makefile cmd src cockpit scripts/ng docker/ng fixture/ng guias contexto)
cmd_checkpoint() {
  cd "$REPO"
  local raices=() r
  for r in "${CHECKPOINT_RAICES[@]}"; do [[ -e $r ]] && raices+=("$r"); done
  local n sha
  n=$(find "${raices[@]}" -type f ! -path '*/node_modules/*' ! -path '*/dist/*' ! -path '*/wailsjs/*' ! -path '*/build/bin/*' ! -name '*.pyc' | wc -l)
  sha=$(find "${raices[@]}" -type f ! -path '*/node_modules/*' ! -path '*/dist/*' ! -path '*/wailsjs/*' ! -path '*/build/bin/*' ! -name '*.pyc' -print0 \
        | sort -z | xargs -0 -r sha256sum | sha256sum | cut -d' ' -f1)
  printf '{"checkpoint":"%s","ficheros":%d,"raices":"%s","orden":"cd REPO && find RAICES -type f ! -path */node_modules/* ! -path */dist/* ! -path */wailsjs/* ! -path */build/bin/* ! -name *.pyc -print0 | LC_ALL=C sort -z | xargs -0 -r sha256sum | sha256sum"}\n' \
    "$sha" "$n" "${raices[*]}"
}

cmd_residuos() {
  local cont red esc cg cgpop procs socks decl_red decl_cont decl_esc
  # PLAN A-40: an interactive surface the operator declares (the Perplexity research capsule, for instance) is not a
  # residue. The declaration is auditable and lives in _estado/residuos/declarados.json; the counters print what was
  # discounted, so a declaration can never hide a real leak silently.
  local DECL="$REPO/_estado/residuos/declarados.json"
  decl_red=0; decl_cont=0; decl_esc=0
  if [[ -r $DECL ]]; then
    decl_red=$(jq -r '(.redes // []) | length' "$DECL" 2>/dev/null || echo 0)
    decl_cont=$(jq -r '(.contenedores // []) | length' "$DECL" 2>/dev/null || echo 0)
    decl_esc=$(jq -r '(.puertos // []) | length' "$DECL" 2>/dev/null || echo 0)
  fi
  cont=$(docker ps -a --format '{{.Names}}' 2>/dev/null | grep -c '^srf-' || true)
  # A-48: a worker that runs `docker run` without --name and without -p leaked a live container the name rule could
  # not see [MEASURED 2026-09-18: cranky_burnell on opensuperficies-browser-arm64:1.0.0 while the census read zero].
  # Count by IMAGE too: a container on a campaign image that no declaration covers is a residue whatever it is called.
  local por_imagen
  por_imagen=$(docker ps --format '{{.Names}} {{.Image}}' 2>/dev/null | awk '$2 ~ /^(opensuperficies-|srf-)/ {print $1}' | grep -cvxF -f <(
      { [[ -r $DECL ]] && jq -r '(.contenedores // [])[]' "$DECL" 2>/dev/null; echo '__ninguno__'; }) || true)
  red=$(docker network ls --format '{{.Name}}' 2>/dev/null | grep -c '^newton-srf' || true)
  esc=$(ss -ltnH 2>/dev/null | awk '{n=split($4,a,":"); p=a[n]+0; if ((p>=10020&&p<=10099)||(p>=9220&&p<=9299)) c++} END{print c+0}')
  cont=$(( cont - decl_cont )); (( cont < 0 )) && cont=0
  red=$(( red - decl_red ));    (( red < 0 )) && red=0
  esc=$(( esc - decl_esc ));    (( esc < 0 )) && esc=0
  cg=0; cgpop=0
  if [[ -d $IA_SLICE ]]; then
    for d in "$IA_SLICE"/srf-*; do
      [[ -d $d ]] || continue
      cg=$((cg + 1))
      grep -q '^populated 1' "$d/cgroup.events" 2>/dev/null && cgpop=$((cgpop + 1))
    done
  fi
  # comm names only: the census never matches its own command line (DOSSIER v1 §8.1)
  procs=$(ps -eo comm= | grep -cxE 'qemu-system-aar|Xvfb|xpra|srfd' || true)
  socks=0
  [[ -d ${XDG_RUNTIME_DIR:-/nonexistent}/opensuperficies ]] && \
    socks=$(find "$XDG_RUNTIME_DIR/opensuperficies" -type s 2>/dev/null | wc -l)
  local total=$((cont + red + esc + cg + procs + socks + por_imagen))
  printf '{"ok":%s,"contenedores":%d,"redes":%d,"escuchas":%d,"cgroups":%d,"cgroups_poblados":%d,"procesos_jaula":%d,"sockets":%d,"contenedores_por_imagen":%d,"declarados":{"contenedores":%d,"redes":%d,"puertos":%d},"alcance":"netns=host pidns=jaula"}\n' \
    "$([[ $total -eq 0 ]] && echo true || echo false)" "$cont" "$red" "$esc" "$cg" "$cgpop" "$procs" "$socks" "$por_imagen" "$decl_cont" "$decl_red" "$decl_esc"
  [[ $total -eq 0 ]] || exit 1
}

cmd_apendar() {
  [[ $# -eq 3 ]] || die 2 "uso: apendar SLUG TYPE PAYLOAD_JSON"
  local slug=$1 tipo=$2 src=$3
  [[ $slug =~ ^(opus|antigravity|verificador|bloqueos)$ ]] || die 2 "slug_no_permitido"
  [[ -f $src ]] || die 2 "payload_ausente"
  python3 - "$src" "$slug" "$tipo" "$PIPE_BUF" <<'PY' || exit $?
import json, sys
src, slug, tipo, cap = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4])
p = json.load(open(src, encoding="utf-8"))
if not isinstance(p, dict):
    print('{"ok":false,"rc":2,"causa":"payload_no_es_objeto"}'); sys.exit(2)
line = {"v": 1, "ts": "2026-01-01T00:00:00Z", "id": f"{slug}-99999", "from": slug, "type": tipo, "payload": p}
n = len((json.dumps(line, ensure_ascii=False) + "\n").encode("utf-8"))
if n > cap:
    print(json.dumps({"ok": False, "rc": 1, "causa": "linea_excede_pipe_buf", "bytes": n, "tope": cap})); sys.exit(1)
PY
  exec 9>>"$SALA/.plaza.lock"
  flock -w 10 9 || die 1 "cerrojo_plaza_ocupado"
  PLAZA_SALA="$SALA" python3 "$APENDAR" "$slug" "$tipo" "$src"
}

cmd_turno() {
  [[ $# -eq 1 && -f $1 ]] || die 2 "uso: turno PAYLOAD_JSON"
  exec 8>>"$DIARIO.lock"
  flock -w 10 8 || die 1 "cerrojo_diario_ocupado"
  python3 - "$1" "$DIARIO" "$PIPE_BUF" <<'PY'
import json, os, sys, datetime
src, diario, cap = sys.argv[1], sys.argv[2], int(sys.argv[3])
p = json.load(open(src, encoding="utf-8"))
tipo = p.get("tipo", "UNIDAD")
base = ["plan_id", "ejecutor"]
unidad = ["unidad", "ola", "oraculo", "rc", "contraste", "contraste_rc", "veredicto",
          "veredicto_ref", "checkpoint", "commit", "idempotency_key"]
req = base + (unidad if tipo == "UNIDAD" else ["detalle"])
falta = [k for k in req if k not in p]
if falta:
    print(json.dumps({"ok": False, "rc": 2, "causa": "claves_ausentes", "faltan": falta})); sys.exit(2)
if tipo == "UNIDAD":
    v = p["veredicto"]
    if v not in ("CONFORME", "DISCONFORME", "ORACULO_MUERTO"):
        print(json.dumps({"ok": False, "rc": 2, "causa": "veredicto_fuera_de_catalogo"})); sys.exit(2)
    if v == "CONFORME" and not (p["rc"] == 0 and p["contraste_rc"] != 0):
        print(json.dumps({"ok": False, "rc": 1, "causa": "conforme_sin_discriminacion"})); sys.exit(1)
    if p["rc"] == 0 and p["contraste_rc"] == 0 and v != "ORACULO_MUERTO":
        print(json.dumps({"ok": False, "rc": 1, "causa": "oraculo_muerto_no_declarado"})); sys.exit(1)
ult = 0
if os.path.exists(diario):
    for l in open(diario, encoding="utf-8"):
        l = l.strip()
        if not l:
            continue
        try:
            ult = max(ult, int(json.loads(l).get("turno", 0)))
        except Exception:
            print(json.dumps({"ok": False, "rc": 126, "causa": "diario_corrupto"})); sys.exit(126)
linea = {"turno": ult + 1,
         "ts": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
         "tipo": tipo}
linea.update({k: v for k, v in p.items() if k not in ("turno", "ts", "tipo")})
b = (json.dumps(linea, ensure_ascii=False) + "\n").encode("utf-8")
if len(b) > cap:
    print(json.dumps({"ok": False, "rc": 1, "causa": "linea_excede_pipe_buf", "bytes": len(b)})); sys.exit(1)
fd = os.open(diario, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
try:
    if os.write(fd, b) != len(b):
        print(json.dumps({"ok": False, "rc": 6, "causa": "escritura_parcial"})); sys.exit(6)
    os.fsync(fd)
finally:
    os.close(fd)
print(json.dumps({"ok": True, "turno": ult + 1, "bytes": len(b)}))
PY
}

cmd_sidecar() {
  [[ $# -ge 1 ]] || die 2 "uso: sidecar FICHERO..."
  local f
  for f in "$@"; do
    [[ -f $f ]] || die 2 "fichero_ausente"
    ( cd "$(dirname "$f")" && sha256sum "$(basename "$f")" > "$(basename "$f").sha256" \
        && sha256sum -c --quiet "$(basename "$f").sha256" )
    printf '{"ok":true,"fichero":"%s","sha256":"%s"}\n' "$f" "$(cut -d' ' -f1 "$f.sha256")"
  done
}

cmd_estado() {
  local pa lb ck re
  pa=$( (cmd_parada) 2>/dev/null || true)
  lb=$( (cmd_linea_base) 2>/dev/null || true)
  ck=$( (cmd_checkpoint) 2>/dev/null || true)
  re=$( (cmd_residuos) 2>/dev/null || true)
  python3 - "$pa" "$lb" "$ck" "$re" "$DIARIO" "$SALA" <<'PY'
import json, sys, os, glob
pa, lb, ck, re_, diario, sala = sys.argv[1:7]
ult = 0
if os.path.exists(diario):
    for l in open(diario, encoding="utf-8"):
        if l.strip():
            ult = max(ult, int(json.loads(l).get("turno", 0)))
lineas = {os.path.basename(f): sum(1 for l in open(f, encoding="utf-8") if l.strip())
          for f in sorted(glob.glob(os.path.join(sala, "*.jsonl")))}
print(json.dumps({"parada": json.loads(pa), "linea_base": json.loads(lb),
                  "checkpoint": json.loads(ck), "residuos": json.loads(re_),
                  "ultimo_turno": ult, "siguiente_turno": ult + 1, "plaza": lineas},
                 ensure_ascii=False))
PY
}

cmd_auditar() {  # structural audit of Plaza streams and of journal turns >= 62
  python3 - "$SALA" "$DIARIO" "$PIPE_BUF" <<'PY'
import json, os, re, sys, glob
sala, diario, cap = sys.argv[1], sys.argv[2], int(sys.argv[3])
cat = {"claim","refute","evidence","question","answer","vote","handoff","verdict","ack","open"}
fallos = []
for f in sorted(glob.glob(os.path.join(sala, "*.jsonl"))):
    slug = os.path.basename(f)[:-6]
    esperado = 1
    for n, raw in enumerate(open(f, "rb"), 1):
        if not raw.strip():
            continue
        if len(raw) > cap:
            fallos.append(f"{slug}:{n}:bytes>{cap}")
        try:
            l = json.loads(raw)
        except Exception:
            fallos.append(f"{slug}:{n}:json"); continue
        if l.get("id") != f"{slug}-{esperado}":
            fallos.append(f"{slug}:{n}:id={l.get('id')} esperado={slug}-{esperado}")
        if l.get("from") != slug:
            fallos.append(f"{slug}:{n}:from")
        if l.get("type") not in cat:
            fallos.append(f"{slug}:{n}:type")
        esperado += 1
prev = None
for n, raw in enumerate(open(diario, "rb"), 1):
    if not raw.strip():
        continue
    t = int(json.loads(raw).get("turno", 0))
    if prev is not None and t != prev + 1:
        fallos.append(f"diario:{n}:turno={t} previo={prev}")
    if t >= 62 and len(raw) > cap:
        fallos.append(f"diario:{n}:bytes>{cap}")
    prev = t
print(json.dumps({"ok": not fallos, "fallos": fallos[:20], "n_fallos": len(fallos), "ultimo_turno": prev}))
sys.exit(0 if not fallos else 1)
PY
}

cmd_verificar_sello() {  # cold re-verification of SELLO-FASE-0-NEXTGEN.json
  cd "$REPO"
  [[ -f SELLO-FASE-0-NEXTGEN.json ]] || die 2 "sello_ausente"
  sha256sum -c --quiet SELLO-FASE-0-NEXTGEN.json.sha256 >/dev/null 2>&1 || die 1 "sello_alterado"
  python3 - <<'PY2'
import json, hashlib, os, sys
s = json.load(open("SELLO-FASE-0-NEXTGEN.json", encoding="utf-8"))
fallos = []
for a in s["artefactos"]:
    r = a["ruta"]
    try:
        h = hashlib.sha256(open(os.path.expanduser(r), "rb").read()).hexdigest()
    except OSError:
        fallos.append({"ruta": r, "causa": "ausente"}); continue
    if h != a["sha256"]:
        fallos.append({"ruta": r, "causa": "sha_distinto"})
print(json.dumps({"ok": not fallos, "artefactos": len(s["artefactos"]), "fallos": fallos}, ensure_ascii=False))
sys.exit(0 if not fallos else 1)
PY2
}

case "${1:-}" in
  auditar)     shift; cmd_auditar "$@" ;;
  verificar-sello) shift; cmd_verificar_sello "$@" ;;
  parada)      shift; cmd_parada "$@" ;;
  linea-base)  shift; cmd_linea_base "$@" ;;
  checkpoint)  shift; cmd_checkpoint "$@" ;;
  residuos)    shift; cmd_residuos "$@" ;;
  apendar)     shift; cmd_apendar "$@" ;;
  turno)       shift; cmd_turno "$@" ;;
  sidecar)     shift; cmd_sidecar "$@" ;;
  estado)      shift; cmd_estado "$@" ;;
  *) die 2 "uso: campana.sh {parada|linea-base|checkpoint|residuos|apendar|turno|sidecar|estado|auditar|verificar-sello}" ;;
esac
