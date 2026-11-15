#!/usr/bin/env python3
"""HB2-09 merge fixture (driven by tests/fixtures_merge.sh): temp git clone of subject tools/tests + Godot fixture + book.

Branches from `base`: W1 and W2 edit two far-apart functions of tools/route_v2.py, W3 edits the scene file, W4 edits one prose section of
book.md; W1-W3 also commit regenerated derived files (a careless but allowed worker), never the generated fences. W5 (negative) edits the
same lines as W1 and a line inside the hb-s8 fence. All 24 merge orders of W1-W4 are replayed; after each, ONE `hb_build --render-derived`
(+ index) and the derived files are hashed. Prints one JSON document.
"""
import itertools, json, os, shutil, subprocess, sys, tempfile

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hblib2 as L  # noqa: E402
import hb_build as B  # noqa: E402
import hb_index as I  # noqa: E402
import hb_splice as SP  # noqa: E402

HERE = L.HERE
ENV = dict(os.environ, GIT_AUTHOR_NAME="hb", GIT_AUTHOR_EMAIL="hb@example.invalid", GIT_COMMITTER_NAME="hb", GIT_COMMITTER_EMAIL="hb@example.invalid",
           PYTHONDONTWRITEBYTECODE="1", GIT_CONFIG_NOSYSTEM="1")
DERIVED = ["book/book.md", "book/symbols.jsonl", "book/nodes.jsonl", "book/anchors.jsonl", "book/claims.jsonl", "book/graph.json",
           "book/index/cols.json", "book/index/post.json", "book/index/vocab.json", "book/index/ID"]
GITATTR = open(os.path.join(HERE, "gitattributes.template"), encoding="utf-8").read()


def git(cwd, *a, check=True):
    p = subprocess.run(["git"] + list(a), cwd=cwd, env=ENV, capture_output=True, text=True)
    if check and p.returncode != 0:
        raise RuntimeError("git %s: %s" % (a, p.stderr[-300:]))
    return p


def setup(dst, root):
    for sub in ("tools", "tests"):
        os.makedirs(os.path.join(dst, "subject", sub))
        for f in sorted(os.listdir(os.path.join(root, "subject", sub))):
            if f.endswith(".py"):
                shutil.copy2(os.path.join(root, "subject", sub, f), os.path.join(dst, "subject", sub, f))
    shutil.copytree(os.path.join(root, "godot_fixture", "project"), os.path.join(dst, "godot_fixture", "project"))
    os.makedirs(os.path.join(dst, "out"))
    shutil.copy2(os.path.join(root, "out", "trace.json"), os.path.join(dst, "out", "trace.json"))
    shutil.copytree(os.path.join(root, L.BOOK_SUB), os.path.join(dst, L.BOOK_SUB), ignore=shutil.ignore_patterns("seed"))
    with open(os.path.join(dst, ".gitattributes"), "w", newline="\n") as f:
        f.write(GITATTR)
    git(dst, "init", "-q", "-b", "base")
    py = sys.executable
    drv = os.path.join(HERE, "hb_merge_driver.py")
    git(dst, "config", "merge.hb-book.driver", "%s %s book %%O %%A %%B %%P" % (py, drv))
    git(dst, "config", "merge.hb-regen.driver", "%s %s regen %%O %%A %%B %%P" % (py, drv))
    # base = rendered state of the clone
    B.build_all(dst)
    I.build_index(dst)
    git(dst, "add", "-A")
    git(dst, "commit", "-qm", "base")


def edit(dst, rel, fn):
    p = os.path.join(dst, rel)
    t = open(p, encoding="utf-8", newline="").read()
    n = fn(t)
    assert n != t, rel
    open(p, "w", encoding="utf-8", newline="").write(n)


def regen_keep_fences(dst):
    B.build_all(dst)
    I.build_index(dst)
    git(dst, "checkout", "base", "--", "book/book.md")


def mk_branches(dst):
    def w1(t):
        return t.replace('def weights(params, intent):\n    w = dict(PROFILES[params["profile"]])', 'def weights(params, intent):\n    w = dict(PROFILES[params["profile"]])\n    _w1 = 1', 1)

    def w2(t):
        return t.replace('def write_log(logdir, agent, q, ans, R):\n', 'def write_log(logdir, agent, q, ans, R):\n    _w2 = 2\n', 1)

    def w3(t):
        return t.replace("offset_left = 64.0", "offset_left = 66.0", 1)

    def w4(t):
        return t.replace("Limits: the router is lexical", "Limits (reviewed): the router is lexical", 1)

    def w5(t):
        return t.replace('def weights(params, intent):\n    w = dict(PROFILES[params["profile"]])', 'def weights(params, intent):\n    w = dict(PROFILES[params["profile"]])\n    _w5 = 5', 1)

    def w5book(t):
        return t.replace("### Files\n", "### Files (hand edited)\n", 1)
    for name, edits, regen in (("W1", [("subject/tools/route_v2.py", w1)], True), ("W2", [("subject/tools/route_v2.py", w2)], True),
                               ("W3", [("godot_fixture/project/level.tscn", w3)], True), ("W4", [("book/book.md", w4)], False),
                               ("W5", [("subject/tools/route_v2.py", w5), ("book/book.md", w5book)], False)):
        git(dst, "checkout", "-q", "-B", name, "base")
        for rel, fn in edits:
            edit(dst, rel, fn)
        if regen:
            regen_keep_fences(dst)
        git(dst, "add", "-A")
        git(dst, "commit", "-qm", name)
    git(dst, "checkout", "-q", "base")


def hash_derived(dst):
    return {f: L.sha_file(os.path.join(dst, f)) for f in DERIVED}


def run(root, orders=None):
    tmp = tempfile.mkdtemp(prefix="hbmerge")
    dst = os.path.join(tmp, "clone")
    try:
        setup(dst, root)
        mk_branches(dst)
        perms = list(itertools.permutations(["W1", "W2", "W3", "W4"]))
        if orders:
            perms = perms[:orders]
        results = {}
        sources = {}
        for perm in perms:
            git(dst, "checkout", "-q", "-B", "m", "base")
            for b in perm:
                p = git(dst, "merge", "--no-edit", "-q", b, check=False)
                if p.returncode != 0:
                    raise RuntimeError("merge %s failed in order %s: %s" % (b, perm, (p.stdout + p.stderr)[-300:]))
            B.build_all(dst)
            I.build_index(dst)
            results["".join(perm)] = hash_derived(dst)
            sources["".join(perm)] = L.sha256b(L.readb(os.path.join(dst, "subject/tools/route_v2.py")) + L.readb(os.path.join(dst, "godot_fixture/project/level.tscn")))
            git(dst, "reset", "-q", "--hard", "base")
            git(dst, "clean", "-fdq")
        uniq = {f: len({r[f] for r in results.values()}) for f in DERIVED}
        # negative branch W5
        git(dst, "checkout", "-q", "-B", "neg", "base")
        git(dst, "merge", "--no-edit", "-q", "W1", check=False)
        git(dst, "merge", "--no-edit", "-q", "W4", check=False)
        p5 = git(dst, "merge", "--no-edit", "W5", check=False)
        out5 = p5.stdout + p5.stderr
        base_src = git(dst, "show", "base:subject/tools/route_v2.py").stdout.encode()
        vers = {b: git(dst, "show", b + ":subject/tools/route_v2.py").stdout.encode() for b in ("W1", "W5")}
        ov = SP.branches_overlap(base_src, vers)
        conflict_files = sorted(git(dst, "diff", "--name-only", "--diff-filter=U").stdout.split())
        git(dst, "merge", "--abort", check=False)
        neg = {"merge_rc": p5.returncode, "E_OVERLAP": ("W1", "W5") in ov, "E_PIN": "E_PIN" in out5, "conflict_files": conflict_files}
        # plain git (no drivers): how many of the 24 orders would conflict on derived files without hb-regen/hb-book
        return {"schema": "hb.merge-fixture.v2", "orders": len(perms), "unique_per_file": uniq, "all_equal": all(v == 1 for v in uniq.values()),
                "derived_sha256": results[next(iter(results))], "sources_unique": len(set(sources.values())), "negative": neg}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main(argv=None):
    root = argv[0] if argv else L.HB
    orders = int(argv[1]) if argv and len(argv) > 1 else None
    r = run(root, orders)
    sys.stdout.write(L.dumps(r) + "\n")
    return 0 if r["all_equal"] and r["negative"]["E_OVERLAP"] and r["negative"]["E_PIN"] else 5


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
