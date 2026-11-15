"""Adversarial claim-pinning tests for ax.normalize_name escaped byte limit."""
import unittest


class TestJq1A6EscapedByteLimit(unittest.TestCase):
    def test_healthy_control_short_string(self):
        """Healthy control: short inputs pass without exceeding byte limit or expectedFailure."""
        from wender.perceive import ax
        raw = "short name"
        res = ax.normalize_name(raw)
        encoded = res.encode("utf-8")
        self.assertLessEqual(len(encoded), ax.MAX_NAME_BYTES)
        self.assertEqual(res, "short name")

    def test_healthy_control_escaped_short_string(self):
        """Healthy control: short string with pipes and backslashes escaped correctly."""
        from wender.perceive import ax
        raw = "a|b\\c"
        res = ax.normalize_name(raw)
        encoded = res.encode("utf-8")
        self.assertLessEqual(len(encoded), ax.MAX_NAME_BYTES)
        self.assertEqual(res, "a\\|b\\\\c")

    def test_defect_normalize_name_cut_before_escape_64_pipes(self):
        # defect normalize_name cut-before-escape
        from wender.perceive import ax
        raw = "|" * 64
        res = ax.normalize_name(raw)
        encoded = res.encode("utf-8")
        self.assertLessEqual(len(encoded), ax.MAX_NAME_BYTES)

    def test_defect_normalize_name_cut_before_escape_multibyte_and_pipes(self):
        # defect normalize_name cut-before-escape
        from wender.perceive import ax
        raw = ("é" * 30) + "||||"
        res = ax.normalize_name(raw)
        encoded = res.encode("utf-8")
        self.assertLessEqual(len(encoded), ax.MAX_NAME_BYTES)

    def test_defect_normalize_name_cut_before_escape_64_backslashes(self):
        # defect normalize_name cut-before-escape
        from wender.perceive import ax
        raw = "\\" * 64
        res = ax.normalize_name(raw)
        encoded = res.encode("utf-8")
        self.assertLessEqual(len(encoded), ax.MAX_NAME_BYTES)

    def test_complete_escape_tokens_at_byte_boundary(self):
        from wender.perceive import ax
        cases = [
            ("|" * 64, "\\|" * 32),
            ("\\" * 64, "\\\\" * 32),
            ("a" * 62 + "\\", "a" * 62 + "\\\\"),
            ("a" * 62 + "\\" + "tail", "a" * 62 + "\\\\"),
            ("a" * 63 + "\\", "a" * 63),
            ("a" * 63 + "|", "a" * 63),
            ("a" * 60 + "\\|" + "tail", "a" * 60 + "\\\\\\|"),
            ("a" * 61 + "\\|" + "tail", "a" * 61 + "\\\\"),
        ]
        for raw, expected in cases:
            with self.subTest(raw=raw):
                result = ax.normalize_name(raw)
                self.assertEqual(result, expected)
                self.assertLessEqual(len(result.encode("utf-8")), ax.MAX_NAME_BYTES)

    def test_normalization_and_complete_codepoints_before_byte_boundary(self):
        from wender.perceive import ax
        cases = [
            (" \t" + "e\u0301" * 31 + "\\\n", "é" * 31 + "\\\\"),
            (" \ta\n\tb|\\ \n", "a b\\|\\\\"),
            ("a" * 63 + "é", "a" * 63),
            ("a" * 61 + "é|", "a" * 61 + "é"),
            ("a" * 60 + "\U0001f600" + "|", "a" * 60 + "\U0001f600"),
        ]
        for raw, expected in cases:
            with self.subTest(raw=raw):
                result = ax.normalize_name(raw)
                self.assertEqual(result, expected)
                self.assertLessEqual(len(result.encode("utf-8")), ax.MAX_NAME_BYTES)

    def test_attribute_serialization_stays_unchanged(self):
        from wender.perceive import ax
        node = ax.prune([{
            "nodeId": "1",
            "role": {"value": "button"},
            "name": {"value": "x|\\é"},
            "properties": [
                {"name": "selected", "value": {"value": True}},
                {"name": "busy", "value": {"value": "a|b\\c,checked=x"}},
                {"name": "focusable", "value": {"value": True}},
            ],
        }])[0]
        self.assertEqual(node["attrs"], (("busy", "a|b\\c,checked=x"),
                                         ("selected", "true")))
        self.assertEqual(ax.canonical_line(node),
                         "0|button|x\\|\\\\é|busy=a|b\\c,checked=x,selected=true")
        self.assertEqual(ax.canonical_line(node, with_state=False), "0|button|x\\|\\\\é|")


if __name__ == "__main__":
    unittest.main()
