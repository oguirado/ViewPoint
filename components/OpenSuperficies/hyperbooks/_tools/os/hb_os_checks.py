"""OS replacements for the hbtools_v2 gates that cannot run over Go (see hb_os_driver.SKIPPED). Each returns {"gate","pass","errors"}.

Every check fails closed: a missing input is a failure, never a pass. Stdlib + the pinned hbtools_v2 modules only.
"""
import hashlib, json, os, re, shutil, sys, tempfile

sys.dont_write_bytecode = True
import hblib2 as L  # noqa: E402
import hb_build as B  # noqa: E402

P3_FIELDS = ("INPUTS", "PRECONDITIONS", "OUTPUTS", "TIME", "SPACE", "ARITHMETIC", "FAILURE")
P3_RE = re.compile(r"# PM-P3-HEADER: (\S+)\n((?:# [A-Z]+: .*\n)+)")
PM_RE = re.compile(r"<!-- PM:BEGIN (\S+) -->\n```json\n(.*?)\n```\n<!-- PM:END \1 -->", re.S)
REF_RE = re.compile(r"(?<![A-Za-z0-9_./-])((?:src|cmd)/[A-Za-z0-9_./-]+\.go):(\d+)(?:-(\d+))?(?:\s+sha_ast[:=]([0-9a-f]{8,64}))?")
GEN_RE = re.compile(r"<!-- HB:GEN BEGIN (\S+) -->.*?<!-- HB:GEN END \1 -->", re.S)
S10_RE = re.compile(r"^\| `([^`]+\.go)` \| `([0-9a-f]{16})` \| (\d+) \|$", re.M)
PRIV_RE = re.compile(r"(/home/|/Users/|https?://|BEGIN [A-Z ]*PRIVATE KEY|(?i:api[_-]?key|secret|token)\s*[:=]\s*\S{8,})")
SEC_SIGNIFICANT = ("symbols.jsonl", "anchors.jsonl", "nodes.jsonl", "claims.jsonl", "graph.json", "aliases.json", "book.md")


def res(name, errs, **kw):
    d = {"gate": name, "pass": not errs, "errors": [str(e) for e in errs[:20]]}
    d.update(kw)
    return d


def _bd(root, book):
    return os.path.join(root, book)


def _read(p):
    with open(p, encoding="utf-8") as f:
        return f.read()


def _live(b):
    return {a["id"]: a for a in b["anchors"]}


ANCHOR_SCHEMA_SHA = "507b3ceb0c953be7356d2f1a8dce52f7fa54939f0b416217928b9ac61e58b513"
NEUTRAL_SYMBOL_KEYS = ("lang", "receiver", "pkg_path", "exported", "is_test")


def g_os_schema(root, subject, b, book):
    import make_schemas, schemalib
    bd = _bd(root, book)
    errs = []
    if not os.path.isfile(os.path.join(bd, "book.md")) or not os.path.isfile(os.path.join(bd, "symbols.jsonl")):
        return res("g_os_schema", ["missing book.md or symbols.jsonl"])
    d = tempfile.mkdtemp(prefix="hbschemas-")
    make_schemas.main([d])
    # the pin's make_schemas still emits the pre-"go" lang enum; use the 681dc0b standards schema (sha256 507b3ceb...b513, verified)
    sp = os.path.join(os.path.dirname(os.path.abspath(__file__)), "schemas", "hb.anchor.v2.schema.json")
    if hashlib.sha256(open(sp, "rb").read()).hexdigest() != ANCHOR_SCHEMA_SHA:
        return res("g_os_schema", ["schemas/hb.anchor.v2.schema.json sha256 mismatch"])
    shutil.copyfile(sp, os.path.join(d, "hb.anchor.v2.schema.json"))
    n = 0
    for name, sid in (("symbols.jsonl", "hb.symbol.v2"), ("anchors.jsonl", "hb.anchor.v2")):
        p = os.path.join(bd, name)
        if not os.path.isfile(p):
            errs.append("missing " + name)
            continue
        for r in L.read_jsonl(p):
            n += 1
            # 681dc0b format gap (REQUEST_VQA0.md 5): the closed hb.symbol.v2 schema has no slot for the bundle's neutral Go metadata
            r = {k: v for k, v in r.items() if k not in NEUTRAL_SYMBOL_KEYS} if name == "symbols.jsonl" else r
            e = schemalib.validate(r, sid, d)
            if e:
                errs.append("%s %s: %s" % (name, r.get("id"), e[0]))
    pb = B.parse_book(_read(os.path.join(bd, "book.md")))
    for k, sid in (("sections", "hb.section.v2"), ("inv", "hb.inv.v2"), ("claims", "hb.claim.v2")):
        for o in pb[k]:
            n += 1
            e = schemalib.validate(o, sid, d)
            if e:
                errs.append("book %s: %s" % (sid, e[0]))
    if n == 0:
        errs.append("0 items validated (vacuous)")
    return res("g_os_schema", errs, items=n)


def g_os_anchor(root, subject, b, book):
    bd = _bd(root, book)
    errs = []
    live = _live(b)
    ap = os.path.join(bd, "anchors.jsonl")
    sp = os.path.join(bd, "symbols.jsonl")
    if not (os.path.isfile(ap) and os.path.isfile(sp)):
        return res("g_os_anchor", ["missing anchors.jsonl or symbols.jsonl"])
    anchors, syms = L.read_jsonl(ap), L.read_jsonl(sp)
    if not anchors:
        return res("g_os_anchor", ["0 anchors (vacuous)"])
    for a in anchors:
        t = live.get(a["id"])
        if t is None:
            errs.append("anchor %s: symbol gone from code" % a["id"])
            continue
        for k in ("sha256", "sha256_ast", "start_byte", "end_byte"):
            if a.get(k) != t.get(k):
                errs.append("anchor %s: %s differs (book %s, code %s)" % (a["id"], k, str(a.get(k))[:12], str(t.get(k))[:12]))
        if a["span"] != t["span"]:
            errs.append("anchor %s: span %s != code %s" % (a["id"], a["span"], t["span"]))
        # reopen path:line from disk and re-hash the slice
        fp = os.path.join(subject, a["path"])
        if not os.path.isfile(fp):
            errs.append("anchor %s: file missing" % a["id"])
        else:
            data = L.readb(fp)
            if L.sha256b(data[a["start_byte"]:a["end_byte"]]) != a["sha256"]:
                errs.append("anchor %s: bytes at %s:%d-%d re-hash differs" % (a["id"], a["path"], a["span"]["start_line"], a["span"]["end_line"]))
    for s in syms:
        t = live.get(s["id"])
        if t is None:
            errs.append("symbol %s: gone from code" % s["id"])
        elif (s["sha256"], s["sha256_ast"], s["span"]["start_line"], s["span"]["end_line"]) != (t["sha256"], t["sha256_ast"], t["span"]["start_line"], t["span"]["end_line"]):
            errs.append("symbol %s: stored hashes/span differ from code" % s["id"])
    stored = {a["id"] for a in anchors}
    for sid in sorted(set(live) - stored):
        errs.append("code symbol %s not in book anchors" % sid)
    return res("g_os_anchor", errs, anchors=len(anchors))


def _prose(text):
    return re.sub(r"```.*?```", " ", GEN_RE.sub(" ", text), flags=re.S)


def g_os_drift(root, subject, b, book):
    bd = _bd(root, book)
    errs = []
    live = _live(b)
    mp = os.path.join(bd, "manifest.json")
    bp = os.path.join(bd, "book.md")
    if not (os.path.isfile(mp) and os.path.isfile(bp)):
        return res("g_os_drift", ["missing manifest.json or book.md"])
    man = L.jload(mp)
    for f, sha in sorted(man.get("files", {}).items()):
        p = os.path.join(bd, f)
        if not os.path.isfile(p):
            errs.append("manifest: %s missing" % f)
        elif L.sha_file(p) != sha:
            errs.append("manifest: %s sha differs from disk (book-side edit without rebuild)" % f)
    for f in SEC_SIGNIFICANT:
        if f not in man.get("files", {}):
            errs.append("manifest lacks %s" % f)
    text = _read(bp)
    # (b) s10 file hashes vs live files
    files = {f["path"]: f["sha256"] for f in b["files"]}
    rows = S10_RE.findall(text)
    seen = set()
    for path, h16, nbytes in rows:
        if path not in files:
            errs.append("s10: %s not in code scope" % path)
            continue
        seen.add(path)
        if files[path][:16] != h16:
            errs.append("s10: %s sha16 %s != code %s (code-side edit)" % (path, h16, files[path][:16]))
        if os.path.getsize(os.path.join(subject, path)) != int(nbytes):
            errs.append("s10: %s bytes differ" % path)
    for path in sorted(set(files) - seen):
        errs.append("s10: code file %s missing from book" % path)
    # (c) every governed reference in the book resolves in live code
    pb = B.parse_book(text)
    sids = []
    for m in P3_RE.finditer(text):
        sids.append(("PM-P3-HEADER", m.group(1)))
    for ib in pb["inv"]:
        for it in ib["inv"] + ib["au"]:
            sids += [("hb.inv.v2 " + it["id"], s) for s in it["symbols"]]
    for cb in pb["claims"]:
        sids += [("hb.claim.v2 " + cb["id"], s) for s in cb["anchors"]]
    for sb in pb["sections"]:
        sids += [("hb.section.v2 " + sb["anchor"], s) for s in sb.get("code_anchors", [])]
    for where, sid in sids:
        if sid not in live:
            errs.append("%s -> %s: not a live symbol" % (where, sid))
    for m in REF_RE.finditer(_prose(text) + "\n" + "\n".join(m.group(2) for m in PM_RE.finditer(text))):
        path, l0, l1, sha = m.group(1), int(m.group(2)), int(m.group(3) or m.group(2)), m.group(4)
        if path not in files:
            errs.append("ref %s:%d: file not in code scope" % (path, l0))
            continue
        nl = max(1, open(os.path.join(subject, path), "rb").read().count(b"\n"))
        if l0 < 1 or l1 < l0 or l1 > nl + 1:
            errs.append("ref %s:%d-%d outside file (%d lines)" % (path, l0, l1, nl))
        if sha:
            hit = [a for a in b["anchors"] if a["path"] == path and a["span"]["start_line"] == l0 and a["span"]["end_line"] == l1]
            if not hit or not hit[0]["sha256_ast"].startswith(sha):
                errs.append("ref %s:%d-%d sha_ast %s disagrees with code" % (path, l0, l1, sha[:8]))
    return res("g_os_drift", errs, refs=len(sids))


def g_os_floor(root, subject, b, book):
    import hb_claim
    bd = _bd(root, book)
    bp = os.path.join(bd, "book.md")
    if not os.path.isfile(bp):
        return res("g_os_floor", ["missing book.md"])
    text = _read(bp)
    errs = []
    heads = P3_RE.findall(text)
    if not heads:
        errs.append("no PM-P3-HEADER found")
    for sid, body in heads:
        keys = tuple(re.findall(r"^# ([A-Z]+):", body, re.M))
        if keys != P3_FIELDS:
            errs.append("PM-P3-HEADER %s fields %s" % (sid, ",".join(keys)))
        for ln in body.splitlines():
            if not ln.split(":", 1)[1].strip():
                errs.append("PM-P3-HEADER %s empty field" % sid)
    pb = B.parse_book(text)
    if not pb["inv"]:
        errs.append("no hb.inv.v2 block")
    for ib in pb["inv"]:
        if len(ib["inv"]) < 2 or len(ib["au"]) < 1:
            errs.append("hb.inv.v2 %s: inv %d (>=2) au %d (>=1)" % (ib["module"], len(ib["inv"]), len(ib["au"])))
    srch = [json.loads(m.group(2)) for m in PM_RE.finditer(text) if m.group(1) == "pm.search.v1"]
    if not srch:
        errs.append("no pm.search.v1 block")
    for s in srch:
        for k in ("questions_answered", "engineering_tasks", "synonyms", "exclusions", "keywords", "retrieval_triggers"):
            if not s.get(k):
                errs.append("pm.search.v1 facet %s empty" % k)
    un = hb_claim.untagged_numbers(re.sub(r"```.*?```", " ", GEN_RE.sub(" ", text), flags=re.S))  # HB:GEN blocks only (ADC-W5A-01); fenced prose stays scanned
    if un:
        errs.append("untagged numbers: %s" % ", ".join(un[:5]))
    for c in pb["claims"]:
        if c["etag"] == "MEASURED":
            if not c["receipts"]:
                errs.append("claim %s MEASURED without receipts" % c["id"])
            for r in c["receipts"]:
                if not os.path.isfile(os.path.join(bd, "receipts", r + ".json")):
                    errs.append("claim %s receipt %s not on disk" % (c["id"], r))
    found = hb_claim.g_claim_book(GEN_RE.sub(" ", text), root, [c for c in pb["claims"] if "value" in c])
    errs += ["claim check %s" % f.get("code") for f in found]
    return res("g_os_floor", errs, headers=len(heads))


def g_os_backlink(root, subject, b, book):
    """g_backlink port for Go (the live g_backlink filters tools/ and is vacuous): every public governed Go symbol names >= 1 section in governs.sections."""
    sp = os.path.join(_bd(root, book), "symbols.jsonl")
    if not os.path.isfile(sp):
        return res("g_os_backlink", ["missing symbols.jsonl"])
    pub = [s for s in L.read_jsonl(sp) if B.is_public(s["id"]) and s["kind"] in ("function", "method", "class", "constant")]
    if not pub:
        return res("g_os_backlink", ["0 public governed Go symbols (vacuous)"])
    errs = ["public symbol %s has no governs.sections" % s["id"] for s in pub if not (s.get("governs") or {}).get("sections")]
    return res("g_os_backlink", errs[:20] + (["... %d more" % (len(errs) - 20)] if len(errs) > 20 else []), public_symbols=len(pub))


def g_os_privacy(root, subject, b, book):
    bd = _bd(root, book)
    errs, n = [], 0
    if not os.path.isfile(os.path.join(bd, "book.md")):
        return res("g_os_privacy", ["missing book.md"])
    for f in ("book.md", "symbols.jsonl", "nodes.jsonl", "claims.jsonl", "anchors.jsonl"):
        p = os.path.join(bd, f)
        if os.path.isfile(p):
            n += 1
            for m in PRIV_RE.finditer(_read(p)):
                errs.append("%s: %r" % (f, m.group(0)[:40]))
    return res("g_os_privacy", errs, scanned=n)


def g_os_rebuild(root, subject, b, book):
    bp = os.path.join(_bd(root, book), "book.md")
    if not os.path.isfile(bp):
        return res("g_os_rebuild", ["missing book.md"])
    try:
        _, d1 = B.build_all(root, write=False)
        _, d2 = B.build_all(root, write=False)
    except B.BookError as e:
        return res("g_os_rebuild", [str(e)])
    errs = []
    if d1["rendered_book"] != d2["rendered_book"] or L.canon(d1["symbols"]) != L.canon(d2["symbols"]):
        errs.append("two rebuilds differ")
    if d1["rendered_book"] != _read(bp):
        errs.append("stored book.md differs from a fresh rebuild")
    return res("g_os_rebuild", errs)


def live_nonvacuous(root, results):
    errs = []
    book = os.environ.get("HBV2_BOOK_SUB", "book")
    sp = os.path.join(root, book, "symbols.jsonl")
    pub = [s for s in L.read_jsonl(sp) if B.is_public(s["id"]) and s["kind"] != "module"] if os.path.isfile(sp) else []
    if not pub:
        errs.append("public_symbols == 0")
    return res("g_live_nonvacuous", errs, public_symbols=len(pub))


# pinned copy: optional hb_visual (PATCHES.md kind 4) and the vendored validator imported by hb_lib_bundle.py (681dc0b, hbtools_v2/vendor/)
ALLOWED_PIN_IMPORTS = ("hb_build.py imports non-stdlib hb_visual", "hb_lib_bundle.py imports non-stdlib vendor")


def g_os_stdlib(root, subject, b, book):
    """g_stdlib over the pinned hbtools_v2 (its only allowed finding is the optional hb_visual import, PATCHES.md kind 4) plus the OS tool dir."""
    import ast, hb_gates2 as G
    errs = [e for e in G.g_stdlib(root)["errors"] if e not in ALLOWED_PIN_IMPORTS]
    std = set(sys.stdlib_module_names)
    here = os.path.dirname(os.path.abspath(__file__))
    local = {f[:-3] for f in os.listdir(here) if f.endswith(".py")} | {f[:-3] for f in os.listdir(L.HERE) if f.endswith(".py")}
    n = 0
    for f in sorted(os.listdir(here)):
        if f.endswith(".py"):
            n += 1
            for node in ast.walk(ast.parse(open(os.path.join(here, f), encoding="utf-8").read())):
                mods = [a.name.split(".")[0] for a in node.names] if isinstance(node, ast.Import) else \
                    [(node.module or "").split(".")[0]] if isinstance(node, ast.ImportFrom) and not node.level else []
                errs += ["%s imports non-stdlib %s" % (f, m) for m in mods if m and m not in std and m not in local]
    return res("g_os_stdlib", errs, modules=n)


def run_os_gates(root, subject, b, book):
    out = []
    for g in (g_os_schema, g_os_anchor, g_os_drift, g_os_floor, g_os_backlink, g_os_privacy, g_os_rebuild, g_os_stdlib):
        try:
            out.append(g(root, subject, b, book))
        except Exception as e:
            out.append(res(g.__name__, ["gate crashed: %s: %s" % (type(e).__name__, str(e)[:160])]))
    return out
