"""Planted defect: g_ids must reject it (rc 5)."""
from _base import GRID  # noqa: F401
GATE = 'g_ids'
OVERLAY = {
    'visioncore/gop.py': "def f(a, b):\n    return f'f{a}:t{b:04x}'\n",
}
