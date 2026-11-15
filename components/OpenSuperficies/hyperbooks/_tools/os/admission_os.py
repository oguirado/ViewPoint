#!/usr/bin/env python3
"""OS admission port (F-B12 lockstep over the OS overlay). Stdlib only.

  admission_os.py [--format hyperbooks/_tools/os/OS_FORMAT.json] [--root hyperbooks]
Exit: 0 admitted, 2 usage, 5 findings. Never 1.
"""
import json, os, re, sys

HERE = os.path.dirname(os.path.abspath(__file__))
PROFILE = "hb-hybrid/v1"


def _load(path):
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else None
    except (OSError, ValueError):
        return None


def check_format(d):
    if d is None:
        return ["format: overlay missing or malformed"]
    out = []
    if d.get("schema") != "hb.companion-format/v1":
        out.append("format: schema")
    if d.get("reference_conformance") != "TARGET":
        out.append("format: reference_conformance must be TARGET")
    for k in ("required_per_book", "expected_books"):
        v = d.get(k)
        if not isinstance(v, list) or not v or len(set(v)) != len(v) or any(not isinstance(x, str) or os.path.basename(x) != x for x in v):
            out.append("format: invalid " + k)
    for b, p in sorted((d.get("profiles") or {}).items()):
        if p != PROFILE:
            out.append("format: profile of %s must be %s" % (b, PROFILE))
        if b not in (d.get("expected_books") or []):
            out.append("format: profiled book %s not in expected_books" % b)
    return out


def check_book(d, root, book):
    bd = os.path.join(root, book)
    if not os.path.isdir(bd):
        return ["%s: expected book directory missing" % book]
    out = []
    for name in d["required_per_book"]:
        p = os.path.join(bd, name)
        if not os.path.isfile(p):
            out.append("%s: %s missing" % (book, name))
            continue
        text = open(p, encoding="utf-8").read()
        try:
            if name == "book.md":
                if not re.match(r"# HB-OS-[A-Za-z0-9-]+(?: |$)", text) or any('id="hb-s%d"' % i not in text for i in range(11)):
                    raise ValueError
            elif name.endswith(".jsonl"):
                for ln in text.splitlines():
                    if ln.strip() and not isinstance(json.loads(ln), dict):
                        raise ValueError
        except ValueError:
            out.append("%s: %s malformed" % (book, name))
    return out


def check_companions(d, root):
    """F-B12 lockstep: a profiled book carries the generated set non-empty with GEN fences; an unprofiled one carries none."""
    out = []
    prof = d.get("profiles") or {}
    spec = (d.get("profile_artifacts") or {}).get(PROFILE) or {}
    for book in d.get("expected_books") or []:
        bd = os.path.join(root, book)
        if not os.path.isdir(bd):
            continue
        names = list(spec.get("required", []))
        if book in prof:
            for n in names:
                p = os.path.join(bd, n)
                if not os.path.isfile(p) or os.path.getsize(p) == 0:
                    out.append("%s: profile %s requires %s" % (book, PROFILE, n))
            for n in spec.get("never", []):
                if os.path.exists(os.path.join(bd, n)):
                    out.append("%s: %s must not exist" % (book, n))
            bp = os.path.join(bd, "book.md")
            if os.path.isfile(bp):
                t = open(bp, encoding="utf-8").read()
                for i in (8, 9, 10):
                    if "<!-- HB:GEN BEGIN hb-s%d -->" % i not in t or "<!-- HB:GEN END hb-s%d -->" % i not in t:
                        out.append("%s: GEN fence hb-s%d missing" % (book, i))
        else:
            for n in names:
                if os.path.exists(os.path.join(bd, n)):
                    out.append("%s: companion file %s present but the book has no profile" % (book, n))
    return out


def check_receipts(d, root):
    out = []
    inv = d.get("receipt_inventory") or {}
    for book in d.get("expected_books") or []:
        rd = os.path.join(root, book, "receipts")
        on = sorted(os.listdir(rd)) if os.path.isdir(rd) else []
        decl = sorted(inv.get(book, []))
        for f in sorted(set(on) - set(decl)):
            out.append("%s: undeclared receipt %s" % (book, f))
        for f in sorted(set(decl) - set(on)):
            out.append("%s: declared receipt %s missing" % (book, f))
        for f in on:
            r = _load(os.path.join(rd, f))
            if r is None or r.get("id") != f[:-5]:
                out.append("%s: receipt %s id must equal file stem" % (book, f))
    return out


def run(fmt, root):
    d = _load(fmt)
    out = check_format(d)
    if out:
        return out
    for b in d["expected_books"]:
        out += check_book(d, root, b)
    return out + check_companions(d, root) + check_receipts(d, root)


def main(argv):
    a = dict(zip(argv[::2], argv[1::2]))
    if len(argv) % 2 or set(a) - {"--format", "--root"}:
        sys.stderr.write(__doc__)
        return 2
    out = run(a.get("--format", os.path.join(HERE, "OS_FORMAT.json")), a.get("--root", "hyperbooks"))
    for m in out:
        sys.stderr.write(m + "\n")
    print(json.dumps({"findings": len(out)}))
    return 5 if out else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
