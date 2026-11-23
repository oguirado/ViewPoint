"""Bake -> UV -> linear ORM -> covered-texel PBR, publishing one immutable bundle.

Run with an explicit component import path (also supported by the test bootstrap).
The optional protected path keeps admitted UV0 and publishes a fixture-level textured
GLB after registered colour transfer, readback and changed-light checks. Factory
manifest assignment and production qualification belong to integration.
Existing destinations are refused; all imported writers see owned scratch paths.
"""
import argparse
import fcntl
import functools
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import threading

from . import look_bake as support
from .bake_support import (Refusal, canonical, core, load_record, read_bytes, require_receipt, result,
                           safe_glb, sha, upstream, write_record)
from .orm import pack_maps, verify_channels
from .uv import check_uv, check_uv_quality
from . import topology

MEMORY_SCOPE = ["systemd-run", "--user", "--scope", "-q", "-p", "MemoryHigh=6G",
                "-p", "MemoryMax=8G", "-p", "MemorySwapMax=0"]


def scoped_runner(argv, timeout):
    # Keep the upstream three-value runner contract on the guarded host path.
    try:
        code, output = topology.default_runner(MEMORY_SCOPE + argv, timeout)
    except subprocess.TimeoutExpired as exc:
        raise Refusal(6, "deadline_exceeded") from exc
    return code, output, ""


def _tool_files(protected=False):
    paths = {Path(upstream(n).__file__).resolve() for n in
             ("bake_pbr", "glb_core", "orm_pack", "pbr_validate", "uv_gate", "mesh_gate", "limits", "turntable")}
    paths.add(upstream("bake_pbr").WORKER.resolve())
    component = Path(__file__).resolve().parents[2]
    sibling = Path(upstream("bake_pbr").__file__).resolve().parents[2]
    paths.update(Path(__file__).resolve().with_name(name) for name in ("bake.py", "bake_support.py", "orm.py", "uv.py"))
    paths.add(Path(core.__file__).resolve())
    if protected:
        from . import glb_io, uv_projection, uv_quality
        from gates import texture_cleanliness
        from tools.input import evidence
        paths.update(Path(module.__file__).resolve() for module in
                     (support, glb_io, uv_projection, uv_quality, texture_cleanliness, evidence))
    paths.update(Path(module.__file__).resolve() for module in
                 (topology, topology.af_proc, topology.resources))
    files = []
    for p in paths:
        root, prefix = (component, "component") if p.is_relative_to(component) else (sibling, "sibling")
        files.append({"path": prefix + "/" + p.relative_to(root).as_posix(), "sha256": sha(read_bytes(p))})
    files.append({"path": "runtime/blender", "sha256": sha(read_bytes(Path(upstream("bake_pbr").BLENDER).resolve()))})
    return sorted(files, key=lambda f: f["path"])


def _bounded(function):
    """Own a host deadline and restore signal state; refuse unsupported threads."""
    @functools.wraps(function)
    def run(*args, **kwargs):
        timeout = kwargs.get("timeout_s", 300)
        if type(timeout) is not int or timeout <= 0:
            return result(2, "policy_invalid")
        if threading.current_thread() is not threading.main_thread() or signal.getitimer(signal.ITIMER_REAL)[0]:
            return result(3, "deadline_unavailable")
        def expired(signum, frame):
            raise Refusal(6, "terminated" if signum == signal.SIGTERM else "deadline_exceeded")
        previous = {sig: signal.signal(sig, expired) for sig in (signal.SIGTERM, signal.SIGALRM)}
        signal.setitimer(signal.ITIMER_REAL, timeout)
        try:
            return function(*args, **kwargs)
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            for sig, handler in previous.items(): signal.signal(sig, handler)
    return run


def _check_ram(path):
    """The foreign RAM record is telemetry, not a domain PASS receipt."""
    rec = load_record(read_bytes(path))
    if not isinstance(rec, dict) or rec.get("schema") != "bvqa.asset-ram/v1":
        raise Refusal(2, "ram_receipt_invalid")
    if ("rc" in rec and (type(rec["rc"]) is not int or rec["rc"] != 0)) or any(
            key in rec and rec[key] != "PASS" for key in ("status", "verdict")):
        raise Refusal(3, "ram_receipt_not_pass")
    keys = ("ram_peak_bytes", "quota", "released_bytes", "swap_used_delta")
    if any(type(rec.get(k)) is not int for k in keys) or rec.get("fstype") != "tmpfs":
        raise Refusal(3, "ram_telemetry_unknown")
    if rec["quota"] <= 0 or rec["ram_peak_bytes"] < 0 or rec["released_bytes"] < 0:
        raise Refusal(2, "ram_telemetry_invalid")
    if rec["ram_peak_bytes"] > rec["quota"] or rec.get("warnings"):
        raise Refusal(6, "ram_budget_exceeded")


def _check_bake(receipt, tool, staging, high, low, res):
    """Re-evaluate bytes independently of worker messages or receipt verdicts."""
    require_receipt(0, receipt, tool.SCHEMA)
    if receipt.get("input_sha256") != sha(read_bytes(low)) or receipt.get("high_sha256") != sha(read_bytes(high)):
        raise Refusal(4, "bake_input_drift")
    scene = upstream("glb_core").parse_glb(read_bytes(low))
    checks, failed = tool.verify_outputs(staging, scene, res)
    if failed:
        raise Refusal(5, "bake_output_failed")
    if not checks.get("coverage_ppm") or not checks.get("maps"):
        raise Refusal(3, "bake_zero_checks")
    if receipt.get("checks") != checks:
        raise Refusal(4, "bake_receipt_drift")
    return checks


def _pbr(staging, bake_checks, previous):
    """Run the imported PBR checks over *covered* albedo, plus the full ORM.

    Bake gutters/uncovered background are not dielectric material observations.
    Persist the coverage-derived probe so the receipt preimage is explicit.
    """
    b, g, png = upstream("bake_pbr"), upstream("glb_core"), upstream("orm_pack")
    scene = g.parse_glb(read_bytes(staging / "low_uv.glb"))
    res = bake_checks["maps"]["albedo"]["w"]
    cov = sorted(b.coverage(b.uv_tris(scene), res))
    w, _, ch, pixels = png.decode_png(read_bytes(staging / "albedo.png"))
    if not cov or ch < 3:
        raise Refusal(3, "albedo_zero_checks")
    selected = [pixels[(y * w + x) * ch:(y * w + x) * ch + 3] + b"\xff" for x, y in cov]
    tool = upstream("pbr_validate")
    # Gate the exact coverage before padding the persisted probe. Padding must
    # never dilute an invalid-texel fraction at the policy boundary.
    bad = 0
    for start in range(0, len(selected), res):
        row = selected[start:start + res]
        metrics, failures = tool.check_albedo_png(png.encode_png(len(row), 1, b"".join(row)))
        if set(failures) - {"albedo_texture"}:
            raise Refusal(5, "covered_albedo_failed")
        bad += metrics["out_of_range_px"]
    if bad * tool.PPM // len(selected) > tool.ALBEDO_OUT_MAX_PPM:
        raise Refusal(5, "covered_albedo_range")
    # Declared neutral padding cannot amplify an isolated bad covered texel.
    selected += [bytes((128, 128, 128, 255))] * (res * res - len(selected))
    probe = staging / "covered_albedo.png"
    probe.write_bytes(png.encode_png(res, res, b"".join(selected)))
    rc, rec = tool.run(["--albedo", str(probe), "--orm", str(staging / "orm.png"),
                        "--prev-receipt", str(previous)])
    require_receipt(rc, rec, tool.SCHEMA, link="prev_sha256", previous=sha(read_bytes(previous)))
    if type(rec.get("checks")) is not int or rec["checks"] != 2:
        raise Refusal(3, "pbr_zero_checks")
    if rec.get("input_sha256") != sha(b"\0".join([read_bytes(probe), read_bytes(staging / "orm.png")])):
        raise Refusal(4, "pbr_input_drift")
    return rec


def verify_bundle(bundle, *, expected_receipt_sha256, production=False, read_checked=None):
    """Verify committed bytes and replay protected LOOK with its authorized reader."""
    try:
        root = Path(bundle)
        if root.is_symlink():
            raise Refusal(2, "symlink_refused")
        raw = read_bytes(root / "bundle_receipt.json")
        if sha(raw) != expected_receipt_sha256:
            raise Refusal(4, "bundle_receipt_drift")
        rec = load_record(raw)
        if not isinstance(rec, dict) or type(rec.get("rc")) is not int:
            raise Refusal(2, "bundle_record_invalid")
        if rec.get("rc") != 0 or rec.get("status") != "OK" or not rec.get("files"):
            raise Refusal(3, "bundle_incomplete")
        if production and rec.get("runner", {}).get("kind") != "scoped-cpu":
            raise Refusal(3, "injected_runner")
        if production:
            # F2-U3 has no frozen factory manifest/schema assignment. Integrity
            # and a real runner cannot establish factory or asset admission.
            raise Refusal(3, "factory_manifest_schema_unassigned")
        if not rec.get("tool_files"):
            raise Refusal(3, "toolchain_unknown")
        # Never follow arbitrary tool paths supplied by a receipt.
        protected = rec.get("scope") == "protected-look-fixture"
        if rec["tool_files"] != (_tool_files(True) if protected else _tool_files()):
            raise Refusal(4, "toolchain_drift")
        names = set()
        for entry in rec["files"]:
            name = entry["path"]
            if not isinstance(name, str) or Path(name).name != name or name in names:
                raise Refusal(2, "bundle_path_invalid")
            names.add(name)
            if sha(read_bytes(root / name)) != entry["sha256"]:
                raise Refusal(4, "bundle_bytes_drift")
        if {p.name for p in root.iterdir()} != names | {"bundle_receipt.json"}:
            raise Refusal(4, "bundle_inventory_drift")
        _check_ram(root / "ram_receipt.json")
        if protected:
            if not callable(read_checked):
                raise Refusal(3, "look_reader_missing")
            if not {role + ".mips.rgba" for role in support.LOOK_MAP_ENCODINGS} <= names:
                raise Refusal(3, "look_mip_bytes_missing")
            high, geometry = (read_bytes(root / name) for name in ("source_high.glb", "source_low.glb"))
            look = _look_inputs(high, geometry, witness_digest=rec["look_witness_digest"], read_checked=read_checked)
            _check_preserved_atlas(read_bytes(root / "low_uv.glb"), look)
            replay = _inspect_look(root / "textured.glb", geometry, look, _stored_mips(root), root)
            if replay["rc"]:
                return replay
            if canonical(replay).encode("utf-8") != read_bytes(root / "look_receipt.json"):
                raise Refusal(4, "look_receipt_drift")
        return result(0, scope="protected-look-replay" if protected else "byte-integrity",
                      production_evidence=production,
                      checks_evaluated=len(names), receipt_sha256=sha(raw))
    except Refusal as exc:
        return result(exc.rc, exc.reason)
    except Exception as exc:
        return result(2, "bundle_error", detail=str(exc))


def _look_scoped_runner(argv, timeout):
    try:
        scope = topology.resources.cgroup_observation()
    except topology.resources.ObservationUnavailable:
        scope = {}
    maximum, high, swap = (scope.get(k) for k in
                           ("memory_max_bytes", "memory_high_bytes", "swap_max_bytes"))
    worker = ["taskset", "-c", "0-15", sys.executable, "-I", "-B", "-c",
              "import os, sys; os.setpriority(os.PRIO_PROCESS, 0, 15); "
              "os.execvp(sys.argv[1], sys.argv[1:])", *argv]
    if not (maximum is not None and maximum <= 8 * 2**30
            and high is not None and high <= 6 * 2**30 and swap == 0):
        # The long option preserves the scope layout while bypassing topology's
        # exact-prefix rewrite, which would apply a second, relative niceness.
        worker = [*MEMORY_SCOPE[:3], "--quiet", *MEMORY_SCOPE[4:], *worker]
    try:
        code, output = topology.default_runner(worker, timeout)
    except subprocess.TimeoutExpired as exc:
        raise Refusal(6, "deadline_exceeded") from exc
    return code, output, ""


def _look_runner(runner, threads, protected):
    """Set actual Cycles threads while keeping the factory-owned process path."""
    def run(argv, timeout):
        worker = Path(upstream("bake_pbr").WORKER).resolve()
        if argv[4:6] != ["--python", str(worker)]:
            raise Refusal(4, "look_worker_argv_drift")
        component = str(Path(__file__).resolve().parents[2])
        expression = (f"import sys; sys.path.insert(0, {component!r}); "
                      "from tools.blender.look_bake import run_look_worker; "
                      f"run_look_worker({str(worker)!r}, {threads!r}, {protected!r})")
        return runner([*argv[:4], "--threads", str(threads), "--python-exit-code", "2",
                       "--python-expr", expression, *argv[6:]], timeout)
    return run


def _look_inputs(high, geometry, *, witness_digest, read_checked):
    from . import uv_projection, uv_quality
    from tools.input.evidence import InputIssue, bytes_digest, checked_record, closed, is_digest, require

    try:
        require(callable(read_checked), 2, "look_reader_invalid")
        witness = checked_record(read_checked, "look-bake-witness", witness_digest)
        optional = ("normal_probe", "orm_reference_digest")
        closed(witness, "af.look-bake-witness/v1", "mesh_digest", "high_digest", "atlas_digest",
               "projection_digest", "geometry_receipt_digest", "protected_regions_digest",
               "albedo_reference_digest", "attenuation_digest", "attenuation_floor_byte", "light_digests",
               "tile_px", "face_max_ppm", "face_tile_ppm", "shadow_max_ppm",
               *(key for key in optional if key in witness))
        subjects = [bytes_digest(high), bytes_digest(geometry)]
        require([witness["high_digest"], witness["mesh_digest"]] == subjects, 4, "look_subject_drift")
        geo = checked_record(read_checked, "look-geometry-receipt", witness["geometry_receipt_digest"])
        require(geo.get("schema") == "af.mesh-regression/v2" and type(geo.get("measurement_rc")) is int
                and geo["measurement_rc"] == 0 and is_digest(geo.get("native_digest")),
                3, "look_geometry_measurement_missing")
        require(type(geo.get("subjects")) is list and len(geo["subjects"]) >= 2
                and geo["subjects"][:2] == subjects, 4, "look_geometry_subject_drift")
        require(any(pair.get("from") == 0 and pair.get("to") == 1 for pair in geo.get("pairs", [])),
                3, "look_geometry_pair_missing")
        atlas = checked_record(read_checked, "uv-layout", witness["atlas_digest"])
        size, height = uv_quality.raster_size(atlas["size"])
        require(size == height and size <= 1024 and size & (size - 1) == 0, 2, "look_atlas_resolution_invalid")
        sampler = atlas["sampler"]
        level = sampler.get("level")
        require(type(level) is int and 0 <= level < size.bit_length(), 2, "look_mip_level_invalid")
        work = size**2
        projection, projected = uv_projection.project_registered(
            geometry, layout_digest=witness["projection_digest"], read_checked=read_checked)
        require(projection["source_rc"] == 0, projection["source_rc"],
                next(iter(projection["reason_codes"]), "look_projection_unavailable"))
        require(projection["size"] == [size, size], 4, "look_projection_size_drift")
        samples = {record["index"] for record in projection["texels"]}
        require(bool(samples), 3, "look_projection_support_missing")
        gutters = atlas["gutters"]
        require(type(gutters) is list and len(gutters) <= work - len(samples), 2, "look_gutters_invalid")
        require(bool(gutters), 3, "look_gutter_witness_missing")
        padded = set()
        for row in gutters:
            require(type(row) is list and len(row) == 2 and all(type(i) is int for i in row),
                    2, "look_gutters_invalid")
            dst, src = row
            require(0 <= dst < work and dst not in samples and dst not in padded and src in samples,
                    5, "uv_gutter_support_invalid")
            padded.add(dst)
        fields = (
            ("look-protected-regions", "protected_regions_digest", work),
            ("look-independent-albedo", "albedo_reference_digest", work * 4),
            ("look-capture-attenuation", "attenuation_digest", work),
        )
        regions, reference, atten = (uv_quality.checked_bytes(read_checked, role, witness[key], length)
                                     for role, key, length in fields)
        require(set(regions) == {0, 1, 2}, 3, "look_protected_regions_missing")
        require(all(i in samples and reference[4*i + 3] == 255 for i, region in enumerate(regions) if region),
                5, "look_protected_support_changed")
        light_refs = witness["light_digests"]
        require(type(light_refs) is list and len(light_refs) == 2 and light_refs[0] != light_refs[1],
                3, "look_changed_light_missing")
        lights = [uv_quality.checked_bytes(read_checked, "look-illumination", pin, work) for pin in light_refs]
        protected = [i for i, region in enumerate(regions) if region]
        require(all(lights[1][i] > 0 for i in protected)
                and any(lights[0][i] != lights[1][i] for i in protected if regions[i] == 1)
                and any(lights[0][i] < lights[1][i] for i in protected if regions[i] == 2),
                3, "look_changed_light_unobserved")
        floor, tile = witness["attenuation_floor_byte"], witness["tile_px"]
        limits = [witness[key] for key in ("face_max_ppm", "face_tile_ppm", "shadow_max_ppm")]
        require(type(floor) is int and 0 < floor <= 255 and type(tile) is int and 0 < tile <= size
                and all(type(v) is int and 0 <= v < 1_000_000 for v in limits),
                2, "look_relight_policy_invalid")
        base = support.debake(projected, samples, atten, floor)
        # Exercise the accepted atlas checker before Blender using occupancy only.
        # Both embedded linear maps are checked again on their actual reopened bytes.
        opaque = bytes((128, 128, 255, 255)) * work
        coverage = support.chart_pixels(opaque, samples, gutters)
        native = check_uv_quality(geometry, witness_digest=witness["atlas_digest"], read_checked=read_checked,
                                  sampler=sampler, mips=support.look_mips(coverage, size, level, "normal"))
        require(native["source_rc"] == 0, native["source_rc"],
                next(iter(native["reason_codes"]), "look_uv_unavailable"))
        # Additionally check all tangent frames, including positive-orientation faces.
        faces, tangents = _look_faces(geometry, atlas)
        require(all(valid is not None for valid in tangents.values()), 3, "look_tangent_missing")
        require(all(tangents.values()), 5, "look_tangent_invalid")
        probe = None
        if "normal_probe" in witness:
            probe = support.normal_probe(geometry, atlas, witness["normal_probe"], samples)
        orm_reference = None
        if "orm_reference_digest" in witness:
            orm_reference = uv_quality.checked_bytes(read_checked, "look-linear-orm-reference",
                                                     witness["orm_reference_digest"], work * 4)
        return dict(witness=witness, atlas=atlas, faces=faces, witness_digest=witness_digest, read_checked=read_checked,
                    size=size, level=level, sampler=sampler, samples=samples, gutters=gutters,
                    projection=projection, uv_receipt=native, base=support.chart_pixels(base, samples, gutters),
                    reference=reference, regions=regions, lights=lights, limits=limits, tile=tile,
                    normal_probe=probe, orm_reference=orm_reference)
    except InputIssue as exc:
        raise Refusal(exc.rc, exc.code) from exc


def _look_faces(raw, atlas):
    from . import uv_quality

    _, faces, tangents = uv_quality.load_triangles(raw, atlas, mirrored=set(range(len(atlas["charts"]))))
    triangles = []
    for xyz, uv in faces:
        a, b, c = zip(xyz, uv)
        triangles.append(min((a, b, c), (c, a, b), (b, c, a)))
    return sorted(triangles), tangents


def _check_preserved_atlas(output, look):
    """Compare exact oriented corners after reading Blender's static GLB."""
    from tools.input.evidence import InputIssue, bytes_digest

    atlas = {**look["atlas"], "mesh_digest": bytes_digest(output)}
    try:
        faces, tangents = _look_faces(output, atlas)
    except InputIssue as exc:
        raise Refusal(exc.rc, exc.code) from exc
    if look["faces"] != faces:
        raise Refusal(5, "look_worker_atlas_drift")
    if not all(tangents.values()):
        raise Refusal(5, "look_worker_tangent_drift")


def _stored_mips(root):
    return {role: read_bytes(root / (role + ".mips.rgba")) for role in support.LOOK_MAP_ENCODINGS}


def _worker_colors(root, witness):
    report = load_record(read_bytes(root / "bake_receipt.json"))
    require_receipt(0, report, upstream("bake_pbr").SCHEMA)
    if (report.get("input_sha256") != witness["mesh_digest"].removeprefix("sha256:")
            or report.get("high_sha256") != witness["high_digest"].removeprefix("sha256:")):
        raise Refusal(4, "bake_input_drift")
    worker = report.get("worker")
    spaces = worker.get("map_color_spaces") if isinstance(worker, dict) else None
    if type(spaces) is not dict or spaces.keys() != {*support.DATA_MAPS, "albedo"} or any(
            type(space) is not str for space in spaces.values()):
        raise Refusal(3, "look_worker_color_space_missing")
    if any(spaces[role] != "Non-Color" for role in support.DATA_MAPS):
        raise Refusal(5, "look_worker_data_color_space_invalid")
    return spaces


def _worker_map(staging, look, role):
    size = look["size"]
    png = upstream("orm_pack")
    w, h, channels, pixels = png.decode_png(read_bytes(staging / (role + ".png")))
    if (w, h) != (size, size) or channels not in (3, 4):
        raise Refusal(5, "look_worker_map_invalid")
    rgba = pixels if channels == 4 else b"".join(pixels[i:i+3] + b"\xff" for i in range(0, len(pixels), 3))
    return support.chart_pixels(rgba, look["samples"], look["gutters"])


def _inspect_look(path, geometry, look, mip_bytes, worker_dir=None):
    from tools.input.evidence import bytes_digest

    raw = read_bytes(path)
    images = support.read_look_maps(raw, geometry)
    size, level = look["size"], look["level"]
    maps = {role: support.decode_look_png(image, size) for role, image in images.items()}
    if type(mip_bytes) is not dict or mip_bytes.keys() != maps.keys() or any(
            type(payload) is not bytes for payload in mip_bytes.values()):
        raise Refusal(3, "look_mip_bytes_missing")
    mip_digests, uv_receipts = {}, {}
    roughness_floor = upstream("orm_pack").ASSET_ORM_ROUGH_MIN_BYTE
    for i in look["samples"]:
        x, y, z = (2 * v - 255 for v in maps["normal"][4*i:4*i + 3])
        norm2 = x*x + y*y + z*z
        # Require Z to remain positive across byte quantization. The 1% norm
        # window encloses the error of rounding a unit vector to RGB8.
        if z <= 1 or not (99*255)**2 <= 100**2 * norm2 <= (101*255)**2:
            raise Refusal(5, "look_linear_normal_invalid")
        if maps["orm"][4*i + 1] < roughness_floor:
            raise Refusal(5, "look_linear_orm_invalid")
    probe = look["normal_probe"]
    if probe is not None:
        green = maps["normal"][4 * probe["texel"] + 1]
        if (2 * green - 255) * probe["expected_y_sign"] <= 1:
            raise Refusal(5, "look_normal_handedness_changed")
    for role, rgba in maps.items():
        if rgba != support.chart_pixels(rgba, look["samples"], look["gutters"]):
            raise Refusal(5, "uv_gutter_pixels_changed")
        if any(rgba[i*4 + 3] != 255 for i in look["samples"]):
            raise Refusal(5, "look_texture_support_changed")
        mips = support.look_mips(rgba, size, level, role)
        if mip_bytes[role] != b"".join(mips[1:]):
            raise Refusal(5, "look_mip_pixels_changed")
        mip_digests[role] = [bytes_digest(mip) for mip in mips]
        if role != "base_color":
            uv = check_uv_quality(geometry, witness_digest=look["witness"]["atlas_digest"],
                                  read_checked=look["read_checked"], sampler=look["sampler"], mips=mips)
            if uv["source_rc"]:
                raise Refusal(uv["source_rc"], uv["reason_codes"][0])
            uv_receipts[role] = uv
    reference_light, changed_light = look["lights"]
    relighting = support.relight_metrics(maps["base_color"], look["reference"], look["regions"],
                                         reference_light, changed_light, size, look["tile"])
    face_max, face_tile, shadow_max = look["limits"]
    reason = None
    if relighting["changed_shadow_max_upper_ppm"] > shadow_max:
        reason = "look_stationary_baked_shadow"
    if relighting["changed_face_max_upper_ppm"] > face_max or relighting["worst_face_tile_upper_ppm"] > face_tile:
        reason = "look_protected_face_changed"
    if reason is None and maps["base_color"] != look["base"]:
        reason = "look_source_colour_drift"
    spaces = None
    if not reason and look["orm_reference"] is not None:
        expected = support.chart_pixels(look["orm_reference"], look["samples"], look["gutters"])
        if maps["orm"] != expected:
            raise Refusal(5, "look_linear_orm_reference_changed")
    if not reason and worker_dir is not None:
        spaces = _worker_colors(worker_dir, look["witness"])
        verify_channels(*(worker_dir / (role + ".png") for role in ("ao", "roughness", "metal", "orm")))
        for role in ("normal", "orm"):
            if maps[role] != _worker_map(worker_dir, look, role):
                raise Refusal(5, "look_worker_map_bytes_changed")
    elif not reason and look["orm_reference"] is None:
        raise Refusal(3, "look_linear_orm_evidence_missing")
    return result(5 if reason else 0, *([reason] if reason else []), subject_digest=bytes_digest(raw),
                  witness_digest=look["witness_digest"], material_slot=0,
                  map_encodings=dict(support.LOOK_MAP_ENCODINGS), worker_map_color_spaces=spaces,
                  normal_probe=probe,
                  map_digests={role: bytes_digest(image) for role, image in images.items()},
                  mip_digests=mip_digests, uv_checks=uv_receipts, relighting=relighting,
                  geometry_receipt_digest=look["witness"]["geometry_receipt_digest"],
                  projection=look["projection"], scope="protected-look-fixture",
                  production_evidence=False, production_refusal="factory_manifest_schema_unassigned")


def verify_look_bake(mesh, *, high, geometry, witness_digest, read_checked, mip_bytes):
    """Recompute fixture admission from reopened embedded maps and authorized input bytes."""
    try:
        look = _look_inputs(high, geometry, witness_digest=witness_digest, read_checked=read_checked)
        return _inspect_look(mesh, geometry, look, mip_bytes)
    except Refusal as exc:
        return result(exc.rc, exc.reason)
    except MemoryError:
        return result(6, "look_memory_budget")
    except (OSError, ValueError, KeyError, IndexError, TypeError, AttributeError) as exc:
        return result(2, "look_boundary_invalid", detail=str(exc))


def _publish_look(staging, geometry, look):
    size, level = look["size"], look["level"]
    png = upstream("orm_pack")
    maps = {"base_color": look["base"], **{role: _worker_map(staging, look, role) for role in ("normal", "orm")}}
    glb = staging / "textured.glb"
    glb.write_bytes(support.embed_look_maps(geometry, {role: png.encode_png(size, size, pixels)
                                                       for role, pixels in maps.items()}))
    files = {role: staging / (role + ".mips.rgba") for role in maps}
    for role, path in files.items():
        path.write_bytes(b"".join(support.look_mips(maps[role], size, level, role)[1:]))
    receipt = _inspect_look(glb, geometry, look, _stored_mips(staging), staging)
    if receipt["rc"]:
        raise Refusal(receipt["rc"], receipt["reason_codes"][0])
    write_record(staging / "look_receipt.json", receipt)
    return {"textured.glb", "look_receipt.json", *(path.name for path in files.values())}


@_bounded
def bake_bundle(high, low, destination, *, res=1024, margin_px=8, cage_extrusion_um=5000,
                timeout_s=300, max_stretch=4000, runner=None, look_scope=None, blender_threads=None):
    """Stage result with retained input snapshots and a guarded CPU worker.

    look_scope supplies a fixture witness digest and an authorized evidence
    reader. Legacy callers still receive their UV/maps bundle. The fixture
    thresholds and native GEO measurements do not qualify a production run.
    """
    staging, lockfd, lock, reserved, published = None, None, None, False, None
    started = time.monotonic()
    try:
        for v in (res, margin_px, cage_extrusion_um, timeout_s, max_stretch):
            if type(v) is not int:
                raise Refusal(2, "policy_invalid")
        if res < 64 or res > 1024 or res & (res - 1) or margin_px < 0 or cage_extrusion_um < 0 or timeout_s <= 0:
            raise Refusal(2, "policy_invalid")
        protected = look_scope is not None
        if blender_threads is not None and (type(blender_threads) is not int or not 1 <= blender_threads <= 16):
            raise Refusal(2, "blender_threads_invalid")
        threads = 5 if blender_threads is None else blender_threads
        dest = Path(destination)
        if dest.is_symlink() or dest.exists():
            raise Refusal(5, "destination_exists")
        if not dest.parent.is_dir():
            raise Refusal(2, "destination_parent_missing")
        source_paths = [Path(high), Path(low)]
        originals = [read_bytes(p) for p in source_paths]
        for raw in originals:
            safe_glb(raw)
        look = None
        if protected:
            if type(look_scope) is not dict or look_scope.keys() != {"witness_digest", "read_checked"}:
                raise Refusal(2, "look_scope_invalid")
            look = _look_inputs(*originals, **look_scope)
            if look["size"] != res:
                raise Refusal(4, "look_atlas_resolution_drift")
        # Stable inode: never unlink a flock file while another waiter may hold it.
        lock = dest.parent / (".f2u3-lock-" + dest.name)
        try:
            lockfd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            fcntl.flock(lockfd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(lockfd)
            lockfd = None
            raise Refusal(5, "destination_locked") from exc
        for stale in dest.parent.glob("." + dest.name + ".f2u3-*"):
            if stale.is_symlink() or stale.stat().st_uid != os.getuid():
                raise Refusal(2, "stale_staging_unowned")
            shutil.rmtree(stale)
        staging = Path(tempfile.mkdtemp(prefix="." + dest.name + ".f2u3-", dir=dest.parent))
        hp, lp = staging / "source_high.glb", staging / "source_low.glb"
        hp.write_bytes(originals[0])
        lp.write_bytes(originals[1])
        tool = upstream("bake_pbr")
        tool_files = _tool_files(True) if protected else _tool_files()
        args = ["--high", str(hp), "--low", str(lp), "--out-dir", str(staging), "--res", str(res),
                "--margin-px", str(margin_px), "--cage-extrusion-um", str(cage_extrusion_um),
                "--timeout", str(timeout_s)]
        instrumented = protected or blender_threads is not None
        active_runner = runner or (_look_scoped_runner if instrumented else scoped_runner)
        if instrumented:
            active_runner = _look_runner(active_runner, threads, protected)
        rc, bake_rec = tool.run(args, runner=active_runner)
        require_receipt(rc, bake_rec, tool.SCHEMA)
        _check_ram(staging / "ram_receipt.json")
        checks = _check_bake(bake_rec, tool, staging, hp, lp, res)
        write_record(staging / "bake_receipt.json", bake_rec)
        if protected:
            worker_geometry = read_bytes(staging / "low_uv.glb")
            _check_preserved_atlas(worker_geometry, look)
            uv_record = {**look["uv_receipt"], "worker_uv_sha256": sha(worker_geometry),
                         "bake_receipt_sha256": sha(read_bytes(staging / "bake_receipt.json"))}
            uv_faces = len(look["faces"])
        else:
            uv = check_uv(staging / "low_uv.glb", previous=staging / "bake_receipt.json", max_stretch=max_stretch)
            if uv["rc"]:
                return result(uv["rc"], *uv["reason_codes"])
            uv_record = uv["receipt"]
            uv_faces = sum(p["uv_tris"] for p in uv_record["prims"])
        write_record(staging / "uv_receipt.json", uv_record)
        orm_rec, orm_metrics = pack_maps(staging, previous=staging / "uv_receipt.json")
        write_record(staging / "orm_receipt.json", orm_rec)
        if not protected:
            pbr_rec = _pbr(staging, checks, staging / "orm_receipt.json")
            write_record(staging / "pbr_receipt.json", pbr_rec)
        if protected:
            measured = bake_rec.get("worker", {})
            if runner is None and (measured.get("threads") != threads or measured.get("device") != "CPU"
                                   or measured.get("cpu_affinity") != list(range(16)) or measured.get("nice") != 15):
                return result(3, "look_cpu_policy_unwitnessed", worker=measured)
            look_files = _publish_look(staging, originals[1], look)
        if [read_bytes(p) for p in source_paths] != originals or [read_bytes(hp), read_bytes(lp)] != originals:
            raise Refusal(4, "source_bytes_drift")
        # Recheck the entire baked map set after every downstream writer.
        _check_bake(bake_rec, tool, staging, hp, lp, res)
        files = [{"path": p.name, "sha256": sha(read_bytes(p))} for p in sorted(staging.iterdir())]
        expected = {"source_high.glb", "source_low.glb", "low_uv.glb", "orm.png", "covered_albedo.png",
                    "bake_receipt.json", "uv_receipt.json", "orm_receipt.json", "pbr_receipt.json",
                    "ram_receipt.json"} | {n + ".png" for n in tool.MAPS}
        if protected:
            expected.difference_update({"covered_albedo.png", "pbr_receipt.json"})
            expected.update(look_files)
        if {f["path"] for f in files} != expected:
            raise Refusal(3, "bundle_incomplete")
        rec = result(0, scope="protected-look-fixture" if protected else "verified-uv-maps",
                     files=files, tool_files=tool_files, orm=orm_metrics,
                     runner={"kind": "injected" if runner is not None else "scoped-cpu",
                             "identity": (type(runner).__module__ + "." + type(runner).__qualname__) if runner is not None else "scoped_runner"},
                     checks_evaluated=sum(m["covered"] for m in checks["maps"].values()) + orm_metrics["pixels_checked"] +
                     uv_faces + (0 if protected else pbr_rec["checks"]),
                     pending_gates=["factory_manifest_schema", "final_glb_material_binding", "raw_geometry_regression", "appearance"],
                     policy={"res": res, "margin_px": margin_px, "cage_extrusion_um": cage_extrusion_um,
                             "max_stretch_permille": max_stretch, "timeout_s": timeout_s})
        if protected:
            rec["pending_gates"] = ["factory_manifest_schema", "production_geometry_and_appearance"]
            rec["look_witness_digest"] = look["witness_digest"]
        if protected or blender_threads is not None:
            rec["policy"]["blender_threads"] = threads
        write_record(staging / "bundle_receipt.json", rec)
        digest = sha(read_bytes(staging / "bundle_receipt.json"))
        integrity = verify_bundle(staging, expected_receipt_sha256=digest,
                                  read_checked=look["read_checked"] if protected else None)
        if integrity["rc"]:
            raise Refusal(integrity["rc"], "staged_bundle_drift")
        if time.monotonic() - started >= timeout_s:
            raise Refusal(6, "deadline_exceeded")
        if dest.exists() or dest.is_symlink():
            raise Refusal(5, "destination_exists")
        for p in staging.iterdir():
            with p.open("rb") as stream:
                os.fsync(stream.fileno())
        # Exclusively reserve the name before replacing our own empty directory.
        dest.mkdir()
        reserved = True
        os.replace(staging, dest)
        staging = None
        reserved = False
        published = dest.stat().st_ino
        parentfd = os.open(dest.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(parentfd)
        finally:
            os.close(parentfd)
        published = None
        return result(0, scope=rec["scope"], bundle=str(dest), receipt_sha256=digest,
                      production_evidence=False, runtime_measured=runner is None,
                      checks_evaluated=rec["checks_evaluated"], pending_gates=rec["pending_gates"])
    except Refusal as exc:
        return result(exc.rc, exc.reason)
    except Exception as exc:
        # Imported domain exceptions do not share a base class. Preserve only
        # declared stage codes; every other unexpected boundary error is rc 2.
        value = getattr(exc, "rc", None)
        domain = tuple(cls for module, name in (("bake_pbr", "BakeError"), ("turntable", "TurntableError"))
                       if isinstance(cls := getattr(sys.modules.get("blender_visual_qa.assets." + module), name, None), type))
        code = value if isinstance(exc, domain) and type(value) is int and value in (3, 4, 5, 6) else 2
        return result(code, "bake_error", detail=str(exc))
    finally:
        if published is not None and not dest.is_symlink() and dest.stat().st_ino == published:
            shutil.rmtree(dest)
        if staging is not None:
            shutil.rmtree(staging, ignore_errors=True)
        if reserved:
            try: dest.rmdir()
            except OSError: pass
        if lockfd is not None:
            os.close(lockfd)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--high", required=True)
    p.add_argument("--low", required=True)
    p.add_argument("--out-dir", required=True)
    p.add_argument("--res", type=int, default=1024)
    p.add_argument("--margin-px", type=int, default=8)
    p.add_argument("--cage-extrusion-um", type=int, default=5000)
    p.add_argument("--max-stretch", type=int, default=4000)
    p.add_argument("--timeout", type=int, default=300)
    p.add_argument("--blender-threads", type=int)
    try:
        a = p.parse_args(argv)
    except SystemExit:
        print(canonical(result(2, "usage_invalid")), end="")
        return 2
    r = bake_bundle(a.high, a.low, a.out_dir, res=a.res, margin_px=a.margin_px,
                    cage_extrusion_um=a.cage_extrusion_um, timeout_s=a.timeout, max_stretch=a.max_stretch,
                    blender_threads=a.blender_threads)
    print(canonical(r), end="")
    return r["rc"]


if __name__ == "__main__":
    sys.exit(main())
