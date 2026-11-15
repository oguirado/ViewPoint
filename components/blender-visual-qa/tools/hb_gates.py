#!/usr/bin/env python3
"""Deterministic HyperBook gates (stdlib only). rc 5 = unmet contract; each gate has a named code (E_*).

Usage: hb_gates.py [--all | g_hb_x ...] [--root DIR]. One canonical JSON object on stdout.
Formats and budgets come from hyperbooks/standards/ (schemas + vocabulary.json), never from constants here.
Book layout: hyperbooks/HB-BVQA-<STAGE>/ book.md (+ mathlinks.jsonl, receipts/R-*.json, and optionally manifest.json
triptych.json anchors.jsonl symbols.jsonl imports.json claims.jsonl). Missing mandatory pieces are declared AUSENTE or fail.
"""
# @module {"id":"tools.hbgates","layer":4,"owner":"BINT"}
import argparse
import ast
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parent.parent
HEAD = re.compile(r'^## <a id="(hb-s(10|[0-9]))"></a>(?:<a id="pm-s\2"></a>)?\2\. (\S.*)$')
ANCHOR_TICK = re.compile(r"`((?:bv|bpy|tools)(?:\.[A-Za-z0-9_]+){2,})`")
ANCHOR_DEF = re.compile(r"#\s*@anchor:((?:bv|bpy|tools)(?:\.[A-Za-z0-9_]+){2,})")
RECEIPT_REF = re.compile(r"\bR-[a-z][a-z0-9]*(?:[-_.][a-z0-9]+)*")
CLAIM_LINE = re.compile(r"^- (C-[A-Z0-9-]+)")
MUTANT_ID = re.compile(r"\b(?:bm|bt|bp|bo|vo|cm)\.[a-z][a-z0-9_.*]*")
GEN_LINE = re.compile(r"^(?:\*\*)?Generated:?(?:\*\*)?:?\s")
QUOTE_MAX = 240
MODULE_RE = re.compile(r"^# @module (\{.*\})\s*$")
GEN_RE = re.compile(r"^<!-- hb-generated sha256=([0-9a-f]{64}) -->$")
CODE_DIRS = ("blender_visual_qa", "bpy_side", "tools")


def canon(o):
    return json.dumps(o, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"


def sha(b):
    return hashlib.sha256(b).hexdigest()


def jload(p):
    return json.loads(Path(p).read_text(encoding="utf-8"))


def jlines(p):
    return [json.loads(x) for x in Path(p).read_text(encoding="utf-8").splitlines() if x.strip()]


def code_files(root):  # JW9b-5: filter repo-RELATIVE parts only; an ancestor dir named build must not hide sources
    for d in CODE_DIRS:
        yield from sorted(p for p in (root / d).rglob("*.py") if not {"__pycache__", "build"} & set(p.relative_to(root).parts))


def vocab(root):
    return jload(root / "hyperbooks/standards/vocabulary.json")


def schema_of(root, name):
    return jload(root / ("hyperbooks/standards/schemas/%s.schema.json" % name))


_TYPES = {"string": str, "boolean": bool, "object": dict, "array": list, "null": type(None)}


def _is(v, t):
    if t == "integer":
        return isinstance(v, int) and not isinstance(v, bool)
    return isinstance(v, _TYPES[t]) and not (t != "boolean" and isinstance(v, bool))


def validate(v, s, at="$"):
    """JSON-schema subset: const enum type(list ok) required properties additionalProperties items pattern
    minimum minItems minProperties. Returns defect strings."""
    out = []
    if "const" in s and v != s["const"]: return ["%s:const" % at]
    if "enum" in s and v not in s["enum"]: return ["%s:enum" % at]
    t = s.get("type")
    if t is not None:
        ts = t if isinstance(t, list) else [t]
        if not any(_is(v, x) for x in ts): return ["%s:type" % at]
    if isinstance(v, str) and "pattern" in s and not re.search(s["pattern"], v): out.append("%s:pattern" % at)
    if _is(v, "integer") and "minimum" in s and v < s["minimum"]: out.append("%s:minimum" % at)
    if isinstance(v, dict):
        for k in s.get("required", []):
            if k not in v: out.append("%s.%s:required" % (at, k))
        if len(v) < s.get("minProperties", 0): out.append("%s:minProperties" % at)
        props = s.get("properties", {})
        for k, x in sorted(v.items()):
            if k in props: out += validate(x, props[k], at + "." + k)
            elif s.get("additionalProperties") is False: out.append("%s.%s:additional" % (at, k))
    if isinstance(v, list):
        if len(v) < s.get("minItems", 0): out.append("%s:minItems" % at)
        if "items" in s:
            for i, x in enumerate(v): out += validate(x, s["items"], "%s[%d]" % (at, i))
    return out


def books(root):
    """-> [(book_id, book .md path, optional sidecar dir hyperbooks/<book_id>/)]; flat layout, sidecar files are optional."""
    base = root / "hyperbooks"
    return [(p.stem, p, base / p.stem) for p in sorted(base.glob("HB-BVQA-*.md"))] if base.is_dir() else []


def ticks(text):
    return re.findall(r"`([^`\n]+)`", text)


def path_like(t):
    return bool(re.fullmatch(r"[A-Za-z0-9_./*-]+", t)) and ("/" in t or t.endswith((".py", ".json", ".jsonl", ".md", ".tsv")))


def exists(root, t):
    t = t.split("#")[0].rstrip(":")
    return bool(list(root.glob(t))) if "*" in t else (root / t).exists()


def known_ids(root):
    ids = set(anchor_defs(root))
    for rel in ("hyperbooks/nodes.jsonl", "catalogue/symbols.jsonl"):
        if (root / rel).is_file():
            ids |= {r.get("id") or r.get("anchor") for r in jlines(root / rel)}
    return ids


def rows_of(p):
    return jlines(p) if p.is_file() else []


def split_sections(text):
    """-> ({id: (title, body_lines)}, [ids in order], [bad ## lines])."""
    secs, order, bad, cur = {}, [], [], None
    for ln in text.split("\n"):
        if ln.startswith("## "):
            m = HEAD.match(ln)
            if not m: bad.append(ln[:60]); cur = None; continue
            cur = m.group(1); order.append(cur); secs.setdefault(cur, (m.group(3), []))
        elif cur: secs[cur][1].append(ln)
    return secs, order, bad


def body_sha(lines):
    return sha(("\n".join(lines).strip() + "\n").encode("utf-8"))


def seal_generated(lines):
    """Writer helper: marker line for a generated section body (without the marker itself)."""
    return "<!-- hb-generated sha256=%s -->" % body_sha(lines)


def anchor_defs(root):
    out = {}
    for p in code_files(root):
        for i, ln in enumerate(p.read_text(encoding="utf-8").split("\n"), 1):
            m = ANCHOR_DEF.search(ln)
            if m: out[m.group(1)] = (p.relative_to(root).as_posix(), i)
    return out


SIDE_FILES = {"manifest.json": "hb.manifest.v1", "triptych.json": "hb.triptych.v1", "imports.json": "hb.imports.v1"}
SIDE_ROWS = {"anchors.jsonl": "hb.anchor.v1", "symbols.jsonl": "hb.symbol.v1", "claims.jsonl": "hb.claim.v1", "mathlinks.jsonl": "hb.mathlink.v1"}


def g_hb_schema(root):
    out, voc = [], vocab(root)
    have = {b[0] for b in books(root)}
    out += ["book_absent:" + b for b in sorted(set(voc["books"]) - have)]
    out += ["book_unknown:" + b for b in sorted(have - set(voc["books"]))]
    for bk, md, side in books(root):
        secs, order, bad = split_sections(md.read_text(encoding="utf-8"))
        out += ["%s:bad_heading:%s" % (bk, x) for x in bad]
        if order != voc["sections"]:
            out.append("%s:sections_not_hb-s0..hb-s10_once_in_order" % bk)
        for f, sn in SIDE_FILES.items():
            if (side / f).is_file():
                try:
                    out += ["%s:%s%s" % (bk, f, d) for d in validate(jload(side / f), schema_of(root, sn))[:6]]
                except ValueError:
                    out.append("%s:%s:json" % (bk, f))
        for f, sn in SIDE_ROWS.items():
            if (side / f).is_file():
                try:
                    lines = jlines(side / f)
                except ValueError:
                    out.append("%s:%s:json" % (bk, f))
                    continue
                for i, r in enumerate(lines, 1):
                    out += ["%s:%s:%d%s" % (bk, f, i, d) for d in validate(r, schema_of(root, sn))[:4]]
        if (side / "manifest.json").is_file():
            try:
                if jload(side / "manifest.json").get("book_id") != bk:
                    out.append(bk + ":manifest_book_id")
            except ValueError:
                pass
    nodes = root / "hyperbooks/nodes.jsonl"
    if not nodes.is_file():
        return out + ["nodes.jsonl:AUSENTE"]
    try:
        nr = jlines(nodes)
    except ValueError:
        return out + ["nodes.jsonl:json"]
    seen = set()
    for i, r in enumerate(nr, 1):
        out += ["nodes.jsonl:%d%s" % (i, d) for d in validate(r, schema_of(root, "ctx.node.v1"))[:4]]
        if isinstance(r, dict):
            if (r.get("kind"), r.get("id")) in seen:
                out.append("nodes.jsonl:%d:dup_id" % i)
            seen.add((r.get("kind"), r.get("id")))
    return out


def g_hb_anchor(root):
    out, ids = [], known_ids(root)
    defs = anchor_defs(root)
    for bk, md, side in books(root):
        for t in sorted(set(ticks(md.read_text(encoding="utf-8")))):
            if ANCHOR_TICK.fullmatch("`%s`" % t) and t not in ids:
                out.append("%s:anchor_unresolved:%s" % (bk, t))
        for r in rows_of(side / "anchors.jsonl"):
            a = r.get("anchor")
            if a not in defs:
                out.append("%s:anchors.jsonl:unresolved:%s" % (bk, a))
            elif defs[a][0] != r.get("path"):
                out.append("%s:anchors.jsonl:path_moved:%s" % (bk, a))
    return out


def g_hb_drift(root):
    out = []
    for bk, md, side in books(root):
        for r in rows_of(side / "anchors.jsonl"):
            f = root / str(r.get("path", ""))
            if not f.is_file():
                out.append("%s:%s:path_missing" % (bk, r.get("anchor")))
                continue
            ls = f.read_bytes().splitlines(keepends=True)
            if not 1 <= r["line"] <= r["end_line"] <= len(ls):
                out.append("%s:%s:span_out_of_file" % (bk, r["anchor"]))
            elif sha(b"".join(ls[r["line"] - 1:r["end_line"]])) != r["span_sha256"]:
                out.append("%s:%s:drift" % (bk, r["anchor"]))
    return out


def g_hb_receipt(root):
    """Receipts: sidecar hb.receipt.v1 files (sources re-hashed) and in-prose `- C-... MEASURED `path`` claims (path must exist)."""
    out, schema = [], schema_of(root, "hb.receipt.v1")
    for bk, md, side in books(root):
        have = set()
        for p in sorted((side / "receipts").glob("R-*.json")) if (side / "receipts").is_dir() else []:
            tag = "%s/%s" % (bk, p.name)
            try:
                r = jload(p)
            except ValueError:
                out.append(tag + ":json")
                continue
            d = validate(r, schema)
            out += [tag + x for x in d[:6]]
            if d:
                continue
            have.add(r["id"])
            if r["id"] != p.stem:
                out.append(tag + ":id_ne_stem")
            if r["commit"] is not None and not r["tree_clean"]:
                out.append(tag + ":commit_with_dirty_tree")
            for a in r["argv"]:
                if a.endswith(".py") and not (root / a).is_file():
                    out.append("%s:runner_missing:%s" % (tag, a))
            for sp, h in sorted(r["sources"].items()):
                f = root / sp
                if not f.is_file():
                    out.append("%s:E_STALE_RECEIPT:source_missing:%s" % (tag, sp))
                elif sha(f.read_bytes()) != h:
                    out.append("%s:E_STALE_RECEIPT:source_changed:%s" % (tag, sp))
        withdrawn = set()
        if (side / "manifest.json").is_file():
            try:
                withdrawn = set(jload(side / "manifest.json").get("withdrawn_receipts", []))
            except ValueError:
                pass
        text = md.read_text(encoding="utf-8")
        for rid in sorted(set(RECEIPT_REF.findall(text))):
            if rid not in have and rid not in withdrawn:
                out.append("%s:receipt_unresolved:%s" % (bk, rid))
        for ln in text.split("\n"):
            m = CLAIM_LINE.match(ln)
            if m and "MEASURED" in ln:
                paths = [t for t in ticks(ln) if path_like(t)]
                if not paths:
                    out.append("%s:MEASURED_without_receipt:%s" % (bk, m.group(1)))
                out += ["%s:MEASURED_receipt_missing:%s:%s" % (bk, m.group(1), t) for t in paths if not exists(root, t)]
        for c in rows_of(side / "claims.jsonl"):
            if c.get("status") == "MEASURED" and c.get("receipt") not in have:
                out.append("%s:MEASURED_without_receipt:%s" % (bk, c.get("id")))
    return out


def g_hb_oracle(root):
    """hb-s4 must carry an `**Oracle**` line naming existing tests/gates or mutant ids (or start AUSENTE); a sidecar triptych needs >=3 mutants killed rc 5."""
    out, voc = [], vocab(root)
    for bk, md, side in books(root):
        secs = split_sections(md.read_text(encoding="utf-8"))[0]
        body = "\n".join(secs.get("hb-s4", ("", []))[1])
        lines = [x for x in body.split("\n") if x.startswith("**Oracle**")]
        if not lines:
            out.append(bk + ":oracle_line_missing")
        else:
            refs = [t for t in ticks(lines[0]) if path_like(t)]
            if not refs and not MUTANT_ID.search(lines[0]) and voc["absence_prefix"] not in lines[0]:
                out.append(bk + ":oracle_names_nothing")
            out += ["%s:oracle_path_missing:%s" % (bk, t) for t in refs if not exists(root, t)]
        if (side / "triptych.json").is_file():
            doc = jload(side / "triptych.json")
            d = validate(doc, schema_of(root, "hb.triptych.v1"))
            out += ["%s:triptych%s" % (bk, x) for x in d[:6]]
            if d:
                continue
            o = doc["oracle"]
            if sum(1 for m in o["mutants"] if m["killed"] and m["rc"] == 5) < o["min_killed"]:
                out.append(bk + ":mutants_killed_below_min")
            if any(m["killed"] and m["rc"] != 5 for m in o["mutants"]):
                out.append(bk + ":kill_rc_not_5")
            out += ["%s:oracle_runner_missing:%s" % (bk, a) for a in o["argv"] if a.endswith(".py") and not (root / a).is_file()]
    return out


def g_hb_reading(root):
    out = []
    for bk, md, side in books(root):
        text = md.read_text(encoding="utf-8")
        secs, _, _ = split_sections(text)
        for sid, (_, body) in sorted(secs.items()):
            if not "".join(body).strip():
                out.append("%s:%s:empty_section" % (bk, sid))
        for m in re.finditer(r"\]\(([^)#\s:]+)(?:#[^)]*)?\)", text):
            if not (md.parent / m.group(1)).exists() and not (root / m.group(1)).exists():
                out.append("%s:link_unresolved:%s" % (bk, m.group(1)))
        # mathlinks cite, never copy: no long blockquote lines
        out += ["%s:copied_text_suspect:line%d" % (bk, i) for i, ln in enumerate(text.split("\n"), 1) if ln.startswith(">") and len(ln) > QUOTE_MAX]
        for r in rows_of(side / "mathlinks.jsonl"):
            q = r.get("quote")
            if r.get("status") == "SEALED" and not (r.get("target_path") and q and r.get("quote_sha256")):
                out.append("%s:mathlink_sealed_without_quote:%s" % (bk, r.get("target_id")))
            elif r.get("status") == "SEALED" and sha(q.encode("utf-8")) != r.get("quote_sha256"):
                out.append("%s:mathlink_quote_sha:%s" % (bk, r.get("target_id")))
            if r.get("status") == "AUSENTE" and (q or r.get("quote_sha256")):
                out.append("%s:mathlink_ausente_with_quote:%s" % (bk, r.get("target_id")))
    return out


def g_hb_rebuild(root):
    """hb-s8/9/10 are generated: a sha-sealed marker (body re-hashed), a `Generated:` line naming an existing file, or AUSENTE."""
    out, voc = [], vocab(root)
    for bk, md, side in books(root):
        text = md.read_bytes().decode("utf-8")
        if "\r" in text or not text.endswith("\n"):
            out.append(bk + ":not_lf_or_no_final_newline")
        secs, _, _ = split_sections(text)
        for sid in voc["generated_sections"]:
            body = secs.get(sid, ("", []))[1]
            nz = [x for x in body if x.strip()]
            if not nz:
                out.append("%s:%s:empty" % (bk, sid))
                continue
            first = nz[0]
            m = GEN_RE.match(first)
            if m:
                rest = body[body.index(first) + 1:]
                if body_sha(rest) != m.group(1):
                    out.append("%s:%s:generated_body_changed" % (bk, sid))
            elif first.startswith(voc["absence_prefix"]):
                continue
            elif GEN_LINE.match(first):
                src = [t for t in ticks(first) if path_like(t)]
                if not src or not exists(root, src[0]):
                    out.append("%s:%s:generator_source_missing" % (bk, sid))
            elif any(GEN_LINE.match(x) for x in nz):
                src = [t for x in nz if GEN_LINE.match(x) for t in ticks(x) if path_like(t)]
                if not src or not exists(root, src[0]):
                    out.append("%s:%s:generator_source_missing" % (bk, sid))
            else:
                out.append("%s:%s:not_declared_generated" % (bk, sid))
    return out


def g_hb_budget(root):
    out, bud = [], vocab(root)["budgets"]
    for i, r in enumerate(rows_of(root / "hyperbooks/nodes.jsonl"), 1):
        lim = bud["section_node_bytes"] if r.get("kind") == "section" else bud["symbol_node_bytes"]
        if len(canon(r).encode("utf-8")) > lim:
            out.append("nodes.jsonl:%d:over_budget:%s" % (i, r.get("id")))
    for bk, md, side in books(root):
        if (side / "symbols.jsonl").is_file():
            for i, ln in enumerate((side / "symbols.jsonl").read_text(encoding="utf-8").split("\n"), 1):
                if len(ln.encode("utf-8")) > bud["symbol_node_bytes"]:
                    out.append("%s:symbols.jsonl:%d:over_budget" % (bk, i))
    return out


def g_hb_public(root):
    out, pats = [], [re.compile(x) for x in vocab(root)["secret_patterns"]]
    files = [root / n for n in ("llms.txt", "AGENTS.md", "README.md", "QUICKSTART.md")]
    base = root / "hyperbooks"
    if base.is_dir():
        files += sorted(p for p in base.rglob("*") if p.is_file() and p.suffix in (".md", ".json", ".jsonl") and p.name != "vocabulary.json")
    for f in files:
        if f.is_file():
            t = f.read_text(encoding="utf-8", errors="replace")
            out += ["%s:%s" % (f.relative_to(root).as_posix(), p.pattern[:24]) for p in pats if p.search(t)]
    return out


def g_hb_router(root):
    """O-B11 (no node with path "" line 0) + the frozen router bench: tests/router_bench/bench.py --split TEST must exit 0."""
    out = []
    for i, r in enumerate(rows_of(root / "hyperbooks/nodes.jsonl"), 1):
        if r.get("path") == "" and r.get("line") == 0:
            out.append("nodes.jsonl:%d:O-B11_empty_path_line0:%s" % (i, r.get("id")))
    bench = root / "tests/router_bench/bench.py"
    if not bench.is_file():
        return out + ["router_bench:AUSENTE"]
    try:
        r = subprocess.run([sys.executable, "-B", str(bench), "--split", "TEST"], capture_output=True, text=True, timeout=120, cwd=str(root))
        if r.returncode:
            out.append("router_bench:rc%d" % r.returncode)
    except (OSError, subprocess.SubprocessError) as e:
        out.append("router_bench:" + type(e).__name__)
    return out


def g_hb_header(root):
    voc, ok, rows = vocab(root), 0, []
    for p in code_files(root):
        for ln in p.read_text(encoding="utf-8").split("\n")[:40]:
            m = MODULE_RE.match(ln)
            if m: rows.append((sha(p.relative_to(root).as_posix().encode()), p, json.loads(m.group(1))))
    rows = sorted(rows, key=lambda r: r[0])[:voc["budgets"]["header_sample"]]
    if not rows: return ["header:no_modules"]
    for _, _, h in rows:
        ok += 1 if h.get("book") in voc["books"] and (root / "hyperbooks" / (str(h.get("book")) + ".md")).is_file() else 0
    # integer form of ok/n >= header_min/header_sample
    return [] if ok * voc["budgets"]["header_sample"] >= voc["budgets"]["header_min"] * len(rows) else ["header:book_field %d/%d" % (ok, len(rows))]


def g_stdlib(root):
    out, std = [], sys.stdlib_module_names
    files = [root / "tools/hb_gates.py", root / "tools/route_code.py"] + sorted((root / "hyperbooks").rglob("*.py"))
    for f in files:
        if not f.is_file(): continue
        for n in ast.walk(ast.parse(f.read_text(encoding="utf-8"))):
            mods = [a.name for a in n.names] if isinstance(n, ast.Import) else [n.module or ""] if isinstance(n, ast.ImportFrom) and not n.level else []
            for m in mods:
                if m.split(".")[0] not in std: out.append("%s:non_stdlib:%s" % (f.relative_to(root).as_posix(), m))
    return out


def g_fresh(root):
    out = []
    try:
        r = subprocess.run([sys.executable, "-B", str(root / "tools/build_catalogue.py"), "--root", str(root), "--check"],
                           capture_output=True, text=True, timeout=60)
        if r.returncode: out.append("catalogue:build_catalogue_check_rc%d" % r.returncode)
    except (OSError, subprocess.SubprocessError) as e:
        out.append("catalogue:" + type(e).__name__)
    p = root / "hyperbooks/nodes.jsonl"
    if p.is_file():
        defs = anchor_defs(root)
        for i, r in enumerate(jlines(p), 1):
            if r.get("path") and not (root / r["path"]).is_file(): out.append("nodes.jsonl:%d:path_missing:%s" % (i, r["path"]))
            elif r.get("kind") == "anchor" and r.get("id") not in defs: out.append("nodes.jsonl:%d:anchor_stale:%s" % (i, r.get("id")))
    return out


GATES = {fn.__name__: fn for fn in (g_hb_schema, g_hb_anchor, g_hb_drift, g_hb_receipt, g_hb_oracle, g_hb_reading, g_hb_rebuild,
                                    g_hb_budget, g_hb_public, g_hb_router, g_hb_header, g_stdlib, g_fresh)}


# @anchor:tools.hbgates.main
def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("gates", nargs="*", choices=tuple(GATES))
    p.add_argument("--all", action="store_true")
    p.add_argument("--root", type=Path, default=ROOT)
    a = p.parse_args(argv)
    names = list(GATES) if a.all else a.gates
    if not names:
        sys.stdout.write(canon({"code": "E_USAGE", "rc": 2, "reason": "no gates selected"}))
        return 2
    root, res = a.root.resolve(), []
    for n in names:
        try: f = GATES[n](root)
        except Exception as e: f = ["precondition:%s:%s" % (type(e).__name__, e)]
        res.append({"gate": n, "rc": 5 if f else 0, "code": "E_" + n.replace("g_hb_", "").upper() if f else "OK", "failures": sorted(f)})
    rc = 5 if any(r["rc"] for r in res) else 0
    sys.stdout.write(canon({"code": "E_GATE" if rc else "OK", "rc": rc, "gates": res}))
    return rc


if __name__ == "__main__":
    sys.exit(main())
