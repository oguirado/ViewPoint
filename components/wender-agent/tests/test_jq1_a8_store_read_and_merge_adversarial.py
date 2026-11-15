"""Adversarial store read and merge boundary tests for Store._read, _maps, _skills, and _guard."""
import hashlib
import tempfile
import unittest
from pathlib import Path

from wender.remember.store import Store, StoreCorrupt, StoreRefused
from wender.state.schema import dumps


def make_record(kind, body, seq=1, schema="wender.store/v1", extra_keys=None):
    rec = {"schema": schema, "kind": kind, "seq": seq, "body": body}
    if extra_keys:
        rec.update(extra_keys)
    unsigned = {k: v for k, v in rec.items() if k != "line_sha256"}
    rec["line_sha256"] = hashlib.sha256(dumps(unsigned).encode("utf-8")).hexdigest()
    return dumps(rec) + "\n"


def valid_node_body(program="editor", screen_fp="screen", ax_stable_sha="ax", seen=1):
    key = "%s:%s" % (screen_fp, ax_stable_sha)
    return {
        "program": program,
        "key": key,
        "node": {"screen_fp": screen_fp, "ax_stable_sha": ax_stable_sha, "seen": seen},
    }


def valid_skill_body(skill_id="open_dialog", version=1):
    return {
        "id": skill_id,
        "intent": "open dialog",
        "params": {},
        "steps": [{"path": "ax"}],
        "checkpoints": ["dialog_open"],
        "provenance": {"source": "unit_test"},
        "evidence": {"reproduced": True, "verified_by": "test_runner", "version": version},
    }


class TestStoreReadAndMergeAdversarial(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)
        self.store = Store(self.directory)

    def test_read_varied_record_keys_rejected(self):
        body = valid_node_body()
        record_str = make_record("map_node", body, seq=1, extra_keys={"unexpected": "value"})
        (self.directory / "program_map.jsonl").write_text(record_str, encoding="utf-8")
        with self.assertRaisesRegex(StoreCorrupt, r"program_map\.jsonl line 1: record keys"):
            self.store.load_map("editor")

    def test_read_schema_v0_rejected(self):
        body = valid_node_body()
        record_str = make_record("map_node", body, seq=1, schema="wender.store/v0")
        (self.directory / "program_map.jsonl").write_text(record_str, encoding="utf-8")
        with self.assertRaisesRegex(StoreCorrupt, r"program_map\.jsonl line 1: schema"):
            self.store.load_map("editor")

    def test_read_disallowed_kind_skill_in_program_map_rejected(self):
        body = valid_skill_body()
        record_str = make_record("skill", body, seq=1)
        (self.directory / "program_map.jsonl").write_text(record_str, encoding="utf-8")
        with self.assertRaisesRegex(StoreCorrupt, r"program_map\.jsonl line 1: kind/body"):
            self.store.load_map("editor")

    def test_read_non_dictionary_body_rejected(self):
        record_str = make_record("map_node", ["not", "a", "dict"], seq=1)
        (self.directory / "program_map.jsonl").write_text(record_str, encoding="utf-8")
        with self.assertRaisesRegex(StoreCorrupt, r"program_map\.jsonl line 1: kind/body"):
            self.store.load_map("editor")

    def test_read_guard_forbidden_frame_text_leak_rejected(self):
        body = valid_node_body()
        body["frame"] = "text_leak_only"
        record_str = make_record("map_node", body, seq=1)
        (self.directory / "program_map.jsonl").write_text(record_str, encoding="utf-8")
        with self.assertRaisesRegex(StoreCorrupt, r"program_map\.jsonl line 1: forbidden body key: frame"):
            self.store.load_map("editor")

    def test_second_line_failure_after_valid_first_record(self):
        line1 = make_record("map_node", valid_node_body(), seq=1)
        line2 = make_record("map_node", valid_node_body(), seq=2, schema="wender.store/v0")
        (self.directory / "program_map.jsonl").write_text(line1 + line2, encoding="utf-8")
        with self.assertRaisesRegex(StoreCorrupt, r"program_map\.jsonl line 2: schema"):
            self.store.load_map("editor")

    def test_maps_node_negative_seen_rejected(self):
        line1 = make_record("map_node", valid_node_body(), seq=1)
        bad_body = valid_node_body(seen=-1)
        line2 = make_record("map_node", bad_body, seq=2)
        (self.directory / "program_map.jsonl").write_text(line1 + line2, encoding="utf-8")
        with self.assertRaisesRegex(StoreCorrupt, r"program_map\.jsonl line 2: seen must be a non-negative integer"):
            self.store.load_map("editor")

    def test_maps_node_wrong_key_identity_rejected(self):
        body = valid_node_body()
        body["key"] = "wrong:key"
        record_str = make_record("map_node", body, seq=1)
        (self.directory / "program_map.jsonl").write_text(record_str, encoding="utf-8")
        with self.assertRaisesRegex(StoreCorrupt, r"program_map\.jsonl line 1: node key"):
            self.store.load_map("editor")

    def test_maps_edge_missing_changed_key_rejected(self):
        line1 = make_record("map_node", valid_node_body(), seq=1)
        edge_body = {
            "program": "editor",
            "edge": {"pre": "screen:ax", "post": "screen:ax", "action": {"click": "button"}},
        }
        line2 = make_record("map_edge", edge_body, seq=2)
        (self.directory / "program_map.jsonl").write_text(line1 + line2, encoding="utf-8")
        with self.assertRaisesRegex(StoreCorrupt, r"program_map\.jsonl line 2: edge fields"):
            self.store.load_map("editor")

    def test_maps_edge_unknown_endpoint_rejected(self):
        line1 = make_record("map_node", valid_node_body(), seq=1)
        edge_body = {
            "program": "editor",
            "edge": {"pre": "screen:ax", "post": "z:z", "action": {"click": "button"}, "changed": None},
        }
        line2 = make_record("map_edge", edge_body, seq=2)
        (self.directory / "program_map.jsonl").write_text(line1 + line2, encoding="utf-8")
        with self.assertRaisesRegex(StoreCorrupt, r"program_map\.jsonl line 2: 'unknown state z:z'"):
            self.store.load_map("editor")

    def test_skills_non_string_id_rejected(self):
        body = valid_skill_body()
        body["id"] = 12345
        record_str = make_record("skill", body, seq=1)
        (self.directory / "skills.jsonl").write_text(record_str, encoding="utf-8")
        with self.assertRaisesRegex(StoreCorrupt, r"skills\.jsonl line 1: skill id must be a string"):
            self.store.load_skills()

    def test_append_skill_cyclic_dictionary_refused_before_file_creation(self):
        skill_data = valid_skill_body()
        cycle_dict = {}
        cycle_dict["self"] = cycle_dict
        skill_data["params"]["nested"] = cycle_dict

        with self.assertRaisesRegex(StoreRefused, r"cyclic body"):
            self.store.append_skill(skill_data)

        self.assertFalse((self.directory / "skills.jsonl").exists())


if __name__ == "__main__":
    unittest.main()
