#!/usr/bin/python3
"""Negative-control and boundary tests for hackathon/vp_mcp/vp_mcp.py.

File location: hackathon/vp_mcp/tests/test_negctl_vpmcp.py
Run with: cd hackathon/vp_mcp && python3 -B -m unittest discover -s tests

Package items this file relies on (all from the pack):
  module:   hackathon/vp_mcp/vp_mcp.py (SERVER_NAME viewpoint-agent v0.2.0)
  fixtures: hackathon/vp_mcp/tests/test_vp_mcp.py (FakeRunner, make_session,
            REPORT_JSON, mvctl_verb), reused here, never modified.
  symbols:  validate_url, ViewpointServer, browser_lane_lock,
            _BROWSER_THREAD_LOCK, BROWSER_LOCK_WAIT_S, MAX_URL_CHARS (2048),
            MAX_GOAL_CHARS (500), MAX_TEXT_CHARS (4000), MAX_CHARS_LIMIT (6000)
  statuses: STALE_FRAME, INPUT_UNCERTAIN, "agent busy", "unknown identity"
  lock file: <xdg_state>/vp_mcp.browser_task.lock

Observed from reading the module (declared hypotheses, no receipts exist):
  * validate_url never rejects benign userinfo (https://user@example.com/):
    urlparse().hostname is what is checked. Only userinfo that smuggles a
    private host is tested here.
  * validate_url does not decode numeric IPv4 spellings (2130706433, 0x7f.0.0.1,
    0177.0.0.1, 127.1): ipaddress.ip_address() raises for them, so they fall
    through to "return url, None". That is a real gap, recorded below as an
    expectedFailure so the suite stays green today and flags the day it is fixed.
  * No tests open sockets or touch the network; every subprocess is the
    FakeRunner seam.
"""

import fcntl
import json
import os
import sys
import tempfile
import threading
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import vp_mcp  # noqa: E402
import test_vp_mcp as base  # noqa: E402


class FailNthWaitRunner(base.FakeRunner):
    """FakeRunner whose wait-verify refuses with `code` on the fail_at-th wait."""

    def __init__(self, report_path, fail_at, code):
        super().__init__(report_path=report_path)
        self.fail_at = fail_at
        self.code = code

    def _mvctl(self, argv):
        idx = argv.index("metaviewer_agents.mvctl")
        if argv[idx + 1] == "wait-verify" and len(self.wait_ids) == self.fail_at:
            self.wait_ids.append(argv[argv.index("--proposal-id") + 1])
            return 1, json.dumps({"verdict": "refuse", "reason_code": self.code}), ""
        return super()._mvctl(argv)


class TestNegCtlVpMcp(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = self.tmp.name
        self.report_path = os.path.join(root, "report.json")
        with open(self.report_path, "w", encoding="utf-8") as fh:
            fh.write(base.REPORT_JSON)
        self.session = base.make_session(root, self.report_path)
        self.runner = base.FakeRunner(report_path=self.report_path)
        self.server = vp_mcp.ViewpointServer(
            self.session, runner=self.runner, sleep=lambda s: None)

    # -- helpers -----------------------------------------------------------

    def call_tool(self, name, arguments, server=None):
        srv = server if server is not None else self.server
        line = srv.handle_line(json.dumps(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": name, "arguments": arguments}}))
        self.assertIsNotNone(line)
        result = json.loads(line)["result"]
        return result, json.loads(result["content"][0]["text"])

    def nav(self, url, identity="caps4-1"):
        return self.call_tool("vp_navigate", {"identity": identity, "url": url})

    def assert_url_refused(self, url, fragment=None):
        checked, reason = vp_mcp.validate_url(url)
        self.assertIsNone(checked, url)
        self.assertIsNotNone(reason, url)
        if fragment is not None:
            self.assertIn(fragment, reason, url)

    def assert_url_allowed(self, url):
        checked, reason = vp_mcp.validate_url(url)
        self.assertEqual(checked, url)
        self.assertIsNone(reason, url)

    # -- 1. URL scheme refusals ---------------------------------------------

    def test_exotic_schemes_and_hostless_urls_refused_before_any_subprocess(self):
        # Protects: `if parsed.scheme != "https"` and `if not parsed.hostname`
        # in validate_url. Removing either lets these reach mvctl.
        urls = (
            "data:text/html,<script>1</script>",
            "javascript:alert(1)",
            "view-source:https://example.com/",
            "blob:https://example.com/0000",
            "wss://example.com/socket",
            "HTTP://example.com/a",
            "//example.com/a",
            "example.com/a",
            "https:///path-without-host",
            "https:example.com",
        )
        for url in urls:
            with self.subTest(url=url):
                result, payload = self.nav(url)
                self.assertTrue(result["isError"])
                self.assertTrue(payload["error"].startswith("url"), payload)
                self.assertEqual(self.runner.raw_calls, [])

    # -- 2. userinfo smuggling ----------------------------------------------

    def test_userinfo_cannot_hide_a_private_host(self):
        # Protects: validating `parsed.hostname` (the host after the last '@')
        # instead of the raw netloc. With netloc, ip_address() raises and the
        # URL would be accepted.
        urls = (
            "https://example.com@127.0.0.1/",
            "https://user:pw@10.0.0.5/x",
            "https://trusted.example@[::1]/",
            "https://a@169.254.169.254/latest/meta-data",
            "https://ok:ok@192.168.0.1:8443/",
        )
        for url in urls:
            with self.subTest(url=url):
                self.assert_url_refused(url, "non-public host refused")

    # -- 3. IPv6 ------------------------------------------------------------

    def test_ipv6_loopback_local_and_mapped_hosts_refused(self):
        # Protects: the ipaddress is_loopback/is_private/is_link_local/
        # is_multicast/is_unspecified chain in validate_url for IPv6 literals.
        urls = (
            "https://[::1]/",
            "https://[::]/",
            "https://[fe80::1]/",
            "https://[fc00::1]/",
            "https://[fd12:3456::1]/",
            "https://[ff02::1]/",
            "https://[::ffff:127.0.0.1]/",
            "https://[::ffff:10.0.0.1]/",
        )
        for url in urls:
            with self.subTest(url=url):
                self.assert_url_refused(url, "non-public host refused")

    # -- 4. range edges -------------------------------------------------------

    def test_private_range_edges_refused_and_their_neighbours_allowed(self):
        # Protects the exact CIDR edges: 10/8, 172.16/12, 192.168/16,
        # 169.254/16 plus special-use blocks. Refused side fails if the check
        # is removed; allowed side fails if it is widened (e.g. startswith
        # "172." or "169.").
        refused = (
            "10.0.0.0", "10.255.255.255", "172.16.0.0", "172.31.255.255",
            "192.168.0.0", "192.168.255.255", "169.254.0.0", "169.254.255.255",
            "127.0.0.2", "127.255.255.254", "0.0.0.0", "224.0.0.1",
            "240.0.0.1", "255.255.255.255",
        )
        allowed = (
            "9.255.255.255", "11.0.0.0", "172.15.255.255", "172.32.0.0",
            "192.167.255.255", "192.169.0.0", "169.253.255.255",
            "169.255.0.0", "8.8.8.8", "1.1.1.1",
        )
        for ip in refused:
            with self.subTest(refused=ip):
                self.assert_url_refused("https://%s/" % ip, "non-public host refused")
        # Policy since 13 Nov: every IP literal is refused (numeric spellings
        # bypassed ipaddress); public neighbours are refused too.
        for ip in allowed:
            with self.subTest(refused_public_literal=ip):
                self.assert_url_refused("https://%s/" % ip, "non-public host refused")

    # -- 5. localhost variants ------------------------------------------------

    def test_localhost_variants_refused_and_lookalike_names_allowed(self):
        # Protects: `host in ("localhost", "localdomain")` and
        # `host.endswith((".localhost", ".local", ".internal"))`, plus the
        # `.lower()` normalisation. Lookalikes must stay allowed (no
        # substring matching).
        refused = (
            "https://localhost/", "https://LOCALHOST/", "https://LocalHost:8443/x",
            "https://app.localhost/", "https://a.b.localhost/",
            "https://printer.local/", "https://nas.internal/",
            "https://localdomain/", "https://metadata.internal/computeMetadata",
        )
        allowed = (
            "https://notlocalhost.example/", "https://localhost.example.com/",
            "https://internal.example.com/", "https://local.example.org/",
            "https://evil-localdomain.com/",
        )
        for url in refused:
            with self.subTest(refused=url):
                self.assert_url_refused(url, "localhost hosts refused")
        for url in allowed:
            with self.subTest(allowed=url):
                self.assert_url_allowed(url)

    # -- 6. known gap, numeric IPv4 spellings -----------------------------------

    def test_numeric_ipv4_spellings_known_gap(self):
        # KNOWN GAP (observed in validate_url): ipaddress.ip_address() rejects
        # these spellings, so they fall to `return url, None` although a
        # browser resolves them to 127.0.0.1. This asserts the DESIRED
        # behaviour; it is an expected failure until the gate decodes them.
        # When it starts passing, unittest reports an unexpected success:
        # remove the decorator then.
        urls = (
            "https://2130706433/",
            "https://0x7f000001/",
            "https://017700000001/",
            "https://0177.0.0.1/",
            "https://0x7f.0.0.1/",
            "https://127.1/",
        )
        for url in urls:
            checked, reason = vp_mcp.validate_url(url)
            self.assertIsNotNone(reason, url)

    # -- 7. identity -----------------------------------------------------------

    def test_identity_must_match_a_session_key_exactly(self):
        # Protects: `self.capsules.get(identity)` exact-key lookup and the
        # isinstance guard in _capsule. Case folding, trimming, container or
        # surface names, and non-strings must never resolve to a capsule.
        bad = [
            None, "", 123, ["caps4-1"], {"a": 1},
            "CAPS4-1", "caps4-1 ", " caps4-1", "caps4-1\n", "caps4-1\x00",
            "caps4-", "../caps4-1",
            "srf-id-caps4-1", "surface-caps4-1", "w1",
        ]
        calls = (
            ("vp_read_page", {}),
            ("vp_navigate", {"url": "https://example.com/"}),
            ("vp_browser_task", {"goal": "hi"}),
        )
        for identity in bad:
            for name, extra in calls:
                with self.subTest(identity=repr(identity), tool=name):
                    args = dict(extra)
                    args["identity"] = identity
                    result, payload = self.call_tool(name, args)
                    self.assertTrue(result["isError"])
                    if isinstance(identity, str) and identity:
                        self.assertIn("unknown identity", payload["error"])
                    self.assertEqual(self.runner.raw_calls, [])

    # -- 8. argument type confusion ----------------------------------------------

    def test_argument_type_confusion_refused_without_subprocess(self):
        # Protects the per-argument guards in tool_browser_task
        # (`isinstance(goal, str)`, NUL checks, `not text`, `key != "Enter"`)
        # and tool_read_page (`isinstance(max_chars, bool)` exclusion and the
        # int/range gate). A bool max_chars of True would otherwise read as 1.
        task_cases = (
            {"goal": 123}, {"goal": ["x"]}, {"goal": "a\x00b"},
            {"goal": "hi", "text": ""}, {"goal": "hi", "text": 5},
            {"goal": "hi", "text": ["x"]}, {"goal": "hi", "text": "a\x00b"},
            {"goal": "hi", "key": "enter"}, {"goal": "hi", "key": "Return"},
            {"goal": "hi", "key": ""}, {"goal": "hi", "key": ["Enter"]},
        )
        for case in task_cases:
            with self.subTest(task=repr(case)):
                args = {"identity": "caps4-1"}
                args.update(case)
                result, payload = self.call_tool("vp_browser_task", args)
                self.assertTrue(result["isError"])
                self.assertIn("error", payload)
        for value in (True, False, "10", 10.5, -1, None, [5]):
            with self.subTest(max_chars=repr(value)):
                result, payload = self.call_tool(
                    "vp_read_page", {"identity": "caps4-1", "max_chars": value})
                self.assertTrue(result["isError"])
                self.assertIn("error", payload)
        self.assertEqual(self.runner.raw_calls, [])

    # -- 9. URL length and control characters -------------------------------------

    def test_url_length_boundary_and_control_characters(self):
        # Protects: `len(url) > MAX_URL_CHARS` (off-by-one) and the
        # NUL/CR/LF check. 2048 chars must pass, 2049 must be refused.
        self.assertEqual(vp_mcp.MAX_URL_CHARS, 2048)
        exact = "https://example.org/" + "a" * (2048 - len("https://example.org/"))
        self.assertEqual(len(exact), 2048)
        self.assert_url_allowed(exact)
        self.assert_url_refused(exact + "a", "exceeds")
        for url in ("https://example.com/a\nb", "https://example.com/\r\nHost: x",
                    "https://example.com/\x00"):
            with self.subTest(url=repr(url)):
                self.assert_url_refused(url, "unsafe characters")
        for url in (None, 123, b"https://example.com/", ["https://example.com/"], ""):
            with self.subTest(url=repr(url)):
                self.assert_url_refused(url)

    # -- 10. srf argv is read-only ax only --------------------------------------------

    def test_every_srf_invocation_is_read_only_ax_through_the_launcher(self):
        # Protects: _ax_argv (the only srf argv builder). Any native `input`
        # or `frame` verb bypasses core admission; every raw argv must go
        # through env_launcher and every srf call must be `ax` with the
        # session-pinned container/window/state-dir.
        self.call_tool("vp_list_capsules", {})
        self.call_tool("vp_navigate", {"identity": "caps4-1", "url": "https://example.org/a"})
        self.call_tool("vp_navigate", {"identity": "caps4-2", "url": "https://example.org/b"})
        self.call_tool("vp_read_page", {"identity": "caps4-1"})
        self.call_tool("vp_read_page", {"identity": "caps4-2"})
        self.call_tool("vp_browser_task", {"identity": "caps4-1", "goal": "Search Wikipedia"})
        self.runner.wait_fail_mode = "uncertain"
        self.call_tool("vp_navigate", {"identity": "caps4-1", "url": "https://example.org/c"})

        srf = self.session["srf_bin"]
        launcher = self.session["env_launcher"]
        allowed_pairs = {(c["container"], c["window"]) for c in self.session["capsules"]}
        ax_calls = 0
        for raw in self.runner.raw_calls:
            self.assertEqual(raw[0], "/bin/bash")
            self.assertEqual(raw[1], launcher)
            for tok in raw:
                self.assertNotIn(tok, ("input", "frame"))
            if srf in raw:
                i = raw.index(srf)
                self.assertEqual(i, 2)
                self.assertEqual(raw[i + 1], "ax")
                self.assertEqual(raw[raw.index("--state-dir") + 1], self.session["state_dir"])
                pair = (raw[raw.index("--container") + 1], raw[raw.index("--window") + 1])
                self.assertIn(pair, allowed_pairs)
                ax_calls += 1
        self.assertGreaterEqual(ax_calls, 5)

    # -- 11. one browser lane across capsules --------------------------------------------

    def test_browser_lane_is_shared_across_different_capsules(self):
        # Protects: the single process-wide lane in browser_lane_lock
        # (_BROWSER_THREAD_LOCK plus the file lock). Without it, tasks for
        # caps4-1 and caps4-2 would overlap in the runner.
        self.runner.mw_delay_s = 0.25
        results = {}

        def run(name, identity):
            results[name] = self.server.dispatch_tool(
                "vp_browser_task", {"identity": identity, "goal": "hi"})

        threads = [threading.Thread(target=run, args=("a", "caps4-1")),
                   threading.Thread(target=run, args=("b", "caps4-2"))]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=20)
            self.assertFalse(t.is_alive())
        for text, is_error in results.values():
            self.assertFalse(is_error, text)
        self.assertEqual(len(self.runner.mw_intervals), 2)
        first, second = sorted(self.runner.mw_intervals)
        self.assertGreaterEqual(second[0], first[1])

    # -- 12. file-lock contention ---------------------------------------------------------

    def test_file_lock_contention_reports_busy_skips_runner_and_frees_thread_lock(self):
        # Protects: the `raise AgentBusyError` on file-lock timeout AND the
        # `finally: _BROWSER_THREAD_LOCK.release()` in browser_lane_lock. If
        # the thread lock leaked, `locked()` stays True and the recovery call
        # below would also report busy. The runner must never be invoked
        # while the lane is held elsewhere.
        previous = vp_mcp.BROWSER_LOCK_WAIT_S
        vp_mcp.BROWSER_LOCK_WAIT_S = 0.2
        lock_path = os.path.join(self.session["xdg_state"], "vp_mcp.browser_task.lock")
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            result, payload = self.call_tool(
                "vp_browser_task", {"identity": "caps4-2", "goal": "hi"})
            self.assertTrue(result["isError"])
            self.assertIn("agent busy", payload["error"])
            self.assertEqual(self.runner.raw_calls, [])
            self.assertFalse(vp_mcp._BROWSER_THREAD_LOCK.locked())
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
            fd = None
            result, payload = self.call_tool(
                "vp_browser_task", {"identity": "caps4-2", "goal": "hi"})
            self.assertFalse(result["isError"])
            self.assertEqual(payload["rc"], 0)
        finally:
            vp_mcp.BROWSER_LOCK_WAIT_S = previous
            if fd is not None:
                os.close(fd)

    # -- 13. STALE_FRAME bound -------------------------------------------------------------

    def test_stale_frame_succeeds_on_last_allowed_attempt_with_fresh_observations(self):
        # Protects: `for attempt in range(3)` / `attempt < 2` in tool_navigate
        # and the re-observe on every attempt. Two refusals then success is
        # the exact edge of the bound; the three attempts of step 1 must each
        # be based on a newer frame generation (no replay of a stale frame).
        self.runner.propose_fail_code = "STALE_FRAME"
        self.runner.propose_fail_times = 2
        with mock.patch.object(vp_mcp.time, "sleep", lambda s: None):
            result, payload = self.nav("https://example.org/a")
        self.assertFalse(result["isError"], payload)
        self.assertEqual(len(payload["proposal_ids"]), 3)
        self.assertEqual(len(self.runner.proposals), 5)
        self.assertEqual(len(self.runner.observations), 5)
        self.assertEqual(len(self.runner.wait_ids), 3)
        gens = [p["based_on_frame"]["generation"] for p in self.runner.proposals[:3]]
        self.assertEqual(gens, sorted(set(gens)))
        self.assertEqual(len(gens), 3)
        ids = [p["proposal_id"] for p in self.runner.proposals[:3]]
        self.assertEqual(len(set(ids)), 3)
        self.assertEqual(payload["proposal_ids"][0], ids[2])

    # -- 14. non-STALE refusals never retried --------------------------------------------------

    def test_non_stale_propose_refusals_are_not_retried(self):
        # Protects: the `"STALE_FRAME" in str(exc)` condition guarding the
        # retry. Any other refusal (including INPUT_UNCERTAIN) must stop after
        # exactly one observe and one propose, with no wait-verify.
        for code in ("INPUT_UNCERTAIN", "POLICY_DENIED", "BUDGET_EXCEEDED"):
            with self.subTest(code=code):
                runner = base.FakeRunner(report_path=self.report_path)
                runner.propose_fail_code = code
                server = vp_mcp.ViewpointServer(
                    self.session, runner=runner, sleep=lambda s: None)
                with mock.patch.object(vp_mcp.time, "sleep", lambda s: None):
                    text, is_error = server.dispatch_tool(
                        "vp_navigate",
                        {"identity": "caps4-1", "url": "https://example.org/a"})
                self.assertTrue(is_error)
                self.assertIn(code, text)
                self.assertEqual(base.verbs_of(runner), ["ax", "observe", "propose"])
                self.assertEqual(len(runner.proposals), 1)
                self.assertEqual(runner.wait_ids, [])

    # -- 15. later-step wait-verify failure -----------------------------------------------------

    def test_wait_verify_failure_on_later_step_is_never_replayed(self):
        # Protects: the early `return` in the wait-verify except-branch of
        # tool_navigate. The input was already dispatched, so after a failed
        # verification no further proposal (and no replay) may be sent; in
        # particular the Return key must not follow an unverified typed URL.
        step_actions = [
            {"type": "key", "keys": ["ctrl", "l"]},
            {"type": "type", "text": "https://example.org/a"},
            {"type": "key", "keys": ["Return"]},
        ]
        for code in ("INPUT_UNCERTAIN", "STALE_FRAME"):
            for fail_at in (1, 2):
                with self.subTest(code=code, fail_at=fail_at):
                    runner = FailNthWaitRunner(self.report_path, fail_at, code)
                    server = vp_mcp.ViewpointServer(
                        self.session, runner=runner, sleep=lambda s: None)
                    with mock.patch.object(vp_mcp.time, "sleep", lambda s: None):
                        text, is_error = server.dispatch_tool(
                            "vp_navigate",
                            {"identity": "caps4-1", "url": "https://example.org/a"})
                    self.assertTrue(is_error)
                    self.assertIn(code, text)
                    expected = ["ax"] + ["observe", "propose", "wait-verify"] * (fail_at + 1)
                    self.assertEqual(base.verbs_of(runner), expected)
                    self.assertEqual(len(runner.proposals), fail_at + 1)
                    self.assertEqual(len(runner.wait_ids), fail_at + 1)
                    self.assertEqual(
                        [p["action"] for p in runner.proposals],
                        step_actions[:fail_at + 1])


if __name__ == "__main__":
    unittest.main()
