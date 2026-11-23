from copy import deepcopy
from dataclasses import asdict, replace
import inspect
from io import BytesIO
import json
from pathlib import Path
import struct
import subprocess
import sys
from threading import Event
from unittest import TestCase
from unittest.mock import Mock, patch

from LIVE_microvm_restore import FIXTURE, load_fixture
from rewind import microvm


REPO = FIXTURE.parents[5]
RUNNER = FIXTURE.with_name("LIVE_microvm_restore.py")
sys.path.insert(0, str(REPO / "daemon-adapter"))

from rewind_runtime import native as runtime
from rewind_runtime.controller import Controller


class MicroVMReplayTests(TestCase):
    def setUp(self):
        self.previous, self.following, self.phases = load_fixture()

    def replay(self, phases=None, following=None):
        return microvm.replay_restore(
            self.previous,
            self.following if following is None else following,
            self.phases if phases is None else phases,
        )

    def with_evidence(self, index, evidence):
        phases = list(self.phases)
        phases[index] = replace(phases[index], evidence=evidence)
        return phases

    def assert_fenced(self, result):
        self.assertIsNone(result.binding)
        self.assertFalse(result.command_allowed(self.previous))
        self.assertFalse(result.command_allowed(self.following))
        self.assertIsNone(result.duration_ns)
        self.assertIsNotNone(result.refusal)

    def test_complete_restore_renews_authority_and_preserves_external_effects(self):
        result = self.replay()
        self.assertEqual(result.binding, self.following)
        self.assertIsNone(result.refusal)
        self.assertEqual(result.duration_ns, 7_000_000)
        self.assertEqual(result.external_effect_frontier, "UNREVERTED_HOST_NET_JOURNAL")
        self.assertTrue(result.command_allowed(self.following))
        self.assertFalse(result.command_allowed(self.previous))
        for field in ("generation", "authority_epoch", "session_id", "reservation_id"):
            with self.subTest(field=field):
                mixed = replace(self.following, **{field: getattr(self.previous, field)})
                self.assertFalse(result.command_allowed(mixed))

    def test_controller_returns_correlated_ready_after_native_load_and_reset(self):
        request_phase, load_phase, reset_phase, ready_phase = self.phases
        backend = Mock(spec=runtime.Native)
        backend.points = ready_phase.evidence["points"]
        backend.root = Path("synthetic-fixture")
        backend.stream = None
        backend.process = None
        backend.abort = Event()
        backend.paused = False
        backend.nonces = set()
        backend.config = {"startup_seconds": 1}
        backend.boot_token = "0" * 64
        backend.reset.return_value = 0
        controller = Controller({"owner_id": self.previous.owner_id}, native=backend)
        controller.create(asdict(self.previous))
        observed = []

        def record(op, stage, data=None):
            if (op, stage) == ("restore", "owner_intent"):
                self.assertEqual(data["next_binding"], asdict(self.following))
                self.assert_fenced(self.replay([]))
                observed.append(replace(request_phase, request_id=data["request_id"]))

        def load(selected):
            runtime.Native.restore(backend, selected)
            observed.append(replace(load_phase, evidence=selected))
            self.assert_fenced(self.replay(observed))

        def reset(binding):
            counter = runtime.Native.reset(backend, binding)
            observed.append(reset_phase)
            self.assert_fenced(self.replay(observed))
            return counter

        backend.record.side_effect = record
        backend.restore.side_effect = load
        backend.reset.side_effect = reset
        reply = json.dumps(reset_phase.evidence).encode("utf-8")
        response = BytesIO(b"RWR1" + struct.pack(">I", len(reply)) + reply)
        socket = Mock()
        socket.recv.side_effect = response.read
        wire = Mock()
        ready = {key: getattr(self.following, key)
                 for key in ("workload_id", "generation", "reservation_id")}
        wire.receive.return_value = (5, dict(ready, kind="ready"), b"")
        with (patch.object(runtime.checks, "connect_guest", return_value=socket),
              patch.object(runtime, "FramedSocket", return_value=wire),
              patch.object(runtime.secrets, "token_hex", side_effect=["1" * 64, "3" * 128])):
            status = controller.request(request_phase.evidence, next_binding=asdict(self.following))
        observed.append(replace(ready_phase, evidence=status))
        self.assertEqual(backend._api.call_args_list[0].args[:2], ("PUT", "/snapshot/load"))
        self.assertEqual(backend._api.call_args_list[1].args[:2], ("PATCH", "/vm"))
        self.assertEqual(wire.send.call_args.args[1]["generation"], self.following.generation)
        self.assertEqual(status, ready_phase.evidence)
        self.assertEqual(self.replay(observed), self.replay())

    def test_reopen_replays_identical_fixture_and_output(self):
        before = FIXTURE.read_bytes()
        first = self.replay()
        self.assertEqual(first, microvm.replay_restore(*load_fixture()))
        cmd = [sys.executable, "-I", "-B", str(RUNNER), "--fixture", str(FIXTURE)]
        results = [subprocess.run(cmd, check=True, capture_output=True) for _ in range(2)]
        self.assertEqual(results[0].stdout, results[1].stdout)
        output = json.loads(results[0].stdout)
        self.assertEqual(output["mode"], "local_replay")
        self.assertEqual(output["duration_ns"], first.duration_ns)
        self.assertTrue(output["commands_allowed"])
        self.assertFalse(output["pre_restore_reference_allowed"])
        self.assertEqual(before, FIXTURE.read_bytes())

    def assert_live_backend_absent(self, result):
        self.assertEqual(result.returncode, 1, "default invocation must refuse")
        self.assertEqual(result.stdout, b"")
        self.assertEqual(result.stderr, b"E_LIVE_BACKEND_ABSENT NOT-DONE\n")

    def test_default_invocation_refuses_without_a_live_backend(self):
        result = subprocess.run([sys.executable, "-I", "-B", str(RUNNER)], capture_output=True)
        self.assert_live_backend_absent(result)

    def test_fixture_replay_requires_the_explicit_option(self):
        command = [sys.executable, "-I", "-B", str(RUNNER)]
        for args in ([str(FIXTURE)], ["--fix", str(FIXTURE)]):
            with self.subTest(args=args):
                result = subprocess.run([*command, *args], capture_output=True)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, b"")
                self.assertIn(b"unrecognized arguments", result.stderr)

    def test_mutant_defaulting_to_the_fixture_fails_live_refusal_oracle(self):
        source = RUNNER.read_text(encoding="utf-8")
        original = 'parser.add_argument("--fixture", type=Path,'
        self.assertEqual(source.count(original), 1)
        mutant = source.replace(original, original + " default=FIXTURE,")
        result = subprocess.run(
            [sys.executable, "-I", "-B", "-c", f"__file__ = {str(RUNNER)!r}\n{mutant}"],
            capture_output=True,
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stdout)["mode"], "local_replay")
        with self.assertRaisesRegex(AssertionError, "default invocation must refuse"):
            self.assert_live_backend_absent(result)

    def test_every_missing_phase_leaves_duration_unavailable_and_commands_fenced(self):
        for i, phase in enumerate(self.phases):
            with self.subTest(phase=phase.name):
                self.assert_fenced(self.replay(self.phases[:i] + self.phases[i + 1:]))

    def test_reordered_or_repeated_reseed_is_refused(self):
        request, native_load, reseed, app_ready = self.phases
        self.assert_fenced(self.replay((request, reseed, native_load, app_ready)))
        self.assert_fenced(self.replay((request, native_load, reseed, reseed, app_ready)))

    def test_substituted_generation_cannot_renew_authority(self):
        reset = dict(self.phases[2].evidence, command_generation=self.previous.generation)
        self.assert_fenced(self.replay(self.with_evidence(2, reset)))

    def test_missing_timestamp_leaves_duration_unavailable(self):
        for i, phase in enumerate(self.phases):
            with self.subTest(phase=phase.name):
                phases = list(self.phases)
                phases[i] = replace(phase, mono_ns=None)
                result = self.replay(phases)
                self.assertIsNone(result.duration_ns)
                self.assertIsNone(result.refusal)
                self.assertTrue(result.command_allowed(self.following))

    def test_unsupported_timestamps_never_report_duration(self):
        for value in (False, -1, 999_999_999):
            with self.subTest(value=value):
                phases = list(self.phases)
                phases[1] = replace(phases[1], mono_ns=value)
                self.assertIsNone(self.replay(phases).duration_ns)

    def test_reseed_requires_explicit_true_command_fence(self):
        reset = dict(self.phases[2].evidence)
        del reset["commands_fenced"]
        for value in (reset, dict(reset, commands_fenced=False), dict(reset, commands_fenced=1)):
            self.assert_fenced(self.replay(self.with_evidence(2, value)))

    def test_controller_ready_must_match_session_epoch_counter_and_point(self):
        ready = self.phases[3].evidence
        cases = [
            dict(ready, phase="restoring"),
            dict(ready, ownership="released"),
            dict(ready, counter=ready["counter"] + 1),
            dict(ready, points={}),
        ]
        for field in ("session_id", "reservation_id", "authority_epoch", "owner_id", "workload_id"):
            cases.append(dict(ready, binding=dict(ready["binding"], **{field: "substituted"})))
        for i, status in enumerate(cases):
            with self.subTest(case=i):
                self.assert_fenced(self.replay(self.with_evidence(3, status)))

    def test_replay_refuses_stale_authority_wrong_point_or_crossed_request(self):
        for field in ("generation", "authority_epoch", "session_id", "reservation_id"):
            with self.subTest(field=field):
                following = replace(self.following, **{field: getattr(self.previous, field)})
                self.assert_fenced(self.replay(following=following))
        self.assert_fenced(self.replay(self.with_evidence(1, "point-other")))
        phases = list(self.phases)
        phases[3] = replace(phases[3], request_id="request-other")
        self.assert_fenced(self.replay(phases))

    def assert_mutant_killed(self, name, original, mutation, oracle):
        function = getattr(microvm, name)
        source = inspect.getsource(function)
        self.assertEqual(source.count(original), 1)
        namespace = vars(microvm).copy()
        exec(compile(source.replace(original, mutation), inspect.getfile(function), "exec"), namespace)
        oracle()
        with patch.object(microvm, name, namespace[name]):
            with self.assertRaises(AssertionError):
                oracle()

    def test_mutant_skipping_reseed_accepts_refused_fence(self):
        self.assert_mutant_killed(
            "replay_restore", "if not _reseed_matches(reseed, following):",
            "if False:", self.test_reseed_requires_explicit_true_command_fence,
        )

    def test_mutant_ignoring_controller_ready_accepts_substituted_status(self):
        self.assert_mutant_killed(
            "replay_restore",
            'if not _ready_matches(ready_phase.evidence, following, reseed["counter"], params["point_id"]):',
            "if False:", self.test_substituted_generation_cannot_renew_authority_at_ready,
        )

    def test_substituted_generation_cannot_renew_authority_at_ready(self):
        ready = deepcopy(self.phases[3].evidence)
        ready["binding"]["generation"] = self.previous.generation
        self.assert_fenced(self.replay(self.with_evidence(3, ready)))

    def test_mutant_duration_from_endpoints_invents_missing_time(self):
        def oracle():
            phases = list(self.phases)
            phases[1] = replace(phases[1], mono_ns=None)
            self.assertIsNone(self.replay(phases).duration_ns)

        self.assert_mutant_killed(
            "_duration",
            "stamp in stamps):\n        return None",
            "stamp in stamps):\n        return stamps[-1] - stamps[0]",
            oracle,
        )

    def test_mutant_generation_only_gate_reuses_old_epoch(self):
        def oracle():
            mixed = replace(self.following, authority_epoch=self.previous.authority_epoch)
            self.assertFalse(self.replay().command_allowed(mixed))

        method = microvm.ReplayResult.command_allowed
        with patch.object(microvm.ReplayResult, "command_allowed",
                          lambda self, ref: self.binding.generation == ref.generation):
            with self.assertRaises(AssertionError):
                oracle()
        self.assertIs(microvm.ReplayResult.command_allowed, method)
        oracle()
