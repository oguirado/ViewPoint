"""Focused unit, property, and concurrency tests for wender.perceive.pslot_adapter."""

from __future__ import annotations

import copy
import hashlib
import importlib
import random
import threading
import unittest

import numpy as np


def _load_modules():
    schema = importlib.import_module("wender.state.schema")
    session_registry = importlib.import_module("wender.perceive.session_registry")
    pslot_adapter = importlib.import_module("wender.perceive.pslot_adapter")
    return schema, session_registry, pslot_adapter


def _make_rgb(width: int = 80, height: int = 60, seed_byte: int = 17) -> np.ndarray:
    arr = np.zeros((height, width, 3), dtype=np.uint8)
    arr[:, :, 0] = np.uint8(seed_byte & 0xFF)
    arr[:, :, 1] = np.uint8((seed_byte * 3) & 0xFF)
    arr[:, :, 2] = np.uint8((seed_byte * 7) & 0xFF)
    return arr


def _make_frame_ref(session_registry, *, kind: str = "cdp_tab", canonical_id: str = "tab-1",
                    generation: int = 1, epoch: int = 2, frame_seq: int = 5,
                    width: int = 80, height: int = 60, seed_byte: int = 17):
    handle = session_registry.Handle(kind=kind, canonical_id=canonical_id, generation=generation)
    rgb = _make_rgb(width=width, height=height, seed_byte=seed_byte)
    return session_registry.FrameRef.from_rgb(handle, epoch, frame_seq, rgb)


def _valid_elements():
    return (
        {
            "element_key": "root-dialog",
            "role": "dialog",
            "text": "Settings   Modal",
            "box": (4, 4, 72, 52),
            "confidence_ppm": 950_000,
            "parent_key": None,
            "states": (("expanded", "true"),),
        },
        {
            "element_key": "save-btn",
            "role": "button",
            "text": "Save | Confirm",
            "box": (10, 12, 24, 14),
            "confidence_ppm": 910_000,
            "parent_key": "root-dialog",
            "states": (("pressed", "false"),),
        },
        {
            "element_key": "query-input",
            "role": "textbox",
            "text": "Search query",
            "box": (10, 30, 40, 16),
            "confidence_ppm": 880_000,
            "parent_key": "root-dialog",
            "states": (),
        },
    )


class TestJq3EPslotAdapter(unittest.TestCase):
    """Tests for PSLOT perception-provider contract adapter and sidecar binding."""

    def test_module_exports_and_Planned_pslot_ausente_contract(self):
        schema, session_registry, pa = _load_modules()
        self.assertIn("vision", schema.SOURCE_KINDS)
        self.assertNotIn("confidence", schema.NODE_KEYS)
        self.assertNotIn("confidence_ppm", schema.NODE_KEYS)
        self.assertNotIn("frame_sha256", schema.NODE_KEYS)

        expected_exports = {
            "AUSENTE",
            "CAPABILITY_MATRIX",
            "DEFAULT_PERCEIVER_MODEL",
            "ALT_PERCEIVER_MODEL",
            "OUT_OF_LOOP_JUDGE_MODEL",
            "UNCONFIGURED_MODEL",
            "ProviderIdentity",
            "ProviderElement",
            "ProviderResponse",
            "PerceptionProviderContract",
            "DeterministicFakeTransport",
            "DeterministicFakeProvider",
            "EvidenceSidecar",
            "AdaptedVisionBundle",
            "PslotProviderAdapter",
            "PslotAdapterError",
            "ProviderContractRefusalError",
            "ProviderIdentityError",
            "ProviderCapabilityRefusalError",
            "QuotaFloorRefusalError",
            "FrameBindingRefusalError",
            "GeometryRefusalError",
            "ElementValidationRefusalError",
            "AmbiguousElementRefusalError",
            "PrimaryEvidenceOverrideRefusalError",
            "adapt_provider_response",
            "verify_sidecar_binding",
            "pack_sidecar_binary",
            "unpack_sidecar_binary",
            "resolve_provider_capability",
            "check_judge_quota_floor",
        }
        self.assertTrue(expected_exports.issubset(set(pa.__all__)))
        for name in expected_exports:
            self.assertTrue(hasattr(pa, name), f"missing export {name}")
        self.assertIsNotNone(session_registry.Observation)

    def test_control_valid_adaptation_and_closed_state_sidecar_binding(self):
        schema, sr, pa = _load_modules()
        frame = _make_frame_ref(sr, epoch=3, frame_seq=7, width=80, height=60, seed_byte=23)
        transport = pa.DeterministicFakeTransport()
        provider = pa.DeterministicFakeProvider(
            identity=pa.ProviderIdentity(
                provider_id="muse-spark-provider",
                provider_version="1.3.0",
                model_id=pa.DEFAULT_PERCEIVER_MODEL,
                role=pa.NETWORK_ROLE_DEFAULT_PERCEIVER,
            ),
            elements=_valid_elements(),
            transport=transport,
        )
        adapter = pa.PslotProviderAdapter(provider, min_confidence_ppm=500_000)
        bundle = adapter.perceive_and_adapt(
            frame,
            expected_handle=frame.handle,
            expected_epoch=3,
            expected_frame_seq=7,
            dpr_ppm=2_000_000,
            url="https://example.test/app",
            title="Test App",
        )

        self.assertEqual(transport.call_count, 1)
        self.assertEqual(schema.validate_state(bundle.state), [])
        self.assertEqual(sorted(bundle.state.keys()), sorted(schema.TOP_KEYS))
        self.assertEqual(bundle.state["source"]["kind"], "vision")
        self.assertEqual(bundle.state["counts"], {"raw": 3, "nodes": 3, "actionable": 2})

        for node in bundle.state["nodes"]:
            self.assertEqual(sorted(node.keys()), sorted(schema.NODE_KEYS))
            self.assertNotIn("confidence", node)
            self.assertNotIn("confidence_ppm", node)
            self.assertNotIn("frame_sha256", node)

        # At dpr_ppm=2_000_000, device box (10, 12, 24, 14) maps to CSS [5, 6, 12, 7].
        save_node = bundle.state["nodes"][1]
        self.assertEqual(save_node["role"], "button")
        self.assertEqual(save_node["name"], r"Save \| Confirm")
        self.assertEqual(save_node["box"], [5, 6, 12, 7])
        self.assertEqual(save_node["parent"], bundle.state["nodes"][0]["id"])

        # Observation and sidecar provenance match exactly.
        self.assertIsInstance(bundle.observation, sr.Observation)
        self.assertEqual(bundle.observation.handle, frame.handle)
        self.assertEqual(bundle.observation.epoch, 3)
        self.assertEqual(bundle.observation.frame_seq, 7)
        self.assertEqual(bundle.observation.dpr_ppm, 2_000_000)
        self.assertTrue(
            pa.verify_sidecar_binding(
                bundle.sidecar,
                bundle.observation,
                frame=frame,
                expected_handle=frame.handle,
                expected_epoch=3,
                expected_frame_seq=7,
                expected_dpr_ppm=2_000_000,
            )
        )

        # Alternative perceiver Gemini 3.8 Flash also succeeds as a valid control.
        alt_provider = pa.DeterministicFakeProvider(
            identity=pa.ProviderIdentity(
                provider_id="gemini-flash-provider",
                provider_version="3.8.0",
                model_id=pa.ALT_PERCEIVER_MODEL,
                role=pa.NETWORK_ROLE_ALT_PERCEIVER,
            ),
            elements=_valid_elements(),
        )
        alt_bundle = pa.PslotProviderAdapter(alt_provider).perceive_and_adapt(frame)
        self.assertEqual(alt_bundle.sidecar.provider.model_id, pa.ALT_PERCEIVER_MODEL)

        # Ensure no raw RGB media payload bytes appear in receipts, state, or sidecar JSON.
        raw_rgb_bytes = np.asarray(frame.rgb).tobytes()
        receipt_json = schema.dumps(transport.calls_snapshot()).encode("utf-8")
        sidecar_json = schema.dumps(bundle.sidecar.to_dict()).encode("utf-8")
        state_json = schema.dumps(bundle.state).encode("utf-8")
        self.assertNotIn(raw_rgb_bytes, receipt_json)
        self.assertNotIn(raw_rgb_bytes, sidecar_json)
        self.assertNotIn(raw_rgb_bytes, state_json)
        self.assertNotIn(raw_rgb_bytes, bundle.sidecar.packed_zlib)

    def test_geometry_single_condition_negatives(self):
        _, sr, pa = _load_modules()
        frame = _make_frame_ref(sr, width=80, height=60)
        base_el = copy.deepcopy(_valid_elements()[0])

        # Control passes
        control_provider = pa.DeterministicFakeProvider(elements=(base_el,))
        pa.PslotProviderAdapter(control_provider).perceive_and_adapt(frame)

        bad_boxes = (
            (True, 4, 20, 20),     # bool x
            (4, False, 20, 20),    # bool y
            (4, 4, True, 20),      # bool w
            (4, 4, 20, True),      # bool h
            (4.0, 4, 20, 20),      # float x
            (4, 4, 20.5, 20),      # float w
            (-1, 4, 20, 20),       # negative x
            (4, -1, 20, 20),       # negative y
            (4, 4, 0, 20),         # zero w
            (4, 4, 20, 0),         # zero h
            (4, 4, -5, 20),        # negative w
            (61, 4, 20, 20),       # x + w = 81 > 80 (out of horizontal bounds)
            (4, 41, 20, 20),       # y + h = 61 > 60 (out of vertical bounds)
            (0, 0, 81, 60),        # width exceeds frame width by 1
            (0, 0, 80, 61),        # height exceeds frame height by 1
            (1, 2, 3),             # 3-tuple instead of 4-tuple
        )
        for bad_box in bad_boxes:
            mutated = dict(base_el, box=bad_box)
            with self.subTest(bad_box=bad_box):
                with self.assertRaises(pa.GeometryRefusalError):
                    prov = pa.DeterministicFakeProvider(elements=(mutated,))
                    pa.PslotProviderAdapter(prov).perceive_and_adapt(frame)

    def test_element_role_text_confidence_and_states_single_condition_negatives(self):
        _, sr, pa = _load_modules()
        frame = _make_frame_ref(sr, width=80, height=60)
        base_el = copy.deepcopy(_valid_elements()[1])
        base_el["parent_key"] = None

        # Control passes
        pa.PslotProviderAdapter(
            pa.DeterministicFakeProvider(elements=(base_el,)),
            min_confidence_ppm=800_000,
        ).perceive_and_adapt(frame)

        single_condition_mutations = (
            {"role": "unknown_widget_role"},
            {"role": ""},
            {"role": " button "},
            {"role": "button|injected"},
            {"role": True},
            {"text": None},
            {"text": 42},
            {"text": "bad\x00null"},
            {"text": "bad\x07control"},
            {"confidence_ppm": True},
            {"confidence_ppm": False},
            {"confidence_ppm": 0.91},
            {"confidence_ppm": -1},
            {"confidence_ppm": 1_000_001},
            {"confidence_ppm": "910000"},
            {"confidence_ppm": 799_999},  # below min_confidence_ppm=800_000
            {"states": (("unsupported_attr", "true"),)},
            {"states": (("pressed", "bad=val"),)},
            {"states": (("pressed", ""),)},
        )
        for mutation in single_condition_mutations:
            mutated = dict(base_el, **mutation)
            with self.subTest(mutation=mutation):
                with self.assertRaises(pa.ElementValidationRefusalError):
                    prov = pa.DeterministicFakeProvider(elements=(mutated,))
                    pa.PslotProviderAdapter(prov, min_confidence_ppm=800_000).perceive_and_adapt(frame)

    def test_frame_digest_handle_epoch_seq_and_dpr_single_condition_negatives(self):
        _, sr, pa = _load_modules()
        frame = _make_frame_ref(sr, kind="cdp_tab", canonical_id="tab-1", generation=1, epoch=4, frame_seq=9)
        elements = (_valid_elements()[0],)

        # Control passes
        control = pa.DeterministicFakeProvider(elements=elements)
        bundle = pa.PslotProviderAdapter(control).perceive_and_adapt(
            frame,
            expected_handle=frame.handle,
            expected_epoch=4,
            expected_frame_seq=9,
            dpr_ppm=1_000_000,
        )
        self.assertTrue(pa.verify_sidecar_binding(bundle.sidecar, bundle.observation, frame=frame))

        wrong_sha = ("0" if frame.digest[0] != "0" else "1") + frame.digest[1:]
        wrong_handle = sr.Handle("cdp_tab", "tab-other", 1)
        wrong_gen_handle = sr.Handle("cdp_tab", "tab-1", 2)

        for label, kwargs in (
            ("wrong_frame_sha256", {"override_frame_sha256": wrong_sha}),
            ("wrong_handle_id", {"override_handle": wrong_handle}),
            ("wrong_handle_generation", {"override_handle": wrong_gen_handle}),
            ("wrong_epoch", {"override_epoch": 5}),
            ("wrong_frame_seq", {"override_frame_seq": 10}),
            ("wrong_dpr_ppm", {"override_dpr_ppm": 2_000_000}),
        ):
            with self.subTest(case=label):
                prov = pa.DeterministicFakeProvider(elements=elements, **kwargs)
                with self.assertRaises(pa.FrameBindingRefusalError):
                    pa.PslotProviderAdapter(prov).perceive_and_adapt(frame, dpr_ppm=1_000_000)

        # Expected caller fence mismatches
        adapter = pa.PslotProviderAdapter(control)
        with self.assertRaises(pa.FrameBindingRefusalError):
            adapter.perceive_and_adapt(frame, expected_handle=wrong_handle)
        with self.assertRaises(pa.FrameBindingRefusalError):
            adapter.perceive_and_adapt(frame, expected_epoch=99)
        with self.assertRaises(pa.FrameBindingRefusalError):
            adapter.perceive_and_adapt(frame, expected_frame_seq=99)
        with self.assertRaises(pa.FrameBindingRefusalError):
            adapter.perceive_and_adapt(frame, dpr_ppm=True)

        # Sidecar verification negatives when bound against wrong frame or tampered binary
        other_frame = _make_frame_ref(sr, kind="cdp_tab", canonical_id="tab-1", generation=1,
                                      epoch=4, frame_seq=9, seed_byte=99)
        with self.assertRaises(pa.FrameBindingRefusalError):
            pa.verify_sidecar_binding(bundle.sidecar, bundle.observation, frame=other_frame)

        tampered_blob = bytes([bundle.sidecar.packed_zlib[0] ^ 0xFF]) + bundle.sidecar.packed_zlib[1:]
        with self.assertRaises(pa.FrameBindingRefusalError):
            pa.unpack_sidecar_binary(tampered_blob)

    def test_ambiguous_element_identities_single_condition_negatives(self):
        _, sr, pa = _load_modules()
        frame = _make_frame_ref(sr, width=80, height=60)

        # Control: two distinct buttons with identical role and text but different boxes succeed with ordinals 0, 1.
        distinct_buttons = (
            {
                "element_key": "btn-1",
                "role": "button",
                "text": "Submit",
                "box": (4, 4, 20, 10),
                "confidence_ppm": 900_000,
            },
            {
                "element_key": "btn-2",
                "role": "button",
                "text": "Submit",
                "box": (4, 20, 20, 10),
                "confidence_ppm": 900_000,
            },
        )
        bundle = pa.PslotProviderAdapter(
            pa.DeterministicFakeProvider(elements=distinct_buttons)
        ).perceive_and_adapt(frame)
        self.assertEqual([n["ordinal"] for n in bundle.state["nodes"]], [0, 1])
        self.assertNotEqual(bundle.state["nodes"][0]["id"], bundle.state["nodes"][1]["id"])

        ambiguous_cases = (
            # 1. Duplicate element_key
            (
                dict(distinct_buttons[0], element_key="dup-key"),
                dict(distinct_buttons[1], element_key="dup-key"),
            ),
            # 2. Identical (parent_key, role, normalized text, box) despite distinct element_key
            (
                dict(distinct_buttons[0], element_key="btn-a", box=(4, 4, 20, 10)),
                dict(distinct_buttons[1], element_key="btn-b", box=(4, 4, 20, 10)),
            ),
            # 3. Colliding explicit_node_id
            (
                dict(distinct_buttons[0], explicit_node_id="0123456789abcdef"),
                dict(distinct_buttons[1], explicit_node_id="0123456789abcdef"),
            ),
            # 4. Parent does not precede child
            (
                dict(distinct_buttons[0], element_key="child", parent_key="late-parent"),
                dict(distinct_buttons[1], element_key="late-parent", parent_key=None),
            ),
            # 5. Self-parenting element
            (
                dict(distinct_buttons[0], element_key="self-p", parent_key="self-p"),
            ),
            # 6. Conflicting element_key and id in dict
            (
                dict(distinct_buttons[0], element_key="k1", id="k2"),
            ),
            # 7. Conflicting text and name in dict
            (
                dict(distinct_buttons[0], text="Save", name="Cancel"),
            ),
        )
        for idx, elements in enumerate(ambiguous_cases):
            with self.subTest(case_index=idx):
                with self.assertRaises(pa.AmbiguousElementRefusalError):
                    prov = pa.DeterministicFakeProvider(elements=elements)
                    pa.PslotProviderAdapter(prov).perceive_and_adapt(frame)

    def test_primary_structured_evidence_precedence_and_contract_refusals(self):
        schema, sr, pa = _load_modules()
        frame = _make_frame_ref(sr, width=80, height=60)
        transport = pa.DeterministicFakeTransport()
        provider = pa.DeterministicFakeProvider(elements=_valid_elements(), transport=transport)
        adapter = pa.PslotProviderAdapter(provider)

        for primary_kind in ("cdp", "atspi", "godot", "bpy"):
            with self.subTest(primary_kind=primary_kind):
                with self.assertRaises(pa.PrimaryEvidenceOverrideRefusalError):
                    adapter.perceive_and_adapt(frame, primary_evidence_kind=primary_kind)
        self.assertEqual(transport.call_count, 0)

        primary_cdp_state = schema.build_state(
            [{"id": "0000000000000001", "role": "button", "name": "OK", "depth": 0, "ordinal": 0, "attrs": ()}],
            kind="cdp",
            url="https://example.test",
            title="Primary",
            ax_sha256="a" * 64,
            ax_stable_sha256="b" * 64,
            prune={"name": "prune_ax_v1", "version": 1},
            raw_count=1,
            actionable_roles={"button"},
        )
        with self.assertRaises(pa.PrimaryEvidenceOverrideRefusalError):
            adapter.perceive_and_adapt(frame, primary_state=primary_cdp_state)
        self.assertEqual(transport.call_count, 0)

        # Missing or AUSENTE provider contract blocks construction/dispatch
        with self.assertRaises(pa.ProviderContractRefusalError):
            pa.PslotProviderAdapter(None)
        with self.assertRaises(pa.ProviderContractRefusalError):
            pa.PslotProviderAdapter(pa.AUSENTE)
        with self.assertRaises(pa.ProviderContractRefusalError):
            pa.PslotProviderAdapter(object())

    def test_network_first_roles_capability_matrix_and_judge_quota_floor_refusal(self):
        _, sr, pa = _load_modules()
        frame = _make_frame_ref(sr, width=80, height=60)

        # DeepSeek V4.1 Flash is present only as not configured and refused.
        self.assertIn(pa.UNCONFIGURED_MODEL, pa.CAPABILITY_MATRIX)
        self.assertFalse(pa.CAPABILITY_MATRIX[pa.UNCONFIGURED_MODEL]["configured"])
        with self.assertRaises(pa.ProviderCapabilityRefusalError):
            pa.ProviderIdentity(
                provider_id="deepseek-prov",
                provider_version="4.1.0",
                model_id=pa.UNCONFIGURED_MODEL,
                role=pa.NETWORK_ROLE_NOT_CONFIGURED,
            )
        with self.assertRaises(pa.ProviderCapabilityRefusalError):
            pa.resolve_provider_capability(pa.UNCONFIGURED_MODEL)

        # Unknown capabilities, limits, and tariffs are AUSENTE (never 0) and require explicit operator config.
        default_cap = pa.resolve_provider_capability(pa.DEFAULT_PERCEIVER_MODEL)
        self.assertEqual(default_cap["capabilities"], pa.AUSENTE)
        self.assertEqual(default_cap["limits"], pa.AUSENTE)
        self.assertEqual(default_cap["tariffs"], pa.AUSENTE)
        with self.assertRaises(pa.ProviderCapabilityRefusalError):
            pa.resolve_provider_capability(pa.DEFAULT_PERCEIVER_MODEL, require_operational_limits=True)

        configured_cap = pa.resolve_provider_capability(
            pa.DEFAULT_PERCEIVER_MODEL,
            operator_config={
                "capabilities": {"max_elements": 64},
                "limits": {"rpm": 30},
                "tariffs": {"cost_micro_usd_per_call": 250},
            },
            require_operational_limits=True,
        )
        self.assertEqual(configured_cap["limits"]["rpm"], 30)

        # Out-of-loop judge Gemini 4 Argon enforces quota-floor refusal BEFORE transport.
        judge_id = pa.ProviderIdentity(
            provider_id="argon-judge",
            provider_version="4.0.0",
            model_id=pa.OUT_OF_LOOP_JUDGE_MODEL,
            role=pa.NETWORK_ROLE_OUT_OF_LOOP_JUDGE,
        )
        with self.assertRaises(pa.ProviderIdentityError):
            pa.DeterministicFakeProvider(identity=judge_id)

        unconfigured_judge_transport = pa.DeterministicFakeTransport(remaining_quota_ppm=500_000)
        with self.assertRaises(pa.QuotaFloorRefusalError):
            unconfigured_judge_transport.dispatch(judge_id, frame, dpr_ppm=1_000_000)
        self.assertEqual(unconfigured_judge_transport.call_count, 0)

        low_quota_transport = pa.DeterministicFakeTransport(
            operator_config={"quota_floor_ppm": 250_000},
            remaining_quota_ppm=249_999,
        )
        with self.assertRaises(pa.QuotaFloorRefusalError):
            low_quota_transport.dispatch(judge_id, frame, dpr_ppm=1_000_000)
        self.assertEqual(low_quota_transport.call_count, 0)

        passing_judge_transport = pa.DeterministicFakeTransport(
            operator_config={"quota_floor_ppm": 250_000},
            remaining_quota_ppm=250_000,
        )
        receipt = passing_judge_transport.dispatch(judge_id, frame, dpr_ppm=1_000_000)
        self.assertEqual(passing_judge_transport.call_count, 1)
        self.assertEqual(receipt["model_id"], pa.OUT_OF_LOOP_JUDGE_MODEL)

        # Local HTTP design seam with on-demand capped-unit hook (no network/socket evaluation dependency).
        seam = pa.LocalHttpSeamConfig(
            endpoint="http://127.0.0.1:18080/perceive",
            max_units=8,
            on_demand_hook=lambda units: units,
            enabled=True,
        )
        self.assertEqual(seam.invoke_capped_hook(5), 5)
        with self.assertRaises(pa.QuotaFloorRefusalError):
            seam.invoke_capped_hook(9)

    def test_seeded_bounded_synthetic_property_cases_and_minimal_counterexample(self):
        schema, sr, pa = _load_modules()
        seed = 20261020
        rng = random.Random(seed)
        frame = _make_frame_ref(sr, width=100, height=80, seed_byte=31)
        roles = sorted(pa.ACTIONABLE_VISION_ROLES)

        # 32 bounded valid synthetic property trials
        for trial in range(32):
            n_els = rng.randint(1, 6)
            elements = []
            for idx in range(n_els):
                x = rng.randint(0, 70)
                y = rng.randint(0, 50)
                w = rng.randint(1, 100 - x)
                h = rng.randint(1, 80 - y)
                parent_key = elements[0]["element_key"] if idx > 0 and rng.random() < 0.5 else None
                elements.append({
                    "element_key": f"syn-{trial}-{idx}",
                    "role": roles[rng.randrange(len(roles))],
                    "text": f"Item {trial} {idx}",
                    "box": (x, y, w, h),
                    "confidence_ppm": rng.randint(0, pa.CONFIDENCE_PPM_MAX),
                    "parent_key": parent_key,
                })
            prov = pa.DeterministicFakeProvider(elements=elements)
            bundle = pa.PslotProviderAdapter(prov).perceive_and_adapt(frame)
            self.assertEqual(schema.validate_state(bundle.state), [], f"seed={seed} trial={trial}")
            self.assertTrue(pa.verify_sidecar_binding(bundle.sidecar, bundle.observation, frame=frame))
            unpacked = pa.unpack_sidecar_binary(bundle.sidecar.packed_zlib)
            self.assertEqual(len(unpacked["items"]), n_els)

        # Shrink an invalid synthetic batch to its minimal 1-element counterexample and record seed + counterexample.
        invalid_batch = [
            {"element_key": "ok-1", "role": "button", "text": "A", "box": (0, 0, 10, 10), "confidence_ppm": 500_000},
            {"element_key": "bad-oob", "role": "button", "text": "B", "box": (95, 0, 10, 10), "confidence_ppm": 500_000},
            {"element_key": "ok-2", "role": "button", "text": "C", "box": (20, 0, 10, 10), "confidence_ppm": 500_000},
        ]
        minimal_counterexample = None
        for candidate in invalid_batch:
            try:
                pa.PslotProviderAdapter(pa.DeterministicFakeProvider(elements=(candidate,))).perceive_and_adapt(frame)
            except pa.GeometryRefusalError:
                minimal_counterexample = {"seed": seed, "counterexample": candidate}
                break
        self.assertIsNotNone(minimal_counterexample)
        self.assertEqual(minimal_counterexample["seed"], 20261020)
        self.assertEqual(minimal_counterexample["counterexample"]["element_key"], "bad-oob")

    def test_controlled_concurrency_isolation_and_no_thread_leaks(self):
        _, sr, pa = _load_modules()
        n_workers = 4
        barrier = threading.Barrier(n_workers, timeout=5.0)
        start_event = threading.Event()
        errors: list[BaseException] = []
        results: dict[int, object] = {}
        lock = threading.Lock()
        baseline_threads = threading.active_count()

        def worker(worker_idx: int) -> None:
            try:
                frame = _make_frame_ref(
                    sr,
                    kind="cdp_tab",
                    canonical_id=f"tab-{worker_idx}",
                    generation=worker_idx + 1,
                    epoch=worker_idx,
                    frame_seq=worker_idx * 10 + 1,
                    width=80,
                    height=60,
                    seed_byte=worker_idx + 40,
                )
                els = (
                    {
                        "element_key": f"btn-{worker_idx}",
                        "role": "button",
                        "text": f"Action {worker_idx}",
                        "box": (worker_idx * 2, worker_idx * 2, 20, 12),
                        "confidence_ppm": 800_000 + worker_idx * 1000,
                    },
                )
                def on_send(_prov, _frame):
                    barrier.wait(timeout=5.0)
                    if not start_event.wait(timeout=5.0):
                        raise TimeoutError("start_event timed out")

                transport = pa.DeterministicFakeTransport(on_send_hook=on_send)
                provider = pa.DeterministicFakeProvider(elements=els, transport=transport)
                adapter = pa.PslotProviderAdapter(provider)
                bundle = adapter.perceive_and_adapt(
                    frame,
                    expected_handle=frame.handle,
                    expected_epoch=frame.epoch,
                    expected_frame_seq=frame.frame_seq,
                )
                with lock:
                    results[worker_idx] = (frame, bundle)
            except BaseException as exc:  # propagate any failure to the main test thread
                with lock:
                    errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,), daemon=True) for i in range(n_workers)]
        for t in threads:
            t.start()
        start_event.set()
        for t in threads:
            t.join(timeout=5.0)
            self.assertFalse(t.is_alive(), "worker thread leaked")

        if errors:
            raise errors[0]

        self.assertEqual(len(results), n_workers)
        for i in range(n_workers):
            frame_i, bundle_i = results[i]
            self.assertEqual(bundle_i.sidecar.handle.canonical_id, f"tab-{i}")
            self.assertEqual(bundle_i.sidecar.frame_sha256, frame_i.digest)
            self.assertTrue(pa.verify_sidecar_binding(bundle_i.sidecar, bundle_i.observation, frame=frame_i))
        self.assertLessEqual(threading.active_count(), baseline_threads)


if __name__ == "__main__":
    unittest.main()
