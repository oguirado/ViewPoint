"""Scoped residency conformance tests and an offline worker double."""
from contextlib import contextmanager
from dataclasses import replace
from hashlib import sha256
import os
from pathlib import Path
import runpy
import sys
from tempfile import TemporaryDirectory
import time
import unittest

from asset_factory import core, proc

ROOT = Path(__file__).resolve().parents[1]
THIS = Path(__file__).resolve()


def run_worker(mailbox, blender=False, probe=None, isolation=None):
    """Child entry point. Acknowledgements identify the exact outstanding request."""
    root = Path(mailbox)
    launch = core.load_json(root / "launch.json", max_bytes=65536)
    time.sleep(.025 if not blender else 0)
    core.atomic_write_json(root / "placement.json", {
        "affinity": sorted(os.sched_getaffinity(0)),
        "nice": os.getpriority(os.PRIO_PROCESS, 0),
    })
    core.atomic_write_json(root / "ready.json", launch)
    while True:
        path = root / "request.json"
        if not path.exists():
            time.sleep(.002)
            continue
        request = core.load_json(path, max_bytes=65536)
        path.unlink()
        payload = request["payload"]
        operation = payload.get("op")
        if operation == "crash":
            return 42
        if operation == "hang":
            time.sleep(30)
        before = isolation.state() if isolation is not None else None
        value = payload.get("text", "fixture").encode().hex()
        if blender:
            import bpy

            bpy.ops.wm.read_factory_settings(use_empty=False)
            original_args = sys.argv
            sys.argv = ["fixture_blender.py", "--", payload["output"]]
            try:
                runpy.run_path(str(ROOT / "tools/pipeline/fixture_blender.py"), run_name="__main__")
            finally:
                sys.argv = original_args
            value = "rendered"
        if probe is not None:
            value = probe(root, payload, value)
        identity = {name: item for name, item in request.items() if name != "payload"}
        if operation == "bad-echo":
            identity["lease_epoch"] += 1
        reply = {**identity, "value": value}
        if isolation is not None:
            if operation == "poison":
                isolation.perturb()
            if operation in ("poison", "observe"):
                reply["value"] = {"before": before, "state": isolation.state(),
                                  "identity": isolation.identity, "fixture": value}
            isolation.reset()
            reply["post_job_state"] = isolation.state()
        core.atomic_write_json(root / "reply.json", reply)


if len(sys.argv) == 3 and sys.argv[1] == "--resident-worker":
    raise SystemExit(run_worker(sys.argv[2]))


from tools.workers.lease import Binding, Ceiling, LeaseError, Residency


class OfflineAuthority:
    """Independent test authority and single-use permit ledger."""
    def __init__(self, provider="fixture"):
        self.binding = Binding(provider, "a" * 64, "b" * 64, "c" * 64, "d" * 64,
                               Ceiling(6 << 30, 8 << 30, 0, tuple(range(16))))
        self.issued = {}
        self.used = set()
        self.admitted = True
        self.release_confirmed = True
        self.operations = []
        self.drains = []

    def authorize(self, operation, owner, payload):
        self.operations.append(operation)
        return self.binding if owner == "lease-test" and self.admitted else None

    def issue(self, permit, target):
        self.issued[permit] = target

    def consume(self, permit, binding, clock_id, generation, lease_epoch):
        if (self.issued.get(permit) != (clock_id, generation, lease_epoch)
                or permit in self.used or binding != self.binding):
            return False
        self.used.add(permit)
        return True

    def retired(self, binding, clock_id, generation, unit_name):
        self.drains.append((clock_id, generation, unit_name))
        return self.release_confirmed


def dummy_argv(mailbox):
    return [sys.executable, "-B", str(THIS), "--resident-worker", str(mailbox)]


def supervisor(authority, argv=dummy_argv, *, adapter_scope=None):
    return Residency("lease-test", argv, authorize=authority.authorize,
                     consume_permit=authority.consume, confirm_retired=authority.retired,
                     runtime_s=120, cwd=ROOT, adapter_scope=adapter_scope,
                     env={"PATH": os.defpath, "PYTHONPATH": str(ROOT), "PYTHONDONTWRITEBYTECODE": "1"})


def deadline(seconds=5):
    return time.monotonic_ns() + int(seconds * 1_000_000_000)


@contextmanager
def launch_tripwire():
    """Observe proc.popen calls without replacing the real process helper."""
    previous = sys.getprofile()
    launch_code = proc.popen.__code__
    calls = []

    def observe(frame, event, argument):
        if event == "call" and frame.f_code is launch_code:
            calls.append(frame.f_locals["argv"])
        if previous is not None:
            previous(frame, event, argument)

    sys.setprofile(observe)
    try:
        yield calls
    finally:
        sys.setprofile(previous)


class LeaseTests(unittest.TestCase):
    def setUp(self):
        self.authority = OfflineAuthority()
        self.worker = supervisor(self.authority)
        self.addCleanup(self.cleanup_worker)

    def cleanup_worker(self):
        self.authority.admitted = self.authority.release_confirmed = True
        self.worker.recover(deadline_ns=deadline())

    def load(self, permit="first", *, expiry=5):
        if permit is not None and permit not in self.authority.issued:
            self.authority.issue(permit, self.worker.launch_target(deadline_ns=deadline()))
        return self.worker.load(expires_ns=deadline(expiry), deadline_ns=deadline(), permit=permit)

    def check_error(self, code, fn, *args, **kwargs):
        with self.assertRaises(LeaseError) as cm:
            fn(*args, **kwargs)
        self.assertEqual(cm.exception.code, code)

    def query(self, lease, op=None, *, seconds=5):
        return self.worker.query(lease, {"text": "undamaged", "op": op}, deadline_ns=deadline(seconds))

    def test_exactly_once_release(self):
        with launch_tripwire() as launch:
            first = self.load()
            self.assertEqual(self.query(first), b"undamaged".hex())
            released = self.worker.release(first, deadline_ns=deadline())
            self.assertEqual(released.state, "released")
            self.check_error("lease_already_closed", self.worker.release, first, deadline_ns=deadline())
            self.check_error("permit_required", self.load, None)
            self.assertEqual(len(launch), 1)
        self.assertEqual(len(self.authority.drains), 1)

    def test_stale_expired_and_replay_never_launch(self):
        first = self.load(expiry=1)
        with launch_tripwire() as tripwire:
            self.check_error("lease_identity", self.query, replace(first, generation=first.generation+1))
            self.check_error("lease_identity", self.query, replace(first, generation=True))
            self.check_error("lease_identity", self.query, replace(first, lease_epoch=first.lease_epoch+1))
            self.check_error("lease_identity", self.query, replace(first, clock_id="past-supervisor"))
            time.sleep(max(0, (first.expires_ns - time.monotonic_ns()) / 1e9))
            self.check_error("lease_expired", self.query, first)
            self.check_error("permit_replayed_or_invalid", self.load)
            self.check_error("permit_required", self.load, None)
            self.assertEqual(len(tripwire), 0)
        second = self.load("second")
        self.assertGreater(second.generation, first.generation)

    def test_scope_and_authority_refusal_precede_permit_consumption(self):
        with launch_tripwire() as tripwire:
            self.authority.admitted = False
            self.check_error("lease_authority", self.load)
            self.authority.admitted = True
            self.authority.binding = replace(self.authority.binding, ceiling=Ceiling(1, 2, 0, tuple(range(16))))
            self.check_error("resource_ceiling_unproven", self.load)
            self.assertEqual(self.authority.used, set())
            self.assertEqual(len(tripwire), 0)

    def test_changed_pin_and_crash_fence_until_independent_reconciliation(self):
        first = self.load()
        self.authority.binding = replace(self.authority.binding, runtime_sha256="e" * 64)
        self.check_error("lease_identity", self.query, first)
        second = self.load("second")
        self.assertGreater(second.generation, first.generation)
        self.authority.release_confirmed = False
        self.check_error("worker_crashed", self.query, second, "crash")
        with launch_tripwire() as tripwire:
            self.check_error("generation_broken", self.load, "third")
            self.check_error("release_unproven", self.worker.recover, deadline_ns=deadline())
            self.assertEqual(len(tripwire), 0)
        self.authority.release_confirmed = True
        self.worker.recover(deadline_ns=deadline())
        third = self.load("third")
        self.assertGreater(third.generation, second.generation)

    def test_in_flight_deadline_breaks_generation_and_drains(self):
        first = self.load()
        self.check_error("request_deadline", self.query, first, "hang", seconds=.06)
        self.assertEqual(self.worker.last_terminal.state, "invalidated")
        self.assertEqual(self.worker.last_terminal.close_reason, "request_deadline")
        self.assertEqual(len(self.authority.drains), 1)
        with launch_tripwire() as tripwire:
            self.check_error("generation_broken", self.load, "second")
            self.assertEqual(len(tripwire), 0)
        self.worker.recover(deadline_ns=deadline())
        second = self.load("second")
        self.assertGreater(second.generation, first.generation)
        self.assertEqual(self.query(second), b"undamaged".hex())

    def test_wrong_reply_epoch_breaks_generation(self):
        lease = self.load()
        self.check_error("reply_identity", self.query, lease, "bad-echo")
        self.assertEqual(self.worker.last_terminal.close_reason, "reply_identity")

    def test_expired_before_claim_has_no_side_effect_and_inflight_expiry_drains(self):
        first = self.load()
        self.check_error("request_deadline", self.worker.query, first, {}, deadline_ns=0)
        self.assertEqual(self.query(first), b"undamaged".hex())
        self.worker.unload(first, deadline_ns=deadline())
        second = self.load("second", expiry=.6)
        self.check_error("request_deadline", self.query, second, "hang")
        self.assertEqual(self.worker.last_terminal.state, "invalidated")
        self.assertEqual(len(self.authority.drains), 2)

    def test_unproven_unload_cannot_claim_release(self):
        first = self.load()
        self.authority.release_confirmed = False
        self.check_error("release_unproven", self.worker.unload, first, deadline_ns=deadline())
        self.assertEqual(self.worker.last_terminal.state, "invalidated")
        self.check_error("generation_broken", self.load, "second")
        self.authority.release_confirmed = True
        self.worker.recover(deadline_ns=deadline())
        self.assertGreater(self.load("second").generation, first.generation)


if __name__ == "__main__":
    unittest.main()
