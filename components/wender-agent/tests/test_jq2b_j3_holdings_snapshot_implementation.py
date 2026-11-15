"""Offline J3 gate: real public API, RAM-only inline fixtures, no live services."""
import copy
from dataclasses import replace
import hashlib
import json
import unittest

from wender.state import schema
from wender.state.holdings_snapshot import (
    SnapshotError, freeze_snapshot, difference_metadata, append_executed_tick,
    validate_trail, export_trail,
)
from wender.perceive.godot import validate_uitree


def digest(value):
    return hashlib.sha256(schema.dumps(value).encode("utf-8")).hexdigest()


def node(path, *, role="button", name="Pause"):
    identity = hashlib.sha256(path.encode("utf-8")).hexdigest()[:16]
    return {"id": identity, "role": role, "name": name,
            "attrs": [], "depth": 0, "ordinal": 0}


def base_state(*items, kind="godot"):
    return schema.build_state(
        list(items), kind=kind, url="", title="Offline Holdings",
        ax_sha256="a" * 64, ax_stable_sha256="b" * 64,
        prune={"name": "fixture", "version": 1}, raw_count=len(items),
        actionable_roles=frozenset({"button"}),
    )


def query():
    return {"sort_key": "ticker", "sort_dir": "asc", "page_index": 0,
            "page_size": 10, "filter_spec": []}


def godot_fixture(*, summary=False, as_of_tick=10, cadence="current"):
    path = "Dashboard/HoldingsTable" if summary else "Dashboard/Pause"
    n = node(path, role="table" if summary else "button")
    base = base_state(n)
    entry = {"control_path": path, "screen_id": "Dashboard"}
    if summary:
        entry.update({"table_id": "holdings", "refresh_cadence": cadence,
                      "as_of_tick": as_of_tick, "query": query(),
                      "visible_range": {"count": 0, "first_row_id": None, "last_row_id": None}})
    sidecar = {"schema": "holdings.godot.uitree/v1",
               "base_sha256": digest(base), "nodes": {n["id"]: entry}}
    return base, sidecar, n["id"]


def binding(*, kind="godot", tick=10, prior=None, query_bindings=None):
    is_godot = kind == "godot"
    screen = "Dashboard" if is_godot else "BrowserTab"
    return {"source_tick": tick, "cadence": "current", "screen_id": screen,
            "expected_screen_id": screen, "entity_id": "portfolio-A",
            "expected_entity_id": "portfolio-A", "binding_ref": "session-1",
            "identity_policy": "godot-path" if is_godot else "cdp-fingerprint",
            "simulation_state_sha256": "c" * 64,
            "presentation_sha256": "d" * 64,
            "fingerprint": "e" * 64 if not is_godot else None,
            "expected_fingerprint": "e" * 64 if not is_godot else None,
            "cadence_limits": {"current": 0, "daily_close": 0},
            "query_bindings": query_bindings if query_bindings is not None else {},
            "prior_daily_close": prior if prior is not None else {}}


def freeze_godot(*, tick=10, summary=False, as_of_tick=10, cadence="current", prior=None):
    base, sidecar, nid = godot_fixture(summary=summary, as_of_tick=as_of_tick, cadence=cadence)
    b = binding(tick=tick, query_bindings={nid: query()} if summary else {}, prior=prior)
    return freeze_snapshot(base, sidecar=sidecar, binding=b,
                           validators={"pair": validate_uitree})


class TestHoldingsSnapshot(unittest.TestCase):
    def test_hash_domains_and_independent_recompute(self):
        base, sidecar, _ = godot_fixture()
        snap = freeze_snapshot(base, sidecar=sidecar, binding=binding(),
                               validators={"pair": validate_uitree})
        self.assertEqual(snap.base_sha256, digest(base))
        self.assertEqual(snap.sidecar_sha256, digest(sidecar))
        self.assertEqual(snap.ax_stable_sha256, base["ax_stable_sha256"])
        self.assertNotEqual(snap.base_sha256, snap.simulation_state_sha256)
        self.assertNotEqual(snap.sidecar_sha256, snap.base_sha256)
        self.assertEqual(set(snap.metadata()), {
            "base_sha256", "ax_stable_sha256", "simulation_state_sha256",
            "sidecar_sha256", "presentation_sha256", "identity_policy",
            "source_tick", "cadence", "screen_id", "binding_ref"})

    def test_immutable_ram_copies_defeat_alias_mutation(self):
        base, sidecar, nid = godot_fixture()
        original_base = copy.deepcopy(base)
        original_tree = copy.deepcopy(sidecar)
        snap = freeze_snapshot(base, sidecar=sidecar, binding=binding(),
                               validators={"pair": validate_uitree})
        base["nodes"][0]["name"] = "SECRET RAW TEXT"
        sidecar["nodes"][nid]["control_path"] = "Dashboard/Changed"
        self.assertEqual(snap.base, original_base)
        self.assertEqual(snap.tree, original_tree)
        snap.base["nodes"][0]["name"] = "view mutates only itself"
        snap.tree["nodes"][nid]["control_path"] = "view mutates only itself"
        self.assertEqual(snap.base, original_base)
        self.assertEqual(snap.tree, original_tree)
        self.assertEqual(snap.base_sha256, digest(original_base))

    def test_simulation_base_hash_negative_contrast(self):
        base, sidecar, _ = godot_fixture()
        b = binding()
        b["simulation_state_sha256"] = digest(base)
        with self.assertRaisesRegex(SnapshotError, "simulation hash"):
            freeze_snapshot(base, sidecar=sidecar, binding=b,
                            validators={"pair": validate_uitree})

    def test_godot_requires_genuine_validator_and_binding(self):
        base, sidecar, _ = godot_fixture()
        with self.assertRaisesRegex(SnapshotError, "pair validator"):
            freeze_snapshot(base, sidecar=sidecar, binding=binding(), validators={})
        sidecar["base_sha256"] = "f" * 64
        with self.assertRaisesRegex(SnapshotError, "binding mismatch"):
            freeze_snapshot(base, sidecar=sidecar, binding=binding(),
                            validators={"pair": validate_uitree})

    def test_screen_entity_and_godot_path_identity(self):
        base, sidecar, nid = godot_fixture()
        for key, wrong in (("expected_screen_id", "OtherScreen"),
                           ("expected_entity_id", "other-portfolio")):
            b = binding()
            b[key] = wrong
            with self.subTest(key=key), self.assertRaisesRegex(SnapshotError, "identity mismatch"):
                freeze_snapshot(base, sidecar=sidecar, binding=b, validators={"pair": validate_uitree})
        sidecar["nodes"][nid]["control_path"] = "Dashboard/Other"
        with self.assertRaises(SnapshotError):
            freeze_snapshot(base, sidecar=sidecar, binding=binding(),
                            validators={"pair": validate_uitree})

    def test_current_stale_summary_rejected(self):
        with self.assertRaisesRegex(SnapshotError, "stale summary"):
            freeze_godot(summary=True, tick=10, as_of_tick=9, cadence="current")

    def test_declared_prior_daily_close_is_valid(self):
        _, _, nid = godot_fixture(summary=True, as_of_tick=9, cadence="daily_close")
        proof = {nid: {"as_of_tick": 9, "cadence": "daily_close", "query": query(),
                       "screen_id": "Dashboard", "binding_ref": "session-1"}}
        snap = freeze_godot(summary=True, tick=10, as_of_tick=9,
                            cadence="daily_close", prior=proof)
        self.assertEqual(snap.source_tick, 10)
        self.assertEqual(snap.cadence, "current")
        self.assertIsNotNone(snap.sidecar_sha256)

    def test_prior_close_needs_matching_evidence_and_query(self):
        base, sidecar, nid = godot_fixture(summary=True, as_of_tick=9, cadence="daily_close")
        b = binding(query_bindings={nid: query()})
        with self.assertRaisesRegex(SnapshotError, "prior daily-close"):
            freeze_snapshot(base, sidecar=sidecar, binding=b, validators={"pair": validate_uitree})
        wrong = query()
        wrong["page_index"] = 1
        b["query_bindings"] = {nid: wrong}
        with self.assertRaisesRegex(SnapshotError, "query identity"):
            freeze_snapshot(base, sidecar=sidecar, binding=b, validators={"pair": validate_uitree})

    def test_cdp_policy_and_unsupported_electron_sidecar(self):
        cdp = base_state(node("tab/one"), kind="cdp")
        snap = freeze_snapshot(cdp, binding=binding(kind="cdp"), validators={})
        self.assertEqual(snap.identity_policy, "cdp-fingerprint")
        self.assertIsNone(snap.tree)
        self.assertIsNone(snap.sidecar_sha256)
        b = binding(kind="cdp")
        b["expected_fingerprint"] = "f" * 64
        with self.assertRaisesRegex(SnapshotError, "fingerprint identity"):
            freeze_snapshot(cdp, binding=b, validators={})
        with self.assertRaisesRegex(SnapshotError, "Electron/CDP sidecar"):
            freeze_snapshot(cdp, sidecar={"schema": "holdings.godot.uitree/v1"},
                            binding=binding(kind="cdp"), validators={})
        b = binding(kind="cdp")
        b["identity_policy"] = "godot-path"
        with self.assertRaisesRegex(SnapshotError, "identity policy"):
            freeze_snapshot(cdp, binding=b, validators={})

    def test_stable_id_projection_not_base_order(self):
        original = base_state(node("tab/three"), node("tab/one"), kind="cdp")
        after = base_state(node("tab/three", name="Other"), node("tab/one"),
                           node("tab/four"), kind="cdp")
        a = freeze_snapshot(original, binding=binding(kind="cdp", tick=10), validators={})
        b = freeze_snapshot(after, binding=binding(kind="cdp", tick=11), validators={})
        projection = difference_metadata(a, b, max_ids=4)
        self.assertEqual(projection["added_ids"], (node("tab/four")["id"],))
        self.assertEqual(projection["changed_ids"], (node("tab/three")["id"],))
        self.assertEqual([n["id"] for n in a.base["nodes"]],
                         [node("tab/three")["id"], node("tab/one")["id"]])
        with self.assertRaisesRegex(SnapshotError, "configured ID bound"):
            difference_metadata(a, b, max_ids=1)

    def test_trail_represents_unchanged_executed_tick(self):
        a = freeze_godot(tick=10)
        b = freeze_godot(tick=11)
        trail = append_executed_tick((), a, max_ticks=3, max_diff_ids=3)
        trail = append_executed_tick(trail, b, previous_snapshot=a, max_ticks=3, max_diff_ids=3)
        self.assertEqual(len(trail), 2)
        self.assertTrue(validate_trail(trail, max_ticks=3, max_ids=3))
        self.assertEqual(trail[1].previous_entry_sha256, trail[0].entry_sha256)
        self.assertEqual((trail[1].added_ids, trail[1].removed_ids, trail[1].changed_ids),
                         ((), (), ()))
        safe = export_trail(trail, max_ticks=3, max_diff_ids=3)
        self.assertEqual(len(json.loads(safe)), 2)
        for forbidden in ("Pause", "control_path", "states", "node", "rgb", "receipt"):
            self.assertNotIn('"' + forbidden + '"', safe)

    def test_trail_parent_tamper_missing_tick_and_bound(self):
        a = freeze_godot(tick=10)
        trail = append_executed_tick((), a, max_ticks=2, max_diff_ids=2)
        tampered = (replace(trail[0], entry_sha256="f" * 64),)
        with self.assertRaisesRegex(SnapshotError, "digest mismatch"):
            validate_trail(tampered, max_ticks=2, max_ids=2)
        b = freeze_godot(tick=12)
        with self.assertRaisesRegex(SnapshotError, "missing or duplicate executed tick"):
            append_executed_tick(trail, b, previous_snapshot=a, max_ticks=2, max_diff_ids=2)
        with self.assertRaisesRegex(SnapshotError, "preceding frozen snapshot"):
            append_executed_tick(trail, a, max_ticks=2, max_diff_ids=2)
        with self.assertRaisesRegex(SnapshotError, "capacity"):
            append_executed_tick(trail, b, previous_snapshot=a, max_ticks=1, max_diff_ids=2)

    def test_configured_limits_required(self):
        base, sidecar, _ = godot_fixture()
        b = binding()
        del b["cadence_limits"]
        with self.assertRaisesRegex(SnapshotError, "configured cadence_limits"):
            freeze_snapshot(base, sidecar=sidecar, binding=b, validators={"pair": validate_uitree})
        with self.assertRaisesRegex(SnapshotError, "configured"):
            append_executed_tick((), freeze_godot(), max_ticks=0, max_diff_ids=3)


if __name__ == "__main__":
    unittest.main()
