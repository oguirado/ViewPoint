"""Planted defect: g_route_budget must reject it (rc 5)."""
from _base import GRID  # noqa: F401
GATE = 'g_route_budget'
OVERLAY = {
    'catalogue/routes.tsv': 'format a field id\tvc.ids.field_id\tR3\n',
    'catalogue/route_queries.tsv': 'format a field id\tvc.ids.nope\n',
}
