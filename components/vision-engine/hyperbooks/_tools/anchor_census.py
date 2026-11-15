# HB-VC runner for R-s6-anchors / R-s7-vmem-inventory (tracked; JDOCS-2)
import glob, json, os, re, sys
root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(root)
out = {}
for pat in sys.argv[1:]:
    for p in sorted(glob.glob(pat)):
        for i, line in enumerate(open(p, encoding="utf-8"), 1):
            m = re.search(r"@anchor:(\S+)", line)
            if m:
                out[m.group(1)] = [p, i]
print(json.dumps({"anchors": out, "n": len(out), "ingest_py": os.path.exists("vmem/ingest.py"),
                  "mutants_vmem_dir": os.path.isdir("tests/mutants/vmem")}, sort_keys=True))
