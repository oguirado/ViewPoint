# @module {"id":"wa.tests.test_ax_bitpack","layer":1,"owner":"PERCEIVE","deps":["wa.perceive.ax","wa.state.schema"],"book":"HB-WA-s1-perceive"}
"""W1 predicates, planted rejection fixtures, and positive controls."""
import copy
import dataclasses
import itertools
import random
import unittest

from wender.perceive import ax
from wender.state import schema
from tests.test_ax import _n, fixture


# @anchor:wa.tests.test_ax_bitpack.fixture_raw_ax_password_and_normal_textbox_value_chars_leaked
def fixture_raw_ax_password_and_normal_textbox_value_chars_leaked():
    raw = [_n(1, "RootWebArea", "Doc", [2, 3, 4, 5]),
           _n(2, "textbox", "Password", props={"value": "S3cr3tP@ss!", "type": "password"}),
           _n(3, "searchbox", "Search", props={"value": "Canberra query"}),
           _n(4, "textbox", "Empty"), _n(5, "button", "Send")]
    raw[1]["value"] = {"value": "S3cr3tP@ss!"}
    return raw


# @anchor:wa.tests.test_ax_bitpack.fixture_state_word_uint16_bit_collision_or_exceeding_65535
def fixture_state_word_uint16_bit_collision_or_exceeding_65535():
    return [65536, 1 << 13, 1 << 15, -1, True, "0", None,
            ax.BIT_DISABLED | ax.BIT_ENABLED, ax.BIT_HIDDEN | ax.BIT_VISIBLE]


# @anchor:wa.tests.test_ax_bitpack.fixture_gen16_non_incrementing_on_structural_parent_role_change
def fixture_gen16_non_incrementing_on_structural_parent_role_change():
    before = fixture()
    after = copy.deepcopy(before)
    after[2]["role"]["value"] = "dialog"
    return before, after


# @anchor:wa.tests.test_ax_bitpack.fixture_anchor_id_missing_or_unstable_under_ax_nodeid_shuffling
def fixture_anchor_id_missing_or_unstable_under_ax_nodeid_shuffling():
    raw = fixture(offset=500)
    random.Random(7).shuffle(raw)
    return raw


# @anchor:wa.tests.test_ax_bitpack.fixture_build_state_violating_NODE_KEYS_exact_lock_without_companion_projection
def fixture_build_state_violating_NODE_KEYS_exact_lock_without_companion_projection():
    nodes = ax.prune(fixture())
    state = _state(nodes)
    state["nodes"][0]["state_word"] = nodes[0]["state_word"]
    return state


def _state(nodes):
    return schema.build_state(nodes, kind="cdp", url="", title="",
                              ax_sha256=ax.snapshot_sha(nodes),
                              ax_stable_sha256=ax.snapshot_sha(nodes, False),
                              prune={"name": ax.PRUNE_NAME, "version": ax.PRUNE_VERSION},
                              raw_count=len(nodes), actionable_roles=ax.ACTIONABLE_ROLES)


# @anchor:wa.tests.test_ax_bitpack.TestAxBitpack
class TestAxBitpack(unittest.TestCase):
    # @anchor:wa.tests.test_ax_bitpack.test_fixture_raw_ax_password_and_normal_textbox_value_chars_leaked
    def test_fixture_raw_ax_password_and_normal_textbox_value_chars_leaked(self):
        nodes = ax.prune(fixture_raw_ax_password_and_normal_textbox_value_chars_leaked())
        state = _state(nodes)
        sidecar = schema.build_companion_projection(nodes, state)
        self.assertEqual([n["value_len"] for n in nodes], [None, 11, 14, 0, None])
        for serialized in (repr(nodes), ax.canon(nodes), schema.dumps(state), schema.dumps(sidecar)):
            for secret in ("S3cr3tP@ss!", "Canberra query"):
                self.assertNotIn(secret, serialized)
        for key in ("name", "attrs", "value"):
            bad = copy.deepcopy(sidecar)
            bad["elements"][1][key] = "S3cr3tP@ss!"
            self.assertTrue(schema.validate_companion_projection(state, bad))
        raw = fixture_raw_ax_password_and_normal_textbox_value_chars_leaked()
        raw[1]["value"] = {"value": "secretA"}
        a = ax.prune(raw)
        raw[1]["value"] = {"value": "secretB"}
        b = ax.prune(raw)
        self.assertEqual(ax.canon(a), ax.canon(b))
        self.assertEqual(ax.snapshot_sha(a), ax.snapshot_sha(b))
        self.assertEqual([n["id"] for n in a], [n["id"] for n in b])

    # @anchor:wa.tests.test_ax_bitpack.test_fixture_state_word_uint16_bit_collision_or_exceeding_65535
    def test_fixture_state_word_uint16_bit_collision_or_exceeding_65535(self):
        nodes = ax.prune(fixture())
        state = _state(nodes)
        for word in fixture_state_word_uint16_bit_collision_or_exceeding_65535():
            with self.subTest(word=word):
                for gate in (ax.validate_state_word, ax.decode_state_word, ax.is_ready_to_act):
                    with self.assertRaises(ValueError):
                        gate(word)
                bad = schema.build_tags_sidecar(nodes, state)
                bad["elements"][0]["state_word"] = word
                self.assertTrue(schema.validate_tags_sidecar(state, bad))
        for bits in range(1 << 9):
            props = {k: "true" if bits & (1 << i) else "false"
                     for i, k in enumerate(ax._STATE_FLAGS[:9])}
            for role, value in itertools.product(("button", "main"), (None, 0, 1)):
                word = ax.encode_state_word(props, role, value)
                flags = ax.decode_state_word(word)
                self.assertEqual([flags[k] for k in ax._STATE_FLAGS[:9]],
                                 [props[k] == "true" for k in ax._STATE_FLAGS[:9]])
                self.assertEqual(word & ~0x1FFF, 0)
                self.assertEqual(flags["value_nonempty"], value == 1)
                self.assertEqual(flags["actionable"], role == "button")
                self.assertEqual(flags["visible"], not flags["hidden"])
                self.assertEqual(flags["enabled"], not flags["disabled"])
                self.assertEqual(ax.is_ready_to_act(word), role == "button" and not flags["hidden"] and not flags["disabled"])
        self.assertTrue(ax.decode_state_word(ax.encode_state_word({"checked": "mixed", "pressed": "mixed"}))["checked"])

    # @anchor:wa.tests.test_ax_bitpack.test_fixture_gen16_non_incrementing_on_structural_parent_role_change
    def test_fixture_gen16_non_incrementing_on_structural_parent_role_change(self):
        raw, changed = fixture_gen16_non_incrementing_on_structural_parent_role_change()
        for gen in (7, 65535):
            before = ax.prune(raw, gen16=gen)
            after = ax.prune(changed, prev_nodes=before)
            self.assertEqual({n["gen16"] for n in after}, {(gen + 1) & 65535})
            self.assertTrue(ax.verify_gen16_transition(before, after))
            stale = copy.deepcopy(after)
            for node in stale:
                node["gen16"] = gen
            with self.assertRaises(ValueError):
                ax.verify_gen16_transition(before, stale)
        self.assertTrue(ax.gen16_newer(0, 65535))
        self.assertFalse(ax.gen16_newer(32768, 0))
        self.assertFalse(ax.gen16_newer(1, 1))
        self.assertFalse(ax.gen16_fresh(0, 65535))
        self.assertTrue(ax.gen16_fresh(0, 0))
        for gen in (-1, 65536, True):
            with self.assertRaises(ValueError):
                ax.prune(raw, gen16=gen)
        before = ax.prune(raw, gen16=9)
        raw[3]["properties"].append({"name": "focused", "value": {"value": True}})
        after = ax.prune(raw, prev_nodes=before)
        self.assertEqual({n["gen16"] for n in after}, {9})
        for with_state in (True, False):
            self.assertEqual(ax.snapshot_sha(before, with_state), ax.snapshot_sha(after, with_state))
        self.assertEqual([n["id"] for n in before], [n["id"] for n in after])
        self.assertEqual(before[2]["state_word"] ^ after[2]["state_word"], ax.BIT_FOCUSED)

    # @anchor:wa.tests.test_ax_bitpack.test_fixture_anchor_id_missing_or_unstable_under_ax_nodeid_shuffling
    def test_fixture_anchor_id_missing_or_unstable_under_ax_nodeid_shuffling(self):
        a = ax.prune(fixture())
        b = ax.prune(fixture_anchor_id_missing_or_unstable_under_ax_nodeid_shuffling())
        self.assertTrue(ax.validate_anchor_stability(a, b))
        for invalid in ("", None, "504"):
            bad = copy.deepcopy(b)
            bad[2]["anchor_id"] = invalid
            with self.assertRaises(ValueError):
                ax.validate_anchor_stability(a, bad)
            state = _state(b)
            sidecar = schema.build_companion_projection(b, state)
            sidecar["elements"][2]["anchor_id"] = invalid
            self.assertTrue(schema.validate_companion_projection(state, sidecar))
        for offset in (0, 500):
            raw = fixture(offset)
            raw[3]["properties"].append({"name": "data-agent-id", "value": {"value": "inbox.item.msg-103"}})
            random.Random(7).shuffle(raw)
            self.assertEqual(ax.prune(raw)[2]["anchor_id"], "inbox.item.msg-103")

    # @anchor:wa.tests.test_ax_bitpack.test_fixture_build_state_violating_NODE_KEYS_exact_lock_without_companion_projection
    def test_fixture_build_state_violating_NODE_KEYS_exact_lock_without_companion_projection(self):
        bad = fixture_build_state_violating_NODE_KEYS_exact_lock_without_companion_projection()
        self.assertIn("nodes[0] keys invalid", schema.validate_state(bad))
        nodes = ax.prune(fixture())
        state = _state(nodes)
        original = schema.dumps(state)
        sidecar = schema.build_companion_projection(nodes, state)
        self.assertFalse(schema.validate_companion_projection(state, sidecar))
        self.assertEqual(schema.dumps(state), original)
        self.assertEqual(set(state["nodes"][0]), set(schema.NODE_KEYS))

    # @anchor:wa.tests.test_ax_bitpack.test_semilattice
    def test_semilattice(self):
        a = schema.PackedElement("ax:abc", None, 0, "a" * 16, None, "button", ax.M_ACT, None)
        b = dataclasses.replace(a, box=(1, 2, 3, 4))
        c = dataclasses.replace(a, mark_id=1, state_word=a.state_word | ax.BIT_FOCUSED)
        merge = schema.merge_packed_elements
        for x, y, z in itertools.permutations((a, b, c)):
            self.assertEqual(merge(x, x), x)
            self.assertEqual(merge(x, y), merge(y, x))
            self.assertEqual(merge(x, merge(y, z)), merge(merge(x, y), z))
            self.assertEqual(merge(x, merge(y, z)), merge(a, merge(b, c)))
        disabled = dataclasses.replace(a, state_word=ax.BIT_DISABLED)
        self.assertTrue(merge(a, disabled).is_bottom)
        for x, y, z in itertools.permutations((a, b, disabled)):
            self.assertEqual(merge(x, merge(y, z)), schema.BOTTOM_ELEMENT)
            self.assertEqual(merge(merge(x, y), z), schema.BOTTOM_ELEMENT)
        for field, value in (("id", "b" * 16), ("anchor_id", "other"), ("gen16", 1), ("role", "link")):
            self.assertEqual(merge(a, dataclasses.replace(a, **{field: value})), schema.BOTTOM_ELEMENT)

    # @anchor:wa.tests.test_ax_bitpack.test_utf16_and_property_fallback
    def test_utf16_and_property_fallback(self):
        self.assertEqual(ax.utf16_len("a😀b"), 4)
        raw = [_n(1, "textbox", "Input", props={"value": "a😀b"})]
        before = ax.prune(raw, gen16=22)
        self.assertEqual(before[0]["value_len"], 4)
        raw[0]["properties"][0]["value"]["value"] = ""
        after = ax.prune(raw, prev_nodes=before)
        self.assertEqual(after[0]["value_len"], 0)
        self.assertEqual(after[0]["gen16"], 22)
        self.assertEqual(ax.snapshot_sha(before), ax.snapshot_sha(after))

    # @anchor:wa.tests.test_ax_bitpack.test_sidecar_binding_rejects_tampering
    def test_sidecar_binding_rejects_tampering(self):
        nodes = ax.prune(fixture())
        state = _state(nodes)
        good = schema.build_tags_sidecar(nodes, state, mark_map={nodes[2]["id"]: 1})
        self.assertFalse(schema.validate_tags_sidecar(state, good))
        for key, value in (("id", "0" * 16), ("role", "link"), ("box", [1, 2, 3, 4]),
                           ("gen16", 1), ("value_len", True), ("mark_id", 25)):
            bad = copy.deepcopy(good)
            bad["elements"][2][key] = value
            self.assertTrue(schema.validate_tags_sidecar(state, bad), key)
        for key, value in (("ax_sha256", "0" * 64), ("srf_state_hash", "0" * 16),
                           ("gen16", True), ("elements", {})):
            bad = copy.deepcopy(good)
            bad[key] = value
            self.assertTrue(schema.validate_tags_sidecar(state, bad), key)
        self.assertEqual(schema.srf_state_hash(good["elements"]), schema.srf_state_hash(list(reversed(good["elements"]))))
        with self.assertRaises(schema.StateError):
            schema.build_tags_sidecar(nodes, state, gen16=1)
        with self.assertRaises(schema.StateError):
            schema.build_tags_sidecar(list(reversed(nodes)), state)

    # @anchor:wa.tests.test_ax_bitpack.test_box_numeric_types_and_mark_uniqueness
    def test_box_numeric_types_and_mark_uniqueness(self):
        nodes = ax.prune(fixture())
        state = _state(nodes)
        state["nodes"][2]["box"] = [1, 2, 3, 4]
        good = schema.build_tags_sidecar(nodes, state)
        self.assertFalse(schema.validate_tags_sidecar(state, good))
        for bad_value in (True, 1.0):
            bad = copy.deepcopy(good)
            bad["elements"][2]["box"][0] = bad_value
            self.assertTrue(schema.validate_tags_sidecar(state, bad))
        bad = copy.deepcopy(good)
        bad["elements"][1]["mark_id"] = 1
        bad["elements"][2]["mark_id"] = 1
        self.assertTrue(schema.validate_tags_sidecar(state, bad))
        bad["elements"][2]["mark_id"] = 2
        self.assertFalse(schema.validate_tags_sidecar(state, bad))

    # @anchor:wa.tests.test_ax_bitpack.test_generation_reparent_and_invalid_freshness
    def test_generation_reparent_and_invalid_freshness(self):
        raw = fixture()
        before = ax.prune(raw, gen16=14)
        raw[1]["childIds"].remove("3")
        raw[0]["childIds"].insert(0, "3")
        # The generic wrapper is pruned, so moving through it is canon-equivalent.
        self.assertEqual(ax.next_gen16_on_structural_change(before, ax.prune(raw)), 14)
        raw[0]["childIds"].append("4")
        raw[2]["childIds"].remove("4")
        after = ax.prune(raw, gen16=14, prev_nodes=before)
        self.assertEqual({n["gen16"] for n in after}, {15})
        for invalid in (True, -1, 65536):
            for a, b in ((invalid, 0), (0, invalid)):
                with self.assertRaises(ValueError):
                    ax.gen16_fresh(a, b)
                with self.assertRaises(ValueError):
                    ax.gen16_newer(a, b)

    # @anchor:wa.tests.test_ax_bitpack.test_anchor_priority_and_hash_channels
    def test_anchor_priority_and_hash_channels(self):
        raw = [_n(1, "textbox", "Input", props={"data-agent-id": " bad anchor ",
                     "anchor_id": " e\u0301 ", "data-testid": "last", "value": "a"})]
        a = ax.prune(raw)
        self.assertEqual(a[0]["anchor_id"], "é")
        self.assertNotIn("value", ax._props(raw[0]))
        for invalid in ("", None, "123", "bad anchor", "x" * 129):
            with self.assertRaises(ValueError):
                ax.validate_anchor_id(invalid)
        sa = schema.build_tags_sidecar(a, _state(a))
        raw[0]["properties"].append({"name": "focused", "value": {"value": True}})
        b = ax.prune(raw)
        sb = schema.build_tags_sidecar(b, _state(b))
        self.assertEqual(sa["ax_sha256"], sb["ax_sha256"])
        self.assertNotEqual(sa["srf_state_hash"], sb["srf_state_hash"])
        self.assertFalse(schema.validate_tags_sidecar(_state(b), sb))
