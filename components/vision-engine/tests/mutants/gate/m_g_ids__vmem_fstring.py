"""Planted defect: g_ids must reject it (rc 5)."""
from _base import GRID  # noqa: F401
GATE = 'g_ids'
OVERLAY = {
    'vmem/store.py': "def f(a, b):\n    return f'r{a}:{b:04x}'\n",
}
