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
from time import monotonic_ns
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

TESTS = Path(__file__).resolve().parent
DIRECT = TESTS.parents[2]
sys.path.insert(0, str(DIRECT))
from bus.broker import Broker, BrokerError, call
from bus.broker import broker as implementation, journal, port
from jsonschema.exceptions import ValidationError
from schemas.validation import canonical_bytes, validate_contract
from vendor.rewind import schema as rw

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


@implementation.serialized
def append_checkpoint(broker, record):
    # Test-only F1 append port. Both writers use the U3a journal's lock and sequence.
    fields = dict(record)
    return broker._write(fields.pop("rec"), **fields)


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

    def test_journal_append_refuses_wrong_pid_or_closed_file(self):
        writer = journal.Journal(self.rt / "writer")
        self.addCleanup(writer.close)
        with patch.object(writer, "pid", writer.pid + 1):
            self.denied("NO_LEASE", writer.append, {})
        writer.file.close()
        self.denied("NO_LEASE", writer.append, {})
        self.assertEqual(writer.seq, 0)
        self.assertEqual((self.rt / "writer" / "events.jsonl").read_bytes(), b"")

    def test_port_receive_reports_clean_eof(self):
        receiver, peer = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        with receiver, peer:
            peer.shutdown(socket.SHUT_WR)
            with self.assertRaises(EOFError):
                port._receive(receiver)

    def test_port_send_refuses_oversized_packet_before_writing(self):
        sock = Mock()
        # JSON serialization adds the surrounding quotes.
        with self.assertRaisesRegex(ValueError, "oversized packet"):
            port._send(sock, "x" * (port.MAX_PACKET - 1))
        sock.sendall.assert_not_called()

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

    def write_prior_s4_log(self, *, with_preparation, checkpoint_records=()):
        # These v1 records are from the original S4 writer format. Construct
        # them independently of the current Broker transition methods.
        self.b.close()
        take = dict(schema="vp.human_lease/v1", session_id="session", state="HUMAN",
                    by="broker", requested_by="operator", reason="login",
                    epoch_before=1, epoch_after=2, outstanding=[])
        returned = dict(take, state="AGENT", epoch_before=2, epoch_after=3)
        checkpoint = dict(focus=dict(xid=1, pid=101, witness="saved", workspace=0),
                          identity=WITNESS, old_observation="before-take")
        records = [dict(rec="control", op="take", lease=take, handoff=checkpoint)]
        records.extend(checkpoint_records[:1])
        if with_preparation:
            records.append(dict(rec="control", op="prepare_human_return", session_id="session",
                                epoch_before=2, epoch_after=3))
        records.extend(checkpoint_records[1:])
        records.append(dict(rec="control", op="return", lease=returned))
        path = self.rt / "events.jsonl"
        with path.open("ab") as stream:
            for seq, fields in enumerate(records, start=2):
                stream.write(canonical_bytes(dict(fields, v=1, seq=seq, by="broker", ts_ms=seq)))
        return path

    def test_replay_prior_s4_log_keeps_history_and_fences_both_broker_names(self):
        from human.authority import HandoffBroker

        path = self.write_prior_s4_log(with_preparation=True)
        before = path.read_bytes()
        self.assertIs(HandoffBroker, Broker)
        for broker_class in (Broker, HandoffBroker):
            with broker_class(self.rt) as recovered:
                taken, returned = recovered.history("session")
                self.assertEqual(taken["handoff"]["old_observation"], "before-take")
                self.assertEqual(returned["lease"]["epoch_after"], 3)
                self.assertEqual(recovered.lease("session"), dict(state="AGENT", holder=None, authority_epoch=3))
                self.assertEqual(recovered.binding(ALIAS[1])["authority_epoch"], 3)
                self.denied("NO_LEASE", recovered.try_acquire, "owner", ALIAS[0], "inc", 3, "stale")
        self.assertEqual(path.read_bytes(), before)

    def test_replay_refuses_s4_return_without_preparation(self):
        path = self.write_prior_s4_log(with_preparation=False)
        before = path.read_bytes()
        with self.assertRaisesRegex(BrokerError, "^NO_LEASE$"):
            with Broker(self.rt):
                self.fail("incomplete handoff log admitted a writer")
        self.assertEqual(path.read_bytes(), before)

    def test_mutant_accepting_unprepared_s4_return_exposes_agent_input(self):
        self.write_prior_s4_log(with_preparation=False)
        bad = variant(implementation, "if prepared_return and self._preparations.get(session) != lease:",
                      "if False:", "Broker")
        with bad(self.rt) as recovered:
            with self.assertRaisesRegex(AssertionError, "BrokerError not raised"):
                self.denied("NO_LEASE", recovered.try_acquire, "owner", ALIAS[0], "inc", 3, "too-early")
            self.assertEqual(recovered.lock_status(ALIAS[0])["request_id"], "too-early")

    def checkpoint_pin(self, tick):
        digest = hashlib.sha256(f"fixture checkpoint {tick}".encode()).hexdigest()
        pin = hashlib.sha256(f"fixture acceptance {tick}".encode()).hexdigest()
        return dict(rw.ckpt_rec(f"p{tick}", tick, rw.SCENE_EPOCH_0, digest, 1,
                                {"col:PROPS": digest}, 0), acceptance_sha256=pin)

    def assert_interleaved_replay(self, broker_class):
        checkpoints = (self.checkpoint_pin(0), self.checkpoint_pin(1))
        path = self.write_prior_s4_log(with_preparation=True, checkpoint_records=checkpoints)
        before = path.read_bytes()
        with broker_class(self.rt) as recovered:
            take, returned = recovered.history("session")
            self.assertEqual((take["seq"], returned["seq"]), (2, 6))
            self.assertEqual(take["lease"]["state"], "HUMAN")
            self.assertEqual(recovered.lease("session"), dict(state="AGENT", holder=None, authority_epoch=3))
            self.denied("NO_LEASE", recovered.try_acquire, "owner", ALIAS[0], "inc", 3, "too-early")
        self.assertEqual(path.read_bytes(), before)
        rows = self.rows()
        self.assertEqual(rows[3]["op"], "prepare_human_return")
        rewind = rw.replay(rows)
        self.assertEqual(rewind.violations, [])
        self.assertEqual(tuple(rewind.points), ("p0", "p1"))
        self.assertEqual([rewind.points[c["point_id"]]["acceptance_sha256"] for c in checkpoints],
                         [c["acceptance_sha256"] for c in checkpoints])

    def test_replay_interleaved_s4_and_blender_checkpoint_pins(self):
        self.assert_interleaved_replay(Broker)

    def test_interleaved_replay_rejects_invalid_checkpoint_pins_and_unknown_controls(self):
        valid = self.checkpoint_pin(0)
        unpinned = dict(valid)
        del unpinned["acceptance_sha256"]
        path = self.write_prior_s4_log(with_preparation=True, checkpoint_records=(valid,))
        rows = self.rows()
        for fields, code in (
            (unpinned, "invalid checkpoint pin"),
            (dict(valid, acceptance_sha256="invalid"), "invalid checkpoint pin"),
            (dict(valid, point_id="invalid"), "invalid checkpoint pin"),
            (dict(valid, rec="control", op="unexpected"), "unknown broker control"),
        ):
            with self.subTest(fields=fields):
                rows[2] = dict(fields, v=1, seq=3, by="broker", ts_ms=3)
                raw = b"".join(canonical_bytes(row) for row in rows)
                path.write_bytes(raw)
                self.denied(code, Broker, self.rt)
                self.assertEqual(path.read_bytes(), raw)

    def test_mutant_rejecting_checkpoint_records_breaks_interleaved_replay(self):
        bad = variant(implementation, 'elif kind == "ckpt":', 'elif False:', "Broker")
        with self.assertRaisesRegex(BrokerError, "^unknown broker record$"):
            self.assert_interleaved_replay(bad)

    def write_s4_u4b_history(self):
        from bus.journal import Journal as LineageJournal
        from data.locator import ArtifactLocator
        from human import FocusWitness, FreshObservation, HandoffRuntime, HumanHandoff
        from rewind.blender_rw1 import BlenderRewind, RuntimeBinding
        with patch.object(sys, "path", [str(DIRECT / "vendor" / "broker"), *sys.path]):
            from vendor.rewind import fakes

        capsule = fakes.Capsule(rt=str(self.rt / "blender-transport"))
        self.addCleanup(capsule.broker.close)
        self.addCleanup(capsule.stop_bridge)
        transport = capsule.broker

        artifact_root = self.rt / "artifacts"
        artifact_root.mkdir()
        raw = b"admitted fixture mesh"
        (artifact_root / "mesh").write_bytes(raw)
        locator = ArtifactLocator({"fixture": artifact_root})
        artifact = locator.put(dict(example("vp.artifact_ref"), bytes=len(raw),
                                    sha256=hashlib.sha256(raw).hexdigest()))
        session = dict(example("vp.session"), session_id="session", incarnation="inc",
                       display="xpra:90", lease={"holder": "owner", "authority_epoch": 1})
        registry = Mock(spec=["get_session"])
        registry.get_session.return_value = session
        lineage = LineageJournal(self.rt)

        def open_rewind(broker):
            checkpoint_port = SimpleNamespace(
                rt=self.rt, p_events=self.rt / "events.jsonl", _lock=broker._journal.file,
                d_outbox=transport.d_outbox, _dispatch_script=transport._dispatch_script,
                _append=lambda record: append_checkpoint(broker, record),
            )
            return BlenderRewind(
                checkpoint_port, fakes.FakeOps(), capsule_id=session["capsule_id"],
                goal_id="shared-fixture", admitted_inputs=(artifact,),
                binding=lambda: RuntimeBinding(broker.binding("surface")["authority_epoch"], WITNESS, WITNESS),
                compatibility_sha256=WITNESS, resolve_artifact=locator.resolve,
            )

        rewind = open_rewind(self.b)
        budget = dict(attempts_used=0, attempts_limit=10, rewinds_used=0, rewinds_limit=3)

        def mark(tick):
            def score_checkpoint(digest):
                predicate = SimpleNamespace(render="render/frame_000000.png", frame_not_blank=True)
                return SimpleNamespace(scene_digest=digest, score=50 + tick, accepted=True, predicates=(predicate,))

            return rewind.mark(by="broker", scene_epoch=rw.SCENE_EPOCH_0, tick=tick, frame=tick,
                               authority_epoch=self.b.binding("surface")["authority_epoch"],
                               artifact_refs=(artifact,), budget=budget, score_checkpoint=score_checkpoint)

        windows = Mock(spec=["save_focus", "summon", "dismiss", "restore_focus", "verify_focus"])
        windows.save_focus.return_value = FocusWitness(xid=1, pid=101, witness="saved", workspace=0)
        runtime = Mock(spec=HandoffRuntime)
        runtime.last_observation_id.return_value = "before-take"
        runtime.attachments.return_value = ()
        runtime.reconnect.side_effect = lambda *_: setattr(runtime.attachments, "return_value", ("cdp",))
        runtime.reauthorize.return_value = True
        runtime.retain.return_value = dict(example("vp.perception_state"), session_id="session",
                                          surface_id="surface", incarnation="inc", state="ASLEEP", authority_epoch=2)
        handoff = HumanHandoff(broker=self.b, registry=registry, session_id="session", surface_id="surface",
                               windows=windows, runtime=runtime, lineage=lineage)

        def request(op, epoch, **args):
            return dict(schema="vp.control.req/v1", request_id=op, op=op, dry_run=False,
                        context=dict(caller="operator", agent_instance="operator", session_id="session",
                                     incarnation="inc", authority_epoch=epoch), args=dict(session_id="session", **args))

        taken = handoff.vp_human_take(request("vp_human_take", 1, reason="login"))
        self.assertEqual((taken.ok, taken.code), (True, "OK"))
        base = mark(0).checkpoint
        self.denied("NO_LEASE", self.b.human_lease, "session", "AGENT", 2, "login")
        self.denied("NO_LEASE", self.b.try_acquire, "owner", ALIAS[0], "inc", 2, "during-take")
        capsule.scene["col:PROPS"].append("repair")
        stage = None

        def readback(_session, _binding):
            nonlocal stage
            stage = mark(1).checkpoint
            self.denied("NO_LEASE", self.b.try_acquire, "owner", ALIAS[1], "inc", 3, "during-return")
            return FreshObservation("session", "surface", "inc", 3, "after-return", monotonic_ns(), 1)

        runtime.observe.side_effect = readback
        returned = handoff.vp_human_return(request("vp_human_return", 2))
        self.assertEqual((returned.ok, returned.code), (True, "OK"))
        self.assertEqual(self.b.returned_observation("session").observation_id, "after-return")
        self.assertEqual(self.b.lease("session"), dict(state="AGENT", holder="owner", authority_epoch=3))
        self.assertEqual([row["payload"] for row in lineage.verify()],
                         [taken.result["lease"], returned.result["lease"]])
        runtime.observe.assert_called_once()
        self.assertNotEqual(base.record["blend_sha256"], stage.record["blend_sha256"])
        self.b.close()
        return open_rewind, stage

    def assert_s4_u4b_replay(self, broker_class, open_rewind, stage):
        path = self.rt / "events.jsonl"
        before = path.read_bytes()
        rows = self.rows()
        self.assertEqual([r["seq"] for r in rows], list(range(1, len(rows) + 1)))
        self.assertEqual([r.get("op", r.get("point_id")) for r in rows],
                         ["bind", "take", "perception", "p0", "prepare_human_return", "p1", "return"])
        with broker_class(self.rt) as recovered:
            self.assertEqual(recovered.history("session"), (rows[1], rows[-1]))
            self.assertEqual(recovered.lease("session"), dict(state="AGENT", holder=None, authority_epoch=3))
            self.denied("NO_LEASE", recovered.try_acquire, "owner", ALIAS[2], "inc", 3, "after-reopen")
            rewind = open_rewind(recovered)
            self.assertEqual(rewind.points(), ("p0", "p1"))
            self.assertEqual(rewind.current(), stage)
        self.assertEqual(path.read_bytes(), before)

    def test_actual_s4_u4b_writers_share_a_replayable_journal(self):
        self.assert_s4_u4b_replay(Broker, *self.write_s4_u4b_history())

    def test_mutant_rejecting_real_u4b_checkpoint_breaks_shared_replay(self):
        history = self.write_s4_u4b_history()
        bad = variant(implementation, 'elif kind == "ckpt":', 'elif False:', "Broker")
        path = self.rt / "events.jsonl"
        before = path.read_bytes()
        with self.assertRaisesRegex(BrokerError, "^unknown broker record$"):
            self.assert_s4_u4b_replay(bad, *history)
        self.assertEqual(path.read_bytes(), before)

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
