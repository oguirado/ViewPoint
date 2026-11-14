"""RT1-L0: bench refusals and JSON schema. Tiny runs only (256x160 is below the synth minimum for res table, so a patched RES)."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
import uuid
from unittest import mock

from eval import rt_bench
from vadapt import ramws

KEYS = {"res", "cores", "stage", "median_us", "p95_us", "load1", "cpus"}


class Refusals(unittest.TestCase):
    def test_cpu_outside_5_9_refused(self):
        for aff in ({0}, {5, 10}, {14}, set()):
            with self.assertRaises(rt_bench.BenchError) as c:
                rt_bench.check_cpus(aff, 1)
            self.assertEqual(c.exception.code, "E_RT_CPU")

    def test_cores_beyond_affinity_refused(self):
        with self.assertRaises(rt_bench.BenchError) as c:
            rt_bench.check_cpus({5}, 5)
        self.assertEqual(c.exception.code, "E_RT_CPU")
        self.assertEqual(rt_bench.check_cpus({7, 5, 6, 8, 9}, 5), [5, 6, 7, 8, 9])

    def test_bench_refuses_cpu_before_any_work(self):
        with self.assertRaises(rt_bench.BenchError):
            rt_bench.bench("720p", 1, 20, ["gpu_jitter"], affinity={0})

    def test_no_ramws_root_refused(self):
        with tempfile.TemporaryDirectory(dir=os.getcwd()) as d:   # build tree = disk, not tmpfs
            with self.assertRaises(ramws.RamError) as c:
                rt_bench.bench("720p", 1, 20, ["gpu_jitter"], ram_root=d, affinity={5})
            self.assertTrue(c.exception.code.startswith("E_RAM_"))

    def test_cli_rc_2_on_refusal(self):
        with tempfile.TemporaryDirectory(dir=os.getcwd()) as d:
            self.assertEqual(rt_bench.main(["--res", "720p", "--frames", "20", "--ram-root", d]), 2)


class Schema(unittest.TestCase):
    def test_rows_schema_and_ram_only(self):
        tool = mock.patch.object(rt_bench, "TOOL", "rt_bench_" + uuid.uuid4().hex)
        tool.start()
        self.addCleanup(tool.stop)
        orig = dict(rt_bench.RES)
        rt_bench.RES["tiny"] = (320, 200)
        self.addCleanup(lambda: (rt_bench.RES.pop("tiny"), rt_bench.RES.update(orig)))
        aff = os.sched_getaffinity(0)
        os.sched_setaffinity(0, {sorted(aff)[0]})
        self.addCleanup(os.sched_setaffinity, 0, aff)
        # affinity override is checked against 5-9; the test patches the allow-list, not the check
        old = rt_bench.ALLOWED_CPUS
        rt_bench.ALLOWED_CPUS = frozenset(range(0, 64))
        self.addCleanup(setattr, rt_bench, "ALLOWED_CPUS", old)
        def snap():
            return sorted(os.path.join(r, f) for r, _, fs in os.walk("build") for f in fs)
        before = snap()
        rows = rt_bench.bench("tiny", 1, 20, ["gpu_jitter"], affinity={sorted(aff)[0]})
        self.assertEqual({r["stage"] for r in rows}, set(rt_bench.STAGES))
        for r in rows:
            self.assertTrue(KEYS <= set(r), r)
            self.assertIsInstance(r["median_us"], int)
            self.assertLessEqual(r["median_us"], r["p95_us"] + 1)
        json.dumps(rows)
        self.assertEqual(snap(), before)   # frames lived in the ramws run only
        self.assertFalse(os.path.exists(os.path.join(ramws.ROOT, rt_bench.TOOL, rt_bench.CAMPAIGN)))

    def test_x5_check_absent_is_none(self):
        self.assertIsNone(rt_bench.x5_check([]))


class _Tiny(unittest.TestCase):
    def setUp(self):
        tool = mock.patch.object(rt_bench, "TOOL", "rt_bench_" + uuid.uuid4().hex)
        tool.start()
        self.addCleanup(tool.stop)
        orig = dict(rt_bench.RES)
        rt_bench.RES["tiny"] = (320, 200)
        self.addCleanup(lambda: (rt_bench.RES.pop("tiny"), rt_bench.RES.update(orig)))
        old = rt_bench.ALLOWED_CPUS
        rt_bench.ALLOWED_CPUS = frozenset(range(0, 64))
        self.addCleanup(setattr, rt_bench, "ALLOWED_CPUS", old)
        aff = os.sched_getaffinity(0)
        self.addCleanup(os.sched_setaffinity, 0, aff)
        self.cpus = sorted(old)[:2]  # use the benchmark CPU allow-list; widen after peer tests
        os.sched_setaffinity(0, set(self.cpus))


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class ThreadEnv(unittest.TestCase):
    def test_needs_reexec_cases(self):
        ok = {k: "1" for k in rt_bench.THREAD_VARS}
        self.assertFalse(rt_bench.needs_reexec(ok, preloaded=False))
        self.assertTrue(rt_bench.needs_reexec(dict(ok, OMP_NUM_THREADS="4"), preloaded=False))
        self.assertTrue(rt_bench.needs_reexec({}, preloaded=False))
        self.assertTrue(rt_bench.needs_reexec(ok, preloaded=True))          # numpy imported before the env was set
        self.assertFalse(rt_bench.needs_reexec(dict(ok, RT_BENCH_REEXEC="1"), preloaded=True))   # no re-exec loop

    def test_env_set_before_numpy_import(self):
        code = ("import os;[os.environ.pop(k,None) for k in ('OMP_NUM_THREADS','OPENBLAS_NUM_THREADS','MKL_NUM_THREADS')];"
                "import eval.rt_bench as b;print(b.NUMPY_PRELOADED,[os.environ[k] for k in b.THREAD_VARS])")
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120, cwd=ROOT)
        self.assertEqual(out.stdout.strip(), "False ['1', '1', '1']", out.stderr)

    def test_preloaded_numpy_detected(self):
        code = "import numpy;import eval.rt_bench as b;print(b.NUMPY_PRELOADED, b.needs_reexec())"
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120, cwd=ROOT)
        self.assertEqual(out.stdout.strip(), "True True", out.stderr)


class Provenance(_Tiny):
    def test_one_main_call_emits_method_argv_per_row(self):
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "o.json")
            argv = ["--res", "tiny", "--cores", "1,2", "--frames", "16", "--scenario", "gpu_jitter", "--out", out]
            os.sched_setaffinity(0, set(self.cpus))
            self.assertEqual(rt_bench.main(argv), 0)
            with open(out) as fh:
                doc = json.load(fh)
        self.assertEqual(doc["argv"], argv)
        self.assertTrue(doc["method"])
        self.assertEqual({r["cores"] for r in doc["rows"]}, {1, 2})      # both core counts from one call
        for r in doc["rows"]:
            self.assertEqual(r["argv"], argv)
            self.assertEqual(r["method"], doc["method"])
            self.assertIsInstance(r["loaded_host"], bool)
            self.assertIn("threads_env_ok", r)

    def test_loaded_host_flag_follows_load1(self):
        real = os.getloadavg
        self.addCleanup(setattr, os, "getloadavg", real)
        os.getloadavg = lambda: (5.0, 5.0, 5.0)
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "o.json")
            os.sched_setaffinity(0, {self.cpus[0]})
            self.assertEqual(rt_bench.main(["--res", "tiny", "--frames", "16", "--scenario", "gpu_jitter", "--out", out]), 0)
            with open(out) as fh:
                doc = json.load(fh)
        self.assertTrue(doc["loaded_host"])
        self.assertTrue(all(r["loaded_host"] for r in doc["rows"]))


class MultiCore(_Tiny):
    def test_fork_row_frame0_excluded_pad_not_doubled(self):
        n = 16
        # CARD_D02F: both sealed S2 and RT0D02 fail the sum-of-medians
        # tolerance under scheduling jitter. Test the accounting with a clock
        # whose parallel pass costs 16.5 ms, including 1.5 ms of FORK;
        # each consumer interval is 10 us. Real frame processing still runs.
        ticks = iter([0, 16_500_000] + [16_500_000 + k * 10_000 for k in range(1, 7 * n + 4)])
        with mock.patch.object(rt_bench.time, "perf_counter_ns", side_effect=lambda: next(ticks)), \
                mock.patch.object(rt_bench, "fork_cost_us", return_value=1500), \
                mock.patch.object(rt_bench, "summarize", wraps=rt_bench.summarize) as summary:
            rows = rt_bench.bench("tiny", 2, n, ["clean_static"], affinity=set(self.cpus))
        samples = summary.call_args.args[0][0]
        for stage in rt_bench.STAGES:
            self.assertEqual(len(samples[stage]), n - 1, stage)
        self.assertEqual(samples["FORK"], [1500])
        self.assertEqual(samples["S0"], [10] * (n - 1))  # exclude the pad interval
        self.assertEqual(samples["S1"], [1000] * (n - 1))  # FORK removed, frame 0 untimed
        self.assertEqual(samples["S0-S4"], [1040] * (n - 1))
        by = {r["stage"]: r for r in rows}
        self.assertIn("FORK", by)
        self.assertEqual(by["FORK"]["unit"], "us_per_pass")
        self.assertGreater(by["FORK"]["median_us"], 0)
        self.assertTrue(by["S1"]["parallel"])
        self.assertFalse(by["S2"]["parallel"])
        self.assertEqual(by["S1"]["frames"], n - 1)
        parts = sum(by[s]["median_us"] for s in ("S0", "S1", "S1_rest", "S2", "S1b", "S3S4"))
        self.assertEqual(parts, by["S0-S4"]["median_us"])  # FORK is not part of S0-S4

    def test_one_core_has_no_fork_row(self):
        rows = rt_bench.bench("tiny", 1, 16, ["gpu_jitter"], affinity={self.cpus[0]})
        self.assertNotIn("FORK", {r["stage"] for r in rows})
        self.assertFalse(any(r["parallel"] for r in rows))

    def test_frame0_split_keeps_tiles_identical(self):
        import numpy as np
        from visioncore import parallel
        from visioncore.grid import Grid
        from visioncore import params as P
        g = Grid(320, 200)
        fr = np.random.RandomState(3).randint(0, 255, (5, 200, 320, 3)).astype(np.uint8)
        a = parallel.describe_many(fr[:1], g, 1, None, dict(P.PARAMS), lazy_xh=True)
        b = parallel.describe_many(fr[1:], g, 2, self.cpus, dict(P.PARAMS), lazy_xh=True)
        full = parallel.describe_many(fr, g, 1, None, dict(P.PARAMS), lazy_xh=True)
        self.assertEqual(a.tobytes() + b.tobytes(), full.tobytes())


if __name__ == "__main__":
    unittest.main()
