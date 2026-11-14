#!/usr/bin/env bash
# dopar.sh - context doping pipeline (PLAN v5.0, A-61). Three filters: crear -> auditar -> sellar.
# No unit is dispatched without its sealed package (INV-19). Floor 80,000 tok o200k, ceiling 150,000, English only.
#
#   dopar.sh ciclo   --unidad U-NN --query '<q>' [--query '<q>'] [--techo 120000] [--reintentos 1]
#                    create, audit with three Flash workers, re-dope ONCE with the gaps they name, then SHIP:
#                    what is still missing is written into the package as known gaps. No hunt for a perfect package.
#   dopar.sh crear   --unidad U-NN --query '<english query>' [--query '<more>'] [--techo 120000] [--nivel 2]
#   dopar.sh auditar --unidad U-NN            mechanical gate: size, language, contract surface, provenance
#   dopar.sh sellar  --unidad U-NN --auditor '<who>' --veredicto ok   seals the package and prints its manifest line
#   dopar.sh estado  --unidad U-NN
#
# What the package carries, in this order: the unit's Q7 contract verbatim · the current content of every file it
# reads and writes · the interface signatures it must respect · the invariants and gates that judge it · the findings
# of its previous attempts · the campaign's measured anti-pattern list · SuperRAG laminar context and paper/catalog
# excerpts · host facts. The semantic audit is done by FLASH workers in fan-out (A-62): a 100k package blows one
# worker's 45 s wall, so `ciclo` splits it in seven, runs six at a time, merges their verdicts and RE-DOPES with the
# gaps they name until it passes. No Sonnet ever audits a context package.
set -euo pipefail
export LC_ALL=C
R=/home/user/OpenSuperficies
CAT=${NEWTON_RAIZ:-/home/user/Documentos/newton-shell}/scripts/agent
PACK=$(command -v scr-ds-super-rag-pack 2>/dev/null || echo "$CAT/scr-ds-super-rag-pack")
D=$R/_estado/dopaje
SUELO=${SRF_DOPAJE_SUELO:-80000}
TECHO=${SRF_DOPAJE_TECHO:-150000}

die() { printf '{"ok":false,"rc":%d,"causa":"%s"}\n' "$1" "$2" >&2; exit "$1"; }
tok() { newton-verbo tok < "$1"; }

bloque_contrato() {  # the unit's yaml block, verbatim
  python3 -B -c '
import re,sys
t=open("'"$R"'/PLAN-CAMPANA-OPENSUPERFICIES-V2-NEXTGEN.md",encoding="utf-8").read()
for b in re.findall(r"```yaml\n(id:.*?)```",t,re.S):
    if b.startswith("id:        "+sys.argv[1]) or re.match(r"id:\s+"+re.escape(sys.argv[1])+r"\s*$",b.splitlines()[0]):
        print("```yaml\n"+b+"```"); break' "$1"
}

cmd_crear() {
  local u="" queries=() techo=$TECHO nivel=2
  while [[ $# -gt 0 ]]; do case "$1" in
    --unidad) u=$2; shift 2;; --query) queries+=("$2"); shift 2;;
    --techo) techo=$2; shift 2;; --nivel) nivel=$2; shift 2;; *) die 2 "arg_desconocido:$1";; esac; done
  [[ -n $u && ${#queries[@]} -ge 1 ]] || die 2 argumentos_incompletos
  [[ -x $PACK ]] || die 127 "binario_ausente:scr-ds-super-rag-pack"
  mkdir -p "$D/$u"
  local out="$D/$u/paquete.md" parte techo_por=$(( techo / ${#queries[@]} )) i=0
  {
    echo "# DOPING PACKAGE · $u · campaign opensuperficies-nextgen"
    echo
    echo "English only. Everything below was read from disk by instruments, not recalled. Trust the literals."
    echo
    echo "## 1. The contract you must satisfy, verbatim from the plan"
    echo
    bloque_contrato "$u"
    echo
    echo "## 2. Anti-patterns measured in THIS campaign — every one of them cost an attempt"
    cat "$R/contexto/ANTIPATRONES.md" 2>/dev/null || echo "(anti-pattern list absent: create contexto/ANTIPATRONES.md)"
    echo
    echo "## 3. Findings of previous attempts on this unit"
    local hallado=0
    for f in "$R"/evidencia/VEREDICTO-"${u/U-/U}"*.json "$R"/evidencia/HALLAZGOS-"${u/U-/U}"*.json; do
      [[ -r $f ]] && { hallado=1; echo; echo "### $(basename "$f")"; echo '```json'; head -c 12000 "$f"; echo; echo '```'; }
    done
    # the verdicts live in the Plaza stream, not only in evidencia: that is where the MO writes them
    if python3 -B -c '
import json,sys
S="/home/user/.openfresnel/plaza/sala-opensuperficies-nextgen/opus.jsonl"
u=sys.argv[1]; n=0
for l in open(S):
    d=json.loads(l); p=d["payload"]
    if p.get("unidad")!=u: continue
    if p.get("evento") not in ("VEREDICTO_UNIDAD","AUDITORIA_OBRERO","PARTE_OBRERO","ATRIBUCION_FALLO","ORDEN_TAREA"): continue
    n+=1
    print("### "+d["id"]+" "+str(p.get("evento"))+" attempt "+str(p.get("intento","-")))
    print("```json"); print(json.dumps(p,ensure_ascii=False)[:6000]); print("```")
sys.exit(0 if n else 1)' "$u"; then hallado=1; fi
    (( hallado == 0 )) && echo "This unit has NEVER been attempted in this campaign: there are no prior findings, and none are missing from this package. You are the first build. Read section 2 instead: those are the mistakes other units made."

    echo
    echo "## 3a. The plan registers this contract cites: decisions, invariants, gates and resource ids"
    python3 -B -c '
import re,sys
t=open("'"$R"'/PLAN-CAMPANA-OPENSUPERFICIES-V2-NEXTGEN.md",encoding="utf-8").read()
for marca in ("Q:OBJETIVOS","Q:DECISIONES","Q:INVARIANTES","Q:VERIFICACION"):
    i=t.find(marca)
    if i<0: continue
    j=t.find("\n## ",i)
    print(t[t.rfind("\n## ",0,i):j][:14000])
for l in re.findall(r"^\{\"u\":\"U-\d+\",\"rec\":.*\}$",t,re.M):
    print(l)'
    echo
    echo "## 3aa. Current content of the files this contract WRITES that already exist (a repair needs them)"
    while read -r f; do
      [[ -z $f || ! -r $R/$f ]] && continue
      echo; echo "### $f (existing, you are modifying it)"; echo '```'; head -c 60000 "$R/$f"; echo; echo '```'
    done < <(python3 -B -c '
import re,sys,yaml
t=open("'"$R"'/PLAN-CAMPANA-OPENSUPERFICIES-V2-NEXTGEN.md",encoding="utf-8").read()
for b in re.findall(r"```yaml\n(id:.*?)```",t,re.S):
    d=yaml.safe_load(b)
    if d["id"]==sys.argv[1]:
        [print(x) for x in d["writes"] if not x.endswith("/")]; break' "$u")
    echo
    echo "## 3b. Full content of every file this contract READS"
    while read -r f; do
      [[ -z $f || ! -r $R/$f ]] && continue
      echo; echo "### $f"; echo '```'; head -c 60000 "$R/$f"; echo; echo '```'
    done < <(python3 -B -c '
import re,sys,yaml
t=open("'"$R"'/PLAN-CAMPANA-OPENSUPERFICIES-V2-NEXTGEN.md",encoding="utf-8").read()
for b in re.findall(r"```yaml\n(id:.*?)```",t,re.S):
    d=yaml.safe_load(b)
    if d["id"]==sys.argv[1]:
        [print(x) for x in d["reads"] if not x.endswith("/")]; break' "$u")
    echo
    echo "## 4. Tier XL multidomain block (GUIA_INTEGRACION_DINAMICA_CATALOGOS_Y_PAPERS.md §5)"
    echo
    echo "### 4.a Paper invariants relevant to this unit (PAPER_IKG, verified against disk)"
    # scr-paper-dope accepts techo 250 only; anything else is rc 2 techo_invalido [MEASURED 2026-09-18].
    # When it answers k:0 the corpus has no invariant for this domain: say so, never leave a raw error in the package.
    local pd hit=0
    for q in "${queries[@]}"; do
      pd=$(newton-verbo scr-paper-dope "{\"prompt\":$(python3 -B -c 'import json,sys;print(json.dumps(sys.argv[1]))' "$q"),\"dut\":\"CO\",\"db\":\"${NEWTON_RAIZ:-/home/user/Documentos/newton-shell}/_cas/corteza.db\",\"techo\":250}" 2>/dev/null || true)
      if [[ -n $pd ]] && [[ $(jq -r '.k // 0' <<<"$pd" 2>/dev/null || echo 0) -gt 0 ]]; then
        hit=1; echo; echo "query: $q"; echo '```json'; echo "$pd"; echo '```'
      fi
    done
    (( hit == 0 )) && echo "No PAPER_IKG invariant in the indexed corpus matches this unit's domain (queried with scr-paper-dope, k=0). This is a fact about the corpus, not a gap in this package: do not look for paper backing that does not exist, and do not invent it."
    echo
    echo "### 4.b Host facts this unit must build against [MEASURED]"
    echo '```json'
    "$R/scripts/ng/campana.sh" estado 2>/dev/null | head -c 2000
    echo; echo '```'
    echo
    echo "## 5. SuperRAG laminar context and exact coordinates"
  } > "$out"
  for q in "${queries[@]}"; do
    i=$((i+1)); parte="$D/$u/rag-$i.md"
    "$PACK" --raiz "$R" --query "$q" --salida "$parte" --techo-tokens "$techo_por" --nivel "$nivel" >"$D/$u/rag-$i.json" 2>&1 || true
    { echo; echo "### 4.$i query: $q"; echo; cat "$parte" 2>/dev/null; } >> "$out"
  done
  printf '{"ok":true,"unidad":"%s","paquete":"%s","tok":%s,"suelo":%d,"techo":%d,"consultas":%d}\n' \
    "$u" "$out" "$(tok "$out")" "$SUELO" "$techo" "${#queries[@]}"
}

cmd_auditar() {   # mechanical gate only; the semantic judgement is the Flash fan-out in cmd_ciclo (A-62)
  local u=""; while [[ $# -gt 0 ]]; do case "$1" in --unidad) u=$2; shift 2;; *) die 2 "arg_desconocido:$1";; esac; done
  local p="$D/$u/paquete.md"; [[ -r $p ]] || die 2 paquete_ausente
  local t; t=$(tok "$p")
  local fallos=()
  (( t < SUELO )) && fallos+=("\"bajo_suelo:$t<$SUELO\"")
  (( t > TECHO )) && fallos+=("\"sobre_techo:$t>$TECHO\"")
  grep -q "^id:        $u" "$p" || grep -q "id:  *$u" "$p" || fallos+=('"contrato_ausente"')
  # every file the contract writes or reads must appear by path in the package
  local faltan=0 f
  while read -r f; do
    [[ -z $f ]] && continue
    grep -qF "$f" "$p" || faltan=$((faltan+1))
  done < <(python3 -B -c '
import re,sys,yaml
t=open("'"$R"'/PLAN-CAMPANA-OPENSUPERFICIES-V2-NEXTGEN.md",encoding="utf-8").read()
for b in re.findall(r"```yaml\n(id:.*?)```",t,re.S):
    d=yaml.safe_load(b)
    if d["id"]==sys.argv[1]:
        [print(x) for x in list(d["writes"])+list(d["reads"])]; break' "$u")
  (( faltan > 0 )) && fallos+=("\"superficie_incompleta:$faltan\"")
  # language: a doping package in Spanish is rejected (A-61)
  local es; es=$(grep -ciE '\b(el|la|los|las|una|para|porque|debe|ventana)\b' "$p" || true)
  (( es > 400 )) && fallos+=("\"idioma_no_ingles:$es\"")
  local n=${#fallos[@]}
  printf '{"ok":%s,"unidad":"%s","tok":%s,"fallos":[%s],"nota":"mechanical gate only; the semantic audit is the Flash fan-out of dopar.sh ciclo (A-62)"}\n' \
    "$([[ $n -eq 0 ]] && echo true || echo false)" "$u" "$t" "$(IFS=,; echo "${fallos[*]}")"
  [[ $n -eq 0 ]]
}

cmd_sellar() {
  local u="" auditor="" veredicto=""
  while [[ $# -gt 0 ]]; do case "$1" in
    --unidad) u=$2; shift 2;; --auditor) auditor=$2; shift 2;; --veredicto) veredicto=$2; shift 2;; *) die 2 "arg_desconocido:$1";; esac; done
  [[ -n $u && -n $auditor && $veredicto == ok ]] || die 2 argumentos_incompletos
  local p="$D/$u/paquete.md"; [[ -r $p ]] || die 2 paquete_ausente
  cmd_auditar --unidad "$u" >/dev/null || die 1 gate_mecanico_rojo
  "$R/scripts/ng/campana.sh" sidecar "$p" >/dev/null
  printf '{"ok":true,"unidad":"%s","paquete":"%s","sha256":"%s","tok":%s,"auditor":"%s","sellado":"%s"}\n' \
    "$u" "$p" "$(sha256sum "$p" | cut -d' ' -f1)" "$(tok "$p")" "$auditor" "$(date -Is)" | tee -a "$D/manifiesto.jsonl"
}

cmd_estado() {
  local u=""; while [[ $# -gt 0 ]]; do case "$1" in --unidad) u=$2; shift 2;; *) die 2 "arg_desconocido:$1";; esac; done
  local p="$D/$u/paquete.md"
  [[ -r $p ]] || { printf '{"ok":false,"unidad":"%s","causa":"sin_paquete"}\n' "$u"; return 1; }
  printf '{"ok":true,"unidad":"%s","tok":%s,"sellado":%s}\n' "$u" "$(tok "$p")" \
    "$([[ -r $p.sha256 ]] && echo true || echo false)"
}

# A-62 (v5.1): the package auditor is a Flash worker, not Sonnet. If the audit is red, the package is RE-DOPED with
# the gaps the auditor named, and the cycle repeats until it passes. Spending a Sonnet on auditing a context package
# is the wrong tier: Flash is fast, cheap and good enough to say "this section is missing, this path is absent".
cmd_ciclo() {
  local u="" queries=() techo=$TECHO nivel=2 max=${SRF_VUELTAS_DOPAJE:-1} i=0
  while [[ $# -gt 0 ]]; do case "$1" in
    --unidad) u=$2; shift 2;; --query) queries+=("$2"); shift 2;; --techo) techo=$2; shift 2;;
    --nivel) nivel=$2; shift 2;; --reintentos) max=$2; shift 2;; *) die 2 "arg_desconocido:$1";; esac; done
  [[ -n $u && ${#queries[@]} -ge 1 ]] || die 2 argumentos_incompletos
  mkdir -p "$D/$u"
  while (( i < max )); do
    i=$((i+1))
    cmd_crear_directo "$u" "$techo" "$nivel" "${queries[@]}" >"$D/$u/crear-$i.json"
    if ! cmd_auditar --unidad "$u" > "$D/$u/gate-$i.json"; then
      queries+=("$(python3 -B -c 'import json,sys;print(" ".join(json.load(open(sys.argv[1]))["fallos"]))' "$D/$u/gate-$i.json" 2>/dev/null || echo "missing contract surface")")
      continue
    fi
    # Semantic audit: THREE workers, each reading the WHOLE package, with a 300 s wall (operator decision,
    # 2026-09-18). Measured before the change: seven fragments at a 45 s wall produced dead workers (code 55) and
    # FALSE gaps — a fragment asking for what lived in another fragment. A worker at 214-299 tok/s reads 100k inside
    # three minutes, and three launches cost less than seven. Roles differ; the text does not.
    local enc="$D/$u/encargos-$i"; rm -rf "$enc"; mkdir -p "$enc"
    # The task travels as a command-line argument to the caged launcher, and a 52 KB encargo dies with
    # "Argument list too long" [MEASURED 2026-09-18]. So the encargo is SHORT and the worker READS the package
    # from disk itself with --permiso lectura. Three angles, one whole package each, 300 s wall.
    local roles=("COMPLETENESS against the contract: is the contract verbatim here, the content of every file it reads, the paths it writes, the anti-patterns, the host facts?"
                 "TRACEABILITY: is every claim traceable to disk? Flag anything invented, stale, or contradicted by the files quoted inside the package itself."
                 "USEFULNESS: you are about to build this unit with only this package. What would you still have to guess?")
    local n=0 rol
    for rol in "${roles[@]}"; do
      n=$((n+1))
      { echo "You audit a context doping package for unit $u of campaign opensuperficies-nextgen. English only."
        echo "READ THE FILE FIRST with your read tool, in slices if it is large: $D/$u/paquete.md"
        echo "Your angle: $rol"
        echo "Answer ONLY with JSON: {\"ok\":true|false,\"faltan\":[\"<missing thing, phrased as a search query>\"],\"inventado\":[\"<claim not traceable to disk>\"]}"
        echo "A unit never attempted has no prior findings: that is not a gap. An explicit statement that something does not exist in the corpus is an ANSWER, not a gap."
        echo "Be strict about what a builder truly needs, and never ask for the impossible."
      } > "$enc/encargo-$n.txt"
    done
    "$R/scripts/ng/obrero.sh" enjambre --dir-encargos "$enc" --dir "$D/$u/auditoria-$i" --k 3 --permiso lectura --tope-wall "${SRF_MURO_OBRERO:-720s}" --tope-tools "${SRF_TOPE_TOOLS_OBRERO:-40}" >/dev/null 2>&1 || true
    local veredicto
    veredicto=$(python3 -B -c '
import json,sys,glob,re,os
faltan=[]; vistos=0; ok=0
for f in sorted(glob.glob(os.path.join(sys.argv[1],"*.json"))):
    try: d=json.load(open(f))
    except Exception: continue
    d=d[-1] if isinstance(d,list) else d
    t=d.get("result") or d.get("respuesta") or json.dumps(d)
    m=re.search(r"\{.*\}",t,re.S)
    if not m: continue
    try: j=json.loads(m.group(0))
    except Exception: continue
    vistos+=1
    if j.get("ok"): ok+=1
    faltan += [x for x in (j.get("faltan") or []) if isinstance(x,str)][:2]
print(json.dumps({"ok": vistos>=2 and ok>=max(2,int(vistos*0.66)), "vistos":vistos, "verdes":ok, "inconcluso": vistos<2, "faltan":faltan[:4]}))' "$D/$u/auditoria-$i")
    if [[ $(jq -r .ok <<<"$veredicto") == true ]]; then
      printf '{"ok":true,"unidad":"%s","vueltas":%d,"auditor":"deepseek-v4.1-flash","tok":%s}\n' "$u" "$i" "$(tok "$D/$u/paquete.md")"
      return 0
    fi
    mapfile -t nuevas < <(jq -r '.faltan[]?' <<<"$veredicto")
    (( ${#nuevas[@]} )) && queries+=("${nuevas[@]}")
  done
  # Operator rule (2026-09-18): ONE correction round, never a hunt for the perfect package. An auditor will always
  # find something; a builder does not need perfection, it needs to know what is missing. What the last audit still
  # named is written INTO the package as known gaps and the package SHIPS.
  {
    echo
    echo "## KNOWN GAPS — the package audit named these and they were not closed. Do not assume any of them: measure."
    python3 -B -c '
import json,glob,re,sys,os
vistos=[]
for f in sorted(glob.glob(os.path.join(sys.argv[1],"w-*.json"))):
    try: d=json.load(open(f))
    except Exception: continue
    d=d[-1] if isinstance(d,list) else d
    m=re.search(r"\{.*\}",str(d.get("result") or ""),re.S)
    if not m: continue
    try: j=json.loads(m.group(0))
    except Exception: continue
    vistos += [x for x in (j.get("faltan") or []) if isinstance(x,str)]
    vistos += ["INVENTED?: "+x for x in (j.get("inventado") or []) if isinstance(x,str)]
for x in dict.fromkeys(vistos):
    print("- "+x[:300])
if not vistos: print("- (the audit named nothing; it simply did not reach quorum)")' "$D/$u/auditoria-$i"
  } >> "$D/$u/paquete.md"
  printf '{"ok":true,"unidad":"%s","vueltas":%d,"nota":"shipped with known gaps declared inside the package (one-round rule)","tok":%s}\n' \
    "$u" "$i" "$(tok "$D/$u/paquete.md")"
  return 0
}

cmd_crear_directo() {  # crear with positional args, used by the cycle
  local u=$1 techo=$2 nivel=$3; shift 3
  local args=(--unidad "$u" --techo "$techo" --nivel "$nivel")
  local q; for q in "$@"; do args+=(--query "$q"); done
  cmd_crear "${args[@]}"
}

case "${1:-ayuda}" in
  ciclo) shift; cmd_ciclo "$@" ;;
  crear) shift; cmd_crear "$@" ;;
  auditar) shift; cmd_auditar "$@" ;;
  sellar) shift; cmd_sellar "$@" ;;
  estado) shift; cmd_estado "$@" ;;
  *) echo "uso: $0 {crear|auditar|sellar|estado} --unidad U-NN ..." >&2; exit 2 ;;
esac
