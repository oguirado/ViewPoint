"""Planted defect: g_tier1 must reject it (rc 5)."""
from _base import GRID  # noqa: F401
GATE = 'g_tier1'
OVERLAY = {
    'llms.txt': 'see [x](tools/nope.py)\n',
}
