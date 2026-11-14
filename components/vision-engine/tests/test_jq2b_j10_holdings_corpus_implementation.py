"""Ordinary unit tests for eval.v2eval.holdings_corpus (jq2b-j10-holdings-corpus).

Exercises the real public API of `eval.v2eval.holdings_corpus` and
`eval.v2x.per_window_detector` with independent expected outputs across:
- Gate G J10 1: Fixed-seed regeneration, per-lane SplitMix64 seed-tag isolation
  under lane permutation/reordering (INV-J10-1), and pinned provenance
  verification for seed, generator_sha256, and environment (INV-J10-2),
  contrasted against negative schedule-coupled/unpinned mutants.
- Gate G J10 2: Isolated vs interleaved per-lane operation totals, outer GM/FL
  ContextVar preservation and restoration on plugin exceptions (INV-J10-3),
  null retention for missing counters, and independent intent-authored expected
  labels including sparse unchanged baseline labels (INV-J10-4), contrasted
  against negative shared-global/prediction-copied mutants.
- Complete 11 case families: static_control, localized_change,
  integer_translation, periodic_flicker_veto, sparse_support, mixed_history,
  skipped_frame, resize_epoch, caller_mutation, plugin_exception, and
  cache_eviction.
- Ceiling enforcement before allocation, explicit malformed seed/fault
  rejection, no caller alias retention, source-neutral Holdings T2/observation
  boundary (Godot and fake-CDP DOM/AX fixtures, relabeled Godot tree rejection,
  pause/new_game schema, rejected attempts advancing trusted bookkeeping without
  simulation mutation, AUSENTE prerequisite blocking), and PixelPersistenceSpy.
"""
from __future__ import annotations

import copy
import sys
import unittest

import numpy as np

from eval.gui_synth.rng import SplitMix64
from eval.v2eval import holdings_corpus as hc
from eval.v2eval.holdings_corpus import (
    ALGORITHM_ID,
    AUSENTE,
    CASE_FAMILIES,
    CLASS_CHANGED,
    CLASS_FLICKER,
    CLASS_MOVED,
    CLASS_UNCHANGED,
    CONDUCTOR_ADMISSION_BINDING,
    ELECTRON_BROKERED_PUBLISHER,
    EXECUTION_WAVE,
    EXTERNAL_T2_FIELDS,
    FAKE_CGROUP_CPU_MAX,
    FAKE_CGROUP_MEMORY_MAX,
    FAKE_PROCFS_MEMINFO,
    MANIFEST_SCHEMA,
    MAX_FRAMES,
    MAX_HEIGHT,
    MAX_TOTAL_PIXELS,
    MAX_WIDTH,
    MIN_DIMENSION,
    NEW_GAME_ARG_FIELDS,
    TIMING_DOMAIN,
    CaseSpec,
    CorpusCeilingError,
    CorpusInputError,
    Handle,
    MalformedFaultError,
    MalformedSeedError,
    MissingPrerequisiteError,
    PixelPersistenceError,
    PixelPersistenceSpy,
    ProvenanceMismatchError,
    SourceBoundaryError,
    SourceNeutralBoundary,
    build_complete_case_family_specs,
    build_corpus_manifest,
    capture_environment_pin,
    check_host_admission,
    compute_dependency_digest,
    compute_generator_sha256,
    derive_frame_seed,
    derive_lane_seed,
    evaluate_gate_g_j10_1,
    evaluate_gate_g_j10_2,
    generate_case,
    make_fake_cdp_observation,
    make_holdings_t2_command,
    make_neutral_godot_observation,
    preserve_operation_contexts,
    run_corpus_schedule,
    validate_holdings_t2_record,
    validate_source_observation,
    verify_regeneration,
)
from eval.v2x import fl_blinkprior, fl_v3, gm_bands_hv
from eval.v2x.per_window_detector import DetectorInputError, PerWindowDetector


class _RecordingRNGFactory:
    """Spy wrapper around SplitMix64 recording every requested seed initialization."""

    def __init__(self) -> None:
        self.init_seeds: list[int] = []

    def __call__(self, seed: int) -> SplitMix64:
        self.init_seeds.append(seed)
        return SplitMix64(seed)


class TestHoldingsCorpusImplementation(unittest.TestCase):
    """Comprehensive verification of eval.v2eval.holdings_corpus contracts."""

    def test_sealed_splitmix64_import_and_no_adapter_imports(self) -> None:
        """Verify SplitMix64 is imported from sealed eval.gui_synth.rng and no Godot/CDP adapters are imported."""
        self.assertIs(hc.SplitMix64, SplitMix64)
        for mod_name in tuple(sys.modules.keys()):
            self.assertFalse(
                mod_name.startswith("vadapt.godot") or mod_name.startswith("wender"),
                f"forbidden adapter module imported: {mod_name}",
            )
        self.assertEqual(ELECTRON_BROKERED_PUBLISHER, AUSENTE)
        self.assertEqual(CONDUCTOR_ADMISSION_BINDING, AUSENTE)
        self.assertEqual(EXECUTION_WAVE, 1)
        self.assertEqual(TIMING_DOMAIN, "VM")
        self.assertIn("SwapTotal:             0 kB", FAKE_PROCFS_MEMINFO)
        self.assertTrue(FAKE_CGROUP_MEMORY_MAX.strip().isdigit())
        self.assertIn(" ", FAKE_CGROUP_CPU_MAX.strip())

    def test_geometry_and_count_ceilings_enforced_before_allocation(self) -> None:
        """Verify declared finite geometry and frame count ceilings reject before allocating arrays."""
        invalid_specs = [
            {"case_id": "too_wide", "width": MAX_WIDTH + 16, "height": 64, "frames": 2},
            {"case_id": "too_tall", "width": 64, "height": MAX_HEIGHT + 16, "frames": 2},
            {"case_id": "too_many_frames", "width": 64, "height": 64, "frames": MAX_FRAMES + 1},
            {"case_id": "too_small_w", "width": MIN_DIMENSION - 1, "height": 64, "frames": 2},
            {"case_id": "too_small_h", "width": 64, "height": MIN_DIMENSION - 1, "frames": 2},
            {"case_id": "zero_frames", "width": 64, "height": 64, "frames": 0},
            {"case_id": "bool_width", "width": True, "height": 64, "frames": 2},
            {"case_id": "float_height", "width": 64, "height": 64.0, "frames": 2},
            {"case_id": "pixel_budget_exceeded", "width": MAX_WIDTH, "height": MAX_HEIGHT, "frames": 65},
        ]
        for bad in invalid_specs:
            with self.subTest(case_id=bad["case_id"]):
                with self.assertRaises(CorpusCeilingError):
                    generate_case(bad)

        self.assertEqual(MAX_TOTAL_PIXELS, 1920 * 1088 * 64)

    def test_malformed_seed_and_fault_rejection(self) -> None:
        """Verify malformed seeds, rng_factory, and faults are rejected with explicit error types."""
        bad_seeds = [True, False, -1, 1.5, "123", None, (1 << 64)]
        for bad_seed in bad_seeds:
            with self.subTest(bad_seed=repr(bad_seed)):
                with self.assertRaises(MalformedSeedError):
                    derive_lane_seed(0, bad_seed)  # type: ignore[arg-type]
                with self.assertRaises(MalformedSeedError):
                    derive_lane_seed(bad_seed, 1)  # type: ignore[arg-type]
                with self.assertRaises(MalformedSeedError):
                    generate_case({"case_id": "bad_seed", "seed_tag": bad_seed})

        with self.assertRaises(MalformedSeedError):
            generate_case({"case_id": "bad_rng"}, rng_factory="not_callable")  # type: ignore[arg-type]

        with self.assertRaises(MalformedSeedError):
            generate_case({"case_id": "bad_rng_obj"}, rng_factory=lambda s: object())

        bad_faults = [
            "skip_frame",
            [{"kind": "nonexistent_fault"}],
            [{"kind": ""}],
            [12345],
            [{"kind": "skip_frame", "at_step": -1}],
            [{"kind": "skip_frame", "skip": 0}],
        ]
        for bf in bad_faults:
            with self.subTest(bad_fault=repr(bf)):
                with self.assertRaises(MalformedFaultError):
                    generate_case({"case_id": "bad_fault", "faults": bf})

    def test_no_caller_alias_retention_and_owned_ram_frames(self) -> None:
        """Verify caller spec and returned frame mutations never alias internal corpus state."""
        mutable_fault_dict = {"kind": "caller_mutation"}
        raw_spec = {
            "case_id": "alias_check",
            "family": "caller_mutation",
            "seed_tag": 77,
            "width": 64,
            "height": 64,
            "frames": 4,
            "faults": [mutable_fault_dict],
        }
        gcase = generate_case(raw_spec, master_seed=99)

        # Mutate caller input spec after generate_case
        raw_spec["case_id"] = "mutated_outside"
        raw_spec["seed_tag"] = 9999
        mutable_fault_dict["kind"] = "corrupted_fault"
        self.assertEqual(gcase.case_id, "alias_check")
        self.assertEqual(gcase.seed_tag, 77)
        self.assertEqual(gcase.spec.faults[0]["kind"], "caller_mutation")

        # Unpack frames and expected_labels via public iterable protocol
        frames_1, labels_1 = gcase
        self.assertEqual(len(frames_1), 4)
        self.assertEqual(len(labels_1), 4)

        orig_sha0 = gcase.frame_metadata[0].raw_sha256
        self.assertEqual(frames_1[0].dtype, np.uint8)
        self.assertTrue(frames_1[0].flags.owndata)
        self.assertTrue(frames_1[0].flags.c_contiguous)

        # Mutate caller's returned frame array in-place
        frames_1[0][:] = 255
        frames_2 = gcase.frames
        self.assertFalse(np.all(frames_2[0] == 255))
        import hashlib
        self.assertEqual(
            hashlib.sha256(frames_2[0].tobytes(order="C")).hexdigest(),
            orig_sha0,
        )

        # Labels are read-only intent arrays
        with self.assertRaises(ValueError):
            labels_1[0].cls[0] = 6

    def test_gate_g_j10_1_and_inv_j10_1_lane_permutation_and_rng_isolation(self) -> None:
        """Gate G J10 1 & INV-J10-1: Prove healthy control isolates RNG, history, and cache under lane reordering while negative contrast fails."""
        gate_res = evaluate_gate_g_j10_1(master_seed=20261020)
        self.assertTrue(gate_res["healthy_control_passed"])
        self.assertTrue(gate_res["negative_contrast_detected"])
        self.assertTrue(gate_res["differ"])

        rng_spy = _RecordingRNGFactory()
        spec_1 = CaseSpec(
            case_id="perm_lane_1",
            family="integer_translation",
            seed_tag=501,
            width=64,
            height=64,
            frames=5,
            handle=Handle("godot_window", "perm_1", 1),
            dx=4,
        )
        spec_2 = CaseSpec(
            case_id="perm_lane_2",
            family="periodic_flicker_veto",
            seed_tag=502,
            width=64,
            height=64,
            frames=6,
            handle=Handle("cdp_tab", "perm_2", 1),
            dx=4,
        )
        spec_3 = CaseSpec(
            case_id="perm_lane_3",
            family="sparse_support",
            seed_tag=503,
            width=64,
            height=64,
            frames=5,
            handle=Handle("x11_window", "perm_3", 1),
            dx=1,
        )

        # Forward generation vs reversed generation vs sibling-added generation
        c1_fwd = generate_case(spec_1, rng_factory=rng_spy, master_seed=777)
        c2_fwd = generate_case(spec_2, rng_factory=rng_spy, master_seed=777)
        self.assertGreater(len(rng_spy.init_seeds), 0)

        c3_rev = generate_case(spec_3, rng_factory=SplitMix64, master_seed=777)
        c2_rev = generate_case(spec_2, rng_factory=SplitMix64, master_seed=777)
        c1_rev = generate_case(spec_1, rng_factory=SplitMix64, master_seed=777)

        for f_a, f_b in zip(c1_fwd.frames, c1_rev.frames):
            np.testing.assert_array_equal(f_a, f_b)
        for f_a, f_b in zip(c2_fwd.frames, c2_rev.frames):
            np.testing.assert_array_equal(f_a, f_b)

        # Solo vs reversed vs interleaved detector runs must yield identical claims, history, cache, and totals
        solo_1 = run_corpus_schedule([c1_fwd])
        solo_2 = run_corpus_schedule([c2_fwd])
        solo_3 = run_corpus_schedule([c3_rev])

        interleaved_sched = []
        max_len = max(len(c1_fwd.steps), len(c2_fwd.steps), len(c3_rev.steps))
        for step_i in range(max_len):
            for c_i, c_obj in enumerate((c3_rev, c2_rev, c1_rev)):
                if step_i < len(c_obj.steps):
                    interleaved_sched.append((c_i, step_i))

        interleaved = run_corpus_schedule([c3_rev, c2_rev, c1_rev], schedule=interleaved_sched)

        for solo_rep, h in ((solo_1, spec_1.handle), (solo_2, spec_2.handle), (solo_3, spec_3.handle)):
            self.assertEqual(interleaved.per_lane_totals[h], solo_rep.per_lane_totals[h])
            self.assertEqual(interleaved.per_lane_digests[h], solo_rep.per_lane_digests[h])
            for st_int, st_solo in zip(interleaved.per_lane_steps[h], solo_rep.per_lane_steps[h]):
                self.assertEqual(st_int.cache_size_after, st_solo.cache_size_after)
                self.assertEqual(st_int.result.settled, st_solo.result.settled)
                np.testing.assert_array_equal(st_int.result.claims["cls"], st_solo.result.claims["cls"])
                np.testing.assert_array_equal(st_int.result.claims["mv_dx"], st_solo.result.claims["mv_dx"])
                np.testing.assert_array_equal(st_int.result.claims["mv_dy"], st_solo.result.claims["mv_dy"])

    def test_inv_j10_2_manifest_provenance_and_regeneration_contract(self) -> None:
        """INV-J10-2: Manifest pins schema, generator_sha256, algorithm_id, master_seed, environment, lanes, cases, frames."""
        specs = build_complete_case_family_specs()[:4]
        manifest = build_corpus_manifest(specs, master_seed=123456)
        m_dict = manifest.to_dict()

        expected_top_keys = {
            "schema",
            "generator_sha256",
            "algorithm_id",
            "master_seed",
            "environment",
            "lanes",
            "cases",
            "frames",
        }
        self.assertEqual(set(m_dict.keys()), expected_top_keys)
        self.assertEqual(manifest["schema"], MANIFEST_SCHEMA)
        self.assertEqual(manifest["algorithm_id"], ALGORITHM_ID)
        self.assertEqual(manifest["master_seed"], 123456)
        self.assertEqual(len(manifest["generator_sha256"]), 64)
        self.assertEqual(manifest["generator_sha256"], compute_generator_sha256())

        env = manifest["environment"]
        self.assertEqual(set(env.keys()), {"python", "numpy", "platform", "dependency_digest"})
        self.assertEqual(env, capture_environment_pin())
        self.assertEqual(len(env["dependency_digest"]), 64)
        self.assertEqual(env["dependency_digest"], compute_dependency_digest())

        for lane in manifest["lanes"]:
            self.assertEqual(set(lane.keys()), {"handle", "epoch_plan", "case_ids"})
            self.assertEqual(set(lane["handle"].keys()), {"kind", "canonical_id", "generation"})
            self.assertTrue(len(lane["epoch_plan"]) >= 1)
            self.assertTrue(len(lane["case_ids"]) >= 1)

        for case_entry in manifest["cases"]:
            self.assertTrue(
                {"case_id", "seed_tag", "geometry", "faults", "expected_labels"}.issubset(case_entry.keys())
            )
            self.assertTrue(len(case_entry["expected_labels"]) >= 1)

        for frame_entry in manifest["frames"]:
            self.assertEqual(
                set(frame_entry.keys()),
                {"case_id", "frame_seq", "pixel_decl_sha256", "raw_sha256", "normalized_sha256"},
            )
            self.assertEqual(len(frame_entry["pixel_decl_sha256"]), 64)
            self.assertEqual(len(frame_entry["raw_sha256"]), 64)
            self.assertEqual(len(frame_entry["normalized_sha256"]), 64)

        # Optional normalized_sha256 can be None when omitted
        case_no_norm = generate_case(specs[0], master_seed=123456, include_normalized_sha=False)
        self.assertIsNone(case_no_norm.frame_metadata[0].normalized_sha256)

        # Positive control: pinned seed, generator, and environment verify cleanly
        ver_ok = verify_regeneration(manifest, specs)
        self.assertTrue(ver_ok["verified"])

        # Negative contrasts: mismatched seed, generator_sha256, or environment pin must raise ProvenanceMismatchError
        with self.assertRaises(ProvenanceMismatchError):
            verify_regeneration(manifest, specs, master_seed=999999)

        with self.assertRaises(ProvenanceMismatchError):
            verify_regeneration(manifest, specs, generator_sha256="f" * 64)

        for env_key in ("python", "numpy", "platform", "dependency_digest"):
            bad_env = copy.deepcopy(env)
            bad_env[env_key] = "mismatched_pin"
            with self.assertRaises(ProvenanceMismatchError):
                verify_regeneration(manifest, specs, environment=bad_env)

            ausente_env = copy.deepcopy(env)
            ausente_env[env_key] = AUSENTE
            with self.assertRaises(ProvenanceMismatchError):
                verify_regeneration(manifest, specs, environment=ausente_env)

    def test_gate_g_j10_2_and_inv_j10_3_contextvar_preservation_and_null_counters(self) -> None:
        """Gate G J10 2 & INV-J10-3: Preserve outer GM/FL ContextVars, restore on exception, and keep missing counters null."""
        gate_res = evaluate_gate_g_j10_2(master_seed=20261020)
        self.assertTrue(gate_res["healthy_control_passed"])
        self.assertTrue(gate_res["negative_contrast_detected"])
        self.assertTrue(gate_res["differ"])

        # Set outer ContextVar values before running normal and exception cases
        gm_token = gm_bands_hv._ops.set(4242)
        diag_token = gm_bands_hv._diagnostics.set({"outer_marker": 99})
        fl_token = fl_blinkprior._ops.set(1313)
        try:
            spec_ok = CaseSpec(
                case_id="ctx_ok",
                family="integer_translation",
                seed_tag=601,
                width=64,
                height=64,
                frames=3,
                handle=Handle("godot_window", "ctx_lane_ok", 1),
                dx=4,
            )
            spec_exc = CaseSpec(
                case_id="ctx_exc",
                family="plugin_exception",
                seed_tag=602,
                width=64,
                height=64,
                frames=4,
                handle=Handle("cdp_tab", "ctx_lane_exc", 1),
                faults=({"kind": "plugin_exception", "at_step": 1},),
            )
            case_ok = generate_case(spec_ok, master_seed=55)
            case_exc = generate_case(spec_exc, master_seed=55)

            rep = run_corpus_schedule([case_ok, case_exc], schedule=[(0, 0), (1, 0), (1, 1), (0, 1), (1, 2), (0, 2), (1, 3)])

            # Outer ContextVars must remain untouched despite inner GM/FL work and plugin exception!
            self.assertEqual(gm_bands_hv.OPS, 4242)
            self.assertEqual(gm_bands_hv.OPS_DETAIL, {"outer_marker": 99})
            self.assertEqual(fl_v3.OPS, 1313)

            # Verify exception step recorded RuntimeError and cleaned up only its own lane
            exc_steps = rep.per_lane_steps[spec_exc.handle]
            self.assertIsNone(exc_steps[0].error)
            self.assertEqual(exc_steps[1].error, "RuntimeError")
            self.assertIsNone(exc_steps[1].result)
            self.assertEqual(exc_steps[1].ops, {"gm": None, "fl": None})
            # Step 2 on exc lane recovers with a fresh stream (not settled)
            self.assertIsNone(exc_steps[2].error)
            self.assertFalse(exc_steps[2].result.settled)

            # Missing counters stay None (null), never converted to 0
            rep_missing = run_corpus_schedule([case_ok], missing_gm_counter=True, missing_fl_counter=True)
            for st in rep_missing.per_lane_steps[spec_ok.handle]:
                self.assertIsNone(st.ops["fl"])
            # Frame 1+ had GM motion, so missing GM counter must be None
            self.assertIsNone(rep_missing.per_lane_steps[spec_ok.handle][1].ops["gm"])
            self.assertEqual(rep_missing.per_lane_totals[spec_ok.handle], {"gm": None, "fl": None})
        finally:
            gm_bands_hv._ops.reset(gm_token)
            gm_bands_hv._diagnostics.reset(diag_token)
            fl_blinkprior._ops.reset(fl_token)

    def test_all_11_case_families_and_inv_j10_4_independent_expected_labels(self) -> None:
        """Verify all 11 case families, sparse unchanged baseline labels, and reset/gap isolation."""
        specs = build_complete_case_family_specs(master_Handle_prefix="all11")
        self.assertEqual(tuple(s.family for s in specs), CASE_FAMILIES)

        with PixelPersistenceSpy() as spy:
            cases = [generate_case(s, master_seed=2026) for s in specs]
            report = run_corpus_schedule(cases)
            self.assertEqual(len(spy.blocked_calls), 0)

        by_family = {c.spec.family: c for c in cases}

        # 1. Verify every case's independent intent labels match actual detector outcomes
        for gcase in cases:
            eval_steps = report.per_lane_steps[gcase.handle]
            self.assertEqual(len(eval_steps), len(gcase.expected_labels))
            for ev, exp in zip(eval_steps, gcase.expected_labels):
                if exp.expected_error is not None:
                    self.assertEqual(ev.error, exp.expected_error)
                    continue
                self.assertIsNone(ev.error)
                self.assertEqual(ev.result.settled, exp.settled)
                np.testing.assert_array_equal(ev.result.claims["cls"], exp.cls)
                np.testing.assert_array_equal(ev.result.claims["mv_dx"], exp.mv_dx)
                np.testing.assert_array_equal(ev.result.claims["mv_dy"], exp.mv_dy)

        # 2. Specific check on sparse_support: top row (ty==0) has unchanged baseline labels (0) preserved
        sparse_case = by_family["sparse_support"]
        sparse_lbl_1 = sparse_case.expected_labels[1]
        self.assertTrue(np.any(sparse_lbl_1.baseline_cls == CLASS_UNCHANGED))
        self.assertTrue(np.any(sparse_lbl_1.cls == CLASS_MOVED))
        unchanged_mask = sparse_lbl_1.baseline_cls == CLASS_UNCHANGED
        np.testing.assert_array_equal(sparse_lbl_1.cls[unchanged_mask], CLASS_UNCHANGED)
        np.testing.assert_array_equal(sparse_lbl_1.mv_dx[unchanged_mask], 0)

        # 3. Specific check on periodic_flicker_veto: frames 2,3 have FLICKER(4), frames 4,5 vetoed by real motion MOVED(3)
        flick_case = by_family["periodic_flicker_veto"]
        self.assertTrue(np.any(flick_case.expected_labels[2].cls == CLASS_FLICKER))
        self.assertTrue(np.any(flick_case.expected_labels[3].cls == CLASS_FLICKER))
        self.assertFalse(np.any(flick_case.expected_labels[4].cls == CLASS_FLICKER))
        self.assertTrue(np.any(flick_case.expected_labels[4].cls == CLASS_MOVED))

        # 4. Specific check on cache_eviction: 36 frames (35 transitions) capped at MAX_CACHE_PAIRS (32)
        evict_case = by_family["cache_eviction"]
        evict_steps = report.per_lane_steps[evict_case.handle]
        self.assertEqual(evict_steps[-1].cache_size_after, PerWindowDetector.MAX_CACHE_PAIRS)

        # 5. Specific check on skipped_frame & reset isolation: gap on one lane never resets sibling lane
        det = PerWindowDetector()
        static_a = generate_case(
            CaseSpec(
                case_id="reset_iso_a",
                family="static_control",
                seed_tag=801,
                width=64,
                height=64,
                frames=5,
                handle=Handle("godot_window", "iso_a", 1),
            ),
            master_seed=11,
        )
        skip_b = generate_case(
            CaseSpec(
                case_id="reset_iso_b",
                family="skipped_frame",
                seed_tag=802,
                width=64,
                height=64,
                frames=5,
                handle=Handle("cdp_tab", "iso_b", 1),
                faults=({"kind": "skip_frame", "at_step": 2, "skip": 4},),
            ),
            master_seed=11,
        )
        rep_iso = run_corpus_schedule(
            [static_a, skip_b],
            schedule=[(0, 0), (1, 0), (0, 1), (1, 1), (0, 2), (1, 2), (0, 3), (1, 3), (0, 4), (1, 4)],
            detector=det,
        )
        # static_a settles on step 3 (frame_seq 3) and stays settled on step 4 despite skip_b's gap at step 2!
        self.assertTrue(rep_iso.per_lane_steps[static_a.handle][3].result.settled)
        self.assertTrue(rep_iso.per_lane_steps[static_a.handle][4].result.settled)
        # skip_b had a gap at step 2, so its clean_run reset and step 3 is not settled
        self.assertFalse(rep_iso.per_lane_steps[skip_b.handle][3].result.settled)

        # Also verify stale epoch / geometry change without new epoch raises DetectorInputError on that lane only
        step0 = static_a.steps[0]
        with self.assertRaises(DetectorInputError):
            det.update(static_a.handle, step0.baseline_tiles, step0.rgb, epoch=0, frame_seq=10)

    def test_source_neutral_boundary_holdings_t2_and_ausente_guards(self) -> None:
        """Verify source-neutral observations, CDP relabeled-Godot rejection, Holdings T2 commands, and AUSENTE guards."""
        h_godot = Handle("godot_window", "holdings_main", 1)
        h_cdp = Handle("cdp_tab", "holdings_web", 1)

        obs_godot = make_neutral_godot_observation(h_godot, epoch=1, frame_seq=0, tick=15, country_id="ESP")
        obs_cdp = make_fake_cdp_observation(h_cdp, epoch=1, frame_seq=0)

        boundary = SourceNeutralBoundary()
        self.assertEqual(boundary.electron_brokered_publisher, AUSENTE)
        self.assertEqual(boundary.conductor_admission_binding, AUSENTE)

        boundary.inject_observation(obs_godot)
        boundary.inject_observation(obs_cdp)

        # Reject relabeled Godot tree masquerading as source.kind == 'cdp'
        relabeled_godot = {
            "handle": h_cdp,
            "epoch": 1,
            "frame_seq": 1,
            "frame_digest": "2" * 64,
            "source": {
                "kind": "cdp",
                "scene_tree": {"root": "Node2D"},
                "ax_tree": [{"backendDOMNodeId": 1, "role": "WebArea", "godot_class": "Control"}],
                "dom_nodes": [{"backendNodeId": 1, "nodeName": "DIV"}],
            },
        }
        with self.assertRaises(SourceBoundaryError):
            validate_source_observation(relabeled_godot)

        # Construct inline Holdings T2 records: pause (empty args) and new_game (country_id, difficulty, opening_mode, seed)
        cmd_pause = make_holdings_t2_command(tick=15, seq=1, cmd="pause", args={})
        self.assertEqual(tuple(cmd_pause.keys()), EXTERNAL_T2_FIELDS)
        self.assertEqual(cmd_pause["args"], {})

        cmd_new_game = make_holdings_t2_command(
            tick=16,
            seq=2,
            cmd="new_game",
            args={
                "country_id": "ARG",
                "difficulty": "hard",
                "opening_mode": "historical",
                "seed": 987654321,
            },
        )
        self.assertEqual(tuple(cmd_new_game.keys()), EXTERNAL_T2_FIELDS)
        self.assertEqual(tuple(cmd_new_game["args"].keys()), NEW_GAME_ARG_FIELDS)

        # Reject malformed T2 records (non-empty pause args, missing new_game fields, or embedded authority)
        with self.assertRaises(SourceBoundaryError):
            make_holdings_t2_command(tick=15, seq=1, cmd="pause", args={"unexpected": 1})
        with self.assertRaises(SourceBoundaryError):
            make_holdings_t2_command(tick=16, seq=2, cmd="new_game", args={"country_id": "ARG"})
        with self.assertRaises(SourceBoundaryError):
            validate_holdings_t2_record({"tick": 1, "seq": 1, "cmd": "pause", "args": {}, "authority": "spoofed"})

        # Admission vs economic acceptance:
        # 1. Accepted new_game mutates simulation state
        out_acc = boundary.submit_t2(h_godot, cmd_new_game, allow_economic_acceptance=True)
        self.assertTrue(out_acc["admitted"])
        self.assertTrue(out_acc["accepted"])
        snap_after_acc = boundary.lane_snapshot(h_godot)
        self.assertEqual(snap_after_acc["simulation_state"]["country_id"], "ARG")
        self.assertEqual(snap_after_acc["simulation_state"]["mutations"], 1)
        self.assertEqual(snap_after_acc["trusted_bookkeeping"]["admitted_attempts"], 1)
        self.assertEqual(snap_after_acc["trusted_bookkeeping"]["accepted_count"], 1)
        self.assertEqual(snap_after_acc["trusted_bookkeeping"]["rejected_count"], 0)

        # 2. Economically rejected attempt advances trusted bookkeeping without mutating simulation state
        cmd_rejected = make_holdings_t2_command(
            tick=20,
            seq=3,
            cmd="new_game",
            args={
                "country_id": "FRA",
                "difficulty": "easy",
                "opening_mode": "sandbox",
                "seed": 111,
            },
        )
        out_rej = boundary.submit_t2(
            h_godot,
            cmd_rejected,
            allow_economic_acceptance=False,
            rejection_reason="budget_locked",
        )
        self.assertTrue(out_rej["admitted"])
        self.assertFalse(out_rej["accepted"])
        self.assertEqual(out_rej["status"], "rejected")
        self.assertEqual(out_rej["reason"], "budget_locked")

        snap_after_rej = boundary.lane_snapshot(h_godot)
        # Simulation state unchanged!
        self.assertEqual(snap_after_rej["simulation_state"], snap_after_acc["simulation_state"])
        # Trusted bookkeeping advanced!
        self.assertEqual(snap_after_rej["trusted_bookkeeping"]["admitted_attempts"], 2)
        self.assertEqual(snap_after_rej["trusted_bookkeeping"]["accepted_count"], 1)
        self.assertEqual(snap_after_rej["trusted_bookkeeping"]["rejected_count"], 1)
        self.assertEqual(snap_after_rej["trusted_bookkeeping"]["last_seq"], 3)

        # Resetting h_godot does not reset h_cdp
        boundary.reset_lane(h_godot)
        self.assertEqual(boundary.lane_snapshot(h_godot)["observation_count"], 0)
        self.assertEqual(boundary.lane_snapshot(h_cdp)["observation_count"], 1)

        # Host admission blocks when conductor_binding is AUSENTE or owner gate receipts are missing/inferred
        with self.assertRaises(MissingPrerequisiteError):
            check_host_admission(
                {"G_M5_1": {"status": "PASS", "revision": "rev1"}},
                conductor_binding=AUSENTE,
            )
        with self.assertRaises(MissingPrerequisiteError):
            check_host_admission(
                {"G_M5_1": {"status": "PASS", "revision": "rev1", "inferred_consumer_label": True}},
                conductor_binding="bound-conductor-v1",
            )
        admitted_ok = check_host_admission(
            {"G_M5_1": {"status": "PASS", "revision": "rev1"}},
            expected_revision="rev1",
            conductor_binding="bound-conductor-v1",
        )
        self.assertTrue(admitted_ok["admitted"])

    def test_pixel_persistence_write_spy_blocks_forbidden_writes_without_execution(self) -> None:
        """Verify PixelPersistenceSpy rejects file writes, numpy.save, and screenshot APIs without writing."""
        frame = np.zeros((16, 16, 3), dtype=np.uint8)
        with PixelPersistenceSpy() as spy:
            with self.assertRaises(PixelPersistenceError):
                open("/tmp/forbidden_frame_dump.raw", "wb")
            with self.assertRaises(PixelPersistenceError):
                np.save("/tmp/forbidden_frame.npy", frame)
            with self.assertRaises(PixelPersistenceError):
                spy.save_screenshot("/tmp/forbidden_screen.png")
            with self.assertRaises(PixelPersistenceError):
                spy.capture_screenshot_to_file("/tmp/forbidden_cap.png")

        self.assertEqual(len(spy.blocked_calls), 4)
        self.assertEqual(
            [c["api"] for c in spy.blocked_calls],
            ["builtins.open", "numpy.save", "save_screenshot", "capture_screenshot_to_file"],
        )


if __name__ == "__main__":
    unittest.main()
