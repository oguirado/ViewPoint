# @module {"id":"vc.p","layer":0,"stage":"L0","owner":"ARCH","deps":["vc.canon","vc.errors"],"book":"HB-VC-a0-arch"}
"""@module vc.params L0 - single int-only parameter table (SPEC_INT R5). Rows marked P may be overridden by a profile."""
import re

from . import canon
from .errors import VqaError

THRESHOLD_ID_RE = re.compile(r"^[A-Z][A-Z0-9_]{2,40}$")

# id -> (default, unit, owner, profile-overridable, (lo, hi))
_ROWS = {
    "TILE_MIN_PX": (16, "px", "CORE", False, (16, 16)),  # @anchor:vc.p.TILE_MIN_PX
    "CELL_MAX": (4096, "cells", "CORE", False, (4096, 4096)),  # @anchor:vc.p.CELL_MAX
    "AXIS_MAX": (255, "cells", "CORE", False, (255, 255)),  # @anchor:vc.p.AXIS_MAX
    "T0_Q4": (32, "q4", "CORE", True, (1, 4080)),  # @anchor:vc.p.T0_Q4
    "HYST_BAND_PERMILLE": (100, "permille", "CORE", False, (0, 500)),  # @anchor:vc.p.HYST_BAND_PERMILLE
    "H_DEAD": (2, "bits", "CORE", False, (0, 64)),  # @anchor:vc.p.H_DEAD
    "SIG_DEAD_Q4": (24, "q4", "CORE", False, (0, 4080)),  # @anchor:vc.p.SIG_DEAD_Q4
    "EMA_F_Q16": (61440, "q16", "CORE", False, (1, 65535)),  # @anchor:vc.p.EMA_F_Q16
    "EPS_Q4": (9, "q4", "CORE", False, (1, 255)),  # @anchor:vc.p.EPS_Q4
    "DRIFT_C_Q4": (8, "q4", "CORE", True, (1, 4080)),  # @anchor:vc.p.DRIFT_C_Q4
    "DRIFT_U_Q4": (35, "q4", "CORE", True, (1, 4080)),  # @anchor:vc.p.DRIFT_U_Q4
    "TAU_EDGE": (48, "grey", "CORE", False, (1, 255)),  # @anchor:vc.p.TAU_EDGE
    "FROZEN_M": (8, "frames", "CORE", False, (1, 255)),  # @anchor:vc.p.FROZEN_M
    "FROZEN_COV_PPM": (10000, "ppm", "CORE", False, (0, 1000000)),  # @anchor:vc.p.FROZEN_COV_PPM
    "DRIFT_STALL": (8, "frames", "CORE", False, (1, 255)),  # @anchor:vc.p.DRIFT_STALL
    "MV_SAD_RATIO_DEN": (3, "ratio", "CORE", False, (2, 16)),  # @anchor:vc.p.MV_SAD_RATIO_DEN
    "GLOBAL_SHARE_PPM": (500000, "ppm", "CORE", False, (0, 1000000)),  # @anchor:vc.p.GLOBAL_SHARE_PPM
    "GOP_K": (20, "frames", "TIME", False, (1, 255)),  # @anchor:vc.p.GOP_K
    "CUT_PPM_2F": (500000, "ppm", "TIME", False, (0, 1000000)),  # @anchor:vc.p.CUT_PPM_2F
    "CUT_PPM_1F": (800000, "ppm", "TIME", False, (0, 1000000)),  # @anchor:vc.p.CUT_PPM_1F
    "TRACK_LINK_PPM": (500000, "ppm", "TIME", False, (0, 1000000)),  # @anchor:vc.p.TRACK_LINK_PPM
    "PACK_GOP_B": (2000, "bytes", "TIME", False, (1, 1 << 20)),  # @anchor:vc.p.PACK_GOP_B
    "PACK_BATCH_B": (8000, "bytes", "TIME", False, (1, 1 << 20)),  # @anchor:vc.p.PACK_BATCH_B
    "PACK_CALL_TOK": (15500, "tokens", "TIME", False, (15500, 15500)),  # @anchor:vc.p.PACK_CALL_TOK
    "PACK_RUN_TOK": (62000, "tokens", "TIME", False, (62000, 62000)),  # @anchor:vc.p.PACK_RUN_TOK
    "PACK_RETRY_TOK": (93000, "tokens", "TIME", False, (93000, 93000)),  # @anchor:vc.p.PACK_RETRY_TOK
    "Z1_MAX_B": (6000, "bytes", "TIME", False, (1, 1 << 20)),  # @anchor:vc.p.Z1_MAX_B
    "PACK_RAG_EDGES_B": (0, "bytes", "TIME", False, (0, 1 << 20)),  # @anchor:vc.p.PACK_RAG_EDGES_B
    "SIG_HAM_MAX": (3, "bits", "VMEM", False, (0, 8)),  # @anchor:vc.p.SIG_HAM_MAX
    "COMPACT_K_CLEAN": (5, "runs", "VMEM", False, (1, 1000)),  # @anchor:vc.p.COMPACT_K_CLEAN
    "L1_CAP_MIB": (512, "mib", "VMEM", False, (1, 1 << 20)),  # @anchor:vc.p.L1_CAP_MIB
    "L2_CAP_MIB": (2048, "mib", "VMEM", False, (1, 1 << 20)),  # @anchor:vc.p.L2_CAP_MIB
    "FG_WARN_PERMILLE": (250, "permille", "VMEM", False, (0, 1000)),  # @anchor:vc.p.FG_WARN_PERMILLE
    "FG_BLOCK_PERMILLE": (900, "permille", "VMEM", False, (0, 1000)),  # @anchor:vc.p.FG_BLOCK_PERMILLE
    "QUOTE_MIN_CHARS": (12, "chars", "VMEM", False, (1, 1000)),  # @anchor:vc.p.QUOTE_MIN_CHARS
}

PARAMS = {k: v[0] for k, v in _ROWS.items()}
BOUNDS = {k: v[4] for k, v in _ROWS.items()}
UNITS = {k: v[1] for k, v in _ROWS.items()}
OWNERS = {k: v[2] for k, v in _ROWS.items()}
PROFILE_KEYS = frozenset(k for k, v in _ROWS.items() if v[3])


def _check(table):
    for k, v in table.items():
        if not isinstance(v, int) or isinstance(v, bool):
            raise VqaError("E_VC_PARAM_RANGE", {"param": k, "reason": "not int"})
        lo, hi = BOUNDS[k]
        if not lo <= v <= hi:
            raise VqaError("E_VC_PARAM_RANGE", {"param": k, "value": v, "bounds": [lo, hi]})


# @anchor:vc.p.effective
def effective(profile=None):
    """Defaults overlaid by profile (P rows only; keys case-insensitive). Non-P -> E_SCHEMA, unknown -> E_VC_PARAM_UNKNOWN."""
    table = dict(PARAMS)
    seen = set()
    for key, val in (profile or {}).items():
        k = key.upper() if isinstance(key, str) else key
        if k in seen:
            raise VqaError("E_SCHEMA", {"param": key, "reason": "duplicate key after case folding"})
        seen.add(k)
        if k not in PARAMS:
            raise VqaError("E_VC_PARAM_UNKNOWN", {"param": key})
        if k not in PROFILE_KEYS:
            raise VqaError("E_SCHEMA", {"param": key, "reason": "not profile-overridable"})
        table[k] = val
    _check(table)
    return table


def params_sha256(table=None):
    t = PARAMS if table is None else table
    return canon.sha256_obj({k: t[k] for k in sorted(t)})
