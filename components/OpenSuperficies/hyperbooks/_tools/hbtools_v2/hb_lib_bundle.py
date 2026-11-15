"""Language-neutral bundle adapter. No parser is used when a bundle is supplied.

HBV2_SYMBOLS_BUNDLE is the environment equivalent of --symbols-bundle.
Optional legacy/source_b64 fields preserve sealed Python artefacts byte for byte.
"""
import base64
import copy
import os
import subprocess
import sys
import tempfile
from types import SimpleNamespace

import hblib2 as L

SCHEMA = "hb.symbols.bundle/v1"
# Compatibility identity for configless, sealed router/mini fixtures only.
LEGACY_FIXTURE_ID = "HB-ROUTER-002"


def book_id(root=None, explicit=None, config=None):
    if explicit or os.environ.get("HBV2_HB_ID"):
        return explicit or os.environ["HBV2_HB_ID"]
    if config:
        value = config.get("hb_id") or config.get("book_id")
        if value:
            return value
    if root:
        # A sealed manifest pins its historical identity, including legacy IDs.
        for rel in ("manifest.json", "seed/slots.json"):
            path = os.path.join(root, L.BOOK_SUB, rel)
            if os.path.isfile(path):
                cfg = L.jload(path)
                value = cfg.get("hb_id") or cfg.get("book_id")
                if value:
                    return value
    return LEGACY_FIXTURE_ID


def emit(x, root, hb_id):
    import hb_extract2 as X
    files = [{"path": m.path, "lang": "python", "sha256": L.sha256b(m.data),
              "source_b64": base64.b64encode(m.data).decode("ascii")} for m in x["repo"].mods]
    symbols = [{"id": r["id"], "path": r["path"], "name": r["id"].split("::", 1)[1],
                "kind": r["kind"], "span": [r["span"]["start_line"], r["span"]["end_line"]],
                "lang": "python", "signature": r["sig"], "signature_sha256": r["signature_sha256"],
                "legacy": copy.deepcopy(r)} for r in x["symbols"]]
    return dict(schema=SCHEMA, hb_id=hb_id, root=os.fspath(root), lang_default="python", files=files,
                symbols=symbols, anchors=x["anchors"], edges=[dict(src=a, dst=b, kind=k) for a, b, k in sorted(x["edges"])],
                texts=X.symbol_texts(x["repo"]), extraction=dict(stats=x["stats"], unresolved=x["unresolved"]))


def validate(b):
    """Validate the JSON contract and cross references before touching outputs."""
    from vendor import jsonschema_lite
    schema = L.jload(os.path.join(os.path.dirname(__file__), "schema", "hb.symbols.bundle.v1.json"))
    errors = jsonschema_lite.validate(b, schema)
    if errors:
        raise ValueError("invalid symbol bundle: " + "; ".join(errors[:5]))
    files = {f["path"]: f for f in b["files"]}
    byid = {s["id"]: s for s in b["symbols"]}
    ids = set(byid)
    if len(files) != len(b["files"]) or len(ids) != len(b["symbols"]):
        raise ValueError("duplicate bundle path or symbol id")
    contents = {}
    for f in b["files"]:
        parts = f["path"].replace("\\", "/").split("/")
        if os.path.isabs(f["path"]) or ".." in parts:
            raise ValueError("bundle paths must be relative and confined")
        if "source_b64" in f:
            contents[f["path"]] = base64.b64decode(f["source_b64"], validate=True)
            if L.sha256b(contents[f["path"]]) != f["sha256"]:
                raise ValueError("source checksum mismatch")
    for s in b["symbols"]:
        if s["path"] not in files or s["lang"] != files[s["path"]]["lang"] or s["span"][1] < s["span"][0]:
            raise ValueError("invalid symbol file, language or span")
        if "legacy" in s:
            r = s["legacy"]
            if any(r.get(k) != s[k] for k in ("id", "path", "kind", "signature_sha256")) or r.get("sig") != s["signature"]:
                raise ValueError("legacy symbol disagrees with neutral record")
            if [r["span"]["start_line"], r["span"]["end_line"]] != s["span"]:
                raise ValueError("legacy span disagrees with neutral record")
            start, end = r["span"]["start_byte"], r["span"]["end_byte"]
            if end < start or r["bytes"] != end - start:
                raise ValueError("invalid legacy byte span")
            f = files[s["path"]]
            if "source_b64" in f:
                data = contents[s["path"]]
                if end > len(data) or L.sha256b(data[start:end]) != r["sha256"]:
                    raise ValueError("legacy symbol checksum mismatch")
    if set(b["texts"]) != ids:
        raise ValueError("texts must cover exactly the bundle symbols")
    seen_anchors = set()
    for a in b["anchors"]:
        if a["id"] in seen_anchors:
            raise ValueError("duplicate bundle anchor")
        seen_anchors.add(a["id"])
        if a["id"] not in ids or a["lang"] != byid[a["id"]]["lang"] or a["path"] != byid[a["id"]]["path"]:
            raise ValueError("anchor symbol or language mismatch")
        if a["signature_sha256"] != byid[a["id"]]["signature_sha256"] or [a["span"]["start_line"], a["span"]["end_line"]] != byid[a["id"]]["span"]:
            raise ValueError("anchor signature or span mismatch")
    for e in b["edges"]:
        if e["src"] not in ids or e["dst"] not in ids:
            raise ValueError("dangling bundle edge")
    return b


def decode(b):
    import hb_extract2 as X
    validate(b)
    mods, rows = [], []
    anchors_byid = {a["id"]: a for a in b["anchors"]}
    for f in b["files"]:
        data = base64.b64decode(f.get("source_b64", ""), validate=True)
        mods.append(SimpleNamespace(path=f["path"], data=data, nlines=data.count(b"\n") + int(bool(data) and not data.endswith(b"\n")),
                                    file_sha256=f["sha256"], lang=f["lang"], source_known="source_b64" in f))
    for s in b["symbols"]:
        if "legacy" in s:
            r = copy.deepcopy(s["legacy"])
        else:
            r = dict(schema="hb.symbol.v2", id=s["id"], path=s["path"], kind=s["kind"], lang=s["lang"],
                     span=dict(start_line=s["span"][0], end_line=s["span"][1], start_byte=None, end_byte=None),
                     bytes=None, sha256=None, sha256_ast=None,
                     doc_sha256=L.sha256s(b["texts"][s["id"]]["doc"]), sig=s["signature"],
                     signature_sha256=s["signature_sha256"], governs=dict(sections=[], inv=[], au=[], none_declared=True))
        anchor = anchors_byid.get(s["id"])
        if "legacy" not in s and anchor:
            for key in ("sha256", "sha256_ast", "doc_sha256", "sha256_shell"):
                if key in anchor:
                    r[key] = anchor[key]
            for key in ("start_byte", "end_byte"):
                if key in anchor:
                    r["span"][key] = anchor[key]
            if all(r["span"][key] is not None for key in ("start_byte", "end_byte")):
                r["bytes"] = r["span"]["end_byte"] - r["span"]["start_byte"]
        if s["lang"] != "python":
            r["lang"] = s["lang"]
        for key in ("receiver", "pkg_path", "exported", "is_test"):
            if key in s:
                r[key] = s[key]
        rows.append(r)
    rows.sort(key=lambda r: r["id"].encode("utf-8"))
    edges = {(e["src"], e["dst"], e["kind"]) for e in b["edges"]}
    anchors = copy.deepcopy(b["anchors"]) or X.anchors_from_symbols(rows, lang_default=b["lang_default"])
    return dict(repo=SimpleNamespace(mods=mods), symbols=rows, anchors=anchors, edges=edges,
                call_edges={e for e in edges if e[2] == "CALLS"}, import_edges={e for e in edges if e[2] == "IMPORTS"},
                static_test_edges={e for e in edges if e[2] == "TESTS_STATIC"},
                stats=b.get("extraction", {}).get("stats", dict(calls=None, resolved=None, external=None, unresolved=None)),
                unresolved=b.get("extraction", {}).get("unresolved", []), kinds={r["id"]: r["kind"] for r in rows},
                texts=copy.deepcopy(b["texts"]), bundle=b)


def load(path):
    return decode(L.jload(path))


def source(subject, out=None, symbols_bundle=None, hb_id=None):
    path = symbols_bundle or os.environ.get("HBV2_SYMBOLS_BUNDLE")
    if path:
        return load(path)
    cached = os.path.join(out, "symbols_bundle.json") if out else None
    if cached and os.path.isfile(cached):
        x = load(cached)
        raw = os.path.join(out, "symbols_raw.jsonl")
        if os.path.isfile(raw):
            rows = L.read_jsonl(raw)
            if rows != x["symbols"]:
                raise ValueError("raw symbols disagree with cached bundle; rerun extractor")
            x["symbols"] = rows
        return x
    # Extraction is an explicit producer boundary, never an in-process parser in
    # build/index. Existing raw rows remain the symbol source when present.
    with tempfile.TemporaryDirectory(prefix="hb-bundle-") as d:
        proc = subprocess.run([sys.executable, os.path.join(L.HERE, "hb_extract2.py"),
                               "--subject", subject, "--out", d, "--hb-id", hb_id or book_id()],
                              capture_output=True, text=True, timeout=300,
                              env={k: v for k, v in os.environ.items() if k != "HBV2_SYMBOLS_BUNDLE"})
        if proc.returncode:
            raise ValueError("Python bundle producer failed: " + proc.stderr[-800:])
        x = load(os.path.join(d, "symbols_bundle.json"))
    raw = os.path.join(out, "symbols_raw.jsonl") if out else None
    if raw and os.path.isfile(raw):
        rows = L.read_jsonl(raw)
        if rows != x["symbols"]:
            raise ValueError("symbols_raw.jsonl is stale; run hb_extract2.py first")
        x["symbols"] = rows
    return x
