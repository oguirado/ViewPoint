from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path
import struct
import sys
from tempfile import TemporaryDirectory
from threading import Event
from time import monotonic, sleep
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from jsonschema import ValidationError

DIRECT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(DIRECT))

from perception.pool import PerceptionPool, PoolError
from perception.pool.transport import DescribeResult, Frame, VServeError, VisionWorker
from perception.stream import Observation, PerceptionStream, StreamToken
from vadapt.rtring import ring_bytes
from vadapt.x11_ramcap import SourceConfig

ENGINE = DIRECT.parent / "vision-engine"
BINDING = json.loads((DIRECT / "schemas/tests/examples/vp.surface_binding_v1.valid.json").read_text())
BLACK = Frame(1, 1, b"\0" * 3)
WHITE = Frame(1, 1, b"\xff" * 3)


class Worker:
    def __init__(self):
        self.calls = []
        self.close_count = 0
        self.vserve_error = self.c2_error = None

    def describe(self, transport, frame, previous, masks):
        self.calls.append((transport, frame, previous))
        if self.close_count:
            raise RuntimeError("worker closed")
        error = self.vserve_error if transport == "vserve" else self.c2_error
        if error is not None:
            raise error
        return DescribeResult("ve.cu_describe/v1", sha256(frame.data).hexdigest(),
                              frame.width, frame.height,
                              None if previous is None else frame != previous, (), (), 0)

    def close(self):
        self.close_count += 1


class PoolTests(unittest.TestCase):
    def setUp(self):
        self.workers = []

    def make_pool(self, **overrides):
        def new_worker(*args, **kwargs):
            worker = Worker()
            self.workers.append(worker)
            return worker

        options = dict(max_sessions=4, max_frame_bytes=12, max_reserved_bytes=64,
                       transport="vserve", worker_factory=new_worker)
        pool = PerceptionPool(ENGINE, **(options | overrides))
        self.addCleanup(pool.close)
        return pool

    def open(self, pool, label, *, owner="owner", trust="fixture"):
        return pool.open_session(label, binding=BINDING | {"owner": owner}, trust_domain=trust)

    def assert_histories_isolated(self, pool):
        a, b = self.open(pool, "a"), self.open(pool, "b")
        self.assertIsNone(a.describe(BLACK).description.changed)
        self.assertIsNone(b.describe(WHITE).description.changed, "foreign history")
        self.assertEqual(len(self.workers), 1)
        self.assertFalse(a.describe(BLACK).description.changed)
        self.assertFalse(b.describe(WHITE).description.changed)

    def test_per_session_history_on_shared_worker(self):
        self.assert_histories_isolated(self.make_pool())

    def test_partition_by_owner_and_trust_and_refuse_duplicate_id(self):
        pool = self.make_pool()
        sessions = [self.open(pool, "same"), self.open(pool, "same", owner="other"),
                    self.open(pool, "same", trust="other")]
        with self.assertRaisesRegex(PoolError, "already active"):
            self.open(pool, "same")
        for session in sessions:
            self.assertIsNone(session.describe(WHITE).description.changed)
        self.assertEqual([len(worker.calls) for worker in self.workers], [1, 1, 1])
        self.assertEqual(pool.status().partitions, 3)

    def test_invalid_binding_refused_before_worker_construction(self):
        pool = self.make_pool()
        binding = {key: value for key, value in BINDING.items() if key != "owner"}
        with self.assertRaises(ValidationError):
            pool.open_session("a", binding=binding, trust_domain="fixture")
        self.assertEqual(self.workers, [])
        self.assertEqual(pool.status().reserved_bytes, 0)

    def test_reject_saturation_before_worker_or_frame_work(self):
        pool = self.make_pool(max_sessions=2, max_reserved_bytes=24)
        a, b = self.open(pool, "a"), self.open(pool, "b")
        with self.assertRaisesRegex(PoolError, "session capacity"):
            self.open(pool, "c", owner="unused")
        with self.assertRaisesRegex(PoolError, "frame budget"):
            a.describe(BLACK)
        with self.assertRaisesRegex(PoolError, "session budget"):
            a.describe(Frame(5, 1, b"x" * 15))
        self.assertEqual(len(self.workers), 1)
        self.assertEqual(self.workers[0].calls, [])
        self.assertEqual(pool.status().reserved_bytes, 24)
        b.close()
        a.describe(BLACK)

    def test_refuse_new_worker_when_history_budget_is_full(self):
        pool = self.make_pool(max_reserved_bytes=12)
        self.open(pool, "a")
        with self.assertRaisesRegex(PoolError, "frame budget"):
            self.open(pool, "b", owner="unused")
        self.assertEqual(len(self.workers), 1)

    def test_late_result_rejected_after_recycling_slot(self):
        pool = self.make_pool(max_sessions=2, max_reserved_bytes=48)
        a, other = self.open(pool, "same"), self.open(pool, "peer")
        worker = self.workers[0]
        waiting, release = Event(), Event()
        describe = worker.describe

        def hold_result(transport, frame, previous, masks):
            result = describe(transport, frame, previous, masks)
            if frame is BLACK:
                waiting.set()
                if not release.wait(5):
                    raise AssertionError("test did not release publication")
            return result

        with patch.object(worker, "describe", side_effect=hold_result):
            with ThreadPoolExecutor() as executor:
                late = executor.submit(a.describe, BLACK)
                try:
                    self.assertTrue(waiting.wait(5))
                    retired = executor.submit(a.describe, WHITE)
                    self.assert_queued(pool, 1)
                    a.close()
                    with self.assertRaisesRegex(PoolError, "retired"):
                        retired.result(timeout=5)
                    self.assertEqual(pool.status().reserved_bytes, 27)
                    self.assertEqual(pool.status().retiring_sessions, 1)
                    b = self.open(pool, "same")
                    self.assertIsNot(b, a)
                    self.assertEqual(a._slot, b._slot)
                    queued = executor.submit(b.describe, WHITE)
                    self.assert_queued(pool, 1)
                finally:
                    release.set()
                with self.assertRaisesRegex(PoolError, "retired"):
                    late.result(timeout=5)
                self.assertIsNone(queued.result(timeout=5).description.changed)
        self.assertIsNone(a.diagnostics.last_transport)
        self.assertFalse(b.describe(WHITE).description.changed)
        self.assertIsNone(other.describe(WHITE).description.changed)
        self.assertEqual(pool.status().reserved_bytes, 24)
        self.assertEqual(worker.close_count, 0)

    def assert_queued(self, pool, count):
        deadline = monotonic() + 5
        while pool.status().queued_calls != count:
            self.assertLess(monotonic(), deadline, "queue did not reach expected depth")
            sleep(0.005)

    def test_engine_admission_is_fifo_under_saturation(self):
        pool = self.make_pool()
        client = self.open(pool, "fixture")
        worker = self.workers[0]
        engine = worker.describe
        frames = [Frame(1, 1, bytes([value]) * 3) for value in range(3)]
        started, release = Event(), Event()

        def hold_first(transport, frame, previous, masks):
            if frame is frames[0]:
                started.set()
                if not release.wait(5):
                    raise AssertionError("engine did not release")
            return engine(transport, frame, previous, masks)

        with patch.object(worker, "describe", side_effect=hold_first):
            with ThreadPoolExecutor(max_workers=3) as executor:
                jobs = [executor.submit(client.describe, frames[0])]
                try:
                    self.assertTrue(started.wait(5))
                    for position, frame in enumerate(frames[1:], 1):
                        jobs.append(executor.submit(client.describe, frame))
                        self.assert_queued(pool, position)
                    self.assertEqual(pool.status().inflight, 1)
                finally:
                    release.set()
                for job in jobs:
                    job.result(timeout=5)
        self.assertEqual([call[1] for call in worker.calls], frames)

    def assert_peer_survives_close(self, pool):
        a, b = self.open(pool, "a"), self.open(pool, "b")
        b.describe(WHITE)
        a.close()
        self.assertEqual(self.workers[0].close_count, 0, "shared worker closed")
        self.assertFalse(b.describe(WHITE).description.changed)
        b.close()
        self.assertEqual(self.workers[0].close_count, 1)
        self.assertEqual(pool.status().partitions, 0)
        with self.assertRaisesRegex(PoolError, "retired"):
            a.describe(BLACK)
        reopened = self.open(pool, "a")
        self.assertIsNone(reopened.describe(BLACK).description.changed)
        self.assertEqual(len(self.workers), 2)

    def test_peer_survives_close_and_closed_partition_is_rebuilt(self):
        self.assert_peer_survives_close(self.make_pool())

    def test_uncertain_worker_shutdown_keeps_charge_until_retry(self):
        pool = self.make_pool()
        a = self.open(pool, "a")
        worker = self.workers[0]
        with patch.object(worker, "close", side_effect=OSError("unverified exit")):
            with self.assertRaisesRegex(OSError, "unverified exit"):
                a.close()
        self.assertEqual(pool.status().reserved_bytes, 12)
        with self.assertRaisesRegex(PoolError, "shutdown has not settled"):
            self.open(pool, "b")
        a.close()
        self.assertEqual(pool.status().reserved_bytes, 0)
        self.assertEqual(worker.close_count, 1)

    def test_explicit_fallback_preserves_diagnostics_even_on_c2_failure(self):
        pool = self.make_pool(allow_fallback=True)
        a = self.open(pool, "a")
        worker = self.workers[0]
        worker.vserve_error = VServeError("unavailable", unavailable=True)
        result = a.describe(BLACK)
        self.assertEqual(a.diagnostics, result.diagnostics)
        self.assertEqual(result.diagnostics.last_transport, "c2")
        self.assertEqual(result.diagnostics.last_vserve_error, "unavailable")
        self.assertEqual(result.diagnostics.last_fallback_reason, "unavailable")
        worker.c2_error = RuntimeError("C2 failed")
        with self.assertRaisesRegex(RuntimeError, "C2 failed"):
            a.describe(WHITE)
        self.assertEqual(a.diagnostics, result.diagnostics)
        worker.vserve_error = None
        recovered = a.describe(BLACK)
        self.assertFalse(recovered.description.changed)
        self.assertEqual(recovered.diagnostics.last_transport, "vserve")
        self.assertIsNone(recovered.diagnostics.last_vserve_error)
        self.assertIsNone(recovered.diagnostics.last_fallback_reason)

    def assert_fallback_disabled(self, session, worker, *, unavailable):
        worker.vserve_error = VServeError("vserve refused", unavailable=unavailable)
        with self.assertRaisesRegex(VServeError, "vserve refused"):
            session.describe(BLACK)
        self.assertEqual([call[0] for call in worker.calls], ["vserve"])
        self.assertEqual(session.diagnostics.last_vserve_error, "vserve refused")
        self.assertIsNone(session.diagnostics.last_fallback_reason)

    def test_vserve_refusal_and_disabled_fallback_never_call_c2(self):
        for enabled, unavailable in ((False, True), (True, False)):
            with self.subTest(enabled=enabled, unavailable=unavailable):
                pool = self.make_pool(allow_fallback=enabled)
                session = self.open(pool, "a")
                self.assert_fallback_disabled(session, self.workers[-1], unavailable=unavailable)

    def test_p3_binding_and_generation_retire_observations_and_history(self):
        pool = self.make_pool()
        session = self.open(pool, "a")
        token = StreamToken(BINDING["endpoint"], BINDING["surface_id"], BINDING["incarnation"],
                            BINDING["owner"], BINDING["authority_epoch"], 1, "nonce")
        observation = Observation(token, "fresh", 1, sha256(BLACK.data).hexdigest())
        stream = SimpleNamespace(read=lambda *_: BLACK.data,
                                 status=lambda: dict(state="ACTIVE", stream_generation=1,
                                                     fresh_observation_id="fresh"))

        def describe(obs):
            return session.describe_observation(stream, obs, width=1, height=1).description

        with self.assertRaisesRegex(PoolError, "binding"):
            describe(replace(observation, token=replace(token, owner="foreign")))
        self.assertEqual(self.workers[0].calls, [])
        self.assertIsNone(describe(observation).changed)
        self.assertFalse(describe(observation).changed)
        stream.status = lambda: dict(state="ACTIVE", stream_generation=2, fresh_observation_id="new")
        with self.assertRaisesRegex(PoolError, "retired before publication"):
            describe(observation)
        fresh = replace(observation, token=replace(token, generation=2, nonce="new"),
                        observation_id="new")
        self.assertIsNone(describe(fresh).changed)

    def test_mutant_shared_history(self):
        pool = self.make_pool()
        describe = pool._describe
        latest = []

        def shared(client, load_frame, width, height, masks, **kwargs):
            if latest:
                client._previous = latest[-1]._previous
            result = describe(client, load_frame, width, height, masks, **kwargs)
            latest.append(client)
            return result

        with patch.object(pool, "_describe", side_effect=shared):
            with self.assertRaisesRegex(AssertionError, "foreign history"):
                self.assert_histories_isolated(pool)
        self.assertIs(self.workers[0].calls[-1][2], BLACK)

    def test_mutant_silent_c2_fallback(self):
        pool = self.make_pool()
        session = self.open(pool, "a")
        worker = self.workers[0]
        describe = worker.describe

        def silent(transport, frame, previous, masks):
            try:
                return describe(transport, frame, previous, masks)
            except VServeError:
                return describe("c2", frame, previous, masks)

        with patch.object(worker, "describe", side_effect=silent):
            with self.assertRaisesRegex(AssertionError, "VServeError not raised"):
                self.assert_fallback_disabled(session, worker, unavailable=True)
        self.assertEqual([call[0] for call in worker.calls], ["vserve", "c2"])

    def test_mutant_kill_shared_worker_on_client_close(self):
        pool = self.make_pool()
        retire = pool._retire

        def kill_shared(client):
            if client._partition.users > 1:
                client._partition.worker.close()
            retire(client)

        with patch.object(pool, "_retire", side_effect=kill_shared):
            with self.assertRaisesRegex(AssertionError, "shared worker closed"):
                self.assert_peer_survives_close(pool)
        self.assertEqual(self.workers[0].close_count, 1)

    def test_real_vserve_worker_persists_and_keeps_per_session_history(self):
        black = Frame(32, 32, b"\0" * (32 * 32 * 3))
        white = Frame(32, 32, b"\xff" * (32 * 32 * 3))
        pool = self.make_pool(worker_factory=VisionWorker, timeout=10, max_frame_bytes=4096,
                              max_reserved_bytes=16384)
        a, b = self.open(pool, "a"), self.open(pool, "b")
        first = a.describe(black)
        child = a._partition.worker._vserve._proc
        self.assertEqual(first.diagnostics.last_transport, "vserve")
        self.assertIsNone(first.description.changed)
        self.assertIsNone(b.describe(white).description.changed)
        a.close()
        self.assertFalse(b.describe(white).description.changed)
        self.assertIs(b._partition.worker._vserve._proc, child)
        self.assertIsNone(child.poll())
        b.close()
        self.assertIsNotNone(child.poll())

    def test_real_c2_requires_explicit_transport(self):
        pool = self.make_pool(worker_factory=VisionWorker, transport="c2", timeout=10,
                              max_frame_bytes=4096, max_reserved_bytes=16384)
        session = self.open(pool, "a")
        frame = Frame(32, 32, b"\0" * (32 * 32 * 3))
        result = session.describe(frame)
        self.assertIsNone(result.description.changed)
        self.assertEqual(result.diagnostics.last_transport, "c2")
        self.assertIsNone(result.diagnostics.last_vserve_error)
        self.assertIsNone(result.diagnostics.last_fallback_reason)

    def test_real_p3_observation_flows_through_vserve(self):
        pool = self.make_pool(worker_factory=VisionWorker, timeout=10, max_frame_bytes=4096,
                              max_reserved_bytes=16384)
        session = self.open(pool, "a")
        with TemporaryDirectory(dir="/dev/shm") as workspace:
            root = Path(workspace)
            header = (100, 7, 2, 24, 32, 32, 0, 0, 32, 0, 32, 32, 128,
                      4, 0xff0000, 0xff00, 0xff, 8, 0, 0, 32, 32, 0, 0, 0)
            rgb = b"\x12\x34\x56" * 1024
            (root / "source").write_bytes(struct.pack(">25I", *header) + b"\x56\x34\x12\0" * 1024)
            stream = PerceptionStream(
                BINDING, SourceConfig(workspace, "source", 32, 32, "synthetic XWD"),
                ring_slots=2, interval_ns=1000, ttl_ns=10**10, max_frame_bytes=8192,
                max_ring_bytes=ring_bytes(2, 32, 32), workspace_root=root / "rings",
            )
            try:
                observation = stream.wake()
                first = session.describe_observation(stream, observation, width=32, height=32)
                self.assertEqual(first.description.frame_sha256, sha256(rgb).hexdigest())
                self.assertIsNone(first.description.changed)
                stream.pause(observation.token)
                new = stream.wake()
                after = session.describe_observation(stream, new, width=32, height=32)
                self.assertIsNone(after.description.changed)
                self.assertEqual(first.description.frame_sha256, after.description.frame_sha256)
            finally:
                stream.close()
