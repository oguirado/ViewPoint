#!/usr/bin/env python3
"""OS driver: runs the pinned hbtools_v2 (vision-engine 681dc0b) over Go through its language-neutral --symbols-bundle path (hbtools_v2 is never edited).

  hb_os_driver.py extract [--out DIR]     goextract bundle (validated) -> DIR/bundle.json
  hb_os_driver.py build                   hb_build.build_all(symbols_bundle=..., hb_id=HB_ID)
  hb_os_driver.py index                   hb_index.build_index(symbols_bundle=...)
  hb_os_driver.py gates [--out FILE]      live hbtools_v2 gates + OS gates (g_os_*); every skip listed with its replacement
  hb_os_driver.py scaffold-before --out DIR   skeleton book (no prose) for the 'before' measurement

Env (defaults in hb_os_gates.sh): HBV2_ROOT HBV2_SUBJECT HBV2_SRC_DIRS HBV2_BOOK_SUB HBV2_BENCH, OS_GOEXTRACT (prebuilt binary).
Exit codes: 0 ok, 2 usage, 3 book-link issue, 5 gate failure. Never 1.
"""
import base64, hashlib, json, os, subprocess, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
HBT = os.path.join(os.path.dirname(HERE), "hbtools_v2")
sys.dont_write_bytecode = True
sys.path.insert(0, HBT)
sys.path.insert(0, HERE)
os.environ["PATH"] = os.environ.get("PATH", "") + ":/usr/local/go/bin"
os.environ.setdefault("HBV2_BOOK_SUB", "HB-OS-cubook")
os.environ.setdefault("HBV2_SRC_DIRS", "src/cubook,cmd/srf-cu-exec,cmd/srf-cu-mcp,cmd/srf-cu-snap,cmd/srf-cu-wlcap")

import hblib2 as L  # noqa: E402
import hb_build as B  # noqa: E402

# pins: HBV2_SYMBOLS_BUNDLE (set by bundle_path) selects the bundle for build, index and the live gates of hb_gates2
HB_ID = "HB-OSCUBOOK-001"
GOEXTRACT_SRC = os.path.join(HERE, "goextract")
_BIN = {}
_BUNDLE = {}

# gates run live over the OS book; every other gate is skipped on purpose and mapped to an OS replacement (AW5-05)
LIVE = ["g_ids", "g_backlink", "g_budget", "g_subanchor", "g_determinism", "g_nofloat"]
SKIPPED = {
    "g_schema": ("loads standards/schemas + tools/ literals of the Python subject", "g_os_schema"),
    "g_graph": ("graph edges are Python CALLS/IMPORTS; goextract emits none", "g_os_anchor"),
    "g_fresh": ("compares Python tree against the stored extraction", "g_os_drift"),
    "g_anchor": ("filters path.startswith(tools/,tests/): vacuous for Go", "g_os_anchor"),
    "g_drift": ("hb_drift2 classifies Python anchors only", "g_os_drift"),
    "g_relocate": ("relocation by Python AST", "g_os_anchor"),
    "g_claim": ("rank gate filters tools/ literal", "g_os_floor"),
    "g_oracle": ("oracle witness runs Python mutants", "g_os_floor"),
    "g_merge": ("merge driver fixture of the Python subject", "g_os_drift"),
    "g_visual": ("Godot visual profile absent in the OS copy", "g_os_floor"),
    "g_rebuild": ("rebuilds with Python extractor", "g_os_rebuild"),
    "g_blind": ("reads router bench layout", "g_os_floor"),
    "g_privacy": ("needs v1_frozen scanner absent in OS", "g_os_privacy"),
    "g_stdlib": ("flags the optional hb_visual import of the pinned copy (PATCHES.md kind 4)", "g_os_stdlib"),
    "g_freeze": ("hb_freeze hardcodes book/ and bench/ (AW5-03: run per snapshot by the conductor)", "g_os_floor"),
}


def subject_dirs():
    return [d for d in os.environ["HBV2_SRC_DIRS"].split(",") if d]


def goextract_bin():
    if os.environ.get("OS_GOEXTRACT"):
        return os.environ["OS_GOEXTRACT"]
    if "bin" not in _BIN:
        d = tempfile.mkdtemp(prefix="goextract-bin-", dir=os.environ.get("TMPDIR") or "/tmp")
        out = os.path.join(d, "goextract")
        mod = subprocess.run(["go", "env", "GOMOD"], cwd=GOEXTRACT_SRC, capture_output=True, text=True).stdout.strip()
        r = subprocess.run(["go", "build", "-p", "5", "-o", out, "."], cwd=GOEXTRACT_SRC, capture_output=True, text=True, timeout=300)
        if r.returncode != 0:
            raise RuntimeError("goextract build failed: " + r.stderr[:300] + mod)
        _BIN["bin"] = out
    return _BIN["bin"]


def bundle(subject, dirs=None):
    key = (os.path.abspath(subject), tuple(dirs or subject_dirs()))
    if key not in _BUNDLE:
        r = subprocess.run([goextract_bin(), "--root", subject, "--dirs", ",".join(key[1]), "--hb-id", HB_ID], capture_output=True, timeout=300)
        if r.returncode != 0:
            raise RuntimeError("goextract rc %d: %s" % (r.returncode, r.stderr.decode()[:300]))
        b = json.loads(r.stdout)
        # source_b64 is optional in hb.symbols.bundle/v1; with it the pin computes per-file lines/bytes (otherwise s8 shows AUSENTE)
        for f in b["files"]:
            f["source_b64"] = base64.b64encode(L.readb(os.path.join(subject, f["path"]))).decode("ascii")
        _BUNDLE[key] = b
    return _BUNDLE[key]


# ---- bundle plumbing: the pinned hbtools_v2 (681dc0b) consumes the goextract bundle through HBV2_SYMBOLS_BUNDLE / --symbols-bundle ----
def bundle_path():
    """Path of the goextract bundle for this process: env HBV2_SYMBOLS_BUNDLE when set, else a deterministic bundle written to a temp dir."""
    p = os.environ.get("HBV2_SYMBOLS_BUNDLE")
    if not p:
        d = tempfile.mkdtemp(prefix="hb-os-bundle-", dir=os.environ.get("TMPDIR") or "/tmp")
        p = os.path.join(d, "bundle.json")
        L.write_json(p, bundle(L.SUBJECT))
        os.environ["HBV2_SYMBOLS_BUNDLE"] = p
    return p


def validate_bundle(b):
    """Validate against hbtools_v2/schema/hb.symbols.bundle.v1.json plus the pin's cross-reference checks."""
    import hb_lib_bundle as SB
    return SB.validate(b)


def install():
    """No-op kept for callers that still call D.install() (eval/hb_os/measure.sh). The 681dc0b pin needs no monkeypatch for Go."""


def cmd_extract(a):
    out = a.get("--out") or os.path.join(L.HB, "out")
    b = validate_bundle(bundle(L.SUBJECT))
    os.makedirs(out, exist_ok=True)
    L.write_json(os.path.join(out, "bundle.json"), b)
    print(L.dumps({"symbols": len(b["symbols"]), "files": len(b["files"])}))
    return 0


def cmd_build(a):
    path = bundle_path()
    validate_bundle(L.jload(path))
    try:
        ctx, d = B.build_all(L.HB, symbols_bundle=path, hb_id=HB_ID)
    except B.BookError as e:
        sys.stderr.write(str(e) + "\n")
        return 3
    for i in d["issues"]:
        sys.stderr.write("%s %s\n" % (i["code"], i["detail"]))
    print(L.dumps({"symbols": len(d["symbols"]), "nodes": len(d["nodes"]), "issues": len(d["issues"])}))
    return 3 if d["issues"] else 0


def cmd_index(a):
    import hb_index as I
    print(I.build_index(L.HB, symbols_bundle=bundle_path()))
    return 0


def bench_rows():
    p = os.path.join(os.path.abspath(os.environ.get("HBV2_BENCH") or os.path.join(L.HB, "bench")), "hb_dev.jsonl")
    return [json.loads(l) for l in open(p, encoding="utf-8") if l.strip()] if os.path.isfile(p) else []


def cmd_gates(a):
    import hb_gates2 as G
    import hb_os_checks as C
    bundle_path()
    G.dev_rows = lambda root: bench_rows()
    results = []
    nq = len(bench_rows())
    results.append(G.res("g_bench_rows", [] if nq >= 17 else ["dev queries %d < 17: live budget/determinism gates would be vacuous" % nq], queries=nq))
    for g in LIVE:
        try:
            r = getattr(G, g)(L.HB)
        except Exception as e:  # fail closed
            r = G.res(g, ["gate crashed: %s: %s" % (type(e).__name__, str(e)[:160])])
        results.append(r)
    results.append(C.live_nonvacuous(L.HB, results))
    results += C.run_os_gates(L.HB, L.SUBJECT, bundle(L.SUBJECT), os.environ["HBV2_BOOK_SUB"])
    doc = {"schema": "hb.receipt.v2", "kind": "gate", "tool": "hb_os_driver.py/1", "inputs": {}, "gates": results,
           "skipped": [{"gate": k, "reason": v[0], "replacement": v[1]} for k, v in sorted(SKIPPED.items())],
           "metrics": {"gates": len(results), "failed": sum(1 for r in results if not r["pass"]), "skipped": len(SKIPPED)}}
    bad = sorted({v[1] for v in SKIPPED.values()} - {r["gate"] for r in results})
    if bad:
        doc["metrics"]["failed"] += 1
        results.append(G.res("g_skip_mapping", ["skipped gate replacement not run: " + ",".join(bad)]))
    out = a.get("--out") or os.path.join(L.HB, "out", "gates.json")
    L.write_json(out, doc)
    for r in results:
        sys.stderr.write("%s %s\n" % (r["gate"], "PASS" if r["pass"] else "FAIL " + "; ".join(r["errors"][:2])))
    print(L.dumps(doc["metrics"]))
    return 5 if doc["metrics"]["failed"] else 0


def cmd_scaffold_before(a):
    out = a.get("--out")
    if not out:
        return 2
    b = bundle(L.SUBJECT)
    os.makedirs(out, exist_ok=True)
    lines = ["# HB-OS-cubook (before skeleton)\n\nSkeleton: one section per file, no prose, no invariants.\n"]
    for n in range(11):
        lines.append('\n## <a id="hb-s%d"></a><a id="pm-s%d"></a>%d. Section\n\n' % (n, n, n))
        if n == 5:
            for i, f in enumerate(b["files"]):
                sec = {"anchor": "hb-s5.f%d" % i, "claims": [], "code_anchors": [], "covers": f["path"], "diataxis": "reference", "hb_id": HB_ID,
                       "kind": "file", "n": 5, "purpose": os.path.basename(f["path"]), "route": {"emit": True, "tags": []},
                       "schema": "hb.section.v2", "title": os.path.basename(f["path"])}
                lines.append('<!-- HB:BEGIN hb.section.v2 -->\n```json\n%s\n```\n<!-- HB:END -->\n' % L.dumps(sec))
        elif n >= 8:
            lines.append("<!-- HB:GEN BEGIN hb-s%d -->\n<!-- HB:GEN END hb-s%d -->\n" % (n, n))
    with open(os.path.join(out, "book.md"), "w", encoding="utf-8", newline="\n") as f:
        f.write("".join(lines))
    print(L.dumps({"files": len(b["files"])}))
    return 0


CMDS = {"extract": cmd_extract, "build": cmd_build, "index": cmd_index, "gates": cmd_gates, "scaffold-before": cmd_scaffold_before}


def main(argv):
    if not argv or argv[0] not in CMDS or len(argv) % 2 == 0:
        sys.stderr.write(__doc__)
        return 2
    a = dict(zip(argv[1::2], argv[2::2]))
    install()
    try:
        return CMDS[argv[0]](a)
    except Exception as e:  # fail closed, never rc 1
        sys.stderr.write("hb_os_driver: %s: %s\n" % (type(e).__name__, e))
        return 5


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
