"""Explicit keyword routing, with read-only coordinates from route/query.py any.

Multiple environment matches abstain instead of silently choosing a default.
A known but unimplemented adapter is reported with rc 3 and its declared status.
Coordinates use the tracked index plus a read-only AST overlay of this package.
The overlay exposes newly created files without writing outside the package.
"""
import importlib.util
from pathlib import Path
import re
import sys

from .registry import ADAPTERS

KEYWORDS = {"blender": ("blender", "blend", "bpy", "eevee", "cycles"),
            "mujoco": ("mujoco", "mjcf", "simulation", "robot", "physics"),
            "autocad": ("autocad", "dwg", "dxf"),
            "omniverse": ("omniverse", "usd"), "isaac_sim": ("isaac",)}


def code_coordinates(question):
    path = Path(__file__).resolve().parents[1] / "route/query.py"
    old_path = sys.path[:]
    try:
        spec = importlib.util.spec_from_file_location("vqa3d_component_route_query", path)
        query = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(query)
        component = path.parents[1]
        rows = {(row["type"], row["id"]): row for row in query.load()}
        # route/build.py persists only tracked files. Use its exact AST row
        # builder in memory so this new package has coordinates before staging,
        # while route/index and the frozen benchmark stay byte unchanged.
        for source in sorted(Path(__file__).resolve().parent.rglob("*.py")):
            relative = source.relative_to(component).as_posix()
            added, problem = query.B.py_rows(relative, source.read_text(encoding="utf-8"))
            if problem:
                raise ValueError("adapter_coordinate_parse_failed:" + relative)
            for row in added:
                rows[(row["type"], row["id"])] = row
        result = query.Index(list(rows.values())).any(question, k=5)
        return dict(result, scope="tracked_index_and_read_only_vqa3d_overlay")
    finally:
        sys.path[:] = old_path


def route_intent(text):
    if type(text) is not str:
        raise ValueError("intent_must_be_string")
    terms = set(re.findall(r"[a-z0-9]+", text.lower()))
    matched = [name for name, words in KEYWORDS.items() if terms.intersection(words)]
    coordinates = code_coordinates(text)
    if len(matched) != 1:
        return {"adapter": None, "status": None, "rc": 3,
                "abstain": "no_keyword" if not matched else "ambiguous_adapter",
                "candidates": matched, "code": coordinates}
    name = matched[0]
    status = ADAPTERS[name].status
    if not coordinates.get("rows"):
        # The keyword table proves the adapter identity even when the intent's
        # wording is absent from the lexical index. Query that explicit identity.
        missing = coordinates.get("abstain", "no_match")
        coordinates = dict(code_coordinates(name), query=name, intent_abstain=missing)
    return {"adapter": name, "status": status, "rc": 0 if status == "implemented" else 3,
            "code": coordinates}
