"""Adversarial pin tests for wender.remember.store contract."""
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path

try:
    from wender.remember.store import Store, StoreCorrupt, StoreRefused
except ImportError as _err:
    if os.environ.get("WA1_PIN_ALLOW_SKIP") == "1":
        Store = None
        StoreCorrupt = Exception
        StoreRefused = Exception
        _import_err = _err
    else:
        raise
else:
    _import_err = None

from wender.remember.program_map import ProgramMap
from wender.state.schema import dumps


def _make_skill(skill_id="skill_a", version=1):
    return {
        "id": skill_id,
        "intent": "execute task",
        "params": {},
        "steps": [{"path": "ax"}],
        "checkpoints": ["done"],
        "provenance": {"source": "unit_test"},
        "evidence": {"reproduced": True, "verified_by": "tester", "version": version},
    }


def _make_map(program="editor"):
    pm = ProgramMap(program)
    k1 = pm.observe("screen", "ax_tree_1")
    k2 = pm.observe("dialog", "ax_tree_2")
    pm.add_edge(k1, {"action": "open"}, k2, {"opened": True})
    return pm


def _make_record(kind, body, seq):
    record = {"schema": "wender.store/v1", "kind": kind, "seq": seq, "body": body}
    record["line_sha256"] = hashlib.sha256(dumps(record).encode("utf-8")).hexdigest()
    return dumps(record)


@unittest.skipIf(_import_err is not None and os.environ.get("WA1_PIN_ALLOW_SKIP") == "1",
                 "wa1-spark-remember-store module wender.remember.store not found")
class TestWa1StorePins(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)
        self.store = Store(self.directory)

    def test_truncated_last_line_without_newline(self):
        """Kills mutant accepting truncated JSON line or record without a trailing newline character."""
        body = {"program": "editor", "key": "k1",
                "node": {"screen_fp": "s1", "ax_stable_sha": "a1", "seen": 1}}
        record_str = _make_record("map_node", body, 1)
        path = self.directory / "program_map.jsonl"
        path.write_bytes(record_str.encode("utf-8"))  # Missing trailing \n

        with self.assertRaisesRegex(StoreCorrupt, r"program_map\.jsonl line 1"):
            self.store.load_map("editor")

    def test_empty_file_loads_as_initial_state(self):
        """Kills mutant failing on empty jsonl file or incorrectly raising StoreCorrupt on empty file."""
        (self.directory / "program_map.jsonl").write_bytes(b"")
        (self.directory / "skills.jsonl").write_bytes(b"")

        pm = self.store.load_map("editor")
        self.assertEqual(pm.program, "editor")
        self.assertEqual(pm.nodes, {})
        self.assertEqual(pm.edges, [])
        self.assertEqual(self.store.load_skills(), [])

    def test_wrong_line_sha256_raises_corrupt(self):
        """Kills mutant ignoring line_sha256 digest verification or always returning true."""
        body = {"program": "editor", "key": "k1",
                "node": {"screen_fp": "s1", "ax_stable_sha": "a1", "seen": 1}}
        record = {"schema": "wender.store/v1", "kind": "map_node", "seq": 1, "body": body,
                  "line_sha256": "0000000000000000000000000000000000000000000000000000000000000000"}
        path = self.directory / "program_map.jsonl"
        path.write_text(dumps(record) + "\n", encoding="utf-8")

        with self.assertRaisesRegex(StoreCorrupt, r"program_map\.jsonl line 1"):
            self.store.load_map("editor")

    def test_duplicate_and_non_increasing_seq_raises_corrupt(self):
        """Kills mutant allowing non-increasing, zero, or duplicate sequence numbers."""
        body = {"program": "editor", "key": "k1",
                "node": {"screen_fp": "s1", "ax_stable_sha": "a1", "seen": 1}}
        line1 = _make_record("map_node", body, 1) + "\n"
        line2 = _make_record("map_node", body, 1) + "\n"  # Duplicate seq 1
        path = self.directory / "program_map.jsonl"
        path.write_text(line1 + line2, encoding="utf-8")

        with self.assertRaisesRegex(StoreCorrupt, r"program_map\.jsonl line 2"):
            self.store.load_map("editor")

    def test_storage_law_nested_forbidden_key_3_levels_deep(self):
        """Kills mutant checking forbidden frame keys only at top or shallow level."""
        for forbidden_key in ("frame", "frames", "pixels", "image", "screenshot", "png"):
            with self.subTest(key=forbidden_key):
                bad_skill = _make_skill("s_forbidden")
                bad_skill["params"] = {"level1": {"level2": {forbidden_key: "raw_image_data"}}}
                with self.assertRaises(StoreRefused):
                    self.store.append_skill(bad_skill)
                self.assertFalse((self.directory / "skills.jsonl").exists())

    def test_storage_law_bytes_inside_list(self):
        """Kills mutant failing to inspect list or tuple elements for bytes, bytearray, or memoryview types."""
        for binary_val in (b"raw_bytes", bytearray(b"raw_bytes"), memoryview(b"raw_bytes")):
            with self.subTest(type=type(binary_val).__name__):
                bad_skill = _make_skill("s_binary")
                bad_skill["params"] = {"nested_list": [1, 2, binary_val]}
                with self.assertRaises(StoreRefused):
                    self.store.append_skill(bad_skill)
                self.assertFalse((self.directory / "skills.jsonl").exists())

    def test_float_in_body_refused_by_dumps_or_store(self):
        """Kills mutant allowing float values to leak into store body or JSON state."""
        bad_map = ProgramMap("editor")
        k1 = bad_map.observe("screen", "ax1")
        bad_map.nodes[k1]["seen"] = 1.5  # float
        with self.assertRaises((ValueError, TypeError, StoreRefused)):
            self.store.append_map(bad_map)
        self.assertFalse((self.directory / "program_map.jsonl").exists())

    def test_unicode_program_names_roundtrip(self):
        """Kills mutant failing on non-ASCII unicode program names or corrupting encoding."""
        program_name = "editor_🤖_测试_v1"
        pm = _make_map(program_name)
        self.store.append_map(pm)

        loaded = self.store.load_map(program_name)
        self.assertEqual(loaded.to_json(), pm.to_json())

    def test_compact_interrupted_stray_temp_file_ignored(self):
        """Kills mutant attempting to load non-jsonl or stray temporary compaction files in directory."""
        pm = _make_map("editor")
        self.store.append_map(pm)

        # Create stray temporary compaction files
        stray_tmp = self.directory / "tmp12345.tmp"
        stray_tmp.write_text("corrupted temporary content", encoding="utf-8")
        stray_jsonl_tmp = self.directory / ".tmp_compact.jsonl"
        stray_jsonl_tmp.write_text("junk", encoding="utf-8")

        loaded = self.store.load_map("editor")
        self.assertEqual(loaded.to_json(), pm.to_json())

    def test_two_store_instances_strictly_increasing_seq(self):
        """Kills mutant using local instance counter that resets sequence numbers across Store instances."""
        store1 = Store(self.directory)
        pm = _make_map("editor")
        store1.append_map(pm)

        store2 = Store(self.directory)
        skill_item = _make_skill("skill_1")
        store2.append_skill(skill_item)

        map_lines = (self.directory / "program_map.jsonl").read_text(encoding="utf-8").splitlines()
        skill_lines = (self.directory / "skills.jsonl").read_text(encoding="utf-8").splitlines()

        rec_map = json.loads(map_lines[0])
        rec_skill = json.loads(skill_lines[0])

        self.assertEqual(rec_map["seq"], 1)
        self.assertEqual(rec_skill["seq"], 1)

        # Now append another map with store2
        store2.append_map(pm)
        map_lines_after = (self.directory / "program_map.jsonl").read_text(encoding="utf-8").splitlines()
        rec_map_2 = json.loads(map_lines_after[-1])
        self.assertGreater(rec_map_2["seq"], rec_map["seq"])

    def test_load_skills_returns_sorted_by_id(self):
        """Kills mutant returning skills in insertion order or unsorted order instead of sorted by skill id."""
        self.store.append_skill(_make_skill("skill_z"))
        self.store.append_skill(_make_skill("skill_a"))
        self.store.append_skill(_make_skill("skill_m"))

        loaded = self.store.load_skills()
        loaded_ids = [s["id"] for s in loaded]
        self.assertEqual(loaded_ids, ["skill_a", "skill_m", "skill_z"])


if __name__ == "__main__":
    unittest.main()
