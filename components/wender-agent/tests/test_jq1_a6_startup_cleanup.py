import os
import shutil
import tempfile
import unittest
from unittest import mock


class TestStartupCleanupAndAttachUrl(unittest.TestCase):

    def test_cdp_pipe_start_cleanup_on_popen_failure(self):
        # Defect S1-F2: wender/perceive/cdp_pipe.py cleans up tempfile.mkdtemp and os.pipe() when Popen fails
        from wender.perceive.cdp_pipe import CdpPipe

        created_dirs = []
        created_fds = []

        orig_mkdtemp = tempfile.mkdtemp
        orig_pipe = os.pipe

        def tracking_mkdtemp(*args, **kwargs):
            d = orig_mkdtemp(*args, **kwargs)
            created_dirs.append(d)
            return d

        def tracking_pipe(*args, **kwargs):
            r, w = orig_pipe(*args, **kwargs)
            created_fds.extend([r, w])
            return r, w

        def cleanup_resources():
            for d in created_dirs:
                if os.path.exists(d):
                    shutil.rmtree(d, ignore_errors=True)
            for fd in created_fds:
                try:
                    os.close(fd)
                except OSError:
                    pass

        self.addCleanup(cleanup_resources)

        pipe = CdpPipe(chrome_bin="/bin/false")

        with mock.patch("tempfile.mkdtemp", side_effect=tracking_mkdtemp), \
             mock.patch("os.pipe", side_effect=tracking_pipe), \
             mock.patch("subprocess.Popen", side_effect=OSError("launch failed")):
            with self.assertRaises(OSError):
                pipe.start()

        # Check temporary user_data_dir cleanup on startup failure
        self.assertTrue(len(created_dirs) > 0)
        user_dir = created_dirs[0]
        self.assertFalse(
            os.path.exists(user_dir),
            "Temporary user_data_dir should be removed on Popen failure"
        )

        # Check pipe file descriptors cleanup on startup failure
        self.assertTrue(len(created_fds) > 0)
        for fd in created_fds:
            with self.assertRaises(OSError, msg=f"FD {fd} should be closed on Popen failure"):
                os.fstat(fd)

    def test_cdp_pipe_repeat_start_guarded(self):
        # Defect S1-F2: wender/perceive/cdp_pipe.py guards and rejects repeat start() calls while running
        from wender.perceive.cdp_pipe import CdpError, CdpPipe

        fake_proc = mock.MagicMock()
        fake_proc.poll.return_value = None

        pipe = CdpPipe(chrome_bin="/bin/false")

        def cleanup_pipe():
            pipe.close()
            if pipe.user_data_dir and os.path.exists(pipe.user_data_dir):
                shutil.rmtree(pipe.user_data_dir, ignore_errors=True)

        self.addCleanup(cleanup_pipe)

        with mock.patch("subprocess.Popen", return_value=fake_proc), \
             mock.patch("threading.Thread"):
            pipe.start()
            with self.assertRaises((CdpError, RuntimeError)):
                pipe.start()

    def test_cdp_port_attach_url_matching_target(self):
        from wender.perceive.cdp_port import CdpPort

        port = CdpPort(port=9222, host="127.0.0.1")

        def fake_send(method, params=None, session_id=None, timeout=None):
            if method == "Target.getTargets":
                return {
                    "targetInfos": [
                        {"type": "page", "targetId": "target-chatgpt-1", "url": "https://chatgpt.com/c/12345"},
                        {"type": "page", "targetId": "target-other-2", "url": "https://example.com"},
                    ]
                }
            elif method == "Target.attachToTarget":
                self.assertEqual(params, {"targetId": "target-chatgpt-1", "flatten": True})
                return {"sessionId": "session-chatgpt-99"}
            elif method == "Page.enable":
                self.assertEqual(session_id, "session-chatgpt-99")
                return {}
            raise ValueError(f"Unexpected method: {method}")

        port.send = fake_send

        sid = port.attach_url("https://chatgpt.com")
        self.assertEqual(sid, "session-chatgpt-99")
        self.assertIn("session-chatgpt-99", port._attached)

    def test_cdp_port_attach_url_no_matching_target(self):
        from wender.perceive.cdp_pipe import CdpError
        from wender.perceive.cdp_port import CdpPort

        port = CdpPort(port=9222, host="127.0.0.1")

        def fake_send(method, params=None, session_id=None, timeout=None):
            if method == "Target.getTargets":
                return {
                    "targetInfos": [
                        {"type": "page", "targetId": "target-other-1", "url": "https://example.com"}
                    ]
                }
            raise ValueError(f"Unexpected method: {method}")

        port.send = fake_send

        with self.assertRaises(CdpError) as ctx:
            port.attach_url("https://chatgpt.com")

        self.assertIn("no open page starts with 'https://chatgpt.com'", str(ctx.exception))
        self.assertEqual(port._attached, [])


if __name__ == "__main__":
    unittest.main()
