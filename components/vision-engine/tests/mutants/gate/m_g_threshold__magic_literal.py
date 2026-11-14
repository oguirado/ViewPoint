"""Planted defect: g_threshold must reject it (rc 5)."""
from _base import GRID  # noqa: F401
GATE = 'g_threshold'
OVERLAY = {
    'visioncore/change.py': 'from visioncore import grid\n_T_SECRET = 37\n',
}
