# @module {"id":"vc.ids","layer":0,"stage":"L0","owner":"ARCH","deps":["vc.layout","vc.errors"],"book":"HB-VC-a0-arch"}
"""@module vc.ids L0 - sole formatter/parser/range-checker of every id string (SPEC_INT R1)."""
import re

from . import layout
from .errors import VqaError

FRAME_MAX = (1 << 32) - 1
_F = r"(0|[1-9][0-9]{0,9})"
_M = r"([0-9a-f]{4})"
_RUN = r"r_([0-9]{6})_([0-9a-f]{8})"

_PATTERNS = (
    ("run_id", re.compile(r"^" + _RUN + r"$")),
    ("cross_cite", re.compile(r"^(" + _RUN + r")/(.+)$")),
    ("field", re.compile(r"^f" + _F + r":t" + _M + r"$")),
    ("global", re.compile(r"^f" + _F + r":all$")),
    ("region", re.compile(r"^r" + _F + r":" + _M + r"$")),
    ("node", re.compile(r"^p" + _F + r":L([0-8]):" + _M + r"$")),
    ("track", re.compile(r"^k" + _F + r":" + _M + r"$")),
    ("gop", re.compile(r"^g(0|[1-9][0-9]{0,3})$")),
    ("track_key", re.compile(r"^T:([a-z0-9_]{1,32}):([a-z0-9_]{1,48}):([0-9]+)x([0-9]+):(tile|ui|actor|a11y|island):"
                             r"([0-9a-f]{4})-([0-9a-f]{4})$")),
    ("unit", re.compile(r"^V-(BAS|EVT|EPI|VER|DEF)-([0-9]{6})$")),
    ("threshold", re.compile(r"^[A-Z][A-Z0-9_]{2,40}$")),
    ("grid", re.compile(r"^grid:([1-9][0-9]{0,3})x([1-9][0-9]{0,3}):s(16|32|64|128)$")),
)


def _frame(f):
    if not isinstance(f, int) or isinstance(f, bool) or not 0 <= f <= FRAME_MAX:
        raise VqaError("E_VC_ID_RANGE", {"frame": f})
    return f


def _morton(m, grid=None):
    if not isinstance(m, int) or isinstance(m, bool) or not 0 <= m <= 0xFFFF:
        raise VqaError("E_VC_ID_RANGE", {"morton": m})
    if grid is not None:
        tx, ty = layout.unmorton(m)
        if tx >= grid[1] or ty >= grid[2]:
            raise VqaError("E_VC_ID_RANGE", {"morton": m, "tx": tx, "ty": ty})
    return m


def _grid(W, H):
    return layout.grid_params(W, H) if W is not None else None


# @anchor:vc.ids.field_id
def field_id(frame, morton, W=None, H=None):
    return "f%d:t%04x" % (_frame(frame), _morton(morton, _grid(W, H)))


def global_id(frame):
    return "f%d:all" % _frame(frame)


# @anchor:vc.ids.region_id
def region_id(frame, min_morton, W=None, H=None):
    return "r%d:%04x" % (_frame(frame), _morton(min_morton, _grid(W, H)))


def node_id(frame, level, m0):
    if not isinstance(level, int) or not 0 <= level <= 8:
        raise VqaError("E_VC_ID_RANGE", {"level": level})
    return "p%d:L%d:%04x" % (_frame(frame), level, _morton(m0))


def track_id(frame, first_region_morton):
    return "k%d:%04x" % (_frame(frame), _morton(first_region_morton))


def gop_id(n):
    if not isinstance(n, int) or not 0 <= n <= 9999:
        raise VqaError("E_VC_ID_RANGE", {"gop": n})
    return "g%d" % n


def run_id(build_seq, sha8):
    if not isinstance(build_seq, int) or not 0 <= build_seq <= 999999 or not re.fullmatch(r"[0-9a-f]{8}", str(sha8)):
        raise VqaError("E_ID", {"run": [build_seq, sha8]})
    return "r_%06d_%s" % (build_seq, sha8)


def unit_id(kind, n):
    if kind not in ("BAS", "EVT", "EPI", "VER", "DEF") or not isinstance(n, int) or not 0 <= n <= 999999:
        raise VqaError("E_ID", {"unit": [kind, n]})
    return "V-%s-%06d" % (kind, n)


def cross_cite(run, local):
    parse_any(run)
    parse_any(local)
    return "%s/%s" % (run, local)


def episode_id(run, gop):
    return "%s/%s" % (run, gop_id(gop))


def grid_ident(W, H):
    return layout.grid_ident(W, H)


def track_key(app, scene, W, H, role, m0, m1):
    """Cross-run track key T:<app>:<scene>:<W>x<H>:<role>:<m0>-<m1> (sole formatter; re-validated by parse_any)."""
    tk = "T:%s:%s:%dx%d:%s:%04x-%04x" % (app, scene, W, H, role, m0, m1)
    parse_any(tk)
    return tk


# @anchor:vc.ids.parse
def parse_any(s, W=None, H=None):
    """-> (kind, fields). Syntax failure E_VC_ID_SYNTAX; range failure E_VC_ID_RANGE. Optional W,H enables tx<gw,ty<gh."""
    if not isinstance(s, str):
        raise VqaError("E_VC_ID_SYNTAX", {"id": repr(s)})
    for kind, rx in _PATTERNS:
        m = rx.fullmatch(s)
        if not m:
            continue
        g = m.groups()
        if kind == "run_id":
            return kind, {"build_seq": int(g[0]), "sha8": g[1]}
        if kind == "cross_cite":
            local = g[3]
            lk, lf = parse_any(local, W, H)
            if lk not in ("field", "region", "global", "track"):
                raise VqaError("E_VC_ID_SYNTAX", {"id": s})
            return kind, {"run": g[0], "local": local, "local_kind": lk, "fields": lf}
        if kind in ("field", "region", "track"):
            f, mo = _frame(int(g[0])), int(g[1], 16)
            _morton(mo, _grid(W, H))
            return kind, {"frame": f, "morton": mo}
        if kind == "global":
            return kind, {"frame": _frame(int(g[0]))}
        if kind == "node":
            return kind, {"frame": _frame(int(g[0])), "level": int(g[1]), "morton": _morton(int(g[2], 16))}
        if kind == "gop":
            return kind, {"gop": int(g[0])}
        if kind == "track_key":
            return kind, {"app": g[0], "scene": g[1], "W": int(g[2]), "H": int(g[3]), "role": g[4],
                          "m0": int(g[5], 16), "m1": int(g[6], 16)}
        if kind == "unit":
            return kind, {"kind": g[0], "n": int(g[1])}
        if kind == "grid":
            Wg, Hg, sg = int(g[0]), int(g[1]), int(g[2])
            if layout.grid_params(Wg, Hg)[0] != sg:
                raise VqaError("E_GRID", {"id": s})
            return kind, {"W": Wg, "H": Hg, "s": sg}
        return kind, {"name": s}
    raise VqaError("E_VC_ID_SYNTAX", {"id": s})


# @anchor:vc.ids.field_ids_of_rect
def field_ids_of_rect(rect_px, W, H, frame):
    """Closed-box tile ids touched by rect (x,y,w,h) clipped to the image (EVAL ground truth). Empty rect -> []."""
    s, gw, gh = layout.grid_params(W, H)
    x, y, w, h = rect_px
    if w <= 0 or h <= 0:
        return []
    x0, y0, x1, y1 = max(x, 0), max(y, 0), min(x + w - 1, W - 1), min(y + h - 1, H - 1)
    if x0 > x1 or y0 > y1:
        raise VqaError("E_VC_ID_RANGE", {"rect": list(rect_px)})
    ms = sorted(layout.morton(tx, ty) for ty in range(y0 // s, y1 // s + 1) for tx in range(x0 // s, x1 // s + 1))
    return [field_id(frame, m) for m in ms]
