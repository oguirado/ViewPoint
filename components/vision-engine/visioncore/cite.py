# @module {"id":"vc.cite","layer":3,"stage":"S6","owner":"TIME","deps":["vc.ids","vc.layout","vc.errors","vc.canon"],"anti_uses":["trusting judge ids","float math"],"oracle":"tests/test_cite.py","book":"HB-VC-s6-pack-cite"}
"""@module vc.cite L3 - S6 harness-side citation verifier (SPEC_TIME §1, §4.2, §5). Fails closed: a FAIL result whose
pointers do not all-pass rules 1-6 for at least one pointer becomes CANNOT_SEE (rc 3). Data in, verdict out; no clock/random."""
import json
from pathlib import Path

from . import canon, ids, layout
from .errors import VqaError

SCHEMA = "vqa.cite-verdict/v1"
POINTER_MAX_LEN = 24
POINTERS_MAX = 8
QUOTE_MAX = 120
REASONS = ("frozen", "flicker", "teleport", "overlap", "drift", "missing", "black", "static_content")
TAGS = (("E_CITE_SYNTAX", "SY"), ("E_CITE_UNKNOWN", "UK"), ("E_CITE_WINDOW", "WI"), ("E_CITE_QUOTE", "QU"),
        ("E_CITE_CLASS", "CL"), ("E_CITE_IRRELEVANT", "RE"), ("E_CITE_NONE", "NO"))
TAG_OF = dict(TAGS)
# SPEC_INT §2 O4 vision-class checks: static_content is not accepted evidence for these (FC-T3)
VISION_CHECKS = ("render.flicker", "render.frozen_region", "ui.overlap", "ui.drift", "gui.scroll_stuck")


def is_vision_check(check_id):
    return isinstance(check_id, str) and (check_id in VISION_CHECKS or check_id.endswith(".teleport"))


_KINDS = ("field", "region", "global", "track", "gop")
# reason_class -> (tile/region classes that support it, track flags that support it)
_COMPAT = {
    "frozen": (("FROZEN",), ("FROZEN",)),
    "flicker": (("FLICKER",), ("FLICKER", "CYCLE")),
    "teleport": (("MOVED",), ("APPEAR", "VANISH")),
    "overlap": (("CHANGED", "MOVED"), ()),
    "drift": (("DRIFT",), ("DRIFT",)),
    "missing": (("CHANGED",), ("VANISH",)),
}


# --------------------------------------------------------------------------- loading
def _jsonl(path):
    p = Path(path)
    if not p.is_file():
        return []
    return [canon.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]


def build_vision(grid_W, grid_H, tiles, frames, regions, gops, params_sha256, api_version):  # @anchor:vc.s6.build_vision
    """In-memory vision view. tiles = frame-major N x 32 B records (header excluded); frames = frames.jsonl rows (file
    order = tiles row order); regions = regions.jsonl rows; gops = gops.jsonl rows."""
    s, gw, gh = layout.grid_params(grid_W, grid_H)
    order = {m: i for i, m in enumerate(sorted(layout.morton(x, y) for y in range(gh) for x in range(gw)))}
    fr = {}
    for row, r in enumerate(frames):
        fr[r["frame"]] = {"row": row, "n": list(r.get("n", []))}
    cyc = {}
    for g in gops:
        for c in g.get("cycles", []):
            k, d = ids.parse_any(c["t"])
            if k == "field":
                cyc.setdefault(d["morton"], []).append((d["frame"], c.get("end", d["frame"]), c["p"]))
    return {"grid": layout.grid_ident(grid_W, grid_H), "W": grid_W, "H": grid_H, "gw": gw, "gh": gh, "N": len(order), "order": order,
            "tiles": tiles, "frames": fr, "regions": {r["id"]: r for r in regions}, "gops": list(gops),
            "cycles": cyc, "params_sha256": params_sha256, "api_version": api_version}


def load_vision(vision_dir):  # @anchor:vc.s6.load_vision
    d = Path(vision_dir)
    tp = d / "tiles.bin"
    if not tp.is_file():
        raise VqaError("E_IO", {"path": str(tp)})
    raw = tp.read_bytes()
    h = layout.parse_tile_header(raw)
    frames, gops = _jsonl(d / "frames.jsonl"), _jsonl(d / "gops.jsonl")
    psha = gops[0]["params_sha256"] if gops and "params_sha256" in gops[0] else h["params_sha256_16"]
    return build_vision(h["W"], h["H"], raw[layout.TILE_HDR_SIZE:], frames, _jsonl(d / "regions.jsonl"), gops,
                        psha, h["api_version"])


# --------------------------------------------------------------------------- host facts
def _tile(v, frame, m):
    fr = v["frames"].get(frame)
    i = v["order"].get(m)
    if fr is None or i is None:
        return None
    off = (fr["row"] * v["N"] + i) * layout.TILE_SIZE
    if off + layout.TILE_SIZE > len(v["tiles"]):
        return None
    return layout.unpack_tile(v["tiles"], off)


def _spans_tiles(row):
    out = set()
    for lo, hi in row.get("spans", []):
        for m in range(int(lo, 16), int(hi, 16) + 1):
            out.add(layout.unmorton(m))
    return out


def _first_region(track):
    return "r" + track["k"][1:]


def _track(v, kid):
    for g in v["gops"]:
        for t in g.get("tracks", []):
            if t.get("k") == kid:
                return t
    return None


def _tracks_of_region(v, rid):
    return [t for g in v["gops"] for t in g.get("tracks", []) if rid in (t.get("r_rep"), _first_region(t))]


def _cut_flash(v, frame):
    cut = flash = False
    for g in v["gops"]:
        if g.get("f0") == frame and g.get("cut"):
            cut = True
        for ev in g.get("events", []):
            if len(ev) == 2 and ev[0] == "FLASH" and ev[1] == ids.global_id(frame):
                flash = True
    return cut, flash


def _gop_row(v, n):
    for g in v["gops"]:
        if g.get("g") == n:
            return g
    return None


def _cls_of_mask(mask):
    return {layout.CLS[i] for i in range(len(layout.CLS)) if mask >> i & 1}


def _facts(v, kind, d):
    """-> dict(exists, cls, trk, edge, all_unch, cut, flash, cycle, mv, period) for one parsed pointer."""
    f = {"exists": False, "cls": set(), "trk": set(), "edge": False, "all_unch": False, "cut": False, "flash": False,
         "cycle": False, "mv": (0, 0), "period": 0}
    if kind == "field":
        t = _tile(v, d["frame"], d["morton"])
        if t is None:
            return f
        f.update(exists=True, cls={layout.CLS[t["cls"]]}, edge=bool(t["flags"] & 1), mv=(t["mv_dx"], t["mv_dy"]))
        for i0, end, p in v["cycles"].get(d["morton"], []):
            if i0 <= d["frame"] <= end:
                f["cycle"], f["period"] = True, p
    elif kind == "region":
        r = v["regions"].get(ids.region_id(d["frame"], d["morton"]))
        if r is None:
            return f
        f.update(exists=True, cls={r["cls"]}, edge=r.get("edge_ppm", 0) > 0, mv=tuple(r.get("mv", (0, 0))))
        for t in _tracks_of_region(v, r["id"]):
            f["trk"].update(t.get("flags", []))
            f["period"] = f["period"] or t.get("period", 0)
    elif kind == "global":
        fr = v["frames"].get(d["frame"])
        if fr is None:
            return f
        f.update(exists=True, all_unch=bool(fr["n"]) and fr["n"][0] == v["N"])
        f["cut"], f["flash"] = _cut_flash(v, d["frame"])
    elif kind == "track":
        t = _track(v, ids.track_id(d["frame"], d["morton"]))
        if t is None:
            return f
        r = v["regions"].get(t.get("r_rep"))
        f.update(exists=True, cls=_cls_of_mask(t.get("cls_mask", 0)), trk=set(t.get("flags", [])),
                 edge=bool(r and r.get("edge_ppm", 0) > 0), period=t.get("period", 0),
                 mv=tuple(t.get("mv_mode", (0, 0))))
        f["cycle"] = f["period"] >= 2
    elif kind == "gop":
        g = _gop_row(v, d["gop"])
        if g is None:
            return f
        f["exists"] = True
        f["cut"] = bool(g.get("cut"))
        f["flash"] = any(ev[0] == "FLASH" for ev in g.get("events", []) if len(ev) == 2)
        for t in g.get("tracks", []):
            f["trk"].update(t.get("flags", []))
            f["cls"] |= _cls_of_mask(t.get("cls_mask", 0))
        f["cycle"] = bool(g.get("cycles"))
    return f


def _evidence(reason, f):
    """Rule 5: does the host evidence for this pointer support the claimed reason_class?"""
    cls, trk = f["cls"], f["trk"]
    if reason == "static_content":
        return f["exists"]
    if reason == "frozen":
        return "FROZEN" in cls or "FROZEN" in trk or f["all_unch"]
    if reason == "flicker":
        return "FLICKER" in cls or bool(trk & {"FLICKER", "CYCLE"}) or f["cycle"]
    if reason == "teleport":
        return bool(trk & {"APPEAR", "VANISH"}) or "MOVED" in cls
    if reason == "overlap":
        return bool(cls & {"CHANGED", "MOVED"}) and f["edge"]
    if reason == "drift":
        return "DRIFT" in cls or "DRIFT" in trk
    if reason == "missing":
        return "CHANGED" in cls or "VANISH" in trk
    if reason == "black":
        return f["cut"] or f["flash"]
    return False


def render_quote(pointer, v, kind, d):  # @anchor:vc.s6.render_quote
    """Host digest string `<pointer> <CLASS>[ mv=<dx>,<dy>][ p=<period>]` (SPEC_TIME §3.1)."""
    f = _facts(v, kind, d)
    if kind in ("field", "region"):
        name = sorted(f["cls"])[0] if f["cls"] else "UNCHANGED"
    elif kind == "track":
        name = layout.CLS[max([layout.CLS_ID[c] for c in f["cls"]] or [0])]
    else:
        name = "ALL"
    s = pointer + " " + name
    if f["mv"] != (0, 0):
        s += " mv=%d,%d" % (f["mv"][0], f["mv"][1])
    if f["period"]:
        s += " p=%d" % f["period"]
    return s


# --------------------------------------------------------------------------- window / relevance
def window_frames(v, pack):
    """-> (SEEN frames = frame_map U crop_frames, sent gop numbers, BATCH frames, V3-printed region ids)."""
    fs = set()
    for e in pack.get("frame_map", []):
        if isinstance(e, (list, tuple)) and len(e) == 2 and isinstance(e[1], int):
            fs.add(e[1])
    fs.update(x for x in pack.get("crop_frames", []) if isinstance(x, int))
    sent = set()
    for x in pack.get("gops", []):
        if isinstance(x, int):
            sent.add(x)
        elif isinstance(x, str):
            try:
                kind, d = ids.parse_any(x)
            except VqaError:
                continue
            if kind == "gop":
                sent.add(d["gop"])
    batch = set()
    for g in v["gops"]:
        if g.get("g") in sent:
            batch.update(g.get("frame_ids", range(g["f0"], g["f0"] + g.get("n_frames", 0))))
    printed = set()
    for blk in (pack.get("v3") or {}).get("g", []):
        for it in blk.get("i", []):
            if isinstance(it, list) and len(it) > 1 and isinstance(it[1], str):
                printed.add(it[1])
    return Window(fs, sent, batch, printed)


class Window(tuple):
    """(SEEN frames, sent gop numbers, BATCH frames, V3-printed region ids), also reachable by name."""
    def __new__(cls, seen, sent, batch, printed):
        return super().__new__(cls, (seen, sent, batch, printed))

    seen = property(lambda self: self[0])
    sent = property(lambda self: self[1])
    batch = property(lambda self: self[2])
    printed = property(lambda self: self[3])


def _in_window(v, kind, d, win):
    if kind in ("field", "global"):
        return d["frame"] in win.seen
    if kind == "region":
        return d["frame"] in win.seen or ids.region_id(d["frame"], d["morton"]) in win.printed
    if kind == "gop":
        return d["gop"] in win.sent
    t = _track(v, ids.track_id(d["frame"], d["morton"]))
    first = t.get("first", d["frame"]) if t else d["frame"]
    last = t.get("last", d["frame"]) if t else d["frame"]
    return any(first <= x <= last for x in win.batch)


def _pointer_tiles(v, kind, d):
    if kind == "field":
        return {layout.unmorton(d["morton"])}
    if kind == "region":
        return _spans_tiles(v["regions"].get(ids.region_id(d["frame"], d["morton"]), {}))
    if kind == "track":
        t = _track(v, ids.track_id(d["frame"], d["morton"])) or {}
        r = v["regions"].get(t.get("r_rep")) or v["regions"].get(ids.region_id(d["frame"], d["morton"]))
        return _spans_tiles(r or {})
    return None  # whole frame


def _flagged_tiles(v, reason, fs):
    cls_ok, trk_ok = _COMPAT[reason]
    out = set()
    for r in v["regions"].values():
        if r["frame"] in fs and r["cls"] in cls_ok:
            out |= _spans_tiles(r)
    for g in v["gops"]:
        for t in g.get("tracks", []):
            if set(t.get("flags", [])) & set(trk_ok) and any(t["first"] <= x <= t["last"] for x in fs):
                for rid in (t.get("r_rep"), _first_region(t)):
                    if rid in v["regions"]:
                        out |= _spans_tiles(v["regions"][rid])
    return out


def _relevant(v, reason, kind, d, fs, f):
    if reason == "static_content":
        return True
    if reason == "black":
        return f["cut"] or f["flash"]
    flagged = _flagged_tiles(v, reason, fs)
    if not flagged:
        return False
    tiles = _pointer_tiles(v, kind, d)
    if tiles is None:
        return True
    return any((x + dx, y + dy) in flagged for x, y in tiles for dx in (-1, 0, 1) for dy in (-1, 0, 1))


# --------------------------------------------------------------------------- verifier
def _check_pointer(v, p, reason, quote, win, check_id=None):
    """-> (accepted, code|None, kind, parsed). First failing rule wins (SPEC_TIME §5 order 1-6)."""
    if not isinstance(p, str) or not p or len(p) > POINTER_MAX_LEN:
        return False, "E_CITE_SYNTAX", None, None
    try:
        kind, d = ids.parse_any(p)
    except VqaError:
        return False, "E_CITE_SYNTAX", None, None
    if kind not in _KINDS:
        return False, "E_CITE_SYNTAX", None, None
    try:
        ids.parse_any(p, v["W"], v["H"])
    except VqaError:
        return False, "E_CITE_UNKNOWN", kind, d
    f = _facts(v, kind, d)
    if not f["exists"]:
        return False, "E_CITE_UNKNOWN", kind, d
    if not _in_window(v, kind, d, win):
        return False, "E_CITE_WINDOW", kind, d
    if quote is not None and quote != render_quote(p, v, kind, d):
        return False, "E_CITE_QUOTE", kind, d
    if reason == "static_content" and is_vision_check(check_id):
        return False, "E_CITE_CLASS", kind, d
    if not _evidence(reason, f):
        return False, "E_CITE_CLASS", kind, d
    if not _relevant(v, reason, kind, d, win.seen | win.batch, f):
        return False, "E_CITE_IRRELEVANT", kind, d
    required = {"render.flicker": "flicker", "render.frozen_region": "frozen",
                "gui.scroll_stuck": "frozen", "ui.overlap": "overlap", "ui.drift": "drift"}.get(
                    check_id if isinstance(check_id, str) else None)
    if isinstance(check_id, str) and check_id.endswith(".teleport"):
        required = "teleport"
    if required and (not _evidence(required, f) or not _relevant(v, required, kind, d, win.seen | win.batch, f)):
        return False, "E_CITE_IRRELEVANT", kind, d
    return True, None, kind, d


def _stale(v, pack):
    for k in ("grid", "params_sha256", "api_version"):
        if not isinstance(pack.get(k), str):
            raise VqaError("E_USAGE", {"reason": "pack lacks " + k})
    if pack["grid"] != v["grid"]:
        return "grid"
    pv, vv = pack["params_sha256"], v["params_sha256"]
    if not (pv == vv or (len(vv) == 32 and pv.startswith(vv)) or (len(pv) == 32 and vv.startswith(pv))):
        return "params_sha256"
    if pack["api_version"] != v["api_version"]:
        return "api_version"
    return None


def _result(v, res, win):
    cid = res.get("check_id")
    status = res.get("status")
    out = {"check_id": cid, "status_in": status, "status_out": status, "pointers": [], "accepted_pointers": []}
    if status != "FAIL":
        return out, set(), False
    reason = res.get("reason_class")
    quote = res.get("digest_quote")
    ptrs = res.get("pointers", [])
    codes = set()
    if reason not in REASONS or not isinstance(ptrs, list) or (quote is not None and (
            not isinstance(quote, str) or len(quote) > QUOTE_MAX)):
        # malformed claim: nothing can be verified, every pointer is a syntax failure
        reason = reason if reason in REASONS else "static_content"
        bad_claim = True
    else:
        bad_claim = False
    quote_pointer = quote.split(" ", 1)[0] if isinstance(quote, str) else None
    quote_unbound = quote is not None and quote_pointer not in ptrs
    if quote is not None and not quote_unbound and not bad_claim:
        try:
            qkind, qdata = ids.parse_any(quote_pointer)
            quote_unbound = qkind not in _KINDS or quote != render_quote(quote_pointer, v, qkind, qdata)
        except VqaError:
            quote_unbound = True
    for i, p in enumerate(ptrs):
        if bad_claim or i >= POINTERS_MAX:
            ok, code = False, "E_CITE_SYNTAX"
        elif quote_unbound:
            ok, code = False, "E_CITE_QUOTE"
        else:
            own_quote = quote if p == quote_pointer else None
            ok, code, _, _ = _check_pointer(v, p, reason, own_quote, win, cid)
        out["pointers"].append({"p": p if isinstance(p, str) else repr(p), "ok": ok, "code": code or "OK"})
        if ok:
            out["accepted_pointers"].append(p)
        else:
            codes.add(code)
    accepted = bool(out["accepted_pointers"])
    if not accepted:
        codes.add("E_CITE_NONE")
        out["status_out"] = "CANNOT_SEE"
    return out, codes, accepted


def verify_citations(vision, judge_output, pack):  # @anchor:vc.s6.verify
    """Pure verifier -> vqa.cite-verdict/v1 dict with `rc`. 0 all accepted; 3 >=1 FAIL rejected; 2 usage; 4 stale."""
    if not isinstance(judge_output, dict) or not isinstance(judge_output.get("results"), list) or not isinstance(
            pack, dict):
        return _verdict("E_USAGE", 2, pack, [], 0, 0, set())
    try:
        stale = _stale(vision, pack)
    except VqaError:
        return _verdict("E_USAGE", 2, pack, [], 0, 0, set())
    if stale:
        return _verdict("E_STALE", 4, pack, [], 0, 0, set())
    win = window_frames(vision, pack)
    results, codes, acc, rej = [], set(), 0, 0
    for res in judge_output["results"]:
        if not isinstance(res, dict):
            return _verdict("E_USAGE", 2, pack, [], 0, 0, set())
        r, c, ok = _result(vision, res, win)
        results.append(r)
        codes |= c
        if res.get("status") == "FAIL":
            acc, rej = (acc + 1, rej) if ok else (acc, rej + 1)
    if rej:
        return _verdict("E_CITE", 3, pack, results, acc, rej, codes)
    return _verdict("OK", 0, pack, results, acc, rej, codes)


def _verdict(code, rc, pack, results, acc, rej, codes):
    tags = [t for name, t in TAGS if name in codes]
    receipt = "CV:PASS" if not tags and rc == 0 else "CV:FAIL:" + ",".join(tags) if tags else "CV:FAIL"
    psha = canon.sha256_obj(pack) if isinstance(pack, dict) else None
    return {"schema": SCHEMA, "code": code, "rc": rc, "pack_sha256": psha, "results": results,
            "counts": {"accepted": acc, "rejected": rej}, "receipt": receipt}


def verify_dir(vision_dir, pack, judge_output):  # @anchor:vc.s6.verify_dir
    """Load <vision_dir> and verify; pack/judge_output are dicts or paths to canonical JSON files."""
    def _ld(x):
        return canon.loads(Path(x).read_text(encoding="utf-8")) if not isinstance(x, dict) else x
    try:
        v = load_vision(vision_dir)
        p, j = _ld(pack), _ld(judge_output)
    except VqaError as e:
        return _verdict("E_USAGE", 2, None, [], 0, 0, set()) if e.rc == 2 else _verdict(e.code, e.rc, None, [], 0, 0, set())
    return verify_citations(v, j, p)
