"""Unit tests for vp_mcp with a fake runner replaying real output shapes."""

import json
import os
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import vp_mcp  # noqa: E402


# Real shapes (AX reply, mw_run/v1 line, mvctl replies).
AX_JSON = json.dumps(
    {
        "window": {
            "id": "w1",
            "x": 0,
            "y": 0,
            "w": 1280,
            "h": 900,
            "title": "Example https://example.org/a",
            "url": "https://example.org/a",
        },
        "frame_seq": 1,
        "scroll": {"x": 0, "y": 0},
        "elements": [
            {
                "id": "ab12cd34",
                "role": "heading",
                "name": "Example Domain",
                "box": [40, 100, 900, 40],
                "focused": False,
            },
            {
                "id": "cd3456ef",
                "role": "textbox",
                "name": "Search",
                "box": [10, 80, 120, 30],
                "focused": False,
            },
            {
                "id": "ef789012",
                "role": "link",
                "name": "Learn more",
                "box": [40, 200, 200, 20],
                "focused": False,
            },
            {
                "id": "01234567",
                "role": "paragraph",
                "name": "This domain is for examples.",
                "box": [40, 250, 1000, 30],
                "focused": False,
            },
            {
                "id": "89abcdef",
                "role": "statictext",
                "name": "Footer note",
                "box": [40, 600, 500, 20],
                "focused": False,
            },
            {
                "id": "45678901",
                "role": "button",
                "name": "Submit",
                "box": [40, 300, 80, 30],
                "focused": True,
            },
        ],
        "truncated": False,
        "skipped_frames": 0,
    }
)

MW_RUN_JSON = json.dumps(
    {
        "schema": "metaviewer.mw_run/v1",
        "rc": 0,
        "verdict_code": "BROWSER_BOUNDED_ACTIONS_VERIFIED",
        "receipt": "/dev/shm/vp/runs/r1/moves.json",
        "report": None,
    }
)

REPORT_JSON = json.dumps(
    {
        "attempts": [
            {"action_kind": "click", "status": "verified_move"},
            {"action_kind": "type", "status": "verified_move"},
            {"action_kind": "key", "status": "verified_move"},
        ]
    }
)

SURFACES = [
    {
        "schema": "surface/v0.2",
        "surface_id": "surface-caps4-1",
        "kind": "browser",
        "display": ":101",
        "geometry": {"w": 1280, "h": 900},
        "state": "active",
        "visibility": "unfolded",
        "control": "agent",
        "profile_id": "caps4-1",
    },
    {
        "schema": "surface/v0.2",
        "surface_id": "surface-caps4-2",
        "kind": "browser",
        "display": ":102",
        "geometry": {"w": 1280, "h": 900},
        "state": "active",
        "visibility": "unfolded",
        "control": "agent",
        "profile_id": "caps4-2",
    },
]


def mvctl_verb(argv):
    """Return the mvctl/mw_run/ax verb of a recorded stripped argv."""
    if "metaviewer_agents.mvctl" in argv:
        idx = argv.index("metaviewer_agents.mvctl")
        return argv[idx + 1] if idx + 1 < len(argv) else ""
    if "metaviewer_agents.mw_run" in argv:
        return "mw_run"
    if len(argv) > 1 and argv[1] == "ax":
        return "ax"
    return None


class FakeRunner:
    """Runner seam replaying real shapes; records argv."""

    def __init__(self, report_path=None):
        self.calls = []
        self.raw_calls = []
        self.report_path = report_path
        self.timeout_mode = False
        self.propose_fail_code = None
        self.propose_fail_times = None
        self.ax_title = "Example https://example.org/a"
        self.ax_url = "https://example.org/a"
        self.ax_elements = None  # when set, replaces payload["elements"]
        self.ax_raw = None  # when set, returned verbatim for ax calls
        self.ax_url_final = None
        # Optional per-final-ax URL sequence for retry-on-mismatch tests:
        # ax_finals[0] is the first post-sequence observed URL, [1] the next.
        self.ax_finals = None
        self._ax_calls = 0
        self.ax_truncated = False
        self.ax_skipped_frames = 0
        self.mw_rc = 0
        self.mw_verdict = "BROWSER_BOUNDED_ACTIONS_VERIFIED"
        self.mw_stderr = ""
        self.wait_fail_mode = None  # None | "stale" | "uncertain" | "unverified"
        self.proposals = []
        self.observations = []
        self.wait_ids = []
        self._gen = 0
        self.mw_delay_s = 0.0
        self.mw_intervals = []
        self._mutex = threading.Lock()

    def __call__(self, argv, timeout, cwd=None):
        argv = list(argv)
        self.raw_calls.append(list(argv))
        if len(argv) > 2 and argv[0] == "/bin/bash":
            argv = argv[2:]
        self.calls.append(list(argv))
        if self.timeout_mode:
            raise TimeoutError("fake timeout")
        joined = " ".join(argv)
        if "mw_run" in joined:
            start = time.monotonic()
            if self.mw_delay_s:
                time.sleep(self.mw_delay_s)
            end = time.monotonic()
            with self._mutex:
                self.mw_intervals.append((start, end))
            payload = json.loads(MW_RUN_JSON)
            payload["rc"] = self.mw_rc
            payload["verdict_code"] = self.mw_verdict
            if self.report_path:
                payload["report"] = self.report_path
            return self.mw_rc, json.dumps(payload) + "\n", self.mw_stderr
        if len(argv) > 1 and argv[1] == "ax":
            self._ax_calls += 1
            if self.ax_raw is not None:
                return 0, self.ax_raw, ""
            payload = json.loads(AX_JSON)
            payload["window"]["title"] = self.ax_title
            if self.ax_finals is not None and self._ax_calls > 1:
                idx = min(self._ax_calls - 2, len(self.ax_finals) - 1)
                payload["window"]["url"] = self.ax_finals[idx]
            elif self.ax_url_final is not None and self._ax_calls > 1:
                payload["window"]["url"] = self.ax_url_final
            else:
                payload["window"]["url"] = self.ax_url
            if self.ax_elements is not None:
                payload["elements"] = self.ax_elements
            payload["truncated"] = self.ax_truncated
            payload["skipped_frames"] = self.ax_skipped_frames
            return 0, json.dumps(payload), ""
        if "metaviewer_agents.mvctl" in argv:
            return self._mvctl(argv)
        return 0, "{}", ""

    def _mvctl(self, argv):
        idx = argv.index("metaviewer_agents.mvctl")
        cmd = argv[idx + 1] if idx + 1 < len(argv) else ""
        if cmd == "list":
            return 0, json.dumps({"surfaces": SURFACES, "next_cursor": None}), ""
        if cmd == "observe":
            sid = argv[argv.index("--surface-id") + 1]
            self._gen += 1
            sha = "%064x" % (0xA0 + self._gen)
            obs = {
                "schema": "observation/v0.2",
                "frame_ref": {
                    "schema": "frame_ref/v0.2",
                    "surface_id": sid,
                    "generation": self._gen,
                    "tick": self._gen,
                    "ts_ns": 1000 * self._gen,
                    "width": 1280,
                    "height": 900,
                    "format": "rgb24",
                    "ramws_path": "/dev/shm/ramws/test.rgb",
                    "sha256": sha,
                    "capture_us": 1,
                },
            }
            self.observations.append(obs)
            return 0, json.dumps(obs), ""
        if cmd == "propose":
            path = argv[argv.index("--json-arg") + 1]
            with open(path, "r", encoding="utf-8") as fh:
                proposal = json.load(fh)
            self.proposals.append(proposal)
            if self.propose_fail_code is not None:
                if self.propose_fail_times is not None and len(self.proposals) > self.propose_fail_times:
                    pass
                else:
                    reply = {"verdict": "refuse", "reason_code": self.propose_fail_code}
                    return 1, json.dumps(reply), ""
            verdict = {
                "schema": "action_verdict/v0.2",
                "proposal_id": proposal["proposal_id"],
                "decision": "admitted",
                "core_seq": 1,
            }
            return 0, json.dumps(verdict), ""
        if cmd == "wait-verify":
            pid = argv[argv.index("--proposal-id") + 1]
            self.wait_ids.append(pid)
            if self.wait_fail_mode == "stale":
                return 1, json.dumps({"verdict": "refuse", "reason_code": "STALE_FRAME"}), ""
            if self.wait_fail_mode == "uncertain":
                return 1, json.dumps({"verdict": "refuse", "reason_code": "INPUT_UNCERTAIN"}), ""
            # IMP-WAIT: the core's own pending 'timeout' result (mvctl rc 1).
            self.wait_chunks = getattr(self, "wait_chunks", [])
            self.wait_chunks.append(int(argv[argv.index("--timeout-ms") + 1]))
            pending_left = getattr(self, "wait_timeouts", 0)
            if pending_left:
                self.wait_timeouts = pending_left - 1
                # Real mvctl writes its refusal record to stderr (mvctl.py _refuse).
                if getattr(self, "wait_timeout_kind", "core") == "client":
                    return 2, "", json.dumps({"verdict": "refuse", "code": "timeout", "error": "request timed out"})
                if getattr(self, "wait_timeout_kind", "core") == "transport":
                    return 2, "", json.dumps({"verdict": "refuse", "code": "transport_error", "error": "transport error"})
                return 1, json.dumps({"schema": "mv_tool_result/v0.2", "request_id": "r", "op": "wait_verify",
                                      "status": "timeout", "proposal_id": pid, "verified": False}), ""
            receipt = {
                "schema": "action_receipt/v0.2",
                "proposal_id": pid,
                "executed_at_ns": 2000,
                "after_frame": {"schema": "frame_ref/v0.2", "surface_id": "surface-caps4-1"},
                "verified": True,
                "verification": {"method": "pixel", "detail": "changed"},
                "error": "",
            }
            if self.wait_fail_mode == "unverified":
                receipt["verified"] = False
                return 0, json.dumps(receipt), ""
            return 0, json.dumps(receipt), ""
        return 0, "{}", ""


class TimeoutExpiredRunner:
    def __call__(self, argv, timeout, cwd=None):
        raise subprocess.TimeoutExpired(cmd=argv, timeout=timeout)


class FailNthWaitRunner(FakeRunner):
    """Refuse the `fail_at`-th wait-verify (0-based) with `code`."""

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


def make_session(root, report_path=None):
    env_launcher = os.path.join(root, "env.sh")
    srf_bin = os.path.join(root, "srf-identity-surface")
    for path in (env_launcher, srf_bin):
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("#!/bin/sh\n")
        os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)
    state_dir = os.path.join(root, "state")
    repo = os.path.join(root, "repo")
    xdg_state = os.path.join(root, "xdg")
    runtime = os.path.join(root, "runtime")
    for path in (state_dir, repo, xdg_state, runtime):
        os.makedirs(path, exist_ok=True)
    config_path = os.path.join(root, "core-config.json")
    config = {
        "session_id": "test-session",
        "core": {"endpoint": {"component": "core", "instance": "core-1"}},
        "peers": [{"endpoint": {"component": "agent", "instance": "agent-1"}}],
    }
    with open(config_path, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(config))
    attach_env = os.path.join(root, "current.env")
    with open(attach_env, "w", encoding="utf-8") as fh:
        fh.write("export MV_CORE_CONFIG='%s'\n" % config_path)
        fh.write('export XDG_RUNTIME_DIR="%s"\n' % runtime)
    session = {
        "env_launcher": env_launcher,
        "srf_bin": srf_bin,
        "state_dir": state_dir,
        "repo": repo,
        "xdg_state": xdg_state,
        "attach_env": attach_env,
        "capsules": [
            {"identity": "caps4-1", "container": "srf-id-caps4-1", "window": "w1",
             "surface_id": "surface-caps4-1"},
            {"identity": "caps4-2", "container": "srf-id-caps4-2", "window": "w1"},
        ],
    }
    return session


def verbs_of(runner):
    return [mvctl_verb(c) for c in runner.calls]


class TestVpMcp(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = self.tmp.name
        self.report_path = os.path.join(root, "report.json")
        with open(self.report_path, "w", encoding="utf-8") as fh:
            fh.write(REPORT_JSON)
        self.session = make_session(root, self.report_path)
        self.runner = FakeRunner(report_path=self.report_path)
        self.server = vp_mcp.ViewpointServer(self.session, runner=self.runner, sleep=lambda s: None)

    def tearDown(self):
        self.tmp.cleanup()

    # -- JSON-RPC plumbing -------------------------------------------------

    def rpc(self, obj):
        line = self.server.handle_line(json.dumps(obj))
        self.assertIsNotNone(line)
        return json.loads(line)

    def call_tool(self, name, arguments):
        return self.rpc(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}}
        )

    def tool_payload(self, response):
        result = response["result"]
        text = result["content"][0]["text"]
        return result, json.loads(text)

    def assert_no_native_verbs(self):
        for raw in self.runner.raw_calls:
            flat = list(raw)
            self.assertNotIn("input", flat)
            self.assertNotIn("frame", flat)
        for verb in verbs_of(self.runner):
            self.assertNotIn(verb, ("input", "frame"))

    def test_initialize(self):
        resp = self.rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        self.assertEqual(resp["result"]["serverInfo"]["name"], "viewpoint-agent")

    def test_tools_list_schema(self):
        resp = self.rpc({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
        tools = resp["result"]["tools"]
        names = [t["name"] for t in tools]
        # IMP-MCPID adds four identity tools; the original four are unchanged.
        self.assertEqual(
            set(names), {"vp_list_capsules", "vp_navigate", "vp_browser_task", "vp_read_page",
                         "vp_identity_list", "vp_identity_create", "vp_identity_set_label",
                         "vp_identity_delete", "vp_show", "vp_hide"}
        )
        for tool in tools:
            self.assertEqual(tool["inputSchema"]["type"], "object")
            self.assertIn("properties", tool["inputSchema"])
            self.assertIn("required", tool["inputSchema"])
        nav = [t for t in tools if t["name"] == "vp_navigate"][0]
        self.assertEqual(set(nav["inputSchema"]["required"]), {"identity", "url"})
        task = [t for t in tools if t["name"] == "vp_browser_task"][0]
        self.assertEqual(set(task["inputSchema"]["required"]), {"identity", "goal"})
        self.assertEqual(task["inputSchema"]["properties"]["key"]["enum"], ["Enter"])

    def test_jsonrpc_parse_error(self):
        line = self.server.handle_line("not json {")
        self.assertIsNotNone(line)
        self.assertEqual(json.loads(line)["error"]["code"], -32700)

    def test_jsonrpc_unknown_method(self):
        resp = self.rpc({"jsonrpc": "2.0", "id": 9, "method": "bogus/method", "params": {}})
        self.assertEqual(resp["error"]["code"], -32601)

    def test_jsonrpc_unknown_tool(self):
        resp = self.rpc(
            {"jsonrpc": "2.0", "id": 9, "method": "tools/call", "params": {"name": "bogus", "arguments": {}}}
        )
        self.assertEqual(resp["error"]["code"], -32602)

    def test_jsonrpc_invalid_params(self):
        resp = self.rpc({"jsonrpc": "2.0", "id": 9, "method": "tools/call", "params": {}})
        self.assertEqual(resp["error"]["code"], -32602)

    # -- happy paths ---------------------------------------------------------

    def test_list_capsules_happy(self):
        resp = self.call_tool("vp_list_capsules", {})
        _, payload = self.tool_payload(resp)
        self.assertEqual(len(payload["capsules"]), 2)
        first = payload["capsules"][0]
        self.assertEqual(first["identity"], "caps4-1")
        self.assertEqual(first["surface_id"], "surface-caps4-1")
        self.assertEqual(first["kind"], "browser")
        self.assertEqual(first["state"], "active")
        verbs = verbs_of(self.runner)
        self.assertEqual(verbs, ["list"])
        argv = self.runner.calls[0]
        self.assertIn("metaviewer_agents.mvctl", argv)
        self.assertIn("--limit", argv)
        # Core identity routing comes from the attach file, not argv guessing.
        self.assertIn("--session-id", argv)
        self.assertIn("--agent-instance", argv)
        self.assertIn("--core-instance", argv)
        self.assertIn("--socket", argv)
        self.assert_no_native_verbs()

    def test_navigate_happy(self):
        resp = self.call_tool("vp_navigate", {"identity": "caps4-1", "url": "https://example.org/a"})
        _, payload = self.tool_payload(resp)
        self.assertEqual(payload["identity"], "caps4-1")
        self.assertEqual(payload["surface_id"], "surface-caps4-1")
        self.assertEqual(payload["url"], "https://example.org/a")
        self.assertEqual(payload["observed_url"], "https://example.org/a")
        self.assertEqual(len(payload["proposal_ids"]), 3)
        # read-only ax first, then observe -> propose -> wait-verify per
        # step, then a final read-only ax reporting the observed URL.
        verbs = verbs_of(self.runner)
        self.assertEqual(
            verbs,
            ["ax",
             "observe", "propose", "wait-verify",
             "observe", "propose", "wait-verify",
             "observe", "propose", "wait-verify",
             "ax"],
        )
        self.assert_no_native_verbs()
        # Three bound proposals: fresh observation per step, no blind retries.
        self.assertEqual(len(self.runner.proposals), 3)
        self.assertEqual(len(self.runner.observations), 3)
        actions = [p["action"] for p in self.runner.proposals]
        self.assertEqual(actions[0], {"type": "key", "keys": ["ctrl", "l"]})
        self.assertEqual(actions[1], {"type": "type", "text": "https://example.org/a"})
        self.assertEqual(actions[2], {"type": "key", "keys": ["Return"]})
        for idx, proposal in enumerate(self.runner.proposals):
            observed = self.runner.observations[idx]["frame_ref"]
            self.assertEqual(proposal["schema"], "action_proposal/v0.2")
            self.assertEqual(proposal["surface_id"], "surface-caps4-1")
            self.assertEqual(proposal["based_on_frame"]["generation"], observed["generation"])
            self.assertEqual(proposal["based_on_frame"]["sha256"], observed["sha256"])
            self.assertEqual(proposal["budget"]["max_retries"], 0)
            self.assertEqual(self.runner.wait_ids[idx], proposal["proposal_id"])
            self.assertEqual(payload["proposal_ids"][idx], proposal["proposal_id"])

    def test_navigate_resolves_surface_via_list(self):
        resp = self.call_tool("vp_navigate", {"identity": "caps4-2", "url": "https://example.org/a"})
        _, payload = self.tool_payload(resp)
        self.assertEqual(payload["surface_id"], "surface-caps4-2")
        verbs = verbs_of(self.runner)
        self.assertEqual(verbs[0], "list")
        self.assertEqual(verbs[1], "ax")
        self.assertEqual(
            verbs[2:],
            ["observe", "propose", "wait-verify"] * 3 + ["ax"],
        )
        self.assert_no_native_verbs()

    def test_browser_task_happy(self):
        resp = self.call_tool(
            "vp_browser_task",
            {"identity": "caps4-1", "goal": "Search Wikipedia", "text": "AI agent", "key": "Enter"},
        )
        _, payload = self.tool_payload(resp)
        self.assertEqual(payload["rc"], 0)
        self.assertEqual(payload["verdict_code"], "BROWSER_BOUNDED_ACTIONS_VERIFIED")
        self.assertEqual(payload["report"], self.report_path)
        self.assertEqual(payload["attempts"], ["verified_move", "verified_move", "verified_move"])
        argv = self.runner.calls[0]
        self.assertEqual(self.runner.raw_calls[0][:2], ["/bin/bash", self.session["env_launcher"]])
        self.assertEqual(argv[0:1], ["/usr/bin/env"])
        self.assertTrue(argv[1].startswith("XDG_STATE_HOME="))
        self.assertTrue(argv[2].startswith("MV_WENDER_ROOT="))
        self.assertTrue(argv[3].startswith("PYTHONPATH="))
        self.assertIn("metaviewer_agents.mw_run", argv)
        self.assertIn("--steps", argv)
        self.assertEqual(argv[argv.index("--steps") + 1], "6")
        self.assertEqual(argv[argv.index("--timeout-ms") + 1], "30000")
        self.assertEqual(argv[argv.index("--planner") + 1], "rules")
        self.assertEqual(argv[argv.index("--goal") + 1], "Search Wikipedia")

    def test_browser_task_minimal(self):
        resp = self.call_tool("vp_browser_task", {"identity": "caps4-1", "goal": "Search Wikipedia"})
        _, payload = self.tool_payload(resp)
        self.assertEqual(payload["rc"], 0)
        argv = self.runner.calls[0]
        self.assertNotIn("--text", argv)
        self.assertNotIn("--key", argv)

    def test_read_page_happy(self):
        resp = self.call_tool("vp_read_page", {"identity": "caps4-1", "max_chars": 2000})
        _, payload = self.tool_payload(resp)
        self.assertEqual(payload["title"], "Example https://example.org/a")
        self.assertIn("UNTRUSTED PAGE CONTENT", json.dumps(payload))
        self.assertIn("Example Domain", payload["text"])
        self.assertIn("Learn more", payload["text"])
        self.assertIn("This domain is for examples.", payload["text"])
        self.assertIn("Footer note", payload["text"])
        self.assertFalse(payload["truncated"])
        self.assertNotIn("Submit", payload["text"])
        self.assertNotIn("Search", payload["text"] if "Search" not in ("Example Domain",) else "")
        argv = self.runner.calls[0]
        self.assertEqual(argv[1], "ax")

    def test_read_page_truncation(self):
        resp = self.call_tool("vp_read_page", {"identity": "caps4-1", "max_chars": 10})
        _, payload = self.tool_payload(resp)
        self.assertTrue(payload["truncated"])
        self.assertLessEqual(len(payload["text"]), 10)

    # -- refusal / stop behaviour ---------------------------------------------

    def test_navigate_stops_on_stale_frame(self):
        self.runner.propose_fail_code = "STALE_FRAME"
        resp = self.call_tool("vp_navigate", {"identity": "caps4-1", "url": "https://example.org/a"})
        result, payload = self.tool_payload(resp)
        self.assertTrue(result["isError"])
        self.assertIn("STALE_FRAME", payload["error"])
        # Up to 3 attempts per step: re-observe + propose again, then stop.
        verbs = verbs_of(self.runner)
        self.assertEqual(
            verbs,
            ["ax", "observe", "propose", "observe", "propose", "observe", "propose"],
        )
        self.assertEqual(len(self.runner.proposals), 3)
        self.assertEqual(self.runner.wait_ids, [])
        self.assert_no_native_verbs()

    def test_navigate_stops_on_input_uncertain(self):
        self.runner.wait_fail_mode = "uncertain"
        resp = self.call_tool("vp_navigate", {"identity": "caps4-1", "url": "https://example.org/a"})
        result, payload = self.tool_payload(resp)
        self.assertTrue(result["isError"])
        self.assertIn("INPUT_UNCERTAIN", payload["error"])
        verbs = verbs_of(self.runner)
        self.assertEqual(verbs, ["ax", "observe", "propose", "wait-verify"])
        # No retry and no second step after the unverified input.
        self.assertEqual(len(self.runner.proposals), 1)

    def test_navigate_skips_ctrl_l_on_about_blank(self):
        self.runner.ax_title = "about:blank - Chromium"
        self.runner.ax_url = ""
        self.runner.ax_elements = []
        self.runner.ax_url_final = "https://example.org/a"
        resp = self.call_tool("vp_navigate", {"identity": "caps4-1", "url": "https://example.org/a"})
        _, payload = self.tool_payload(resp)
        self.assertEqual(len(payload["proposal_ids"]), 2)
        verbs = verbs_of(self.runner)
        self.assertEqual(
            verbs,
            ["ax",
             "observe", "propose", "wait-verify",
             "observe", "propose", "wait-verify",
             "ax"],
        )
        actions = [p["action"] for p in self.runner.proposals]
        self.assertEqual(actions[0], {"type": "type", "text": "https://example.org/a"})
        self.assertEqual(actions[1], {"type": "key", "keys": ["Return"]})
        for action in actions:
            self.assertNotEqual(action, {"type": "key", "keys": ["ctrl", "l"]})

    def test_navigate_includes_ctrl_l_when_not_blank(self):
        self.runner.ax_title = "Example https://example.org/a"
        resp = self.call_tool("vp_navigate", {"identity": "caps4-1", "url": "https://example.org/a"})
        _, payload = self.tool_payload(resp)
        self.assertEqual(len(payload["proposal_ids"]), 3)
        actions = [p["action"] for p in self.runner.proposals]
        self.assertEqual(actions[0], {"type": "key", "keys": ["ctrl", "l"]})
        self.assertEqual(actions[1], {"type": "type", "text": "https://example.org/a"})
        self.assertEqual(actions[2], {"type": "key", "keys": ["Return"]})

    def test_navigate_retries_stale_frame_then_succeeds(self):
        self.runner.propose_fail_code = "STALE_FRAME"
        self.runner.propose_fail_times = 1
        resp = self.call_tool("vp_navigate", {"identity": "caps4-1", "url": "https://example.org/a"})
        _, payload = self.tool_payload(resp)
        self.assertEqual(len(payload["proposal_ids"]), 3)
        verbs = verbs_of(self.runner)
        self.assertEqual(
            verbs,
            ["ax",
             "observe", "propose",
             "observe", "propose", "wait-verify",
             "observe", "propose", "wait-verify",
             "observe", "propose", "wait-verify",
             "ax"],
        )
        self.assertEqual(len(self.runner.proposals), 4)
        self.assertEqual(len(self.runner.observations), 4)
        self.assertEqual(len(self.runner.wait_ids), 3)

    def test_navigate_stops_on_unverified_receipt(self):
        self.runner.wait_fail_mode = "unverified"
        resp = self.call_tool("vp_navigate", {"identity": "caps4-1", "url": "https://example.org/a"})
        result, payload = self.tool_payload(resp)
        self.assertTrue(result["isError"])
        self.assertIn("INPUT_UNCERTAIN", payload["error"])
        self.assertEqual(len(self.runner.proposals), 1)

    # -- regression: unknown/uncertain failure surfacing ----------------------

    def test_browser_task_failure_is_error(self):
        self.runner.mw_rc = 5
        self.runner.mw_verdict = "INPUT_UNCERTAIN"
        self.runner.mw_stderr = "frame stale"
        resp = self.call_tool("vp_browser_task", {"identity": "caps4-1", "goal": "hi"})
        result, payload = self.tool_payload(resp)
        self.assertTrue(result["isError"])
        self.assertEqual(payload["rc"], 5)
        self.assertIn("INPUT_UNCERTAIN", payload["error"])
        self.assertIn("do not replay", payload["error"])
        self.assertEqual(payload["receipt"], "/dev/shm/vp/runs/r1/moves.json")
        self.assertEqual(payload["stderr"], "frame stale")

    def test_browser_task_unverified_verdict_is_error(self):
        self.runner.mw_verdict = "BROWSER_UNVERIFIED"
        resp = self.call_tool("vp_browser_task", {"identity": "caps4-1", "goal": "hi"})
        result, _ = self.tool_payload(resp)
        self.assertTrue(result["isError"])

    def test_navigate_propose_timeout_is_input_uncertain(self):
        self.runner.timeout_mode = False
        self.runner.propose_fail_code = None
        calls = []

        def timing_out(argv, timeout, cwd=None):
            if "propose" in " ".join(argv):
                raise TimeoutError("fake propose timeout")
            return orig(argv, timeout, cwd=cwd)

        orig = self.runner.__call__
        self.server.runner = lambda argv, timeout, cwd=None: timing_out(argv, timeout, cwd)
        resp = self.call_tool("vp_navigate", {"identity": "caps4-1", "url": "https://example.org/a"})
        result, payload = self.tool_payload(resp)
        self.assertTrue(result["isError"])
        self.assertIn("INPUT_UNCERTAIN", payload["error"])
        self.assertIn("completed_proposal_ids", payload)
        self.assertIn("failed_step", payload)

    def test_navigate_observed_url_mismatch_is_error(self):
        # Both attempts mismatch (bounded retry exhausts), then isError.
        self.runner.ax_url_final = "https://evil.example/a"
        resp = self.call_tool("vp_navigate", {"identity": "caps4-1", "url": "https://example.org/a"})
        result, payload = self.tool_payload(resp)
        self.assertTrue(result["isError"])
        self.assertTrue(payload.get("url_mismatch"))
        self.assertEqual(payload["observed_url"], "https://evil.example/a")
        self.assertEqual(payload["url"], "https://example.org/a")
        # One retry of the whole 3-step sequence, then stop.
        self.assertEqual(len(self.runner.proposals), 6)
        self.assertEqual(len(payload["proposal_ids"]), 6)
        self.assertEqual(len(payload["completed_proposal_ids"]), 6)

    def test_navigate_retries_once_on_mismatch_then_succeeds(self):
        # First observed URL mismatches, the retry matches: success, and the
        # step labels are exactly the 3-step sequence repeated twice.
        self.runner.ax_finals = ["https://evil.example/a", "https://example.org/a"]
        resp = self.call_tool("vp_navigate", {"identity": "caps4-1", "url": "https://example.org/a"})
        result, payload = self.tool_payload(resp)
        self.assertFalse(result["isError"], payload)
        self.assertEqual(payload["observed_url"], "https://example.org/a")
        self.assertNotIn("url_mismatch", payload)
        self.assertEqual(len(self.runner.proposals), 6)
        self.assertEqual(len(payload["proposal_ids"]), 6)
        actions = [p["action"] for p in self.runner.proposals]
        seq = [
            {"type": "key", "keys": ["ctrl", "l"]},
            {"type": "type", "text": "https://example.org/a"},
            {"type": "key", "keys": ["Return"]},
        ]
        self.assertEqual(actions, seq * 2)
        for action in actions:
            self.assertNotEqual(action, {"type": "key", "keys": ["ctrl", "a"]})

    def test_navigate_input_uncertain_on_retry_is_not_retried(self):
        # Mismatch on attempt 1, INPUT_UNCERTAIN on attempt 2: the uncertain
        # input stops navigation; there is no third attempt.
        runner = FailNthWaitRunner(self.report_path, fail_at=3, code="INPUT_UNCERTAIN")
        runner.ax_finals = ["https://evil.example/a", "https://example.org/a"]
        server = vp_mcp.ViewpointServer(self.session, runner=runner, sleep=lambda s: None)
        text, is_error = server.dispatch_tool(
            "vp_navigate", {"identity": "caps4-1", "url": "https://example.org/a"})
        payload = json.loads(text)
        self.assertTrue(is_error, payload)
        self.assertIn("INPUT_UNCERTAIN", payload["error"])
        # 3 proposals of attempt 1 + 1 of attempt 2, then stop.
        self.assertEqual(len(runner.proposals), 4)
        self.assertEqual(payload.get("failed_step"), "ctrl+l")

    def test_navigate_appended_omnibox_text_is_error(self):
        # Regression for the hn.algolia.com run: typed text appended to the
        # existing URL, same host, must be an error (old host-only check
        # wrongly succeeded). The normalised URLs differ only in the path.
        self.runner.ax_url_final = "https://hn.algolia.com/https://hn.algolia.com/"
        resp = self.call_tool("vp_navigate", {"identity": "caps4-1", "url": "https://hn.algolia.com/"})
        result, payload = self.tool_payload(resp)
        self.assertTrue(result["isError"])
        self.assertTrue(payload.get("url_mismatch"))
        self.assertEqual(payload["observed_url"], "https://hn.algolia.com/https://hn.algolia.com/")
        self.assertEqual(payload["url"], "https://hn.algolia.com/")
        want_host = vp_mcp.urlparse(payload["url"]).hostname
        got_host = vp_mcp.urlparse(payload["observed_url"]).hostname
        self.assertEqual(want_host, got_host)

    def test_navigate_exact_match_success(self):
        cases = [
            ("https://example.org/a", "https://example.org/a"),
            # A query URL never reaches the matcher any more (the real AX
            # strips the query); see test_navigate_query_url_refused_*.
            ("https://example.org/a/b", "https://example.org/a/b/"),
        ]
        for requested, observed in cases:
            with self.subTest(requested=requested, observed=observed):
                runner = FakeRunner(report_path=self.report_path)
                runner.ax_url = requested
                runner.ax_url_final = observed
                server = vp_mcp.ViewpointServer(self.session, runner=runner, sleep=lambda s: None)
                text, is_error = server.dispatch_tool(
                    "vp_navigate", {"identity": "caps4-1", "url": requested})
                payload = json.loads(text)
                self.assertFalse(is_error, payload)
                self.assertEqual(payload["url"], requested)
                self.assertEqual(payload["observed_url"], observed)
                self.assertNotIn("url_mismatch", payload)

    def test_navigate_trailing_slash_tolerance(self):
        cases = [
            ("https://example.org/a", "https://example.org/a/"),
            ("https://example.org/a/", "https://example.org/a"),
            ("https://example.org", "https://example.org/"),
        ]
        for requested, observed in cases:
            with self.subTest(requested=requested, observed=observed):
                runner = FakeRunner(report_path=self.report_path)
                runner.ax_url = requested
                runner.ax_url_final = observed
                server = vp_mcp.ViewpointServer(self.session, runner=runner, sleep=lambda s: None)
                text, is_error = server.dispatch_tool(
                    "vp_navigate", {"identity": "caps4-1", "url": requested})
                payload = json.loads(text)
                self.assertFalse(is_error, payload)
                self.assertEqual(payload["url"], requested)
                self.assertEqual(payload["observed_url"], observed)

    def test_navigate_ctrl_l_verification_failed_is_fatal(self):
        # No step tolerates VERIFICATION_FAILED: fail_at=0 (ctrl+l) stops
        # navigation immediately, unretried.
        runner = FailNthWaitRunner(self.report_path, fail_at=0, code="VERIFICATION_FAILED")
        server = vp_mcp.ViewpointServer(self.session, runner=runner, sleep=lambda s: None)
        text, is_error = server.dispatch_tool(
            "vp_navigate", {"identity": "caps4-1", "url": "https://example.org/a"})
        payload = json.loads(text)
        self.assertTrue(is_error, payload)
        self.assertIn("VERIFICATION_FAILED", payload["error"])
        self.assertEqual(payload.get("failed_step"), "ctrl+l")
        self.assertEqual(len(runner.proposals), 1)
        self.assertNotIn("tolerated_verification_failures", payload)

    def test_navigate_type_verification_failed_still_fatal(self):
        # VERIFICATION_FAILED must never be tolerated for typing or Enter.
        for fail_at, failed_step in ((1, "type-url"), (2, "Return")):
            with self.subTest(fail_at=fail_at, failed_step=failed_step):
                runner = FailNthWaitRunner(self.report_path, fail_at=fail_at, code="VERIFICATION_FAILED")
                server = vp_mcp.ViewpointServer(self.session, runner=runner, sleep=lambda s: None)
                text, is_error = server.dispatch_tool(
                    "vp_navigate", {"identity": "caps4-1", "url": "https://example.org/a"})
                payload = json.loads(text)
                self.assertTrue(is_error, payload)
                self.assertIn("VERIFICATION_FAILED", payload["error"])
                self.assertEqual(payload.get("failed_step"), failed_step)

    def test_navigate_query_url_refused_before_any_input(self):
        # L1: the AX surface strips query and fragment from window.url
        # (ax-shim.py safe_url, surface/ax.go safeURL), so a non-empty
        # query can never verify. Refuse before list/ax/propose.
        for url in ("https://example.org/a?x=1",
                    "https://example.com?demo_capsule=1",
                    "https://en.wikipedia.org/w/index.php?search=agent&title=Special:Search",
                    "https://example.org/a?x=1#frag"):
            with self.subTest(url=url):
                self.runner.calls.clear()
                self.runner.proposals.clear()
                text, is_error = self.server.dispatch_tool(
                    "vp_navigate", {"identity": "caps4-1", "url": url})
                payload = json.loads(text)
                self.assertTrue(is_error, payload)
                self.assertEqual(payload["code"], "URL_QUERY_UNOBSERVABLE")
                self.assertTrue(payload["error"].startswith("URL_QUERY_UNOBSERVABLE:"))
                self.assertNotIn("INPUT_UNCERTAIN", payload["error"])
                self.assertEqual(payload["completed_proposal_ids"], [])
                self.assertEqual(payload["url"], url)
                self.assertIn("hint", payload)
                # Zero proposals and zero subprocess calls of any kind.
                self.assertEqual(self.runner.proposals, [])
                self.assertEqual(self.runner.calls, [])

    def test_navigate_fragment_url_refused_before_any_input(self):
        # Sol IMP-QURL fix: _urls_match ignores fragments and the AX surface
        # strips them, so '#/route' could "verify" with no proof it arrived.
        for url in ("https://example.org/a#sec", "https://example.org/app#/wanted"):
            with self.subTest(url=url):
                self.runner.calls.clear()
                self.runner.proposals.clear()
                text, is_error = self.server.dispatch_tool(
                    "vp_navigate", {"identity": "caps4-1", "url": url})
                payload = json.loads(text)
                self.assertTrue(is_error, payload)
                self.assertEqual(payload["code"], "URL_FRAGMENT_UNOBSERVABLE")
                self.assertEqual(payload["completed_proposal_ids"], [])
                self.assertEqual(self.runner.proposals, [])
                self.assertEqual(self.runner.calls, [])

    def test_navigate_query_free_urls_unchanged_negative_control(self):
        # Negative control: plain URLs, and an empty '?' or '#' (nothing the
        # AX surface could fail to report), keep the normal three-proposal
        # path and verify against the document URL as before.
        for url, observed in (("https://example.org/a", "https://example.org/a"),
                              ("https://example.org/a?", "https://example.org/a"),
                              ("https://example.org/a#", "https://example.org/a")):
            with self.subTest(url=url):
                self.runner.proposals.clear()
                self.runner.ax_url_final = observed
                text, is_error = self.server.dispatch_tool(
                    "vp_navigate", {"identity": "caps4-1", "url": url})
                payload = json.loads(text)
                self.assertFalse(is_error, payload)
                self.assertNotIn("code", payload)
                self.assertEqual(payload["url"], url)
                self.assertEqual(payload["observed_url"], observed)
                self.assertEqual(len(self.runner.proposals), 3)

    def test_urls_match_query_still_strict(self):
        # The fix must not relax the matcher: a different query is still
        # a different document.
        self.assertFalse(vp_mcp._urls_match("https://example.org/a?x=1", "https://example.org/a"))
        self.assertFalse(vp_mcp._urls_match("https://example.org/a?x=1", "https://example.org/a?x=2"))

    def test_navigate_budget_and_wait_timeout_alignment(self):
        # BH02 class A: 30 s budget, wait-verify timeout > budget, outer
        # subprocess timeout > wait timeout.
        self.assertEqual(vp_mcp._NAV_BUDGET["max_ms"], 30000)
        self.assertGreater(vp_mcp.MVCTL_WAIT_TIMEOUT_MS, vp_mcp._NAV_BUDGET["max_ms"])
        self.assertGreater(vp_mcp.TIMEOUT_MVCTL_WAIT, vp_mcp.MVCTL_WAIT_TIMEOUT_MS / 1000.0)
        # Budget is sent on every proposal.
        resp = self.call_tool("vp_navigate", {"identity": "caps4-1", "url": "https://example.org/a"})
        _, payload = self.tool_payload(resp)
        self.assertEqual(payload["identity"], "caps4-1")
        self.assertEqual(len(self.runner.proposals), 3)
        for proposal in self.runner.proposals:
            self.assertEqual(proposal["budget"]["max_ms"], 30000)
        # IMP-WAIT: each wait-verify call stays below the core's 30 s idle
        # deadline; the total wait still outlasts the 30 s verify budget.
        self.assertLess(vp_mcp.MVCTL_WAIT_CHUNK_MS, 30000)
        for call in self.runner.calls:
            if "wait-verify" in call:
                timeout = int(call[call.index("--timeout-ms") + 1])
                self.assertEqual(timeout, vp_mcp.MVCTL_WAIT_CHUNK_MS)
                self.assertLess(timeout, 30000)

    def test_navigate_wait_core_timeout_rewaits_same_proposal(self):
        # IMP-WAIT: a core 'timeout' (still pending) is waited on again, read
        # only, on the same proposal id; no extra proposal is sent.
        self.runner.wait_timeouts = 1
        text, is_error = self.server.dispatch_tool(
            "vp_navigate", {"identity": "caps4-1", "url": "https://example.org/a"})
        self.assertFalse(is_error, text)
        self.assertEqual(len(self.runner.proposals), 3)
        first = self.runner.proposals[0]["proposal_id"]
        self.assertEqual(self.runner.wait_ids[:2], [first, first])
        self.assertEqual(self.runner.wait_chunks[:2], [vp_mcp.MVCTL_WAIT_CHUNK_MS,
                                                      vp_mcp.MVCTL_WAIT_TIMEOUT_MS - vp_mcp.MVCTL_WAIT_CHUNK_MS])

    def test_navigate_wait_core_timeout_bounded_then_uncertain(self):
        # Total wait stays the 35 s budget: exactly two chunks, then INPUT_UNCERTAIN.
        self.runner.wait_timeouts = 99
        text, is_error = self.server.dispatch_tool(
            "vp_navigate", {"identity": "caps4-1", "url": "https://example.org/a"})
        payload = json.loads(text)
        self.assertTrue(is_error)
        self.assertIn("INPUT_UNCERTAIN", payload["error"])
        self.assertEqual(len(self.runner.wait_ids), 2)
        self.assertEqual(sum(self.runner.wait_chunks), vp_mcp.MVCTL_WAIT_TIMEOUT_MS)
        self.assertEqual(len(self.runner.proposals), 1)

    def test_navigate_wait_client_timeout_rewaits_same_proposal(self):
        # mvctl's own request deadline equals the chunk, so it usually fires
        # before the core's 'timeout' result: also a read-only re-wait.
        self.runner.wait_timeouts = 1
        self.runner.wait_timeout_kind = "client"
        text, is_error = self.server.dispatch_tool(
            "vp_navigate", {"identity": "caps4-1", "url": "https://example.org/a"})
        self.assertFalse(is_error, text)
        first = self.runner.proposals[0]["proposal_id"]
        self.assertEqual(self.runner.wait_ids[:2], [first, first])
        self.assertEqual(len(self.runner.proposals), 3)

    def test_navigate_wait_transport_error_is_final(self):
        self.runner.wait_timeouts = 1
        self.runner.wait_timeout_kind = "transport"
        text, is_error = self.server.dispatch_tool(
            "vp_navigate", {"identity": "caps4-1", "url": "https://example.org/a"})
        self.assertTrue(is_error)
        self.assertIn("transport_error", json.loads(text)["error"])
        self.assertEqual(len(self.runner.wait_ids), 1)

    def test_navigate_wait_respects_monotonic_deadline(self):
        # Sol IMP-WAIT: calls that take real time shrink the next chunk and the
        # subprocess timeout; no chunk starts past the 35 s deadline.
        now = [0.0]
        runner = self.runner
        runner.wait_timeouts = 99
        base = type(runner).__call__
        timeouts = []
        def call(argv, timeout, cwd=None):
            if "wait-verify" in argv:
                timeouts.append(timeout)
                now[0] += 27.0  # chunk 25 s plus 2 s of mvctl start/teardown
            return base(runner, argv, timeout, cwd=cwd)
        server = vp_mcp.ViewpointServer(self.session, runner=call, sleep=lambda s: None, clock=lambda: now[0])
        text, is_error = server.dispatch_tool(
            "vp_navigate", {"identity": "caps4-1", "url": "https://example.org/a"})
        self.assertTrue(is_error)
        self.assertEqual(runner.wait_chunks, [25000, 8000])
        self.assertEqual(timeouts, [25000 / 1000 + vp_mcp.MVCTL_WAIT_CALL_SLACK_S, 8000 / 1000 + vp_mcp.MVCTL_WAIT_CALL_SLACK_S])

    def test_navigate_wait_malformed_timeout_is_final(self):
        # Timeout-shaped but not exact (extra refusal field, wrong rc, wrong op)
        # is never re-waited.
        for reply, rc in (({"schema": "mv_tool_result/v0.2", "request_id": "r", "op": "wait_verify", "status": "timeout",
                            "proposal_id": "PID", "verified": False, "reason_code": "INPUT_UNCERTAIN"}, 1),
                          ({"schema": "mv_tool_result/v0.2", "request_id": "r", "op": "wait_verify", "status": "timeout",
                            "proposal_id": "PID", "verified": False}, 2),
                          ({"schema": "mv_tool_result/v0.2", "request_id": "r", "op": "observe", "status": "timeout",
                            "proposal_id": "PID", "verified": False}, 1)):
            with self.subTest(reply=reply, rc=rc):
                runner = FakeRunner(report_path=self.report_path)
                base = type(runner).__call__
                waits = []
                def call(argv, timeout, cwd=None, reply=reply, rc=rc, runner=runner, waits=waits):
                    if "wait-verify" in argv:
                        pid = argv[argv.index("--proposal-id") + 1]
                        waits.append(pid)
                        if len(waits) == 1:
                            r = dict(reply); r["proposal_id"] = pid
                            return rc, json.dumps(r), ""
                    return base(runner, argv, timeout, cwd=cwd)
                server = vp_mcp.ViewpointServer(self.session, runner=call, sleep=lambda s: None)
                text, is_error = server.dispatch_tool(
                    "vp_navigate", {"identity": "caps4-1", "url": "https://example.org/a"})
                self.assertTrue(is_error, text)
                self.assertEqual(len(waits), 1)

    def test_navigate_wait_contradictory_stderr_is_final(self):
        # Sol IMP-WAIT r2: a timeout record after another record is not a clean
        # client timeout.
        runner = FakeRunner(report_path=self.report_path)
        base = type(runner).__call__
        waits = []
        def call(argv, timeout, cwd=None):
            if "wait-verify" in argv:
                waits.append(1)
                if len(waits) == 1:
                    return 2, "", (json.dumps({"verdict": "refuse", "code": "transport_error", "error": "x"}) + "\n"
                                   + json.dumps({"verdict": "refuse", "code": "timeout", "error": "request timed out"}))
            return base(runner, argv, timeout, cwd=cwd)
        server = vp_mcp.ViewpointServer(self.session, runner=call, sleep=lambda s: None)
        text, is_error = server.dispatch_tool(
            "vp_navigate", {"identity": "caps4-1", "url": "https://example.org/a"})
        self.assertTrue(is_error)
        self.assertEqual(len(waits), 1)

    def test_navigate_wait_no_subminimum_chunk_after_delay(self):
        # The remaining time is checked right before dispatch: a delay after the
        # first chunk leaving < 1 s never launches a second wait.
        now = [0.0]
        runner = self.runner
        runner.wait_timeouts = 99
        base = type(runner).__call__
        def call(argv, timeout, cwd=None):
            if "wait-verify" in argv:
                now[0] += 34.5
            return base(runner, argv, timeout, cwd=cwd)
        server = vp_mcp.ViewpointServer(self.session, runner=call, sleep=lambda s: None, clock=lambda: now[0])
        text, is_error = server.dispatch_tool(
            "vp_navigate", {"identity": "caps4-1", "url": "https://example.org/a"})
        self.assertTrue(is_error)
        self.assertEqual(runner.wait_chunks, [25000])

    def test_navigate_wait_refusal_is_never_rewaited(self):
        self.runner.wait_fail_mode = "uncertain"
        text, is_error = self.server.dispatch_tool(
            "vp_navigate", {"identity": "caps4-1", "url": "https://example.org/a"})
        self.assertTrue(is_error)
        self.assertEqual(len(self.runner.wait_ids), 1)

    def test_navigate_propose_timeout_after_admitted_step_includes_current_id(self):
        # After ctrl+l was admitted and verified, a propose timeout on
        # type-url must be INPUT_UNCERTAIN and include the current
        # possibly-dispatched id in completed_proposal_ids.
        # First propose succeeds, second propose times out.
        calls = {"n": 0}
        orig = self.runner.__call__
        captured_ids = []

        def flaky(argv, timeout, cwd=None):
            if "propose" in " ".join(argv):
                calls["n"] += 1
                if calls["n"] == 2:
                    # Record the would-be proposal before timing out so the
                    # test can compare against completed_proposal_ids.
                    path = argv[argv.index("--json-arg") + 1]
                    with open(path, "r", encoding="utf-8") as fh:
                        _p = json.load(fh)
                    captured_ids.append(_p["proposal_id"])
                    raise TimeoutError("fake propose timeout after admission")
            # Delegate to FakeRunner; it records proposals for the 1st call.
            return orig(argv, timeout, cwd=cwd)

        self.server.runner = flaky
        text, is_error = self.server.dispatch_tool(
            "vp_navigate", {"identity": "caps4-1", "url": "https://example.org/a"})
        payload = json.loads(text)
        self.assertTrue(is_error)
        self.assertIn("INPUT_UNCERTAIN", payload["error"])
        self.assertIn("propose: subprocess timeout", payload["error"])
        self.assertEqual(payload.get("failed_step"), "type-url")
        # First admitted id plus the current timed-out id.
        self.assertEqual(len(payload["completed_proposal_ids"]), 2)
        self.assertEqual(payload["completed_proposal_ids"][0], self.runner.proposals[0]["proposal_id"])
        # The timed-out proposal's id is the captured one.
        self.assertEqual(len(captured_ids), 1)
        self.assertEqual(payload["completed_proposal_ids"][1], captured_ids[0])

    def test_navigate_propose_error_after_admission_is_input_uncertain(self):
        # A plain propose refusal (e.g. non-STALE) after admission must be
        # promoted to INPUT_UNCERTAIN with prior ids retained.
        self.runner.propose_fail_code = "SOME_OTHER_CODE"
        self.runner.propose_fail_times = 2  # fail on 3rd propose overall (after 2 admitted)
        # Fail only after first admission: allow first propose (ctrl+l) through,
        # fail the second (type-url) by setting propose_fail_times=1? Use custom.
        # Simpler: fail second propose only via FailNthProposeRunner below.
        class FailSecondPropose(FakeRunner):
            def _mvctl(self, argv):
                idx = argv.index("metaviewer_agents.mvctl")
                if argv[idx + 1] == "propose" and len(self.proposals) == 1:
                    path = argv[argv.index("--json-arg") + 1]
                    import json as _json
                    with open(path, "r", encoding="utf-8") as fh:
                        proposal = _json.load(fh)
                    self.proposals.append(proposal)
                    return 1, _json.dumps({"verdict": "refuse", "reason_code": "BUDGET_EXCEEDED"}), ""
                return super()._mvctl(argv)

        runner = FailSecondPropose(report_path=self.report_path)
        server = vp_mcp.ViewpointServer(self.session, runner=runner, sleep=lambda s: None)
        text, is_error = server.dispatch_tool(
            "vp_navigate", {"identity": "caps4-1", "url": "https://example.org/a"})
        payload = json.loads(text)
        self.assertTrue(is_error)
        self.assertIn("INPUT_UNCERTAIN", payload["error"])
        self.assertIn("BUDGET_EXCEEDED", payload["error"])
        self.assertEqual(payload.get("failed_step"), "type-url")
        self.assertEqual(len(payload["completed_proposal_ids"]), 1)
        self.assertEqual(payload["completed_proposal_ids"][0], runner.proposals[0]["proposal_id"])

    def test_navigate_observe_failure_after_admission_is_input_uncertain(self):
        # Observe failure after a prior admitted step must be INPUT_UNCERTAIN.
        class FlakyObserve(FakeRunner):
            def __init__(self, *a, **kw):
                super().__init__(*a, **kw)
                self._obs_calls = 0

            def _mvctl(self, argv):
                idx = argv.index("metaviewer_agents.mvctl")
                if argv[idx + 1] == "observe":
                    self._obs_calls += 1
                    if self._obs_calls == 2:
                        return 1, json.dumps({"verdict": "refuse", "reason_code": "OBSERVE_BROKEN"}), ""
                return super()._mvctl(argv)

        runner = FlakyObserve(report_path=self.report_path)
        server = vp_mcp.ViewpointServer(self.session, runner=runner, sleep=lambda s: None)
        text, is_error = server.dispatch_tool(
            "vp_navigate", {"identity": "caps4-1", "url": "https://example.org/a"})
        payload = json.loads(text)
        self.assertTrue(is_error)
        self.assertIn("INPUT_UNCERTAIN", payload["error"])
        self.assertEqual(payload.get("failed_step"), "type-url")
        self.assertEqual(len(payload["completed_proposal_ids"]), 1)

    def test_navigate_final_ax_failure_after_admission_is_input_uncertain(self):
        # Final ax failure after all 3 steps admitted must be INPUT_UNCERTAIN
        # with all 3 completed ids retained.
        class FailingFinalAx(FakeRunner):
            def __init__(self, *a, **kw):
                super().__init__(*a, **kw)
                self._ax_n = 0

            def __call__(self, argv, timeout, cwd=None):
                # Detect ax call (after stripping /bin/bash + launcher the
                # verb is argv[1] in the FakeRunner view, but here argv is raw).
                # Match on the literal "ax" token in argv.
                if "ax" in argv:
                    self._ax_n += 1
                    if self._ax_n >= 2:
                        return 1, "ax boom", "ax boom"
                return super().__call__(argv, timeout, cwd=cwd)

        runner = FailingFinalAx(report_path=self.report_path)
        server = vp_mcp.ViewpointServer(self.session, runner=runner, sleep=lambda s: None)
        text, is_error = server.dispatch_tool(
            "vp_navigate", {"identity": "caps4-1", "url": "https://example.org/a"})
        payload = json.loads(text)
        self.assertTrue(is_error)
        self.assertIn("INPUT_UNCERTAIN", payload["error"])
        self.assertIn("cannot observe final URL", payload["error"])
        self.assertIn("ax boom", payload["error"])  # BH03: cause kept
        self.assertEqual(runner._ax_n, 1 + vp_mcp._FINAL_AX_ATTEMPTS)
        self.assertEqual(payload.get("failed_step"), "Return")
        self.assertEqual(len(payload["completed_proposal_ids"]), 3)
        self.assertEqual(len(payload["proposal_ids"]), 3)
        self.assertEqual(payload["completed_proposal_ids"], payload["proposal_ids"])

    def test_navigate_final_ax_transient_failure_recovers(self):
        # BH03: one refused final read during page load is retried (read only).
        class OnceFailingFinalAx(FakeRunner):
            def __init__(self, *a, **kw):
                super().__init__(*a, **kw)
                self._ax_n = 0

            def __call__(self, argv, timeout, cwd=None):
                if "ax" in argv:
                    self._ax_n += 1
                    if self._ax_n == 2:
                        return 4, '{"error":{"code":4,"detail":"AX shim unavailable"}}', ""
                return super().__call__(argv, timeout, cwd=cwd)

        runner = OnceFailingFinalAx(report_path=self.report_path)
        sleeps = []
        server = vp_mcp.ViewpointServer(self.session, runner=runner, sleep=sleeps.append)
        text, is_error = server.dispatch_tool(
            "vp_navigate", {"identity": "caps4-1", "url": "https://example.org/a"})
        self.assertFalse(is_error, text)
        self.assertEqual(runner._ax_n, 3)
        self.assertIn(vp_mcp._FINAL_AX_BACKOFFS_S[0], sleeps)

    def _final_ax_runner(self, fail_reads, rc=4):
        # Fails the first `fail_reads` FINAL reads (ax call 1 is the pre-nav
        # blank check) with the given srf rc, then answers normally.
        class Runner(FakeRunner):
            def __init__(self, *a, **kw):
                super().__init__(*a, **kw)
                self._ax_n = 0
                self.proposals_at_final = None

            def __call__(self, argv, timeout, cwd=None):
                if "ax" in argv:
                    self._ax_n += 1
                    if self._ax_n == 2:
                        self.proposals_at_final = len(self.proposals)
                    if 2 <= self._ax_n < 2 + fail_reads:
                        return rc, '{"error":{"code":%d,"detail":"AX shim refused or unavailable"},"result":null}' % rc, ""
                return super().__call__(argv, timeout, cwd=cwd)
        return Runner(report_path=self.report_path)

    def test_navigate_final_ax_slow_page_recovers_on_later_read(self):
        # IMP-FAX: three refused reads recover on the fourth, read only, after
        # 2+4+6 s of settling (BH03 gave up after 2+2+2 s): no extra input.
        runner = self._final_ax_runner(fail_reads=3)
        sleeps = []
        server = vp_mcp.ViewpointServer(self.session, runner=runner, sleep=sleeps.append)
        text, is_error = server.dispatch_tool(
            "vp_navigate", {"identity": "caps4-1", "url": "https://example.org/a"})
        self.assertFalse(is_error, text)
        self.assertEqual(runner._ax_n, 1 + 4)
        self.assertEqual(sleeps[-3:], [2.0, 4.0, 6.0])
        self.assertEqual(len(runner.proposals), runner.proposals_at_final)
        self.assertEqual(json.loads(text)["observed_url"], "https://example.org/a")

    def test_navigate_final_ax_gives_up_within_budget(self):
        # Never more than _FINAL_AX_ATTEMPTS reads, never past the budget,
        # never any input after Return; still INPUT_UNCERTAIN with the cause.
        runner = self._final_ax_runner(fail_reads=99)
        now = [0.0]
        def sleep(s):
            now[0] += s
        server = vp_mcp.ViewpointServer(self.session, runner=runner, sleep=sleep,
                                        clock=lambda: now[0])
        text, is_error = server.dispatch_tool(
            "vp_navigate", {"identity": "caps4-1", "url": "https://example.org/a"})
        payload = json.loads(text)
        self.assertTrue(is_error)
        self.assertIn("INPUT_UNCERTAIN: cannot observe final URL: 4 reads in 12 s: rc=4", payload["error"])
        self.assertEqual(runner._ax_n, 1 + vp_mcp._FINAL_AX_ATTEMPTS)
        # Sol IMP-FAX: never more final reads (shim expired ids) than BH03's 4.
        self.assertEqual(vp_mcp._FINAL_AX_ATTEMPTS, 4)
        self.assertLessEqual(now[0], vp_mcp._FINAL_AX_BUDGET_S)
        self.assertEqual(len(runner.proposals), runner.proposals_at_final)
        self.assertEqual(payload["failed_step"], "Return")

    def test_navigate_final_ax_budget_cuts_spacing(self):
        # Slow reads (5 s each) stop when the next pause would end past the
        # budget; worst case overruns the budget by at most one read.
        runner = self._final_ax_runner(fail_reads=99)
        now = [0.0]
        def sleep(s):
            now[0] += s
        base_call = type(runner).__call__
        def slow_call(argv, timeout, cwd=None):
            if "ax" in argv:
                now[0] += 5.0
            return base_call(runner, argv, timeout, cwd=cwd)
        server = vp_mcp.ViewpointServer(self.session, runner=slow_call, sleep=sleep,
                                        clock=lambda: now[0])
        text, is_error = server.dispatch_tool(
            "vp_navigate", {"identity": "caps4-1", "url": "https://example.org/a"})
        payload = json.loads(text)
        self.assertTrue(is_error)
        # final reads end at 5, 12, 21 (relative); pause 6 would end at 27 > 24
        self.assertIn("3 reads in 21 s", payload["error"])
        self.assertEqual(runner._ax_n, 1 + 3)

    def test_navigate_final_ax_oversleep_starts_no_read_past_budget(self):
        # Sol IMP-FAX: a pause that overruns (loaded host) must not be
        # followed by another read once the budget has passed.
        runner = self._final_ax_runner(fail_reads=99)
        now = [0.0]
        def sleep(s):
            now[0] += s + 30.0  # the first 2 s pause actually takes 32 s
        server = vp_mcp.ViewpointServer(self.session, runner=runner, sleep=sleep,
                                        clock=lambda: now[0])
        text, is_error = server.dispatch_tool(
            "vp_navigate", {"identity": "caps4-1", "url": "https://example.org/a"})
        payload = json.loads(text)
        self.assertTrue(is_error)
        self.assertIn("1 reads in 32 s", payload["error"])
        self.assertEqual(runner._ax_n, 1 + 1)

    def test_navigate_final_ax_dead_shim_not_reread(self):
        # rc 5: the shim failed and exits; rereading cannot help.
        runner = self._final_ax_runner(fail_reads=99, rc=5)
        sleeps = []
        server = vp_mcp.ViewpointServer(self.session, runner=runner, sleep=sleeps.append)
        text, is_error = server.dispatch_tool(
            "vp_navigate", {"identity": "caps4-1", "url": "https://example.org/a"})
        payload = json.loads(text)
        self.assertTrue(is_error)
        self.assertIn("1 reads in", payload["error"])
        self.assertIn("rc=5", payload["error"])
        self.assertEqual(runner._ax_n, 2)

    def test_navigate_ctrl_l_verification_failed_stays_input_uncertain(self):
        # Conductor decision: no ctrl+l toleration. A VERIFICATION_FAILED on
        # ctrl+l stays a fatal INPUT_UNCERTAIN and is not retried.
        runner = FailNthWaitRunner(self.report_path, fail_at=0, code="VERIFICATION_FAILED")
        server = vp_mcp.ViewpointServer(self.session, runner=runner, sleep=lambda s: None)
        text, is_error = server.dispatch_tool(
            "vp_navigate", {"identity": "caps4-1", "url": "https://example.org/a"})
        payload = json.loads(text)
        self.assertTrue(is_error)
        self.assertIn("INPUT_UNCERTAIN", payload["error"])
        self.assertIn("VERIFICATION_FAILED", payload["error"])
        self.assertEqual(payload.get("failed_step"), "ctrl+l")
        self.assertEqual(len(runner.proposals), 1)
        self.assertEqual(len(payload["completed_proposal_ids"]), 1)
        # No retry of ctrl+l: only one propose attempt, no tolerated list.
        self.assertNotIn("tolerated_verification_failures", payload)
        self.assertEqual(payload["completed_proposal_ids"][0], runner.proposals[0]["proposal_id"])

    def test_urls_match_direct(self):
        cases = [
            ("https://example.org/a", "https://example.org/a", True),
            ("https://example.org/a", "https://EXAMPLE.ORG/a", True),
            ("https://example.org/a", "https://example.org/a/", True),
            ("https://example.org/a/", "https://example.org/a", True),
            ("https://example.org/a?x=1", "https://example.org/a?x=1", True),
            ("https://example.org/a?x=1", "https://example.org/a?x=2", False),
            ("https://example.org/a", "https://example.org/b", False),
            ("https://hn.algolia.com/", "https://hn.algolia.com/https://hn.algolia.com/", False),
            ("https://example.org/a", "", False),
            ("https://example.org/a", "about:blank", False),
        ]
        for requested, observed, expected in cases:
            with self.subTest(requested=requested, observed=observed):
                self.assertEqual(vp_mcp._urls_match(requested, observed), expected)

    def test_browser_task_embedded_url_refused(self):
        for text in ("file:///etc/hostname", "http://example.com/a",
                     "https://127.0.0.1/a", "https://2130706433/"):
            with self.subTest(text=text):
                resp = self.call_tool(
                    "vp_browser_task",
                    {"identity": "caps4-1", "goal": "open", "text": text, "key": "Enter"},
                )
                result, payload = self.tool_payload(resp)
                self.assertTrue(result["isError"])
                self.assertIn("embedded URL", payload["error"])
                self.assertEqual(len(self.runner.calls), 0)

    def test_navigate_evil_about_blank_title_does_not_skip(self):
        self.runner.ax_title = "about:blank evil page"
        self.runner.ax_url = "https://evil.example/a"
        self.runner.ax_url_final = "https://example.org/a"
        resp = self.call_tool("vp_navigate", {"identity": "caps4-1", "url": "https://example.org/a"})
        _, payload = self.tool_payload(resp)
        # Even with a spoofed title, ctrl+l must run (3 steps, not 2).
        actions = [p["action"] for p in self.runner.proposals]
        self.assertEqual(actions[0], {"type": "key", "keys": ["ctrl", "l"]})
        self.assertEqual(len(payload["proposal_ids"]), 3)

    def test_navigate_reports_agent_busy_when_lane_held(self):
        previous = vp_mcp.BROWSER_LOCK_WAIT_S
        vp_mcp.BROWSER_LOCK_WAIT_S = 0.05
        try:
            with vp_mcp.browser_lane_lock(self.session["xdg_state"]):
                resp = self.call_tool(
                    "vp_navigate", {"identity": "caps4-1", "url": "https://example.org/a"})
                result, payload = self.tool_payload(resp)
                self.assertTrue(result["isError"])
                self.assertIn("agent busy", payload["error"])
        finally:
            vp_mcp.BROWSER_LOCK_WAIT_S = previous

    def test_read_page_reports_truncation_and_omitted_roles(self):
        self.runner.ax_truncated = True
        self.runner.ax_skipped_frames = 2
        resp = self.call_tool("vp_read_page", {"identity": "caps4-1", "max_chars": 2000})
        _, payload = self.tool_payload(resp)
        self.assertTrue(payload["truncated"])
        self.assertTrue(payload["ax_truncated"])
        self.assertEqual(payload["frame_seq"], 1)
        self.assertEqual(payload["skipped_frames"], 2)
        self.assertEqual(payload["omitted_roles"].get("textbox"), 1)
        self.assertEqual(payload["omitted_roles"].get("button"), 1)

    def test_browser_lock_serialises_concurrent_tasks(self):
        self.runner.mw_delay_s = 0.25
        results = {}

        def run_one(name, goal):
            results[name] = self.server.dispatch_tool(
                "vp_browser_task", {"identity": "caps4-1", "goal": goal}
            )

        threads = [
            threading.Thread(target=run_one, args=("a", "Task A")),
            threading.Thread(target=run_one, args=("b", "Task B")),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)
        for thread in threads:
            self.assertFalse(thread.is_alive())
        for name in ("a", "b"):
            _, payload = results[name][0], json.loads(results[name][0])
            # dispatch_tool returns (text, is_error); check success.
        for text, is_error in results.values():
            self.assertFalse(is_error)
        self.assertEqual(len(self.runner.mw_intervals), 2)
        intervals = sorted(self.runner.mw_intervals)
        self.assertGreaterEqual(intervals[1][0], intervals[0][1])

    def test_browser_lock_reports_agent_busy(self):
        previous = vp_mcp.BROWSER_LOCK_WAIT_S
        vp_mcp.BROWSER_LOCK_WAIT_S = 0.15
        try:
            with vp_mcp.browser_lane_lock(self.session["xdg_state"]):
                resp = self.call_tool("vp_browser_task", {"identity": "caps4-1", "goal": "hi"})
                result, payload = self.tool_payload(resp)
                self.assertTrue(result["isError"])
                self.assertIn("agent busy", payload["error"])
        finally:
            vp_mcp.BROWSER_LOCK_WAIT_S = previous
        # After release the lane is usable again.
        resp = self.call_tool("vp_browser_task", {"identity": "caps4-1", "goal": "hi"})
        _, payload = self.tool_payload(resp)
        self.assertEqual(payload["rc"], 0)

    def test_url_refusals(self):
        bad = [
            "http://example.com/a",
            "file:///etc/passwd",
            "https://127.0.0.1/admin",
            "https://169.254.169.254/latest/meta-data",
            "https://10.0.0.1/a",
            "https://192.168.1.1/a",
            "https://172.16.0.1/a",
            "https://localhost/a",
            "ftp://example.com/a",
        ]
        for url in bad:
            with self.subTest(url=url):
                resp = self.call_tool("vp_navigate", {"identity": "caps4-1", "url": url})
                result, payload = self.tool_payload(resp)
                self.assertTrue(result["isError"])
                self.assertIn("error", payload)
                self.assertEqual(len(self.runner.calls), 0)

    def test_url_happy_public(self):
        for url in ("https://example.com/a", "https://en.wikipedia.org/wiki/AI_agent"):
            with self.subTest(url=url):
                resp = self.call_tool("vp_navigate", {"identity": "caps4-1", "url": url})
                _, payload = self.tool_payload(resp)
                self.assertEqual(payload["url"], url)

    def test_unknown_identity(self):
        cases = [
            ("vp_navigate", {"identity": "nope", "url": "https://example.com/"}),
            ("vp_browser_task", {"identity": "nope", "goal": "hi"}),
            ("vp_read_page", {"identity": "nope"}),
        ]
        for name, args in cases:
            with self.subTest(name=name):
                resp = self.call_tool(name, args)
                result, payload = self.tool_payload(resp)
                self.assertTrue(result["isError"])
                self.assertIn("unknown identity", payload["error"])

    def test_timeout_handling(self):
        self.runner.timeout_mode = True
        resp = self.call_tool("vp_read_page", {"identity": "caps4-1"})
        result, payload = self.tool_payload(resp)
        self.assertTrue(result["isError"])
        self.assertIn("timeout", payload["error"].lower())

    def test_timeout_expired(self):
        server = vp_mcp.ViewpointServer(self.session, runner=TimeoutExpiredRunner(), sleep=lambda s: None)
        line = server.handle_line(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": "vp_browser_task", "arguments": {"identity": "caps4-1", "goal": "hi"}},
                }
            )
        )
        resp = json.loads(line)
        _, payload = (resp["result"], json.loads(resp["result"]["content"][0]["text"]))
        self.assertIn("timeout", payload["error"].lower())

    def test_oversized_inputs(self):
        cases = [
            ("vp_browser_task", {"identity": "caps4-1", "goal": "g" * 501}),
            ("vp_browser_task", {"identity": "caps4-1", "goal": "ok", "text": "t" * 4001}),
            ("vp_browser_task", {"identity": "caps4-1", "goal": "ok", "key": "Escape"}),
            ("vp_browser_task", {"identity": "caps4-1", "goal": ""}),
            ("vp_read_page", {"identity": "caps4-1", "max_chars": 6001}),
            ("vp_read_page", {"identity": "caps4-1", "max_chars": 0}),
            ("vp_navigate", {"identity": "caps4-1", "url": "https://" + "a" * 2100}),
        ]
        for name, args in cases:
            with self.subTest(name=name, args=str(args)[:60]):
                resp = self.call_tool(name, args)
                result, payload = self.tool_payload(resp)
                self.assertTrue(result["isError"])
                self.assertIn("error", payload)
                self.assertEqual(len(self.runner.calls), 0)

    # -- session / attach validation ------------------------------------------

    def test_session_refusal_relative(self):
        with self.assertRaises(ValueError):
            vp_mcp.load_session_file("relative/session.json")

    def test_session_refusal_missing(self):
        with self.assertRaises(ValueError):
            vp_mcp.load_session_file(self.tmp.name + "/missing.json")

    def test_session_requires_attach_env(self):
        data = dict(self.session)
        data.pop("attach_env")
        with self.assertRaises(ValueError) as ctx:
            vp_mcp.validate_session(data)
        self.assertIn("attach_env", str(ctx.exception))

    def test_session_rejects_relative_attach_env(self):
        data = dict(self.session)
        data["attach_env"] = "relative/current.env"
        with self.assertRaises(ValueError):
            vp_mcp.validate_session(data)

    def test_session_rejects_missing_attach_file(self):
        data = dict(self.session)
        data["attach_env"] = self.tmp.name + "/absent.env"
        with self.assertRaises(ValueError):
            vp_mcp.validate_session(data)

    def test_session_rejects_invalid_surface_id(self):
        data = json.loads(json.dumps(self.session))
        data["capsules"][0]["surface_id"] = "bad/surface"
        with self.assertRaises(ValueError):
            vp_mcp.validate_session(data)

    def test_parse_attach_text_variants(self):
        values = vp_mcp.parse_attach_text(
            "export MV_CORE_CONFIG='/a/config.json'\n"
            'export XDG_RUNTIME_DIR="/run/user/1000"\n'
            "MV_AGENT_INSTANCE=agent-1\n"
            "# comment\n\n"
        )
        self.assertEqual(values["MV_CORE_CONFIG"], "/a/config.json")
        self.assertEqual(values["XDG_RUNTIME_DIR"], "/run/user/1000")
        self.assertEqual(values["MV_AGENT_INSTANCE"], "agent-1")

    def test_resolve_core_context(self):
        ctx = vp_mcp.resolve_core_context(self.session)
        self.assertTrue(ctx["config_path"].endswith("core-config.json"))
        self.assertTrue(ctx["socket"].endswith("/metaviewer/core.sock"))
        self.assertEqual(ctx["session_id"], "test-session")
        self.assertEqual(ctx["agent_instance"], "agent-1")
        self.assertEqual(ctx["core_instance"], "core-1")

    def test_validate_url_direct(self):
        url, reason = vp_mcp.validate_url("https://example.com/a")
        self.assertEqual(url, "https://example.com/a")
        self.assertIsNone(reason)
        for bad in ("http://example.com", "https://127.0.0.1", "https://localhost"):
            _, reason = vp_mcp.validate_url(bad)
            self.assertIsNotNone(reason)

    def test_validate_url_literal_only_documented_limitation(self):
        # F4: DNS-resolved private hosts are out of scope (no DNS).
        url, reason = vp_mcp.validate_url("https://127.0.0.1.nip.io/")
        self.assertEqual(url, "https://127.0.0.1.nip.io/")
        self.assertIsNone(reason)
        for bad in ("https://127.0.0.1/", "https://169.254.169.254/"):
            _, reason = vp_mcp.validate_url(bad)
            self.assertIsNotNone(reason)

    def test_is_on_blank_window_requires_all_three(self):
        # Genuine blank: empty url + exact title + no ax elements.
        self.assertTrue(vp_mcp._is_on_blank_window(
            {"title": "about:blank - Chromium", "url": ""}, []))
        # Spoof via page title: matching title text but a live URL.
        self.assertFalse(vp_mcp._is_on_blank_window(
            {"title": "about:blank - Chromium", "url": "https://evil.example/"}, []))
        # Exact title + empty URL but the page has elements: not blank.
        self.assertFalse(vp_mcp._is_on_blank_window(
            {"title": "about:blank - Chromium", "url": ""}, [{"name": "x"}]))
        # Prefix-only titles never count.
        self.assertFalse(vp_mcp._is_on_blank_window(
            {"title": "about:blank evil page", "url": ""}, []))

    def test_navigate_spoofed_exact_title_does_not_skip(self):
        # F2: document.title = "about:blank" yields the exact Chromium
        # title but a live URL; ctrl+l must still run.
        self.runner.ax_title = "about:blank - Chromium"
        self.runner.ax_url = "https://evil.example/a"
        self.runner.ax_url_final = "https://example.org/a"
        resp = self.call_tool("vp_navigate", {"identity": "caps4-1", "url": "https://example.org/a"})
        _, payload = self.tool_payload(resp)
        actions = [p["action"] for p in self.runner.proposals]
        self.assertEqual(actions[0], {"type": "key", "keys": ["ctrl", "l"]})
        self.assertEqual(len(payload["proposal_ids"]), 3)

    def test_browser_task_schemeless_host_refused(self):
        # F3: scheme-less embedded hosts must not bypass the URL gate.
        for text in ("169.254.169.254", "open 10.0.0.1/admin", "localhost:8080",
                     "192.168.0.1", "nas.internal/db"):
            with self.subTest(text=text):
                resp = self.call_tool(
                    "vp_browser_task",
                    {"identity": "caps4-1", "goal": "open", "text": text, "key": "Enter"},
                )
                result, payload = self.tool_payload(resp)
                self.assertTrue(result["isError"])
                self.assertIn("embedded URL", payload["error"])
                self.assertEqual(len(self.runner.calls), 0)

    def test_browser_task_human_handoff_not_masked(self):
        # F5: a HUMAN_HANDOFF stop must surface as HUMAN_HANDOFF.
        self.runner.mw_rc = 4
        self.runner.mw_verdict = "HUMAN_HANDOFF"
        resp = self.call_tool("vp_browser_task", {"identity": "caps4-1", "goal": "hi"})
        result, payload = self.tool_payload(resp)
        self.assertTrue(result["isError"])
        self.assertTrue(payload["error"].startswith("HUMAN_HANDOFF"), payload["error"])
        self.assertEqual(payload["verdict_code"], "HUMAN_HANDOFF")

    def test_navigate_wait_stale_frame_is_input_uncertain(self):
        # F6a: wait-verify failure after admission always carries the marker.
        self.runner.wait_fail_mode = "stale"
        resp = self.call_tool("vp_navigate", {"identity": "caps4-1", "url": "https://example.org/a"})
        result, payload = self.tool_payload(resp)
        self.assertTrue(result["isError"])
        self.assertIn("INPUT_UNCERTAIN", payload["error"])
        self.assertIn("STALE_FRAME", payload["error"])
        self.assertEqual(len(payload["completed_proposal_ids"]), 1)

    def test_navigate_nondict_final_ax_is_input_uncertain(self):
        # F6b: a non-dict ax reply after Return must not escape as a
        # generic error without the marker.
        self.runner.ax_raw = '"not-an-object"'
        resp = self.call_tool("vp_navigate", {"identity": "caps4-1", "url": "https://example.org/a"})
        result, payload = self.tool_payload(resp)
        self.assertTrue(result["isError"])
        self.assertIn("INPUT_UNCERTAIN", payload["error"])
        self.assertIn("completed_proposal_ids", payload)

    def test_navigate_ambiguous_initial_malformed_is_input_uncertain(self):
        class MalformedPropose(FakeRunner):
            def _mvctl(self, argv):
                idx = argv.index("metaviewer_agents.mvctl")
                if argv[idx + 1] == "propose":
                    path = argv[argv.index("--json-arg") + 1]
                    with open(path, "r", encoding="utf-8") as fh:
                        proposal = json.load(fh)
                    self.proposals.append(proposal)
                    return 1, "not json {", "boom"
                return super()._mvctl(argv)

        runner = MalformedPropose(report_path=self.report_path)
        server = vp_mcp.ViewpointServer(self.session, runner=runner, sleep=lambda s: None)
        text, is_error = server.dispatch_tool(
            "vp_navigate", {"identity": "caps4-1", "url": "https://example.org/a"})
        payload = json.loads(text)
        self.assertTrue(is_error, payload)
        self.assertIn("INPUT_UNCERTAIN", payload["error"])
        self.assertEqual(payload.get("failed_step"), "ctrl+l")
        self.assertEqual(len(payload["completed_proposal_ids"]), 1)
        self.assertEqual(
            payload["completed_proposal_ids"][0],
            runner.proposals[0]["proposal_id"])
        self.assertEqual(len(runner.proposals), 1)

    def test_navigate_ambiguous_later_includes_all_ids(self):
        class FailSecondMalformed(FakeRunner):
            def _mvctl(self, argv):
                idx = argv.index("metaviewer_agents.mvctl")
                if argv[idx + 1] == "propose" and len(self.proposals) == 1:
                    path = argv[argv.index("--json-arg") + 1]
                    with open(path, "r", encoding="utf-8") as fh:
                        proposal = json.load(fh)
                    self.proposals.append(proposal)
                    return 1, "not json {", "boom"
                return super()._mvctl(argv)

        runner = FailSecondMalformed(report_path=self.report_path)
        server = vp_mcp.ViewpointServer(self.session, runner=runner, sleep=lambda s: None)
        text, is_error = server.dispatch_tool(
            "vp_navigate", {"identity": "caps4-1", "url": "https://example.org/a"})
        payload = json.loads(text)
        self.assertTrue(is_error, payload)
        self.assertIn("INPUT_UNCERTAIN", payload["error"])
        self.assertEqual(payload.get("failed_step"), "type-url")
        self.assertEqual(len(payload["completed_proposal_ids"]), 2)
        self.assertEqual(
            payload["completed_proposal_ids"][0],
            runner.proposals[0]["proposal_id"])
        self.assertEqual(
            payload["completed_proposal_ids"][1],
            runner.proposals[1]["proposal_id"])
        self.assertEqual(len(runner.proposals), 2)

    def test_navigate_uncorrelated_verdict_is_ambiguous(self):
        class UncorrelatedPropose(FakeRunner):
            def _mvctl(self, argv):
                idx = argv.index("metaviewer_agents.mvctl")
                if argv[idx + 1] == "propose":
                    path = argv[argv.index("--json-arg") + 1]
                    with open(path, "r", encoding="utf-8") as fh:
                        proposal = json.load(fh)
                    self.proposals.append(proposal)
                    reply = {
                        "schema": "action_verdict/v0.2",
                        "proposal_id": "vpnav-other-id",
                        "decision": "refused",
                        "reason_code": "STALE_FRAME",
                    }
                    return 1, json.dumps(reply), ""
                return super()._mvctl(argv)

        runner = UncorrelatedPropose(report_path=self.report_path)
        server = vp_mcp.ViewpointServer(self.session, runner=runner, sleep=lambda s: None)
        text, is_error = server.dispatch_tool(
            "vp_navigate", {"identity": "caps4-1", "url": "https://example.org/a"})
        payload = json.loads(text)
        self.assertTrue(is_error, payload)
        self.assertIn("INPUT_UNCERTAIN", payload["error"])
        self.assertEqual(payload.get("failed_step"), "ctrl+l")
        self.assertEqual(len(payload["completed_proposal_ids"]), 1)
        self.assertEqual(
            payload["completed_proposal_ids"][0],
            runner.proposals[0]["proposal_id"])
        self.assertEqual(len(runner.proposals), 1)

    def test_navigate_ambiguous_text_stale_frame_no_retry(self):
        class StaleTextPropose(FakeRunner):
            def _mvctl(self, argv):
                idx = argv.index("metaviewer_agents.mvctl")
                if argv[idx + 1] == "propose":
                    path = argv[argv.index("--json-arg") + 1]
                    with open(path, "r", encoding="utf-8") as fh:
                        proposal = json.load(fh)
                    self.proposals.append(proposal)
                    return 1, "log says STALE_FRAME happened", ""
                return super()._mvctl(argv)

        runner = StaleTextPropose(report_path=self.report_path)
        server = vp_mcp.ViewpointServer(self.session, runner=runner, sleep=lambda s: None)
        text, is_error = server.dispatch_tool(
            "vp_navigate", {"identity": "caps4-1", "url": "https://example.org/a"})
        payload = json.loads(text)
        self.assertTrue(is_error, payload)
        self.assertIn("INPUT_UNCERTAIN", payload["error"])
        self.assertEqual(len(runner.proposals), 1)
        self.assertEqual(len(payload["completed_proposal_ids"]), 1)

    def test_navigate_confirmed_stale_frame_strict_retries_then_succeeds(self):
        class StrictStaleOnce(FakeRunner):
            def _mvctl(self, argv):
                idx = argv.index("metaviewer_agents.mvctl")
                if argv[idx + 1] == "propose" and len(self.proposals) == 0:
                    path = argv[argv.index("--json-arg") + 1]
                    with open(path, "r", encoding="utf-8") as fh:
                        proposal = json.load(fh)
                    self.proposals.append(proposal)
                    reply = {
                        "schema": "action_verdict/v0.2",
                        "proposal_id": proposal["proposal_id"],
                        "decision": "refused",
                        "reason_code": "STALE_FRAME",
                    }
                    return 1, json.dumps(reply), ""
                return super()._mvctl(argv)

        runner = StrictStaleOnce(report_path=self.report_path)
        server = vp_mcp.ViewpointServer(self.session, runner=runner, sleep=lambda s: None)
        text, is_error = server.dispatch_tool(
            "vp_navigate", {"identity": "caps4-1", "url": "https://example.org/a"})
        payload = json.loads(text)
        self.assertFalse(is_error, payload)
        self.assertEqual(len(payload["proposal_ids"]), 3)
        self.assertEqual(len(runner.proposals), 4)

    def test_navigate_failure_after_mismatch_keeps_both_attempts_ids(self):
        class FailSecondAttempt(FakeRunner):
            def _mvctl(self, argv):
                idx = argv.index("metaviewer_agents.mvctl")
                if argv[idx + 1] == "propose" and len(self.proposals) == 3:
                    path = argv[argv.index("--json-arg") + 1]
                    with open(path, "r", encoding="utf-8") as fh:
                        proposal = json.load(fh)
                    self.proposals.append(proposal)
                    return 1, "not json {", "boom"
                return super()._mvctl(argv)

        runner = FailSecondAttempt(report_path=self.report_path)
        runner.ax_finals = ["https://evil.example/a", "https://example.org/a"]
        server = vp_mcp.ViewpointServer(self.session, runner=runner, sleep=lambda s: None)
        text, is_error = server.dispatch_tool(
            "vp_navigate", {"identity": "caps4-1", "url": "https://example.org/a"})
        payload = json.loads(text)
        self.assertTrue(is_error, payload)
        self.assertIn("INPUT_UNCERTAIN", payload["error"])
        self.assertEqual(payload.get("failed_step"), "ctrl+l")
        self.assertEqual(len(runner.proposals), 4)
        self.assertEqual(len(payload["completed_proposal_ids"]), 4)
        self.assertEqual(
            list(payload["completed_proposal_ids"]),
            [p["proposal_id"] for p in runner.proposals])

    def test_mvctl_propose_classifies_refused_vs_ambiguous(self):
        ctx = {
            "config_path": self.session["attach_env"],
            "xdg_runtime": self.session["xdg_state"],
            "socket": "/tmp/test.sock",
            "session_id": "test-session",
            "agent_instance": "agent-1",
            "core_instance": "core-1",
        }
        proposal = {
            "schema": "action_proposal/v0.2",
            "proposal_id": "pid-1",
            "surface_id": "surface-caps4-1",
        }

        def runner_for(argv_reply):
            rc, reply_text = argv_reply
            def _run(argv, timeout, cwd=None):
                return rc, reply_text, ""
            return _run

        # Confirmed: correlated refused verdict with structured code.
        verdict = json.dumps({
            "schema": "action_verdict/v0.2",
            "proposal_id": "pid-1",
            "decision": "refused",
            "reason_code": "STALE_FRAME",
        })
        server = vp_mcp.ViewpointServer(
            self.session, runner=runner_for((1, verdict)), sleep=lambda s: None)
        with self.assertRaises(vp_mcp.ProposeRefused) as caught:
            server._mvctl_propose(ctx, proposal)
        self.assertEqual(caught.exception.code, "STALE_FRAME")
        # Ambiguous: malformed stdout.
        server2 = vp_mcp.ViewpointServer(
            self.session, runner=runner_for((1, "not json {")),
            sleep=lambda s: None)
        with self.assertRaises(vp_mcp.ProposeAmbiguous):
            server2._mvctl_propose(ctx, proposal)
        # Ambiguous: uncorrelated verdict.
        uncorrelated = json.dumps({
            "schema": "action_verdict/v0.2",
            "proposal_id": "pid-other",
            "decision": "refused",
            "reason_code": "STALE_FRAME",
        })
        server3 = vp_mcp.ViewpointServer(
            self.session, runner=runner_for((1, uncorrelated)),
            sleep=lambda s: None)
        with self.assertRaises(vp_mcp.ProposeAmbiguous):
            server3._mvctl_propose(ctx, proposal)

        # Sol w133 holes: each must be ambiguous (never a retryable refusal).
        holes = [
            (1, {"schema": "action_verdict/v0.2", "proposal_id": "pid-1",
                 "decision": "garbled", "reason_code": "STALE_FRAME"}),
            (0, {"schema": "action_verdict/v0.2", "proposal_id": "pid-1",
                 "decision": "refused", "reason_code": "STALE_FRAME"}),
            (1, {"schema": "action_verdict/v0.2", "proposal_id": "pid-1",
                 "decision": "refused", "error": "STALE_FRAME"}),
            (0, {"verdict": "refuse", "reason_code": "STALE_FRAME"}),
            (1, {"verdict": "refuse", "reason_code": "STALE_FRAME", "extra": 1}),
            (1, {"verdict": "refuse", "reason_code": "stale frame (text)"}),
            (2, {"verdict": "refuse", "reason_code": "STALE_FRAME"}),
            (1, {"verdict": "refuse", "reason_code": "STALE_FRAME",
                 "proposal_id": "pid-other"}),
        ]
        for rc, body in holes:
            with self.subTest(rc=rc, body=body):
                srv = vp_mcp.ViewpointServer(
                    self.session, runner=runner_for((rc, json.dumps(body))),
                    sleep=lambda s: None)
                with self.assertRaises(vp_mcp.ProposeAmbiguous) as amb:
                    srv._mvctl_propose(ctx, proposal)
                self.assertNotIsInstance(amb.exception, vp_mcp.ProposeRefused)
                self.assertIsNone(amb.exception.code)
        # Production refusal shape (mvctl.py:555): rc 1, verdict/reason_code.
        srv = vp_mcp.ViewpointServer(
            self.session,
            runner=runner_for((1, json.dumps(
                {"verdict": "refuse", "reason_code": "STALE_FRAME"}))),
            sleep=lambda s: None)
        with self.assertRaises(vp_mcp.ProposeRefused) as ref:
            srv._mvctl_propose(ctx, proposal)
        self.assertEqual(ref.exception.code, "STALE_FRAME")


if __name__ == "__main__":
    unittest.main()



