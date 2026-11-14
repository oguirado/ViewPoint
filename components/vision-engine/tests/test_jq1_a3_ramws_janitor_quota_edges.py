"""Adversarial tests for ramws janitor sweep state combinations and reserve atomicity/quota edge cases.

VM timings labeled VM, never MEASURED.
"""
import json
import os
import shutil
import types
import unittest
from pathlib import Path
from unittest import mock

from vadapt import ramws
from vadapt.ramws import RamError

ROOT = Path(ramws.ROOT) / "ut_jq1_a3_ramws"
TOOL = "jq1_a3_tool"


def sv(free):
    return lambda p: types.SimpleNamespace(f_bavail=free, f_frsize=1)


class TestJanitorQuotaEdges(unittest.TestCase):
    def setUp(self):
        if ramws.is_ram_fs("/dev/shm") is None:
            raise unittest.SkipTest("verified RAM storage /dev/shm unavailable")
        shutil.rmtree(ROOT, ignore_errors=True)
        self.ws_list = []

    def tearDown(self):
        for ws in self.ws_list:
            try:
                ws.close()
            except Exception:
                pass
        shutil.rmtree(ROOT, ignore_errors=True)

    def _open_ws(self, campaign="c", run_id="r1", quota=ramws.RUN_DEFAULT, **kwargs):
        ws = ramws.open_run(campaign, run_id, quota, tool=TOOL, root=str(ROOT), **kwargs)
        self.ws_list.append(ws)
        return ws

    # -------------------------------------------------------------------------
    # Reserve Atomicity Refusal Tests
    # -------------------------------------------------------------------------
    def test_reserve_atomicity_refusals(self):
        ws = self._open_ws(quota=10_000, statvfs=sv(ramws.FREE_MIN + 100_000))
        ws.reserve(1_000)
        self.assertEqual(ws.used, 1_000)
        self.assertEqual(ws.peak, 1_000)

        # 1. Refusal on n = -1
        with self.assertRaises(RamError) as c:
            ws.reserve(-1)
        self.assertEqual(c.exception.code, "E_RAM_QUOTA")
        self.assertEqual(ws.used, 1_000)
        self.assertEqual(ws.peak, 1_000)

        # 2. Refusal on n = True
        with self.assertRaises(RamError) as c:
            ws.reserve(True)
        self.assertEqual(c.exception.code, "E_RAM_QUOTA")
        self.assertEqual(ws.used, 1_000)
        self.assertEqual(ws.peak, 1_000)

        # 3. Refusal on n = False
        with self.assertRaises(RamError) as c:
            ws.reserve(False)
        self.assertEqual(c.exception.code, "E_RAM_QUOTA")
        self.assertEqual(ws.used, 1_000)
        self.assertEqual(ws.peak, 1_000)

        # 4. Refusal on float
        with self.assertRaises(RamError) as c:
            ws.reserve(500.0)
        self.assertEqual(c.exception.code, "E_RAM_QUOTA")
        self.assertEqual(ws.used, 1_000)
        self.assertEqual(ws.peak, 1_000)

        # 5. Refusal on run quota overflow (used 1,000 + 9,001 > quota 10,000)
        with self.assertRaises(RamError) as c:
            ws.reserve(9_001)
        self.assertEqual(c.exception.code, "E_RAM_QUOTA")
        self.assertEqual(ws.used, 1_000)
        self.assertEqual(ws.peak, 1_000)

        # 6. Refusal on HOST_MAX overflow
        with mock.patch.object(ramws, "_host_used", return_value=ramws.HOST_MAX - 1_500):
            with self.assertRaises(RamError) as c:
                ws.reserve(600)  # 1500 - 1000 = 500 max allowed, 600 overflows HOST_MAX
            self.assertEqual(c.exception.code, "E_RAM_QUOTA")
            self.assertEqual(ws.used, 1_000)
            self.assertEqual(ws.peak, 1_000)

        # 7. Refusal on FREE_MIN floor violation
        ws_low_free = self._open_ws(campaign="c", run_id="r_low_free", quota=10_000, statvfs=sv(ramws.FREE_MIN + 500))
        ws_low_free.reserve(200)
        self.assertEqual(ws_low_free.used, 200)
        self.assertEqual(ws_low_free.peak, 200)
        with self.assertRaises(RamError) as c:
            ws_low_free.reserve(501)  # statvfs free (FREE_MIN + 500) - 501 < FREE_MIN
        self.assertEqual(c.exception.code, "E_RAM_QUOTA")
        self.assertEqual(ws_low_free.used, 200)
        self.assertEqual(ws_low_free.peak, 200)

    # -------------------------------------------------------------------------
    # Pin Boundary Acceptance Tests
    # -------------------------------------------------------------------------
    def test_pin_boundary_acceptance(self):
        # Boundary 1: Exact run quota boundary
        ws1 = self._open_ws(campaign="c", run_id="b_quota", quota=10_000, statvfs=sv(ramws.FREE_MIN + 100_000))
        ws1.reserve(4_000)
        ws1.reserve(6_000)  # exact quota 10,000
        self.assertEqual(ws1.used, 10_000)
        self.assertEqual(ws1.peak, 10_000)

        # Boundary 2: Exact HOST_MAX boundary
        ws2 = self._open_ws(campaign="c", run_id="b_host", quota=1_000_000, statvfs=sv(ramws.FREE_MIN + 1_000_000))
        ws2.reserve(1_000)
        with mock.patch.object(ramws, "_host_used", return_value=ramws.HOST_MAX - 5_000):
            # _host_used (HOST_MAX - 5000) + used (1000) + n (4000) == HOST_MAX
            ws2.reserve(4_000)
            self.assertEqual(ws2.used, 5_000)
            self.assertEqual(ws2.peak, 5_000)

        # Boundary 3: Exact FREE_MIN floor boundary
        ws3 = self._open_ws(campaign="c", run_id="b_freemin", quota=10_000, statvfs=sv(ramws.FREE_MIN + 2_000))
        ws3.reserve(500)
        # statvfs free (FREE_MIN + 2000) - 1500 == FREE_MIN + 500
        # to reach exact FREE_MIN floor, reserve 1500 when free is FREE_MIN + 2000: (FREE_MIN + 2000) - 2000 = FREE_MIN
        ws3.reserve(1_500)  # free remaining after n=1500 is FREE_MIN + 2000 - 1500 = FREE_MIN + 500
        # Let's reserve another 500 to hit exact FREE_MIN floor: (FREE_MIN + 2000) - 2000 == FREE_MIN
        ws3.reserve(500)
        self.assertEqual(ws3.used, 2_500)
        self.assertEqual(ws3.peak, 2_500)

    # -------------------------------------------------------------------------
    # Orphan Run Directories and Janitor Sweep Combinations
    # -------------------------------------------------------------------------
    def test_sweep_orphan_missing_owner_unlocked_lock_young(self):
        # Missing owner.json with unlocked .lock:
        # Must survive when age < 5 seconds.
        run_dir = ROOT / TOOL / "c_orphan" / "r_no_owner_young"
        run_dir.mkdir(parents=True)
        lock_file = run_dir / ".lock"
        lock_file.write_bytes(b"")

        now_time = 1000.0
        os.utime(run_dir, (now_time, now_time))
        with mock.patch.object(ramws.time, "time", return_value=now_time + 2.0):  # age = 2.0s (< 5s)
            gone = ramws.sweep(TOOL, root=str(ROOT))
            self.assertEqual(gone, [])
            self.assertTrue(run_dir.exists())

    def test_sweep_orphan_missing_owner_unlocked_lock_old(self):
        # Missing owner.json with unlocked .lock:
        # Must be removed when age >= 5 seconds.
        run_dir = ROOT / TOOL / "c_orphan" / "r_no_owner_old"
        run_dir.mkdir(parents=True)
        lock_file = run_dir / ".lock"
        lock_file.write_bytes(b"")

        now_time = 1000.0
        os.utime(run_dir, (now_time, now_time))
        with mock.patch.object(ramws.time, "time", return_value=now_time + 10.0):  # age = 10.0s (>= 5s)
            gone = ramws.sweep(TOOL, root=str(ROOT))
            self.assertEqual(gone, ["c_orphan/r_no_owner_old"])
            self.assertFalse(run_dir.exists())

    def test_sweep_orphan_missing_lock_valid_current_boot_owner(self):
        # Missing .lock with valid current-boot owner must survive.
        run_dir = ROOT / TOOL / "c_orphan" / "r_no_lock_valid_owner"
        run_dir.mkdir(parents=True)
        owner = {
            "pid": os.getpid(),
            "starttime": ramws._starttime(),
            "boot_id": ramws._boot_id(),
            "tool": TOOL,
            "uid": os.getuid(),
        }
        (run_dir / "owner.json").write_text(json.dumps(owner), encoding="utf-8")

        gone = ramws.sweep(TOOL, root=str(ROOT))
        self.assertEqual(gone, [])
        self.assertTrue(run_dir.exists())

    def test_sweep_orphan_stale_boot_id_removed(self):
        # Missing .lock or unlocked .lock with stale boot_id "old-boot" must be removed.
        run_dir_nolock = ROOT / TOOL / "c_orphan" / "r_stale_nolock"
        run_dir_nolock.mkdir(parents=True)
        owner_stale = {
            "pid": 1234,
            "starttime": 100,
            "boot_id": "old-boot",
            "tool": TOOL,
            "uid": os.getuid(),
        }
        (run_dir_nolock / "owner.json").write_text(json.dumps(owner_stale), encoding="utf-8")

        run_dir_unlocked = ROOT / TOOL / "c_orphan" / "r_stale_unlocked"
        run_dir_unlocked.mkdir(parents=True)
        (run_dir_unlocked / ".lock").write_bytes(b"")
        (run_dir_unlocked / "owner.json").write_text(json.dumps(owner_stale), encoding="utf-8")

        gone = ramws.sweep(TOOL, root=str(ROOT))
        self.assertEqual(sorted(gone), ["c_orphan/r_stale_nolock", "c_orphan/r_stale_unlocked"])
        self.assertFalse(run_dir_nolock.exists())
        self.assertFalse(run_dir_unlocked.exists())


if __name__ == "__main__":
    unittest.main()
