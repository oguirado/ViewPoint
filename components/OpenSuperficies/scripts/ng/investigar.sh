#!/usr/bin/env bash
# investigar.sh - external research instrument (PLAN v3.8, A-38). The metaorchestrator must open a research task
# before any repair that follows a failed attempt: a second attempt written from the same knowledge repeats the
# first one. U-11 is the measured case (three attempts, converging fixes, safety property still broken).
#
#   investigar.sh estado
#       prints which engines are available here.
#   investigar.sh consulta --unidad U-NN --tema '<question in English>' [--contexto <file>] --salida <out.json>
#       Perplexity when PPLX_API_KEY (or PERPLEXITY_API_KEY) is exported: sonar-pro, citations returned.
#       Without a key it writes a PENDIENTE research task: the MO then answers it with its own WebSearch /
#       WebFetch and the deepwiki MCP, and records the result with `registrar`.
#   investigar.sh registrar --salida <out.json> --resumen '<finding>' --fuentes '<url>[,<url>]' [--motor <name>]
#       fills a PENDIENTE task with what the research produced.
#
# Budget (A-38): at most SRF_TOPE_CONSULTAS (default 3) paid queries per unit and attempt; the count lives in
# _estado/investigacion/registro.jsonl. Paid queries beyond the cap are a CRITICAL decision, as in A-32.
set -euo pipefail
export LC_ALL=C

R=/home/user/OpenSuperficies
E=$R/_estado/investigacion
REG=$E/registro.jsonl
TOPE=${SRF_TOPE_CONSULTAS:-0}   # A-42: 0 = no cap. Research is expected, not rationed.
CLAVE=${PPLX_API_KEY:-${PERPLEXITY_API_KEY:-}}
MODELO=${SRF_MODELO_PERPLEXITY:-sonar-reasoning-pro}
# Perplexity exposes two endpoints [MEASURED 2026-09-18, docs.perplexity.ai]: the Agent API at /v1/agent
# (alias /v1/responses) and the legacy Sonar chat completions, supported only until 2026-09-27. Default to the
# Agent API and fall back to the legacy one, so the instrument survives that date either way.
API=${SRF_API_PERPLEXITY:-agent}

die() { printf '{"ok":false,"rc":%d,"causa":"%s"}\n' "$1" "$2" >&2; exit "$1"; }

cmd_estado() {
  local pplx=no; [[ -n $CLAVE ]] && pplx=si
  printf '{"perplexity_api":"%s","endpoint":"%s","modelo":"%s","modelos_conocidos":["sonar","sonar-pro","sonar-reasoning-pro","sonar-deep-research"],"tope_por_unidad_intento":%d,"motores_del_metaorquestador":["WebSearch","WebFetch","deepwiki MCP"],"registro":"%s"}\n' \
    "$pplx" "$API" "$MODELO" "$TOPE" "$REG"
}

usadas() { # unit + attempt so far
  [[ -r $REG ]] || { echo 0; return; }
  python3 -B -c 'import json,sys
n=0
for l in open(sys.argv[1]):
    try: d=json.loads(l)
    except Exception: continue
    if d.get("unidad")==sys.argv[2] and d.get("motor")=="perplexity": n+=1
print(n)' "$REG" "$1"
}

anota() {
  mkdir -p "$E"
  python3 -B -c 'import json,sys,os,fcntl,datetime
l=json.dumps({"ts":datetime.datetime.now().astimezone().isoformat(timespec="seconds"),"unidad":sys.argv[1],
              "motor":sys.argv[2],"tema":sys.argv[3][:300],"salida":sys.argv[4],"rc":int(sys.argv[5])},ensure_ascii=False)+"\n"
b=l.encode(); assert len(b)<=4096
fd=os.open(sys.argv[6],os.O_WRONLY|os.O_CREAT|os.O_APPEND,0o644)
fcntl.flock(fd,fcntl.LOCK_EX); os.write(fd,b); os.fsync(fd); os.close(fd)' "$@" "$REG"
}

cmd_consulta() {
  local unidad="" tema="" contexto="" salida="" forzar=0
  while [[ $# -gt 0 ]]; do case "$1" in
    --unidad) unidad=$2; shift 2;; --tema) tema=$2; shift 2;; --contexto) contexto=$2; shift 2;;
    --salida) salida=$2; shift 2;; --forzar) forzar=1; shift;; *) die 2 "arg_desconocido:$1";; esac; done
  [[ -n $unidad && -n $tema && -n $salida ]] || die 2 argumentos_incompletos
  "$R/scripts/ng/campana.sh" parada >/dev/null || die 3 parada_activa
  mkdir -p "$(dirname "$salida")" "$E"
  local ctx=""; [[ -n $contexto && -r $contexto ]] && ctx=$(head -c 6000 "$contexto")

  if [[ -z $CLAVE ]]; then
    python3 -B -c 'import json,sys
json.dump({"ok":True,"estado":"PENDIENTE","motor":"metaorquestador","unidad":sys.argv[1],"tema":sys.argv[2],
 "contexto":sys.argv[3],
 "instruccion":"No Perplexity key is exported. Answer this with your own WebSearch and WebFetch, and with the deepwiki MCP for library semantics (ask_question on the upstream repository). Prefer primary sources: the protocol or library documentation, the upstream issue tracker, and the source of the version actually pinned in go.mod. Then run investigar.sh registrar to fill this file.",
 "consultas_sugeridas":[sys.argv[2]]},open(sys.argv[4],"w"),ensure_ascii=False,indent=1)
print(json.dumps({"ok":True,"estado":"PENDIENTE","salida":sys.argv[4],"motor":"metaorquestador"}))' \
      "$unidad" "$tema" "$ctx" "$salida"
    anota "$unidad" metaorquestador "$tema" "$salida" 0
    return 0
  fi

  local n; n=$(usadas "$unidad")
  if (( TOPE > 0 && n >= TOPE && forzar == 0 )); then
    die 77 "tope_de_consultas_pagadas:$n/$TOPE"
  fi
  local cuerpo rc url
  cuerpo=$(python3 -B -c 'import json,sys
sis="You are a senior systems engineer. Answer in English. Be specific, cite primary sources, prefer the documented semantics of the exact library version. If the question is about a protocol, quote the relevant part of the protocol documentation."
u=sys.argv[1]+("\n\nContext:\n"+sys.argv[2] if sys.argv[2] else "")
if sys.argv[4]=="agent":
    print(json.dumps({"model":sys.argv[3],"input":sis+"\n\n"+u}))
else:
    print(json.dumps({"model":sys.argv[3],"messages":[{"role":"system","content":sis},{"role":"user","content":u}]}))' \
    "$tema" "$ctx" "$MODELO" "$API")
  url=https://api.perplexity.ai/v1/agent
  [[ $API == chat ]] && url=https://api.perplexity.ai/chat/completions
  set +e
  curl -sS --max-time 180 "$url" \
    -H "Authorization: Bearer $CLAVE" -H 'Content-Type: application/json' -d "$cuerpo" > "$salida.raw"
  rc=$?
  # the Agent API answers 404/400 on an account still on the legacy endpoint: retry once on chat completions
  if [[ $rc -eq 0 && $API == agent ]] && grep -qiE '"error"|not found|unsupported' "$salida.raw"; then
    cuerpo=$(python3 -B -c 'import json,sys
sis="You are a senior systems engineer. Answer in English. Cite primary sources."
print(json.dumps({"model":sys.argv[2],"messages":[{"role":"user","content":sys.argv[1]}]}))' "$tema" "$MODELO")
    curl -sS --max-time 180 https://api.perplexity.ai/chat/completions \
      -H "Authorization: Bearer $CLAVE" -H 'Content-Type: application/json' -d "$cuerpo" > "$salida.raw"
    rc=$?
  fi
  set -e
  if [[ $rc -ne 0 ]]; then anota "$unidad" perplexity "$tema" "$salida" "$rc"; die 1 "perplexity_sin_respuesta:$rc"; fi
  python3 -B -c 'import json,sys
d=json.load(open(sys.argv[1]))
# Agent API: output_text, or output[].content[].text; legacy: choices[0].message.content
msg=d.get("output_text") or ""
if not msg:
    for o in d.get("output") or []:
        for c in (o.get("content") or []):
            if isinstance(c,dict) and c.get("text"): msg+=c["text"]
if not msg:
    msg=(d.get("choices") or [{}])[0].get("message",{}).get("content","")
json.dump({"ok":bool(msg),"estado":"RESPONDIDA","motor":"perplexity","modelo":d.get("model"),
           "unidad":sys.argv[2],"tema":sys.argv[3],"resumen":msg,
           "fuentes":d.get("citations") or d.get("search_results") or [],
           "uso":d.get("usage",{})},open(sys.argv[4],"w"),ensure_ascii=False,indent=1)
print(json.dumps({"ok":bool(msg),"estado":"RESPONDIDA","motor":"perplexity","salida":sys.argv[4],
                  "fuentes":len(d.get("citations") or [])}))' "$salida.raw" "$unidad" "$tema" "$salida"
  rm -f "$salida.raw"
  anota "$unidad" perplexity "$tema" "$salida" 0
}

cmd_registrar() {
  local salida="" resumen="" fuentes="" motor=metaorquestador
  while [[ $# -gt 0 ]]; do case "$1" in
    --salida) salida=$2; shift 2;; --resumen) resumen=$2; shift 2;; --fuentes) fuentes=$2; shift 2;;
    --motor) motor=$2; shift 2;; *) die 2 "arg_desconocido:$1";; esac; done
  [[ -n $salida && -n $resumen ]] || die 2 argumentos_incompletos
  python3 -B -c 'import json,sys,os
p=sys.argv[1]
d=json.load(open(p)) if os.path.exists(p) else {}
d.update({"ok":True,"estado":"RESPONDIDA","motor":sys.argv[4],"resumen":sys.argv[2],
          "fuentes":[x for x in sys.argv[3].split(",") if x]})
json.dump(d,open(p,"w"),ensure_ascii=False,indent=1)
print(json.dumps({"ok":True,"estado":"RESPONDIDA","salida":p,"fuentes":len(d["fuentes"])}))' \
    "$salida" "$resumen" "$fuentes" "$motor"
}

case "${1:-ayuda}" in
  estado) cmd_estado ;;
  consulta) shift; cmd_consulta "$@" ;;
  registrar) shift; cmd_registrar "$@" ;;
  *) echo "uso: $0 {estado|consulta --unidad U-NN --tema '<q>' [--contexto <f>] --salida <f>|registrar --salida <f> --resumen '<r>' --fuentes '<u,u>'}" >&2; exit 2 ;;
esac
