"""Unit tests for integer-only serialization boundary (D-A8-06) in schema.dumps and _no_float."""
import math
import unittest

from wender.state.schema import StateError, _no_float, dumps


class TestSchemaNoFloatDictKeys(unittest.TestCase):

    # --- Passing Controls ---

    def test_nested_float_value_rejected_with_exact_path(self):
        """Passing control: nested float value under a, sequence index zero, b

        must raise StateError with exact path identifying '$.a[0].b'.
        """
        payload = {"a": [{"b": 1.5}]}
        with self.assertRaises(StateError) as cm:
            _no_float(payload)
        self.assertIn("$.a[0].b", str(cm.exception))

        with self.assertRaises(StateError) as cm:
            dumps(payload)
        self.assertIn("$.a[0].b", str(cm.exception))

    def test_infinity_and_nan_values_rejected(self):
        """Passing control: infinity and NaN float values raise StateError."""
        for val in (float("inf"), float("-inf"), float("nan"), math.inf, -math.inf, math.nan):
            with self.assertRaises(StateError):
                _no_float({"key": val})
            with self.assertRaises(StateError):
                dumps({"key": val})

    # --- Regressions for D-A8-06 ---

    def test_top_level_float_key_rejected(self):
        # Known defect D-A8-06: schema.dumps and _no_float fail to reject float dict keys
        payload = {1.5: "val"}
        with self.assertRaises(StateError):
            dumps(payload)

    def test_nested_float_key_rejected(self):
        # Known defect D-A8-06: schema.dumps and _no_float fail to reject nested float dict keys
        payload = {"outer": {0.25: "val"}}
        with self.assertRaises(StateError):
            dumps(payload)

    def test_float_key_in_list_rejected(self):
        # Known defect D-A8-06: schema.dumps and _no_float fail to reject float dict keys inside lists
        payload = [{"a": 1}, {3.14: "b"}]
        with self.assertRaises(StateError):
            dumps(payload)

    def test_mixed_float_string_keys_rejected(self):
        # Known defect D-A8-06: schema.dumps fails to catch float dict keys before json.dumps,
        # leading to raw TypeError during key sorting or unwanted stringification
        payload = {"str_key": 1, 2.5: "float_key"}
        with self.assertRaises(StateError):
            dumps(payload)

    def test_nonfinite_and_integral_float_keys_in_nested_tuples_rejected(self):
        for key in (0.0, 1.0, 0.25, math.inf, -math.inf, math.nan):
            for check in (_no_float, dumps):
                with self.subTest(key=key, check=check.__name__):
                    payload = {"outer": ([{"text": 1, key: "value"}],)}
                    with self.assertRaises(StateError) as caught:
                        check(payload)
                    self.assertIn("$.outer[0][0].<key>", str(caught.exception))

    def test_float_inside_tuple_key_rejected_before_json_key_error(self):
        payload = {("label", (3, 0.25)): "value"}
        for check in (_no_float, dumps):
            with self.subTest(check=check.__name__):
                with self.assertRaises(StateError) as caught:
                    check(payload)
                self.assertIn("$.<key>[1][1]", str(caught.exception))

    def test_integer_and_bool_values_remain_canonical(self):
        payload = {"number": 3, "items": (0, -2, False), "flag": True}
        self.assertIsNone(_no_float(payload))
        self.assertEqual(dumps(payload), '{"flag":true,"items":[0,-2,false],"number":3}')


if __name__ == "__main__":
    unittest.main()
