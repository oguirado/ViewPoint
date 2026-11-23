"""Validate digest lineage and group separation before any case is opened."""
from graphlib import CycleError, TopologicalSorter

from .common import CalibrationError, require

SPLITS = ("calibration", "held-out")


def _ancestors(start, graph):
    seen = set()
    pending = [start]
    while pending:
        pin = pending.pop()
        if pin not in seen:
            seen.add(pin)
            pending.extend(graph[pin])
    return seen


def verify_split(lineage, cases):
    nodes = {row["digest"]: row for row in lineage}
    require(len(nodes) == len(lineage), 2, "duplicate_lineage_digest")
    graph = {pin: row["parents"] for pin, row in nodes.items()}
    require(all(parent in nodes for parents in graph.values() for parent in parents),
            3, "lineage_parent_missing")
    try:
        list(TopologicalSorter(graph).static_order())
    except CycleError as exc:
        raise CalibrationError(2, "lineage_cycle") from exc

    require(len({case["id"] for case in cases}) == len(cases), 2, "duplicate_case_id")
    require({case["split"] for case in cases} == set(SPLITS), 3, "split_missing")
    seen_pins = {split: set() for split in SPLITS}
    seen_origins = {split: set() for split in SPLITS}
    for case in cases:
        audio, annotation = case["audio_digest"], case["annotation_digest"]
        require(audio in nodes and annotation in nodes, 3, "case_lineage_missing")
        ancestors = _ancestors(annotation, graph)
        require(audio != annotation and audio in ancestors, 3, "annotation_lineage_unbound")
        alignment = case["alignment_digest"]
        if alignment is not None:
            require(alignment in nodes, 3, "case_lineage_missing")
            require(alignment not in ancestors, 4, "annotation_uses_alignment")
            alignment_ancestors = _ancestors(alignment, graph)
            require(annotation not in alignment_ancestors and audio != alignment and audio in alignment_ancestors,
                    4, "alignment_lineage_unbound")
            ancestors.update(alignment_ancestors)
        split = case["split"]
        seen_pins[split].update(ancestors)
        seen_origins[split].update(nodes[pin]["origin"] for pin in ancestors if not graph[pin])

    require(not seen_pins["calibration"] & seen_pins["held-out"]
            and not seen_origins["calibration"] & seen_origins["held-out"], 4, "cross_split_lineage")
    require(seen_pins["calibration"] | seen_pins["held-out"] == nodes.keys(), 2, "unreferenced_lineage")
    return frozenset(seen_pins["held-out"])
