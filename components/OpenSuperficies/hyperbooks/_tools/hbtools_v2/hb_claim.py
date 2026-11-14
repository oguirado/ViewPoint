#!/usr/bin/env python3
"""HB2-07: numeric claims checked against receipts with exact rationals (Fraction only; no float anywhere).

check_prose(text, ref, root)       -> hb.claim-check.v2 (source=prose)
check_structured(value, root)      -> hb.claim-check.v2 (source=structured), value = hb.claim.v2 `value`
g_claim_book(text, root, claims)   -> list of findings; any finding fails the gate (fail closed)
"""
import argparse, json, os, re, sys
from decimal import Decimal
from fractions import Fraction

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hblib2 as L  # noqa: E402

NUM_RE = re.compile(r"-?\d{1,3}(?:,\d{3})+(?:\.\d+)?|-?\d+(?:\.\d+)?")
REF_RE = re.compile(r"[A-Za-z0-9_./-]{1,200}\.json:[A-Za-z0-9_.-]{1,200}")
UNIT_WORDS = {"ms": "ms", "msec": "ms", "millisecond": "ms", "milliseconds": "ms", "s": "s", "sec": "s", "second": "s", "seconds": "s",
              "us": "us", "µs": "us", "microsecond": "us", "microseconds": "us", "ns": "ns", "nanosecond": "ns", "nanoseconds": "ns",
              "b": "B", "byte": "B", "bytes": "B", "kib": "KiB", "mib": "MiB", "%": "%", "percent": "%", "pct": "%"}
UNITS = ["ms", "s", "us", "ns", "B", "KiB", "MiB", "%", "count"]
# unit -> (family, factor to the family base unit as an exact Fraction)
FAMILY = {"ms": ("time", Fraction(1)), "s": ("time", Fraction(1000)), "us": ("time", Fraction(1, 1000)), "ns": ("time", Fraction(1, 10 ** 6)),
          "B": ("bytes", Fraction(1)), "KiB": ("bytes", Fraction(1024)), "MiB": ("bytes", Fraction(1024 * 1024)),
          "%": ("ratio", Fraction(1)), "count": ("count", Fraction(1))}
CMP = [(("at most", "no more than", "under", "below", "<="), "<="), (("less than", "<"), "<"),
       (("at least", "no fewer than", ">="), ">="), (("more than", "over", "above", ">"), ">"),
       (("about", "approximately", "around", "~"), "~")]
TAGS = ("[MEASURED", "[CITED", "[TARGET")
UNITNUM_RE = re.compile(r"(?<![A-Za-z0-9_.-])(-?\d{1,3}(?:,\d{3})+(?:\.\d+)?|-?\d+(?:\.\d+)?)\s*(%|ms|msec|milliseconds?|seconds?|sec|µs|us|ns|bytes?|B|KiB|MiB|percent)(?![A-Za-z])")
SHA_EMPTY = L.EMPTY_SHA256


def parse_number(s):
    """-> (Fraction, decimals) for a numeric literal with optional thousands commas."""
    t = s.replace(",", "")
    p = len(t.split(".")[1]) if "." in t else 0
    return Fraction(t), p


def parse_prose(text):
    """First number, unit and comparator of a claim sentence (receipt reference already removed)."""
    m = NUM_RE.search(text)
    if not m:
        return None
    num, p = parse_number(m.group(0))
    rest = text[m.end():]
    um = re.match(r"\s*(%|[A-Za-zµ]+)", rest)
    unit = "count"
    if um:
        w = um.group(1)
        unit = UNIT_WORDS.get(w) or UNIT_WORDS.get(w.lower()) or "count"
        if w in ("B", "ms", "s", "us", "ns", "KiB", "MiB"):
            unit = w
    before = text[max(0, m.start() - 24):m.start()].lower()
    op = "=="
    best = None
    for phrases, o in CMP:
        for ph in phrases:
            i = before.rfind(ph)
            if i >= 0 and (ph[0] in "<>~=" or re.search(r"(?<![a-z])" + re.escape(ph) + r"(?![a-z])", before)):
                if best is None:
                    best = (o, i)
                break
        if best:
            break
    if best:
        op = best[0]
    return {"number": num, "decimals": p, "unit": unit, "op": op}


def dec_str(fr, places=None):
    """Exact decimal string of a Fraction whose denominator divides a power of ten (else rounded to `places`)."""
    fr = Fraction(fr)
    d = fr.denominator
    k = 0
    while d % 10 == 0:
        d //= 10
        k += 1
    n2 = n5 = 0
    t = d
    while t % 2 == 0:
        t //= 2
        n2 += 1
    t2 = t
    while t2 % 5 == 0:
        t2 //= 5
        n5 += 1
    if t2 == 1:
        pl = k + max(n2, n5)
    else:
        pl = places if places is not None else 6
    return L.fmt_dec(fr, pl)


# ---- receipts ---------------------------------------------------------------------------------------
def walk(doc, path):
    for tok in path.split("."):
        if isinstance(doc, list):
            if not tok.isdigit() or int(tok) >= len(doc):
                raise KeyError(tok)
            doc = doc[int(tok)]
        elif isinstance(doc, dict):
            if tok not in doc:
                raise KeyError(tok)
            doc = doc[tok]
        else:
            raise KeyError(tok)
    return doc


def receipt_unit(path, sibling_unit):
    last = path.split(".")[-1].lower()
    cands = [last] + last.split("_")[-1:]
    for c in cands:
        if c in UNIT_WORDS:
            return UNIT_WORDS[c]
    if isinstance(sibling_unit, str) and sibling_unit.lower() in UNIT_WORDS:
        return UNIT_WORDS[sibling_unit.lower()]
    return None


def load_receipt(root, ref, registry=None):
    """-> (outcome, value Fraction|None, unit|None, file_sha256|None)."""
    fn, _, path = ref.partition(":")
    p = os.path.join(root, fn)
    try:
        raw = L.readb(p)
    except OSError:
        return "KeyMiss", None, None, None
    sha = L.sha256b(raw)
    if sha == SHA_EMPTY or not raw.strip():
        return "ForgedEmpty", None, None, sha
    if registry and fn in registry and registry[fn] != sha:
        return "DigestMismatch", None, None, sha
    try:
        doc = json.loads(raw.decode("utf-8"), parse_float=Decimal)
    except ValueError:
        return "KeyMiss", None, None, sha
    try:
        v = walk(doc, path)
    except KeyError:
        return "KeyMiss", None, None, sha
    if isinstance(v, bool) or not isinstance(v, (int, Decimal)):
        return "NotNumber", None, None, sha
    sib = None
    parent = path.rsplit(".", 1)[0] if "." in path else ""
    try:
        par = walk(doc, parent) if parent else doc
        if isinstance(par, dict):
            sib = par.get("unit")
    except KeyError:
        pass
    return "Hit", Fraction(v), receipt_unit(path, sib), sha


def convert(value, from_unit, to_unit):
    """Exact conversion of `value` from receipt unit to claim unit; None when the unit families differ."""
    ff, fk = FAMILY[from_unit]
    tf, tk = FAMILY[to_unit]
    if ff == tf:
        return value * fk / tk
    return None


def compare(v, c, op, p):
    """Exact verdict of receipt value v against the claim (op, c, p)."""
    if op == "<=":
        return v <= c
    if op == "<":
        return v < c
    if op == ">=":
        return v >= c
    if op == ">":
        return v > c
    if op == "~":
        return abs(v - c) <= abs(c) / 20
    if p == 0:
        return v == c
    return abs(v - c) <= Fraction(1, 10 ** p) / 2


def tolerance_str(op, c, p):
    if op == "~":
        t = abs(c) / 20
    elif op == "==":
        t = Fraction(0) if p == 0 else Fraction(1, 10 ** p) / 2
    else:
        t = Fraction(0)
    return "%d/%d" % (t.numerator, t.denominator)


def evaluate(parsed, ref, root, claim_text, source, registry=None):
    outcome, val, runit, sha = load_receipt(root, ref, registry)
    rec = {"ref": L.clip_bytes(ref, 402), "outcome": outcome}
    if sha:
        rec["file_sha256"] = sha
    verdict, code = "unverifiable", "E_RECEIPT_KEY"
    if outcome == "Hit":
        key = ref.split(":", 1)[1]
        explicit = runit is not None
        runit = runit or parsed["unit"]                      # declared assumption: receipt unit = claim unit
        cands = []
        if runit in FAMILY and parsed["unit"] in FAMILY:
            conv = convert(val, runit, parsed["unit"])
            if conv is not None:
                cands.append(conv)
                if parsed["unit"] == "%" and not explicit and abs(val) <= 1 and "%" not in key and "pct" not in key:
                    cands.append(conv * 100)                  # ratio receipt vs percent claim (value <= 1, key lacks %/pct)
        if cands:
            rec["value"] = dec_str(cands[0])
            rec["unit"] = parsed["unit"]
            ok = any(compare(c, parsed["number"], parsed["op"], parsed["decimals"]) for c in cands)
            verdict, code = ("supported", "OK") if ok else ("contradicted", "E_REFUTED_CLAIM")
    return {"schema": "hb.claim-check.v2", "claim": L.clip_bytes(claim_text, 160), "source": source,
            "parsed": {"number": L.fmt_dec(parsed["number"], parsed["decimals"]), "decimals": parsed["decimals"], "unit": parsed["unit"], "op": parsed["op"]},
            "receipt": rec, "tolerance": tolerance_str(parsed["op"], parsed["number"], parsed["decimals"]), "verdict": verdict, "code": code}


def split_ref(text):
    """Separate the receipt reference `file.json:dot.path` from the claim sentence."""
    m = REF_RE.search(text)
    if not m:
        return text, None
    return (text[:m.start()] + " " + text[m.end():]).strip(), m.group(0)


def check_prose(text, ref=None, root=None, registry=None):
    claim, r2 = split_ref(text)
    ref = ref or r2
    parsed = parse_prose(claim)
    if parsed is None:
        return None
    if not ref:
        return {"schema": "hb.claim-check.v2", "claim": L.clip_bytes(claim, 160), "source": "prose",
                "parsed": {"number": L.fmt_dec(parsed["number"], parsed["decimals"]), "decimals": parsed["decimals"], "unit": parsed["unit"], "op": parsed["op"]},
                "receipt": {"ref": "", "outcome": "KeyMiss"}, "verdict": "unverifiable", "code": "E_RECEIPT_KEY"}
    return evaluate(parsed, ref, root, claim, "prose", registry)


def check_structured(value, root, claim_text="", registry=None):
    num, p = parse_number(value["number"])
    parsed = {"number": num, "decimals": p, "unit": value["unit"], "op": value["op"]}
    return evaluate(parsed, value["metric_ref"], root, claim_text or value["metric_ref"], "structured", registry)


def untagged_numbers(text):
    """Unit-bearing numbers of a prose block that carry no [MEASURED]/[CITED]/[TARGET] tag within 60 bytes after them."""
    out = []
    for m in UNITNUM_RE.finditer(text):
        tail = text[m.end():m.end() + 80]
        if not any(t in tail for t in TAGS):
            out.append(m.group(0))
    return out


TAGGED_RE = re.compile(r"(?P<sent>" + UNITNUM_RE.pattern + r")\s*\[MEASURED\s+(?P<ref>[A-Za-z0-9_./-]+\.json:[A-Za-z0-9_.-]+)\]")


def g_claim_book(text, root, structured=(), registry=None):
    """Findings for a whole book: structured claims, receipt-bound tagged numbers, untagged unit-bearing numbers."""
    f = []
    for v in structured:
        c = check_structured(v["value"], root, v.get("statement", ""), registry)
        if c["code"] != "OK":
            f.append({"code": c["code"], "claim": c["claim"], "verdict": c["verdict"]})
    for m in TAGGED_RE.finditer(text):
        c = check_prose(m.group("sent"), m.group("ref"), root, registry)
        if c and c["code"] != "OK":
            f.append({"code": c["code"], "claim": c["claim"], "verdict": c["verdict"]})
    stripped = re.sub(r"```.*?```", " ", text, flags=re.S)
    for u in untagged_numbers(stripped):
        f.append({"code": "E_UNTAGGED_NUMBER", "claim": u, "verdict": "unverifiable"})
    return f


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=L.SUBJECT)
    ap.add_argument("--text", required=True)
    ap.add_argument("--ref")
    a = ap.parse_args(argv)
    c = check_prose(a.text, a.ref, a.root)
    sys.stdout.write(L.dumps(c) + "\n")
    return 0 if c and c["code"] == "OK" else 5


if __name__ == "__main__":
    sys.exit(main())
