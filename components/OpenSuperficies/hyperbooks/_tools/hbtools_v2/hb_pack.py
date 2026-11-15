#!/usr/bin/env python3
"""HB2-11: UTF-8 byte law, sub-anchoring of symbols > 1,800 B, suffix-first answer packer.

Every budget is the UTF-8 length of the exact serialisation + LF. The packer never truncates: it demotes (a -> b -> g -> drop,
last item first, one level across the whole suffix before the next level) and re-checks the real serialisation; if even the
empty envelope does not fit it refuses (`E_BUDGET`).
"""
import ast, sys, os

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hblib2 as L  # noqa: E402

SYM_MAX, SKEL_MAX = L.SYM_MAX, L.SKEL_MAX


class BudgetError(Exception):
    """E_BUDGET: the content cannot fit the byte budget even fully demoted (never truncated)."""
    code = "E_BUDGET"


def utf8_boundary(b, i):
    """Largest j <= i such that b[j] is not a UTF-8 continuation byte (a code-point boundary)."""
    j = min(i, len(b))
    while 0 < j < len(b) and (b[j] & 0xC0) == 0x80:
        j -= 1
    return j


def split_piece(b, cap=SYM_MAX):
    """Split one oversized segment: at the last LF <= cap, else at cap on a UTF-8 boundary. Returns list of byte pieces <= cap."""
    out = []
    while len(b) > cap:
        k = b.rfind(b"\n", 0, cap)          # LF index < cap -> piece b[:k+1] has length <= cap
        cut = k + 1 if k >= 0 else utf8_boundary(b, cap)
        if cut <= 0:
            cut = utf8_boundary(b, cap) or cap
        out.append(b[:cut])
        b = b[cut:]
    if b:
        out.append(b)
    return out


def _parse(text):
    """Parse the symbol text (module-level or indented). Returns (node, line_shift)."""
    s = text.decode("utf-8")
    if s[:1] in (" ", "\t"):
        t = ast.parse("if 1:\n" + s)
        return t.body[0].body[0], 1
    return ast.parse(s).body[0], 0


def segments(text):
    """Consecutive byte segments of the symbol body, split at top-level statement starts. Returns (header_bytes, [segments])."""
    node, shift = _parse(text)
    lines = text.split(b"\n")
    offs = [0]
    for ln in lines[:-1]:
        offs.append(offs[-1] + len(ln) + 1)
    body = list(getattr(node, "body", []))
    if not body:
        return b"", [text]
    first = min([body[0].lineno] + [d.lineno for d in getattr(body[0], "decorator_list", [])]) - shift
    if first <= (node.lineno - shift):
        return b"", [text]                  # body starts on the header line: no statement boundary to use
    starts = []
    for st in body:
        ln = min([st.lineno] + [d.lineno for d in getattr(st, "decorator_list", [])]) - shift
        starts.append(offs[ln - 1])
    hdr_end = starts[0]
    bounds = starts + [len(text)]
    segs = [text[bounds[i]:bounds[i + 1]] for i in range(len(starts))]
    return text[:hdr_end], segs


def children_of(text, cap=SYM_MAX):
    """Greedy packing of statement segments into children <= cap; oversize statements are split first (D-HB2-11)."""
    hdr, segs = segments(text)
    items = []
    for s in segs:
        items += split_piece(s, cap) if len(s) > cap else [s]
    out, cur = [], b""
    for it in items:
        if cur and len(cur) + len(it) > cap:
            out.append(cur)
            cur = b""
        cur += it
    if cur:
        out.append(cur)
    return hdr, out


def subanchor(sid, text, start_byte=0, start_line=1, cap=SYM_MAX):
    """Sub-anchoring of one symbol. text = exact symbol bytes. Returns None for symbols <= cap, else
    {"parent": {...skeleton...}, "children": [{id, start_byte, end_byte, bytes, sha256, stmt_sha8, start_line, end_line}]}.
    Concatenation of the header and all children equals the symbol text (sha256 equality is asserted)."""
    if len(text) <= cap:
        return None
    hdr, kids = children_of(text, cap)
    assert hdr + b"".join(kids) == text
    pos = start_byte + len(hdr)
    ln = start_line + hdr.count(b"\n")
    rows = []
    for k, c in enumerate(kids, 1):
        n_l = c.count(b"\n")
        rows.append({"id": "%s#b%d" % (sid, k), "start_byte": pos, "end_byte": pos + len(c), "bytes": len(c),
                     "sha256": L.sha256b(c), "stmt_sha8": L.sha256b(c)[:8], "start_line": ln, "end_line": ln + max(0, n_l - (1 if c.endswith(b"\n") else 0))})
        pos += len(c)
        ln += n_l
    doc1 = ""
    try:
        node, _ = _parse(text)
        d = ast.get_docstring(node)
        doc1 = d.strip().split("\n")[0] if d else ""
    except SyntaxError:
        pass
    return {"parent": skeleton(sid, hdr, doc1, rows), "children": rows, "header_bytes": len(hdr),
            "body_sha256": L.sha256b(b"".join(kids))}


def skeleton(sid, hdr, doc1, rows):
    sig = hdr.decode("utf-8", "replace").strip().split("\n")[0]
    table = ["#b%d L%d-%d %dB %s" % (i + 1, r["start_line"], r["end_line"], r["bytes"], r["stmt_sha8"]) for i, r in enumerate(rows)]
    parts = [sid, L.clip_bytes(sig, 200), L.clip_bytes(doc1, 160)] + table
    s = "\n".join(p for p in parts if p)
    if L.blen(s) > SKEL_MAX:
        parts = [sid, L.clip_bytes(sig, 120), L.clip_bytes(doc1, 60)] + table
        s = "\n".join(p for p in parts if p)
    while L.blen(s) > SKEL_MAX and len(table) > 1:
        table = table[:-1] + ["... %d more" % (len(rows) - len(table) + 1)] if not table[-1].startswith("...") else table[:-2] + ["... more"]
        s = "\n".join(p for p in [sid, L.clip_bytes(sig, 80), L.clip_bytes(doc1, 40)] + table if p)
    return s


# ---- packer ----------------------------------------------------------------------------------------
LEVELS = ["a", "b", "g"]


def pack(items, C, H, render=None):
    """items: rank-ordered [{"id", "a", "b", "g"}] (texts; sizes a >= b >= g). Two conditions on the real serialisation:
    the rows alone serialise to <= C - H bytes, and the rendered envelope serialises to <= C bytes (render(rows) -> object).

    Demotion: one level across the whole suffix before the next level, last item first: a -> b, then b -> g, then drop.
    Returns {"rows": [[id, level, text]], "levels", "dropped", "trace": [(index, from, to)], "bytes": full size, "rows_bytes"}.
    Raises BudgetError (E_BUDGET) when even zero rows do not fit; content is never truncated."""
    render = render or (lambda rows: rows)

    def sizes(levels):
        rows = [[it["id"], lv, it[lv]] for it, lv in zip(items, levels) if lv != "drop"]
        return L.ser_len(rows), L.ser_len(render(rows)), rows

    def ok(n_rows, n_full):
        return n_rows <= C - H and n_full <= C

    levels = ["a"] * len(items)
    trace = []
    nr, nf, rows = sizes(levels)
    nxt = {"a": "b", "b": "g", "g": "drop"}
    for want in ("a", "b", "g"):
        if ok(nr, nf):
            break
        for i in range(len(items) - 1, -1, -1):
            if ok(nr, nf):
                break
            if levels[i] == want:
                trace.append((i, want, nxt[want]))
                levels[i] = nxt[want]
                nr, nf, rows = sizes(levels)
    if not ok(nr, nf):
        raise BudgetError("E_BUDGET")
    return {"rows": rows, "levels": levels, "dropped": levels.count("drop"), "trace": trace, "bytes": nf, "rows_bytes": nr}
