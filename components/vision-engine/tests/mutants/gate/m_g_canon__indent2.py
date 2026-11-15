"""Planted defect: g_canon must reject it (rc 5)."""
from _base import GRID  # noqa: F401
GATE = 'g_canon'
OVERLAY = {
    'catalogue/measurements.json': '{\n  "a": 1\n}\n',
}
