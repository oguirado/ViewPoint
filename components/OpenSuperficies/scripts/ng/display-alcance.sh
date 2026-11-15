#!/usr/bin/env bash
# display-alcance.sh - campaign-scoped display invariants for a SHARED X display (PLAN v3.6, A-35, DEC-20).
#
# Why this exists: display :1 is the human operator's desktop. A whole-tree diff against an old snapshot measures the
# operator, not the capsule stack (BLQ-NG-05: 174 diff lines, every one an operator application). INV-1 and INV-2 are
# therefore measured over the windows the CAMPAIGN owns, which is what U-02 Door A actually protects: a quarantined
# capsule must never map a window on the operator's display, and must never take the focus.
#
#   display-alcance.sh base                 snapshot: window ids, full tree (forensics), active window -> _estado/display/
#   display-alcance.sh censo [--json]       campaign-owned windows on the display right now
#   display-alcance.sh diff                 one line per violation, nothing when clean (INV-1: `diff | wc -l` = 0)
#   display-alcance.sh foco                 exit 0 unless a campaign window holds focus without a declaration (INV-2)
#   display-alcance.sh declarar --ventanas '<regex>[,<regex>]' --foco si|no   declare the windows a unit legitimately owns
#   display-alcance.sh olvidar-declaracion  clear the declaration (units must clear what they declare)
#
# A window belongs to the campaign when (a) its WM_CLASS matches SRF_PATRON (default: srf-* / opensuperficies),
# (b) its WM_CLIENT_MACHINE is not this host - an X client from inside a container reports the container hostname, which is
# exactly how an escaped capsule window looks and is what U-02 Door A forbids - or (c) its _NET_WM_PID resolves in this
# namespace to a process in a campaign cgroup or to a binary under the repository.
# (c) is a positive signal only: this instrument runs inside a Newton cage with a private PID namespace, so _NET_WM_PID
# values coming from host applications do not resolve here and must never be read as suspicion [MEASURED 2026-09-17].
set -euo pipefail
export LC_ALL=C

R=/home/user/OpenSuperficies
D=${SRF_DISPLAY:-:1}
E=$R/_estado/display
PATRON=${SRF_PATRON:-'(^|[^[:alnum:]])srf[-_]|opensuperficies'}
DECL=$E/slots-declarados.json

# Real windows only: the managed client list plus the window ids that open a child line of the tree.
# Ids scraped loosely out of the tree text (colormaps, ids inside titles) make xprop fail with BadWindow.
todas() {
  {
    xprop -display "$D" -root _NET_CLIENT_LIST 2>/dev/null | sed -e 's/^[^#]*# *//' -e 's/,/ /g' | tr ' ' '\n' | grep -E '^0x[0-9a-f]+$' || true
    xwininfo -display "$D" -root -tree | grep -E '^[[:space:]]+0x[0-9a-f]+ ' | awk '{print $1}' || true
  } | sort -u
}

prop() {
  local out
  out=$(xprop -display "$D" -id "$1" "$2" 2>/dev/null) || return 0
  [[ $out == *"not found"* ]] && return 0
  grep -q '=' <<< "$out" || return 0            # a failed request prints the name without a value
  sed -e 's/^[^=]*= *//' -e 's/"//g' <<< "$out"
}

MAQUINA=$(cat /proc/sys/kernel/hostname)

# clase|nombre|pid|maquina for a window, empty fields when the property is absent
# window titles carry '|' routinely and would shift the record; trailing whitespace must go or WM_CLIENT_MACHINE never matches
limpiar() { tr '|\n\t' '/  ' | head -c 300 | sed -e 's/[[:space:]]\+$//'; }

identidad() {
  local w=$1 c n p m
  c=$(prop "$w" WM_CLASS | limpiar); n=$(prop "$w" WM_NAME | limpiar); p=$(prop "$w" _NET_WM_PID | limpiar); m=$(prop "$w" WM_CLIENT_MACHINE | limpiar)
  [[ $c == *"not found"* ]] && c=""
  [[ $n == *"not found"* ]] && n=""
  [[ $m == *"not found"* ]] && m=""
  [[ $p == *"not found"* || ! $p =~ ^[0-9]+$ ]] && p=""
  printf '%s|%s|%s|%s' "$c" "$n" "$p" "$m"
}

# 0 = campaign-owned, 1 = foreign, 2 = client on another machine (container escape)
dueno() {
  local c=$1 n=$2 p=$3 m=$4
  if [[ -n $m && $m != "$MAQUINA" && $m != localhost && $m != localhost.localdomain ]]; then return 2; fi
  # WM_CLASS only: window titles are operator text and routinely mention the project (a browser tab, an editor buffer),
  # which would make an operator window look like a capsule [MEASURED 2026-09-17: a Chromium title matched].
  if [[ -n $c ]] && grep -qiE "$PATRON" <<< "$c"; then return 0; fi
  if [[ -n $p && -r /proc/$p/cgroup ]] && grep -qE 'ia\.slice/srf|srf-ng|srf-probe' "/proc/$p/cgroup" 2>/dev/null; then return 0; fi
  if [[ -n $p && -r /proc/$p/exe ]] && [[ "$(readlink -f "/proc/$p/exe" 2>/dev/null || true)" == $R/* ]]; then return 0; fi
  return 1
}

declarados() { [[ -r $DECL ]] && jq -r '.ventanas[]?' "$DECL" 2>/dev/null || true; }
declarado_p() {
  local c=$1 n=$2 re
  while IFS= read -r re; do
    [[ -z $re ]] && continue
    printf '%s %s' "$c" "$n" | grep -qiE "$re" && return 0
  done < <(declarados)
  return 1
}
foco_autorizado() { [[ -r $DECL ]] && [[ "$(jq -r '.foco_autorizado // false' "$DECL" 2>/dev/null)" == "true" ]]; }

cmd_base() {
  mkdir -p "$E"
  todas > "$E/base-ids.txt"
  xwininfo -display "$D" -root -tree > "$E/base-arbol.txt"
  DISPLAY=$D xdotool getactivewindow > "$E/base-activa.txt" 2>/dev/null || echo none > "$E/base-activa.txt"
  date -Is > "$E/base-ts.txt"
  printf '{"ok":true,"display":"%s","ventanas":%d,"activa":"%s","ts":"%s"}\n' \
    "$D" "$(wc -l < "$E/base-ids.txt")" "$(cat "$E/base-activa.txt")" "$(cat "$E/base-ts.txt")"
}

recorrer() { # emits: estado|id|clase|nombre|pid|maquina   estado in campana|maquina_ajena (foreign host windows are not emitted)
  local w c n p m
  while read -r w; do
    [[ -z $w ]] && continue
    IFS='|' read -r c n p m <<< "$(identidad "$w")"
    [[ -z $c && -z $n && -z $p && -z $m ]] && continue
    [[ $c == mutter-x11-frames* ]] && continue   # the window manager's frame duplicates the client it decorates
    set +e; dueno "$c" "$n" "$p" "$m"; local r=$?; set -e
    case $r in
      0) printf 'campana|%s|%s|%s|%s|%s\n' "$w" "$c" "$n" "$p" "$m" ;;
      2) printf 'maquina_ajena|%s|%s|%s|%s|%s\n' "$w" "$c" "$n" "$p" "$m" ;;
      *) : ;;
    esac
  done < <(todas)
}

cmd_censo() {
  local json=0; [[ ${1:-} == --json ]] && json=1
  if [[ $json -eq 1 ]]; then
    recorrer | python3 -B -c '
import sys, json
f=[]
for l in sys.stdin:
    e,i,c,n,p,m=(l.rstrip("\n").split("|")+[""]*6)[:6]
    f.append({"estado":e,"id":i,"clase":c,"nombre":n,"pid":p,"maquina":m})
print(json.dumps({"ventanas_campana":[x for x in f if x["estado"]=="campana"],
                  "maquina_ajena":[x for x in f if x["estado"]=="maquina_ajena"],
                  "n_campana":len([x for x in f if x["estado"]=="campana"]),
                  "n_maquina_ajena":len([x for x in f if x["estado"]=="maquina_ajena"])},ensure_ascii=False))'
  else
    recorrer
  fi
}

cmd_diff() { # one line per violation; silence = invariant green
  local estado id c n p m
  while IFS='|' read -r estado id c n p m; do
    [[ -z ${estado:-} ]] && continue
    if [[ $estado == campana ]]; then
      declarado_p "$c" "$n" && continue
      printf 'VIOLACION ventana_de_campana_no_declarada id=%s clase=%s nombre=%s pid=%s\n' "$id" "$c" "$n" "$p"
    else
      printf 'VIOLACION cliente_x_de_otra_maquina id=%s clase=%s nombre=%s maquina=%s\n' "$id" "$c" "$n" "$m"
    fi
  done < <(cmd_censo)
}

cmd_foco() {
  local w c n p
  w=$(DISPLAY=$D xdotool getactivewindow 2>/dev/null || echo "")
  [[ -z $w ]] && { echo '{"ok":true,"activa":"none"}'; return 0; }
  w=$(printf '0x%x' "$w")
  IFS='|' read -r c n p m <<< "$(identidad "$w")"
  set +e; dueno "$c" "$n" "$p" "$m"; local r=$?; set -e
  if [[ $r -eq 0 || $r -eq 2 ]]; then
    if foco_autorizado || declarado_p "$c" "$n"; then
      printf '{"ok":true,"activa":"%s","clase":"%s","dueno":"campana","declarada":true}\n' "$w" "$c"; return 0
    fi
    printf '{"ok":false,"activa":"%s","clase":"%s","dueno":"campana","declarada":false,"causa":"foco_secuestrado"}\n' "$w" "$c"; return 1
  fi
  printf '{"ok":true,"activa":"%s","clase":"%s","dueno":"ajena"}\n' "$w" "$c"; return 0
}

cmd_declarar() {
  local ventanas="" foco=no
  while [[ $# -gt 0 ]]; do case "$1" in
    --ventanas) ventanas=$2; shift 2;; --foco) foco=$2; shift 2;; *) echo "arg_desconocido:$1" >&2; exit 2;; esac; done
  mkdir -p "$E"
  python3 -B -c '
import json,sys
v=[x for x in sys.argv[1].split(",") if x]
json.dump({"ventanas":v,"foco_autorizado":sys.argv[2]=="si"},open(sys.argv[3],"w"),ensure_ascii=False)
print(json.dumps({"ok":True,"ventanas":v,"foco_autorizado":sys.argv[2]=="si"}))' "$ventanas" "$foco" "$DECL"
}

case "${1:-ayuda}" in
  base)    cmd_base ;;
  censo)   shift; cmd_censo "${1:-}" ;;
  diff)    cmd_diff ;;
  foco)    cmd_foco ;;
  declarar) shift; cmd_declarar "$@" ;;
  olvidar-declaracion) rm -f "$DECL"; echo '{"ok":true,"declaracion":"borrada"}' ;;
  *) echo "uso: $0 {base|censo [--json]|diff|foco|declarar --ventanas <re>[,<re>] --foco si|no|olvidar-declaracion}" >&2; exit 2 ;;
esac
