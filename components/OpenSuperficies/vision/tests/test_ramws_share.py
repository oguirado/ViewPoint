"""Lane U: ramws_shim `session --share-uid` (S-LIVE-1A v2). The pinned law2 ramws has no share kw; law3 adds
open_run(share_with_uid=...). The shim feature-detects it and fails closed (rc 5 E_RAM_SHARE_UNSUPPORTED), never chmod."""
import ast
import functools
import io
import json
import os
import unittest
from contextlib import redirect_stdout
from unittest import mock

from vision.tests.helpers import REPO


def _load_shim():
    from vision.tests.test_wheel_fixture import ensure_real_wheel
    ensure_real_wheel()
    import importlib
    return importlib.import_module("vision.ramws_shim")


class FakeWs:
    root = "/dev/shm/vqa-ramws-test/fake"
    _fstype = "tmpfs"
    quota = 1 << 20

    def close(self):
        pass

    def receipt(self):
        return {"run_id": "r", "peak": 0}


def _run_session(shim, open_run, argv_opt, stdin_lines):
    calls = []

    @functools.wraps(open_run)  # keeps the signature visible to the shim's feature detection
    def wrapped(*a, **kw):
        calls.append((a, kw))
        return open_run(*a, **kw)

    buf = io.StringIO()
    with mock.patch.object(shim.ramws, "open_run", wrapped), mock.patch("sys.stdin", io.StringIO("".join(stdin_lines))), \
            redirect_stdout(buf):
        try:
            rc = shim.main(["session"] + argv_opt)
        except SystemExit as e:  # pragma: no cover
            rc = e.code
    return rc, [json.loads(x) for x in buf.getvalue().splitlines()], calls


def _share_open_run(campaign, rid, quota, *, tool, root=None, share_with_uid=None):
    return FakeWs()


def _plain_open_run(campaign, rid, quota, *, tool, root=None):
    return FakeWs()


class ShareTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.shim = _load_shim()

    def test_session_share_uid_passed_to_open_run(self):
        rc, lines, calls = _run_session(self.shim, _share_open_run, ["--run-id", "u1", "--share-uid", "10001"],
                                        ['{"cmd":"close"}\n'])
        self.assertEqual(rc, 0, lines)
        self.assertEqual(calls[0][1].get("share_with_uid"), 10001)
        self.assertEqual(lines[0]["share"], {"uid": 10001, "via": "open_run.share_with_uid"})

    def test_session_share_unsupported_rc5_no_chmod(self):
        with mock.patch("os.chmod") as chm, mock.patch("os.fchmod") as fchm:
            rc, lines, calls = _run_session(self.shim, _plain_open_run, ["--run-id", "u2", "--share-uid", "10001"], [])
        self.assertEqual(rc, 5, lines)
        self.assertEqual(lines[0]["code"], "E_RAM_SHARE_UNSUPPORTED")
        self.assertEqual(calls, [], "open_run must not be called when the kw is absent")
        chm.assert_not_called()
        fchm.assert_not_called()

    def test_session_share_uid_bad_value_rc2(self):
        for bad in ("0", "-1", "abc", "99999999999"):
            rc, lines, _ = _run_session(self.shim, _share_open_run, ["--run-id", "u3", "--share-uid", bad], [])
            self.assertEqual(rc, 2, (bad, lines))

    def test_receipt_records_share(self):
        rc, lines, _ = _run_session(self.shim, _share_open_run, ["--run-id", "u4", "--share-uid", "10001"],
                                    ['{"cmd":"receipt"}\n', '{"cmd":"close"}\n'])
        self.assertEqual(rc, 0, lines)
        self.assertEqual(lines[1]["receipt"]["share"], {"uid": 10001, "via": "open_run.share_with_uid"})

    def test_receipt_merges_engine_share_not_overwrite(self):
        """REPORT_R F4: law3 receipt() already carries share {uid, method}; the shim adds `via` and keeps `method`."""
        class EngineShareWs(FakeWs):
            def receipt(self):
                return {"run_id": "r", "peak": 0, "share": {"uid": 10001, "method": "acl"}}

        def open_run(campaign, rid, quota, *, tool, root=None, share_with_uid=None):
            return EngineShareWs()

        rc, lines, _ = _run_session(self.shim, open_run, ["--run-id", "u6", "--share-uid", "10001"],
                                    ['{"cmd":"receipt"}\n', '{"cmd":"close"}\n'])
        self.assertEqual(rc, 0, lines)
        self.assertEqual(lines[1]["receipt"]["share"],
                         {"uid": 10001, "method": "acl", "via": "open_run.share_with_uid"})

    def test_open_line_merges_engine_share(self):
        """ADC-W2-06(1): the open line carries the engine share block (uid, method, files_read) plus the shim `via`."""
        eng = {"uid": 10001, "method": "acl", "files_read": [".lock", "owner.json"]}

        class EngineShareWs(FakeWs):
            def receipt(self):
                return {"run_id": "r", "peak": 0, "share": dict(eng)}

        def open_run(campaign, rid, quota, *, tool, root=None, share_with_uid=None):
            return EngineShareWs()

        rc, lines, _ = _run_session(self.shim, open_run, ["--run-id", "u7", "--share-uid", "10001"],
                                    ['{"cmd":"receipt"}\n', '{"cmd":"close"}\n'])
        self.assertEqual(rc, 0, lines)
        want = dict(eng, via="open_run.share_with_uid")
        self.assertEqual(lines[0]["share"], want)
        self.assertEqual(lines[1]["receipt"]["share"], want)

    def test_docstring_names_law4(self):
        doc = self.shim.__doc__
        self.assertIn("ve-sealed-1-law4", doc)
        self.assertNotIn("law3", doc)

    def test_no_share_flag_unchanged(self):
        rc, lines, calls = _run_session(self.shim, _plain_open_run, ["--run-id", "u5"], ['{"cmd":"close"}\n'])
        self.assertEqual(rc, 0, lines)
        self.assertNotIn("share", lines[0])
        self.assertNotIn("share_with_uid", calls[0][1])

    def test_shim_never_calls_chmod(self):
        tree = ast.parse((REPO / "vision" / "ramws_shim.py").read_text(encoding="utf-8"))
        bad = {"chmod", "fchmod", "lchmod", "chown", "fchown", "lchown", "setfacl", "umask"}
        for n in ast.walk(tree):
            if isinstance(n, ast.Attribute) and n.attr in bad:
                self.fail("shim references %s at line %d" % (n.attr, n.lineno))
            if isinstance(n, ast.Name) and n.id in bad:
                self.fail("shim references %s at line %d" % (n.id, n.lineno))
            if isinstance(n, ast.Constant) and isinstance(n.value, str) and any(w in n.value for w in ("setfacl", "chmod ")):
                self.fail("shim mentions %r at line %d" % (n.value, n.lineno))


if __name__ == "__main__":
    unittest.main()
