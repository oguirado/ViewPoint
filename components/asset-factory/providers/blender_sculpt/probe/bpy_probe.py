"""Blender-only disposable capability experiment; never opens caller assets."""

import argparse
import hashlib
import json
from pathlib import Path
import struct
import sys

import bpy


MARKER = "AF_SCULPT_PROBE "


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False) + "\n"


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def rna_value(value):
    # RNA floating values retain their exact binary value as tagged hex strings.
    if isinstance(value, float):
        return {"float_hex": value.hex()}
    if value is None or type(value) in (str, int, bool):
        return value
    return [rna_value(v) for v in value]


def properties(rna):
    result = []
    for prop in rna.properties:
        row = {"identifier": prop.identifier, "type": prop.type,
               "description": prop.description, "is_readonly": prop.is_readonly,
               "is_required": prop.is_required}
        for name in ("array_length", "subtype", "hard_min", "hard_max"):
            if hasattr(prop, name):
                row[name] = rna_value(getattr(prop, name))
        if hasattr(prop, "default_array") and getattr(prop, "is_array", False):
            row["default"] = rna_value(prop.default_array)
        elif hasattr(prop, "default"):
            row["default"] = rna_value(prop.default)
        if prop.type in ("POINTER", "COLLECTION"):
            row["fixed_type"] = prop.fixed_type.identifier
        if prop.type == "ENUM":
            row["enum_items"] = [{"identifier": v.identifier, "name": v.name,
                                  "description": v.description, "value": v.value}
                                 for v in prop.enum_items]
        result.append(row)
    return result


def mesh_bytes(obj):
    data = bytearray(struct.pack("<II", len(obj.data.vertices), len(obj.data.polygons)))
    for vertex in obj.data.vertices:
        data.extend(struct.pack("<fff", *vertex.co))
    for face in obj.data.polygons:
        data.extend(struct.pack("<I", len(face.vertices)))
        for index in face.vertices:
            data.extend(struct.pack("<I", index))
    return bytes(data)


def context_state():
    ctx = bpy.context
    return {"background": bpy.app.background, "window": ctx.window is not None,
            "area": ctx.area.type if ctx.area else None,
            "region": ctx.region.type if ctx.region else None, "mode": ctx.mode,
            "windows": len(ctx.window_manager.windows)}


def main(argv):
    parser = argparse.ArgumentParser()
    parser.add_argument("--case", choices=("rna", "unknown", "direct", "no_window", "object", "view3d"), required=True)
    parser.add_argument("--strength-ppm", type=int, choices=(0, 500000), default=0)
    args = parser.parse_args(argv)
    record = probe(args.case, args.strength_ppm)
    print(MARKER + canonical(record).rstrip(), flush=True)
    return record["rc"]


def probe(case, strength_ppm, *, reuse_fixture=False):
    args = argparse.Namespace(case=case, strength_ppm=strength_ppm, reuse_fixture=reuse_fixture)
    record = {"case": args.case, "strength_ppm": args.strength_ppm, "rc": 3,
              "status": "INCOMPLETE", "reason_codes": [], "runtime_version": bpy.app.version_string,
              "runtime_version_tuple": list(bpy.app.version), "initial_context": context_state(),
              "probe_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    try:
        if args.case == "unknown":
            # bpy.ops creates proxies for nonexistent attributes: get_rna_type is essential.
            try:
                bpy.ops.sculpt.af_s1_u1_unknown_operator.get_rna_type()
            except (AttributeError, RuntimeError, KeyError) as exc:
                record.update(rc=2, status="ERROR", reason_codes=["unknown_operator"], error=str(exc))
            else:
                record.update(rc=5, status="FAIL", reason_codes=["unknown_operator_accepted"])
        elif args.case == "rna":
            operator = bpy.ops.sculpt.brush_stroke
            rna = operator.get_rna_type()
            dump = {"identifier": rna.identifier, "properties": properties(rna),
                    "OperatorStrokeElement": properties(bpy.types.OperatorStrokeElement.bl_rna)}
            record.update(rc=0, status="OK", reason_codes=[], rna=dump, rna_sha256=digest(dump),
                          operator_poll=operator.poll(),
                          build_hash=bpy.app.build_hash.decode("ascii", errors="replace"))
        else:
            run_stroke(args, record)
    except Exception as exc:
        record.update(rc=2, status="ERROR", reason_codes=["probe_error"],
                      error=type(exc).__name__ + ": " + str(exc))
    return record


def run_stroke(args, record):
    if args.reuse_fixture:
        obj = bpy.context.object
        if obj is None or obj.name != "AF_S1_U1_Fixture" or obj.type != "MESH":
            raise ValueError("owned_fixture_missing")
    else:
        if bpy.context.object and bpy.context.object.mode != "OBJECT":
            bpy.ops.object.mode_set(mode="OBJECT")
        bpy.ops.object.select_all(action="SELECT")
        bpy.ops.object.delete(use_global=False)
        bpy.ops.mesh.primitive_uv_sphere_add(segments=64, ring_count=32, radius=1.0,
                                           enter_editmode=False, align="WORLD",
                                           location=(0.0, 0.0, 0.0), rotation=(0.0, 0.0, 0.0))
        obj = bpy.context.object
        obj.name = "AF_S1_U1_Fixture"
    if obj.mode != "SCULPT":
        bpy.ops.object.mode_set(mode="SCULPT")
    brush = bpy.data.brushes.new("AF_S1_U1_Draw", mode="SCULPT")
    brush.sculpt_tool = "DRAW"
    brush.strength = args.strength_ppm / 1000000
    brush.size = 80
    brush.use_pressure_strength = False
    brush.use_pressure_size = False
    bpy.context.tool_settings.sculpt.brush = brush
    bpy.context.tool_settings.unified_paint_settings.use_unified_strength = False
    bpy.context.tool_settings.unified_paint_settings.use_unified_size = False
    sculpt = bpy.context.tool_settings.sculpt
    sculpt.use_symmetry_x = sculpt.use_symmetry_y = sculpt.use_symmetry_z = False
    # Choose the analytic fixture's pole from its actual mesh, never planner coordinates.
    surface_point = tuple(max(obj.data.vertices, key=lambda v: (v.co.z, -v.index)).co)
    before = mesh_bytes(obj)
    before_vertices = [struct.pack("<fff", *v.co) for v in obj.data.vertices]
    record.update(before_sha256=hashlib.sha256(before).hexdigest(),
                  before_vertex_sha256=hashlib.sha256(b"".join(before_vertices)).hexdigest(),
                  reused_fixture=args.reuse_fixture,
                  brush={"tool": brush.sculpt_tool, "strength_hex": float(brush.strength).hex(),
                         "size": brush.size, "pressure_strength": brush.use_pressure_strength},
                  vertex_count=len(obj.data.vertices), polygon_count=len(obj.data.polygons))
    override = {}
    if args.case == "no_window":
        override = {"window": None, "area": None, "region": None}
    elif args.case == "object":
        override = {"active_object": obj, "object": obj}
    elif args.case == "view3d":
        for window in bpy.context.window_manager.windows:
            for area in window.screen.areas:
                region = next((r for r in area.regions if r.type == "WINDOW"), None)
                if area.type == "VIEW_3D" and region:
                    override = {"window": window, "area": area, "region": region}
                    break
            if override:
                break
        if not override:
            record.update(reason_codes=["view3d_context_missing"])
            return
    record["override_keys"] = sorted(override)
    with bpy.context.temp_override(**override):
        record["execution_context"] = context_state()
        operator = bpy.ops.sculpt.brush_stroke
        record["operator_poll"] = operator.poll()
        print("AF_SCULPT_PHASE " + canonical({"phase": "context_ready", "record": record}).rstrip(), flush=True)
        # Let the RNA-backed API itself reject the context; record the real failure.
        mouse = (0.0, 0.0)
        if bpy.context.region and bpy.context.region.type == "WINDOW":
            from mathutils import Quaternion, Vector
            from bpy_extras.view3d_utils import location_3d_to_region_2d
            space = bpy.context.space_data
            space.region_3d.view_rotation = Quaternion((1.0, 0.0, 0.0, 0.0))
            space.region_3d.view_distance = 4.0
            space.region_3d.view_location = Vector((0.0, 0.0, 0.0))
            space.region_3d.view_perspective = "ORTHO"
            bpy.context.view_layer.update()
            print("AF_SCULPT_PHASE " + canonical({"phase": "before_region_update", "record": record}).rstrip(), flush=True)
            # RegionView3D.update itself crashes in the measured background runtime.
            # Leave background projection untouched so the operator is tested too.
            if not bpy.app.background:
                space.region_3d.update()
            record["projection_update"] = not bpy.app.background
            point = location_3d_to_region_2d(bpy.context.region, space.region_3d, Vector(surface_point))
            if point is not None:
                mouse = tuple(point)
        stroke = [{"name": "", "location": surface_point, "mouse": mouse,
                   "mouse_event": mouse, "pressure": 1.0, "size": 80.0,
                   "pen_flip": False, "x_tilt": 0.0, "y_tilt": 0.0,
                   "time": 0.0, "is_start": True}]
        record["stroke"] = {
            "location_hex": [v.hex() for v in stroke[0]["location"]],
            "mouse_hex": [float(v).hex() for v in mouse], "samples": len(stroke)}
        try:
            print("AF_SCULPT_PHASE " + canonical({"phase": "before_operator", "record": record}).rstrip(), flush=True)
            result = operator("EXEC_DEFAULT", stroke=stroke, mode="NORMAL", ignore_background_click=False)
            record["operator_return"] = sorted(result)
        except RuntimeError as exc:
            record.update(reason_codes=["context_refused"], error=str(exc))
        bpy.context.view_layer.update()
    after = mesh_bytes(obj)
    after_vertices = [struct.pack("<fff", *v.co) for v in obj.data.vertices]
    record["after_vertex_sha256"] = hashlib.sha256(b"".join(after_vertices)).hexdigest()
    record["changed_vertices"] = sum(a != b for a, b in zip(before_vertices, after_vertices))
    record["after_sha256"] = hashlib.sha256(after).hexdigest()
    record["mesh_unchanged"] = before == after
    if record.get("operator_return") == ["FINISHED"]:
        if (args.strength_ppm == 0) == record["mesh_unchanged"]:
            record.update(rc=0, status="OK", reason_codes=[])
        else:
            record.update(rc=5, status="FAIL", reason_codes=["null_stroke_changed_mesh" if args.strength_ppm == 0 else "nonzero_stroke_no_effect"])
    elif not record["reason_codes"]:
        record["reason_codes"] = ["operator_not_finished"]


if __name__ == "__main__":
    sys.exit(main(sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []))
