"""Projection tests use analytic fixtures; no model outputs or private paths."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from asset_factory import core
from tools.blender import texture_project as T
from tools.blender.bake_support import Refusal, load_record, read_bytes, sha, upstream

G, P = upstream("glb_core"), upstream("orm_pack")


def fixture(root):
    # Analytic cube, glTF Y-up, outward winding. Test geometry only.
    positions = [(-500000, -500000, -500000), (500000, -500000, -500000),
                 (500000, 500000, -500000), (-500000, 500000, -500000),
                 (-500000, -500000, 500000), (500000, -500000, 500000),
                 (500000, 500000, 500000), (-500000, 500000, 500000)]
    indices = [0, 2, 1, 0, 3, 2, 4, 5, 6, 4, 6, 7, 0, 1, 5, 0, 5, 4,
               3, 7, 6, 3, 6, 2, 0, 4, 7, 0, 7, 3, 1, 2, 6, 1, 6, 5]
    mesh = root / "mesh.glb"
    mesh.write_bytes(G.write_glb(G.Scene([G.Prim("cube", "/cube", G.IDENTITY_Q16,
                                               positions, indices)])))
    image = root / "reference.png"
    rgba = bytes(c for y in range(64) for x in range(64)
                 for c in (32 + x * 3, 32 + y * 3, 224 - x * 3, 255))
    image.write_bytes(P.encode_png(64, 64, rgba))
    rights = {"schema": "af.texture-project-rights/v1", "jurisdiction": "ES", "commercial_use": False,
              "inputs": [{"role": role, "sha256": sha(path.read_bytes()), "licence": "CC0-1.0",
                          "provider_id": "analytic-fixture", "commercial": True,
                          "territory_excluded": [], "provenance": "original synthetic unit fixture"}
                         for role, path in (("mesh", mesh), ("reference", image))]}
    rp = root / "rights.json"
    core.atomic_write_json(rp, rights)
    return mesh, image, rp


class HostAdmission(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="texture-host-")
        self.root = Path(self.temp.name)
        self.mesh, self.image, self.rights = fixture(self.root)
        self.dest = self.root / "result"
        self.options = dict(rights=self.rights, front="-Y", resolution=128, margin_px=4,
                            timeout_s=120, max_front_mae_ppm=50000)
        self.inputs = (self.mesh.read_bytes(), self.image.read_bytes(), self.rights.read_bytes())

    def tearDown(self):
        self.assertEqual(self.inputs[:2], (self.mesh.read_bytes(), self.image.read_bytes()))
        self.temp.cleanup()

    def change_rights(self, fn):
        r = load_record(self.rights.read_bytes())
        fn(r)
        core.atomic_write_json(self.rights, r)

    def refuse_before_worker(self, expected):
        with patch.object(T, "scoped_run", side_effect=AssertionError("must refuse before Blender")):
            result = T.project(self.mesh, self.image, self.dest, **self.options)
        self.assertEqual(expected, result["rc"], result)
        self.assertEqual("CANNOT_SEE", result["asset_verdict"])
        self.assertFalse(self.dest.exists())
        return result

    def test_changed_reference_rights_are_stale(self):
        self.change_rights(lambda r: r["inputs"][1].update(sha256="0" * 64))
        self.assertIn("rights_subject_drift", self.refuse_before_worker(4)["reason_codes"])

    def test_eu_blocked_provider_is_refused(self):
        self.change_rights(lambda r: r["inputs"][0].update(provider_id="hunyuan3d21"))
        self.refuse_before_worker(5)

    def test_eu_exclusion_is_expanded(self):
        self.change_rights(lambda r: r["inputs"][0].update(territory_excluded=["EU"]))
        self.refuse_before_worker(5)

    def test_commercial_noncommercial_refused(self):
        self.change_rights(lambda r: (r.update(commercial_use=True), r["inputs"][0].update(commercial=False)))
        self.refuse_before_worker(5)

    def test_missing_commercial_use_has_no_default(self):
        self.change_rights(lambda r: r.pop("commercial_use"))
        self.refuse_before_worker(2)

    def test_duplicate_roles_do_not_count_as_coverage(self):
        self.change_rights(lambda r: r["inputs"][1].update(role="mesh"))
        self.refuse_before_worker(2)

    def test_unrecognised_licence_is_not_safe(self):
        self.change_rights(lambda r: r["inputs"][0].update(licence="unknown"))
        self.refuse_before_worker(2)

    def test_unknown_territory_is_not_empty_exclusion(self):
        self.change_rights(lambda r: r["inputs"][0].update(territory_excluded=["unknown"]))
        self.refuse_before_worker(2)

    def test_existing_output_preserved(self):
        self.dest.mkdir()
        sentinel = self.dest / "receipt.json"
        sentinel.write_bytes(b"caller-owned\x00bytes")
        result = T.project(self.mesh, self.image, self.dest, **self.options)
        self.assertEqual(2, result["rc"])
        self.assertEqual(b"caller-owned\x00bytes", sentinel.read_bytes())

    def test_output_ancestor_symlink_preserved(self):
        (self.root / "link").symlink_to(self.root, target_is_directory=True)
        result = T.project(self.mesh, self.image, self.root / "link" / "out", **self.options)
        self.assertEqual(2, result["rc"])
        self.assertFalse((self.root / "out").exists())

    def test_timeout_publishes_nothing(self):
        def timeout(argv, seconds, log):
            (Path(log).parent / "base_color.png").write_bytes(b"partial")
            raise subprocess.TimeoutExpired(argv, seconds)
        result = T.project(self.mesh, self.image, self.dest, runner=timeout, **self.options)
        self.assertEqual(6, result["rc"], result)
        self.assertFalse(self.dest.exists())
        self.assertEqual([], list(self.root.glob(".result.*")))

    def test_foreign_rc_one_never_propagates(self):
        result = T.project(self.mesh, self.image, self.dest, runner=lambda *a: 1, **self.options)
        self.assertEqual(2, result["rc"])

    def test_worker_success_without_measurements_cannot_pass(self):
        result = T.project(self.mesh, self.image, self.dest, runner=lambda *a: 0, **self.options)
        self.assertEqual(3, result["rc"], result)

    def test_bad_policy_and_bool_numbers_refused(self):
        self.options["resolution"] = True
        self.refuse_before_worker(2)

    def test_host_deadline_cleans_partial_output(self):
        import time
        def stall(argv, timeout, log):
            (Path(log).parent / "partial").write_bytes(b"partial")
            time.sleep(5)
        self.options["timeout_s"] = 1
        r = T.project(self.mesh, self.image, self.dest, runner=stall, **self.options)
        self.assertEqual(6, r["rc"])
        self.assertIn("deadline_exceeded", r["reason_codes"])
        self.assertFalse(self.dest.exists())
        self.assertEqual([], list(self.root.glob(".result.*")))

    def test_malformed_png_refused_before_blender(self):
        self.image.write_bytes(b"bad png")
        self.change_rights(lambda r: r["inputs"][1].update(sha256=sha(self.image.read_bytes())))
        self.inputs = (self.mesh.read_bytes(), self.image.read_bytes(), self.rights.read_bytes())
        self.refuse_before_worker(2)

    def test_provider_manifest_conforms_and_stays_blocked(self):
        from asset_factory.contracts import validate_provider
        provider = load_record(read_bytes(Path(T.__file__).with_name("texture_project_provider.json")))
        validate_provider(provider)
        self.assertFalse(provider["runnable"])
        self.assertEqual([], provider["weights"])
        self.assertEqual("sha256:" + core.sha256_json(T.tool_snapshot()), provider["revision"])

    def test_real_runner_timeout_reaps_owned_child(self):
        pidfile = self.root / "child.pid"
        script = "import os,time;open(" + repr(str(pidfile)) + ",'w').write(str(os.getpid()));time.sleep(60)"
        original = T.af_proc.popen
        children = []
        prefix = T._scope_prefix()
        def launch(argv, **kwargs):
            payload, limits = T.af_proc._command(argv, None)
            self.assertEqual(payload[:len(prefix)], prefix)
            self.assertEqual(limits, T.af_proc.MemoryLimits(6*2**30, 8*2**30, 0))
            # Wrap the guarded boundary, returning a real registered process.
            # Never mock subprocess.Popen on a signalling path.
            child = original(argv, **kwargs)
            children.append(child)
            return child
        with patch.object(T.af_proc, "popen", side_effect=launch):
            with self.assertRaises(subprocess.TimeoutExpired):
                T.scoped_run(["python3", "-c", script], 2, self.root / "timeout.log")
        self.assertEqual(len(children), 1)
        self.assertIsNotNone(children[0].returncode)
        self.assertTrue(pidfile.exists(), "timeout fixture must actually start")
        pid = int(pidfile.read_text())
        procstat = Path("/proc") / str(pid) / "stat"
        if procstat.exists():
            # A zombie is stopped; a live child would violate cleanup.
            self.assertEqual("Z", procstat.read_text().split(")", 1)[1].split()[0])

    def test_zero_and_reversed_winding_change_identity(self):
        scene = G.parse_glb(self.mesh.read_bytes())
        original = T.geometry_identity(self.mesh.read_bytes())
        scene.prims[0].indices[:3] = reversed(scene.prims[0].indices[:3])
        self.assertNotEqual(original, T.geometry_identity(G.write_glb(scene)))


class RealBlenderBake(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="texture-real-")
        cls.root = Path(cls.temp.name)
        cls.mesh, cls.image, cls.rights = fixture(cls.root)
        cls.dest = cls.root / "bundle"
        cls.result = T.project(cls.mesh, cls.image, cls.dest, rights=cls.rights, front="-Y",
                               resolution=128, margin_px=4, timeout_s=120, max_front_mae_ppm=50000)
        if cls.result["rc"] != 0:
            raise AssertionError(cls.result)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_real_cube_has_front_and_back_measured(self):
        m = self.result["metrics"]
        self.assertEqual(12, m["triangles"])
        self.assertGreater(m["front_projected_texels"], 0)
        self.assertGreater(m["mirrored_texels"], 0)
        self.assertEqual(1000000, m["populated_coverage_ppm"])
        self.assertLessEqual(m["front_reconstruction_mae_ppm"], 50000)
        self.assertEqual("CANNOT_SEE", self.result["asset_verdict"])
        self.assertEqual("NOT_PERFORMED", self.result["worker"]["delighting"])
        self.assertEqual("CPU", self.result["worker"].get("device"))
        self.assertGreater(self.result["worker"]["inpainted_atlas_pixels"], 0)
        self.assertEqual(m["covered_texels"], sum(m[k] for k in
                         ("front_projected_texels", "mirrored_texels", "seam_mixed_texels", "inpainted_texels")))

    def test_published_file_hashes_are_bound(self):
        for entry in self.result["files"]:
            self.assertEqual(entry["sha256"], sha(read_bytes(self.dest / entry["path"])))
        self.assertEqual(self.mesh.read_bytes(), (self.dest / "input.glb").read_bytes())
        raw = (self.dest / "receipt.json").read_text()
        self.assertEqual(raw, core.canonical_json(self.result))
        core.validate(self.result, load_record(read_bytes(Path(T.__file__).with_name("texture_project.schema.json"))))

    def mutation(self, fn, reason):
        clone = self.root / self._testMethodName
        shutil.copytree(self.dest, clone)
        fn(clone)
        with self.assertRaises(Refusal) as caught:
            T.measure_bundle(clone, self.mesh.read_bytes(), 128, 50000)
        self.assertEqual(5, caught.exception.rc)
        self.assertEqual(reason, caught.exception.reason)
        # Restore passing bytes: the original measurement must still pass.
        self.assertEqual(1000000, T.measure_bundle(self.dest, self.mesh.read_bytes(), 128, 50000)
                         ["populated_coverage_ppm"])

    def test_negative_missing_backfill_then_original_green(self):
        def strip(stage):
            w, h, ch, pixels = P.decode_png((stage / "provenance.png").read_bytes())
            pixels = bytearray(pixels)
            for i in range(0, len(pixels), ch):
                if pixels[i + 1] > 128:
                    pixels[i:i + 4] = b"\x00\x00\x00\x00"
            (stage / "provenance.png").write_bytes(P.encode_png(w, h, pixels))
        self.mutation(strip, "unpopulated_surface_texels")

    def test_negative_swapped_front_channels_then_original_green(self):
        def swap(stage):
            w, h, ch, pixels = P.decode_png((stage / "front_baked.png").read_bytes())
            pixels = bytearray(pixels)
            for i in range(0, len(pixels), ch):
                pixels[i], pixels[i + 2] = pixels[i + 2], pixels[i]
            (stage / "front_baked.png").write_bytes(P.encode_png(w, h, pixels))
        self.mutation(swap, "front_reconstruction_changed")

    def test_negative_blank_atlas_then_original_green(self):
        self.mutation(lambda s: (s / "base_color.png").write_bytes(P.encode_png(128, 128,
                      bytes([0, 0, 0, 255]) * 128 ** 2)), "embedded_base_color_drift")

    def test_empty_front_renders_cannot_see(self):
        clone = self.root / "empty-front"
        shutil.copytree(self.dest, clone)
        (clone / "front_baked.png").write_bytes(P.encode_png(128, 128, bytes(128 ** 2 * 4)))
        with self.assertRaises(Refusal) as caught:
            T.measure_bundle(clone, self.mesh.read_bytes(), 128, 50000)
        self.assertEqual(3, caught.exception.rc)
        self.assertEqual("front_render_unobserved", caught.exception.reason)

    def test_negative_shortened_geometry_then_original_green(self):
        def shorten(stage):
            scene = G.parse_glb((stage / "textured.glb").read_bytes())
            scene.prims[0].positions_um[0] = (0, 0, 0)
            (stage / "textured.glb").write_bytes(G.write_glb(scene))
        self.mutation(shorten, "geometry_changed")

    def test_reused_bundle_is_refused_with_no_byte_changes(self):
        before = {p.name: sha(p.read_bytes()) for p in self.dest.iterdir()}
        r = T.project(self.mesh, self.image, self.dest, rights=self.rights, front="-Y",
                      resolution=128, margin_px=4, timeout_s=120, max_front_mae_ppm=50000)
        self.assertEqual(2, r["rc"])
        self.assertEqual(before, {p.name: sha(p.read_bytes()) for p in self.dest.iterdir()})


if __name__ == "__main__":
    unittest.main()
