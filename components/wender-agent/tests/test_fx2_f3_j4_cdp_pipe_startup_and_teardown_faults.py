import os
import shutil
import tempfile
import unittest
from unittest import mock

from wender.perceive.cdp_pipe import CdpError, CdpPipe


class TestCdpPipeStartupAndTeardownFaults(unittest.TestCase):

    # -- Healthy controls (ordinary passing tests) --

    def test_startup_fault_profile_creation_failure_ordinary_control(self):
        """Profile creation failure (mkdtemp) raises primary error without lingering state."""
        pipe = CdpPipe(chrome_bin="/bin/true")
        with mock.patch("tempfile.mkdtemp", side_effect=OSError("mkdtemp permission denied")):
            with self.assertRaises(OSError) as ctx:
                pipe.start()
            self.assertEqual(str(ctx.exception), "mkdtemp permission denied")
        self.assertIsNone(pipe.user_data_dir)
        self.assertIsNone(pipe.proc)

    def test_unallocated_close_ordinary_control(self):
        """Close on an unstarted CdpPipe is idempotent and does not close unrelated FDs."""
        pipe = CdpPipe(chrome_bin="/bin/true")
        r_new, w_new = os.pipe()
        try:
            pipe.close()
            os.fstat(r_new)
            os.fstat(w_new)
        finally:
            os.close(r_new)
            os.close(w_new)

    def test_repeated_close_does_not_close_reused_fds_ordinary_control(self):
        """Repeated close() calls are idempotent and do not close reused unrelated FDs."""
        fake_proc = mock.MagicMock()
        fake_proc.poll.return_value = 0  # Process already exited

        pipe = CdpPipe(chrome_bin="/bin/true")
        with mock.patch("subprocess.Popen", return_value=fake_proc), \
             mock.patch("threading.Thread"):
            pipe.start()
            pipe.close()

            # Allocate new dummy FDs that might reuse the same FD numbers
            r_new, w_new = os.pipe()
            try:
                # Call close again on pipe
                pipe.close()
                # Verify that new FDs are still valid and open
                os.fstat(r_new)
                os.fstat(w_new)
            finally:
                os.close(r_new)
                os.close(w_new)

    def test_send_failure_raises_cdp_error_ordinary_control(self):
        """Send failure on pipe write propagates as CdpError."""
        fake_proc = mock.MagicMock()
        fake_proc.poll.return_value = None

        pipe = CdpPipe(chrome_bin="/bin/true")
        with mock.patch("subprocess.Popen", return_value=fake_proc), \
             mock.patch("threading.Thread"):
            pipe.start()
            pipe._w = 999  # Invalid FD for os.write
            with mock.patch("os.write", side_effect=OSError("Bad file descriptor")):
                with self.assertRaises(CdpError) as ctx:
                    pipe.send("Target.getTargets")
                self.assertIn("CDP write failed", str(ctx.exception))
            pipe.close()

    def test_wait_timeout_and_closed_reader_teardown_ordinary_controls(self):
        """Wait timeout and closed pipe detection in send and wait_event."""
        fake_proc = mock.MagicMock()
        fake_proc.poll.return_value = None

        pipe = CdpPipe(chrome_bin="/bin/true")
        with mock.patch("subprocess.Popen", return_value=fake_proc), \
             mock.patch("threading.Thread"):
            pipe.start()
            with mock.patch("os.write", return_value=10):
                with self.assertRaises(CdpError) as ctx:
                    pipe.send("Target.getTargets", timeout=0.01)
                self.assertIn("timeout waiting for Target.getTargets", str(ctx.exception))

            with self.assertRaises(CdpError) as ctx:
                pipe.wait_event("Page.loadEventFired", timeout=0.01)
            self.assertIn("timeout waiting for event Page.loadEventFired", str(ctx.exception))

            # Simulate closed reader pipe
            pipe._closed = True
            with mock.patch("os.write", return_value=10):
                with self.assertRaises(CdpError) as ctx:
                    pipe.send("Target.getTargets", timeout=0.01)
                self.assertIn("CDP connection is closed", str(ctx.exception))

            with self.assertRaises(CdpError) as ctx:
                pipe.wait_event("Page.loadEventFired", timeout=0.01)
            self.assertIn("browser closed transport waiting for Page.loadEventFired", str(ctx.exception))

            pipe.close()

    # -- Startup cleanup and repeated-start regression controls --

    def test_startup_fault_first_pipe_allocation_leak(self):
        """First pipe allocation fault cleans up user_data_dir and preserves error."""
        created_dirs = []
        orig_mkdtemp = tempfile.mkdtemp

        def track_mkdtemp(*args, **kwargs):
            d = orig_mkdtemp(*args, **kwargs)
            created_dirs.append(d)
            return d

        pipe = CdpPipe(chrome_bin="/bin/true")
        try:
            with mock.patch("tempfile.mkdtemp", side_effect=track_mkdtemp), \
                 mock.patch("os.pipe", side_effect=OSError("pipe 1 allocation failed")):
                with self.assertRaises(OSError) as ctx:
                    pipe.start()
                self.assertEqual(str(ctx.exception), "pipe 1 allocation failed")

            self.assertTrue(len(created_dirs) > 0)
            user_dir = created_dirs[0]
            self.assertFalse(
                os.path.exists(user_dir),
                "Temporary user_data_dir must be removed if 1st os.pipe() fails"
            )
            self.assertIsNone(pipe.user_data_dir)
        finally:
            for d in created_dirs:
                if os.path.exists(d):
                    shutil.rmtree(d, ignore_errors=True)

    def test_startup_fault_second_pipe_allocation_leak(self):
        """Second pipe allocation fault cleans up user_data_dir and first pipe FDs."""
        created_dirs = []
        created_fds = []
        orig_mkdtemp = tempfile.mkdtemp
        orig_pipe = os.pipe
        pipe_call_count = 0

        def track_mkdtemp(*args, **kwargs):
            d = orig_mkdtemp(*args, **kwargs)
            created_dirs.append(d)
            return d

        def fail_second_pipe(*args, **kwargs):
            nonlocal pipe_call_count
            pipe_call_count += 1
            if pipe_call_count == 2:
                raise OSError("pipe 2 allocation failed")
            r, w = orig_pipe(*args, **kwargs)
            created_fds.extend([r, w])
            return r, w

        pipe = CdpPipe(chrome_bin="/bin/true")
        try:
            with mock.patch("tempfile.mkdtemp", side_effect=track_mkdtemp), \
                 mock.patch("os.pipe", side_effect=fail_second_pipe):
                with self.assertRaises(OSError) as ctx:
                    pipe.start()
                self.assertEqual(str(ctx.exception), "pipe 2 allocation failed")

            self.assertTrue(len(created_dirs) > 0)
            self.assertFalse(
                os.path.exists(created_dirs[0]),
                "Temporary user_data_dir must be removed if 2nd os.pipe() fails"
            )
            self.assertTrue(len(created_fds) > 0)
            for fd in created_fds:
                with self.assertRaises(OSError):
                    os.fstat(fd)
        finally:
            for d in created_dirs:
                if os.path.exists(d):
                    shutil.rmtree(d, ignore_errors=True)
            for fd in created_fds:
                try:
                    os.close(fd)
                except OSError:
                    pass

    def test_startup_fault_popen_failure_leak(self):
        """Popen failure cleans up user_data_dir and all allocated pipe FDs."""
        created_dirs = []
        created_fds = []
        orig_mkdtemp = tempfile.mkdtemp
        orig_pipe = os.pipe

        def track_mkdtemp(*args, **kwargs):
            d = orig_mkdtemp(*args, **kwargs)
            created_dirs.append(d)
            return d

        def track_pipe(*args, **kwargs):
            r, w = orig_pipe(*args, **kwargs)
            created_fds.extend([r, w])
            return r, w

        pipe = CdpPipe(chrome_bin="/bin/true")
        try:
            with mock.patch("tempfile.mkdtemp", side_effect=track_mkdtemp), \
                 mock.patch("os.pipe", side_effect=track_pipe), \
                 mock.patch("subprocess.Popen", side_effect=OSError("Popen launch failed")):
                with self.assertRaises(OSError) as ctx:
                    pipe.start()
                self.assertEqual(str(ctx.exception), "Popen launch failed")

            self.assertTrue(len(created_dirs) > 0)
            self.assertFalse(
                os.path.exists(created_dirs[0]),
                "Temporary user_data_dir must be removed on Popen failure"
            )
            self.assertTrue(len(created_fds) == 4)
            for fd in created_fds:
                with self.assertRaises(OSError):
                    os.fstat(fd)
        finally:
            for d in created_dirs:
                if os.path.exists(d):
                    shutil.rmtree(d, ignore_errors=True)
            for fd in created_fds:
                try:
                    os.close(fd)
                except OSError:
                    pass

    def test_startup_fault_reader_thread_start_failure_reaping(self):
        """Reader thread startup failure reaps spawned child process and cleans up resources."""
        created_dirs = []
        created_fds = []
        orig_mkdtemp = tempfile.mkdtemp
        orig_pipe = os.pipe

        def track_mkdtemp(*args, **kwargs):
            d = orig_mkdtemp(*args, **kwargs)
            created_dirs.append(d)
            return d

        def track_pipe(*args, **kwargs):
            r, w = orig_pipe(*args, **kwargs)
            created_fds.extend([r, w])
            return r, w

        fake_proc = mock.MagicMock()
        fake_proc.poll.return_value = None

        fake_thread = mock.MagicMock()
        fake_thread.start.side_effect = RuntimeError("Thread start failed")

        pipe = CdpPipe(chrome_bin="/bin/true")
        try:
            with mock.patch("tempfile.mkdtemp", side_effect=track_mkdtemp), \
                 mock.patch("os.pipe", side_effect=track_pipe), \
                 mock.patch("subprocess.Popen", return_value=fake_proc), \
                 mock.patch("threading.Thread", return_value=fake_thread):
                with self.assertRaises(RuntimeError) as ctx:
                    pipe.start()
                self.assertEqual(str(ctx.exception), "Thread start failed")

            self.assertTrue(fake_proc.kill.called or fake_proc.wait.called, "Spawned process must be reaped")
            self.assertFalse(
                os.path.exists(created_dirs[0]),
                "Temporary user_data_dir must be removed on reader thread start failure"
            )
            for fd in created_fds:
                with self.assertRaises(OSError):
                    os.fstat(fd)
        finally:
            for d in created_dirs:
                if os.path.exists(d):
                    shutil.rmtree(d, ignore_errors=True)
            for fd in created_fds:
                try:
                    os.close(fd)
                except OSError:
                    pass

    def test_repeated_start_rejected_before_allocation(self):
        """Repeated start() calls reject before allocating new resources."""
        fake_proc = mock.MagicMock()
        fake_proc.poll.return_value = None

        pipe = CdpPipe(chrome_bin="/bin/true")
        with mock.patch("subprocess.Popen", return_value=fake_proc), \
             mock.patch("threading.Thread"):
            pipe.start()

            with mock.patch("tempfile.mkdtemp") as mock_mkdtemp, \
                 mock.patch("os.pipe") as mock_pipe:
                with self.assertRaises((CdpError, RuntimeError)):
                    pipe.start()
                mock_mkdtemp.assert_not_called()
                mock_pipe.assert_not_called()

            pipe.close()


if __name__ == "__main__":
    unittest.main()
