"""Unit and acceptance gate tests for w8u-wa-electron (wender.perceive.w8u_electron).

Runs positive and must-FAIL controls in a single unittest invocation offline in RAM
with a DiskSinkSpy, deterministic fake clocks, and independent byte/digest builders.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import unittest

import numpy as np

from wender.perceive import cdp_pipe, w8u_electron as w8u
from wender.perceive.session_registry import (
    Observation,
    SessionRegistry,
)
from wender.state import schema


class FakeClockNs:
    """Deterministic monotonic clock in nanoseconds."""

    def __init__(self, initial_ns: int = 1_000_000_000) -> None:
        self.now_ns = initial_ns

    def __call__(self) -> int:
        return self.now_ns

    def advance_ms(self, ms: int) -> int:
        self.now_ns += ms * 1_000_000
        return self.now_ns


def _independent_sha256_json(obj: dict) -> str:
    raw = json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    assert "\n" not in raw
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _sample_ax_tree() -> list[dict]:
    return [
        {
            "nodeId": "n-root",
            "ignored": False,
            "role": {"value": "RootWebArea"},
            "name": {"value": "Electron Capsule App"},
            "childIds": ["n-main"],
            "backendDOMNodeId": 1,
        },
        {
            "nodeId": "n-main",
            "ignored": False,
            "role": {"value": "main"},
            "name": {"value": "Main Workspace"},
            "childIds": ["n-btn", "n-input"],
            "backendDOMNodeId": 2,
        },
        {
            "nodeId": "n-btn",
            "ignored": False,
            "role": {"value": "button"},
            "name": {"value": "Run Check"},
            "properties": [{"name": "disabled", "value": {"value": False}}],
            "childIds": [],
            "backendDOMNodeId": 3,
        },
        {
            "nodeId": "n-input",
            "ignored": False,
            "role": {"value": "textbox"},
            "name": {"value": "Command Input"},
            "properties": [{"name": "disabled", "value": {"value": False}}],
            "childIds": [],
            "backendDOMNodeId": 4,
        },
    ]


def _sample_box_models() -> dict[int, list[int]]:
    return {
        1: [0, 0, 800, 0, 800, 600, 0, 600],
        2: [10, 10, 790, 10, 790, 590, 10, 590],
        3: [20, 40, 120, 40, 120, 80, 20, 80],
        4: [20, 100, 320, 100, 320, 140, 20, 140],
    }


def _sample_viewport() -> dict:
    return {
        "w": 800,
        "h": 600,
        "scroll_x": 0,
        "scroll_y": 0,
        "doc_w": 800,
        "doc_h": 600,
        "dpr_ppm": 1000000,
    }


def _make_ram_rgb(width: int = 8, height: int = 6, seed_byte: int = 42) -> np.ndarray:
    arr = np.zeros((height, width, 3), dtype=np.uint8)
    arr[:, :, 0] = (seed_byte & 0xFF)
    arr[:, :, 1] = ((seed_byte * 3) & 0xFF)
    arr[:, :, 2] = ((seed_byte * 7) & 0xFF)
    return arr


def _make_pinned_os_contract() -> tuple[w8u.PinnedOsContract, np.ndarray, np.ndarray]:
    good_rgb = _make_ram_rgb(8, 6, 42)
    bad_rgb = _make_ram_rgb(8, 6, 99)
    good_sha = hashlib.sha256(good_rgb.tobytes()).hexdigest()
    bad_sha = hashlib.sha256(bad_rgb.tobytes()).hexdigest()
    contract = w8u.PinnedOsContract(
        contract_id="w8u-os-contract",
        revision="w8u-os-contract/v1.0-pinned",
        accepted_vectors=(good_sha,),
        rejected_vectors=(bad_sha,),
    )
    return contract, good_rgb, bad_rgb


class TestElectronCdpAdapterGate(unittest.TestCase):
    """Gate 1: Electron CDP adapter positive & must-FAIL controls over fd3/fd4 pipes."""

    def setUp(self) -> None:
        self.clock = FakeClockNs()
        self.registry = SessionRegistry(clock=self.clock)
        self.broker = w8u.InMemoryFiniteCdpBroker(
            session_id="sess-electron-1",
            target_id="tab-electron-1",
            url="https://capsule.local/app",
            title="Electron Capsule App",
            viewport=_sample_viewport(),
            ax_nodes=_sample_ax_tree(),
            box_models=_sample_box_models(),
        )
        self.handle = self.registry.register(
            "cdp_tab",
            "tab-electron-1",
            self.broker,
            owned=False,
        )
        self.registry.bind_attachment(self.handle, "sess-electron-1", owned=True)
        self.capability = w8u.SessionCapability(
            handle=self.handle,
            session_id="sess-electron-1",
            target_id="tab-electron-1",
            epoch=0,
            dpr_ppm=1000000,
        )

    def test_positive_finite_broker_returns_valid_cdp_state_and_bound_frame(self) -> None:
        with w8u.DiskSinkSpy() as spy:
            rgb = _make_ram_rgb(8, 6, 42)
            frame_ref = self.registry.admit_frame(self.handle, 1, rgb, epoch=0)
            adapter = w8u.ElectronCdpAdapter(
                capability=self.capability,
                registry=self.registry,
                broker=self.broker,
            )
            state, envelope, obs = adapter.perceive(frame_ref=frame_ref, with_boxes=True)

            self.assertEqual(spy.write_attempts, [])
            self.assertEqual(schema.validate_state(state), [])
            self.assertEqual(tuple(sorted(state.keys())), schema.TOP_KEYS)
            self.assertEqual(state["schema"], "wender.state/v1")
            self.assertEqual(state["source"]["kind"], "cdp")
            self.assertEqual(state["source"]["url"], "https://capsule.local/app")
            self.assertEqual(state["counts"]["raw"], 4)
            self.assertEqual(state["counts"]["nodes"], 4)
            self.assertEqual(state["counts"]["actionable"], 2)
            for node in state["nodes"]:
                self.assertEqual(tuple(sorted(node.keys())), schema.NODE_KEYS)
                self.assertIsNotNone(node["box"])

            # Verify independent digest calculation without LF
            expected_digest = _independent_sha256_json(state)
            self.assertEqual(w8u.state_digest(state), expected_digest)

            # Verify observation envelope and FrameRef binding
            self.assertEqual(
                schema.validate_target_descriptor(envelope, expected_epoch=0, registry_epoch=0),
                [],
            )
            self.assertIsInstance(obs, Observation)
            self.assertEqual(obs.handle, self.handle)
            self.assertEqual(obs.epoch, 0)
            self.assertEqual(obs.frame_seq, 1)
            self.assertEqual(obs.frame.digest, frame_ref.digest)
            self.assertEqual(len(self.broker.requests_seen), 10)

            # Verify provenance admission succeeds for genuine CDP
            admitted = w8u.admit_state_with_provenance(
                state,
                handle=self.handle,
                cdp_adapter=adapter,
            )
            self.assertEqual(admitted["ax_sha256"], state["ax_sha256"])

    def test_bind_cdp_pipe_injected_os_pipes_without_browser_launch(self) -> None:
        cmd_r, cmd_w = os.pipe()
        rep_r, rep_w = os.pipe()
        pipe = w8u.bind_cdp_pipe_injected(cmd_w, rep_r, timeout=2.0)
        try:
            # Calling start() or evaluate() on the bound CdpPipe is forbidden
            with self.assertRaises(w8u.W8UElectronError) as ctx_start:
                pipe.start()
            self.assertEqual(ctx_start.exception.code, "E_SECURITY")

            with self.assertRaises(w8u.W8UElectronError) as ctx_eval:
                pipe.evaluate("sess-1", "1+1")
            self.assertEqual(ctx_eval.exception.code, "E_VERB")

            # Responder on the other end of fd3/fd4
            def _responder() -> None:
                raw = os.read(cmd_r, 4096)
                msgs = cdp_pipe.Deframer().feed(raw)
                for m in msgs:
                    os.write(rep_w, cdp_pipe.frame({"method": "Page.loadEventFired", "sessionId": m.get("sessionId")}))
                    os.write(rep_w, cdp_pipe.frame({"id": m["id"], "result": {"ok": 1}}))

            t = threading.Thread(target=_responder, daemon=True)
            t.start()
            res = pipe.send("Accessibility.enable", session_id="sess-1", timeout=2.0)
            ev = pipe.wait_event("Page.loadEventFired", session_id="sess-1", timeout=2.0)
            t.join(timeout=1.0)
            self.assertEqual(res, {"ok": 1})
            self.assertEqual(ev["sessionId"], "sess-1")
        finally:
            pipe.close()
            for fd in (cmd_r, rep_w):
                try:
                    os.close(fd)
                except OSError:
                    pass

    def test_fragmented_and_coalesced_replies_stay_correlated(self) -> None:
        for frag_size, coalesce in ((3, False), (5, True), (None, True)):
            with self.subTest(frag_size=frag_size, coalesce=coalesce):
                with w8u.DiskSinkSpy() as spy:
                    reg = SessionRegistry(clock=self.clock)
                    broker = w8u.InMemoryFiniteCdpBroker(
                        session_id="sess-frag",
                        target_id="tab-frag",
                        url="https://capsule.local/frag",
                        title="Fragmented Coalesced App",
                        viewport=_sample_viewport(),
                        ax_nodes=_sample_ax_tree(),
                        box_models=_sample_box_models(),
                        fragment_size=frag_size,
                        coalesce_replies=coalesce,
                    )
                    h = reg.register("cdp_tab", "tab-frag", broker, owned=False)
                    reg.bind_attachment(h, "sess-frag", owned=True)
                    cap = w8u.SessionCapability(handle=h, session_id="sess-frag", target_id="tab-frag", epoch=0)
                    adapter = w8u.ElectronCdpAdapter(capability=cap, registry=reg, broker=broker)

                    pipelined_results = adapter.send_pipelined([
                        ("Accessibility.enable", {}),
                        ("Target.getTargetInfo", {}),
                        ("Page.getLayoutMetrics", {}),
                        ("Accessibility.disable", {}),
                    ])
                    self.assertEqual(len(pipelined_results), 4)
                    self.assertEqual(pipelined_results[1]["targetInfo"]["targetId"], "tab-frag")
                    self.assertEqual(pipelined_results[2]["viewport"]["w"], 800)

                    frame_ref = reg.admit_frame(h, 1, _make_ram_rgb(8, 6, 11), epoch=0)
                    state, _env, obs = adapter.perceive(frame_ref=frame_ref)
                    self.assertEqual(schema.validate_state(state), [])
                    self.assertEqual(obs.frame_seq, 1)
                    self.assertEqual(spy.write_attempts, [])

    def test_missing_broker_refuses_e_modality_not_empty_tree(self) -> None:
        frame_ref = self.registry.admit_frame(self.handle, 1, _make_ram_rgb(), epoch=0)
        adapter_none = w8u.ElectronCdpAdapter(
            capability=self.capability,
            registry=self.registry,
            broker=None,
        )
        with self.assertRaises(w8u.W8UElectronError) as ctx:
            adapter_none.perceive(frame_ref=frame_ref)
        self.assertEqual(ctx.exception.code, "E_MODALITY")
        self.assertEqual(ctx.exception.rc, w8u.RC_UNAVAILABLE)

        # Broker returning empty AX node list must also refuse E_MODALITY
        empty_broker = w8u.InMemoryFiniteCdpBroker(
            session_id="sess-electron-1",
            target_id="tab-electron-1",
            url="https://capsule.local/app",
            title="Empty",
            viewport=_sample_viewport(),
            ax_nodes=[],
        )
        adapter_empty = w8u.ElectronCdpAdapter(
            capability=self.capability,
            registry=self.registry,
            broker=empty_broker,
        )
        with self.assertRaises(w8u.W8UElectronError) as ctx2:
            adapter_empty.perceive(frame_ref=frame_ref)
        self.assertEqual(ctx2.exception.code, "E_MODALITY")
        self.assertEqual(ctx2.exception.rc, w8u.RC_UNAVAILABLE)

    def test_must_fail_arbitrary_evaluate_js_regex_unknown_methods_and_holdings_hooks(self) -> None:
        self.assertEqual(w8u.HOLDINGS_HOOK_METHODS, "AUSENTE")
        self.assertEqual(w8u.HOLDINGS_RUNNER, "AUSENTE")
        self.assertEqual(w8u.FINITE_SEMANTIC_BROKER_API, "AUSENTE")

        adapter = w8u.ElectronCdpAdapter(
            capability=self.capability,
            registry=self.registry,
            broker=self.broker,
        )
        # 1. Direct evaluate() call refused
        with self.assertRaises(w8u.W8UElectronError) as ctx:
            adapter.evaluate("sess-electron-1", "1+1")
        self.assertEqual(ctx.exception.code, "E_VERB")
        self.assertEqual(len(self.broker.requests_seen), 0)

        # 2. Runtime.evaluate via send() refused before writing to pipe
        with self.assertRaises(w8u.W8UElectronError) as ctx:
            adapter.send("Runtime.evaluate", {"expression": "window.location.href"})
        self.assertEqual(ctx.exception.code, "E_VERB")
        self.assertEqual(len(self.broker.requests_seen), 0)

        # 3. Unknown CDP method refused
        with self.assertRaises(w8u.W8UElectronError) as ctx:
            adapter.send("Page.captureScreenshot", {})
        self.assertEqual(ctx.exception.code, "E_VERB")
        self.assertEqual(len(self.broker.requests_seen), 0)

        # 4. Regex sanitizer refused
        with self.assertRaises(w8u.W8UElectronError) as ctx:
            adapter.sanitize_js_regex(r"^[a-z]+$")
        self.assertEqual(ctx.exception.code, "E_VERB")

        # 5. Invented Holdings hooks refused
        with self.assertRaises(w8u.W8UElectronError) as ctx:
            adapter.invoke_holdings_hook("__holdings_dump")
        self.assertEqual(ctx.exception.code, "E_MODALITY")
        with self.assertRaises(w8u.W8UElectronError) as ctx:
            w8u.ReviewedSemanticContract(holdings_hook_methods="window.__holdings")
        self.assertEqual(ctx.exception.code, "E_VERB")

    def test_must_fail_host_launch_and_debug_ports(self) -> None:
        with self.assertRaises(w8u.W8UElectronError) as ctx:
            w8u.ElectronCdpAdapter(
                capability=self.capability,
                registry=self.registry,
                broker=self.broker,
                launch_host=True,
            )
        self.assertEqual(ctx.exception.code, "E_SECURITY")

        with self.assertRaises(w8u.W8UElectronError) as ctx:
            w8u.ElectronCdpAdapter(
                capability=self.capability,
                registry=self.registry,
                broker=self.broker,
                chrome_bin="/usr/bin/google-chrome",
            )
        self.assertEqual(ctx.exception.code, "E_SECURITY")

        with self.assertRaises(w8u.W8UElectronError) as ctx:
            w8u.ElectronCdpAdapter(
                capability=self.capability,
                registry=self.registry,
                broker=self.broker,
                debug_port=9222,
            )
        self.assertEqual(ctx.exception.code, "E_SECURITY")

        adapter = w8u.ElectronCdpAdapter(
            capability=self.capability,
            registry=self.registry,
            broker=self.broker,
        )
        with self.assertRaises(w8u.W8UElectronError) as ctx:
            adapter.start()
        self.assertEqual(ctx.exception.code, "E_SECURITY")

    def test_must_fail_foreign_session_and_epoch_mismatch(self) -> None:
        adapter = w8u.ElectronCdpAdapter(
            capability=self.capability,
            registry=self.registry,
            broker=self.broker,
        )
        # Explicit foreign session_id in send()
        with self.assertRaises(w8u.W8UElectronError) as ctx:
            adapter.send("Accessibility.enable", session_id="foreign-session-99")
        self.assertEqual(ctx.exception.code, "E_SECURITY")
        self.assertEqual(len(self.broker.requests_seen), 0)

        # Foreign sessionId in broker reply
        self.broker.queue_raw_reply_bytes(
            cdp_pipe.frame({"id": 1, "sessionId": "foreign-session-99", "result": {}})
        )
        with self.assertRaises(w8u.W8UElectronError) as ctx:
            adapter.send("Accessibility.enable")
        self.assertEqual(ctx.exception.code, "E_SECURITY")
        self.assertEqual(len(self.broker.requests_seen), 1)

        # Advance registry epoch so capability epoch is stale
        self.registry.advance_epoch(self.handle)
        with self.assertRaises(w8u.W8UElectronError) as ctx:
            adapter.send("Accessibility.enable")
        self.assertEqual(ctx.exception.code, "E_EPOCH")

    def test_must_fail_malformed_nul_utf8_duplicate_keys_and_conflicting_reply(self) -> None:
        # 1. Invalid UTF-8 bytes on fd4
        adapter = w8u.ElectronCdpAdapter(
            capability=self.capability,
            registry=self.registry,
            broker=self.broker,
        )
        self.broker.queue_raw_reply_bytes(b'{"id":1,"sessionId":"sess-electron-1","result":"\xff\xfe"}\x00')
        with self.assertRaises(w8u.W8UElectronError) as ctx:
            adapter.send("Accessibility.enable")
        self.assertEqual(ctx.exception.code, "E_SCHEMA")

        # 2. Empty NUL frame b"\x00"
        deframer = w8u.BoundedDeframer()
        with self.assertRaises(w8u.W8UElectronError) as ctx:
            deframer.feed(b"\x00")
        self.assertEqual(ctx.exception.code, "E_SCHEMA")

        # 3. Duplicate JSON key in reply
        deframer2 = w8u.BoundedDeframer()
        with self.assertRaises(w8u.W8UElectronError) as ctx:
            deframer2.feed(b'{"id":1,"id":2,"sessionId":"sess-electron-1","result":{}}\x00')
        self.assertEqual(ctx.exception.code, "E_SCHEMA")

        # 4. Unterminated frame at EOF
        deframer3 = w8u.BoundedDeframer()
        self.assertEqual(deframer3.feed(b'{"id":1'), [])
        with self.assertRaises(w8u.W8UElectronError) as ctx:
            deframer3.finish_eof()
        self.assertEqual(ctx.exception.code, "E_SCHEMA")

        # 5. Conflicting duplicate reply for same id
        adapter2 = w8u.ElectronCdpAdapter(
            capability=self.capability,
            registry=self.registry,
            broker=self.broker,
        )
        self.broker.queue_fault_handler(
            lambda req: [
                cdp_pipe.frame({"id": req["id"], "sessionId": "sess-electron-1", "result": {}}),
                cdp_pipe.frame({"id": req["id"], "sessionId": "sess-electron-1", "result": {"conflict": 1}}),
            ]
        )
        with self.assertRaises(w8u.W8UElectronError) as ctx:
            adapter2.send("Accessibility.enable")
        self.assertEqual(ctx.exception.code, "E_PROTOCOL")

        # 6. Reply containing both result and error
        adapter3 = w8u.ElectronCdpAdapter(
            capability=self.capability,
            registry=self.registry,
            broker=self.broker,
        )
        self.broker.queue_fault_handler(
            lambda req: [
                cdp_pipe.frame({
                    "id": req["id"],
                    "sessionId": "sess-electron-1",
                    "result": {},
                    "error": {"code": -1, "message": "conflict"},
                })
            ]
        )
        with self.assertRaises(w8u.W8UElectronError) as ctx:
            adapter3.send("Accessibility.enable")
        self.assertEqual(ctx.exception.code, "E_PROTOCOL")

    def test_must_fail_fabricated_cdp_source_and_unreviewed_app_tree(self) -> None:
        rgb = _make_ram_rgb()
        frame_ref = self.registry.admit_frame(self.handle, 1, rgb, epoch=0)
        adapter = w8u.ElectronCdpAdapter(
            capability=self.capability,
            registry=self.registry,
            broker=self.broker,
        )
        state, _env, _obs = adapter.perceive(frame_ref=frame_ref)

        # Fabricated CDP state without adapter provenance must fail
        with self.assertRaises(w8u.W8UElectronError) as ctx:
            w8u.admit_state_with_provenance(state, handle=self.handle, cdp_adapter=None)
        self.assertEqual(ctx.exception.code, "E_SECURITY")

        # Claiming kind='cdp' on a godot_window handle must fail
        godot_h = self.registry.register("godot_window", "win-godot-1", object(), owned=False)
        with self.assertRaises(w8u.W8UElectronError) as ctx:
            w8u.admit_state_with_provenance(state, handle=godot_h, cdp_adapter=adapter)
        self.assertEqual(ctx.exception.code, "E_SECURITY")

        # App-exported godot tree without ReviewedProvenance must fail E_MODALITY
        godot_state = dict(state, source={"kind": "godot", "url": "godot://scene", "title": "Scene"})
        with self.assertRaises(w8u.W8UElectronError) as ctx:
            w8u.admit_state_with_provenance(godot_state, handle=godot_h, provenance=None)
        self.assertEqual(ctx.exception.code, "E_MODALITY")

        # App-exported godot tree WITH valid ReviewedProvenance succeeds
        prov = w8u.ReviewedProvenance(
            source_kind="godot",
            exporter_id="holdings-t2-reviewed",
            contract_revision="w8u-godot-tree/v1",
            witness_sha256=w8u.state_digest(godot_state),
        )
        admitted_godot = w8u.admit_state_with_provenance(godot_state, handle=godot_h, provenance=prov)
        self.assertEqual(admitted_godot["source"]["kind"], "godot")


class TestFrameStreamAndCreditsGate(unittest.TestCase):
    """Gate 2: OS contract, XWD held-pair equality, credit controller, and stream lifecycle."""

    def setUp(self) -> None:
        self.clock = FakeClockNs()
        self.registry = SessionRegistry(clock=self.clock)
        self.os_contract, self.good_rgb, self.bad_rgb = _make_pinned_os_contract()
        self.handle = self.registry.register("xvfb_screen", "screen-0", object(), owned=False)

    def test_credit_controller_capacity_one_preferred_two_exhaustion_duplicate_and_inflation(self) -> None:
        # Case A: reserved_capacity = 1 -> initial ACK seq 0 grants min(2, 1) == 1
        ctrl_cap1 = w8u.CreditWindowController(reserved_capacity=1)
        self.assertEqual(ctrl_cap1.ack(seq=0), 1)
        self.assertEqual(ctrl_cap1.available_credits, 1)
        ctrl_cap1.consume_credit_for_emit()
        self.assertEqual(ctrl_cap1.available_credits, 0)

        # Credit exhaustion: second emit without ACK fails E_CREDIT
        with self.assertRaises(w8u.W8UElectronError) as ctx:
            ctrl_cap1.consume_credit_for_emit()
        self.assertEqual(ctx.exception.code, "E_CREDIT")

        # Overgrant at seq=0 when capacity=1 (requesting 2) fails E_CREDIT
        ctrl_over0 = w8u.CreditWindowController(reserved_capacity=1)
        with self.assertRaises(w8u.W8UElectronError) as ctx:
            ctrl_over0.ack(seq=0, grant=2)
        self.assertEqual(ctx.exception.code, "E_CREDIT")

        # Case B: reserved_capacity = 3 -> initial ACK seq 0 grants min(2, 3) == 2
        ctrl_cap3 = w8u.CreditWindowController(reserved_capacity=3)
        self.assertEqual(ctrl_cap3.ack(seq=0), 2)
        ctrl_cap3.consume_credit_for_emit()  # frame 1 emitted
        ctrl_cap3.consume_credit_for_emit()  # frame 2 emitted
        self.assertEqual(ctrl_cap3.available_credits, 0)

        # Duplicate initial ACK seq 0 fails E_CREDIT
        with self.assertRaises(w8u.W8UElectronError) as ctx:
            ctrl_cap3.ack(seq=0, grant=1)
        self.assertEqual(ctx.exception.code, "E_CREDIT")

        # Valid ACK for seq 1 granting 2 brings cumulative to 4 <= acknowledged(1) + capacity(3) = 4
        self.assertEqual(ctrl_cap3.ack(seq=1, grant=2), 4)

        # Duplicate ACK for seq 1 fails E_CREDIT
        with self.assertRaises(w8u.W8UElectronError) as ctx:
            ctrl_cap3.ack(seq=1, grant=1)
        self.assertEqual(ctx.exception.code, "E_CREDIT")

        # Credit inflation on seq 2: acknowledged will be 2, max cumulative is 2 + 3 = 5.
        # Currently cumulative is 4, so granting 2 (4 + 2 = 6 > 5) fails E_CREDIT!
        with self.assertRaises(w8u.W8UElectronError) as ctx:
            ctrl_cap3.ack(seq=2, grant=2)
        self.assertEqual(ctx.exception.code, "E_CREDIT")

        # Granting 1 (4 + 1 = 5 <= 5) succeeds
        self.assertEqual(ctrl_cap3.ack(seq=2, grant=1), 5)

    def test_xwd_held_pair_equality_rejected_vectors_reopen_and_shutdown_lifecycle(self) -> None:
        with w8u.DiskSinkSpy() as spy:
            stream = w8u.CapsuleFrameStream(
                os_contract=self.os_contract,
                registry=self.registry,
                handle=self.handle,
                run_id="run-electron-01",
                reserved_capacity=2,
            )
            self.assertEqual(stream.ack(seq=0), 2)

            xwd_good = w8u.build_xwd_bytes(self.good_rgb)
            xwd_bad = w8u.build_xwd_bytes(self.bad_rgb)

            # 1. Mismatched grab pair is rejected with E_PAIR_MISMATCH
            with self.assertRaises(w8u.W8UElectronError) as ctx:
                stream.ingest_held_pair(
                    xwd_good,
                    xwd_bad,
                    seq=1,
                    capture_mono_ns=self.clock(),
                    epoch=0,
                    tick=None,
                    tick_valid=False,
                )
            self.assertEqual(ctx.exception.code, "E_PAIR_MISMATCH")
            self.assertEqual(stream.pair_mismatches, 1)
            self.assertEqual(stream.frames_dropped, 1)

            # 2. Pinned rejected vector is rejected with E_SCHEMA
            with self.assertRaises(w8u.W8UElectronError) as ctx:
                stream.ingest_held_pair(
                    xwd_bad,
                    xwd_bad,
                    seq=1,
                    capture_mono_ns=self.clock(),
                    epoch=0,
                    tick=None,
                    tick_valid=False,
                )
            self.assertEqual(ctx.exception.code, "E_SCHEMA")
            self.assertEqual(stream.frames_dropped, 2)

            # 3. tick_valid=False with committed zero tick=0 is rejected
            with self.assertRaises(w8u.W8UElectronError) as ctx:
                stream.ingest_held_pair(
                    xwd_good,
                    xwd_good,
                    seq=1,
                    capture_mono_ns=self.clock(),
                    epoch=0,
                    tick=0,
                    tick_valid=False,
                )
            self.assertEqual(ctx.exception.code, "E_SCHEMA")

            # 4. Valid held pair with tick_valid=False, tick=None succeeds
            fref1, fmeta1 = stream.ingest_held_pair(
                xwd_good,
                xwd_good,
                seq=1,
                capture_mono_ns=self.clock(),
                epoch=0,
                tick=None,
                tick_valid=False,
            )
            self.assertEqual(fref1.frame_seq, 1)
            self.assertIsNone(fmeta1["tick"])
            self.assertFalse(fmeta1["tick_valid"])
            self.assertEqual(w8u.validate_frame_metadata(fmeta1), [])

            # 5. Reopen per frame is forbidden
            with self.assertRaises(w8u.W8UElectronError) as ctx:
                stream.reopen_channel()
            self.assertEqual(ctx.exception.code, "E_PROTOCOL")
            self.assertEqual(stream.pipe.reopen_attempts, 1)

            # 6. Mandatory STOP -> END -> EOF -> joined exit
            stream.shutdown(end_code="OK", reason="done", purge_ram=True)
            self.assertTrue(stream.stopped)
            self.assertTrue(stream.ended)
            self.assertTrue(stream.eof_sent)
            self.assertTrue(stream.reader_joined)
            self.assertEqual(stream.reader_exit_code, 0)
            self.assertTrue(stream.ram_purged)

            # 7. Stale publication after STOP/END is refused
            with self.assertRaises(w8u.W8UElectronError) as ctx:
                stream.ingest_held_pair(
                    xwd_good,
                    xwd_good,
                    seq=2,
                    capture_mono_ns=self.clock(),
                    epoch=0,
                )
            self.assertEqual(ctx.exception.code, "E_STALE")
            self.assertEqual(spy.write_attempts, [])


class TestClosedActionChannelAndReceiptGate(unittest.TestCase):
    """Gate 3: w8u.action/v1 validation, actuator fencing, replay/quarantine, and w8u.receipt/v1."""

    def setUp(self) -> None:
        self.clock = FakeClockNs(initial_ns=10_000_000_000)
        self.registry = SessionRegistry(clock=self.clock)
        self.handle = self.registry.register("cdp_tab", "tab-act-1", object(), owned=False)
        self.authority = w8u.ExternalLeaseAuthority(
            run_id="run-act-01",
            lease_id="lease-act-01",
            handle=self.handle,
        )
        self.frame_ref = self.registry.admit_frame(self.handle, 1, _make_ram_rgb(16, 12, 55), epoch=0)

    def _base_action(
        self,
        *,
        action_id: str = "act-1",
        action_seq: int = 1,
        verb: str = "pointer",
        args: dict | None = None,
        epoch: int = 0,
        generation: int | None = None,
        deadline_ns: int = 20_000_000_000,
    ) -> dict:
        eff_gen = self.handle.generation if generation is None else generation
        return {
            "schema": "w8u.action/v1",
            "run_id": "run-act-01",
            "lease_id": "lease-act-01",
            "epoch": epoch,
            "generation": eff_gen,
            "action_id": action_id,
            "action_seq": action_seq,
            "observed_frame_seq": self.frame_ref.frame_seq,
            "observed_frame_sha256": self.frame_ref.digest,
            "deadline_ns": deadline_ns,
            "verb": verb,
            "args": args if args is not None else {"operation": "move", "x": 100, "y": 200},
            "grant_sha256": self.authority.compute_grant_sha256(epoch=epoch, generation=eff_gen),
        }

    def test_action_schema_rejects_invalid_wire_and_verb_bounds_before_actuator(self) -> None:
        channel = w8u.ClosedActionChannel(
            authority=self.authority,
            registry=self.registry,
            handle=self.handle,
            clock_ns=self.clock,
            screen_width=800,
            screen_height=600,
            actuator=lambda _a: {"applied": True, "applied_tick": 1},
        )

        # 1. > 64 KiB payload rejected
        huge_raw = b'{"a":"' + (b"x" * 66000) + b'"}'
        resp, rc = channel.submit(huge_raw, latest_frame=self.frame_ref)
        self.assertEqual((resp["status"], resp["code"], rc), ("refused", "E_SCHEMA", 2))

        # 2. Duplicate JSON key rejected
        dup_raw = b'{"schema":"w8u.action/v1","schema":"w8u.action/v1"}'
        resp, rc = channel.submit(dup_raw, latest_frame=self.frame_ref)
        self.assertEqual((resp["status"], resp["code"], rc), ("refused", "E_SCHEMA", 2))

        # 3. Trailing data rejected
        valid_json = w8u.dumps_canonical(self._base_action()).encode("utf-8") + b"extra"
        resp, rc = channel.submit(valid_json, latest_frame=self.frame_ref)
        self.assertEqual((resp["status"], resp["code"], rc), ("refused", "E_SCHEMA", 2))

        # 4. Float coordinate and bool-as-int coordinate rejected
        for bad_coord in (10.5, True, False, -1, 800):
            act = self._base_action(args={"operation": "move", "x": bad_coord, "y": 50})
            resp, rc = channel.submit(act, latest_frame=self.frame_ref)
            self.assertEqual((resp["status"], resp["code"], rc), ("refused", "E_SCHEMA", 2))

        # 5. Pointer click button outside 1,2,3 and scroll zero or >120 delta rejected
        for bad_btn in (0, 4, True):
            act = self._base_action(args={"operation": "click", "x": 10, "y": 10, "button": bad_btn})
            resp, rc = channel.submit(act, latest_frame=self.frame_ref)
            self.assertEqual((resp["status"], resp["code"], rc), ("refused", "E_SCHEMA", 2))

        for bad_dx, bad_dy in ((0, 0), (121, 0), (0, -121)):
            act = self._base_action(args={"operation": "scroll", "x": 10, "y": 10, "dx": bad_dx, "dy": bad_dy})
            resp, rc = channel.submit(act, latest_frame=self.frame_ref)
            self.assertEqual((resp["status"], resp["code"], rc), ("refused", "E_SCHEMA", 2))

        # 6. Key finite allowlist and modifier canonical order Shift,Control,Alt,Super
        for bad_key_args in (
            {"operation": "press", "key": "F12", "modifiers": []},
            {"operation": "press", "key": "Enter", "modifiers": ["Control", "Shift"]},
            {"operation": "press", "key": "Enter", "modifiers": ["Shift", "Shift"]},
        ):
            act = self._base_action(verb="key", args=bad_key_args)
            resp, rc = channel.submit(act, latest_frame=self.frame_ref)
            self.assertEqual((resp["status"], resp["code"], rc), ("refused", "E_SCHEMA", 2))

        # 7. Text NUL and >4096 bytes rejected
        for bad_txt in ("hello\x00world", "a" * 4097):
            act = self._base_action(verb="text", args={"mode": "type", "text": bad_txt})
            resp, rc = channel.submit(act, latest_frame=self.frame_ref)
            self.assertEqual((resp["status"], resp["code"], rc), ("refused", "E_SCHEMA", 2))

        # 8. Wait outside 0..5000 ms rejected
        for bad_wait in (-1, 5001, True):
            act = self._base_action(verb="wait", args={"duration_ms": bad_wait})
            resp, rc = channel.submit(act, latest_frame=self.frame_ref)
            self.assertEqual((resp["status"], resp["code"], rc), ("refused", "E_SCHEMA", 2))

        # 9. Unknown verb rejected with E_VERB
        act_unknown = self._base_action(verb="eval_js", args={"operation": "move", "x": 1, "y": 1})
        resp, rc = channel.submit(act_unknown, latest_frame=self.frame_ref)
        self.assertEqual((resp["status"], resp["code"], rc), ("refused", "E_VERB", 2))

        # Actuator must never have been called for any schema/verb rejection
        self.assertEqual(len(channel.actuator_calls), 0)

    def test_verification_refusals_idempotent_replay_and_lost_ack_quarantine(self) -> None:
        act_state = {"lost_ack": False, "applied": False, "applied_tick": None}

        def _actuator(_act: dict) -> dict:
            return dict(act_state)

        channel = w8u.ClosedActionChannel(
            authority=self.authority,
            registry=self.registry,
            handle=self.handle,
            clock_ns=self.clock,
            screen_width=800,
            screen_height=600,
            budget_remaining=2,
            editable_focus=False,
            actuator=_actuator,
        )

        # 1. Text without editable focus refuses E_FOCUS (rc=5)
        act_text = self._base_action(action_id="a-focus", action_seq=1, verb="text", args={"mode": "type", "text": "hi"})
        resp, rc = channel.submit(act_text, latest_frame=self.frame_ref)
        self.assertEqual((resp["status"], resp["code"], rc, resp["emitted"]), ("refused", "E_FOCUS", 5, False))

        # 2. Wait exceeding deadline_ns refuses E_DEADLINE (rc=5)
        act_wait = self._base_action(
            action_id="a-deadline",
            action_seq=1,
            verb="wait",
            args={"duration_ms": 4000},
            deadline_ns=self.clock() + 1_000_000_000,  # only 1000 ms left
        )
        resp, rc = channel.submit(act_wait, latest_frame=self.frame_ref)
        self.assertEqual((resp["status"], resp["code"], rc), ("refused", "E_DEADLINE", 5))

        # 3. Stale observed_frame_seq refuses E_STALE_FRAME (rc=5)
        act_stale = self._base_action(action_id="a-stale", action_seq=1)
        act_stale["observed_frame_seq"] = 999
        resp, rc = channel.submit(act_stale, latest_frame=self.frame_ref)
        self.assertEqual((resp["status"], resp["code"], rc), ("refused", "E_STALE_FRAME", 5))

        # 4. Bad external grant_sha256 refuses E_LOCK (rc=5)
        act_bad_grant = self._base_action(action_id="a-grant", action_seq=1)
        act_bad_grant["grant_sha256"] = "0" * 64
        resp, rc = channel.submit(act_bad_grant, latest_frame=self.frame_ref)
        self.assertEqual((resp["status"], resp["code"], rc), ("refused", "E_LOCK", 5))

        # 5. Stale epoch in action refuses E_EPOCH (rc=5)
        act_bad_epoch = self._base_action(action_id="a-epoch", action_seq=1, epoch=99)
        resp, rc = channel.submit(act_bad_epoch, latest_frame=self.frame_ref)
        self.assertEqual((resp["status"], resp["code"], rc), ("refused", "E_EPOCH", 5))

        # 6. Exhausted budget refuses E_BUDGET (rc=5)
        channel.budget_remaining = 0
        act_budget = self._base_action(action_id="a-budget", action_seq=1)
        resp, rc = channel.submit(act_budget, latest_frame=self.frame_ref)
        self.assertEqual((resp["status"], resp["code"], rc), ("refused", "E_BUDGET", 5))
        channel.budget_remaining = 2

        # 7. Missing actuator returns rc=4 (unavailable)
        channel_no_act = w8u.ClosedActionChannel(
            authority=self.authority,
            registry=self.registry,
            handle=self.handle,
            clock_ns=self.clock,
            actuator=None,
        )
        resp_unavail, rc_unavail = channel_no_act.submit(self._base_action(), latest_frame=self.frame_ref)
        self.assertEqual((resp_unavail["status"], rc_unavail), ("refused", 4))

        self.assertEqual(len(channel.actuator_calls), 0)

        # 8. Valid admitted action (admission is not effect -> applied_tick is None)
        act1 = self._base_action(
            action_id="act-1",
            action_seq=1,
            verb="key",
            args={"operation": "press", "key": "Enter", "modifiers": ["Shift", "Control"]},
        )
        resp1, rc1 = channel.submit(act1, latest_frame=self.frame_ref)
        self.assertEqual((resp1["status"], resp1["code"], rc1), ("admitted", "OK_ADMITTED", 0))
        self.assertTrue(resp1["emitted"])
        self.assertTrue(resp1["result_known"])
        self.assertIsNone(resp1["applied_tick"])
        self.assertEqual(len(channel.actuator_calls), 1)

        # 9. Identical replay is idempotent (does not call actuator again)
        resp1_replay, rc1_replay = channel.submit(act1, latest_frame=self.frame_ref)
        self.assertEqual(resp1_replay, resp1)
        self.assertEqual(rc1_replay, 0)
        self.assertEqual(len(channel.actuator_calls), 1)

        # 10. Changed payload on same action_id refuses E_LOCK (rc=5)
        act1_mutated = dict(act1, args={"operation": "press", "key": "Escape", "modifiers": []})
        resp1_mut, rc1_mut = channel.submit(act1_mutated, latest_frame=self.frame_ref)
        self.assertEqual((resp1_mut["status"], resp1_mut["code"], rc1_mut), ("refused", "E_LOCK", 5))
        self.assertEqual(len(channel.actuator_calls), 1)

        # 11. Lost ACK on act-2 -> status='uncertain', code='E_UNCERTAIN', rc=6, and quarantines channel
        act_state["lost_ack"] = True
        act2 = self._base_action(
            action_id="act-2",
            action_seq=2,
            verb="pointer",
            args={"operation": "click", "x": 50, "y": 60, "button": 1},
        )
        resp2, rc2 = channel.submit(act2, latest_frame=self.frame_ref)
        self.assertEqual((resp2["status"], resp2["code"], rc2), ("uncertain", "E_UNCERTAIN", 6))
        self.assertTrue(resp2["emitted"])
        self.assertFalse(resp2["result_known"])
        self.assertIsNone(resp2["applied_tick"])
        self.assertEqual(channel.quarantined_action_id, "act-2")

        # 12. New-ID retry while quarantined is refused (E_LOCK, rc=5) without calling actuator
        act3 = self._base_action(
            action_id="act-3",
            action_seq=3,
            verb="pointer",
            args={"operation": "move", "x": 10, "y": 10},
        )
        resp3, rc3 = channel.submit(act3, latest_frame=self.frame_ref)
        self.assertEqual((resp3["status"], resp3["code"], rc3), ("refused", "E_LOCK", 5))
        self.assertFalse(resp3["emitted"])
        self.assertEqual(len(channel.actuator_calls), 2)

    def test_receipt_schema_positive_and_zero_frame_indeterminate(self) -> None:
        os_contract, good_rgb, _bad_rgb = _make_pinned_os_contract()
        h_rec = self.registry.register("cdp_tab", "tab-receipt-1", object(), owned=False)
        auth_rec = w8u.ExternalLeaseAuthority(
            run_id="run-act-01",
            lease_id="lease-act-01",
            handle=h_rec,
        )
        stream = w8u.CapsuleFrameStream(
            os_contract=os_contract,
            registry=self.registry,
            handle=h_rec,
            run_id="run-act-01",
            reserved_capacity=2,
        )
        stream.ack(seq=0)
        xwd = w8u.build_xwd_bytes(good_rgb)
        _fref, fmeta = stream.ingest_held_pair(
            xwd,
            xwd,
            seq=1,
            capture_mono_ns=self.clock(),
            epoch=0,
            tick=7,
            tick_valid=True,
        )
        stream.shutdown(end_code="OK")

        channel = w8u.ClosedActionChannel(
            authority=auth_rec,
            registry=self.registry,
            handle=h_rec,
            clock_ns=self.clock,
            actuator=lambda _a: {"applied": True, "applied_tick": 7},
        )
        act = self._base_action(action_id="act-ok", action_seq=1, generation=h_rec.generation)
        act["observed_frame_seq"] = _fref.frame_seq
        act["observed_frame_sha256"] = _fref.digest
        act["grant_sha256"] = auth_rec.compute_grant_sha256(epoch=0, generation=h_rec.generation)
        resp, rc = channel.submit(act, latest_frame=_fref)
        self.assertEqual((resp["status"], rc, resp["applied_tick"]), ("applied", 0, 7))

        receipt = w8u.build_receipt(
            run_id="run-act-01",
            lease_id="lease-act-01",
            target_handle=h_rec,
            epoch=0,
            frames=[fmeta],
            action_channel=channel,
            final_state_sha256="a" * 64,
            simulation_state_sha256=None,
            t2_trace_sha256=None,
            t2_terminal_sha256=None,
            t2_ticks=None,
        )
        self.assertEqual(w8u.validate_receipt(receipt), [])
        self.assertEqual(receipt["frames_root"], "/dev/shm/vqa-ramws")
        self.assertEqual(receipt["frame_run_root"], "/dev/shm/vqa-ramws/opensuperficies/os-cu-0/run-act-01")
        self.assertEqual(receipt["source"], "xvfb-grab-held-pair")
        self.assertEqual(
            receipt["target_handle"],
            {"kind": "cdp_tab", "canonical_id": "tab-receipt-1", "generation": h_rec.generation},
        )
        self.assertEqual(receipt["ui_gate_verdict"], "pass")
        self.assertIsNone(receipt["t2_ticks"])

        # Zero-frame pixel-required run -> indeterminate, first_frame/last_frame null
        zero_receipt = w8u.build_receipt(
            run_id="run-act-01",
            lease_id="lease-act-01",
            target_handle=h_rec,
            epoch=0,
            frames=[],
            action_channel=channel,
            pixel_required=True,
        )
        self.assertEqual(w8u.validate_receipt(zero_receipt, pixel_required=True), [])
        self.assertEqual(zero_receipt["frames_captured"], 0)
        self.assertIsNone(zero_receipt["first_frame"])
        self.assertIsNone(zero_receipt["last_frame"])
        self.assertEqual(zero_receipt["ui_gate_verdict"], "indeterminate")

    def test_state_and_envelope_reject_float_bool_coords_unknown_keys_and_perception_wrappers(self) -> None:
        rgb = _make_ram_rgb()
        fref = self.registry.admit_frame(self.handle, 2, rgb, epoch=0)
        broker = w8u.InMemoryFiniteCdpBroker(
            session_id="sess-s",
            target_id="tab-act-1",
            url="https://capsule.local/s",
            title="S",
            viewport=_sample_viewport(),
            ax_nodes=_sample_ax_tree(),
            box_models=_sample_box_models(),
        )
        self.registry.bind_attachment(self.handle, "sess-s", owned=True)
        cap = w8u.SessionCapability(handle=self.handle, session_id="sess-s", target_id="tab-act-1", epoch=0)
        adapter = w8u.ElectronCdpAdapter(capability=cap, registry=self.registry, broker=broker)
        state, env, _obs = adapter.perceive(frame_ref=fref)

        # _perception wrapper rejected
        bad_state = dict(state, _perception={"leaked": 1})
        with self.assertRaises(w8u.W8UElectronError) as ctx:
            w8u.state_digest(bad_state)
        self.assertEqual(ctx.exception.code, "E_SCHEMA")

        # Bool coordinate in box rejected by validate_state
        bad_box_state = json.loads(schema.dumps(state))
        bad_box_state["nodes"][0]["box"] = [True, 0, 10, 10]
        self.assertTrue(schema.validate_state(bad_box_state))
        with self.assertRaises(w8u.W8UElectronError):
            w8u.state_digest(bad_box_state)

        # Float in envelope rejected by validate_target_descriptor
        bad_env = dict(env, dpr_ppm=1.5)
        self.assertTrue(schema.validate_target_descriptor(bad_env))

    def test_disk_sink_spy_blocks_any_filesystem_write(self) -> None:
        with w8u.DiskSinkSpy() as spy:
            with self.assertRaises(w8u.W8UElectronError) as ctx:
                open("/tmp/forbidden_frame.raw", "wb")
            self.assertEqual(ctx.exception.code, "E_STORAGE_LAW")
            self.assertEqual(len(spy.write_attempts), 1)


if __name__ == "__main__":
    unittest.main()
