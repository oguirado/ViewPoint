import json
import os
import tempfile
import unittest

from bench.oracle import sampler as S

MUTANTS_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))),
    "scripts", "cu", "mutants_proposed", "os-g-hard-rowsink-cap.json"
)
GATE_CMD = "python3 -B -m unittest bench.oracle.tests.test_rowsink_hard bench.oracle.tests.test_sampler"


class TestRowSinkHard(unittest.TestCase):
    def test_exactly_cap_kept_not_truncated(self):
        cap = 100
        sink = S.RowSink(cap=cap)
        row1 = {"a": "x" * 41}  # 50 bytes
        row2 = {"a": "y" * 41}  # 50 bytes
        self.assertEqual(S.row_bytes(row1), 50)
        self.assertEqual(S.row_bytes(row2), 50)

        self.assertTrue(sink.add(row1))
        self.assertTrue(sink.add(row2))
        self.assertEqual(sink.size, 100)
        self.assertFalse(sink.truncated)
        self.assertEqual(len(sink.rows), 2)

    def test_one_byte_over_stops_and_truncates(self):
        cap = 100
        sink = S.RowSink(cap=cap)
        row1 = {"a": "x" * 41}  # 50 bytes
        self.assertTrue(sink.add(row1))

        row2 = {"a": "y" * 42}  # 51 bytes (total would be 101)
        self.assertEqual(S.row_bytes(row2), 51)
        self.assertFalse(sink.add(row2))
        self.assertTrue(sink.truncated)
        self.assertEqual(sink.size, 50)
        self.assertEqual(len(sink.rows), 1)

    def test_single_first_row_larger_than_cap(self):
        cap = 100
        sink = S.RowSink(cap=cap)
        row = {"a": "x" * 92}  # 101 bytes
        self.assertEqual(S.row_bytes(row), 101)
        self.assertFalse(sink.add(row))
        self.assertTrue(sink.truncated)
        self.assertEqual(sink.size, 0)
        self.assertEqual(len(sink.rows), 0)

    def test_once_truncated_later_small_rows_refused_in_sample_loop(self):
        # Once truncated (sink.add returns False), sample loop stops adding rows and keeps sink.truncated set.
        cap = 100
        sink = S.RowSink(cap=cap)
        rows = [
            {"a": "x" * 41},  # 50 bytes: fits
            {"a": "y" * 51},  # 60 bytes: total 110 > 100 -> truncated
            {"a": "z"}        # 10 bytes: would fit in remaining 50B if naive, but loop must stop and refuse
        ]
        kept = []
        for r in rows:
            if not sink.add(r):
                break
            kept.append(r)

        self.assertTrue(sink.truncated)
        self.assertEqual(len(kept), 1)
        self.assertEqual(len(sink.rows), 1)
        self.assertEqual(sink.size, 50)

    def test_row_bytes_equals_write_rows_encoded_length(self):
        rows = [
            {"seq": 1, "t_us": 0, "snap": "abc"},
            {"seq": 2, "t_us": 33333, "snap": "def", "nodes": [[1, 2, 3]]},
        ]
        for r in rows:
            expected_len = S.row_bytes(r)
            with tempfile.TemporaryDirectory() as td:
                p = os.path.join(td, "row.jsonl")
                written = S.write_rows(p, [r])
                file_size = os.path.getsize(p)
                self.assertEqual(expected_len, written)
                self.assertEqual(written, file_size)

    def test_small_cap_from_constructor(self):
        sink = S.RowSink(cap=20)
        self.assertEqual(sink.cap, 20)
        row1 = {"a": "1"}  # 10 bytes
        self.assertEqual(S.row_bytes(row1), 10)
        self.assertTrue(sink.add(row1))
        self.assertEqual(sink.size, 10)
        self.assertFalse(sink.truncated)

        row2 = {"a": "12345"}  # 14 bytes (10 + 14 = 24 > 20)
        self.assertEqual(S.row_bytes(row2), 14)
        self.assertFalse(sink.add(row2))
        self.assertTrue(sink.truncated)

    def test_written_rows_file_never_exceeds_cap(self):
        cap = 200
        sink = S.RowSink(cap=cap)
        for i in range(100):
            r = {"seq": i, "data": "payload_" + str(i)}
            if not sink.add(r):
                break

        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "rows.jsonl")
            written_bytes = S.write_rows(p, sink.rows, cap=cap)
            file_size = os.path.getsize(p)
            self.assertLessEqual(written_bytes, cap)
            self.assertLessEqual(file_size, cap)

        # Also verify write_rows raises ValueError if rows exceed cap
        oversized_rows = [{"a": "x" * 200}]
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "rows.jsonl")
            with self.assertRaises(ValueError):
                S.write_rows(p, oversized_rows, cap=cap)

    def test_proposed_mutants_structure(self):
        self.assertTrue(os.path.exists(MUTANTS_PATH), f"Missing {MUTANTS_PATH}")
        with open(MUTANTS_PATH, "r") as f:
            data = json.load(f)

        self.assertEqual(data.get("schema"), "os.cu.mutants/v1")
        mutants = data.get("mutants")
        self.assertIsInstance(mutants, list)
        self.assertGreater(len(mutants), 0)

        required_keys = {"id", "file", "find", "replace", "precheck", "oracle"}
        repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

        for m in mutants:
            self.assertEqual(set(m.keys()), required_keys, f"Mutant {m.get('id')} keys mismatch")
            self.assertTrue(m["id"].startswith("prop.rowsink-cap."), f"Mutant ID {m['id']} must start with prop.rowsink-cap.")
            self.assertEqual(m["oracle"], GATE_CMD, f"Mutant {m['id']} oracle command mismatch")

            file_path = os.path.join(repo_root, m["file"])
            self.assertTrue(os.path.exists(file_path), f"File {file_path} in mutant {m['id']} does not exist")

            with open(file_path, "r") as f:
                content = f.read()

            count = content.count(m["find"])
            self.assertEqual(count, 1, f"Mutant {m['id']} find string must occur exactly once in {m['file']}, found {count}")


if __name__ == "__main__":
    unittest.main()
