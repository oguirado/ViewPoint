"""Unit and contract tests for wender.learn.w8u_trace (task w8u-wa-trace).

Covers positive and must-FAIL controls in the same unittest invocation:
- Target-frozen constants, genesis domain (`w8u.genesis/v1`), transition chain domain
  (`w8u.transition/v1`), control magic/kinds/bodies, END numeric errors, and `AUSENTE`.
- Bounded declared record limits, exact row continuity, and raw duplicate-key rejection
  before any recorder/dispatcher mutation; custom mapping input rejection.
- Revision-pinned `w8u-os-contract` frame evidence with independent accepted/rejected vectors,
  common XWD subset validation, and same-grab canonical pair equality.
- Frame credit controller (`min(2, reserved_capacity)` on initial `ACK(seq=0)`, capacity=1,
  credit exhaustion, duplicate ACK, credit inflation -> `E_CREDIT`), no reopen per frame,
  no stale publication, and mandatory `STOP -> END -> EOF -> join_reader`.
- Action contract (`w8u.action/v1`): 64 KiB body cap, duplicate/unknown keys, floats,
  bool-as-int, invalid UTF-8, trailing data, pointer (`move`, `click`/`down`/`up`, `scroll`),
  key (`press`/`down`/`up`, finite key allowlist, ordered unique modifiers), text (`type`,
  editable focus, UTF-8 without NUL, 4096-byte cap), wait (`0..5000` within deadline),
  response codes/rc values, idempotent replay, changed payload refusal, and lost-ack quarantine.
- Canonical `wender.state/v1` validation, separate observation envelope (`SessionRegistry`),
  frame metadata (`tick_valid=False` -> `tick=None`, missing witnesses `None`, zero-frame
  pixel-required runs `INDETERMINATE`).
- T2 command/outcome stream with closed injected producer schemas, LF-inclusive JSON-L digest,
  and non-T2 policy retaining `None` rather than inventing empty success.
- Independent trace/receipt verification (`verify_w8u_trace`) with exact predicate error codes:
  `E_STATE`, `E_T2`, `E_JOIN`, `E_CHAIN`, `E_PIXEL_LEAK`, `E_CREDIT`.
- Recursive metadata allowlists, volatile RAM purge, approved metadata sinks, and `DiskSinkSpy`.
"""

from __future__ import annotations

from collections.abc import Mapping
import copy
import hashlib
import pathlib
import unittest
from typing import Any

import numpy as np

from wender.learn import w8u_trace as wt
from wender.perceive import session_registry
from wender.state import schema


class FakeClock:
    """Deterministic VM fake clock in nanoseconds."""

    def __init__(self, initial_ns: int = 1_000_000_000) -> None:
        self.now_ns = initial_ns

    def __call__(self) -> int:
        return self.now_ns

    def advance(self, delta_ns: int) -> int:
        self.now_ns += delta_ns
        return self.now_ns


class CustomMappingWrapper(Mapping):
    """Non-dict mapping wrapper to verify that custom mappings are rejected."""

    def __init__(self, data: dict[str, Any]) -> None:
        self._data = data

    def __getitem__(self, key: str) -> Any:
        return self._data[key]

    def __iter__(self):
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)


def _sha256_hex(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _make_ui_state(
    *,
    kind: str = "godot",
    title: str = "Holdings Desk",
    node_name: str = "Submit Order",
    box: list[int] | None = None,
) -> dict[str, Any]:
    nid = hashlib.sha256(f"{title}:{node_name}".encode("utf-8")).hexdigest()[:16]
    ax_sha = _sha256_hex(f"ax:{title}:{node_name}")
    ax_stable_sha = _sha256_hex(f"ax_stable:{title}:{node_name}")
    return schema.build_state(
        [
            {
                "id": nid,
                "role": "button",
                "name": node_name,
                "depth": 0,
                "ordinal": 0,
                "attrs": [("focusable", "true")],
            }
        ],
        kind=kind,
        url="godot://main/desk",
        title=title,
        ax_sha256=ax_sha,
        ax_stable_sha256=ax_stable_sha,
        prune="prune_ax_v1",
        raw_count=3,
        boxes={nid: box or [10, 20, 100, 40]},
        viewport={"width": 640, "height": 480},
        actionable_roles=frozenset({"button"}),
    )


def _make_frame_meta(
    *,
    seq: int,
    tick: int | None,
    tick_valid: bool = True,
    capture_mono_ns: int = 1_000_000,
    width: int = 640,
    height: int = 480,
    pixel_format: str = "RGB24",
    run_id: str = "run-w8u-01",
    lease_id: str = "lease-0dc7e82e66345908",
    target_handle: dict[str, Any] | None = None,
    epoch: int = 0,
    with_binding: bool = True,
) -> dict[str, Any]:
    handle = target_handle or {"kind": "godot_window", "canonical_id": "win-0", "generation": 1}
    frame_sha = _sha256_hex(f"frame-pixels-seq-{seq}-tick-{tick}")
    binding_sha = (
        wt.compute_frame_binding_sha256(
            run_id=run_id,
            lease_id=lease_id,
            target_handle=handle,
            epoch=epoch,
            seq=seq,
            capture_mono_ns=capture_mono_ns,
            frame_sha256=frame_sha,
        )
        if with_binding
        else None
    )
    return {
        "seq": seq,
        "tick": tick,
        "tick_valid": tick_valid,
        "capture_mono_ns": capture_mono_ns,
        "width": width,
        "height": height,
        "stride": width * 3,
        "pixel_format": pixel_format,
        "sha256": frame_sha,
        "binding_sha256": binding_sha,
    }


def _make_action(
    *,
    action_id: str = "act-1",
    action_seq: int = 1,
    observed_frame_seq: int = 1,
    observed_frame_sha256: str | None = None,
    verb: str = "pointer",
    args: dict[str, Any] | None = None,
    run_id: str = "run-w8u-01",
    lease_id: str = "lease-0dc7e82e66345908",
    epoch: int = 0,
    generation: int = 1,
    deadline_ns: int = 2_000_000_000,
    grant_sha256: str | None = None,
) -> dict[str, Any]:
    return {
        "schema": wt.ACTION_SCHEMA_ID,
        "run_id": run_id,
        "lease_id": lease_id,
        "epoch": epoch,
        "generation": generation,
        "action_id": action_id,
        "action_seq": action_seq,
        "observed_frame_seq": observed_frame_seq,
        "observed_frame_sha256": observed_frame_sha256 or _sha256_hex("frame-pixels-seq-1-tick-0"),
        "deadline_ns": deadline_ns,
        "verb": verb,
        "args": args if args is not None else {"operation": "click", "x": 50, "y": 30, "button": 1},
        "grant_sha256": grant_sha256 or _sha256_hex("grant-01"),
    }


PRODUCER_SCHEMAS = {
    "econ_allocate": {
        "type": "economic",
        "required_args": ("amount", "desk"),
        "arg_types": {"amount": "int", "desk": "str"},
    },
    "inspect_view": {
        "type": "boundary",
        "required_args": ("panel",),
        "arg_types": {"panel": "str"},
    },
}


class TestW8UContractAndConstants(unittest.TestCase):
    """Verify frozen schemas, domains, control wire definitions, and AUSENTE markers."""

    def test_frozen_constants_and_domains(self) -> None:
        self.assertEqual(wt.AUSENTE, "AUSENTE")
        self.assertEqual(wt.TIMING_DOMAIN, "VM")
        self.assertEqual(wt.SPECIFICATION_CLASS, "TARGET")
        self.assertEqual(wt.EXECUTION_WAVE, 1)
        self.assertEqual(wt.GENESIS_DOMAIN, "w8u.genesis/v1")
        self.assertEqual(wt.TRANSITION_CHAIN_DOMAIN, "w8u.transition/v1")
        self.assertNotEqual(wt.GENESIS_DOMAIN, wt.TRANSITION_CHAIN_DOMAIN)
        self.assertIn("genesis", wt.DIGEST_DOMAINS)
        self.assertIn("transition_chain", wt.DIGEST_DOMAINS)
        self.assertIn("ui_state", wt.DIGEST_DOMAINS)
        self.assertIn("t2_trace", wt.DIGEST_DOMAINS)
        self.assertFalse(wt.DIGEST_DOMAINS["ui_state"]["newline_terminated"])
        self.assertTrue(wt.DIGEST_DOMAINS["t2_trace"]["newline_terminated"])
        self.assertEqual(set(wt.CONTROL_KINDS.keys()), {"HELLO", "FRAME", "ACK", "STOP", "END", "EOF"})
        self.assertEqual(set(wt.CONTROL_BODY_SCHEMAS.keys()), set(wt.CONTROL_KINDS.keys()))
        self.assertEqual(wt.END_NUMERIC_ERRORS[0], "OK")
        self.assertEqual(wt.END_NUMERIC_ERRORS[3], "E_CREDIT")
        self.assertEqual(wt.END_NUMERIC_ERRORS[8], "E_PIXEL_LEAK")


class TestW8UFrameEvidenceAndCreditControl(unittest.TestCase):
    """Verify revision-pinned w8u-os-contract, common XWD subset, grab pair equality, and credit rules."""

    def test_revision_pinned_os_contract_positive_and_negative(self) -> None:
        hdr = wt.pack_xwd_header(width=640, height=480)
        good_a = _make_frame_meta(seq=1, tick=0)
        good_a["xwd_header"] = hdr
        good_b = copy.deepcopy(good_a)

        bad_b = copy.deepcopy(good_a)
        bad_b["sha256"] = _sha256_hex("corrupted-pixel-digest")

        evidence = {
            "contract_id": "w8u-os-contract",
            "revision": "96bcde632f9fea0e0d228b7fb02e74b5d4a98ebb",
            "accepted_vectors": [{"grab_a": good_a, "grab_b": good_b}],
            "rejected_vectors": [{"grab_a": good_a, "grab_b": bad_b}],
        }
        validated = wt.validate_injected_os_contract(evidence)
        self.assertEqual(validated["contract_id"], "w8u-os-contract")

        # Must-FAIL: unpinned revision token
        bad_rev = copy.deepcopy(evidence)
        bad_rev["revision"] = "latest"
        with self.assertRaises(wt.W8UTraceError) as ctx:
            wt.validate_injected_os_contract(bad_rev)
        self.assertEqual(ctx.exception.code, wt.E_STATE)

        # Must-FAIL: rejected vector that actually passes
        bad_vec = copy.deepcopy(evidence)
        bad_vec["rejected_vectors"] = [{"grab_a": good_a, "grab_b": good_b}]
        with self.assertRaises(wt.W8UTraceError) as ctx2:
            wt.validate_injected_os_contract(bad_vec)
        self.assertEqual(ctx2.exception.code, wt.E_STATE)

    def test_xwd_common_subset_and_grab_pair_equality(self) -> None:
        hdr = wt.pack_xwd_header(width=320, height=240)
        parsed = wt.validate_xwd_common_subset(hdr)
        self.assertEqual(parsed["pixmap_width"], 320)
        self.assertEqual(parsed["pixmap_height"], 240)
        self.assertEqual(parsed["file_version"], 7)

        # Must-FAIL: non-zero ncolors in XWD header
        bad_dict = dict(parsed)
        bad_dict["ncolors"] = 16
        with self.assertRaises(wt.W8UTraceError) as ctx:
            wt.validate_xwd_common_subset(bad_dict)
        self.assertEqual(ctx.exception.code, wt.E_STATE)

        # Must-FAIL: same-grab pair mismatch
        g1 = _make_frame_meta(seq=1, tick=1)
        g2 = _make_frame_meta(seq=1, tick=1)
        g2["stride"] = 640 * 4
        with self.assertRaises(wt.W8UTraceError) as ctx2:
            wt.verify_same_grab_pair(g1, g2)
        self.assertEqual(ctx2.exception.code, wt.E_STATE)

    def test_credit_controller_capacity_one_exhaustion_duplicate_and_inflation(self) -> None:
        # Capacity = 1: initial ACK seq=0 grants min(2, 1) == 1
        ctrl = wt.FrameCreditController(reserved_capacity=1)
        ctrl.open_stream()
        granted = ctrl.receive_ack(0)
        self.assertEqual(granted, 1)
        self.assertEqual(ctrl.available_credits, 1)

        # Reopen per frame is forbidden
        with self.assertRaises(wt.W8UTraceError) as reopen_ctx:
            ctrl.open_stream()
        self.assertEqual(reopen_ctx.exception.code, wt.E_CREDIT)

        # Publish frame 1 consumes the single credit
        f1 = _make_frame_meta(seq=1, tick=0, capture_mono_ns=100)
        ctrl.publish_frame_pair(f1, copy.deepcopy(f1))
        self.assertEqual(ctrl.available_credits, 0)

        # Credit exhaustion: publishing frame 2 before ACK(seq=1) fails with E_CREDIT
        f2 = _make_frame_meta(seq=2, tick=1, capture_mono_ns=200)
        with self.assertRaises(wt.W8UTraceError) as exh_ctx:
            ctrl.publish_frame_pair(f2, copy.deepcopy(f2))
        self.assertEqual(exh_ctx.exception.code, wt.E_CREDIT)
        self.assertEqual(ctrl.emitted_frames, 1)

        # Duplicate ACK(seq=0) fails with E_CREDIT
        with self.assertRaises(wt.W8UTraceError) as dup0_ctx:
            ctrl.receive_ack(0)
        self.assertEqual(dup0_ctx.exception.code, wt.E_CREDIT)

        # ACK(seq=1) with inflated grant=2 fails with E_CREDIT
        ctrl_infl = wt.FrameCreditController(reserved_capacity=1)
        ctrl_infl.open_stream()
        ctrl_infl.receive_ack(0)
        ctrl_infl.publish_frame_pair(f1, copy.deepcopy(f1))
        with self.assertRaises(wt.W8UTraceError) as infl_ctx:
            ctrl_infl.receive_ack(1, grant=2)
        self.assertEqual(infl_ctx.exception.code, wt.E_CREDIT)

        # Valid ACK(seq=1) grants 1 credit; duplicate ACK(seq=1) fails with E_CREDIT
        self.assertEqual(ctrl.receive_ack(1), 1)
        self.assertEqual(ctrl.available_credits, 1)
        with self.assertRaises(wt.W8UTraceError) as dup1_ctx:
            ctrl.receive_ack(1)
        self.assertEqual(dup1_ctx.exception.code, wt.E_CREDIT)

        # Overgrant on initial ACK(seq=0) when capacity=1 and grant=2
        ctrl_over = wt.FrameCreditController(reserved_capacity=1)
        ctrl_over.open_stream()
        with self.assertRaises(wt.W8UTraceError) as over_ctx:
            ctrl_over.receive_ack(0, grant=2)
        self.assertEqual(over_ctx.exception.code, wt.E_CREDIT)

        # Mandatory STOP -> END -> EOF -> join_reader and stale publication rejection
        ctrl.publish_frame_pair(f2, copy.deepcopy(f2))
        ctrl.send_stop()
        f3 = _make_frame_meta(seq=3, tick=2, capture_mono_ns=300)
        with self.assertRaises(wt.W8UTraceError) as stale_ctx:
            ctrl.publish_frame_pair(f3, copy.deepcopy(f3))
        self.assertEqual(stale_ctx.exception.code, wt.E_STATE)

        # Assert quiescent fails before END/EOF/join
        with self.assertRaises(wt.W8UTraceError) as q_ctx:
            ctrl.assert_quiescent()
        self.assertEqual(q_ctx.exception.code, wt.E_STATE)

        ctrl.receive_end(0)
        ctrl.receive_eof()
        ctrl.join_reader(0)
        ctrl.assert_quiescent()


class TestW8UActionContractAndDispatcher(unittest.TestCase):
    """Verify w8u.action/v1 validation, verbs, replay idempotency, and lost-ack quarantine."""

    def setUp(self) -> None:
        self.clock = FakeClock(1_000_000_000)
        self.frame_sha = _sha256_hex("frame-pixels-seq-1-tick-0")
        self.grant_sha = _sha256_hex("grant-01")
        self.dispatcher = wt.ActionGateDispatcher(
            run_id="run-w8u-01",
            lease_id="lease-0dc7e82e66345908",
            epoch=0,
            generation=1,
            grant_sha256=self.grant_sha,
            screen_width=640,
            screen_height=480,
            clock=self.clock,
            actuator=lambda act: {"status": "applied", "applied_tick": 1, "outcome_sha256": _sha256_hex("out-1")},
        )
        self.dispatcher.update_observed_frame(1, self.frame_sha)

    def test_all_valid_action_verbs_positive(self) -> None:
        verbs_and_args = [
            ("pointer", {"operation": "move", "x": 0, "y": 0}),
            ("pointer", {"operation": "click", "x": 639, "y": 479, "button": 3}),
            ("pointer", {"operation": "scroll", "x": 100, "y": 100, "dx": 0, "dy": -120}),
            ("key", {"operation": "press", "key": "Enter", "modifiers": ["Shift", "Control", "Alt", "Super"]}),
            ("text", {"mode": "type", "text": "Hello W8U"}),
            ("wait", {"duration_ms": 500}),
        ]
        for idx, (verb, args) in enumerate(verbs_and_args, start=1):
            act = _make_action(
                action_id=f"act-{idx}",
                action_seq=idx,
                observed_frame_seq=1,
                observed_frame_sha256=self.frame_sha,
                verb=verb,
                args=args,
                grant_sha256=self.grant_sha,
            )
            resp, rc = self.dispatcher.dispatch(schema.dumps(act).encode("utf-8"))
            self.assertEqual(rc, wt.RC_OK)
            self.assertEqual(resp["status"], "applied")
            self.assertEqual(resp["code"], wt.OK_ADMITTED)
        self.assertEqual(len(self.dispatcher.actuator_calls), len(verbs_and_args))

    def test_pre_actuator_rejections_zero_actuator_calls(self) -> None:
        oversized = b'{"schema":"w8u.action/v1","pad":"' + (b"a" * 65536) + b'"}'
        resp, rc = self.dispatcher.dispatch(oversized)
        self.assertEqual(rc, wt.RC_SCHEMA)
        self.assertEqual(resp["code"], wt.E_SCHEMA)

        base_act = _make_action()
        raw_dup = schema.dumps(base_act)[:-1] + ',"verb":"wait"}'
        resp, rc = self.dispatcher.dispatch(raw_dup)
        self.assertEqual(rc, wt.RC_SCHEMA)
        self.assertEqual(resp["code"], wt.E_SCHEMA)

        bad_bool = _make_action(args={"operation": "move", "x": True, "y": 10})
        resp, rc = self.dispatcher.dispatch(bad_bool)
        self.assertEqual(rc, wt.RC_SCHEMA)
        self.assertEqual(resp["code"], wt.E_SCHEMA)

        bad_float = (
            '{"schema":"w8u.action/v1","run_id":"run-w8u-01","lease_id":"lease-0dc7e82e66345908",'
            '"epoch":0,"generation":1,"action_id":"a1","action_seq":1,"observed_frame_seq":1,'
            '"observed_frame_sha256":"' + self.frame_sha + '","deadline_ns":2000000000,'
            '"verb":"pointer","args":{"operation":"move","x":1.5,"y":2},'
            '"grant_sha256":"' + self.grant_sha + '"}'
        )
        resp, rc = self.dispatcher.dispatch(bad_float)
        self.assertEqual(rc, wt.RC_SCHEMA)

        for bad_args in (
            {"operation": "move", "x": 640, "y": 10},
            {"operation": "click", "x": 10, "y": 10, "button": 4},
            {"operation": "scroll", "x": 10, "y": 10, "dx": 0, "dy": 0},
            {"operation": "scroll", "x": 10, "y": 10, "dx": 121, "dy": 0},
        ):
            resp, rc = self.dispatcher.dispatch(_make_action(verb="pointer", args=bad_args))
            self.assertEqual(rc, wt.RC_SCHEMA)

        for bad_key_args in (
            {"operation": "press", "key": "Enter", "modifiers": ["Control", "Shift"]},
            {"operation": "press", "key": "Enter", "modifiers": ["Shift", "Shift"]},
            {"operation": "press", "key": "F12", "modifiers": []},
        ):
            resp, rc = self.dispatcher.dispatch(_make_action(verb="key", args=bad_key_args))
            self.assertEqual(rc, wt.RC_SCHEMA)

        for bad_text in ("hello\x00world", "é" * 2049):
            resp, rc = self.dispatcher.dispatch(_make_action(verb="text", args={"mode": "type", "text": bad_text}))
            self.assertEqual(rc, wt.RC_SCHEMA)

        resp, rc = self.dispatcher.dispatch(b"\xff\xfeinvalid")
        self.assertEqual(rc, wt.RC_SCHEMA)
        resp, rc = self.dispatcher.dispatch(schema.dumps(base_act) + "\n{}")
        self.assertEqual(rc, wt.RC_SCHEMA)

        self.assertEqual(len(self.dispatcher.actuator_calls), 0)

    def test_focus_deadline_stale_frame_replay_and_quarantine(self) -> None:
        self.dispatcher.editable_focus = False
        resp, rc = self.dispatcher.dispatch(_make_action(verb="text", args={"mode": "type", "text": "abc"}))
        self.assertEqual(rc, wt.RC_REFUSED)
        self.assertEqual(resp["code"], wt.E_FOCUS)
        self.assertEqual(len(self.dispatcher.actuator_calls), 0)
        self.dispatcher.editable_focus = True

        resp, rc = self.dispatcher.dispatch(
            _make_action(verb="wait", args={"duration_ms": 2000}, deadline_ns=1_000_000_000 + 500_000_000)
        )
        self.assertEqual(rc, wt.RC_REFUSED)
        self.assertEqual(resp["code"], wt.E_DEADLINE)
        self.assertEqual(len(self.dispatcher.actuator_calls), 0)

        resp, rc = self.dispatcher.dispatch(_make_action(observed_frame_seq=0))
        self.assertEqual(rc, wt.RC_REFUSED)
        self.assertEqual(resp["code"], wt.E_STALE_FRAME)
        self.assertEqual(len(self.dispatcher.actuator_calls), 0)

        valid_act = _make_action(action_id="act-1", action_seq=1)
        resp1, rc1 = self.dispatcher.dispatch(valid_act)
        resp2, rc2 = self.dispatcher.dispatch(copy.deepcopy(valid_act))
        self.assertEqual(rc1, wt.RC_OK)
        self.assertEqual(resp1, resp2)
        self.assertEqual(len(self.dispatcher.actuator_calls), 1)

        changed_act = _make_action(action_id="act-1", action_seq=1, args={"operation": "move", "x": 99, "y": 99})
        resp_chg, rc_chg = self.dispatcher.dispatch(changed_act)
        self.assertEqual(rc_chg, wt.RC_REFUSED)
        self.assertEqual(resp_chg["status"], "refused")
        self.assertEqual(len(self.dispatcher.actuator_calls), 1)

        self.dispatcher.actuator = lambda act: {"lost_ack": True}
        act2 = _make_action(action_id="act-2", action_seq=2)
        resp_unc, rc_unc = self.dispatcher.dispatch(act2)
        self.assertEqual(rc_unc, wt.RC_UNCERTAIN)
        self.assertEqual(resp_unc["status"], "uncertain")
        self.assertEqual(resp_unc["code"], wt.E_UNCERTAIN)
        self.assertTrue(self.dispatcher.quarantined)
        self.assertEqual(len(self.dispatcher.actuator_calls), 2)

        act3_retry = _make_action(action_id="act-3-retry", action_seq=3)
        resp_q, rc_q = self.dispatcher.dispatch(act3_retry)
        self.assertEqual(rc_q, wt.RC_REFUSED)
        self.assertEqual(resp_q["code"], wt.E_LOCK)
        self.assertEqual(len(self.dispatcher.actuator_calls), 2)


class TestW8UStateAndObservationEnvelope(unittest.TestCase):
    """Verify STATE contract, separate Observation envelope, and frame metadata rules."""

    def test_state_and_observation_envelope_validation(self) -> None:
        state = _make_ui_state()
        digest = wt.compute_state_sha256(state)
        self.assertEqual(len(digest), 64)

        reg = session_registry.SessionRegistry(clock=FakeClock())
        handle = reg.register("godot_window", "win-0", object(), owned=True)
        rgb = np.zeros((4, 4, 3), dtype=np.uint8)
        fref = reg.admit_frame(handle, 0, rgb)
        obs = reg.admit_observation(handle, fref, state=state, dpr_ppm=1_000_000)

        validated_env = wt.validate_observation_envelope(obs, expected_epoch=0)
        self.assertEqual(validated_env["frame_seq"], 0)

        with self.assertRaises(wt.W8UTraceError) as ctx:
            wt.validate_state_strict(
                {
                    "handle": {"kind": "godot_window", "canonical_id": "win-0", "generation": 1},
                    "epoch": 0,
                    "frame_seq": 0,
                    "dpr_ppm": 1_000_000,
                    "state": state,
                }
            )
        self.assertEqual(ctx.exception.code, wt.E_STATE)

        wrapped = copy.deepcopy(state)
        wrapped["_perception"] = {"extra": 1}
        with self.assertRaises(wt.W8UTraceError) as ctx2:
            wt.validate_state_strict(wrapped)
        self.assertEqual(ctx2.exception.code, wt.E_STATE)

        bad_frame = _make_frame_meta(seq=1, tick=0, tick_valid=False)
        with self.assertRaises(wt.W8UTraceError) as ctx3:
            wt.validate_frame_metadata(bad_frame)
        self.assertEqual(ctx3.exception.code, wt.E_STATE)

        good_unwitnessed = _make_frame_meta(seq=1, tick=None, tick_valid=False, with_binding=False)
        validated_meta = wt.validate_frame_metadata(good_unwitnessed)
        self.assertIsNone(validated_meta["tick"])
        self.assertIsNone(validated_meta["binding_sha256"])


class TestW8UTraceRecorderAndVerifier(unittest.TestCase):
    """End-to-end positive and must-FAIL tests for W8UTraceRecorder and verify_w8u_trace."""

    def _build_two_step_t2_run(self) -> dict[str, Any]:
        handle = {"kind": "godot_window", "canonical_id": "win-0", "generation": 1}
        sim0 = _sha256_hex("sim-state-tick-0")
        sim1 = _sha256_hex("sim-state-tick-1")

        st0 = _make_ui_state(node_name="Tick 0 Ready")
        st1 = _make_ui_state(node_name="Tick 1 Allocated")
        st2 = _make_ui_state(node_name="Tick 1 Rejected Modal")

        f1 = _make_frame_meta(seq=1, tick=0, capture_mono_ns=100, target_handle=handle)
        f2 = _make_frame_meta(seq=2, tick=1, capture_mono_ns=200, target_handle=handle)
        f3 = _make_frame_meta(seq=3, tick=1, capture_mono_ns=300, target_handle=handle)

        sink = wt.ApprovedMetadataSink()
        rec = wt.W8UTraceRecorder(
            run_id="run-w8u-01",
            lease_id="lease-0dc7e82e66345908",
            target_handle=handle,
            epoch=0,
            session_id="session-main",
            t2_enabled=True,
            producer_schemas=PRODUCER_SCHEMAS,
            initial_simulation_sha256=sim0,
            reserved_capacity=2,
            sink=sink,
        )

        rec.credit_controller.open_stream()
        rec.credit_controller.receive_ack(0)
        rec.credit_controller.publish_frame_pair(f1, copy.deepcopy(f1))
        rec.credit_controller.receive_ack(1)
        rec.credit_controller.publish_frame_pair(f2, copy.deepcopy(f2))
        rec.credit_controller.receive_ack(2)
        rec.credit_controller.publish_frame_pair(f3, copy.deepcopy(f3))
        rec.credit_controller.receive_ack(3)

        act1 = _make_action(
            action_id="act-1",
            action_seq=1,
            observed_frame_seq=1,
            observed_frame_sha256=f1["sha256"],
        )
        resp1 = {
            "schema": wt.ACTION_RESPONSE_SCHEMA_ID,
            "action_id": "act-1",
            "action_seq": 1,
            "lease_id": "lease-0dc7e82e66345908",
            "epoch": 0,
            "status": "applied",
            "code": wt.OK_ADMITTED,
            "emitted": True,
            "result_known": True,
            "applied_tick": 1,
            "outcome_sha256": _sha256_hex("outcome-1"),
        }
        cmd1 = {"tick": 1, "seq": 1, "cmd": "econ_allocate", "args": {"amount": 500, "desk": "alpha"}}
        out1 = {
            "seq": 1,
            "requested_tick": 1,
            "applied_tick": 1,
            "order_seq_id": 10,
            "status": "accepted",
            "reason": None,
            "state_sha256": sim1,
        }
        rec.record_step(
            row_seq=1,
            action=act1,
            action_response=resp1,
            pre_state=st0,
            post_state=st1,
            pre_frame=f1,
            post_frame=f2,
            screen_width=640,
            screen_height=480,
            t2_command=cmd1,
            t2_outcome=out1,
            simulation_pre_sha256=sim0,
            simulation_post_sha256=sim1,
            session_id="session-main",
            presentation_id="pres-1",
        )

        act2 = _make_action(
            action_id="act-2",
            action_seq=2,
            observed_frame_seq=2,
            observed_frame_sha256=f2["sha256"],
        )
        resp2 = {
            "schema": wt.ACTION_RESPONSE_SCHEMA_ID,
            "action_id": "act-2",
            "action_seq": 2,
            "lease_id": "lease-0dc7e82e66345908",
            "epoch": 0,
            "status": "admitted",
            "code": wt.OK_ADMITTED,
            "emitted": True,
            "result_known": True,
            "applied_tick": None,
            "outcome_sha256": None,
        }
        cmd2 = {"tick": 2, "seq": 2, "cmd": "econ_allocate", "args": {"amount": 999999, "desk": "alpha"}}
        out2 = {
            "seq": 2,
            "requested_tick": 2,
            "applied_tick": 1,
            "order_seq_id": 11,
            "status": "rejected",
            "reason": "INSUFFICIENT_FUNDS",
            "state_sha256": sim1,
        }
        rec.record_step(
            row_seq=2,
            action=act2,
            action_response=resp2,
            pre_state=st1,
            post_state=st2,
            pre_frame=f2,
            post_frame=f3,
            screen_width=640,
            screen_height=480,
            t2_command=cmd2,
            t2_outcome=out2,
            simulation_pre_sha256=sim1,
            simulation_post_sha256=sim1,
            session_id="session-main",
            presentation_id="pres-2",
        )

        rec.credit_controller.send_stop()
        rec.credit_controller.receive_end(0)
        rec.credit_controller.receive_eof()
        rec.credit_controller.join_reader(0)
        receipt = rec.finalize_receipt()

        return {
            "recorder": rec,
            "receipt": receipt,
            "run_descriptor": copy.deepcopy(rec.run_descriptor),
            "transitions": copy.deepcopy(rec.transitions),
            "actions": copy.deepcopy(rec.actions),
            "action_responses": copy.deepcopy(rec.action_responses),
            "ui_states": [st0, st1, st2],
            "t2_jsonl_stream": b"".join(rec.t2_jsonl_lines),
            "initial_simulation_sha256": sim0,
            "sink": sink,
        }

    def test_positive_t2_trace_and_independent_recomputation(self) -> None:
        with wt.DiskSinkSpy() as spy:
            bundle = self._build_two_step_t2_run()
            res = wt.verify_w8u_trace(
                run_descriptor=bundle["run_descriptor"],
                receipt=bundle["receipt"],
                transitions=bundle["transitions"],
                actions=bundle["actions"],
                action_responses=bundle["action_responses"],
                ui_states=bundle["ui_states"],
                screen_width=640,
                screen_height=480,
                t2_jsonl_stream=bundle["t2_jsonl_stream"],
                producer_schemas=PRODUCER_SCHEMAS,
                initial_simulation_sha256=bundle["initial_simulation_sha256"],
                expected_row_count=2,
                claim_economic_success=True,
            )
            self.assertEqual(spy.write_events, [])

        self.assertTrue(res["ok"])
        self.assertEqual(res["verdict"], "ACCEPT")
        self.assertEqual(res["rows_verified"], 2)
        self.assertEqual(res["accepted_t2_count"], 1)
        self.assertEqual(res["rejected_t2_count"], 1)
        self.assertTrue(res["economic_success"])
        self.assertEqual(res["timing_domain"], "VM")
        self.assertEqual(res["host_measurements"], wt.AUSENTE)
        self.assertEqual(len(bundle["sink"].records), 2)

        r = bundle["receipt"]
        digests = {
            r["action_log_sha256"],
            r["t2_trace_sha256"],
            r["t2_terminal_sha256"],
            r["final_state_sha256"],
            r["simulation_state_sha256"],
            r["first_frame"]["sha256"],
            res["genesis_sha256"],
            res["terminal_chain_sha256"],
        }
        self.assertEqual(len(digests), 8)

    def test_positive_non_t2_policy_retains_null_and_zero_frame_indeterminate(self) -> None:
        handle = {"kind": "cdp_tab", "canonical_id": "tab-1", "generation": 1}
        st0 = _make_ui_state(kind="cdp", node_name="Start")
        st1 = _make_ui_state(kind="cdp", node_name="Clicked")

        rec = wt.W8UTraceRecorder(
            run_id="run-non-t2-01",
            lease_id="lease-0dc7e82e66345908",
            target_handle=handle,
            epoch=0,
            session_id="session-cdp",
            t2_enabled=False,
            pixel_required=True,
        )
        rec.credit_controller.open_stream()
        rec.credit_controller.receive_ack(0)

        act1 = _make_action(run_id="run-non-t2-01", action_id="act-1", action_seq=1)
        resp1 = {
            "schema": wt.ACTION_RESPONSE_SCHEMA_ID,
            "action_id": "act-1",
            "action_seq": 1,
            "lease_id": "lease-0dc7e82e66345908",
            "epoch": 0,
            "status": "admitted",
            "code": wt.OK_ADMITTED,
            "emitted": True,
            "result_known": True,
            "applied_tick": None,
            "outcome_sha256": None,
        }
        tr = rec.record_step(
            row_seq=1,
            action=act1,
            action_response=resp1,
            pre_state=st0,
            post_state=st1,
            pre_frame=None,
            post_frame=None,
            screen_width=640,
            screen_height=480,
        )
        self.assertIsNone(tr["t2_outcome"])
        self.assertIsNone(tr["simulation_pre_sha256"])
        self.assertIsNone(tr["simulation_post_sha256"])

        rec.credit_controller.send_stop()
        rec.credit_controller.receive_end(0)
        rec.credit_controller.receive_eof()
        rec.credit_controller.join_reader(0)
        receipt = rec.finalize_receipt()

        self.assertIsNone(receipt["t2_trace_sha256"])
        self.assertIsNone(receipt["t2_terminal_sha256"])
        self.assertIsNone(receipt["t2_ticks"])
        self.assertIsNone(receipt["simulation_state_sha256"])
        self.assertIsNone(receipt["first_frame"])
        self.assertIsNone(receipt["last_frame"])
        self.assertEqual(receipt["ui_gate_verdict"], "INDETERMINATE")

        res = wt.verify_w8u_trace(
            run_descriptor=rec.run_descriptor,
            receipt=receipt,
            transitions=rec.transitions,
            actions=rec.actions,
            action_responses=rec.action_responses,
            ui_states=[st0, st1],
            screen_width=640,
            screen_height=480,
            t2_jsonl_stream=None,
            expected_row_count=1,
            claim_economic_success=False,
            pixel_required=True,
        )
        self.assertTrue(res["ok"])
        self.assertEqual(res["verdict"], "INDETERMINATE")
        self.assertFalse(res["economic_success"])

    def test_must_fail_missing_reordered_conflicting_rows_and_pre_mutation_limits(self) -> None:
        bundle = self._build_two_step_t2_run()

        with self.assertRaises(wt.W8UTraceError) as ctx_missing:
            wt.verify_w8u_trace(
                run_descriptor=bundle["run_descriptor"],
                receipt=bundle["receipt"],
                transitions=bundle["transitions"][:1],
                actions=bundle["actions"][:1],
                action_responses=bundle["action_responses"][:1],
                ui_states=bundle["ui_states"],
                screen_width=640,
                screen_height=480,
                t2_jsonl_stream=bundle["t2_jsonl_stream"],
                producer_schemas=PRODUCER_SCHEMAS,
                initial_simulation_sha256=bundle["initial_simulation_sha256"],
                expected_row_count=2,
            )
        self.assertEqual(ctx_missing.exception.code, wt.E_JOIN)

        with self.assertRaises(wt.W8UTraceError) as ctx_reord:
            wt.verify_w8u_trace(
                run_descriptor=bundle["run_descriptor"],
                receipt=bundle["receipt"],
                transitions=list(reversed(bundle["transitions"])),
                actions=bundle["actions"],
                action_responses=bundle["action_responses"],
                ui_states=bundle["ui_states"],
                screen_width=640,
                screen_height=480,
                t2_jsonl_stream=bundle["t2_jsonl_stream"],
                producer_schemas=PRODUCER_SCHEMAS,
                initial_simulation_sha256=bundle["initial_simulation_sha256"],
            )
        self.assertEqual(ctx_reord.exception.code, wt.E_JOIN)

        dup_rows = [bundle["transitions"][0], copy.deepcopy(bundle["transitions"][0])]
        with self.assertRaises(wt.W8UTraceError) as ctx_conf:
            wt.verify_w8u_trace(
                run_descriptor=bundle["run_descriptor"],
                receipt=bundle["receipt"],
                transitions=dup_rows,
                actions=bundle["actions"],
                action_responses=bundle["action_responses"],
                ui_states=bundle["ui_states"],
                screen_width=640,
                screen_height=480,
                t2_jsonl_stream=bundle["t2_jsonl_stream"],
                producer_schemas=PRODUCER_SCHEMAS,
                initial_simulation_sha256=bundle["initial_simulation_sha256"],
            )
        self.assertEqual(ctx_conf.exception.code, wt.E_JOIN)

        rec = wt.W8UTraceRecorder(
            run_id="run-lim-01",
            lease_id="lease-0dc7e82e66345908",
            target_handle={"kind": "cdp_tab", "canonical_id": "tab-1", "generation": 1},
            t2_enabled=False,
            limits=wt.TraceLimits(max_transitions=1),
        )
        st0 = _make_ui_state(kind="cdp", node_name="A")
        st1 = _make_ui_state(kind="cdp", node_name="B")
        act1 = _make_action(run_id="run-lim-01", action_id="act-1", action_seq=1)
        resp1 = {
            "schema": wt.ACTION_RESPONSE_SCHEMA_ID,
            "action_id": "act-1",
            "action_seq": 1,
            "lease_id": "lease-0dc7e82e66345908",
            "epoch": 0,
            "status": "admitted",
            "code": wt.OK_ADMITTED,
            "emitted": True,
            "result_known": True,
            "applied_tick": None,
            "outcome_sha256": None,
        }

        dup_pairs = wt.RawKeyValuePairs(tuple(list(act1.items()) + [("verb", "wait")]))
        with self.assertRaises(wt.W8UTraceError) as dup_ctx:
            rec.record_step(
                row_seq=1,
                action=dup_pairs,
                action_response=resp1,
                pre_state=st0,
                post_state=st1,
                pre_frame=None,
                post_frame=None,
                screen_width=640,
                screen_height=480,
            )
        self.assertEqual(dup_ctx.exception.code, wt.E_SCHEMA)
        self.assertEqual(len(rec.transitions), 0)

        with self.assertRaises(wt.W8UTraceError) as map_ctx:
            rec.record_step(
                row_seq=1,
                action=CustomMappingWrapper(act1),
                action_response=resp1,
                pre_state=st0,
                post_state=st1,
                pre_frame=None,
                post_frame=None,
                screen_width=640,
                screen_height=480,
            )
        self.assertEqual(map_ctx.exception.code, wt.E_SCHEMA)
        self.assertEqual(len(rec.transitions), 0)

        rec.record_step(
            row_seq=1,
            action=act1,
            action_response=resp1,
            pre_state=st0,
            post_state=st1,
            pre_frame=None,
            post_frame=None,
            screen_width=640,
            screen_height=480,
        )
        self.assertEqual(len(rec.transitions), 1)
        with self.assertRaises(wt.W8UTraceError) as lim_ctx:
            rec.record_step(
                row_seq=2,
                action=_make_action(run_id="run-lim-01", action_id="act-2", action_seq=2),
                action_response=resp1,
                pre_state=st1,
                post_state=st0,
                pre_frame=None,
                post_frame=None,
                screen_width=640,
                screen_height=480,
            )
        self.assertEqual(lim_ctx.exception.code, wt.E_SCHEMA)
        self.assertEqual(len(rec.transitions), 1)

    def test_must_fail_wrong_session_wrong_tick_stale_basis_and_chain_tamper(self) -> None:
        bundle = self._build_two_step_t2_run()
        rec: wt.W8UTraceRecorder = bundle["recorder"]

        handle = {"kind": "godot_window", "canonical_id": "win-0", "generation": 1}
        sim0 = _sha256_hex("sim-state-tick-0")
        sim1 = _sha256_hex("sim-state-tick-1")
        st0 = bundle["ui_states"][0]
        st1 = bundle["ui_states"][1]
        f1 = _make_frame_meta(seq=1, tick=0, capture_mono_ns=100, target_handle=handle)
        f2 = _make_frame_meta(seq=2, tick=1, capture_mono_ns=200, target_handle=handle)
        act1 = bundle["actions"][0]
        resp1 = bundle["action_responses"][0]
        cmd1 = {"tick": 1, "seq": 1, "cmd": "econ_allocate", "args": {"amount": 500, "desk": "alpha"}}
        out1 = bundle["transitions"][0]["t2_outcome"]

        fresh = wt.W8UTraceRecorder(
            run_id="run-w8u-01",
            lease_id="lease-0dc7e82e66345908",
            target_handle=handle,
            epoch=0,
            session_id="session-main",
            t2_enabled=True,
            producer_schemas=PRODUCER_SCHEMAS,
            initial_simulation_sha256=sim0,
        )

        with self.assertRaises(wt.W8UTraceError) as sess_ctx:
            fresh.record_step(
                row_seq=1,
                action=act1,
                action_response=resp1,
                pre_state=st0,
                post_state=st1,
                pre_frame=f1,
                post_frame=f2,
                screen_width=640,
                screen_height=480,
                t2_command=cmd1,
                t2_outcome=out1,
                simulation_pre_sha256=sim0,
                simulation_post_sha256=sim1,
                session_id="wrong-session",
            )
        self.assertEqual(sess_ctx.exception.code, wt.E_JOIN)
        self.assertEqual(len(fresh.transitions), 0)

        bad_cmd = dict(cmd1, tick=5)
        bad_out = dict(out1, requested_tick=5, applied_tick=5)
        with self.assertRaises(wt.W8UTraceError) as tick_ctx:
            fresh.record_step(
                row_seq=1,
                action=act1,
                action_response=resp1,
                pre_state=st0,
                post_state=st1,
                pre_frame=f1,
                post_frame=f2,
                screen_width=640,
                screen_height=480,
                t2_command=bad_cmd,
                t2_outcome=bad_out,
                simulation_pre_sha256=sim0,
                simulation_post_sha256=sim1,
            )
        self.assertEqual(tick_ctx.exception.code, wt.E_JOIN)
        self.assertEqual(len(fresh.transitions), 0)

        with self.assertRaises(wt.W8UTraceError) as basis_ctx:
            fresh.record_step(
                row_seq=1,
                action=act1,
                action_response=resp1,
                pre_state=st0,
                post_state=st1,
                pre_frame=f1,
                post_frame=f2,
                screen_width=640,
                screen_height=480,
                t2_command=cmd1,
                t2_outcome=out1,
                simulation_pre_sha256=_sha256_hex("stale-sim-basis"),
                simulation_post_sha256=sim1,
            )
        self.assertEqual(basis_ctx.exception.code, wt.E_JOIN)
        self.assertEqual(len(fresh.transitions), 0)

        tampered_transitions = copy.deepcopy(bundle["transitions"])
        tampered_transitions[0]["chain_sha256"] = _sha256_hex("tampered-chain")
        with self.assertRaises(wt.W8UTraceError) as chain_ctx:
            wt.verify_w8u_trace(
                run_descriptor=bundle["run_descriptor"],
                receipt=bundle["receipt"],
                transitions=tampered_transitions,
                actions=bundle["actions"],
                action_responses=bundle["action_responses"],
                ui_states=bundle["ui_states"],
                screen_width=640,
                screen_height=480,
                t2_jsonl_stream=bundle["t2_jsonl_stream"],
                producer_schemas=PRODUCER_SCHEMAS,
                initial_simulation_sha256=bundle["initial_simulation_sha256"],
            )
        self.assertEqual(chain_ctx.exception.code, wt.E_CHAIN)
        self.assertIsNotNone(rec)

    def test_must_fail_newline_domain_confusion_simulation_hash_as_ui_and_admitted_only_econ(self) -> None:
        bundle = self._build_two_step_t2_run()

        st0_with_lf_sha = hashlib.sha256((schema.dumps(bundle["ui_states"][0]) + "\n").encode("utf-8")).hexdigest()
        bad_tr_nl = copy.deepcopy(bundle["transitions"])
        bad_tr_nl[0]["pre_ui_state_sha256"] = st0_with_lf_sha
        bad_tr_nl[0]["chain_sha256"] = wt.compute_transition_chain_sha256(
            bad_tr_nl[0], bad_tr_nl[0]["previous_chain_sha256"]
        )
        bad_tr_nl[1]["previous_chain_sha256"] = bad_tr_nl[0]["chain_sha256"]
        bad_tr_nl[1]["chain_sha256"] = wt.compute_transition_chain_sha256(
            bad_tr_nl[1], bad_tr_nl[1]["previous_chain_sha256"]
        )
        with self.assertRaises(wt.W8UTraceError) as nl_state_ctx:
            wt.verify_w8u_trace(
                run_descriptor=bundle["run_descriptor"],
                receipt=bundle["receipt"],
                transitions=bad_tr_nl,
                actions=bundle["actions"],
                action_responses=bundle["action_responses"],
                ui_states=bundle["ui_states"],
                screen_width=640,
                screen_height=480,
                t2_jsonl_stream=bundle["t2_jsonl_stream"],
                producer_schemas=PRODUCER_SCHEMAS,
                initial_simulation_sha256=bundle["initial_simulation_sha256"],
            )
        self.assertEqual(nl_state_ctx.exception.code, wt.E_STATE)

        with self.assertRaises(wt.W8UTraceError) as nl_t2_ctx:
            wt.compute_t2_trace_sha256(bundle["t2_jsonl_stream"].rstrip(b"\n"))
        self.assertEqual(nl_t2_ctx.exception.code, wt.E_T2)

        bad_tr_sim_ui = copy.deepcopy(bundle["transitions"])
        bad_tr_sim_ui[0]["post_ui_state_sha256"] = bad_tr_sim_ui[0]["simulation_post_sha256"]
        with self.assertRaises(wt.W8UTraceError) as sim_ui_ctx:
            wt.validate_transition_record(bad_tr_sim_ui[0], t2_enabled=True)
        self.assertEqual(sim_ui_ctx.exception.code, wt.E_STATE)

        bad_responses = copy.deepcopy(bundle["action_responses"])
        bad_responses[0]["status"] = "admitted"
        bad_responses[0]["applied_tick"] = None
        bad_responses[0]["outcome_sha256"] = None
        with self.assertRaises(wt.W8UTraceError) as econ_ctx:
            wt.verify_w8u_trace(
                run_descriptor=bundle["run_descriptor"],
                receipt=bundle["receipt"],
                transitions=bundle["transitions"],
                actions=bundle["actions"],
                action_responses=bad_responses,
                ui_states=bundle["ui_states"],
                screen_width=640,
                screen_height=480,
                t2_jsonl_stream=bundle["t2_jsonl_stream"],
                producer_schemas=PRODUCER_SCHEMAS,
                initial_simulation_sha256=bundle["initial_simulation_sha256"],
                claim_economic_success=True,
            )
        self.assertEqual(econ_ctx.exception.code, wt.E_T2)

    def test_must_fail_nested_encoded_compressed_pixels_sinks_and_disk_spy(self) -> None:
        leak_samples = [
            {"nested": {"rgb": [0, 1, 2]}},
            {"nested": {"note": "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAAB"}},
            {"nested": {"blob": "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJ"}},
            {"nested": {"gz": "H4sIAAAAAAAAA+3BMQEAAADCoPVPbQwfoAAAAAAAAAAAAAAAAAAAAIC3AYbSVKsAAA=="}},
            {"nested": {"zlib": "eJwrSS0u0SupKAEADtkDtw=="}},
            {"nested": {"ppm": "P6\n2 2\n255\n"}},
            {"nested": {"hex_png": "89504e470d0a1a0a0000000d49484452"}},
            {"nested": {"scanline": [[255, 0, 128], [10, 20, 30]]}},
            {"nested": {"raw_bytes": b"\x00\x01\x02"}},
            {"nested": {"ndarray": np.zeros((2, 2, 3), dtype=np.uint8)}},
        ]
        for sample in leak_samples:
            with self.subTest(sample=sample):
                with self.assertRaises(wt.W8UTraceError) as ctx:
                    wt.assert_metadata_only(sample)
                self.assertEqual(ctx.exception.code, wt.E_PIXEL_LEAK)

        with self.assertRaises(wt.W8UTraceError) as sink_ctx:
            wt.verify_approved_sink(lambda x: None)
        self.assertEqual(sink_ctx.exception.code, wt.E_PIXEL_LEAK)

        with self.assertRaises(wt.W8UTraceError) as path_sink_ctx:
            wt.verify_approved_sink(pathlib.Path("/tmp/unapproved_trace.jsonl"))
        self.assertEqual(path_sink_ctx.exception.code, wt.E_PIXEL_LEAK)

        with wt.DiskSinkSpy() as spy:
            with self.assertRaises(wt.W8UTraceError) as spy_ctx:
                pathlib.Path("/tmp/forbidden_frame_dump.raw").write_bytes(b"pixel_bytes")
            self.assertEqual(spy_ctx.exception.code, wt.E_PIXEL_LEAK)
            self.assertEqual(len(spy.write_events), 1)


if __name__ == "__main__":
    unittest.main()
