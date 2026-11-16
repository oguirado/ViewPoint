"""Adversarial reproductions; synthetic runners never establish production proof."""
import ast
import contextlib
import inspect
import json
import os
from pathlib import Path
import signal
import shutil
import struct
import subprocess
import sys
import tempfile
import textwrap
import time
import types
import unittest
from unittest import mock

from test_f2u3_pipeline import (Pipeline, FakeWorker, plane, textured_fixture,
                               SAMPLER, support, bake, uv, orm, B, G, PNG, PV)


def glb(doc, binary):
    js = json.dumps(doc).encode()
    js += b" " * (-len(js) % 4)
    binary += b"\0" * (-len(binary) % 4)
    return (struct.pack("<4sII", b"glTF", 2, 28 + len(js) + len(binary)) +
            struct.pack("<II", len(js), G.CHUNK_JSON) + js +
            struct.pack("<II", len(binary), G.CHUNK_BIN) + binary)


class Repair(unittest.TestCase):
    setUp = Pipeline.setUp
    run_bake = Pipeline.run_bake
    assert_refused = Pipeline.assert_refused

    def test_f1_material_semantics_and_extensions(self):
        for key, value, want in (("strength", 0, 5), ("scale", -1, 5),
                                 ("metallicFactor", 0, 5), ("emissiveTexture", {"index": 0}, 3),
                                 ("root_extension", ["KHR_materials_variants"], 3),
                                 ("primitive_extension", {"KHR_materials_variants": {}}, 3)):
            doc, hashes, serialize = textured_fixture()
            mat = doc["materials"][0]
            mat["pbrMetallicRoughness"].update(baseColorFactor=[1, 1, 1, 1], metallicFactor=1, roughnessFactor=1)
            if key == "strength": mat["occlusionTexture"][key] = value
            elif key == "scale": mat["normalTexture"][key] = value
            elif key == "metallicFactor": mat["pbrMetallicRoughness"][key] = value
            elif key == "root_extension": doc["extensionsUsed"] = value
            elif key == "primitive_extension": doc["meshes"][0]["primitives"][0]["extensions"] = value
            else: mat[key] = value
            self.low.write_bytes(serialize())
            with self.subTest(key=key):
                self.assertEqual(uv.check_textured_glb(self.low, expected_maps=hashes,
                                                     sampler_policy=SAMPLER)["rc"], want)

    def test_f1_geometry_must_be_pinned(self):
        doc, hashes, serialize = textured_fixture()
        doc["materials"][0]["pbrMetallicRoughness"].update(baseColorFactor=[1, 1, 1, 1], metallicFactor=1, roughnessFactor=1)
        self.low.write_bytes(serialize())
        self.assertEqual(uv.check_textured_glb(self.low, expected_maps=hashes, sampler_policy=SAMPLER)["rc"], 3)

    def test_f1_texture_objects_are_closed(self):
        doc, hashes, serialize = textured_fixture()
        doc["textures"][0]["unrecognised"] = True
        self.low.write_bytes(serialize())
        self.assertEqual(uv.check_textured_glb(self.low, expected_maps=hashes, sampler_policy=SAMPLER,
                         expected_geometry=self.low, expected_geometry_sha256=support.sha(self.low.read_bytes()))["rc"], 3)

    def test_f1_pinned_geometry_and_uv_identity(self):
        doc, hashes, serialize = textured_fixture()
        self.low.write_bytes(serialize())
        pin = self.root / "low_uv.glb"
        pin.write_bytes(plane(material=False))
        kw = dict(expected_maps=hashes, sampler_policy=SAMPLER, expected_geometry=pin,
                  expected_geometry_sha256=support.sha(pin.read_bytes()))
        self.assertEqual(uv.check_textured_glb(self.low, **kw)["rc"], 0)
        for fixture in (plane(shift=1, material=False), plane(uv_kind="stretch", material=False)):
            pin.write_bytes(fixture)
            kw["expected_geometry_sha256"] = support.sha(fixture)
            self.assertEqual(uv.check_textured_glb(self.low, **kw)["rc"], 5)
        pin.write_bytes(plane(material=False))
        kw["expected_geometry_sha256"] = "0" * 64
        self.assertEqual(uv.check_textured_glb(self.low, **kw)["rc"], 4)

    def test_f2u3_final_glb_missing_orm_fails(self):
        for key in ("occlusionTexture", "metallicRoughnessTexture"):
            doc, hashes, serialize = textured_fixture()
            mat = doc["materials"][0]
            del (mat if key == "occlusionTexture" else mat["pbrMetallicRoughness"])[key]
            self.low.write_bytes(serialize())
            self.assertEqual(uv.check_textured_glb(self.low, expected_maps=hashes, sampler_policy=SAMPLER)["rc"], 5)

    def test_f2_tool_paths_are_relocatable(self):
        self.assertTrue(all(not Path(f["path"]).is_absolute() for f in bake._tool_files()))

    def test_f2_blender_binary_is_bound(self):
        files = bake._tool_files()
        self.assertTrue(any(f["path"] == "runtime/blender" for f in files), files)

    def test_f2_bundle_verifies_after_checkout_relocation(self):
        r = self.run_bake()
        self.assertEqual(r["rc"], 0, r)
        component = self.root / "relocated" / "components" / "asset-factory"
        (component / "tools").mkdir(parents=True)
        shutil.copytree(Path(bake.__file__).parent, component / "tools" / "blender",
                        ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copytree(Path(bake.core.__file__).parent, component / "asset_factory",
                        ignore=shutil.ignore_patterns("__pycache__"))
        (component.parent / "blender-visual-qa").symlink_to(support.SIBLING, target_is_directory=True)
        script = 'from tools.blender.bake import verify_bundle; import json,sys; print(json.dumps(verify_bundle(sys.argv[1], expected_receipt_sha256=sys.argv[2])))'
        proc = subprocess.run([sys.executable, "-B", "-c", script, str(self.dest), r["receipt_sha256"]],
                              cwd=component, timeout=30, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout)["rc"], 0, proc.stdout)

    def test_f2_blender_drift_is_stale(self):
        r = self.run_bake()
        self.assertEqual(r["rc"], 0, r)
        replacement = self.root / "different-blender"
        replacement.write_bytes(b"different binary")
        with mock.patch.object(B, "BLENDER", str(replacement)):
            self.assertEqual(bake.verify_bundle(self.dest, expected_receipt_sha256=r["receipt_sha256"])["rc"], 4)

    def test_f3_exception_zero_is_error(self):
        with mock.patch.object(bake, "pack_maps", side_effect=B.BakeError(0, "bad")):
            self.assert_refused(2)

    def test_f3_unknown_exception_code_is_error(self):
        exc = RuntimeError("not a domain exception")
        exc.rc = 6
        with mock.patch.object(bake, "pack_maps", side_effect=exc):
            self.assert_refused(2)

    def test_f4_dead_process_lock_and_staging_recover(self):
        script = '''from pathlib import Path
from tools.blender.bake import bake_bundle
import os, signal, sys
def die(argv, timeout):
    os.kill(os.getpid(), signal.SIGKILL)
bake_bundle(Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3]), res=64, runner=die)
'''
        proc = subprocess.run([sys.executable, "-B", "-c", script, str(self.high), str(self.low), str(self.dest)],
                              timeout=30, capture_output=True)
        self.assertEqual(proc.returncode, -signal.SIGKILL, proc.stderr)
        r = self.run_bake()
        self.assertEqual(r["rc"], 0, r)
        self.assertEqual(list(self.root.glob(".bundle.f2u3-*")), [])

    def test_f4_sigterm_cleans_staging(self):
        def terminate():
            os.kill(os.getpid(), signal.SIGTERM)
        self.assert_refused(6, FakeWorker(callback=terminate))

    def test_f4_live_lock_is_refused(self):
        import fcntl
        path = self.root / ".f2u3-lock-bundle"
        with path.open("w") as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            worker = FakeWorker()
            self.assert_refused(5, worker)
            self.assertEqual(worker.calls, [])

    def test_f5_wrong_upstream_channel_order_is_caught_in_pipeline(self):
        real = PNG.pack_orm
        def wrong(ao, rough, metal, w, h, floor=None):
            return real(metal, rough, ao, w, h, floor)
        with mock.patch.object(PNG, "pack_orm", wrong):
            self.assert_refused(5)

    def test_f5_publication_probe_reaches_worker(self):
        worker = FakeWorker()
        real = bake.os.replace
        seen = []
        def fail_publish(src, dst, *a, **kw):
            if Path(src).is_dir():
                seen.append(str(src))
                raise OSError("directory publication only")
            return real(src, dst, *a, **kw)
        with mock.patch.object(bake.os, "replace", fail_publish):
            self.assert_refused(2, worker)
        self.assertTrue(worker.calls)
        self.assertEqual(len(seen), 1)

    def test_f5_real_external_uri_is_refused(self):
        doc, binary = G._split(self.high.read_bytes())
        doc["images"] = [{"uri": "private.png"}]
        self.high.write_bytes(glb(doc, binary))
        worker = FakeWorker()
        self.assert_refused(2, worker)
        self.assertEqual(worker.calls, [])

    def test_f5_staged_bytes_are_checked_in_pipeline(self):
        real = bake.write_record
        def tamper(path, record):
            real(path, record)
            if path.name == "bundle_receipt.json":
                (path.parent / "orm.png").write_bytes(b"changed after inventory")
        with mock.patch.object(bake, "write_record", tamper):
            self.assert_refused(4)

    def test_f5_bad_receipt_schema_in_pipeline(self):
        real = B.run
        def tamper(*args, **kw):
            rc, rec = real(*args, **kw)
            rec["schema"] = "wrong"
            return rc, rec
        with mock.patch.object(B, "run", tamper):
            self.assert_refused(2)

    def test_f5_duplicate_ram_key_in_pipeline(self):
        real = B.run
        def tamper(args, **kw):
            rc, rec = real(args, **kw)
            path = Path(args[args.index("--out-dir") + 1]) / "ram_receipt.json"
            path.write_bytes(b'{"schema":"duplicate",' + path.read_bytes()[1:])
            return rc, rec
        with mock.patch.object(B, "run", tamper):
            self.assert_refused(2)

    def test_f5_poisoned_module_is_refused_before_worker(self):
        worker = FakeWorker()
        poison = types.ModuleType("blender_visual_qa.assets.untrusted")
        poison.__file__ = "/untrusted/origin.py"
        with mock.patch.dict(sys.modules, {poison.__name__: poison}):
            self.assert_refused(4, worker)
        self.assertEqual(worker.calls, [])

    def test_f5_unknown_material_in_handoff(self):
        doc, hashes, serialize = textured_fixture()
        doc["materials"][0]["emissiveTexture"] = {"index": 0}
        self.low.write_bytes(serialize())
        pin = self.root / "low_uv.glb"
        pin.write_bytes(plane(material=False))
        self.assertEqual(uv.check_textured_glb(self.low, expected_maps=hashes, sampler_policy=SAMPLER,
                         expected_geometry=pin, expected_geometry_sha256=support.sha(pin.read_bytes()))["rc"], 3)

    def test_f5_real_projection_hole_is_rejected(self):
        scene = G.parse_glb(plane())
        scene.prims[0].indices = [0, 1, 2]
        self.high.write_bytes(G.write_glb(scene))
        r = bake.bake_bundle(self.high, self.low, self.dest, res=64, timeout_s=60)
        self.assertEqual(r["rc"], 5, r)
        self.assertFalse(self.dest.exists())

    def test_f6_shared_kernel_or_visible_stub(self):
        text = Path(support.__file__).read_text()
        self.assertIn("from asset_factory import core", text)
        self.assertNotIn("_afstub_", text)
        from asset_factory import core
        self.assertIs(support.core, core)

    def test_f6_unassigned_manifest_schema_blocks_admission(self):
        r = self.run_bake()
        self.assertEqual(r["rc"], 0, r)
        path = self.dest / "bundle_receipt.json"
        rec = support.load_record(path.read_bytes())
        rec["runner"]["kind"] = "scoped-cpu"
        support.write_record(path, rec)
        admitted = bake.verify_bundle(self.dest, expected_receipt_sha256=support.sha(path.read_bytes()), production=True)
        self.assertEqual(admitted["rc"], 3, admitted)

    def test_f7_synthetic_runner_cannot_be_production_evidence(self):
        r = self.run_bake()
        self.assertEqual(r["rc"], 0, r)
        rec = support.load_record((self.dest / "bundle_receipt.json").read_bytes())
        self.assertEqual(rec.get("runner", {}).get("kind"), "injected")
        admitted = bake.verify_bundle(self.dest, expected_receipt_sha256=r["receipt_sha256"], production=True)
        self.assertEqual(admitted["rc"], 3, admitted)

    def test_f8_nonpassing_ram_receipt_refused(self):
        real = B.run
        def tamper(args, **kw):
            rc, rec = real(args, **kw)
            path = Path(args[args.index("--out-dir") + 1]) / "ram_receipt.json"
            obj = support.load_record(path.read_bytes())
            obj["status"] = "FAIL"
            support.write_record(path, obj)
            return rc, rec
        with mock.patch.object(B, "run", tamper):
            self.assert_refused(3)

    def test_f9_last_bad_covered_texel_does_not_bias_padding(self):
        real = bake._pbr
        def probe(staging, checks, previous):
            raw = bake.read_bytes(staging / "albedo.png")
            w, h, ch, pixels = PNG.decode_png(raw)
            scene = G.parse_glb(bake.read_bytes(staging / "low_uv.glb"))
            cov = sorted(B.coverage(B.uv_tris(scene), w))
            pixels = bytearray(pixels)
            x, y = cov[-1]
            pixels[(y*w+x)*ch:(y*w+x)*ch+3] = b"\xff\xff\xff"
            (staging / "albedo.png").write_bytes(PNG.encode_png(w, h, bytes(pixels)))
            try:
                return real(staging, checks, previous)
            finally:
                (staging / "albedo.png").write_bytes(raw)
        with mock.patch.object(bake, "_pbr", probe):
            r = self.run_bake()
            self.assertEqual(r["rc"], 0, r)

    def test_f10_parent_symlink_is_refused(self):
        link = self.root / "alias"
        link.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(support.Refusal) as exc:
            support.read_bytes(link / "high.glb")
        self.assertEqual(exc.exception.rc, 2)

    def test_f10_uv_reason_is_preserved(self):
        with mock.patch.object(bake, "check_uv", return_value=support.result(5, "distinct_uv_reason")):
            r = self.run_bake()
        self.assertEqual(r["rc"], 5)
        self.assertIn("distinct_uv_reason", r["reason_codes"])

    def test_f10_concurrent_empty_destination_preserved(self):
        real = bake.os.fsync
        def race(fd):
            if not self.dest.exists(): self.dest.mkdir()
            return real(fd)
        with mock.patch.object(bake.os, "fsync", race):
            r = self.run_bake()
        self.assertNotEqual(r["rc"], 0, r)
        self.assertTrue(self.dest.is_dir())
        self.assertEqual(list(self.dest.iterdir()), [])

    def test_f10_host_stage_deadline(self):
        real = bake.pack_maps
        def slow(*a, **kw):
            time.sleep(2)
            return real(*a, **kw)
        started = time.monotonic()
        with mock.patch.object(bake, "pack_maps", slow):
            self.assert_refused(6, timeout_s=1)
        self.assertLess(time.monotonic() - started, 1.8)

    def test_f10_parent_sync_error_does_not_leave_published_bundle(self):
        real = bake.os.fsync
        def fail_parent(fd):
            if self.dest.is_dir() and (self.dest / "bundle_receipt.json").is_file():
                raise OSError("parent durability failed")
            return real(fd)
        with mock.patch.object(bake.os, "fsync", fail_parent):
            self.assert_refused(2)


class LocalMutations(unittest.TestCase):
    def test_six_local_condition_mutants_go_red(self):
        """Mutate one condition, retaining the real pipeline and all other rules."""
        cases = [
            (bake, "verify_bundle", 'if sha(read_bytes(root / name)) != entry["sha256"]:',
             "if False:", "test_f5_staged_bytes_are_checked_in_pipeline", []),
            (orm, "verify_channels", 'if (ow, oh, ch) != (w, h, 4) or any(',
             'if False and any(', "test_f5_wrong_upstream_channel_order_is_caught_in_pipeline", []),
            (support, "require_receipt", 'if receipt.get("schema") != schema or type(receipt.get("rc")) is not int or receipt["rc"] != rc:',
             'if False:', "test_f5_bad_receipt_schema_in_pipeline", [(bake, "require_receipt")]),
            (support, "load_record", 'if key in out:', 'if False:',
             "test_f5_duplicate_ram_key_in_pipeline", [(bake, "load_record")]),
            (support, "upstream", 'if origin is None or not Path(origin).resolve().is_relative_to(SIBLING.resolve()):',
             'if False:', "test_f5_poisoned_module_is_refused_before_worker", [(bake, "upstream")]),
            (uv, "check_textured_glb", 'if set(mat) - {"name", "pbrMetallicRoughness", "normalTexture", "occlusionTexture"}:',
             'if False:', "test_f5_unknown_material_in_handoff", []),
        ]
        for module, name, old, new, control, aliases in cases:
            source = textwrap.dedent(inspect.getsource(getattr(module, name)))
            self.assertEqual(source.count(old), 1, name)
            namespace = vars(module).copy()
            exec(compile(source.replace(old, new), "<single-condition-mutant>", "exec"), namespace)
            with self.subTest(rule=name), contextlib.ExitStack() as stack:
                stack.enter_context(mock.patch.object(module, name, namespace[name]))
                for other, attr in aliases:
                    stack.enter_context(mock.patch.object(other, attr, namespace[name]))
                outcome = unittest.TestResult()
                Repair(control).run(outcome)
                self.assertTrue(outcome.failures, (name, outcome.errors))
                print("LOCAL_MUTATION_RED", name, "failures=" + str(len(outcome.failures)))


del Pipeline
