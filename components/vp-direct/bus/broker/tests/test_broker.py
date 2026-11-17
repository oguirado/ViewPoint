import concurrent.futures
import hashlib
import inspect
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

TESTS = Path(__file__).resolve().parent
DIRECT = TESTS.parents[2]
sys.path.insert(0, str(DIRECT))
from bus.broker import Broker, BrokerError, call
from bus.broker import broker as implementation, journal, port
from jsonschema.exceptions import ValidationError
from schemas.validation import validate_contract

ALIAS = ("cdp:tab", "x11:window", "atspi:object")
WITNESS = hashlib.sha256(b"verified fixture observation").hexdigest()


def example(name):
    return json.loads((DIRECT / "schemas/tests/examples" / f"{name}_v1.valid.json").read_bytes())


def variant(module, before, after, symbol):
    """Compile one deliberately altered implementation in memory, isolated from normal imports."""
    source = inspect.getsource(module)
    if source.count(before) != 1:
        raise AssertionError("mutant no longer targets one guard")
    namespace = dict(vars(module))
    exec(compile(source.replace(before, after), module.__file__, "exec"), namespace)
    return namespace[symbol]


def worker(args):
    root, fd, target = args
    sys.path.insert(0, root)
    with socket.socket(fileno=int(fd)) as sock:
        try:
            result = call(sock, "try_acquire", target=target, authority_epoch=1,
                          incarnation="inc", request_id="r." + str(os.getpid()))
            print("acquired", result)
        except BrokerError as exc:
            print(exc)


class BrokerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=TESTS)
        self.addCleanup(self.temp.cleanup)
        self.rt = Path(self.temp.name)
        self.b = self.start()

    def start(self, cls=Broker):
        broker = cls(self.rt)
        self.addCleanup(broker.close)
        binding = example("vp.surface_binding")
        broker.bind("session", binding, ALIAS, lease_state="AGENT")
        return broker

    def rows(self):
        return [json.loads(line) for line in (self.rt / "events.jsonl").read_bytes().splitlines()]

    def enqueue(self, name):
        return self.b.enqueue("owner", name, ALIAS[0], "inc", 1)

    def acquire(self, name, alias=ALIAS[0]):
        return self.b.try_acquire("owner", alias, "inc", 1, name)

    def denied(self, code, fn, *args, **kwargs):
        with self.assertRaises(BrokerError) as err:
            fn(*args, **kwargs)
        self.assertEqual(str(err.exception), code)

    def test_only_one_writer(self):
        with self.assertRaises(BlockingIOError):
            with Broker(self.rt):
                pass

    def test_mutant_second_writer(self):
        with patch.object(journal.fcntl, "flock", return_value=None):
            with self.assertRaises(AssertionError):
                self.test_only_one_writer()

    def test_other_process_cannot_open_writer(self):
        code = """import sys
sys.path.insert(0, sys.argv[1])
from bus.broker import Broker
try:
    with Broker(sys.argv[2]):
        sys.exit(3)
except BlockingIOError:
    pass"""
        subprocess.run([sys.executable, "-I", "-B", "-c", code, str(DIRECT), str(self.rt)],
                       check=True, timeout=10)

    def test_missing_authority_and_human_start(self):
        binding = dict(example("vp.surface_binding"), surface_id="login", endpoint="login-cdp")
        self.denied("NO_LEASE", self.b.bind, "new-session", binding)
        self.b.bind("new-session", binding, lease_state="HUMAN")
        self.assertEqual(self.b.lease("new-session")["holder"], "operator")
        self.denied("NO_LEASE", self.b.try_acquire, "owner", "login-cdp", "inc", 1, "r")
        self.b.close()
        self.b = self.start()
        self.denied("NO_LEASE", self.b.bind, "new-session", binding, lease_state="AGENT")
        self.b.human_lease("new-session", "AGENT", 1, "operator")
        self.assertEqual(self.b.lease("new-session"), {"holder": "owner", "state": "AGENT", "authority_epoch": 2})

    def test_take_return_and_notification(self):
        with self.b.agent_port("owner") as sock:
            row = call(sock, "human_needed", session_id="session", goal_id="g.fixture", reason="login")
            self.assertEqual(row["rec"], "HUMAN_NEEDED")
            self.assertEqual(set(row), {"by", "v", "seq", "ts_ms", "rec", "goal_id",
                                        "session_id", "reason", "raised_by"})
        self.b.human_lease("session", "HUMAN", 1, "login")
        self.denied("NO_LEASE", self.acquire, "during-take")
        self.b.human_lease("session", "AGENT", 2, "operator")
        self.denied("NO_LEASE", self.acquire, "old-epoch")
        self.b.try_acquire("owner", ALIAS[0], "inc", 3, "new-epoch")
        controls = [r for r in self.rows() if r["rec"] == "control"]
        self.assertTrue(all(r["by"] == "broker" for r in controls))
        self.assertEqual([r["op"] for r in controls if "lease" in r], ["take", "return"])
        for row in controls:
            if "lease" in row:
                validate_contract("vp.human_lease/v1", row["lease"])

    def assert_agent_return_refused(self, provision):
        self.b.human_lease("session", "HUMAN", 1, "operator")
        before = self.rows()
        with provision(self.b, "owner") as sock:
            self.denied("NO_LEASE", call, sock, "human_lease", session_id="session",
                        state="AGENT", authority_epoch=2, reason="operator")
            self.denied("NO_LEASE", call, sock, "control", by="broker", state="AGENT")
        self.assertEqual(before, self.rows())

    def test_agent_control_refused(self):
        self.assert_agent_return_refused(port.provision)

    def test_mutant_agent_written_return(self):
        bad = variant(port, 'op, args = req["op"], req["args"]', '''op, args = req["op"], req["args"]
                        if op == "human_lease":
                            _send(server, {"result": broker.human_lease(**args)})
                            continue''', "provision")
        with self.assertRaises(AssertionError):
            self.assert_agent_return_refused(bad)

    def test_provisioned_identity_cannot_be_replaced(self):
        with self.b.agent_port("outsider") as sock:
            for extra in ({}, {"agent_id": "owner"}, {"by": "broker"}):
                self.denied("NO_LEASE", call, sock, "try_acquire", target=ALIAS[0],
                            authority_epoch=1, incarnation="inc", request_id="r", **extra)
            for privileged in ("bind", "finish", "reconcile", "perception_transition"):
                self.denied("NO_LEASE", call, sock, privileged)

    def test_queue_full_duplicate_and_fifo(self):
        for index in range(8):
            self.enqueue(f"r{index}")
        before = self.rows()
        self.denied("QUEUE_FULL", self.enqueue, "overflow")
        self.denied("DUPLICATE_EVENT", self.enqueue, "r0")
        self.denied("NO_LEASE", self.acquire, "r2")
        self.assertEqual(before, self.rows())
        self.b.drop("r0", "STALE_FRAME")
        self.b.dispatch("owner", self.acquire("r1"))
        self.enqueue("overflow")

    def test_third_drop_is_terminal(self):
        self.enqueue("r")
        for index in (1, 2, 3):
            row = self.b.drop("r", "TURN_TIMEOUT")
            self.assertEqual((row["drops"], row["requeued"]), (index, index < 3))
        self.denied("NO_LEASE", self.acquire, "r")

    def test_mutant_drops_less_than_or_equal(self):
        bad = variant(implementation, "drops < MAX_DROPS", "drops <= MAX_DROPS", "Broker")
        with patch.object(Broker, "_can_requeue", bad._can_requeue):
            with self.assertRaises(AssertionError):
                self.test_third_drop_is_terminal()

    def test_post_dispatch_timeout_never_requeues(self):
        self.enqueue("work")
        lock = self.acquire("work")
        self.b.dispatch("owner", lock)
        row = self.b.finish("owner", lock, "TURN_TIMEOUT")
        self.assertFalse(row["requeued"])
        self.assertEqual(row["verdict"], "OUTCOME_UNCERTAIN")
        self.denied("NO_LEASE", self.b.drop, "work", "TURN_TIMEOUT")
        self.denied("DUPLICATE_EVENT", self.acquire, "work")
        self.denied("NO_LEASE", self.acquire, "other", ALIAS[2])

    def test_mutant_requeue_after_dispatch(self):
        before = "return self.quarantine(agent_id, lock_id)"
        bad = variant(implementation, before,
                      'return self._close_event(self._events[lock["request_id"]], verdict, 1, True, lock_id=lock_id)',
                      "Broker")
        with patch.object(Broker, "finish", bad.finish):
            with self.assertRaises(AssertionError):
                self.test_post_dispatch_timeout_never_requeues()

    def test_aliases_exclude_competing_acquisitions(self):
        lock = self.acquire("r")
        for alias in ALIAS[1:]:
            self.denied("NO_LEASE", self.acquire, "competitor", alias)
        self.b.release("owner", lock)
        second = self.acquire("competitor", ALIAS[2])
        self.denied("NO_LEASE", self.b.release, "owner", lock)
        self.assertNotEqual(lock, second)

    def test_mutant_two_alias_holders(self):
        bad = variant(implementation, 'return session, b["surface_id"]', 'return session, target', "Broker")
        with patch.object(Broker, "_admit", bad._admit):
            with self.assertRaises(AssertionError):
                self.test_aliases_exclude_competing_acquisitions()

    def test_cross_process_alias_contention(self):
        def contender(alias):
            with self.b.agent_port("owner") as sock:
                proc = subprocess.run([sys.executable, "-I", "-B", __file__, str(DIRECT),
                                       str(sock.fileno()), alias], pass_fds=(sock.fileno(),),
                                      capture_output=True, text=True, check=True, timeout=15)
            return proc.stdout.strip().split()[0]

        with concurrent.futures.ThreadPoolExecutor() as pool:
            outcomes = list(pool.map(contender, ALIAS))
        self.assertCountEqual(outcomes, ["acquired", "NO_LEASE", "NO_LEASE"])
        rows = [r for r in self.rows() if r.get("op") == "acquire"]
        self.assertEqual([r["surface_id"] for r in rows], ["surface"])

    def test_unknown_needs_reconcile(self):
        lock = self.acquire("r")
        self.b.quarantine("owner", lock)
        self.denied("UNCERTAIN_OUTCOME", self.b.release, "owner", lock)
        self.denied("NO_LEASE", self.b.reconcile, lock, "unrelated", "VERIFIED", WITNESS)
        self.denied("UNCERTAIN_OUTCOME", self.b.reconcile, lock, "r", "UNKNOWN", WITNESS)
        self.denied("UNCERTAIN_OUTCOME", self.b.reconcile, lock, "r", "NO_EFFECT", "")
        self.b.reconcile(lock, "r", "NO_EFFECT", WITNESS)
        self.acquire("other")

    def test_mutant_release_unknown(self):
        bad = variant(implementation, 'if lock["status"] != "held":\n            raise BrokerError("UNCERTAIN_OUTCOME")',
                      'if False:\n            raise BrokerError("UNCERTAIN_OUTCOME")', "Broker")
        with patch.object(Broker, "release", bad.release):
            with self.assertRaises(AssertionError):
                self.test_unknown_needs_reconcile()

    def test_handoff_retires_only_stale_queued_work(self):
        self.enqueue("old")
        self.b.bind("other", dict(example("vp.surface_binding"), endpoint="other-endpoint",
                                  surface_id="other-surface"), lease_state="AGENT")
        self.b.enqueue("owner", "keep", "other-surface", "inc", 1)
        self.b.human_lease("session", "HUMAN", 1, "operator")
        self.b.human_lease("session", "AGENT", 2, "operator")
        self.assertEqual([(r["event_id"], r["verdict"], r["requeued"]) for r in self.rows()
                          if r["rec"] == "close"], [("old", "NO_LEASE", False)])
        self.b.try_acquire("owner", "other-surface", "inc", 1, "keep")

    def test_take_fences_inflight(self):
        self.enqueue("r")
        lock = self.acquire("r")
        self.b.dispatch("owner", lock)
        lease = self.b.human_lease("session", "HUMAN", 1, "operator")["lease"]
        self.assertEqual(lease["outstanding"], [{"request_id": "r", "disposition": "UNCERTAIN_OUTCOME"}])
        self.denied("NO_LEASE", self.b.finish, "owner", lock, "VERIFIED")
        self.denied("UNCERTAIN_OUTCOME", self.b.human_lease, "session", "AGENT", 2, "operator")
        self.b.reconcile(lock, "r", "VERIFIED", WITNESS)
        self.b.human_lease("session", "AGENT", 2, "operator")
        self.denied("DUPLICATE_EVENT", self.b.enqueue, "owner", "r", ALIAS[0], "inc", 3)

    def test_recover_dispatched_and_queued(self):
        self.enqueue("first")
        self.enqueue("second")
        self.b.dispatch("owner", self.acquire("first"))
        self.b.close()
        self.b = self.start()
        held = self.b.lock_status(ALIAS[1])
        self.assertEqual((held["status"], held["request_id"]), ("UNKNOWN", "first"))
        self.denied("NO_LEASE", self.acquire, "second")
        self.b.reconcile(held["lock_id"], "first", "NO_EFFECT", WITNESS)
        self.b.dispatch("owner", self.acquire("second"))
        self.denied("DUPLICATE_EVENT", self.acquire, "first")

    def test_abrupt_writer_exit_fences_possible_send(self):
        self.b.close()
        code = """import os, sys
sys.path.insert(0, sys.argv[1])
from bus.broker import Broker
b = Broker(sys.argv[2])
b.enqueue("owner", "interrupted", "surface", "inc", 1)
b.dispatch("owner", b.try_acquire("owner", "surface", "inc", 1, "interrupted"))
os._exit(19)"""
        child = subprocess.run([sys.executable, "-I", "-B", "-c", code, str(DIRECT), str(self.rt)],
                               timeout=10)
        self.assertEqual(child.returncode, 19)
        self.b = self.start()
        lock = self.b.lock_status(ALIAS[2])
        self.assertEqual(lock["status"], "UNKNOWN")
        self.denied("DUPLICATE_EVENT", self.acquire, "interrupted")
        self.denied("NO_LEASE", self.acquire, "another")
        self.b.reconcile(lock["lock_id"], "interrupted", "NO_EFFECT", WITNESS)
        self.assertEqual([r["event_id"] for r in self.rows() if r["rec"] == "turn"], ["interrupted"])

    def test_recover_held_lock(self):
        lock = self.acquire("lost")
        self.b.close()
        self.b = self.start()
        self.denied("UNCERTAIN_OUTCOME", self.b.release, "owner", lock)
        self.b.reconcile(lock, "lost", "VERIFIED", WITNESS)
        self.denied("DUPLICATE_EVENT", self.acquire, "lost")

    def test_binding_and_perception_replay(self):
        binding = self.b.binding(ALIAS[1])
        binding["owner"] = "impostor"
        self.denied("identity_mismatch", self.b.bind, "session", dict(binding, surface_id="other"))
        self.denied("identity_mismatch", self.b.try_acquire, "owner", ALIAS[0], "old", 1, "r")
        self.denied("NO_LEASE", self.b.try_acquire, "owner", ALIAS[0], "inc", True, "r")
        with self.assertRaises(ValidationError):
            self.b.bind("session", dict(binding, extra=1))
        state = example("vp.perception_state")
        state["authority_epoch"] = 1
        self.b.perception_transition(state)
        self.b.perception_transition(dict(state, state="WAKING", since_mono_ns=6))
        self.denied("STALE_FRAME", self.b.perception_transition, state)
        self.denied("NO_LEASE", self.b.perception_transition, dict(state, authority_epoch=0))
        self.denied("identity_mismatch", self.b.perception_transition, dict(state, session_id="other"))
        self.b.close()
        self.b = self.start()
        self.assertEqual(self.b.binding(ALIAS[2])["owner"], "owner")
        self.assertEqual(self.b.perception_state(ALIAS[0])["state"], "WAKING")
        self.assertEqual([r["perception"]["state"] for r in self.rows() if r.get("op") == "perception"],
                         ["ASLEEP", "WAKING"])

    def test_journal_torn_fails_closed(self):
        self.b.close()
        with (self.rt / "events.jsonl").open("ab") as log:
            log.write(b'{"rec":')
        self.denied("JOURNAL_TORN", Broker, self.rt)

    def test_failed_sync_grants_nothing(self):
        with patch.object(journal.os, "fsync", side_effect=OSError("fixture sync failure")):
            with self.assertRaises(OSError):
                self.acquire("uncertain-append")
        self.denied("NO_LEASE", self.acquire, "next")
        self.b.close()
        self.b = self.start()
        self.assertEqual(self.b.lock_status(ALIAS[0])["status"], "UNKNOWN")

    def test_handoff_waits_for_verified_predecessor(self):
        self.b.bind("second-session", dict(example("vp.surface_binding"), owner="reader",
                                           endpoint="reader-endpoint", surface_id="second"), lease_state="AGENT")
        def request(actor, target, after):
            task = f"t.{actor}.1"
            tm = {"v": 1, "task_id": task, "microtask_id": task + ".m1", "text": "fixture",
                  "supersedes": None, "after": after}
            self.b.enqueue(actor, tm["microtask_id"], target, "inc", 1, tm)
            return tm["microtask_id"]

        first = request("owner", "surface", [])
        second = request("reader", "second", [first])
        lock = self.acquire(first)
        self.b.dispatch("owner", lock)
        self.denied("MICROTASK_BUSY", self.b.try_acquire, "reader", "second", "inc", 1, second)
        self.b.finish("owner", lock, "VERIFIED")
        self.b.try_acquire("reader", "second", "inc", 1, second)


if __name__ == "__main__":
    worker(sys.argv[1:])
