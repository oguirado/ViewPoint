"""Unit tests for wender.perceive.ax normalize_name, _attrs_txt, and match telemetry contracts."""
import unittest
from wender.perceive import ax


class TestAxNormalizeName(unittest.TestCase):
    def test_normalize_name_healthy_controls(self):
        """Healthy controls: short strings, whitespace collapsing, NFC, escaping within limit."""
        self.assertEqual(ax.normalize_name(""), "")
        self.assertEqual(ax.normalize_name("  a\n\tb  "), "a b")
        # NFC normalization: NFD decomposed "e\u0301" -> NFC composed "\u00e9"
        self.assertEqual(ax.normalize_name("e\u0301"), "\u00e9")
        # Pipe and backslash escaping for short string
        res = ax.normalize_name("a|b\\c")
        self.assertEqual(res, "a\\|b\\\\c")
        self.assertLessEqual(len(res.encode("utf-8")), ax.MAX_NAME_BYTES)

    def test_defect_cut_before_escape_64_pipes(self):
        """Defect: 64 pipes cut before escape causes output bytes (128) to exceed MAX_NAME_BYTES (64)."""
        raw = "|" * 64
        res = ax.normalize_name(raw)
        self.assertLessEqual(len(res.encode("utf-8")), ax.MAX_NAME_BYTES)

    def test_defect_cut_before_escape_64_backslashes(self):
        """Defect: 64 backslashes cut before escape causes output bytes (128) to exceed MAX_NAME_BYTES (64)."""
        raw = "\\" * 64
        res = ax.normalize_name(raw)
        self.assertLessEqual(len(res.encode("utf-8")), ax.MAX_NAME_BYTES)

    def test_defect_cut_before_escape_multibyte_and_pipes(self):
        """Defect: multibyte UTF-8 chars + pipes cut before escape exceeds MAX_NAME_BYTES after escaping."""
        raw = ("é" * 30) + "||||"
        res = ax.normalize_name(raw)
        self.assertLessEqual(len(res.encode("utf-8")), ax.MAX_NAME_BYTES)

    def test_defect_cut_before_escape_decomposed_accents_and_combining_marks(self):
        """Defect: decomposed accents/combining marks + pipes cut before escape exceeds MAX_NAME_BYTES after escaping."""
        # NFD "e\u0301" (2 codepoints, 3 bytes) normalizes to NFC "\u00e9" (2 bytes).
        raw = ("e\u0301" * 20) + ("|" * 30)
        res = ax.normalize_name(raw)
        self.assertLessEqual(len(res.encode("utf-8")), ax.MAX_NAME_BYTES)


class TestAttrsTxtContract(unittest.TestCase):
    def test_attrs_txt_healthy_control(self):
        """Healthy control: _attrs_txt formats sorted (key, value) tuples into key=value joined by comma."""
        attrs = (("disabled", "false"), ("pressed", "true"))
        self.assertEqual(ax._attrs_txt(attrs), "disabled=false,pressed=true")
        self.assertEqual(ax._attrs_txt(()), "")

    def test_attrs_txt_undefined_escaping_ausente(self):
        """Declare undefined escaping as AUSENTE: commas/equals in attribute values are not escaped, producing identical output."""
        tuple_with_commas = (("busy", "a,checked=b"),)
        tuple_two_attrs = (("busy", "a"), ("checked", "b"))
        # Existing contract does not escape delimiter characters (S1-F12 review finding)
        out_single = ax._attrs_txt(tuple_with_commas)
        out_double = ax._attrs_txt(tuple_two_attrs)
        self.assertEqual(out_single, "busy=a,checked=b")
        self.assertEqual(out_single, out_double)


class TestAxMatchAndTelemetry(unittest.TestCase):
    def test_match_healthy_controls(self):
        """Healthy controls: L0_EXACT, L1_STABLE, L2_NAME, L3_VARIANT, L4_UNIQUE_ROLE, and ties."""
        raw_nodes = [
            {"nodeId": "1", "role": {"value": "RootWebArea"}, "name": {"value": "Doc"}, "childIds": ["2", "3", "4"]},
            {"nodeId": "2", "role": {"value": "button"}, "name": {"value": "Submit"}, "properties": [{"name": "disabled", "value": {"value": False}}]},
            {"nodeId": "3", "role": {"value": "button"}, "name": {"value": "Submit"}, "properties": [{"name": "disabled", "value": {"value": True}}]},
            {"nodeId": "4", "role": {"value": "textbox"}, "name": {"value": "Search"}},
        ]
        nodes = ax.prune(raw_nodes)

        # L0_EXACT match
        rec_l0 = ax.describe(nodes[1])  # button "Submit" ordinal 0
        res_l0 = ax.match(nodes, rec_l0)
        self.assertEqual(res_l0["level"], "L0_EXACT")
        self.assertEqual(res_l0["candidates"], 1)

        # L1_STABLE match (exact FP differs due to state change)
        raw_modified = [
            {"nodeId": "1", "role": {"value": "RootWebArea"}, "name": {"value": "Doc"}, "childIds": ["2", "3", "4"]},
            {"nodeId": "2", "role": {"value": "button"}, "name": {"value": "Submit"}, "properties": [{"name": "disabled", "value": {"value": True}}]},
            {"nodeId": "3", "role": {"value": "button"}, "name": {"value": "Submit"}, "properties": [{"name": "disabled", "value": {"value": True}}]},
            {"nodeId": "4", "role": {"value": "textbox"}, "name": {"value": "Search"}},
        ]
        nodes_mod = ax.prune(raw_modified)
        res_l1 = ax.match(nodes_mod, rec_l0)
        self.assertEqual(res_l1["level"], "L1_STABLE")

        # L2_NAME match at different ordinal
        rec_l2 = dict(rec_l0, ordinal=5, fp_exact="0" * 16, fp_stable="0" * 16)
        res_l2 = ax.match(nodes, rec_l2)
        self.assertEqual(res_l2["level"], "L2_NAME")
        self.assertEqual(res_l2["candidates"], 2)

        # L3_VARIANT match
        rec_l3 = {"role": "button", "name": "Send", "ordinal": 0, "name_variants": ["Submit"], "fp_exact": "0" * 16, "fp_stable": "0" * 16}
        res_l3 = ax.match(nodes, rec_l3)
        self.assertEqual(res_l3["level"], "L3_VARIANT")

        # L4_UNIQUE_ROLE match
        rec_l4 = {"role": "textbox", "name": "Input", "ordinal": 0, "fp_exact": "0" * 16, "fp_stable": "0" * 16}
        res_l4 = ax.match(nodes, rec_l4)
        self.assertEqual(res_l4["level"], "L4_UNIQUE_ROLE")

    def test_defect_l2_fallback_telemetry_stale_count(self):
        """Defect S1-F8: L2_NAME fallback to at_ord records candidates=0 in ladder_tried despite selecting 1 candidate."""
        raw_nodes = [
            {"nodeId": "1", "role": {"value": "RootWebArea"}, "name": {"value": "Doc"}, "childIds": ["2"]},
            {"nodeId": "2", "role": {"value": "button"}, "name": {"value": "Send"}},
        ]
        nodes = ax.prune(raw_nodes)

        # Entry with matching role, name, ordinal, but mismatched exact and stable fingerprints
        entry = {
            "role": "button",
            "name": "Send",
            "ordinal": 0,
            "fp_exact": "0" * 16,
            "fp_stable": "0" * 16,
        }

        res = ax.match(nodes, entry)

        self.assertEqual(res["level"], "L2_NAME")
        self.assertEqual(res["candidates"], 1)

        l2_entry = next(item for item in res["ladder_tried"] if item.get("level") == "L2_NAME")
        # In production, l2_entry["candidates"] is 0, while res["candidates"] is 1.
        # This test requires them to agree, which fails in current production code.
        self.assertEqual(l2_entry["candidates"], res["candidates"])


if __name__ == "__main__":
    unittest.main()
