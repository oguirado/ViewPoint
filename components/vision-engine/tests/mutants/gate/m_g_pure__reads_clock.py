"""Planted defect: g_pure must reject it (rc 5)."""
from _base import GRID  # noqa: F401
GATE = 'g_pure'
OVERLAY = {
    'tools/gates.py': 'import time\n',
}
