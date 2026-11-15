"""Mutation sweep: remove one check per gate, run the tests, expect the test suite to FAIL."""
import os, shutil, subprocess, sys, tempfile
src = sys.argv[1] if len(sys.argv) > 1 else os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MUT = [
 ("g1_paths.py", 'out.append(("DISPLAY_1", "display :1 in `%s`" % raw[:120]))', "pass"),
 ("g1_paths.py", 'out.append(("FORBIDDEN_RT", "%s in `%s`" % (m.group(0), raw[:120])))', "pass"),
 ("g1_paths.py", 'findings.append(finding("SWARM_TREE_COMMIT", "%s:%s" % (worktree, name),\n                                    "branch changes a Gemini-only file versus %s" % base))', "pass"),
 ("g2_secrets.py", 'out.append(finding(rule, "%s:%d:%d" % (path, ln, m.start() + 1), _redact(m), sev))', "pass"),
 ("g2_secrets.py", 'out.append(("CREDENTIAL_FILE_READ", "`%s ...` names a credential path" % c["head"], "red"))', "pass"),
 ("g3_memcaps.py", 'out.append(("UNCAPPED", "%s outside systemd-run: `%s`" % (kind, raw)))', "pass"),
 ("g3_memcaps.py", "elif eff > cap_bytes:", "elif False:"),
 ("g3_memcaps.py", 'if str(cap.get("MemorySwapMax", "")).strip() != "0":', "if False:"),
 ("g4_pidstops.py", "if head in _NAME_KILLERS:", "if False:"),
 ("g4_pidstops.py", "if _PATTERN_SUB.search(t) or not _PIDFILE_SUB.match(t.strip('\"')):", "if False:"),
 ("g5_nonce.py", "if rule in ran:", "if False:"),
 ("g5_nonce.py", "if lines[0] != want:", "if False:"),
 ("g6_claims.py", "if outside and verdict not in REFUSALS:", "if False:"),
 ("g6_claims.py", "if verdict in CLEAN and (spill or eff_out):", "if False:"),
 ("g7_reconcile.py", "elif now_ms - at > freshness_ms or now_ms < at:", "elif False:"),
 ("g7_reconcile.py", "if mem != want:", "if False:"),
 ("g7_reconcile.py", "if started[n] != d.get(\"Id\"):", "if False:"),
 ("g7_reconcile.py", "for f in O.observe(spec, inspected):", "for f in []:"),
 ("run_gates.py", 'f["detail"] = scrub(f["detail"])', "pass"),
 ("common.py", "elif observed_count <= 0 or any(f[\"severity\"] == \"unobserved\" for f in findings):", "elif False:"),
]
base = subprocess.run([sys.executable, "-I", "-B", "-m", "unittest", "discover", "-s", "tests"], cwd=src,
                      capture_output=True, text=True)
if base.returncode != 0:  # a sweep over a red baseline proves nothing
    print("BASELINE RED: fix the suite before sweeping")
    sys.exit(2)
print("baseline: %s" % base.stderr.strip().split("\n")[-3])
bad = 0
for i, (fn, old, new) in enumerate(MUT):
    d = tempfile.mkdtemp(prefix="vp-gates-mut-")
    dst = os.path.join(d, "gates")
    shutil.copytree(os.path.dirname(src), d, ignore=shutil.ignore_patterns("__pycache__", "patches"), dirs_exist_ok=True)
    p = os.path.join(dst, fn)
    s = open(p).read()
    assert s.count(old) == 1, (fn, old[:40], s.count(old))
    open(p, "w").write(s.replace(old, new))
    r = subprocess.run([sys.executable, "-I", "-m", "unittest", "discover", "-s", "tests"], cwd=dst, capture_output=True, text=True)
    tail = r.stderr.strip().split("\n")[-1]
    killed = r.returncode != 0
    bad += not killed
    print("M%02d %-16s %-8s %s | %s" % (i + 1, fn, "KILLED" if killed else "SURVIVED", tail, old[:50].replace("\n", " ")))
    shutil.rmtree(d, True)
print("mutants=%d killed=%d survived=%d" % (len(MUT), len(MUT) - bad, bad))
sys.exit(1 if bad else 0)
