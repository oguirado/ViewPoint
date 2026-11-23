"""Reproduce PARTS review findings and exercise registered non-fixture joints."""
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

import test_d19_parts_pilot
from tools.blender.assembly import fixture, geometry, pilot
from tools.registry import Registry


class PartsRepairTests(unittest.TestCase):
    def test_mirrored_witness_is_classified_before_missing_hand(self):
        shapes = fixture.parts()
        shapes["arm-right"] = shapes["arm-left"]
        with self.assertRaisesRegex(fixture.AssemblyError, "^mirrored_part$"):
            fixture.check_geometry(shapes, fixture.PLACEMENTS)


    def test_overlapping_non_mates_do_not_receive_a_separation_certificate(self):
        parts = {"a": {"world": [(0, 0, 0), (10, 10, 10)]},
                 "b": {"world": [(5, 5, 5), (15, 15, 15)]}}
        with self.assertRaisesRegex(fixture.AssemblyError, "^non_mating_separation_unproven$") as caught:
            geometry.check_separation(parts, set())
        self.assertEqual(caught.exception.rc, 3)


class PartsRegisteredRepairTests(unittest.TestCase):
    setUpClass = classmethod(test_d19_parts_pilot.RegistryBackedPilotTests.setUpClass.__func__)

    def prep(self, recipe, *, registry=None, approved=None):
        raw = pilot.encode(recipe)
        return pilot.prepare(
            registry or self.registry, raw, expected_recipe_digest=pilot.pin(raw),
            approved_parts=self.approved if approved is None else approved,
            blender=self.blender, view_template=self.template,
        )

    def test_wrong_namespace_is_a_typed_assembly_refusal(self):
        recipe = json.loads(self.raw)
        head = recipe["parts"][2]
        head["locator"] = head["locator"].replace("parts-pilot", "other-ns")
        with self.assertRaisesRegex(fixture.AssemblyError, "^part_unresolvable$") as caught:
            self.prep(recipe)
        self.assertEqual(caught.exception.rc, 3)

    def test_wrong_glb_refuses_in_preflight(self):
        recipe = json.loads(self.raw)
        left, right = recipe["parts"][:2]
        record = self.registry.resolve(right["locator"], snapshot=recipe["snapshot"])["record"]
        mesh = (self.root / "author" / "arm-left.glb").read_bytes()
        digest = left["artifact_sha256"]
        record["dependencies"].append(digest)
        record["dependencies"].sort()
        record["geometry_digest"] = record["representations"]["visual"] = digest
        record["files"][0].update(digest=digest, size_bytes=len(mesh))
        encoded = pilot.encode(record)
        with tempfile.TemporaryDirectory() as directory:
            store = Path(directory) / "registry"
            shutil.copytree(self.registry.root, store)
            registry = Registry(store, namespace=self.registry.namespace)
            recipe["snapshot"] = registry.put(
                "arm-right", encoded, blobs={}, expected_snapshot=registry.current_snapshot(),
            )
            right.update(artifact_sha256=digest, part_record_sha256=pilot.pin(encoded),
                         locator=f"af://parts-pilot/arm-right@{pilot.pin(encoded)}")
            approved = dict(self.approved, **{"arm-right": pilot.pin(encoded)})
            with self.assertRaisesRegex(fixture.AssemblyError, "^fixture_reconstruction_mismatch$"):
                self.prep(recipe, registry=registry, approved=approved)

    def test_body_view_requires_camera_observations(self):
        root = self.root / "without-camera-observations"
        root.mkdir()
        for name, _ in fixture.VIEWS:
            image = Image.new("RGBA", (1024, 1024))
            image.paste((40, 60, 80, 255), (400, 400, 600, 600))
            image.save(root / f"rgba-{name}.png")
        rows = json.loads(self.raw)["parts"]
        with self.assertRaisesRegex(fixture.AssemblyError, "^camera_evidence_unavailable$"):
            pilot._composite_views(root, rows)
        with self.assertRaisesRegex(fixture.AssemblyError, "^assembled_height_unavailable$"):
            pilot._composite_views(root, rows, {}, {})

    def test_fixture_front_face_has_flat_luminance(self):
        root = self.root / "flat-face"
        pilot.assemble(
            self.registry, self.raw, expected_recipe_digest=pilot.pin(self.raw),
            approved_parts=self.approved, expected_snapshot=self.registry.current_snapshot(),
            root=root, blender=self.blender, view_template=self.template,
        )
        with Image.open(root / "humanoid_front.png") as image:
            lo, hi = image.crop((490, 404, 534, 448)).convert("L").getextrema()
        self.assertLessEqual(hi - lo, 4, (lo, hi))
        evidence = json.loads((root / "receipt.json").read_bytes())
        lighting = evidence["execution"]["flat_lighting"]
        self.assertLessEqual(lighting["luma_spread"], pilot.POLICY["max_flat_face_luma_spread"])
        observed = evidence["execution"]["cameras"]["front"]
        rig = pilot._render_rig(evidence["geometry"])
        rgba = root / "rgba-front.png"
        with self.assertRaisesRegex(fixture.AssemblyError, "^camera_raster_digest_mismatch$"):
            pilot._check_camera(dict(observed, rgba_sha256=pilot.pin(b"replayed")), (0, -1), rig, rgba)
        with self.assertRaisesRegex(fixture.AssemblyError, "^camera_rig_mismatch$"):
            pilot._check_camera(dict(observed, shift_xy=["0x1p-4", "0x0p+0"]), (0, -1), rig, rgba)

    def test_non_fixture_profile_can_be_selected(self):
        recipe = json.loads(self.raw)
        recipe["geometry_profile"] = "planar-surface-seams"
        with self.assertRaisesRegex(fixture.AssemblyError, "^incompatible_socket_profile$"):
            self.prep(recipe)


    def test_real_blender_rejects_one_changed_ortho_scale(self):
        mutated = self.root / "camera-worker"
        mutated.mkdir()
        shutil.copy(pilot.HERE / "camera.py", mutated)
        source = (pilot.HERE / "worker.py").read_text()
        anchor = '        camera.ortho_scale = render["span_um"] / units'
        self.assertEqual(source.count(anchor), 1)
        (mutated / "worker.py").write_text(source.replace(anchor, anchor + (
            '\n        if name == "left":\n            camera.ortho_scale *= 0.5'
        )))
        run = pilot._worker

        def use_mutant(blender, root, request):
            with patch.object(pilot, "HERE", mutated):
                return run(blender, root, request)

        root = self.root / "changed-ortho-scale"
        snapshot = self.registry.current_snapshot()
        with patch.object(pilot, "_worker", use_mutant):
            with self.assertRaisesRegex(fixture.AssemblyError, "^camera_rig_mismatch$"):
                pilot.assemble(self.registry, self.raw, expected_recipe_digest=pilot.pin(self.raw),
                               approved_parts=self.approved, expected_snapshot=snapshot, root=root,
                               blender=self.blender, view_template=self.template)
        observations = json.loads((root / "worker-result.json").read_bytes())["cameras"]
        self.assertEqual(float.fromhex(observations["left"]["ortho_scale"]),
                         float.fromhex(observations["front"]["ortho_scale"]) / 2)
        self.assertEqual(self.registry.current_snapshot(), snapshot)
        self.assertFalse((root / "registration.json").exists())

    def test_flat_lighting_gate_rejects_measured_spread(self):
        root = self.root / "grain"
        root.mkdir()
        size = pilot.RENDER["size"]
        image = Image.new("RGBA", (size, size), (90, 90, 90, 255))
        center = size // 2
        image.paste((120, 120, 120, 255), (center, center - 16, center + 16, center + 16))
        path = root / "lighting-flat.png"
        image.save(path)
        with self.assertRaisesRegex(fixture.AssemblyError, "^flat_lighting_spread$"):
            pilot._flat_lighting(root, pilot.RENDER, pilot.pin(path.read_bytes()))


class NonFixturePartsTests(unittest.TestCase):
    def setUp(self):
        blender = shutil.which("blender")
        if blender is None:
            self.skipTest("Blender executable AUSENTE")
        self.blender = Path(blender).resolve()
        retained = os.environ.get("AF_PARTS_GENERAL_EVIDENCE")
        if retained:
            self.root = Path(retained)
            self.root.mkdir()
        else:
            self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def _register(self, registry, name, mesh, boxes, sockets, profiles, *, front_axis="-Y"):
        taxonomy = pilot.encode(pilot.TAXONOMY)
        proof = pilot.encode({"kind": "measured-extents", "geometry": pilot.pin(mesh), "part": name})
        dims = fixture.dimensions(boxes.values())
        record = pilot._record(name, "part", mesh, proof, dims, taxonomy)
        record["classification"]["method"] = "test-dimensioned-mesh"
        if front_axis is not None:
            record["labels"]["front_axis"] = front_axis
        record["sockets"] = sockets
        record["compatibility_profile_digests"] = sorted(set(map(pilot.pin, profiles)))
        blobs = {pilot.pin(data): data for data in (mesh, taxonomy, proof, *profiles)}
        record["dependencies"] = sorted(blobs)
        raw = pilot.encode(record)
        snapshot = registry.put(name, raw, blobs=blobs, expected_snapshot=registry.current_snapshot())
        return snapshot, pilot.pin(raw)

    def test_non_fixture_registered_mating_and_refusals(self):
        template_path = os.environ.get("AF_PARTS_VIEW_TEMPLATE")
        if template_path:
            template = Path(template_path)
        else:
            template = self.root / "template.md"
            template.write_text("Test view identity.\n")
            self.enterContext(patch.object(pilot, "TEMPLATE_DIGEST", pilot.pin(template.read_bytes())))
        store = self.root / "registry"
        store.mkdir()
        registry = Registry(store, namespace="parts-general")
        registry.initialize()
        shapes = {name: {role: [[n * 4 // 5 for n in point] for point in box]
                         for role, box in boxes.items()} for name, boxes in fixture.parts().items()}
        positions = {name: tuple(n * 4 // 5 for n in point) for name, point in fixture.PLACEMENTS.items()}
        shapes["head"]["crest"] = [[-20_000, -40_000, 216_000], [30_000, 40_000, 236_123]]
        # Author the head in a local frame turned a quarter turn clockwise.
        # The recipe turn puts its nose and declared local -X axis along world -Y.
        shapes["head"] = {role: [[lo[1], -hi[0], lo[2]], [hi[1], -lo[0], hi[2]]]
                          for role, (lo, hi) in shapes["head"].items()}
        narrow = dict(shapes["head"], neck=[[-56_000, -51_000, 0], [56_000, 51_000, 44_000]])
        request = pilot._mesh_request(shapes, positions)
        narrow_v, narrow_f = fixture.boundary(narrow)
        request["head-narrow"] = {
            "vertices": list(map(list, narrow_v)), "faces": list(map(list, narrow_f)),
        }
        authored = self.root / "authored"
        authored.mkdir()
        pilot._worker(self.blender, authored, {
            "operation": "author", "units": pilot.UM_PER_METER, "parts": request,
        })
        rows, approved, matings = [], {}, []
        sockets = {"torso": []}
        profiles = {}
        for name, (_, _, sizes) in fixture.MATES.items():
            a, b = pilot._socket(name, False), pilot._socket(name, True)
            # Define a keyed patch in each socket's XY frame.
            dims = [n * 4 // 5 for n in (sizes[::-1] if name.startswith("arm-") else sizes)] + [1]
            profile = pilot.encode({"type": "surface_seam", "orientation": "keyed-opposed-x",
                                    "dimensions_um": dims})
            profiles[name] = profile
            digest = pilot.pin(profile)
            for sock in (a, b):
                sock.update(profile_digest=digest, mating_dimensions_um=dims)
                sock["frame"]["translation_um"] = [n * 4 // 5 for n in sock["frame"]["translation_um"]]
            x, y, z, w = a["frame"]["rotation_xyzw_ppm"]
            q = [w, z, -y, -x]
            if q[3] < 0 or (q[3] == 0 and next(n for n in q if n) < 0):
                q = [-n for n in q]
            # The head turns a quarter turn in the recipe. Its own collar frame
            # carries the inverse turn so the same world socket constraint holds.
            b["frame"]["rotation_xyzw_ppm"] = [707_107, -707_107, 0, 0] if name == "head" else q
            sockets["torso"].append(a)
            sockets[name] = [b]
            matings.append({"parent_instance_id": "torso", "parent_socket": name,
                            "child_instance_id": name, "child_socket": "root",
                            "profile_sha256": digest, "seam_mode": "none"})

        for name, boxes in shapes.items():
            mesh = (authored / f"{name}.glb").read_bytes()
            applicable = list(profiles.values()) if name == "torso" else [profiles[name]]
            snapshot, digest = self._register(registry, name, mesh, boxes, sockets[name], applicable,
                                              front_axis="-X" if name == "head" else "-Y")
            placement = fixture.transform(positions[name])
            if name == "head":
                placement["rotation_xyzw_ppm"] = [0, 0, 707_107, 707_107]
            rows.append({"instance_id": name, "locator": f"af://parts-general/{name}@{digest}",
                         "part_record_sha256": digest, "artifact_sha256": pilot.pin(mesh),
                         "socket_set_sha256": pilot.pin(pilot.encode(sockets[name])), "transform": placement})
            approved[name] = digest
        recipe = {"schema": "af.assembly-recipe/v1", "category": "character", "snapshot": snapshot,
                  "geometry_profile": "planar-surface-seams", "root_instance_id": "torso",
                  "target_dimensions_um": [1_152_000, 248_000, 1_460_123],
                  "toolchain_sha256": pilot.pin(pilot._toolchain(self.blender)),
                  "view_template_sha256": pilot.TEMPLATE_DIGEST,
                  "taxonomy_sha256": pilot.pin(pilot.encode(pilot.TAXONOMY)),
                  "render_profile_sha256": pilot.pin(pilot.encode(pilot.RENDER)),
                  "acceptance_policy_sha256": pilot.pin(pilot.encode(pilot.POLICY)),
                  "parts": rows, "matings": matings}

        def prepare(candidate, approval=approved):
            raw = pilot.encode(candidate)
            return pilot.prepare(registry, raw, expected_recipe_digest=pilot.pin(raw),
                                 approved_parts=approval, blender=self.blender, view_template=template)

        _, result, _, parts, _ = prepare(recipe)
        self.assertEqual(result["dimensions_um"], tuple(recipe["target_dimensions_um"]))
        head_y = [point[1] for point in parts["head"]["world_vertices"]]
        self.assertEqual((min(head_y), max(head_y)), (-108_000, 84_000))
        self.assertEqual(result["part_facing"]["head"]["local_front_axis"], "-X")
        self.assertEqual(result["part_facing"]["head"]["world_front_cosine_num_den"], [1, 1])
        head_seam = result["seams"]["head"]
        self.assertEqual(head_seam["projected_overlap_lower_um2"], 112_000 ** 2)
        self.assertEqual(head_seam["frame_anchor_error_um"], 0)
        raw = pilot.encode(recipe)
        out = self.root / "general-run"
        entry = pilot.assemble(registry, raw, expected_recipe_digest=pilot.pin(raw),
                               approved_parts=approved, expected_snapshot=snapshot, root=out,
                               blender=self.blender, view_template=template)
        self.assertEqual(set(entry["lineage_records"]), set(approved.values()))
        self.assertEqual(entry["record"]["dimensions"]["xyz_um"], recipe["target_dimensions_um"])
        manifest = json.loads((out / "views.json").read_bytes())
        self.assertEqual(len(manifest["views"]), len(fixture.VIEWS))
        view_metadata = {"height_cm": 146, "height_um": 1_460_123, "symmetric": None}
        for field, expected in view_metadata.items():
            with self.subTest(field=field):
                self.assertEqual(manifest.get(field, "missing"), expected)
        with self.subTest(field="symmetry_absence_reason"):
            self.assertIn("AUSENTE", manifest.get("symmetry_absence_reason", ""))

        for reason in ("socket_gap", "socket_frame_orientation", "physical_scale",
                       "incompatible_socket_profile", "socket_already_occupied"):
            with self.subTest(reason=reason):
                variant = json.loads(raw)
                head = variant["parts"][2]
                if reason == "socket_gap":
                    head["transform"]["translation_um"][2] += 1
                elif reason == "socket_frame_orientation":
                    head["transform"]["rotation_xyzw_ppm"] = fixture.IDENTITY
                elif reason == "physical_scale":
                    variant["target_dimensions_um"][2] -= 1
                elif reason == "incompatible_socket_profile":
                    variant["matings"][2]["profile_sha256"] = variant["matings"][0]["profile_sha256"]
                else:
                    variant["matings"][-1]["parent_socket"] = variant["matings"][-2]["parent_socket"]
                with self.assertRaisesRegex(fixture.AssemblyError, "^" + reason + "$"):
                    prepare(variant)

        variant = json.loads(raw)
        mesh = (authored / "head-narrow.glb").read_bytes()
        snapshot, digest = self._register(registry, "head", mesh, narrow, sockets["head"], [profiles["head"]],
                                          front_axis="-X")
        variant["snapshot"] = snapshot
        variant["parts"][2].update(locator=f"af://parts-general/head@{digest}",
                                   part_record_sha256=digest, artifact_sha256=pilot.pin(mesh))
        with self.assertRaisesRegex(fixture.AssemblyError, "^socket_patch_overlap$"):
            prepare(variant, dict(approved, head=digest))

        def replace_part(name, front_axis, socket_rotation=None, rotation=None, depth_um=None):
            candidate = json.loads(raw)
            part = next(row for row in candidate["parts"] if row["instance_id"] == name)
            replacement_sockets = json.loads(pilot.encode(sockets[name]))
            if socket_rotation is not None:
                replacement_sockets[0]["frame"]["rotation_xyzw_ppm"] = socket_rotation
                part["transform"]["rotation_xyzw_ppm"] = rotation
                candidate["target_dimensions_um"][1] = depth_um
            mesh = (authored / f"{name}.glb").read_bytes()
            current, digest = self._register(registry, name, mesh, shapes[name], replacement_sockets,
                                             [profiles[name]], front_axis=front_axis)
            candidate["snapshot"] = current
            part.update(locator=f"af://parts-general/{name}@{digest}", part_record_sha256=digest,
                        socket_set_sha256=pilot.pin(pilot.encode(replacement_sockets)))
            return candidate, dict(approved, **{name: digest})

        for name, axis, socket_rotation, rotation, depth in (
            ("head", "-X", [707_107, 707_107, 0, 0], [0, 0, -707_107, 707_107], 253_600),
            ("leg-left", "-Y", [0, 0, 1_000_000, 0], [0, 0, 1_000_000, 0], 291_200),
        ):
            with self.subTest(backwards=name):
                candidate, approval = replace_part(name, axis, socket_rotation, rotation, depth)
                with self.assertRaisesRegex(fixture.AssemblyError, "^wrong_part_facing$") as caught:
                    prepare(candidate, approval)
                self.assertEqual(caught.exception.rc, 5)

        for axis, reason, code in ((None, "missing_part_front_axis", 3),
                                   ("front", "invalid_part_front_axis", 2),
                                   ("+Z", "wrong_part_facing", 5)):
            with self.subTest(front_axis=axis):
                candidate, approval = replace_part("head", axis)
                with self.assertRaisesRegex(fixture.AssemblyError, "^" + reason + "$") as caught:
                    prepare(candidate, approval)
                self.assertEqual(caught.exception.rc, code)
