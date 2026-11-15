#!/usr/bin/env bash
# oracle_gates.sh: W6a oracle gate bundle (proxy/launch/no-listener, label, cross-check suites; determinism x2; storage law; oracle mutants).
#   oracle_gates.sh                 full bundle; rc 0 ok, 5 any failure
#   oracle_gates.sh --canon A B     rc 0 iff the canonical projections (sorted keys, run_id and measured t_us dropped) of two JSONL files hash equal and are non-empty
#   oracle_gates.sh --storage DIR   rc 0 iff every episode dir <= 100 KB, no frame files, DIR non-empty (g_disk_size rules)
# rc 2 usage. Never 1. Mutants are exercised by scripts/cu/tests/test_oracle_gates.py (tempdir copy, AW6-11); INT wires `g oracle` and mutants.json.
export PATH="$PATH:/usr/local/go/bin" PYTHONDONTWRITEBYTECODE=1
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

canon() { # canon A B
  [ -f "$1" ] && [ -f "$2" ] || return 2
  python3 -B -I - "$1" "$2" <<'PY'
import hashlib, json, sys
DROP = ("run_id", "t_us")
def proj(p):
    out = []
    for ln in open(p, encoding="utf-8").read().split("\n"):
        if not ln.strip():
            continue
        o = json.loads(ln)
        if not isinstance(o, dict):
            raise ValueError("row not an object")
        out.append(json.dumps({k: v for k, v in o.items() if k not in DROP}, sort_keys=True, separators=(",", ":")))
    if not out:
        raise ValueError("empty")
    return hashlib.sha256("\n".join(out).encode()).hexdigest()
try:
    a, b = proj(sys.argv[1]), proj(sys.argv[2])
except (ValueError, OSError):
    print("CANON_ERROR"); sys.exit(5)
print("canon_a=%s canon_b=%s" % (a, b))
sys.exit(0 if a == b else 5)
PY
  local rc=$?; [ $rc -eq 0 ] || [ $rc -eq 5 ] || rc=5; return $rc
}

storage() { # storage DIR
  [ -d "$1" ] || return 2
  python3 -B -I - "$1" "$REPO" <<'PY'
import os, subprocess, sys
d, repo = sys.argv[1], sys.argv[2]
EXT = (".png", ".rgb", ".ppm", ".pgm", ".raw", ".jpg", ".jpeg", ".bmp", ".webp", ".lz4", ".yuv")
MAGIC = (b"\x89PNG", b"\xff\xd8\xff", b"P6", b"P5", b"BM", b"RIFF")
bad, n = [], 0
for dp, _, fn in os.walk(d):
    for f in fn:
        p = os.path.join(dp, f)
        if os.path.islink(p):
            bad.append("symlink:" + p); continue
        n += 1
        if f.lower().endswith(EXT):
            bad.append("frame_ext:" + f)
        with open(p, "rb") as fh:
            if fh.read(8).startswith(MAGIC):
                bad.append("frame_magic:" + f)
if n == 0:
    bad.append("empty")
cmd = ["python3", "-B", os.path.join(repo, "scripts/cu/g_disk_size.py"), "--out", d]
wd = os.path.join(repo, "build", "wheels")
if not [w for w in (os.listdir(wd) if os.path.isdir(wd) else []) if w.endswith(".whl")]:
    print("STORAGE_NOTE wheel absent (fresh clone): size check without pin activation; the frame ext/magic scan above is unchanged")  # ADC-W6A-05
    cmd.append("--no-pin")
r = subprocess.run(cmd, capture_output=True)
if r.returncode != 0:
    bad.append("g_disk_size rc %d" % r.returncode)
for b in bad:
    print("STORAGE_BAD", b)
sys.exit(5 if bad else 0)
PY
  local rc=$?; [ $rc -eq 0 ] || rc=5; return $rc
}

case "${1:-}" in
  --canon) [ $# -eq 3 ] || exit 2; canon "$2" "$3"; exit $? ;;
  --storage) [ $# -eq 2 ] || exit 2; storage "$2"; exit $? ;;
  "") ;;
  *) echo "usage: oracle_gates.sh [--canon A B | --storage DIR]" >&2; exit 2 ;;
esac

cd "$REPO" || exit 5
FAILS=0
g() { # g <name> <cmd...>
  local n="$1"; shift
  timeout "${GATE_TMO:-300}" taskset -c "${CU_CPUSET:-10-14}" "$@"; local rc=$?
  echo "GATE $n rc=$rc"
  [ $rc -eq 0 ] || FAILS=$((FAILS+1))
}
g oracle-bash-n bash -n scripts/cu/oracle_gates.sh docker/cu/oracle/launch-oracle-cu.sh docker/cu/tests/test_oracle_launch.sh
g oracle-proxy python3 -B -m unittest discover -s docker/cu/tests -p test_cdp_proxy.py
g oracle-launch bash docker/cu/tests/test_oracle_launch.sh
g oracle-launcher-unchanged bash -c 'cd "$1"; [ "$(sha256sum docker/cu/launch-chromium-cu.sh | cut -c1-8)" = 854c560f ] && [ "$(sha256sum docker/cu/profiles/chromium.flags | cut -c1-8)" = 2e3b876e ]' _ "$REPO"
g oracle-labels python3 -B -m unittest discover -s bench/oracle/tests -t .
g oracle-crosscheck python3 -B -m unittest bench.oracle.tests.test_crosscheck
# determinism x2: same fixture stream, two runs (different run_id), canonical projections must hash equal
SCR="$REPO/build/room_G/oracle_det"
rm -rf "$SCR"; mkdir -p "$SCR/a" "$SCR/b"
for k in a b; do
  g "oracle-det-run-$k" python3 -B -I - "$REPO" "$SCR/$k/rows.jsonl" "run_$k" <<'PY'
import json, sys
repo, out, rid = sys.argv[1:4]
sys.path.insert(0, repo)
from bench.oracle import labels_oracle as L
d = json.load(open(repo + "/bench/oracle/tests/fixtures/oracle_samples_wl1s.json"))
labels = L.derive(d["samples"], d["ops"], 1920, 1080, rid)
if not labels:
    sys.exit(5)
L.write_jsonl(out, labels)
PY
done
g oracle-det-x2 bash scripts/cu/oracle_gates.sh --canon "$SCR/a/rows.jsonl" "$SCR/b/rows.jsonl"
g oracle-storage bash scripts/cu/oracle_gates.sh --storage "$SCR"
GATE_TMO=1200 g oracle-gate-tests python3 -B -m unittest scripts.cu.tests.test_oracle_gates
[ "$FAILS" -eq 0 ] && { echo "ORACLE GATES OK"; exit 0; }
echo "ORACLE GATES FAIL $FAILS"; exit 5
