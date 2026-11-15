"""Planted defect: g_layer must reject it (rc 5)."""
from _base import GRID  # noqa: F401
GATE = 'g_layer'
OVERLAY = {
    'visioncore/layout.py': 'import numpy\n',
}
