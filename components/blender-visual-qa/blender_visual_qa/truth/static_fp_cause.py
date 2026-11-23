"""Attribute static false positives using persisted truth-bench evidence."""
import hashlib
import json
import re
from pathlib import Path

import numpy as np

from . import pin as _pin

_pin.pin()

from . import calibrate, labels, noise_rescore, score  # noqa: E402
from visioncore import layout  # noqa: E402
from visioncore.grid import Grid  # noqa: E402

SCHEMA = "bvqa.static-fp-cause/v1"
REPORT_JSON = "static_fp_cause.json"
SUMMARY_MD = "SUMMARY.md"

CAUSE_SHADE = "SHADE"
CAUSE_EDGE_BLEED = "EDGE_BLEED"
CAUSE_GLOBAL_EXPOSURE = "GLOBAL_EXPOSURE"
CAUSE_LIGHT_CHANGE = "LIGHT_CHANGE"
CAUSE_AA_JITTER = "AA_JITTER"
CAUSE_UNATTRIBUTED = "UNATTRIBUTED"
CAUSE_ABSENT = "ABSENT"

RULE_ORDER = (
    CAUSE_SHADE,
    CAUSE_EDGE_BLEED,
    CAUSE_GLOBAL_EXPOSURE,
    CAUSE_LIGHT_CHANGE,
    CAUSE_AA_JITTER,
    CAUSE_UNATTRIBUTED,
)
ALL_CAUSES = RULE_ORDER + (CAUSE_ABSENT,)

DIRTY_TL = tuple(sorted(tl for tl, (kind, _) in score.EXPECT.items() if kind == "dirty"))

# Default static dirty fraction threshold for GLOBAL_EXPOSURE: 200,000 ppm (20%).
GLOBAL_EXPOSURE_THRESHOLD_PPM = 200000
TOP_FRAMES_LIMIT = 20

GLOBAL_LUMA_KEYS = (
    "global_luma",
    "global_luma_q4",
    "luma_delta",
    "luma_delta_q4",
    "dy_q4",
    "y_delta_q4",
    "luma_shift",
)
GLOBAL_DRIFT_KEYS = (
    "global_drift",
    "drift_signal",
    "drift_ppm",
)


class StaticFpCauseError(ValueError):
    def __init__(self, code, message, **detail):
        super().__init__(message)
        self.code = code
        self.detail = detail
        self.rc = 2


def _fail(code, message, **detail):
    raise StaticFpCauseError(code, message, **detail)


def _int(value, name, lo=0, hi=None):
    if isinstance(value, bool) or not isinstance(value, (int, np.integer)):
        _fail("E_SCHEMA", "%s must be an integer" % name)
    value = int(value)
    if value < lo or (hi is not None and value > hi):
        _fail("E_SCHEMA", "%s out of range" % name)
    return value


def _inside(run, path):
    resolved = path.resolve()
    if run != resolved and run not in resolved.parents:
        _fail("E_ARGS", "input path escapes --run: %s" % path)
    return resolved


def _json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        _fail("E_ABSENT", "ABSENT readable JSON %s: %s" % (path, exc))


def _jsonl(path):
    try:
        return [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    except (OSError, ValueError) as exc:
        _fail("E_ABSENT", "ABSENT readable JSONL %s: %s" % (path, exc))


def _grid(ident):
    match = re.fullmatch(r"grid:(\d+)x(\d+):s(\d+)", str(ident))
    if not match:
        _fail("E_SCHEMA", "invalid grid identity: %r" % (ident,))
    grid = Grid(int(match[1]), int(match[2]))
    if grid.ident != ident:
        _fail("E_SCHEMA", "grid identity disagrees with pinned grid rules")
    return grid


def _parse_cls_token(val):
    """Parse a tile class entry; returns -1 when explicitly unknown/ABSENT."""
    if val is None:
        return -1
    if isinstance(val, str):
        up = val.strip().upper()
        if up in ("ABSENT", "UNKNOWN", "?"):
            return -1
        if up in layout.CLS:
            return layout.CLS.index(up)
        _fail("E_SCHEMA", "unknown tile cls string: %r" % (val,))
    c = _int(val, "tile cls", -1, score.C_DRIFT)
    return c


def load_tile_cls_jsonl(path, grid, expected_frames):
    """Load per-tile engine classes from vision/tile_cls.jsonl.

    Supports either:
      1. One JSON object per frame with key 'cls' (or 'tile_cls'/'classes'/'tiles')
         holding a length-N list in Morton rank order (or a morton->cls mapping).
      2. One JSON object per (frame, morton/tile) cell.
    Unknown or omitted tiles receive -1 and are counted as ABSENT.
    """
    rows = _jsonl(path)
    if not rows:
        _fail("E_ABSENT", "ABSENT records in %s" % path)
    rank = {int(m): i for i, m in enumerate(grid.mortons)}

    first = rows[0]
    if not isinstance(first, dict):
        _fail("E_SCHEMA", "tile_cls.jsonl rows must be JSON objects")

    has_frame_array = any(k in first for k in ("cls", "tile_cls", "classes", "tiles")) and isinstance(
        first.get("cls", first.get("tile_cls", first.get("classes", first.get("tiles")))),
        (list, dict),
    )

    if has_frame_array:
        if len(rows) != expected_frames:
            _fail(
                "E_SCHEMA",
                "tile_cls.jsonl frame count %d != expected %d" % (len(rows), expected_frames),
            )
        cls = np.full((expected_frames, grid.N), -1, dtype=np.int8)
        frame_ids = []
        for f_idx, row in enumerate(rows):
            if "grid" in row and row["grid"] != grid.ident:
                _fail("E_SCHEMA", "tile_cls.jsonl grid mismatch")
            f_num = _int(row.get("frame", f_idx), "tile_cls frame")
            frame_ids.append(f_num)
            arr = None
            for k in ("cls", "tile_cls", "classes", "tiles"):
                if k in row:
                    arr = row[k]
                    break
            if isinstance(arr, list):
                if len(arr) != grid.N:
                    _fail("E_SCHEMA", "tile_cls list length %d != %d" % (len(arr), grid.N))
                if arr and isinstance(arr[0], dict):
                    for item in arr:
                        m = item.get("morton")
                        if isinstance(m, str):
                            m = int(m, 16)
                        r_idx = rank.get(_int(m, "morton"))
                        if r_idx is None:
                            _fail("E_SCHEMA", "out-of-grid Morton in tile_cls.jsonl")
                        cls[f_idx, r_idx] = _parse_cls_token(item.get("cls"))
                else:
                    for i, v in enumerate(arr):
                        cls[f_idx, i] = _parse_cls_token(v)
            elif isinstance(arr, dict):
                for m_key, v in arr.items():
                    m_val = int(m_key, 16) if isinstance(m_key, str) and not m_key.isdigit() else int(m_key)
                    if m_val in rank:
                        cls[f_idx, rank[m_val]] = _parse_cls_token(v)
                    elif 0 <= m_val < grid.N:
                        cls[f_idx, m_val] = _parse_cls_token(v)
                    else:
                        _fail("E_SCHEMA", "out-of-grid key in tile_cls.jsonl")
            else:
                _fail("E_SCHEMA", "missing tile cls array in tile_cls.jsonl row")
        if frame_ids != list(range(frame_ids[0], frame_ids[0] + len(frame_ids))):
            _fail("E_SCHEMA", "tile_cls.jsonl frames must be ordered and consecutive")
    else:
        f_vals = sorted({_int(r.get("frame"), "frame") for r in rows})
        if len(f_vals) != expected_frames or f_vals != list(range(f_vals[0], f_vals[0] + len(f_vals))):
            _fail("E_SCHEMA", "per-tile tile_cls.jsonl frames mismatch")
        frame_ids = f_vals
        f_map = {f_num: idx for idx, f_num in enumerate(frame_ids)}
        cls = np.full((expected_frames, grid.N), -1, dtype=np.int8)
        for r in rows:
            f_idx = f_map[_int(r.get("frame"), "frame")]
            if "morton" in r:
                m = r["morton"]
                m_val = int(m, 16) if isinstance(m, str) else _int(m, "morton")
                if m_val not in rank:
                    _fail("E_SCHEMA", "out-of-grid morton in tile_cls.jsonl")
                t_idx = rank[m_val]
            elif "tile" in r:
                t_idx = _int(r.get("tile"), "tile", 0, grid.N - 1)
            else:
                _fail("E_SCHEMA", "per-tile tile_cls.jsonl row needs morton or tile")
            cls[f_idx, t_idx] = _parse_cls_token(r.get("cls"))

    unknown_count = int((cls < 0).sum())
    return {
        "cls": cls,
        "frame_ids": frame_ids,
        "mixed_class_tiles": unknown_count,
        "clean_class_absent_tiles": 0,
        "class_absent_tiles": unknown_count,
        "jitter_collapsed_tiles": 0,
    }


def _locate_unit_dir(run, row, seed_groups, bench_meta):
    bench = row.get("bench")
    if not isinstance(bench, str) or bench in ("", ".", "..") or Path(bench).name != bench or "\\" in bench:
        _fail("E_SCHEMA", "invalid bench path component: %r" % (bench,))
    seed = _int(row.get("seed"), "row seed")

    candidates = []
    meta = bench_meta.get((bench, seed), {})
    if isinstance(meta.get("seed_group"), str):
        grp = meta["seed_group"]
        if grp == "legacy":
            candidates.append(run / "legacy" / bench)
        else:
            candidates.append(run / grp / ("seed_%06d" % seed) / bench)

    if seed in seed_groups.get("fit", set()):
        candidates.append(run / "fit" / ("seed_%06d" % seed) / bench)
    if seed in seed_groups.get("gate", set()):
        candidates.append(run / "gate" / ("seed_%06d" % seed) / bench)

    candidates.extend([
        run / "fit" / ("seed_%06d" % seed) / bench,
        run / "gate" / ("seed_%06d" % seed) / bench,
        run / "legacy" / bench,
        run / ("seed_%06d" % seed) / bench,
        run / bench / ("seed_%06d" % seed),
        run / ("%s_s%d" % (bench, seed)),
        run / ("%s_seed%d" % (bench, seed)),
        run / bench / str(seed),
        run / bench,
        run,
    ])

    seen = set()
    for cand in candidates:
        if cand in seen:
            continue
        seen.add(cand)
        if not cand.exists() or not cand.is_dir():
            continue
        resolved = _inside(run, cand)
        if (
            (resolved / "labels" / "tiles_truth.bin").is_file()
            or (resolved / "tiles_truth.bin").is_file()
            or list(resolved.glob("labels*/tiles_truth.bin"))
        ):
            return resolved
    _fail("E_ABSENT", "ABSENT unit directory for bench=%s seed=%d" % (bench, seed), bench=bench, seed=seed)


def _locate_truth_path(run, unit, row, meta):
    t0 = _int(row.get("t0_q4", 32), "t0_q4", 1)
    default_t0 = int(calibrate.params.effective(None)["T0_Q4"])
    name = "labels" if t0 == default_t0 else "labels_t0_%d" % t0
    preferred = _inside(run, unit / name / "tiles_truth.bin")
    legacy = _inside(run, unit / "labels" / "tiles_truth.bin")
    direct = _inside(run, unit / "tiles_truth.bin")
    extra = sorted(_inside(run, p) for p in unit.glob("labels*/tiles_truth.bin") if p.is_file())
    choices = list(dict.fromkeys(p for p in [preferred, legacy, direct] + extra if p.is_file()))
    want = meta.get("labels_sha256")
    if want:
        choices = [p for p in choices if hashlib.sha256(p.read_bytes()).hexdigest() == want]
    if preferred in choices:
        return preferred
    if len(choices) == 1:
        return choices[0]
    if legacy in choices:
        return legacy
    _fail("E_ABSENT", "ABSENT uniquely bound truth tiles", bench=row.get("bench"), seed=row.get("seed"))


def _dilate_chebyshev_1(mask_2d):
    """Chebyshev radius-1 dilation (3x3 Moore neighborhood, including center) on a 2D bool array."""
    gh, gw = mask_2d.shape
    padded = np.pad(mask_2d, 1, mode="constant", constant_values=False)
    out = np.zeros_like(mask_2d, dtype=bool)
    for dy in (-1, 0, 1):
        for dx in (-1, 0, 1):
            out |= padded[1 + dy:1 + dy + gh, 1 + dx:1 + dx + gw]
    return out


def _has_signal_value(val):
    if isinstance(val, bool):
        return val
    if isinstance(val, (int, float, np.integer, np.floating)):
        return abs(float(val)) > 0
    if isinstance(val, (list, tuple)):
        return any(_has_signal_value(x) for x in val)
    if isinstance(val, dict):
        return any(_has_signal_value(v) for v in val.values())
    return False


def _inspect_global_exposure(frame_rows, tl, pred_dirty, threshold_ppm):
    """Evaluate GLOBAL_EXPOSURE per frame and determine field availability/ABSENT status."""
    n_frames = tl.shape[0]
    frame_active = np.zeros(n_frames, dtype=bool)
    exceeded_any = False

    if frame_rows is None:
        return frame_active, {
            "status": "ABSENT",
            "reason": "ABSENT: vision/frames.jsonl not persisted",
            "global_luma_persisted": False,
            "drift_hist_persisted": False,
        }

    has_luma_key = False
    has_drift_key = False
    has_drift_hist = False
    any_signal_seen = False

    for f in range(n_frames):
        row = frame_rows[f] if f < len(frame_rows) else {}
        static_mask = tl[f] == labels.TL_ID["STATIC"]
        n_static = int(static_mask.sum())
        n_dirty_static = int((static_mask & pred_dirty[f]).sum())
        exceeds = n_static > 0 and (n_dirty_static * 1000000 > int(threshold_ppm) * n_static)
        if exceeds:
            exceeded_any = True

        luma_sig = False
        for k in GLOBAL_LUMA_KEYS:
            if k in row and row[k] is not None:
                has_luma_key = True
                if _has_signal_value(row[k]):
                    luma_sig = True

        drift_sig = False
        for k in GLOBAL_DRIFT_KEYS:
            if k in row and row[k] is not None:
                has_drift_key = True
                if _has_signal_value(row[k]):
                    drift_sig = True

        n_hist = row.get("n")
        if isinstance(n_hist, list) and len(n_hist) == 7:
            has_drift_hist = True
            if _int(n_hist[score.C_DRIFT], "frame drift count") > 0:
                drift_sig = True

        if luma_sig or drift_sig:
            any_signal_seen = True
        if exceeds and (luma_sig or drift_sig):
            frame_active[f] = True

    if not has_luma_key and not has_drift_key and not has_drift_hist:
        return frame_active, {
            "status": "ABSENT",
            "reason": "ABSENT: engine frame rows lack global luma and drift histogram fields",
            "global_luma_persisted": False,
            "drift_hist_persisted": False,
        }

    if not has_luma_key and not any_signal_seen:
        return frame_active, {
            "status": "ABSENT",
            "reason": (
                "ABSENT: global_luma field not persisted in vision/frames.jsonl (vqa.core-frame/v1)"
                + ("; static dirty threshold exceeded without persisted global_luma" if exceeded_any else "")
            ),
            "global_luma_persisted": False,
            "drift_hist_persisted": has_drift_hist,
        }

    return frame_active, {
        "status": "OK",
        "reason": None if has_luma_key else "ABSENT: global_luma field not persisted in vqa.core-frame/v1 (using drift signal n[6])",
        "global_luma_persisted": has_luma_key,
        "drift_hist_persisted": has_drift_hist,
    }


def _inspect_light_change(run, unit, bench, frame_ids, f0):
    """Inspect journal.json / scene.manifest.json for per-frame light or world changes."""
    n_frames = len(frame_ids)
    frame_active = np.zeros(n_frames, dtype=bool)

    candidates = [
        unit / "journal.json",
        unit / "scene.manifest.json",
        unit / ("%s.scene.json" % bench),
    ]
    loaded = []
    for p in candidates:
        if p.is_file():
            loaded.append((p.name, _json(_inside(run, p))))

    if not loaded:
        return frame_active, {
            "status": "ABSENT",
            "reason": "ABSENT: journal.json and scene.manifest.json not persisted in unit directory",
            "journal_persisted": False,
            "world_field_persisted": False,
        }

    has_animated_field = False
    has_world_field = False
    change_frames = set()

    for _, doc in loaded:
        if not isinstance(doc, dict):
            continue
        if "animated" in doc and isinstance(doc["animated"], list):
            has_animated_field = True
            for entry in doc["animated"]:
                if not isinstance(entry, dict):
                    continue
                kind = entry.get("kind")
                if kind == "world":
                    has_world_field = True
                if kind in ("light", "world") and isinstance(entry.get("frame"), (int, np.integer)):
                    change_frames.add(int(entry["frame"]))
        for wk in ("world", "world_changes", "light_changes"):
            if wk in doc and doc[wk] is not None:
                has_animated_field = True
                if wk.startswith("world"):
                    has_world_field = True
                val = doc[wk]
                if isinstance(val, list):
                    for item in val:
                        if isinstance(item, (int, np.integer)):
                            change_frames.add(int(item))
                        elif isinstance(item, dict) and isinstance(item.get("frame"), (int, np.integer)):
                            change_frames.add(int(item["frame"]))

    if not has_animated_field:
        return frame_active, {
            "status": "ABSENT",
            "reason": "ABSENT: animated light/world field not persisted in journal.json or scene.manifest.json",
            "journal_persisted": True,
            "world_field_persisted": False,
        }

    for f in range(n_frames):
        fid = int(frame_ids[f])
        ri = int(f0) + f
        if fid in change_frames or ri in change_frames:
            frame_active[f] = True

    return frame_active, {
        "status": "OK",
        "reason": None if has_world_field else "ABSENT: world change kind not persisted in labels.ANIM_KINDS (light persisted)",
        "journal_persisted": True,
        "world_field_persisted": has_world_field,
    }


def _attribute_unit(
    run,
    unit,
    row,
    meta,
    global_exposure_ppm=GLOBAL_EXPOSURE_THRESHOLD_PPM,
):
    bench = row["bench"]
    seed = _int(row["seed"], "seed")
    grid = _grid(row["grid"])
    truth_path = _locate_truth_path(run, unit, row, meta)
    truth = score.read_truth_tiles(truth_path, (grid.s, grid.W, grid.H, grid.gw, grid.gh, grid.N))

    n_frames = int(truth["hdr"]["n_frames"])
    f0 = int(truth["hdr"]["f0"])
    if row.get("frames") is not None and _int(row["frames"], "row frames") != n_frames:
        _fail("E_BASELINE", "row frames disagree with truth tiles", bench=bench, seed=seed)
    if row.get("tiles") is not None and _int(row["tiles"], "row tiles") != grid.N:
        _fail("E_BASELINE", "row tiles disagree with grid", bench=bench, seed=seed)

    tile_cls_path = unit / "vision" / "tile_cls.jsonl"
    frames_path = unit / "vision" / "frames.jsonl"
    regions_path = unit / "vision" / "regions.jsonl"

    frame_rows = _jsonl(_inside(run, frames_path)) if frames_path.is_file() else None

    if tile_cls_path.is_file():
        cls_source = "tile_cls.jsonl"
        decoded = load_tile_cls_jsonl(_inside(run, tile_cls_path), grid, n_frames)
    else:
        cls_source = "regions.jsonl"
        if frame_rows is None or not regions_path.is_file():
            _fail("E_ABSENT", "ABSENT vision/tile_cls.jsonl and vision/regions.jsonl", bench=bench, seed=seed)
        decoded = noise_rescore.decode_regions(
            frame_rows,
            _jsonl(_inside(run, regions_path)),
            grid,
        )

    frame_ids = decoded["frame_ids"]
    if len(frame_ids) != n_frames or frame_ids[0] != f0:
        _fail("E_BASELINE", "truth/engine frame alignment mismatch", bench=bench, seed=seed)

    tl = truth["tl"]          # uint8 (F, N)
    cls = decoded["cls"]      # int8 (F, N), -1 for unknown
    unknown_mask = cls < 0
    pred_dirty = cls >= score.DIRTY_MIN
    fp_pop_mask = np.isin(tl, score.FP_POP)
    fp_mask = fp_pop_mask & pred_dirty
    fp_unknown_mask = fp_pop_mask & unknown_mask

    cause_masks = {c: np.zeros((n_frames, grid.N), dtype=bool) for c in ALL_CAUSES}
    cause_masks[CAUSE_ABSENT] = fp_unknown_mask.copy()
    remaining = fp_mask.copy()

    # Rule 1: SHADE (truth tl == SHADE_WEAK)
    shade_absent = row.get("clean_resid") is False
    shade_status = {
        "status": "ABSENT" if shade_absent else "OK",
        "reason": "ABSENT: clean_resid is False (SHADE_WEAK not labelled without residual)" if shade_absent else None,
    }
    m_shade = remaining & (tl == labels.TL_ID["SHADE_WEAK"])
    cause_masks[CAUSE_SHADE] = m_shade
    remaining &= ~m_shade

    # Rule 2: EDGE_BLEED (within 1 tile Chebyshev of a truth-dirty tile in the same frame)
    edge_bleed_mask = np.zeros((n_frames, grid.N), dtype=bool)
    for f in range(n_frames):
        tl_2d = tl[f][grid.order]
        dirty_truth_2d = np.isin(tl_2d, DIRTY_TL)
        if dirty_truth_2d.any():
            dilated_2d = _dilate_chebyshev_1(dirty_truth_2d)
            edge_bleed_mask[f] = dilated_2d[grid.ty, grid.tx]
    m_edge = remaining & edge_bleed_mask
    cause_masks[CAUSE_EDGE_BLEED] = m_edge
    remaining &= ~m_edge
    edge_status = {"status": "OK", "reason": None}

    # Rule 3: GLOBAL_EXPOSURE
    ge_frames, ge_status = _inspect_global_exposure(frame_rows, tl, pred_dirty, global_exposure_ppm)
    m_ge = remaining & ge_frames[:, None]
    cause_masks[CAUSE_GLOBAL_EXPOSURE] = m_ge
    remaining &= ~m_ge

    # Rule 4: LIGHT_CHANGE
    lc_frames, lc_status = _inspect_light_change(run, unit, bench, frame_ids, f0)
    m_lc = remaining & lc_frames[:, None]
    cause_masks[CAUSE_LIGHT_CHANGE] = m_lc
    remaining &= ~m_lc

    # Rule 5: AA_JITTER (static tile whose engine class flips across consecutive frames with no truth change)
    aa_mask = np.zeros((n_frames, grid.N), dtype=bool)
    aa_unknown_blocked = False
    if n_frames < 2:
        aa_status = {
            "status": "ABSENT",
            "reason": "ABSENT: fewer than 2 frames persisted (cannot evaluate consecutive-frame flips)",
        }
    else:
        for f in range(n_frames):
            cand_indices = np.flatnonzero(remaining[f] & (tl[f] == labels.TL_ID["STATIC"]))
            for i in cand_indices:
                c_cur = int(cls[f, i])
                flipped = False
                blocked = False
                for f_adj in (f - 1, f + 1):
                    if not (0 <= f_adj < n_frames):
                        continue
                    if int(tl[f_adj, i]) != labels.TL_ID["STATIC"]:
                        continue
                    c_adj = int(cls[f_adj, i])
                    if c_adj < 0:
                        blocked = True
                        continue
                    if c_adj != c_cur:
                        flipped = True
                        break
                if flipped:
                    aa_mask[f, i] = True
                elif blocked:
                    aa_unknown_blocked = True
        if aa_unknown_blocked or int(unknown_mask.sum()) > 0:
            aa_status = {
                "status": "ABSENT",
                "reason": "ABSENT: unknown tile classes in mixed regions block exact consecutive-frame class comparison",
            }
        else:
            aa_status = {"status": "OK", "reason": None}

    m_aa = remaining & aa_mask
    cause_masks[CAUSE_AA_JITTER] = m_aa
    remaining &= ~m_aa

    # Rule 6: UNATTRIBUTED
    cause_masks[CAUSE_UNATTRIBUTED] = remaining.copy()

    fp_pop_n = int(fp_pop_mask.sum())
    fp_tiles = int(fp_mask.sum())
    fp_absent_tiles = int(fp_unknown_mask.sum())
    absent_tiles = int(decoded["class_absent_tiles"])

    assert sum(int(cause_masks[c].sum()) for c in RULE_ORDER) == fp_tiles

    field_status = {
        CAUSE_SHADE: shade_status,
        CAUSE_EDGE_BLEED: edge_status,
        CAUSE_GLOBAL_EXPOSURE: {
            "status": ge_status["status"],
            "reason": ge_status["reason"],
            "global_luma_persisted": ge_status["global_luma_persisted"],
            "drift_hist_persisted": ge_status["drift_hist_persisted"],
        },
        CAUSE_LIGHT_CHANGE: {
            "status": lc_status["status"],
            "reason": lc_status["reason"],
            "journal_persisted": lc_status["journal_persisted"],
            "world_field_persisted": lc_status["world_field_persisted"],
        },
        CAUSE_AA_JITTER: aa_status,
        CAUSE_UNATTRIBUTED: {"status": "OK", "reason": None},
        CAUSE_ABSENT: {
            "status": "ABSENT" if absent_tiles > 0 else "OK",
            "reason": "ABSENT: %d tiles with unrecoverable class in regions.jsonl" % absent_tiles if absent_tiles > 0 else None,
        },
    }

    causes_out = {}
    for c in ALL_CAUSES:
        cnt = int(cause_masks[c].sum())
        st = field_status[c]
        causes_out[c] = {
            "count": cnt,
            "ppm": score.ppm(cnt, fp_pop_n),
            "share_ppm": score.ppm(cnt, fp_tiles),
            "status": st["status"],
            "reason": st["reason"],
        }

    frame_summaries = []
    for f in range(n_frames):
        f_fp = int(fp_mask[f].sum())
        f_pop = int(fp_pop_mask[f].sum())
        f_causes = {c: int(cause_masks[c][f].sum()) for c in ALL_CAUSES}
        frame_summaries.append({
            "bench": bench,
            "seed": seed,
            "frame": int(frame_ids[f]),
            "fp_tiles": f_fp,
            "fp_pop_n": f_pop,
            "fp_ppm": score.ppm(f_fp, f_pop),
            "absent_tiles": int(unknown_mask[f].sum()),
            "causes": f_causes,
        })

    top_frames = sorted(
        [fs for fs in frame_summaries if fs["fp_tiles"] > 0 or fs["absent_tiles"] > 0],
        key=lambda x: (-x["fp_tiles"], -x["absent_tiles"], x["bench"], x["seed"], x["frame"]),
    )[:TOP_FRAMES_LIMIT]

    return {
        "bench": bench,
        "seed": seed,
        "cls_source": cls_source,
        "frames": n_frames,
        "tiles": grid.N,
        "fp_pop_n": fp_pop_n,
        "fp_tiles": fp_tiles,
        "fp_ppm": score.ppm(fp_tiles, fp_pop_n),
        "absent_tiles": absent_tiles,
        "unknown_tiles": absent_tiles,
        "fp_absent_tiles": fp_absent_tiles,
        "causes": causes_out,
        "field_status": field_status,
        "frame_summaries": frame_summaries,
        "top_frames": top_frames,
    }


def _aggregate_causes(unit_list, fp_pop_n, fp_tiles):
    agg = {}
    for c in ALL_CAUSES:
        cnt = sum(u["causes"][c]["count"] for u in unit_list)
        statuses = [u["causes"][c]["status"] for u in unit_list]
        reasons = [u["causes"][c]["reason"] for u in unit_list if u["causes"][c]["reason"]]
        status = "ABSENT" if "ABSENT" in statuses else "OK"
        reason = reasons[0] if reasons else None
        agg[c] = {
            "count": cnt,
            "ppm": score.ppm(cnt, fp_pop_n),
            "share_ppm": score.ppm(cnt, fp_tiles),
            "status": status,
            "reason": reason,
        }
    return agg


def render_summary_md(report):
    """Render SUMMARY.md with per-bench and per-cause counts, ppm, ABSENT status, and top frames."""
    lines = [
        "# Static False Positive Cause Attribution Summary",
        "",
        "- **Schema**: `%s`" % report["schema"],
        "- **Total FP_POP tiles**: %d" % report["fp_pop_n"],
        "- **Total static FP tiles**: %d (%s ppm)" % (
            report["fp_tiles"],
            "-" if report["fp_ppm"] is None else str(report["fp_ppm"]),
        ),
        "- **Unknown / ABSENT class tiles**: %d (FP_POP ABSENT: %d)" % (
            report["absent_tiles"],
            report["fp_absent_tiles"],
        ),
        "",
        "## Per-Bench Attribution (Counts and PPM of FP_POP)",
        "",
        "| bench | units | fp_pop_n | fp_tiles | fp_ppm | SHADE (ppm) | EDGE_BLEED (ppm) | GLOBAL_EXPOSURE (ppm) | LIGHT_CHANGE (ppm) | AA_JITTER (ppm) | UNATTRIBUTED (ppm) | ABSENT |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for bench, b in sorted(report["benches"].items()):
        def _cell(cause):
            c_info = b["causes"][cause]
            p = "-" if c_info["ppm"] is None else str(c_info["ppm"])
            suffix = " [ABSENT]" if c_info["status"] == "ABSENT" and c_info["count"] == 0 else ""
            return "%d (%s)%s" % (c_info["count"], p, suffix)

        lines.append(
            "| %s | %d | %d | %d | %s | %s | %s | %s | %s | %s | %s | %d |"
            % (
                bench,
                b["units"],
                b["fp_pop_n"],
                b["fp_tiles"],
                "-" if b["fp_ppm"] is None else str(b["fp_ppm"]),
                _cell(CAUSE_SHADE),
                _cell(CAUSE_EDGE_BLEED),
                _cell(CAUSE_GLOBAL_EXPOSURE),
                _cell(CAUSE_LIGHT_CHANGE),
                _cell(CAUSE_AA_JITTER),
                _cell(CAUSE_UNATTRIBUTED),
                b["absent_tiles"],
            )
        )

    lines.extend([
        "",
        "## Overall Per-Cause Breakdown",
        "",
        "| cause | count | ppm (of FP_POP) | share_ppm (of FP) | status | reason |",
        "|---|---:|---:|---:|---|---|",
    ])
    for c in ALL_CAUSES:
        info = report["causes"][c]
        lines.append(
            "| %s | %d | %s | %s | %s | %s |"
            % (
                c,
                info["count"],
                "-" if info["ppm"] is None else str(info["ppm"]),
                "-" if info["share_ppm"] is None else str(info["share_ppm"]),
                info["status"],
                info["reason"] or "-",
            )
        )

    lines.extend([
        "",
        "## Top Frames",
        "",
        "| bench | seed | frame | fp_tiles | fp_pop_n | fp_ppm | SHADE | EDGE_BLEED | GLOBAL_EXPOSURE | LIGHT_CHANGE | AA_JITTER | UNATTRIBUTED | ABSENT |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ])
    for tf in report["top_frames"]:
        tc = tf["causes"]
        lines.append(
            "| %s | %d | %d | %d | %d | %s | %d | %d | %d | %d | %d | %d | %d |"
            % (
                tf["bench"],
                tf["seed"],
                tf["frame"],
                tf["fp_tiles"],
                tf["fp_pop_n"],
                "-" if tf["fp_ppm"] is None else str(tf["fp_ppm"]),
                tc[CAUSE_SHADE],
                tc[CAUSE_EDGE_BLEED],
                tc[CAUSE_GLOBAL_EXPOSURE],
                tc[CAUSE_LIGHT_CHANGE],
                tc[CAUSE_AA_JITTER],
                tc[CAUSE_UNATTRIBUTED],
                tc[CAUSE_ABSENT],
            )
        )
    return "\n".join(lines) + "\n"


def diagnose(run, out, global_exposure_ppm=GLOBAL_EXPOSURE_THRESHOLD_PPM):
    """Run static FP cause attribution over all units in RUN/rows.jsonl and write NEW outputs."""
    if Path(out).is_symlink():
        _fail("E_ARGS", "--out must be a new directory, not a symlink")
    run, out = Path(run).resolve(), Path(out).resolve()
    if not run.is_dir():
        _fail("E_ARGS", "--run must be a directory")
    if out == run or run in out.parents or out in run.parents or out.exists():
        _fail("E_ARGS", "--out must be a NEW directory outside --run (never overwrite)")
    global_exposure_ppm = _int(global_exposure_ppm, "global_exposure_ppm", 0, 1000000)

    rows_path = _inside(run, run / "rows.jsonl")
    if not rows_path.is_file():
        _fail("E_ABSENT", "ABSENT rows.jsonl in %s" % run)
    rows = _jsonl(rows_path)
    if not rows:
        _fail("E_ABSENT", "ABSENT scoring rows in rows.jsonl")

    seed_groups = {"fit": set(), "gate": set()}
    bench_meta = {}
    calib_path = run / "calib_report.json"
    if calib_path.is_file():
        rep = _json(_inside(run, calib_path))
        seeds = rep.get("seeds") or (rep.get("calibration") or {}).get("seeds")
        if isinstance(seeds, dict):
            if isinstance(seeds.get("fit"), list):
                seed_groups["fit"] = {_int(s, "fit seed") for s in seeds["fit"]}
            if isinstance(seeds.get("gate"), list):
                seed_groups["gate"] = {_int(s, "gate seed") for s in seeds["gate"]}
        for b in rep.get("benches", []):
            if isinstance(b, dict) and "bench" in b and "seed" in b:
                bench_meta[(b["bench"], b["seed"])] = b

    seen_keys = set()
    unit_reports = []
    all_frame_summaries = []

    for row in rows:
        if row.get("schema") != score.SCHEMA:
            _fail("E_SCHEMA", "not a calibration row")
        bench = row.get("bench")
        seed = _int(row.get("seed"), "row seed")
        key = (bench, seed)
        if key in seen_keys:
            _fail("E_SCHEMA", "duplicate (bench, seed) in rows.jsonl: %r" % (key,))
        seen_keys.add(key)

        unit_dir = _locate_unit_dir(run, row, seed_groups, bench_meta)
        u_rep = _attribute_unit(
            run,
            unit_dir,
            row,
            bench_meta.get(key, {}),
            global_exposure_ppm=global_exposure_ppm,
        )
        all_frame_summaries.extend(u_rep.pop("frame_summaries"))
        unit_reports.append(u_rep)

    by_bench_units = {}
    for u in unit_reports:
        by_bench_units.setdefault(u["bench"], []).append(u)

    benches_out = {}
    for bench, u_list in sorted(by_bench_units.items()):
        b_fp_pop_n = sum(u["fp_pop_n"] for u in u_list)
        b_fp_tiles = sum(u["fp_tiles"] for u in u_list)
        b_absent = sum(u["absent_tiles"] for u in u_list)
        b_fp_absent = sum(u["fp_absent_tiles"] for u in u_list)
        b_frames = sum(u["frames"] for u in u_list)
        b_tiles = sum(u["frames"] * u["tiles"] for u in u_list)
        b_causes = _aggregate_causes(u_list, b_fp_pop_n, b_fp_tiles)
        b_top_frames = sorted(
            [fs for fs in all_frame_summaries if fs["bench"] == bench and (fs["fp_tiles"] > 0 or fs["absent_tiles"] > 0)],
            key=lambda x: (-x["fp_tiles"], -x["absent_tiles"], x["seed"], x["frame"]),
        )[:TOP_FRAMES_LIMIT]
        benches_out[bench] = {
            "bench": bench,
            "seeds": sorted(u["seed"] for u in u_list),
            "units": len(u_list),
            "frames": b_frames,
            "tiles": b_tiles,
            "fp_pop_n": b_fp_pop_n,
            "fp_tiles": b_fp_tiles,
            "fp_ppm": score.ppm(b_fp_tiles, b_fp_pop_n),
            "absent_tiles": b_absent,
            "unknown_tiles": b_absent,
            "fp_absent_tiles": b_fp_absent,
            "causes": b_causes,
            "top_frames": b_top_frames,
        }

    total_fp_pop_n = sum(u["fp_pop_n"] for u in unit_reports)
    total_fp_tiles = sum(u["fp_tiles"] for u in unit_reports)
    total_absent = sum(u["absent_tiles"] for u in unit_reports)
    total_fp_absent = sum(u["fp_absent_tiles"] for u in unit_reports)
    total_tiles = sum(u["frames"] * u["tiles"] for u in unit_reports)
    overall_causes = _aggregate_causes(unit_reports, total_fp_pop_n, total_fp_tiles)

    top_frames = sorted(
        [fs for fs in all_frame_summaries if fs["fp_tiles"] > 0 or fs["absent_tiles"] > 0],
        key=lambda x: (-x["fp_tiles"], -x["absent_tiles"], x["bench"], x["seed"], x["frame"]),
    )[:TOP_FRAMES_LIMIT]

    report = {
        "schema": SCHEMA,
        "rule_order": list(RULE_ORDER),
        "fp_pop_tl": list(score.FP_POP),
        "dirty_min_cls": int(score.DIRTY_MIN),
        "global_exposure_threshold_ppm": global_exposure_ppm,
        "total_tiles": total_tiles,
        "fp_pop_n": total_fp_pop_n,
        "fp_tiles": total_fp_tiles,
        "fp_ppm": score.ppm(total_fp_tiles, total_fp_pop_n),
        "absent_tiles": total_absent,
        "unknown_tiles": total_absent,
        "fp_absent_tiles": total_fp_absent,
        "causes": overall_causes,
        "benches": benches_out,
        "units": unit_reports,
        "top_frames": top_frames,
    }
    md_text = render_summary_md(report)
    report["summary_md"] = md_text

    out.mkdir(parents=True, exist_ok=False)
    (out / REPORT_JSON).write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (out / SUMMARY_MD).write_text(md_text, encoding="utf-8")
    return report
