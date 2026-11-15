"""Hierarchy text extraction contracts and claim pinning (fx2-f1-j2-text-extract-hierarchy)."""
import copy
import importlib
import random
import unittest

from tests.test_jq1_a6_perceive_primitives import extract as ref_extract


def _n(nid, role, name="", kids=(), ignored=False, props=None):
    d = {
        "nodeId": str(nid),
        "role": {"type": "role", "value": role},
        "name": {"type": "computedString", "value": name},
        "childIds": [str(k) for k in kids],
        "ignored": ignored,
    }
    if props:
        d["properties"] = [{"name": k, "value": {"type": "value", "value": v}} for k, v in props.items()]
    return d


REGION_START = {"role": "heading", "name": "ChatGPT dijo:"}
REGION_END = {"role": "button", "name": "Copiar mensaje"}
TRAILERS = (
    "ChatGPT puede cometer errores. Comprueba la información importante.",
    "Última respuesta",
)

LONG_TEXT = (
    "This is a very long text that exceeds sixty four characters in total length "
    "so we can test that full text is preserved beyond chassis truncation limit."
)
QUOTED_TRAILER = (
    "Quote: ChatGPT puede cometer errores. Comprueba la información importante. End quote."
)


def make_hierarchy_fixture():
    """Build a single-root raw AX tree with nested articles, repeated text, StaticText/InlineTextBox,

    code block, interior quoted trailer, long text > 64 chars, and terminal trailer blocks.
    """
    return [
        _n(1, "RootWebArea", "Doc", kids=[2]),
        _n(2, "article", "Outer Article", kids=[3]),
        _n(3, "article", "Assistant turn", kids=[4, 5, 10, 15, 20, 25, 30, 40, 42, 35]),
        _n(4, "heading", "ChatGPT dijo:", kids=[401]),
        _n(401, "StaticText", "ChatGPT dijo:", kids=[]),
        _n(5, "paragraph", "", kids=[6]),
        _n(6, "StaticText", "Hello world", kids=[7, 8]),
        _n(7, "InlineTextBox", "Hello ", kids=[]),
        _n(8, "InlineTextBox", "world", kids=[]),
        _n(10, "listitem", "", kids=[11]),
        _n(11, "StaticText", "Yes", kids=[]),
        _n(15, "listitem", "", kids=[16]),
        _n(16, "StaticText", "Yes", kids=[]),
        _n(20, "paragraph", "", kids=[21]),
        _n(21, "StaticText", QUOTED_TRAILER, kids=[]),
        _n(25, "paragraph", "", kids=[26]),
        _n(26, "StaticText", LONG_TEXT, kids=[]),
        _n(30, "code", "", kids=[31]),
        _n(31, "StaticText", "def foo():\n    return 42", kids=[]),
        _n(40, "paragraph", "", kids=[41]),
        _n(41, "StaticText", "ChatGPT puede cometer errores. Comprueba la información importante.", kids=[]),
        _n(42, "paragraph", "", kids=[43]),
        _n(43, "StaticText", "Última respuesta", kids=[]),
        _n(35, "button", "Copiar mensaje", kids=[]),
    ]


def rename_node_ids(nodes, prefix="renamed_"):
    """Apply consistent bijective node-ID renaming across nodeId and childIds."""
    renamed = []
    for node in nodes:
        new_node = copy.deepcopy(node)
        new_node["nodeId"] = f"{prefix}{node['nodeId']}"
        new_node["childIds"] = [f"{prefix}{k}" for k in node["childIds"]]
        renamed.append(new_node)
    return renamed


class TestTextExtractHierarchyReferenceControls(unittest.TestCase):
    """Healthy control tests for reference text extract hierarchy handling."""

    def test_fixture_extraction_correctness(self):
        nodes = make_hierarchy_fixture()
        res = ref_extract(nodes, REGION_START, REGION_END, trailers=TRAILERS)

        self.assertEqual(res["extraction_status"], "clean")
        text = res["text"]

        # Reconstructed text sections
        self.assertIn("Hello world", text)
        # Verify InlineTextBox deduplication: "Hello world" appears once in paragraph, not "Hello worldHello world"
        self.assertNotIn("Hello worldHello world", text)

        # Repeated lines "Yes"
        self.assertEqual(text.count("Yes"), 2)

        # Quoted interior trailer preserved
        self.assertIn(QUOTED_TRAILER, text)

        # Long text beyond 64 chars preserved intact
        self.assertIn(LONG_TEXT, text)

        # Code block preserved
        self.assertIn("def foo():\n    return 42", text)

        # Terminal trailers stripped
        self.assertNotIn("Última respuesta", text)
        self.assertNotIn("ChatGPT puede cometer errores. Comprueba la información importante.\n", text)

    def test_bijective_id_renaming_and_flat_permutation_invariance(self):
        """Require identical extraction under bijective ID renaming and flat array permutation."""
        base_nodes = make_hierarchy_fixture()
        expected = ref_extract(base_nodes, REGION_START, REGION_END, trailers=TRAILERS)

        # Bijective renaming
        renamed_nodes = rename_node_ids(base_nodes, prefix="mapped_id_")

        # Reverse flat order
        reversed_nodes = list(reversed(renamed_nodes))
        res_reversed = ref_extract(reversed_nodes, REGION_START, REGION_END, trailers=TRAILERS)
        self.assertEqual(res_reversed, expected)

        # Random shuffle flat order with fixed seed
        shuffled_nodes = copy.deepcopy(renamed_nodes)
        random.Random(1337).shuffle(shuffled_nodes)
        res_shuffled = ref_extract(shuffled_nodes, REGION_START, REGION_END, trailers=TRAILERS)
        self.assertEqual(res_shuffled, expected)

    def test_malformed_graph_rejection(self):
        """Independently introduce disconnected cycles, multiple parents, dangling refs; expect incomplete_tree."""
        # 1. Dangling reference
        dangling_nodes = make_hierarchy_fixture()
        dangling_nodes[2]["childIds"].append("99999")  # Node 3 now has non-existent child 99999
        res_dangling = ref_extract(dangling_nodes, REGION_START, REGION_END, trailers=TRAILERS)
        self.assertEqual(res_dangling, {"text": "", "extraction_status": "incomplete_tree"})

        # 2. Multiple parents
        multi_parent_nodes = make_hierarchy_fixture()
        # Node 11 added to root's childIds as well as node 10's childIds
        multi_parent_nodes[0]["childIds"].append("11")
        res_multi = ref_extract(multi_parent_nodes, REGION_START, REGION_END, trailers=TRAILERS)
        self.assertEqual(res_multi, {"text": "", "extraction_status": "incomplete_tree"})

        # 3. Disconnected cycle
        cycle_nodes = make_hierarchy_fixture()
        cycle_nodes.extend([
            _n(800, "group", "Cycle1", kids=[801]),
            _n(801, "group", "Cycle2", kids=[800]),
        ])
        res_cycle = ref_extract(cycle_nodes, REGION_START, REGION_END, trailers=TRAILERS)
        self.assertEqual(res_cycle, {"text": "", "extraction_status": "incomplete_tree"})

    def test_reject_interior_phrase_stripping_mutant(self):
        """Reject interior-phrase stripping mutant that improperly strips trailers inside content."""
        nodes = make_hierarchy_fixture()
        res = ref_extract(nodes, REGION_START, REGION_END, trailers=TRAILERS)
        text = res["text"]

        # Interior quoted trailer must exist in extracted text
        self.assertIn(QUOTED_TRAILER, text)

        # Simulate a mutant that does global string replace of trailer strings
        mutant_text = text
        for t in TRAILERS:
            mutant_text = mutant_text.replace(t, "")

        # Assert that the mutant wrongfully stripped interior trailer text
        self.assertNotIn(QUOTED_TRAILER, mutant_text)
        self.assertNotEqual(text, mutant_text)


class TestTextExtractHierarchyProductionPinning(unittest.TestCase):
    """Pin production extract API (wender.perceive.text.extract) gap on checkout."""

    def test_production_extract_hierarchy_wp1_04(self):
        """Production extract is absent on checkout and must fail until implemented."""
        text_mod = importlib.import_module("wender.perceive.text")
        prod_extract = getattr(text_mod, "extract")

        nodes = make_hierarchy_fixture()
        res = prod_extract(nodes, REGION_START, REGION_END, trailers=TRAILERS)
        self.assertEqual(res["extraction_status"], "clean")


if __name__ == "__main__":
    unittest.main()
