import contextlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tools.render import pipeline
from asset_factory import core


class PipelineTests(unittest.TestCase):
    def test_absent_oidn_cannot_see(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(pipeline.Refusal) as caught:
                pipeline.probe_oidn(Path(d) / "missing", Path(d))
            self.assertEqual((caught.exception.rc, caught.exception.reason), (3, "oidn_absent"))

    def test_wrong_version_cannot_see_then_pinned_version_green(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            binary = root / "oidn"
            binary.write_text("#!/bin/sh\nexit 0\n")
            binary.chmod(0o700)
            def fake(argv, log, timeout_s, **kwargs):
                Path(log).write_text("Intel(R) Open Image Denoise 2.4.0\n  Device    : CPU\n")
                (root / "probe_out.pfm").write_bytes(b"out")
            with patch.object(pipeline, "run", side_effect=fake):
                with self.assertRaises(pipeline.Refusal) as caught:
                    pipeline.probe_oidn(binary, root)
                self.assertEqual(caught.exception.rc, 3)
            def valid(argv, log, timeout_s, **kwargs):
                Path(log).write_text("Intel(R) Open Image Denoise 2.5.1\n  Device    : CPU\n")
                (root / "probe_out.pfm").write_bytes(b"out")
            with patch.object(pipeline, "run", side_effect=valid):
                self.assertEqual(pipeline.probe_oidn(binary, root)["version"], "2.5.1")

    def test_existing_output_is_preserved_without_launch(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            source = root / "scene.blend"
            source.write_bytes(b"scene")
            output = root / "caller"
            output.mkdir()
            (output / "receipt.json").write_bytes(b"caller bytes")
            with patch.object(pipeline, "probe_oidn") as probe:
                with self.assertRaises(pipeline.Refusal) as caught:
                    pipeline.render(source, output, oidn=source, blender=Path("/bin/true"), start=1, end=2)
                self.assertEqual(caught.exception.rc, 5)
                probe.assert_not_called()
            self.assertEqual((output / "receipt.json").read_bytes(), b"caller bytes")

    def test_bool_frame_refused_before_launch(self):
        with self.assertRaises(pipeline.Refusal) as caught:
            pipeline.render(Path("missing.blend"), Path("out"), oidn=Path("none"),
                            blender=Path("/bin/true"), start=True, end=2)
        self.assertEqual(caught.exception.rc, 2)

    def test_frame_schema_rejects_false_success(self):
        schema = core.load_json(pipeline.HERE / "frame.schema.json", max_bytes=32768)
        with self.assertRaises(core.ContractError):
            core.validate({"schema": "af.render-frame/v1", "rc": 0, "status": "OK"}, schema)

    def test_timeout_reaps_owned_group(self):
        import subprocess
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(subprocess.TimeoutExpired):
                pipeline.run(["/bin/sleep", "5"], Path(d) / "log", 0.01)

    def test_cli_invalid_arguments_rc_two(self):
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(pipeline.main([]), 2)


if __name__ == "__main__":
    unittest.main()
