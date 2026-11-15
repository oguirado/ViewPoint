"""Synthetic fixture: a tiny Go subject plus a hand-written OS book, built through the real driver (tmp copy, no repo state)."""
import json, os, shutil, subprocess, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
OSDIR = os.path.dirname(HERE)
DRIVER = os.path.join(OSDIR, "hb_os_driver.py")
BOOK = "HB-OS-cubook"
GO_SRC = '''package demo

// Add sums two ints.
func Add(a, b int) int {
	return a + b
}

// Sub subtracts b from a.
func Sub(a, b int) int {
	return a - b
}
'''
P3 = "".join("# %s: x\n" % k for k in ("INPUTS", "PRECONDITIONS", "OUTPUTS", "TIME", "SPACE", "ARITHMETIC", "FAILURE"))
SID_ADD, SID_SUB = "src/cubook/demo/demo.go::Add", "src/cubook/demo/demo.go::Sub"


def block(kind, o):
    return "<!-- HB:BEGIN %s -->\n```json\n%s\n```\n<!-- HB:END -->\n" % (kind, json.dumps(o, sort_keys=True, separators=(",", ":")))


def book_text():
    sec = {"schema": "hb.section.v2", "hb_id": "HB-OSCUBOOK-001", "n": 0, "anchor": "hb-s0", "kind": "reading", "diataxis": "reference", "title": "Demo adder",
           "purpose": "Adds and subtracts.", "code_anchors": [SID_ADD, SID_SUB], "claims": [], "route": {"emit": True, "tags": []}}
    inv = {"module": "src/cubook/demo/demo.go",
           "inv": [{"id": "INV-DEMO-ADD", "text": "Add is plain integer addition.", "symbols": [SID_ADD]},
                   {"id": "INV-DEMO-SUB", "text": "Sub is plain integer subtraction.", "symbols": [SID_SUB]}],
           "au": [{"id": "AU-DEMO-OVERFLOW", "text": "Do not rely on overflow detection.", "symbols": [SID_ADD]}]}
    claim = {"schema": "hb.claim.v2", "id": "C-DEMO-CITED", "kind": "behaviour", "statement": "Add adds two ints.", "anchors": [SID_ADD], "etag": "CITED", "receipts": [],
             "citation": "fixture"}
    search = {"questions_answered": ["how to add"], "engineering_tasks": ["sum"], "synonyms": ["plus"], "exclusions": ["floats"],
              "keywords": ["add"], "retrieval_triggers": ["adder"]}
    out = ["# HB-OS-cubook fixture\n\nProse reference src/cubook/demo/demo.go:4-6 ok.\n"]
    for n in range(11):
        out.append('\n## <a id="hb-s%d"></a><a id="pm-s%d"></a>%d. Section\n\n' % (n, n, n))
        if n == 0:
            out.append(block("hb.section.v2", sec))
            out.append(block("hb.claim.v2", claim))
            out.append("<!-- PM:BEGIN pm.search.v1 -->\n```json\n%s\n```\n<!-- PM:END pm.search.v1 -->\n" % json.dumps(search, sort_keys=True))
        if n == 4:
            out.append("```\n# PM-P3-HEADER: %s\n%s```\n" % (SID_ADD, P3))
        if n == 5:
            out.append(block("hb.inv.v2", inv))
        if n >= 8:
            out.append("<!-- HB:GEN BEGIN hb-s%d -->\n<!-- HB:GEN END hb-s%d -->\n" % (n, n))
    return "".join(out)


def bench_rows():
    rows = []
    for i in range(17):
        sid = SID_ADD if i % 2 == 0 else SID_SUB
        rows.append({"qid": "Q%02d" % i, "split": "DEV", "class": "FIND", "text": "query number %d adder" % i, "intent": "find_code", "gold": [sid],
                     "gold_tests": [], "receipt": "", "verdict": "", "source_event": "fixture", "notes": ""})
    return rows


class Fixture:
    def __init__(self):
        self.tmp = tempfile.mkdtemp(prefix="hbosfix-", dir="/tmp/os-cu-0-w5t" if os.path.isdir("/tmp/os-cu-0-w5t") else None)
        self.root = os.path.join(self.tmp, "hyperbooks")
        self.subject = self.tmp
        d = os.path.join(self.tmp, "src", "cubook", "demo")
        os.makedirs(d)
        self.write_go(GO_SRC)
        os.makedirs(os.path.join(self.root, BOOK, "receipts"))
        os.makedirs(os.path.join(self.tmp, "bench"))
        with open(os.path.join(self.tmp, "bench", "hb_dev.jsonl"), "w") as f:
            for r in bench_rows():
                f.write(json.dumps(r) + "\n")
        self.write_book(book_text())
        ve = os.environ.get("VE_REPO", "")  # W3b CI-in-VM: another checkout of the peer, with history
        if not (os.path.isabs(ve) and os.path.isdir(ve)):
            ve = "/home/user/Documentos/vision-engine"
        with open(os.path.join(self.root, "params.json"), "wb") as f:
            f.write(subprocess.run(["git", "-C", ve, "show", "8120aab:params.json"], capture_output=True, check=True).stdout)
        self.env = dict(os.environ, HBV2_ROOT=self.root, HBV2_SUBJECT=self.subject, HBV2_SRC_DIRS="src/cubook", HBV2_BOOK_SUB=BOOK,
                        HBV2_BENCH=os.path.join(self.tmp, "bench"), PYTHONDONTWRITEBYTECODE="1")

    def go_path(self):
        return os.path.join(self.tmp, "src", "cubook", "demo", "demo.go")

    def book_path(self):
        return os.path.join(self.root, BOOK, "book.md")

    def write_go(self, s):
        with open(self.go_path(), "w") as f:
            f.write(s)

    def write_book(self, s):
        with open(self.book_path(), "w") as f:
            f.write(s)

    def drv(self, *args):
        r = subprocess.run([sys.executable, "-B", DRIVER] + list(args), env=self.env, capture_output=True, text=True, timeout=300)
        return r.returncode, r.stdout + r.stderr

    def build(self):
        rc, out = self.drv("build")
        assert rc == 0, out
        rc, out = self.drv("index")
        assert rc == 0, out

    def gates(self):
        return self.drv("gates", "--out", os.path.join(self.tmp, "gates.json"))

    def cleanup(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
