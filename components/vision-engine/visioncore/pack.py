# @module {"id":"vc.s6","layer":3,"stage":"S6","owner":"TIME","deps":["vc.canon","vc.cite","vc.errors","vc.ids","vc.layout","vc.p"],"book":"HB-VC-s6-pack-cite"}
"""@module vc.s6 L3 - judge input pack vqa.pack/v2: Z1 static text, V1 heatmap PNGs, V2 crops, V3 fitted JSON (SPEC_TIME §3, §6)."""
import os
import zlib

import numpy as np

from . import canon, cite, ids, layout
from .errors import VqaError
from .params import PARAMS

Z1_VERSION = "z1.1"
# Token model (SPEC_TIME §6): ASSUMED until EVAL T8 re-measures; private, not thresholds.
_TOK_IMG = 258
_TOK_IMG_SMALL_PX = 384   # P8: <= this on both sides -> one flat image charge
_TOK_TILE_PX = 768        # vendor Gemini tiling, ASSUMED for the R2 model ids (refit from provider usage after X0)
_CHECK_LADDER_PX = 768    # last ladder step: s6on check PNGs downscaled to this long side
_TOK_TARGET = PARAMS["PACK_CALL_TOK"]
# ADV-13 retry count; REG-REQ PACK_RETRY_MAX=2 (SPEC_TIME §6). Falls back until T2 registers it.
_RETRY_MAX = PARAMS.get("PACK_RETRY_MAX", 2)
_TOK_HARD = _TOK_TARGET
MAX_HEATMAPS = 4
MAX_CROP_PAIRS = 6
CROP_SIDE = 256
MP4_SLOTS = 12
MP4_SLOTS_MIN = 8

BG = (32, 32, 32)
# class id -> (weight, colour); cls 0/1 are never painted
CLS_WEIGHT = {2: 3, 3: 4, 4: 8, 5: 8, 6: 6}
CLS_COLOUR = {2: (230, 25, 25), 3: (30, 90, 230), 4: (250, 220, 0), 5: (0, 200, 220), 6: (255, 140, 0)}
FLAG_WEIGHT = {"FLASH": 9, "CUT": 9, "FLICKER": 8, "CYCLE": 8, "FROZEN": 8, "APPEAR": 7, "VANISH": 7,
               "DRIFT": 6, "GLOBAL": 2}
FRAMEALL_WEIGHT = {"FLASH": 9, "CUT": 9, "GLOBAL": 2}

# ---------------------------------------------------------------- Z1 (static, byte-stable)
_Z1 = """VISION PACK LEGEND (z1.1)
GRID: the frame is cut into square tiles in Morton (Z) order; tile id t<hhhh> = 4 lower-case hex digits of the Morton code.
Heatmap PNGs show one cell per tile; grey lines every 8 tiles; hex labels mark macroblock corners.
CLASSES (heatmap colour): CHANGED red, MOVED blue + line to its motion, FLICKER yellow, FROZEN cyan, DRIFT orange. Dark = quiet.
Brightness is the share of GOP frames in which the cell was active.
POINTERS (cite only ids you can see in the pack; max 8 per result, max 24 chars each):
  f<frame>:t<hhhh>  tile       r<frame>:<hhhh>  region    f<frame>:all  whole frame
  k<frame>:<hhhh>   track      g<n>             GOP
frame = decimal, no leading zeros. Hex is lower case, exactly 4 digits.
V3 JSON: {"g":[{"g":n,"C":changed,"M":moved,"F":flicker,"Z":frozen,"D":drift,"X":frame-wide,"i":[items]}]}
  item L0 [track,region,CLASS,[x,y,w,h],n_tiles,[dx,dy],period,[flags],quote]; L1 [track,first,last,CLASS,[dx,dy]];
  frame-wide L0 [f<n>:all,KIND,quote]; L1 [f<n>:all,KIND]. Counts are tracks per class in the GOP.
REASON CLASSES (required on every FAIL): frozen, flicker, teleport, overlap, drift, missing, black, static_content.
  frozen=FROZEN cell or whole frame unchanged; flicker=FLICKER cell or cycle period>=2; teleport=APPEAR+VANISH or MOVED;
  overlap=CHANGED/MOVED cell on an edge; drift=DRIFT; missing=CHANGED cell or VANISH; black=cut/flash frame; static_content=legacy checks only.
Vision-class checks require compatible host regions independently of the reason_class.
OUTPUT: per check {"check_id":str,"status":"PASS|FAIL|CANNOT_SEE","pointers":[ids],"reason_class":str,"digest_quote":str}.
  A FAIL needs at least one pointer whose evidence matches its reason class; otherwise answer CANNOT_SEE.
  digest_quote, if given, must repeat the quote of the cited item byte for byte.
EXAMPLES:
  1. FAIL frozen: pointers ["k38:019c","f45:t01a3"], digest_quote "k38:019c FROZEN"
  2. FAIL black: pointers ["f57:all"], digest_quote "f57:all ALL"
  3. CANNOT_SEE: pointers [] when the pack shows no evidence for the check.
Never cite an id that is not in this pack or in the media you were shown.
"""


def z1_text():
    """Z1 bytes: identical across batches and runs, no run ids or timestamps."""
    b = _Z1.encode("ascii")
    if len(b) > PARAMS["Z1_MAX_B"]:
        raise VqaError("E_BUDGET", {"part": "z1", "bytes": len(b), "cap": PARAMS["Z1_MAX_B"]})
    return b


# ---------------------------------------------------------------- PNG (deterministic)
def _chunk(tag, data):
    crc = zlib.crc32(tag + data) & 0xFFFFFFFF
    return len(data).to_bytes(4, "big") + tag + data + crc.to_bytes(4, "big")


def png_encode(rgb):
    """uint8[H,W,3] -> PNG bytes. zlib level 6, filter 0, no text/time chunks (byte-stable)."""
    a = np.ascontiguousarray(rgb, dtype=np.uint8)
    if a.ndim != 3 or a.shape[2] != 3 or a.shape[0] < 1 or a.shape[1] < 1:
        raise VqaError("E_USAGE", {"reason": "png_encode needs uint8[H,W,3]"})
    h, w = a.shape[0], a.shape[1]
    raw = np.zeros((h, 1 + w * 3), dtype=np.uint8)
    raw[:, 1:] = a.reshape(h, w * 3)
    ihdr = w.to_bytes(4, "big") + h.to_bytes(4, "big") + bytes([8, 2, 0, 0, 0])
    return (b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", ihdr) + _chunk(b"IDAT", zlib.compress(raw.tobytes(), 6))
            + _chunk(b"IEND", b""))


def has_forbidden_colour(rgb):
    """True iff any pixel is in the magenta box R>=200, B>=200, G<=60 (fallback-shader detector)."""
    a = np.asarray(rgb)
    return bool(np.any((a[..., 0] >= 200) & (a[..., 2] >= 200) & (a[..., 1] <= 60)))


# ---------------------------------------------------------------- 3x5 font
_GLYPHS = {
    "0": "111101101101111", "1": "010110010010111", "2": "111001111100111", "3": "111001111001111",
    "4": "101101111001001", "5": "111100111001111", "6": "111100111101111", "7": "111001001010010",
    "8": "111101111101111", "9": "111101111001111", "a": "010101111101101", "b": "110101110101110",
    "c": "011100100100011", "d": "110101101101110", "e": "111100110100111", "f": "111100110100100",
    ":": "000010000010000",
}
LABEL_COLOUR = (190, 190, 190)
GRID_LINE_COLOUR = (80, 80, 80)
ARROW_COLOUR = (255, 255, 255)


def _draw_text(img, x, y, text, colour):
    h, w = img.shape[0], img.shape[1]
    for ci, ch in enumerate(text):
        bits = _GLYPHS[ch]
        for r in range(5):
            for c in range(3):
                if bits[r * 3 + c] == "1":
                    px, py = x + ci * 4 + c, y + r
                    if 0 <= px < w and 0 <= py < h:
                        img[py, px] = colour


def _line(img, x0, y0, x1, y1, colour):
    h, w = img.shape[0], img.shape[1]
    dx, dy = abs(x1 - x0), -abs(y1 - y0)
    sx = 1 if x0 < x1 else -1
    sy = 1 if y0 < y1 else -1
    err = dx + dy
    while True:
        if 0 <= x0 < w and 0 <= y0 < h:
            img[y0, x0] = colour
        if x0 == x1 and y0 == y1:
            return
        e2 = 2 * err
        if e2 >= dy:
            err += dy
            x0 += sx
        if e2 <= dx:
            err += dx
            y0 += sy


def _sgn(v):
    return (v > 0) - (v < 0)


def cell_px(gw):
    return max(4, 768 // gw)


# @anchor:vc.s6.heatmap
def heatmap_png(W, H, tile_frames):
    """V1: one PNG for a GOP. tile_frames = list of TILE_DTYPE[N] arrays (one per frame, same Morton order).
    Cell class = highest-weight class seen (ties to the higher class id); intensity = active frames / n_frames."""
    s, gw, gh = layout.grid_params(W, H)
    if not tile_frames:
        raise VqaError("E_USAGE", {"reason": "heatmap needs >= 1 frame"})
    n = len(tile_frames)
    mort = np.asarray(tile_frames[0]["morton"])
    N = len(mort)
    wt = np.zeros(8, dtype=np.int64)
    for c, w in CLS_WEIGHT.items():
        wt[c] = w
    best = np.zeros(N, dtype=np.int64)
    act = np.zeros(N, dtype=np.int64)
    mvx = np.zeros(N, dtype=np.int64)
    mvy = np.zeros(N, dtype=np.int64)
    for tf in tile_frames:
        if len(tf) != N:
            raise VqaError("E_SCHEMA", {"reason": "tile frame length differs"})
        cls = np.asarray(tf["cls"]).astype(np.int64)
        if cls.max(initial=0) > 6:
            raise VqaError("E_SCHEMA", {"cls": int(cls.max())})
        key = np.where(wt[cls] > 0, wt[cls] * 8 + cls, 0)
        upd = key >= best
        upd &= key > 0
        mvx = np.where(upd, np.asarray(tf["mv_dx"]).astype(np.int64), mvx)
        mvy = np.where(upd, np.asarray(tf["mv_dy"]).astype(np.int64), mvy)
        best = np.where(upd, key, best)
        act += (cls >= 2)
    c = cell_px(gw)
    img = np.empty((gh * c, gw * c, 3), dtype=np.uint8)
    img[:] = BG
    cells = []
    for r in range(N):
        tx, ty = layout.unmorton(int(mort[r]))
        cells.append((tx, ty))
        if best[r] == 0:
            continue
        col = CLS_COLOUR[int(best[r]) % 8]
        a = int(act[r])
        px = tuple(BG[k] + (col[k] - BG[k]) * a // n for k in range(3))
        img[ty * c:(ty + 1) * c, tx * c:(tx + 1) * c] = px
    for g in range(8, gw, 8):
        img[:, g * c] = GRID_LINE_COLOUR
    for g in range(8, gh, 8):
        img[g * c, :] = GRID_LINE_COLOUR
    rad = max(1, c // 2 - 1)
    for r in range(N):
        if best[r] and int(best[r]) % 8 == 3 and (mvx[r] or mvy[r]):
            tx, ty = cells[r]
            cx, cy = tx * c + c // 2, ty * c + c // 2
            ex = max(-rad, min(rad, int(mvx[r]) * c // s)) or _sgn(int(mvx[r]))
            ey = max(-rad, min(rad, int(mvy[r]) * c // s)) or _sgn(int(mvy[r]))
            _line(img, cx, cy, cx + ex, cy + ey, ARROW_COLOUR)
    seen = set()
    for r in range(N):
        mb = int(mort[r]) >> 6
        if mb in seen:
            continue
        seen.add(mb)
        tx0, ty0 = layout.unmorton(mb << 6)
        _draw_text(img, tx0 * c + 1, ty0 * c + 1, "%04x" % (mb << 6), LABEL_COLOUR)
    return png_encode(img)


# ---------------------------------------------------------------- crops (V2)
# @anchor:vc.s6.crop
def crop_png(rgb, bbox_t, s, grow=1):
    """Crop of rgb around tile bbox (tx0,ty0,tx1,ty1) grown by `grow` tiles, clamped; side > 256 px is box-downscaled
    by ceil(side/256). -> (png bytes, [x,y,w,h] in px, factor)."""
    H, W = rgb.shape[0], rgb.shape[1]
    tx0, ty0, tx1, ty1 = bbox_t
    x0, y0 = max(0, (tx0 - grow) * s), max(0, (ty0 - grow) * s)
    x1, y1 = min(W, (tx1 + 1 + grow) * s), min(H, (ty1 + 1 + grow) * s)
    if x1 <= x0 or y1 <= y0:
        raise VqaError("E_VC_ID_RANGE", {"bbox_t": list(bbox_t)})
    sub = np.asarray(rgb[y0:y1, x0:x1], dtype=np.uint8)
    side = max(x1 - x0, y1 - y0)
    f = -(-side // CROP_SIDE)
    if f > 1:
        hh, ww = (sub.shape[0] // f) * f, (sub.shape[1] // f) * f
        if hh == 0 or ww == 0:
            f = 1
        else:
            blk = sub[:hh, :ww].astype(np.uint32).reshape(hh // f, f, ww // f, f, 3)
            sub = (blk.sum(axis=(1, 3)) // (f * f)).astype(np.uint8)
    return png_encode(sub), [x0, y0, x1 - x0, y1 - y0], f


# ---------------------------------------------------------------- items (V3)
def _fid(s):
    return ids.parse_any(s)[1]["frame"]


def _mm(s):
    return ids.parse_any(s)[1]["morton"]


def _labels(tr):
    """-> (weight, label) of a track row: max over flags and class bits."""
    flags = set(tr.get("flags", []))
    mask = int(tr.get("cls_mask", 0))
    cands = []
    for f in flags:
        if f in FLAG_WEIGHT:
            cands.append((FLAG_WEIGHT[f], "FLICKER" if f == "CYCLE" else f))
    for bit, (w, name) in ((2, (3, "CHANGED")), (3, (2 if "GLOBAL" in flags else 4, "MOVED")),
                           (4, (8, "FLICKER")), (5, (8, "FROZEN")), (6, (6, "DRIFT"))):
        if mask >> bit & 1:
            cands.append((w, name))
    if not cands:
        return 1, "CHANGED"
    cands.sort(key=lambda c: (-c[0], c[1]))
    return cands[0]


def _track_items(g):
    out = []
    for tr in g.get("tracks", []):
        w, lab = _labels(tr)
        out.append({"id": tr["k"], "kind": "track", "w": w, "lab": lab, "first": int(tr["first"]),
                    "mm": _mm(tr["k"]), "tr": tr, "g": g["g"]})
    return out


def _frameall_items(g):
    best = {}

    def add(kind, frame):
        fid = ids.global_id(frame)
        if fid not in best or FRAMEALL_WEIGHT[kind] > FRAMEALL_WEIGHT[best[fid][0]]:
            best[fid] = (kind, frame)

    if g.get("cut"):
        add("CUT", int(g["f0"]))
    for ev in g.get("events", []):
        if ev[0] == "FLASH":
            add("FLASH", _fid(ev[1]))
    for row in g.get("global", []):
        if row[1] or row[2]:
            add("GLOBAL", int(row[0]))
    return [{"id": fid, "kind": "frameall", "w": FRAMEALL_WEIGHT[k], "lab": k, "first": fr, "mm": 0, "g": g["g"]}
            for fid, (k, fr) in sorted(best.items())]


def _quote(item, regions):
    # Render the advertised pointer against the same host facts as the verifier.
    if item["kind"] == "frameall":
        kind, data = ids.parse_any(item["id"])
        v = {"frames": {data["frame"]: {"n": []}}, "gops": []}
        return cite.render_quote(item["id"], v, kind, data)
    v = {"gops": [{"tracks": [item["tr"]]}], "regions": regions}
    return cite.render_quote(item["id"], v, *ids.parse_any(item["id"]))


def _encode(item, level, regions):
    if item["kind"] == "frameall":
        return [item["id"], item["lab"], _quote(item, regions)] if level == 0 else [item["id"], item["lab"]]
    tr = item["tr"]
    mv = list(tr.get("mv_mode") or [0, 0])
    if level == 1:
        return [tr["k"], int(tr["first"]), int(tr["last"]), item["lab"], mv]
    reg = regions.get(tr["r_rep"])
    if reg is None:
        raise VqaError("E_SCHEMA", {"reason": "r_rep not in regions", "r_rep": tr["r_rep"]})
    return [tr["k"], tr["r_rep"], item["lab"], list(reg["bbox_px"]), int(tr["n_tiles_max"]), mv,
            int(tr.get("period", 0)), list(tr.get("flags", [])), _quote(item, regions)]


def _counts(g, frameall_n):
    c = {"C": 0, "M": 0, "F": 0, "Z": 0, "D": 0}
    for tr in g.get("tracks", []):
        m, fl = int(tr.get("cls_mask", 0)), set(tr.get("flags", []))
        c["C"] += m >> 2 & 1
        c["M"] += m >> 3 & 1
        c["F"] += 1 if (m >> 4 & 1) or fl & {"FLICKER", "CYCLE"} else 0
        c["Z"] += 1 if (m >> 5 & 1) or "FROZEN" in fl else 0
        c["D"] += 1 if (m >> 6 & 1) or "DRIFT" in fl else 0
    c["X"] = frameall_n
    return c


def _key(item, acc):
    return (-(item["w"] + acc.get(item["id"], 0)), item["first"], item["mm"], item["id"])


def _v3(gops, items, level, regions, with_items=True):
    blocks = []
    for g in gops:
        its = [it for it in items if it["g"] == g["g"]]
        fa = sum(1 for it in its if it["kind"] == "frameall")
        b = {"g": g["g"]}
        b.update(_counts(g, fa))
        enc = [_encode(it, level[it["id"]], regions) for it in its if level[it["id"]] < 2]
        if enc:
            b["i"] = enc
        blocks.append(b)
    return {"g": blocks}


def _size(o):
    return len(canon.dumps(o).encode("utf-8")) - 1


# @anchor:vc.s6.fit
def fit(gops, items, regions, acc, cap_bytes, gop_cap):
    """Greedy fit (SPEC_TIME §3.1): everything at L2, then promote in strict key order to L1, then to L0.
    A promotion that overflows its GOP block is skipped; one that overflows the batch cap ends the pass.
    -> (v3 dict, level dict). All-L2 over a cap -> E_BUDGET."""
    order = sorted(items, key=lambda it: _key(it, acc))
    items = order  # encoded items inside a block follow the strict key order, not discovery order
    level = {it["id"]: 2 for it in items}
    for g in gops:
        v = _v3([g], [it for it in items if it["g"] == g["g"]], level, regions)
        if _size(v["g"][0]) > gop_cap:
            raise VqaError("E_BUDGET", {"part": "v3", "gop": g["g"], "cap": gop_cap})
    if _size(_v3(gops, items, level, regions)) > cap_bytes:
        raise VqaError("E_BUDGET", {"part": "v3", "reason": "all-L2 exceeds batch cap", "cap": cap_bytes})
    for target in (1, 0):
        for it in order:
            if level[it["id"]] != target + 1:
                continue
            level[it["id"]] = target
            v3 = _v3(gops, items, level, regions)
            blk = [b for b in v3["g"] if b["g"] == it["g"]][0]
            if _size(blk) > gop_cap:
                level[it["id"]] = target + 1
                continue
            if _size(v3) > cap_bytes:
                level[it["id"]] = target + 1
                break
    return _v3(gops, items, level, regions), level


# ---------------------------------------------------------------- MP4 frame selection
def _frame_ids(g):
    """Exact capture timeline; older dense GOP records predate frame_ids."""
    return list(g.get("frame_ids", range(int(g["f0"]), int(g["f0"]) + int(g["n_frames"]))))


# @anchor:vc.s6.select_frames
def select_frames(gops, slots=MP4_SLOTS):
    """Frames for the 1 fps clip. Must-keep (priority): cut/flash, APPEAR/VANISH, first frame of each cycle, FROZEN onset,
    DRIFT onset, the window I-frame; fill by dirty_ppm desc then frame asc. -> (frames asc, frame_map [[sec, frame]], must_n)."""
    if not gops:
        return [], [], 0
    captured = sorted({f for g in gops for f in _frame_ids(g)})
    tiers = [[], [], [], [], [], []]
    dirty = {}
    for g in gops:
        f0 = int(g["f0"])
        for f, d in zip(_frame_ids(g), g.get("dirty_ppm", [])):
            dirty[f] = int(d)
        if g.get("cut"):
            tiers[0].append(f0)
        for ev in g.get("events", []):
            if ev[0] == "FLASH":
                tiers[0].append(_fid(ev[1]))
            elif ev[0] == "APPEAR":
                tiers[1].append(_fid(ev[1]))
            elif ev[0] == "VANISH":
                tiers[1].append(_fid(ev[1]))
        for tr in g.get("tracks", []):
            fl = set(tr.get("flags", []))
            if "VANISH" in fl:
                following = [f for f in captured if f > int(tr["last"])]
                if following:
                    tiers[1].append(following[0])
            if "APPEAR" in fl:
                tiers[1].append(int(tr["first"]))
            if "FROZEN" in fl:
                tiers[3].append(int(tr["first"]))
            if "DRIFT" in fl:
                tiers[4].append(int(tr["first"]))
        for cy in g.get("cycles", []):
            tiers[2].append(_fid(cy["t"]))
    tiers[5].append(int(gops[0]["f0"]))
    chosen, seen = [], set()
    for tier in tiers:
        for f in sorted(set(tier)):
            if f in captured and f not in seen:
                seen.add(f)
                chosen.append(f)
    must_n = len(chosen)
    if must_n > slots:
        raise VqaError("E_BUDGET", {"part": "mandatory_frames", "count": must_n, "cap": slots})
    if len(chosen) < slots:
        rest = sorted((f for f in captured if f not in seen and f in dirty), key=lambda f: (-dirty[f], f))
        chosen += rest[:slots - len(chosen)]
    chosen.sort()
    return chosen, [[i, f] for i, f in enumerate(chosen)], must_n


def tok_image(w, h):
    """SPEC_TIME §6 P8: 258 if max(w,h) <= 384 else 258 * ceil(w/768) * ceil(h/768) (integers)."""
    if max(w, h) <= _TOK_IMG_SMALL_PX:
        return _TOK_IMG
    return _TOK_IMG * -(-w // _TOK_TILE_PX) * -(-h // _TOK_TILE_PX)


def est_tokens(z1_b, v3_b, n_images, mp4_s, text_b=0, image_wh=()):
    """SPEC_TIME §6 estimator (integers): ceil(Z1/4)+ceil(V3/3)+258*(n_images+mp4 s)+ceil(text/4)+sum tok_image(w,h)
    over image_wh (images with a real size; n_images counts only the flat-charge ones)."""
    return (-(-z1_b // 4) + -(-v3_b // 3) + _TOK_IMG * (n_images + mp4_s) + -(-text_b // 4)
            + sum(tok_image(w, h) for w, h in image_wh))


def _shift_tile(m, sx, sy, gw, gh):
    tx, ty = layout.unmorton(m)
    return layout.morton((tx + sx) % gw, (ty + sy) % gh)


def _shift_id(i, sx, sy, gw, gh):
    """Toroidally shift the tile coordinate carried by a region/track id (frames and kinds unchanged)."""
    _, d = ids.parse_any(i)
    m = _shift_tile(d["morton"], sx, sy, gw, gh)
    return (ids.track_id if i[0] == "k" else ids.region_id)(d["frame"], m)


def _shift_box(b, sx, sy, gw, gh, s, t):
    """Shift an origin pair-based box [x0,y0,x1,y1] (tiles if t else px) keeping its size."""
    u = 1 if t else s
    x0, y0 = int(b[0]), int(b[1])
    nx = ((x0 // u + sx) % gw) * u + x0 % u
    ny = ((y0 // u + sy) % gh) * u + y0 % u
    return [nx, ny, nx + int(b[2]) - x0, ny + int(b[3]) - y0]


def _placebo(gops, regions, sx, sy, gw, gh, s):
    """-> (gops', regions', back) with every printed tile coordinate shifted; back maps shifted region id -> true id."""
    def sh(i):
        return _shift_id(i, sx, sy, gw, gh)

    new_regions, back = {}, {}
    for rid, r in regions.items():
        nid = sh(rid)
        nr = dict(r)
        nr["id"] = nid
        for key, t in (("bbox_px", False), ("bbox_t", True)):
            if r.get(key):
                nr[key] = _shift_box(r[key], sx, sy, gw, gh, s, t)
        new_regions[nid] = nr
        back[nid] = rid
    new_gops = []
    for g in gops:
        ng = dict(g)
        tracks = []
        for tr in g.get("tracks", []):
            nt = dict(tr)
            nt["k"] = sh(tr["k"])
            if tr.get("r_rep"):
                nt["r_rep"] = sh(tr["r_rep"])
            tracks.append(nt)
        ng["tracks"] = tracks
        new_gops.append(ng)
    return new_gops, new_regions, back


# ---------------------------------------------------------------- result + pack()
class PackResult:
    def __init__(self, pack, files, acc):
        self.pack, self.files, self.acc = pack, files, acc

    def write(self, out_dir):
        """Stage into <out_dir>.tmp and rename; an existing out_dir or any failure leaves the filesystem as before."""
        out_dir = str(out_dir)
        tmp = out_dir + ".tmp"
        if os.path.exists(out_dir) or os.path.exists(tmp):
            raise VqaError("E_USAGE", {"reason": "output exists", "path": out_dir})
        try:
            for rel, data in self.files.items():
                p = os.path.join(tmp, rel)
                os.makedirs(os.path.dirname(p), exist_ok=True)
                with open(p, "wb") as fh:
                    fh.write(data)
            os.replace(tmp, out_dir)
        except OSError as e:
            _rmtree(tmp)
            raise VqaError("E_IO", {"path": out_dir, "reason": str(e)[:80]})


def _rmtree(path):
    if not os.path.isdir(path):
        return
    for root, dirs, files in os.walk(path, topdown=False):
        for f in files:
            os.unlink(os.path.join(root, f))
        for d in dirs:
            os.rmdir(os.path.join(root, d))
    os.rmdir(path)


def _roll_tiles(a, shift, gw, gh):
    out = np.array(a, copy=True)
    out["morton"] = [_shift_tile(int(m), shift["sx"], shift["sy"], gw, gh) for m in a["morton"]]
    return out


def _regions_map(r):
    if isinstance(r, dict):
        return r
    return {x["id"]: x for x in (r or [])}


# @anchor:vc.s6.pack
def pack(batch, cap_bytes=None, shuffle_window=None, batch_no=0):
    """Build vqa.pack/v2 for one judge batch. batch keys: W, H, params_sha256, gops (gops.jsonl rows), regions
    (dict id->row or list), tiles (callable frame -> TILE_DTYPE[N]) optional, frames (callable frame -> uint8[H,W,3])
    optional, acc (starvation accumulator, dict) optional, n_check_png, text_b optional.
    Nothing is returned (and so nothing written) on E_BUDGET."""
    cap = PARAMS["PACK_BATCH_B"] if cap_bytes is None else cap_bytes
    if not isinstance(cap, int) or cap < 1:
        raise VqaError("E_USAGE", {"cap_bytes": repr(cap)})
    gops = sorted(batch["gops"], key=lambda g: int(g["g"]))
    W, H = batch["W"], batch["H"]
    s, gw_, gh_ = layout.grid_params(W, H)[:3]
    grid = layout.grid_ident(W, H)
    regions = _regions_map(batch.get("regions"))
    acc = dict(batch.get("acc") or {})
    z1 = z1_text()
    for g in gops:
        for tr in g.get("tracks", []):
            if tr.get("r_rep") not in regions:
                raise VqaError("E_SCHEMA", {"reason": "r_rep not in regions", "r_rep": tr.get("r_rep")})
    frame_sel, frame_map, must_n = select_frames(gops, MP4_SLOTS)
    captured = sorted({f for g in gops for f in _frame_ids(g)})
    src = gops
    placebo = shuffle_window is not None
    true_regions, back, shift = regions, {}, None
    if placebo:
        gw, gh = gw_, gh_
        if gw < 2:
            raise VqaError("E_USAGE", {"reason": "placebo needs grid width >= 2"})
        seed = int(shuffle_window)
        shift = {"sx": 1 + seed % (gw - 1), "sy": (seed // gw) % gh}
        src, regions, back = _placebo(gops, regions, shift["sx"], shift["sy"], gw, gh, s)
    items = []
    for g in src:
        items += _track_items(g) + _frameall_items(g)
    v3, level = fit(src, items, regions, acc, cap, PARAMS["PACK_GOP_B"])
    v3_b = _size(v3)
    new_acc = dict(acc)
    for it in items:
        new_acc[it["id"]] = 0 if level[it["id"]] == 0 else acc.get(it["id"], 0) + it["w"]
    ordered = sorted(items, key=lambda it: _key(it, acc))
    crop_items = [it for it in ordered if it["kind"] == "track" and it["tr"].get("r_rep") in regions
                  and regions[it["tr"]["r_rep"]].get("bbox_t")]
    if batch.get("frames") is None:
        crop_items = []
    crop_items = crop_items[:MAX_CROP_PAIRS]
    gsel = [g for g in src] if batch.get("tiles") is not None else []
    if len(gsel) > MAX_HEATMAPS:
        wmax = {g["g"]: max([it["w"] for it in items if it["g"] == g["g"]] or [0]) for g in gsel}
        gsel = sorted(sorted(gsel, key=lambda g: (-wmax[g["g"]], g["g"]))[:MAX_HEATMAPS], key=lambda g: g["g"])
    slots = MP4_SLOTS
    n_chk, text_b = batch.get("n_check_png", 0), batch.get("text_b", 0)
    if any(type(x) is not int or x < 0 for x in (n_chk, text_b)):
        raise VqaError("E_USAGE", {"reason": "token inputs must be nonnegative integers"})
    chk_wh = [tuple(x) for x in batch.get("check_png_wh") or []] or [(W, H)] * n_chk
    chk_px = [None]  # ladder step: long-side cap applied to check PNGs
    hm_wh = (gw_ * cell_px(gw_), gh_ * cell_px(gw_))

    def _chk():
        if chk_px[0] is None:
            return chk_wh
        return [(w, h) if max(w, h) <= chk_px[0] else (max(1, w * chk_px[0] // max(w, h)),
                                                      max(1, h * chk_px[0] // max(w, h))) for w, h in chk_wh]

    def est():
        wh = _chk() + [hm_wh] * len(gsel)
        return est_tokens(len(z1), v3_b, 2 * len(crop_items), len(frame_map), text_b, wh)

    while est() > _TOK_TARGET:  # media ladder
        if crop_items:
            crop_items.pop()
        elif len(gsel) > 1:
            gsel.pop()
        elif slots > max(MP4_SLOTS_MIN, must_n):
            slots -= 1
            frame_sel, frame_map, must_n = select_frames(gops, slots)
        elif chk_px[0] is None and any(max(w, h) > _CHECK_LADDER_PX for w, h in chk_wh):
            chk_px[0] = _CHECK_LADDER_PX
        else:
            break
    if est() > _TOK_HARD:
        raise VqaError("E_BUDGET", {"part": "tokens", "est_tok": est(), "cap": _TOK_HARD})
    files = {"z1.txt": z1}
    v1, v2 = [], []
    for g in gsel:
        tf = [batch["tiles"](f) for f in _frame_ids(g)]
        if placebo:
            tf = [_roll_tiles(a, shift, gw, gh) for a in tf]
        png = heatmap_png(W, H, tf)
        name = "heatmap_g" + str(g["g"]) + ".png"
        files[name] = png
        v1.append({"g": g["g"], "file": name, "sha256": canon.sha256_bytes(png)})
    for it in crop_items:
        reg = true_regions[back.get(it["tr"]["r_rep"], it["tr"]["r_rep"])]
        rf = _fid(it["tr"]["r_rep"])
        rec = {"item": it["id"], "region": it["tr"]["r_rep"]}
        previous = [f for f in captured if f < rf]
        for side, fr in (("before", previous[-1] if previous else rf), ("after", rf)):
            png, box, fac = crop_png(batch["frames"](fr), reg["bbox_t"], s)
            name = "crops/" + it["tr"]["r_rep"].replace(":", "_") + "_" + side + ".png"
            files[name] = png
            rec[side] = {"file": name, "frame": fr, "box": box, "scale": fac, "sha256": canon.sha256_bytes(png)}
        v2.append(rec)
    mp4 = {"frames": frame_sel, "frame_map": frame_map, "must_keep": must_n}
    files["mp4_frames.json"] = canon.dumps(mp4).encode("utf-8")
    pk = {
        "schema": "vqa.pack/v2", "batch": int(batch_no), "grid": grid, "params_sha256": batch["params_sha256"],
        "z1_version": Z1_VERSION, "z1_sha256": canon.sha256_bytes(z1), "z1_bytes": len(z1),
        "cap_bytes": cap, "gop_cap_bytes": PARAMS["PACK_GOP_B"], "v3_bytes": v3_b, "v3": v3,
        "v1": v1, "v2": v2, "frame_map": frame_map, "gops": [ids.gop_id(int(g["g"])) for g in gops],
        "est_tok": est(), "mp4_frames_sha256": canon.sha256_bytes(files["mp4_frames.json"]),
    }
    if placebo:
        pk["placebo"] = dict(shift)
    pk["crop_frames"] = sorted({rec[side]["frame"] for rec in v2 for side in ("before", "after")})
    if chk_px[0] is not None:
        pk["check_png_max_side"] = chk_px[0]
    files["pack.json"] = canon.dumps(pk).encode("utf-8")
    return PackResult(pk, files, new_acc)


# ---------------------------------------------------------------- judge request + response cache (CDESIGN_CENG F6)
class JudgeRequest:
    """One judge call per pack: byte-stable static prefix first, dynamic suffix after it."""

    def __init__(self, prefix, suffix):
        self.prefix, self.suffix = prefix, suffix
        self.prefix_sha256 = canon.sha256_bytes(prefix)
        self.body = prefix + b"\n--DYNAMIC--\n" + suffix
        self.request_sha256 = canon.sha256_bytes(self.body)


# @anchor:vc.s6.build_request
def build_request(pr, static_extra=b""):
    """Prefix = Z1 text + caller static bytes (rubric, schema); suffix = canonical pack.json plus media name/sha rows.
    No run ids, paths or timestamps enter the prefix, so it is identical across batches."""
    if not isinstance(static_extra, (bytes, bytearray)):
        raise VqaError("E_USAGE", {"reason": "static_extra must be bytes"})
    prefix = pr.files["z1.txt"] + b"\n" + bytes(static_extra)
    media = [{"file": k, "sha256": canon.sha256_bytes(v)} for k, v in sorted(pr.files.items())
             if k not in ("z1.txt", "pack.json")]
    suffix = canon.dumps({"pack": pr.pack, "media": media}).encode("utf-8")
    return JudgeRequest(prefix, suffix)


# @anchor:vc.s6.response_cache
class ResponseCache:
    """Content-addressed response store keyed by request sha256; root is passed in (vmem L2 dir by default at the caller)."""

    def __init__(self, root):
        self.root = str(root)
        self.hits = self.misses = 0

    def _path(self, sha):
        if not (isinstance(sha, str) and len(sha) == 64 and all(c in "0123456789abcdef" for c in sha)):
            raise VqaError("E_USAGE", {"reason": "bad cache key"})
        return os.path.join(self.root, sha[:2], sha + ".resp")

    def get(self, sha):
        p = self._path(sha)
        try:
            with open(p, "rb") as fh:
                data = fh.read()
        except OSError:
            self.misses += 1
            return None
        self.hits += 1
        return data

    def put(self, sha, data):
        p = self._path(sha)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        canon.write_atomic(p, bytes(data))


# @anchor:vc.s6.judge_batch
def judge_batch(pr, call, cache, static_extra=b"", budget=None, retry=False):
    """Cache hit -> stored bytes, call not invoked. Miss -> exactly one call(request) -> bytes, then stored.
    Dispatch requires one shared RunTokenBudget across every call/retry of a run. Cache hits consume no allowance.
    Returns (response_bytes, receipt)."""
    req = build_request(pr, static_extra)
    # Include the caller rubric and request envelope, which are unavailable to pack().
    extra_b = max(0, len(req.suffix) - pr.pack["v3_bytes"])
    tokens = pr.pack["est_tok"] + -(-len(static_extra) // 4) + -(-extra_b // 4)
    if tokens > _TOK_TARGET:
        raise VqaError("E_BUDGET", {"part": "request_tokens", "est_tok": tokens, "cap": _TOK_TARGET})
    data = cache.get(req.request_sha256)
    state = "hit"
    if data is None:
        state = "miss"
        if not isinstance(budget, RunTokenBudget):
            raise VqaError("E_USAGE", {"reason": "judge dispatch requires a shared RunTokenBudget"})
        budget.charge(tokens, retry=retry)
        data = call(req)
        if not isinstance(data, (bytes, bytearray)):
            raise VqaError("E_USAGE", {"reason": "call must return bytes"})
        cache.put(req.request_sha256, data)
    rc = {"request_sha256": req.request_sha256, "prefix_sha256": req.prefix_sha256, "prefix_bytes": len(req.prefix),
          "request_bytes": len(req.body), "cache": state, "response_sha256": canon.sha256_bytes(bytes(data))}
    return bytes(data), rc


class RunTokenBudget:
    """SPEC_TIME §6 run allowance; charge before dispatch, including failed provider attempts.
    A caller must share this object across its run's judge_batch calls and retries.
    """
    def __init__(self):
        self.tokens = 0
        self.calls = 0
        self.retries = 0
        self.primary_tokens = 0

    def charge(self, tokens, retry=False):
        if type(tokens) is not int or tokens < 0 or type(retry) is not bool:
            raise VqaError("E_USAGE", {"reason": "invalid token charge"})
        cap = PARAMS["PACK_RETRY_TOK"] if retry or self.retries else PARAMS["PACK_RUN_TOK"]
        if (tokens > PARAMS["PACK_CALL_TOK"] or self.tokens + tokens > cap
                or (retry and self.retries >= _RETRY_MAX)
                or (not retry and (self.primary_tokens + tokens > PARAMS["PACK_RUN_TOK"]))):
            raise VqaError("E_BUDGET", {"part": "run_tokens", "tokens": self.tokens, "cap": cap})
        self.tokens += tokens
        if retry:
            self.retries += 1
        else:
            self.calls += 1
            self.primary_tokens += tokens
