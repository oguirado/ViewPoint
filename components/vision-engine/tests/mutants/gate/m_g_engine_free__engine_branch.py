"""Planted defect: g_engine_free must reject it (rc 5)."""
from _base import GRID  # noqa: F401
GATE = 'g_engine_free'
OVERLAY = {
    'visioncore/regions.py': "def f(adapter_id):\n    if adapter_id == 'godot':\n        return 1\n",
}
