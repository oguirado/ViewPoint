#!/usr/bin/env python3
"""Build or check the blender-visual-qa route index (stdlib, deterministic, no clock).

  python3 -B route/build.py          rebuild route/index/*.jsonl and route/index/manifest.json
  python3 -B route/build.py --check  rebuild in memory and compare bytes: rc 0 ROUTE_OK, rc 4 STALE

Sources are the tracked files of this component (git ls-files), or every file under the
component when it is not inside a Git work tree. Rows are coordinates, not proof.
Row types: sym (Python def/class), doc (Markdown section), schema (JSON schema), test (test file).
Every sym row carries env: bpy (runs inside Blender), engine (touches the pinned vision engine),
blender (host code that names Blender), generic (no Blender coupling found lexically).
"""
import ast
import hashlib
import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
INDEX = os.path.join(HERE, "index")
SCHEMA = "bvqa.route-index/v2"
OUTPUTS = ("docs.jsonl", "symbols.jsonl", "manifest.json")
SKIP_PREFIX = ("route/index/", "route/bench/", "tests/router_bench/", "tests/golden/", "examples/golden/", ".wavemind/")
BODY_CAP = 160
ANCHOR_RE = re.compile(r"#\s*@anchor:([A-Za-z0-9_.]+)")
HTML_ID_RE = re.compile(r'<a id="([^"]+)"></a>')
TAG_RE = re.compile(r"<[^>]+>")
HEAD_RE = re.compile(r"^(#{1,3})\s+(.*?)\s*#*\s*$")
WORD_RE = re.compile(r"[A-Za-z][a-z]+|[A-Z]+(?![a-z])|[0-9]+")
BLENDER_WORDS = frozenset(("blender", "bpy", "bmesh", "mathutils", "cycles", "eevee", "workbench", "blend"))
ENGINE_WORDS = frozenset(("visioncore", "vadapt", "vmem"))


def canon(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def words(text):
    """Identifier-aware lowercase words: snake_case and camelCase parts plus the whole lowercased word."""
    out = []
    for part in re.split(r"[^A-Za-z0-9]+", text):
        if not part:
            continue
        pieces = [w.lower() for w in WORD_RE.findall(part)]
        out.extend(pieces)
        if len(pieces) > 1:
            out.append(part.lower())
    return out


def tracked(root):
    try:
        res = subprocess.run(["git", "-C", root, "ls-files", "-z", "--", "."], capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        res = None
    if res is not None and res.returncode == 0 and res.stdout:
        names = [n for n in res.stdout.decode("utf-8").split("\0") if n]
    else:
        names = []
        for base, dirs, files in os.walk(root):
            dirs[:] = sorted(d for d in dirs if d not in (".git", "__pycache__", "build"))
            for f in files:
                names.append(os.path.relpath(os.path.join(base, f), root).replace(os.sep, "/"))
    return sorted(n for n in names if not n.startswith(SKIP_PREFIX) and "__pycache__" not in n)


def module_id(path):
    """Dotted id compatible with the v1 router names (bv., bpy., tools.)."""
    stem = path[:-3]
    parts = stem.split("/")
    if parts[-1] == "__init__":
        parts = parts[:-1]
    head = {"blender_visual_qa": "bv", "bpy_side": "bpy"}.get(parts[0], parts[0])
    return ".".join([head] + parts[1:])


def env_of(path, body_words):
    if path.startswith("bpy_side/"):
        return "bpy"
    bag = set(body_words)
    if bag & ENGINE_WORDS:
        return "engine"
    if bag & BLENDER_WORDS:
        return "blender"
    return "generic"


def first_line(doc):
    if not doc:
        return ""
    line = doc.strip().splitlines()[0].strip()
    return line[:200]


def py_rows(path, text):
    try:
        tree = ast.parse(text, filename=path)
    except (SyntaxError, ValueError):
        return [], "syntax"
    lines = text.splitlines()
    mod = module_id(path)
    rows = []

    def anchors_above(lineno):
        found = []
        for i in range(max(0, lineno - 4), lineno - 1):
            m = ANCHOR_RE.search(lines[i])
            if m:
                found.append(m.group(1))
        return found

    def body_words(node):
        ws = []
        for sub in ast.walk(node):
            if isinstance(sub, ast.Name):
                ws.extend(words(sub.id))
            elif isinstance(sub, ast.Attribute):
                ws.extend(words(sub.attr))
            elif isinstance(sub, ast.Constant) and isinstance(sub.value, str) and len(sub.value) <= 200:
                ws.extend(words(sub.value))
        return ws

    def literals(node):
        out = set()
        for sub in ast.walk(node):
            if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                for m in re.findall(r"\bE_[A-Z0-9_]{2,40}\b", sub.value):
                    out.add(m)
        return sorted(out)

    def add(node, qual, kind):
        doc = ast.get_docstring(node) or ""
        bw = body_words(node)
        start = node.lineno
        if getattr(node, "decorator_list", None):
            start = min([start] + [d.lineno for d in node.decorator_list])
        name_words = words(node.name)
        rows.append({
            "anchors": anchors_above(start),
            "doc": first_line(doc),
            "end": getattr(node, "end_lineno", node.lineno),
            "env": env_of(path, bw + words(doc)),
            "errors": literals(node),
            "id": mod + "." + qual,
            "kind": kind,
            "line": node.lineno,
            "path": path,
            "t": {"name": name_words, "doc": words(doc)[:BODY_CAP],
                  "body": [w for w in bw if len(w) > 2][:BODY_CAP]},
            "type": "sym",
        })

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            add(node, node.name, "function")
        elif isinstance(node, ast.ClassDef):
            add(node, node.name, "class")
            for sub in node.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    add(sub, node.name + "." + sub.name, "method")
    return rows, None


def slug(text):
    t = TAG_RE.sub("", text).strip().lower().replace(" ", "-")
    return re.sub(r"[^a-z0-9-]", "", t)


def md_rows(path, text):
    lines = text.splitlines()
    heads = []
    fence = False
    for i, line in enumerate(lines):
        if line.lstrip().startswith("```"):
            fence = not fence
            continue
        m = None if fence else HEAD_RE.match(line)
        if m:
            heads.append((i, len(m.group(1)), m.group(2)))
    rows = []
    stem = os.path.splitext(os.path.basename(path))[0]
    if not heads or heads[0][0] > 0:
        heads.insert(0, (0, 0, ""))
    for n, (i, depth, title) in enumerate(heads):
        end = heads[n + 1][0] if n + 1 < len(heads) else len(lines)
        body = "\n".join(lines[i + 1:end])
        clean = TAG_RE.sub("", title).strip()
        aliases = ["hb:%s#%s" % (stem, a) for a in HTML_ID_RE.findall(title)] if path.startswith("hyperbooks/") else []
        rows.append({
            "anchors": aliases,
            "doc": clean[:200] or os.path.basename(path),
            "end": end,
            "id": "doc:%s#%s" % (path, slug(title) if depth else ""),
            "kind": "h%d" % depth if depth else "lead",
            "line": i + 1,
            "path": path,
            "t": {"name": words(clean) + words(stem), "doc": words(body)[:BODY_CAP * 2], "body": []},
            "type": "doc",
        })
    return rows


def schema_rows(path, text):
    try:
        data = json.loads(text)
    except ValueError:
        return []
    if not isinstance(data, dict):
        return []
    props = sorted(data.get("properties", {}).keys()) if isinstance(data.get("properties"), dict) else []
    desc = " ".join(str(data.get(k, "")) for k in ("title", "description"))
    return [{
        "anchors": [str(data["$id"])] if data.get("$id") else [],
        "doc": (str(data.get("$id") or os.path.basename(path)))[:200],
        "end": len(text.splitlines()),
        "id": "schema:" + path,
        "kind": "schema",
        "line": 1,
        "path": path,
        "t": {"name": words(os.path.basename(path).split(".")[0]) + ["schema"],
              "doc": words(desc) + [w for p in props for w in words(p)], "body": []},
        "type": "schema",
    }]


def test_row(path, text, symbols_by_name):
    """A test file row: its docstring, test names and the code names it mentions."""
    try:
        tree = ast.parse(text, filename=path)
        doc = ast.get_docstring(tree) or ""
    except (SyntaxError, ValueError):
        tree, doc = None, ""
    names = []
    if tree is not None:
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test"):
                names.extend(words(node.name[4:]))
    refs = sorted({n for n in re.findall(r"[A-Za-z_][A-Za-z0-9_]{3,}", text) if n in symbols_by_name})
    return {
        "anchors": [],
        "doc": first_line(doc) or os.path.basename(path),
        "end": len(text.splitlines()),
        "id": "test:" + path,
        "kind": "test",
        "line": 1,
        "path": path,
        "refs": refs[:80],
        "t": {"name": words(os.path.basename(path)[:-3]) + ["test", "tests"], "doc": words(doc)[:BODY_CAP] + names[:BODY_CAP],
              "body": [w for r in refs[:80] for w in words(r)]},
        "type": "test",
    }


def build(root=ROOT):
    files = tracked(root)
    manifest_files, sym, docs, problems = [], [], [], []
    pending_tests = []
    for rel in files:
        full = os.path.join(root, rel)
        if os.path.islink(full) or not os.path.isfile(full):
            continue
        data = open(full, "rb").read()
        manifest_files.append([rel, hashlib.sha256(data).hexdigest()])
        if rel.endswith((".py", ".md", ".json")):
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                problems.append([rel, "utf8"])
                continue
        else:
            continue
        if rel.endswith(".py"):
            rows, err = py_rows(rel, text)
            if err:
                problems.append([rel, err])
            sym.extend(rows)
            base = os.path.basename(rel)
            if rel.startswith("tests/") and base.startswith("test"):
                pending_tests.append((rel, text))
        elif rel.endswith(".md"):
            docs.extend(md_rows(rel, text))
        elif rel.endswith(".schema.json"):
            docs.extend(schema_rows(rel, text))
    by_name = {}
    for r in sym:
        if not r["path"].startswith("tests/"):
            by_name.setdefault(r["id"].rsplit(".", 1)[-1], []).append(r["id"])
    for rel, text in pending_tests:
        docs.append(test_row(rel, text, by_name))
    sym.sort(key=lambda r: (r["path"], r["line"], r["id"]))
    docs.sort(key=lambda r: (r["path"], r["line"], r["id"]))
    out = {
        "symbols.jsonl": "".join(canon(r) + "\n" for r in sym),
        "docs.jsonl": "".join(canon(r) + "\n" for r in docs),
    }
    env_counts = {}
    for r in sym:
        env_counts[r["env"]] = env_counts.get(r["env"], 0) + 1
    manifest = {
        "counts": {"docs": sum(1 for r in docs if r["type"] == "doc"), "schemas": sum(1 for r in docs if r["type"] == "schema"),
                   "symbols": len(sym), "tests": sum(1 for r in docs if r["type"] == "test"), "env": env_counts},
        "files": manifest_files,
        "outputs": {k: hashlib.sha256(v.encode("utf-8")).hexdigest() for k, v in sorted(out.items())},
        "problems": problems,
        "schema": SCHEMA,
    }
    out["manifest.json"] = json.dumps(manifest, sort_keys=True, indent=1, ensure_ascii=False) + "\n"
    return out


def check(root=ROOT):
    want = build(root)
    stale = []
    for name in OUTPUTS:
        path = os.path.join(root, "route", "index", name)
        try:
            have = open(path, encoding="utf-8").read()
        except OSError:
            have = None
        if have != want[name]:
            stale.append(name)
    return stale


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if args not in ([], ["--check"]):
        print(canon({"code": "E_USAGE", "rc": 2, "usage": "build.py [--check]"}))
        return 2
    if args == ["--check"]:
        stale = check()
        if stale:
            print(canon({"code": "E_STALE", "rc": 4, "stale": stale, "fix": "python3 -B route/build.py"}))
            return 4
        print("ROUTE_OK")
        return 0
    out = build()
    os.makedirs(INDEX, exist_ok=True)
    for name in OUTPUTS:
        tmp = os.path.join(INDEX, "." + name + ".tmp")
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(out[name])
        os.replace(tmp, os.path.join(INDEX, name))
    m = json.loads(out["manifest.json"])
    print(canon({"code": "OK", "counts": m["counts"], "problems": len(m["problems"]), "rc": 0}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
