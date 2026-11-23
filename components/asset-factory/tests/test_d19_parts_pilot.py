from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

from tools.blender.assembly import fixture, pilot
from tools.registry import Registry


class PartsPilotTests(unittest.TestCase):
    def test_fixture_has_expected_parts_and_exact_height(self):
        parts = fixture.parts()
        self.assertEqual(set(parts), {
            "torso", "head", "arm-left", "arm-right", "leg-left", "leg-right",
        })
        witness = fixture.check_geometry(parts, fixture.PLACEMENTS)
        self.assertEqual(witness["dimensions_um"][2], 1_800_000)
        self.assertEqual(witness["max_gap_um"], 0)
        self.assertEqual(witness["max_interpenetration_um"], 0)

    def test_gap_and_interpenetration(self):
        for x, reason in ((350_000, "socket_gap"), (200_000, "interpenetration")):
            with self.subTest(reason=reason):
                positions = dict(fixture.PLACEMENTS, **{"arm-left": (x, 0, 1_350_000)})
                with self.assertRaisesRegex(fixture.AssemblyError, reason):
                    fixture.check_geometry(fixture.parts(), positions)

    def test_omitted_hand(self):
        parts = fixture.parts()
        del parts["arm-left"]["hand"]
        with self.assertRaisesRegex(fixture.AssemblyError, "missing_hand"):
            fixture.check_geometry(parts, fixture.PLACEMENTS)

    def test_wrong_side(self):
        with self.assertRaisesRegex(fixture.AssemblyError, "wrong_side"):
            pilot.check_part_role("arm-left", {"side": "right", "anatomical_role": "arm"})


class RegistryBackedPilotTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        blender = shutil.which("blender")
        retained = os.environ.get("AF_PARTS_EVIDENCE")
        template_path = os.environ.get("AF_PARTS_VIEW_TEMPLATE")
        if blender is None:
            if retained or template_path:
                raise AssertionError("Blender executable AUSENTE")
            raise unittest.SkipTest("Blender executable AUSENTE")
        cls.blender = Path(blender)
        if retained:
            cls.root = Path(retained)
            cls.root.mkdir()
        else:
            tmp = tempfile.TemporaryDirectory()
            cls.addClassCleanup(tmp.cleanup)
            cls.root = Path(tmp.name)
        if template_path:
            cls.template = Path(template_path)
        elif retained:
            raise AssertionError("Retained evidence requires AF_PARTS_VIEW_TEMPLATE")
        else:
            cls.template = cls.root / "template.md"
            template = b"Portable test fixture for template identity.\n"
            cls.template.write_bytes(template)
            cls.enterClassContext(patch.object(pilot, "TEMPLATE_DIGEST", pilot.pin(template)))
        store = cls.root / "registry"
        store.mkdir()
        cls.registry = Registry(store, namespace="parts-pilot")
        initial = cls.registry.initialize()
        cls.raw, cls.approved, author_process = pilot.author_parts(
            cls.registry, root=cls.root / "author", blender=cls.blender,
            view_template=cls.template, expected_snapshot=initial,
        )
        (cls.root / "author-process.json").write_bytes(pilot.encode(author_process))

    def prepare(self, recipe, *, registry=None):
        raw = pilot.encode(recipe)
        return pilot.prepare(
            self.registry if registry is None else registry, raw,
            expected_recipe_digest=pilot.pin(raw), approved_parts=self.approved,
            blender=self.blender, view_template=self.template,
        )

    def test_assemble_render_register_and_resolve_every_part(self):
        root = self.root / "run"
        recipe = json.loads(self.raw)
        result = pilot.assemble(
            self.registry, self.raw, expected_recipe_digest=pilot.pin(self.raw),
            approved_parts=self.approved, expected_snapshot=self.registry.current_snapshot(),
            root=root, blender=self.blender, view_template=self.template,
        )
        self.assertEqual(result["record"]["dimensions"]["xyz_um"], list(fixture.DIMENSIONS_UM))
        self.assertEqual(result["record"]["lineage"]["parent_records"], sorted(self.approved.values()))
        self.assertEqual(result["record"]["gates"], [])
        for part in recipe["parts"]:
            parent = result["lineage_records"][part["part_record_sha256"]]
            self.assertEqual(parent["geometry_digest"], part["artifact_sha256"])
            original = self.registry.read_blob(result["locator"], part["artifact_sha256"],
                                               snapshot=result["snapshot"])
            self.assertEqual(original, (self.root / "author" / (part["instance_id"] + ".glb")).read_bytes())
        for file in result["record"]["files"]:
            stored = self.registry.read_blob(result["locator"], file["digest"], snapshot=result["snapshot"])
            self.assertEqual(stored, (root / file["logical_path"]).read_bytes())
        views = json.loads((root / "views.json").read_bytes())["views"]
        self.assertEqual([row["view_id"] for row in views], [name for name, _ in fixture.VIEWS])
        self.assertEqual(len({row["camera_digest"] for row in views}), 6)
        self.assertEqual(len({row["preview_digest"] for row in views}), 6)
        for view in views:
            path = root / ("humanoid_" + view["view_id"] + ".png")
            self.assertEqual(view["preview_digest"], "sha256:" + sha256(path.read_bytes()).hexdigest())
            with Image.open(path) as image:
                self.assertEqual(image.getpixel((0, 0)), (128, 128, 128))
        receipt = json.loads((root / "receipt.json").read_bytes())
        self.assertEqual(receipt["execution"]["render_device"], "CPU")
        self.assertEqual(receipt["execution"]["reconstructed_parts"].keys(), self.approved.keys())
        self.assertEqual(receipt["execution"]["reconstructed_master"]["triangles"],
                         sum(part["triangles"] for part in receipt["execution"]["reconstructed_parts"].values()))

    def test_wrong_side_even_when_both_records_resolve(self):
        recipe = json.loads(self.raw)
        left, right = recipe["parts"][:2]
        for key in ("locator", "part_record_sha256", "artifact_sha256", "socket_set_sha256"):
            left[key] = right[key]
        with self.assertRaisesRegex(fixture.AssemblyError, "wrong_side"):
            self.prepare(recipe)

    def test_incompatible_socket_profile(self):
        recipe = json.loads(self.raw)
        recipe["matings"][0]["profile_sha256"] = recipe["matings"][1]["profile_sha256"]
        with self.assertRaisesRegex(fixture.AssemblyError, "incompatible_socket_profile"):
            self.prepare(recipe)

    def test_other_valid_same_side_record_digest_refuses(self):
        recipe = json.loads(self.raw)
        part = recipe["parts"][0]
        record = self.registry.resolve(part["locator"], snapshot=recipe["snapshot"])["record"]
        record["annotations"]["revision"] = "other"
        alternate = pilot.encode(record)
        with tempfile.TemporaryDirectory() as directory:
            store = Path(directory) / "registry"
            shutil.copytree(self.registry.root, store)
            clone = Registry(store, namespace=self.registry.namespace)
            recipe["snapshot"] = clone.put(
                "arm-left", alternate, blobs={}, expected_snapshot=clone.current_snapshot(),
            )
            part["part_record_sha256"] = pilot.pin(alternate)
            part["locator"] = part["locator"].split("@")[0] + "@" + part["part_record_sha256"]
            with self.assertRaisesRegex(fixture.AssemblyError, "part_digest_mismatch"):
                self.prepare(recipe, registry=clone)

    def test_wrong_dimensions_and_gap_refuse_at_preflight(self):
        for expected in ("physical_scale", "socket_gap"):
            with self.subTest(expected=expected):
                recipe = json.loads(self.raw)
                if expected == "physical_scale":
                    recipe["target_dimensions_um"][2] += 1
                else:
                    recipe["parts"][0]["transform"]["translation_um"][0] += 1
                with self.assertRaisesRegex(fixture.AssemblyError, expected):
                    self.prepare(recipe)

    def test_incomplete_mating_tree_cannot_reduce_required_population(self):
        recipe = json.loads(self.raw)
        recipe["matings"][0] = recipe["matings"][1].copy()
        with self.assertRaisesRegex(fixture.AssemblyError, "incomplete_or_unordered_mating_tree"):
            self.prepare(recipe)

    def test_invalid_canonical_bytes_and_stale_recipe_pin(self):
        for data, digest, reason in (
            (self.raw.rstrip(), pilot.pin(self.raw.rstrip()), "recipe_schema_or_canonical_bytes"),
            (self.raw, "sha256:" + "0" * 64, "recipe_digest_mismatch"),
        ):
            with self.subTest(reason=reason):
                with self.assertRaisesRegex(fixture.AssemblyError, reason):
                    pilot.prepare(self.registry, data, expected_recipe_digest=digest,
                                  approved_parts=self.approved, blender=self.blender,
                                  view_template=self.template)

    def test_glb_without_hand_fails_real_blender_reconstruction(self):
        root = self.root / "without-hand"
        root.mkdir()
        shapes = fixture.parts()
        del shapes["arm-left"]["hand"]
        vertices, faces = fixture.boundary(shapes["arm-left"])
        negative = {"arm-left": {
            "vertices": [list(vertex) for vertex in vertices],
            "faces": [list(face) for face in faces],
            "hand": fixture.parts()["arm-left"]["hand"],
        }}
        pilot._worker(self.blender.resolve(), root, {
            "operation": "author", "units": pilot.UM_PER_METER, "parts": negative,
        })
        for name in set(self.approved) - {"arm-left"}:
            shutil.copyfile(self.root / "author" / (name + ".glb"), root / (name + ".glb"))
        with self.assertRaisesRegex(fixture.AssemblyError, "fixture_reconstruction_mismatch"):
            pilot._worker(self.blender.resolve(), root, {
                "operation": "assemble", "units": pilot.UM_PER_METER,
                "parts": pilot._mesh_request(fixture.parts(), fixture.PLACEMENTS),
                "height_um": fixture.DIMENSIONS_UM[2], "render": pilot.RENDER,
                "max_import_residual_nm": pilot.POLICY["max_import_residual_nm"],
            })
        self.assertFalse((root / "scene.glb").exists())
