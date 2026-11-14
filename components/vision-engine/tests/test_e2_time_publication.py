"""W3 T2: refusal preserves the entire caller tree, including late failures."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from visioncore import canon, cli, pipeline
from visioncore.errors import VqaError


SCRATCH = Path(__file__).resolve().parents[1] / "build" / "room_T2"


def snapshot(root):
    return {str(p.relative_to(root)): p.read_bytes() if p.is_file() else None
            for p in sorted(root.rglob("*"))}


class PublicationTests(unittest.TestCase):
    def setUp(self):
        SCRATCH.mkdir(parents=True, exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=SCRATCH)
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.run = pipeline.analyze(cli._selftest_source("clean_static", n=2))

    def test_jc9_write_refusal_preserves_stale_tiles_and_collision(self):
        out = self.root / "vision"
        out.mkdir()
        (out / "tiles.bin").write_bytes(b"old tiles")
        (out / "frames.jsonl").mkdir()
        (out / "frames.jsonl" / "guard").write_bytes(b"keep")
        before = snapshot(self.root)
        with self.assertRaises(VqaError) as c:
            self.run.write(out)
        self.assertEqual(c.exception.rc, 3)
        self.assertEqual(snapshot(self.root), before)
        self.assertIsNone(self.run.out_dir)

    def test_rw3_4_toctou_recheck_before_rename(self):
        out = self.root / "vision"
        original = canon.write_atomic
        calls = []

        def racer(path, data):
            calls.append(path)
            if len(calls) == 1:
                out.mkdir(exist_ok=True)
                (out / "tiles.bin").write_bytes(b"raced")
            return original(path, data)

        with patch.object(canon, "write_atomic", side_effect=racer):
            with self.assertRaises(VqaError) as c:
                self.run.write(out)
        self.assertEqual((c.exception.code, c.exception.rc), ("E_VC_OUTPUT_EXISTS", 3))
        self.assertEqual((out / "tiles.bin").read_bytes(), b"raced")
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ["vision"])
        self.assertEqual(sorted(p.name for p in out.iterdir()), ["tiles.bin"])

    def test_refuse_existing_file_nonempty_directory_and_symlink(self):
        for kind in ("file", "directory", "symlink"):
            with self.subTest(kind=kind):
                out = self.root / kind
                if kind == "file":
                    out.write_bytes(b"keep")
                elif kind == "directory":
                    out.mkdir()
                    (out / "guard").write_bytes(b"keep")
                else:
                    out.symlink_to(self.root / "missing")
                with self.assertRaises(VqaError) as c:
                    self.run.write(out)
                self.assertEqual(c.exception.rc, 3)

    def test_every_staged_write_failure_leaves_no_partial_output(self):
        original = canon.write_atomic
        count = len(self.run._blobs())
        for fail_at in range(count):
            with self.subTest(fail_at=fail_at):
                before = snapshot(self.root)
                calls = []

                def fail(path, data):
                    calls.append(path)
                    if len(calls) == fail_at + 1:
                        raise VqaError("E_IO", {"reason": "injected stage failure"})
                    return original(path, data)

                with patch.object(canon, "write_atomic", side_effect=fail):
                    with self.assertRaises(VqaError):
                        self.run.write(self.root / "vision")
                self.assertEqual(snapshot(self.root), before)

    def test_success_publishes_complete_directory_once(self):
        out = self.root / "vision"
        self.run.write(out)
        self.assertEqual(snapshot(out), self.run._blobs())
        self.assertEqual(self.run.out_dir, out)
        before = snapshot(self.root)
        with self.assertRaises(VqaError) as c:
            self.run.write(out)
        self.assertEqual(c.exception.rc, 3)
        self.assertEqual(snapshot(self.root), before)

    def test_publication_failure_removes_stage_and_new_parents(self):
        before = snapshot(self.root)
        with patch("os.rename", side_effect=OSError("injected rename failure")):
            with self.assertRaises(VqaError) as c:
                self.run.write(self.root / "new" / "parent" / "vision")
        self.assertEqual(c.exception.rc, 2)
        self.assertEqual(snapshot(self.root), before)
        self.assertIsNone(self.run.out_dir)

    def test_empty_output_directory_is_atomic_on_success_and_refusal(self):
        out = self.root / "vision"
        out.mkdir()
        before = snapshot(self.root)
        with patch("os.rename", side_effect=OSError("injected rename failure")):
            with self.assertRaises(VqaError):
                self.run.write(out)
        self.assertEqual(snapshot(self.root), before)
        self.run.write(out)
        self.assertEqual(snapshot(out), self.run._blobs())


if __name__ == "__main__":
    unittest.main()
