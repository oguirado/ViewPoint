"""Unit tests for canonical JSON and hashing helpers in visioncore/canon.py."""
import hashlib
import os
import random
import tempfile
import unittest
from pathlib import Path

from visioncore import canon
from visioncore.errors import VqaError


def _generate_fuzz_obj(rng, depth=0, max_depth=3):
    if depth >= max_depth:
        if rng.choice([True, False]):
            return rng.randint(-1000, 1000)
        return rng.choice(["hello", "world", "foo", "bar", "test", ""])

    kind = rng.choice(["dict", "list", "int", "str"])
    if kind == "int":
        return rng.randint(-10000, 10000)
    elif kind == "str":
        return rng.choice(["a", "b", "c", "key_1", "string with spaces and \n", ""])
    elif kind == "list":
        length = rng.randint(0, 4)
        return [_generate_fuzz_obj(rng, depth + 1, max_depth) for _ in range(length)]
    else:  # dict
        length = rng.randint(0, 4)
        res = {}
        for i in range(length):
            k = rng.choice(["k1", "k2", "key_a", "key_b", f"key_{i}", "x"])
            res[k] = _generate_fuzz_obj(rng, depth + 1, max_depth)
        return res


class TestCanonProps(unittest.TestCase):
    def test_fuzz_dumps_loads_identity_and_canonical(self):
        for seed in range(20):
            rng = random.Random(seed)
            obj = _generate_fuzz_obj(rng)
            s = canon.dumps(obj)
            loaded = canon.loads(s)
            self.assertEqual(loaded, obj)
            self.assertTrue(canon.is_canonical(s))

    def test_dumps_positive(self):
        obj = {"b": 2, "a": 1, "c": [3, 2, 1], "d": {"y": "val", "x": "val"}}
        res = canon.dumps(obj)
        self.assertEqual(res, '{"a":1,"b":2,"c":[3,2,1],"d":{"x":"val","y":"val"}}\n')

    def test_dumps_negative_float(self):
        with self.assertRaises(VqaError) as cm:
            canon.dumps(1.5)
        self.assertEqual(cm.exception.code, "E_FLOAT")

        with self.assertRaises(VqaError) as cm:
            canon.dumps({"a": [1, 2.5]})
        self.assertEqual(cm.exception.code, "E_FLOAT")

    def test_dumps_negative_non_string_key(self):
        with self.assertRaises(VqaError) as cm:
            canon.dumps({123: "val"})
        self.assertEqual(cm.exception.code, "E_SCHEMA")

    def test_loads_positive(self):
        s = '{"a":1,"b":"hello","c":[1,2,3]}\n'
        res = canon.loads(s)
        self.assertEqual(res, {"a": 1, "b": "hello", "c": [1, 2, 3]})

    def test_loads_negative_float(self):
        with self.assertRaises(VqaError) as cm:
            canon.loads('{"a": 1.5}')
        self.assertEqual(cm.exception.code, "E_FLOAT")

        with self.assertRaises(VqaError) as cm:
            canon.loads("3.14159")
        self.assertEqual(cm.exception.code, "E_FLOAT")

    def test_loads_negative_constant(self):
        with self.assertRaises(VqaError) as cm:
            canon.loads('{"a": NaN}')
        self.assertEqual(cm.exception.code, "E_FLOAT")

    def test_loads_negative_invalid_json(self):
        with self.assertRaises(VqaError) as cm:
            canon.loads("{invalid json}")
        self.assertEqual(cm.exception.code, "E_SCHEMA")

    def test_is_canonical_positive(self):
        self.assertTrue(canon.is_canonical('{"a":1,"b":2}\n'))
        self.assertTrue(canon.is_canonical('["a","b",1,2]\n'))

    def test_is_canonical_negative(self):
        # Missing trailing newline
        self.assertFalse(canon.is_canonical('{"a":1}'))
        # Extra whitespace after colon
        self.assertFalse(canon.is_canonical('{"a": 1}\n'))
        # Unsorted keys
        self.assertFalse(canon.is_canonical('{"b":2,"a":1}\n'))
        # Float value
        self.assertFalse(canon.is_canonical('{"a":1.0}\n'))
        # Invalid JSON
        self.assertFalse(canon.is_canonical("not json\n"))

    def test_sha256_bytes_positive(self):
        data = b"visioncore test bytes"
        expected = hashlib.sha256(data).hexdigest()
        self.assertEqual(canon.sha256_bytes(data), expected)
        self.assertEqual(
            canon.sha256_bytes(b"hello"),
            "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824",
        )

    def test_sha256_bytes_negative(self):
        with self.assertRaises(TypeError):
            canon.sha256_bytes("string, not bytes")

    def test_sha256_obj_positive_and_key_order_invariance(self):
        obj1 = {"a": 1, "b": 2, "c": {"x": 10, "y": 20}}
        obj2 = {"b": 2, "c": {"y": 20, "x": 10}, "a": 1}
        h1 = canon.sha256_obj(obj1)
        h2 = canon.sha256_obj(obj2)
        self.assertEqual(h1, h2)

        expected = hashlib.sha256(canon.dumps(obj1).encode("utf-8")).hexdigest()
        self.assertEqual(h1, expected)

    def test_sha256_obj_negative(self):
        with self.assertRaises(VqaError) as cm:
            canon.sha256_obj({"a": 1.23})
        self.assertEqual(cm.exception.code, "E_FLOAT")

    def test_write_atomic_positive(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "sub" / "output.json"
            target.parent.mkdir(parents=True, exist_ok=True)
            data_str = '{"status":"ok"}\n'

            canon.write_atomic(target, data_str)
            self.assertEqual(target.read_text(encoding="utf-8"), data_str)

            # Check that no staging file remains in tmpdir or sub
            tmp_files = [f for f in os.listdir(target.parent) if f.endswith(".tmp")]
            self.assertEqual(tmp_files, [])

            # Test bytes input
            data_bytes = b"binary data"
            canon.write_atomic(target, data_bytes)
            self.assertEqual(target.read_bytes(), data_bytes)
            tmp_files_2 = [f for f in os.listdir(target.parent) if f.endswith(".tmp")]
            self.assertEqual(tmp_files_2, [])

    def test_write_atomic_negative(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            non_existent = Path(tmpdir) / "non_existent_dir" / "file.txt"
            with self.assertRaises(VqaError) as cm:
                canon.write_atomic(non_existent, "data")
            self.assertEqual(cm.exception.code, "E_IO")

    def test_load_schema_positive(self):
        schema = canon.load_schema("vqa.frame/v1")
        self.assertIsInstance(schema, dict)
        self.assertIn("$schema", schema)

        schema_vmem = canon.load_schema("vmem.answer/v1")
        self.assertIsInstance(schema_vmem, dict)

    def test_load_schema_negative(self):
        with self.assertRaises(VqaError) as cm:
            canon.load_schema("invalid_id_no_slash")
        self.assertEqual(cm.exception.code, "E_SCHEMA")

        with self.assertRaises(VqaError) as cm:
            canon.load_schema("vqa.frame/v999999")
        self.assertEqual(cm.exception.code, "E_SCHEMA")

        with self.assertRaises(VqaError) as cm:
            canon.load_schema("nonexistent.schema/v1")
        self.assertEqual(cm.exception.code, "E_SCHEMA")


if __name__ == "__main__":
    unittest.main()
