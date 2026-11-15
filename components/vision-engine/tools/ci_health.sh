#!/bin/bash
# ci_health.sh [outdir]: per-file health of the test suite (one fresh `python3 -m unittest <module>` per test file,
# 300 s cap each, serial). Heavy files listed in HEAVY are reported as SKIPPED_HEAVY. Exit 1 if any file is not OK.
# Used by .github/workflows/ci.yml on the default branch; also runnable locally from the repository root.
set -u
OUT=${1:-ci_health}; mkdir -p "$OUT/logs"; RES="$OUT/health.res"; : > "$RES"
HEAVY="tests/test_fx2_f3_harvest_production.py"
bad=0; n=0
for f in tests/test_*.py; do
  n=$((n+1))
  case " $HEAVY " in *" $f "*) echo "$f SKIPPED_HEAVY" >> "$RES"; continue;; esac
  m=${f%.py}; m=${m//\//.}
  timeout 300 python3 -m unittest "$m" > "$OUT/logs/$m.log" 2>&1; rc=$?
  r=$(grep -E '^(OK|FAILED)' "$OUT/logs/$m.log" | tail -1)
  [ $rc -eq 5 ] && r="NO_TESTS"
  echo "$m rc=$rc ${r:-NORESULT}" >> "$RES"
  case "$rc:$r" in 0:OK*|5:*) ;; *) bad=$((bad+1));; esac
done
echo "files=$n not_ok=$bad" >> "$RES"
[ $bad -eq 0 ]
