#!/usr/bin/env bash
# SCRIPT_HEADER:BEGIN
# {"id":"srf-ng-certificar","version":"1.0.0","tipo":"obrero","plazo_ms":1800000,"plan_id":"opensuperficies-nextgen","fase":"U-23"}
# SCRIPT_HEADER:END
#
# U-23 certification instrument for OpenSuperficies Next-Gen: re-runs the 37 gates B-01..B-37 of the plan's Q12 table COLD,
# audits the evidence sidecars, derives the CAS Merkle seal and writes the inventory of every sealed artifact.
#
#   certificar.sh [--json]                     full run: 37 gates, sidecar audit, seal, inventory (stdout: ONE JSON report)
#   certificar.sh --gate B-05[,B-06...] [--json]   only those gates (partial: no seal, no audit; its captures go to gates-parcial/)
#   certificar.sh verificar-sello              cold re-derivation of evidencia/v2/u23/sello.json from disk (rc 0 = the root re-derives)
#   certificar.sh auditar-sidecars             cold audit of every sidecar under evidencia/v2 (rc 0 = none altered, orphan or malformed)
#   certificar.sh sellar                       the seal alone: derive the n1: root three ways, write sello.json and the inventory, re-verify cold
#   certificar.sh tabla                        prints the gate table derived from the plan rows (JSONL, one line per gate) and exits
#   certificar.sh juzgar GATE.json LEGS.jsonl  the judge alone on a gate definition and the captures of its legs (test hook)
#   certificar.sh sonda-deuda03                the DEUDA-03 probe alone (test hook): it must leave no lock file behind on any path
#   certificar.sh contenedores-srf             the census of campaign containers alone (test hook): a failed docker call prints null, never 0
#   SRF_REPO=<dir>                             run against a scratch tree (the tests do; the default is the repository this file lives in)
#   SRF_CERT_MODO_PRUEBA=1                     REQUIRED for the two substitutions below; it stamps the report as a TEST (never certifiable)
#   SRF_CERT_TABLA=<file.jsonl>                test hook: a synthetic gate table instead of the one derived from the plan (refused without the stamp)
#   SRF_CAMPANA=<file>                         test hook: another campana.sh (refused without the stamp)
#
# WHAT IT DOES. For every gate the EXECUTOR (this file) runs the literal of the plan row under `bash -o pipefail -c` from the
# repository root and captures stdout and stderr RAW into a STAGING area (_cas/sello/.salida.*, outside evidencia/v2) with a sidecar
# written by campana.sh sidecar. The JUDGE (the python block JUZGAR) reads those captures and applies the expectation written in the
# plan row: some gates expect rc 1, a count of 0 or a literal stdout, so rc 0 is never read as green by itself. The plan
# rows are read AT RUN TIME (the table below holds only what the rows leave unsaid), so a gate is never paraphrased.
# Every gate also runs its MUTANT (or a scratch copy with the guarded field flipped): a gate whose mutant is not red
# cannot discriminate and does not pass. gates_pass counts the gates whose primary evidence meets the expectation AND whose
# blocking mutants are red AND whose blocking cross-checks hold. The certification itself stays with the metaorchestrator.
#
# THE GATE TABLE IS ALWAYS THE PLAN'S. It is derived from the plan rows on every run and its sha256 is recorded in the report
# (`tabla`). A substituted table exists only for the self-tests; a run that uses one is stamped `prueba` and its `gates_pass` reads
# "no_certificable". The same happens when the plan on disk is not the plan the phase-0 seal pins.
#
# A gate that cannot be re-run in this cage (a precondition of the instrument, exit 98/99 of its literal) is reported as
# no_reproducible with the raw error and counts as NOT passed. Nothing is softened.
#
# RE-RUNNABLE BY CONSTRUCTION. A run never writes into evidencia/v2 while it works: every capture and every derived file is staged in
# _cas/sello/.salida.<random> (same file system, so the final moves are renames) and PROMOTED, with its sidecar, only after
# every gate has run. Before a file is replaced, the previous one is moved to _cas/sello/archivo/<UTC>-<pid>/ (never deleted). A gate that
# scans evidencia/v2 (B-32) therefore sees only complete, consistent files, and a run killed halfway leaves evidencia/v2 exactly as the
# previous run left it. The next run archives the staging area an interrupted run left behind.
#
# THE SEAL IS A FUNCTION OF STABLE INPUTS. It covers the sealed evidence of the units (evidencia/v2 except the unit's own per-run records
# in evidencia/v2/u23/), the campaign source trees, the plan, the phase-0 seal and the guest manifest. It does NOT cover the per-run
# captures (they carry pids, temporary names and timings): they are listed with their hashes in the certificate. Two runs over an unchanged
# tree give a byte-identical sello.json and inventory; an existing inventory is never overwritten in place (identical: left alone;
# different: archived first, then replaced by an atomic rename).
#
# Evidence layout (all inside evidencia/v2/u23/, never inside another unit's directory):
#   gates/<gate>.<leg>.stdout|stderr (+ .sha256)   raw captures of every leg (primario, control<n>, mut<n>, cruce<n>) of every gate (partial runs: gates-parcial/)
#   gates-evidencia.jsonl (+ .sha256)              ONE line per gate: literal executed, raw rc, sha256 of the captures, expectation (partial: gates-parcial.jsonl)
#   sello.json (+ .sha256), sello-corrida-N.*      the n1: root, its derivation command, the raw runs of the catalog piece
#   ../../../_cas/sello/INVENTARIO-ARTEFACTOS-NEXTGEN.json   every sealed artifact with its sha256
# certificacion.json is written by the oracle's `tee` from this script's stdout; its sidecar is written by the capture
# command after the run, so a stale sidecar of the previous run is removed here before anything else.
#
# Exit codes: 0 every selected gate passes (full run: and the seal re-derives) · 1 a gate does not pass or the seal fails ·
# 2 usage or instrument precondition · 3 PARADA (fail-closed).
set -uo pipefail
export LC_ALL=C

AQUI=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO=${SRF_REPO:-$(cd "$AQUI/../.." && pwd)}
export SRF_REPO=$REPO
PLAN=$REPO/PLAN-CAMPANA-OPENSUPERFICIES-V2-NEXTGEN.md
OUT=$REPO/evidencia/v2/u23
INVENTARIO=$REPO/_cas/sello/INVENTARIO-ARTEFACTOS-NEXTGEN.json
ARCHIVO=$REPO/_cas/sello/archivo          # everything a run replaces or abandons goes here (never deleted, outside the sealed tree)
GATE_TIMEOUT=${SRF_CERT_GATE_TIMEOUT:-900}

# the harness PATH carries Node 18 and no Go: the gates need the toolchain the plan pins (go 1.26.4, node 24.14.0)
for d in "$HOME/.nvm/versions/node/v24.14.0/bin" "$HOME/go/bin" "/usr/local/go/bin"; do
  [[ -d $d && ":$PATH:" != *":$d:"* ]] && PATH="$d:$PATH"
done
export PATH
export GOFLAGS=-count=1     # a cached `go test` result is not a cold re-run

JSON=0; SOLO=""; SUBCMD=""; ARG1=""; ARG2=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --json) JSON=1; shift ;;
    --gate) [[ $# -ge 2 ]] || { echo '{"ok":false,"rc":2,"causa":"--gate needs a value"}'; exit 2; }; SOLO=$2; shift 2 ;;
    verificar-sello) SUBCMD=verificar-sello; shift ;;
    auditar-sidecars) SUBCMD=auditar-sidecars; shift ;;
    sellar) SUBCMD=sellar; shift ;;
    tabla) SUBCMD=tabla; shift ;;
    sonda-deuda03) SUBCMD=sonda-deuda03; shift ;;
    contenedores-srf) SUBCMD=contenedores-srf; shift ;;
    juzgar) SUBCMD=juzgar; ARG1=${2:-}; ARG2=${3:-}; shift $(( $# > 3 ? 3 : $# )) ;;
    -h|--help) sed -n '2,/^set -uo pipefail/p' "${BASH_SOURCE[0]}" | sed '$d'; exit 0 ;;
    *) printf '{"ok":false,"rc":2,"causa":"argumento_desconocido","valor":"%s"}\n' "$1"; exit 2 ;;
  esac
done

# The two substitutions below exist only for the self-tests. Without the stamp they are REFUSED, so the production entry point cannot be
# handed another gate table or another campaign instrument; with the stamp the report is a TEST and can never be certifiable (H-3).
PRUEBA=0
if [[ -n ${SRF_CERT_TABLA:-} || -n ${SRF_CAMPANA:-} ]]; then
  if [[ ${SRF_CERT_MODO_PRUEBA:-} == 1 ]]; then
    PRUEBA=1
  else
    echo '{"ok":false,"rc":2,"causa":"sustitucion_fuera_de_las_autopruebas","nota":"SRF_CERT_TABLA and SRF_CAMPANA replace the plan gate table and the campaign instrument; they are honoured only with SRF_CERT_MODO_PRUEBA=1, which stamps the report as a test that can never be a certification"}'
    exit 2
  fi
fi
CAMPANA=$AQUI/campana.sh
[[ $PRUEBA -eq 1 && -n ${SRF_CAMPANA:-} ]] && CAMPANA=$SRF_CAMPANA

TMP=$(mktemp -d "${TMPDIR:-/tmp}/srf-certificar.XXXXXX") || { echo '{"ok":false,"rc":2,"causa":"sin_directorio_temporal"}'; exit 2; }
printf '%s\n' "$$" > "$TMP/pid"     # lets a later run recognise a temporary directory whose owner died without cleaning up
SALIDA=""            # the staging area of THIS run (preparar_salida): captures and derived files wait there until every gate has run
SAL_U23=""           # its overlay of evidencia/v2/u23
GATES=""             # where the captures of the gates are written while the run works (inside the staging area, never inside evidencia/v2)
GATES_REL=""         # the path the captures will have once promoted (recorded in the evidence lines)
ARCH_CORRIDA=""      # the archive directory of this run (created only when something is archived)
INV_ACCION="no_aplica"
HUERFANOS=()
REPORTADO=0          # 1 once a JSON report has been printed; the EXIT trap prints one when the run ends without it (A-53)

# conservar_salida <reason>: a staging area that was never promoted holds raw captures, which are evidence: it is moved to the archive
# (never deleted) when it holds any file, removed when it is empty; prints where it went
conservar_salida() {
  local motivo=$1 destino pendiente
  [[ -n $SALIDA && -d $SALIDA ]] || return 0
  pendiente=$(find "$SALIDA" -type f ! -name pid 2>/dev/null | head -n 1)
  if [[ -n $pendiente ]]; then
    destino=$ARCHIVO/$(date -u +%Y%m%dT%H%M%SZ)-$motivo-$$
    if mkdir -p "$ARCHIVO" && mv -T -- "$SALIDA" "$destino" 2>/dev/null; then printf '%s' "${destino#$REPO/}"
    else printf '%s' "${SALIDA#$REPO/} (could not be archived)"; fi
  else
    rm -rf -- "$SALIDA"
  fi
}

limpiar() {
  local rc=$? conservado=""
  [[ -n $SALIDA ]] && conservado=$(conservar_salida terminacion-anticipada)
  if [[ $REPORTADO -eq 0 && $JSON -eq 1 && -z $SUBCMD && $rc -ne 0 ]]; then
    local t=0 p=0 err=""
    if [[ -s $TMP/gates.jsonl ]]; then      # a failed count is null with its error, never 0 (H-10)
      t=$(jq -s 'length' "$TMP/gates.jsonl" 2>&1) || { err=$t; t=null; }
      p=$(jq -s '[.[]|select(.veredicto=="pass")]|length' "$TMP/gates.jsonl" 2>&1) || { err="$err $p"; p=null; }
    fi
    jq -cn --argjson rc "$rc" --argjson t "$t" --argjson p "$p" --arg e "$err" --arg c "$conservado" \
      '{ok:false,rc:$rc,causa:"terminacion_anticipada",gates_pass:"no_certificable",gates_completados:$t,gates_pass_hasta_aqui:$p,
        error_al_contar:(if $e=="" then null else $e end),capturas_conservadas:(if $c=="" then null else $c end),
        nota:"the run ended before its report and is NOT a certification; the raw captures written so far were moved to capturas_conservadas and evidencia/v2 was not touched"}'
  fi
  rm -rf -- "$TMP"
}
abortar() { REPORTADO=1; printf '%s\n' "$2"; exit "$1"; }   # abortar <rc> <json>: a precondition failure, reported as JSON
trap limpiar EXIT
trap 'exit 143' TERM
trap 'exit 130' INT

log() { printf '%s\n' "$*" >&2; }

# ---------------------------------------------------------------------------------------------------------------------
# staging and promotion: a run never writes into evidencia/v2 while it works (H-1), and never replaces a file without archiving it
# ---------------------------------------------------------------------------------------------------------------------
# preparar_salida: the staging area of this run, an overlay of the repository tree on the repository's own file system (the final
# moves are renames). Its directory carries the owner's pid so that a run that died without its trap can be recognised later.
preparar_salida() {
  mkdir -p "$REPO/_cas/sello" || return 1
  SALIDA=$(mktemp -d "$REPO/_cas/sello/.salida.XXXXXX") || return 1
  printf '%s\n' "$$" > "$SALIDA/pid"
  SAL_U23=$SALIDA/evidencia/v2/u23
  mkdir -p "$SAL_U23" || return 1
}

# barrer_huerfanos: what a run killed without running its trap (SIGKILL, power loss) left behind. Only when the owner process is dead:
# its temporary directory is removed; its staging area holds raw captures, so it is archived (never deleted).
barrer_huerfanos() {
  local d p destino
  for d in "${TMPDIR:-/tmp}"/srf-certificar.*; do
    [[ -d $d && $d != "$TMP" && -f $d/pid ]] || continue
    p=$(cat "$d/pid" 2>/dev/null)
    [[ $p =~ ^[0-9]+$ ]] || continue
    kill -0 "$p" 2>/dev/null && continue
    rm -rf -- "$d" && HUERFANOS+=("tmp:$(basename "$d")")
  done
  for d in "$REPO"/_cas/sello/.salida.*; do
    [[ -d $d && $d != "$SALIDA" && -f $d/pid ]] || continue
    p=$(cat "$d/pid" 2>/dev/null)
    [[ $p =~ ^[0-9]+$ ]] || continue
    kill -0 "$p" 2>/dev/null && continue
    if [[ -n $(find "$d" -type f ! -name pid 2>/dev/null | head -n 1) ]]; then
      destino=$ARCHIVO/$(date -u +%Y%m%dT%H%M%SZ)-huerfana-$(basename "$d")
      mkdir -p "$ARCHIVO" && mv -T -- "$d" "$destino" && HUERFANOS+=("salida:${destino#$REPO/}")
    else
      rm -rf -- "$d" && HUERFANOS+=("salida-vacia:$(basename "$d")")
    fi
  done
}

# asegurar_archivo_de_la_corrida: creates (once) this run's archive directory; sets ARCH_CORRIDA
asegurar_archivo_de_la_corrida() {
  [[ -n $ARCH_CORRIDA ]] && return 0
  ARCH_CORRIDA=$ARCHIVO/$(date -u +%Y%m%dT%H%M%SZ)-$$
  mkdir -p "$ARCH_CORRIDA"
}

# promover_u23 <name>...: installs staged per-run files or directories into evidencia/v2/u23, each with its sidecar. A byte-identical file
# (sidecar included) is left alone. The previous one is archived first, its sidecar before it, so that at every instant a file is absent,
# alone or consistent with its sidecar: a kill in the middle never leaves a mismatched pair for a gate that scans evidencia/v2.
promover_u23() {
  local it src dst
  mkdir -p "$OUT" || return 1
  for it in "$@"; do
    src=$SAL_U23/$it; dst=$OUT/$it
    [[ -e $src ]] || continue
    if [[ -f $src && -f $dst ]] && cmp -s -- "$src" "$dst" && cmp -s -- "$src.sha256" "$dst.sha256" 2>/dev/null; then
      continue
    fi
    if [[ -e $dst || -e $dst.sha256 ]]; then
      asegurar_archivo_de_la_corrida || return 1
      mkdir -p "$ARCH_CORRIDA/$(dirname "$it")" || return 1
      if [[ -e $dst.sha256 ]]; then mv -f -- "$dst.sha256" "$ARCH_CORRIDA/$it.sha256" || return 1; fi
      if [[ -e $dst ]]; then mv -T -- "$dst" "$ARCH_CORRIDA/$it" || return 1; fi
    fi
    mv -T -- "$src" "$dst" || return 1
    if [[ -e $src.sha256 ]]; then mv -f -- "$src.sha256" "$dst.sha256" || return 1; fi
  done
}

# promover_inventario: the sealed inventory is never overwritten in place. Identical: left alone. Different: the previous one is copied to
# the archive (with its own hash in the name), then replaced by an atomic rename (same file system).
promover_inventario() {
  local nuevo=$SALIDA/INVENTARIO-ARTEFACTOS-NEXTGEN.json viejo_sha
  [[ -f $nuevo ]] || return 1
  if [[ -f $INVENTARIO ]]; then
    if cmp -s -- "$nuevo" "$INVENTARIO"; then INV_ACCION=sin_cambios; rm -f -- "$nuevo"; return 0; fi
    viejo_sha=$(sha256sum < "$INVENTARIO" | cut -c1-12)
    mkdir -p "$ARCHIVO/inventario" || return 1
    cp -p -- "$INVENTARIO" "$ARCHIVO/inventario/INVENTARIO-ARTEFACTOS-NEXTGEN.$(date -u +%Y%m%dT%H%M%SZ).$viejo_sha.json" || return 1
    INV_ACCION=reemplazado_tras_archivar
  else
    INV_ACCION=creado
  fi
  mv -T -- "$nuevo" "$INVENTARIO"
}

# ---------------------------------------------------------------------------------------------------------------------
# the JUDGE: reads the raw captures of the legs of ONE gate and applies the expectation written in the plan row
# ---------------------------------------------------------------------------------------------------------------------
cat > "$TMP/juzgar.py" <<'JUZGAR'
import json, os, re, subprocess, sys

gate = json.loads(sys.argv[1])
legs = [json.loads(l) for l in open(sys.argv[2], encoding="utf-8") if l.strip()]
repo = sys.argv[3]
etapa = sys.argv[4] if len(sys.argv) > 4 else ""     # the staging overlay of the run: the captures wait there until every gate has run


def leer(ruta):
    """A capture is read from the staging overlay when it is there (the run's own), otherwise from the tree (sealed unit evidence)."""
    for base in ([etapa] if etapa else []) + [repo]:
        try:
            with open(os.path.join(base, ruta), encoding="utf-8", errors="replace") as fh:
                return fh.read()
        except OSError:
            continue
    return ""


def adjudica(esp, rc, out):
    """Applies an expectation (rc, exact stdout, stdout regex, jq -e over stdout) to CAPTURED values. rc 0 is never green by itself."""
    if rc != esp.get("rc", 0):
        return False, f"measured rc {rc}, expected {esp.get('rc', 0)}"
    if "stdout" in esp and out.rstrip("\n") != esp["stdout"]:
        return False, f"stdout {out.rstrip()[:80]!r} differs from the expected {esp['stdout']!r}"
    if "stdout_regex" in esp and not re.search(esp["stdout_regex"], out, re.M):
        return False, f"stdout does not match {esp['stdout_regex']!r}: {out.strip()[:80]!r}"
    if "stdout_jq" in esp:
        p = subprocess.run(["jq", "-e", esp["stdout_jq"]], input=out, capture_output=True, text=True)
        if p.returncode != 0:
            return False, f"jq -e {esp['stdout_jq']!r} over the captured stdout: rc {p.returncode}"
    return True, "meets the expectation"


def regla(esp):
    partes = [f"rc == {esp.get('rc', 0)}"]
    if "stdout" in esp:
        partes.append(f"stdout == {esp['stdout']!r}")
    if "stdout_regex" in esp:
        partes.append(f"stdout matches /{esp['stdout_regex']}/")
    if "stdout_jq" in esp:
        partes.append(f"jq -e '{esp['stdout_jq']}' over the stdout is true")
    return " and ".join(partes)


def resumen_leg(l):
    return {k: l.get(k) for k in ("rol", "nombre", "modo", "literal", "cwd", "rc", "duracion_ms", "stdout", "stderr", "stdout_sha256", "stderr_sha256", "stdout_bytes", "stderr_bytes")}


prim = next(l for l in legs if l["rol"] == "primario")
esperado = gate["esperado"]
salida_prim = leer(prim["stdout"])
ok_prim, razon_prim = adjudica(esperado, prim["rc"], salida_prim)
precondicion = []
if prim["rc"] in (98, 99):
    precondicion.append(f"primary rc {prim['rc']}: {leer(prim['stderr']).strip()[-300:]}")

# mutants ---------------------------------------------------------------------------------------------------------------
resultado_mut = []
for i, m in enumerate(gate["mutantes"], 1):
    r = {"n": i, "nombre": m["nombre"], "modo": m["modo"], "rojo": None, "razon": "", "leg": None, "control": None}
    if m["modo"] == "sellado":
        cuerpo = leer(m["fuente"])
        r["fuente"] = m["fuente"]
        r["rojo"] = bool(cuerpo) and re.search(m["regex"], cuerpo, re.M) is not None
        r["razon"] = "the sealed raw run of the unit's own mutant shows the red outcome" if r["rojo"] else f"sealed file missing or does not match /{m['regex']}/"
        resultado_mut.append(r)
        continue
    lm = next((l for l in legs if l["rol"] == "mutante" and l.get("n") == i), None)
    lc = next((l for l in legs if l["rol"] == "control" and l.get("n") == i), None)
    if lm is None:
        r["razon"] = "the mutant leg was not run"
        precondicion.append(f"mutant {i}: not executed")
        resultado_mut.append(r)
        continue
    r["leg"] = resumen_leg(lm)
    if lc is not None:
        r["control"] = resumen_leg(lc)
    if lm["rc"] in (98, 99) or (lc is not None and lc["rc"] in (98, 99)):
        r["razon"] = f"precondition of the mutant failed (rc {lm['rc']}): {leer(lm['stderr']).strip()[-200:]}"
        precondicion.append(f"mutant {i}: {r['razon']}")
        resultado_mut.append(r)
        continue
    so, se = leer(lm["stdout"]), leer(lm["stderr"])
    if m["modo"] == "copia":
        ok_control, _ = adjudica(esperado, lc["rc"], leer(lc["stdout"]))
        ok_mut, why = adjudica(esperado, lm["rc"], so)
        r["rojo"] = bool(ok_control and not ok_mut)
        r["razon"] = (f"control (unmutated copy) meets the expectation; mutated copy does not: {why}" if r["rojo"]
                      else f"control_ok={ok_control}, mutant_ok={ok_mut}: the mutation did not make the gate red for the right reason")
    else:
        if "control_literal" in m and lc is not None and lc["rc"] != 0:
            r["rojo"] = False
            r["razon"] = f"the control run (same command without the mutation) returned rc {lc['rc']}, not 0"
        elif lm["rc"] not in m["rojo_rc"]:
            r["rojo"] = False
            r["razon"] = f"rc {lm['rc']} is not one of {m['rojo_rc']}"
        elif "rojo_regex" in m and re.search(m["rojo_regex"], so + "\n" + se, re.S) is None:
            r["rojo"] = False
            r["razon"] = f"rc {lm['rc']} but the output does not show /{m['rojo_regex']}/"
        elif "rojo_stdout" in m and so.strip() != m["rojo_stdout"]:
            r["rojo"] = False
            r["razon"] = f"rc {lm['rc']} but stdout {so.strip()[:60]!r} != {m['rojo_stdout']!r}"
        elif "rojo_stdout_regex" in m and re.search(m["rojo_stdout_regex"], so, re.M) is None:
            r["rojo"] = False
            r["razon"] = f"rc {lm['rc']} but stdout does not match /{m['rojo_stdout_regex']}/: {so.strip()[:80]!r}"
        else:
            r["rojo"] = True
            r["razon"] = f"rc {lm['rc']} in {m['rojo_rc']}" + (f" and the output shows /{m['rojo_regex']}/" if "rojo_regex" in m else "")
    resultado_mut.append(r)

# cross-checks ----------------------------------------------------------------------------------------------------------
resultado_cruce = []
for i, c in enumerate(gate["cruces"], 1):
    lc = next((l for l in legs if l["rol"] == "cruce" and l.get("n") == i), None)
    r = {"n": i, "nombre": c["nombre"], "bloquea": c["bloquea"], "ok": None, "razon": "", "leg": None}
    if lc is None:
        r["razon"] = "not run"
    else:
        r["leg"] = resumen_leg(lc)
        if lc["rc"] in (98, 99):
            r["razon"] = f"precondition failed (rc {lc['rc']}): {leer(lc['stderr']).strip()[-200:]}"
            if c["bloquea"]:
                precondicion.append(f"cross-check {i}: {r['razon']}")
        else:
            r["ok"], r["razon"] = adjudica(c["esperado"], lc["rc"], leer(lc["stdout"]))
    resultado_cruce.append(r)

# substitute slot -----------------------------------------------------------------------------------------------------
marca = re.compile(r"^ranura_sustituta: (.*)$", re.M)
sustituta = None
informativos = {c_i for c_i, c in enumerate(gate["cruces"], 1) if not c["bloquea"]}
for l in legs:
    if l["rol"] == "cruce" and l.get("n") in informativos:
        continue
    hallado = marca.search(leer(l["stderr"]))
    if hallado:
        sustituta = {"plan": "slot 0 (srf-ng-0, ports 10020/9220)", "ejecutado": "slot 1 (srf-ng-1, ports 10021/9221)", "error_crudo_del_intento_en_slot_0": hallado.group(1)[:400], "leg": l["rol"] + (str(l.get("n", "")) if l.get("n") else "")}
        break

mut_ok = all(r["rojo"] is True for r in resultado_mut)
cruce_ok = all((r["ok"] is True) for r in resultado_cruce if r["bloquea"])
if precondicion:
    veredicto = "no_reproducible"
    motivo = "; ".join(precondicion)[:600]
elif ok_prim and mut_ok and cruce_ok:
    veredicto, motivo = "pass", ""
else:
    veredicto = "fail"
    causas = []
    if not ok_prim:
        causas.append(f"primary: {razon_prim}")
    causas += [f"mutant {r['n']} not red: {r['razon']}" for r in resultado_mut if r["rojo"] is not True]
    causas += [f"cross-check {r['n']} ({r['nombre']}): {r['razon']}" for r in resultado_cruce if r["bloquea"] and r["ok"] is not True]
    motivo = "; ".join(causas)[:900]

linea = {
    "gate": gate["id"], "unidad": gate["unidad"], "titulo": gate["titulo"], "modo": gate.get("modo"), "divulgacion": gate.get("divulgacion"),
    "literal": gate["literal"], "literal_es_del_plan": gate["literal_es_del_plan"], "completado_de": gate["completado_de"], "limpieza": gate["limpieza"],
    "rc": prim["rc"], "stdout_sha256": prim["stdout_sha256"], "stderr_sha256": prim["stderr_sha256"],
    "captura": {"stdout": prim["stdout"], "stderr": prim["stderr"]}, "duracion_ms": prim["duracion_ms"],
    "esperado_plan": gate["plan_primaria"], "mutante_plan": gate["plan_mutante"], "esperado": esperado, "regla_aplicada": regla(esperado),
    "adjudicacion_primaria": {"ok": ok_prim, "razon": razon_prim},
    "mutantes": resultado_mut, "cruces": resultado_cruce,
    "ranura_sustituta": sustituta, "veredicto": veredicto, "motivo": motivo,
}
print(json.dumps(linea, ensure_ascii=False, separators=(",", ":")))
JUZGAR

# auditar_sidecars: cold audit of every sidecar under evidencia/v2 (what verifies, what is orphan or malformed, what has no sidecar)
auditar_sidecars() {
  ( cd "$REPO" && python3 -B - <<'AUDITORIA'
import hashlib, json, os, re
base = "evidencia/v2"
archivos, sidecars = [], []
for dp, dn, fn in os.walk(base):
    for f in fn:
        p = os.path.join(dp, f)
        if os.path.islink(p):
            continue
        (sidecars if f.endswith(".sha256") else archivos).append(p)
cubiertos, ok, no_coincide, ausentes, malformados, vacios = set(), 0, [], [], [], []
for s in sorted(sidecars):
    d = os.path.dirname(s)
    lineas = [l for l in open(s, encoding="utf-8", errors="replace").read().splitlines() if l.strip()]
    if not lineas:
        vacios.append(s)
        continue
    for l in lineas:
        m = re.match(r"^([0-9a-f]{64}) [ *](.+)$", l)
        if not m:
            malformados.append(s)
            break
        objetivo = os.path.normpath(os.path.join(d, m.group(2)))
        if not os.path.isfile(objetivo):
            ausentes.append(s)
            continue
        h = hashlib.sha256()
        with open(objetivo, "rb") as fh:
            for b in iter(lambda: fh.read(1 << 20), b""):
                h.update(b)
        if h.hexdigest() == m.group(1):
            ok += 1
            cubiertos.add(objetivo)
        else:
            no_coincide.append(s)
sin = sorted(a for a in archivos if os.path.normpath(a) not in cubiertos)
por_unidad = {}
for a in archivos:
    u = a.split("/")[2] if a.count("/") >= 2 else "raiz"
    r = por_unidad.setdefault(u, {"ficheros": 0, "sin_sidecar": 0})
    r["ficheros"] += 1
    if os.path.normpath(a) not in cubiertos:
        r["sin_sidecar"] += 1
print(json.dumps({"ficheros": len(archivos), "sidecars": len(sidecars), "verificaciones_ok": ok, "no_coinciden": no_coincide[:20], "objetivo_ausente": ausentes[:20],
                  "malformados": malformados[:20], "vacios": vacios[:20], "con_sidecar": len(archivos) - len(sin), "sin_sidecar": len(sin),
                  "sin_sidecar_por_unidad": {u: v for u, v in sorted(por_unidad.items()) if v["sin_sidecar"]}, "sin_sidecar_ejemplos": sin[:15]}))
AUDITORIA
  )
}

if [[ $SUBCMD == juzgar ]]; then       # test hook: the judge alone, on a gate definition and the legs of its runs
  [[ $# -eq 0 && -f ${ARG1:-} && -f ${ARG2:-} ]] || { echo '{"ok":false,"rc":2,"causa":"usage: juzgar GATE.json LEGS.jsonl"}'; exit 2; }
  python3 -B "$TMP/juzgar.py" "$(cat "$ARG1")" "$ARG2" "$REPO"; exit $?
fi
if [[ $SUBCMD == auditar-sidecars ]]; then
  auditar_sidecars > "$TMP/aud.json" || exit 2
  cat "$TMP/aud.json"
  jq -e '(.no_coinciden|length)==0 and (.objetivo_ausente|length)==0 and (.malformados|length)==0 and (.vacios|length)==0' "$TMP/aud.json" > /dev/null
  exit $?
fi

faltan=()
for b in jq python3 sha256sum timeout node go docker newton file curl find awk; do command -v "$b" >/dev/null 2>&1 || faltan+=("$b"); done
if [[ ${#faltan[@]} -gt 0 ]]; then
  printf '{"ok":false,"rc":2,"causa":"binario_ausente","faltan":"%s"}\n' "${faltan[*]}"; exit 2
fi

# ---------------------------------------------------------------------------------------------------------------------
# the seal: stage = every sealed artifact copied to a temporary tree, sealed by the catalog piece scr-ds-arbol-merkle
# ---------------------------------------------------------------------------------------------------------------------
# Scope rule (published in the inventory's regla_de_alcance): the seal is a function of STABLE inputs (H-2). It covers every regular file
# under evidencia/v2 except the unit's own directory evidencia/v2/u23/ (per-run captures with pids, temporary names and timings, and the
# records ABOUT the run), of which only the unit's tests (autopruebas.sh, mutar.sh and their sidecars) are sealed; the campaign
# checkpoint roots (campana.sh checkpoint); the phase-0 seal, the plan and the frozen-baseline list; the guest manifest.
# The per-run captures are listed with their hashes in the certificate, outside the root.
CHECKPOINT_RAICES=(go.mod go.sum Makefile cmd src cockpit scripts/ng docker/ng fixture/ng guias contexto)
FASE0=(SELLO-FASE-0-NEXTGEN.json SELLO-FASE-0-NEXTGEN.json.sha256 PLAN-CAMPANA-OPENSUPERFICIES-V2-NEXTGEN.md PLAN-CAMPANA-OPENSUPERFICIES-V2-NEXTGEN.md.sha256 contexto/LINEA-BASE-V1.sha256 guest/MANIFIESTO.sha256 guest/PROCEDENCIA.json guest/kernel.config)
EXCLUIDOS_DEL_SELLO='^evidencia/v2/u23/'   # the unit's own directory: per-run captures (gates/, gates-evidencia.jsonl, sello-corrida-*, sello-universo-*) and records about the run
DENTRO_DE_U23=(evidencia/v2/u23/autopruebas.sh evidencia/v2/u23/autopruebas.sh.sha256 evidencia/v2/u23/mutar.sh evidencia/v2/u23/mutar.sh.sha256)   # ... except the unit's tests, which are stable inputs
COMANDO_PLANTILLA='newton run scr-ds-arbol-merkle --repo ev=<etapa>/ev --repo fuente=<etapa>/fuente --repo fase0=<etapa>/fase0 --indice <indice> --sin-cache'   # the stage paths of a real run are temporary and differ per run
ITEMS_SELLO=(sello-corrida-1.txt sello-corrida-1.err sello-universo-1.json sello-corrida-2.txt sello-corrida-2.err sello-universo-2.json sello.json)

# lists the repo-relative path of every artifact of the seal, sorted (LC_ALL=C), one per line, NUL-free
lista_del_sello() {
  ( cd "$REPO" || exit 1
    local r
    { { find evidencia/v2 -type f
        for r in "${CHECKPOINT_RAICES[@]}"; do
          [[ -e $r ]] && find "$r" -type f ! -path '*/node_modules/*' ! -path '*/dist/*' ! -path '*/wailsjs/*' ! -path '*/build/bin/*' ! -name '*.pyc'
        done
        for r in "${FASE0[@]}"; do [[ -f $r ]] && printf '%s\n' "$r"; done
      } | grep -vE "$EXCLUIDOS_DEL_SELLO"
      for r in "${DENTRO_DE_U23[@]}"; do [[ -e $r ]] && find "$r" -type f; done
    } | sort -u
  )
}

# armar_etapa <dir>: copies the artifacts into <dir>/{ev,fuente,fase0} so that the catalog piece can index them (it takes directories)
armar_etapa() {
  local dir=$1 lista=$2 f dom
  mkdir -p "$dir/ev" "$dir/fuente" "$dir/fase0" || return 1
  while IFS= read -r f; do
    case "$f" in
      evidencia/v2/*) dom=ev ;;
      SELLO-FASE-0-NEXTGEN.json*|PLAN-CAMPANA-*|contexto/LINEA-BASE-V1.sha256|guest/*) dom=fase0 ;;
      *) dom=fuente ;;
    esac
    mkdir -p "$dir/$dom/$(dirname "$f")" && cp -p -- "$REPO/$f" "$dir/$dom/$f" || { log "armar_etapa: could not copy $f"; return 1; }
  done < "$lista"
}

# derivar_sello <n> <extra-args>: stage from disk + catalog piece + raw captures; prints the full root on stdout
derivar_sello() {
  local n=$1; shift
  local d="$TMP/etapa$n" idx="$TMP/indice$n" lista="$TMP/lista$n.txt"
  lista_del_sello > "$lista" || return 1
  armar_etapa "$d" "$lista" || return 1
  mkdir -p "$idx" || return 1
  local cmd=(newton run scr-ds-arbol-merkle --repo "ev=$d/ev" --repo "fuente=$d/fuente" --repo "fase0=$d/fase0" --indice "$idx" --sin-cache)
  printf '%s\n' "${cmd[*]}" > "$TMP/comando$n.txt"
  "${cmd[@]}" > "$SAL_U23/sello-corrida-$n.txt" 2> "$SAL_U23/sello-corrida-$n.err" < /dev/null
  local rc=$?
  echo "rc=$rc" >> "$SAL_U23/sello-corrida-$n.txt"
  [[ $rc -eq 0 ]] || { log "derivar_sello $n: the catalog piece returned rc $rc"; return 1; }
  cp -- "$idx/universo.json" "$SAL_U23/sello-universo-$n.json" || return 1
  cp -- "$idx/universo.jsonl" "$TMP/universo$n.jsonl" || return 1
  jq -r '.sello' "$idx/universo.json"
}

# raiz_independiente <etapa-dir>: the same Merkle root recomputed by python from the bytes on disk, without the catalog piece
# (leaf = sha256("leaf:" + ruta + NUL + sha256 bytes), node = sha256("node:" + left + right), odd node duplicated, LC_ALL=C order)
raiz_independiente() {
  python3 -B - "$1" <<'PY'
import hashlib, os, sys
raiz = sys.argv[1]
entradas = []
for idr in ("ev", "fuente", "fase0"):
    base = os.path.join(raiz, idr)
    for dp, dn, fn in os.walk(base):
        for f in fn:
            p = os.path.join(dp, f)
            if os.path.islink(p) or not os.path.isfile(p):
                continue
            h = hashlib.sha256()
            with open(p, "rb") as fh:
                for bloque in iter(lambda: fh.read(1 << 20), b""):
                    h.update(bloque)
            entradas.append((idr + "/" + os.path.relpath(p, base), h.digest()))
entradas.sort(key=lambda e: e[0].encode())
nivel = [hashlib.sha256(b"leaf:" + r.encode() + b"\0" + d).digest() for r, d in entradas]
while len(nivel) > 1:
    nivel = [hashlib.sha256(b"node:" + nivel[i] + (nivel[i + 1] if i + 1 < len(nivel) else nivel[i])).digest() for i in range(0, len(nivel), 2)]
print("n1:" + nivel[0].hex() if nivel else "")
PY
}

# verificar-sello: re-derive from disk and compare with evidencia/v2/u23/sello.json (rc 0 = agrees)
cmd_verificar_sello() {
  local sello=$OUT/sello.json
  [[ -f $sello ]] || { echo '{"ok":false,"rc":2,"causa":"sello_ausente"}'; return 2; }
  mkdir -p "$OUT" || return 2
  local guardado; guardado=$(jq -r '.raiz' "$sello") || return 2
  # the check must not overwrite the sealed raw runs: derive into the temporary directory only
  local d="$TMP/vs" idx="$TMP/vsi" lista="$TMP/vs-lista.txt"
  lista_del_sello > "$lista" || { echo '{"ok":false,"rc":2,"causa":"lista_del_sello"}'; return 2; }
  armar_etapa "$d" "$lista" || { echo '{"ok":false,"rc":2,"causa":"etapa"}'; return 2; }
  mkdir -p "$idx"
  local salida; salida=$(newton run scr-ds-arbol-merkle --repo "ev=$d/ev" --repo "fuente=$d/fuente" --repo "fase0=$d/fase0" --indice "$idx" --sin-cache < /dev/null 2>"$TMP/vs.err")
  local rc=$?
  [[ $rc -eq 0 ]] || { printf '{"ok":false,"rc":1,"causa":"pieza_rc_%s"}\n' "$rc"; return 1; }
  local pieza indep
  pieza=$(jq -r '.sello' "$idx/universo.json") || return 2
  indep=$(raiz_independiente "$d")
  local ok=false; [[ $pieza == "$guardado" && $indep == "$guardado" ]] && ok=true
  jq -cn --arg g "$guardado" --arg p "$pieza" --arg i "$indep" --argjson ok "$ok" \
    '{ok:$ok, raiz_guardada:$g, raiz_pieza:$p, raiz_independiente:$i}'
  [[ $ok == true ]]
}

# sellar_todo: three derivations that must agree (the catalog piece twice from two fresh stages, and python from the bytes). It only STAGES
# sello-corrida-N.*, sello-universo-N.json, the inventory and sello.json (sidecars included); promotion is separate. Sets RAIZ1 RAIZ2 SELLO_OK.
# Nothing per-run (timestamp, temporary path, pid) goes into sello.json or the inventory: two runs over an unchanged tree write identical bytes.
sellar_todo() {
log "[seal] deriving the n1: root (catalog piece scr-ds-arbol-merkle twice, and python from disk)"
RAIZ1=$(derivar_sello 1) || { log "[seal] the first derivation failed"; RAIZ1=""; }
RAIZ2=$(derivar_sello 2) || { log "[seal] the second derivation failed"; RAIZ2=""; }
cat > "$TMP/sellar.py" <<'SELLAR'
import hashlib, json, os, re, sys
etapa, manifiesto, raiz_pieza, raiz_pieza_2, inv_ruta, comando = sys.argv[1:7]
hashes = {}
for idr in ("ev", "fuente", "fase0"):
    base = os.path.join(etapa, idr)
    for dp, dn, fn in os.walk(base):
        for f in fn:
            p = os.path.join(dp, f)
            if os.path.islink(p) or not os.path.isfile(p):
                continue
            h = hashlib.sha256()
            with open(p, "rb") as fh:
                for bloque in iter(lambda: fh.read(1 << 20), b""):
                    h.update(bloque)
            hashes[idr + "/" + os.path.relpath(p, base)] = (h.hexdigest(), os.path.getsize(p))
orden = sorted(hashes, key=lambda r: r.encode())
nivel = [hashlib.sha256(b"leaf:" + r.encode() + b"\0" + bytes.fromhex(hashes[r][0])).digest() for r in orden]
while len(nivel) > 1:
    nivel = [hashlib.sha256(b"node:" + nivel[i] + (nivel[i + 1] if i + 1 < len(nivel) else nivel[i])).digest() for i in range(0, len(nivel), 2)]
raiz_independiente = ("n1:" + nivel[0].hex()) if nivel else ""

# per-artifact hashes of the catalog piece against python's own hashing of the same bytes
filas = [json.loads(l) for l in open(manifiesto, encoding="utf-8") if l.strip()]
discrepancias = []
por_pieza = {}
for f in filas:
    por_pieza[f["ruta"]] = f["sha256"]
    if f["ruta"] not in hashes or hashes[f["ruta"]][0] != f["sha256"]:
        discrepancias.append(f["ruta"])
discrepancias += [r for r in hashes if r not in por_pieza]

repos = {}
for f in filas:
    r = repos.setdefault(f["repo"], {"id": f["repo"], "ficheros": 0, "bytes": 0})
    r["ficheros"] += 1
    r["bytes"] += f["bytes"]

def dominio(ruta):
    idr, rel = ruta.split("/", 1)
    return idr, rel

artefactos = []
for r in orden:
    idr, rel = dominio(r)
    hexa, tam = hashes[r]
    unidad = None
    m = re.match(r"^evidencia/v2/(u\d+|fo)/", rel)
    if m:
        unidad = m.group(1).upper()
    lateral = os.path.join(etapa, idr, rel + ".sha256")
    if rel.endswith(".sha256"):
        sidecar = "es_sidecar"
    elif os.path.isfile(lateral):
        txt = open(lateral, encoding="utf-8", errors="replace").read().split()
        sidecar = "verificado" if txt and txt[0] == hexa else "no_coincide"
    else:
        sidecar = "sin_sidecar"
    artefactos.append({"ruta": rel, "sha256": hexa, "bytes": tam, "dominio": {"ev": "evidencia", "fuente": "fuente", "fase0": "fase0"}[idr], "unidad": unidad, "sidecar": sidecar})

por_dominio = {}
for a in artefactos:
    d = por_dominio.setdefault(a["dominio"], {"ficheros": 0, "bytes": 0})
    d["ficheros"] += 1
    d["bytes"] += a["bytes"]

acuerdo = bool(raiz_pieza) and raiz_pieza == raiz_pieza_2 == raiz_independiente and not discrepancias
inventario = {
    "esquema": "srf-inventario/v2", "campana": "opensuperficies-nextgen", "unidad": "U-23",
    "aviso_de_alcance": "Every regular file the seal covers, with its sha256, taken from the manifest of the catalog piece and compared byte for byte with python's own hashing of the same files. The seal root is in evidencia/v2/u23/sello.json (it is derived from exactly this list). The list holds STABLE inputs only: the per-run captures of the certifier (evidencia/v2/u23/gates/, gates-evidencia.jsonl, sello-corrida-*, sello-universo-*) are outside it and are listed with their hashes in the certificate.",
    "raiz": raiz_pieza, "comando": comando,
    "regla_de_alcance": "evidencia/v2/** except the unit's own directory evidencia/v2/u23/ (only u23/autopruebas.sh(.sha256) and u23/mutar.sh(.sha256), the unit's tests, are sealed; the per-run captures and the records about the run are not) ; the campaign checkpoint roots (go.mod go.sum Makefile cmd src cockpit scripts/ng docker/ng fixture/ng guias contexto, without node_modules, dist, wailsjs, build/bin, *.pyc) ; SELLO-FASE-0-NEXTGEN.json(.sha256), the plan(.sha256), contexto/LINEA-BASE-V1.sha256, guest/MANIFIESTO.sha256, guest/PROCEDENCIA.json, guest/kernel.config",
    "totales": {"artefactos": len(artefactos), "bytes": sum(a["bytes"] for a in artefactos), "por_dominio": por_dominio,
                "sin_sidecar": sum(1 for a in artefactos if a["sidecar"] == "sin_sidecar" and a["dominio"] == "evidencia")},
    "artefactos": artefactos,
}
with open(inv_ruta, "w", encoding="utf-8") as fh:      # the STAGED copy: promotion (archive + atomic rename) is the caller's
    json.dump(inventario, fh, ensure_ascii=False, indent=1)
    fh.write("\n")
print(json.dumps({"raiz_independiente": raiz_independiente, "acuerdo": acuerdo, "discrepancias": discrepancias[:20], "n_discrepancias": len(discrepancias),
                  "ficheros": len(artefactos), "repos": sorted(repos.values(), key=lambda r: r["id"])}))
SELLAR
python3 -B "$TMP/sellar.py" "$TMP/etapa2" "$TMP/universo1.jsonl" "$RAIZ1" "$RAIZ2" "$SALIDA/INVENTARIO-ARTEFACTOS-NEXTGEN.json" "$COMANDO_PLANTILLA" > "$TMP/sellar.json" 2> "$TMP/sellar.err" \
  || { log "[seal] the sealer failed: $(tail -c 300 "$TMP/sellar.err")"; echo '{"raiz_independiente":"","acuerdo":false,"discrepancias":[],"n_discrepancias":-1,"ficheros":0,"repos":[]}' > "$TMP/sellar.json"; }
SELLO_OK=$(jq -r '.acuerdo' "$TMP/sellar.json")
"$CAMPANA" sidecar "$SAL_U23"/sello-corrida-1.txt "$SAL_U23"/sello-corrida-1.err "$SAL_U23"/sello-universo-1.json "$SAL_U23"/sello-corrida-2.txt "$SAL_U23"/sello-corrida-2.err "$SAL_U23"/sello-universo-2.json > /dev/null 2>> "$TMP/sidecar.err" \
  || log "[seal] sidecars of the piece runs: $(tail -c 200 "$TMP/sidecar.err")"

# the checkpoint of the campaign: a failed call is null WITH its error, never a made-up value (H-10)
CKPT_CRUDO=$("$CAMPANA" checkpoint 2> "$TMP/ckpt.err") && CKPT=$(jq -c '{checkpoint,ficheros}' <<<"$CKPT_CRUDO" 2>> "$TMP/ckpt.err") || CKPT=null
CKPT_ERROR=""; [[ $CKPT == null ]] && CKPT_ERROR=$(head -c 300 "$TMP/ckpt.err")
jq -n --arg raiz "${RAIZ1:-}" --arg raiz2 "${RAIZ2:-}" --arg comando "$COMANDO_PLANTILLA" \
  --arg inv_sha "$(sha256sum "$SALIDA/INVENTARIO-ARTEFACTOS-NEXTGEN.json" 2>/dev/null | cut -d' ' -f1)" \
  --slurpfile s "$TMP/sellar.json" --argjson ckpt "${CKPT:-null}" --arg ckpt_err "$CKPT_ERROR" \
  '{esquema:"srf-sello/v2", campana:"opensuperficies-nextgen", unidad:"U-23",
    raiz:$raiz,
    algoritmo:"catalog piece scr-ds-arbol-merkle -> F0 scr-merkle-corpus-raiz: leaf = sha256(\"leaf:\" + ruta + NUL + sha256 bytes), node = sha256(\"node:\" + left + right), odd node duplicated, rutas in LC_ALL=C byte order, ruta = repo-id/path",
    comando:$comando,
    regla_de_alcance:"see _cas/sello/INVENTARIO-ARTEFACTOS-NEXTGEN.json .regla_de_alcance",
    alcance:{
      cubre:["every regular file under evidencia/v2 for the units U-00..U-22 and U-24 and for evidencia/v2/fo/ (the phase-0 orders and answers), sidecars included",
             "evidencia/v2/u23/autopruebas.sh and mutar.sh with their sidecars (the unit tests: stable inputs)",
             "the campaign source trees: go.mod go.sum Makefile cmd src cockpit scripts/ng docker/ng fixture/ng guias contexto (without node_modules, dist, wailsjs, build/bin, *.pyc)",
             "SELLO-FASE-0-NEXTGEN.json(.sha256), the plan(.sha256), contexto/LINEA-BASE-V1.sha256, guest/MANIFIESTO.sha256, guest/PROCEDENCIA.json, guest/kernel.config"],
      no_cubre:["the per-run captures of the certifier: evidencia/v2/u23/gates/, gates-evidencia.jsonl, sello-corrida-*, sello-universo-* (they carry pids, temporary names and timings; the certificate lists their hashes)",
                "every other file of evidencia/v2/u23/, by the rule EXCLUIDOS_DEL_SELLO: the unit records about its own delivery (certificacion.json, oraculo-*, contraste-*, mutaciones-*, pruebas-*, reproducibilidad-*, cifras-*, parte*, corrida-1-2/ and any file added there)",
                "sello.json itself (it carries the root) and the inventory file (pinned by .inventario.sha256)",
                "the closing dossier, bin/, runtime/, _estado/ and _cas/"]},
    como_se_rederiva:"scripts/ng/certificar.sh verificar-sello  (rebuilds the stage from disk by the rule above, runs the catalog piece with --sin-cache in a fresh index and python hashing of the same bytes; rc 0 only when both equal .raiz). The stage paths of a real run are temporary and no longer exist, which is why .comando is a template.",
    repos:$s[0].repos, ficheros:$s[0].ficheros,
    rederivacion:{segunda_corrida_de_la_pieza:{raiz:$raiz2, coincide:($raiz==$raiz2 and $raiz!="")},
                  python_desde_disco:{raiz:$s[0].raiz_independiente, coincide:($raiz==$s[0].raiz_independiente and $raiz!="")},
                  hashes_por_artefacto:{comparados:$s[0].ficheros, discrepancias:$s[0].n_discrepancias, ejemplos:$s[0].discrepancias}},
    acuerdo_de_las_tres:$s[0].acuerdo,
    inventario:{ruta:"_cas/sello/INVENTARIO-ARTEFACTOS-NEXTGEN.json", sha256:$inv_sha},
    checkpoint_de_la_campana:$ckpt, checkpoint_de_la_campana_error:(if $ckpt_err=="" then null else $ckpt_err end),
    nota:"A function of stable inputs: two runs over an unchanged tree write this file byte for byte. The seal covers every artifact listed in the inventory; it does not cover the per-run captures, certificacion.json nor sello.json itself. The certification is issued by the metaorchestrator."}' \
  > "$SAL_U23/sello.json" && "$CAMPANA" sidecar "$SAL_U23/sello.json" > /dev/null || { log "could not write sello.json"; return 1; }
}

# sellar_y_promover: stage the seal, install it (archiving what it replaces), then re-verify the INSTALLED files cold, as a third party would
sellar_y_promover() {
  sellar_todo || return 1
  promover_u23 "${ITEMS_SELLO[@]}" && promover_inventario || { log "could not promote the seal"; return 1; }
  VERIF=$(cmd_verificar_sello 2>/dev/null); VERIF_RC=$?
}

# contar_contenedores_srf: the census of campaign containers. A failed docker call is null WITH its error, never 0 (H-10).
contar_contenedores_srf() {
  local salida rc n
  salida=$(docker ps -a --format '{{.Names}}' 2>&1); rc=$?
  if [[ $rc -ne 0 ]]; then
    jq -cn --arg e "$salida" --argjson rc "$rc" '{contenedores_srf:null,error:$e,rc:$rc}'
    return 0
  fi
  n=$(printf '%s\n' "$salida" | grep -c '^srf-' || true)
  jq -cn --argjson n "$n" '{contenedores_srf:$n,error:null,rc:0}'
}

# escribir_sonda_deuda03 <file>: the DEUDA-03 probe (A-59): with a paused guest alive, QEMU answers the certifier directly and the answer is
# compared with the unit's report. It leaves nothing behind on ANY path (H-6): the slot lock it creates is removed by its own trap when the
# probe created it, and the directories it created with it when they are empty.
escribir_sonda_deuda03() {
cat > "$1" <<'EOS'
T=$(mktemp -d) || exit 99
LOCK=runtime/slots/slot-3.lock
[[ -d runtime ]] && RT=1 || RT=0
[[ -d runtime/slots ]] && RS=1 || RS=0
[[ -e $LOCK ]] && PREVIO=1 || PREVIO=0
limpiar_sonda() {
  bin/srf-capsula detener --clase microvm --slot 3 --json >/dev/null 2>&1
  [[ $PREVIO -eq 0 && -f $LOCK ]] && { flock -n "$LOCK" rm -f -- "$LOCK" 2>/dev/null || rm -f -- "$LOCK"; }
  [[ $RS -eq 0 ]] && rmdir runtime/slots 2>/dev/null
  [[ $RT -eq 0 ]] && rmdir runtime 2>/dev/null
  rm -rf "$T"
}
trap limpiar_sonda EXIT
bin/srf-capsula iniciar --clase microvm --slot 3 --pausado --json > "$T/pausado.json" || exit 98
R=$(jq -r .rundir "$T/pausado.json")
Q=$(python3 -B -c 'import socket,json,sys
s=socket.socket(socket.AF_UNIX); s.connect(sys.argv[1]); f=s.makefile("rw")
f.readline()
f.write(json.dumps({"execute":"qmp_capabilities"})+"\n"); f.flush(); f.readline()
f.write(json.dumps({"execute":"query-status"})+"\n"); f.flush()
print(json.loads(f.readline())["return"]["status"])' "$R/qmp.sock") || exit 1
printf 'qmp=%s estado=%s estado_qmp=%s\n' "$Q" "$(jq -r .estado "$T/pausado.json")" "$(jq -r .estado_qmp "$T/pausado.json")"
test "$Q" = "$(jq -r .estado "$T/pausado.json")"
EOS
}

# generar_tabla: the gate table derived from the plan rows (what the rows leave unsaid is completed here, per gate: literal, expectation,
# mutants, cross-checks, mode). JSONL on stdout, one line per gate; rc 2 when the plan's Q12 matrix is not exactly B-01..B-37.
generar_tabla() {
python3 -B - "$PLAN" <<'TABLA'
import json, re, sys

plan = open(sys.argv[1], encoding="utf-8").read().split("\n")


def celdas(linea):
    partes = re.split(r"(?<!\\)\|", linea.strip())
    return [p.strip().replace("\\|", "|") for p in partes[1:-1]]


q12, q4 = {}, {}
for l in plan:
    m = re.match(r"^\| `(B-\d+)` \|", l)
    if m:
        c = celdas(l)
        q12[m.group(1)] = {"gate": c[1], "unidad": c[2], "primaria": c[3], "mutante": c[4]}
    m = re.match(r"^\| `(INV-\w+)` \|", l)
    if m:
        c = celdas(l)
        q4[m.group(1)] = {"oraculo": c[2]}


def span(cell, n=0):
    s = re.findall(r"`([^`]+)`", cell)
    return s[n] if len(s) > n else None


def inv(id_):
    return span(q4[id_]["oraculo"])


ARRANQUE = r"""S=0
E=$(bin/srf-capsula iniciar --clase browser --slot 0 --json 2>&1 >/dev/null) || {
  case "$E" in
    *'port is already allocated'*)
      printf 'ranura_sustituta: slot 0 unavailable: %s\n' "$E" >&2
      bin/srf-capsula detener --clase browser --slot 0 >/dev/null 2>&1   # a failed start leaves runtime/slots/slot-0.lock behind
      S=1
      bin/srf-capsula iniciar --clase browser --slot 1 --json >/dev/null || exit 98 ;;
    *) printf '%s\n' "$E" >&2; exit 98 ;;
  esac
}
"""
DETENER_S = "bin/srf-capsula detener --clase browser --slot 0 >/dev/null 2>&1; bin/srf-capsula detener --clase browser --slot ${S:-0} >/dev/null 2>&1"

TABLA = []

# MODE of each gate (H-8): what its PRIMARY literal computes. It is declared here and CHECKED against the literal below.
#   vivo               the verdict comes from executing something now: a build, a test binary, a capsule, the daemon, a census over the live
#                      tree, a hash re-verification of the tree; no sealed unit evidence file is read
#   evidencia_sellada  the verdict comes from a sealed unit evidence file (evidencia/v2/uNN/...) read by the plan literal; nothing is started
#   mixto              both: a primary that combines execution with sealed evidence (B-34), a sealed primary with a BLOCKING live cross-check
#                      (B-16, B-18, B-21), or an aggregator that runs live instruments and reads the sealed unit files itself (B-31)
MODOS = {
    "B-01": "vivo", "B-02": "evidencia_sellada", "B-03": "evidencia_sellada", "B-04": "vivo", "B-05": "evidencia_sellada", "B-06": "evidencia_sellada",
    "B-07": "vivo", "B-08": "evidencia_sellada", "B-09": "vivo", "B-10": "vivo", "B-11": "vivo", "B-12": "vivo", "B-13": "vivo", "B-14": "evidencia_sellada",
    "B-15": "evidencia_sellada", "B-16": "mixto", "B-17": "vivo", "B-18": "mixto", "B-19": "evidencia_sellada", "B-20": "vivo", "B-21": "mixto",
    "B-22": "vivo", "B-23": "evidencia_sellada", "B-24": "evidencia_sellada", "B-25": "evidencia_sellada", "B-26": "evidencia_sellada", "B-27": "vivo",
    "B-28": "evidencia_sellada", "B-29": "evidencia_sellada", "B-30": "evidencia_sellada", "B-31": "mixto", "B-32": "vivo", "B-33": "vivo", "B-34": "mixto",
    "B-35": "evidencia_sellada", "B-36": "evidencia_sellada", "B-37": "evidencia_sellada",
}
MIXTO_POR_CONSTRUCCION = {"B-31"}     # the literal only executes the aggregator, which reads the six sealed files of the four units itself
EJECUTA = re.compile(r"(bin/|\bgo (build|test)\b|\bnode |\bdocker\b|scripts/ng/|\bnewton\b|sha256sum|srfd|grep -r)")


def modo_coherente(t):
    """The declared mode must agree with what the literal does: a gate declared evidencia_sellada that starts something, or a gate declared
    vivo that reads sealed unit evidence, is a wrong declaration and stops the table."""
    lit = t["literal"]
    ejecuta = EJECUTA.search(lit) is not None
    lee_sellada = "evidencia/v2/" in lit
    cruce_vivo = any(c["bloquea"] and EJECUTA.search(c["literal"]) for c in t["cruces"])
    m = t["modo"]
    if m == "evidencia_sellada":
        return lee_sellada and not ejecuta and not cruce_vivo
    if m == "vivo":
        return ejecuta and not lee_sellada and t["id"] not in MIXTO_POR_CONSTRUCCION
    if m == "mixto":
        return t["id"] in MIXTO_POR_CONSTRUCCION or (ejecuta and lee_sellada) or (lee_sellada and not ejecuta and cruce_vivo)
    return False


def gate(id_, literal, esperado=None, completado_de=None, limpieza=None, mutantes=(), cruces=(), vivo=False, divulgacion=None):
    fila = q12[id_]
    lit = literal
    if lit == "{{PLAN}}":
        lit = span(fila["primaria"])
    lit = lit.replace("{{INV-6}}", inv("INV-6") or "").replace("{{INV-7}}", inv("INV-7") or "").replace("{{INV-7b}}", inv("INV-7b") or "")
    lit = lit.replace("{{PLAN}}", span(fila["primaria"]) or "").replace("{{ARRANQUE}}", ARRANQUE)
    TABLA.append({
        "id": id_, "unidad": fila["unidad"], "titulo": fila["gate"], "plan_primaria": fila["primaria"], "plan_mutante": fila["mutante"],
        "literal": lit, "literal_es_del_plan": lit in [x for x in re.findall(r"`([^`]+)`", fila["primaria"])],
        "completado_de": completado_de, "limpieza": limpieza, "esperado": esperado or {"rc": 0},
        "mutantes": list(mutantes), "cruces": list(cruces), "vivo": vivo, "modo": MODOS[id_], "divulgacion": divulgacion,
    })


def go_mutante(nombre, etiquetas, prueba, paquete):
    return {"nombre": nombre, "modo": "literal", "rojo_rc": [1], "rojo_regex": f"--- FAIL: {prueba}",
            "literal": f"CGO_ENABLED=0 go test -count=1 -tags {etiquetas} -run {prueba} {paquete}"}


def copia(nombre, ruta, jq=None, sh=None, extra=()):
    m = {"nombre": nombre, "modo": "copia", "copias": [ruta, *extra], "mutacion": [{"ruta": ruta, "jq": jq} if jq else {"ruta": ruta, "sh": sh}]}
    return m


# B-01 ------------------------------------------------------------------------------------------------------------------
gate("B-01", "{{PLAN}}", mutantes=[{
    "nombre": "cgo import outside cockpit/ in a scratch copy of the module", "modo": "literal", "rojo_rc": [1],
    "rojo_regex": "build constraints exclude all Go files",
    "literal": r"""T=$(mktemp -d) || exit 99
trap 'rm -rf "$T"' EXIT
mkdir -p "$T/src" "$T/cmd" && cp go.mod go.sum "$T/" && cp -r src/. "$T/src/" && cp -r cmd/. "$T/cmd/" || exit 99
cd "$T" || exit 99
CGO_ENABLED=0 go build ./... || exit 99
mkdir -p src/zzcgo cmd/zzmut
printf 'package zzcgo\n\n/*\nint zz;\n*/\nimport "C"\n\nfunc F() int { return int(C.zz) }\n' > src/zzcgo/x.go
printf 'package main\n\nimport "opensuperficies.local/srf/src/zzcgo"\n\nfunc main() { _ = zzcgo.F() }\n' > cmd/zzmut/main.go
CGO_ENABLED=0 go build ./...
""",
}])
# B-02 ------------------------------------------------------------------------------------------------------------------
gate("B-02", "{{PLAN}}", mutantes=[go_mutante("-tags mutante_r21 (U-00 contrast)", "mutante_r21", "TestMPSCDrenaUltimo", "./src/kernelbook/mpsc/")])
# B-03 ------------------------------------------------------------------------------------------------------------------
gate("B-03", "{{PLAN}}", mutantes=[copia("nacido_en_hermano=false in a copy", "evidencia/v2/u00/cgtest.json", jq=".nacido_en_hermano=false")])
# B-04 ------------------------------------------------------------------------------------------------------------------
gate("B-04", "{{PLAN}}", esperado={"rc": 0, "stdout_jq": ".xpra==10020 and .cdp==9220"}, mutantes=[{
    "nombre": "-tags mutante_formula_v1 binary (U-01 mutator)", "modo": "literal", "rojo_rc": [1], "rojo_stdout": "false",
    "literal": r"""T=$(mktemp -d) || exit 99
trap 'rm -rf "$T"' EXIT
CGO_ENABLED=0 go build -tags mutante_formula_v1 -o "$T/srf-puertos" ./cmd/srf-puertos || exit 99
"$T/srf-puertos" par --slot 0 --json | jq -e '.xpra==10020 and .cdp==9220'
""",
}])
# B-05 ------------------------------------------------------------------------------------------------------------------
gate("B-05", "{{PLAN}}", mutantes=[copia("segundo_rc=0 in a copy (single holder)", "evidencia/v2/u01/reserva.json", jq=".segundo_rc=0")])
# B-06 ------------------------------------------------------------------------------------------------------------------
gate("B-06", "{{PLAN}}", mutantes=[copia("13 doors in a copy", "evidencia/v2/u02/puertas.json", jq=".puertas|=.[0:13]")])
# B-07 ------------------------------------------------------------------------------------------------------------------
gate("B-07", r"""scripts/ng/sondas-cuarentena.sh arrancar-veneno && bin/srf-cuarentena --contenedor srf-probe-poison --clase browser --json | jq -e '.verde==true'; r=$?; scripts/ng/sondas-cuarentena.sh parar; exit $r""",
     esperado={"rc": 1, "stdout": "false"}, completado_de="the U-02 contrast literal (the row reads `contrast of U-02`); the poisoned witness must be RED",
     limpieza="scripts/ng/sondas-cuarentena.sh parar >/dev/null 2>&1", vivo=True,
     cruces=[{"nombre": "the poisoned witness is red BECAUSE Door A turns red (the U-02 mutator names it)", "bloquea": True, "esperado": {"rc": 0, "stdout": "true"},
              "limpieza": "scripts/ng/sondas-cuarentena.sh parar >/dev/null 2>&1",
              "literal": r"""scripts/ng/sondas-cuarentena.sh arrancar-veneno || exit 99
O=$(bin/srf-cuarentena --contenedor srf-probe-poison --clase browser --json); t=$?
scripts/ng/sondas-cuarentena.sh parar
test $t -eq 1 && printf '%s' "$O" | jq -e '[.puertas[]|select(.verde==false)|.puerta]|contains(["A"])'
"""}],
     mutantes=[{
         "nombre": "clean container is green (row mutant)", "modo": "literal", "rojo_rc": [0], "rojo_stdout": "true",
         "limpieza": "scripts/ng/sondas-cuarentena.sh parar >/dev/null 2>&1",
         "literal": r"""scripts/ng/sondas-cuarentena.sh arrancar && bin/srf-cuarentena --contenedor srf-probe --clase browser --json | jq -e '.verde==true'; r=$?; scripts/ng/sondas-cuarentena.sh parar; exit $r""",
     }])
# B-08 ------------------------------------------------------------------------------------------------------------------
gate("B-08", "{{PLAN}}", esperado={"rc": 0, "stdout": "0"},
     mutantes=[copia("one appended line in a copy of display1.diff", "evidencia/v2/u02/display1.diff", sh="printf 'x\\n' >> evidencia/v2/u02/display1.diff")],
     cruces=[{"nombre": "INV-1 live (read-only queries of :1)", "bloquea": False, "literal": "scripts/ng/display-alcance.sh diff | wc -l", "esperado": {"rc": 0, "stdout": "0"}}])
# B-09 ------------------------------------------------------------------------------------------------------------------
gate("B-09", "{{PLAN}}", mutantes=[{
    "nombre": "SRF_PATRON pointed at the focused window (row mutant)", "modo": "literal", "rojo_rc": [1], "rojo_regex": "foco_secuestrado",
    "literal": r"""C=$(scripts/ng/display-alcance.sh foco | jq -er '.clase') || exit 99
P=$(printf '%s' "$C" | sed -e 's/, */|/g')
SRF_PATRON="$P" scripts/ng/display-alcance.sh foco
""",
}])
# B-10 ------------------------------------------------------------------------------------------------------------------
gate("B-10", r"""test "$({{INV-7}})" -eq 0 && {{PLAN}}""", completado_de="row = INV-7 census (Q4 literal) AND the go test span of the row",
     cruces=[{"nombre": "INV-7b: the DEUDA-04 exception is frozen at 7", "bloquea": True, "literal": r"""n=$({{INV-7b}}); printf '%s\n' "$n"; test "$n" -eq 7""".replace("{{INV-7b}}", inv("INV-7b")), "esperado": {"rc": 0, "stdout": "7"}}],
     mutantes=[{
         "nombre": "a direct exec.Command in a scratch copy of src/ (row mutant)", "modo": "literal", "rojo_rc": [1],
         "literal": r"""T=$(mktemp -d) || exit 99
trap 'rm -rf "$T"' EXIT
mkdir -p "$T/src" "$T/cmd" && cp -r src/. "$T/src/" && cp -r cmd/. "$T/cmd/" || exit 99
cd "$T" || exit 99
test "$({{INV-7}})" -eq 0 || exit 99
mkdir -p src/zzmut && printf 'package zzmut\n\nimport "os/exec"\n\nfunc F() { _ = exec.Command("true") }\n' > src/zzmut/m.go
test "$({{INV-7}})" -eq 0
""".replace("{{INV-7}}", inv("INV-7")),
     }])
# B-11 ------------------------------------------------------------------------------------------------------------------
gate("B-11", "{{PLAN}}", mutantes=[go_mutante("-tags mutante_demux (U-03 contrast)", "mutante_demux", "TestExecHijackCabecera", "./src/dockerapi/")])
# B-12 / B-13 -----------------------------------------------------------------------------------------------------------
gate("B-12", r"""bin/srf-wal-verify --libro fixture/ng/wal/correcto.jsonl --orden --json | jq -e '.violaciones==0 and .orden_valido==true'""",
     completado_de="U-04 oracle: the row is `bin/srf-wal-verify --orden` with no ledger; the ledger is fixture/ng/wal/correcto.jsonl",
     mutantes=[{
         "nombre": "reordered fixture: LSN 3 and 4 swapped (row mutant)", "modo": "literal", "rojo_rc": [1], "rojo_regex": '"orden_valido": false',
         "literal": r"""T=$(mktemp -d) || exit 99
trap 'rm -rf "$T"' EXIT
awk 'NR==3{a=$0;next} NR==4{print;print a;next} {print}' fixture/ng/wal/correcto.jsonl > "$T/reordenado.jsonl" || exit 99
bin/srf-wal-verify --libro "$T/reordenado.jsonl" --orden --json
""",
     }])
gate("B-13", r"""bin/srf-wal-verify --libro fixture/ng/wal/correcto.jsonl --claves --json | jq -e '.violaciones==0 and .claves_validas==true'""",
     completado_de="U-04 oracle: the row is `bin/srf-wal-verify --claves` with no ledger; the ledger is fixture/ng/wal/correcto.jsonl",
     mutantes=[{
         "nombre": "doble-envio.jsonl (row mutant, U-04 contrast)", "modo": "literal", "rojo_rc": [1], "rojo_regex": '"claves_validas": false',
         "literal": "bin/srf-wal-verify --libro fixture/ng/wal/doble-envio.jsonl --claves --json",
     }])
# B-14 ------------------------------------------------------------------------------------------------------------------
gate("B-14", "{{PLAN}}", mutantes=[copia("row 8 removed in a copy (row mutant)", "evidencia/v2/u04/rcmap.json", jq='del(.["8"])')])
# B-15 / B-16 -----------------------------------------------------------------------------------------------------------
gate("B-15", "{{PLAN}}", mutantes=[go_mutante("-tags mutante_sin_gobernador (U-05 contrast)", "mutante_sin_gobernador", "TestNoDesborda", "./src/orchestratorbook/gobernador/")])
gate("B-16", "{{PLAN}}", mutantes=[copia("constant source in a copy (row mutant)", "evidencia/v2/u05/vivo.json", jq='.origen_mem="constante"')],
     cruces=[{"nombre": "live read of /proc/meminfo through the governor", "bloquea": True,
              "literal": "bin/srf-gobernador estado --json | jq -e '.origen_mem==\"/proc/meminfo\" and .mem_disponible_bytes>0'",
              "esperado": {"rc": 0}}])
# B-17 / B-18 -----------------------------------------------------------------------------------------------------------
gate("B-17", "{{INV-6}}", esperado={"rc": 1, "stdout": "0"},
     completado_de="INV-6 (Q4) literal, which is what the row cites; under `bash -o pipefail -c` a clean census exits 1 (the first grep and `grep -v` select nothing), so the tolerance is the printed count 0 and not the exit code",
     mutantes=[{
    "nombre": "temporary executable with a forbidden token (U-06 contrast)", "modo": "literal", "rojo_rc": [1], "rojo_stdout": "false",
    "literal": r"""T=$(mktemp -d) && printf 'st%sealth=1\n' '' > $T/m.sh && scripts/ng/censo-antievasion.sh --extra $T/m.sh --json | jq -e '.ocurrencias==0'; r=$?; rm -rf $T; exit $r""",
}])
gate("B-18", r"""jq -e '.ficheros_md_escaneados==0 and .patron_literal_en_arbol==0' evidencia/v2/u06/censo.json""",
     completado_de="U-06 oracle: the row omits the file it reads (evidencia/v2/u06/censo.json)",
     mutantes=[copia("md files scanned in a copy (whole-tree scan)", "evidencia/v2/u06/censo.json", jq=".ficheros_md_escaneados=5")],
     cruces=[{"nombre": "U-06 census re-run live (it also scans the files U-23 adds)", "bloquea": True,
              "literal": "scripts/ng/censo-antievasion.sh --json | jq -e '.ocurrencias==0 and .ficheros_escaneados>0 and .ficheros_md_escaneados==0 and .patron_literal_en_arbol==0'",
              "esperado": {"rc": 0}}])
# B-19 ------------------------------------------------------------------------------------------------------------------
gate("B-19", "{{PLAN}}", mutantes=[go_mutante("-tags mutante_sin_epoca (U-07 contrast)", "mutante_sin_epoca", "TestZombiRechazado", "./src/orchestratorbook/arrendador/")])
# B-20 / B-21 / B-22 / B-23 ---------------------------------------------------------------------------------------------
gate("B-20", ARRANQUE + r"""! docker exec srf-ng-$S sh -c 'ss -ltn | grep -q 0.0.0.0:9222'""",
     completado_de="U-08 oracle (`! docker exec srf-ng-0 sh -c 'ss -ltn | grep -q 0.0.0.0:9222'`) on a freshly started capsule; slot 0 first, slot 1 only when 127.0.0.1:10020 is held by the operator-declared surface",
     limpieza=DETENER_S, vivo=True,
     divulgacion=("The plan's B-20 mutant (--remote-debugging-address=0.0.0.0) is INERT on this Chromium: given that flag it still listens on 127.0.0.1:9222 only "
                  "(informative cross-check 1 measures it live; the judge of attempt 1 captured it in _estado/verificacion/U-23-juez/evidencia-juez/b20-genuine-mutant-slot1.txt). "
                  "It cannot go red by construction. The gate itself is sound: its detector fires on a genuine listener, which the labelled SUBSTITUTE mutant creates."),
     cruces=[{"nombre": "the plan's own mutant flag measured: Chromium given --remote-debugging-address=0.0.0.0 in a throwaway container (informative: shows the plan mutant is inert)", "bloquea": False,
              "limpieza": "docker rm -f srf-sonda-b20p >/dev/null 2>&1",
              "literal": r"""docker run --rm --name srf-sonda-b20p --network none --entrypoint bash opensuperficies-browser-arm64:2.0.0 -c 'Xvfb :99 -screen 0 1280x900x24 -nolisten tcp -nolisten unix -ac >/dev/null 2>&1 &
for i in $(seq 50); do xdpyinfo -display :99 >/dev/null 2>&1 && break; sleep 0.1; done
DISPLAY=:99 setsid chromium --user-data-dir=/tmp/pf --remote-debugging-address=0.0.0.0 --remote-debugging-port=9222 --no-sandbox --no-first-run about:blank >/tmp/c.log 2>&1 &
for i in $(seq 100); do ss -ltn | grep -q :9222 && break; sleep 0.1; done
ss -ltn | grep :9222 || { echo chromium_did_not_listen; exit 99; }
if ss -ltn | grep -q 0.0.0.0:9222; then echo plan_mutant_flag_inert=false; else echo plan_mutant_flag_inert=true; fi
chromium --version'
""", "esperado": {"rc": 0, "stdout_regex": "plan_mutant_flag_inert=true"}}],
     mutantes=[{
         "nombre": "SUBSTITUTE for the plan's mutant: a real listener on 0.0.0.0:9222 in a throwaway container, judged by the same detector", "modo": "literal", "rojo_rc": [1], "rojo_stdout_regex": "detector fired",
         "limpieza": "docker rm -f srf-sonda-b20 >/dev/null 2>&1",
         "literal": r"""docker run --rm --name srf-sonda-b20 --network none --entrypoint python3 opensuperficies-browser-arm64:2.0.0 -c 'import socket, subprocess, sys
s = socket.socket(); s.bind(("0.0.0.0", 9222)); s.listen()
sys.exit(subprocess.run(["sh", "-c", "ss -ltn | grep -q 0.0.0.0:9222"]).returncode)'
r=$?
case $r in 0) echo 'detector fired: a real listener on 0.0.0.0:9222 was found'; exit 1 ;; 1) exit 0 ;; *) exit 99 ;; esac
""",
     }])
gate("B-21", r"""jq -e '.["Protocol-Version"]=="1.3" and (.webSocketDebuggerUrl|test("127.0.0.1:9220"))' evidencia/v2/u08/version.json""",
     completado_de="U-08 oracle: the answer to the bearer request, tee'd by the unit to evidencia/v2/u08/version.json; the row names no file",
     vivo=True,
     cruces=[{"nombre": "the same bearer request answered live by a fresh capsule", "bloquea": True, "limpieza": DETENER_S,
              "literal": ARRANQUE + r"""TOK=$(bin/srf-capsula token --slot $S) && curl -sf -H "Authorization: Bearer $TOK" http://127.0.0.1:$((9220+S))/json/version | jq -e --arg p "127.0.0.1:$((9220+S))" '.["Protocol-Version"]=="1.3" and (.webSocketDebuggerUrl|contains($p))'""",
              "esperado": {"rc": 0}}],
     mutantes=[{
         "nombre": "unauthenticated request is refused (row mutant, U-08 contrast)", "modo": "literal", "rojo_rc": [22], "rojo_stdout": "401", "limpieza": DETENER_S,
         "literal": ARRANQUE + r"""curl -sf -o /dev/null -w '%{http_code}' http://127.0.0.1:$((9220+S))/json/version""",
     }])
gate("B-22", ARRANQUE + r"""P0=$(bin/srf-capsula pid --slot $S --proceso chromium) && bin/srf-xpra intervenir --slot $S --json >/dev/null && P1=$(bin/srf-capsula pid --slot $S --proceso chromium) && bin/srf-xpra blindar --slot $S --json >/dev/null && P2=$(bin/srf-capsula pid --slot $S --proceso chromium) && printf 'P0=%s P1=%s P2=%s\n' "$P0" "$P1" "$P2" && test -n "$P0" -a "$P0" = "$P1" -a "$P1" = "$P2"
""",
     esperado={"rc": 0, "stdout_regex": r"^P0=(\d+) P1=\1 P2=\1$"},
     completado_de="U-09 oracle (P0/P1/P2 around `srf-xpra intervenir` and `blindar`, which the unit never wrote to disk) on a freshly started capsule; slot 0 first, slot 1 only when 10020 is held by the operator-declared surface",
     limpieza=DETENER_S, vivo=True, mutantes=[{
         "nombre": "-tags mutante_reinicio_total (row mutant, U-09 contrast)", "modo": "literal", "rojo_rc": [1], "rojo_stdout_regex": r"^P0=(\d+) P1=(?!\1$)\d+$", "limpieza": DETENER_S,
         "literal": r"""T=$(mktemp -d) || exit 99
trap 'rm -rf "$T"; bin/srf-capsula detener --clase browser --slot 0 >/dev/null 2>&1; bin/srf-capsula detener --clase browser --slot ${S:-0} >/dev/null 2>&1' EXIT
CGO_ENABLED=0 go build -tags mutante_reinicio_total -o "$T/srf-xpra" ./cmd/srf-xpra || exit 99
""" + ARRANQUE + r"""P0=$(bin/srf-capsula pid --slot $S --proceso chromium) && "$T/srf-xpra" intervenir --slot $S --json >/dev/null && P1=$(bin/srf-capsula pid --slot $S --proceso chromium) || exit 99
printf 'P0=%s P1=%s\n' "$P0" "$P1"
test -n "$P0" -a "$P0" = "$P1"
""",
     }])
gate("B-23", r"""for f in intervenir blindar; do jq -e '.ms<800' evidencia/v2/u09/$f.json || exit 1; done""",
     esperado={"rc": 0, "stdout": "true\ntrue"},
     completado_de="the row: `jq -e '.ms<800'` on both switch files (evidencia/v2/u09/intervenir.json and blindar.json); one jq per file so that both are judged",
     mutantes=[copia("intervenir.ms=900 in a copy (full restart)", "evidencia/v2/u09/intervenir.json", jq=".ms=900", extra=["evidencia/v2/u09/blindar.json"]),
               copia("blindar.ms=900 in a copy (full restart)", "evidencia/v2/u09/blindar.json", jq=".ms=900", extra=["evidencia/v2/u09/intervenir.json"])],
     cruces=[{"nombre": "both switches measured live on a fresh capsule (informative: a timing)", "bloquea": False, "limpieza": DETENER_S,
              "literal": ARRANQUE + r"""bin/srf-xpra intervenir --slot $S --json | jq -e '.ms<800' && bin/srf-xpra blindar --slot $S --json | jq -e '.ms<800'""",
              "esperado": {"rc": 0}}])
# B-24 ... B-26 ---------------------------------------------------------------------------------------------------------
gate("B-24", "{{PLAN}}", mutantes=[copia("huerfanos=3 in a copy", "evidencia/v2/u10/huerfanos.json", jq=".huerfanos=3"),
                                  {"nombre": "sealed raw run of the U-10 mutant (-tags mutante_sin_cgroup_kill)", "modo": "sellado", "fuente": "evidencia/v2/u10/contraste-3.txt", "regex": "^false$"}])
gate("B-25", "{{PLAN}}", mutantes=[copia("gates.attach=false in a copy (chip without aria/title)", "evidencia/v2/u11/aria.json", jq=".gates.attach=false")])
gate("B-26", "{{PLAN}}", mutantes=[copia("rc=0 in a copy (clean fixture)", "evidencia/v2/u11/desafio.json", jq=".rc=0")])
# B-27 ------------------------------------------------------------------------------------------------------------------
gate("B-27", "{{PLAN}}", esperado={"rc": 0, "stdout_jq": ".ok==true and .fallos==0"}, mutantes=[{
    "nombre": "one byte changed in a copy of the frozen v1 set (row mutant)", "modo": "literal", "rojo_rc": [1], "rojo_regex": '"ok":false',
    "literal": r"""T=$(mktemp -d) || exit 99
trap 'rm -rf "$T"' EXIT
mkdir -p "$T/scripts" && cp --parents contexto/LINEA-BASE-V1.sha256 -t "$T" || exit 99
awk '{print $2}' contexto/LINEA-BASE-V1.sha256 | xargs -d '\n' cp --parents -t "$T" || exit 99
ln -s docker-xpra-surface.sh "$T/scripts/srf" || exit 99
SRF_REPO="$T" scripts/ng/campana.sh linea-base >/dev/null || exit 99
printf x >> "$T/$(awk 'NR==1{print $2}' contexto/LINEA-BASE-V1.sha256)"
SRF_REPO="$T" scripts/ng/campana.sh linea-base
""",
}])
# B-28 ------------------------------------------------------------------------------------------------------------------
gate("B-28", "{{PLAN}}", mutantes=[go_mutante("-tags mutante_sin_limpieza (U-12 contrast)", "mutante_sin_limpieza", "TestCosechaLimpia", "./src/databook/cas/")])
# B-29 ------------------------------------------------------------------------------------------------------------------
gate("B-29", "test -s evidencia/v2/u13/bus.txt", completado_de="the row: `evidencia/v2/u13/bus.txt` non-empty",
     mutantes=[copia("empty bus.txt in a copy", "evidencia/v2/u13/bus.txt", sh=": > evidencia/v2/u13/bus.txt"),
               {"nombre": "sealed raw run of the U-13 contrast: the host system bus is refused", "modo": "sellado", "fuente": "evidencia/v2/u13/contraste-3.txt", "regex": "Failed to connect to bus"}],
     cruces=[{"nombre": "private bus answers and the host system bus is unreachable, live on a desktop capsule", "bloquea": False,
              "limpieza": "bin/srf-capsula detener --clase desktop --slot 2 >/dev/null 2>&1",
              "literal": r"""bin/srf-capsula iniciar --clase desktop --slot 2 --json >/dev/null || exit 99
docker exec srf-ng-2 busctl --address=unix:path=/run/newton-tenant/bus list | wc -l | xargs test 0 -lt || exit 1
docker exec srf-ng-2 busctl --system list >/dev/null 2>&1; test $? -ne 0
""", "esperado": {"rc": 0}}])
# B-30 ------------------------------------------------------------------------------------------------------------------
gate("B-30", r"""jq -e '.verde' evidencia/v2/u14/vgates.json && jq -e '.estado=="listo" and .agente_control=="listo" and .hostfwd_tcp==true' evidencia/v2/u14/arranque.json""",
     esperado={"rc": 0, "stdout": "true\ntrue"},
     completado_de="the row names `.verde` for both files, but arranque.json has no `.verde` field (measured: jq -e '.verde' prints null, rc 1); the predicate for arranque.json is the one of the U-14 oracle",
     mutantes=[copia("verde=false in a copy of vgates.json", "evidencia/v2/u14/vgates.json", jq=".verde=false", extra=["evidencia/v2/u14/arranque.json"]),
               copia("hostfwd_tcp=false in a copy of arranque.json", "evidencia/v2/u14/arranque.json", jq=".hostfwd_tcp=false", extra=["evidencia/v2/u14/vgates.json"])])
# B-31 ------------------------------------------------------------------------------------------------------------------
gate("B-31", r"""node scripts/ng/medir-cockpit.mjs --json | jq -e '.cumple_todo==true'""",
     completado_de="the row: `node scripts/ng/medir-cockpit.mjs --json` + the sealed unit files (six files of U-18..U-21), which the aggregator reads itself and compares with its own live run",
     cruces=[{"nombre": "U-20 hot switch through the card, live capsule, quiet window (opus-364 item 3; informative: a timing)", "bloquea": False,
              "limpieza": "bin/srf-capsula detener --clase browser --slot 6 >/dev/null 2>&1",
              "literal": r"""bin/srf-capsula iniciar --clase browser --slot 6 --json >/dev/null || exit 98
node scripts/ng/medir-contraste.mjs --conmutar-slot 6 --json | tee /dev/stderr | jq -e '.ms<800 and .iframe_recargado==false and .iframe_desmontado==false'
""", "esperado": {"rc": 0}}],
     mutantes=[
         {"nombre": "--envolver-if (row mutant)", "modo": "literal", "rojo_rc": [1], "rojo_regex": r'"id":"cero-desmontajes".*?"cumple_vivo":false', "literal": "node scripts/ng/medir-cockpit.mjs --mutante envolver-if --json"},
         {"nombre": "--sin-arranque-sincrono (row mutant)", "modo": "literal", "rojo_rc": [1], "rojo_regex": r'"id":"cls-0".*?"cumple_vivo":false', "literal": "node scripts/ng/medir-cockpit.mjs --mutante sin-arranque-sincrono --json"},
         {"nombre": "degraded tokens (row mutant)", "modo": "literal", "rojo_rc": [1], "rojo_regex": r'"id":"aaa".*?"cumple_vivo":false', "literal": "node scripts/ng/medir-cockpit.mjs --mutante tokens-degradados --json"},
     ])
# B-32 ------------------------------------------------------------------------------------------------------------------
gate("B-32", "{{PLAN}}", mutantes=[{
    "nombre": "one appended byte in a copy of a sealed artifact (row mutant, the U-23 contrast)", "modo": "literal", "rojo_rc": [1], "rojo_regex": "cosecha.md: FAILED",
    "control_literal": r"""T=$(mktemp -d) && cp -r evidencia/v2 $T/ && (cd $T/v2/u12 && sha256sum -c --quiet cosecha.md.sha256); r=$?; rm -rf $T; exit $r""",
    "literal": r"""T=$(mktemp -d) && cp -r evidencia/v2 $T/ && printf x >> $T/v2/u12/cosecha.md && (cd $T/v2/u12 && sha256sum -c --quiet cosecha.md.sha256); r=$?; rm -rf $T; exit $r""",
}])
# B-33 ------------------------------------------------------------------------------------------------------------------
gate("B-33", r"""(cd guest && sha256sum -c MANIFIESTO.sha256) && for s in CONFIG_VIRTIO_MMIO CONFIG_VIRTIO_BLK CONFIG_VIRTIO_NET CONFIG_VIRTIO_CONSOLE CONFIG_HW_RANDOM_VIRTIO CONFIG_FW_CFG_SYSFS CONFIG_EXT4_FS CONFIG_DEVTMPFS_MOUNT CONFIG_USER_NS CONFIG_SECCOMP_FILTER; do grep -qx "$s=y" guest/kernel.config || exit 1; done && test "$(stat -c %a guest/guest-root.raw)" = 444 && jq -e '.kernel_version and .kernel_tarball_sha256 and .rootfs_origen=="opensuperficies-browser-arm64:1.0.0" and .init_estatico==true' guest/PROCEDENCIA.json""",
     completado_de="the row's `(cd guest && sha256sum -c MANIFIESTO.sha256)` + the required-symbol, 0444 and provenance clauses of the U-24 oracle",
     mutantes=[{
         "nombre": "kernel config without CONFIG_VIRTIO_BLK=y (row mutant, U-24 contrast)", "modo": "literal", "rojo_rc": [1],
         "literal": r"""T=$(mktemp -d) && sed 's/^CONFIG_VIRTIO_BLK=y$/# CONFIG_VIRTIO_BLK is not set/' guest/kernel.config > $T/kernel.config && grep -qx 'CONFIG_VIRTIO_BLK=y' $T/kernel.config; r=$?; rm -rf $T; exit $r""",
     }])
# B-34 ------------------------------------------------------------------------------------------------------------------
gate("B-34", r"""T=$(mktemp -d) || exit 99
S=$T/srfd.sock
( bin/srfd --socket "$S" & D=$!; trap 'kill $D 2>/dev/null; wait $D 2>/dev/null' EXIT; for i in $(seq 50); do test -S "$S" && break; sleep 0.1; done; stat -c %a "$S" | grep -qx 600 ); r=$?
rm -rf "$T"
test $r -eq 0 && jq -e '.ajenos==0' evidencia/v2/u15/mediacion.json""",
     completado_de="the row: socket mode 600 (measured on a live srfd on a private socket, as the U-15 oracle does) AND `jq -e '.ajenos==0' evidencia/v2/u15/mediacion.json`",
     vivo=True, mutantes=[{
         "nombre": "effective PARADA refuses with rc 3 (row mutant, U-15 contrast)", "modo": "literal", "rojo_rc": [3], "rojo_regex": "mutation refused under an effective PARADA",
         "limpieza": "bin/srf-capsula detener --clase browser --slot 5 >/dev/null 2>&1",
         "literal": r"""T=$(mktemp -d) && printf 'PARADA %s\n' "$(date -Is)" > $T/PARADA.txt && S=$T/srfd.sock && ( SRF_PARADA_REPO=$T/PARADA.txt bin/srfd --socket $S & D=$!; trap 'kill $D 2>/dev/null; wait $D 2>/dev/null; rm -rf $T' EXIT; for i in $(seq 50); do test -S $S && break; sleep 0.1; done; SRF_SOCKET=$S bin/srf capsula iniciar --clase browser --slot 5 --json )""",
     }])
# B-35 ... B-36 ---------------------------------------------------------------------------------------------------------
gate("B-35", "{{PLAN}}", mutantes=[
    {"nombre": "tie fixture (row mutant, U-16 contrast)", "modo": "literal", "rojo_rc": [1], "rojo_stdout": "false", "literal": "bin/srf-grabar objetivo --candidatos fixture/ng/grabador/candidatos-empate.json --json | jq -e '.xid != null'"},
    {"nombre": "tie fixture, the tool's own answer", "modo": "literal", "rojo_rc": [1], "rojo_regex": "grabador_objetivo_ambiguo", "literal": "bin/srf-grabar objetivo --candidatos fixture/ng/grabador/candidatos-empate.json --json"}])
gate("B-36", "{{PLAN}}", mutantes=[{
    "nombre": "recycled PID identity (row mutant, U-17 contrast)", "modo": "literal", "rojo_rc": [1], "rojo_regex": "recycled PID detected",
    "literal": "bin/srf-pbx registrar --slug srf-ng-fantasma --rol OPENSUPERFICIES --identidad fixture/ng/pbx/pid-reciclado.json --json"}])
# B-37 ------------------------------------------------------------------------------------------------------------------
gate("B-37", r"""grep -cE '^\| *`?[0-9]+`? *\|' guias/ng/GUIA-OPERACION-HIPERVISOR.md | xargs test 11 -le && grep -qE '^\| *`?126`? *\|' guias/ng/GUIA-OPERACION-HIPERVISOR.md && jq -e '.dominios.opensuperficies.libros >= 25' evidencia/v2/u22/censo.json""",
     esperado={"rc": 0, "stdout": "true"},
     completado_de="U-22 oracle: >= 11 code rows including 126 in the hypervisor guide, and the census the unit tee'd to evidencia/v2/u22/censo.json",
     cruces=[{"nombre": "U-22 checker of the three guides (the envelope asks for its rc)", "bloquea": False, "literal": "bash evidencia/v2/u22/verificar-guias.sh", "esperado": {"rc": 0}},
             {"nombre": "catalog census read live", "bloquea": False, "literal": "newton scr scr-cat-node-lookup '{\"censo\":true}' | jq -e '.dominios.opensuperficies.libros >= 25'", "esperado": {"rc": 0}}],
     mutantes=[{
         "nombre": "v1 guide (row mutant, U-22 contrast)", "modo": "literal", "rojo_rc": [123],
         "literal": r"""grep -cE '^\| *`?[0-9]+`? *\|' guias/GUIA-OPERACION-SUPERFICIES.md | xargs test 11 -le && grep -qE '^\| *`?126`? *\|' guias/GUIA-OPERACION-SUPERFICIES.md"""}])

ids = [t["id"] for t in TABLA]
esperados = [f"B-{n:02d}" for n in range(1, 38)]
if ids != esperados or sorted(q12) != esperados:
    sys.stderr.write(f"tabla o plan incompletos: tabla={ids} plan={sorted(q12)}\n")
    sys.exit(2)
incoherentes = [t["id"] for t in TABLA if not modo_coherente(t)]
if incoherentes:
    sys.stderr.write(f"modo declarado incoherente con el literal: {incoherentes}\n")
    sys.exit(2)
for t in TABLA:
    print(json.dumps(t, ensure_ascii=False))
TABLA
}

if [[ $SUBCMD == verificar-sello ]]; then
  cd "$REPO" || exit 2
  cmd_verificar_sello; exit $?
fi

if [[ $SUBCMD == sellar ]]; then         # the seal alone: derive, write sello.json + inventory, re-verify cold (rc 0 = the three derivations agree)
  cd "$REPO" && mkdir -p "$OUT" "$(dirname "$INVENTARIO")" || exit 2
  barrer_huerfanos
  preparar_salida || exit 2
  sellar_y_promover || exit 1
  rm -rf -- "$SALIDA"; SALIDA=""
  jq -cn --arg r "${RAIZ1:-}" --arg ok "$SELLO_OK" --argjson v "$VERIF_RC" --arg acc "$INV_ACCION" '{ok:($ok=="true" and $v==0),raiz:$r,acuerdo_de_las_tres:($ok=="true"),verificacion_en_frio_rc:$v,inventario:$acc}'
  [[ $SELLO_OK == true && $VERIF_RC -eq 0 ]]; exit $?
fi

if [[ $SUBCMD == tabla ]]; then           # the gate table derived from the plan (a third party can hash it and compare with the report's `tabla`)
  [[ -f $PLAN ]] || { echo '{"ok":false,"rc":2,"causa":"plan_ausente"}'; exit 2; }
  generar_tabla; exit $?
fi
if [[ $SUBCMD == contenedores-srf ]]; then contar_contenedores_srf; exit $?; fi
if [[ $SUBCMD == sonda-deuda03 ]]; then   # test hook: the probe alone, from the repository root
  cd "$REPO" || exit 2
  escribir_sonda_deuda03 "$TMP/deuda03.sh" && bash "$TMP/deuda03.sh"; exit $?
fi

# ---------------------------------------------------------------------------------------------------------------------
# preflight
# ---------------------------------------------------------------------------------------------------------------------
cd "$REPO" || { echo '{"ok":false,"rc":2,"causa":"repo_inaccesible"}'; exit 2; }
if ! "$CAMPANA" parada >"$TMP/parada.json" 2>&1; then
  abortar 3 "$(jq -cn --arg p "$(cat "$TMP/parada.json")" '{ok:false,rc:3,causa:"PARADA",parada:$p}')"
fi
[[ -f $PLAN ]] || abortar 2 '{"ok":false,"rc":2,"causa":"plan_ausente"}'
mkdir -p "$OUT" "$(dirname "$INVENTARIO")" || abortar 2 '{"ok":false,"rc":2,"causa":"no_se_puede_escribir_u23"}'
barrer_huerfanos
preparar_salida || abortar 2 '{"ok":false,"rc":2,"causa":"no_se_puede_preparar_la_salida"}'
if [[ -n $SOLO ]]; then GATES_REL=evidencia/v2/u23/gates-parcial; else GATES_REL=evidencia/v2/u23/gates; fi
GATES=$SALIDA/$GATES_REL
mkdir -p "$GATES" || abortar 2 '{"ok":false,"rc":2,"causa":"no_se_puede_preparar_la_salida"}'
# The sidecar of certificacion.json can only be written after the oracle's tee has finished, and the tee truncates the file the moment the pipeline
# starts: a sidecar left by the previous run would describe a file that no longer matches, and B-32 (which scans evidencia/v2) would fail on it.
# A FULL run drops it unconditionally (it cannot know whether its stdout goes to that file); a partial run, which never rewrites the certificate,
# drops it only when it is already stale.
if [[ -z $SOLO ]] || ! ( cd "$OUT" 2>/dev/null && sha256sum -c --quiet certificacion.json.sha256 >/dev/null 2>&1 ); then
  rm -f -- "$OUT/certificacion.json.sha256"
fi
# what exists before the run, to report what the run itself left behind (regular files, lock files, temp dirs the census cannot see)
ls -A "$REPO/runtime/slots" 2>/dev/null | sort > "$TMP/rt-antes.txt"
ls -A "${TMPDIR:-/tmp}" 2>/dev/null | grep -E '^srf-' | sort > "$TMP/tmp-antes.txt"

# ---------------------------------------------------------------------------------------------------------------------
# the gate table: what the plan rows leave unsaid (literal completions, expectations, mutants, cross-checks)
# ---------------------------------------------------------------------------------------------------------------------
generar_tabla > "$TMP/tabla.plan.jsonl" 2> "$TMP/tabla.err"
if [[ ! -s $TMP/tabla.plan.jsonl ]]; then
  abortar 2 "$(jq -cn --arg e "$(head -c 600 "$TMP/tabla.err")" '{ok:false,rc:2,causa:"tabla_de_gates",error:$e}')"
fi
TABLA_PLAN_SHA=$(sha256sum < "$TMP/tabla.plan.jsonl" | cut -d' ' -f1)
if [[ -n ${SRF_CERT_TABLA:-} ]]; then      # test hook: a synthetic table (JSONL, same fields); honoured only under SRF_CERT_MODO_PRUEBA=1 (see the top of this file)
  TABLA_ORIGEN=sustituta_de_prueba
  cp -- "$SRF_CERT_TABLA" "$TMP/tabla.jsonl" 2> "$TMP/tabla.err"
else
  TABLA_ORIGEN=plan
  cp -- "$TMP/tabla.plan.jsonl" "$TMP/tabla.jsonl"
fi
if [[ ! -s $TMP/tabla.jsonl ]]; then
  abortar 2 "$(jq -cn --arg e "$(head -c 600 "$TMP/tabla.err")" '{ok:false,rc:2,causa:"tabla_de_gates",error:$e}')"
fi
TABLA_SHA=$(sha256sum < "$TMP/tabla.jsonl" | cut -d' ' -f1)
# a run is CERTIFIABLE only when its table is the plan's, it is not a test, the plan on disk is the plan the phase-0 seal pins, and it runs
# on the tree this script lives in (SRF_REPO can point the script at a scratch or a forged tree: such a run is reported, never certifiable)
PLAN_SHA=$(sha256sum "$PLAN" | cut -d' ' -f1)
PLAN_SELLADO=$(jq -r '[.artefactos[]|select(.ruta=="PLAN-CAMPANA-OPENSUPERFICIES-V2-NEXTGEN.md")|.sha256][0] // ""' "$REPO/SELLO-FASE-0-NEXTGEN.json" 2>/dev/null)
SELLO_F0_SHA=$(sha256sum < "$REPO/SELLO-FASE-0-NEXTGEN.json" 2>/dev/null | cut -d' ' -f1)
REPO_DEL_SCRIPT=$(cd "$AQUI/../.." && pwd -P)
REPO_FISICO=$(cd "$REPO" && pwd -P)
CERTIFICABLE=false
[[ $PRUEBA -eq 0 && $TABLA_SHA == "$TABLA_PLAN_SHA" && -n $PLAN_SELLADO && $PLAN_SHA == "$PLAN_SELLADO" && $REPO_FISICO == "$REPO_DEL_SCRIPT" ]] && CERTIFICABLE=true

# ---------------------------------------------------------------------------------------------------------------------
# the EXECUTOR: runs each leg raw, captures stdout/stderr, records rc and sha256
# ---------------------------------------------------------------------------------------------------------------------
ARCHIVOS_DE_GATE=()    # captures of the current gate (for their sidecars)

# correr <prefix> <cwd> <literal> <cleanup>: raw capture into <prefix>.stdout/.stderr; sets RC and DUR_MS
correr() {
  local pre=$1 cwd=$2 lit=$3 limp=$4 cuerpo t0 t1
  if [[ -n $limp ]]; then cuerpo=$(printf 'trap %q EXIT; %s' "$limp" "$lit"); else cuerpo=$lit; fi
  t0=$(date +%s%N)
  ( cd "$cwd" && timeout -k 15 "$GATE_TIMEOUT" bash -o pipefail -c "$cuerpo" ) < /dev/null > "$pre.stdout" 2> "$pre.stderr"
  RC=$?
  t1=$(date +%s%N)
  DUR_MS=$(( (t1 - t0) / 1000000 ))
  ARCHIVOS_DE_GATE+=("$pre.stdout" "$pre.stderr")
}

# registrar_leg <legs-file> <role> <n> <name> <mode> <literal> <cwd> <prefix>
registrar_leg() {
  local f=$1 rol=$2 n=$3 nombre=$4 modo=$5 lit=$6 cwd=$7 pre=$8
  jq -cn --arg rol "$rol" --argjson n "$n" --arg nombre "$nombre" --arg modo "$modo" --arg literal "$lit" --arg cwd "${cwd/#$REPO/.}" \
    --argjson rc "$RC" --argjson ms "$DUR_MS" --arg so "${pre#$SALIDA/}.stdout" --arg se "${pre#$SALIDA/}.stderr" \
    --arg hso "$(sha256sum < "$pre.stdout" | cut -d' ' -f1)" --arg hse "$(sha256sum < "$pre.stderr" | cut -d' ' -f1)" \
    --argjson bo "$(stat -c %s "$pre.stdout")" --argjson be "$(stat -c %s "$pre.stderr")" \
    '{rol:$rol,n:$n,nombre:$nombre,modo:$modo,literal:$literal,cwd:$cwd,rc:$rc,duracion_ms:$ms,stdout:$so,stderr:$se,stdout_sha256:$hso,stderr_sha256:$hse,stdout_bytes:$bo,stderr_bytes:$be}' >> "$f"
}

# a mutant that works on COPIES: copy the evidence into a scratch root, run the gate literal on the untouched copy (control),
# flip the guarded field, run the SAME literal again (mutant). The mutation must change the bytes, or the leg is an instrument error.
correr_copia() {
  local g=$1 i=$2 m=$3 legs=$4 lit=$5 limp=$6
  local S; S=$(mktemp -d "$TMP/copia.XXXXXX") || return 1
  local rel
  while IFS= read -r rel; do
    mkdir -p "$S/$(dirname "$rel")" && cp -p -- "$REPO/$rel" "$S/$rel" || { log "copy: could not copy $rel"; return 1; }
  done < <(jq -r '.copias[]' <<<"$m")
  correr "$GATES/$g.control$i" "$S" "$lit" ""
  registrar_leg "$legs" control "$i" "control: unmutated copy" copia "$lit" "$S" "$GATES/$g.control$i"
  local mu ruta filtro sh cambiado=0 fallo=0
  while IFS= read -r mu; do
    ruta=$(jq -r '.ruta' <<<"$mu"); filtro=$(jq -r '.jq // empty' <<<"$mu"); sh=$(jq -r '.sh // empty' <<<"$mu")
    if [[ -n $filtro ]]; then
      if jq -c "$filtro" "$S/$ruta" > "$S/$ruta.mutado" 2>>"$S/mutacion.err"; then mv -- "$S/$ruta.mutado" "$S/$ruta"; else fallo=1; fi
    else
      ( cd "$S" && bash -c "$sh" ) 2>>"$S/mutacion.err" || fallo=1
    fi
    cmp -s -- "$REPO/$ruta" "$S/$ruta" || cambiado=1
  done < <(jq -c '.mutacion[]' <<<"$m")
  if [[ $cambiado -eq 0 || $fallo -eq 1 ]]; then
    : > "$GATES/$g.mut$i.stdout"
    { printf 'mutation ineffective or failed (changed=%s failed=%s)\n' "$cambiado" "$fallo"; cat "$S/mutacion.err" 2>/dev/null; } > "$GATES/$g.mut$i.stderr"
    RC=99; DUR_MS=0; ARCHIVOS_DE_GATE+=("$GATES/$g.mut$i.stdout" "$GATES/$g.mut$i.stderr")
  else
    correr "$GATES/$g.mut$i" "$S" "$lit" ""
  fi
  registrar_leg "$legs" mutante "$i" "$(jq -r '.nombre' <<<"$m")" copia "$lit" "$S" "$GATES/$g.mut$i"
  rm -rf -- "$S"
}

# ---------------------------------------------------------------------------------------------------------------------
# the gates
# ---------------------------------------------------------------------------------------------------------------------
: > "$TMP/gates.jsonl"
n_total=0
while IFS= read -r linea <&3; do
  id=$(jq -r '.id' <<<"$linea")
  if [[ -n $SOLO && ",$SOLO," != *",$id,"* ]]; then continue; fi
  n_total=$((n_total + 1))
  log "[$id] $(jq -r '.titulo' <<<"$linea")"
  legs="$TMP/legs-$id.jsonl"; : > "$legs"
  ARCHIVOS_DE_GATE=()
  lit=$(jq -r '.literal' <<<"$linea"); limp=$(jq -r '.limpieza // empty' <<<"$linea")
  correr "$GATES/$id.primario" "$REPO" "$lit" "$limp"
  registrar_leg "$legs" primario 0 primary primario "$lit" "$REPO" "$GATES/$id.primario"
  nm=$(jq '.mutantes | length' <<<"$linea")
  for ((i = 1; i <= nm; i++)); do
    m=$(jq -c ".mutantes[$((i - 1))]" <<<"$linea"); modo=$(jq -r '.modo' <<<"$m")
    case "$modo" in
      copia) correr_copia "$id" "$i" "$m" "$legs" "$lit" "" ;;
      literal)
        mlit=$(jq -r '.literal' <<<"$m"); mlimp=$(jq -r '.limpieza // empty' <<<"$m"); ctl=$(jq -r '.control_literal // empty' <<<"$m")
        if [[ -n $ctl ]]; then
          correr "$GATES/$id.control$i" "$REPO" "$ctl" ""
          registrar_leg "$legs" control "$i" "control: the same command without the mutation" literal "$ctl" "$REPO" "$GATES/$id.control$i"
        fi
        correr "$GATES/$id.mut$i" "$REPO" "$mlit" "$mlimp"
        registrar_leg "$legs" mutante "$i" "$(jq -r '.nombre' <<<"$m")" literal "$mlit" "$REPO" "$GATES/$id.mut$i" ;;
      sellado) : ;;   # read by the judge from the unit's sealed raw run; nothing is executed
      *) log "[$id] unknown mutant mode: $modo"; exit 2 ;;
    esac
  done
  nc=$(jq '.cruces | length' <<<"$linea")
  for ((i = 1; i <= nc; i++)); do
    c=$(jq -c ".cruces[$((i - 1))]" <<<"$linea")
    correr "$GATES/$id.cruce$i" "$REPO" "$(jq -r '.literal' <<<"$c")" "$(jq -r '.limpieza // empty' <<<"$c")"
    registrar_leg "$legs" cruce "$i" "$(jq -r '.nombre' <<<"$c")" literal "$(jq -r '.literal' <<<"$c")" "$REPO" "$GATES/$id.cruce$i"
  done
  "$CAMPANA" sidecar "${ARCHIVOS_DE_GATE[@]}" > /dev/null 2>> "$TMP/sidecar.err" || { log "[$id] campana.sh sidecar failed: $(tail -c 300 "$TMP/sidecar.err")"; exit 1; }
  if ! python3 -B "$TMP/juzgar.py" "$linea" "$legs" "$REPO" "$SALIDA" >> "$TMP/gates.jsonl" 2>> "$TMP/juzgar.err"; then
    log "[$id] the judge failed: $(tail -c 400 "$TMP/juzgar.err")"; exit 1
  fi
  log "[$id] $(tail -n 1 "$TMP/gates.jsonl" | jq -r '"verdict=\(.veredicto) rc=\(.rc) red_mutants=\([.mutantes[]|select(.rojo==true)]|length)/\(.mutantes|length)" + (if .motivo != "" then " reason=" + .motivo[0:200] else "" end)')"
done 3< "$TMP/tabla.jsonl"

[[ $n_total -gt 0 ]] || abortar 2 "$(jq -cn --arg g "$SOLO" '{ok:false,rc:2,causa:"gate_desconocido",gate:$g}')"
GATES_PASS=$(jq -s '[.[]|select(.veredicto=="pass")]|length' "$TMP/gates.jsonl")
GATES_NOREP=$(jq -s '[.[]|select(.veredicto=="no_reproducible")]|length' "$TMP/gates.jsonl")
GATES_FAIL=$(jq -s '[.[]|select(.veredicto=="fail")]|length' "$TMP/gates.jsonl")

if [[ -n $SOLO ]]; then
  # partial run: staged and promoted like a full run, under names that never overwrite the full run's (gates-parcial/, gates-parcial.jsonl)
  cp -- "$TMP/gates.jsonl" "$SAL_U23/gates-parcial.jsonl" && "$CAMPANA" sidecar "$SAL_U23/gates-parcial.jsonl" > /dev/null \
    && promover_u23 gates-parcial gates-parcial.jsonl || { log "could not promote the partial captures"; exit 1; }
  rm -rf -- "$SALIDA"; SALIDA=""
  REPORTADO=1
  jq -cn --argjson t "$n_total" --argjson p "$GATES_PASS" --argjson f "$GATES_FAIL" --argjson r "$GATES_NOREP" --slurpfile g "$TMP/gates.jsonl" \
    --arg origen "$TABLA_ORIGEN" --arg tsha "$TABLA_SHA" --arg tplan "$TABLA_PLAN_SHA" --argjson prueba "$PRUEBA" --arg cert "$CERTIFICABLE" \
    --arg plan_sha "$PLAN_SHA" --arg plan_sellado "$PLAN_SELLADO" --arg repo "$REPO_FISICO" --arg repo_del_script "$REPO_DEL_SCRIPT" \
    '($cert=="true") as $certificable
     | {ok:($p==$t and $certificable), parcial:true, prueba:($prueba==1), certificable:$certificable,
        tabla:{origen:$origen, sha256:$tsha, sha256_derivada_del_plan:$tplan, coincide:($tsha==$tplan)},
        plan:{sha256:$plan_sha, sha256_en_el_sello_de_fase_0:$plan_sellado, coincide:($plan_sha==$plan_sellado)},
        arbol:{repo:$repo, es_el_del_script:($repo==$repo_del_script)},
        gates_total:$t, gates_pass:(if $certificable then $p else "no_certificable" end), gates_pass_medido:$p, gates_fail:$f, gates_no_reproducible:$r,
        gates:[$g[]|{gate,veredicto,rc,motivo}]}'
  [[ $GATES_PASS -eq $n_total && $CERTIFICABLE == true ]]; exit $?
fi

# gates-evidencia.jsonl: ONE line per gate, staged with its sidecar
cp -- "$TMP/gates.jsonl" "$SAL_U23/gates-evidencia.jsonl" && "$CAMPANA" sidecar "$SAL_U23/gates-evidencia.jsonl" > /dev/null || { log "could not stage gates-evidencia.jsonl"; exit 1; }

# ---------------------------------------------------------------------------------------------------------------------
# the seal (sellar_todo, defined above): derived from disk three ways and STAGED; then everything this run produced is PROMOTED together
# (each replaced file archived first) and the installed seal is re-verified cold, as a third party would
# ---------------------------------------------------------------------------------------------------------------------
sellar_todo || { log "could not seal"; exit 1; }
promover_u23 gates gates-evidencia.jsonl "${ITEMS_SELLO[@]}" && promover_inventario || { log "could not promote the files of the run"; exit 1; }
rm -rf -- "$SALIDA"; SALIDA=""
VERIF=$(cmd_verificar_sello 2>/dev/null); VERIF_RC=$?

# capturas_de_la_corrida: the per-run files (outside the seal) with their hashes, for the certificate
capturas_de_la_corrida() {
  ( cd "$OUT" || exit 1
    local f
    jq -n --argjson n "$(find gates -type f ! -name '*.sha256' | wc -l)" \
          --arg listado "$(find gates -type f ! -name '*.sha256' | LC_ALL=C sort | xargs -r sha256sum | sha256sum | cut -d' ' -f1)" \
          --slurpfile f <(for f in gates-evidencia.jsonl sello-corrida-1.txt sello-corrida-1.err sello-universo-1.json sello-corrida-2.txt sello-corrida-2.err sello-universo-2.json; do
                            jq -cn --arg r "evidencia/v2/u23/$f" --arg h "$(sha256sum < "$f" 2>/dev/null | cut -d' ' -f1)" '{ruta:$r,sha256:(if $h=="" then null else $h end)}'
                          done) \
          --arg arch "${ARCH_CORRIDA:+${ARCH_CORRIDA#$REPO/}}" \
          '{gates:{directorio:"evidencia/v2/u23/gates", ficheros:$n, sha256_del_listado:$listado, listado:"sha256sum of every non-sidecar file of gates/ (LC_ALL=C order of the paths), hashed again"},
            ficheros:$f, archivo_de_lo_reemplazado:(if $arch=="" then null else $arch end)}' )
}
capturas_de_la_corrida > "$TMP/capturas.json" 2> "$TMP/capturas.err" || echo "{\"error\":$(head -c 300 "$TMP/capturas.err" | jq -Rs .)}" > "$TMP/capturas.json"

# ---------------------------------------------------------------------------------------------------------------------
# sidecar audit of evidencia/v2 (sees everything the run installed except certificacion.json, which does not exist yet)
# ---------------------------------------------------------------------------------------------------------------------
auditar_sidecars > "$TMP/auditoria.json" 2> "$TMP/auditoria.err" || log "sidecar audit: $(tail -c 300 "$TMP/auditoria.err")"
AUDIT_OK=$(jq -r '(.no_coinciden|length)==0 and (.objetivo_ausente|length)==0 and (.malformados|length)==0 and (.vacios|length)==0' "$TMP/auditoria.json")

# ---------------------------------------------------------------------------------------------------------------------
# debts and facts about this cage (informative: they never change gates_pass)
# ---------------------------------------------------------------------------------------------------------------------
obs() { # obs <name> <command>: raw output (truncated) and rc of a read-only probe
  local nombre=$1 cmd=$2 salida rc
  timeout 60 bash -o pipefail -c "$cmd" < /dev/null > "$TMP/obs.out" 2>&1; rc=$?
  salida=$(head -c 600 "$TMP/obs.out")
  jq -cn --arg n "$nombre" --arg c "$cmd" --arg s "$salida" --argjson rc "$rc" '{nombre:$n,comando:$c,rc:$rc,salida:$s}'
}
escribir_sonda_deuda03 "$TMP/deuda03.sh"      # DEUDA-03 (A-59): QEMU answers the certifier directly; the probe removes every file it creates
{
  obs "extractClase (DEUDA-01)" "awk '/^func extractClase/,/^}/' cmd/srf-capsula/main.go | grep -n 'return'"
  obs "DEUDA-03: QEMU answers the certifier directly (paused guest, torn down)" "bash $TMP/deuda03.sh"
  obs "sealed paused-guest report of U-14 (what DEUDA-03 compares against)" "jq -c '{estado,estado_qmp,rundir}' evidencia/v2/u14/pausado.json; ls -d \$(jq -r .rundir evidencia/v2/u14/pausado.json) 2>&1"
  obs "live srf-cgtest in this cage (B-03 reads sealed evidence, it is not re-run live)" "bin/srf-cgtest --json"
  obs "operator-declared surface holding slot 0's port" "docker port newton-srf-perplexity; jq -c .puertos _estado/residuos/declarados.json"
  obs "/dev/kvm access" "test -r /dev/kvm -a -w /dev/kvm && echo kvm_rw_ok || echo kvm_no_access"
  obs "toolchain" "go version; node --version; docker --version; newton --version"
} > "$TMP/entorno.jsonl"

# what this run left behind, by looking (the census counts sockets only): lock files, /tmp/srf-* entries, staging areas, campaign containers.
# A failed call is null with its error, never 0 (H-10).
ls -A "$REPO/runtime/slots" 2>/dev/null | sort > "$TMP/rt-despues.txt"
ls -A "${TMPDIR:-/tmp}" 2>/dev/null | grep -E '^srf-' | grep -vxF "$(basename "$TMP")" | sort > "$TMP/tmp-despues.txt"
contar_contenedores_srf > "$TMP/ctr.json"
jq -cn --argjson rt "$(comm -13 "$TMP/rt-antes.txt" "$TMP/rt-despues.txt" | jq -R . | jq -sc .)" \
       --argjson tmp "$(comm -13 "$TMP/tmp-antes.txt" "$TMP/tmp-despues.txt" | jq -R . | jq -sc .)" \
       --argjson sal "$(ls -A "$REPO/_cas/sello" 2>/dev/null | grep -E '^\.salida\.' | jq -R . | jq -sc .)" \
       --argjson huerf "$(printf '%s\n' "${HUERFANOS[@]}" | jq -R . | jq -sc 'map(select(length>0))')" \
       --slurpfile ctr "$TMP/ctr.json" \
       '{runtime_slots_nuevos:$rt, tmp_srf_nuevos:$tmp, salidas_de_preparacion_restantes:$sal, contenedores_srf:$ctr[0].contenedores_srf, contenedores_srf_error:$ctr[0].error,
         huerfanos_de_corridas_anteriores:$huerf}' > "$TMP/residuos-corrida.json"

# ---------------------------------------------------------------------------------------------------------------------
# the report (stdout: ONE JSON object; the oracle tees it into evidencia/v2/u23/certificacion.json)
# ---------------------------------------------------------------------------------------------------------------------
"$CAMPANA" verificar-sello > "$TMP/fase0.json" 2>&1; FASE0_RC=$?
jq -n --arg ts "$(date -u +%Y-%m-%dT%H:%M:%SZ)" --argjson pid "$$" --argjson t "$n_total" --argjson p "$GATES_PASS" --argjson f "$GATES_FAIL" --argjson r "$GATES_NOREP" \
  --slurpfile g "$TMP/gates.jsonl" --slurpfile a "$TMP/auditoria.json" --slurpfile s "$TMP/sellar.json" --slurpfile e "$TMP/entorno.jsonl" --slurpfile rc "$TMP/residuos-corrida.json" \
  --slurpfile cap "$TMP/capturas.json" \
  --arg raiz "${RAIZ1:-}" --arg raiz2 "${RAIZ2:-}" --arg verif "$VERIF" --argjson verif_rc "$VERIF_RC" --arg plan_sha "$PLAN_SHA" --arg plan_sellado "$PLAN_SELLADO" \
  --arg fase0 "$(cat "$TMP/fase0.json")" --argjson fase0_rc "$FASE0_RC" --arg sello_ok "$SELLO_OK" --arg audit_ok "$AUDIT_OK" --arg inv_sha "$(sha256sum "$INVENTARIO" | cut -d' ' -f1)" \
  --arg inv_accion "$INV_ACCION" --arg sello_sha "$(sha256sum "$OUT/sello.json" | cut -d' ' -f1)" \
  --arg gates_sha "$(sha256sum "$OUT/gates-evidencia.jsonl" | cut -d' ' -f1)" \
  --arg origen "$TABLA_ORIGEN" --arg tsha "$TABLA_SHA" --arg tplan "$TABLA_PLAN_SHA" --argjson prueba "$PRUEBA" --arg cert "$CERTIFICABLE" \
  --arg repo "$REPO_FISICO" --arg repo_del_script "$REPO_DEL_SCRIPT" --arg sello_f0_sha "$SELLO_F0_SHA" \
  '($cert=="true") as $certificable
   | {esquema:"srf-certificacion/v2", campana:"opensuperficies-nextgen", unidad:"U-23", emitido:$ts, pid:$pid,
    prueba:($prueba==1), certificable:$certificable,
    tabla:{origen:$origen, sha256:$tsha, sha256_derivada_del_plan:$tplan, coincide:($tsha==$tplan), gates:[$g[]|.gate],
           nota:"the gate table is derived from the plan rows on every run; a table that differs from the derived one, a test run, or a plan that is not the one the phase-0 seal pins makes gates_pass read no_certificable"},
    arbol:{repo:$repo, es_el_del_script:($repo==$repo_del_script)},
    gates_total:$t, gates_pass:(if $certificable then $p else "no_certificable" end), gates_pass_medido:$p, gates_fail:$f, gates_no_reproducible:$r,
    modos:{vivo:([$g[]|select(.modo=="vivo")]|length), evidencia_sellada:([$g[]|select(.modo=="evidencia_sellada")]|length), mixto:([$g[]|select(.modo=="mixto")]|length),
           definicion:{vivo:"the verdict comes from executing something now (a build, a test binary, a capsule, the daemon, a census over the live tree, a hash re-verification of the tree)",
                       evidencia_sellada:"the verdict comes from a sealed unit evidence file read by the plan literal; nothing is started",
                       mixto:"both: execution plus sealed evidence in the primary (B-34), a sealed primary with a BLOCKING live cross-check (B-16, B-18, B-21), or an aggregator that runs live instruments and reads the sealed unit files itself (B-31)"}},
    gates:[$g[]|{gate,unidad,modo,veredicto,rc,rc_mutantes:[.mutantes[]|(.leg.rc // null)],mutantes_rojos:([.mutantes[]|select(.rojo==true)]|length),mutantes_total:(.mutantes|length),modos_de_mutante:[.mutantes[]|.modo],cruces_ok:([.cruces[]|select(.ok==true)]|length),cruces_total:(.cruces|length),ranura_sustituta:(.ranura_sustituta!=null),divulgacion,motivo}],
    gates_no_reproducibles:[$g[]|select(.veredicto=="no_reproducible")|{gate,motivo}],
    gates_que_no_pasan:[$g[]|select(.veredicto!="pass")|{gate,veredicto,motivo}],
    gates_con_ranura_sustituta:[$g[]|select(.ranura_sustituta!=null)|.gate],
    adjudicacion:"gates_pass applies the expectation written in each plan row to the captured values (rc, stdout) and requires every blocking mutant to be red and every blocking cross-check to hold; the PASS adjudication and the certification stay with the metaorchestrator",
    divulgaciones:{
      mutante_del_plan_inerte:[$g[]|select(.divulgacion!=null)|{gate,texto:.divulgacion}],
      modos_de_mutante:{literal:([$g[]|.mutantes[]|select(.modo=="literal")]|length), copia:([$g[]|.mutantes[]|select(.modo=="copia")]|length), sellado:([$g[]|.mutantes[]|select(.modo=="sellado")]|length),
                        nota:"copia = the same literal on a scratch copy of the sealed evidence with the guarded field flipped, after a control run on the untouched copy; sellado = read from the unit own sealed raw run. Neither executes the mutant the plan names on live code: they are stand-ins that show the gate literal turns red when the evidence it guards is wrong. For B-03, B-08, B-24, B-29 and B-30 the plan mutant needs a live guest, 50 live sessions, a mounted host bus or the operator display and is replaced this way; for B-20 the plan mutant is inert (see mutante_del_plan_inerte) and a labelled SUBSTITUTE runs"},
      alcance_del_sello:"see sello.json .alcance (cubre and no_cubre)",
      como_fijar_el_certificado:"the seal cannot cover the certificate that carries it: pin certificacion.json by its own sha256 (sha256sum evidencia/v2/u23/certificacion.json), recorded where the operator holds it (a journal line of the certification turn)"},
    gates_evidencia:{ruta:"evidencia/v2/u23/gates-evidencia.jsonl", sha256:$gates_sha},
    capturas_de_la_corrida:$cap[0],
    plan:{sha256:$plan_sha, sha256_en_el_sello_de_fase_0:$plan_sellado, coincide:($plan_sha==$plan_sellado)},
    sello_de_fase_0:{sha256_del_fichero:$sello_f0_sha, verificar_sello_rc:$fase0_rc, informe:($fase0|try fromjson catch $fase0)},
    sello:{ruta:"evidencia/v2/u23/sello.json", sha256_del_fichero:$sello_sha, raiz:$raiz, segunda_corrida:$raiz2, raiz_independiente:$s[0].raiz_independiente, acuerdo_de_las_tres:($sello_ok=="true"),
           verificacion_en_frio:{rc:$verif_rc, informe:($verif|try fromjson catch $verif)}, ficheros:$s[0].ficheros, discrepancias_de_hash:$s[0].n_discrepancias},
    inventario:{ruta:"_cas/sello/INVENTARIO-ARTEFACTOS-NEXTGEN.json", sha256:$inv_sha, accion:$inv_accion},
    auditoria_de_sidecars:($a[0] + {integra:($audit_ok=="true")}),
    deudas:{
      "DEUDA-01":{estado:(if ([$e[]|select(.nombre|startswith("extractClase"))][0].salida|contains("return \"browser\"")) then "abierta" else "revisar" end), medida:([$e[]|select(.nombre|startswith("extractClase"))][0].salida)},
      "DEUDA-02":{estado:"AUSENTE", medida:"the plan owes it to a successor unit scoped when a downstream unit needs it; nothing to measure"},
      "DEUDA-03":{estado:(([$e[]|select(.nombre|startswith("DEUDA-03"))][0].rc) as $r | if $r==0 then "cerrada" elif $r==98 then "no_verificada" else "abierta" end), medida:([$e[]|select(.nombre|startswith("DEUDA-03"))][0].salida)},
      "DEUDA-04":{estado:(if ([$g[]|select(.gate=="B-10")|.cruces[0].ok][0]==true) then "abierta_congelada_en_7_por_INV-7b" else "revisar" end), medida:"INV-7b census of src/displaybook/desktop/desktop.go read 7 unmediated calls; capture in evidencia/v2/u23/gates/B-10.cruce1.stdout"}},
    residuos_de_la_corrida:$rc[0],
    observaciones_del_entorno:$e,
    ok:(($p==$t) and $certificable and ($sello_ok=="true") and ($verif_rc==0) and ($audit_ok=="true"))}' > "$TMP/certificacion.json"
REPORTADO=1
if [[ $JSON -eq 1 ]]; then
  jq -c . "$TMP/certificacion.json"
else
  jq -r '"gates \(.gates_pass)/\(.gates_total)  fail \(.gates_fail)  no_reproducible \(.gates_no_reproducible)  seal \(.sello.raiz)  seal_agreement \(.sello.acuerdo_de_las_tres)  sidecars_intact \(.auditoria_de_sidecars.integra)  modos \(.modos.vivo)/\(.modos.evidencia_sellada)/\(.modos.mixto)", (.gates[]|"\(.gate) \(.modo) \(.veredicto) rc=\(.rc) mutants_red \(.mutantes_rojos)/\(.mutantes_total)")' "$TMP/certificacion.json"
fi
[[ $(jq -r '.ok' "$TMP/certificacion.json") == true ]]
