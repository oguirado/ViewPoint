"""Planted defect: g_anchor must reject it (rc 5)."""
from _base import GRID  # noqa: F401
GATE = 'g_anchor'
OVERLAY = {
    'catalogue/routes.tsv': 'x\tvc.s2.nope\tR3\n',
}
