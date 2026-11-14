"""IMP-MCPID: identity management tools over MCP (operator ruling 13 Nov 18:35)."""
import json
import os
import sys
import tempfile
import unittest
import unittest.mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import vp_mcp  # noqa: E402
from test_vp_mcp import make_session, REPORT_JSON  # noqa: E402


class IdentityRunner:
    """Records argv; replies like tools/mv_identity.py (one JSON line, rc 0 or 6)."""

    def __init__(self, reply=None, rc=0, raise_timeout=False):
        self.calls = []
        self.reply, self.rc, self.raise_timeout = reply, rc, raise_timeout

    def __call__(self, argv, timeout, cwd=None):
        self.calls.append((list(argv), timeout))
        if self.raise_timeout:
            raise TimeoutError("slow")
        out = self.reply if isinstance(self.reply, str) else json.dumps(self.reply)
        return self.rc, out + "\n", ""


class IdentityToolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        report = os.path.join(self.tmp.name, "report.json")
        with open(report, "w", encoding="utf-8") as fh:
            fh.write(REPORT_JSON)
        self.session = make_session(self.tmp.name, report)

    def tearDown(self):
        self.tmp.cleanup()

    def call(self, name, args, runner):
        server = vp_mcp.ViewpointServer(self.session, runner=runner, sleep=lambda s: None)
        text, is_error = server.dispatch_tool(name, args)
        return json.loads(text), is_error

    def ok_reply(self, result):
        return {"schema": "metaviewer.identity/v1", "ok": True, "result": result}

    def test_tools_listed_with_schemas(self):
        names = [t["name"] for t in vp_mcp.TOOLS]
        self.assertEqual(names[:4], ["vp_list_capsules", "vp_navigate", "vp_browser_task", "vp_read_page"])
        self.assertEqual(names[4:], ["vp_identity_list", "vp_identity_create",
                                     "vp_identity_set_label", "vp_identity_delete", "vp_show", "vp_hide"])
        delete = next(t for t in vp_mcp.TOOLS if t["name"] == "vp_identity_delete")
        self.assertEqual(delete["inputSchema"]["required"], ["id", "confirm"])

    def test_list_runs_mv_identity_through_env_launcher(self):
        runner = IdentityRunner(self.ok_reply([{"profile_id": "wikipedia", "account_label": "Wikipedia"}]))
        payload, is_error = self.call("vp_identity_list", {}, runner)
        self.assertFalse(is_error, payload)
        self.assertEqual(payload["result"][0]["profile_id"], "wikipedia")
        argv, timeout = runner.calls[0]
        self.assertEqual(argv[:4], ["/bin/bash", self.session["env_launcher"], "python3", "-B"])
        self.assertEqual(argv[4], os.path.join(self.session["repo"], "tools", "mv_identity.py"))
        self.assertEqual(argv[5:], ["list"])
        self.assertEqual(timeout, vp_mcp.TIMEOUT_IDENTITY)

    def test_create_and_set_label_argv(self):
        runner = IdentityRunner(self.ok_reply({"profile_id": "arxiv"}))
        _, is_error = self.call("vp_identity_create",
                                {"id": "arxiv", "label": "arXiv", "domains": "arxiv.org"}, runner)
        self.assertFalse(is_error)
        self.assertEqual(runner.calls[0][0][5:], ["create", "--id", "arxiv", "--label=arXiv",
                                                  "--domains", "arxiv.org"])
        _, is_error = self.call("vp_identity_set_label", {"id": "arxiv", "label": "arXiv search"}, runner)
        self.assertFalse(is_error)
        self.assertEqual(runner.calls[1][0][5:], ["label", "--id", "arxiv", "--label=arXiv search"])
        # Sol IMP-MCPID: a label that looks like an option stays one value.
        _, is_error = self.call("vp_identity_set_label", {"id": "arxiv", "label": "--help"}, runner)
        self.assertFalse(is_error)
        self.assertEqual(runner.calls[2][0][5:], ["label", "--id", "arxiv", "--label=--help"])

    def test_invalid_arguments_never_spawn(self):
        cases = [
            ("vp_identity_create", {"id": "../x", "label": "a", "domains": "a.org"}),
            ("vp_identity_create", {"id": "UPPER", "label": "a", "domains": "a.org"}),
            ("vp_identity_create", {"id": "ok-id", "label": " pad", "domains": "a.org"}),
            ("vp_identity_create", {"id": "ok-id", "label": "a\nb", "domains": "a.org"}),
            ("vp_identity_create", {"id": "ok-id", "label": "a", "domains": "a.org; rm -rf /"}),
            ("vp_identity_create", {"id": "ok-id", "label": "a"}),
            ("vp_identity_set_label", {"id": "ok-id", "label": "x" * 129}),
            ("vp_identity_set_label", {"id": "ok-id"}),
            ("vp_identity_delete", {"id": "-bad", "confirm": "-bad"}),
        ]
        for name, args in cases:
            with self.subTest(name=name, args=args):
                runner = IdentityRunner(self.ok_reply({}))
                payload, is_error = self.call(name, args, runner)
                self.assertTrue(is_error, payload)
                self.assertEqual(runner.calls, [])

    def test_delete_requires_confirm_equal_to_id(self):
        for confirm in (None, "", "Wikipedia", "wikipedia ", "arxiv"):
            with self.subTest(confirm=confirm):
                runner = IdentityRunner(self.ok_reply({}))
                args = {"id": "wikipedia"}
                if confirm is not None:
                    args["confirm"] = confirm
                payload, is_error = self.call("vp_identity_delete", args, runner)
                self.assertTrue(is_error)
                self.assertTrue(payload["error"].startswith("CONFIRM_MISMATCH"))
                self.assertEqual(runner.calls, [])
        runner = IdentityRunner(self.ok_reply({"deleted": "wikipedia"}))
        payload, is_error = self.call("vp_identity_delete", {"id": "wikipedia", "confirm": "wikipedia"}, runner)
        self.assertFalse(is_error, payload)
        self.assertEqual(runner.calls[0][0][5:], ["delete", "--id", "wikipedia"])

    def test_tool_refusal_codes_pass_through(self):
        for code in ("IDENTITY_PROTECTED", "RESERVED_ID", "IDENTITY_ACTIVE", "NATIVE_REFUSED"):
            with self.subTest(code=code):
                runner = IdentityRunner({"schema": "metaviewer.identity/v1", "ok": False, "error": code}, rc=6)
                payload, is_error = self.call("vp_identity_delete", {"id": "wikipedia", "confirm": "wikipedia"}, runner)
                self.assertTrue(is_error)
                self.assertEqual(payload, {"error": code, "verb": "delete"})

    def test_garbage_or_timeout_fail_closed(self):
        payload, is_error = self.call("vp_identity_list", {}, IdentityRunner("not json"))
        self.assertTrue(is_error)
        self.assertTrue(payload["error"].startswith("IDENTITY_UNAVAILABLE"))
        payload, is_error = self.call("vp_identity_list", {}, IdentityRunner({"ok": True, "result": []}))
        self.assertTrue(is_error, "a reply without the identity schema is not trusted")
        payload, is_error = self.call("vp_identity_list", {}, IdentityRunner(raise_timeout=True))
        self.assertTrue(is_error)
        self.assertTrue(payload["error"].startswith("IDENTITY_TIMEOUT"))
        payload, is_error = self.call("vp_identity_list", {}, IdentityRunner(self.ok_reply([]), rc=6))
        self.assertTrue(is_error, "rc != 0 is never success")


class TakeViewTests(unittest.TestCase):
    """vp_show / vp_hide: fixed argv to the operator launcher, refused without a take."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        report = os.path.join(self.tmp.name, "report.json")
        with open(report, "w", encoding="utf-8") as fh:
            fh.write(REPORT_JSON)
        self.session = make_session(self.tmp.name, report)
        base = os.path.dirname(self.session["env_launcher"])
        self.pidfile = os.path.join(base, "app.pid")
        self.launcher = os.path.join(self.tmp.name, "vp_demo_take.sh")
        with open(self.launcher, "w") as fh:
            fh.write("#!/bin/bash\necho launcher\n")
        self.display = os.path.join(self.tmp.name, "X98")
        self.env = unittest.mock.patch.dict(os.environ, VP_TAKE_LAUNCHER=self.launcher)
        self.env.start()
        self.disp = unittest.mock.patch.object(vp_mcp, "TAKE_DISPLAY_SOCKET", self.display)
        self.disp.start()

    def tearDown(self):
        self.disp.stop()
        self.env.stop()
        self.tmp.cleanup()

    def running(self, alive=True, display=True, born=None):
        with open(f"/proc/{os.getpid()}/stat") as fh:
            mine = fh.read().rsplit(")", 1)[1].split()[19]
        with open(self.pidfile, "w") as fh:
            fh.write(f"{os.getpid() if alive else 2 ** 22 + 12345} {born or mine}\n")
        if display:
            open(self.display, "w").close()

    def call(self, name, runner):
        server = vp_mcp.ViewpointServer(self.session, runner=runner, sleep=lambda s: None)
        text, is_error = server.dispatch_tool(name, {})
        return json.loads(text), is_error

    def test_show_and_hide_fixed_argv(self):
        self.running()
        for name, verb in (("vp_show", "show"), ("vp_hide", "hide")):
            with self.subTest(name=name):
                runner = IdentityRunner("program shown on the desktop")
                payload, is_error = self.call(name, runner)
                self.assertFalse(is_error, payload)
                self.assertEqual(payload["action"], verb)
                self.assertEqual(runner.calls[0][0], ["/bin/bash", os.path.realpath(self.launcher), verb])
                self.assertEqual(runner.calls[0][1], vp_mcp.TIMEOUT_TAKE_VIEW)

    def test_refused_without_running_take(self):
        for alive, display in ((False, True), (True, False)):
            with self.subTest(alive=alive, display=display):
                for f in (self.pidfile, self.display):
                    if os.path.exists(f):
                        os.remove(f)
                self.running(alive, display)
                runner = IdentityRunner("x")
                payload, is_error = self.call("vp_show", runner)
                self.assertTrue(is_error)
                self.assertTrue(payload["error"].startswith("TAKE_NOT_RUNNING"))
                self.assertEqual(runner.calls, [])
        # Sol: a live pid with another start time is a reused pid, not the app.
        for f in (self.pidfile, self.display):
            if os.path.exists(f):
                os.remove(f)
        self.running(born="1")
        runner = IdentityRunner("x")
        payload, is_error = self.call("vp_show", runner)
        self.assertTrue(payload["error"].startswith("TAKE_NOT_RUNNING"))
        self.assertEqual(runner.calls, [])
        for bad in ("0 1\n", "1 1\n", "abc\n", "\n", f"{os.getpid()}\n"):
            with open(self.pidfile, "w") as fh:
                fh.write(bad)
            self.assertTrue(self.call("vp_hide", IdentityRunner("x"))[1], bad)
        os.remove(self.pidfile)
        payload, is_error = self.call("vp_hide", IdentityRunner("x"))
        self.assertTrue(is_error)

    def test_missing_launcher_and_failure(self):
        self.running()
        os.remove(self.launcher)
        payload, is_error = self.call("vp_show", IdentityRunner("x"))
        self.assertEqual(payload["error"], "TAKE_LAUNCHER_ABSENT")
        with open(self.launcher, "w") as fh:
            fh.write("#!/bin/bash\n")
        payload, is_error = self.call("vp_show", IdentityRunner("no take running: run start first", rc=1))
        self.assertTrue(is_error)
        self.assertTrue(payload["error"].startswith("TAKE_VIEW_FAILED"))
        payload, is_error = self.call("vp_show", IdentityRunner(raise_timeout=True))
        self.assertTrue(payload["error"].startswith("TAKE_VIEW_TIMEOUT"))


if __name__ == "__main__":
    unittest.main()
