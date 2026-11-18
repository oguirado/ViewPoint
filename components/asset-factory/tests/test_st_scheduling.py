"""Focused correctness tests for the optional S-TORO test scheduler."""
import copy
import hashlib
import json
import signal
import sys
import time
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tools.testing import impact, shard


def digest(data):
    return hashlib.sha256(data.encode()).hexdigest()


class SchedulingTests(unittest.TestCase):
    def setUp(self):
        self.roster = ["affected", "other", "skipped"]
        self.inputs = {p: digest(p) for p in
                       ("test_a.py", "test_b.py", "skip.py", "loader.py", "fixture.json",
                        "shape.schema.json")}
        self.toolchain = digest("python and external tools")
        self.graph = {
            "schema": "af.test-impact/v1", "inputs": self.inputs.copy(),
            "toolchain_sha256": self.toolchain,
            "dependencies": {"test_a.py": ["loader.py"], "loader.py": ["fixture.json"],
                             "fixture.json": ["shape.schema.json"], "shape.schema.json": [],
                             "test_b.py": [], "skip.py": []},
            "observed_inputs": {"affected": ["test_a.py", "loader.py", "fixture.json",
                                              "shape.schema.json"],
                                "other": ["test_b.py"], "skipped": ["skip.py"]},
            "roots": {"affected": ["test_a.py"], "other": ["test_b.py"], "skipped": ["skip.py"]},
            "prior": {"affected": "ok", "other": "ok", "skipped": "skip"},
        }
        self.pin = impact.graph_digest(self.graph)

    def selected(self, inputs, graph=None, runtime=None):
        return impact.select(self.roster, inputs, runtime or self.toolchain,
                             self.graph if graph is None else graph, self.pin)[0]

    def test_transitive_fixture_schema_code_and_prior_skip(self):
        for name in ("shape.schema.json", "fixture.json", "loader.py", "test_a.py"):
            with self.subTest(name=name):
                self.assertEqual(self.selected({**self.inputs, name: digest("changed")}),
                                 ["affected", "skipped"])

    def test_stale_omission_falls_back_and_seed_is_observed(self):
        omitted = copy.deepcopy(self.graph)
        omitted["dependencies"]["loader.py"].clear()
        inputs = {**self.inputs, "fixture.json": digest("bad")}
        plan = self.selected(inputs, omitted)
        self.assertEqual(plan, self.roster)
        def exercise(ids):
            return [name for name in ids if name == "affected" and
                    inputs["fixture.json"] != self.graph["inputs"]["fixture.json"]]
        self.assertEqual(exercise(plan), ["affected"])
        self.assertEqual(exercise(plan), exercise(self.roster))

    def test_resealed_omission_cannot_hide_recorded_reader(self):
        self.graph["dependencies"]["test_a.py"].clear()
        self.graph["roots"]["other"].append("loader.py")
        self.pin = impact.graph_digest(self.graph)
        self.assertIn("affected", self.selected({**self.inputs, "fixture.json": digest("seed")}))

    def test_unknown_inventory_and_toolchain_run_full(self):
        self.assertEqual(self.selected({**self.inputs, "new": digest("new")}), self.roster)
        self.assertEqual(self.selected(self.inputs, runtime=digest("new toolchain")), self.roster)
        self.assertEqual(self.selected({}), self.roster)
        self.assertEqual(impact.select(self.roster, self.inputs, None, None, None)[0], self.roster)

    def test_shard_ties_permutation_and_missing_duration(self):
        values = {"c": 8, "a": 8, "b": 6, "d": 2}
        plan = shard.partition(list(values), values, 2)
        self.assertEqual(plan, [["a", "b"], ["c", "d"]])
        self.assertEqual(plan, shard.partition(sorted(values), values, 2))
        self.assertEqual(sorted(sum(shard.partition(["a", "new", "b"], values, 2), [])),
                         ["a", "b", "new"])
        with self.assertRaises(ValueError):
            shard.partition(["a", "a"], values, 2)

    def test_invalid_worker_or_weight_cannot_produce_plan(self):
        for workers in (0, 17, True):
            with self.subTest(workers=workers), self.assertRaises(ValueError):
                shard.partition(["one"], {}, workers)
        with self.assertRaises(ValueError):
            shard.partition(["one"], {"one": -1}, 1)
        self.assertEqual(shard.partition(["aa", "bb"], {}, 16), [["aa"], ["bb"]])

    def test_missing_duplicate_crossed_and_cancelled_shards_refused(self):
        plan = [["a"], ["b"]]
        valid = {0: {"a": "skip"}, 1: {"b": "ok"}}
        self.assertEqual(shard.merge(plan, valid, False), {"a": "skip", "b": "ok"})
        for bad in ({0: valid[0]}, {0: valid[1], 1: valid[0]}, {0: valid[0], 1: {}}):
            with self.assertRaises(ValueError):
                shard.merge(plan, bad, False)
        with self.assertRaises(InterruptedError):
            shard.merge(plan, valid, True)
        with self.assertRaises(ValueError):
            shard.merge([["a"], ["a"]], {0: valid[0], 1: valid[0]}, False)

    def test_configuration_refuses_invalid_impact_graph_as_config_error(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            config = {"schema": "af.test-schedule/v1", "workers": 1}
            path.write_text(json.dumps(config))
            self.assertNotIn("impact_graph", shard.configuration(path))
            path.write_text(json.dumps({**config, "impact_graph": "graph.json"}))
            self.assertEqual(shard.configuration(path)["impact_graph"],
                             path.parent.resolve() / "graph.json")
            for invalid in (None, 1, True, [], {}, ""):
                with self.subTest(invalid=invalid):
                    path.write_text(json.dumps({**config, "impact_graph": invalid}))
                    with self.assertRaisesRegex(ValueError, "^impact_graph$"):
                        shard.configuration(path)

    def test_gate_impact_matches_seeded_failure_and_refuses_dropped_leg(self):
        from asset_factory import core
        from tools import gates

        seed, *others = gates.REQUIRED
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture, stable, tool = (root / name for name in ("seed.txt", "stable.txt", "tool"))
            fixture.write_text("valid")
            stable.write_text("stable")
            tool.write_text("tool")
            tools = {"fixture_tool": str(tool.resolve())}
            pin = core.sha256_json({"fixture_tool": core.sha256_file(tool)})
            inputs = {path.name: core.sha256_file(path) for path in (fixture, stable)}
            roots = {name: [fixture.name if name == seed else stable.name]
                     for name in gates.REQUIRED}
            graph = {"schema": "af.test-impact/v1", "inputs": inputs,
                     "toolchain_sha256": impact.runtime_identity(pin, tools),
                     "roots": roots, "observed_inputs": roots,
                     "dependencies": {key: [] for key in inputs},
                     "prior": dict.fromkeys(gates.REQUIRED, "ok")}
            graph_path = root / "graph.json"
            core.atomic_write_json(graph_path, graph)
            config = {"workers": 1, "impact_graph": graph_path,
                      "impact_sha256": impact.graph_digest(graph),
                      "toolchain_sha256": pin, "toolchain_files": tools}
            fixture.write_text("defect")
            snapshot = [{"path": str(path), "sha256": core.sha256_file(path)}
                        for path in (fixture, stable)]

            def run_leg(name, argv, timeout):
                failed = name == seed and fixture.read_text() != "valid"
                return gates._leg(name, argv, 5 if failed else 0, checks=1,
                                  failures=["seeded_fixture_failure"] if failed else [],
                                  evidence={"fixture": True})

            with patch.object(gates, "run_leg", side_effect=run_leg):
                exhaustive = gates._run_legs(1)
                narrowed = shard.gate_legs(config, snapshot, root, gates)
                with patch.object(shard, "_execute", return_value={0: {}}):
                    with self.assertRaisesRegex(ValueError, "^shard_membership$"):
                        shard.gate_legs(config, snapshot, root, gates)

            self.assertEqual([leg["name"] for leg in narrowed], [seed])
            reference, selected = map(gates.aggregate, (exhaustive, narrowed))
            self.assertEqual((reference["rc"], reference["verdict"]), (5, "FAIL"))
            self.assertEqual((selected["rc"], selected["verdict"]), (5, "FAIL"))
            self.assertIn(f"{seed}:seeded_fixture_failure", selected["failures"])
            self.assertTrue(all(f"{name}:missing_leg" in selected["failures"] for name in others))

            fixture.write_text("valid")
            complete = [run_leg(name, gates.LEGS[name], 300) for name in gates.REQUIRED]
            self.assertEqual(gates.aggregate(complete)["verdict"], "PASS")
            dropped = gates.aggregate(complete[1:])
            self.assertEqual((dropped["rc"], dropped["verdict"]), (3, "CANNOT_SEE"))
            self.assertIn(f"{seed}:missing_leg", dropped["failures"])


class LiveSchedulingTests(unittest.TestCase):
    def setUp(self):
        from asset_factory import proc
        proc.check_test_environment()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.root = self.base / "repo" / "components" / "asset-factory"
        self.tests = self.root / "tests"
        self.tests.mkdir(parents=True)
        self.pattern = "test*.py"

    def make_case(self, module, body, setup=""):
        (self.tests / (module + ".py")).write_text(
            "import unittest\nfrom pathlib import Path\n" + setup
            + "class Live(unittest.TestCase):\n" + body)
        self.addCleanup(sys.modules.pop, module, None)
        self.addCleanup(lambda: sys.path.remove(str(self.tests)) if str(self.tests) in sys.path else None)

    def test_failing_shards_still_require_exact_membership(self):
        for module in ("test_st_rp1_first", "test_st_rp1_second"):
            self.make_case(module, "    def test_case(self):\n        pass\n")
        first = "test_st_rp1_first.Live.test_case"
        second = "test_st_rp1_second.Live.test_case"
        for label, bad_rows in (("missing", {}), ("duplicate", {first: {}, second: {}})):
            out = self.base / label
            receipts = {0: {"rc": 5, "rows": bad_rows, "events": []},
                        1: {"rc": 0, "rows": {second: {}}, "events": []}}
            with self.subTest(label=label), patch.object(shard, "_execute", return_value=receipts):
                with self.assertRaisesRegex(ValueError, "^shard_membership$"):
                    shard.run_suite({"workers": 2}, self.root, self.pattern, out)
                self.assertFalse((out / "result.json").exists())

    def test_dynamic_fixture_skips_count_each_blocked_method(self):
        body = ("    def test_first(self):\n        self.fail('fixture should block me')\n"
                "    def test_second(self):\n        self.fail('fixture should block me')\n")
        self.make_case("test_st_rp1_module_skip", body,
                       "def setUpModule():\n    raise unittest.SkipTest('module unavailable')\n")
        self.make_case("test_st_rp1_class_skip",
                       "    @classmethod\n    def setUpClass(cls):\n"
                       "        raise unittest.SkipTest('class unavailable')\n" + body)
        self.make_case("test_st_rp1_runs", "    def test_case(self):\n        pass\n")
        out = self.base / "skips"
        self.assertEqual(shard.run_suite({"workers": 1}, self.root, self.pattern, out), 0)
        result = json.loads((out / "result.json").read_bytes())
        self.assertEqual({key: row["outcome"] for key, row in result["rows"].items()}, {
            f"test_st_rp1_{level}_skip.Live.test_{method}": "skip"
            for level in ("module", "class") for method in ("first", "second")
        } | {"test_st_rp1_runs.Live.test_case": "ok"})
        self.assertEqual([event[:2] for event in result["events"]], [
            ["skip", "setUpClass (test_st_rp1_class_skip.Live)"],
            ["skip", "setUpModule (test_st_rp1_module_skip)"],
        ])

    def test_genuine_setup_errors_remain_failures_with_complete_membership(self):
        body = "    def test_case(self):\n        self.fail('fixture should block me')\n"
        self.make_case("test_st_rp1_class_error",
                       "    @classmethod\n    def setUpClass(cls):\n"
                       "        raise RuntimeError('broken class fixture')\n" + body)
        self.make_case("test_st_rp1_module_error", body,
                       "def setUpModule():\n    raise RuntimeError('broken module fixture')\n")
        out = self.base / "errors"
        self.assertEqual(shard.run_suite({"workers": 1}, self.root, self.pattern, out), 5)
        result = json.loads((out / "result.json").read_bytes())
        self.assertEqual({key: row["outcome"] for key, row in result["rows"].items()}, {
            "test_st_rp1_class_error.Live.test_case": "error",
            "test_st_rp1_module_error.Live.test_case": "error",
        })
        self.assertEqual([event[:2] for event in result["events"]], [
            ["error", "setUpClass (test_st_rp1_class_error.Live)"],
            ["error", "setUpModule (test_st_rp1_module_error)"],
        ])

    def test_resealed_omitted_edge_catches_real_seed_and_preserves_skip(self):
        from asset_factory import core
        from tools import gates

        module = "test_st_live_reader"
        self.make_case(module, "    def test_seed(self):\n"
                       "        self.assertEqual(Path(__file__).with_name('value.txt').read_text(), 'valid')\n")
        other = "test_st_live_other"
        self.make_case(other, "    def test_unaffected(self):\n        self.assertTrue(True)\n"
                       "    @unittest.skip('fixture skip')\n    def test_unavailable(self):\n        self.fail()\n")
        fixture = self.tests / "value.txt"
        fixture.write_text("valid")
        key = fixture.relative_to(self.root).as_posix()
        reader, passed, skipped = (module+".Live.test_seed", other+".Live.test_unaffected",
                                   other+".Live.test_unavailable")
        first, second = ("tests/"+m+".py" for m in (module, other))
        hashes = impact.inputs_from_snapshot(gates._snapshot(self.root, set()), self.root)
        tools = {"scheduler": str(Path(shard.__file__).resolve())}
        runtime = core.sha256_json({"scheduler": core.sha256_file(Path(tools["scheduler"]))})
        graph = {"schema": "af.test-impact/v1", "inputs": hashes,
                 "toolchain_sha256": impact.runtime_identity(runtime, tools),
                 "roots": {reader: [first], passed: [second], skipped: [second, key]},
                 "prior": {reader: "ok", passed: "ok", skipped: "skip"},
                 "observed_inputs": {reader: [first, key], passed: [second], skipped: [second]},
                 # A valid digest over the wrong graph. The fixture is mapped to
                 # another test; merely checking membership would miss the reader.
                 "dependencies": {first: [], second: [], key: []}}
        path = self.base / "graph.json"
        core.atomic_write_json(path, graph)
        config = {"workers": 2, "toolchain_sha256": runtime, "impact_graph": path,
                  "impact_sha256": impact.graph_digest(graph), "toolchain_files": tools}
        config["module_history"] = {
            module: {"members_sha256": core.sha256_json([reader]), "wall_ns": 100},
            other: {"members_sha256": digest("different roster"), "wall_ns": 200}}
        fixture.write_text("seeded defect")
        runs = []
        for label, cfg in (("impact", config), ("exhaustive", {"workers": 1})):
            out = self.base / label
            self.assertEqual(shard.run_suite(cfg, self.root, self.pattern, out), 5)
            runs.append(json.loads((out / "result.json").read_bytes()))
        plan = json.loads((self.base / "impact" / "plan.json").read_bytes())
        self.assertEqual(plan["module_weights_used"], [module])
        self.assertEqual(runs[0]["scope"], "impact")
        self.assertNotIn(passed, runs[0]["rows"])
        self.assertEqual(runs[0]["rows"][reader]["outcome"], "failure")
        self.assertEqual(runs[0]["events"], runs[1]["events"])
        self.assertEqual(runs[0]["rows"][skipped]["outcome"], "skip")

    def test_real_cancellation_after_child_start_has_no_green_receipt(self):
        from asset_factory import core, proc

        module = "test_st_live_cancel"
        marker = self.base / "entered"
        self.make_case(module, "    def test_wait(self):\n"
                       "        import os, time\n"
                       f"        Path({str(marker)!r}).write_text(str(os.getpid()))\n"
                       "        time.sleep(25)\n")
        config = self.base / "config.json"
        core.atomic_write_json(config, {"schema":"af.test-schedule/v1", "workers":1})
        out = self.base / "cancelled"
        argv = [sys.executable, "-B", "-m", "tools.testing.shard", "--config", str(config),
                "--root", str(self.root), "--out", str(out)]
        job = proc.popen(argv, start_new_session=True)
        try:
            deadline = time.monotonic() + 12
            while not marker.exists():
                if time.monotonic() >= deadline:
                    self.fail("test worker did not reach the cancellation checkpoint")
                time.sleep(.02)
            proc.signal_pid(job, signal.SIGTERM)
            self.assertEqual(job.returncode, 6)
        finally:
            self.assertEqual(proc.cleanup(job), [])
        self.assertFalse((out / "result.json").exists())
        self.assertFalse(Path("/proc", marker.read_text()).exists())


    def test_worker_requires_terminal_outcome_and_honours_unittest_stop(self):
        from asset_factory import core

        for module, action in (("test_st_silent", ""),
                               ("test_st_stop", "result.addSuccess(self)\n        result.stop()")):
            self.make_case(module, "    def test_case(self):\n        self.assertTrue(True)\n"
                           "    def run(self, result):\n        result.startTest(self)\n"
                           f"        {action}\n        result.stopTest(self)\n")
            request = self.base / (module + ".json")
            core.atomic_write_json(request, {"root":str(self.root), "pattern":module + ".py",
                                             "ids":[module + ".Live.test_case"]})
            with self.subTest(module=module):
                self.assertNotEqual(shard.run_worker(request), 0)

    def test_changed_tool_bytes_invalidate_unchanged_configuration_pin(self):
        from asset_factory import core

        tool = self.base / "instrument"
        tool.write_text("accepted runtime")
        slots = {"instrument": str(tool.resolve())}
        pin = core.sha256_json({"instrument": core.sha256_file(tool)})
        self.assertIsNotNone(impact.runtime_identity(pin, slots))
        tool.write_text("different runtime")
        self.assertIsNone(impact.runtime_identity(pin, slots))
