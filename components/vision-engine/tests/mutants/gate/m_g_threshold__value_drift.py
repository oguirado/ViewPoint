"""Planted defect: g_threshold must reject it (rc 5)."""
from _base import GRID  # noqa: F401
GATE = 'g_threshold'
OVERLAY = {
    'catalogue/thresholds.tsv': 'T0_Q4\t33\tq4\nH_DEAD\t2\tbits\n',
}
