"""HB-VC companion-corpus admission checks (stdlib only). Each check returns a list of defect strings.

Rules (JDOCS-1..4, JW5-2..7): format declared; receipts honest (commit => tree_clean, runner and sources exist,
source hashes always match); mathlinks rows sealed or explicitly AUSENTE; prose absence claims
match the tree. Profiles (F-B12, HB_DECISIONS 2.3): a book named in COMPANION_FORMAT.json "profiles" must carry the
generated companion set of its profile; every other book must carry none of those files.
"""
import glob
import hashlib
import json
import os
import re
import subprocess

RECEIPT_KEYS = ("schema", "id", "kind", "argv", "cwd", "commit", "tree_clean", "sources", "exit_code",
                "output_sha256", "metrics")
MATHLINK_SCHEMA = "hb.mathlink.v1"
BOOK_PREFIXES = ("HB-VC-", "HB-VN-")
DEFAULT_PROFILE = "hb-hybrid/v1"
PROFILE_REQUIRED = ["manifest.json", "aliases.json", "symbols.jsonl", "anchors.jsonl", "graph.json", "nodes.jsonl",
                    "claims.jsonl", "index/cols.json", "index/post.json", "index/vocab.json", "index/ID"]
PROFILE_NEVER = ["triptych.json", "imports.json"]


def _decl(hb_root):
    try:
        with open(os.path.join(hb_root, "COMPANION_FORMAT.json"), encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def profiles_of(hb_root):
    """book -> profile id from COMPANION_FORMAT.json (empty when undeclared or malformed)."""
    p = _decl(hb_root).get("profiles")
    return dict(p) if isinstance(p, dict) else {}


def profile_required(hb_root, profile):
    pa = _decl(hb_root).get("profile_artifacts")
    req = pa.get(profile, {}).get("required") if isinstance(pa, dict) and isinstance(pa.get(profile), dict) else None
    return list(req) if isinstance(req, list) and req else list(PROFILE_REQUIRED)


def sha256_file(p):
    with open(p, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def _dirty(repo):
    try:
        out = subprocess.run(["git", "status", "--porcelain", "-uall"], cwd=repo, capture_output=True,
                             text=True, timeout=60).stdout
    except Exception:
        return set()
    return {ln[3:].strip() for ln in out.splitlines() if len(ln) > 3}


def check_format(hb_root):
    p = os.path.join(hb_root, "COMPANION_FORMAT.json")
    if not os.path.isfile(p):
        return ["format: COMPANION_FORMAT.json missing"]
    try:
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
    except ValueError:
        return ["format: COMPANION_FORMAT.json malformed"]
    out = []
    if d.get("schema") != "hb.companion-format/v1":
        out.append("format: schema")
    if d.get("reference_conformance") != "TARGET":
        out.append("format: reference_conformance must be TARGET")
    for k in ("required_per_book", "reference_artifacts_absent"):
        if not isinstance(d.get(k), list) or not d[k]:
            out.append("format: %s" % k)
    prof = d.get("profiles", {})
    if not isinstance(prof, dict):
        out.append("format: profiles must be an object")
    else:
        for b, v in sorted(prof.items()):
            if v != DEFAULT_PROFILE:
                out.append("format: profile of %s must be %s" % (b, DEFAULT_PROFILE))
            if isinstance(d.get("expected_books"), list) and b not in d["expected_books"]:
                out.append("format: profiled book %s not in expected_books" % b)
    return out



def check_required(hb_root):
    """Enforce the declared inventory before admitting content or receipts."""
    try:
        with open(os.path.join(hb_root, "COMPANION_FORMAT.json"), encoding="utf-8") as f:
            declaration = json.load(f)
    except (OSError, ValueError):
        return ["format: COMPANION_FORMAT.json unreadable"]
    books = declaration.get("expected_books")
    required = declaration.get("required_per_book")
    out = []
    for key, values in (("expected_books", books), ("required_per_book", required)):
        if (not isinstance(values, list) or not values
                or any(not isinstance(v, str) or not v or os.path.basename(v) != v
                       or v in (".", "..") for v in values)
                or len(set(values)) != len(values)):
            out.append("format: invalid " + key)
    if out:
        return out
    actual = {b for b in os.listdir(hb_root)
              if b.startswith(BOOK_PREFIXES) and os.path.isdir(os.path.join(hb_root, b))}
    for book in sorted(set(books) | actual):
        bd = os.path.join(hb_root, book)
        if not os.path.isdir(bd):
            out.append("%s: expected book directory missing" % book)
            continue
        for name in required:
            path = os.path.join(bd, name)
            if not os.path.isfile(path):
                out.append("%s: %s missing" % (book, name))
                continue
            try:
                with open(path, encoding="utf-8") as f:
                    text = f.read()
                if name == "book.md":
                    if not re.match(r"# HB-V[CN]-[A-Za-z0-9-]+(?: |$)", text) or any(
                            'id="hb-s%d"' % i not in text for i in range(11)):
                        raise ValueError("missing book identity or section anchors")
                elif name.endswith(".jsonl"):
                    for line in text.splitlines():
                        if line.strip() and not isinstance(json.loads(line), dict):
                            raise ValueError("row must be an object")
                elif name.endswith(".json"):
                    if not isinstance(json.loads(text), dict):
                        raise ValueError("artifact must be an object")
                elif not text.strip():
                    raise ValueError("empty artifact")
            except (OSError, UnicodeError, ValueError):
                out.append("%s: %s malformed" % (book, name))
    return out


def check_companions(hb_root):
    """F-B12 lockstep: a profiled book carries the generated set (non-empty, GEN fences present); any other book carries none."""
    decl = _decl(hb_root)
    books = decl.get("expected_books")
    if not isinstance(books, list):
        return []
    profiles = profiles_of(hb_root)
    forbidden = set(decl.get("reference_artifacts_absent") or []) | set(PROFILE_REQUIRED) | set(PROFILE_NEVER)
    out = []
    for book in sorted(books):
        bd = os.path.join(hb_root, book)
        if not os.path.isdir(bd):
            continue
        if book in profiles:
            req = profile_required(hb_root, profiles[book])
            for name in req:
                path = os.path.join(bd, name)
                if not os.path.isfile(path) or os.path.getsize(path) == 0:
                    out.append("%s: profile %s requires %s" % (book, profiles[book], name))
                elif name.endswith((".json", ".jsonl")):
                    try:
                        with open(path, encoding="utf-8") as f:
                            if name.endswith(".jsonl"):
                                for ln in f:
                                    if ln.strip():
                                        json.loads(ln)
                            else:
                                json.load(f)
                    except ValueError:
                        out.append("%s: %s malformed" % (book, name))
            for name in PROFILE_NEVER:
                if os.path.exists(os.path.join(bd, name)):
                    out.append("%s: %s is not produced by profile %s" % (book, name, profiles[book]))
            bp = os.path.join(bd, "book.md")
            if os.path.isfile(bp):
                with open(bp, encoding="utf-8") as f:
                    text = f.read()
                for n in (8, 9, 10):
                    if "<!-- HB:GEN BEGIN hb-s%d -->" % n not in text or "<!-- HB:GEN END hb-s%d -->" % n not in text:
                        out.append("%s: generated fence hb-s%d missing" % (book, n))
        else:
            for name in sorted(forbidden):
                if os.path.exists(os.path.join(bd, name)):
                    out.append("%s: companion file %s present but the book has no profile" % (book, name))
    return out


def _sources(rc):
    s = rc.get("sources")
    if isinstance(s, list):
        return {e.get("path"): e.get("sha256") for e in s}
    return dict(s) if isinstance(s, dict) else {}


def _waivers(repo):
    p = os.path.join(repo, "hyperbooks", "COMPANION_FORMAT.json")
    try:
        with open(p, encoding="utf-8") as f:
            w = json.load(f).get("waivers", {})
        return w if isinstance(w, dict) else {}
    except (OSError, ValueError):
        return {}


def check_receipt(path, repo, dirty=None):
    out = []
    name = os.path.basename(path)
    try:
        with open(path, encoding="utf-8") as f:
            rc = json.load(f)
    except ValueError:
        return ["%s: malformed JSON" % name]
    for k in RECEIPT_KEYS:
        if k not in rc:
            out.append("%s: missing key %s" % (name, k))
    if out:
        return out
    if rc["commit"] is not None and rc["tree_clean"] is not True:
        out.append("%s: commit set with tree_clean=%r" % (name, rc["tree_clean"]))
    if rc["exit_code"] != 0 and not _waivers(repo).get(rc["id"]):
        out.append("%s: exit_code %r without waiver in COMPANION_FORMAT.json" % (name, rc["exit_code"]))
    for a in rc["argv"]:
        if isinstance(a, str) and re.fullmatch(r"[\w./-]+\.py", a) and not os.path.isfile(os.path.join(repo, a)):
            out.append("%s: runner %s absent" % (name, a))
    for p, h in sorted(_sources(rc).items()):
        fp = os.path.join(repo, p)
        if not os.path.isfile(fp):
            out.append("%s: source %s absent" % (name, p))
        elif sha256_file(fp) != h:
            out.append("%s: source %s hash drift" % (name, p))
    return out


def check_receipts(hb_root, repo):
    out = []
    for dp, _dn, fn in sorted(os.walk(hb_root)):
        if os.path.basename(dp) == "receipts":
            for f in sorted(fn):
                if f.endswith(".json"):
                    out += check_receipt(os.path.join(dp, f), repo)
    return out


def check_mathlink_row(row, repo, where="row"):
    if row.get("schema") != MATHLINK_SCHEMA:
        return ["%s: schema" % where]
    st = row.get("status")
    if st == "AUSENTE":
        if row.get("quote") or row.get("quote_sha256") or row.get("target_path"):
            return ["%s: AUSENTE row must not carry target/quote" % where]
        return [] if row.get("target_id") and row.get("claimed_location") else ["%s: target_id/claimed_location" % where]
    if st != "SEALED":
        return ["%s: status %r" % (where, st)]
    out = []
    for k in ("target_id", "target_path", "target_anchor", "quote", "quote_sha256"):
        if not row.get(k):
            out.append("%s: missing %s" % (where, k))
    if out:
        return out
    tp = os.path.join(repo, row["target_path"]) if not os.path.isabs(row["target_path"]) else row["target_path"]
    if not os.path.isfile(tp):
        return ["%s: target %s absent" % (where, row["target_path"])]
    with open(tp, encoding="utf-8", errors="replace") as f:
        text = f.read()
    if row["target_anchor"] not in text:
        out.append("%s: anchor not in target" % where)
    if row["quote"] not in text:
        out.append("%s: quote not in target" % where)
    if hashlib.sha256(row["quote"].encode()).hexdigest() != row["quote_sha256"]:
        out.append("%s: quote hash mismatch" % where)
    return out


def check_mathlinks(hb_root, repo):
    out = []
    for b in sorted(os.listdir(hb_root)):
        bd = os.path.join(hb_root, b)
        if not (os.path.isdir(bd) and b.startswith(BOOK_PREFIXES)):
            continue
        mp = os.path.join(bd, "mathlinks.jsonl")
        if not os.path.isfile(mp):
            out.append("%s: mathlinks.jsonl missing" % b)
            continue
        with open(mp, encoding="utf-8") as f:
            lines = f.readlines()
        for i, ln in enumerate(lines, 1):
            if ln.strip():
                try:
                    out += check_mathlink_row(json.loads(ln), repo, "%s:%d" % (b, i))
                except ValueError:
                    out.append("%s:%d malformed" % (b, i))
    return out


_ABS = re.compile(r"`([\w./-]+)`[^.\n|;]{0,60}?AUSENTE")


def check_prose(hb_root, repo):
    """A book must not call an existing path AUSENTE."""
    out = []
    for b in sorted(os.listdir(hb_root)):
        bp = os.path.join(hb_root, b, "book.md")
        if not os.path.isfile(bp):
            continue
        with open(bp, encoding="utf-8") as f:
            lines = f.readlines()
        for i, ln in enumerate(lines, 1):
            for m in _ABS.finditer(ln):
                if os.path.exists(os.path.join(repo, m.group(1))):
                    out.append("%s:%d: %s declared AUSENTE but exists" % (b, i, m.group(1)))
    return out


_PIN = re.compile(r"`([\w./-]+\.(?:py|json|md|tsv))`[^`\n]{0,40}`([0-9a-f]{64})`")


def check_pins(hb_root, repo):
    """sha256 values pinned in book prose next to a repo path must match the current file bytes."""
    out = []
    for b in sorted(os.listdir(hb_root)):
        bp = os.path.join(hb_root, b, "book.md")
        if not os.path.isfile(bp):
            continue
        with open(bp, encoding="utf-8") as f:
            lines = f.readlines()
        for i, ln in enumerate(lines, 1):
            for m in _PIN.finditer(ln):
                fp = os.path.join(repo, m.group(1))
                if not os.path.isfile(fp):
                    out.append("%s:%d: pinned path %s absent" % (b, i, m.group(1)))
                elif sha256_file(fp) != m.group(2):
                    out.append("%s:%d: pinned sha of %s drifted" % (b, i, m.group(1)))
    return out


_RID = re.compile(r"R-[a-z0-9]+(?:-[a-z0-9]+)+")


def check_claims(hb_root):
    """Test counts and `rc 0` claims written next to a receipt id must match that receipt."""
    out = []
    for b in sorted(os.listdir(hb_root)):
        bd = os.path.join(hb_root, b)
        bp = os.path.join(bd, "book.md")
        if not os.path.isfile(bp):
            continue
        rcs = {}
        for f in glob.glob(os.path.join(bd, "receipts", "*.json")):
            with open(f, encoding="utf-8") as fh:
                r = json.load(fh)
            rcs[r.get("id")] = r
        with open(bp, encoding="utf-8") as fh:
            lines = fh.readlines()
        for i, ln in enumerate(lines, 1):
            ms = list(_RID.finditer(ln))
            for k, m in enumerate(ms):
                r = rcs.get(m.group(0))
                if r is None:
                    continue
                end = ms[k + 1].start() if k + 1 < len(ms) else len(ln)
                seg = ln[m.end():min(end, m.end() + 50)]
                mt = re.search(r"\((?:rc \d, )?([\d,]+) tests", seg) or re.search(r"= (\d+) tests", ln[max(0, m.start() - 30):m.start()] + seg)
                n = r.get("metrics", {}).get("tests_run")
                if mt and n is not None and int(mt.group(1).replace(",", "")) != n:
                    out.append("%s:%d: %s says %s tests, receipt has %s" % (b, i, m.group(0), mt.group(1), n))
                if re.search(r"\(rc 0\b", seg) and r.get("exit_code") != 0:
                    out.append("%s:%d: %s says rc 0, receipt exit_code %r" % (b, i, m.group(0), r.get("exit_code")))
    return out


def check_receipt_inventory(hb_root):
    """Receipt completeness (JD2-1): the declared inventory must exist exactly, ids unique, prose refs resolve."""
    try:
        with open(os.path.join(hb_root, "COMPANION_FORMAT.json"), encoding="utf-8") as f:
            decl = json.load(f)
    except (OSError, ValueError):
        return ["inventory: COMPANION_FORMAT.json unreadable"]
    inv = decl.get("receipt_inventory")
    wd = decl.get("withdrawn_receipts", {})
    books = decl.get("expected_books")
    if not isinstance(inv, dict) or not inv or not isinstance(books, list):
        return ["inventory: receipt_inventory missing or invalid"]
    out = []
    if not isinstance(wd, dict) or any(not isinstance(k, str) or not isinstance(v, str) or not v.strip()
                                       for k, v in wd.items()):
        return ["inventory: withdrawn_receipts must map id to a non-empty reason"]
    if set(inv) != set(books):
        out.append("inventory: receipt_inventory books %s != expected_books" % sorted(set(inv) ^ set(books)))
    ids = {}
    for book in sorted(inv):
        names = inv[book]
        if (not isinstance(names, list) or not names
                or any(not isinstance(n, str) or os.path.basename(n) != n or not n.endswith(".json") for n in names)
                or len(set(names)) != len(names)):
            out.append("%s: invalid receipt inventory" % book)
            continue
        rd = os.path.join(hb_root, book, "receipts")
        if not os.path.isdir(rd):
            out.append("%s: receipts directory missing" % book)
            continue
        for n in names:
            fp = os.path.join(rd, n)
            if not os.path.isfile(fp):
                out.append("%s: declared receipt %s missing" % (book, n))
                continue
            try:
                with open(fp, encoding="utf-8") as f:
                    rid = json.load(f).get("id")
            except (OSError, ValueError, AttributeError):
                out.append("%s: receipt %s malformed" % (book, n))
                continue
            if rid != n[:-5]:
                out.append("%s: receipt %s id %r != file stem" % (book, n, rid))
            elif rid in ids:
                out.append("%s: receipt id %s duplicated (also %s)" % (book, rid, ids[rid]))
            elif rid in wd:
                out.append("%s: receipt id %s is declared withdrawn" % (book, rid))
            else:
                ids[rid] = book
        for n in sorted(f for f in os.listdir(rd) if f.endswith(".json") and f not in names):
            out.append("%s: receipt %s not declared in inventory" % (book, n))
    for book in sorted(inv):
        bp = os.path.join(hb_root, book, "book.md")
        if not os.path.isfile(bp):
            continue
        with open(bp, encoding="utf-8") as f:
            lines = f.readlines()
        for i, ln in enumerate(lines, 1):
            for m in _RID.finditer(ln):
                if m.group(0) not in ids and m.group(0) not in wd:
                    out.append("%s:%d: receipt %s referenced but not in inventory" % (book, i, m.group(0)))
    return out


def check_corpus(hb_root, repo):
    structural = check_format(hb_root) + check_required(hb_root) + check_receipt_inventory(hb_root) + check_companions(hb_root)
    if structural:
        return structural
    return (check_receipts(hb_root, repo) + check_mathlinks(hb_root, repo)
            + check_prose(hb_root, repo)
            + check_claims(hb_root) + check_pins(hb_root, repo))
