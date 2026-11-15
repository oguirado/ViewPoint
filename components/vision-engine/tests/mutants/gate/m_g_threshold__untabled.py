"""Planted defect: g_threshold must reject it (rc 5)."""
from _base import GRID  # noqa: F401
GATE = 'g_threshold'
OVERLAY = {
    'visioncore/params.py': "PARAMS = {'T0_Q4': 32, 'H_DEAD': 2, 'X_NEW_PX': 3}\n",
}
