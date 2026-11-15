"""Offline J8 contrasts: exercise the REAL, public HoldingsAbstention API.

All fixtures are authored targets, not measurements from any host. Fake CDP,
brokered IPC, clocks, process text and frames stay in volatile test memory.
"""

import builtins
from copy import deepcopy
from dataclasses import FrozenInstanceError
import hashlib
import json
import unittest
from unittest.mock import patch

import numpy as np

from wender.perceive.holdings_abstention import (
    ABSTAIN, CORROBORATED, MISMATCH, FAILURE_CODES, HoldingsAbstention,
    corroborate, describe_options,
)


class FakeClock:
    def __init__(self, tick=103):
        self.tick = tick

    def now(self):
        return self.tick


class FakeCDP:
    def __init__(self):
        self.harvest_count = 0
        self._receipts = set()

    def harvest(self, binding):
        self.harvest_count += 1
        receipt = dict(binding, kind="cdp_harvest", observed=True,
                       harvested=True, frame_ref="frame-7")
        self._receipts.add(id(receipt))
        return receipt

    def verify(self, receipt):
        return self.harvest_count > 0 and id(receipt) in self._receipts


class FakeBrokeredIPC:
    def __init__(self):
        self._receipts = set()

    def verify(self, receipt):
        return id(receipt) in self._receipts

    def capture(self, binding):
        stream = dict(binding, id="stream-1", verified=True)
        ticket = dict(binding, id="ticket-1", verified=True)
        session = dict(binding, id="session-1", verified=True)
        receipt = dict(binding, kind="brokered_ipc", observed=True,
                       frame_ref="frame-7", stream_id=stream["id"],
                       ticket_id=ticket["id"], session_id=session["id"])
        self._receipts.add(id(receipt))
        return {"kind": "ipc", "capture_supported": True, "receipt": receipt,
                "verify_capture": self.verify,
                "trusted_stream": stream, "ticket": ticket, "session": session}


def fake_procfs(status):
    """Parse *fixture text*, never touch the worker's procfs."""
    rows = {}
    for line in status.splitlines():
        k, sep, v = line.partition(":")
        if sep:
            chunks = v.strip().split()
            if len(chunks) == 2 and chunks[1] == "kB":
                rows[k] = int(chunks[0]) * 1024
    return rows["VmRSS"], rows["VmSwap"]


def fake_cgroup(current, swap):
    return int(current.strip()), int(swap.strip())


class WriteSpy:
    def __init__(self):
        self.persisted_bytes = 0
        self.artifact_count = 0

    def write(self, _pixels_or_receipt):
        raise AssertionError("pixel, dump or raw receipt persistence forbidden")

    def row(self, tick):
        return {"source": "write_spy", "observed": True, "verified": True,
                "at_tick": tick, "persisted_bytes": self.persisted_bytes,
                "artifact_count": self.artifact_count}


def packet(source_kind="cdp", *, handle_id="holdings-one", seq=7, epoch=2,
           generation=3, clock=None):
    """In-RAM Holdings pause/outcome, independent sources and authored limits."""
    clock = clock or FakeClock()
    handle = {"kind": "cdp_tab" if source_kind == "cdp" else "godot_window",
              "canonical_id": handle_id, "generation": generation}
    binding = {"handle": handle, "epoch": epoch, "frame_seq": seq,
               "dpr_ppm": 1000000, "screen_id": "arena", "query_id": "holdings"}
    pixels = np.full((4, 4, 3), 27, dtype=np.uint8)
    frame = dict(binding, width=4, height=4, rgb=pixels,
                 digest=hashlib.sha256(pixels.tobytes()).hexdigest(),
                 frame_ref="frame-7", commit_ref="commit-7", capture_tick=102,
                 skipped=False, stale=False, hidden_information=False)
    command = {"tick": 100, "seq": 21, "cmd": "pause", "args": {}}
    outcome = {"seq": 21, "requested_tick": 100, "applied_tick": 101,
               "order_seq_id": 9, "status": "applied", "reason": "ok",
               "state_sha256": "a" * 64}
    committed = dict(binding, committed=True, seq=21, order_seq_id=9,
                     applied_tick=101, state_sha256=outcome["state_sha256"],
                     commit_ref="commit-7")
    nodes = [{"id": "health", "role": "progressbar", "box": [0, 0, 3, 3],
              "hidden": False}]
    expected_nodes = [{"id": "health", "role": "progressbar", "box": [0, 0, 3, 3]}]
    tree = dict(binding, observed=True, origin="independent_ui_tree",
                tree_ref="tree-7", frame_ref="frame-7", commit_ref="commit-7",
                hidden_information=False, nodes=nodes)
    expected_structure = dict(binding, origin="episode_spec", commit_ref="commit-7",
                              nodes=expected_nodes)
    samples = {"health": {"unit": "points", "value": 98},
               "progress": {"unit": "tiles", "value": 12}}
    target_samples = {"health": {"unit": "points", "value": 100},
                      "progress": {"unit": "tiles", "value": 13}}
    telemetry = dict(binding, observed=True, origin="trusted_hook",
                     commit_ref="commit-7", samples=samples)
    expected_telemetry = dict(binding, origin="calibrated_spec", commit_ref="commit-7",
                              samples=target_samples)
    rss, vswap = fake_procfs("Name: wender-test\nVmRSS: 1 kB\nVmSwap: 0 kB\n")
    memory, cswap = fake_cgroup("2048\n", "0\n")
    spy = WriteSpy()
    observers = {
        "procfs": {"source": "procfs", "observed": True, "verified": True,
                   "at_tick": 103, "resident_bytes": rss, "swap_bytes": vswap},
        "cgroup": {"source": "cgroup", "observed": True, "verified": True,
                   "at_tick": 103, "memory_current_bytes": memory,
                   "swap_current_bytes": cswap},
        "write_spy": spy.row(103),
    }
    residency = dict(binding, observed=True, observers=observers)
    if source_kind == "cdp":
        cdp = FakeCDP()
        source = {"kind": "cdp", "capture_supported": True,
                  "receipt": cdp.harvest(binding), "verify_capture": cdp.verify}
    else:
        source = FakeBrokeredIPC().capture(binding)
    # Disjoint record ownership matters: mutating one independent source must
    # not silently mutate the capture binding through a shared nested dict.
    for row in (frame, tree, expected_structure, telemetry, expected_telemetry,
                residency, committed, source["receipt"]):
        row["handle"] = dict(row["handle"])
    for name in ("trusted_stream", "ticket", "session"):
        if name in source:
            source[name]["handle"] = dict(source[name]["handle"])
    return {"frame": frame, "source": source, "tree": tree,
            "expected_structure": expected_structure, "telemetry": telemetry,
            "expected_telemetry": expected_telemetry, "residency": residency,
            "command": command, "outcome": outcome, "committed": committed,
            "tolerances": {"geometry_css_px": 2,
                           "metrics": {"health": {"unit": "points", "absolute": 2},
                                       "progress": {"unit": "tiles", "absolute": 1}}},
            "limits": {"max_frame_age_ticks": 3,
                       "max_procfs_resident_bytes": 4096,
                       "max_cgroup_memory_bytes": 8192,
                       "max_swap_bytes": 0, "max_persisted_bytes": 0,
                       "max_artifact_count": 0},
            "now_tick": clock.now()}


class TestHoldingsAbstention(unittest.TestCase):
    def assert_decision(self, row, verdict, code):
        self.assertEqual((row.verdict, row.failure_code), (verdict, code))
        self.assertIn(row.failure_code, FAILURE_CODES) if code else self.assertIsNone(row.failure_code)
        self.assertIs(type(row.structure_checked), bool)
        self.assertIs(type(row.telemetry_checked), bool)
        self.assertIs(type(row.residency_checked), bool)

    def test_both_source_policies_have_independent_positive_controls(self):
        for source_kind in ("cdp", "ipc"):
            with self.subTest(source_kind=source_kind):
                r = corroborate(packet(source_kind))
                self.assert_decision(r, CORROBORATED, None)
                self.assertEqual((r.structure_checked, r.telemetry_checked,
                                  r.residency_checked), (True, True, True))
                self.assertEqual((r.tree_ref, r.frame_ref, r.commit_ref),
                                 ("tree-7", "frame-7", "commit-7"))

    def test_explicit_missing_telemetry_never_corroborates_even_with_equal_hashes(self):
        p = packet()
        del p["telemetry"]
        p["tree"]["screen_identity"] = "same_screen"
        p["frame"]["screen_identity"] = "same_screen"
        p["expected_telemetry"]["hash"] = p["outcome"]["state_sha256"]
        result = corroborate(p)
        self.assert_decision(result, ABSTAIN, "TELEMETRY_AUSENTE")
        self.assertTrue(result.structure_checked)
        self.assertFalse(result.telemetry_checked)
        self.assertFalse(result.residency_checked)

    def test_each_named_mutation_is_a_negative_contrast(self):
        mutations = {
            "no_expected_structure": (lambda p: p.pop("expected_structure"),
                                      ABSTAIN, "ESTRUCTURA_AUSENTE"),
            "no_tree": (lambda p: p.pop("tree"), ABSTAIN, "ESTRUCTURA_AUSENTE"),
            "tree_wrong_role": (lambda p: p["tree"]["nodes"][0].__setitem__("role", "button"),
                                MISMATCH, "ESTRUCTURA_DIVERGENTE"),
            "tree_wrong_geometry": (lambda p: p["tree"]["nodes"][0].__setitem__("box", [100, 0, 3, 3]),
                                    MISMATCH, "ESTRUCTURA_DIVERGENTE"),
            "matching_offscreen_boxes": (lambda p: (p["tree"]["nodes"][0].__setitem__("box", [100, 0, 3, 3]),
                                                     p["expected_structure"]["nodes"][0].__setitem__("box", [100, 0, 3, 3])),
                                         MISMATCH, "ESTRUCTURA_DIVERGENTE"),
            "tree_hidden": (lambda p: p["tree"]["nodes"][0].__setitem__("hidden", True),
                            ABSTAIN, "INFORMACION_OCULTA"),
            "hidden_frame": (lambda p: p["frame"].__setitem__("hidden_information", True),
                             ABSTAIN, "INFORMACION_OCULTA"),
            "no_telemetry": (lambda p: p.pop("telemetry"), ABSTAIN, "TELEMETRY_AUSENTE"),
            "uncalibrated_telemetry": (lambda p: p["expected_telemetry"].__setitem__("origin", "hash"),
                                       ABSTAIN, "TELEMETRY_AUSENTE"),
            "wrong_units": (lambda p: p["telemetry"]["samples"]["health"].__setitem__("unit", "ticks"),
                            MISMATCH, "TELEMETRIA_DIVERGENTE"),
            "wrong_value": (lambda p: p["telemetry"]["samples"]["health"].__setitem__("value", 10),
                            MISMATCH, "TELEMETRIA_DIVERGENTE"),
            "no_tolerance": (lambda p: p["tolerances"]["metrics"].pop("health"),
                             ABSTAIN, "TOLERANCIA_AUSENTE"),
            "zero_default": (lambda p: p["tolerances"]["metrics"]["health"].__setitem__("absolute", 0),
                             ABSTAIN, "TOLERANCIA_AUSENTE"),
            "no_residency": (lambda p: p.pop("residency"), ABSTAIN, "RESIDENCIA_NO_OBSERVADA"),
            "no_procfs": (lambda p: p["residency"]["observers"].pop("procfs"),
                          ABSTAIN, "RESIDENCIA_NO_OBSERVADA"),
            "no_cgroup": (lambda p: p["residency"]["observers"].pop("cgroup"),
                          ABSTAIN, "RESIDENCIA_NO_OBSERVADA"),
            "no_write_spy": (lambda p: p["residency"]["observers"].pop("write_spy"),
                             ABSTAIN, "RESIDENCIA_NO_OBSERVADA"),
            "unobserved_procfs": (lambda p: p["residency"]["observers"]["procfs"].__setitem__("observed", False),
                                  ABSTAIN, "RESIDENCIA_NO_OBSERVADA"),
            "swap_leak": (lambda p: p["residency"]["observers"]["procfs"].__setitem__("swap_bytes", 10),
                          MISMATCH, "RESIDENCIA_DIVERGENTE"),
            "artifact_leak": (lambda p: p["residency"]["observers"]["write_spy"].__setitem__("artifact_count", 1),
                              MISMATCH, "RESIDENCIA_DIVERGENTE"),
            "unsupported": (lambda p: p["source"].__setitem__("capture_supported", False),
                            ABSTAIN, "CAPTURA_NO_SOPORTADA"),
            "unharvested_cdp": (lambda p: p["source"]["receipt"].__setitem__("harvested", False),
                                ABSTAIN, "CAPTURA_NO_VERIFICADA"),
            "unknown_source": (lambda p: p["source"].__setitem__("kind", "unknown"),
                               ABSTAIN, "FUENTE_DESCONOCIDA"),
            "no_capture_validator": (lambda p: p["source"].pop("verify_capture"),
                                     ABSTAIN, "CAPTURA_NO_VERIFICADA"),
            "forged_capture_receipt": (lambda p: p["source"].__setitem__(
                "receipt", dict(p["source"]["receipt"])),
                ABSTAIN, "CAPTURA_NO_VERIFICADA"),
            "skipped": (lambda p: p["frame"].__setitem__("skipped", True),
                        ABSTAIN, "FRAME_OMITIDO"),
            "stale": (lambda p: p["frame"].__setitem__("stale", True),
                      ABSTAIN, "FRAME_OBSOLETO"),
            "old_clock": (lambda p: p.__setitem__("now_tick", 109),
                          ABSTAIN, "FRAME_OBSOLETO"),
            "unknown_geometry": (lambda p: p["frame"].__setitem__("width", None),
                                 ABSTAIN, "GEOMETRIA_DESCONOCIDA"),
            "wrong_digest": (lambda p: p["frame"].__setitem__("digest", "0" * 64),
                             ABSTAIN, "GEOMETRIA_DESCONOCIDA"),
            "wrong_commit": (lambda p: p["committed"].__setitem__("commit_ref", "wrong"),
                             ABSTAIN, "COMPROMISO_AUSENTE"),
            "missing_seq": (lambda p: p["outcome"].__setitem__("seq", 999),
                            ABSTAIN, "COMPROMISO_AUSENTE"),
            "nonpause": (lambda p: p["command"].__setitem__("cmd", "play"),
                         ABSTAIN, "COMPROMISO_AUSENTE"),
            "wrong_query": (lambda p: p["tree"].__setitem__("query_id", "unbound"),
                            MISMATCH, "ESTRUCTURA_DIVERGENTE"),
            "wrong_dpr": (lambda p: p["tree"].__setitem__("dpr_ppm", 2000000),
                          MISMATCH, "ESTRUCTURA_DIVERGENTE"),
            "wrong_epoch": (lambda p: p["tree"].__setitem__("epoch", 900),
                            MISMATCH, "ESTRUCTURA_DIVERGENTE"),
            "wrong_generation": (lambda p: p["tree"]["handle"].__setitem__("generation", 999),
                                 MISMATCH, "ESTRUCTURA_DIVERGENTE"),
            "wrong_frame_seq": (lambda p: p["tree"].__setitem__("frame_seq", 6),
                                MISMATCH, "ESTRUCTURA_DIVERGENTE"),
            "missing_limit": (lambda p: p["limits"].pop("max_swap_bytes"),
                              ABSTAIN, "EVIDENCIA_INVALIDA"),
        }
        for name, (change, verdict, reason) in mutations.items():
            with self.subTest(mutation=name):
                p = packet()
                change(p)
                self.assert_decision(corroborate(p), verdict, reason)

    def test_ipc_stream_ticket_session_are_distinct_and_mandatory(self):
        for missing in ("ticket", "trusted_stream", "session"):
            with self.subTest(missing=missing):
                p = packet("ipc")
                p["source"].pop(missing)
                self.assert_decision(corroborate(p), ABSTAIN, "CAPTURA_NO_VERIFICADA")
        p = packet("cdp")
        p["source"]["kind"] = "ipc"  # not interchangeable just by renaming
        self.assert_decision(corroborate(p), ABSTAIN, "CAPTURA_NO_VERIFICADA")
        p = packet("ipc")
        p["source"]["receipt"]["kind"] = "cdp_harvest"
        self.assert_decision(corroborate(p), ABSTAIN, "CAPTURA_NO_VERIFICADA")

    def test_temporal_lanes_are_independent_and_no_last_good_fallback(self):
        verifier = HoldingsAbstention()
        self.assertEqual(verifier.evaluate(packet()).verdict, CORROBORATED)
        self.assert_decision(verifier.evaluate(packet()), ABSTAIN, "FRAME_OBSOLETO")
        self.assert_decision(verifier.evaluate(packet(seq=9)), ABSTAIN, "FRAME_OMITIDO")
        self.assertEqual(verifier.evaluate(packet(seq=8)).verdict, CORROBORATED)
        self.assertEqual(verifier.evaluate(packet("ipc")).verdict, CORROBORATED)
        self.assertEqual(verifier.evaluate(packet(handle_id="sibling")).verdict, CORROBORATED)
        self.assertEqual(verifier.evaluate(packet(generation=4)).verdict, CORROBORATED)
        self.assertEqual(verifier.evaluate(packet(epoch=3)).verdict, CORROBORATED)

    def test_resolver_exception_resets_current_lane_only(self):
        gate = HoldingsAbstention()
        self.assertEqual(gate.evaluate(packet()).verdict, CORROBORATED)
        self.assertEqual(gate.evaluate(packet(handle_id="sibling")).verdict, CORROBORATED)
        def failing(_tree, _expected, *, geometry_tolerance_raw):
            raise RuntimeError("raw ticket/pixels must never reach metadata")
        self.assert_decision(gate.evaluate(packet(seq=8), structure_resolver=failing),
                             ABSTAIN, "EVIDENCIA_INVALIDA")
        # Own lane reset; sibling lane still rejects replay.
        self.assertEqual(gate.evaluate(packet(seq=8)).verdict, CORROBORATED)
        self.assert_decision(gate.evaluate(packet(handle_id="sibling")),
                             ABSTAIN, "FRAME_OBSOLETO")

    def test_injected_independent_checker_and_closed_abstention(self):
        called = []
        def checker(tree, expected, *, geometry_tolerance_raw):
            called.append((tree["origin"], expected["origin"], geometry_tolerance_raw))
            return {"independent": True, "checked": True, "matched": True}
        r = corroborate(packet(), structure_resolver=checker)
        self.assertEqual(r.verdict, CORROBORATED)
        self.assertEqual(called, [("independent_ui_tree", "episode_spec", 2)])
        def false_checker(*args, **kwargs):
            return {"checked": True, "independent": True, "matched": False}
        self.assert_decision(corroborate(packet(), structure_resolver=false_checker),
                             MISMATCH, "ESTRUCTURA_DIVERGENTE")
        def unknown_checker(*args, **kwargs):
            return {"checked": False, "independent": False, "matched": True}
        self.assert_decision(corroborate(packet(), structure_resolver=unknown_checker),
                             ABSTAIN, "ESTRUCTURA_AUSENTE")
        self.assertNotIn("UNMAPPED", FAILURE_CODES)

    def test_no_disk_artifacts_and_bounded_metadata_only(self):
        p = packet()
        p["tree"]["raw_dump"] = "pixel bytes here should never persist"
        p["telemetry"]["raw_hook"] = b"opaque raw receipt"
        p["committed"]["checkpoint_bytes"] = b"opaque checkpoint"
        before = p["frame"]["rgb"].copy()
        # Any disk access from production would fail in the spy.
        with patch.object(builtins, "open", side_effect=AssertionError("forbidden disk write")):
            result = corroborate(p)
        self.assertEqual(result.verdict, CORROBORATED)
        self.assertTrue(np.array_equal(p["frame"]["rgb"], before))
        metadata = result.metadata()
        self.assertEqual(set(metadata), {"verdict", "failure_code", "tree_ref",
                                         "frame_ref", "commit_ref", "structure_checked",
                                         "telemetry_checked", "residency_checked"})
        serialized = json.dumps(metadata, sort_keys=True)
        for forbidden in ("raw_dump", "raw_hook", "checkpoint_bytes", "pixel", "rgb"):
            self.assertNotIn(forbidden, serialized)
        self.assertTrue(all(v is None or type(v) in (bool, str) for v in metadata.values()))
        with self.assertRaises((FrozenInstanceError, AttributeError)):
            result.verdict = "changed"
        with self.assertRaises(AssertionError):
            WriteSpy().write(p["frame"]["rgb"])
        options = describe_options()
        self.assertFalse(any(v is True for v in options.values()))
        self.assertIsNone(options["output_path"])

    def test_disabled_optional_training_failure_does_not_override_retail_proof(self):
        p = packet()
        p["training"] = {"enabled": False, "status": "failed", "reason": "optional"}
        self.assertEqual(corroborate(p).verdict, CORROBORATED)

    def test_unknown_or_unbounded_provenance_never_escapes(self):
        p = packet()
        p["tree"]["tree_ref"] = "secret/with/slashes" * 30
        self.assert_decision(corroborate(p), MISMATCH, "ESTRUCTURA_DIVERGENTE")
        self.assertIsNone(corroborate(p).tree_ref)
        p = packet()
        p["frame"]["handle"]["generation"] = True
        self.assert_decision(corroborate(p), ABSTAIN, "VINCULO_INVALIDO")


if __name__ == "__main__":
    unittest.main()
