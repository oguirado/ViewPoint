"""Persistence contracts, integrity failures and STORAGE LAW negatives."""
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from wender.remember.program_map import ProgramMap, state_key
from wender.remember.skills import SkillRejected
from wender.remember.store import Store, StoreCorrupt, StoreRefused
from wender.state.schema import dumps


def sample_map(program="editor"):
    result = ProgramMap(program)
    first = result.observe("screen", "ax")
    second = result.observe("dialog", "bx")
    result.observe("screen", "ax")
    result.add_edge(first, {"click": "open"}, second, {"added": ["dialog"]})
    return result


def skill(version=1):
    return {"id": "open", "intent": "open dialog", "params": {},
            "steps": [{"path": "ax"}], "checkpoints": ["dialog"],
            "provenance": {"source": "test"},
            "evidence": {"reproduced": True, "verified_by": "test", "version": version}}


class TestStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)
        self.store = Store(self.directory)

    def test_roundtrip_reopen_and_canonical_lines(self):
        original = sample_map()
        self.store.append_map(original)
        self.assertEqual(Store(self.directory).load_map("editor").to_json(), original.to_json())
        for number, line in enumerate((self.directory / "program_map.jsonl").read_text().splitlines(), 1):
            record = json.loads(line)
            self.assertEqual(record["seq"], number)
            self.assertEqual(line, dumps(record))
            digest = record.pop("line_sha256")
            self.assertEqual(digest, hashlib.sha256(dumps(record).encode()).hexdigest())

    def test_seen_batches_add_edges_deduplicate_and_programs_isolate(self):
        original = sample_map()
        self.store.append_map(original)
        self.store.append_map(original)
        self.store.append_map(sample_map("other"))
        loaded = self.store.load_map("editor")
        self.assertEqual(len(loaded.edges), 1)
        self.assertEqual(loaded.nodes[state_key("screen", "ax")]["seen"], 4)
        self.assertEqual(self.store.load_map("other").to_json(), original.to_json().replace('"editor"', '"other"'))
        self.assertEqual(self.store.load_map("missing").nodes, {})

    def test_tamper_and_seq_rejected_with_line(self):
        for mutation in ("hash", "seq", "json", "body"):
            with self.subTest(mutation=mutation):
                self.store.append_map(sample_map())
                path = self.directory / "program_map.jsonl"
                original = path.read_bytes()
                lines = original.decode().splitlines()
                row = json.loads(lines[1])
                if mutation == "json":
                    lines[1] = "{"
                else:
                    if mutation == "hash":
                        row["body"]["node"]["seen"] += 1
                    else:
                        if mutation == "seq":
                            row["seq"] = 1
                        else:
                            row["body"] = {}
                        row.pop("line_sha256")
                        row["line_sha256"] = hashlib.sha256(dumps(row).encode()).hexdigest()
                    lines[1] = dumps(row)
                path.write_text("\n".join(lines) + "\n")
                with self.assertRaisesRegex(StoreCorrupt, "line 2"):
                    self.store.load_map("editor")
                path.write_bytes(original)

    def test_storage_law_refuses_before_any_write(self):
        self.store.append_map(sample_map())
        path = self.directory / "program_map.jsonl"
        before = path.read_bytes()
        payloads = [b"x", bytearray(b"x"), memoryview(b"x")]
        payloads += [{key: "data"} for key in ("frame", "frames", "pixels", "image", "screenshot", "png")]
        for payload in payloads:
            with self.subTest(payload=repr(payload)):
                bad = sample_map()
                bad.edges[0]["action"] = {"nested": [payload]}
                with self.assertRaises(StoreRefused):
                    self.store.append_map(bad)
                self.assertEqual(path.read_bytes(), before)
                candidate = skill()
                candidate["params"] = {"nested": [payload]}
                with self.assertRaises(StoreRefused):
                    self.store.append_skill(candidate)
                self.assertFalse((self.directory / "skills.jsonl").exists())

    def test_skills_admission_duplicate_and_version(self):
        rejected = skill()
        rejected["evidence"]["reproduced"] = False
        with self.assertRaises(SkillRejected):
            self.store.append_skill(rejected)
        self.assertFalse((self.directory / "skills.jsonl").exists())
        self.store.append_skill(skill())
        path = self.directory / "skills.jsonl"
        before = path.read_bytes()
        self.store.append_skill(skill())
        self.assertEqual(path.read_bytes(), before)
        conflicting = skill()
        conflicting["intent"] = "different"
        with self.assertRaises(StoreRefused):
            self.store.append_skill(conflicting)
        self.assertEqual(path.read_bytes(), before)
        self.store.append_skill(skill(2))
        stale = skill(1)
        with self.assertRaises(StoreRefused):
            self.store.append_skill(stale)
        no_version = skill()
        del no_version["evidence"]["version"]
        with self.assertRaises(StoreRefused):
            self.store.append_skill(no_version)
        other = skill()
        other["id"] = "aaa"
        self.store.append_skill(other)
        self.assertEqual([x["id"] for x in self.store.load_skills()], ["aaa", "open"])
        self.assertEqual(self.store.load_skills()[1], skill(2))

    def test_skill_versions_need_integer_evidence(self):
        self.store.append_skill(skill())
        path = self.directory / "skills.jsonl"
        before = path.read_bytes()
        for version in (True, "2", None, 0):
            candidate = skill(version)
            with self.assertRaises(StoreRefused):
                self.store.append_skill(candidate)
            self.assertEqual(path.read_bytes(), before)

    def test_corrupt_skill_blocks_load_append_and_compact(self):
        self.store.append_skill(skill())
        path = self.directory / "skills.jsonl"
        corrupted = path.read_bytes().replace(b'"open"', b'"evil"')
        path.write_bytes(corrupted)
        for operation in (self.store.load_skills, lambda: self.store.append_skill(skill(2)),
                          self.store.compact):
            with self.assertRaisesRegex(StoreCorrupt, "line 1"):
                operation()
            self.assertEqual(path.read_bytes(), corrupted)

    def test_failed_compaction_replace_keeps_original_and_cleans_temp(self):
        self.store.append_map(sample_map())
        self.store.append_map(sample_map())
        path = self.directory / "program_map.jsonl"
        before = path.read_bytes()
        with patch("wender.remember.store.os.replace", side_effect=OSError("replace failed")):
            with self.assertRaises(OSError):
                self.store.compact()
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(list(self.directory.iterdir()), [path])

    def test_compact_preserves_content_and_order_fsync_replace(self):
        for _ in range(3):
            self.store.append_map(sample_map())
        self.store.append_skill(skill())
        self.store.append_skill(skill(2))
        before_map = self.store.load_map("editor").to_json()
        before_skills = self.store.load_skills()
        with patch("wender.remember.store.os.replace", wraps=__import__("os").replace) as replace:
            self.store.compact()
            self.assertEqual(replace.call_count, 2)
            for call in replace.call_args_list:
                self.assertEqual(Path(call.args[0]).parent, Path(call.args[1]).parent)
        self.assertEqual(self.store.load_map("editor").to_json(), before_map)
        self.assertEqual(self.store.load_skills(), before_skills)
        first = {p.name: p.read_bytes() for p in self.directory.iterdir()}
        self.store.compact()
        self.assertEqual({p.name: p.read_bytes() for p in self.directory.iterdir()}, first)
        self.assertEqual(len(first["program_map.jsonl"].splitlines()), 3)
        with patch("wender.remember.store.os.fsync") as sync:
            self.store.append_skill(skill(3))
            sync.assert_called()


if __name__ == "__main__":
    unittest.main()
