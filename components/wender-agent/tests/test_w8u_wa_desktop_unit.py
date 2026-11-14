"""Offline unit tests for `wender.perceive.w8u_desktop` (W8U Desktop Adapter, Credit Flow,
XWD Held-Pair Verification, AT-SPI vs. Vision-Only Modality, Action Gate `w8u.action/v1`,
Receipt `w8u.receipt/v1`, and Zero-Disk-Write Storage Law).

All timings are VM-only; numeric widths, limits, waves, and fixture choices are TARGET.
Positive and must-FAIL controls run in the same unittest invocation with exact error
and event-count assertions; zero skips and zero expectedFailures.
"""
from __future__ import annotations

import base64
import copy
from dataclasses import FrozenInstanceError
import hashlib
import pathlib
import struct
import unittest
import zlib

import numpy as np

from wender.act.focus_policy import FocusPolicy, FocusRefused
from wender.perceive import atspi, atspi_tree, ax
from wender.perceive.session_registry import (
    ActorLease,
    FrameRef,
    FrameValidationError,
    Handle,
    Observation,
    RegistryError,
    SessionRegistry,
    StaleHandleError,
)
from wender.perceive.w8u_desktop import (
    ACTION_MAX_UTF8_BYTES,
    ACTION_REQUEST_KEYS,
    ACTION_RESPONSE_KEYS,
    AUSENTE,
    BINDING_SCHEMA_KEYS,
    CONTROL_BODY_SCHEMAS,
    CONTROL_KINDS,
    CONTROL_MAGIC,
    DIGEST_DOMAINS,
    END_NUMERIC_ERRORS,
    END_OK,
    E_BUDGET,
    E_COORDINATES,
    E_CREDIT,
    E_DEADLINE,
    E_DPR,
    E_EPOCH,
    E_FOCUS,
    E_FOREIGN_TARGET,
    E_LOCK,
    E_MODALITY,
    E_PAIR_MISMATCH,
    E_PROTOCOL,
    E_REOPEN,
    E_SCHEMA,
    E_STALE_FRAME,
    E_STALE_GENERATION,
    E_STORAGE_LAW,
    E_TICK_INVENTED,
    E_UNCERTAIN,
    E_VERB,
    FINITE_KEYS,
    FRAME_METADATA_KEYS,
    FRAME_PAIR_MAGIC,
    FRAMES_ROOT,
    OK_ADMITTED,
    ORDERED_MODIFIERS,
    POINTER_BUTTONS,
    RC_OK,
    RC_REFUSED,
    RC_SCHEMA,
    RC_UNAVAILABLE,
    RC_UNCERTAIN,
    RECEIPT_REQUIRED_KEYS,
    RECEIPT_SOURCE,
    RESULT_SCHEMA_KEYS,
    W8U_ACTION_RESPONSE_SCHEMA,
    W8U_ACTION_SCHEMA,
    W8U_BINDING_SCHEMA,
    W8U_OS_CONTRACT_REVISION,
    W8U_RECEIPT_SCHEMA,
    W8U_RESULT_SCHEMA,
    ActionAuthorityContext,
    AuthenticatedAtspiCapability,
    CreditController,
    CreditError,
    DiskSinkSpy,
    ForeignTargetError,
    InMemoryExecPipe,
    InjectedPairEvidence,
    ModalityError,
    PairMismatchError,
    PrivateXIdentity,
    ProtocolError,
    RegistryFocusAdapter,
    StorageLawError,
    TickInventedError,
    TickWitness,
    W8UActionGate,
    W8UDesktopAdapter,
    W8UDesktopError,
    build_frame_binding,
    build_observation_result,
    build_vision_only_state,
    build_xwd_bytes,
    canonical_digest,
    compute_action_grant_sha256,
    parse_strict_json_object,
    parse_xwd_common_subset,
    state_digest,
    validate_frame_metadata,
    validate_receipt,
    verify_same_grab_pair,
)
from wender.state import schema


class DeterministicVmClock:
    """Deterministic monotonic nanosecond clock for VM unit tests."""

    def __init__(self, start_ns: int = 1_000_000_000) -> None:
        self.now_ns = start_ns

    def __call__(self) -> int:
        self.now_ns += 1_000
        return self.now_ns


class FakeDesktopTransport:
    def __init__(self) -> None:
        self.closed_targets: list[str] = []

    def close_target(self, canonical_id: str) -> None:
        self.closed_targets.append(canonical_id)


class FakeTargetLocks:
    def __init__(self) -> None:
        self.valid_nonces: set[bytes] = set()

    def owns(self, lease: ActorLease, *, now_ns: int) -> bool:
        return lease.nonce in self.valid_nonces and now_ns < lease.expires_ns


def make_fake_atspi_call(tree_spec: dict[str, tuple[str, str, int, list[str] | None]]):
    """Build an in-memory AT-SPI D-Bus call responder without any external process."""
    calls: list[tuple] = []

    def _call(address, bus, path, method, *args, timeout=10):
        calls.append((address, bus, path, method, args, timeout))
        if path not in tree_spec:
            return None
        role, name, bits, children = tree_spec[path]
        if method.endswith(".GetChildren"):
            if children is None:
                return None
            return "(" + repr([(":1.42", c) for c in children]) + ",)"
        if method.endswith(".GetRoleName"):
            return "(" + repr(role) + ",)"
        if method.endswith(".GetState"):
            return f"([uint32 {bits}, 0],)"
        if method == "org.freedesktop.DBus.Properties.Get" and args == ("org.a11y.atspi.Accessible", "Name"):
            return "(<" + repr(name) + ">,)"
        return None

    return _call, calls


def default_atspi_tree() -> dict[str, tuple[str, str, int, list[str] | None]]:
    sensitive = 1 << 23
    checked = (1 << 23) | (1 << 4)
    return {
        "/root": ("window", "TARGET Calc", sensitive, ["/toolbar", "/entry", "/opt"]),
        "/toolbar": ("tool bar", "Main", sensitive, ["/btn_run", "/btn_clear"]),
        "/btn_run": ("push button", "Run", sensitive, []),
        "/btn_clear": ("push button", "Clear", sensitive, []),
        "/entry": ("entry", "Expression", sensitive, []),
        "/opt": ("check box", "Exact", checked, []),
    }


def make_target_rgb(width: int = 8, height: int = 6, seed: int = 17) -> np.ndarray:
    """Create a deterministic TARGET H x W x 3 uint8 RGB frame in RAM."""
    total = width * height * 3
    vals = [(seed + i * 31) & 0xFF for i in range(total)]
    return np.array(vals, dtype=np.uint8).reshape((height, width, 3))


class W8UDesktopAdapterAndModalityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = DeterministicVmClock()
        self.registry = SessionRegistry(clock=self.clock)
        self.transport = FakeDesktopTransport()
        self.handle = self.registry.register(
            "xvfb_screen",
            "xvfb-screen-target-0",
            self.transport,
            owned=True,
        )
        self.auth_token = b"secret-x11-cookie-01"
        self.identity = PrivateXIdentity(
            handle=self.handle,
            display=":99",
            window_id="0x400001",
            pid=4242,
            auth_token=self.auth_token,
            x11_unix_listeners=1,
            x11_tcp_listeners=0,
            x11_abstract_listeners=0,
        )
        self.spy = DiskSinkSpy()

    def _make_atspi_cap(self, **overrides) -> AuthenticatedAtspiCapability:
        call_fn, _ = make_fake_atspi_call(default_atspi_tree())
        params = {
            "handle": self.handle,
            "display": ":99",
            "window_id": "0x400001",
            "pid": 4242,
            "address": "unix:abstract=/tmp/dbus-fake-w8u",
            "bus": ":1.42",
            "root_path": "/root",
            "title": "TARGET Calc",
            "auth_token": self.auth_token,
            "call": call_fn,
        }
        params.update(overrides)
        return AuthenticatedAtspiCapability(**params)

    def _make_pair_evidence(self, seq: int = 0, rgb: np.ndarray | None = None, **overrides) -> InjectedPairEvidence:
        arr = make_target_rgb(8, 6, seed=10 + seq) if rgb is None else rgb
        xwd_a = build_xwd_bytes(arr, byte_order=1, bits_per_pixel=24)
        xwd_b = build_xwd_bytes(arr, byte_order=0, bits_per_pixel=32, pad_bytes_per_line=4)
        epoch_val = overrides.pop("epoch", None)
        if epoch_val is None:
            epoch_val = self.registry.resolve(self.handle).epoch
        params = {
            "contract_revision": W8U_OS_CONTRACT_REVISION,
            "run_id": "run-target-01",
            "lease_id": "lease-00f2f718776b5603",
            "handle": self.handle,
            "epoch": epoch_val,
            "display": ":99",
            "window_id": "0x400001",
            "seq": seq,
            "capture_mono_ns": 5_000_000 + seq * 10_000,
            "grab_a": xwd_a,
            "grab_b": xwd_b,
            "auth_token": self.auth_token,
            "tick": 100 + seq,
            "tick_valid": True,
            "tick_witness": TickWitness(tick=100 + seq, source_id="sim-clock", witness_token=b"witness-ok"),
        }
        params.update(overrides)
        return InjectedPairEvidence(**params)

    def test_frozen_schemas_magic_end_errors_and_digest_domains(self) -> None:
        self.assertEqual(CONTROL_MAGIC, b"W8UCTRL1")
        self.assertEqual(FRAME_PAIR_MAGIC, b"W8UPAIR1")
        self.assertEqual(sorted(CONTROL_KINDS.keys()), ["ACK", "END", "EOF", "FRAME_PAIR", "HELLO", "STOP"])
        self.assertEqual(sorted(CONTROL_BODY_SCHEMAS.keys()), ["ACK", "END", "EOF", "FRAME_PAIR", "HELLO", "STOP"])
        self.assertIn(END_OK, END_NUMERIC_ERRORS)
        self.assertGreaterEqual(len(END_NUMERIC_ERRORS), 12)
        self.assertEqual(
            sorted(DIGEST_DOMAINS.keys()),
            ["action_grant", "action_log", "action_outcome", "frame_binding", "frame_rgb24", "state", "t2_trace"],
        )
        binding, bind_sha = build_frame_binding(
            contract_revision=W8U_OS_CONTRACT_REVISION,
            run_id="run-target-01",
            lease_id="lease-00f2f718776b5603",
            handle=self.handle,
            epoch=0,
            display=":99",
            window_id="0x400001",
            seq=0,
            width=8,
            height=6,
            stride=24,
            frame_sha256="a" * 64,
        )
        self.assertEqual(tuple(binding.keys()), BINDING_SCHEMA_KEYS)
        self.assertEqual(len(bind_sha), 64)
        res = build_observation_result(
            run_id="run-target-01",
            lease_id="lease-00f2f718776b5603",
            handle=self.handle,
            epoch=0,
            frame_seq=0,
            dpr_ppm=1_000_000,
            modality_policy="vision_only",
            semantic_modality=None,
            state_sha256="b" * 64,
            frame_sha256="a" * 64,
            binding_sha256=bind_sha,
        )
        self.assertEqual(tuple(res.keys()), RESULT_SCHEMA_KEYS)
        self.assertEqual(res["semantic_modality"], AUSENTE)

    def test_positive_complete_atspi_converter_and_receipt_t2_evidence(self) -> None:
        with self.spy.guard():
            cap = self._make_atspi_cap()
            adapter = W8UDesktopAdapter(
                registry=self.registry,
                handle=self.handle,
                identity=self.identity,
                run_id="run-target-01",
                lease_id="lease-00f2f718776b5603",
                modality_policy="atspi",
                atspi_capability=cap,
                dpr_ppm=1_000_000,
                reserved_capacity=2,
                clock=self.clock,
            )
            ev0 = self._make_pair_evidence(seq=0)
            obs0, desc0, meta0 = adapter.observe_pair(
                ev0,
                simulation_state_sha256="a" * 64,
            )
            self.assertIsInstance(obs0, Observation)
            self.assertIsInstance(obs0.frame, FrameRef)
            self.assertEqual(obs0.handle, self.handle)
            self.assertEqual(obs0.epoch, 0)
            self.assertEqual(obs0.frame_seq, 0)
            self.assertEqual(obs0.state["schema"], "wender.state/v1")
            self.assertEqual(obs0.state["source"]["kind"], "atspi")
            self.assertGreater(len(obs0.state["nodes"]), 0)
            self.assertEqual(schema.validate_state(obs0.state), [])
            self.assertEqual(schema.validate_target_descriptor(desc0, expected_epoch=0), [])
            self.assertEqual(validate_frame_metadata(meta0), [])

            raw_state_json = schema.dumps(obs0.state)
            self.assertNotIn("\n", raw_state_json)
            expected_state_sha = hashlib.sha256(raw_state_json.encode("utf-8")).hexdigest()
            self.assertEqual(state_digest(obs0.state), expected_state_sha)

            adapter.ack_frame(0, grant=1)
            ev1 = self._make_pair_evidence(seq=1)
            obs1, desc1, meta1 = adapter.observe_pair(ev1, simulation_state_sha256="b" * 64)
            self.assertEqual(obs1.frame_seq, 1)
            self.assertEqual(schema.validate_target_descriptor(desc1, expected_epoch=0), [])
            self.assertEqual(validate_frame_metadata(meta1), [])

            receipt = adapter.purge_ram_and_close()
            self.assertEqual(validate_receipt(receipt, modality_policy="atspi"), [])
            self.assertEqual(receipt["ui_gate_verdict"], "PASS")
            self.assertEqual(receipt["ui_gate_code"], "OK")
            self.assertEqual(receipt["frames_captured"], 2)
            self.assertEqual(receipt["t2_ticks"], 2)
            self.assertEqual(receipt["t2_terminal_sha256"], state_digest(obs1.state))
            self.assertEqual(receipt["final_state_sha256"], state_digest(obs1.state))
            self.assertEqual(receipt["simulation_state_sha256"], "b" * 64)
            self.assertTrue(receipt["reader_quiescent"])
            self.assertTrue(receipt["ram_purged"])
            self.assertEqual(receipt["frames_disk_bytes"], 0)
            self.assertEqual(len(self.spy.events), 0)

    def test_positive_explicit_vision_only_policy_retains_semantic_absence_and_null_t2(self) -> None:
        with self.spy.guard():
            adapter = W8UDesktopAdapter(
                registry=self.registry,
                handle=self.handle,
                identity=self.identity,
                run_id="run-vision-01",
                lease_id="lease-00f2f718776b5603",
                modality_policy="vision_only",
                atspi_capability=None,
                dpr_ppm=1_500_000,
                reserved_capacity=2,
                clock=self.clock,
            )
            self.assertIsNone(adapter.semantic_modality)
            ev0 = self._make_pair_evidence(
                seq=0,
                run_id="run-vision-01",
                tick=None,
                tick_valid=False,
                tick_witness=None,
            )
            obs0, desc0, meta0 = adapter.observe_pair(ev0)
            self.assertEqual(obs0.state["source"]["kind"], "vision")
            self.assertEqual(obs0.state["nodes"], [])
            self.assertEqual(obs0.state["counts"], {"raw": 0, "nodes": 0, "actionable": 0})
            self.assertEqual(schema.validate_state(obs0.state), [])
            self.assertEqual(schema.validate_target_descriptor(desc0, expected_epoch=0), [])
            self.assertIsNone(meta0["tick"])
            self.assertFalse(meta0["tick_valid"])
            self.assertEqual(adapter._results[0]["semantic_modality"], AUSENTE)

            receipt = adapter.purge_ram_and_close()
            self.assertEqual(validate_receipt(receipt, modality_policy="vision_only"), [])
            self.assertEqual(receipt["ui_gate_verdict"], "PASS")
            self.assertIsNone(receipt["t2_trace_sha256"])
            self.assertIsNone(receipt["t2_terminal_sha256"])
            self.assertIsNone(receipt["t2_ticks"])
            self.assertIsNone(receipt["simulation_state_sha256"])
            self.assertEqual(receipt["final_state_sha256"], state_digest(obs0.state))
            self.assertEqual(len(self.spy.events), 0)

    def test_must_fail_modality_controls_empty_semantics_discovery_only_and_truncation(self) -> None:
        # 1. Missing mandatory converter in atspi policy
        with self.assertRaises(ModalityError) as ctx_missing:
            W8UDesktopAdapter(
                registry=self.registry,
                handle=self.handle,
                identity=self.identity,
                run_id="run-fail-mod",
                lease_id="lease-00f2f718776b5603",
                modality_policy="atspi",
                atspi_capability=None,
            )
        self.assertEqual(ctx_missing.exception.code, E_MODALITY)

        # 2. Discovery-only fixture claiming AT-SPI completeness must fail with E_MODALITY
        idx_call = lambda address, dest, path, method, *args, timeout=10: (
            "([(':1.42', objectpath '/org/a11y/atspi/accessible/root')],)"
            if method.endswith("GetChildren")
            else "(uint32 4242,)"
            if method.endswith("GetConnectionUnixProcessID")
            else "(<3>,)"
            if args and args[-1] == "ChildCount"
            else "(<'TARGET Calc'>,)"
        )
        idx = atspi.Index(address="unix:abstract=/tmp/dbus-fake", call=idx_call)
        summary = atspi.window_summary(4242, idx)
        self.assertTrue(summary["available"])
        self.assertTrue(summary["tree"])

        disc_cap = self._make_atspi_cap(discovery_only=True)
        adapter_disc = W8UDesktopAdapter(
            registry=self.registry,
            handle=self.handle,
            identity=self.identity,
            run_id="run-target-01",
            lease_id="lease-00f2f718776b5603",
            modality_policy="atspi",
            atspi_capability=disc_cap,
        )
        with self.assertRaises(ModalityError) as ctx_disc:
            adapter_disc.observe_pair(self._make_pair_evidence(seq=0))
        self.assertEqual(ctx_disc.exception.code, E_MODALITY)

        # 3. Empty semantics sold as complete (tree with only unmapped filler role -> 0 emitted nodes)
        empty_call, _ = make_fake_atspi_call({"/root": ("filler", "Empty", 1 << 23, [])})
        empty_cap = self._make_atspi_cap(call=empty_call)
        adapter_empty = W8UDesktopAdapter(
            registry=self.registry,
            handle=self.handle,
            identity=self.identity,
            run_id="run-target-01",
            lease_id="lease-00f2f718776b5603",
            modality_policy="atspi",
            atspi_capability=empty_cap,
        )
        with self.assertRaises(ModalityError) as ctx_empty:
            adapter_empty.observe_pair(self._make_pair_evidence(seq=0))
        self.assertEqual(ctx_empty.exception.code, E_MODALITY)

        # 4. Partial / truncated AT-SPI traversal must fail with E_MODALITY
        trunc_cap = self._make_atspi_cap(max_nodes=2)
        adapter_trunc = W8UDesktopAdapter(
            registry=self.registry,
            handle=self.handle,
            identity=self.identity,
            run_id="run-target-01",
            lease_id="lease-00f2f718776b5603",
            modality_policy="atspi",
            atspi_capability=trunc_cap,
        )
        with self.assertRaises(ModalityError) as ctx_trunc:
            adapter_trunc.observe_pair(self._make_pair_evidence(seq=0))
        self.assertEqual(ctx_trunc.exception.code, E_MODALITY)

        # 5. Partial box coverage when require_boxes=True -> E_MODALITY
        partial_box_cap = self._make_atspi_cap(boxes={}, require_boxes=True)
        adapter_pbox = W8UDesktopAdapter(
            registry=self.registry,
            handle=self.handle,
            identity=self.identity,
            run_id="run-target-01",
            lease_id="lease-00f2f718776b5603",
            modality_policy="atspi",
            atspi_capability=partial_box_cap,
        )
        with self.assertRaises(ModalityError) as ctx_pbox:
            adapter_pbox.observe_pair(self._make_pair_evidence(seq=0))
        self.assertEqual(ctx_pbox.exception.code, E_MODALITY)

        # 6. Out-of-bounds or bool box coordinates in AT-SPI capability -> E_COORDINATES
        walk_res = atspi_tree.walk("fake", ":1.42", "/root", call=self._make_atspi_cap().call)
        first_nid = walk_res["nodes"][0]["id"]
        bad_box_cap = self._make_atspi_cap(boxes={first_nid: [0, 0, 99, 99]}, require_boxes=False)
        adapter_bbox = W8UDesktopAdapter(
            registry=self.registry,
            handle=self.handle,
            identity=self.identity,
            run_id="run-target-01",
            lease_id="lease-00f2f718776b5603",
            modality_policy="atspi",
            atspi_capability=bad_box_cap,
        )
        with self.assertRaises(W8UDesktopError) as ctx_bbox:
            adapter_bbox.observe_pair(self._make_pair_evidence(seq=0))
        self.assertEqual(ctx_bbox.exception.code, E_COORDINATES)

        # 7. _perception wrapper or float in override_state rejected
        valid_vision = build_vision_only_state(title="v", width=8, height=6)
        wrapped = copy.deepcopy(valid_vision)
        wrapped["_perception"] = {"wrapper": True}
        adapter_v = W8UDesktopAdapter(
            registry=self.registry,
            handle=self.handle,
            identity=self.identity,
            run_id="run-target-01",
            lease_id="lease-00f2f718776b5603",
            modality_policy="vision_only",
        )
        with self.assertRaises(W8UDesktopError) as ctx_wrap:
            adapter_v.observe_pair(self._make_pair_evidence(seq=0, tick=None, tick_valid=False, tick_witness=None), override_state=wrapped)
        self.assertEqual(ctx_wrap.exception.code, E_SCHEMA)

        # 8. Forbidden fallback options (xdotool, global_focus, host_clipboard, active_window_discovery)
        for bad_opt in ("xdotool", "global_focus", "host_clipboard", "active_window_discovery", "display_discovery_fallback"):
            with self.assertRaises(ModalityError) as ctx_opt:
                W8UDesktopAdapter(
                    registry=self.registry,
                    handle=self.handle,
                    identity=self.identity,
                    run_id="run-target-01",
                    lease_id="lease-00f2f718776b5603",
                    modality_policy="vision_only",
                    options={bad_opt: True},
                )
            self.assertEqual(ctx_opt.exception.code, E_MODALITY)

    def test_must_fail_foreign_target_stale_generation_bad_dpr_and_invented_tick(self) -> None:
        adapter = W8UDesktopAdapter(
            registry=self.registry,
            handle=self.handle,
            identity=self.identity,
            run_id="run-target-01",
            lease_id="lease-00f2f718776b5603",
            modality_policy="atspi",
            atspi_capability=self._make_atspi_cap(),
        )

        # Foreign display
        with self.assertRaises(ForeignTargetError) as ctx_disp:
            adapter.observe_pair(self._make_pair_evidence(seq=0, display=":77"))
        self.assertEqual(ctx_disp.exception.code, E_FOREIGN_TARGET)

        # Foreign window
        with self.assertRaises(ForeignTargetError) as ctx_win:
            adapter.observe_pair(self._make_pair_evidence(seq=0, window_id="0x999999"))
        self.assertEqual(ctx_win.exception.code, E_FOREIGN_TARGET)

        # Host display :0 rejected at PrivateXIdentity creation
        with self.assertRaises(ForeignTargetError):
            PrivateXIdentity(
                handle=self.handle,
                display=":0",
                window_id="0x400001",
                pid=4242,
                auth_token=self.auth_token,
            )

        # Non-zero TCP listeners rejected
        with self.assertRaises(ForeignTargetError):
            PrivateXIdentity(
                handle=self.handle,
                display=":99",
                window_id="0x400001",
                pid=4242,
                auth_token=self.auth_token,
                x11_tcp_listeners=1,
            )

        # Stale generation on evidence handle
        stale_handle = Handle(self.handle.kind, self.handle.canonical_id, self.handle.generation + 1)
        with self.assertRaises(ForeignTargetError) as ctx_gen:
            adapter.observe_pair(self._make_pair_evidence(seq=0, handle=stale_handle))
        self.assertEqual(ctx_gen.exception.code, E_STALE_GENERATION)

        # Respawned target in SessionRegistry invalidates old adapter handle generation
        close_rec = self.registry.close(self.handle)
        self.assertEqual(close_rec.status, "closed")
        respawned = self.registry.register("xvfb_screen", "xvfb-screen-target-0", self.transport, owned=True)
        self.assertGreater(respawned.generation, self.handle.generation)
        with self.assertRaises(ForeignTargetError) as ctx_respawn:
            adapter.observe_pair(self._make_pair_evidence(seq=0, epoch=0))
        self.assertEqual(ctx_respawn.exception.code, E_STALE_GENERATION)

        # Re-bind to fresh respawned handle for remaining checks
        self.handle = respawned
        self.identity = PrivateXIdentity(
            handle=self.handle,
            display=":99",
            window_id="0x400001",
            pid=4242,
            auth_token=self.auth_token,
        )
        adapter = W8UDesktopAdapter(
            registry=self.registry,
            handle=self.handle,
            identity=self.identity,
            run_id="run-target-01",
            lease_id="lease-00f2f718776b5603",
            modality_policy="atspi",
            atspi_capability=self._make_atspi_cap(),
        )

        # Bad DPR (0, negative, bool, float)
        for bad_dpr in (0, -100, True, 1.5):
            with self.assertRaises(W8UDesktopError) as ctx_dpr:
                W8UDesktopAdapter(
                    registry=self.registry,
                    handle=self.handle,
                    identity=self.identity,
                    run_id="run-target-01",
                    lease_id="lease-00f2f718776b5603",
                    modality_policy="vision_only",
                    dpr_ppm=bad_dpr,  # type: ignore[arg-type]
                )
            self.assertEqual(ctx_dpr.exception.code, E_DPR)

        # Timestamp invented as tick
        mono_ns = 5_000_000
        with self.assertRaises(TickInventedError) as ctx_tick1:
            adapter.observe_pair(
                self._make_pair_evidence(
                    seq=0,
                    capture_mono_ns=mono_ns,
                    tick=mono_ns,
                    tick_valid=True,
                    tick_witness=TickWitness(tick=mono_ns, source_id="clock", witness_token=b"tok"),
                )
            )
        self.assertEqual(ctx_tick1.exception.code, E_TICK_INVENTED)

        # Tick invalid committed as 0 instead of None
        with self.assertRaises(TickInventedError) as ctx_tick2:
            adapter.observe_pair(
                self._make_pair_evidence(
                    seq=0,
                    tick=0,
                    tick_valid=False,
                    tick_witness=None,
                )
            )
        self.assertEqual(ctx_tick2.exception.code, E_TICK_INVENTED)

    def test_post_observation_producer_mutation_cannot_alter_owned_data(self) -> None:
        rgb = make_target_rgb(8, 6, seed=33)
        grab_a = bytearray(build_xwd_bytes(rgb))
        grab_b = bytearray(build_xwd_bytes(rgb))

        adapter = W8UDesktopAdapter(
            registry=self.registry,
            handle=self.handle,
            identity=self.identity,
            run_id="run-target-01",
            lease_id="lease-00f2f718776b5603",
            modality_policy="atspi",
            atspi_capability=self._make_atspi_cap(),
        )
        ev = self._make_pair_evidence(seq=0, grab_a=bytes(grab_a), grab_b=bytes(grab_b))
        obs, desc, meta = adapter.observe_pair(ev)

        orig_rgb_bytes = obs.frame.rgb.tobytes()
        orig_state_json = schema.dumps(obs.state)
        orig_meta_sha = meta["sha256"]

        # Mutate producer buffers and returned copies
        rgb.fill(255)
        grab_a[-10:] = b"\xff" * 10
        grab_b[-10:] = b"\x00" * 10
        desc["state"]["nodes"].clear()
        meta["width"] = 99999

        self.assertEqual(obs.frame.rgb.tobytes(), orig_rgb_bytes)
        self.assertFalse(obs.frame.rgb.flags.writeable)
        with self.assertRaises(ValueError):
            obs.frame.rgb.setflags(write=True)
        with self.assertRaises(FrozenInstanceError):
            obs.epoch = 77  # type: ignore[misc]
        self.assertEqual(schema.dumps(obs.state), orig_state_json)
        self.assertEqual(adapter._frame_metas[0]["width"], 8)
        self.assertEqual(adapter._frame_metas[0]["sha256"], orig_meta_sha)


class XwdPairAndCreditControllerTests(unittest.TestCase):
    def test_xwd_common_subset_and_same_grab_pair_vectors(self) -> None:
        rgb = make_target_rgb(10, 4, seed=91)
        xwd_24_msb = build_xwd_bytes(rgb, bits_per_pixel=24, byte_order=1)
        xwd_32_lsb = build_xwd_bytes(rgb, bits_per_pixel=32, byte_order=0, pad_bytes_per_line=8)

        info = verify_same_grab_pair(xwd_24_msb, xwd_32_lsb)
        self.assertEqual((info.width, info.height, info.stride), (10, 4, 30))
        self.assertEqual(info.rgb_bytes, rgb.tobytes())
        self.assertEqual(info.rgb_sha256, hashlib.sha256(rgb.tobytes()).hexdigest())

        # Corrupted second grab in held pair -> PairMismatchError (E_PAIR_MISMATCH)
        other_rgb = rgb.copy()
        other_rgb[0, 0, 0] ^= 0xFF
        xwd_corrupt = build_xwd_bytes(other_rgb, bits_per_pixel=24, byte_order=1)
        with self.assertRaises(PairMismatchError) as ctx_pair:
            verify_same_grab_pair(xwd_24_msb, xwd_corrupt)
        self.assertEqual(ctx_pair.exception.code, E_PAIR_MISMATCH)

        # Invalid XWD header version -> PairMismatchError
        bad_ver = bytearray(xwd_24_msb)
        struct.pack_into(">I", bad_ver, 4, 6)
        with self.assertRaises(PairMismatchError):
            parse_xwd_common_subset(bytes(bad_ver))

    def test_credit_controller_capacity_one_exhaustion_duplicate_ack_and_inflation(self) -> None:
        # Capacity 1: initial ACK seq 0 grants min(2, 1) == 1
        c1 = CreditController(reserved_capacity=1)
        self.assertEqual(c1.ack_initial(0), 1)
        self.assertEqual(c1.cumulative_credits, 1)
        self.assertEqual(c1.available_credits, 1)

        # Overgrant on initial ACK seq 0 for capacity 1 -> E_CREDIT
        c1_over = CreditController(reserved_capacity=1)
        with self.assertRaises(CreditError) as ctx_over0:
            c1_over.ack_initial(0, grant=2)
        self.assertEqual(ctx_over0.exception.code, E_CREDIT)

        # Emit frame 0 -> exhausts capacity 1
        c1.consume_for_frame(0)
        self.assertEqual(c1.available_credits, 0)
        self.assertEqual(c1.in_flight, 1)

        # Exhaustion: emitting frame 1 before ACK of frame 0 -> E_CREDIT
        with self.assertRaises(CreditError) as ctx_exh:
            c1.consume_for_frame(1)
        self.assertEqual(ctx_exh.exception.code, E_CREDIT)

        # Credit inflation: attempting to grant 2 credits on ACK of frame 0 (cumulative 3 > 1 + 1) -> E_CREDIT
        with self.assertRaises(CreditError) as ctx_inf:
            c1.ack_frame(0, grant=2)
        self.assertEqual(ctx_inf.exception.code, E_CREDIT)

        # Valid ACK of frame 0 with grant 1 -> cumulative_credits == 2 == acked(1) + capacity(1)
        self.assertEqual(c1.ack_frame(0, grant=1), 2)
        self.assertEqual(c1.available_credits, 1)

        # Duplicate ACK of frame 0 -> E_CREDIT
        with self.assertRaises(CreditError) as ctx_dup:
            c1.ack_frame(0, grant=1)
        self.assertEqual(ctx_dup.exception.code, E_CREDIT)

    def test_pipe_no_reopen_per_frame_and_mandatory_stop_end_eof_join(self) -> None:
        pipe = InMemoryExecPipe(reserved_capacity=2)
        hello = {
            "contract_revision": W8U_OS_CONTRACT_REVISION,
            "run_id": "run-pipe-1",
            "lease_id": "lease-1",
            "canonical_id": "xvfb-0",
            "generation": 1,
            "epoch": 0,
            "reserved_capacity": 2,
        }
        self.assertEqual(pipe.open_once(hello), 2)

        # Reopen per frame is forbidden -> E_REOPEN
        with self.assertRaises(ProtocolError) as ctx_reopen:
            pipe.open_once(hello)
        self.assertEqual(ctx_reopen.exception.code, E_REOPEN)

        # Join before STOP/END/EOF is forbidden
        with self.assertRaises(ProtocolError):
            pipe.join()

        pipe.send_stop("user_stop")
        # Stale publication after STOP is forbidden
        with self.assertRaises(ProtocolError):
            pipe.ensure_active()

        pipe.send_end(END_OK, frames_captured=0, reason="stopped")
        pipe.close_eof()
        self.assertEqual(pipe.join(), 0)
        self.assertTrue(pipe.joined)


class W8UActionGateContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = DeterministicVmClock()
        self.registry = SessionRegistry(clock=self.clock)
        self.handle = self.registry.register("x11_window", "0x400001", FakeDesktopTransport(), owned=True)
        self.secret = b"external-authority-hmac-key"
        self.rgb = make_target_rgb(100, 80, seed=5)
        self.frame_ref = self.registry.admit_frame(self.handle, 0, self.rgb, epoch=0)
        self.actuator_calls: list[tuple[str, dict]] = []
        self.actuator_behavior: dict = {"applied": True, "applied_tick": 12}

        def _fake_actuator(verb: str, args: dict):
            self.actuator_calls.append((verb, args))
            return copy.deepcopy(self.actuator_behavior)

        self.authority = ActionAuthorityContext(
            run_id="run-act-01",
            lease_id="lease-00f2f718776b5603",
            handle=self.handle,
            epoch=0,
            authority_secret=self.secret,
            lock_owned=True,
            focused=True,
            editable_focus=True,
            remaining_budget=25,
        )
        self.gate = W8UActionGate(
            registry=self.registry,
            authority=self.authority,
            clock=self.clock,
            actuator=_fake_actuator,
        )
        self.gate.update_observed_frame(self.frame_ref)

    def _make_req(
        self,
        *,
        action_id: str = "act-0",
        action_seq: int = 0,
        verb: str = "pointer",
        args: dict | None = None,
        **overrides,
    ) -> dict:
        if args is None:
            args = {"operation": "click", "x": 10, "y": 20, "button": 1}
        grant = compute_action_grant_sha256(
            run_id="run-act-01",
            lease_id="lease-00f2f718776b5603",
            handle=self.handle,
            epoch=0,
            action_id=action_id,
            action_seq=action_seq,
            authority_secret=self.secret,
        )
        req = {
            "schema": W8U_ACTION_SCHEMA,
            "run_id": "run-act-01",
            "lease_id": "lease-00f2f718776b5603",
            "epoch": 0,
            "generation": self.handle.generation,
            "action_id": action_id,
            "action_seq": action_seq,
            "observed_frame_seq": self.frame_ref.frame_seq,
            "observed_frame_sha256": self.frame_ref.digest,
            "deadline_ns": self.clock.now_ns + 50_000_000_000,
            "verb": verb,
            "args": args,
            "grant_sha256": grant,
        }
        req.update(overrides)
        return req

    def test_valid_verbs_pointer_key_text_wait_and_idempotent_replay(self) -> None:
        # 1. Pointer click
        req0 = self._make_req(
            action_id="act-0",
            action_seq=0,
            verb="pointer",
            args={"operation": "click", "x": 50, "y": 40, "button": 1},
        )
        resp0, rc0 = self.gate.submit(schema.dumps(req0).encode("utf-8"))
        self.assertEqual(rc0, RC_OK)
        self.assertEqual(sorted(resp0.keys()), sorted(ACTION_RESPONSE_KEYS))
        self.assertEqual((resp0["status"], resp0["code"], resp0["emitted"], resp0["result_known"], resp0["applied_tick"]),
                         ("applied", OK_ADMITTED, True, True, 12))
        self.assertEqual(len(self.actuator_calls), 1)

        # Identical replay is idempotent (does not call actuator again)
        replay_resp, replay_rc = self.gate.submit(schema.dumps(req0).encode("utf-8"))
        self.assertEqual(replay_rc, RC_OK)
        self.assertEqual(replay_resp, resp0)
        self.assertEqual(len(self.actuator_calls), 1)

        # Changed payload with same action_id refuses with E_SCHEMA, rc=2 without calling actuator
        changed_req0 = copy.deepcopy(req0)
        changed_req0["args"]["x"] = 51
        bad_replay_resp, bad_replay_rc = self.gate.submit(schema.dumps(changed_req0))
        self.assertEqual(bad_replay_rc, RC_SCHEMA)
        self.assertEqual((bad_replay_resp["status"], bad_replay_resp["code"], bad_replay_resp["emitted"]),
                         ("refused", E_SCHEMA, False))
        self.assertEqual(len(self.actuator_calls), 1)

        # 2. Pointer scroll with valid delta -120..120
        req1 = self._make_req(
            action_id="act-1",
            action_seq=1,
            verb="pointer",
            args={"operation": "scroll", "x": 10, "y": 10, "dx": 0, "dy": -120},
        )
        resp1, rc1 = self.gate.submit(req1)
        self.assertEqual((rc1, resp1["code"]), (RC_OK, OK_ADMITTED))

        # 3. All finite keys and ordered modifiers
        seq_num = 2
        for kname in sorted(FINITE_KEYS):
            req_k = self._make_req(
                action_id=f"act-key-{kname}",
                action_seq=seq_num,
                verb="key",
                args={"operation": "press", "key": kname, "modifiers": list(ORDERED_MODIFIERS)},
            )
            resp_k, rc_k = self.gate.submit(req_k)
            self.assertEqual((rc_k, resp_k["code"]), (RC_OK, OK_ADMITTED), kname)
            seq_num += 1

        # 4. Text type with editable focus
        req_txt = self._make_req(
            action_id="act-txt",
            action_seq=seq_num,
            verb="text",
            args={"mode": "type", "text": "hello_w8u"},
        )
        resp_txt, rc_txt = self.gate.submit(req_txt)
        self.assertEqual((rc_txt, resp_txt["code"]), (RC_OK, OK_ADMITTED))
        seq_num += 1

        # 5. Wait within 0..5000ms and deadline (admission without applied effect)
        self.actuator_behavior = {"applied": False, "applied_tick": None}
        req_wait = self._make_req(
            action_id="act-wait",
            action_seq=seq_num,
            verb="wait",
            args={"duration_ms": 250},
        )
        resp_wait, rc_wait = self.gate.submit(req_wait)
        self.assertEqual((rc_wait, resp_wait["status"], resp_wait["code"], resp_wait["result_known"], resp_wait["applied_tick"]),
                         (RC_OK, "admitted", OK_ADMITTED, False, None))

    def test_schema_and_payload_refusals_before_actuator(self) -> None:
        # 1. > 64 KiB body cap
        oversized = b'{"a":"' + (b"x" * ACTION_MAX_UTF8_BYTES) + b'"}'
        resp, rc = self.gate.submit(oversized)
        self.assertEqual((rc, resp["code"], resp["emitted"]), (RC_SCHEMA, E_SCHEMA, False))

        # 2. Invalid UTF-8 bytes
        resp, rc = self.gate.submit(b'{"schema":"\xff\xfe"}')
        self.assertEqual((rc, resp["code"]), (RC_SCHEMA, E_SCHEMA))

        # 3. Duplicate JSON keys
        base_json = schema.dumps(self._make_req(action_id="dup-1"))
        dup_json = base_json[:-1] + ',"verb":"pointer"}'
        resp, rc = self.gate.submit(dup_json)
        self.assertEqual((rc, resp["code"]), (RC_SCHEMA, E_SCHEMA))

        # 4. Trailing data
        resp, rc = self.gate.submit(base_json + " trailing")
        self.assertEqual((rc, resp["code"]), (RC_SCHEMA, E_SCHEMA))

        # 5. Float coordinate & bool-as-int coordinate
        resp, rc = self.gate.submit(base_json.replace('"x":10', '"x":10.5'))
        self.assertEqual((rc, resp["code"]), (RC_SCHEMA, E_SCHEMA))
        bool_coord_req = self._make_req(
            action_id="bool-coord",
            args={"operation": "move", "x": True, "y": 10},
        )
        resp, rc = self.gate.submit(bool_coord_req)
        self.assertEqual((rc, resp["code"]), (RC_SCHEMA, E_SCHEMA))

        # 6. Out-of-bounds coordinates, zero scroll delta, disordered modifiers, NUL text, wait > 5000, unknown verb
        bad_cases = [
            ("oob-x", "pointer", {"operation": "move", "x": 100, "y": 10}, E_SCHEMA),
            ("bad-btn", "pointer", {"operation": "click", "x": 10, "y": 10, "button": 4}, E_SCHEMA),
            ("bool-btn", "pointer", {"operation": "click", "x": 10, "y": 10, "button": True}, E_SCHEMA),
            ("zero-scroll", "pointer", {"operation": "scroll", "x": 10, "y": 10, "dx": 0, "dy": 0}, E_SCHEMA),
            ("big-scroll", "pointer", {"operation": "scroll", "x": 10, "y": 10, "dx": 121, "dy": 0}, E_SCHEMA),
            ("bad-mods", "key", {"operation": "press", "key": "Enter", "modifiers": ["Control", "Shift"]}, E_SCHEMA),
            ("dup-mods", "key", {"operation": "press", "key": "Enter", "modifiers": ["Shift", "Shift"]}, E_SCHEMA),
            ("bad-key", "key", {"operation": "press", "key": "F13", "modifiers": []}, E_SCHEMA),
            ("nul-text", "text", {"mode": "type", "text": "abc\x00def"}, E_SCHEMA),
            ("paste-text", "text", {"mode": "paste", "text": "abc"}, E_SCHEMA),
            ("long-text", "text", {"mode": "type", "text": "a" * 4097}, E_SCHEMA),
            ("long-wait", "wait", {"duration_ms": 5001}, E_SCHEMA),
            ("neg-wait", "wait", {"duration_ms": -1}, E_SCHEMA),
            ("bad-verb", "clipboard_copy", {}, E_VERB),
        ]
        for aid, verb, args, expected_code in bad_cases:
            req = self._make_req(action_id=aid, verb=verb, args=args)
            resp, rc = self.gate.submit(req)
            self.assertEqual((rc, resp["code"], resp["emitted"]), (RC_SCHEMA, expected_code, False), aid)

        self.assertEqual(len(self.actuator_calls), 0)

    def test_verification_refusals_budget_unavailable_and_lost_ack_quarantine(self) -> None:
        # Stale frame -> E_STALE_FRAME (rc=5)
        stale_req = self._make_req(action_id="stale-f", observed_frame_seq=99)
        resp, rc = self.gate.submit(stale_req)
        self.assertEqual((rc, resp["code"]), (RC_REFUSED, E_STALE_FRAME))

        # Forged grant -> E_LOCK (rc=5)
        forged_req = self._make_req(action_id="forged-g", grant_sha256="f" * 64)
        resp, rc = self.gate.submit(forged_req)
        self.assertEqual((rc, resp["code"]), (RC_REFUSED, E_LOCK))

        # Stale epoch -> E_EPOCH (rc=5)
        epoch_req = self._make_req(action_id="bad-ep", epoch=3)
        resp, rc = self.gate.submit(epoch_req)
        self.assertEqual((rc, resp["code"]), (RC_REFUSED, E_EPOCH))

        # Expired deadline -> E_DEADLINE (rc=5)
        dl_req = self._make_req(action_id="bad-dl", deadline_ns=10)
        resp, rc = self.gate.submit(dl_req)
        self.assertEqual((rc, resp["code"]), (RC_REFUSED, E_DEADLINE))

        # Budget exhausted -> E_BUDGET (rc=5)
        self.gate.remaining_budget = 0
        bud_req = self._make_req(action_id="no-budget", verb="wait", args={"duration_ms": 0})
        resp, rc = self.gate.submit(bud_req)
        self.assertEqual((rc, resp["code"]), (RC_REFUSED, E_BUDGET))
        self.gate.remaining_budget = 5

        # Unavailable actuator -> RC_UNAVAILABLE (rc=4)
        saved_actuator = self.gate.actuator
        self.gate.actuator = None
        unavail_req = self._make_req(action_id="no-actuator", verb="wait", args={"duration_ms": 0})
        resp, rc = self.gate.submit(unavail_req)
        self.assertEqual((rc, resp["code"]), (RC_UNAVAILABLE, E_UNCERTAIN))
        self.gate.actuator = saved_actuator

        # Editable focus required for text -> E_FOCUS (rc=5)
        self.gate.authority = ActionAuthorityContext(
            run_id="run-act-01",
            lease_id="lease-00f2f718776b5603",
            handle=self.handle,
            epoch=0,
            authority_secret=self.secret,
            focused=True,
            editable_focus=False,
        )
        txt_req = self._make_req(action_id="no-edit-focus", verb="text", args={"mode": "type", "text": "hi"})
        resp, rc = self.gate.submit(txt_req)
        self.assertEqual((rc, resp["code"]), (RC_REFUSED, E_FOCUS))

        # Lost ack -> status=="uncertain", code==E_UNCERTAIN, rc==6, and session quarantined against new-ID retry
        self.gate.authority = ActionAuthorityContext(
            run_id="run-act-01",
            lease_id="lease-00f2f718776b5603",
            handle=self.handle,
            epoch=0,
            authority_secret=self.secret,
            focused=True,
            editable_focus=True,
        )
        self.actuator_behavior = {"lost_ack": True}
        lost_req = self._make_req(
            action_id="act-lost",
            action_seq=0,
            verb="pointer",
            args={"operation": "move", "x": 5, "y": 5},
        )
        resp_lost, rc_lost = self.gate.submit(lost_req)
        self.assertEqual(
            (rc_lost, resp_lost["status"], resp_lost["code"], resp_lost["emitted"], resp_lost["result_known"], resp_lost["applied_tick"]),
            (RC_UNCERTAIN, "uncertain", E_UNCERTAIN, True, False, None),
        )
        self.assertTrue(self.gate.quarantined)

        # Retry with a new action_id while quarantined is refused without calling actuator
        calls_before = len(self.actuator_calls)
        retry_req = self._make_req(
            action_id="act-retry-new-id",
            action_seq=1,
            verb="pointer",
            args={"operation": "move", "x": 6, "y": 6},
        )
        resp_retry, rc_retry = self.gate.submit(retry_req)
        self.assertEqual((rc_retry, resp_retry["status"], resp_retry["code"], resp_retry["emitted"]),
                         (RC_REFUSED, "refused", E_LOCK, False))
        self.assertEqual(len(self.actuator_calls), calls_before)

    def test_zero_frame_receipt_indeterminate_and_disk_sink_spy_enforcement(self) -> None:
        clock = DeterministicVmClock()
        registry = SessionRegistry(clock=clock)
        h = registry.register("capsule", "capsule-target-0", FakeDesktopTransport(), owned=True)
        ident = PrivateXIdentity(
            handle=h,
            display=":42",
            window_id="capsule-target-0",
            pid=9001,
            auth_token=b"capsule-auth-token",
        )
        adapter = W8UDesktopAdapter(
            registry=registry,
            handle=h,
            identity=ident,
            run_id="run-zero-frames",
            lease_id="lease-00f2f718776b5603",
            modality_policy="vision_only",
            clock=clock,
        )
        receipt = adapter.purge_ram_and_close()
        self.assertEqual(validate_receipt(receipt, modality_policy="vision_only"), [])
        self.assertEqual(receipt["frames_captured"], 0)
        self.assertIsNone(receipt["first_frame"])
        self.assertIsNone(receipt["last_frame"])
        self.assertEqual(receipt["ui_gate_verdict"], "INDETERMINATE")

        # DiskSinkSpy blocks raw, base64, and zlib-compressed frame writes
        raw_bytes = self.rgb.tobytes()
        b64_bytes = base64.b64encode(raw_bytes)
        z_bytes = zlib.compress(raw_bytes)
        spy = DiskSinkSpy()
        spy.register_frame_bytes(raw_bytes)
        with spy.guard():
            for payload in (raw_bytes, b64_bytes, z_bytes):
                with self.assertRaises(StorageLawError) as ctx_spy:
                    pathlib.Path("/tmp/forbidden_frame.bin").write_bytes(payload)
                self.assertEqual(ctx_spy.exception.code, E_STORAGE_LAW)
        self.assertEqual(spy.blocked_writes, 3)

    def test_focus_policy_bridge_with_real_session_registry(self) -> None:
        clock = DeterministicVmClock()
        registry = SessionRegistry(clock=clock)
        h = registry.register("x11_window", "0x500001", FakeDesktopTransport(), owned=True)
        locks = FakeTargetLocks()
        nonce = b"nonce-target-500001"
        locks.valid_nonces.add(nonce)
        lease = ActorLease(h, "worker-w8u", h.generation, nonce, clock.now_ns + 1_000_000)

        focus_policy = FocusPolicy(RegistryFocusAdapter(registry), locks, clock=clock)
        focus_policy.set_focus(h, domain="x11-private", epoch=0)
        grant = focus_policy.grant(lease, domain="x11-private")
        f_ref = registry.admit_frame(h, 0, make_target_rgb(4, 4), epoch=0)
        obs = registry.admit_observation(
            h,
            f_ref,
            state=build_vision_only_state(title="win", width=4, height=4),
            dpr_ppm=1_000_000,
        )
        focus_policy.validate(lease, grant, obs, domain="x11-private")
        focus_policy.consume(grant)
        with self.assertRaises(FocusRefused):
            focus_policy.consume(grant)


if __name__ == "__main__":
    unittest.main()
