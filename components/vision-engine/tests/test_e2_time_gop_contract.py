"""W3 T2 temporal metadata, canonical/blob identity and registry regressions."""
import hashlib
import struct
import unittest
import zlib

from visioncore import canon, cli, gop, params, pipeline
from visioncore.errors import VqaError


class GopContractTests(unittest.TestCase):
    def test_sparse_frame_ids_are_explicit_in_every_record(self):
        run = pipeline.analyze(cli._selftest_source("clean_static", n=4))
        gs = gop.encode_run(run.tiles, run.grid, [0, 5, 10, 15])
        self.assertEqual(len(gs), 1)
        row = gop.gop_record(gs[0], run.grid.ident, run.params_sha256, 0)
        self.assertEqual(row["frame_ids"], [0, 5, 10, 15])
        self.assertEqual((row["f0"], row["f1"], row["n_frames"]), (0, 15, 4))
        self.assertEqual([f for f, _ in gop.decode_gop(gs[0].blob)[1]], row["frame_ids"])

    def test_adv8_distinct_hashes_match_independent_byte_rendering(self):
        run = pipeline.analyze(cli._selftest_source("scroll", n=4))
        for g in run.gops_raw:
            row = gop.gop_record(g, run.grid.ident, run.params_sha256, 0)
            fields = list(struct.unpack(gop.HDR_FMT, g.blob[:48]))
            izlen = fields[10]
            raw_i = zlib.decompress(g.blob[48:48 + izlen])
            body = g.blob[48 + izlen:]
            fields[10] = fields[13] = 0
            identity = struct.pack(gop.HDR_FMT, *fields) + raw_i + body
            self.assertEqual(row["gop_blob_sha256"], hashlib.sha256(g.blob).hexdigest())
            self.assertEqual(row["gop_canon_sha256"], hashlib.sha256(identity).hexdigest())
            self.assertNotEqual(row["gop_blob_sha256"], row["gop_canon_sha256"])

    def test_registry_rejects_nonfinite_json_constants(self):
        for value in ("NaN", "Infinity", "-Infinity", '{"x":NaN}'):
            with self.subTest(value=value), self.assertRaises(VqaError) as c:
                canon.loads(value)
            self.assertEqual((c.exception.rc, c.exception.code), (2, "E_FLOAT"))

    def test_registered_token_caps_are_fixed_and_not_profile_overridable(self):
        for key, value in (("PACK_CALL_TOK", 15500), ("PACK_RUN_TOK", 62000), ("PACK_RETRY_TOK", 93000)):
            with self.subTest(key=key):
                self.assertEqual(params.PARAMS[key], value)
                self.assertEqual(params.UNITS[key], "tokens")
                with self.assertRaises(VqaError) as c:
                    params.effective({key: value + 1})
                self.assertEqual(c.exception.rc, 2)

    def test_temporal_schemas_describe_exact_frame_ids_and_both_hashes(self):
        for name in ("vqa.gop/v1", "vqa.episode/v1"):
            with self.subTest(name=name):
                properties = canon.load_schema(name)["properties"]
                self.assertEqual(properties["frame_ids"]["items"]["type"], "integer")
                self.assertTrue(properties["frame_ids"]["uniqueItems"])
                self.assertEqual(properties["gop_canon_sha256"]["pattern"], "^[0-9a-f]{64}$")
                self.assertEqual(properties["gop_blob_sha256"]["pattern"], "^[0-9a-f]{64}$")


if __name__ == "__main__":
    unittest.main()
