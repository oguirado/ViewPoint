"""C4 tests against actual files and the C1 dispatch boundary."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import importlib.util
import json
import os
from pathlib import Path
import socket
import sys
from tempfile import TemporaryDirectory
from threading import Event
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from jsonschema import ValidationError


ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT.parent / "schemas" / "tests" / "examples"


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


m = load("vp_oflane", ROOT / "__init__.py")
c = load("vp_oflane_test_control", ROOT.parent / "control" / "core" / "control.py")
IDENTITY = c.LaneIdentity("agent", "fixture-agent")
KEY = b"synthetic verifier key for unit tests only"
WINDOW = "vp_window_summon"
CAPTURE = "vp_capture_call"


def request(request_id="fixture", *, tool=WINDOW, args=None, dry_run=False):
    return {
        "schema": c.REQUEST, "op": tool, "request_id": request_id, "dry_run": dry_run,
        "context": {"caller": IDENTITY.caller, "agent_instance": IDENTITY.agent_instance},
        "args": {"session_id": "fixture"} if args is None else args,
    }


def allow(_request, _risk):
    return True


class MailboxTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory(dir=ROOT / "tests", prefix=".c4-")))
        self.mailbox = m.Mailbox(self.root, IDENTITY)
        self.calls = []
        self.capability = json.loads((EXAMPLES / "vp.capability_v1.valid.json").read_bytes())
        self.adapters = json.loads((EXAMPLES / "adapter_pairs.json").read_bytes())

    def dispatcher(self, *, run=None, pace=allow, risk="observar"):
        def execute(req):
            self.calls.append(req)
            return c.Outcome(True, "OK")

        def preview(req):
            self.calls.append(req)
            return c.Outcome(True, "DRY_RUN")

        return c.Dispatcher(
            c.CapabilityRegistry([dict(self.capability, risk_class=risk)]),
            c.RequestStore(self.root / "requests.sqlite"), pace_risk=pace, lease_epoch=allow,
            tools={WINDOW: c.Handler(run or execute, preview)},
        )

    def response(self, envelope, *, result=None, code="OK"):
        return {
            "schema": c.REPLY, "request_id": envelope["request_id"], "op": envelope["op"],
            "ok": True, "result": {} if result is None else result, "code": code,
            "effective_request_sha256": c.digest(envelope), "evidence": [], "not_verified": [],
        }

    def exercise_files_only(self, run_one):
        envelope = request(dry_run=True)
        with patch("socket.socket", side_effect=AssertionError("socket transport used")):
            self.mailbox.submit(envelope)
            self.assertEqual(self.mailbox.pending(), ("fixture",))
            self.assertEqual(json.loads((self.root / "fixture.req.json").read_bytes()), envelope)
            reply = run_one()
            self.assertEqual(self.mailbox.pending(), ())
            self.assertEqual(json.loads((self.root / "fixture.reply.json").read_bytes()), reply)
            self.assertEqual(reply["code"], "DRY_RUN")

    def test_file_exchange_and_independent_terminal_verification(self):
        dispatcher = self.dispatcher()
        self.exercise_files_only(lambda: self.mailbox.run_one("fixture", dispatcher))
        self.assertIsNone(self.mailbox.completion("fixture", KEY))
        self.assertFalse((self.root / "fixture.done").exists())

        def inspect(request, reply):
            # Independent fixture evidence: the preview really ran with the submitted envelope.
            return self.calls == [request] and request["dry_run"] and reply["code"] == "DRY_RUN"

        m.Verifier(m.Mailbox(self.root, IDENTITY), KEY, inspect).seal("fixture")
        self.assertEqual(self.mailbox.completion("fixture", KEY)["code"], "DRY_RUN")
        self.assertEqual((self.root / "fixture.done").read_text().splitlines()[0],
                         "<!-- fixture_DONE -->")

    def test_bound_capture_result_must_match_request_selector(self):
        capture = self.adapters[CAPTURE]
        envelope = request(tool=CAPTURE, args=capture["screenshot"]["args"])
        other = self.response(envelope, result=capture["frame_subscribe"]["result"])
        c.validate_contract(c.REPLY, other)
        self.mailbox.submit(envelope)
        with self.assertRaises(ValidationError):
            self.mailbox.run_one("fixture", SimpleNamespace(dispatch_mailbox=lambda *_: other))
        self.assertFalse((self.root / "fixture.reply.json").exists())

        envelope = dict(envelope, request_id="qualified-capture")
        expected = self.response(envelope, result=capture["screenshot"]["result"])
        self.mailbox.submit(envelope)
        self.mailbox.run_one("qualified-capture", SimpleNamespace(dispatch_mailbox=lambda *_: expected))
        verifier = m.Verifier(self.mailbox, KEY, lambda _, reply: reply == expected)
        verifier.seal("qualified-capture")
        self.assertEqual(self.mailbox.completion("qualified-capture", KEY), expected)

    def test_mcp_cli_mailbox_share_the_same_effective_request(self):
        dispatcher = self.dispatcher()
        envelope = request(dry_run=True)
        args = dict(envelope["args"], request_id="fixture", dry_run=True)
        mcp = dispatcher.dispatch_mcp(WINDOW, args, IDENTITY)
        cli = dispatcher.dispatch_cli("window-summon", envelope, IDENTITY)
        self.mailbox.submit(envelope)
        file_reply = self.mailbox.run_one("fixture", dispatcher)
        self.assertEqual(file_reply, cli)
        self.assertEqual(file_reply, mcp)
        self.assertEqual(file_reply["effective_request_sha256"], c.digest(envelope))
        self.assertEqual(self.calls, [envelope])

    def test_reply_with_mismatched_digest_is_refused_before_publication(self):
        envelope = request()
        reply = self.response(envelope)
        reply["effective_request_sha256"] = c.digest(dict(envelope, dry_run=True))
        self.mailbox.submit(envelope)
        with self.assertRaisesRegex(m.MailboxError, "REPLY_REQUEST_MISMATCH"):
            self.mailbox.run_one("fixture", SimpleNamespace(dispatch_mailbox=lambda *_: reply))
        self.assertFalse((self.root / "fixture.reply.json").exists())

    def test_replay_and_mutated_id_after_restart(self):
        dispatcher = self.dispatcher()
        envelope = request()
        self.mailbox.submit(envelope)
        original = self.mailbox.run_one("fixture", dispatcher)
        reopened = m.Mailbox(self.root, IDENTITY)
        reopened.submit(deepcopy(envelope))
        self.assertEqual(reopened.run_one("fixture", self.dispatcher()), original)
        with self.assertRaisesRegex(m.MailboxError, "^REQUEST_ID_CONFLICT$"):
            reopened.submit(dict(envelope, dry_run=True))
        self.assertEqual(self.calls, [envelope])

    def test_c1_shared_store_refuses_cross_lane_id_conflict(self):
        dispatcher = self.dispatcher()
        first = request()
        c1_reply = dispatcher.dispatch_cli("window-summon", first, IDENTITY)
        self.assertTrue(c1_reply["ok"])
        self.mailbox.submit(dict(first, dry_run=True))
        reply = self.mailbox.run_one("fixture", dispatcher)
        self.assertFalse(reply["ok"])
        self.assertEqual(reply["code"], "REQUEST_ID_CONFLICT")
        self.assertEqual(self.calls, [first])

    def test_literal_arguments_and_filename_confinement(self):
        dispatcher = self.dispatcher()
        for value in ("fixture;exit", "a|b", "$(example)", "a\nb", "\\value", "a\x00b"):
            with self.subTest(value=value), self.assertRaisesRegex(m.MailboxError, "METACHARACTERS_REFUSED"):
                self.mailbox.submit(request(args={"session_id": value}))
        with self.assertRaisesRegex(m.MailboxError, "INVALID_REQUEST_ID"):
            self.mailbox.submit(request("../outside"))
        envelope = request(args={"session_id": "literal value"})
        self.mailbox.submit(envelope)
        self.assertTrue(self.mailbox.run_one("fixture", dispatcher)["ok"])
        self.assertEqual(self.calls, [envelope])

    def test_direct_file_submission_cannot_bypass_literal_or_filename_guards(self):
        envelope = request(args={"session_id": "fixture;other"})
        path = self.root / "fixture.req.json"
        path.write_bytes(c.canonical_bytes(envelope))
        with self.assertRaisesRegex(m.MailboxError, "METACHARACTERS_REFUSED"):
            self.mailbox.run_one("fixture", self.dispatcher())
        path.write_bytes(c.canonical_bytes(request("another")))
        with self.assertRaisesRegex(m.MailboxError, "REQUEST_ID_MISMATCH"):
            self.mailbox.run_one("fixture", self.dispatcher())
        self.assertEqual(self.calls, [])

    def test_request_read_refuses_a_fifo(self):
        os.mkfifo(self.root / "fixture.req.json")
        with self.assertRaisesRegex(m.MailboxError, "^MAILBOX_FILE_REQUIRED$"):
            self.mailbox._request("fixture")

    def test_reply_path_accepts_the_longest_id_that_fits(self):
        longest = "a" * self.mailbox._max_id
        self.assertEqual(self.mailbox._path(longest, ".reply.json"), self.root / (longest + ".reply.json"))
        with self.assertRaisesRegex(m.MailboxError, "^INVALID_REQUEST_ID$"):
            self.mailbox._path(longest + "a", ".reply.json")

    def test_mailbox_rejects_a_symlink_to_an_existing_state_directory(self):
        root = self.root / "other-mailbox"
        root.mkdir()
        (root / ".oflane-state").symlink_to(self.mailbox._state, target_is_directory=True)
        with self.assertRaisesRegex(m.MailboxError, "^MAILBOX_STATE_DIRECTORY_REQUIRED$"):
            m.Mailbox(root, IDENTITY)

    def test_lock_refuses_a_fifo_before_flock(self):
        key = "fixture"
        lock_file = self.mailbox._state / ("request-" + m._sha(key.encode()) + ".lock")
        os.mkfifo(lock_file)
        with patch.object(m.fcntl, "flock") as flock:
            with self.assertRaisesRegex(m.MailboxError, "^MAILBOX_FILE_REQUIRED$"):
                with self.mailbox._lock("request", key):
                    self.fail("non-regular lock yielded")
            flock.assert_not_called()

    def test_enviar_requires_explicit_human_gate_and_stamps_identity(self):
        subject = request()
        subject["context"] = {"caller": "operator", "agent_instance": "spoofed"}
        decisions = []

        def human_gate(req, risk):
            decisions.append((req["context"], risk))
            return "HUMAN_NEEDED" if risk == "enviar" and not approved else True

        approved = False
        dispatcher = self.dispatcher(pace=human_gate, risk="enviar")
        self.mailbox.submit(subject)
        reply = self.mailbox.run_one("fixture", dispatcher)
        self.assertEqual(reply["code"], "HUMAN_NEEDED")
        self.assertEqual(self.calls, [])
        self.assertEqual(decisions, [(request()["context"], "enviar")])
        self.assertEqual(reply["effective_request_sha256"], c.digest(request()))

        approved = True
        self.mailbox.submit(dict(subject, request_id="approved"))
        self.assertTrue(self.mailbox.run_one("approved", dispatcher)["ok"])
        self.assertEqual(self.calls, [request("approved")])

    def test_abort_before_dispatch_publishes_held_order(self):
        self.mailbox.submit(request())
        self.assertTrue(self.mailbox.abort("fixture"))
        self.assertTrue(self.mailbox.abort("fixture"))
        reply = self.mailbox.run_one("fixture", self.dispatcher())
        self.assertEqual((reply["ok"], reply["code"], reply["not_verified"]),
                         (False, "HOLD", ["aborted_before_dispatch"]))
        self.assertEqual(self.calls, [])
        self.assertFalse(self.mailbox.abort("fixture"))

    def test_abort_during_dispatch_never_signals_or_retries(self):
        entered, finish = Event(), Event()

        def execute(req):
            entered.set()
            if not finish.wait(5):
                raise AssertionError("fixture did not release the worker")
            self.calls.append(req)
            return c.Outcome(True, "OK")

        dispatcher = self.dispatcher(run=execute)
        self.mailbox.submit(request())
        with patch("os.kill", side_effect=AssertionError("process signalled")), \
             patch("os.killpg", side_effect=AssertionError("process group signalled")), \
             patch("subprocess.Popen.send_signal", side_effect=AssertionError("process signalled")), \
             ThreadPoolExecutor() as pool:
            future = pool.submit(self.mailbox.run_one, "fixture", dispatcher)
            try:
                self.assertTrue(entered.wait(5))
                self.assertTrue(self.mailbox.abort("fixture"))
                self.assertFalse(future.done())
            finally:
                finish.set()
            reply = future.result(timeout=5)
        self.assertEqual((reply["ok"], reply["code"]), (False, "UNCERTAIN_OUTCOME"))
        self.assertEqual(self.mailbox.run_one("fixture", dispatcher), reply)
        self.assertEqual(self.calls, [request()])

    def test_interrupted_dispatch_is_not_resent(self):
        self.mailbox.submit(request())

        def interrupted(req, _identity):
            self.calls.append(req)
            raise RuntimeError("simulated loss after dispatch claim")

        with self.assertRaisesRegex(RuntimeError, "simulated loss"):
            self.mailbox.run_one("fixture", SimpleNamespace(dispatch_mailbox=interrupted))
        reply = m.Mailbox(self.root, IDENTITY).run_one("fixture", self.dispatcher())
        self.assertEqual(reply["code"], "UNCERTAIN_OUTCOME")
        self.assertEqual(self.calls, [request()])

    def test_resource_locks_block_concurrent_execution(self):
        worker = m.Mailbox(self.root, IDENTITY)
        dispatcher = self.dispatcher()
        for field in ("session_id", "capsule_id"):
            with self.subTest(field=field):
                name = field + "-fixture"
                envelope = request(field, args={"session_id": name})
                envelope["context"][field] = name
                self.mailbox.submit(envelope)
                observed = Event()
                acquire = worker._lock

                def record_lock(scope, key):
                    if scope == field:
                        observed.set()
                    return acquire(scope, key)

                with ThreadPoolExecutor() as pool, patch.object(worker, "_lock", record_lock):
                    with self.mailbox._lock(field, name):
                        pending = pool.submit(worker.run_one, field, dispatcher)
                        self.assertTrue(observed.wait(5))
                        self.assertFalse(pending.done())
                        self.assertFalse((self.root / (field + ".reply.json")).exists())
                    self.assertTrue(pending.result(timeout=5)["ok"])

    def test_verifier_rejects_missing_evidence_or_replaced_reply(self):
        self.mailbox.submit(request())
        reply = self.mailbox.run_one("fixture", self.dispatcher())
        with self.assertRaisesRegex(m.MailboxError, "TERMINAL_EVIDENCE_REQUIRED"):
            m.Verifier(self.mailbox, KEY, lambda *_: False).seal("fixture")
        self.assertIsNone(self.mailbox.completion("fixture", KEY))
        m.Verifier(self.mailbox, KEY, lambda *_: True).seal("fixture")
        (self.root / "fixture.reply.json").write_bytes(c.canonical_bytes(dict(reply, code="HOLD")))
        with self.assertRaisesRegex(m.MailboxError, "UNVERIFIED_COMPLETION"):
            self.mailbox.completion("fixture", KEY)

    def test_mutant_executor_written_sentinel_is_rejected(self):
        self.mailbox.submit(request())
        dispatcher = self.dispatcher()
        reply = self.mailbox.run_one("fixture", dispatcher)
        sentinel = self.root / "fixture.done"
        sentinel.write_bytes(self.mailbox._sentinel(request(), reply, b"x" * 32))
        # A completion reader accepting the marker alone would publish this reply.
        self.assertTrue(sentinel.read_bytes().startswith(b"<!-- fixture_DONE -->"))
        with self.assertRaisesRegex(m.MailboxError, "UNVERIFIED_COMPLETION"):
            self.mailbox.completion("fixture", KEY)
        with self.assertRaisesRegex(m.MailboxError, "UNVERIFIED_COMPLETION"):
            m.Verifier(self.mailbox, KEY, lambda *_: True).seal("fixture")

    def test_mutant_socket_transport_is_rejected(self):
        dispatcher = self.dispatcher()

        def socket_worker():
            socket.socket(socket.AF_UNIX)
            return self.mailbox.run_one("fixture", dispatcher)

        with self.assertRaisesRegex(AssertionError, "socket transport used"):
            self.exercise_files_only(socket_worker)

    def test_mutant_accepted_as_done_is_rejected_even_with_valid_tag(self):
        envelope = request()
        reply = self.response(envelope, code="accepted")
        self.mailbox.submit(envelope)
        c.validate_contract(c.REPLY, reply)
        (self.root / "fixture.reply.json").write_bytes(c.canonical_bytes(reply))
        with self.assertRaisesRegex(m.MailboxError, "NON_TERMINAL_REPLY"):
            m.Verifier(self.mailbox, KEY, lambda *_: True).seal("fixture")
        self.assertIsNone(self.mailbox.completion("fixture", KEY))
        (self.root / "fixture.done").write_bytes(self.mailbox._sentinel(envelope, reply, KEY))
        with self.assertRaisesRegex(m.MailboxError, "NON_TERMINAL_REPLY"):
            self.mailbox.completion("fixture", KEY)


if __name__ == "__main__":
    unittest.main()
