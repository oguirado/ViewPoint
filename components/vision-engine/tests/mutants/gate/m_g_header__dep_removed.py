"""Planted defect: g_header must reject it (rc 5)."""
from _base import GRID  # noqa: F401
GATE = 'g_header'
OVERLAY = {
    'visioncore/grid.py': GRID.replace('"deps":["vc.ids","vc.p"]', '"deps":["vc.ids"]'),
}
