"""Adversarial tests for state key injectivity defects D-A8-01 and D-A8-02."""
import hashlib
import tempfile
import unittest
from pathlib import Path

from wender.remember.program_map import ProgramMap, canonicalize_state_key, state_key
from wender.remember.store import Store, StoreCorrupt
from wender.state.schema import dumps


class TestStateKeyInjectivity(unittest.TestCase):

    def test_explicit_legacy_reader_returns_plain_typed_key(self):
        expected = '["wender.state-key/v1",["str","screen"],["str","ax"]]'
        for stored in ("screen:ax", expected):
            actual = canonicalize_state_key(stored, "screen", "ax")
            self.assertIs(type(actual), str)
            self.assertEqual(actual, expected)
        self.assertEqual(
            canonicalize_state_key("é:設定", "é", "設定"),
            '["wender.state-key/v1",["str","é"],["str","設定"]]')

    def test_legacy_sentinels_require_the_supplied_raw_identity(self):
        raw_values = (None, "", "-", 0, False)
        keys = [canonicalize_state_key("-:ax", value, "ax") for value in raw_values]
        self.assertEqual(len(set(keys)), 5)
        self.assertEqual(keys[0], '["wender.state-key/v1",["none",null],["str","ax"]]')
        self.assertEqual(keys[4], '["wender.state-key/v1",["bool",false],["str","ax"]]')
        self.assertNotEqual(canonicalize_state_key("1:ax", 1, "ax"),
                            canonicalize_state_key("1:ax", "1", "ax"))

    def test_legacy_colons_are_validated_without_guessing_boundaries(self):
        first = canonicalize_state_key("fp:1:2", "fp:1", "2")
        second = canonicalize_state_key("fp:1:2", "fp", "1:2")
        self.assertEqual(first, '["wender.state-key/v1",["str","fp:1"],["str","2"]]')
        self.assertEqual(second, '["wender.state-key/v1",["str","fp"],["str","1:2"]]')
        self.assertNotEqual(first, second)

    def test_legacy_reader_rejects_wrong_spelling_identity_and_raw_types(self):
        canonical = state_key("screen", "ax")
        for stored in ("wrong:key", "screen:ax ", "screen:ax:extra",
                       canonical.replace(',["str","screen"]', ', ["str","screen"]'),
                       state_key("other", "ax"), "\ud800", None, b"screen:ax"):
            with self.subTest(stored=repr(stored)):
                with self.assertRaises(ValueError):
                    canonicalize_state_key(stored, "screen", "ax")
        for raw in (1.0, [], {}, b"screen", "\ud800"):
            with self.subTest(raw=repr(raw)):
                with self.assertRaises(ValueError):
                    canonicalize_state_key("screen:ax", raw, "ax")
        with self.assertRaises(ValueError):
            canonicalize_state_key("False:ax", False, "ax")

    def test_typed_v1_literal_keys_and_rejected_components(self):
        self.assertEqual(state_key(None, False),
                         '["wender.state-key/v1",["none",null],["bool",false]]')
        self.assertEqual(state_key(0, "a:b"),
                         '["wender.state-key/v1",["int",0],["str","a:b"]]')
        self.assertNotEqual(state_key(True, "x"), state_key(1, "x"))
        self.assertNotEqual(state_key("1", "x"), state_key(1, "x"))
        for value in ([], {}, (), 1.0, b"x", "\ud800"):
            with self.subTest(value=repr(value)):
                with self.assertRaises(ValueError):
                    state_key(value, "x")
                with self.assertRaises(ValueError):
                    state_key("x", value)

    # Defect D-A8-01: Falsey and hyphen state component collapse in state_key
    def test_falsey_state_key_injectivity_defect_d_a8_01(self):
        # D-A8-01: state_key maps None, "", "-", 0, and False to the same fallback "-",
        # causing state key collisions and loss of raw identity in ProgramMap and Store.
        falsey_pairs = [
            (None, "ax1"),
            ("", "ax1"),
            ("-", "ax1"),
            (0, "ax1"),
            (False, "ax1"),
        ]

        # 1. State keys must be distinct for distinct screen_fp values.
        keys = [state_key(fp, ax) for fp, ax in falsey_pairs]
        self.assertEqual(len(keys), len(set(keys)), "state_key returned duplicate keys for distinct falsey inputs")

        # 2. ProgramMap.observe must create separate nodes retaining every raw identity.
        pm = ProgramMap("test_program")
        for fp, ax in falsey_pairs:
            pm.observe(fp, ax)

        self.assertEqual(len(pm.nodes), len(falsey_pairs), "ProgramMap lost distinct nodes due to key collision")
        for fp, ax in falsey_pairs:
            k = state_key(fp, ax)
            self.assertIn(k, pm.nodes)
            self.assertEqual(pm.nodes[k]["screen_fp"], fp)
            self.assertEqual(pm.nodes[k]["ax_stable_sha"], ax)

        # 3. Order independence: observe multiset in opposite orders in fresh maps.
        pm1 = ProgramMap("test_program")
        for fp, ax in falsey_pairs:
            pm1.observe(fp, ax)

        pm2 = ProgramMap("test_program")
        for fp, ax in reversed(falsey_pairs):
            pm2.observe(fp, ax)

        self.assertEqual(len(pm1.nodes), len(falsey_pairs))
        self.assertEqual(len(pm2.nodes), len(falsey_pairs))
        self.assertEqual(pm1.to_json(), pm2.to_json(), "to_json output differs for multiset observed in opposite orders")

        # 4. Persistence via Store in separate TemporaryDirectory instances.
        with tempfile.TemporaryDirectory() as dir1, tempfile.TemporaryDirectory() as dir2:
            store1 = Store(dir1)
            store2 = Store(dir2)

            store1.append_map(pm1)
            store2.append_map(pm2)

            loaded1 = store1.load_map("test_program")
            loaded2 = store2.load_map("test_program")

            self.assertEqual(loaded1.to_json(), loaded2.to_json(), "Loaded maps differ across Store instances")

    # Defect D-A8-02: Embedded colons in state components cause state_key collisions
    def test_colon_state_key_injectivity_defect_d_a8_02(self):
        # D-A8-02: state_key joins screen_fp and ax_stable_sha with a colon without escaping,
        # so ("fp:1", "2") and ("fp", "1:2") collide as "fp:1:2".
        colon_pairs = [
            ("fp:1", "2"),
            ("fp", "1:2"),
        ]

        # 1. State keys must be distinct.
        keys = [state_key(fp, ax) for fp, ax in colon_pairs]
        self.assertEqual(len(keys), len(set(keys)), "state_key produced duplicate key for embedded colons")

        # 2. ProgramMap.observe must create separate nodes retaining raw identities.
        pm = ProgramMap("test_program")
        for fp, ax in colon_pairs:
            pm.observe(fp, ax)

        self.assertEqual(len(pm.nodes), len(colon_pairs), "ProgramMap collapsed nodes with embedded colons")
        for fp, ax in colon_pairs:
            k = state_key(fp, ax)
            self.assertIn(k, pm.nodes)
            self.assertEqual(pm.nodes[k]["screen_fp"], fp)
            self.assertEqual(pm.nodes[k]["ax_stable_sha"], ax)

        # 3. Order independence: observe multiset in opposite orders in fresh maps.
        pm1 = ProgramMap("test_program")
        for fp, ax in colon_pairs:
            pm1.observe(fp, ax)

        pm2 = ProgramMap("test_program")
        for fp, ax in reversed(colon_pairs):
            pm2.observe(fp, ax)

        self.assertEqual(len(pm1.nodes), len(colon_pairs))
        self.assertEqual(len(pm2.nodes), len(colon_pairs))
        self.assertEqual(pm1.to_json(), pm2.to_json(), "to_json output differs for colon pairs observed in opposite orders")

        # 4. Persistence via Store in separate TemporaryDirectory instances.
        with tempfile.TemporaryDirectory() as dir1, tempfile.TemporaryDirectory() as dir2:
            store1 = Store(dir1)
            store2 = Store(dir2)

            store1.append_map(pm1)
            store2.append_map(pm2)

            loaded1 = store1.load_map("test_program")
            loaded2 = store2.load_map("test_program")

            self.assertEqual(loaded1.to_json(), loaded2.to_json(), "Loaded colon maps differ across Store instances")

    def test_store_rejects_conflicting_legacy_node_identity(self):
        # A valid legacy spelling is normalized from its supplied raw identity.
        # Reusing that spelling for another typed identity is ambiguous and must
        # fail on the second record, without creating an alias node.
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "program_map.jsonl"

            # Construct record 1: key "-:ax1", screen_fp None, ax_stable_sha "ax1"
            body1 = {
                "program": "test_program",
                "key": "-:ax1",
                "node": {"screen_fp": None, "ax_stable_sha": "ax1", "seen": 1},
            }
            rec1 = {
                "schema": "wender.store/v1",
                "kind": "map_node",
                "seq": 1,
                "body": body1,
            }
            rec1["line_sha256"] = hashlib.sha256(dumps(rec1).encode("utf-8")).hexdigest()

            # Construct record 2: same key "-:ax1", but conflicting raw screen_fp ""
            body2 = {
                "program": "test_program",
                "key": "-:ax1",
                "node": {"screen_fp": "", "ax_stable_sha": "ax1", "seen": 1},
            }
            rec2 = {
                "schema": "wender.store/v1",
                "kind": "map_node",
                "seq": 2,
                "body": body2,
            }
            rec2["line_sha256"] = hashlib.sha256(dumps(rec2).encode("utf-8")).hexdigest()

            content = dumps(rec1) + "\n" + dumps(rec2) + "\n"
            path.write_text(content, encoding="utf-8")

            store = Store(tmp_dir)
            with self.assertRaisesRegex(StoreCorrupt, "line 2: node identity conflict"):
                store.load_map("test_program")


if __name__ == "__main__":
    unittest.main()
