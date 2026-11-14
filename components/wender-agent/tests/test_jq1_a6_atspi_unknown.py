"""Adversarial claim-pinning tests for wender.perceive.atspi.window_summary (Defect S1-F6)."""
import unittest


def _make_fake_index(atspi_mod):
    def fake_call(address, dest, path, method, *args, timeout=10):
        if dest == "org.a11y.atspi.Registry":
            return "[(':1.101', '/app/101'), (':1.102', '/app/102'), (':1.103', '/app/103')]"
        if dest == "org.freedesktop.DBus":
            bus = args[0]
            if bus == ":1.101":
                return "(uint32 101,)"
            if bus == ":1.102":
                return "(uint32 102,)"
            if bus == ":1.103":
                return "(uint32 103,)"
        if dest in (":1.101", ":1.102", ":1.103"):
            prop = args[1]
            if prop == "ChildCount":
                if dest == ":1.101":
                    return ""
                if dest == ":1.102":
                    return "<0>"
                if dest == ":1.103":
                    return "<-1>"
            if prop == "Name":
                return "<'TestApp'>"
            if prop == "ToolkitName":
                return "<'GTK'>"
        return None

    return atspi_mod.Index(address="unix:abstract=/fake", call=fake_call)


class TestJq1A6AtspiUnknown(unittest.TestCase):
    def test_verified_childless_app_reports_tree_false_rung_4(self):
        """Healthy control: PID 102 (ChildCount=0) reports tree=False, rung=4."""
        from wender.perceive import atspi

        idx = _make_fake_index(atspi)
        summary = atspi.window_summary(102, idx)
        self.assertTrue(summary["available"])
        self.assertEqual(summary["n_children"], 0)
        self.assertIs(summary["tree"], False)
        self.assertEqual(summary["rung"], 4)

    def test_unreadable_childcount_reports_tree_none_unknown(self):
        """Defect S1-F6: unreadable ChildCount (n_children is None) reports tree=None (unknown) rather than tree=False."""
        from wender.perceive import atspi

        idx = _make_fake_index(atspi)
        summary = atspi.window_summary(101, idx)
        self.assertTrue(summary["available"])
        self.assertIsNone(summary["n_children"])
        # Defect S1-F6: window_summary currently uses bool(app["n_children"]), collapsing None to False
        self.assertIsNone(summary["tree"])

    def test_negative_parsed_childcount_not_classified_as_tree_true(self):
        """Defect S1-F6: negative parsed ChildCount (n_children is -1) is never classified as tree=True, rung=2."""
        from wender.perceive import atspi

        idx = _make_fake_index(atspi)
        summary = atspi.window_summary(103, idx)
        self.assertTrue(summary["available"])
        self.assertEqual(summary["n_children"], -1)
        # Defect S1-F6: window_summary currently uses bool(-1) which evaluates to True, reporting tree=True, rung=2
        self.assertNotEqual(summary["tree"], True)
        self.assertNotEqual(summary.get("rung"), 2)

    def test_childcount_states_preserve_direct_and_family_link_metadata(self):
        from wender.perceive import atspi

        for count, tree, rung in ((None, None, 4), (-1, None, 4), (0, False, 4), (3, True, 2)):
            app = {"n_children": count, "bus": ":1.500", "pid": 500,
                   "name": "Test App", "toolkit": "GTK", "path": "/app/500"}
            idx = {500: app}
            for pid, family, link in ((500, None, "by_pid"), (999, [404, 500], "by_family_pid")):
                with self.subTest(count=count, link=link):
                    self.assertEqual(atspi.window_summary(pid, idx, family),
                                     {"available": True, "tree": tree, "link": link,
                                      "bus": ":1.500", "pid_atspi": 500, "name": "Test App",
                                      "n_children": count, "toolkit": "GTK", "rung": rung})

    def test_unknown_direct_link_does_not_select_a_different_family_app(self):
        from wender.perceive import atspi

        idx = {101: {"n_children": None, "bus": ":1.101", "pid": 101,
                     "name": "Direct", "toolkit": "GTK"},
               102: {"n_children": 3, "bus": ":1.102", "pid": 102,
                     "name": "Family", "toolkit": "QT"}}
        summary = atspi.window_summary(101, idx, family_pids=[102])
        self.assertIsNone(summary["tree"])
        self.assertEqual((summary["link"], summary["pid_atspi"], summary["name"], summary["rung"]),
                         ("by_pid", 101, "Direct", 4))


if __name__ == "__main__":
    unittest.main()
