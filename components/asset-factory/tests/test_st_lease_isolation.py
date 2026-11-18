"""Scoped lease regressions with real owned CPU dummy and Blender workers."""
from dataclasses import replace
from functools import partial
from hashlib import sha256
import os
from pathlib import Path
import random
import runpy
import sys
from tempfile import TemporaryDirectory
import time
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch

from asset_factory import core, proc
from test_st_lease import ROOT, OfflineAuthority, deadline, launch_tripwire, run_worker
from tools.workers.lease import (
    AdapterScope, LeaseError, Residency, ReuseQualification, verify_isolation,
)

THIS = Path(__file__).resolve()
_CACHE = {}


def starttime(pid):
    return int(Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19])


def identity_present(identity):
    pid, started = identity
    try:
        return starttime(pid) == started
    except FileNotFoundError:
        return False


def contamination_probe(blender):
    # The fixture renders before the probe. Only the poison callback is
    # persistent, so reading factory settings would not clear it.
    if blender:
        import bpy
        from bpy.app.handlers import persistent
        from io_scene_gltf2.blender.exp import gltf2_blender_gather_primitives_extract as exporter

        @persistent
        def dirty_handler(*args):
            pass

        handlers = bpy.app.handlers.load_post
        initial_rounding = exporter.ROUNDING_DIGIT
    random.seed(314159)

    def probe(root, payload, value):
        op = payload.get("op")
        if op == "poison":
            random.seed(271828)
            core._lease_isolation_probe = True
            _CACHE["job_a"] = True
            if blender:
                handlers.append(dirty_handler)
                exporter.ROUNDING_DIGIT = initial_rounding + 1
        state = {
            "rng": random.getstate() == random.Random(314159).getstate(),
            "module": not hasattr(core, "_lease_isolation_probe"),
            "cache": not _CACHE,
        }
        if blender:
            state["handlers"] = not any(handler.__name__ == "dirty_handler" for handler in handlers)
            state["exporter"] = exporter.ROUNDING_DIGIT == initial_rounding
        pid = os.getpid()
        return {"identity": [pid, starttime(pid)], "state": state, "fixture": value}

    return probe


def export_probe(root, payload, value):
    import bpy
    from io_scene_gltf2.blender.exp import gltf2_blender_gather_primitives_extract as exporter
    from tools.blender import look_bake

    # Keep the actual Blender GLTF operator in the path on both outcomes.
    script = root / "protected_export_probe.py"
    script.write_text(
        "from types import ModuleType, SimpleNamespace\n"
        "from pathlib import Path\n"
        "def parse_args(argv): return SimpleNamespace(out_dir=argv[-2], fail=argv[-1]=='fail')\n"
        "def require_ram_out(path): pass\n"
        "def bake(bpy, args):\n"
        "    opts = dict(invalid_export_option=True) if args.fail else {}\n"
        "    bpy.ops.export_scene.gltf(filepath=str(Path(args.out_dir) / 'protected.glb'),"
        " export_format='GLB', **opts)\n"
        "    return {'maps': {}}\n"
    )
    original = sys.argv
    previous_profile = sys.getprofile()
    operator = type(bpy.ops.export_scene.gltf).__call__.__code__
    observed = []

    def record(frame, event, arg):
        if event == "call" and frame.f_code is operator:
            if frame.f_locals["self"].idname_py() == "export_scene.gltf":
                observed.append(exporter.ROUNDING_DIGIT)
        if previous_profile is not None:
            previous_profile(frame, event, arg)

    before = 11
    exporter.ROUNDING_DIGIT = before
    try:
        sys.argv = [str(script), payload["output"], payload["case"]]
        sys.setprofile(record)
        try:
            look_bake.run_look_worker(script, 2, True)
        except (TypeError, RuntimeError) as exc:
            outcome = "export_failed" if "invalid_export_option" in str(exc) else str(exc)
        else:
            outcome = "exported"
    finally:
        sys.setprofile(previous_profile)
        sys.argv = original
    return {"outcome": outcome, "rounding": exporter.ROUNDING_DIGIT,
            "before": before, "during": observed}


def worker_argv(root, *, blender=False, export=False, qualified=False):
    if not blender:
        mode = "--qualified-worker" if qualified else "--isolation-worker"
        return [sys.executable, "-B", str(THIS), mode, str(root)]
    # Blender embeds Python. Its -B is a Blender option, so use the Python
    # bytecode-disable flag in the expression and its inherited environment.
    expression = (
        f"import runpy,sys; sys.dont_write_bytecode=True; sys.path[:0]=[{str(ROOT)!r},"
        f"{str(THIS.parent)!r}]; namespace=runpy.run_path({str(THIS)!r});"
        f" raise SystemExit(namespace['isolation_worker']({str(root)!r}, True, {export!r}, {qualified!r}))"
    )
    return ["/usr/bin/blender", "-b", "--factory-startup", "--disable-autoexec",
            "--threads", "2", "--python-exit-code", "2", "--python-expr", expression]


class FixtureIsolation:
    # Only these named surfaces are qualified. Other modules can legitimately
    # be imported lazily by Blender.
    module_key = "af_lease_qualification_poison"
    module_attr = "_lease_isolation_probe"

    def __init__(self, blender):
        self.identity = [os.getpid(), starttime(os.getpid())]
        self.random_state = random.Random(314159).getstate()
        random.setstate(self.random_state)
        self.cache = {}
        self.handlers = None
        if blender:
            import bpy
            from bpy.app.handlers import persistent
            from io_scene_gltf2.blender.exp import gltf2_blender_gather_primitives_extract as exporter

            @persistent
            def dirty_handler(*args):
                pass

            self.handlers = bpy.app.handlers.load_post
            self.initial_handlers = tuple(self.handlers)
            self.dirty_handler = dirty_handler
            self.exporter = exporter
            self.rounding = exporter.ROUNDING_DIGIT

    def state(self):
        state = {
            "rng": random.getstate() == self.random_state,
            "module_attribute": not hasattr(core, self.module_attr),
            "module_entry": self.module_key not in sys.modules,
            "cache": not self.cache,
        }
        if self.handlers is not None:
            state["handlers"] = tuple(self.handlers) == self.initial_handlers
            state["exporter"] = self.exporter.ROUNDING_DIGIT == self.rounding
        return state

    def perturb(self):
        random.seed(271828)
        setattr(core, self.module_attr, True)
        sys.modules[self.module_key] = ModuleType(self.module_key)
        self.cache["job_a"] = True
        if self.handlers is not None:
            self.handlers.append(self.dirty_handler)
            self.exporter.ROUNDING_DIGIT = self.rounding + 1

    def reset(self):
        random.setstate(self.random_state)
        if hasattr(core, self.module_attr):
            delattr(core, self.module_attr)
        sys.modules.pop(self.module_key, None)
        self.cache.clear()
        if self.handlers is not None:
            self.handlers[:] = self.initial_handlers
            self.exporter.ROUNDING_DIGIT = self.rounding


def isolation_worker(root, blender=False, export=False, qualified=False):
    if qualified:
        return run_worker(root, blender, isolation=FixtureIsolation(blender))
    return run_worker(root, blender, export_probe if export else contamination_probe(blender))


def adapter_scope(blender):
    sources = [THIS, THIS.with_name("test_st_lease.py"), Path(core.__file__)]
    probes = ("rng", "module_attribute", "module_entry", "cache")
    if blender:
        sources.append(ROOT / "tools/pipeline/fixture_blender.py")
        probes += ("handlers", "exporter")
    pins = {str(path.relative_to(ROOT)): core.sha256_file(path) for path in sources}
    digest = sha256(core.canonical_json(pins).encode()).hexdigest()
    return AdapterScope("fixture-blender-cpu" if blender else "fixture-dummy", digest,
                        "procedural-cycles-cpu" if blender else "dummy-bytes", probes)


def qualified_argv(blender):
    return partial(worker_argv, blender=blender, qualified=True)


def qualification_run(blender):
    """Exercise the adapter protocol twice under one supervised process."""
    scope = adapter_scope(blender)
    with TemporaryDirectory() as scratch:
        root = Path(scratch)
        launch = {"qualification": scope.adapter_sha256}
        core.atomic_write_json(root / "launch.json", launch)
        job = proc.popen(qualified_argv(blender)(root), start_new_session=True,
                         memory_limits=proc.MemoryLimits(6 << 30, 8 << 30), cwd=ROOT,
                         env={"PATH": os.defpath, "PYTHONPATH": os.pathsep.join(map(str, (ROOT, THIS.parent))),
                              "PYTHONDONTWRITEBYTECODE": "1"}, runtime_s=120)

        def receive(name):
            path = root / name
            end = deadline(30)
            while not path.exists():
                if time.monotonic_ns() >= end or proc.poll_exit(job) is not None:
                    raise LeaseError("qualification_worker_unproven")
                time.sleep(.003)
            reply = core.load_json(path, max_bytes=65536)
            path.unlink()
            return reply

        def owned_identity():
            # This offline harness uses the PID-namespace backend. Its
            # supervisor starts the adapter as its direct child.
            supervisor_pid = job._backend._child.pid
            children = Path(f"/proc/{supervisor_pid}/task/{supervisor_pid}/children")
            try:
                child_pids = children.read_text().split()
                if len(child_pids) == 1:
                    pid = int(child_pids[0])
                    return [pid, starttime(pid)]
            except (OSError, ValueError, IndexError) as exc:
                raise LeaseError("qualification_worker_unproven") from exc
            raise LeaseError("qualification_worker_unproven")

        def submit(op, epoch):
            identity = {"lease_epoch": epoch}
            payload = {"op": op, "output": str(root / op)}
            core.atomic_write_json(root / "request.json", {**identity, "payload": payload})
            reply = receive("reply.json")
            if reply.pop("lease_epoch", None) != epoch or (root / "request.json").exists():
                raise LeaseError("reply_identity")
            if reply["value"]["identity"] != owned_identity():
                raise LeaseError("qualification_worker_unproven")
            return reply

        try:
            if receive("ready.json") != launch:
                raise LeaseError("reply_identity")
            first = submit("poison", 1)
            second = submit("observe", 2)
        finally:
            job.stop()
        a, b = first["value"], second["value"]
        evidence = {
            "schema": "af.worker-isolation/v1",
            "adapter_id": scope.adapter_id, "adapter_sha256": scope.adapter_sha256,
            "workload_kind": scope.workload_kind, "before": a["before"],
            "identity_a": a["identity"], "identity_b": b["identity"],
            "perturbed": a["state"], "post_a": first.get("post_job_state"),
            "job_b": b["before"], "post_b": second.get("post_job_state"),
        }
        receipt = core.canonical_json(evidence).encode()
        qualification = ReuseQualification(sha256(receipt).hexdigest(), receipt)
        verify_isolation(scope, qualification)
        if job.result is None or identity_present(a["identity"]):
            raise LeaseError("release_unproven")
        return qualification


if len(sys.argv) == 3 and sys.argv[1] in ("--isolation-worker", "--qualified-worker"):
    raise SystemExit(isolation_worker(sys.argv[2], qualified=sys.argv[1] == "--qualified-worker"))


class LeaseRepairTests(unittest.TestCase):
    def worker(self, argv=worker_argv):
        authority = OfflineAuthority()
        worker = Residency("lease-test", argv, authorize=authority.authorize,
                           consume_permit=authority.consume, confirm_retired=authority.retired,
                           runtime_s=120, cwd=ROOT, env={"PATH": os.defpath,
                           "PYTHONPATH": os.pathsep.join(map(str, (ROOT, THIS.parent))),
                           "PYTHONDONTWRITEBYTECODE": "1"})
        self.addCleanup(lambda: worker.recover(deadline_ns=deadline()))
        return worker, authority

    def target(self, worker):
        return worker.launch_target(deadline_ns=deadline())

    def load(self, worker, permit=None):
        return worker.load(permit=permit, deadline_ns=deadline(), expires_ns=deadline(30))

    def refuse(self, reason, call, *args, **kwargs):
        with self.assertRaises(LeaseError) as cm:
            call(*args, **kwargs)
        self.assertEqual(cm.exception.code, reason)

    def test_unused_permit_cannot_cross_generation(self):
        worker, authority = self.worker()
        authority.issue("use", self.target(worker))
        authority.issue("stale", self.target(worker))
        first = self.load(worker, "use")
        worker.unload(first, deadline_ns=deadline())
        with launch_tripwire() as calls:
            self.refuse("permit_replayed_or_invalid", self.load, worker, "stale")
        self.assertEqual(calls, [])
        authority.issue("current", self.target(worker))
        self.assertGreater(self.load(worker, "current").generation, first.generation)

    def test_permit_epoch_mismatch_is_refused(self):
        worker, authority = self.worker()
        clock_id, generation, epoch = self.target(worker)
        authority.issue("wrong-epoch", (clock_id, generation, epoch + 1))
        with launch_tripwire() as calls:
            self.refuse("permit_replayed_or_invalid", self.load, worker, "wrong-epoch")
        self.assertEqual(calls, [])
        authority.issue("current", self.target(worker))
        self.assertEqual(self.load(worker, "current").lease_epoch, epoch)

    def test_permit_cannot_cross_worker(self):
        worker, authority = self.worker()
        other, _ = self.worker()
        authority.issue("other-worker", self.target(other))
        with launch_tripwire() as calls:
            self.refuse("permit_replayed_or_invalid", self.load, worker, "other-worker")
        self.assertEqual(calls, [])

    def test_failed_permit_preserves_generation(self):
        worker, authority = self.worker()
        authority.issue("first", self.target(worker))
        first = self.load(worker, "first")
        worker.unload(first, deadline_ns=deadline())
        with launch_tripwire() as calls:
            self.refuse("permit_replayed_or_invalid", self.load, worker, "first")
        self.assertEqual(calls, [])
        self.assertEqual(worker._generation, first.generation)
        self.assertEqual(self.target(worker)[1], first.generation + 1)

    def test_authority_rejects_same_target_replay(self):
        worker, authority = self.worker()
        target = self.target(worker)
        authority.issue("first", target)
        self.load(worker, "first")
        self.assertIs(authority.consume("first", authority.binding, *target), False)

    def test_unready_launch_preserves_generation_and_retires_attempt(self):
        worker, authority = self.worker(lambda root: [sys.executable, "-B", "-c", "raise SystemExit(42)"])
        candidate = self.target(worker)
        authority.issue("crash", candidate)
        authority.issue("unused-twin", candidate)
        self.refuse("worker_crashed", self.load, worker, "crash")
        self.assertEqual(worker._generation, 0)
        self.assertEqual([record[:2] for record in authority.drains], [candidate[:2]])
        worker.recover(deadline_ns=deadline())
        with launch_tripwire() as calls:
            self.refuse("permit_replayed_or_invalid", self.load, worker, "unused-twin")
        self.assertEqual(calls, [])

    def test_permit_advance_never_authorizes_warm_load(self):
        worker, authority = self.worker()
        candidate = self.target(worker)
        authority.issue("first", candidate)
        authority.issue("same-epoch", candidate)
        first = self.load(worker, "first")
        worker.release(first, deadline_ns=deadline())
        second = self.load(worker)
        worker.release(second, deadline_ns=deadline())
        with launch_tripwire() as calls:
            self.refuse("permit_unexpected", self.load, worker, "same-epoch")
        self.assertEqual(calls, [])
        self.assertGreater(second.lease_epoch, first.lease_epoch)
        self.assertNotIn("same-epoch", authority.used)

    def test_pin_change_after_release_drains_before_reuse(self):
        worker, authority = self.worker()
        authority.issue("first", self.target(worker))
        first = self.load(worker, "first")
        supervisor_pid = worker._job._backend._child.pid
        owned = (supervisor_pid, starttime(supervisor_pid))
        self.assertTrue(identity_present(owned))
        worker.release(first, deadline_ns=deadline())
        self.assertTrue(identity_present(owned))
        authority.binding = replace(authority.binding, runtime_sha256="e" * 64)
        with launch_tripwire() as calls:
            self.refuse("lease_identity", self.load, worker)
        self.assertEqual(calls, [])
        self.assertFalse(identity_present(owned))
        self.assertEqual(len(authority.drains), 1)

    def _boundary(self, *, lease_first):
        worker, authority = self.worker()
        authority.issue("first", self.target(worker))
        first = self.load(worker, "first")
        probe = worker.query(first, {}, deadline_ns=deadline())
        owned = probe["identity"]
        caller_deadline = deadline() if not lease_first else first.expires_ns + 1
        edge = min(caller_deadline, first.expires_ns)
        clock = SimpleNamespace(monotonic_ns=time.monotonic_ns, sleep=time.sleep)
        real_poll = proc.poll_exit

        def cross_boundary(*args, **kwargs):
            result = real_poll(*args, **kwargs)
            clock.monotonic_ns = lambda: edge
            return result

        with patch("tools.workers.lease.time", clock), patch.object(proc, "poll_exit", side_effect=cross_boundary):
            reason = "lease_expired" if lease_first else "request_deadline"
            self.refuse(reason, worker.query, first, {}, deadline_ns=caller_deadline)
        if lease_first:
            self.assertEqual(worker.last_terminal.close_reason, "lease_expired")
            self.assertEqual(len(authority.drains), 1)
            self.assertFalse(identity_present(owned))
        else:
            self.assertIsNone(worker.last_terminal)
            self.assertEqual(authority.drains, [])
            self.assertTrue(identity_present(owned))

    def test_liveness_lease_expiry_drains_owned_pid(self):
        self._boundary(lease_first=True)

    def test_earlier_caller_deadline_leaves_live_lease(self):
        self._boundary(lease_first=False)

    def test_second_job_in_same_unqualified_lease_is_refused(self):
        worker, authority = self.worker()
        authority.issue("first", self.target(worker))
        first = self.load(worker, "first")
        poison = worker.query(first, {"op": "poison"}, deadline_ns=deadline())
        self.assertFalse(any(poison["state"].values()))
        self.refuse("worker_reuse_unqualified", worker.query, first, {}, deadline_ns=deadline())
        self.assertFalse(identity_present(poison["identity"]))

    def _recycle(self, *, blender):
        with TemporaryDirectory() as scratch:
            worker, authority = self.worker(lambda root: worker_argv(root, blender=blender))
            authority.issue("first", self.target(worker))
            first = self.load(worker, "first")
            output_a = Path(scratch) / "job-a"
            poison = worker.query(first, {"op": "poison", "output": str(output_a)}, deadline_ns=deadline(20))
            self.assertFalse(any(poison["state"].values()), poison)
            self.assertTrue(identity_present(poison["identity"]))
            worker.release(first, deadline_ns=deadline())
            with launch_tripwire() as calls:
                self.refuse("permit_required", self.load, worker)
            self.assertEqual(calls, [])
            self.assertFalse(identity_present(poison["identity"]))
            authority.issue("fresh", self.target(worker))
            fresh = self.load(worker, "fresh")
            output_b = Path(scratch) / "job-b"
            clean = worker.query(fresh, {"op": "observe", "output": str(output_b)}, deadline_ns=deadline(20))
            self.assertEqual(clean["state"], dict.fromkeys(poison["state"], True))
            self.assertNotEqual(poison["identity"], clean["identity"])
            self.assertGreater(fresh.generation, first.generation)
            if blender:
                self.assertEqual(clean["fixture"], "rendered")
                for output in (output_a, output_b):
                    self.assertEqual({file.name for file in output.iterdir()},
                                     {"different.png", "prop.glb", "reference.png"})

    def test_recycle_dirty_dummy_between_jobs(self):
        self._recycle(blender=False)

    def test_recycle_dirty_blender_between_jobs(self):
        self._recycle(blender=True)

    def _export_precision(self, case, expected):
        with TemporaryDirectory() as scratch:
            worker, authority = self.worker(lambda root: worker_argv(root, blender=True, export=True))
            authority.issue("first", self.target(worker))
            lease = self.load(worker, "first")
            original = worker.query(lease, {"output": scratch, "case": case}, deadline_ns=deadline(20))
            self.assertEqual(original["outcome"], expected)
            self.assertEqual(original["during"], [7])
            self.assertEqual(original["rounding"], original["before"])

    def test_export_precision_restored_on_success(self):
        self._export_precision("ok", "exported")

    def test_export_precision_restored_on_exception(self):
        self._export_precision("fail", "export_failed")
