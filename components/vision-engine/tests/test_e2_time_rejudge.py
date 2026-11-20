"""ADV-16: deterministic offline verification, stored-run immutability and refusal."""
import contextlib
import io
import os
import subprocess
import sys
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from visioncore import API_VERSION, canon, cli, pipeline
from tests.test_e2_time_publication import SCRATCH, snapshot


def invoke(args):
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
        rc = cli.main(args)
    return rc, out.getvalue()


class RejudgeTests(unittest.TestCase):
    def setUp(self):
        SCRATCH.mkdir(parents=True, exist_ok=True)
        tmp = tempfile.TemporaryDirectory(dir=SCRATCH)
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.run = pipeline.analyze(cli._selftest_source("clean_static", n=3))
        self.vdir = self.root / "vision"
        self.run.write(self.vdir)
        self.pk = self.root / "pack.json"
        self.judge = self.root / "judge-output.json"
        self.pack = {"schema": "vqa.pack/v2", "grid": self.run.grid.ident,
                     "params_sha256": self.run.params_sha256, "api_version": API_VERSION,
                     "frame_map": [[0, 0], [1, 1], [2, 2]], "gops": ["g0"],
                     "z1_sha256": "00" * 32, "cap_bytes": 8000}
        canon.write_atomic(self.pk, canon.dumps(self.pack))
        canon.write_atomic(self.judge, canon.dumps({"results": [{"check_id": "custom", "status": "PASS"}]}))
        self.args = ["rejudge", "--from-run", str(self.vdir), "--pack", str(self.pk),
                     "--judge-output", str(self.judge)]

    def test_stored_evidence_is_deterministic_without_analysis(self):
        before = snapshot(self.root)
        with patch.object(pipeline, "analyze", side_effect=AssertionError("must not recapture")):
            first = invoke(self.args)
            second = invoke(self.args)
        self.assertEqual(first[0], 0)
        self.assertEqual(first, second)
        direct = invoke(["verify-citations", str(self.vdir), "--pack", str(self.pk),
                         "--judge-output", str(self.judge)])
        self.assertEqual(first, direct)
        self.assertEqual(snapshot(self.root), before)

    def test_rw3_5_determinism_across_hashseed_and_cwd(self):
        repo = Path(__file__).resolve().parents[1]
        outs = []
        for seed, cwd in (("1", self.root), ("2", repo)):
            out = self.root / ("o%s.json" % seed)
            env = dict(os.environ, PYTHONHASHSEED=seed, PYTHONPATH=str(repo), PYTHONDONTWRITEBYTECODE="1")
            r = subprocess.run([sys.executable, "-m", "visioncore.cli"] + self.args + ["--out", str(out)],
                               cwd=cwd, env=env, capture_output=True, timeout=120)
            self.assertEqual(r.returncode, 0, r.stderr)
            outs.append((r.stdout, hashlib.sha256(out.read_bytes()).hexdigest()))
        self.assertEqual(outs[0], outs[1])

    def test_writes_only_success_and_refuses_existing_output(self):
        out = self.root / "verdict.json"
        before = snapshot(self.vdir)
        rc, text = invoke(self.args + ["--out", str(out)])
        self.assertEqual(rc, 0)
        self.assertEqual(out.read_text(), text)
        self.assertEqual(snapshot(self.vdir), before)
        guard = snapshot(self.root)
        self.assertEqual(invoke(self.args + ["--out", str(out)])[0], 3)
        self.assertEqual(snapshot(self.root), guard)

    def test_rejects_stale_pack_and_keeps_output_absent(self):
        self.pack["params_sha256"] = "ff" * 32
        canon.write_atomic(self.pk, canon.dumps(self.pack))
        before = snapshot(self.root)
        self.assertEqual(invoke(self.args + ["--out", str(self.root / "result.json")])[0], 4)
        self.assertEqual(snapshot(self.root), before)

    def test_forged_pointer_is_rejected_without_output(self):
        canon.write_atomic(self.judge, canon.dumps({"results": [
            {"check_id": "render.flicker", "status": "FAIL", "reason_class": "flicker",
             "pointers": ["f999:t0000"]}]}))
        before = snapshot(self.root)
        self.assertEqual(invoke(self.args + ["--out", str(self.root / "result.json")])[0], 3)
        self.assertEqual(snapshot(self.root), before)

    def test_output_must_not_modify_stored_evidence(self):
        before = snapshot(self.root)
        self.assertEqual(invoke(self.args + ["--out", str(self.vdir / "new.json")])[0], 2)
        self.assertEqual(snapshot(self.root), before)

    def test_missing_flags_and_judge_execution_flags_are_usage_errors(self):
        for args in (["rejudge"], self.args + ["--arm", "argon"], self.args + ["--s6", "on"]):
            with self.subTest(args=args):
                self.assertEqual(invoke(args)[0], 2)

    def test_pack_rejects_malformed_numbers_before_reading_stored_evidence(self):
        for flag in ("--batch", "--cap", "--shuffle-window"):
            with self.subTest(flag=flag):
                rc, text = invoke(["pack", str(self.root / "missing"), flag, "invalid"])
                self.assertEqual(rc, 2)
                self.assertEqual(canon.loads(text)["code"], "E_USAGE")

    def test_pack_keeps_argument_type_and_consumer_range_errors_distinct(self):
        from visioncore import pack

        args = ["pack", str(self.vdir), "--cap"]
        rc, text = invoke(args + [1.5])
        self.assertEqual(rc, cli.VqaError("E_SCHEMA", {}).rc)
        self.assertEqual(canon.loads(text)["code"], "E_SCHEMA")

        budget = cli.VqaError("E_BUDGET", {})
        batch = {"gops": ["g0"]}
        with patch.object(cli, "_vision_batch", return_value=batch), \
                patch.object(pack, "pack", side_effect=budget) as run_pack:
            rc, text = invoke(args + ["0"])
        run_pack.assert_called_once_with(batch, batch_no=0, cap_bytes=0)
        self.assertEqual((rc, canon.loads(text)["code"]), (budget.rc, budget.code))

    def test_pack_reports_missing_tiles_as_io_error(self):
        missing = self.vdir / "tiles.bin"
        missing.unlink()
        rc, text = invoke(["pack", str(self.vdir)])
        self.assertEqual(rc, cli.VqaError("E_IO", {}).rc)
        self.assertEqual(canon.loads(text)["code"], "E_IO")

    def test_rejudge_reports_ordinary_write_failure(self):
        out = self.root / "verdict.json"
        before = snapshot(self.root)
        with patch.object(canon, "write_atomic", side_effect=PermissionError("writing verdict")):
            rc, text = invoke(self.args + ["--out", str(out)])
        self.assertEqual(rc, cli.VqaError("E_IO", {}).rc)
        self.assertEqual(canon.loads(text)["code"], "E_IO")
        self.assertEqual(snapshot(self.root), before)


if __name__ == "__main__":
    unittest.main()
