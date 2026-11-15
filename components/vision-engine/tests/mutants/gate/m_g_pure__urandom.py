"""Planted defect: g_pure must reject it (rc 5)."""
from _base import GRID  # noqa: F401
GATE = 'g_pure'
OVERLAY = {
    'tools/route_code.py': 'import os\nX = os.urandom(4)\n',
}
