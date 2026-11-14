import random
import unittest

from wender.perceive import ax


def _n(nid, role, name="", kids=(), ignored=False, props=None, backend=None):
    d = {"nodeId": str(nid), "role": {"type": "role", "value": role},
         "name": {"type": "computedString", "value": name}, "childIds": [str(k) for k in kids],
         "ignored": ignored}
    if props:
        d["properties"] = [{"name": k, "value": {"type": "x", "value": v}} for k, v in props.items()]
    if backend is not None:
        d["backendDOMNodeId"] = backend
    return d


def fixture(offset=0):
    o = offset
    return [
        _n(1 + o, "RootWebArea", "Doc", [2 + o]),
        _n(2 + o, "generic", "", [3 + o, 6 + o, 8 + o]),
        _n(3 + o, "main", "", [4 + o, 5 + o]),
        _n(4 + o, "button", "  Send\n now ", props={"focusable": True, "disabled": False}, backend=40),
        _n(5 + o, "StaticText", "hello"),
        _n(6 + o, "article", "Answer", [7 + o]),
        _n(7 + o, "button", "inside article"),
        _n(8 + o, "button", "hidden", ignored=True),
    ]


class TestAx(unittest.TestCase):
    def test_normalize_name(self):
        self.assertEqual(ax.normalize_name("  a\n\tb  "), "a b")
        self.assertEqual(ax.normalize_name("x|y\\z"), "x\\|y\\\\z")
        self.assertEqual(len(ax.normalize_name("\u00e9" * 40).encode("utf-8")), 64)
        self.assertEqual(ax.normalize_name("e\u0301"), "\u00e9")  # NFC

    def test_prune_rules(self):
        nodes = ax.prune(fixture())
        got = [(n["depth"], n["role"], n["name"]) for n in nodes]
        self.assertEqual(got, [(0, "RootWebArea", "Doc"), (1, "main", ""), (2, "button", "Send now"),
                               (1, "article", "Answer")])
        btn = nodes[2]
        self.assertEqual(btn["attrs"], (("disabled", "false"),))  # focusable excluded
        self.assertEqual(btn["backend_id"], 40)

    def test_sha_independent_of_ids_and_list_order(self):
        a = ax.prune(fixture())
        raw = fixture(offset=100)
        random.Random(7).shuffle(raw)
        b = ax.prune(raw)
        self.assertEqual(ax.snapshot_sha(a), ax.snapshot_sha(b))
        self.assertEqual([n["id"] for n in a], [n["id"] for n in b])

    def test_stable_sha_ignores_state(self):
        raw = fixture()
        raw[3]["properties"] = [{"name": "disabled", "value": {"type": "boolean", "value": True}}]
        a, b = ax.prune(fixture()), ax.prune(raw)
        self.assertNotEqual(ax.snapshot_sha(a, True), ax.snapshot_sha(b, True))
        self.assertEqual(ax.snapshot_sha(a, False), ax.snapshot_sha(b, False))

    def test_cycle_does_not_hang(self):
        raw = [_n(1, "RootWebArea", "r", [2]), _n(2, "main", "", [1])]
        self.assertEqual(len(ax.prune(raw)), 2)

    def test_canon_format(self):
        self.assertEqual(ax.canon(ax.prune(fixture())).splitlines()[2], "2|button|Send now|disabled=false")

    def test_match_ladder(self):
        nodes = ax.prune(fixture())
        rec = ax.describe(nodes[2])
        self.assertEqual(ax.match(nodes, rec)["level"], "L0_EXACT")
        raw = fixture()
        raw[3]["properties"] = [{"name": "disabled", "value": {"type": "boolean", "value": True}}]
        self.assertEqual(ax.match(ax.prune(raw), rec)["level"], "L1_STABLE")
        raw[3]["name"]["value"] = "Submit"
        r = ax.match(ax.prune(raw), dict(rec, name_variants=["Submit"]))
        self.assertEqual(r["level"], "L3_VARIANT")
        self.assertFalse(ax.level_allows_irreversible(r["level"]))

    def test_raw_tree_retries_then_raises(self):
        class Bad:
            calls = 0

            def send(self, method, params=None, session_id=None):
                Bad.calls += 1
                raise OSError("boom")
        old = ax.AX_RETRY_WAIT_S
        ax.AX_RETRY_WAIT_S = 0
        try:
            with self.assertRaises(RuntimeError):
                ax.raw_tree(Bad(), "s")
        finally:
            ax.AX_RETRY_WAIT_S = old
        self.assertEqual(Bad.calls, ax.AX_RETRIES)


if __name__ == "__main__":
    unittest.main()
