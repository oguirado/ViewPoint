#!/usr/bin/env bash
# Host-side evidence harvest for one capsule, run BEFORE any `docker rm` (W3 lost the WL-7 logs to that order).  Usage: harvest.sh NAME OUTDIR
# Copies tails (each <= 32 KB; the bytes already in OUTDIR count, so the whole episode dir stays <= 100 KB - 2 KB margin = g_disk_size's MAX_EP_BYTES; JW4-02;
# CU_HARVEST_SUB optionally lowers harvest's own sub-budget below that remainder): docker logs, inspect (ExitCode, OOMKilled),
# in-capsule /tmp/cl.log, the launcher's raw chromium rc side file and the capsule-diag.sh output.  Idempotent: if the container is gone and
# OUTDIR/.harvested exists, OUTDIR is left untouched.  Exit 0 always (container_absent is recorded), 2 on usage; never 1.
set -u
trap 'exit 5' ERR
PER_FILE=32768; EP_MAX=$((100 * 1024 - 2048)); SUB="${CU_HARVEST_SUB:-98304}"; TMO="${CU_HARVEST_TMO:-30}"
[ $# -eq 2 ] && [ -n "$1" ] && [ -n "$2" ] || { echo "usage: harvest.sh NAME OUTDIR" >&2; exit 2; }
NAME="$1"; OUT="$2"
[[ "$NAME" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]*$ ]] || { echo "harvest: bad container name" >&2; exit 2; }
[[ "$SUB" =~ ^[0-9]{1,9}$ ]] || { echo "harvest: bad CU_HARVEST_SUB" >&2; exit 2; }
# ADC-W4-03: idempotence first, before any docker call (a live container must not be re-harvested over an earlier harvest)
[ -e "$OUT/.harvested" ] && exit 0
umask 027
# JW4-02: episode budget = EP_MAX minus the bytes already in OUTDIR (receipts, rmcap output, diag files); own budget = min(SUB, that remainder)
existing=0
[ -d "$OUT" ] && existing=$(find "$OUT" -type f -not -type l -printf '%s\n' 2>/dev/null | awk '{s+=$1} END{print s+0}')
TOTAL=$((EP_MAX - existing)); [ "$TOTAL" -le "$SUB" ] || TOTAL=$SUB
used=0

# harvest_one FILE CMD...: run CMD bounded by timeout, keep the tail within the per-file and remaining total budget
harvest_one() {
  local f="$1" left cap prev=0; shift
  [ -f "$OUT/$f" ] && prev=$(wc -c <"$OUT/$f")   # a same-named earlier file is replaced, so its bytes are not extra
  left=$((TOTAL + prev - used)); cap=$PER_FILE; [ "$left" -ge "$cap" ] || cap=$left
  if [ "$cap" -le 0 ]; then return 0; fi
  timeout "$TMO" "$@" 2>&1 | tail -c "$cap" >"$OUT/$f" || true
  used=$((used + $(wc -c <"$OUT/$f") - prev))
}

if ! timeout "$TMO" docker inspect "$NAME" >/dev/null 2>&1; then
  # gone (an earlier live harvest was already handled by the .harvested check above)
  mkdir -p -- "$OUT" 2>/dev/null || exit 0
  echo "container_absent $NAME" >"$OUT/container_absent"
  exit 0
fi
mkdir -p -- "$OUT" || exit 5
harvest_one docker_logs.txt docker logs --tail 400 "$NAME"
harvest_one inspect.txt docker inspect -f 'ExitCode={{.State.ExitCode}} OOMKilled={{.State.OOMKilled}} Status={{.State.Status}} Error={{.State.Error}}' "$NAME"
harvest_one cl_log.txt docker exec "$NAME" tail -c "$PER_FILE" /tmp/cl.log
harvest_one chromium_raw_rc.txt docker exec "$NAME" cat /tmp/chromium-raw.rc
harvest_one capsule_diag.txt docker exec -w /opt/cu "$NAME" /opt/cu/exec-umask.sh /opt/cu/diag/capsule-diag.sh
: >"$OUT/.harvested"
exit 0
