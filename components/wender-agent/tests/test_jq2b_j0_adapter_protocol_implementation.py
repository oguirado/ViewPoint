"""Tests for jq2b-j0-adapter-protocol: EpisodeAdapter Protocol, capability contracts, and reset protocol.

Acceptance gates:
- gate G J0 1: Neutral import graph (Negative contrast: core imports cdp_port; Positive control: dependency-free protocol import)
- gate G J0 2: Reset fences old generation (Negative contrast: same handle reused after reset; Positive control: fresh handle plus verified initial digest)
- gate G J0 4: Capability truth is preserved (Negative contrast: unavailable capture returns success; Positive control: unavailable yields explicit unsupported)

Invariants INV J0 1 through INV J0 4:
- INV J0 1: forbid backend imports in core
- INV J0 2: fence old generations after reset
- INV J0 3: prevent authority or capture-receipt export; volatile hooks/pixels rejected by WriteSpy
- INV J0 4: preserve unavailable capability as typed unsupported evidence
"""
from __future__ import annotations

import ast
import hashlib
import io
import importlib
import sys
import unittest
from unittest import mock
from pathlib import Path
import numpy as np

from wender.learn import adapter_protocol
from wender.learn.adapter_protocol import (
    ApprovedFrameMetadata,
    AusenteError,
    Capabilities,
    CapabilityError,
    Command,
    EpisodeAdapter,
    FencedGenerationError,
    LauncherEvidence,
    NeutralEpisodeAdapter,
    Outcome,
    ResetResult,
    SUPPORTED_HANDLE_KINDS,
    UnsupportedEvidence,
    WriteSpy,
    capabilities_from_launcher,
    default_handle_kind,
    make_pause_command,
    make_receipt,
    make_stream_spec,
    min_stream_quota_bytes,
    require_prerequisite_provider,
    verify_neutral_import_graph,
    verify_prerequisite_contracts,
)
from wender.state import schema
from wender.state.schema import FrameRef, Handle, Observation, Receipt, StreamSpec, StreamToken


def _make_dummy_state(canonical_id: str = "win-1", kind: str = "cdp_tab") -> dict:
    return {
        "actionable": [],
        "ax_sha256": "0" * 64,
        "ax_stable_sha256": "0" * 64,
        "counts": {"actionable": 0, "nodes": 1, "raw": 1},
        "nodes": [
            {
                "actionable": False,
                "box": [0, 0, 100, 100],
                "depth": 0,
                "id": "0000000000000001",
                "name": "Root",
                "ordinal": 0,
                "parent": None,
                "role": "RootWebArea",
                "states": {},
            }
        ],
        "prune": {"name": "prune_ax_v1", "version": 1},
        "schema": schema.SCHEMA_ID,
        "source": {
            "kind": "cdp" if "cdp" in kind else "godot",
            "title": "Test",
            "url": "app://window",
        },
        "viewport": {
            "doc_h": 720,
            "doc_w": 1280,
            "dpr_ppm": 1000000,
            "h": 720,
            "scroll_x": 0,
            "scroll_y": 0,
            "w": 1280,
        },
    }


def _make_dummy_frame_ref(handle: Handle, epoch: int = 0, seq: int = 0) -> FrameRef:
    dummy_pixels = np.zeros((100, 100, 3), dtype=np.uint8)
    digest = hashlib.sha256(dummy_pixels.tobytes(order="C")).hexdigest()
    return FrameRef(
        handle=handle,
        epoch=epoch,
        frame_seq=seq,
        width=100,
        height=100,
        rgb=dummy_pixels,
        digest=digest,
    )


class TestGateGJ01NeutralImportGraph(unittest.TestCase):
    """gate G J0 1 requires Neutral import graph.

    Negative contrast: core imports cdp_port.
    Positive control: dependency-free protocol import.
    """

    def test_positive_control_dependency_free_protocol_import(self):
        """Protocol module imports purely from stdlib and merged schema, importing zero backend ports."""
        self.assertTrue(verify_neutral_import_graph())

        mod_file = Path(adapter_protocol.__file__).resolve()
        content = mod_file.read_text(encoding="utf-8")
        parsed = ast.parse(content, filename=str(mod_file))

        imported_names = []
        for node in ast.walk(parsed):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imported_names.append(alias.name)
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    imported_names.append(node.module)

        forbidden_prefixes = ("cdp", "cdp_port", "godot", "selenium", "playwright", "chromium")
        for imp in imported_names:
            for forbidden in forbidden_prefixes:
                self.assertFalse(
                    imp == forbidden or imp.startswith(forbidden + ".") or forbidden in imp.split("."),
                    f"gate G J0 1 violation: found forbidden backend import '{imp}' in adapter_protocol.py",
                )

    def test_negative_contrast_core_imports_cdp_port_rejected(self):
        """Negative contrast: core importing cdp_port or godot fails verify_neutral_import_graph loudly."""
        mutant_cdp_src = "from wender.perceive import cdp_port\n"
        with self.assertRaises(ValueError) as ctx:
            verify_neutral_import_graph(mutant_cdp_src)
        self.assertIn("cdp_port", str(ctx.exception))

        mutant_godot_src = "import wender.perceive.godot\n"
        with self.assertRaises(ValueError) as ctx2:
            verify_neutral_import_graph(mutant_godot_src)
        self.assertIn("godot", str(ctx2.exception))

        with self.assertRaises(ImportError):
            import cdp_port  # noqa: F401


class TestGateGJ02ResetFencesOldGeneration(unittest.TestCase):
    """gate G J0 2 requires Reset fences old generation.

    Negative contrast: same handle reused after reset.
    Positive control: fresh handle plus verified initial digest.
    """

    def setUp(self):
        self.evidence = LauncherEvidence(
            source_kind="cdp",
            tree=True,
            frame=True,
            t2=True,
            reset=True,
            observer=True,
            binding_id="bind-cdp-01",
        )
        self.initial_handle = Handle(kind="cdp_tab", canonical_id="tab-1", generation=1)

        def tree_prov(h, ep):
            frame_ref = _make_dummy_frame_ref(h, ep)
            return Observation(
                handle=h,
                epoch=ep,
                frame_seq=0,
                dpr_ppm=1000000,
                state=_make_dummy_state(),
                frame=frame_ref,
            )

        self.adapter = NeutralEpisodeAdapter(
            self.evidence,
            initial_handle=self.initial_handle,
            tree_provider=tree_prov,
        )

    def test_positive_control_fresh_handle_plus_verified_initial_digest(self):
        """Reset returns verified initial_state_sha256 and fresh handle (generation 2) can be observed."""
        desc_state = _make_dummy_state()
        self.assertEqual(schema.validate_state(desc_state), [])
        desc = {"state": desc_state, "schema": schema.SCHEMA_ID}
        expected_sha = hashlib.sha256(schema.dumps(desc_state).encode("utf-8")).hexdigest()

        reset_result = self.adapter.reset(seed=123, descriptor=desc, checkpoint=None)
        self.assertIsInstance(reset_result, ResetResult)
        self.assertEqual(reset_result.initial_state_sha256, expected_sha)
        self.assertEqual(reset_result.schema_id, schema.SCHEMA_ID)
        self.assertEqual(reset_result.build_id, "build-123-2")

        fresh_handle = self.adapter.handle
        self.assertEqual(fresh_handle.generation, 2)
        self.assertEqual(fresh_handle.kind, "cdp_tab")

        obs = self.adapter.observe_tree(fresh_handle, epoch=1)
        self.assertEqual(obs.handle, fresh_handle)
        self.assertEqual(obs.epoch, 1)

    def test_negative_contrast_same_handle_reused_after_reset_fails(self):
        """Negative contrast: reusing the old handle from generation 1 after reset raises FencedGenerationError."""
        old_handle = self.adapter.handle
        self.assertEqual(old_handle.generation, 1)

        desc = {"state": _make_dummy_state()}
        self.adapter.reset(seed=456, descriptor=desc)

        self.assertEqual(self.adapter.generation, 2)

        with self.assertRaises(FencedGenerationError) as ctx:
            self.adapter.observe_tree(old_handle, epoch=1)
        self.assertIn("Generation fence violation", str(ctx.exception))

    def test_monotonic_generation_fencing_across_multiple_resets(self):
        """Every reset monotonically increments generation starting at 1 and fences all prior handles."""
        handles = [self.adapter.handle]
        self.assertEqual(handles[0].generation, 1)
        for s in range(1, 4):
            desc = {"state": _make_dummy_state()}
            self.adapter.reset(seed=s, descriptor=desc)
            handles.append(self.adapter.handle)

        self.assertEqual(self.adapter.generation, 4)
        current = self.adapter.handle
        self.assertEqual(current.generation, 4)

        for stale in handles[:3]:
            with self.assertRaises(FencedGenerationError):
                self.adapter.observe_tree(stale, epoch=1)

        obs = self.adapter.observe_tree(current, epoch=1)
        self.assertEqual(obs.handle.generation, 4)


class TestGateGJ04CapabilityTruthPreserved(unittest.TestCase):
    """gate G J0 4 requires Capability truth is preserved.

    Negative contrast: unavailable capture returns success.
    Positive control: unavailable yields explicit unsupported.
    Missing required capabilities raise ValueError before any injected I/O.
    """

    def test_positive_control_unavailable_capture_yields_explicit_unsupported(self):
        """When capture is unavailable, query yields UnsupportedEvidence and observe_frame_digest raises CapabilityError before I/O."""
        io_calls = []
        evidence_no_frame = LauncherEvidence(
            source_kind="cdp",
            tree=True,
            frame=False,
            t2=True,
            reset=True,
            observer=True,
        )
        adapter = NeutralEpisodeAdapter(
            evidence_no_frame,
            frame_provider=lambda obs: io_calls.append(obs),
        )
        self.assertEqual(adapter.handle.kind, "cdp_tab")
        self.assertEqual(adapter.handle.generation, 1)

        res = adapter.query_capability("frame")
        self.assertIsInstance(res, UnsupportedEvidence)
        self.assertEqual(res.capability, "frame")
        self.assertIn("not supported", res.reason)

        obs = Observation(
            handle=adapter.handle,
            epoch=0,
            frame_seq=0,
            dpr_ppm=1000000,
            state=_make_dummy_state(),
            frame=_make_dummy_frame_ref(adapter.handle),
        )
        with self.assertRaises(CapabilityError) as ctx:
            adapter.observe_frame_digest(obs)
        self.assertIsInstance(ctx.exception, ValueError)
        self.assertIsInstance(ctx.exception.evidence, UnsupportedEvidence)
        self.assertEqual(ctx.exception.evidence.capability, "frame")
        self.assertEqual(io_calls, [], "Injected frame_provider must never run when frame capability is False")

    def test_negative_contrast_unavailable_capture_rejects_spurious_success(self):
        """Negative contrast: an adapter cannot claim success or invoke injected I/O for an unavailable capability."""
        io_calls = []
        evidence_no_t2 = LauncherEvidence(
            source_kind="godot",
            tree=True,
            frame=True,
            t2=False,
            reset=True,
            observer=True,
        )
        adapter = NeutralEpisodeAdapter(
            evidence_no_t2,
            t2_executor=lambda cmds, ctx: io_calls.append((cmds, ctx)) or [],
        )
        self.assertEqual(adapter.handle.kind, "godot_window")
        self.assertEqual(adapter.handle.generation, 1)

        cmd = make_pause_command(tick=1, seq=1)
        with self.assertRaises(ValueError):
            adapter.submit_t2_batch([cmd], trusted_context="ctx-1")
        self.assertEqual(io_calls, [], "Injected t2_executor must never run when t2 capability is False")

    def test_all_capabilities_checked_before_injected_io(self):
        """Missing tree, frame, t2, observer, or reset capability raises ValueError before any injected I/O."""
        io_log = []
        disabled_all = LauncherEvidence(
            source_kind="godot",
            tree=False,
            frame=False,
            t2=False,
            reset=False,
            observer=False,
        )
        adapter = NeutralEpisodeAdapter(
            disabled_all,
            tree_provider=lambda h, ep: io_log.append("tree"),
            frame_provider=lambda obs: io_log.append("frame"),
            t2_executor=lambda cmds, ctx: io_log.append("t2"),
            outcomes_reader=lambda: io_log.append("observer"),
            reset_handler=lambda s, d, c: io_log.append("reset"),
        )

        with self.assertRaises(CapabilityError):
            adapter.observe_tree(adapter.handle, epoch=0)
        with self.assertRaises(CapabilityError):
            adapter.observe_frame_digest(None)  # type: ignore[arg-type]
        with self.assertRaises(CapabilityError):
            adapter.submit_t2_batch([make_pause_command(tick=1, seq=1)], trusted_context="ctx")
        with self.assertRaises(CapabilityError):
            adapter.read_outcomes()
        with self.assertRaises(CapabilityError):
            adapter.reset(seed=1, descriptor={"state": _make_dummy_state()})

        self.assertEqual(io_log, [], "Zero injected I/O callables may execute when capabilities are False")


class TestInvariantsAndHoldingsContracts(unittest.TestCase):
    """Invariants INV J0 1 - INV J0 4 and Holdings specification contracts."""

    def test_inv_j0_1_backend_free_protocol_conformance(self):
        """NeutralEpisodeAdapter adheres to runtime EpisodeAdapter Protocol with supported handle kind and generation 1."""
        evidence = LauncherEvidence(
            source_kind="godot",
            tree=True,
            frame=True,
            t2=True,
            reset=True,
            observer=True,
        )
        adapter = NeutralEpisodeAdapter(evidence)
        self.assertIsInstance(adapter, EpisodeAdapter)
        self.assertEqual(adapter.handle.kind, "godot_window")
        self.assertEqual(adapter.handle.generation, 1)

    def test_inv_j0_3_write_spy_rejects_pixel_and_receipt_leakage(self):
        """WriteSpy rejects raw pixel arrays, large volatile buffers, and raw hook receipts."""
        spy = WriteSpy()

        meta = ApprovedFrameMetadata(
            digest="a" * 64,
            width=1920,
            height=1080,
            epoch=1,
            frame_seq=4,
            dpr_ppm=1000000,
            source_kind="godot",
        )
        spy.inspect_and_record(meta)
        self.assertEqual(len(spy.recorded_entries), 1)

        with self.assertRaises(ValueError) as ctx:
            spy.inspect_and_record({"pixels": np.zeros((10, 10, 3), dtype=np.uint8)})
        self.assertIn("leakage rejected", str(ctx.exception).lower())

        for bad_key in ("rgb", "raw_pixels", "hook_receipt", "capture_receipt", "tree_dump", "checkpoint_bytes"):
            with self.assertRaises(ValueError):
                spy.inspect_and_record({bad_key: "leaked_volatile_content"})

        with self.assertRaises(ValueError):
            spy.inspect_and_record({"raw_buf": b"\x00" * 1024})

    def test_inv_j0_4_launcher_evidence_establishes_capabilities_rejects_self_assertion(self):
        """Launcher evidence establishes capabilities; adapter self-assertion raises ValueError."""
        with self.assertRaises(ValueError) as ctx:
            capabilities_from_launcher(None)
        self.assertIn("adapter self-assertion is forbidden", str(ctx.exception))

        with self.assertRaises(ValueError):
            capabilities_from_launcher({"tree": True, "frame": True})

        le = LauncherEvidence(
            source_kind="cdp",
            tree=True,
            frame=True,
            t2=True,
            reset=True,
            observer=True,
            binding_id="bind-test",
        )
        caps = capabilities_from_launcher(le)
        self.assertIsInstance(caps, Capabilities)
        self.assertEqual(caps.binding_id, "bind-test")
        self.assertEqual(caps.source_kind, "cdp")

        # Passing an already-constructed Capabilities object directly as self-assertion is rejected
        with self.assertRaises(ValueError):
            capabilities_from_launcher(caps)  # type: ignore[arg-type]

    def test_holdings_pause_command_and_closed_t2_submission(self):
        """Closed T2: pause command has tick, seq, cmd set to pause and empty args; returns Receipts with explicit reason."""
        evidence = LauncherEvidence(
            source_kind="godot",
            tree=True,
            frame=True,
            t2=True,
            reset=True,
            observer=True,
        )
        adapter = NeutralEpisodeAdapter(evidence)

        cmd = make_pause_command(tick=100, seq=5)
        self.assertEqual(cmd.tick, 100)
        self.assertEqual(cmd.seq, 5)
        self.assertEqual(cmd.cmd, "pause")
        self.assertEqual(cmd.args, {})

        receipts = adapter.submit_t2_batch([cmd], trusted_context="ctx-sec-1")
        self.assertEqual(len(receipts), 1)
        receipt = receipts[0]
        self.assertIsInstance(receipt, Receipt)
        self.assertEqual(receipt.action_id, "cmd-5")
        self.assertEqual(receipt.status, "applied")
        self.assertIsNone(receipt.reason)
        self.assertEqual(receipt.handle.kind, "godot_window")
        self.assertEqual(receipt.handle.generation, 1)

    def test_holdings_outcomes_records_contract(self):
        """Outcomes have seq, requested_tick, applied_tick, order_seq_id, status, reason, state_sha256."""
        evidence = LauncherEvidence(
            source_kind="godot",
            tree=True,
            frame=True,
            t2=True,
            reset=True,
            observer=True,
        )
        adapter = NeutralEpisodeAdapter(evidence)

        dummy_sha = "f" * 64
        outcome = Outcome(
            seq=1,
            requested_tick=50,
            applied_tick=50,
            order_seq_id=1,
            status="success",
            reason=None,
            state_sha256=dummy_sha,
        )
        adapter.record_outcome(outcome)

        outcomes = adapter.read_outcomes()
        self.assertEqual(len(outcomes), 1)
        self.assertEqual(outcomes[0].seq, 1)
        self.assertEqual(outcomes[0].requested_tick, 50)
        self.assertEqual(outcomes[0].applied_tick, 50)
        self.assertEqual(outcomes[0].order_seq_id, 1)
        self.assertEqual(outcomes[0].status, "success")
        self.assertIsNone(outcomes[0].reason)
        self.assertEqual(outcomes[0].state_sha256, dummy_sha)

    def test_observe_frame_digest_approved_metadata(self):
        """observe_frame_digest returns ApprovedFrameMetadata with digest and dimensions."""
        evidence = LauncherEvidence(
            source_kind="cdp",
            tree=True,
            frame=True,
            t2=True,
            reset=True,
            observer=True,
        )
        adapter = NeutralEpisodeAdapter(evidence)
        frame_ref = _make_dummy_frame_ref(adapter.handle, epoch=2, seq=7)
        obs = Observation(
            handle=adapter.handle,
            epoch=2,
            frame_seq=7,
            dpr_ppm=1000000,
            state=_make_dummy_state(),
            frame=frame_ref,
        )

        approved = adapter.observe_frame_digest(obs)
        self.assertIsInstance(approved, ApprovedFrameMetadata)
        self.assertEqual(approved.digest, frame_ref.digest)
        self.assertEqual(approved.width, 100)
        self.assertEqual(approved.height, 100)
        self.assertEqual(approved.epoch, 2)
        self.assertEqual(approved.frame_seq, 7)
        self.assertEqual(approved.dpr_ppm, 1000000)
        self.assertEqual(approved.source_kind, "cdp")


class TestSourcePolicyFixtures(unittest.TestCase):
    """Exercise both source policies through fixtures without relabeling one as the other."""

    def test_cdp_source_policy_fixture(self):
        """CDP source policy fixture with cdp_tab handle (generation 1) and CDP source kind."""
        cdp_evidence = LauncherEvidence(
            source_kind="cdp",
            tree=True,
            frame=True,
            t2=True,
            reset=True,
            observer=True,
            binding_id="cdp-runner-01",
        )
        handle = Handle(kind="cdp_tab", canonical_id="tab-42", generation=1)
        adapter = NeutralEpisodeAdapter(
            cdp_evidence,
            initial_handle=handle,
            tree_provider=lambda h, ep: Observation(
                handle=h,
                epoch=ep,
                frame_seq=0,
                dpr_ppm=1000000,
                state=_make_dummy_state(kind="cdp_tab"),
                frame=_make_dummy_frame_ref(h, ep),
            ),
        )

        self.assertEqual(adapter.capabilities.source_kind, "cdp")
        self.assertEqual(adapter.handle.kind, "cdp_tab")
        self.assertEqual(adapter.handle.generation, 1)
        obs = adapter.observe_tree(handle, epoch=1)
        self.assertEqual(obs.handle.kind, "cdp_tab")
        self.assertEqual(obs.state["source"]["kind"], "cdp")

    def test_godot_source_policy_fixture(self):
        """Godot source policy fixture with godot_window handle (generation 1) and Godot source kind."""
        godot_evidence = LauncherEvidence(
            source_kind="godot",
            tree=True,
            frame=True,
            t2=True,
            reset=True,
            observer=True,
            binding_id="godot-holdings-01",
        )
        handle = Handle(kind="godot_window", canonical_id="godot-main-01", generation=1)
        adapter = NeutralEpisodeAdapter(
            godot_evidence,
            initial_handle=handle,
            tree_provider=lambda h, ep: Observation(
                handle=h,
                epoch=ep,
                frame_seq=0,
                dpr_ppm=1000000,
                state=_make_dummy_state(kind="godot_window"),
                frame=_make_dummy_frame_ref(h, ep),
            ),
        )

        self.assertEqual(adapter.capabilities.source_kind, "godot")
        self.assertEqual(adapter.handle.kind, "godot_window")
        self.assertEqual(adapter.handle.generation, 1)
        obs = adapter.observe_tree(handle, epoch=1)
        self.assertEqual(obs.handle.kind, "godot_window")
        self.assertEqual(obs.state["source"]["kind"], "godot")


class TestPrerequisitesAndWaveContracts(unittest.TestCase):
    """Prerequisites contracts: Handle, Observation, FrameRef, Receipt, StreamSpec, StreamToken."""

    def test_canonical_contracts_unchanged(self):
        """Prerequisite records retain canonical fields and enforce strict session_registry invariants."""
        contracts = verify_prerequisite_contracts()
        sr = contracts["session_registry"]
        self.assertIs(Handle, sr.Handle)
        self.assertIs(Observation, sr.Observation)
        self.assertIs(FrameRef, sr.FrameRef)
        self.assertIs(Receipt, sr.Receipt)
        self.assertIs(StreamSpec, sr.StreamSpec)
        self.assertIs(StreamToken, sr.StreamToken)
        self.assertEqual(SUPPORTED_HANDLE_KINDS, sr.SUPPORTED_KINDS)

        h = Handle(kind="x11_window", canonical_id="win-99", generation=2)
        self.assertEqual((h.kind, h.canonical_id, h.generation), ("x11_window", "win-99", 2))

        required_quota = min_stream_quota_bytes(640, 480, 4)
        self.assertEqual(required_quota, 640 * 480 * 3 * 4)
        spec = make_stream_spec(width=640, height=480, slots=4, quota_bytes=required_quota)
        self.assertEqual((spec.width, spec.height, spec.slots, spec.quota_bytes), (640, 480, 4, 3686400))

        tok = StreamToken(handle=h, epoch=2)
        self.assertEqual(tok.epoch, 2)

        rcpt = make_receipt(action_id="act-1", handle=h, status="applied", reason="ok")
        self.assertEqual((rcpt.action_id, rcpt.handle, rcpt.status, rcpt.reason), ("act-1", h, "applied", "ok"))

    def test_missing_prerequisite_fails_loudly_ausente(self):
        """Lazy import of real prerequisite providers fails loudly as AUSENTE when absent."""
        with mock.patch.dict(sys.modules, {"wender.perceive.session_registry": None}):
            with self.assertRaises(AusenteError) as ctx:
                require_prerequisite_provider(
                    "wender.perceive.session_registry",
                    ("Handle", "Observation", "FrameRef", "Receipt", "StreamSpec", "StreamToken"),
                )
            self.assertIn("AUSENTE", str(ctx.exception))
            self.assertIn("wender.perceive.session_registry", str(ctx.exception))

        with mock.patch.dict(sys.modules, {"wender.perceive.window_stream_bank": None}):
            with self.assertRaises(AusenteError) as ctx2:
                verify_prerequisite_contracts(require_window_stream_bank=True)
            self.assertIn("AUSENTE", str(ctx2.exception))
            self.assertIn("wender.perceive.window_stream_bank", str(ctx2.exception))


class TestNamedMutantDisagreements(unittest.TestCase):
    """Verify healthy fixtures pass and each named mutation / contract violation disagrees loudly."""

    def test_contract_violation_mutants_disagree(self):
        """All 4 previous host contract violations fail loudly against the canonical contracts."""
        # Mutant A: Handle with generation=0
        with self.assertRaises(ValueError):
            Handle(kind="cdp_tab", canonical_id="tab-1", generation=0)

        # Mutant B: Invented handle kind 'cdp_window'
        with self.assertRaises(ValueError):
            Handle(kind="cdp_window", canonical_id="tab-1", generation=1)
        with self.assertRaises(ValueError):
            default_handle_kind("cdp_window")
        self.assertEqual(default_handle_kind("cdp"), "cdp_tab")
        self.assertEqual(default_handle_kind("godot"), "godot_window")

        # Mutant C: StreamSpec quota 1,000,000 for four 640x480 RGB slots (needs 3,686,400)
        with self.assertRaises(ValueError):
            StreamSpec(width=640, height=480, slots=4, quota_bytes=1000000)
        with self.assertRaises(ValueError):
            make_stream_spec(width=640, height=480, slots=4, quota_bytes=1000000)

        # Mutant D: Receipt omitting required reason argument
        h = Handle(kind="cdp_tab", canonical_id="tab-1", generation=1)
        with self.assertRaises(TypeError):
            Receipt(action_id="cmd-1", handle=h, status="applied")  # type: ignore[call-arg]

    def test_gate_and_invariant_mutants_disagree_via_runner(self):
        """Mutating NeutralEpisodeAdapter to skip fencing or skip pre-IO capability check triggers suite failure."""
        orig_check_fence = NeutralEpisodeAdapter._check_generation_fence
        try:
            # Mutant G-J0-2: no-op generation fence allows stale handle reuse after reset
            NeutralEpisodeAdapter._check_generation_fence = lambda self, handle: None
            suite = unittest.defaultTestLoader.loadTestsFromTestCase(TestGateGJ02ResetFencesOldGeneration)
            result = unittest.TextTestRunner(stream=io.StringIO(), verbosity=0).run(suite)
            self.assertFalse(result.wasSuccessful(), "Mutant skipping generation fence must fail Gate G-J0-2")
        finally:
            NeutralEpisodeAdapter._check_generation_fence = orig_check_fence

        orig_req_cap = NeutralEpisodeAdapter._require_capability
        try:
            # Mutant G-J0-4: no-op capability check allows unavailable capture to invoke I/O or succeed
            NeutralEpisodeAdapter._require_capability = lambda self, cap_name: None
            suite = unittest.defaultTestLoader.loadTestsFromTestCase(TestGateGJ04CapabilityTruthPreserved)
            result = unittest.TextTestRunner(stream=io.StringIO(), verbosity=0).run(suite)
            self.assertFalse(result.wasSuccessful(), "Mutant skipping capability check must fail Gate G-J0-4")
        finally:
            NeutralEpisodeAdapter._require_capability = orig_req_cap


if __name__ == "__main__":
    unittest.main()
