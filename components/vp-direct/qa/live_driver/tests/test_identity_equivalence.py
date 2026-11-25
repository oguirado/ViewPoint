from pathlib import Path
import sys
from tempfile import TemporaryDirectory, TemporaryFile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import driver  # Sets the same import paths as the live worker.
from acceptance import load_harness
from privacy import PrivacyBinding, PrivacyBlocked, PrivacyMetadata, PrivacyState
import runtime


def baseline_process_stat(pid):
    fields = (runtime.PROC / str(pid) / "stat").read_text().rsplit(")", 1)[1].split()
    return fields[0], int(fields[19])


def stat_line(state, start):
    # A command name may contain closing parentheses, spaces and newlines.
    fields = [state, *("0" for _ in range(18)), str(start), "0", "0", "0"]
    return "42 (Chrome ) helper\nname) " + " ".join(fields) + "\n"


class IdentityEquivalenceTests(unittest.TestCase):
    def setUp(self):
        root = self.enterContext(TemporaryDirectory())
        self.enterContext(patch.object(runtime, "PROC", Path(root)))
        self.stat = runtime.PROC / "42" / "stat"
        self.stat.parent.mkdir()
        self.stat.write_text(stat_line("S", 100))
        self.error = self.enterContext(TemporaryFile())

    def test_stat_reads_match_and_observe_replacement_exit_and_disappearance(self):
        for state, start in (("S", 100), ("R", 200), ("Z", 200), ("X", 200)):
            with self.subTest(state=state, start=start):
                self.stat.write_text(stat_line(state, start))
                self.assertEqual(baseline_process_stat(42), (state, start))
                self.assertEqual(runtime.process_stat(42), (state, start))

        for raw, error in (("42 (Chrome)", IndexError), (stat_line("S", "bad"), ValueError)):
            self.stat.write_text(raw)
            for read_stat in (baseline_process_stat, runtime.process_stat):
                with self.assertRaises(error):
                    read_stat(42)
        self.stat.unlink()
        for read_stat in (baseline_process_stat, runtime.process_stat):
            with self.assertRaises(FileNotFoundError):
                read_stat(42)

    def browser(self):
        child = runtime.BrowserProcess.__new__(runtime.BrowserProcess)
        child.proc, child.pid, child.start = Mock(), 42, 100
        child.proc.poll.return_value = None

        browser = runtime.Browser.__new__(runtime.Browser)
        browser.child, browser.error, browser.port = child, self.error, 6100
        browser.display = SimpleNamespace(label="xpra:90", verify=Mock(return_value=True))
        browser.url = "http://127.0.0.1:6400/timer.html"
        browser.xid, browser.cdp_window = "0x42", 7
        browser.find_window = Mock(return_value=browser.xid)
        browser.page = runtime.Page(browser, "", "timer")
        browser.page.document = "frame", "loader"
        frame = dict(id="frame", loaderId="loader", url=browser.url)
        window = {"windowId": browser.cdp_window}
        browser.page.call = Mock(side_effect={
            "Page.getFrameTree": {"frameTree": {"frame": frame}},
            "Browser.getWindowForTarget": window,
        }.__getitem__)
        return browser, frame, window

    def test_browser_passes_current_identity_and_refuses_each_changed_witness(self):
        candidate = runtime.process_stat
        for read_stat in (baseline_process_stat, candidate):
            for case, reason in (
                ("current", None),
                ("stale process", "owned Chrome exited before readiness"),
                ("wrong listener", "private browser identity changed"),
                ("wrong X window", "private browser identity changed"),
                ("stale document", "fixture loader changed"),
                ("wrong URL", "private fixture did not load the pinned URL"),
                ("wrong CDP window", "private CDP window changed"),
            ):
                with self.subTest(parser=read_stat.__name__, case=case):
                    self.stat.write_text(stat_line("S", 100))
                    browser, frame, window = self.browser()
                    if case == "stale process":
                        self.stat.write_text(stat_line("S", 200))
                    if case == "wrong X window":
                        browser.find_window.return_value = "0x43"
                    if case == "stale document":
                        frame["loaderId"] = "replacement"
                    if case == "wrong URL":
                        frame["url"] = "http://127.0.0.1:6400/login.html"
                    if case == "wrong CDP window":
                        window["windowId"] = 8

                    with patch.object(runtime, "process_stat", read_stat), patch.object(
                        runtime, "listener_owned", return_value=case != "wrong listener"
                    ):
                        if reason is None:
                            browser.check()
                        else:
                            with self.assertRaisesRegex(runtime.Refused, reason):
                                browser.check()

    def test_protected_observe_rejects_assignment_and_a_fence_advanced_in_staging(self):
        harness = load_harness("t2")
        candidate = runtime.process_stat
        for read_stat in (baseline_process_stat, candidate):
            for case, expected in (("current", "published"), ("assignment", "refused"), ("fence", "stale")):
                with self.subTest(parser=read_stat.__name__, case=case):
                    browser, _, _ = self.browser()
                    binding = PrivacyBinding("surface", "incarnation", 1, "cdp", "witness")
                    assignment = dict(surface_id="other" if case == "assignment" else binding.surface_id,
                                      incarnation=binding.incarnation, authority_epoch=binding.authority_epoch)
                    metadata = PrivacyMetadata(binding, 1, PrivacyState.CLEAR)

                    def read(bound):
                        self.assertEqual(bound, binding)
                        browser.check()
                        return metadata

                    def capture(prior):
                        nonlocal metadata
                        self.assertEqual(prior, metadata)
                        if case == "fence":
                            metadata = PrivacyMetadata(binding, 2, PrivacyState.CLEAR)
                        return {"fixture": "staged"}

                    staged = Mock(side_effect=capture)
                    gap, handoff, publications = Mock(), Mock(), Mock()
                    workload = SimpleNamespace(privacy_ports=lambda combo, login: (
                        binding, read, staged, gap, handoff,
                    ))
                    observe = harness.PrivacySentinel.observe
                    blocked = []

                    def audited_observe(sentinel, acquire, publish):
                        def record_publication(frame):
                            publications(frame)
                            return publish(frame)

                        try:
                            return observe(sentinel, acquire, record_publication)
                        except PrivacyBlocked as exc:
                            blocked.append(exc.code)
                            raise

                    with patch.object(runtime, "process_stat", read_stat), patch.object(
                        runtime, "listener_owned", return_value=True
                    ), patch.object(harness.PrivacySentinel, "observe", audited_observe), patch.object(
                        harness, "validate_contract"
                    ):
                        if expected == "refused":
                            with self.assertRaisesRegex(harness.EvidenceError, "privacy source binding changed"):
                                harness.protected(workload, "timer", assignment, login=False)
                            staged.assert_not_called()
                        elif expected == "stale":
                            self.assertIsNone(harness.protected(workload, "timer", assignment, login=False))
                            self.assertEqual(blocked, ["STALE_FRAME"])
                            staged.assert_called_once()
                            gap.assert_called_once()
                            self.assertTrue(gap.call_args.args[0])
                        else:
                            result = harness.protected(workload, "timer", assignment, login=False)
                            self.assertEqual(result["frame"], {"fixture": "staged"})
                            staged.assert_called_once()
                            publications.assert_called_once_with(result["frame"])

                        if expected != "published":
                            publications.assert_not_called()
                        handoff.assert_not_called()


if __name__ == "__main__":
    unittest.main()
