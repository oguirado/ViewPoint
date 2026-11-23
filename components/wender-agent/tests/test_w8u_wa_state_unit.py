"""Unit and acceptance-gate tests for `wender.state.w8u_digest` (task w8u-wa-state).

Covers:
- Closed `wender.state/v1` contract validation and canonical UTF-8 `schema.dumps` SHA-256 without LF
- Positive control against independent `hashlib.sha256(schema.dumps(state).encode("utf-8"))`
- Mapping insertion-order permutation invariance vs genuine node mutation sensitivity
- Must-FAIL controls: float in any nesting, NaN, +/-Infinity, bool box/viewport/int fields,
  unknown base/node keys, `_perception` wrapper, invalid `source.kind`, non-empty validator error list,
  stale sidecar, and another target's frame
- Separate observation envelope identity, target descriptor validation, `SessionRegistry` epoch/generation
  fencing, injected structural frame evidence with revision-pinned `w8u-os-contract/v1`, and domain separation
  across STATE, UI, simulation, frame, sidecar, and binding digests
- Receipt `w8u.receipt/v1` T2 STATE and training evidence invariants, missing witnesses as null,
  `tick_valid=False -> tick=None`, pixel-required zero-frame indeterminate runs, STORAGE LAW via `DiskSinkSpy`,
  and VM-labeled deterministic clock benchmarking.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys
import unittest

import numpy as np

from tests.test_ax import fixture
from wender.perceive import ax
from wender.perceive import session_registry as sr
from wender.state import schema
from wender.state import w8u_digest


def make_valid_state(*, kind: str = "cdp") -> dict:
    """Build a real valid `wender.state/v1` fixture using `ax.prune(fixture())` and `schema.build_state`."""
    nodes = ax.prune(fixture())
    return schema.build_state(
        nodes,
        kind=kind,
        url="file:///app/index.html",
        title="Main Document",
        ax_sha256=ax.snapshot_sha(nodes),
        ax_stable_sha256=ax.snapshot_sha(nodes, False),
        prune={"name": ax.PRUNE_NAME, "version": 1},
        raw_count=8,
        boxes={nodes[2]["id"]: [10, 20, 120, 32]},
        viewport={"w": 1280, "h": 720, "scroll_x": 0, "scroll_y": 0, "doc_w": 1280, "doc_h": 720, "dpr_ppm": 1000000},
        actionable_roles=ax.ACTIONABLE_ROLES,
    )


def make_rgb_array(width: int = 6, height: int = 4, seed_byte: int = 17) -> np.ndarray:
    """Create a deterministic in-RAM H x W x 3 uint8 RGB array without touching disk."""
    total = width * height * 3
    raw = bytes(((seed_byte + i * 31) & 0xFF) for i in range(total))
    return np.frombuffer(raw, dtype=np.uint8).reshape((height, width, 3))


class DeterministicFakeClock:
    """Deterministic integer nanosecond clock for registry and VM benchmark tests."""
    def __init__(self, start_ns: int = 1_000_000_000, step_ns: int = 250_000) -> None:
        self.current_ns = start_ns
        self.step_ns = step_ns
        self.calls = 0

    def __call__(self) -> int:
        val = self.current_ns
        self.current_ns += self.step_ns
        self.calls += 1
        return val


class TestW8uWaStateUnit(unittest.TestCase):
    def test_wave1_core_imports_no_unmerged_sibling_w8u_modules(self) -> None:
        forbidden_prefixes = (
            "wender.perceive.w8u_",
            "wender.act.w8u_",
            "wender.remember.w8u_",
            "wender.learn.w8u_",
        )
        def check(modules):
            for mod_name in modules:
                for prefix in forbidden_prefixes:
                    self.assertFalse(mod_name.startswith(prefix), f"unexpected sibling W8U module imported: {mod_name}")
        # Inspect the core's own imports, independently of suite discovery order.
        root = Path(__file__).resolve().parents[1]
        script = ("import json, sys; sys.path.insert(0, " + repr(str(root)) + "); "
                  "from wender.state import schema, w8u_digest; "
                  "print(json.dumps(sorted(sys.modules)))")
        child = subprocess.run([sys.executable, "-I", "-B", "-c", script],
                               capture_output=True, text=True, check=True, timeout=30)
        check(json.loads(child.stdout))
        for prefix in forbidden_prefixes:
            with self.assertRaises(AssertionError):
                check([prefix + "negative_control"])

    def test_positive_control_real_state_and_independent_sha256_without_lf(self) -> None:
        for kind in schema.SOURCE_KINDS:
            with self.subTest(kind=kind):
                state = make_valid_state(kind=kind)
                self.assertEqual(schema.validate_state(state), [])
                self.assertEqual(w8u_digest.validate_closed_state(state), [])

                dumped = schema.dumps(state)
                self.assertNotIn("\n", dumped)
                self.assertNotIn("\r", dumped)
                expected_bytes = dumped.encode("utf-8")
                expected_sha = hashlib.sha256(expected_bytes).hexdigest()

                self.assertEqual(w8u_digest.canonical_state_bytes(state), expected_bytes)
                self.assertEqual(w8u_digest.validate_and_dump_state(state), dumped)
                self.assertEqual(w8u_digest.compute_state_digest(state), expected_sha)
                self.assertEqual(w8u_digest.state_digest(state), expected_sha)
                self.assertTrue(w8u_digest.verify_state_digest(state, expected_sha))

                with self.assertRaises(w8u_digest.InvalidStateContractError):
                    w8u_digest.verify_state_digest(state, "0" * 64)

    def test_mapping_insertion_order_permutation_preserves_digest_and_mutation_changes_it(self) -> None:
        base = make_valid_state()
        base_digest = w8u_digest.compute_state_digest(base)

        permuted = w8u_digest.permute_state_mapping_order(base)
        self.assertNotEqual(list(permuted.keys()), list(base.keys()))
        self.assertNotEqual(list(permuted["source"].keys()), list(base["source"].keys()))
        self.assertNotEqual(list(permuted["nodes"][0].keys()), list(base["nodes"][0].keys()))
        self.assertEqual(w8u_digest.validate_closed_state(permuted), [])
        self.assertEqual(w8u_digest.compute_state_digest(permuted), base_digest)
        self.assertEqual(w8u_digest.canonical_state_bytes(permuted), w8u_digest.canonical_state_bytes(base))

        mut_name = make_valid_state()
        mut_name["nodes"][2]["name"] = "Changed Button Label"
        self.assertEqual(schema.validate_state(mut_name), [])
        self.assertNotEqual(w8u_digest.compute_state_digest(mut_name), base_digest)

        mut_box = make_valid_state()
        mut_box["nodes"][2]["box"] = [11, 20, 120, 32]
        self.assertEqual(schema.validate_state(mut_box), [])
        self.assertNotEqual(w8u_digest.compute_state_digest(mut_box), base_digest)

        mut_states = make_valid_state()
        mut_states["nodes"][2]["states"] = {"focused": "true", "pressed": "true"}
        self.assertEqual(schema.validate_state(mut_states), [])
        self.assertNotEqual(w8u_digest.compute_state_digest(mut_states), base_digest)

        mut_ordinal = make_valid_state()
        mut_ordinal["nodes"][2]["ordinal"] = 7
        self.assertEqual(schema.validate_state(mut_ordinal), [])
        self.assertNotEqual(w8u_digest.compute_state_digest(mut_ordinal), base_digest)

    def test_must_fail_float_in_any_nesting_nan_and_infinity(self) -> None:
        cases = []

        s_vp = make_valid_state()
        s_vp["viewport"]["w"] = 1280.0
        cases.append(("viewport_float", s_vp, "float at $.viewport.w"))

        s_box = make_valid_state()
        s_box["nodes"][2]["box"] = [10, 20.5, 120, 32]
        cases.append(("box_float", s_box, "float at $.nodes[2].box[1]"))

        s_depth = make_valid_state()
        s_depth["nodes"][0]["depth"] = 0.0
        cases.append(("depth_float", s_depth, "float at $.nodes[0].depth"))

        s_nested = make_valid_state()
        s_nested["nodes"][1]["states"] = {"nested": ("a", [1, {"k": 0.125}])}
        cases.append(("deep_nested_float", s_nested, "float at $.nodes[1].states.nested[1][1].k"))

        s_key = make_valid_state()
        s_key["nodes"][1]["states"] = {1.5: "true"}
        cases.append(("float_dict_key", s_key, "float at $.nodes[1].states.<key>"))

        s_nan = make_valid_state()
        s_nan["viewport"]["scroll_y"] = float("nan")
        cases.append(("viewport_nan", s_nan, "NaN float at $.viewport.scroll_y"))

        s_box_nan = make_valid_state()
        s_box_nan["nodes"][2]["box"] = [float("nan"), 0, 10, 10]
        cases.append(("box_nan", s_box_nan, "NaN float at $.nodes[2].box[0]"))

        s_inf = make_valid_state()
        s_inf["viewport"]["doc_h"] = float("inf")
        cases.append(("viewport_inf", s_inf, "infinite float at $.viewport.doc_h"))

        for label, bad_state, expected_substr in cases:
            with self.subTest(case=label):
                errs = w8u_digest.validate_closed_state(bad_state)
                self.assertTrue(errs, f"expected errors for {label}")
                self.assertTrue(any(expected_substr in e for e in errs), f"expected {expected_substr!r} in {errs!r}")
                with self.assertRaises(w8u_digest.InvalidStateContractError) as ctx:
                    w8u_digest.compute_state_digest(bad_state)
                self.assertEqual(ctx.exception.code, w8u_digest.E_FLOAT)
                self.assertTrue(any(expected_substr in e for e in ctx.exception.errors))

    def test_must_fail_bool_box_and_bool_coordinates(self) -> None:
        s_bool_box = make_valid_state()
        s_bool_box["nodes"][2]["box"] = [True, 20, 120, 32]
        errs_box = w8u_digest.validate_closed_state(s_bool_box)
        self.assertIn("nodes[2].box must be null or [x,y,w,h] ints with w,h >= 0", errs_box)
        with self.assertRaises(w8u_digest.InvalidStateContractError) as ctx_box:
            w8u_digest.compute_state_digest(s_bool_box)
        self.assertIn("nodes[2].box must be null or [x,y,w,h] ints with w,h >= 0", ctx_box.exception.errors)

        s_bool_vp = make_valid_state()
        s_bool_vp["viewport"]["w"] = True
        errs_vp = w8u_digest.validate_closed_state(s_bool_vp)
        self.assertIn("viewport must be null or an object of integers", errs_vp)
        with self.assertRaises(w8u_digest.InvalidStateContractError) as ctx_vp:
            w8u_digest.compute_state_digest(s_bool_vp)
        self.assertIn("viewport must be null or an object of integers", ctx_vp.exception.errors)

        s_bool_depth = make_valid_state()
        s_bool_depth["nodes"][0]["depth"] = True
        errs_depth = w8u_digest.validate_closed_state(s_bool_depth)
        self.assertIn("nodes[0].depth must be a non-negative int", errs_depth)
        with self.assertRaises(w8u_digest.InvalidStateContractError):
            w8u_digest.compute_state_digest(s_bool_depth)

        s_bool_counts = make_valid_state()
        s_bool_counts["counts"]["raw"] = True
        errs_counts = w8u_digest.validate_closed_state(s_bool_counts)
        self.assertIn("counts inconsistent", errs_counts)
        with self.assertRaises(w8u_digest.InvalidStateContractError):
            w8u_digest.compute_state_digest(s_bool_counts)

    def test_must_fail_unknown_base_key_and_unknown_node_key(self) -> None:
        s_extra_top = make_valid_state()
        s_extra_top["unknown_top_key"] = 1
        errs_top = w8u_digest.validate_closed_state(s_extra_top)
        self.assertTrue(any("top-level keys" in e for e in errs_top))
        with self.assertRaises(w8u_digest.InvalidStateContractError):
            w8u_digest.compute_state_digest(s_extra_top)

        s_missing_top = make_valid_state()
        del s_missing_top["viewport"]
        errs_missing = w8u_digest.validate_closed_state(s_missing_top)
        self.assertTrue(any("top-level keys" in e for e in errs_missing))
        with self.assertRaises(w8u_digest.InvalidStateContractError):
            w8u_digest.compute_state_digest(s_missing_top)

        s_extra_node = make_valid_state()
        s_extra_node["nodes"][1]["unknown_node_key"] = "bad"
        errs_node = w8u_digest.validate_closed_state(s_extra_node)
        self.assertIn("nodes[1] keys invalid", errs_node)
        with self.assertRaises(w8u_digest.InvalidStateContractError) as ctx_node:
            w8u_digest.compute_state_digest(s_extra_node)
        self.assertIn("nodes[1] keys invalid", ctx_node.exception.errors)

        s_missing_node = make_valid_state()
        del s_missing_node["nodes"][0]["states"]
        errs_mnode = w8u_digest.validate_closed_state(s_missing_node)
        self.assertIn("nodes[0] keys invalid", errs_mnode)
        with self.assertRaises(w8u_digest.InvalidStateContractError):
            w8u_digest.compute_state_digest(s_missing_node)

    def test_must_fail_perception_wrapper_and_envelope_leakage(self) -> None:
        valid = make_valid_state()
        wrapped = {"_perception": valid}
        errs_wrapped = w8u_digest.validate_closed_state(wrapped)
        self.assertTrue(any("_perception wrapper forbidden" in e for e in errs_wrapped))
        with self.assertRaises(w8u_digest.InvalidStateContractError) as ctx_w:
            w8u_digest.compute_state_digest(wrapped)
        self.assertEqual(ctx_w.exception.code, w8u_digest.E_WRAPPER)

        embedded = make_valid_state()
        embedded["_perception"] = {"epoch": 0}
        with self.assertRaises(w8u_digest.InvalidStateContractError) as ctx_emb:
            w8u_digest.compute_state_digest(embedded)
        self.assertEqual(ctx_emb.exception.code, w8u_digest.E_WRAPPER)

        leaked = make_valid_state()
        leaked["frame_seq"] = 0
        errs_leaked = w8u_digest.validate_closed_state(leaked)
        self.assertTrue(any("observation envelope keys" in e for e in errs_leaked))
        with self.assertRaises(w8u_digest.InvalidStateContractError):
            w8u_digest.compute_state_digest(leaked)

    def test_must_fail_invalid_source_kind_and_nonempty_validator_errors(self) -> None:
        for bad_kind in ("unity", "x11", "xvfb", "", None, 99):
            with self.subTest(bad_kind=bad_kind):
                s = make_valid_state()
                s["source"]["kind"] = bad_kind
                errs = w8u_digest.validate_closed_state(s)
                self.assertIn(f"source.kind must be one of {schema.SOURCE_KINDS}", errs)
                with self.assertRaises(w8u_digest.InvalidStateContractError) as ctx:
                    w8u_digest.compute_state_digest(s)
                self.assertEqual(ctx.exception.code, w8u_digest.E_SOURCE_KIND)

        valid = make_valid_state()
        with self.assertRaises(w8u_digest.InvalidStateContractError) as ctx_custom:
            w8u_digest.compute_state_digest(valid, validator=lambda _: ["policy: synthetic refusal"])
        self.assertIn("policy: synthetic refusal", ctx_custom.exception.errors)

        with self.assertRaises(w8u_digest.InvalidStateContractError) as ctx_bad_val:
            w8u_digest.compute_state_digest(valid, validator=lambda _: "not-a-list")  # type: ignore[arg-type]
        self.assertEqual(ctx_bad_val.exception.code, w8u_digest.E_VALIDATOR)

    def test_bound_observation_digest_sidecar_and_domain_separation(self) -> None:
        clock = DeterministicFakeClock()
        registry = sr.SessionRegistry(clock=clock)
        handle = registry.register("xvfb_screen", ":99.0", object(), owned=True)
        rgb = make_rgb_array(width=6, height=4, seed_byte=42)
        frame = registry.admit_frame(handle, 0, rgb)
        state = make_valid_state(kind="vision")
        obs = registry.admit_observation(handle, frame, state=state, dpr_ppm=1000000)

        sidecar = w8u_digest.SidecarBasis(
            handle=handle, epoch=0, frame_seq=0, frame_digest=frame.digest,
            source_kind="vision", dpr_ppm=1000000,
            contract_revision=w8u_digest.W8U_OS_CONTRACT_REVISION,
            tick=12, tick_valid=True, capture_mono_ns=clock(),
        )
        sim_state = {"tick": 12, "phase": "playing", "entities": [{"id": 1, "hp": 100}]}

        with w8u_digest.DiskSinkSpy() as spy:
            bound = w8u_digest.bind_observation_digest(
                obs, sidecar, simulation_state=sim_state,
                expected_handle=handle, expected_epoch=0, expected_frame_seq=0, registry=registry,
            )
            self.assertEqual(spy.write_attempts, [])

        expected_state_sha = hashlib.sha256(schema.dumps(state).encode("utf-8")).hexdigest()
        self.assertEqual(bound.state_sha256, expected_state_sha)
        self.assertEqual(bound.frame_sha256, frame.digest)
        self.assertIsNotNone(bound.simulation_state_sha256)

        all_digests = [
            bound.state_sha256, bound.ui_domain_sha256, bound.frame_sha256,
            bound.frame_domain_sha256, bound.simulation_state_sha256,
            bound.sidecar_sha256, bound.binding_sha256,
        ]
        self.assertEqual(len(all_digests), len(set(all_digests)))

        with self.assertRaises(w8u_digest.DomainSeparationError) as ctx_dom:
            w8u_digest.compute_simulation_state_digest(state)
        self.assertEqual(ctx_dom.exception.code, w8u_digest.E_DOMAIN_COLLISION)

    def test_must_fail_stale_sidecar_and_another_targets_frame(self) -> None:
        clock = DeterministicFakeClock()
        registry = sr.SessionRegistry(clock=clock)
        h1 = registry.register("cdp_tab", "tab-alpha", object(), owned=True)
        h2 = registry.register("cdp_tab", "tab-beta", object(), owned=True)

        rgb1 = make_rgb_array(width=4, height=4, seed_byte=1)
        rgb2 = make_rgb_array(width=4, height=4, seed_byte=99)
        frame1 = registry.admit_frame(h1, 0, rgb1)
        frame2 = registry.admit_frame(h2, 0, rgb2)

        state = make_valid_state(kind="cdp")
        obs1 = registry.admit_observation(h1, frame1, state=state, dpr_ppm=1000000)
        valid_sidecar = {
            "handle": h1, "epoch": 0, "frame_seq": 0,
            "frame_digest": frame1.digest, "source_kind": "cdp", "dpr_ppm": 1000000,
        }

        with self.assertRaises(w8u_digest.SidecarStaleError) as ctx_se:
            w8u_digest.bind_observation_digest(obs1, dict(valid_sidecar, epoch=1), registry=registry)
        self.assertEqual(ctx_se.exception.code, w8u_digest.E_STALE_SIDECAR)
        self.assertIn("stale sidecar", str(ctx_se.exception))

        with self.assertRaises(w8u_digest.SidecarStaleError) as ctx_ss:
            w8u_digest.bind_observation_digest(obs1, dict(valid_sidecar, frame_seq=1), registry=registry)
        self.assertEqual(ctx_ss.exception.code, w8u_digest.E_STALE_SIDECAR)

        with self.assertRaises(w8u_digest.SidecarStaleError) as ctx_sd:
            w8u_digest.bind_observation_digest(obs1, dict(valid_sidecar, frame_digest=frame2.digest), registry=registry)
        self.assertEqual(ctx_sd.exception.code, w8u_digest.E_STALE_SIDECAR)

        with self.assertRaises(w8u_digest.SidecarStaleError) as ctx_sk:
            w8u_digest.bind_observation_digest(obs1, dict(valid_sidecar, source_kind="atspi"), registry=registry)
        self.assertEqual(ctx_sk.exception.code, w8u_digest.E_STALE_SIDECAR)

        foreign_obs_dict = {
            "handle": h1, "epoch": 0, "frame_seq": 0,
            "dpr_ppm": 1000000, "state": state, "frame": frame2,
        }
        with self.assertRaises(w8u_digest.ForeignFrameError) as ctx_ff:
            w8u_digest.bind_observation_digest(foreign_obs_dict, valid_sidecar, registry=registry)
        self.assertEqual(ctx_ff.exception.code, w8u_digest.E_FOREIGN_FRAME)
        self.assertIn("another target's frame", str(ctx_ff.exception))

        with self.assertRaises(w8u_digest.ForeignFrameError) as ctx_fsc:
            w8u_digest.bind_observation_digest(obs1, dict(valid_sidecar, handle=h2), registry=registry)
        self.assertEqual(ctx_fsc.exception.code, w8u_digest.E_FOREIGN_FRAME)

        registry.advance_epoch(h1)
        with self.assertRaises(w8u_digest.ObservationBindingError) as ctx_ep:
            w8u_digest.bind_observation_digest(obs1, valid_sidecar, registry=registry)
        self.assertEqual(ctx_ep.exception.code, w8u_digest.E_EPOCH)

    def test_injected_structural_frame_evidence_and_trust_vectors(self) -> None:
        h = schema.Handle("xvfb_screen", ":55.0", 1)
        rgb = make_rgb_array(width=4, height=3, seed_byte=9)
        digest = hashlib.sha256(rgb.tobytes()).hexdigest()
        frame_dict = {
            "handle": h, "epoch": 0, "frame_seq": 0, "width": 4, "height": 3,
            "rgb": rgb, "digest": digest, "contract_revision": w8u_digest.W8U_OS_CONTRACT_REVISION,
            "pair_verified": True, "xwd_common_subset_ok": True, "tick": None, "tick_valid": False,
        }
        trust_events: list[str] = []

        def accept_vector(f: object) -> list[str]:
            trust_events.append("accepted")
            return []

        def reject_vector(f: object) -> list[str]:
            trust_events.append("rejected")
            return ["xwd pair canonical mismatch in injected vector"]

        ev = w8u_digest.validate_structural_frame_evidence(
            frame_dict, expected_contract_revision=w8u_digest.W8U_OS_CONTRACT_REVISION, trust_check=accept_vector,
        )
        self.assertEqual(ev.digest, digest)
        self.assertEqual(trust_events, ["accepted"])

        with self.assertRaises(w8u_digest.ObservationBindingError) as ctx_rej:
            w8u_digest.validate_structural_frame_evidence(
                frame_dict, expected_contract_revision=w8u_digest.W8U_OS_CONTRACT_REVISION, trust_check=reject_vector,
            )
        self.assertEqual(ctx_rej.exception.code, w8u_digest.E_FRAME_EVIDENCE)
        self.assertIn("xwd pair canonical mismatch in injected vector", ctx_rej.exception.errors)
        self.assertEqual(trust_events, ["accepted", "rejected"])

        with self.assertRaises(w8u_digest.ObservationBindingError) as ctx_rev:
            w8u_digest.validate_structural_frame_evidence(dict(frame_dict, contract_revision="w8u-os-contract/unpinned"))
        self.assertEqual(ctx_rev.exception.code, w8u_digest.E_FRAME_EVIDENCE)

        with self.assertRaises(w8u_digest.ObservationBindingError):
            w8u_digest.validate_structural_frame_evidence(dict(frame_dict, pair_verified=False))
        with self.assertRaises(w8u_digest.ObservationBindingError):
            w8u_digest.validate_structural_frame_evidence(dict(frame_dict, xwd_common_subset_ok=False))

    def test_receipt_t2_state_training_evidence_and_disk_sink_spy(self) -> None:
        clock = DeterministicFakeClock()
        registry = sr.SessionRegistry(clock=clock)
        handle = registry.register("godot_window", "holdings-t2-main", object(), owned=True)
        rgb = make_rgb_array(width=8, height=6, seed_byte=77)
        frame = registry.admit_frame(handle, 0, rgb)
        state = make_valid_state(kind="godot")
        obs = registry.admit_observation(handle, frame, state=state, dpr_ppm=1000000)

        sidecar = w8u_digest.SidecarBasis(
            handle=handle, epoch=0, frame_seq=0, frame_digest=frame.digest,
            source_kind="godot", dpr_ppm=1000000, tick=5, tick_valid=True, capture_mono_ns=clock(),
        )
        sim_state = {"tick": 5, "cash_minor": 250000, "holdings": [{"ticker": "ACME", "qty": 10}]}

        with w8u_digest.DiskSinkSpy() as spy:
            bound = w8u_digest.bind_observation_digest(obs, sidecar, simulation_state=sim_state, registry=registry)
            frame_ev = w8u_digest.StructuralFrameEvidence(
                handle=handle, epoch=0, frame_seq=0, width=8, height=6, digest=frame.digest,
                capture_mono_ns=clock(), tick=5, tick_valid=True, binding_sha256=bound.binding_sha256,
            )
            fmeta = w8u_digest.build_frame_metadata(frame_ev)
            self.assertEqual(sorted(fmeta.keys()), sorted(w8u_digest.FRAME_METADATA_KEYS))

            receipt = w8u_digest.build_receipt_t2_state_evidence(
                run_id="run-w8u-001", lease_id="lease-81c00410abd5fc69",
                target_handle=handle, epoch=0, bound_final_state=bound,
                simulation_state=sim_state, t2_trace_events=[{"tick": 5, "event": "order_filled"}],
                t2_terminal_record={"tick": 5, "terminal": True}, t2_ticks=5,
                frames_captured=1, first_frame=fmeta, last_frame=fmeta,
                ui_actions_count=1, admitted_actions=1,
            )
            self.assertEqual(sorted(receipt.keys()), sorted(w8u_digest.RECEIPT_KEYS))
            self.assertEqual(receipt["final_state_sha256"], bound.state_sha256)
            self.assertEqual(receipt["simulation_state_sha256"], bound.simulation_state_sha256)
            self.assertNotEqual(receipt["final_state_sha256"], receipt["simulation_state_sha256"])

            training_ev = w8u_digest.build_training_state_evidence(
                obs, sidecar, simulation_state=sim_state, t2_tick=5, t2_tick_valid=True,
            )
            self.assertEqual(w8u_digest.validate_training_state_evidence(training_ev), [])
            self.assertTrue(training_ev["metadata_only"])
            self.assertEqual(spy.write_attempts, [])

            with self.assertRaises(PermissionError):
                open("/tmp/forbidden_frame_dump.raw", "wb")
            self.assertEqual(len(spy.write_attempts), 1)

        bad_fmeta = dict(fmeta, tick_valid=False, tick=0)
        with self.assertRaises(w8u_digest.StateDigestError):
            w8u_digest.validate_frame_metadata(bad_fmeta)

        zero_receipt = w8u_digest.build_receipt_t2_state_evidence(
            run_id="run-zero-001", lease_id="lease-81c00410abd5fc69",
            target_handle=handle, epoch=0, frames_captured=0, metadata_only=False,
        )
        self.assertIsNone(zero_receipt["first_frame"])
        self.assertIsNone(zero_receipt["last_frame"])
        self.assertIsNone(zero_receipt["final_state_sha256"])
        self.assertEqual(zero_receipt["ui_gate_verdict"], "indeterminate")

    def test_vm_digest_benchmark_labels_and_ausente_host_metric(self) -> None:
        clock = DeterministicFakeClock(start_ns=5_000_000, step_ns=120_000)
        state = make_valid_state()
        bench = w8u_digest.benchmark_state_digest_vm(state, clock=clock, iterations=3)
        self.assertEqual(bench["digest"], w8u_digest.compute_state_digest(state))
        self.assertEqual(bench["iterations"], 3)
        self.assertEqual(bench["elapsed_ns"], 120_000)
        self.assertEqual(bench["per_digest_ns"], 40_000)
        self.assertEqual(bench["timing_label"], "VM")
        self.assertEqual(bench["host_state_digest_ns"], w8u_digest.AUSENTE)
        self.assertEqual(bench["limits_status"], "TARGET")
        self.assertEqual(clock.calls, 2)


if __name__ == "__main__":
    unittest.main()
