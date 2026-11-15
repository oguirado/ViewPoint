"""Planted defect: g_catalogue must reject it (rc 5)."""
from _base import GRID  # noqa: F401
GATE = 'g_catalogue'
OVERLAY = {
    'catalogue/routes.src': '', 'catalogue/thresholds.src': '', 'catalogue/failures.src': '',
    'catalogue/layers.src': '{}\n', 'catalogue/subjects.src': '{}\n', 'catalogue/measurements.src': '{}\n',
    'visioncore/grid.py': GRID + '# @anchor:vc.s0.x\n',
}
