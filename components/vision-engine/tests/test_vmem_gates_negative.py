import os
import tempfile
import unittest
from unittest import mock

from visioncore import canon, ids
from visioncore.errors import VqaError
from vmem import blobs, gates, store, units as U

SC = {"app": "op", "scene": "sc", "res": "1920x1080", "grid": "grid:1920x1080:s32", "build_seq": 7}
TK = U.track_key("op", "sc", 1920, 1080, "ui", 0x0a40, 0x0a7f)
RUN = "r_000007_77abe901"


class TestVmemGatesNegative(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = os.path.join(self.tmp.name, "store")
        self.s = store.Store(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def create_valid_store(self, extra_manifest=None):
        blob_bytes = b"sample_blob_data_for_testing"
        blob_sha = blobs.put_blob(self.root, blob_bytes, "bin")

        anc = {
            "run_id": RUN,
            "cite": RUN + "/f84:t0a40",
            "frame": 84,
            "render_index": 84,
            "bbox_px": [0, 0, 32, 32],
            "mv_px": [0, 0],
            "xh64": "0" * 16,
            "sig64": "f" * 16,
            "crop": blob_sha,
            "desc_blob": None,
        }
        u1 = U.make_unit(1, "baseline", dict(SC), TK, anchors=[anc], look=None)
        u2 = U.make_unit(2, "verdict", dict(SC), TK, pin="FAIL", verdict="FAIL", check_id="chk_01", anchors=[anc], look=None)

        units_list = [u1, u2]
        cat_content = U.render_catalogue(units_list)

        files = {
            "units/active.jsonl": U.dump_units(units_list).encode("utf-8"),
            "catalogue/foreground.tsv": cat_content.encode("utf-8"),
            "VISUAL_DIGEST.md": b"# Visual Digest\n",
        }

        m_extra = {"fg_count": 1, "fg_blob_bytes": len(blob_bytes)}
        if extra_manifest:
            m_extra.update(extra_manifest)

        with self.s.lock():
            res = self.s.commit(files, [], m_extra, strict=False)

        return u1, u2, blob_sha, res

    def test_valid_store_all_gates_pass(self):
        self.create_valid_store()
        res = gates.verify(self.root)
        self.assertEqual(gates.failed(res), [])
        for gate_name, rc in res["gates"].items():
            self.assertEqual(rc, 0, f"Gate {gate_name} failed unexpectedly")
        self.assertEqual(res["fg_count"], 1)
        self.assertEqual(res["gen"], 1)
        self.assertEqual(res["absent"], ["g_determinism", "g_cache_stable"])

    def test_g_schema_negative(self):
        u1, u2, blob_sha, _ = self.create_valid_store()
        ctx_bad = {"units": [dict(u1, schema="vmem.unit/v2")]}
        self.assertFalse(gates._g_schema(ctx_bad))

        u2_bad = dict(u2, schema="vmem.unit/v2")
        units_data = U.dump_units([u1, u2_bad]).encode("utf-8")
        cat_content = U.render_catalogue([u1, u2_bad])
        with self.s.lock():
            self.s.commit({
                "units/active.jsonl": units_data,
                "catalogue/foreground.tsv": cat_content.encode("utf-8"),
                "VISUAL_DIGEST.md": b"# Visual Digest\n",
            }, [], {"fg_count": 1, "fg_blob_bytes": 28}, strict=False)

        res = gates.verify(self.root)
        self.assertEqual(res["gates"]["g_schema"], 5)
        self.assertEqual(gates.failed(res), ["g_schema"])
        for k, v in res["gates"].items():
            if k != "g_schema":
                self.assertEqual(v, 0, f"Gate {k} failed unexpectedly")

    def test_g_nofloat_negative(self):
        u1, u2, _, _ = self.create_valid_store()
        snap_mock = mock.MagicMock()
        snap_mock.read.return_value = b'{"schema": "vmem.unit/v1", "val": 1.5}\n'
        ctx_bad = {"snap": snap_mock}
        self.assertFalse(gates._g_nofloat(ctx_bad))

        bad_jsonl = b'{"schema": "vmem.unit/v1", "val": 1.5}\n'
        with self.s.lock():
            self.s.commit({
                "units/active.jsonl": bad_jsonl,
                "catalogue/foreground.tsv": b"id\tkey\tpin\ttk\tfirst_run\tlast_run\tsummary\tcrop_sha8\n",
                "VISUAL_DIGEST.md": b"# Visual Digest\n",
            }, [], {"fg_count": 0, "fg_blob_bytes": 0}, strict=False)

        with self.assertRaises(VqaError) as cm:
            gates.verify(self.root)
        self.assertEqual(cm.exception.code, "E_GATE")
        self.assertEqual(cm.exception.rc, 5)
        self.assertEqual(cm.exception.detail["reason"], "g_nofloat")
        self.assertEqual(cm.exception.detail["cause"], "E_FLOAT")

    def test_g_ids_negative(self):
        u1, u2, blob_sha, _ = self.create_valid_store()
        self.assertFalse(gates._g_ids({"units": [dict(u1, id="invalid_unit_id")]}))
        self.assertFalse(gates._g_ids({"units": [dict(u1, tk=None)]}))

        anc_bad = dict(u1["anchors"][0], cite=RUN + "/invalid")
        u1_bad_cite = dict(u1, anchors=[anc_bad])
        self.assertFalse(gates._g_ids({"units": [u1_bad_cite]}))

        u1_no_tk = dict(u1, tk=None)
        units_list = [u1_no_tk, u2]
        units_data = U.dump_units(units_list).encode("utf-8")
        cat_content = U.render_catalogue(units_list)
        with self.s.lock():
            self.s.commit({
                "units/active.jsonl": units_data,
                "catalogue/foreground.tsv": cat_content.encode("utf-8"),
                "VISUAL_DIGEST.md": b"# Visual Digest\n",
            }, [], {"fg_count": 1, "fg_blob_bytes": 28}, strict=False)

        res = gates.verify(self.root)
        self.assertEqual(res["gates"]["g_ids"], 5)
        self.assertEqual(gates.failed(res), ["g_ids"])
        for k, v in res["gates"].items():
            if k != "g_ids":
                self.assertEqual(v, 0, f"Gate {k} failed unexpectedly")

    def test_g_layer_pin_negative(self):
        u1, u2, blob_sha, _ = self.create_valid_store()
        self.assertFalse(gates._g_layer_pin({"units": [dict(u1, layer="FG", pin=None)]}))
        self.assertFalse(gates._g_layer_pin({"units": [dict(u1, layer="BG", pin="FAIL")]}))

        ctx = {"units": [dict(u1, layer="FG", pin=None), u2]}
        with mock.patch("vmem.gates._g_schema", return_value=True):
            res = gates._g_layer_pin(ctx)
            self.assertFalse(res)

        u1_mismatch = dict(u1, layer="FG", pin=None)
        units_list = [u1_mismatch, u2]
        units_data = U.dump_units(units_list).encode("utf-8")
        cat_content = f"id\tkey\tpin\ttk\tfirst_run\tlast_run\tsummary\tcrop_sha8\n{u1['id']}\t{u1['key']}\t\t{u1['tk']}\t{RUN}\t{RUN}\tsummary\t\n{u2['id']}\t{u2['key']}\tFAIL\t{u2['tk']}\t{RUN}\t{RUN}\tsummary\t\n"
        with self.s.lock():
            self.s.commit({
                "units/active.jsonl": units_data,
                "catalogue/foreground.tsv": cat_content.encode("utf-8"),
                "VISUAL_DIGEST.md": b"# Visual Digest\n",
            }, [], {"fg_count": 1, "fg_blob_bytes": 28}, strict=False)

        with mock.patch("vmem.gates._g_schema", return_value=True):
            res = gates.verify(self.root)
            self.assertEqual(res["gates"]["g_layer_pin"], 5)
            self.assertEqual(gates.failed(res), ["g_layer_pin"])
            for k, v in res["gates"].items():
                if k != "g_layer_pin":
                    self.assertEqual(v, 0, f"Gate {k} failed unexpectedly")

    def test_g_catalogue_negative(self):
        u1, u2, _, _ = self.create_valid_store()
        units_data = U.dump_units([u1, u2]).encode("utf-8")
        bad_cat = "id\tkey\tpin\ttk\tfirst_run\tlast_run\tsummary\tcrop_sha8\n"
        with self.s.lock():
            self.s.commit({
                "units/active.jsonl": units_data,
                "catalogue/foreground.tsv": bad_cat.encode("utf-8"),
                "VISUAL_DIGEST.md": b"# Visual Digest\n",
            }, [], {"fg_count": 1, "fg_blob_bytes": 28}, strict=False)

        res = gates.verify(self.root)
        self.assertEqual(res["gates"]["g_catalogue"], 5)
        self.assertEqual(gates.failed(res), ["g_catalogue"])
        for k, v in res["gates"].items():
            if k != "g_catalogue":
                self.assertEqual(v, 0, f"Gate {k} failed unexpectedly")

    def test_g_blob_integrity_negative(self):
        u1, u2, blob_sha, _ = self.create_valid_store()
        blob_p = blobs.blob_path(self.root, blob_sha, "bin")
        os.chmod(blob_p, 0o644)
        with open(blob_p, "wb") as f:
            f.write(b"corrupted_blob_content")

        res = gates.verify(self.root)
        self.assertEqual(res["gates"]["g_blob_integrity"], 5)
        self.assertEqual(gates.failed(res), ["g_blob_integrity"])
        for k, v in res["gates"].items():
            if k != "g_blob_integrity":
                self.assertEqual(v, 0, f"Gate {k} failed unexpectedly")

    def test_g_cite_negative(self):
        u1, u2, _, _ = self.create_valid_store()
        self.assertFalse(gates._g_cite({"units": [dict(u2, anchors=[])]}))

        anc_bad_cite = dict(u2["anchors"][0], run_id="r_000007_00000000")
        u2_bad_cite = dict(u2, anchors=[anc_bad_cite])
        self.assertFalse(gates._g_cite({"units": [u2_bad_cite]}))

        units_list = [u1, u2_bad_cite]
        units_data = U.dump_units(units_list).encode("utf-8")
        cat_content = U.render_catalogue(units_list)
        with self.s.lock():
            self.s.commit({
                "units/active.jsonl": units_data,
                "catalogue/foreground.tsv": cat_content.encode("utf-8"),
                "VISUAL_DIGEST.md": b"# Visual Digest\n",
            }, [], {"fg_count": 1, "fg_blob_bytes": 28}, strict=False)

        res = gates.verify(self.root)
        self.assertEqual(res["gates"]["g_cite"], 5)
        self.assertEqual(gates.failed(res), ["g_cite"])
        for k, v in res["gates"].items():
            if k != "g_cite":
                self.assertEqual(v, 0, f"Gate {k} failed unexpectedly")

    def test_g_grid_negative(self):
        u1, u2, blob_sha, _ = self.create_valid_store()
        look_mismatch = {
            "grid": "grid:1280x720:s16",
            "n": 1,
            "ah64": "0" * 16,
            "desc_blob": blob_sha,
            "y_q4_mean": 100,
        }
        u1_bad_grid = dict(u1, look=look_mismatch)
        self.assertFalse(gates._g_grid({"units": [u1_bad_grid]}))

        units_list = [u1_bad_grid, u2]
        units_data = U.dump_units(units_list).encode("utf-8")
        cat_content = U.render_catalogue(units_list)
        with self.s.lock():
            self.s.commit({
                "units/active.jsonl": units_data,
                "catalogue/foreground.tsv": cat_content.encode("utf-8"),
                "VISUAL_DIGEST.md": b"# Visual Digest\n",
            }, [], {"fg_count": 1, "fg_blob_bytes": 28}, strict=False)

        res = gates.verify(self.root)
        self.assertEqual(res["gates"]["g_grid"], 5)
        self.assertEqual(gates.failed(res), ["g_grid"])
        for k, v in res["gates"].items():
            if k != "g_grid":
                self.assertEqual(v, 0, f"Gate {k} failed unexpectedly")

    def test_g_caps_negative(self):
        u1, u2, _, _ = self.create_valid_store()
        units_list = [u1, u2]
        units_data = U.dump_units(units_list).encode("utf-8")
        cat_content = U.render_catalogue(units_list)

        with self.s.lock():
            self.s.commit({
                "units/active.jsonl": units_data,
                "catalogue/foreground.tsv": cat_content.encode("utf-8"),
                "VISUAL_DIGEST.md": b"# " + b"a" * 6000,
            }, [], {"fg_count": 1, "fg_blob_bytes": 28}, strict=False)

        res = gates.verify(self.root)
        self.assertEqual(res["gates"]["g_caps"], 5)
        self.assertEqual(gates.failed(res), ["g_caps"])
        for k, v in res["gates"].items():
            if k != "g_caps":
                self.assertEqual(v, 0, f"Gate {k} failed unexpectedly")

    def test_g_privacy_and_verification(self):
        u1, u2, _, _ = self.create_valid_store()
        ctx = {"units": [u1, u2]}
        self.assertTrue(gates._g_privacy(ctx))

        with mock.patch("vmem.gates._g_privacy", side_effect=VqaError("E_GATE", {"reason": "g_privacy"})):
            res = gates.verify(self.root)
            self.assertEqual(res["gates"]["g_privacy"], 5)
            self.assertEqual(gates.failed(res), ["g_privacy"])
            for k, v in res["gates"].items():
                if k != "g_privacy":
                    self.assertEqual(v, 0, f"Gate {k} failed unexpectedly")

    def test_verify_gen_outside_window_rc3(self):
        self.create_valid_store()
        with self.assertRaises(VqaError) as cm:
            gates.verify(self.root, gen=999)
        self.assertEqual(cm.exception.code, "E_ID")
        self.assertEqual(cm.exception.rc, 3)

    def test_failed_utility(self):
        res1 = {"gates": {"g_schema": 0, "g_ids": 5, "g_caps": 5, "g_grid": 0}}
        self.assertEqual(gates.failed(res1), ["g_caps", "g_ids"])

        res2 = {"gates": {"g_schema": 0, "g_ids": 0}}
        self.assertEqual(gates.failed(res2), [])


if __name__ == "__main__":
    unittest.main()
