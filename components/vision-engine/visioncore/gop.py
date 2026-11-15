# @module {"id":"vc.s5","layer":3,"stage":"S5","owner":"TIME","deps":["vc.ids","vc.layout","vc.errors","vc.p","vc.canon"],"inputs":"tile tables per frame (32 B records), regions + core-link per frame, global mv","outputs":"gops.bin blobs, gops.jsonl records","invariants":["integer-only","deterministic","padding-free headers"],"anti_uses":["float thresholds","re-matching regions","per-engine branches"],"oracle":"tests/test_gop.py","book":"HB-VC-s5-gop"}
"""@module vc.s5 L3 - S5 temporal codec (SPEC_TIME §2): keyframe + XOR-RLE deltas, cuts, tracks, cycles.

Identity is on uncompressed bytes (SPEC_INT I10): zlib is storage only. Integer only; no clock, no RNG.
Pads in PHdr MUST be zero (hash over padding-free state); decoders reject non-zero pads.
"""
import hashlib
import re
import struct
import zlib
from functools import lru_cache
from pathlib import Path

import numpy as np

from . import canon, ids, layout, params as P
from .errors import VqaError

HDR_FMT = "<4sHHHHHHIHHIIQQ"  # magic ver hdr_len gw gh tile_px K f0 n_frames flags izlen body_len key_sha8 body_sha8
HDR_SIZE = 48
PHDR_FMT = "<IHHhhIII"  # t n_dirty pad gdx gdy gshare_ppm dlen pad
PHDR_SIZE = 24
assert struct.calcsize(HDR_FMT) == HDR_SIZE and struct.calcsize(PHDR_FMT) == PHDR_SIZE
MAGIC = b"VGOP"
F_CUT_START, F_FLASH, F_RUN_END = 1, 2, 4
FLAGS_MASK = 7
FROZEN_TRACK_PPM = 500000  # SPEC_INT I3: track FROZEN/GLOBAL when >= 500000 ppm of its tiles
CYCLE_P_MAX = 8
_ZEROS = re.compile(b"\x00*")
_Z4 = b"\x00\x00\x00\x00"
TRACK_FLAG_ORDER = ("APPEAR", "VANISH", "SPLIT", "MERGE", "FROZEN", "FLICKER", "DRIFT", "CYCLE", "GLOBAL")


def _gerr(code, **detail):
    return VqaError(code, detail, rc=2)


# ------------------------------------------------------------------ delta token codec (VG-0098 §4.3)
def rle_bound(n):  # @anchor:vc.s5.rle_bound
    return n + 2 + 2 * (n // 255)


def rle_encode(data):  # @anchor:vc.s5.rle
    """Tokens [z u8][l u8][l literals]; zero run capped at 255; literal run stops at l=255 or 4 zero bytes ahead."""
    data = bytes(data)
    n = len(data)
    out = bytearray()
    pos = 0
    while pos < n:
        z = min(_ZEROS.match(data, pos).end() - pos, 255)
        pos += z
        idx = data.find(_Z4, pos, pos + 258)
        end = min(pos + 255, n)
        if idx != -1 and idx < end:
            end = idx
        out.append(z)
        out.append(end - pos)
        out += data[pos:end]
        pos = end
    return bytes(out)


def rle_decode(buf, n):  # @anchor:vc.s5.rle_decode
    buf = bytes(buf)
    if len(buf) > rle_bound(n):
        raise _gerr("E_DELTA_BOUND", dlen=len(buf), bound=rle_bound(n))
    out = bytearray()
    pos, m = 0, len(buf)
    while pos < m:
        if pos + 2 > m:
            raise _gerr("E_DELTA_TOKEN", at=pos, reason="truncated token header")
        z, ln = buf[pos], buf[pos + 1]
        pos += 2
        if pos + ln > m:
            raise _gerr("E_DELTA_TOKEN", at=pos, reason="literal overrun")
        out += b"\x00" * z
        out += buf[pos:pos + ln]
        pos += ln
        if len(out) > n:
            raise _gerr("E_DELTA_LEN", have=len(out), want=n)
    if len(out) != n:
        raise _gerr("E_DELTA_LEN", have=len(out), want=n)
    return bytes(out)


# ------------------------------------------------------------------ grid helpers (stdlib ids/layout only)
@lru_cache(maxsize=8)
def _cells(gw, gh):
    """-> (mortons u16[N] ascending, tx i32[N], ty i32[N], mb starts i64[], mb values u16[])."""
    pairs = sorted((layout.morton(tx, ty), tx, ty) for ty in range(gh) for tx in range(gw))
    m = np.array([p[0] for p in pairs], dtype=np.uint16)
    tx = np.array([p[1] for p in pairs], dtype=np.int32)
    ty = np.array([p[2] for p in pairs], dtype=np.int32)
    mb = m >> 6
    starts = np.nonzero(np.concatenate(([True], mb[1:] != mb[:-1])))[0]
    for a in (m, tx, ty, starts):
        a.setflags(write=False)
    return m, tx, ty, starts, mb[starts]


def _rec_bytes(tiles, n):
    a = np.ascontiguousarray(tiles)
    if a.dtype.itemsize != layout.TILE_SIZE or a.shape != (n,):
        raise _gerr("E_VC_FORMAT", reason="tile table must be %d records of 32 B" % n)
    return a.view(np.uint8).reshape(n, layout.TILE_SIZE)


def dirty_mask(tiles):
    return np.ascontiguousarray(tiles)["cls"] >= layout.CLS_DIRTY_MIN


# ------------------------------------------------------------------ keyframes and cuts
def dirty_ppm_of(n_dirty, n):
    return n_dirty * 1000000 // n


def cut_flags(dirty_ppm, p=None):  # @anchor:vc.s5.cuts
    p = P.PARAMS if p is None else p
    hard, soft = p["CUT_PPM_1F"], p["CUT_PPM_2F"]
    n = len(dirty_ppm)
    c = [False] * n
    for t in range(1, n):
        nxt = dirty_ppm[t + 1] if t + 1 < n else -1
        c[t] = dirty_ppm[t] >= hard or (dirty_ppm[t] >= soft and nxt >= soft)
    return c


def plan_gops(dirty_ppm, p=None):  # @anchor:vc.s5.keyframe
    """-> [{"start","end","cut","flash"}]; I-frame at 0, every GOP_K frames and at every cut."""
    p = P.PARAMS if p is None else p
    cuts = cut_flags(dirty_ppm, p)
    n = len(dirty_ppm)
    starts = []
    for t in range(n):
        if t == 0 or cuts[t] or t - starts[-1] >= p["GOP_K"]:
            starts.append(t)
    plan = []
    for i, s in enumerate(starts):
        e = starts[i + 1] if i + 1 < len(starts) else n
        plan.append({"start": s, "end": e, "cut": cuts[s], "flash": cuts[s] and s + 1 < n and cuts[s + 1]})
    return plan


# ------------------------------------------------------------------ gop binary
def zlib_bound(n):
    return n + (n >> 12) + (n >> 14) + (n >> 25) + 13


def max_gop_bytes(N, K, gw, gh):  # @anchor:vc.s5.max_bytes
    """Hard worst-case size of one GOP (SPEC_TIME §2.5)."""
    n_mb = len(_cells(gw, gh)[3])
    pmax = PHDR_SIZE + 8 * n_mb + rle_bound(layout.TILE_SIZE * N)
    return HDR_SIZE + zlib_bound(layout.TILE_SIZE * N) + (K - 1) * pmax


def _sha8(b):
    return int.from_bytes(hashlib.sha256(b).digest()[:8], "little")


def _pack_header(gw, gh, s, K, f0, n_frames, flags, izlen, body_len, key_sha8, body_sha8):
    return struct.pack(HDR_FMT, MAGIC, 1, HDR_SIZE, gw, gh, s, K, f0, n_frames, flags, izlen, body_len,
                       key_sha8, body_sha8)


def parse_header(b):  # @anchor:vc.s5.parse_header
    if len(b) < HDR_SIZE:
        raise _gerr("E_GOP_MAGIC", reason="short header")
    f = struct.unpack_from(HDR_FMT, b, 0)
    if f[0] != MAGIC:
        raise _gerr("E_GOP_MAGIC", magic=f[0].hex())
    if f[1] != 1 or f[2] != HDR_SIZE:
        raise _gerr("E_GOP_VERSION", ver=f[1], hdr_len=f[2])
    if not (1 <= f[3] <= layout.AXIS_MAX and 1 <= f[4] <= layout.AXIS_MAX):
        raise _gerr("E_GOP_GRID", gw=f[3], gh=f[4])
    if f[9] & ~FLAGS_MASK:
        raise _gerr("E_GOP_PAD", reason="reserved flag bits", flags=f[9])
    return dict(zip(("magic", "ver", "hdr_len", "gw", "gh", "tile_px", "K", "f0", "n_frames", "flags", "izlen",
                     "body_len", "key_sha8", "body_sha8"), f))


def split_blobs(data):
    """Walk a gops.bin and yield (offset, length) of every GOP."""
    off, out = 0, []
    while off < len(data):
        h = parse_header(data[off:off + HDR_SIZE])
        ln = HDR_SIZE + h["izlen"] + h["body_len"]
        if off + ln > len(data):
            raise _gerr("E_GOP_MAGIC", reason="truncated blob", offset=off)
        out.append((off, ln))
        off += ln
    return out


def _mask_words(dirty, gw, gh):
    m, _, _, starts, _ = _cells(gw, gh)
    bits = np.where(dirty, np.left_shift(np.uint64(1), (m & 63).astype(np.uint64)), np.uint64(0))
    return np.bitwise_or.reduceat(bits.astype(np.uint64), starts).astype("<u8")


def _encode_pframe(t, rec, ref, dirty, gm, gw, gh):
    """rec/ref: uint8[N,32]; commits ref in place. -> bytes."""
    idx = np.nonzero(dirty)[0]
    x = np.bitwise_xor(rec[idx], ref[idx]).tobytes()
    delta = rle_encode(x)
    ref[idx] = rec[idx]
    gdx, gdy, share = gm
    hdr = struct.pack(PHDR_FMT, t, len(idx), 0, gdx, gdy, share, len(delta), 0)
    return hdr + _mask_words(dirty, gw, gh).tobytes() + delta


class Gop:
    """One encoded GOP (blob = header | zlib I-frame | P-frames) plus the host-side description."""

    def __init__(self, **kw):
        self.__dict__.update(kw)


def _clip16(v):
    return max(-32768, min(32767, int(v)))


def _global_triplet(gmv, tiles):
    gdx, gdy = (0, 0) if not gmv else (_clip16(gmv[0]), _clip16(gmv[1]))
    a = np.ascontiguousarray(tiles)
    nz = a["cls"] != 0
    tot = int(nz.sum())
    g = int((nz & ((a["flags"] & 64) != 0)).sum())
    return gdx, gdy, (g * 1000000 // tot if tot else 0)


def encode_run(tiles_seq, grid, frame_nums=None, global_motion=None, regions=None, links=None, params=None):  # @anchor:vc.s5.encode_run
    """tiles_seq: per-frame tile tables (N records of 32 B, ascending Morton); grid: gw, gh, s, N, ident.
    global_motion[i] = (dx, dy) px or None; regions[i] = list of core-region rows; links[i] = core-link object
    for frame i-1 -> i. Returns [Gop]."""
    p = P.PARAMS if params is None else params
    tl = [np.ascontiguousarray(t) for t in tiles_seq]
    nf = len(tl)
    if nf == 0:
        raise _gerr("E_VC_FORMAT", reason="empty run")
    frames = list(range(nf)) if frame_nums is None else [int(f) for f in frame_nums]
    if len(frames) != nf or any(b <= a for a, b in zip(frames, frames[1:])):
        raise _gerr("E_VC_FORMAT", reason="frame_nums must be strictly increasing, one per frame")
    N, gw, gh = grid.N, grid.gw, grid.gh
    recs = [_rec_bytes(t, N) for t in tl]
    ppm = [dirty_ppm_of(int(dirty_mask(t).sum()), N) for t in tl]
    plan = plan_gops(ppm, p)
    out = []
    for gi, pl in enumerate(plan):
        a, b = pl["start"], pl["end"]
        raw_i = recs[a].tobytes()
        iz = zlib.compress(raw_i, 6)
        ref = recs[a].copy()
        pframes, gm_rows = [], []
        for i in range(a + 1, b):
            gm = _global_triplet(global_motion[i] if global_motion else None, tl[i])
            pframes.append(_encode_pframe(frames[i], recs[i], ref, dirty_mask(tl[i]), gm, gw, gh))
            if gm[0] or gm[1]:
                gm_rows.append([frames[i], gm[0], gm[1], gm[2]])
        body = b"".join(pframes)
        flags = (F_CUT_START if pl["cut"] else 0) | (F_FLASH if pl["flash"] else 0) | \
                (F_RUN_END if gi == len(plan) - 1 else 0)
        hdr_args = (gw, gh, grid.s, p["GOP_K"], frames[a], b - a, flags)
        key_sha8, body_sha8 = _sha8(raw_i), _sha8(body)
        blob = _pack_header(*hdr_args, len(iz), len(body), key_sha8, body_sha8) + iz + body
        canon_hdr = _pack_header(*hdr_args, 0, len(body), key_sha8, 0)
        canon_sha = hashlib.sha256(canon_hdr + raw_i + body).hexdigest()  # zlib-version independent identity
        blob_sha = hashlib.sha256(blob).hexdigest()  # stored bytes == gops.bin[offset:offset+length]
        loc = slice(a, b)
        tracks, events = build_tracks(frames[loc], tl[loc], (regions or [None] * nf)[loc],
                                      None if links is None else links[loc], grid)
        cycles = find_cycles(frames[loc], tl[loc], grid, first_hash_exact=(a == 0))
        _attach_cycles(tracks, cycles, grid)
        for c in cycles:
            c.pop("rank")
        events = _events(events, pl, frames, a)
        out.append(Gop(g=gi, f0=frames[a], f1=frames[b - 1], frame_ids=frames[a:b], n_frames=b - a, cut=pl["cut"], flash=pl["flash"],
                       flags=flags, blob=blob, key_sha256=hashlib.sha256(raw_i).hexdigest(),
                       blob_sha256=blob_sha, canon_sha256=canon_sha, delta_bytes=len(body), tracks=[_track_out(t) for t in tracks],
                       cycles=cycles, glob=gm_rows, events=events, dirty_ppm=ppm[loc]))
    return out


def _events(ev, pl, frames, a):
    out = list(ev)
    if pl["cut"]:
        out.append((frames[a], 0, "CUT", ids.global_id(frames[a])))
    if pl["flash"]:
        out.append((frames[a], 0, "FLASH", ids.global_id(frames[a])))
    out.sort()
    return [[e[2], e[3]] for e in out]


# ------------------------------------------------------------------ decoder
def decode_gop(blob):  # @anchor:vc.s5.decode_gop
    """-> (header, [(frame, committed table bytes N*32)]). Verifies sha8 prefixes and zero pads."""
    h = parse_header(blob)
    gw, gh = h["gw"], h["gh"]
    m, _, _, starts, mbs = _cells(gw, gh)
    N = len(m)
    iz, body = blob[HDR_SIZE:HDR_SIZE + h["izlen"]], blob[HDR_SIZE + h["izlen"]:]
    if len(body) != h["body_len"]:
        raise _gerr("E_GOP_MAGIC", reason="body length", have=len(body), want=h["body_len"])
    try:
        raw = zlib.decompress(iz)
    except zlib.error as e:
        raise VqaError("E_GATE", {"reason": "g_blob_integrity", "zlib": str(e)[:60]}, rc=5)
    if len(raw) != N * layout.TILE_SIZE:
        raise _gerr("E_GOP_MAGIC", reason="I-frame size")
    if _sha8(raw) != h["key_sha8"] or _sha8(body) != h["body_sha8"]:
        raise VqaError("E_GATE", {"reason": "g_blob_integrity"}, rc=5)
    ref = np.frombuffer(raw, dtype=np.uint8).reshape(N, layout.TILE_SIZE).copy()
    frames = [(h["f0"], raw)]
    n_mb = len(mbs)
    pos = 0
    for _ in range(h["n_frames"] - 1):
        if pos + PHDR_SIZE > len(body):
            raise _gerr("E_GOP_MAGIC", reason="truncated PHdr")
        t, nd, pad1, _gdx, _gdy, _sh, dlen, pad2 = struct.unpack_from(PHDR_FMT, body, pos)
        if pad1 or pad2:
            raise _gerr("E_GOP_PAD", at=pos)
        pos += PHDR_SIZE
        words = np.frombuffer(body, dtype="<u8", count=n_mb, offset=pos)
        pos += 8 * n_mb
        idx = []
        for k, w in enumerate(int(x) for x in words):
            while w:
                bit = (w & -w).bit_length() - 1
                w &= w - 1
                mo = (int(mbs[k]) << 6) | bit
                r = int(np.searchsorted(m, mo))
                if r >= N or int(m[r]) != mo:
                    raise _gerr("E_GOP_MASK", morton=mo)
                idx.append(r)
        if len(idx) != nd:
            raise _gerr("E_GOP_MASK", reason="popcount", have=len(idx), want=nd)
        if pos + dlen > len(body):
            raise _gerr("E_DELTA_TOKEN", reason="delta overruns body")
        x = rle_decode(body[pos:pos + dlen], layout.TILE_SIZE * nd)
        pos += dlen
        if nd:
            ref[idx] ^= np.frombuffer(x, dtype=np.uint8).reshape(nd, layout.TILE_SIZE)
        frames.append((t, ref.tobytes()))
    if pos != len(body):
        raise _gerr("E_GOP_MAGIC", reason="trailing body bytes")
    return h, frames


# ------------------------------------------------------------------ region tracks (consume core-link, never re-match)
def _region_ranks(reg, grid):
    m, tx, ty, _, _ = _cells(grid.gw, grid.gh)
    if reg.get("spans"):
        parts = []
        for lo, hi in reg["spans"]:
            a = int(np.searchsorted(m, int(lo, 16), "left"))
            b = int(np.searchsorted(m, int(hi, 16), "right"))
            parts.append(np.arange(a, b))
        return np.concatenate(parts) if parts else np.zeros(0, dtype=np.int64)
    x0, y0, x1, y1 = reg["bbox_t"]
    return np.nonzero((tx >= x0) & (tx <= x1) & (ty >= y0) & (ty <= y1))[0]


def _id_strings(entry):
    if isinstance(entry, str):
        return [entry]
    return [x for x in entry if isinstance(x, str)] if isinstance(entry, (list, tuple)) else []


def build_tracks(frames, tiles, regions, links, grid):  # @anchor:vc.s5.tracks
    """-> (tracks, events). tracks: internal dicts; events: (frame, rank, name, id)."""
    track_of, tracks, events = {}, {}, []
    prev_regs = []
    for li, f in enumerate(frames):
        regs = sorted(regions[li] or [], key=lambda r: r["id"]) if regions is not None else []
        lk = (links[li] if links is not None and li > 0 else None) or {}
        pairs = [(x[0], x[1]) for x in lk.get("links", [])]
        linked_in = {r: q for q, r in pairs}
        linked_out = {q for q, _ in pairs}
        cls_arr = tiles[li]["cls"]
        for reg in regs:
            rid = reg["id"]
            q = linked_in.get(rid)
            key = track_of.get(q) if q is not None else None
            if key is None:
                _, fld = ids.parse_any(rid)
                key = ids.track_id(fld["frame"], fld["morton"])
                tracks[key] = {"k": key, "first": fld["frame"], "last": fld["frame"], "cls_mask": 0, "mv": {},
                               "flags": set(), "n_tiles_max": 0, "rep": None, "ranks": {}}
                if li > 0 and links is not None:
                    tracks[key]["flags"].add("APPEAR")
                    events.append((f, int(fld["morton"]), "APPEAR", key))
            track_of[rid] = key
            tr = tracks[key]
            tr["last"] = f
            tr["cls_mask"] |= 1 << layout.cls_id(reg["cls"])
            mv = tuple(reg.get("mv") or (0, 0))
            tr["mv"][mv] = tr["mv"].get(mv, 0) + 1
            tr["n_tiles_max"] = max(tr["n_tiles_max"], int(reg["n_tiles"]))
            cand = (int(reg.get("r_sum", 0)), -f, rid)
            if tr["rep"] is None or cand[:2] > tr["rep"][:2]:
                tr["rep"] = cand
            tr["ranks"].setdefault(f, []).append(_region_ranks(reg, grid))
        if links is not None and li > 0:
            for q in prev_regs:
                if q["id"] not in linked_out and q["id"] in track_of:
                    key = track_of[q["id"]]
                    tracks[key]["flags"].add("VANISH")
                    events.append((f, int(q["id"].split(":")[1], 16), "VANISH", key))
            for name in ("split", "merge"):
                for ent in lk.get(name, []):
                    for rid in _id_strings(ent):
                        if rid in track_of:
                            tracks[track_of[rid]]["flags"].add(name.upper())
        prev_regs = regs
    for key, tr in tracks.items():
        allr = []
        for f, parts in sorted(tr["ranks"].items()):
            r = np.unique(np.concatenate(parts)) if parts else np.zeros(0, dtype=np.int64)
            allr.append(r)
            li = frames.index(f)
            if len(r):
                cls, fl = tiles[li]["cls"][r], tiles[li]["flags"][r]
                if int((cls == 5).sum()) * 1000000 // len(r) >= FROZEN_TRACK_PPM:
                    tr["flags"].add("FROZEN")
                if int((cls == 6).sum()):
                    tr["flags"].add("DRIFT")
                if int((cls == 4).sum()):
                    tr["flags"].add("FLICKER")
                if int(((fl & 64) != 0).sum()) * 1000000 // len(r) >= FROZEN_TRACK_PPM:
                    tr["flags"].add("GLOBAL")
        tr["all_ranks"] = np.unique(np.concatenate(allr)) if allr else np.zeros(0, dtype=np.int64)
    return [tracks[k] for k in sorted(tracks, key=_track_sort)], events


def _track_sort(k):
    f, m = k[1:].split(":")
    return (int(f), int(m, 16))


def _attach_cycles(tracks, cycles, grid):
    for tr in tracks:
        tr["period"] = 0
        rs = set(int(x) for x in tr["all_ranks"])
        ps = [c["p"] for c in cycles if c["rank"] in rs]
        if ps:
            tr["period"] = min(ps)
            tr["flags"].update(("CYCLE", "FLICKER"))


def _track_out(tr):
    mv = sorted(tr["mv"].items(), key=lambda kv: (-kv[1], kv[0]))[0][0] if tr["mv"] else (0, 0)
    return {"k": tr["k"], "first": tr["first"], "last": tr["last"], "cls_mask": tr["cls_mask"],
            "mv_mode": [int(mv[0]), int(mv[1])], "period": tr.get("period", 0),
            "flags": [f for f in TRACK_FLAG_ORDER if f in tr["flags"]], "n_tiles_max": tr["n_tiles_max"],
            "r_rep": tr["rep"][2] if tr["rep"] else None}


# ------------------------------------------------------------------ cycles
def find_cycles(frames, tiles, grid, *, first_hash_exact=False):  # @anchor:vc.s5.cycles
    """Periods over true hashes; first_hash_exact attests all first-row hashes.

    Only the capture's first frame is currently hashed in full by S2. Later
    GOP I-frames may carry copied clean labels, so GOP membership alone cannot
    grant that exemption. Frame numbers may be sparse and do not attest hashes.
    """
    n = len(frames)
    m = _cells(grid.gw, grid.gh)[0]
    H = np.stack([np.ascontiguousarray(t)["xh64"] for t in tiles])
    CL = np.stack([np.ascontiguousarray(t)["cls"] for t in tiles])
    true_hash = CL >= layout.CLS_DIRTY_MIN
    if first_hash_exact:
        true_hash[0] = True
    active = (CL != 0).any(axis=0)
    assigned = np.zeros(len(m), dtype=bool)
    out = []
    for per in range(2, CYCLE_P_MAX + 1):
        if n < 2 * per:
            break
        exact = true_hash[per:] & true_hash[:-per] & true_hash[per - 1:-1]
        c = (H[per:] == H[:-per]) & (H[per:] != H[per - 1:-1]) & exact
        for r in np.nonzero(c.any(axis=0) & active & ~assigned)[0]:
            col = c[:, r]
            k = 0
            while k < len(col):
                if not col[k]:
                    k += 1
                    continue
                k1 = k
                while k1 + 1 < len(col) and col[k1 + 1]:
                    k1 += 1
                run = k1 - k + 1
                if run >= per:
                    out.append({"t": ids.field_id(frames[k], int(m[r])), "p": per, "n": (run + per) // per,
                                "end": frames[k1 + per], "rank": int(r), "s": k})
                    assigned[r] = True
                k = k1 + 1
    out.sort(key=lambda c: (c["s"], c["rank"], c["p"]))
    for c in out:
        c.pop("s")
    return out


# ------------------------------------------------------------------ outputs
def gop_record(g, grid_ident, params_sha256, offset):  # @anchor:vc.s5.gop_record
    return {"schema": "vqa.gop/v1", "g": g.g, "gop": g.g, "f0": g.f0, "f1": g.f1, "frame_ids": list(g.frame_ids), "n_frames": g.n_frames, "cut": g.cut,
            "offset": offset, "length": len(g.blob), "key_sha256": g.key_sha256,
            "gop_blob_sha256": g.blob_sha256, "gop_canon_sha256": g.canon_sha256, "delta_bytes": g.delta_bytes, "grid": grid_ident,
            "params_sha256": params_sha256, "tracks": g.tracks, "cycles": g.cycles, "global": g.glob,
            "events": g.events, "dirty_ppm": g.dirty_ppm}


def write_gops(out_dir, gops, grid_ident, params_sha256):  # @anchor:vc.s5.write_gops
    """Stage-and-rename writer for gops.bin + gops.jsonl; any failure leaves the target files as before."""
    out_dir = Path(out_dir)
    blob, lines, off = b"", [], 0
    for g in gops:
        lines.append(canon.dumps(gop_record(g, grid_ident, params_sha256, off)))
        blob += g.blob
        off += len(g.blob)
    canon.write_atomic(out_dir / "gops.bin", blob)
    canon.write_atomic(out_dir / "gops.jsonl", "".join(lines))
    return lines
