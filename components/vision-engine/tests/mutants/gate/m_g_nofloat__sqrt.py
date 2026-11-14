"""Planted defect: g_nofloat must reject it (rc 5)."""
from _base import GRID  # noqa: F401
GATE = 'g_nofloat'
OVERLAY = {
    'visioncore/change.py': 'import math\nT = math.sqrt(2)\n',
}
