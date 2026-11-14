#!/usr/bin/env bash
# Oracle launch (W6a lane P): the allow-list proxy spawns the UNCHANGED launch-chromium-cu.sh with CDP pipes on fd 3/4; the only
# difference vs the plain path is PATH, which puts the oracle shim (it appends --remote-debugging-pipe) before the real chromium.
# Usage: launch-oracle-cu.sh [--sandbox-mode M] URL. Env: OS_ORACLE_FIFO_DIR (default /run/newton-tenant), OS_ORACLE_TTL (default 3600).
# Exit: 0 ok, 2 usage/refused input, 5 refusal/failure, other chromium rc propagated (never 1).
set -euo pipefail
trap 'echo "launch-oracle-cu: E_LAUNCH failed at line $LINENO" >&2; exit 5' ERR
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
[ $# -ge 1 ] || { echo "launch-oracle-cu: usage: [--sandbox-mode M] URL" >&2; exit 2; }
URL="${*: -1}"
export PATH="$HERE/bin:$PATH"
rc=0
python3 -B "$HERE/cdp_proxy.py" serve --allowlist "$HERE/allowlist.json" --scene-url "$URL" \
  --fifo-dir "${OS_ORACLE_FIFO_DIR:-/run/newton-tenant}" --ttl "${OS_ORACLE_TTL:-3600}" \
  -- bash "$HERE/../launch-chromium-cu.sh" "$@" || rc=$?
[ "$rc" -ne 1 ] || rc=5
exit "$rc"
