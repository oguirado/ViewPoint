import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest

TESTS = Path(__file__).resolve().parent
KIT = Path(os.environ.get("LIVE_KIT_ROOT", TESTS.parent)).resolve()
REPO = KIT.parents[1]
BUILD = Path(os.environ.get("LIVE_KIT_BUILD", TESTS.parent / ".build")).resolve()
OWNER = "actor-host"
CONTROLLER = "mc06-controller/controller.py"
BACKEND = "mc06-controller/arm_backend.py"
GUARD = "mc06-controller/contract_guard.py"
HARNESS = "mc04-runtime/arm_harness.py"
REFERENCE = "mc05-contract-candidate/reference_model.py"
SCHEMAS = [
    f"mc05-contract-candidate/contracts/v0.2/headless_{kind}.schema.json"
    for kind in ("request", "event", "session")
]


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def message(op, request_id, **params):
    return {"schema": "headless_request/v0.2", "request_id": f"request-{request_id}",
            "op": op, "params": params}


def create():
    return message("create", "create", template="headless-sumsq")


class LiveKitTests(unittest.TestCase):
    def setup_kit(self, *relative, fault="", admitted=True):
        for path in relative:
            self.assertTrue((KIT / path).is_file(), f"required kit source missing: {path}")
        self.tmp = tempfile.TemporaryDirectory(prefix="lk-")
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        sys.path.insert(0, str(KIT / "mc06-controller"))
        self.addCleanup(sys.path.pop, 0)
        from controller import Controller
        from arm_backend import ArmBackend
        self.Controller = Controller
        self.state = self.base / "state"
        self.runtime = self.base / "runtime"
        for directory in (self.state, self.runtime):
            directory.mkdir(mode=0o700)

        # Use the actual static ARM64 init, an ELF fixture for the engine pin, and
        # the minimum Linux Image header fixture. Only the fake subprocess runs.
        engine = self.base / "engine"
        shutil.copyfile(BUILD / "init-aarch64", engine)
        kernel = self.base / "kernel"
        image = bytearray(64)
        struct.pack_into("<QQ", image, 16, len(image), 2)
        image[56:60] = b"ARM\x64"
        kernel.write_bytes(image)
        for path in (engine, kernel):
            path.chmod(0o600)
        self.config = {
            "schema": "mc06-host-config/v1", "owner_id": OWNER, "generation": 7,
            "runtime_admitted": admitted, "resources": {"mem_size_mib": 256, "vcpu_count": 1},
            "deadlines": {"startup_seconds": 3, "job_seconds": 2, "stop_seconds": 2},
            "artifacts": {name: {"path": str(path), "sha256": sha(path)}
                          for name, path in (("engine", engine), ("kernel", kernel),
                                             ("init", BUILD / "init-aarch64"))},
            "runtime_root": str(self.runtime), "external_cgroup": "/sys/fs/cgroup/test",
        }
        self.trace = self.base / "fake-trace"
        self.spawned = []
        self.checked = []

        class FakeNative(ArmBackend):
            def _preflight(backend):
                self.checked.append("preflight")

            def _check_cgroup(backend, child=None):
                self.checked.append(child)
                return {"test_scope": True}

            def _spawn(backend):
                process = subprocess.Popen(
                    [sys.executable, "-I", "-B", str(TESTS / "fake_firecracker.py"),
                     str(backend.files["api"]), str(self.trace), fault],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                )
                self.spawned.append(process)
                return process

        self.FakeNative = FakeNative

    def controller(self):
        controller = self.Controller(self.config, self.state, self.FakeNative)
        self.addCleanup(controller.close)
        return controller

    def records(self):
        if not self.trace.exists():
            return []
        return [json.loads(line) for line in self.trace.read_text().splitlines()]

    def send(self, controller, request, actor=OWNER):
        events = list(controller.handle(request, actor))
        for event in events:
            controller.contracts.validate("headless_event", event)
        return events

    def identity(self, controller):
        return {key: controller.session[key] for key in ("workload_id", "generation")}

    def stop(self, controller, request_id="stop"):
        return self.send(controller, message("stop", request_id, **self.identity(controller)))

    def test_config_and_schema_pins(self):
        self.setup_kit(GUARD, *SCHEMAS)
        from contract_guard import Invalid, validate_config
        self.assertEqual(validate_config(self.config, self.state), self.state)
        broken = copy.deepcopy(self.config)
        broken["resources"]["vcpu_count"] = True
        with self.assertRaises(Invalid):
            validate_config(broken, self.state)
        controller = self.controller()
        request = message("run", "invalid", workload_id="workload-a", generation=7,
                          job_id="job-a", operation="sumsq", n=10001)
        self.assertEqual(self.send(controller, request)[0]["data"]["code"], "BAD_REQUEST")
        self.assertEqual(self.spawned, [])

    def test_create_run_stop_and_no_second_job(self):
        self.setup_kit(CONTROLLER, BACKEND, REFERENCE)
        controller = self.controller()
        pending = controller.handle(create(), OWNER)
        try:
            accepted = next(pending)
            self.assertEqual(accepted["data"]["session"]["phase"], "starting")
            self.assertFalse(self.spawned)
            rows = self.journal()
            self.assertEqual([row["kind"] for row in rows], ["owner", "intent", "event"])
            ready, = pending
        finally:
            pending.close()
        session = ready["data"]["session"]
        self.assertEqual((session["backend"], session["isa"], session["phase"]),
                         ("firecracker", "aarch64", "ready"))
        records = self.records()
        calls = records[:4]
        self.assertEqual([row["path"] for row in calls],
                         ["/machine-config", "/boot-source", "/vsock", "/actions"])
        self.assertTrue(all(row["method"] == "PUT" for row in calls))
        self.assertEqual(calls[0]["body"], {"mem_size_mib": 256, "vcpu_count": 1})
        self.assertEqual(calls[2]["body"]["guest_cid"], 3)
        self.assertEqual(calls[3]["body"], {"action_type": "InstanceStart"})
        self.assertIn("rdinit=/init", calls[1]["body"]["boot_args"])
        run_args = dict(self.identity(controller), job_id="job-one", operation="sumsq", n=10000)
        accepted, completed = self.send(controller, message("run", "run", **run_args))
        self.assertEqual(accepted["data"]["session"]["job"]["status"], "accepted")
        self.assertEqual(completed["data"]["session"]["job"]["value"], 333383335000)
        repeat, = self.send(controller, message("run", "repeat", **run_args))
        self.assertEqual(repeat["data"]["code"], "DUPLICATE_JOB")
        distinct, = self.send(controller, message("run", "other", **dict(run_args, job_id="job-two")))
        self.assertEqual(distinct["data"]["code"], "OUT_OF_ORDER")
        self.assertEqual(sum(row.get("guest_op") == "SUMSQ" for row in self.records()), 1)
        accepted, stopped = self.stop(controller)
        self.assertEqual(accepted["data"]["session"]["phase"], "stopping")
        self.assertEqual(stopped["data"]["session"]["ownership"], "released")
        self.assertEqual(stopped["data"]["session"]["job"], completed["data"]["session"]["job"])
        self.assertEqual(list(self.runtime.iterdir()), [])
        self.assertEqual(self.spawned[0].returncode, 0)
        self.assertIn({"closed": True, "exit_status": 0, "outcome": "guest_stopped"},
                      [row["data"] for row in self.journal() if row["kind"] == "firecracker_stop"])

    def journal(self):
        return [json.loads(line) for line in (self.state / "journal.jsonl").read_text().splitlines()]

    def test_admission_owner_generation_and_request_replay(self):
        self.setup_kit(CONTROLLER)
        controller = self.controller()
        wrong, = self.send(controller, create(), "actor-other")
        self.assertEqual(wrong["data"]["code"], "OWNER_MISMATCH")
        self.assertIsNone(wrong["data"]["workload_id"])
        self.assertEqual(self.spawned, [])
        self.assertEqual(self.send(controller, create())[-1]["data"]["session"]["phase"], "ready")
        replay, = self.send(controller, create())
        self.assertEqual(replay["data"]["code"], "DUPLICATE_REQUEST")
        stale, = self.send(controller, message("stop", "stale", **dict(self.identity(controller), generation=8)))
        self.assertEqual(stale["data"]["code"], "STALE_GENERATION")
        self.assertEqual(self.stop(controller)[-1]["data"]["session"]["phase"], "stopped")
        self.assertEqual(len(self.spawned), 1)

    def test_unadmitted_and_existing_attempt(self):
        self.setup_kit(CONTROLLER, admitted=False)
        controller = self.controller()
        refused, = self.send(controller, create())
        self.assertEqual(refused["data"]["code"], "UNAVAILABLE")
        self.assertEqual(refused["data"]["effect"], "not_dispatched")
        self.assertEqual(self.checked, [])
        with self.assertRaises(FileExistsError):
            self.Controller(self.config, self.state, self.FakeNative)
        controller.close()
        with self.assertRaises(FileExistsError):
            self.Controller(self.config, self.state, self.FakeNative)
        previous = "0" * 64
        for seq, row in enumerate(self.journal(), 1):
            self.assertEqual((row["prev"], row["seq"]), (previous, seq))
            previous = row.pop("sha256")
            from controller import encoded
            self.assertEqual(hashlib.sha256(encoded(row)).hexdigest(), previous)

    def test_asset_pin_and_architecture(self):
        self.setup_kit(HARNESS, BACKEND)
        controller = self.controller()
        harness = controller.backend.harness
        init = self.config["artifacts"]["init"]
        harness.pinned_asset(init["path"], init["sha256"], static=True)
        engine = self.config["artifacts"]["engine"]
        path = Path(engine["path"])
        bad = bytearray(path.read_bytes())
        struct.pack_into("<H", bad, 18, 62)
        path.write_bytes(bad)
        with self.assertRaisesRegex(RuntimeError, "SHA256 mismatch"):
            harness.pinned_asset(engine["path"], engine["sha256"])
        with self.assertRaisesRegex(RuntimeError, "not AArch64"):
            harness.pinned_asset(engine["path"], sha(path))
        self.assertEqual(self.send(controller, create())[-1]["event"], "outcome_unknown")
        self.assertFalse(self.spawned)
        self.assertEqual(self.stop(controller)[-1]["data"]["session"]["ownership"], "released")

    def test_api_refusal_stops_dispatch(self):
        self.setup_kit(BACKEND, fault="api_reject")
        controller = self.controller()
        accepted, unknown = self.send(controller, create())
        self.assertEqual(accepted["event"], "accepted")
        self.assertEqual(unknown["event"], "outcome_unknown")
        self.assertEqual(unknown["data"]["session"]["ownership"], "held")
        self.assertEqual([row["path"] for row in self.records()], ["/machine-config"])
        self.assertEqual(self.spawned[0].wait(timeout=2), 3)
        self.assertEqual(self.stop(controller)[-1]["data"]["session"]["phase"], "stopped")
        self.assertEqual(self.journal()[-2]["data"]["outcome"], "already_exited")

    def test_wrong_guest_isa_blocks_readiness(self):
        self.setup_kit(BACKEND, fault="wrong_isa")
        controller = self.controller()
        _, unknown = self.send(controller, create())
        self.assertEqual(unknown["data"]["session"]["phase"], "uncertain")
        self.assertEqual(self.stop(controller)[-1]["data"]["session"]["phase"], "stopped")

    def test_lost_reply_preserves_ownership_and_never_retries(self):
        self.setup_kit(BACKEND, fault="lost_job")
        controller = self.controller()
        self.send(controller, create())
        params = dict(self.identity(controller), job_id="job-lost", operation="sumsq", n=0)
        request = message("run", "lost", **params)
        _, unknown = self.send(controller, request)
        self.assertEqual(unknown["event"], "outcome_unknown")
        self.assertEqual(unknown["data"]["session"]["job"]["status"], "unknown")
        self.assertIsNone(unknown["data"]["session"]["job"]["value"])
        self.assertEqual(self.send(controller, request)[0]["data"]["code"], "DUPLICATE_REQUEST")
        self.assertEqual(self.send(controller, message("run", "new", **dict(params, job_id="job-new")))[0]
                         ["data"]["code"], "OUT_OF_ORDER")
        self.assertEqual(sum(row.get("guest_op") == "SUMSQ" for row in self.records()), 1)
        stopped = self.stop(controller)[-1]["data"]["session"]
        self.assertEqual(stopped["ownership"], "released")
        self.assertEqual(stopped["job"]["status"], "unknown")

    def test_wrong_result_is_uncertain(self):
        self.setup_kit(BACKEND, fault="wrong_result")
        controller = self.controller()
        self.send(controller, create())
        _, unknown = self.send(controller, message("run", "run", **self.identity(controller),
                                                   job_id="job-result", operation="sumsq", n=12))
        self.assertEqual(unknown["data"]["session"]["job"]["status"], "unknown")
        self.assertEqual(self.stop(controller)[-1]["data"]["session"]["phase"], "stopped")

    def test_lost_stop_never_reports_release(self):
        self.setup_kit(BACKEND, fault="lost_stop")
        controller = self.controller()
        self.send(controller, create())
        _, unknown = self.stop(controller)
        self.assertEqual(unknown["data"]["session"]["ownership"], "held")
        self.assertIsNone(self.spawned[0].poll())
        controller.close()
        self.assertIsNotNone(self.spawned[0].returncode)
        self.assertEqual(self.journal()[-1]["data"]["outcome"], "forced_on_owner_exit")
        self.assertEqual(list(self.runtime.iterdir()), [])

    def test_kernel_config_gate(self):
        self.setup_kit("guest/firecracker-arm64.config", "guest/build-kernel.sh")
        linux = self.base / "linux"
        merge = linux / "scripts/kconfig/merge_config.sh"
        merge.parent.mkdir(parents=True)
        merge.write_text('#!/bin/sh\ncat "$4" "$5" > "$KCONFIG_CONFIG"\n')
        merge.chmod(0o700)
        makefile = linux / "Makefile"
        makefile.write_text(
            "olddefconfig:\n\t@if test -f drop-vsock; then "
            "sed -i '/^CONFIG_VIRTIO_VSOCKETS=y$$/d' $(KCONFIG_CONFIG); fi\n"
            "Image:\n\tmkdir -p $(O)/arch/arm64/boot\n\techo fixture > $(O)/arch/arm64/boot/Image\n"
        )
        base = self.base / "approved-config"
        base.write_text("CONFIG_TEST_FIXTURE=y\n")
        positive = self.base / "kernel-good"
        command = [str(KIT / "guest/build-kernel.sh"), str(linux), str(base)]
        passed = subprocess.run(command + [str(positive), ""], text=True, capture_output=True, timeout=5)
        self.assertEqual(passed.returncode, 0, passed.stderr)
        self.assertIn("CONFIG_VIRTIO_VSOCKETS=y", (positive / ".config").read_text())
        self.assertIn(sha(positive / "arch/arm64/boot/Image"), passed.stdout)
        (linux / "drop-vsock").touch()
        negative = self.base / "kernel-bad"
        rejected = subprocess.run(command + [str(negative), ""], text=True, capture_output=True, timeout=5)
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn("Required kernel option missing after olddefconfig: CONFIG_VIRTIO_VSOCKETS=y",
                      rejected.stderr)
        self.assertFalse((negative / "arch/arm64/boot/Image").exists())

    def test_isolated_h1_runner_snapshot(self):
        self.setup_kit(CONTROLLER, BACKEND, GUARD, HARNESS, REFERENCE, *SCHEMAS, admitted=False)
        attempt = self.base / "attempt"
        attempt.mkdir(mode=0o700)
        files = []
        for relative in (CONTROLLER, BACKEND, GUARD, HARNESS, REFERENCE, *SCHEMAS):
            dest = attempt / "bundle" / relative
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(KIT / relative, dest)
            dest.chmod(0o600)
            files.append({"relative": relative, "sha256": sha(dest)})
        runner = attempt / "runner.py"
        shutil.copyfile(REPO / "daemon-adapter/tools/headless_runner.py", runner)
        unit = Path(Path("/proc/self/cgroup").read_text().strip().split("::", 1)[1]).name
        (attempt / "launch.json").write_text(json.dumps({
            "files": files, "config": self.config, "state_dir": str(self.state),
            "runtime_root": str(self.runtime), "unit": unit,
        }))
        # The outer suite runs at 8 GiB. Stub only the 512 MiB service cap check.
        command = (
            "import runpy,sys; "
            "run = runpy.run_path(sys.argv[1])['run']; "
            "run.__globals__['verify_caps'] = lambda _: None; run(sys.argv[2])"
        )
        args = [sys.executable, "-I", "-B", "-c", command, str(runner), str(attempt)]
        got = subprocess.run(args, input=json.dumps(create()) + "\n", text=True,
                             capture_output=True, timeout=5)
        self.assertEqual(got.returncode, 0, got.stderr)
        handshake, refused = [json.loads(line) for line in got.stdout.splitlines()]
        self.assertIs(handshake["mc09_ready"], True)
        self.assertEqual(refused["data"]["code"], "UNAVAILABLE")
        target = attempt / "bundle" / HARNESS
        target.write_bytes(target.read_bytes() + b"\n")
        denied = subprocess.run(args, input="", text=True, capture_output=True, timeout=5)
        self.assertNotEqual(denied.returncode, 0)
        self.assertIn("source pin", denied.stderr)
        self.assertEqual(denied.stdout, "")
        # A valid runner manifest cannot override the closure's intrinsic pin.
        spec = attempt / "launch.json"
        launch = json.loads(spec.read_text())
        fresh = self.base / "fresh"
        fresh.mkdir(mode=0o700)
        launch["state_dir"] = str(fresh)
        next(item for item in launch["files"] if item["relative"] == HARNESS)["sha256"] = sha(target)
        spec.write_text(json.dumps(launch))
        denied = subprocess.run(args, input="", text=True, capture_output=True, timeout=5)
        self.assertNotEqual(denied.returncode, 0)
        self.assertIn("bundle source pin changed", denied.stderr)
        self.assertEqual(denied.stdout, "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
