"""Tile-level ground-truth labels from Blender truth passes (BDESIGN_TRUTH §2.4).

Pure integer numpy; a label is a function of (truth arrays, journal, profile). No Blender needed.
Tile tables are in ascending Morton order over existing cells (visioncore R1 grid).
"""
import hashlib
import json
import math
import os
import struct
import sys

import numpy as np

from . import pin as _pin  # JW9c-1/2: the one engine pin (sha-verified wheel, fail closed), before any engine import

_pin.pin()
from visioncore import grid as vgrid, ids as vids, layout as vlayout  # noqa: E402
from visioncore.errors import VqaError  # noqa: E402

# --- label enum (§2.4) ---
TL = ("STATIC", "INVISIBLE", "SUBPIX", "RIGID", "OCCLUDE", "DEFORM", "SHADE_STRONG",
      "SHADE_WEAK", "SHADE_BAND", "FLICKER_T", "FROZEN_T", "DRIFT_T")
TL_ID = {n: i for i, n in enumerate(TL)}
TL_MAX = len(TL) - 1
EXCLUDED = (TL_ID["INVISIBLE"], TL_ID["SUBPIX"], TL_ID["SHADE_BAND"])
FLAG_EDGE_ONLY, FLAG_BG, FLAG_CAM_MOTION = 1, 2, 4
FLAG_UNOBSERVABLE = 8  # W10F truth amendment: RIGID tile of diag_moved cause a/b (beauty + truth only)
BG_Z = 0xFFFFFFFF
MOV_Q4 = 8            # >= 0.5 px
RIGID_SHARE_PCT = 90
EDGE_RING = 1         # px (Chebyshev); ceil(filter_width)
ANIM_KINDS = ("material", "light", "visibility")

TRUTH_MAGIC = b"BVQTRUT1"
TRUTH_HDR_SIZE = 64
TRUTH_HDR_FMT = "<8sHHHHHHHHII16s16s"   # magic ver s W H gw gh N rec n_frames f0 sha16 zero16
TRUTH_REC_FMT = "<BBbbHH"               # tl flags mv_dx mv_dy ob_major occ_permille
TRUTH_REC_SIZE = 8
assert struct.calcsize(TRUTH_HDR_FMT) == TRUTH_HDR_SIZE
assert struct.calcsize(TRUTH_REC_FMT) == TRUTH_REC_SIZE

FRAME_KEYS = ("ob", "mv", "z")


def _err(code, rc, **detail):
    return VqaError(code, detail, rc=rc)


# --- loading ---
def load_frame(truth_dir, render_index):
    """Read f<RI6>.{ob,mv,z}.npy (+ma,n,uv if present) -> dict of arrays."""
    out = {}
    for key in ("ob", "ma", "mv", "z", "n", "uv"):
        p = os.path.join(truth_dir, "f%06d.%s.npy" % (render_index, key))
        if os.path.exists(p):
            out[key] = np.load(p, allow_pickle=False)
        elif key in FRAME_KEYS:
            raise _err("E_IO", 2, missing=p)
    check_frame(out)
    return out


def check_frame(fr):
    """Dtype/shape contract of §2.1; violations -> rc 2 E_SCHEMA."""
    for k in FRAME_KEYS:
        if k not in fr:
            raise _err("E_SCHEMA", 2, missing=k)
    ob, mv, z = fr["ob"], fr["mv"], fr["z"]
    if ob.dtype != np.uint16 or ob.ndim != 2:
        raise _err("E_SCHEMA", 2, field="ob", dtype=str(ob.dtype))
    if mv.dtype != np.int16 or mv.shape != ob.shape + (2,):
        raise _err("E_SCHEMA", 2, field="mv", dtype=str(mv.dtype), shape=list(mv.shape))
    if z.dtype != np.uint32 or z.shape != ob.shape:
        raise _err("E_SCHEMA", 2, field="z", dtype=str(z.dtype), shape=list(z.shape))


def check_ob_in_manifest(ob, pass_indices):
    """T-TR-QUANT: ob values must be in the manifest pass_index set or 0."""
    allowed = set(int(v) for v in pass_indices) | {0}
    bad = sorted(set(int(v) for v in np.unique(ob)) - allowed)
    if bad:
        raise _err("E_TRUTH_QUANT", 2, values=bad[:8])


# --- pixel predicates ---
def round_px(q4):
    """q4 (1/16 px) -> integer px, round half away from zero."""
    q = q4.astype(np.int32)
    return np.sign(q) * ((np.abs(q) + 8) >> 4)


def edge_mask(ob, ring=EDGE_RING):
    """Pixels within `ring` px (Chebyshev) of an ob boundary."""
    p = np.pad(ob, 1, mode="edge")
    h, w = ob.shape
    b = np.zeros(ob.shape, dtype=bool)
    for dy in (-1, 0, 1):
        for dx in (-1, 0, 1):
            if dy or dx:
                b |= p[1 + dy:1 + dy + h, 1 + dx:1 + dx + w] != ob
    if ring <= 1:
        return b
    e = b
    for _ in range(ring - 1):
        pe = np.pad(e, 1, mode="constant")
        n = e.copy()
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                n |= pe[1 + dy:1 + dy + h, 1 + dx:1 + dx + w]
        e = n
    return e


def pixel_predicates(prev, cur, anim_ob=None, anim_all=False, ring=EDGE_RING):
    """-> dict of bool HxW: mov, occ, anim, edge, obchg; plus mvpx int32 HxWx2."""
    ob, mv, z = cur["ob"], cur["mv"], cur["z"]
    h, w = ob.shape
    mvpx = round_px(mv)
    mov = np.max(np.abs(mv.astype(np.int32)), axis=2) >= MOV_Q4
    yy, xx = np.mgrid[0:h, 0:w]
    qx, qy = xx - mvpx[..., 0], yy - mvpx[..., 1]
    inside = (qx >= 0) & (qx < w) & (qy >= 0) & (qy < h)
    qxc, qyc = np.clip(qx, 0, w - 1), np.clip(qy, 0, h - 1)
    pob = prev["ob"][qyc, qxc]
    pz = prev["z"][qyc, qxc].astype(np.int64)
    cz = z.astype(np.int64)
    both = (prev["z"][qyc, qxc] != BG_Z) & (z != BG_Z)
    thr = np.maximum(10, cz // 100)
    occ = ~inside | (pob != ob) | (both & (np.abs(pz - cz) > thr))
    anim = np.zeros(ob.shape, dtype=bool)
    if anim_all:
        anim[:] = True
    elif anim_ob:
        anim = np.isin(ob, np.fromiter(anim_ob, dtype=np.uint16))
    return {"mov": mov, "occ": occ, "anim": anim, "edge": edge_mask(ob, ring),
            "obchg": prev["ob"] != ob, "mvpx": mvpx}


# --- tile reduction ---
def _tile_sum(mask, g):
    ph, pw = g.gh * g.s - g.H, g.gw * g.s - g.W
    m = np.pad(mask.astype(np.int32), ((0, ph), (0, pw)))
    return m.reshape(g.gh, g.s, g.gw, g.s).sum(axis=(1, 3))


def _mort(rowmajor, g):
    return np.asarray(rowmajor).reshape(-1)[g.flat]


def _mode(vals):
    """Lowest most-frequent value of a 1-D int array -> (value, count)."""
    u, c = np.unique(vals, return_counts=True)
    i = int(np.argmax(c))
    return int(u[i]), int(c[i])


def label_frame(prev, cur, g, anim_ob=None, anim_all=False, cam_motion=False,
                resid=None, t_enter=64, t_exit=48, ring=EDGE_RING):
    """Label one frame t >= 1 against t-1.

    resid: optional int array [N] (Morton order) of clean-frame descriptor residuals per tile; without it
    anim-only tiles are SHADE_BAND (excluded) and geometric-but-invisible detection (INVISIBLE) is skipped.
    -> dict: tl u8[N], flags u8[N], mv i8[N,2] (px), ob_major u16[N], occ_permille u16[N] (Morton order).
    """
    if cur["ob"].shape != (g.H, g.W):
        raise _err("E_STALE", 4, reason="grid", shape=list(cur["ob"].shape), grid=g.ident)
    pp = pixel_predicates(prev, cur, anim_ob, anim_all, ring)
    n = _tile_sum(np.ones(cur["ob"].shape, bool), g)
    geo = pp["mov"] | pp["occ"] | pp["obchg"]
    geo_core = (pp["mov"] | pp["occ"]) & ~pp["edge"]
    n_occ = _tile_sum(pp["occ"], g)
    n_geo = _tile_sum(geo, g)
    n_geo_core = _tile_sum(geo_core, g)
    n_mov = _tile_sum(pp["mov"], g)
    n_anim = _tile_sum(pp["anim"], g)
    n_core = n - _tile_sum(pp["edge"], g)
    s = g.s
    tl = np.zeros((g.gh, g.gw), np.uint8)
    flags = np.zeros((g.gh, g.gw), np.uint8)
    mvo = np.zeros((g.gh, g.gw, 2), np.int8)
    obm = np.zeros((g.gh, g.gw), np.uint16)
    resid_rm = None
    if resid is not None:
        resid_rm = np.empty(g.N, dtype=np.int64)
        resid_rm[g.flat] = np.asarray(resid, dtype=np.int64)
        resid_rm = resid_rm.reshape(g.gh, g.gw)
    for ty in range(g.gh):
        for tx in range(g.gw):
            ys, xs = slice(ty * s, min((ty + 1) * s, g.H)), slice(tx * s, min((tx + 1) * s, g.W))
            nn = int(n[ty, tx])
            om, _ = _mode(cur["ob"][ys, xs].reshape(-1))
            obm[ty, tx] = om
            if om == 0:
                flags[ty, tx] |= FLAG_BG
            if cam_motion:
                flags[ty, tx] |= FLAG_CAM_MOTION
            ng, ngc, no = int(n_geo[ty, tx]), int(n_geo_core[ty, tx]), int(n_occ[ty, tx])
            na, nm = int(n_anim[ty, tx]), int(n_mov[ty, tx])
            if ng and ngc == 0:
                flags[ty, tx] |= FLAG_EDGE_ONLY
            r = None if resid_rm is None else int(resid_rm[ty, tx])
            if ng == 0 and na == 0:
                t = TL_ID["STATIC"]
            elif ng and r is not None and r <= t_exit // 2 and na == 0:
                t = TL_ID["INVISIBLE"]
            elif ng and (ngc == 0 or (nm == 0 and no == 0)):
                t = TL_ID["SUBPIX"]
            elif ng:
                t = TL_ID["DEFORM"]
                mxy = pp["mvpx"][ys, xs].reshape(-1, 2)
                core = ~pp["edge"][ys, xs].reshape(-1)
                if no <= nn // 100 and int(core.sum()) > 0:
                    key = (mxy[core, 0] + 512) * 1024 + (mxy[core, 1] + 512)
                    val, cnt = _mode(key)
                    dx, dy = val // 1024 - 512, val % 1024 - 512
                    if (dx or dy) and cnt * 100 >= RIGID_SHARE_PCT * int(core.sum()):
                        t = TL_ID["RIGID"]
                        mvo[ty, tx] = (max(-127, min(127, dx)), max(-127, min(127, dy)))
                if no > nn // 100:
                    t = TL_ID["OCCLUDE"]
            else:  # animation only
                if r is None:
                    t = TL_ID["SHADE_BAND"]
                elif r >= 2 * t_enter:
                    t = TL_ID["SHADE_STRONG"]
                elif r <= t_exit // 2:
                    t = TL_ID["SHADE_WEAK"]
                else:
                    t = TL_ID["SHADE_BAND"]
            tl[ty, tx] = t
    return {"tl": _mort(tl, g), "flags": _mort(flags, g), "mv": mvo.reshape(-1, 2)[g.flat], "ob_major": _mort(obm, g),
            "occ_permille": _mort((n_occ * 1000) // np.maximum(n, 1), g).astype(np.uint16)}


def static_frame(g, cur=None):
    """Frame 0 (no t-1): every tile STATIC; ob_major/BG flag from the frame if given."""
    N = g.N
    out = {"tl": np.zeros(N, np.uint8), "flags": np.zeros(N, np.uint8), "mv": np.zeros((N, 2), np.int8),
           "ob_major": np.zeros(N, np.uint16), "occ_permille": np.zeros(N, np.uint16)}
    if cur is not None:
        for i in range(N):
            tx, ty = int(g.tx[i]), int(g.ty[i])
            blk = cur["ob"][ty * g.s:(ty + 1) * g.s, tx * g.s:(tx + 1) * g.s].reshape(-1)
            out["ob_major"][i] = _mode(blk)[0]
            if out["ob_major"][i] == 0:
                out["flags"][i] |= FLAG_BG
    return out


# --- journal helpers ---
def animated_at(journal, frame, name_to_index):
    """-> (set of ob/material indices animated at `frame` with kind material|visibility, light_any, camera)."""
    obs, light, cam = set(), False, False
    for a in (journal or {}).get("animated", []):
        if a.get("frame") != frame:
            continue
        k = a.get("kind")
        if k == "light":
            light = True
        elif k == "camera":
            cam = True
        elif k in ("material", "visibility") and a.get("name") in name_to_index:
            obs.add(int(name_to_index[a["name"]]))
    return obs, light, cam


def apply_temporal(lab, frame, journal, name_to_index):
    """Override tl with FLICKER_T/FROZEN_T/DRIFT_T for tiles whose ob_major is named in journal `temporal`
    entries {name, kind: flicker|frozen|drift, frames:[a,b]} active at `frame`."""
    kinds = {"flicker": TL_ID["FLICKER_T"], "frozen": TL_ID["FROZEN_T"], "drift": TL_ID["DRIFT_T"]}
    for t in (journal or {}).get("temporal", []):
        a, b = t["frames"]
        if not a <= frame <= b or t["name"] not in name_to_index or t["kind"] not in kinds:
            continue
        m = (lab["ob_major"] == int(name_to_index[t["name"]]))
        if t["kind"] == "frozen":   # only tiles that are static in this frame
            m &= lab["tl"] == TL_ID["STATIC"]
        lab["tl"][m] = kinds[t["kind"]]
    return lab


def label_sequence(frames, g, journal=None, name_to_index=None, f0=0, resid=None, t_enter=64, t_exit=48,
                   ring=EDGE_RING):
    """frames: list of frame dicts (consecutive render indices from f0). -> list of label dicts."""
    name_to_index = name_to_index or {}
    out = [static_frame(g, frames[0])]
    for i in range(1, len(frames)):
        ri = f0 + i
        obs, light, cam = animated_at(journal, ri, name_to_index)
        lit = [n for n in (journal or {}).get("lit", []) if n in name_to_index]
        if light and lit:  # light reaches only the journaled objects (FC-DLAB-4); no `lit` -> anim_all fallback
            obs = set(obs) | {int(name_to_index[n]) for n in lit}
            light = False
        lab = label_frame(frames[i - 1], frames[i], g, obs, light, cam,
                          None if resid is None else resid[i], t_enter, t_exit, ring)
        out.append(apply_temporal(lab, ri, journal, name_to_index))
    return out


# --- bvqa.truth-tile/v1 ---
def frames_sha16(frames_jsonl_bytes):
    return hashlib.sha256(frames_jsonl_bytes).digest()[:16]


def pack_truth_tiles(g, labels, f0=0, sha16=b"\0" * 16):
    hdr = struct.pack(TRUTH_HDR_FMT, TRUTH_MAGIC, 1, g.s, g.W, g.H, g.gw, g.gh, g.N, TRUTH_REC_SIZE,
                      len(labels), f0, sha16, b"\0" * 16)
    body = bytearray()
    for lab in labels:
        rec = np.zeros(g.N, dtype=[("tl", "u1"), ("fl", "u1"), ("dx", "i1"), ("dy", "i1"),
                                   ("ob", "<u2"), ("oc", "<u2")])
        rec["tl"], rec["fl"] = lab["tl"], lab["flags"]
        rec["dx"], rec["dy"] = lab["mv"][:, 0], lab["mv"][:, 1]
        rec["ob"], rec["oc"] = lab["ob_major"], lab["occ_permille"]
        body += rec.tobytes()
    return hdr + bytes(body)


def parse_truth_tiles(b, g=None):
    """-> (header dict, list of per-frame structured arrays). Grid mismatch -> rc 4 E_STALE; bad data rc 2."""
    if len(b) < TRUTH_HDR_SIZE:
        raise _err("E_SCHEMA", 2, reason="short header")
    f = struct.unpack(TRUTH_HDR_FMT, b[:TRUTH_HDR_SIZE])
    if f[0] != TRUTH_MAGIC or f[1] != 1 or f[8] != TRUTH_REC_SIZE:
        raise _err("E_SCHEMA", 2, reason="bad magic/ver/rec")
    h = {"s": f[2], "W": f[3], "H": f[4], "gw": f[5], "gh": f[6], "N": f[7], "n_frames": f[9],
         "f0": f[10], "sha16": f[11].hex()}
    if g is not None and (h["s"], h["W"], h["H"], h["gw"], h["gh"], h["N"]) != (g.s, g.W, g.H, g.gw, g.gh, g.N):
        raise _err("E_STALE", 4, reason="grid", truth=h, grid=g.ident)
    need = TRUTH_HDR_SIZE + h["n_frames"] * h["N"] * TRUTH_REC_SIZE
    if len(b) != need:
        raise _err("E_SCHEMA", 2, reason="size", have=len(b), need=need)
    dt = np.dtype([("tl", "u1"), ("fl", "u1"), ("dx", "i1"), ("dy", "i1"), ("ob", "<u2"), ("oc", "<u2")])
    arr = np.frombuffer(b, dtype=dt, offset=TRUTH_HDR_SIZE).reshape(h["n_frames"], h["N"])
    if int(arr["tl"].max(initial=0)) > TL_MAX or int(arr["oc"].max(initial=0)) > 1000:
        raise _err("E_SCHEMA", 2, reason="label out of range")
    return h, [arr[i] for i in range(h["n_frames"])]


def mark_unobservable(b, unobs):
    """bvqa.truth-tile/v1 blob + bool unobs [F, N] (Morton) -> new blob with flags |= FLAG_UNOBSERVABLE on unobs.
    Header and every other byte unchanged (bit 3 of the flags byte is free, so v1 readers stay compatible)."""
    h, _ = parse_truth_tiles(b)
    unobs = np.asarray(unobs, bool)
    if unobs.shape != (h["n_frames"], h["N"]):
        raise _err("E_SCHEMA", 2, reason="unobs shape", got=list(unobs.shape), want=[h["n_frames"], h["N"]])
    dt = np.dtype([("tl", "u1"), ("fl", "u1"), ("dx", "i1"), ("dy", "i1"), ("ob", "<u2"), ("oc", "<u2")])
    arr = np.frombuffer(b, dtype=dt, offset=TRUTH_HDR_SIZE).reshape(h["n_frames"], h["N"]).copy()
    arr["fl"][unobs] |= FLAG_UNOBSERVABLE
    return bytes(b[:TRUTH_HDR_SIZE]) + arr.tobytes()


# --- bvqa.truth-region/v1 ---
_REGION_TL = (TL_ID["RIGID"], TL_ID["OCCLUDE"], TL_ID["DEFORM"], TL_ID["SHADE_STRONG"],
              TL_ID["FLICKER_T"], TL_ID["FROZEN_T"], TL_ID["DRIFT_T"])


def regions_truth(frame, lab, g, index_to_name=None):
    """Per dirty ob in a frame -> list of dict rows (sorted by ob)."""
    index_to_name = index_to_name or {}
    rows = []
    sel = np.isin(lab["tl"], _REGION_TL) & (lab["ob_major"] != 0)
    for ob in sorted(set(int(v) for v in lab["ob_major"][sel])):
        idx = np.nonzero(sel & (lab["ob_major"] == ob))[0]
        morts = [int(g.mortons[i]) for i in idx]
        spans, a = [], morts[0]
        for p, q in zip(morts, morts[1:] + [None]):
            if q is None or q != p + 1:
                spans.append([a, p])
                a = q
        tlm, _ = _mode(lab["tl"][idx].astype(np.int32))
        rig = idx[lab["tl"][idx] == TL_ID["RIGID"]]
        mx = lab["mv"][rig if len(rig) else idx].astype(np.int32)   # region mv = mode over its RIGID tiles
        key = (mx[:, 0] + 128) * 256 + (mx[:, 1] + 128)
        kv, _ = _mode(key)
        rows.append({"frame": frame, "grid": g.ident, "id": vids.region_id(frame, morts[0], g.W, g.H),
                     "ob": ob, "name": index_to_name.get(ob, ""), "tl_major": TL[tlm],
                     "n_tiles": len(idx), "spans": spans, "mv": [kv // 256 - 128, kv % 256 - 128]})
    return rows


def canon_line(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"


# --- CLI ---
try:
    from visioncore import params as _vparams
    T0_DEFAULT = _vparams.PARAMS["T0_Q4"]
    HYST_DEFAULT_PERMILLE = _vparams.PARAMS["HYST_BAND_PERMILLE"]
except Exception:  # engine without params: documented defaults
    T0_DEFAULT, HYST_DEFAULT_PERMILLE = 32, 100


def thresholds(profile):
    """(T_enter, T_exit) from profile T0_Q4 and the hysteresis band (BTRUTH 4, IMPL-GAP-2):
    T_enter = T0*(10^6+BAND)//10^6, T_exit = T0*(10^6-BAND)//10^6, BAND = HYST_BAND_PERMILLE*1000.
    HYST_BAND_PERMILLE is not a profile row; free T_enter/T_exit keys are ignored."""
    t0 = int((profile or {}).get("T0_Q4", T0_DEFAULT))
    band = HYST_DEFAULT_PERMILLE * 1000
    return t0 * (1000000 + band) // 1000000, t0 * (1000000 - band) // 1000000


def ring_from_filter(filter_width):
    """EDGE_RING = ceil(render.filter_width) (>= 1); None -> module default."""
    return EDGE_RING if filter_width is None else max(1, int(math.ceil(float(filter_width) - 1e-9)))


# RIGID/OCCLUDE/DEFORM/SHADE_STRONG: tiles covered by real geometric or strong shading change, plus FLICKER_T/DRIFT_T:
# temporal overrides relabel changing content (B4), which is still scene coverage (ADV-W9-04). FROZEN_T is static.
COV_TL = (3, 4, 5, 6, 9, 11)


def frame_meta(frame, lab):
    """{frame, cov_ppm, cam_motion}: cov_ppm = ppm of grid tiles covered by moving content this frame (no engine
    heuristic, BTRUTH 4: scoring strata only); cam_motion = any tile flagged CAM_MOTION (flags b2)."""
    tl = np.asarray(lab["tl"])
    return {"frame": int(frame), "cov_ppm": int(np.isin(tl, COV_TL).sum()) * 1000000 // max(1, tl.size),
            "cam_motion": int(bool((np.asarray(lab["flags"]) & 4).any()))}


def run(run_dir, profile, out_dir, resid=None):
    """resid: optional int array [F, N] (Morton) of per-frame tile residuals vs t-1 (row 0 unused); enables
    INVISIBLE / SHADE_STRONG / SHADE_WEAK. Shape mismatch -> E_SCHEMA rc 2."""
    truth = os.path.join(run_dir, "truth")
    fpath = os.path.join(truth, "frames.jsonl")
    if not os.path.exists(fpath):
        raise _err("E_IO", 2, missing=fpath)
    with open(fpath, "rb") as fh:
        raw = fh.read()
    rows = [json.loads(x) for x in raw.decode().splitlines() if x.strip()]
    if not rows:
        raise _err("E_SCHEMA", 2, reason="empty frames.jsonl")
    if rows[0].get("motion_blur"):
        raise _err("E_TRUTH_MB", 2)
    W, H = rows[0]["width"], rows[0]["height"]
    g = vgrid.Grid(W, H)
    if not rows[0].get("grid"):
        raise _err("E_SCHEMA", 2, reason="grid null")
    if rows[0]["grid"] != g.ident:
        raise _err("E_STALE", 4, reason="grid", truth=rows[0]["grid"], grid=g.ident)
    ris = [r["render_index"] for r in rows]
    if ris != list(range(ris[0], ris[0] + len(ris))):
        raise _err("E_SCHEMA", 2, reason="non-consecutive render_index")
    journal = {}
    jp = os.path.join(run_dir, "journal.json")
    if os.path.exists(jp):
        with open(jp) as fh:
            journal = json.load(fh)
    n2i, i2n = {}, {}
    mp = os.path.join(run_dir, "scene.manifest.json")
    if os.path.exists(mp):
        with open(mp) as fh:
            man = json.load(fh)
        n2i = {o["name"]: o["pass_index"] for o in man.get("objects", [])}
        i2n = {v: k for k, v in n2i.items()}
    frames = [load_frame(truth, ri) for ri in ris]
    if n2i:
        for fr in frames:
            check_ob_in_manifest(fr["ob"], n2i.values())
    if resid is not None:
        resid = np.asarray(resid)
        if resid.shape != (len(frames), g.N):
            raise _err("E_SCHEMA", 2, reason="resid shape", got=list(resid.shape), want=[len(frames), g.N])
    t_enter, t_exit = thresholds(profile)
    labs = label_sequence(frames, g, journal, n2i, ris[0], resid, t_enter, t_exit,
                          ring_from_filter(rows[0].get("filter_width")))
    os.makedirs(out_dir, exist_ok=True)
    blob = pack_truth_tiles(g, labs, ris[0], frames_sha16(raw))
    with open(os.path.join(out_dir, "tiles_truth.bin"), "wb") as fh:
        fh.write(blob)
    with open(os.path.join(out_dir, "frames_meta.jsonl"), "w") as fh:  # normative side table (BTRUTH 4.1)
        for ri, lab in zip(ris, labs):
            fh.write(canon_line(frame_meta(ri, lab)))
    with open(os.path.join(out_dir, "regions_truth.jsonl"), "w") as fh:
        for ri, lab in zip(ris, labs):
            for r in regions_truth(ri, lab, g, i2n):
                fh.write(canon_line(r))
    return {"code": "OK", "rc": 0, "frames": len(labs), "grid": g.ident, "frame_ids": ris,
            "sha256": hashlib.sha256(blob).hexdigest()}


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(prog="blender_visual_qa.truth.labels")
    ap.add_argument("--run", required=True)
    ap.add_argument("--profile")
    ap.add_argument("--out", required=True)
    ap.add_argument("--resid", help=".npy int array [F, N] of per-frame tile residuals (q4) vs t-1")
    a = ap.parse_args(argv)
    try:
        prof = {}
        if a.profile:
            with open(a.profile) as fh:
                prof = json.load(fh)
        rs = None
        if a.resid:
            if not os.path.exists(a.resid):
                raise _err("E_IO", 2, missing=a.resid)
            rs = np.load(a.resid)
        res = run(a.run, prof, a.out, rs)
        sys.stdout.write(canon_line(res))
        return 0
    except VqaError as e:
        sys.stdout.write(canon_line(e.to_obj()))
        return e.rc


if __name__ == "__main__":
    sys.exit(main())
