"""Exceptional adapter cleanup must still reap the owned launcher group."""
from asset_factory import proc as af_proc
import json
import io
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import types
import unittest
from unittest import mock

from providers.blender_sculpt.probe import run as probe


class CleanupRegressionTests(unittest.TestCase):
    def test_adapter_close_io_error_still_reaps_and_records_lifecycle(self):
        bvqa = Path(__file__).resolve().parents[2] / "blender-visual-qa"
        with mock.patch.object(sys, "path", [str(bvqa), *sys.path]):
            from vqa3d.adapters.blender_live import BlenderLiveAdapter

        class BrokenCleanup(BlenderLiveAdapter):
            def __init__(self, *, live_dir, timeout):
                self._lock = threading.Lock()
                self._closed = False
                # Exercise the real close(): an owned file changed to a directory.
                self._owned = [Path(live_dir)]

            def probe(self, render=False):
                raise TimeoutError("injected transport timeout")

        popen, children = af_proc.popen, []

        def launch_owned(_argv, **kwargs):
            child = popen([sys.executable, "-I", "-c", "import time; time.sleep(60)"], **kwargs)
            children.append(child)
            (Path(kwargs["env"]["VQA3D_LIVE_DIR"]) / "bridge.json").write_text("{}")
            return child

        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp) / "live"
            try:
                with mock.patch.object(probe.af_proc, "popen", side_effect=launch_owned), \
                        mock.patch.object(probe.importlib, "import_module",
                                          return_value=types.SimpleNamespace(BlenderLiveAdapter=BrokenCleanup)), \
                        mock.patch.object(probe, "stop_owned", wraps=probe.stop_owned) as stop:
                    with self.assertRaises(IsADirectoryError):
                        probe.live_cases(Path(sys.executable), bvqa, directory, 1)
                    stop.assert_called_once_with(children[0])
                self.assertIsNotNone(af_proc.poll_exit(children[0]))
                lifecycle = json.loads((directory / "lifecycle.json").read_text())
                self.assertTrue(lifecycle["process_reaped"])
                self.assertEqual(lifecycle["adapter_cleanup_error"], "IsADirectoryError")
            finally:
                # The red version of this test must not leave its injected child alive.
                for child in children:
                    if af_proc.poll_exit(child) is not None:
                        continue  # A-SAFE: R44 forbids stale leader authorization after reaping.
                    try:
                        af_proc.signal_group(child, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    child.wait(timeout=3)


class CleanupMutantTests(unittest.TestCase):
    def test_reviewer_cleanup_order_fails_the_same_regression(self):
        source = Path(probe.__file__).read_text()
        start = source.index("        finally:\n            cleanup_error = None", source.index("def live_cases"))
        end = source.index("\n\ndef ", start)
        broken = ("        finally:\n"
                  "            if adapter is not None:\n"
                  "                adapter.close()\n"
                  "            stop_owned(process)\n")
        namespace = {"__name__": "sculpt_cleanup_mutant", "__file__": probe.__file__}
        exec(compile(source[:start] + broken + source[end:], probe.__file__, "exec"), namespace)
        output = io.StringIO()
        case = CleanupRegressionTests("test_adapter_close_io_error_still_reaps_and_records_lifecycle")
        with mock.patch.object(probe, "live_cases", namespace["live_cases"]):
            outcome = unittest.TextTestRunner(stream=output).run(case)
        self.assertEqual(len(outcome.failures), 1, output.getvalue())
        self.assertEqual(outcome.errors, [], output.getvalue())


if __name__ == "__main__":
    unittest.main()
