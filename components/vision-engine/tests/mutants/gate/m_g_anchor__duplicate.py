"""Planted defect: g_anchor must reject it (rc 5)."""
from _base import GRID  # noqa: F401
GATE = 'g_anchor'
OVERLAY = {
    'visioncore/change.py': '# @anchor:vc.s0.grid\nT = 1\n',
}
