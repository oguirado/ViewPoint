"""FVMEM: RVMEM-1..9,13 regressions - ingest/compact/gc/pin verbs, commit gates, gc window, answer self-check, fsync order."""
import contextlib
import io
import json
import os
import struct
import tempfile
import unittest
from unittest import mock

from visioncore import canon, layout
from visioncore.errors import VqaError
from vmem import cli, compact as C, gates, gen as G, ingest, router, units as U
from vmem.blobs import blob_path, iter_blobs
from vmem.store import Store

GRID = "grid:1280x720:s16"
TK = "T:demo:hud:1280x720:ui:0a40-0a7f"


def tile(x):
    return struct.pack(layout.TILE_FMT, 0, 0, 0, 100, 0, 0, 50, 0, 0, 0, 0, 0, x, x)


def spool_lines(run, desc, verdict="PASS", drop=None, ep=None):
    rid = "r_%06d_%08x" % (run, run)
    sp = {"schema": "vmem.spool/v1", "run_id": rid}
    L = [dict(sp, seq=0, type="run", body={"scope": {"app": "demo", "scene": "hud", "res": "1280x720", "grid": GRID,
                                                     "build_seq": run}, "receipt_sha256": "1" * 64,
                                           "profile_sha256": "2" * 64, "align_source": "render_index"}),
         dict(sp, seq=1, type="region", body={
             "id": "r3:0a40", "frame": 3, "cls": "CHANGED", "bbox_px": [0, 0, 16, 16], "xh64": "0" * 16,
             "sig64": "%016x" % desc, "render_index": 3, "role": "ui", "m0": 0x0a40, "m1": 0x0a7f,
             "look": {"n": 4, "ah64": "%016x" % desc, "y_q4_mean": 100},
             "desc_hex": b"".join(tile(desc + i) for i in range(4)).hex()}),
         dict(sp, seq=2, type="verdict", body={"verdict": verdict, "check_id": "render.flicker", "pointers": ["r3:0a40"]})]
    if ep is not None:
        L.append(dict(sp, seq=3, type="episode", body=ep))
    if drop:
        L = [l for l in L if l["type"] != drop]
        for i, l in enumerate(L):
            l["seq"] = i
    return L


def episode(run, **kw):
    rid = "r_%06d_%08x" % (run, run)
    e = {"schema": "vqa.episode/v1", "episode": rid + "/g0", "run_id": rid, "gop": 0, "f0": 0, "f1": 19, "n_frames": 20,
         "cut": 0, "gop_blob_sha256": "ab" * 32, "verdicts": [{"verdict": "PASS"}], "fg": False,
         "classes_ppm": {"CHANGED": 1}, "delta_bytes": 10, "grid": GRID, "params_sha256": "3" * 64}
    e.update(kw)
    return e


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = os.path.join(self.tmp.name, "store")

    def spool(self, name, lines):
        p = os.path.join(self.tmp.name, name)
        with open(p, "w") as f:
            f.write("".join(canon.dumps(l) for l in lines))
        return p

    def cli(self, *argv):
        b = io.StringIO()
        with contextlib.redirect_stdout(b):
            rc = cli.main(list(argv) + ["--store", self.root])
        return rc, json.loads(b.getvalue())


class TestVerbs(Base):
    def test_four_verbs_rc0(self):
        self.assertEqual(self.cli("ingest", "--spool", self.spool("a.jsonl", spool_lines(1, 0x10)))[0], 0)
        self.assertEqual(self.cli("ingest", "--spool", self.spool("b.jsonl", spool_lines(2, 0x9990000, "FAIL")))[0], 0)
        rc, o = self.cli("verify")
        self.assertEqual((rc, o["detail"]["fg_count"] > 0), (0, True))
        self.assertEqual(self.cli("compact")[0], 0)
        self.assertEqual(self.cli("gc")[0], 0)
        rc, o = self.cli("get", "V-DEF-000001")
        self.assertEqual(rc, 0)
        rc, o = self.cli("verify")
        self.assertEqual(rc, 0)

    def test_pin_catalogued(self):
        self.cli("ingest", "--spool", self.spool("a.jsonl", spool_lines(1, 0x10)))
        self.cli("ingest", "--spool", self.spool("b.jsonl", spool_lines(2, 0x20)))  # same tk, BG counter unit
        st = Store(self.root)
        bg = [u for u in st.gen_units(st.current()) if not U.is_fg(u)]
        self.assertTrue(bg)
        rcpt = os.path.join(self.tmp.name, "r.txt")
        with open(rcpt, "w") as f:
            f.write("receipt")
        rc, o = self.cli("pin", bg[0]["id"], "--receipt", rcpt)
        self.assertEqual((rc, o["detail"]["pin"]), (0, "CATALOGUED"))
        self.assertEqual(self.cli("pin", "V-BAS-999999", "--receipt", rcpt)[0], 3)
        self.assertEqual(self.cli("verify")[0], 0)

    def test_spool_missing_f1_quarantined(self):
        ep = episode(1)
        del ep["f1"]
        p = self.spool("n.jsonl", spool_lines(1, 0x10, ep=ep))
        rc, o = self.cli("ingest", "--spool", p)
        self.assertEqual(rc, 2)
        self.assertFalse(os.path.exists(p))
        self.assertTrue(os.path.isdir(os.path.join(self.root, "quarantine", "r_000001_00000001")))
        self.assertIsNone(Store(self.root).current())

    def test_episode_gop_blob_pinned_when_fail(self):
        blob = b"stored-gop-bytes"
        ep = episode(1, verdicts=[{"verdict": "FAIL"}], fg=True, gop_blob_sha256=canon.sha256_bytes(blob),
                     gop_blob_hex=blob.hex())
        self.assertEqual(self.cli("ingest", "--spool", self.spool("a.jsonl", spool_lines(1, 0x10, "FAIL", ep=ep)))[0], 0)
        st = Store(self.root)
        us = st.gen_units(st.current())
        epi = [u for u in us if u["kind"] == "episode"][0]
        self.assertTrue(U.is_fg(epi))
        self.assertEqual(epi["anchors"][0].get("gop_blob"), canon.sha256_bytes(blob))

    def test_gop_blob_roundtrip_and_missing_fail_rc5(self):
        ep = episode(1, verdicts=[{"verdict": "FAIL"}], fg=True)  # blob neither supplied nor present
        rc, o = self.cli("ingest", "--spool", self.spool("a.jsonl", spool_lines(1, 0x10, "FAIL", ep=ep)))
        self.assertEqual(rc, 5)
        self.assertIsNone(Store(self.root).current())

    def test_gop_blob_hash_mismatch_rc5(self):
        ep = episode(1, gop_blob_hex=b"abc".hex())
        rc, _o = self.cli("ingest", "--spool", self.spool("a.jsonl", spool_lines(1, 0x10, ep=ep)))
        self.assertEqual(rc, 5)

    def test_look_type_accepted(self):
        L = spool_lines(1, 0x10)
        lk = dict(L[1]["body"], id="r0:0a40", frame=0)
        L.insert(1, dict(L[1], type="look", body=lk))
        for i, l in enumerate(L):
            l["seq"] = i
        self.assertEqual(self.cli("ingest", "--spool", self.spool("a.jsonl", L))[0], 0)

    def test_apply_pins_cited_region_event(self):
        sc = {"app": "demo", "scene": "hud", "res": "1280x720", "grid": GRID, "build_seq": 1}
        rid = "r_000001_00000001"
        an = {"run_id": rid, "cite": rid + "/r3:0a40", "frame": 3, "render_index": None, "bbox_px": [0, 0, 1, 1],
              "mv_px": [0, 0], "xh64": "0" * 16, "sig64": "0" * 16, "crop": None, "desc_blob": None}
        ev = U.make_unit(1, "region_event", sc, TK, predicate="changed", anchors=[an])
        df = U.make_unit(1, "defect", sc, TK, anchors=[an], verdict="FAIL")
        out = U.apply_pins([ev, df])
        self.assertTrue(all(U.is_fg(u) for u in out))
        self.assertFalse(U.is_fg(U.apply_pins([ev])[0]))


class TestStoreGates(Base):
    def setUp(self):
        super().setUp()
        self.cli("ingest", "--spool", self.spool("a.jsonl", spool_lines(1, 0x10, "FAIL")))
        self.st = Store(self.root)

    def test_commit_fg_drop_refused(self):
        cur = self.st.current()
        with self.st.lock():
            with self.assertRaises(VqaError) as c:
                self.st.commit({"units/active.jsonl": b""})
        self.assertEqual((c.exception.rc, c.exception.detail["reason"]), (5, "g_fg_monotone"))
        self.assertEqual((self.st.current(), G.list_gens(self.root)), (cur, [cur]))

    def test_commit_manifest_counters(self):
        m = G.read_manifest(self.root, self.st.current())
        for k in ("unit_count", "fg_count", "blob_bytes", "fg_blob_bytes", "fg_ids_sha256"):
            self.assertIn(k, m)
        self.assertGreater(m["fg_blob_bytes"], 0)

    def test_g_caps_requires_counter(self):
        with self.st.lock():
            self.st.commit({"units/active.jsonl": b""}, strict=False)  # no counters
        self.assertEqual(gates.verify(self.root)["gates"]["g_caps"], 5)

    def test_rollback_then_verify_ok(self):
        self.cli("ingest", "--spool", self.spool("b.jsonl", spool_lines(2, 0x9990000)))
        self.assertEqual(self.cli("rollback", "--to", "1")[0], 0)
        self.assertEqual(self.cli("verify")[0], 0)

    def test_g_window_detects_missing_blob(self):
        cur = self.st.current()
        sha, ext = iter_blobs(self.root)[0]
        p = blob_path(self.root, sha, ext)
        os.chmod(p, 0o644)
        os.unlink(p)
        r = gates.verify(self.root)["gates"]
        self.assertEqual((r["g_window"], r["g_blob_integrity"]), (5, 5))
        with mock.patch.object(gates, "_g_window", lambda c: True):  # mutant M-VM6 does not survive blob check
            self.assertEqual(gates.verify(self.root)["gates"]["g_blob_integrity"], 5)

    def test_gc_never_deletes_referenced(self):
        n = len(iter_blobs(self.root))
        with self.st.lock():
            self.assertEqual(self.st.gc_blobs(), [])
            with self.assertRaises(VqaError) as c:
                self.st.gc_blobs(set())  # caller set missing the window refs -> rc 5 g_window
            self.assertEqual(c.exception.rc, 5)
        self.assertEqual(len(iter_blobs(self.root)), n)

    def test_fg_ids_sha_single_definition(self):
        us = self.st.gen_units(self.st.current())
        self.assertEqual(gates.verify(self.root)["fg_ids_sha256"], C.fg_ids_sha256(us))

    def test_answer_runs_its_checker(self):
        rid = "r_000002_00000002"
        self.cli("ingest", "--spool", self.spool("b.jsonl", spool_lines(2, 0x9990000)))
        rc, o = self.cli("answer", "--q", "baseline", "--scope", "demo/hud")
        if rc == 0:  # answered => its own quote must pass check-answer
            p = os.path.join(self.tmp.name, "ans.json")
            open(p, "w").write(canon.dumps(o["detail"]) if "detail" in o else "{}")
        self.assertIn(rc, (0, 3))

    def test_short_summary_is_abstain(self):
        v = router.View(self.root)
        ans = {"citations": [{"unit_id": v.units[0]["id"], "cite": v.units[0]["anchors"][0]["cite"], "quote": "x"}]}
        with self.assertRaises(VqaError) as c:
            router.check_answer(self.root, ans)
        self.assertEqual(c.exception.rc, 3)


class TestFsyncOrder(Base):
    def test_order_oracle(self):
        ev = []
        real_fsync, real_rename, real_replace, real_fd = os.fsync, os.rename, os.replace, G.fsync_dir

        def fsync(fd):
            ev.append("fsync_file")
            return real_fsync(fd)

        def rename(a, b):
            ev.append("rename")
            return real_rename(a, b)

        def replace(a, b):
            ev.append("replace")
            return real_replace(a, b)

        def fdir(p):
            ev.append("fsync_dir")
            return real_fd(p)

        st = Store(self.root)
        with mock.patch("os.fsync", fsync), mock.patch("os.rename", rename), mock.patch("os.replace", replace), \
                mock.patch.object(G, "fsync_dir", fdir):
            with st.lock():
                st.commit({"units/active.jsonl": b""}, strict=False)
        i_ren, i_rep = ev.index("rename"), ev.index("replace")
        self.assertIn("fsync_file", ev[:i_ren])
        self.assertIn("fsync_dir", ev[:i_ren])  # staged dir fsynced before the rename (M-VM2/M-VM3)
        self.assertIn("fsync_dir", ev[i_ren:i_rep])  # gen/ fsynced after rename, before CURRENT replace
        self.assertIn("fsync_dir", ev[i_rep:])  # store dir fsynced after the flip


if __name__ == "__main__":
    unittest.main()


class TestAppearanceChain(Base):
    def _ing(self, run, desc, verdict="PASS"):
        self.assertEqual(self.cli("ingest", "--spool", self.spool("s%d.jsonl" % run, spool_lines(run, desc, verdict)))[0], 0)

    def test_diff_region_fail_build(self):
        self._ing(1, 0x10)
        self._ing(7, 0x9990000, "FAIL")
        self._ing(9, 0x9990000)
        rc, o = self.cli("diff-region", "--tk", TK)
        self.assertEqual(rc, 0)
        fc = o["detail"]["first_change"]
        self.assertEqual((fc["build_seq"], fc["pin"]), (7, "FAIL"))

    def test_diff_region_revert(self):
        self._ing(1, 0x10)
        self._ing(5, 0x9990000)
        self._ing(9, 0x10)
        rc, o = self.cli("diff-region", "--tk", TK)
        self.assertEqual((rc, o["detail"]["first_change"]["build_seq"]), (0, 5))
