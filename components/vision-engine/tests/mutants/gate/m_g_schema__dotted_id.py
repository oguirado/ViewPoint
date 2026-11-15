"""Planted defect: g_schema must reject it (rc 5)."""
from _base import GRID  # noqa: F401
GATE = 'g_schema'
OVERLAY = {
    'visioncore/schemas/vqa.tile.v1.schema.json': '{"$id":"vqa.core.region.v1"}',
}
