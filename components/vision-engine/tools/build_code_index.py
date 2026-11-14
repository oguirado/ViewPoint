#!/usr/bin/env python3
"""Deterministic source/HyperBook code index and read-only public-anchor audit."""
# @module {"book":"HB-VC-a0-arch","stage":"TOOLS","owner":"ARCH"}
import argparse
import ast
from collections import Counter
from decimal import Decimal, localcontext, ROUND_FLOOR
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tokenize
import unicodedata

BUILDER = "build_code_index.py/1.0"
CAP = 393216
PKG_DIRS = PUBLIC_CHECK_PKGS = ("visioncore", "vmem", "vadapt", "tools", "vnative")
ZONES = ["anchor", "symbol", "doc", "hb"]
BPPM = dict(zip(ZONES, (300000, 300000, 750000, 750000)))
BOOK_STAGE = {"HB-VC-a0-arch": "a0-arch", "HB-VC-s0-grid": "S0", "HB-VC-s1-desc": "S1",
              "HB-VC-s2-change": "S2", "HB-VC-s3-pyramid": "S3", "HB-VC-s4-regions": "S4",
              "HB-VC-s5-gop": "S5", "HB-VC-s6-pack-cite": "S6", "HB-VC-s7-vmem": "S7",
              "HB-VN-s0s1": "VN-S0S1"}
STOP = frozenset(("a add an and are between can do does for from how i in instead is it new not of on or the to "
                  "what when where which why with").split())
SUFFIX = ("ations", "ation", "ion", "ings", "ing", "ies", "es", "ed", "s")
DOC_ALIAS = {"fg": "foreground", "screen": "screenshot", "png": "screenshot"}
ANCHOR_RE = re.compile(r"(?:^|\s)# " + r"@anchor:((?:vc|vm|va|vn|tools)(?:\.[A-Za-z0-9_]+){2,})(?:\s+\[HB-[^\]]+\])?\s*$")
MODULE_RE = re.compile(r"^# @module (\{.*\})\s*$")
DEFS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
ROOT = Path(__file__).resolve().parent.parent


# @anchor:va.build_code_index.BuildError [HB-VC-a0-arch]
class BuildError(Exception):
    def __init__(self, code, rc, detail):
        super().__init__(detail)
        self.code, self.rc, self.detail = code, rc, detail


# @anchor:va.build_code_index.canon [HB-VC-a0-arch]
def canon(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"


# @anchor:va.build_code_index.sha [HB-VC-a0-arch]
def sha(raw):
    return hashlib.sha256(raw).hexdigest()


# @anchor:va.build_code_index.stem [HB-VC-a0-arch]
def stem(t):
    for suffix in SUFFIX:
        if len(t) > len(suffix) + 3 and t.endswith(suffix):
            return t[:-len(suffix)]
    return t[:-1] if len(t) > 4 and t.endswith("e") else t


# @anchor:va.build_code_index.toks [HB-VC-a0-arch]
def toks(s):
    return [stem(t) for t in re.findall(r"[a-z0-9]+", unicodedata.normalize("NFKC", s).lower()) if t not in STOP]


# @anchor:va.build_code_index.idf_ppm [HB-VC-a0-arch]
def idf_ppm(n, df):
    with localcontext() as ctx:
        ctx.prec = 50
        return int(((Decimal(2 * n + 2) / Decimal(2 * df + 1)).ln() * 1000000).to_integral_value(rounding=ROUND_FLOOR))


def _safe(root, rel):
    p = Path(rel)
    if p.is_absolute() or ".." in p.parts or "\\" in str(rel):
        raise BuildError("E_SCHEMA", 2, "repository-relative path required")
    full = (root / p).resolve()
    if not full.is_relative_to(root):
        raise BuildError("E_SCHEMA", 2, "path escapes repository")
    return full


def _read(root, rel, sources):
    p = _safe(root, rel)
    raw = p.read_bytes()
    sources[rel] = sha(raw)
    return raw.decode("utf-8")


def _pyfiles(root, paths=None):
    if not root.is_dir():
        raise BuildError("E_SCHEMA", 2, "root directory does not exist")
    result = set()
    for rel in paths if paths is not None else PKG_DIRS:
        p = _safe(root, rel)
        if paths is not None and not p.exists():
            raise BuildError("E_SCHEMA", 2, "scope does not exist")
        candidates = [p] if p.is_file() else sorted(p.rglob("*.py"))
        for f in candidates:
            r = f.relative_to(root).as_posix()
            if f.suffix == ".py" and not {"__pycache__", "tests", "build"}.intersection(Path(r).parts):
                _safe(root, r)
                result.add(r)
    return sorted(result)


def _anchors(src):
    # Tokenize comments so strings containing anchor-like text cannot pass coverage.
    return {t.start[0]: m.group(1) for t in tokenize.generate_tokens(io.StringIO(src).readline)
            if t.type == tokenize.COMMENT and (m := ANCHOR_RE.search(t.string))}


def _near(node, line, lines=None):
    first = min([node.lineno] + [d.lineno for d in getattr(node, "decorator_list", [])])
    end = node.body[0].lineno if isinstance(node, DEFS) and node.body else node.lineno
    if not first - 2 <= line <= end:
        return False
    if lines is not None and line < first:
        return all(not text.strip() or text.lstrip().startswith("#") for text in lines[line - 1:first - 1])
    return True


def _stage(anchor, header=None, book="", layer=None):
    header = header or {}
    if header.get("stage"):
        return header["stage"]
    book = header.get("book") or book
    if book in BOOK_STAGE:
        return "L" + str(layer) if book == "HB-VC-a0-arch" and layer in (0, 1, 4) else BOOK_STAGE[book]
    parts = anchor.split(".")
    if anchor.startswith("hb:"):
        return BOOK_STAGE.get(anchor[3:].split("#")[0], "AUSENTE")
    if parts[0] == "vc" and len(parts) > 1:
        if re.fullmatch(r"s[0-6]", parts[1]):
            return parts[1].upper()
        if parts[1] in ("p", "ids", "layout", "errors", "canon", "frame", "topology"):
            return "L0"
        if parts[1] == "decode":
            return "L1"
        if parts[1] in ("pipeline", "cli", "parallel"):
            return "L4"
    return {"vm": "S7", "va": "ADAPT", "vn": "VN-S0S1", "tools": "TOOLS"}.get(parts[0], "AUSENTE")


def _record(anchor, path, line, symbol="", **kw):
    return dict(anchor=anchor, path=path, line=line, symbol=symbol or anchor.rsplit(".", 1)[-1],
                book="AUSENTE", layer=None, owner=None, stage=_stage(anchor), doc="@anchor:" + anchor,
                callers=[], callees=[], hb_sections=[], **kw)


# @anchor:va.build_code_index.scan_code_and_ast [HB-VC-a0-arch]
def scan_code_and_ast(root, sources):
    root = Path(root).resolve()
    recs, calls = {}, {}
    for rel in _pyfiles(root):
        src = _read(root, rel, sources)
        tree = ast.parse(src)
        lines = src.splitlines()
        headers = [json.loads(m.group(1)) for line in src.splitlines()[:40] if (m := MODULE_RE.match(line))]
        hdr = headers[0] if headers else {}
        nodes = [n for n in ast.walk(tree) if isinstance(n, DEFS + (ast.Assign, ast.AnnAssign))]
        for ln, anchor in _anchors(src).items():
            if anchor in recs:
                raise BuildError("E_ANCHOR", 3, "duplicate anchor: " + anchor)
            near = [n for n in nodes if _near(n, ln, lines)]
            node = min(near, key=lambda n: (abs(n.lineno - ln), n.lineno)) if near else None
            r = _record(anchor, rel, ln)
            r.update(book=hdr.get("book", "AUSENTE"), layer=hdr.get("layer"), owner=hdr.get("owner"), _header=hdr)
            r["stage"] = _stage(anchor, hdr, layer=r["layer"])
            if isinstance(node, DEFS):
                r["symbol"] = node.name
                r["doc"] = (ast.get_docstring(node) or r["doc"]).splitlines()[0][:160]
                calls[anchor] = sorted({c.func.id if isinstance(c.func, ast.Name) else c.func.attr
                                        for c in ast.walk(node) if isinstance(c, ast.Call)
                                        and isinstance(c.func, (ast.Name, ast.Attribute))})
            recs[anchor] = r
    by_name = {}
    for a, r in recs.items():
        by_name.setdefault(r["symbol"], []).append(a)
    for a, names in calls.items():
        for name in names:
            choices = by_name.get(name, [])
            local = [b for b in choices if recs[b]["path"] == recs[a]["path"]]
            choices = local or choices
            if len(choices) == 1:
                recs[a]["callees"].append(choices[0])
    return recs


# @anchor:va.build_code_index.scan_hyperbooks_and_catalogue [HB-VC-a0-arch]
def scan_hyperbooks_and_catalogue(root, recs, sources):
    root = Path(root).resolve()
    texts, thr, fail, sections = {}, {}, {}, []
    def read(rel):
        return _read(root, rel, sources) if _safe(root, rel).is_file() else ""
    def zone(a, z, text):
        texts.setdefault(a, {}).setdefault(z, []).append(text)
    for line in read("catalogue/symbols.jsonl").splitlines():
        r = json.loads(line)
        _safe(root, r["path"])
        a = r["anchor"]
        if a not in recs:
            recs[a] = _record(a, r["path"], r["line"], r["symbol"])
        for key in ("doc", "owner", "layer"):
            if r.get(key) is not None:
                recs[a][key] = r[key]
    for name in ("routes", "thresholds", "failures", "book_sections"):
        for line in read("catalogue/" + name + ".tsv").splitlines():
            if not line.strip():
                continue
            c = line.split("\t")
            if name == "routes":
                zone(c[1], "doc", c[0])
            elif name in ("thresholds", "failures"):
                (thr if name == "thresholds" else fail)[c[0]] = c[3]
                zone(c[3], "hb", " ".join([c[0], c[2], c[4], c[6]]) if name == "thresholds" else c[0] + " " + c[4])
            else:
                _safe(root, c[1])
                a = "hb:" + c[0]
                r = _record(a, c[1], int(c[2]), c[3])
                r.update(doc=(c[3] + " " + c[4]).strip(), book=c[0].split("#")[0])
                recs[a] = r
                section = dict(anchor=c[0], path=c[1], line=int(c[2]), title=c[3], purpose=c[4])
                sections.append(section)
                for code in c[5].split(",") if c[5] else []:
                    if code in recs:
                        recs[code]["hb_sections"].append(c[0])
                        if recs[code]["book"] == "AUSENTE":
                            recs[code]["book"] = r["book"]
                        zone(code, "hb", c[3] + " " + c[4])
                        zone(a, "hb", code)
                if len(c) > 6:
                    zone(a, "hb", c[6].replace("/", " "))
    for name in ("layers.json", "subjects.json"):
        read("catalogue/" + name)
    read("hyperbooks/COMPANION_FORMAT.json")
    books, graphs = {}, []
    coord = {r["path"] + "::" + r["symbol"]: a for a, r in recs.items()}
    hb_root = root / "hyperbooks"
    for book_path in sorted(hb_root.glob("HB-*")) if hb_root.exists() else []:
        if not book_path.is_dir():
            continue
        book = book_path.name
        contents = {}
        for p in sorted(book_path.rglob("*")):
            if p.is_file() and (p.parent == book_path or p.parent.name in ("receipts", "index")):
                rel = p.relative_to(root).as_posix()
                contents[p.relative_to(book_path).as_posix()] = read(rel)
        manifest = json.loads(contents.get("manifest.json", "{}"))
        own = set()
        for line in contents.get("symbols.jsonl", "").splitlines():
            s = json.loads(line)
            if s.get("id") in coord:
                own.add(coord[s["id"]])
        book_sections = [s for s in sections if s["anchor"].startswith(book + "#")]
        for ln, line in enumerate(contents.get("book.md", "").splitlines(), 1):
            if not line.startswith("{"):
                continue
            try:
                s = json.loads(line)
            except ValueError:
                continue
            if s.get("schema") not in ("hb.section.v1", "hb.section.v2"):
                continue
            section_id = book + "#" + s["anchor"]
            if not any(x["anchor"] == section_id for x in book_sections):
                book_sections.append(dict(anchor=section_id, path="hyperbooks/" + book + "/book.md",
                                          line=ln, title=s.get("title", ""), purpose=s.get("purpose", "")))
            a = "hb:" + section_id
            if a not in recs:
                recs[a] = _record(a, "hyperbooks/" + book + "/book.md", ln, s.get("title", ""))
                recs[a].update(book=book, doc=s.get("purpose", "") or s.get("title", ""))
            for target in s.get("code_anchors", []):
                target = coord.get(target, target)
                if target in recs:
                    own.add(target)
                    recs[target]["hb_sections"].append(section_id)
                    zone(target, "hb", s.get("title", "") + " " + s.get("purpose", ""))
        for name in ("nodes.jsonl", "claims.jsonl"):
            for line in contents.get(name, "").splitlines():
                node = json.loads(line)
                targets = [coord.get(x.get("a"), x.get("a")) for x in node.get("anchors", []) if isinstance(x, dict)]
                if node.get("id") in coord:
                    targets.append(coord[node["id"]])
                search = node.get("search", {})
                text = " ".join(str(node.get(k, "")) for k in ("title", "purpose", "statement"))
                text += " " + " ".join(str(v) for k in ("keywords", "triggers", "tasks") for v in search.get(k, []))
                for a in targets:
                    if a in recs:
                        own.add(a)
                        zone(a, "hb", text)
        for a in own:
            if recs[a]["book"] == "AUSENTE":
                recs[a]["book"] = book
        if "graph.json" in contents:
            graphs.append(json.loads(contents["graph.json"]))
        books[book] = dict(stage=BOOK_STAGE.get(book, "AUSENTE"), hb_id=manifest.get("hb_id", "AUSENTE"),
                           profile="hb-hybrid/v1", anchors=sorted(own), companion_files=sorted(contents),
                           receipts=sorted(k[9:] for k in contents if k.startswith("receipts/")),
                           sections=sorted(book_sections, key=lambda x: x["anchor"]))
    for g in graphs:
        if not g.get("etypes") or "CALLS" not in g["etypes"]:
            continue
        ids, fwd, et = g["ids"], g["fwd"], g["etypes"].index("CALLS")
        for i, source in enumerate(ids):
            a = coord.get(source, source)
            if a not in recs:
                continue
            for j in range(fwd["row_ptr"][i], fwd["row_ptr"][i + 1]):
                if fwd["etype"][j] == et:
                    target = ids[fwd["col"][j]]
                    recs[a]["callees"].append(coord.get(target, target))
    for a, r in recs.items():
        for b in set(r["callees"]):
            if b in recs:
                recs[b]["callers"].append(a)
    stages = {}
    for a, r in sorted(recs.items()):
        # Explicit module stage outranks companion ownership.
        r["stage"] = _stage(a, r.pop("_header", {}), book=r["book"], layer=r["layer"])
        for key in ("callees", "callers"):
            r[key] = sorted(set(r[key]))[:16]
        r["hb_sections"] = sorted(set(r["hb_sections"]))
        stages.setdefault(r["stage"], []).append(a)
        zone(a, "doc", r["doc"])
        r["zones"] = {"anchor": toks(a) if not a.startswith("hb:") else [], "symbol": toks(r["symbol"]),
                      "doc": toks(" ".join(texts.get(a, {}).get("doc", []))),
                      "hb": toks(" ".join(texts.get(a, {}).get("hb", [])))}
        for values in r["zones"].values():
            values += [DOC_ALIAS[t] for t in values if t in DOC_ALIAS]
    return {"schema": "vqa.hb-companions/v1", "books": books}, stages, thr, fail


# @anchor:va.build_code_index.check_public_anchors [HB-VC-a0-arch]
def check_public_anchors(root, paths=None, pkgs=None):
    root = Path(root).resolve()
    missing = []
    for rel in _pyfiles(root, paths if paths is not None else pkgs):
        if paths is None and Path(rel).name.startswith("_"):
            continue
        src = _safe(root, rel).read_text(encoding="utf-8")
        tree, anchors = ast.parse(src), _anchors(src)
        lines = src.splitlines()
        public = None
        for n in tree.body:
            if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "__all__" for t in n.targets):
                try:
                    value = ast.literal_eval(n.value)
                except (ValueError, TypeError):
                    continue
                if isinstance(value, (list, tuple)) and all(isinstance(v, str) for v in value):
                    public = value
        for n in tree.body:
            if not isinstance(n, DEFS) or not (n.name in public if public is not None else not n.name.startswith("_")):
                continue
            if not any(_near(n, line, lines) for line in anchors):
                prefix = {"visioncore": "vc", "vmem": "vm", "vadapt": "va", "vnative": "vn"}.get(rel.split("/")[0], "tools")
                missing.append(dict(file=rel, kind="class" if isinstance(n, ast.ClassDef) else "function", line=n.lineno,
                                    Location=rel + ":" + str(n.lineno), symbol=n.name,
                                    expected_prefix=prefix + "." + Path(rel).stem + "." + n.name))
    return sorted(missing, key=lambda r: (r["file"], r["line"], r["symbol"]))


# @anchor:va.build_code_index.build_in_memory [HB-VC-a0-arch]
def build_in_memory(root):
    sources = {}
    recs = scan_code_and_ast(root, sources)
    companions, stages, thr, fail = scan_hyperbooks_and_catalogue(root, recs, sources)
    return dict(records=recs, companions=companions, stages=stages, sources=sources, thresholds=thr, failures=fail)


# @anchor:va.build_code_index.build_files [HB-VC-a0-arch]
def build_files(recs, companions, stages, sources, cap=CAP, thresholds=None, failures=None):
    if type(cap) is not int or cap < 1024 or cap > CAP:
        raise BuildError("E_SCHEMA", 2, "invalid shard cap")
    files, shards = {}, {}
    def put(rel, obj):
        raw = canon(obj).encode("utf-8")
        if len(raw) > cap:
            raise BuildError("E_BUDGET", 6, "index item exceeds shard cap: " + rel)
        files[rel] = raw
    def put_map(rel, obj):
        if len(canon(obj).encode("utf-8")) <= cap:
            put(rel, obj)
            return
        items = sorted(obj.items())
        if len(items) < 2:
            raise BuildError("E_BUDGET", 6, "indivisible index entry: " + rel)
        mid = len(items) // 2
        children = [rel[:-5] + "_0.json", rel[:-5] + "_1.json"]
        shards[rel] = children
        put_map(children[0], dict(items[:mid]))
        put_map(children[1], dict(items[mid:]))
    ordered = [recs[a] for a in sorted(recs)]
    lengths = {z: sum(len(r["zones"][z]) for r in ordered) for z in ZONES}
    cols = dict(N=len(ordered), zones=ZONES, B=BPPM, A={z: lengths[z] * 1000 // max(1, len(ordered)) for z in ZONES},
                idf1=idf_ppm(len(ordered), 1) if ordered else 0,
                **{k: [r[k] for r in ordered] for k in ("anchor", "path", "line", "symbol", "book", "stage")})
    post = {}
    for i, r in enumerate(ordered):
        for z in ZONES:
            for t, count in sorted(Counter(r["zones"][z]).items()):
                post.setdefault(t, {}).setdefault(z, []).extend([i, count])
    prefixes = {}
    for t, zones in sorted(post.items()):
        df = len({i for pairs in zones.values() for i in pairs[::2]})
        prefixes.setdefault(t[:2], {})[t] = dict(d=df, i=idf_ppm(len(ordered), df), z=zones)
    for prefix, terms in prefixes.items():
        put_map("post/" + prefix + ".json", terms)
    for directory, keys in (("docs", None), ("digest", ("anchor", "doc", "line", "path", "stage", "symbol"))):
        batch, number = [], 0
        for i, r in enumerate(ordered):
            row = {k: v for k, v in r.items() if k != "zones"} if keys is None else {k: r[k] for k in keys}
            row["i"] = i
            if keys is None:
                row["zones"] = r["zones"]
            raw = canon(row).encode("utf-8")
            if len(raw) > cap:
                raise BuildError("E_BUDGET", 6, "indivisible document")
            if batch and (len(batch) == 256 or sum(map(len, batch)) + len(raw) > cap):
                files[directory + "/" + str(number) + ".jsonl"] = b"".join(batch)
                batch, number = [], number + 1
            batch.append(raw)
        files[directory + "/" + str(number) + ".jsonl"] = b"".join(batch)
    put_map("stages.json", stages)
    put_map("companions.json", companions)
    put_map("edges.json", {str(i): {k: r[k] for k in ("callees", "callers")} for i, r in enumerate(ordered)})
    put_map("aliases.json", dict(thresholds=thresholds or {}, failures=failures or {}))
    # Inline logical shard mappings; shards.json also bootstraps split columns.
    cols["shards"] = dict(shards)
    put_map("cols.json", cols)
    put("shards.json", shards)
    hashes = {rel: sha(raw) for rel, raw in sorted(files.items())}
    identity = sha(canon(dict(files=hashes, sources=sources)).encode("utf-8"))
    files["ID"] = (identity + "\n").encode("ascii")
    manifest = dict(builder=BUILDER, corpus="vision-engine", files=hashes, sources=sources,
                    index_sha256=identity, hb_books=len(companions["books"]), nodes=len(ordered),
                    schema="vqa.code-index-manifest/v1", zones=ZONES, max_file_bytes=0)
    for _ in range(8):
        manifest["max_file_bytes"] = max([len(canon(manifest).encode("utf-8"))] + [len(x) for x in files.values()])
    put("manifest.json", manifest)
    return files, identity


# @anchor:va.build_code_index.build [HB-VC-a0-arch]
def build(root, out_dir=None, cap=CAP):
    root = Path(root).resolve()
    index = build_in_memory(root)
    files, identity = build_files(index["records"], index["companions"], index["stages"], index["sources"], cap,
                                  index["thresholds"], index["failures"])
    out = Path(out_dir) if out_dir is not None else root / "build/code_index"
    out = out.resolve()
    # Only build/ is writable inside the repository, except an explicit out/ target.
    if out == root or root.is_relative_to(out) or any(out.is_relative_to(root / p) for p in (*PKG_DIRS, "hyperbooks", "catalogue")):
        raise BuildError("E_SCHEMA", 2, "index output overlaps source corpus")
    if out.is_relative_to(root) and out.relative_to(root).parts[0] not in ("build", "out"):
        raise BuildError("E_SCHEMA", 2, "repository index output must be under build/ or out/")
    if out.is_symlink() or (out.exists() and not out.is_dir()):
        raise BuildError("E_SCHEMA", 2, "index output must be a directory")
    tmp, old = Path(str(out) + ".tmp"), Path(str(out) + ".old")
    if tmp.exists() or old.exists():
        raise BuildError("E_BUSY", 3, "staging path already exists")
    moved = False
    try:
        tmp.mkdir(parents=True)
        for rel, raw in sorted(files.items()):
            p = tmp / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(raw)
        if out.exists():
            os.replace(out, old)
            moved = True
        try:
            os.replace(tmp, out)
        except OSError:
            if moved:
                os.replace(old, out)
            raise
    finally:
        if tmp.exists():
            shutil.rmtree(tmp)
    if moved:
        shutil.rmtree(old, ignore_errors=True)
    return dict(code="OK", rc=0, index_sha256=identity, nodes=len(index["records"]), files=len(files))


def _parser():
    class Parser(argparse.ArgumentParser):
        def error(self, message):
            raise BuildError("E_USAGE", 2, "invalid arguments")
    parser = Parser(add_help=False)
    parser.add_argument("--root", default=str(ROOT))
    parser.add_argument("--out")
    parser.add_argument("--ram", action="store_true")
    parser.add_argument("--cap", type=int, default=CAP)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--verify-index", action="store_true")
    parser.add_argument("--paths", nargs="+")
    return parser


# @anchor:va.build_code_index.main [HB-VC-a0-arch]
def main(argv=None):
    try:
        args = _parser().parse_args(argv)
        root = Path(args.root).resolve()
        out = args.out or ("/dev/shm/vqa-code-index-" + sha(str(root).encode())[:16] if args.ram else str(root / "build/code_index"))
        if args.check:
            missing = check_public_anchors(root, args.paths)
            res = dict(code="E_MISSING_ANCHORS" if missing else "OK", rc=5 if missing else 0,
                       missing=missing, missing_count=len(missing))
            if not missing and (args.verify_index or Path(out).exists()):
                index = build_in_memory(root)
                files, _ = build_files(index["records"], index["companions"], index["stages"], index["sources"], args.cap,
                                       index["thresholds"], index["failures"])
                existing = {p.relative_to(out).as_posix(): p.read_bytes() for p in Path(out).rglob("*") if p.is_file()}
                if files != existing:
                    res.update(code="E_INDEX_MISMATCH", rc=5)
        elif args.verify_index:
            raise BuildError("E_USAGE", 2, "--verify-index requires --check")
        else:
            res = build(root, out, args.cap)
    except BuildError as e:
        res = dict(code=e.code, rc=e.rc, detail=e.detail)
    except (OSError, ValueError, SyntaxError, KeyError, TypeError, AttributeError, IndexError, tokenize.TokenError):
        res = dict(code="E_SCHEMA", rc=2, detail="invalid corpus or index input")
    sys.stdout.write(canon(res))
    return res["rc"]


if __name__ == "__main__":
    sys.exit(main())
