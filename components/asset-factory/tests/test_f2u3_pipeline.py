"""F2-U3: imported algorithms, refused mutants, coherent publication, real CPU bake.

Run: python3 -I -B -m unittest discover -s tests -p 'test_f2u3_*.py' -v
The bootstrap is explicit because isolated Python intentionally ignores PYTHONPATH.
"""
import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.blender import bake, bake_support as support, orm, uv

G = support.upstream("glb_core")
PNG = support.upstream("orm_pack")
B = support.upstream("bake_pbr")
PV = support.upstream("pbr_validate")
UG = support.upstream("uv_gate")


def plane(*, uv_kind="good", material=True, shift=0):
    coords = [(0, 0, 0), (1000000, 0, 0), (1000000, 1000000, 0), (0, 1000000, 0)]
    tex = [(0, 0), (65536, 0), (65536, 65536), (0, 65536)]
    if uv_kind == "missing":
        tex = None
    if uv_kind == "collapsed":
        tex = [(0, 0)] * 4
    if uv_kind == "stretch":
        tex = [(u, v // 16) for u, v in tex]
    p = G.Prim("plane", "/plane", G.IDENTITY_Q16, [(x + shift, y, z) for x, y, z in coords],
               [0, 1, 2, 0, 2, 3], tex, [(0, 0, 65536)] * 4, "paint" if material else None)
    raw = G.write_glb(G.Scene([p]))
    if material:
        doc, buf = G._split(raw)
        for m in doc.get("materials", []):
            m["pbrMetallicRoughness"] = {"baseColorFactor": [0.4, 0.2, 0.1, 1],
                                          "metallicFactor": 0, "roughnessFactor": 0.6}
        # Fixture construction, not a production geometry or image algorithm.
        import struct
        js = json.dumps(doc).encode()
        js += b" " * (-len(js) % 4)
        raw = (struct.pack("<4sII", b"glTF", 2, 28 + len(js) + len(buf)) +
               struct.pack("<II", len(js), G.CHUNK_JSON) + js + struct.pack("<II", len(buf), G.CHUNK_BIN) + buf)
    return raw


GOOD = {"albedo": (150, 110, 80), "normal": (128, 128, 255), "ao": (220, 220, 220),
        "roughness": (153, 153, 153), "metal": (0, 0, 0)}


class FakeWorker:
    """The real host verifier checks synthetic outputs; this is not runtime proof."""
    def __init__(self, *, maps=None, missing=None, uv_kind="good", callback=None, failure=None):
        self.maps, self.missing, self.uv_kind = dict(GOOD, **(maps or {})), missing, uv_kind
        self.callback, self.failure, self.calls = callback, failure, []

    def __call__(self, argv, timeout):
        self.calls.append(argv)
        if self.failure == "timeout":
            raise B.BakeError(6, "test timeout")
        if self.failure == "worker":
            return 2, "", "test worker error"
        dest = Path(argv[argv.index("--out-dir") + 1])
        res = int(argv[argv.index("--res") + 1])
        for name, rgb in self.maps.items():
            if name != self.missing:
                (dest / (name + ".png")).write_bytes(PNG.encode_png(res, res, bytes((*rgb, 255)) * res**2))
        if self.missing != "low_uv":
            (dest / "low_uv.glb").write_bytes(plane(uv_kind=self.uv_kind, material=False))
        if self.callback:
            self.callback()
        return 0, 'BVQA_BAKE {"maps":5}\n', ""


class Pipeline(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="af_f2u3_")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.high, self.low, self.dest = self.root / "high.glb", self.root / "low.glb", self.root / "bundle"
        self.high.write_bytes(plane())
        self.low.write_bytes(plane(material=False))
        self.before = [self.high.read_bytes(), self.low.read_bytes()]

    def run_bake(self, worker=None, **kw):
        return bake.bake_bundle(self.high, self.low, self.dest, res=64, runner=worker or FakeWorker(), **kw)

    def assert_refused(self, want, worker=None, **kw):
        r = self.run_bake(worker, **kw)
        self.assertEqual(r["rc"], want, r)
        self.assertFalse(self.dest.exists())
        self.assertEqual(list(self.root.glob(".bundle.f2u3*")), [])
        return r

    def test_success_publishes_complete_bundle(self):
        r = self.run_bake()
        self.assertEqual(r["rc"], 0, r)
        self.assertEqual(r["status"], "OK")
        self.assertNotIn("verdict", r)
        self.assertIn("final_glb_material_binding", r["pending_gates"])
        self.assertGreater(r["checks_evaluated"], 0)
        self.assertEqual([self.high.read_bytes(), self.low.read_bytes()], self.before)
        rec = support.load_record((self.dest / "bundle_receipt.json").read_bytes())
        self.assertEqual(len(rec["files"]), 15)
        self.assertEqual(bake.verify_bundle(self.dest, expected_receipt_sha256=r["receipt_sha256"])["rc"], 0)
        receipts = [support.load_record((self.dest / (name + "_receipt.json")).read_bytes())
                    for name in ("bake", "uv", "orm", "pbr")]
        self.assertEqual([x["schema"] for x in receipts], [B.SCHEMA, UG.SCHEMA, PNG.SCHEMA, PV.SCHEMA])
        for index, name in enumerate(("bake", "uv", "orm")):
            self.assertEqual(receipts[index + 1]["prev_sha256"], support.sha((self.dest / (name + "_receipt.json")).read_bytes()))
        self.assertEqual(rec["orm"]["color_space"], "linear")
        w, h, ch, pixels = PNG.decode_png((self.dest / "orm.png").read_bytes())
        self.assertEqual((w, h, ch, pixels[:4]), (64, 64, 4, bytes((220, 153, 0, 255))))

    def test_missing_map_is_incomplete(self):
        self.assert_refused(3, FakeWorker(missing="normal"))

    def test_inverted_normal_fails(self):
        self.assert_refused(5, FakeWorker(maps={"normal": (128, 128, 0)}))

    def test_magenta_albedo_fails(self):
        self.assert_refused(5, FakeWorker(maps={"albedo": (255, 0, 255)}))

    def test_bad_pbr_albedo_fails(self):
        self.assert_refused(5, FakeWorker(maps={"albedo": (255, 255, 255)}))

    def test_uv_collapse_fails(self):
        self.assert_refused(5, FakeWorker(uv_kind="collapsed"))

    def test_uv_stretch_fails(self):
        self.assert_refused(5, FakeWorker(uv_kind="stretch"))

    def test_aabb_drift_refuses_before_worker(self):
        self.low.write_bytes(plane(shift=100000, material=False))
        worker = FakeWorker()
        self.assert_refused(5, worker)
        self.assertEqual(worker.calls, [])

    def test_worker_failure_and_timeout(self):
        self.assert_refused(2, FakeWorker(failure="worker"))
        self.assert_refused(6, FakeWorker(failure="timeout"))

    def test_imported_exception_cannot_escape_as_rc1(self):
        for exception in (PNG.OrmError("injected PNG failure"), B.BakeError(3, "injected missing output"),
                          RuntimeError("injected imported failure")):
            with self.subTest(exception=type(exception).__name__), mock.patch.object(bake, "pack_maps", side_effect=exception):
                self.assert_refused(3 if isinstance(exception, B.BakeError) else 2)

    def test_existing_destination_preserved(self):
        self.dest.mkdir()
        sentinel = self.dest / "sentinel"
        sentinel.write_bytes(b"caller bytes")
        worker = FakeWorker()
        self.assertEqual(self.run_bake(worker)["rc"], 5)
        self.assertEqual(sentinel.read_bytes(), b"caller bytes")
        self.assertEqual(worker.calls, [])

    def test_publication_error_preserves_raw(self):
        with mock.patch.object(bake.os, "replace", side_effect=OSError("injected rename failure")):
            self.assert_refused(2)
        self.assertEqual([self.high.read_bytes(), self.low.read_bytes()], self.before)

    def test_changed_source_is_stale(self):
        worker = FakeWorker(callback=lambda: self.high.write_bytes(plane(shift=1)))
        self.assert_refused(4, worker)

    def test_bake_receipt_digest_is_checked(self):
        real = B.run
        def stale(*a, **kw):
            rc, rec = real(*a, **kw)
            if rc == 0:
                rec["input_sha256"] = "0" * 64
            return rc, rec
        with mock.patch.object(B, "run", stale):
            self.assert_refused(4)

    def test_post_stage_tampering_is_stale(self):
        real = bake.pack_maps
        def tamper(staging, **kw):
            result = real(staging, **kw)
            (staging / "normal.png").write_bytes(PNG.encode_png(64, 64, bytes((128, 128, 230, 255)) * 64**2))
            return result
        with mock.patch.object(bake, "pack_maps", tamper):
            self.assert_refused(4)

    def test_invalid_policy_and_missing_inputs(self):
        self.assert_refused(2, timeout_s=True)
        self.high.unlink()
        self.assert_refused(3)

    def test_external_uri_refused(self):
        with mock.patch.object(G, "_split", return_value=({"images": [{"uri": "private.png"}]}, b"")), \
                mock.patch.object(G, "parse_glb"):
            self.assert_refused(2)

    def test_tampered_bundle_is_stale(self):
        r = self.run_bake()
        self.assertEqual(r["rc"], 0, r)
        (self.dest / "orm.png").write_bytes(b"changed")
        self.assertEqual(bake.verify_bundle(self.dest, expected_receipt_sha256=r["receipt_sha256"])["rc"], 4)

    def test_forged_receipt_digest_is_stale(self):
        r = self.run_bake()
        self.assertEqual(r["rc"], 0, r)
        self.assertEqual(bake.verify_bundle(self.dest, expected_receipt_sha256="0" * 64)["rc"], 4)
        path = self.dest / "bundle_receipt.json"
        record = support.load_record(path.read_bytes())
        record["rc"] = False
        support.write_record(path, record)
        self.assertEqual(bake.verify_bundle(self.dest, expected_receipt_sha256=support.sha(path.read_bytes()))["rc"], 2)

    def test_toolchain_changed_since_bake_is_stale(self):
        r = self.run_bake()
        self.assertEqual(r["rc"], 0, r)
        original = bake.read_bytes
        def changed_tool(path):
            raw = original(path)
            return raw + b"# changed" if Path(path) == Path(B.__file__).resolve() else raw
        with mock.patch.object(bake, "read_bytes", changed_tool):
            self.assertEqual(bake.verify_bundle(self.dest, expected_receipt_sha256=r["receipt_sha256"])["rc"], 4)

    def test_bundle_extra_file_and_symlink_refused(self):
        r = self.run_bake()
        self.assertEqual(r["rc"], 0, r)
        extra = self.dest / "unexpected"
        extra.write_bytes(b"x")
        self.assertEqual(bake.verify_bundle(self.dest, expected_receipt_sha256=r["receipt_sha256"])["rc"], 4)
        extra.unlink()
        ormfile = self.dest / "orm.png"
        ormfile.unlink()
        ormfile.symlink_to(self.high)
        self.assertEqual(bake.verify_bundle(self.dest, expected_receipt_sha256=r["receipt_sha256"])["rc"], 2)

    def test_cli_usage_never_rc1(self):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(bake.main([]), 2)

    def test_real_cpu_bake_and_real_map_mutants(self):
        # Always attempt the real worker. No skip can be confused with evidence.
        r = bake.bake_bundle(self.high, self.low, self.dest, res=64, timeout_s=180)
        self.assertEqual(r["rc"], 0, r)
        rec = support.load_record((self.dest / "bake_receipt.json").read_bytes())
        print("REAL_CPU_BAKE", support.canonical({"rc": r["rc"], "worker": rec["worker"],
              "coverage_ppm": rec["checks"]["coverage_ppm"], "normal_mean_rgb": rec["checks"]["maps"]["normal"]["mean_rgb"],
              "receipt_sha256": r["receipt_sha256"]}).strip())
        self.assertGreater(rec["checks"]["coverage_ppm"], 0)
        self.assertIsNotNone(rec["worker"])
        self.assertEqual(bake.verify_bundle(self.dest, expected_receipt_sha256=r["receipt_sha256"])["rc"], 0)
        real_outputs = {name: (self.dest / name).read_bytes()
                        for name in ["low_uv.glb"] + [m + ".png" for m in B.MAPS]}
        base = ["--verify-only", "--high", str(self.high), "--low", str(self.low), "--res", "64",
                "--out-dir", str(self.dest)]
        w, h, ch, px = PNG.decode_png((self.dest / "normal.png").read_bytes())
        inverted = b"".join(bytes((px[i], px[i + 1], 255 - px[i + 2], 255)) for i in range(0, len(px), ch))
        (self.dest / "normal.png").write_bytes(PNG.encode_png(w, h, inverted))
        rc, rec = B.run(base)
        self.assertEqual(rc, 5)
        self.assertIn("normal:blue_mean", rec["failed"])
        (self.dest / "albedo.png").write_bytes(PNG.encode_png(w, h, bytes((255, 0, 255, 255)) * w * h))
        rc, rec = B.run(base)
        self.assertEqual(rc, 5)
        self.assertIn("albedo:holes", rec["failed"])
        # Replay actual baked bytes through this unit, preserving the original
        # sibling assertions above as well as exercising factory publication.
        for name, changed in (("normal", PNG.encode_png(w, h, inverted)),
                              ("albedo", PNG.encode_png(w, h, bytes((255, 0, 255, 255)) * w * h))):
            def replay(argv, timeout, name=name, changed=changed):
                out = Path(argv[argv.index("--out-dir") + 1])
                for filename, data in real_outputs.items():
                    (out / filename).write_bytes(changed if filename == name + ".png" else data)
                return 0, 'BVQA_BAKE {"maps":5}\n', ""
            destination = self.root / ("replay-" + name)
            rejected = bake.bake_bundle(self.high, self.low, destination, res=64, timeout_s=60, runner=replay)
            self.assertEqual(rejected["rc"], 5, rejected)
            self.assertFalse(destination.exists())

    def test_real_default_resolution_cpu_bake(self):
        r = bake.bake_bundle(self.high, self.low, self.dest, res=1024, timeout_s=180)
        self.assertEqual(r["rc"], 0, r)
        rec = support.load_record((self.dest / "bake_receipt.json").read_bytes())
        self.assertEqual(rec["params"]["res"], 1024)
        self.assertGreater(rec["checks"]["coverage_ppm"], 0)
        self.assertEqual(bake.verify_bundle(self.dest, expected_receipt_sha256=r["receipt_sha256"])["rc"], 0)
        print("REAL_DEFAULT_CPU_BAKE", support.canonical({"rc": r["rc"], "coverage_ppm": rec["checks"]["coverage_ppm"],
              "normal_mean_rgb": rec["checks"]["maps"]["normal"]["mean_rgb"], "worker": rec["worker"],
              "receipt_sha256": r["receipt_sha256"]}).strip())


class Admission(unittest.TestCase):
    def test_worker_scope_and_deadline_are_forwarded(self):
        with mock.patch.object(B, "default_runner", return_value=(0, "", "")) as run:
            argv = ["blender", "-b", "--factory-startup"]
            bake.scoped_runner(argv, 9)
            actual, deadline = run.call_args.args
            self.assertEqual(actual[-3:], argv)
            self.assertIn("MemoryHigh=6G", actual)
            self.assertIn("MemoryMax=8G", actual)
            self.assertIn("MemorySwapMax=0", actual)
            self.assertEqual(deadline, 9)

    def test_receipt_positive_and_negative_controls(self):
        good = {"schema": "test", "rc": 0, "status": "PASS", "prev_sha256": "pin"}
        support.require_receipt(0, good, "test", link="prev_sha256", previous="pin")
        for rc, rec, want in [(1, good, 2), (True, good, 2), (0, None, 3),
                              (0, dict(good, schema="wrong"), 2), (0, dict(good, status="SKIP"), 3),
                              (0, dict(good, prev_sha256="wrong"), 4), (0, dict(good, failed=["bad"]), 5)]:
            with self.subTest(rc=rc, rec=rec), self.assertRaises(support.Refusal) as raised:
                support.require_receipt(rc, rec, "test", link="prev_sha256", previous="pin")
            self.assertEqual(raised.exception.rc, want)

    def test_canonical_integer_and_duplicate_controls(self):
        self.assertEqual(support.canonical({"z": 2, "a": True}), '{"a":true,"z":2}\n')
        for raw in (b'{"a":1,"a":2}', b'{"a":0.1}', b'{"a":NaN}'):
            with self.assertRaises(support.Refusal):
                support.load_record(raw)
        with self.assertRaises(support.Refusal):
            support.canonical({"bad": 1.0})

    def test_missing_dependency_is_incomplete(self):
        with mock.patch.object(support, "SIBLING", Path("/nonexistent-f2u3-sibling")), self.assertRaises(support.Refusal) as exc:
            support.upstream("uv_gate")
        self.assertEqual(exc.exception.rc, 3)

    def test_uv_positive_missing_stretch_and_collapsed(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "mesh.glb"
            for kind, want in (("good", 0), ("missing", 5), ("stretch", 5), ("collapsed", 5)):
                path.write_bytes(plane(uv_kind=kind, material=False))
                r = uv.check_uv(path)
                self.assertEqual(r["rc"], want, r)

    def test_uv_zero_checks_and_chain_drift(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "mesh.glb"
            path.write_bytes(plane(material=False))
            real = UG.run
            def mutate(*args, **kw):
                rc, rec = real(*args, **kw)
                for p in rec["prims"]:
                    p["uv_tris"] = 0
                return rc, rec
            with mock.patch.object(UG, "run", mutate):
                self.assertEqual(uv.check_uv(path)["rc"], 3)
            def drift(*args, **kw):
                rc, rec = real(*args, **kw)
                rec["prev_sha256"] = "wrong"
                return rc, rec
            with mock.patch.object(UG, "run", drift):
                self.assertEqual(uv.check_uv(path)["rc"], 4)

    def test_orm_linear_roundtrip_and_floor_controls(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paths = [root / (n + ".png") for n in ("ao", "roughness", "metal", "orm")]
            for path, val in zip(paths[:3], (220, 0, 30)):
                path.write_bytes(PNG.encode_png(2, 2, bytes((val, val, val, 255)) * 4))
            packed, _ = PNG.pack_orm(bytes([220]) * 4, bytes([0]) * 4, bytes([30]) * 4, 2, 2)
            paths[3].write_bytes(PNG.encode_png(2, 2, packed))
            self.assertEqual(orm.verify_channels(*paths)["roughness_clamped_pixels"], 4)
            for channels in ((11, 220, 30, 255), (220, 58, 96, 255), (220, 0, 30, 255)):
                paths[3].write_bytes(PNG.encode_png(2, 2, bytes(channels) * 4))
                with self.assertRaises(support.Refusal) as exc:
                    orm.verify_channels(*paths)
                self.assertEqual(exc.exception.rc, 5)

    def test_no_materials_is_incomplete(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "stripped.glb"
            path.write_bytes(plane(material=False))
            self.assertEqual(PV.run(["--glb", str(path)])[0], 3)
            path.write_bytes(plane())
            self.assertEqual(PV.run(["--glb", str(path)])[0], 0)


SAMPLER = {"magFilter": 9729, "minFilter": 9729, "wrapS": 33071, "wrapT": 33071}


def textured_fixture():
    """Synthetic embedded images with distinct values; no third-party/model data."""
    import struct
    doc, binary = G._split(plane())
    binary = bytearray(binary)
    hashes = {}
    doc["images"], doc["textures"], doc["samplers"] = [], [], [dict(SAMPLER)]
    for index, (role, rgb) in enumerate((("albedo", (150, 110, 80)), ("normal", (128, 128, 255)), ("orm", (220, 153, 0)))):
        png = PNG.encode_png(2, 2, bytes((*rgb, 255)) * 4)
        hashes[role] = support.sha(png)
        binary += b"\0" * (-len(binary) % 4)
        offset = len(binary)
        binary += png
        view = len(doc["bufferViews"])
        doc["bufferViews"].append({"buffer": 0, "byteOffset": offset, "byteLength": len(png)})
        doc["images"].append({"bufferView": view, "mimeType": "image/png"})
        doc["textures"].append({"source": index, "sampler": 0})
    doc["buffers"][0]["byteLength"] = len(binary)
    m = doc["materials"][0]
    m["pbrMetallicRoughness"].update(baseColorFactor=[1, 1, 1, 1], metallicFactor=1, roughnessFactor=1)
    m["pbrMetallicRoughness"]["baseColorTexture"] = {"index": 0}
    m["pbrMetallicRoughness"]["metallicRoughnessTexture"] = {"index": 2}
    m["normalTexture"], m["occlusionTexture"] = {"index": 1}, {"index": 2}

    def serialize():
        js = json.dumps(doc).encode()
        js += b" " * (-len(js) % 4)
        bb = bytes(binary) + b"\0" * (-len(binary) % 4)
        return (struct.pack("<4sII", b"glTF", 2, 28 + len(js) + len(bb)) +
                struct.pack("<II", len(js), G.CHUNK_JSON) + js + struct.pack("<II", len(bb), G.CHUNK_BIN) + bb)
    return doc, hashes, serialize


class MaterialBoundary(unittest.TestCase):
    def test_textured_roundtrip_and_stripped_binding(self):
        doc, hashes, serialize = textured_fixture()
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "final.glb"
            path.write_bytes(serialize())
            r = uv.check_textured_glb(path, expected_maps=hashes, sampler_policy=SAMPLER,
                                      expected_geometry=path, expected_geometry_sha256=support.sha(path.read_bytes()))
            self.assertEqual(r["rc"], 0, r)
            self.assertGreater(r["checks_evaluated"], 0)
            del doc["materials"][0]["normalTexture"]
            path.write_bytes(serialize())
            self.assertEqual(uv.check_textured_glb(path, expected_maps=hashes, sampler_policy=SAMPLER)["rc"], 5)

    def test_orm_srgb_binding_sampler_and_image_tamper(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "final.glb"
            for mode in ("srgb", "sampler", "hash", "transform"):
                doc, hashes, serialize = textured_fixture()
                if mode == "srgb":
                    # Reading the ORM as base color would introduce sRGB decoding.
                    doc["materials"][0]["pbrMetallicRoughness"]["baseColorTexture"] = {"index": 2}
                elif mode == "sampler":
                    doc["samplers"][0]["wrapS"] = 10497
                elif mode == "hash":
                    hashes["orm"] = "0" * 64
                else:
                    doc["materials"][0]["normalTexture"]["extensions"] = {"KHR_texture_transform": {}}
                path.write_bytes(serialize())
                r = uv.check_textured_glb(path, expected_maps=hashes, sampler_policy=SAMPLER)
                self.assertEqual(r["rc"], 3 if mode == "transform" else 5, (mode, r))


class MutationProof(unittest.TestCase):
    def test_disabled_rules_turn_existing_controls_red(self):
        """Run unchanged assertions against temporary rule-breaking mutations."""
        real_stats = B.map_stats
        def no_normal(*a, **kw):
            metrics, failed = real_stats(*a, **kw)
            return metrics, [x for x in failed if x != "normal:blue_mean"]
        real_scene_gate = UG.gate_scene
        def no_stretch(*a, **kw):
            rc, body = real_scene_gate(*a, **kw)
            body["failed"] = [f for f in body["failed"] if f != "uv_stretch"]
            if not body["failed"] and rc == 5:
                body["status"], rc = "PASS", 0
            return rc, body
        def no_bake_binding(receipt, tool, staging, high, low, res):
            return tool.verify_outputs(staging, G.parse_glb(support.read_bytes(low)), res)[0]
        original_read = bake.read_bytes
        def no_source_drift(path):
            return plane() if Path(path).name == "high.glb" else original_read(path)
        mutations = [
            ("normal-check-disabled", Pipeline, "test_inverted_normal_fails", mock.patch.object(B, "map_stats", no_normal)),
            ("pbr-albedo-disabled", Pipeline, "test_bad_pbr_albedo_fails", mock.patch.object(bake, "_pbr", return_value={"checks": 2})),
            ("integrity-disabled", Pipeline, "test_tampered_bundle_is_stale", mock.patch.object(bake, "verify_bundle", return_value=support.result(0))),
            ("orm-channel-check-disabled", Admission, "test_orm_linear_roundtrip_and_floor_controls", mock.patch.object(orm, "verify_channels", return_value={"roughness_clamped_pixels": 4})),
            ("receipt-check-disabled", Admission, "test_receipt_positive_and_negative_controls", mock.patch.object(support, "require_receipt", return_value=None)),
            ("uv-stretch-disabled", Pipeline, "test_uv_stretch_fails", mock.patch.object(UG, "gate_scene", no_stretch)),
            ("bake-input-binding-disabled", Pipeline, "test_bake_receipt_digest_is_checked", mock.patch.object(bake, "_check_bake", no_bake_binding)),
            ("source-drift-disabled", Pipeline, "test_changed_source_is_stale", mock.patch.object(bake, "read_bytes", no_source_drift)),
            ("integer-json-disabled", Admission, "test_canonical_integer_and_duplicate_controls", mock.patch.object(support, "load_record", json.loads)),
            ("missing-dependency-disabled", Admission, "test_missing_dependency_is_incomplete", mock.patch.object(support, "upstream", return_value=UG)),
            ("memory-scope-disabled", Admission, "test_worker_scope_and_deadline_are_forwarded", mock.patch.object(bake, "MEMORY_SCOPE", [])),
            ("material-binding-disabled", MaterialBoundary, "test_textured_roundtrip_and_stripped_binding", mock.patch.object(uv, "check_textured_glb", return_value=support.result(0, checks_evaluated=1))),
        ]
        for name, case, method, mutation in mutations:
            with self.subTest(mutant=name), mutation, contextlib.redirect_stderr(io.StringIO()):
                outcome = unittest.TestResult()
                case(method).run(outcome)
                self.assertTrue(outcome.failures, (name, outcome.errors))
                print("MUTATION_RED", name, "failures=" + str(len(outcome.failures)))


if __name__ == "__main__":
    unittest.main()
