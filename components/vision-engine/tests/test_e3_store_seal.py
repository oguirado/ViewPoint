"""W4 V1: failing-first durability, prepublication and archived-evidence checks."""
import copy
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from visioncore import canon
from visioncore.errors import VqaError
from vmem import blobs, gates, gen as G, ingest, router, units as U
from vmem.store import Store
from tests.test_vmem_fixes import episode, spool_lines

SCRATCH = Path(__file__).resolve().parents[1] / "build" / "room_V1"


class StoreSeal(unittest.TestCase):
    def setUp(self):
        SCRATCH.mkdir(parents=True, exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=SCRATCH)
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "store"
        self.st = Store(self.root)

    def seed(self):
        spool = Path(self.tmp.name) / "seed.jsonl"
        spool.write_text("".join(canon.dumps(r) for r in spool_lines(1, 0x10, "FAIL")))
        ingest.ingest(self.root, str(spool))
        return self.st.gen_units(self.st.current(), strict=True)

    def assert_refused(self, files, reason):
        before = self.st.current(), G.list_gens(self.root)
        with self.st.lock(), self.assertRaises(VqaError) as cm:
            self.st.commit(files)
        self.assertEqual((cm.exception.rc, cm.exception.detail["reason"]), (5, reason))
        self.assertEqual((self.st.current(), G.list_gens(self.root)), before)

    def test_blob_partial_writes_complete(self):
        real = os.write
        def short(fd, data):
            return real(fd, data[:max(1, len(data) // 3)])
        data = b"blob payload" * 20
        with mock.patch("vmem.blobs.os.write", side_effect=short):
            sha = blobs.put_blob(self.root, data)
        self.assertEqual(blobs.get_blob(self.root, sha), data)

    def test_generation_partial_writes_complete(self):
        real = os.write
        def short(fd, data):
            return real(fd, data[:max(1, len(data) // 3)])
        dest = Path(self.tmp.name) / "staged" / "record"
        with mock.patch("vmem.gen.os.write", side_effect=short):
            G._write_file(str(dest), b"generation payload" * 20)
        self.assertEqual(dest.read_bytes(), b"generation payload" * 20)

    def test_zero_write_never_publishes_blob(self):
        with mock.patch("vmem.blobs.os.write", return_value=0):
            with self.assertRaises((OSError, VqaError)):
                blobs.put_blob(self.root, b"payload")
        self.assertEqual(blobs.iter_blobs(self.root), [])

    def test_existing_corrupt_blob_not_silently_reused(self):
        data = b"payload"
        sha = blobs.put_blob(self.root, data)
        path = Path(blobs.blob_path(self.root, sha, "bin"))
        path.chmod(0o644)
        path.write_bytes(b"corrupt")
        with self.assertRaises(VqaError) as cm:
            blobs.put_blob(self.root, data)
        self.assertEqual(cm.exception.rc, 5)

    def test_fg_drop_without_active_file_refused(self):
        self.seed()
        self.assert_refused({"VISUAL_DIGEST.md": b"missing units\n"}, "g_fg_monotone")

    def test_grid_gate_before_publication(self):
        units = self.seed()
        units = copy.deepcopy(units)
        added = dict(copy.deepcopy(next(u for u in units if u["look"])),
                     id="V-EVT-000099", layer="BG", pin=None)
        added["look"]["grid"] = "grid:1920x1080:s32"
        units.append(added)
        files = {"units/active.jsonl": U.dump_units(units).encode(),
                 "catalogue/foreground.tsv": U.render_catalogue(units).encode()}
        self.assert_refused(files, "g_grid")

    def test_fg_evidence_edit_refused(self):
        units = self.seed()
        units = copy.deepcopy(units)
        units[0]["summary"] = "rewritten foreground"
        files = {"units/active.jsonl": U.dump_units(units).encode(),
                 "catalogue/foreground.tsv": U.render_catalogue(units).encode()}
        self.assert_refused(files, "g_fg_monotone")

    def test_corrupt_reference_refused_before_flip(self):
        units = self.seed()
        sha, ext = blobs.iter_blobs(self.root)[0]
        path = Path(blobs.blob_path(self.root, sha, ext))
        path.chmod(0o644)
        path.write_bytes(b"corrupt")
        files = {"units/active.jsonl": U.dump_units(units).encode(),
                 "catalogue/foreground.tsv": U.render_catalogue(units).encode()}
        self.assert_refused(files, "g_blob_integrity")

    def test_uncitable_fg_refused_before_flip(self):
        units = self.seed()
        added = dict(copy.deepcopy(next(u for u in units if u["kind"] == "defect")),
                     id="V-DEF-000099", anchors=[])
        units.append(added)
        self.assert_refused({"units/active.jsonl": U.dump_units(units).encode(),
                             "catalogue/foreground.tsv": U.render_catalogue(units).encode()}, "g_cite")

    def test_computed_counters_cannot_be_overridden(self):
        units = self.seed()
        with self.st.lock():
            result = ingest.write_gen(self.st, units, [], extra={"fg_blob_bytes": 0, "unit_count": 0})
        manifest = G.read_manifest(self.root, result["gen"])
        self.assertEqual(manifest["unit_count"], len(units))
        self.assertGreater(manifest["fg_blob_bytes"], 0)

    def test_archived_fg_counts_and_missing_evidence_checked(self):
        units = self.seed()
        with self.st.lock():
            ingest.write_gen(self.st, [], units)
        result = gates.verify(self.root)
        self.assertEqual(result["fg_count"], len([u for u in units if U.is_fg(u)]))
        self.assertEqual(gates.failed(result), [])
        sha, ext = blobs.iter_blobs(self.root)[0]
        Path(blobs.blob_path(self.root, sha, ext)).unlink()
        self.assertEqual(gates.verify(self.root)["gates"]["g_blob_integrity"], 5)

    def test_gc_refuses_unacknowledged_current(self):
        self.seed()
        orphan = blobs.put_blob(self.root, b"unreferenced")
        with self.st.lock():
            with mock.patch.object(self.st.journal, "records", return_value=[]):
                with self.assertRaises(VqaError) as cm:
                    self.st.gc_blobs()
        self.assertEqual((cm.exception.rc, cm.exception.detail["reason"]), (5, "g_window"))
        self.assertTrue(Path(blobs.blob_path(self.root, orphan, "bin")).exists())

    def test_static_look_contract_and_changed_build(self):
        tk = "T:demo:hud:1280x720:ui:0000-0003"
        for run, appearance in ((1, 0x10), (2, 0x9990000)):
            records = spool_lines(run, appearance)
            region = records[1]["body"]
            sha = blobs.put_blob(self.root, bytes.fromhex(region["desc_hex"]))
            look = dict(region["look"], grid=records[0]["body"]["scope"]["grid"], desc_blob=sha)
            body = {k: region[k] for k in ("frame", "render_index", "role", "m0", "m1")}
            body.update(m0=0, m1=3)
            body.update(look=look, xh64=["%016x" % (appearance + i) for i in range(4)],
                        sig64=["%016x" % (appearance + i) for i in range(4)])
            records[1] = dict(records[1], type="look", body=body)
            records[2]["body"]["pointers"] = ["r3:0000"]
            spool = Path(self.tmp.name) / ("look%d.jsonl" % run)
            spool.write_text("".join(canon.dumps(r) for r in records))
            ingest.ingest(self.root, str(spool))
        self.assertEqual(router.diff_region(self.root, tk)["first_change"]["build_seq"], 2)
        baselines = [u for u in self.st.gen_units(self.st.current()) if u["pin"] == "GOLDEN"]
        self.assertTrue(baselines)
        self.assertTrue(all(u["verdict"] == "PASS" for u in baselines))

    def test_missing_run_field_with_extension_quarantined(self):
        records = spool_lines(1, 0x10)
        del records[0]["body"]["align_source"]
        records[0]["body"]["extension"] = True
        spool = Path(self.tmp.name) / "bad.jsonl"
        spool.write_text("".join(canon.dumps(r) for r in records))
        with self.assertRaises(VqaError) as cm:
            ingest.ingest(self.root, str(spool))
        self.assertEqual(cm.exception.rc, 2)
        self.assertFalse(spool.exists())
        self.assertIsNone(self.st.current())

    def test_per_gop_fail_does_not_skip_pass_golden(self):
        records = spool_lines(1, 0x10)
        later = spool_lines(1, 0x9990000, "FAIL")[1]
        later["body"].update(id="r23:0a40", frame=23, render_index=23)
        records.append(later)
        records.append(dict(records[2], body={"verdict": "FAIL", "check_id": "render.flicker",
                                              "pointers": ["r23:0a40"]}))
        for gop, verdict in ((0, "PASS"), (1, "FAIL")):
            data = ("gop%d" % gop).encode()
            ep = episode(1, episode="r_000001_00000001/g%d" % gop, gop=gop,
                         f0=gop * 20, f1=gop * 20 + 19, verdicts=[{"verdict": verdict}],
                         gop_blob_sha256=canon.sha256_bytes(data), gop_blob_hex=data.hex())
            records.append(dict(records[0], type="episode", body=ep))
        for seq, record in enumerate(records):
            record["seq"] = seq
        spool = Path(self.tmp.name) / "mixed.jsonl"
        spool.write_text("".join(canon.dumps(r) for r in records))
        ingest.ingest(self.root, str(spool))
        units = self.st.gen_units(self.st.current(), strict=True)
        golden = [u for u in units if u["pin"] == "GOLDEN"]
        self.assertEqual([(u["anchors"][0]["frame"], u["verdict"]) for u in golden], [(3, "PASS")])
        self.assertTrue(any(u["pin"] == "FAIL" and u["kind"] == "region_event"
                            and u["anchors"][0]["frame"] == 23 for u in units))


if __name__ == "__main__":
    unittest.main()
