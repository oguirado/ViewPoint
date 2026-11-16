"""Synthetic real-GLB controls and executable, temporary in-memory mutants."""
import base64
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))  # Explicit test-root bootstrap for python3 -I.
from tools.mesh import api, contracts, worker

VERTS = [(0, 0, 0), (1000000, 0, 0), (0, 1000000, 0), (0, 0, 1000000)]
FACES = [(0, 2, 1), (0, 1, 3), (0, 3, 2), (1, 2, 3)]


def glb(specs=None):
    """Hand-built fixture; seam-split corners, no dependency on the sibling writer."""
    specs = specs if specs is not None else [{"name": "body"}]
    binary = bytearray()
    views, accessors, nodes, meshes = [], [], [], []

    def add(values, fmt, kind):
        raw = struct.pack("<" + fmt * len(values), *values)
        views.append({"buffer": 0, "byteOffset": len(binary), "byteLength": len(raw)})
        binary.extend(raw)
        n = {"VEC3": 3, "VEC2": 2, "SCALAR": 1}[kind]
        accessors.append({"bufferView": len(views) - 1, "componentType": 5126 if fmt == "f" else 5125,
                          "count": len(values) // n, "type": kind})
        return len(accessors) - 1

    for spec in specs:
        verts, faces = spec.get("verts", VERTS), spec.get("faces", FACES)
        points = [verts[i] for face in faces for i in face]
        attrs = {"POSITION": add([x / 1000000 for point in points for x in point], "f", "VEC3")}
        if spec.get("uv"):
            uvs = [uv for k in range(len(faces)) for pair in
                   ((k / len(faces), 0), ((k + 0.8) / len(faces), 0), (k / len(faces), 0.8)) for uv in pair]
            if spec.get("degenerate_uv"):
                uvs = [0.0] * len(uvs)
            attrs["TEXCOORD_0"] = add(uvs, "f", "VEC2")
        primitive = {"attributes": attrs, "indices": add(list(range(len(points))), "I", "SCALAR"), "mode": 4}
        meshes.append({"primitives": [primitive]})
        node = {"name": spec["name"], "mesh": len(meshes) - 1}
        if "translation" in spec:
            node["translation"] = spec["translation"]
        if "scale" in spec:
            node["scale"] = spec["scale"]
        nodes.append(node)
    doc = {"asset": {"version": "2.0"}, "scene": 0, "scenes": [{"nodes": list(range(len(nodes)))}],
           "nodes": nodes, "meshes": meshes, "accessors": accessors, "bufferViews": views,
           "buffers": [{"byteLength": len(binary)}]}
    return pack(doc, bytes(binary))


def pack(doc, binary):
    header = json.dumps(doc, sort_keys=True, separators=(",", ":")).encode()
    header += b" " * (-len(header) % 4)
    binary += b"\0" * (-len(binary) % 4)
    chunks = struct.pack("<II", len(header), 0x4E4F534A) + header
    chunks += struct.pack("<II", len(binary), 0x004E4942) + binary
    return b"glTF" + struct.pack("<II", 2, len(chunks) + 12) + chunks


def policy(**changes):
    result = {"sample_budget": 128, "raster_resolution": 16, "view_basis": "gltf-y-up-ortho4",
              "unit": "metre", "transform": "world", "weld_tolerance_um": 0,
              "allow_open": False, "require_uv": False, "component_allowlist": {"body": 1}, "timeout_s": 20}
    result.update(changes)
    return result


def request(operation, raw, candidate, pol):
    rec = contracts.receipt(contracts.digest(raw), contracts.digest(candidate) if candidate else None,
                            contracts.json_digest(pol), api.toolchain_identity())
    return {"operation": operation, "raw": base64.b64encode(raw).decode(),
            "candidate": base64.b64encode(candidate).decode() if candidate else None, "policy": pol, "receipt": rec}


def mutant(module, old, new):
    source = Path(module.__file__).read_text()
    if source.count(old) != 1:
        raise AssertionError("mutation must have exactly one anchor: " + old)
    namespace = {"__name__": "mesh_test_mutant", "__file__": module.__file__}
    exec(compile(source.replace(old, new), module.__file__, "exec"), namespace)
    return namespace


class MeshTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.raw = Path(self.tmp.name) / "raw.glb"
        self.candidate = Path(self.tmp.name) / "candidate.glb"
        self.raw.write_bytes(glb())
        self.candidate.write_bytes(self.raw.read_bytes())

    def compare(self, pol=None):
        return api.compare_meshes(self.raw, self.candidate, policy=policy() if pol is None else pol)

    def test_identity_copy_preserves_sha256_and_metrics(self):
        before = self.raw.read_bytes()
        rc, rec = self.compare()
        self.assertEqual(rc, 0, rec)
        self.assertEqual(rec["input_sha256"], rec["candidate_sha256"])
        self.assertEqual(rec["artefact_sha256"], hashlib.sha256(before).hexdigest())
        for key in ("hausdorff_forward_um", "hausdorff_backward_um", "hausdorff_relative_ppm"):
            self.assertEqual(rec["metrics"][key], 0)
        self.assertEqual(rec["metrics"]["silhouette_iou_ppm"], 1000000)
        self.assertEqual(self.raw.read_bytes(), before)
        self.candidate.write_bytes(b"not a GLB")
        self.assertEqual(self.compare()[0], 2)

    def test_shortened_limb_rejected_in_both_directions(self):
        self.candidate.write_bytes(glb([{"name": "body", "verts": [*VERTS[:3], (0, 0, 400000)]}]))
        rc, rec = self.compare()
        self.assertEqual(rc, 5, rec)
        self.assertIn("hausdorff_relative", rec["failed"])
        self.assertGreater(rec["metrics"]["hausdorff_forward_um"], 0)
        self.assertEqual(api.compare_meshes(self.candidate, self.raw, policy=policy())[0], 5)

    def test_distance_spike_between_samples_cannot_claim_exact(self):
        rc, rec = self.compare(policy(sample_budget=1))
        self.assertEqual(rc, 0, rec)
        self.assertIs(rec["metrics"]["distance_is_exact"], False)
        self.assertIn("sampled_surface_only", rec["metrics"]["coverage_limits"])
        # A narrow spike is measured as an estimate, even when observed and refused.
        self.candidate.write_bytes(glb([{"name": "body", "verts": [*VERTS[:3], (0, 0, 2000000)]}]))
        rc, rec = self.compare(policy(sample_budget=1))
        self.assertEqual(rc, 5, rec)
        self.assertIs(rec["metrics"]["distance_is_exact"], False)

    def test_common_frame_detects_translation_and_scale(self):
        self.assertEqual(self.compare()[0], 0)
        for change in ({"translation": [0.5, 0, 0]}, {"scale": [0.4, 0.4, 0.4]}):
            with self.subTest(change=change):
                self.candidate.write_bytes(glb([dict(name="body", **change)]))
                self.assertEqual(self.compare()[0], 5)

    def test_empty_flat_or_zero_union_is_cannot_see(self):
        self.assertEqual(self.compare()[0], 0)
        for data in (glb([]), glb([{"name": "body", "verts": [(0, 0, 0)] * 4}]),
                     glb([{"name": "body", "scale": [0, 0, 0]}])):
            self.raw.write_bytes(data)
            self.assertEqual(self.compare()[0], 3)
        from blender_visual_qa.assets import glb_core, ortho4
        scene = glb_core.parse_glb(glb())
        with mock.patch.object(ortho4, "rasterise", return_value=[0] * 16):
            with self.assertRaises(contracts.MeshError) as caught:
                worker._silhouette(scene, scene, policy())
            self.assertEqual(caught.exception.rc, 3)

    def test_required_uv_missing_is_not_zero_stretch_pass(self):
        good = glb([{"name": "body", "uv": True}])
        self.raw.write_bytes(good)
        self.candidate.write_bytes(good)
        self.assertEqual(self.compare(policy(require_uv=True))[0], 0)
        for spec, code in (({"name": "body"}, "topology:uv_missing"),
                           ({"name": "body", "uv": True, "degenerate_uv": True}, "topology:uv_degenerate")):
            self.candidate.write_bytes(glb([spec]))
            rc, rec = self.compare(policy(require_uv=True))
            self.assertEqual(rc, 5, rec)
            self.assertIn(code, rec["failed"])

    def test_components_respect_seams_and_accessory_policy(self):
        rc, rec = api.component_metrics(self.raw, policy=policy())
        self.assertEqual(rc, 0, rec)
        self.assertEqual(rec["metrics"]["component_inventories"][0]["components"], 1)
        # Both primitives use identical local coordinates; nodes stay disjoint.
        self.raw.write_bytes(glb([{"name": "body"}, {"name": "accessory", "translation": [2, 0, 0]}]))
        self.assertEqual(api.component_metrics(self.raw, policy=policy(component_allowlist={"body": 1, "accessory": 1}))[0], 0)
        self.assertEqual(api.component_metrics(self.raw, policy=policy())[0], 5)
        # A floating island inside the same primitive must also be declared.
        verts = VERTS + [(x + 3000000, y, z) for x, y, z in VERTS]
        faces = FACES + [tuple(i + 4 for i in f) for f in FACES]
        self.raw.write_bytes(glb([{"name": "body", "verts": verts, "faces": faces}]))
        self.assertEqual(api.component_metrics(self.raw, policy=policy(component_allowlist={"body": 2}))[0], 0)
        self.assertEqual(api.component_metrics(self.raw, policy=policy())[0], 5)

    def test_repair_preserves_raw_and_refuses_overbudget_change(self):
        pol = policy(repair={"max_passes": 2, "max_hole_edges": 0, "cull_max_permille": 250})
        rc, rec, data = api.repair_mesh(self.raw, policy=pol)
        self.assertEqual(rc, 0, rec)
        self.assertEqual(data, self.raw.read_bytes())
        dirty = glb([{"name": "body", "faces": FACES + [FACES[0]]}])
        self.raw.write_bytes(dirty)
        rc, rec, data = api.repair_mesh(self.raw, policy=pol)
        self.assertEqual(rc, 0, rec)
        self.assertIsInstance(data, bytes)
        self.assertEqual(self.raw.read_bytes(), dirty)
        self.candidate.write_bytes(data)
        self.assertEqual(self.compare(pol)[0], 0)
        pol["repair"]["cull_max_permille"] = 0
        rc, rec, data = api.repair_mesh(self.raw, policy=pol)
        self.assertEqual(rc, 5, rec)
        self.assertIsNone(data)
        self.assertEqual(self.raw.read_bytes(), dirty)
        pol["allow_open"] = True
        pol["repair"]["max_hole_edges"] = 3
        self.assertEqual(api.repair_mesh(self.raw, policy=pol)[0], 2)

    def test_target_faces_cannot_override_geometry_failure(self):
        self.assertEqual(self.compare(policy(target_faces=4))[0], 0)
        self.candidate.write_bytes(glb([{"name": "body", "scale": [0.3, 0.3, 0.3]}]))
        self.assertEqual(self.compare(policy(target_faces=4))[0], 5)

    def test_receipt_missing_tampered_or_stale_never_passes(self):
        path = Path(self.tmp.name) / "receipt.json"
        self.assertEqual(api.read_receipt(path, self.raw, self.candidate, policy=policy())[0], 3)
        rc, rec = self.compare()
        self.assertEqual(rc, 0)
        path.write_text(contracts.canonical(rec))
        self.assertEqual(api.read_receipt(path, self.raw, self.candidate, policy=policy())[0], 0)
        modified = copy.deepcopy(rec)
        modified["metrics"]["silhouette_iou_ppm"] = 999999
        path.write_text(contracts.canonical(modified))
        self.assertEqual(api.read_receipt(path, self.raw, self.candidate, policy=policy())[0], 4)
        path.write_text(contracts.canonical(rec))
        self.assertEqual(api.read_receipt(path, self.raw, self.candidate, policy=policy(sample_budget=1))[0], 4)
        self.candidate.write_bytes(self.candidate.read_bytes() + b"changed")
        self.assertEqual(api.read_receipt(path, self.raw, self.candidate, policy=policy())[0], 4)
        for data in ('{}', '{"rc":0,"rc":0}', '{"rc":0.1}', 'null'):
            path.write_text(data)
            self.assertEqual(api.read_receipt(path, self.raw, self.candidate, policy=policy())[0], 2)

    def test_staged_refusal_and_timeout_preserve_caller_bytes(self):
        pol = policy(repair={"max_passes": 1, "max_hole_edges": 0, "cull_max_permille": 0})
        output = Path(self.tmp.name) / "existing.glb"
        report = Path(self.tmp.name) / "existing.json"
        output.write_bytes(b"caller output")
        report.write_bytes(b"caller receipt")
        before = {p: p.read_bytes() for p in (self.raw, output, report)}
        with mock.patch.object(api, "_spawn", side_effect=subprocess.TimeoutExpired("evaluator", 1)):
            rc, rec, data = api.repair_mesh(self.raw, policy=pol)
        self.assertEqual(rc, 6, rec)
        self.assertIsNone(data)
        with mock.patch.object(os, "replace", side_effect=OSError("must not publish")):
            with mock.patch.object(api, "_spawn", return_value={"receipt": {}, "artefact": None}):
                self.assertEqual(api.repair_mesh(self.raw, policy=pol)[0], 2)
        for p, expected in before.items():
            self.assertEqual(p.read_bytes(), expected)

    def test_gate_mutant_ignoring_reverse_distance_goes_red(self):
        self.assertTrue(worker._distance_failed(0, 500000, 1732050))
        broken = mutant(worker, "return max(forward, backward) * 1000000 > diag * DISTANCE_MAX_PPM",
                        "return forward * 1000000 > diag * DISTANCE_MAX_PPM")
        with self.assertRaises(AssertionError):
            self.assertTrue(broken["_distance_failed"](0, 500000, 1732050))

    def test_mutant_controls_for_uv_components_silhouette_and_exact_claim(self):
        verts = VERTS + [(x + 3000000, y, z) for x, y, z in VERTS]
        faces = FACES + [tuple(i + 4 for i in f) for f in FACES]
        cases = [
            ("require_uv=policy[\"require_uv\"]", "require_uv=False", glb(), glb(), policy(require_uv=True), 5),
            ('if policy["component_allowlist"].get(name) != item["components"]:', 'if False:',
             glb([{"name": "body", "verts": verts, "faces": faces}]), None, policy(), 5),
            ('if iou < IOU_MIN_PPM:', 'if False:', glb(), glb(), policy(), 5),
            ('"distance_is_exact": False', '"distance_is_exact": True', glb(), glb(), policy(), 0),
        ]
        for old, new, raw, candidate, pol, expected in cases:
            with self.subTest(rule=old):
                args = request("compare" if candidate else "components", raw, candidate, pol)
                baseline = worker.evaluate(copy.deepcopy(args))["receipt"]
                broken = mutant(worker, old, new)
                if "iou <" in old:
                    # Isolate this gate with a valid below-floor measurement.
                    with mock.patch.object(worker, "_silhouette", return_value=(900000, 900000, {"front": 900000})):
                        baseline = worker.evaluate(copy.deepcopy(args))["receipt"]
                    broken["_silhouette"] = lambda *a: (900000, 900000, {"front": 900000})
                self.assertEqual(baseline["rc"], expected, baseline)
                result = broken["evaluate"](copy.deepcopy(args))["receipt"]
                with self.assertRaises(AssertionError):
                    if "distance_is_exact" in old:
                        self.assertIs(result["metrics"]["distance_is_exact"], False)
                    else:
                        self.assertEqual(result["rc"], expected, result)

    def test_policy_and_integer_contract_fail_closed(self):
        self.assertEqual(self.compare()[0], 0)
        for key, value in (("timeout_s", True), ("sample_budget", 0), ("raster_resolution", 1),
                           ("unit", "centimetre"), ("transform", "register"), ("weld_tolerance_um", -1),
                           ("require_uv", 0), ("component_allowlist", {}), ("timeout_s", 1.0)):
            self.assertEqual(self.compare(policy(**{key: value}))[0], 2, key)
        missing = policy()
        del missing["allow_open"]
        self.assertEqual(self.compare(missing)[0], 2)
        self.assertEqual(self.compare(policy(toolchain_sha256="0" * 64))[0], 4)
        with self.assertRaises(contracts.MeshError):
            contracts.canonical({"n": 1.0})
        self.assertEqual(contracts.canonical({"z": True, "a": 1}), '{"a":1,"z":true}\n')

    def test_unavailable_changed_inputs_and_broken_child_never_pass(self):
        self.assertEqual(self.compare()[0], 0)
        with mock.patch.object(api, "SIBLING", Path(self.tmp.name) / "missing"):
            self.assertEqual(self.compare()[0], 3)
        original = api._spawn
        def changed(req, timeout):
            result = original(req, timeout)
            self.raw.write_bytes(b"changed during evaluation")
            return result
        with mock.patch.object(api, "_spawn", side_effect=changed):
            self.assertEqual(self.compare()[0], 4)
        self.raw.write_bytes(glb())
        with mock.patch.object(api, "_spawn", return_value={"receipt": {}, "artefact": None}):
            self.assertEqual(self.compare()[0], 2)

    def test_real_child_timeout_kills_owned_process_group(self):
        worker_path = Path(self.tmp.name) / "sleep_worker.py"
        pids_path = Path(self.tmp.name) / "pids.txt"
        worker_path.write_text("import os,time\nfrom pathlib import Path\npid=os.fork()\n"
                               "if pid:\n    Path(" + repr(str(pids_path)) + ").write_text(str(os.getpid())+' '+str(pid))\n"
                               "time.sleep(30)\n")
        with mock.patch.object(api, "WORKER", worker_path):
            rc, rec = self.compare(policy(timeout_s=1))
        self.assertEqual(rc, 6, rec)
        self.assertTrue(pids_path.is_file(), "fixture must actually start both processes")
        for pid in pids_path.read_text().split():
            stat = Path("/proc") / pid / "stat"
            if stat.exists():
                # A zombie has no executing process or resources; init owns its reap.
                self.assertEqual(stat.read_text().split(")", 1)[1].split()[0], "Z")

    def test_worker_protocol_never_returns_rc_one(self):
        for payload in (b"null", b"{}", b"invalid", b'{"receipt":{},"receipt":{}}'):
            result = subprocess.run([sys.executable, "-I", "-B", str(api.WORKER)], input=payload,
                                    capture_output=True, timeout=10, env={"PATH": os.defpath})
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertEqual(contracts.loads(result.stdout)["receipt"]["rc"], 2)
        payload = request("components", glb(), None, policy())
        result = subprocess.run([sys.executable, "-I", "-B", str(api.WORKER)],
                                input=contracts.canonical(payload).encode(), capture_output=True,
                                timeout=10, env={"PATH": os.defpath})
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_geometry_thresholds_include_equality(self):
        self.assertFalse(worker._distance_failed(20000, 0, 1000000))
        self.assertFalse(worker._distance_failed(0, 20000, 1000000))
        self.assertTrue(worker._distance_failed(20001, 0, 1000000))
        self.assertTrue(worker._distance_failed(0, 20001, 1000000))
        from blender_visual_qa.assets import glb_core
        scene = glb_core.parse_glb(glb())
        for value, expected in ((950000, False), (949999, True)):
            rec = contracts.receipt()
            with mock.patch.object(worker, "_silhouette", return_value=(value, value, {"front": value})):
                worker._compare(scene, scene, policy(), rec)
            self.assertEqual("silhouette_iou" in rec["failed"], expected)

    def test_boundaries_extensions_open_uv_and_unsupported_repair(self):
        self.assertEqual(self.compare()[0], 0)
        data = glb()
        size = struct.unpack_from("<I", data, 12)[0]
        doc = json.loads(data[20:20 + size])
        binary = data[28 + size:]
        for extra in ({"extensionsUsed": ["unknown"]}, {"animations": [{}]}, {"skins": [{}]}):
            self.candidate.write_bytes(pack(dict(doc, **extra), binary))
            self.assertEqual(self.compare()[0], 2)
        self.candidate.write_bytes(glb([{"name": "body", "faces": FACES[:-1]}]))
        self.assertEqual(self.compare()[0], 5)
        self.assertEqual(api.component_metrics(self.candidate, policy=policy(allow_open=True))[0], 0)
        self.raw.write_bytes(glb([{"name": "body", "uv": True, "faces": FACES[:-1]}]))
        pol = policy(require_uv=True, repair={"max_passes": 2, "max_hole_edges": 3, "cull_max_permille": 0})
        rc, rec, data = api.repair_mesh(self.raw, policy=pol)
        self.assertEqual(rc, 3, rec)
        self.assertIsNone(data)
        pol["allow_open"] = True
        pol["repair"]["max_hole_edges"] = 0
        rc, rec, data = api.repair_mesh(self.raw, policy=pol)
        self.assertEqual(rc, 0, rec)
        self.assertEqual(data, self.raw.read_bytes())

    def test_schema_and_success_cannot_be_empty(self):
        schema_path = ROOT / "tools/mesh/mesh-regression.schema.json"
        schema = json.loads(schema_path.read_text())
        self.assertEqual(schema["$id"], "urn:af:mesh-regression:v1")
        self.assertFalse(schema["additionalProperties"])
        rc, rec = self.compare()
        contracts.validate_receipt(rec)
        contracts.validate_success(rec, "compare")
        for key in contracts.METRICS:
            broken = copy.deepcopy(rec)
            broken["metrics"][key] = None
            broken["unavailable"][key] = "planted_missing"
            with self.assertRaises(contracts.MeshError):
                contracts.validate_success(broken, "compare")
        for rc in (1, True, 7):
            broken = copy.deepcopy(rec)
            broken["rc"] = rc
            with self.assertRaises(contracts.MeshError):
                contracts.validate_receipt(broken)

    def test_additional_rule_mutants_make_unittest_red(self):
        """Real unittest failures are captured, never treated as successful probes."""
        cases = []

        def should_refuse(function, argument, *args):
            try:
                function(argument, *args)
            except contracts.MeshError:
                return
            self.fail("negative control incorrectly passed")

        cases.append((contracts, 'if key in result:', 'if False:',
                      lambda ns: should_refuse(ns["loads"], '{"a":1,"a":2}')))
        cases.append((contracts, 'if type(policy[key]) is not int or not lo <= policy[key] <= hi:',
                      'if False:', lambda ns: should_refuse(ns["validate_policy"], policy(sample_budget=0))))
        cases.append((contracts, 'if policy["allow_open"] and repair["max_hole_edges"]:', 'if False:',
                      lambda ns: should_refuse(ns["validate_policy"], policy(allow_open=True,
                          repair={"max_passes": 1, "max_hole_edges": 3, "cull_max_permille": 0}))))
        cases.append((contracts, 'if key not in rec["unavailable"]:', 'if False:',
                      lambda ns: should_refuse(ns["validate_receipt"], dict(contracts.receipt(), unavailable={}))))
        cases.append((contracts, 'if not m["component_inventories"]:', 'if False:',
                      lambda ns: should_refuse(ns["validate_success"], dict(contracts.receipt(), rc=0,
                          metrics=dict(contracts.receipt()["metrics"], component_inventories=[])), "components")))
        for module, old, new, probe in cases:
            with self.subTest(rule=old):
                baseline = vars(module)
                probe(baseline)
                altered = mutant(module, old, new)
                # Mutated contracts define their own exception class.
                if module is contracts:
                    altered["MeshError"] = contracts.MeshError
                suite = unittest.TestSuite([unittest.FunctionTestCase(lambda: probe(altered))])
                output = io.StringIO()
                result = unittest.TextTestRunner(stream=output).run(suite)
                self.assertFalse(result.wasSuccessful(), output.getvalue())
                self.assertEqual(len(result.failures), 1, output.getvalue())
                self.assertEqual(result.errors, [], output.getvalue())
                self.assertIn("FAILED (failures=1)", output.getvalue())

    def test_safety_boundary_mutants_make_unittest_red(self):
        def red(probe):
            output = io.StringIO()
            result = unittest.TextTestRunner(stream=output).run(
                unittest.TestSuite([unittest.FunctionTestCase(probe)]))
            self.assertEqual(len(result.failures), 1, output.getvalue())
            self.assertEqual(result.errors, [], output.getvalue())
            self.assertFalse(result.wasSuccessful())

        # Integer threshold: equality is accepted, an excess is rejected.
        altered = mutant(worker, "return max(forward, backward) * 1000000 > diag * DISTANCE_MAX_PPM",
                         "return max(forward, backward) * 1000000 >= diag * DISTANCE_MAX_PPM")
        self.assertFalse(worker._distance_failed(20000, 0, 1000000))
        red(lambda: self.assertFalse(altered["_distance_failed"](20000, 0, 1000000)))

        # target_faces is metadata; plant the legacy bypass and isolate distance.
        damaged = glb([{"name": "body", "scale": [0.4, 0.4, 0.4]}])
        req = request("compare", glb(), damaged, policy(target_faces=4))
        altered = mutant(worker, "if _distance_failed(forward, backward, diag):",
                         'if _distance_failed(forward, backward, diag) and "target_faces" not in policy:')
        full = (1000000, 1000000, {v: 1000000 for v in ("front", "back", "right", "left")})
        with mock.patch.object(worker, "_silhouette", return_value=full):
            self.assertEqual(worker.evaluate(copy.deepcopy(req))["receipt"]["rc"], 5)
        altered["_silhouette"] = lambda *a: full
        red(lambda: self.assertEqual(altered["evaluate"](copy.deepcopy(req))["receipt"]["rc"], 5))

        # Culling beyond the declared budget must discard the derivative.
        dirty = glb([{"name": "body", "faces": FACES + [FACES[0]]}])
        pol = policy(repair={"max_passes": 2, "max_hole_edges": 0, "cull_max_permille": 0})
        req = request("repair", dirty, None, pol)
        altered = mutant(worker, 'if stat["culled_faces"] * 1000 > len(ix) // 3 * limits["cull_max_permille"]:',
                         "if False:")
        self.assertEqual(worker.evaluate(copy.deepcopy(req))["receipt"]["rc"], 5)
        red(lambda: self.assertEqual(altered["evaluate"](copy.deepcopy(req))["receipt"]["rc"], 5))

        # Empty masks are unknown, not a numeric zero followed by a gate FAIL.
        from blender_visual_qa.assets import glb_core, ortho4
        scene = glb_core.parse_glb(glb())
        altered = mutant(worker, 'raise MeshError(3, "silhouette_empty_union")',
                         'raise MeshError(5, "silhouette_empty_union")')
        def empty_probe(ns):
            try:
                ns["_silhouette"](scene, scene, policy())
            except contracts.MeshError as exc:
                self.assertEqual(exc.rc, 3)
            else:
                self.fail("empty silhouette passed")
        with mock.patch.object(ortho4, "rasterise", return_value=[0] * 16):
            empty_probe(vars(worker))
            red(lambda: empty_probe(altered))

        # A timed-out evaluator cannot become a successful API result.
        altered = mutant(api, 'rc, rec = finish(rec, 6, "deadline_exceeded")',
                         'rc, rec = finish(rec, 0, "deadline_exceeded")')
        with mock.patch.object(api, "_spawn", side_effect=subprocess.TimeoutExpired("worker", 1)):
            self.assertEqual(self.compare()[0], 6)
        altered["_spawn"] = mock.Mock(side_effect=subprocess.TimeoutExpired("worker", 1))
        red(lambda: self.assertEqual(altered["compare_meshes"](self.raw, self.candidate, policy=policy())[0], 6))

        # Raw-byte ownership: plant an actual destructive read in the isolated namespace.
        altered = mutant(api, 'data = stream.read(MAX_BYTES + 1)',
                         'data = stream.read(MAX_BYTES + 1)\n    Path(path).write_bytes(b"destroyed")')
        before = self.raw.read_bytes()
        api._read(self.raw)
        self.assertEqual(self.raw.read_bytes(), before)
        altered["_read"](self.raw)
        red(lambda: self.assertEqual(self.raw.read_bytes(), before))
        self.raw.write_bytes(before)  # The temporary fixture is restored; caller files are never used.

        # Stale receipt acceptance must remain closed even if its JSON looks valid.
        path = Path(self.tmp.name) / "receipt.json"
        rc, rec = self.compare()
        self.assertEqual(rc, 0)
        path.write_text(contracts.canonical(rec))
        pol = policy(sample_budget=1)
        altered = mutant(api, 'return finish(rec, 4, "receipt_stale")', 'return previous["rc"], previous')
        self.assertEqual(api.read_receipt(path, self.raw, self.candidate, policy=pol)[0], 4)
        red(lambda: self.assertEqual(altered["read_receipt"](path, self.raw, self.candidate, policy=pol)[0], 4))

        # Error rc values never return artifact bytes.
        req = request("repair", dirty, None, policy(repair={"max_passes": 2, "max_hole_edges": 0,
                                                          "cull_max_permille": 250}, component_allowlist={"body": 2}))
        baseline = worker.evaluate(copy.deepcopy(req))
        self.assertEqual(baseline["receipt"]["rc"], 5)
        self.assertIsNone(baseline["artefact"])
        altered = mutant(worker, 'if rc != 0:\n        artefact = None', 'if False:\n        artefact = None')
        red(lambda: self.assertIsNone(altered["evaluate"](copy.deepcopy(req))["artefact"]))


if __name__ == "__main__":
    unittest.main()
