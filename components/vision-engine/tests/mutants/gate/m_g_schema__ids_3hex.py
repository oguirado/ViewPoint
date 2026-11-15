"""Planted defect: g_schema must reject it (rc 5)."""
from _base import GRID  # noqa: F401
GATE = 'g_schema'
OVERLAY = {
    'visioncore/ids.py': "def field_id(frame, morton):\n    return 'f%d:t%03x' % (frame, morton)\n\ndef region_id(f, m):\n    return ''\n",
}
