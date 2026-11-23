import errno
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from textwrap import dedent
from unittest import TestCase
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import runtime


class BrowserCleanupTests(TestCase):
    def test_profile_removal_waits_for_detached_descendant_and_leaves_foreign_process(self):
        fixture = dedent("""\
            import os
            from pathlib import Path
            import signal
            import sys
            import time

            profile = Path(sys.argv[1])
            if os.fork() == 0:
                os.setsid()
                if os.fork() == 0:
                    def finish(_signal, _frame):
                        time.sleep(0.15)
                        (profile / "flushed").touch()
                        sys.exit(0)

                    signal.signal(signal.SIGTERM, finish)
                    (profile / "descendant").write_text(str(os.getpid()))
                    while True:
                        signal.pause()
                os._exit(0)
            while not (profile / "exit-parent").exists():
                time.sleep(0.01)
            """)
        with tempfile.TemporaryDirectory() as work, tempfile.TemporaryFile() as error:
            browser = runtime.Browser.__new__(runtime.Browser)
            browser.profile = Path(work) / "profile"
            browser.profile.mkdir()
            foreign = subprocess.Popen([sys.executable, "-IB", "-c", "import signal; signal.pause()"], env={})
            try:
                browser.child = runtime.BrowserProcess(
                    [sys.executable, "-IB", "-c", fixture, str(browser.profile)], {}, error,
                )
                try:
                    browser.child.identify()
                    marker = browser.profile / "descendant"
                    runtime.eventually(lambda: marker.exists() and marker.stat().st_size,
                                       "detached fixture did not start", timeout=3)
                    pid = int(marker.read_text())
                    (browser.profile / "exit-parent").touch()
                    runtime.eventually(lambda: not browser.child.live(), "fixture parent did not exit", timeout=3)

                    remove = runtime.remove_private_tree

                    def safe_remove(path):
                        self.assertFalse(Path(f"/proc/{pid}").exists(), "detached child still exists")
                        self.assertTrue((path / "flushed").is_file(), "detached child did not finish flushing")
                        remove(path)

                    # Mutant: removing the profile as soon as the browser parent exits.
                    with self.assertRaisesRegex(AssertionError, "detached child still exists"):
                        safe_remove(browser.profile)
                    with patch.object(runtime, "remove_private_tree", side_effect=safe_remove):
                        browser.close()
                    self.assertFalse(browser.profile.exists())
                    self.assertIsNone(foreign.poll())
                finally:
                    browser.child.close()
            finally:
                foreign.terminate()
                foreign.wait(timeout=3)

    def test_failed_drain_prevents_profile_removal(self):
        browser = runtime.Browser.__new__(runtime.Browser)
        browser.child = Mock()
        browser.child.close.side_effect = runtime.Refused("undrained process tree")
        with patch.object(runtime, "remove_private_tree") as remove:
            with self.assertRaisesRegex(runtime.Refused, "undrained process tree"):
                browser.close()
        remove.assert_not_called()

    def test_drain_escalates_for_owned_child_ignoring_sigterm(self):
        fixture = dedent("""\
            from pathlib import Path
            import signal
            import sys

            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            Path(sys.argv[1]).touch()
            while True:
                signal.pause()
            """)
        with tempfile.TemporaryDirectory() as work, tempfile.TemporaryFile() as error:
            marker = Path(work) / "ready"
            child = runtime.BrowserProcess([sys.executable, "-IB", "-c", fixture, str(marker)], {}, error)
            try:
                child.identify()
                runtime.eventually(marker.exists, "fixture did not install signal handler", timeout=3)
                self.assertTrue(child.live())
            finally:
                child.close()
            self.assertFalse(Path(f"/proc/{child.pid}").exists())

    def test_private_removal_retries_enotempty_and_eventually_refuses(self):
        path = Path("unused-private-path")
        nonempty = OSError(errno.ENOTEMPTY, os.strerror(errno.ENOTEMPTY))
        with patch.object(runtime.shutil, "rmtree", side_effect=[nonempty, None]) as remove, \
             patch.object(runtime.time, "sleep"):
            runtime.remove_private_tree(path)
        self.assertEqual([call.args for call in remove.call_args_list], [(path,), (path,)])

        with patch.object(runtime.shutil, "rmtree", side_effect=nonempty) as remove, \
             patch.object(runtime.time, "sleep"), \
             patch.object(runtime.time, "monotonic", side_effect=[0, 0, 2]):
            with self.assertRaisesRegex(runtime.Refused, "remained nonempty"):
                runtime.remove_private_tree(path)
        remove.assert_called_once_with(path)
