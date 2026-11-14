"""Planted defect: g_layer must reject it (rc 5)."""
from _base import GRID  # noqa: F401
GATE = 'g_layer'
OVERLAY = {
    'visioncore/grid.py': 'from visioncore import gop\n',
}
