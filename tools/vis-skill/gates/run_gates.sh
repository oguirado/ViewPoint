#!/bin/sh
set -eu
VIS_GATE_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$VIS_GATE_ROOT"
python3 -I - "$VIS_GATE_ROOT" "${1:---all}" <<'PY'
import os,pathlib,sys
root=pathlib.Path(sys.argv[1]);sys.path.insert(0,str(root/'gates'))
from common import execute,note,REPORT
if not REPORT.exists():REPORT.write_text('# Writer gate results\n\nObserved results; text only. No hosted pixels sent.\n')
mode=sys.argv[2]
if mode not in ('--all','--unit','--must-fail','--real'):raise SystemExit('usage: gates/run_gates.sh [--all|--unit|--must-fail|--real]')
failed=[];skipped=[]
for kind,path in [('unit','gates/unit/test_cli.py'),('unit','gates/unit/test_perf.py'),('unit','gates/unit/test_runtime.py'),('unit','gates/unit/test_budget.py'),('must-fail','gates/must_fail/test_cleanup.py'),('must-fail','gates/must_fail/test_stability_cleanup.py'),('must-fail','gates/must_fail/test_displayfd.py'),('must-fail','gates/must_fail/test_startup.py'),('must-fail','gates/must_fail/test_rules.py'),('must-fail','gates/must_fail/test_responsiveness.py'),('must-fail','gates/must_fail/test_lean_adopted.py'),('must-fail','gates/must_fail/test_exit.py'),('must-fail','gates/writer/test_observations.py'),('real','gates/real_programs.py')]:
    if mode in ('--all','--'+kind):
        # Inside safe_run (NEWTON_TEST_PIDNS=1) the memory-capped scope already exists and the user bus does not.
        scope=[] if os.environ.get('NEWTON_TEST_PIDNS')=='1' else ['systemd-run','--user','--scope','-q','-p','MemoryMax=4G','-p','MemorySwapMax=0','--collect']
        result=execute([*scope,sys.executable,'-I',str(root/path)],expected=None,timeout=600,brief=False)
        if result.returncode==77:skipped.append(path)
        elif result.returncode:failed.append((path,result.returncode))
if skipped:note('Skipped gate sets (rc 77, reason in their output) [MEASURED]: '+repr(skipped))
if failed:
    note('Failed gate sets [MEASURED]: '+repr(failed));raise SystemExit(1)
# Every listed set is required: a skipped set leaves the run incomplete, never green (rc 3).
if skipped:
    note('INCOMPLETE [MEASURED]: '+str(len(skipped))+' required gate set(s) skipped; this run certifies nothing for them.');raise SystemExit(3)
note('Requested gate sets passed [MEASURED]. gates/false_green is run separately (see docs/RELEASE_NOTES.md for its contract updates).')
PY
