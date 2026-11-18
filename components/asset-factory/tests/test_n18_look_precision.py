"""Protected Blender precision, tangent replay, and tool identity."""
import hashlib
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import patch

from asset_factory import core, domains
from tools.blender import bake, look_bake, uv_quality
from tools.input.evidence import bytes_digest
from tools.pipeline import stages, uv_derivative

from test_e2e_blender import options
from test_n18_geo_look_reg import PINS


DERIVATIVE = (
    "build/day1118/out/wire/uvx/pilot256-precise-smooth.glb",
    "101d269a82b171c140fd7937b873a6d8beca75cb65ee3aad890c3bc5c1e8abac",
)
PROTECTED_SOURCE = "component/tools/blender/look_bake.py"


class PrecisionIdentity(unittest.TestCase):
    def test_precision_policy_changes_protected_bundle_and_stage_identity(self):
        source = Path(look_bake.__file__).resolve()
        data = source.read_bytes()
        policy = b"PROTECTED_GLTF_ROUNDING_DIGITS = 7\n"
        self.assertEqual(data.count(policy), 1)
        changed = data.replace(policy, b"PROTECTED_GLTF_ROUNDING_DIGITS = 6\n")
        digest = hashlib.sha256(changed).hexdigest()

        def tool_entry():
            return next(row for row in bake._tool_files(protected=True) if row["path"] == PROTECTED_SOURCE)

        original_reader = bake.read_bytes
        original_hash = core.sha256_file

        def read_policy(path):
            return changed if path == source else original_reader(path)

        def hash_policy(path):
            return digest if path.resolve() == source else original_hash(path)

        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "high").write_bytes(b"test source")
            (root / "reference").write_bytes(b"\x89PNG\r\n\x1a\n")
            native = stages.NativeStages(source=root / "high", reference=root / "reference",
                                         provider="byo", options=options(root))
            fixed_key = {"stage": "uv_bake_orm", "inputs": [], "parameters": {}, "previous": None}
            before = native._pin("uv_bake_orm")
            self.assertEqual(tool_entry()["sha256"], original_hash(source))
            with patch.object(bake, "read_bytes", side_effect=read_policy):
                self.assertEqual(tool_entry()["sha256"], digest)
            with patch.object(core, "sha256_file", side_effect=hash_policy):
                after = native._pin("uv_bake_orm")
            self.assertNotEqual(before, after)
            self.assertNotEqual(core.sha256_json(dict(fixed_key, toolchain_sha256=before)),
                                core.sha256_json(dict(fixed_key, toolchain_sha256=after)))
            print("LOOK_PRECISION_KEYS", before, after, flush=True)


@unittest.skipUnless(os.environ.get("AF_GEO_LOOK_REG_CAMPAIGN"), "offline campaign fixture root not configured")
class QuadriFlowPrecision(unittest.TestCase):
    def test_live_protected_export_and_stock_rounding_control(self):
        root = Path(os.environ["AF_GEO_LOOK_REG_CAMPAIGN"]).resolve(strict=True)
        prepared = Path(os.environ["AF_GEO_LOOK_REG_PREPARED"]).resolve(strict=True)
        high = prepared / PINS["quadriflow"]["high"][0]
        source = prepared / PINS["quadriflow"]["low"][0]
        derivative = root / DERIVATIVE[0]
        for path, digest in ((high, PINS["quadriflow"]["high"][1]), (source, PINS["quadriflow"]["low"][1]),
                             (derivative, DERIVATIVE[1])):
            self.assertEqual(core.sha256_file(path), digest)
        raw = derivative.read_bytes()
        count = PINS["quadriflow"]["low_faces"]
        atlas = {"mesh_digest": bytes_digest(raw), "size": [256, 256], "frame": domains.FRAME,
                 "charts": [1] * count, "max_faces": 1200, "max_work": 60000000, "max_mesh_bytes": 1000000}
        self.assertEqual(uv_derivative.same_geometry(source.read_bytes(), raw, max_faces=1200), count)
        faces, tangents = bake._look_faces(raw, atlas)
        self.assertEqual(list(tangents.values()), [True] * count)

        def run(output, digits):
            def scoped(argv, timeout):
                if digits is not None:
                    argv = list(argv)
                    index = argv.index("--python-expr") + 1
                    prefix, separator, entry = argv[index].partition("from tools.blender.look_bake")
                    self.assertTrue(separator)
                    argv[index] = (
                        prefix + "from tools.blender import look_bake; "
                        + f"look_bake.PROTECTED_GLTF_ROUNDING_DIGITS = {digits!r}; " + separator + entry
                    )
                return bake._look_scoped_runner(argv, timeout)

            output.mkdir()
            started = time.monotonic()
            rc, worker = bake.upstream("bake_pbr").run(
                ["--high", str(high), "--low", str(derivative), "--out-dir", str(output),
                 "--res", "256", "--margin-px", "2", "--cage-extrusion-um", "5000", "--timeout", "120"],
                runner=bake._look_runner(scoped, 1, True))
            self.assertEqual(rc, 0, worker)
            self.assertEqual(worker["worker"]["gltf_rounding_digits"], 7 if digits is None else digits)
            reopened = (output / "low_uv.glb").read_bytes()
            _, _, checks = uv_quality.load_triangles(
                reopened, {**atlas, "mesh_digest": bytes_digest(reopened)}, mirrored=set(range(count)))
            invalid = sum(valid is not True for valid in checks.values())
            self.assertEqual(len(checks), count)
            print("LOOK_PRECISION_WORKER", worker["worker"]["gltf_rounding_digits"], invalid,
                  time.monotonic() - started, bytes_digest(reopened), flush=True)
            return reopened, invalid

        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            precision, invalid = run(root / "precise", None)
            self.assertEqual(invalid, 0)
            bake._check_preserved_atlas(precision, {"atlas": atlas, "faces": faces})

            # The installed stock exporter uses four decimal places. Six
            # changes the bytes but experimentally stays within the RP3 bound.
            six, invalid = run(root / "six", 6)
            self.assertEqual(invalid, 0)
            self.assertNotEqual(bytes_digest(six), bytes_digest(precision))
            bake._check_preserved_atlas(six, {"atlas": atlas, "faces": faces})

            stock, invalid = run(root / "stock", 4)
            self.assertGreater(invalid, 0)
            with self.assertRaisesRegex(bake.Refusal, "^look_worker_tangent_drift$") as drift:
                bake._check_preserved_atlas(stock, {"atlas": atlas, "faces": faces})
            self.assertEqual(drift.exception.rc, 5)
