# @module {"id":"vc.layout","layer":0,"stage":"L0","owner":"ARCH","deps":["vc.errors","vc.p"],"book":"HB-VC-a0-arch"}
"""@module vc.layout L0 - pure-int grid rule, enums and binary record layouts (SPEC_INT R1/R2, SPEC_CORE §1-§2, §6)."""
import struct

from .errors import VqaError
from .params import PARAMS

# --- grid rule (R1; axis cap from SPEC_CORE §1) ---
TILE_MIN_PX = PARAMS["TILE_MIN_PX"]
CELL_MAX = PARAMS["CELL_MAX"]
AXIS_MAX = PARAMS["AXIS_MAX"]
DIM_MAX = 8192


def grid_params(W, H):
    """-> (s, gw, gh). Smallest power of two >= 16 with cells <= 4096 and both axes <= 255."""
    if not (isinstance(W, int) and isinstance(H, int)) or not (1 <= W <= DIM_MAX and 1 <= H <= DIM_MAX):
        raise VqaError("E_GRID", {"W": W, "H": H, "reason": "dimension outside 1..%d" % DIM_MAX})
    s = TILE_MIN_PX
    while s <= 1 << 16:
        gw, gh = -(-W // s), -(-H // s)
        if gw * gh <= CELL_MAX and max(gw, gh) <= AXIS_MAX:
            return s, gw, gh
        s <<= 1
    raise VqaError("E_GRID", {"W": W, "H": H})


def grid_ident(W, H):
    return "grid:%dx%d:s%d" % ((W, H, grid_params(W, H)[0]))


# --- Morton (8 bit per axis) ---
def _spread(v):
    v &= 0xFF
    v = (v | (v << 4)) & 0x0F0F
    v = (v | (v << 2)) & 0x3333
    v = (v | (v << 1)) & 0x5555
    return v


def _compact(v):
    v &= 0x5555
    v = (v | (v >> 1)) & 0x3333
    v = (v | (v >> 2)) & 0x0F0F
    v = (v | (v >> 4)) & 0x00FF
    return v


# @anchor:vc.layout.morton
def morton(tx, ty):
    if not (0 <= tx <= AXIS_MAX and 0 <= ty <= AXIS_MAX):
        raise VqaError("E_VC_ID_RANGE", {"tx": tx, "ty": ty})
    return _spread(tx) | (_spread(ty) << 1)


def unmorton(m):
    if not 0 <= m <= 0xFFFF:
        raise VqaError("E_VC_ID_RANGE", {"m": m})
    return _compact(m), _compact(m >> 1)


# --- enums (R2) ---
CLS = ("UNCHANGED", "JITTER", "CHANGED", "MOVED", "FLICKER", "FROZEN", "DRIFT")
CLS_ID = {n: i for i, n in enumerate(CLS)}
CLS_DIRTY_MIN = 2
TILE_FLAGS = ("EDGE", "TEXTURE", "SHADE_ONLY", "SAT", "UI_OCC", "ACTOR_OCC", "GLOBAL_MV", "PAD")
TRACK_FLAGS = ("FROZEN", "DRIFT", "CYCLE", "APPEAR", "VANISH", "CUT")
VERDICTS = ("PASS", "FAIL", "CANNOT_SEE")
UNIT_LAYERS = ("FG", "BG")
UNIT_PINS = ("FAIL", "CATALOGUED", "GOLDEN", None)
JUDGE_MODELS = ("gemini-3.8-flash", "gemini-4-argon")


def cls_name(v):
    if not isinstance(v, int) or not 0 <= v < len(CLS):
        raise VqaError("E_SCHEMA", {"cls": v})
    return CLS[v]


def cls_id(name):
    if name not in CLS_ID:
        raise VqaError("E_SCHEMA", {"cls_name": name})
    return CLS_ID[name]


def is_dirty(cls):
    cls_name(cls)
    return cls >= CLS_DIRTY_MIN


# --- tile record (vqa.tile/v1): 32 B packed LE ---
# @anchor:vc.layout.tile_dtype
# tile_dtype: packed 32 B little-endian record
TILE_FMT = "<HBBHbbHHBBbbQQ"
TILE_FIELDS = ("morton", "cls", "flags", "y_q4", "co_h", "cg_h", "sig_q4", "grad_q6",
               "edge", "resid", "mv_dx", "mv_dy", "sig64", "xh64")
TILE_OFFSETS = (0, 2, 3, 4, 6, 7, 8, 10, 12, 13, 14, 15, 16, 24)
TILE_SIZE = 32
assert struct.calcsize(TILE_FMT) == TILE_SIZE

TILE_MAGIC = b"VQATILE1"
TILE_HDR_SIZE = 64
# magic8 ver u16 s u16 W u16 H u16 gw u16 gh u16 N u16 rec u16 n_frames u32 f0 u32 params16 api8 zero8
TILE_HDR_FMT = "<8sHHHHHHHHII16s8s8s"
assert struct.calcsize(TILE_HDR_FMT) == TILE_HDR_SIZE

# --- GlobalMotion (I5): 8 B ---
GLOBAL_MOTION_FMT = "<hhI"
GLOBAL_MOTION_SIZE = 8
assert struct.calcsize(GLOBAL_MOTION_FMT) == GLOBAL_MOTION_SIZE


def pack_tile_header(s, W, H, gw, gh, N, n_frames, f0, params_sha256_hex, api_version):
    return struct.pack(TILE_HDR_FMT, TILE_MAGIC, 1, s, W, H, gw, gh, N, TILE_SIZE, n_frames, f0,
                       bytes.fromhex(params_sha256_hex)[:16], api_version.encode("ascii")[:8].ljust(8, b"\0"),
                       b"\0" * 8)


def parse_tile_header(b):
    if len(b) < TILE_HDR_SIZE:
        raise VqaError("E_VC_FORMAT", {"reason": "short header"})
    f = struct.unpack(TILE_HDR_FMT, b[:TILE_HDR_SIZE])
    if f[0] != TILE_MAGIC or f[1] != 1 or f[8] != TILE_SIZE:
        raise VqaError("E_VC_FORMAT", {"reason": "bad magic/ver/rec"})
    return {"s": f[2], "W": f[3], "H": f[4], "gw": f[5], "gh": f[6], "N": f[7],
            "n_frames": f[9], "f0": f[10], "params_sha256_16": f[11].hex(),
            "api_version": f[12].rstrip(b"\0").decode("ascii")}


def unpack_tile(b, off=0):
    return dict(zip(TILE_FIELDS, struct.unpack_from(TILE_FMT, b, off)))


def check_tile_records(b):
    """Validate cls of every 32 B record in b; cls > 6 -> E_SCHEMA (T-R2)."""
    if len(b) % TILE_SIZE:
        raise VqaError("E_VC_FORMAT", {"reason": "length not multiple of 32"})
    for off in range(0, len(b), TILE_SIZE):
        cls_name(b[off + 2])
