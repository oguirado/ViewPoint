"""Acceptance workload contracts and fail-closed controls without a GUI."""

from contextlib import ExitStack, contextmanager
from hashlib import sha256
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import driver
import acceptance
import inventory
import t2_workload
from runtime import Child, PrivateDisplays, Refused, choose_displays


class AcceptanceWorkloadTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(self.enterContext(TemporaryDirectory()))
        self.controls = {"competing_cgroups": [], "throttle_counters": [],
                         "pair_tolerance": dict.fromkeys(("load", "temperature_millic",
                                                         "competing_memory_bytes", "competing_cpu_usec"), 0)}

    def test_t1_manifest_round_trips_distinct_frozen_records_and_refusal(self):
        from schemas.validation import canonical_bytes, validate_contract

        class FixtureDisplay:
            name, label = ":92", "xpra:92"
            server = SimpleNamespace(pid=42)

            def verify(self, label):
                return label == self.label

        browser = SimpleNamespace(display=FixtureDisplay(), xid="0x22", child=SimpleNamespace(pid=43),
                                  port=44000, profile=self.directory / "profile", incarnation="first",
                                  witness=sha256(b"fixture-witness").hexdigest(),
                                  target={"id": "target"}, fixture_digest=sha256(b"fixture").hexdigest(),
                                  check=lambda: None)
        path = acceptance.t1_manifest(["ASLEEP", "OFF"], [browser], 41000, "/fixture", 123, self.directory)
        raw = path.read_bytes()
        row = json.loads(raw)
        self.assertEqual(raw, canonical_bytes(row))
        self.assertEqual(row["fixture_cgroups"], ["/fixture"])
        self.assertEqual(row["ring_bytes"], 123)
        live, refusal = row["surfaces"]
        self.assertEqual(refusal, {"state": "OFF", "surface_id": "qd-refusal-1"})
        for key, contract in (("session", "vp.session/v1"), ("binding", "vp.surface_binding/v1"),
                              ("agent_demand", "vp.demand/v1")):
            record = json.loads(Path(live[key]).read_bytes())
            validate_contract(contract, record)
            if "surface_id" in record:
                self.assertEqual(live["surface_id"], record["surface_id"])
        fixture = live["private_fixture"]
        self.assertEqual((fixture["browser_pid"], fixture["xvfb_pid"], fixture["xid"]), (43, 42, "0x22"))
        self.assertEqual((fixture["fixture_port"], fixture["cdp_port"]), (41000, 44000))

    def test_t2_plan_pins_source_and_actual_fixture_bytes(self):
        harness = acceptance.load_harness("t2")
        plan, path = acceptance.t2_plan(self.directory, 2, 3, 4, self.controls)
        self.assertEqual(harness.digest(plan), sha256(path.read_bytes()).hexdigest())
        self.assertEqual(plan, json.loads(path.read_bytes()))
        self.assertEqual(plan["workload_sha256"], sha256(acceptance.T2_DRIVER.read_bytes()).hexdigest())
        self.assertEqual(len(plan["combinations"]), 2)
        harness.plan_check(plan)
        for name, digest in plan["fixtures"].items():
            self.assertEqual(sha256((self.directory / name).read_bytes()).hexdigest(), digest)

    def test_harness_rejects_mutated_source_or_fixture_digest_before_prepare(self):
        harness = acceptance.load_harness("t2")
        plan, path = acceptance.t2_plan(self.directory, 2, 3, 4, self.controls)
        args = SimpleNamespace(arm="baseline", plan=path, driver=acceptance.T2_DRIVER)
        with patch.object(harness, "signature", return_value=({}, self.directory)), \
                patch.object(harness, "HostTelemetry", side_effect=RuntimeError("passed digest gate")) as telem, \
                patch.object(harness, "load_driver") as load:
            self.assertEqual(harness.payload(args)["error"], "RuntimeError")
            telem.assert_called_once()
            telem.reset_mock()
            path.write_text(json.dumps(dict(plan, workload_sha256="0" * 64)))
            self.assertEqual(harness.payload(args)["error"], "workload source changed")
            path.write_text(json.dumps(plan))
            (self.directory / "timer.html").write_bytes(b"substituted fixture")
            self.assertEqual(harness.payload(args)["error"], "fixture bytes changed")
            telem.assert_not_called()
            load.assert_not_called()

    def test_t2_requires_active_plan_and_provider_before_preparing(self):
        with patch.dict(sys.modules):
            sys.modules.pop("_vp_qd_invocation", None)
            with self.assertRaisesRegex(Refused, "requires an active QD run"):
                driver.prepare({"combinations": {}})
        with patch.object(driver, "get_ports", return_value=SimpleNamespace()):
            with self.assertRaisesRegex(Refused, "exact provisioned"):
                t2_workload.prepare({"combinations": {}})
        with patch.object(driver, "get_ports", return_value=SimpleNamespace(acceptance=SimpleNamespace(plan={"same": 1}))):
            with self.assertRaisesRegex(Refused, "exact provisioned"):
                t2_workload.prepare({"same": 2})
        unsupported = {"combinations": {"app": {"backend": "vision", "op": "observe"}}}
        with self.assertRaisesRegex(Refused, "lacks a requested provider"):
            t2_workload.T2Workload(unsupported, self.directory, None, {"app": object()}, object(), "/fixture")
        supported = {"combinations": {"app": {"backend": "cdp", "op": "observe"}}}
        with self.assertRaisesRegex(Refused, "login fixture AUSENTE"):
            t2_workload.T2Workload(supported, self.directory, None, {"app": object()}, None, "/fixture")

    def test_partial_multi_app_start_reaps_owned_processes(self):
        owned = []
        argv = [sys.executable, "-IB", "-c", "from time import sleep; sleep(60)"]

        directory = self.directory

        class FakeDisplay:
            def __init__(self, label, **_):
                self.number = int(label[1:])
                self.socket = directory / (label + "-socket")
                self.lock = directory / (label + "-lock")
                self.env = {}

            def start(self, stack, *, framebuffer):
                self.server = Child(argv, os.environ, stack)
                owned.append(self.server)

            def start_xpra(self, stack):
                owned.append(Child(argv, os.environ, stack))

        @contextmanager
        def browser(display, _url, **_):
            if display.number == 91:
                raise Refused("second browser unavailable")
            with ExitStack() as children:
                owned.append(Child(argv, os.environ, children))
                yield object()

        for parent in ("config", "cache", "data", "run"):
            (directory / parent).mkdir()
        with patch("runtime.Display", FakeDisplay), patch.object(acceptance, "open_browser", browser):
            with self.assertRaisesRegex(Refused, "second browser unavailable"):
                with ExitStack() as stack:
                    displays = PrivateDisplays([":90", ":91"])
                    acceptance.start_app(stack, displays, ":90", 41000)
                    acceptance.start_app(stack, displays, ":91", 41000)
        self.assertEqual(len(owned), 5)
        for child in owned:
            self.assertIsNotNone(child.proc.poll())
            self.assertFalse(Path(f"/proc/{child.pid}").exists())

    def test_display_allocation_skips_occupied_host_endpoints(self):
        (self.directory / ".X93-lock").touch()
        unix = self.directory / "unix"
        unix.touch()
        self.assertEqual(choose_displays(":91", None, 5, tmp=self.directory, unix=unix),
                         [":91", ":90", ":92", ":94", ":95"])
        with self.assertRaisesRegex(Refused, "occupied"):
            choose_displays(":93", None, 5, tmp=self.directory, unix=unix)
        self.assertEqual(choose_displays(":93", None, 5, tmp=self.directory, unix=unix, allow_occupied=True),
                         [":90", ":91", ":92", ":94", ":95"])
        self.assertTrue((self.directory / ".X93-lock").exists())

    def test_rss_snapshot_skips_a_child_that_exits_during_proc_read(self):
        group = self.directory / "fixture"
        group.mkdir()
        (group / "cgroup.procs").write_text("101\n102\n")
        (group / "memory.current").write_text("4096\n")
        (self.directory / "memory.current").write_text("8192\n")
        (self.directory / "memory.peak").write_text("16384\n")
        read_text = Path.read_text

        def status(path, *args, **kwargs):
            if path == Path("/proc/101/status"):
                raise ProcessLookupError
            if path == Path("/proc/102/status"):
                return "VmRSS: 4 kB\n"
            return read_text(path, *args, **kwargs)

        with patch.object(Path, "read_text", status):
            self.assertEqual(acceptance.rss_snapshot(group), {
                "fixture_rss_bytes": 4096, "rss_processes": 1, "fixture_charged_bytes": 4096,
                "scope_charge_bytes": 8192, "scope_peak_bytes": 16384,
            })

    def test_inventory_preserves_process_start_identity(self):
        for pid, name in ((41, "Xvfb"), (42, "qemu-system-aar"), (43, "bash")):
            process = self.directory / str(pid)
            process.mkdir()
            (process / "stat").write_text(f"{pid} ({name}) S " + "0 " * 18 + str(pid * 2))
        observed = inventory.process_inventory(self.directory)
        self.assertEqual(observed, {"guests": ["qemu-system-aar:42:84"], "private_display_servers": ["Xvfb:41:82"]})
