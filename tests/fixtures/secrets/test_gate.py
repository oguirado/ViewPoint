"""First tests for the G-SECRETS shared runner (W0-CI-SECRETS).

Stub-driven unit tests (GateUnitTests) plus pinned-tool integration tests
(GateIntegrationTests). Standard library only. A missing runner fails its
tests; nothing here skips.
"""

import base64
import collections
import contextlib
import io
import json
import math
import os
import re
import secrets
import shutil
import stat
import string
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest import mock

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))
import gate  # noqa: E402

REPO_ROOT = _HERE.parents[2]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "secrets.yml"
HOOK = REPO_ROOT / ".githooks" / "pre-push"

_TOKEN_A = "gh"
_TOKEN_B = "p_"


def _token_prefix():
    return _TOKEN_A + _TOKEN_B


def _token_suffix(length):
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


_STUB = r"""#!/usr/bin/env python3
import json
import os
import sys


def _value(argv, flag):
    for pos, token in enumerate(argv):
        if token == flag and pos + 1 < len(argv):
            return argv[pos + 1]
    return ""


def main(argv):
    log = os.environ.get("STUB_ARGV_LOG", "")
    if log:
        with open(log, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(argv) + "\n")
    nap = float(os.environ.get("STUB_SLEEP", "0") or 0)
    if nap:
        import time
        time.sleep(nap)
    if _value(argv, "--source") == ".":
        prefix = "STUB_TREE"
    else:
        prefix = "STUB_NOGIT" if "--no-git" in argv else "STUB"
    status = int(os.environ.get(prefix + "_STATUS", "0"))
    body = os.environ.get(prefix + "_REPORT", "[]")
    blind = os.environ.get("STUB_BLIND", "")
    if blind and os.path.basename(_value(argv, "--source")) == blind:
        status, body = 0, "[]"
    report = _value(argv, "--report-path")
    if os.environ.get("STUB_NO_WRITE", "") != "1" and report:
        with open(report, "w", encoding="utf-8") as handle:
            handle.write(body)
    trouble = os.environ.get(prefix + "_STDERR", "")
    if trouble:
        sys.stderr.write(trouble)
    return status


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
"""


def _finding(rule="test-rule", secret="SENTINEL-SECRET",
             match="SENTINEL-MATCH", file="SENTINEL-FILE"):
    return json.dumps([{
        "RuleID": rule, "Secret": secret, "Match": match,
        "File": file, "StartLine": 1,
    }])


def _write_stub(directory):
    path = directory / "gitleaks-stub"
    path.write_text(_STUB, encoding="utf-8")
    os.chmod(path, 0o700)
    return path


def _scratch(case):
    path = Path(tempfile.mkdtemp(prefix="mv-secrets-test-"))
    case.addCleanup(shutil.rmtree, path, True)
    return path


def _require_git():
    if shutil.which("git") is None:
        raise AssertionError("git is required on PATH")


def _git_env(extra=None):
    env = dict(os.environ)
    env.update({
        "GIT_AUTHOR_NAME": "tctx",
        "GIT_AUTHOR_EMAIL": "tctx@example.invalid",
        "GIT_COMMITTER_NAME": "tctx",
        "GIT_COMMITTER_EMAIL": "tctx@example.invalid",
        "GIT_CONFIG_NOSYSTEM": "1",
    })
    if extra:
        env.update(extra)
    return env


def _run_git(repo, *args, env):
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, timeout=60, env=env)


def _make_repo(path, payloads=()):
    _require_git()
    env = _git_env()
    path.mkdir(parents=True, exist_ok=True)
    assert _run_git(path, "init", "-q", env=env).returncode == 0
    _run_git(path, "config", "user.email", "tctx@example.invalid", env=env)
    _run_git(path, "config", "user.name", "tctx", env=env)
    _run_git(path, "config", "commit.gpgsign", "false", env=env)
    for name, body in payloads:
        target = path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
    assert _run_git(path, "add", "-A", env=env).returncode == 0
    assert _run_git(path, "commit", "-qm", "init", env=env).returncode == 0


def _write_context_repo(root):
    workflow = root / ".github" / "workflows" / "secrets.yml"
    workflow.parent.mkdir(parents=True, exist_ok=True)
    workflow.write_text(
        "name: Secrets\n# pinned digest " + gate.GITLEAKS_SHA256 + "\n",
        encoding="utf-8")


def _stdout_of(func, *args, **kwargs):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        func(*args, **kwargs)
    return buf.getvalue()


def _check_public(repo, *args, env=None):
    script = repo / "tools" / "check_public.sh"
    script.parent.mkdir(exist_ok=True)
    shutil.copyfile(REPO_ROOT / "tools" / "check_public.sh", script)
    return subprocess.run(["bash", str(script), *args], cwd=repo,
                          capture_output=True, text=True, timeout=120, env=env)


class ReleaseScopeTests(unittest.TestCase):
    """Check range validation and history coverage with harmless fixture data."""

    def setUp(self):
        self.tmp = _scratch(self)
        self.repo = self.tmp / "repo"
        _make_repo(self.repo, [("note.txt", "clean content\n")])
        self.stub = _write_stub(self.tmp)
        self.argv_log = self.tmp / "argv.log"

    def git(self, *args):
        result = _run_git(self.repo, *args, env=_git_env())
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def scan(self, *args):
        command = [sys.executable, "-I", str(_HERE / "gate.py"), "history",
                   "--gitleaks", str(self.stub), "--repo", str(self.repo), *args]
        return subprocess.run(command, cwd=self.repo, capture_output=True, text=True,
                              timeout=120, env={**os.environ, "STUB_ARGV_LOG": str(self.argv_log)})

    def assert_base_rejected(self, *args):
        scanner = self.scan(*args)
        privacy = _check_public(self.repo, *args)
        with self.subTest(gate="gitleaks"):
            self.assertEqual(scanner.returncode, 1)
            self.assertFalse(self.argv_log.exists())
        with self.subTest(gate="privacy"):
            self.assertEqual(privacy.returncode, 2)

    def test_descendant_base_rejected(self):
        candidate = self.git("rev-parse", "HEAD")
        self.git("commit", "--allow-empty", "-qm", "next")
        self.assert_base_rejected("--base", "HEAD", "--candidate", candidate)

    def test_divergent_base_rejected(self):
        self.git("branch", "other")
        self.git("commit", "--allow-empty", "-qm", "candidate")
        candidate = self.git("rev-parse", "HEAD")
        self.git("checkout", "-q", "other")
        self.git("commit", "--allow-empty", "-qm", "other")
        self.assert_base_rejected("--base", "HEAD", "--candidate", candidate)

    def test_missing_default_and_unresolvable_base_rejected(self):
        self.assert_base_rejected()
        self.argv_log.unlink(missing_ok=True)
        self.assert_base_rejected("--base", "unresolvable")
        self.assertEqual(self.scan("--base", "").returncode, 0)
        self.assertEqual(_check_public(self.repo, "--base", "").returncode, 0)

    def test_moved_tracking_tip_is_not_a_default_release_base(self):
        later = self.git("rev-parse", "HEAD")
        self.git("update-ref", "refs/remotes/origin/main", later)
        self.git("commit", "--allow-empty", "-qm", "candidate")
        for candidate in ("HEAD", later):
            with self.subTest(candidate=candidate):
                self.assert_base_rejected("--candidate", candidate)
        self.assertEqual(self.scan("--base", later).returncode, 0)
        self.assertEqual(_check_public(self.repo, "--base", later).returncode, 0)

    def test_history_requests_merge_diffs(self):
        result = self.scan("--base", "")
        self.assertEqual(result.returncode, 0)
        history = json.loads(self.argv_log.read_text().splitlines()[0])
        self.assertIn("--log-opts=--diff-merges=separate --root --text " + self.git("rev-parse", "HEAD"),
                      history)

    def test_identical_base_reports_empty_history_and_scans_tree(self):
        scanner = self.scan("--base", "HEAD")
        privacy = _check_public(self.repo, "--base", "HEAD")
        self.assertEqual(scanner.returncode, 0, scanner.stdout)
        self.assertIn("history: no commits in selected range", scanner.stdout)
        self.assertIn("tree: clean", scanner.stdout)
        self.assertEqual(privacy.returncode, 0, privacy.stderr)
        self.assertIn("history empty range", privacy.stderr)

    def test_privacy_reads_changed_blobs_from_commit_diffs(self):
        base = self.git("rev-parse", "HEAD")
        name = "two\nlines.txt"
        (self.repo / name).write_text("harmless history content\n")
        self.git("add", "--", name)
        self.git("commit", "-qm", "add note")
        blob = self.git("rev-parse", "HEAD:" + name)
        self.git("rm", "-q", "--", name)
        self.git("commit", "-qm", "remove note")
        trace = self.tmp / "git-trace.jsonl"
        result = _check_public(self.repo, "--base", base,
                               env={**os.environ, "GIT_TRACE2_EVENT": str(trace)})
        self.assertEqual(result.returncode, 0, result.stderr)
        commands = [event["argv"][4:] for line in trace.read_text().splitlines()
                    if (event := json.loads(line))["event"] == "start"]
        self.assertTrue(any(args[0] == "log" and "--raw" in args and "-z" in args
                            for args in commands))
        self.assertIn(["cat-file", "blob", blob], commands)

    def test_config_is_applied_to_both_release_legs(self):
        config = self.tmp / "release policy.toml"
        gate.write_default_config(config)
        result = self.scan("--base", "", "--config", str(config))
        self.assertEqual(result.returncode, 0)
        calls = [json.loads(line) for line in self.argv_log.read_text().splitlines()]
        self.assertEqual(len(calls), 2)
        for argv in calls:
            self.assertEqual(argv[argv.index("--config") + 1], str(config))


class GateUnitTests(unittest.TestCase):
    """Stub-driven controller behaviour; never touches the network."""

    def test_history_clean_passes(self):
        tmp = _scratch(self)
        repo = tmp / "repo"
        _make_repo(repo, [("note.txt", "clean content\n")])
        stub = _write_stub(tmp)
        env = {"STUB_STATUS": "0", "STUB_REPORT": "[]"}
        with mock.patch.dict(os.environ, env):
            text = _stdout_of(gate.run_history, stub, repo, tmp, base="")
        self.assertIn("history: clean", text)

    def test_history_missing_report_rejects(self):
        tmp = _scratch(self)
        repo = tmp / "repo"
        _make_repo(repo, [("note.txt", "clean content\n")])
        stub = _write_stub(tmp)
        env = {"STUB_STATUS": "0", "STUB_REPORT": "[]", "STUB_NO_WRITE": "1"}
        with mock.patch.dict(os.environ, env):
            with self.assertRaises(gate.GateFailure):
                gate.run_history(stub, repo, tmp, base="HEAD")

    def test_history_malformed_report_rejects(self):
        tmp = _scratch(self)
        repo = tmp / "repo"
        _make_repo(repo, [("note.txt", "clean content\n")])
        stub = _write_stub(tmp)
        env = {"STUB_STATUS": "0", "STUB_REPORT": "{oops"}
        with mock.patch.dict(os.environ, env):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                with self.assertRaises(gate.GateFailure):
                    gate.run_history(stub, repo, tmp, base="HEAD")
        self.assertNotIn("oops", buf.getvalue())

    def test_history_wrong_type_report_rejects(self):
        tmp = _scratch(self)
        repo = tmp / "repo"
        _make_repo(repo, [("note.txt", "clean content\n")])
        stub = _write_stub(tmp)
        bad = ['{"a": 1}', '["x"]', '[{"RuleID": 7}]',
               '[{"RuleID": null}]', '[42]']
        for body in bad:
            with self.subTest(body=body):
                env = {"STUB_STATUS": "0", "STUB_REPORT": body}
                with mock.patch.dict(os.environ, env):
                    with self.assertRaises(gate.GateFailure):
                        gate.run_history(stub, repo, tmp, base="HEAD")

    def test_history_clean_status_with_findings_rejects(self):
        tmp = _scratch(self)
        repo = tmp / "repo"
        _make_repo(repo, [("note.txt", "clean content\n")])
        stub = _write_stub(tmp)
        env = {"STUB_STATUS": "0", "STUB_REPORT": _finding()}
        with mock.patch.dict(os.environ, env):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                with self.assertRaises(gate.GateFailure):
                    gate.run_history(stub, repo, tmp, base="HEAD")
        self.assertIn("1 test-rule", buf.getvalue())

    def test_history_finding_exit_rejects(self):
        tmp = _scratch(self)
        repo = tmp / "repo"
        _make_repo(repo, [("note.txt", "clean content\n")])
        stub = _write_stub(tmp)
        env = {"STUB_STATUS": "1", "STUB_REPORT": _finding()}
        with mock.patch.dict(os.environ, env):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                with self.assertRaises(gate.GateFailure):
                    gate.run_history(stub, repo, tmp, base="HEAD")
        self.assertIn("1 test-rule", buf.getvalue())

    def test_history_finding_exit_empty_report_rejects(self):
        tmp = _scratch(self)
        repo = tmp / "repo"
        _make_repo(repo, [("note.txt", "clean content\n")])
        stub = _write_stub(tmp)
        env = {"STUB_STATUS": "1", "STUB_REPORT": "[]"}
        with mock.patch.dict(os.environ, env):
            with self.assertRaises(gate.GateFailure):
                gate.run_history(stub, repo, tmp, base="HEAD")

    def test_history_crash_status_rejects(self):
        tmp = _scratch(self)
        repo = tmp / "repo"
        _make_repo(repo, [("note.txt", "clean content\n")])
        stub = _write_stub(tmp)
        for body in ("[]", _finding()):
            with self.subTest(body=body[:10]):
                env = {"STUB_STATUS": "2", "STUB_REPORT": body}
                with mock.patch.dict(os.environ, env):
                    with self.assertRaises(gate.GateFailure):
                        gate.run_history(stub, repo, tmp, base="HEAD")

    def test_history_timeout_rejects(self):
        tmp = _scratch(self)
        repo = tmp / "repo"
        _make_repo(repo, [("note.txt", "clean content\n")])
        stub = _write_stub(tmp)
        env = {"STUB_STATUS": "0", "STUB_REPORT": "[]", "STUB_SLEEP": "5"}
        with mock.patch.dict(os.environ, env):
            with mock.patch.object(gate, "SCAN_TIMEOUT", 1):
                with self.assertRaises(gate.GateFailure):
                    gate.run_history(stub, repo, tmp, base="HEAD")

    def test_history_argv_shape(self):
        tmp = _scratch(self)
        repo = tmp / "repo"
        _make_repo(repo, [("note.txt", "clean content\n")])
        stub = _write_stub(tmp)
        log = tmp / "argv.log"
        env = {"STUB_STATUS": "0", "STUB_REPORT": "[]",
               "STUB_ARGV_LOG": str(log)}
        with mock.patch.dict(os.environ, env):
            _stdout_of(gate.run_history, stub, repo, tmp, base="")
        calls = [json.loads(line) for line in
                 log.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(calls), 2)
        argv, tree_argv = calls
        head = _run_git(repo, "rev-parse", "HEAD", env=_git_env()).stdout.strip()
        self.assertIn("--log-opts=--diff-merges=separate --root --text " + head, argv)
        self.assertIn("--no-git", tree_argv)
        self.assertEqual(tree_argv[tree_argv.index("--source") + 1], ".")
        self.assertEqual(argv[0], "detect")
        self.assertNotIn("--no-git", argv)
        self.assertIn("--source", argv)
        history_source = Path(argv[argv.index("--source") + 1])
        self.assertEqual(history_source.parent, tmp)
        self.assertNotEqual(history_source, repo)
        self.assertIn("--redact", argv)
        self.assertIn("--no-banner", argv)
        self.assertEqual(argv[argv.index("--exit-code") + 1], "1")
        self.assertEqual(argv[argv.index("--report-format") + 1], "json")
        self.assertTrue(argv[argv.index("--config") + 1].endswith(".toml"))
        self.assertTrue(argv[argv.index("--report-path") + 1].endswith(".json"))

    def test_release_scans_isolate_automatic_ignore_policy(self):
        tmp = _scratch(self)
        repo = tmp / "repo"
        filename = ".gitleaksignore"
        checked_in = "# tracked fixture\n"
        _make_repo(repo, [(filename, checked_in), ("note.txt", "clean\n")])
        (repo / filename).write_text("# checkout fixture\n")
        stub = _write_stub(tmp)
        original = gate._run_scanner
        calls = []

        def inspect(argv, report, **kwargs):
            cwd = kwargs.get("cwd") or repo
            source = cwd / argv[argv.index("--source") + 1]
            self.assertFalse((source / filename).is_file())
            ignored = Path(argv[argv.index("--gitleaks-ignore-path") + 1])
            self.assertEqual(ignored.read_bytes(), b"")
            calls.append(source.read_text() if source.is_file() else None)
            return original(argv, report, **kwargs)

        with mock.patch.object(gate, "_run_scanner", side_effect=inspect):
            _stdout_of(gate.run_history, stub, repo, tmp, base="")
        self.assertEqual(calls, [None, checked_in, None])
        self.assertEqual((repo / filename).read_text(), "# checkout fixture\n")

    def test_release_legs_disable_inline_suppressions(self):
        tmp = _scratch(self)
        repo = tmp / "repo"
        _make_repo(repo, [("note.txt", "clean\n")])
        stub = _write_stub(tmp)
        log = tmp / "argv.log"
        with mock.patch.dict(os.environ, {"STUB_ARGV_LOG": str(log)}):
            _stdout_of(gate.run_history, stub, repo, tmp, base="")
        calls = [json.loads(line) for line in log.read_text().splitlines()]
        self.assertEqual(len(calls), 2)
        for argv in calls:
            self.assertIn("--ignore-gitleaks-allow", argv)

    def test_workflow_always_uses_pinned_history_scope(self):
        workflow = WORKFLOW.read_text()
        gate_step = workflow.split("      - name: Secrets gate", 1)[1]
        gate_step = gate_step.split("      - name:", 1)[0]
        self.assertIn("if: always()", gate_step)
        self.assertIn('--candidate "$GITHUB_SHA"', gate_step)
        self.assertNotIn("github.event.before", gate_step)
        self.assertNotIn("--base", gate_step)

    def test_automatic_release_policy_matches_checked_in_policy(self):
        self.assertEqual(gate.CONFIG_TEXT.encode("ascii"),
                         (REPO_ROOT / ".gitleaks.toml").read_bytes())
        self.assertNotIn("--config", HOOK.read_text())
        self.assertNotIn("--config", WORKFLOW.read_text())

    def test_history_not_a_repo_rejects(self):
        tmp = _scratch(self)
        plain = tmp / "plain"
        plain.mkdir()
        stub = _write_stub(tmp)
        with mock.patch.dict(os.environ, {"STUB_STATUS": "0"}):
            with self.assertRaises(gate.GateFailure):
                gate.run_history(stub, plain, tmp)

    def test_history_empty_repo_rejects(self):
        _require_git()
        tmp = _scratch(self)
        repo = tmp / "repo"
        repo.mkdir(parents=True)
        env = _git_env()
        self.assertEqual(
            _run_git(repo, "init", "-q", env=env).returncode, 0)
        stub = _write_stub(tmp)
        with mock.patch.dict(os.environ, {"STUB_STATUS": "0"}):
            with self.assertRaises(gate.GateFailure):
                gate.run_history(stub, repo, tmp, base="HEAD")

    def test_history_shallow_rejects_before_scanner(self):
        _require_git()
        tmp = _scratch(self)
        src = tmp / "src"
        _make_repo(src, [("note.txt", "clean\n")])
        clone = tmp / "clone"
        proc = subprocess.run(
            ["git", "clone", "-q", "--depth", "1",
             "file://" + str(src), str(clone)],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, timeout=60, env=_git_env())
        self.assertEqual(proc.returncode, 0)
        shallow = _run_git(
            clone, "rev-parse", "--is-shallow-repository", env=_git_env())
        self.assertEqual(shallow.stdout.strip(), "true")
        stub = _write_stub(tmp)
        log = tmp / "argv.log"
        env = {"STUB_STATUS": "0", "STUB_REPORT": "[]",
               "STUB_ARGV_LOG": str(log)}
        with mock.patch.dict(os.environ, env):
            with self.assertRaises(gate.GateFailure):
                gate.run_history(stub, clone, tmp)
        self.assertFalse(log.exists())

    def test_controls_all_detected_pass(self):
        tmp = _scratch(self)
        repo = tmp / "repo"
        _write_context_repo(repo)
        stub = _write_stub(tmp)
        env = {"STUB_NOGIT_STATUS": "7", "STUB_NOGIT_REPORT": _finding()}
        with mock.patch.dict(os.environ, env):
            text = _stdout_of(gate.run_controls, stub, repo, tmp)
        for control in ("github", "entropy", "appended", "pem"):
            self.assertIn(
                "control " + control + ": detected (1 finding(s))", text)
        self.assertIn("controls: clean", text)

    def test_controls_single_blind_rejects_after_running_all(self):
        tmp = _scratch(self)
        repo = tmp / "repo"
        _write_context_repo(repo)
        stub = _write_stub(tmp)
        env = {"STUB_NOGIT_STATUS": "7", "STUB_NOGIT_REPORT": _finding(),
               "STUB_BLIND": "entropy"}
        with mock.patch.dict(os.environ, env):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                with self.assertRaises(gate.GateFailure):
                    gate.run_controls(stub, repo, tmp)
        text = buf.getvalue()
        self.assertIn("control entropy: NOT detected", text)
        for control in ("github", "appended", "pem"):
            self.assertIn("control " + control + ": detected", text)

    def test_controls_stale_report_is_deleted_before_run(self):
        tmp = _scratch(self)
        repo = tmp / "repo"
        _write_context_repo(repo)
        stub = _write_stub(tmp)
        stale = tmp / "control-github.json"
        stale.write_text(_finding(), encoding="utf-8")
        env = {"STUB_NOGIT_STATUS": "7", "STUB_NOGIT_REPORT": _finding(),
               "STUB_NO_WRITE": "1"}
        with mock.patch.dict(os.environ, env):
            with self.assertRaises(gate.GateFailure):
                gate.run_controls(stub, repo, tmp)
        self.assertFalse(stale.exists())

    def test_controls_crash_with_findings_rejects(self):
        tmp = _scratch(self)
        repo = tmp / "repo"
        _write_context_repo(repo)
        stub = _write_stub(tmp)
        env = {"STUB_NOGIT_STATUS": "2", "STUB_NOGIT_REPORT": _finding()}
        with mock.patch.dict(os.environ, env):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                with self.assertRaises(gate.GateFailure):
                    gate.run_controls(stub, repo, tmp)
        self.assertIn("control github: NOT detected", buf.getvalue())

    def test_controls_clean_status_with_findings_rejects(self):
        tmp = _scratch(self)
        repo = tmp / "repo"
        _write_context_repo(repo)
        stub = _write_stub(tmp)
        env = {"STUB_NOGIT_STATUS": "0", "STUB_NOGIT_REPORT": _finding()}
        with mock.patch.dict(os.environ, env):
            with self.assertRaises(gate.GateFailure):
                gate.run_controls(stub, repo, tmp)

    def test_controls_missing_context_rejects(self):
        tmp = _scratch(self)
        repo = tmp / "repo"
        repo.mkdir()
        stub = _write_stub(tmp)
        with mock.patch.dict(os.environ, {"STUB_NOGIT_STATUS": "7"}):
            with self.assertRaises(gate.GateFailure):
                gate.run_controls(stub, repo, tmp)

    def test_controls_context_without_checksum_rejects(self):
        tmp = _scratch(self)
        repo = tmp / "repo"
        workflow = repo / ".github" / "workflows" / "secrets.yml"
        workflow.parent.mkdir(parents=True, exist_ok=True)
        workflow.write_text("name: Secrets\n# no digest here\n",
                            encoding="utf-8")
        stub = _write_stub(tmp)
        with mock.patch.dict(os.environ, {"STUB_NOGIT_STATUS": "7"}):
            with self.assertRaises(gate.GateFailure):
                gate.run_controls(stub, repo, tmp)

    def test_controls_argv_shape(self):
        tmp = _scratch(self)
        repo = tmp / "repo"
        _write_context_repo(repo)
        stub = _write_stub(tmp)
        log = tmp / "argv.log"
        env = {"STUB_NOGIT_STATUS": "7", "STUB_NOGIT_REPORT": _finding(),
               "STUB_ARGV_LOG": str(log)}
        with mock.patch.dict(os.environ, env):
            _stdout_of(gate.run_controls, stub, repo, tmp)
        calls = [json.loads(line) for line in
                 log.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(calls), 4)
        configs = set()
        reports = set()
        sources = set()
        for argv in calls:
            self.assertIn("--no-git", argv)
            self.assertEqual(argv[argv.index("--exit-code") + 1], "7")
            sources.add(os.path.basename(argv[argv.index("--source") + 1]))
            configs.add(argv[argv.index("--config") + 1])
            reports.add(argv[argv.index("--report-path") + 1])
        self.assertEqual(sources, {"github", "entropy", "appended", "pem"})
        self.assertEqual(len(configs), 1)
        self.assertEqual(len(reports), 4)

    def test_generate_shapes(self):
        tmp = _scratch(self)
        context = tmp / "ctx" / "secrets.yml"
        context.parent.mkdir(parents=True, exist_ok=True)
        line = "pinned " + gate.GITLEAKS_SHA256 + " end"
        context.write_text("first\n" + line + "\nlast\n", encoding="utf-8")
        result = gate.generate_controls(tmp / "plants", context)
        self.assertEqual(set(result), {"github", "entropy", "appended", "pem"})
        self.assertEqual(len({p.resolve() for p in result.values()}), 4)
        github = (result["github"] / "creds.txt").read_text(encoding="utf-8")
        head = 'token = "' + _TOKEN_A + _TOKEN_B
        self.assertTrue(github.startswith(head))
        self.assertTrue(github.endswith('"\n'))
        suffix = github[len(head):-2]
        self.assertEqual(len(suffix), 36)
        alphabet = string.ascii_letters + string.digits
        self.assertTrue(all(c in alphabet for c in suffix))
        entropy = (result["entropy"] / "settings.txt").read_text(
            encoding="utf-8")
        key_head = 'secret_key = "'
        self.assertTrue(entropy.startswith(key_head))
        value = entropy[len(key_head):-2]
        self.assertEqual(value, gate._entropy_control())
        self.assertEqual(len(set(value)), len(value))
        self.assertGreater(gate._shannon_entropy(value), 5.0)
        appended = (result["appended"] / "appended.txt").read_text(
            encoding="utf-8")
        self.assertTrue(appended.startswith(line[:200] + " "))
        tail = appended[len(line[:200]) + 1:-1]
        self.assertTrue(tail.startswith(_token_prefix()))
        self.assertEqual(len(tail), 4 + 36)
        pem = (result["pem"] / "key.pem").read_text(
            encoding="utf-8").splitlines()
        self.assertEqual(pem[0], "-----BEGIN RS" + "A PRIVATE KEY-----")
        self.assertEqual(pem[-1], "-----END RS" + "A PRIVATE KEY-----")
        body = "".join(pem[1:-1])
        self.assertTrue(len(body) > 0)
        base64.b64decode(body)
        for control in result.values():
            self.assertEqual(stat.S_IMODE(os.stat(control).st_mode), 0o700)
            for child in control.iterdir():
                self.assertEqual(stat.S_IMODE(os.stat(child).st_mode), 0o600)

    def test_entropy_control_is_deterministic(self):
        self.assertEqual(gate._entropy_control(), gate._entropy_control())
        self.assertGreater(gate._shannon_entropy(gate._entropy_control()), 5.0)

    def test_generate_missing_or_empty_context_rejects(self):
        tmp = _scratch(self)
        with self.assertRaises(gate.GateFailure):
            gate.generate_controls(tmp / "a", tmp / "absent.yml")
        empty = tmp / "empty.yml"
        empty.write_text("  \n", encoding="utf-8")
        with self.assertRaises(gate.GateFailure):
            gate.generate_controls(tmp / "b", empty)

    def test_generate_context_without_checksum_rejects(self):
        tmp = _scratch(self)
        context = tmp / "secrets.yml"
        context.write_text("name: Secrets\nnothing to see\n", encoding="utf-8")
        with self.assertRaises(gate.GateFailure):
            gate.generate_controls(tmp / "plants", context)

    def test_generate_symlink_destination_rejects(self):
        tmp = _scratch(self)
        context = tmp / "secrets.yml"
        context.write_text("digest " + gate.GITLEAKS_SHA256 + "\n",
                           encoding="utf-8")
        real = tmp / "real"
        real.mkdir()
        link = tmp / "link"
        os.symlink(real, link)
        with self.assertRaises(gate.GateFailure):
            gate.generate_controls(link, context)

    def test_generate_twice_into_same_destination_rejects(self):
        tmp = _scratch(self)
        context = tmp / "secrets.yml"
        context.write_text("digest " + gate.GITLEAKS_SHA256 + "\n",
                           encoding="utf-8")
        gate.generate_controls(tmp / "plants", context)
        with self.assertRaises(gate.GateFailure):
            gate.generate_controls(tmp / "plants", context)

    def test_summarize_counts_sorted_sanitized(self):
        tmp = _scratch(self)
        report = tmp / "r.json"
        entries = [
            {"RuleID": "zeta-rule"}, {"RuleID": "bad rule!"},
            {"RuleID": "alpha"}, {"RuleID": "zeta-rule"},
            {"RuleID": "../x"}, {"RuleID": ""},
        ]
        report.write_text(json.dumps(entries), encoding="utf-8")
        self.assertEqual(
            gate.summarize_findings(report),
            [("unknown", 3), ("zeta-rule", 2), ("alpha", 1)])

    def test_summarize_rejects_defects(self):
        tmp = _scratch(self)
        with self.assertRaises(gate.GateFailure):
            gate.summarize_findings(tmp / "absent.json")
        bad = ['{oops', '{"a": 1}', '["x"]', '[{"RuleID": 7}]',
               '[{"RuleID": null}]', '[42]']
        for pos, body in enumerate(bad):
            with self.subTest(body=body):
                report = tmp / ("r%d.json" % pos)
                report.write_text(body, encoding="utf-8")
                with self.assertRaises(gate.GateFailure):
                    gate.summarize_findings(report)

    def test_workflow_identity(self):
        text = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("name: Secrets", text)
        self.assertRegex(text, r"(?m)^  secrets:$")
        for token in ("push:", "pull_request", "workflow_dispatch",
                      "permissions:", "contents: read",
                      "runs-on: ubuntu-24.04", "timeout-minutes: 15",
                      "fetch-depth: 0", "--max-time 120",
                      "sha256sum -c -", "tar -xzf",
                      "unittest discover -s tests/fixtures/secrets",
                      "gate.py gate --gitleaks", "$RUNNER_TEMP/gitleaks",
                      "$GITHUB_WORKSPACE", gate.GITLEAKS_SHA256,
                      gate.GITLEAKS_URL,
                      "11bd71901bbe5b1630ceea73d27597364c9af683",
                      "# v4.2.2"):
            self.assertIn(token, text)
        for banned in ("ubuntu-latest", "continue-on-error", "|| true",
                       "upload-artifact", "gl.json", "@v", "@main", "@master"):
            self.assertNotIn(banned, text)
        uses = [line.strip() for line in text.splitlines()
                if re.match(r"\s*-\s+uses:", line)]
        self.assertTrue(uses)
        for line in uses:
            self.assertRegex(line, r"uses:\s*\S+@[0-9a-f]{40}\b")
        curl_at = text.index("curl ")
        sum_at = text.index("sha256sum -c -")
        tar_at = text.index("tar -xzf")
        self.assertLess(curl_at, sum_at)
        self.assertLess(sum_at, tar_at)

    def test_pin_constants(self):
        self.assertEqual(gate.GITLEAKS_VERSION, "v8.18.4")
        digest = gate.GITLEAKS_SHA256
        self.assertEqual(len(digest), 64)
        int(digest, 16)
        self.assertEqual(digest[:32], "ba6dbb656933921c775ee5a2d1c13a91")
        self.assertEqual(digest[32:], "046e7952e9d919f9bac4cec61d628e7d")
        self.assertIn(digest, WORKFLOW.read_text(encoding="utf-8"))
        self.assertEqual(
            gate.GITLEAKS_URL,
            "https://github.com/gitleaks/gitleaks/releases/download/"
            "v8.18.4/gitleaks_8.18.4_linux_x64.tar.gz")

    def test_history_output_omits_report_and_scanner_bytes(self):
        tmp = _scratch(self)
        repo = tmp / "repo"
        _make_repo(repo, [("note.txt", "clean content\n")])
        stub = _write_stub(tmp)
        report = _finding(rule="SENTINEL RULE", secret="SENTINEL-SECRET-X1",
                          match="SENTINEL-MATCH-X1", file="SENTINEL-FILE-X1")
        env = {"STUB_STATUS": "1", "STUB_REPORT": report,
               "STUB_STDERR": "SENTINEL-STDERR-X1"}
        with mock.patch.dict(os.environ, env):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                with self.assertRaises(gate.GateFailure):
                    gate.run_history(stub, repo, tmp, base="HEAD")
        text = buf.getvalue()
        for sentinel in ("SENTINEL-SECRET-X1", "SENTINEL-MATCH-X1",
                         "SENTINEL-FILE-X1", "SENTINEL-STDERR-X1",
                         "SENTINEL RULE"):
            self.assertNotIn(sentinel, text)
        self.assertIn("1 unknown", text)

    def test_malformed_report_output_omits_bytes(self):
        tmp = _scratch(self)
        repo = tmp / "repo"
        _make_repo(repo, [("note.txt", "clean content\n")])
        stub = _write_stub(tmp)
        env = {"STUB_STATUS": "0", "STUB_REPORT": "{oops SENTINEL-JSON-X2"}
        with mock.patch.dict(os.environ, env):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = gate.main(["gate.py", "history", "--gitleaks",
                                str(stub), "--repo", str(repo), "--base", "HEAD"])
        self.assertEqual(rc, 1)
        self.assertEqual(out.getvalue(), "history: rejected\n")

    def test_main_missing_tool_rejects(self):
        tmp = _scratch(self)
        repo = tmp / "repo"
        repo.mkdir()
        rc = gate.main(["gate.py", "history", "--gitleaks",
                        "/nonexistent-tool-1", "--repo", str(repo)])
        self.assertEqual(rc, 1)
        plain = tmp / "plain"
        plain.write_text("x", encoding="utf-8")
        os.chmod(plain, 0o600)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = gate.main(["gate.py", "history", "--gitleaks",
                            str(plain), "--repo", str(repo)])
        self.assertEqual(rc, 1)
        self.assertEqual(out.getvalue(), "history: rejected\n")

    def test_main_malformed_returns_two(self):
        tmp = _scratch(self)
        stub = _write_stub(tmp)
        repo = tmp / "repo"
        repo.mkdir()
        good_g = ["--gitleaks", str(stub)]
        good_r = ["--repo", str(repo)]
        cases = [
            [], ["gate.py"], ["gate.py", "bogus", *good_g, *good_r],
            ["gate.py", "history"], ["gate.py", "history", *good_g],
            ["gate.py", "history", *good_g, *good_r, "extra"],
            ["gate.py", "history", "--gitleaks", "relative/bin", *good_r],
            ["gate.py", "history", *good_g, "--repo", "relative/root"],
            ["gate.py", "history", *good_g, *good_g],
            ["gate.py", "SENTINEL-ARGV-UNIQUE-9", *good_g, *good_r],
        ]
        for argv in cases:
            with self.subTest(argv=argv):
                out, err = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(out), \
                        contextlib.redirect_stderr(err):
                    rc = gate.main(argv if argv else ["gate.py"])
                self.assertEqual(rc, 2)
                self.assertIn("usage: gate.py", err.getvalue())
                self.assertNotIn("SENTINEL-ARGV-UNIQUE-9",
                                 out.getvalue() + err.getvalue())

    def test_main_help(self):
        for argv in (["gate.py", "--help"], ["gate.py", "-h"]):
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), \
                    contextlib.redirect_stderr(err):
                rc = gate.main(argv)
            self.assertEqual(rc, 0)
            self.assertIn("usage: gate.py", out.getvalue())

    def test_main_gate_clean(self):
        tmp = _scratch(self)
        repo = tmp / "repo"
        _make_repo(repo, [("note.txt", "clean\n")])
        _write_context_repo(repo)
        stub = _write_stub(tmp)
        env = {"STUB_STATUS": "0", "STUB_REPORT": "[]",
               "STUB_NOGIT_STATUS": "7", "STUB_NOGIT_REPORT": _finding()}
        with mock.patch.dict(os.environ, env):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = gate.main(["gate.py", "gate", "--gitleaks",
                                str(stub), "--repo", str(repo), "--base", "HEAD"])
        self.assertEqual(rc, 0)
        self.assertIn("gate: clean", out.getvalue())

    def test_main_gate_runs_controls_after_history_failure(self):
        tmp = _scratch(self)
        repo = tmp / "repo"
        _make_repo(repo, [("note.txt", "clean\n")])
        _write_context_repo(repo)
        stub = _write_stub(tmp)
        env = {"STUB_STATUS": "1", "STUB_REPORT": _finding(),
               "STUB_NOGIT_STATUS": "7", "STUB_NOGIT_REPORT": _finding()}
        with mock.patch.dict(os.environ, env):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = gate.main(["gate.py", "gate", "--gitleaks",
                                str(stub), "--repo", str(repo), "--base", "HEAD"])
        self.assertEqual(rc, 1)
        text = out.getvalue()
        self.assertIn("control github: detected", text)
        self.assertIn("gate: rejected", text)

    def test_main_internal_error_stays_fixed(self):
        tmp = _scratch(self)
        stub = _write_stub(tmp)
        repo = tmp / "repo"
        repo.mkdir()
        with mock.patch.object(
                gate, "run_history",
                side_effect=RuntimeError("SENTINEL-REPR-LEAK")):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = gate.main(["gate.py", "history", "--gitleaks",
                                str(stub), "--repo", str(repo), "--base", "HEAD"])
        self.assertEqual(rc, 1)
        text = out.getvalue()
        self.assertIn("history: internal error", text)
        self.assertNotIn("SENTINEL-REPR-LEAK", text)
        self.assertNotIn("RuntimeError", text)

    def test_main_cleans_scratch_on_success_and_failure(self):
        tmp = _scratch(self)
        repo = tmp / "repo"
        _make_repo(repo, [("note.txt", "clean\n")])
        stub = _write_stub(tmp)
        created = []
        real_mkdtemp = tempfile.mkdtemp

        def recording(*args, **kwargs):
            path = real_mkdtemp(*args, **kwargs)
            created.append(Path(path))
            return path

        argv = ["gate.py", "history", "--gitleaks", str(stub),
                "--repo", str(repo), "--base", "HEAD"]
        with mock.patch.object(tempfile, "mkdtemp", side_effect=recording):
            with mock.patch.dict(os.environ, {"STUB_STATUS": "0",
                                               "STUB_REPORT": "[]"}):
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(gate.main(argv), 0)
            with mock.patch.dict(os.environ, {"STUB_STATUS": "1",
                                               "STUB_REPORT": _finding()}):
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(gate.main(argv), 1)
        self.assertEqual(len(created), 5)  # Two runs, two history sources and one tree.
        for path in created:
            self.assertFalse(path.exists())
            self.assertNotIn(REPO_ROOT.resolve(), path.resolve().parents)

    def test_config_bytes_identical_across_modes(self):
        tmp = _scratch(self)
        repo = tmp / "repo"
        _make_repo(repo, [("note.txt", "clean\n")])
        _write_context_repo(repo)
        stub = _write_stub(tmp)
        env = {"STUB_STATUS": "0", "STUB_REPORT": "[]",
               "STUB_NOGIT_STATUS": "7", "STUB_NOGIT_REPORT": _finding()}
        with mock.patch.dict(os.environ, env):
            with contextlib.redirect_stdout(io.StringIO()):
                gate.run_history(stub, repo, tmp, base="HEAD")
                gate.run_controls(stub, repo, tmp)
        cfgs = sorted(tmp.glob("gitleaks-*.toml"))
        self.assertEqual(len(cfgs), 2)
        for cfg in cfgs:
            self.assertEqual(cfg.read_bytes(),
                             gate.CONFIG_TEXT.encode("ascii"))

    def test_constant_policy_matches_root_without_loading_it(self):
        self.assertEqual((REPO_ROOT / ".gitleaks.toml").read_bytes(),
                         gate.CONFIG_TEXT.encode("ascii"))
        config = tomllib.loads(gate.CONFIG_TEXT)
        self.assertTrue(config["extend"]["useDefault"])
        self.assertNotIn("allowlist", config)
        self.assertEqual(len(config["rules"]), 6)
        for rule in config["rules"][:4]:
            if "path" in rule:
                self.assertEqual(rule["allowlist"], {
                    "regexTarget": "secret", "regexes": ["^[0-9a-f]{64}$"]})
            else:
                self.assertEqual(list(rule["allowlist"]), ["paths"])

        generic, recipe = config["rules"][-2:]
        self.assertEqual(generic["id"], "generic-api-key")
        self.assertEqual(recipe["id"], "generic-api-key-vm-recipe")
        self.assertEqual(generic["allowlist"]["paths"], [recipe["path"]])
        self.assertEqual(recipe["allowlist"]["regexTarget"], "secret")
        self.assertEqual(generic["allowlist"]["stopwords"], recipe["allowlist"]["stopwords"])
        self.assertEqual(len(recipe["allowlist"]["regexes"]), 1)

    def _hook_bin(self, tmp, stub=None, with_python=True, with_git=True):
        bindir = tmp / "bin"
        bindir.mkdir(parents=True, exist_ok=True)
        if with_python:
            python = shutil.which("python3") or sys.executable
            os.symlink(python, bindir / "python3")
        if with_git:
            real_git = shutil.which("git")
            if real_git is None:
                raise AssertionError("git is required on PATH")
            os.symlink(real_git, bindir / "git")
        if stub is not None:
            os.symlink(stub, bindir / "gitleaks")
        return bindir

    def _hook_repo(self, tmp):
        repo = tmp / "hookrepo"
        fixture = repo / "tests" / "fixtures" / "secrets"
        fixture.mkdir(parents=True, exist_ok=True)
        shutil.copy(_HERE / "gate.py", fixture / "gate.py")
        _write_context_repo(repo)
        (repo / "note.txt").write_text("clean\n", encoding="utf-8")
        _make_repo(repo)
        shutil.copyfile(REPO_ROOT / ".gitleaks.toml", repo / ".gitleaks.toml")
        return repo

    def _hook_head_update(self, repo):
        head = _run_git(repo, "rev-parse", "HEAD", env=_git_env()).stdout.strip()
        return f"refs/heads/local {head} refs/heads/main {head}\n"

    def _run_hook(self, bindir, cwd, *args, extra_env=None, ref_updates=None):
        env = dict(os.environ)
        env["PATH"] = str(bindir)
        if extra_env:
            env.update(extra_env)
        return subprocess.run(
            [str(HOOK), *args], cwd=str(cwd), input=ref_updates,
            stdin=subprocess.DEVNULL if ref_updates is None else None,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=120, env=env)

    def _assert_safe_hook_mode(self, path):
        mode = stat.S_IMODE(os.stat(path).st_mode)
        self.assertTrue(mode & stat.S_IXUSR, "hook must be owner-executable")
        self.assertEqual(mode & stat.S_IWOTH, 0, "hook must not be world-writable")

    def test_hook_mode_negative_controls(self):
        hook = _scratch(self) / "pre-push"
        hook.write_text("#!/bin/sh\n")
        for mode in (0o755, 0o775):
            hook.chmod(mode)
            self._assert_safe_hook_mode(hook)
        for mode in (0o777, 0o664):
            hook.chmod(mode)
            with self.assertRaises(AssertionError):
                self._assert_safe_hook_mode(hook)

    def test_hook_is_executable_posix(self):
        self._assert_safe_hook_mode(HOOK)
        proc = subprocess.run(
            ["sh", "-n", str(HOOK)], stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
        self.assertEqual(proc.returncode, 0)
        text = HOOK.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("#!/bin/sh\n"))
        self.assertIn("set -eu", text)
        self.assertIn("tests/fixtures/secrets/gate.py gate --gitleaks", text)
        self.assertIn(
            "install -m 755 .githooks/pre-push .git/hooks/pre-push", text)
        self.assertNotIn("publication_lint", text)
        self.assertNotIn("claims_check", text)

    def test_hook_clean_passes_from_nested_cwd(self):
        tmp = _scratch(self)
        repo = self._hook_repo(tmp)
        nested = repo / "a" / "b"
        nested.mkdir(parents=True)
        stub = _write_stub(tmp)
        bindir = self._hook_bin(tmp, stub)
        log = tmp / "argv.log"
        env = {"STUB_STATUS": "0", "STUB_REPORT": "[]",
               "STUB_NOGIT_STATUS": "7", "STUB_NOGIT_REPORT": _finding(),
               "STUB_ARGV_LOG": str(log)}
        proc = self._run_hook(bindir, nested, "origin",
                              "git@example.invalid:x.git", extra_env=env,
                              ref_updates=self._hook_head_update(repo))
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        self.assertIn("gate: clean", proc.stdout)
        calls = [json.loads(line) for line in
                 log.read_text(encoding="utf-8").splitlines()]
        histories = [c for c in calls if "--no-git" not in c]
        self.assertEqual(len(histories), 1)
        self.assertNotEqual(
            histories[0][histories[0].index("--source") + 1], str(repo))
        for argv in calls[:2]:
            config = Path(argv[argv.index("--config") + 1])
            self.assertEqual(config.name, "gitleaks-history.toml")
            self.assertNotEqual(config.parent, repo)

    def test_hook_scans_outgoing_candidate_against_advertised_main(self):
        tmp = _scratch(self)
        repo = self._hook_repo(tmp)
        env = _git_env()
        base = _run_git(repo, "rev-parse", "HEAD", env=env).stdout.strip()
        self.assertEqual(_run_git(repo, "checkout", "-qb", "release", env=env).returncode, 0)
        self.assertEqual(
            _run_git(repo, "commit", "--allow-empty", "-qm", "release", env=env).returncode, 0)
        candidate = _run_git(repo, "rev-parse", "HEAD", env=env).stdout.strip()
        self.assertEqual(_run_git(repo, "checkout", "-q", "--detach", base, env=env).returncode, 0)
        stub = _write_stub(tmp)
        log = tmp / "argv.log"
        proc = self._run_hook(
            self._hook_bin(tmp, stub), repo, "origin", "url",
            extra_env={"STUB_ARGV_LOG": str(log), "STUB_NOGIT_STATUS": "7",
                       "STUB_NOGIT_REPORT": _finding()},
            ref_updates=f"refs/heads/release {candidate} refs/heads/main {base}\n")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        history = json.loads(log.read_text(encoding="utf-8").splitlines()[0])
        self.assertIn(f"--log-opts=--diff-merges=separate --root --text {base}..{candidate}", history)

    def test_hook_uses_each_destination_tip(self):
        tmp = _scratch(self)
        repo = self._hook_repo(tmp)
        env = _git_env()
        base = _run_git(repo, "rev-parse", "HEAD", env=env).stdout.strip()
        result = _run_git(repo, "commit", "--allow-empty", "-qm", "candidate", env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        candidate = _run_git(repo, "rev-parse", "HEAD", env=env).stdout.strip()
        result = _run_git(repo, "update-ref", "refs/remotes/origin/main", candidate, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        bindir = self._hook_bin(tmp, _write_stub(tmp))
        log = tmp / "argv.log"
        zero = "0" * len(base)
        for remote, ref, old in (("public", "refs/heads/main", base),
                                 ("url", "refs/heads/main", base),
                                 ("origin", "refs/heads/release", base),
                                 ("origin", "refs/tags/fixture", zero)):
            with self.subTest(remote=remote, ref=ref):
                log.unlink(missing_ok=True)
                proc = self._run_hook(
                    bindir, repo, remote, "url",
                    extra_env={"STUB_ARGV_LOG": str(log), "STUB_NOGIT_STATUS": "7",
                               "STUB_NOGIT_REPORT": _finding()},
                    ref_updates=f"refs/heads/local {candidate} {ref} {old}\n")
                self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                history = json.loads(log.read_text().splitlines()[0])
                revisions = candidate if old == zero else f"{old}..{candidate}"
                self.assertTrue(any(arg.startswith("--log-opts=") and
                                    arg.endswith(" " + revisions) for arg in history))

    def test_hook_missing_scanner_rejects(self):
        tmp = _scratch(self)
        repo = self._hook_repo(tmp)
        bindir = self._hook_bin(tmp)
        proc = self._run_hook(bindir, repo, "origin", "url")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("gitleaks", proc.stderr)

    def test_hook_missing_python_rejects(self):
        tmp = _scratch(self)
        repo = self._hook_repo(tmp)
        stub = _write_stub(tmp)
        bindir = self._hook_bin(tmp, stub, with_python=False)
        proc = self._run_hook(bindir, repo, "origin", "url")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("python3", proc.stderr)

    def test_hook_missing_git_rejects(self):
        tmp = _scratch(self)
        repo = self._hook_repo(tmp)
        stub = _write_stub(tmp)
        bindir = self._hook_bin(tmp, stub, with_git=False)
        proc = self._run_hook(bindir, repo, "origin", "url")
        self.assertEqual(proc.returncode, 1)

    def test_hook_finding_rejects_without_secret_bytes(self):
        tmp = _scratch(self)
        repo = self._hook_repo(tmp)
        stub = _write_stub(tmp)
        bindir = self._hook_bin(tmp, stub)
        report = _finding(secret="SENTINEL-HOOK-SECRET-1")
        env = {"STUB_STATUS": "1", "STUB_REPORT": report,
               "STUB_NOGIT_STATUS": "7", "STUB_NOGIT_REPORT": _finding()}
        proc = self._run_hook(bindir, repo, "origin", "url", extra_env=env,
                              ref_updates=self._hook_head_update(repo))
        self.assertEqual(proc.returncode, 1)
        combined = proc.stdout + proc.stderr
        self.assertNotIn("SENTINEL-HOOK-SECRET-1", combined)
        self.assertIn("pre-push: secrets gate rejected the push", combined)


class GateIntegrationTests(unittest.TestCase):
    """Pinned-tool evidence; requires absolute GITLEAKS_BIN, never skips."""

    @classmethod
    def setUpClass(cls):
        raw = os.environ.get("GITLEAKS_BIN", "")
        if not raw:
            raise AssertionError(
                "GITLEAKS_BIN is not set; pinned scanner required")
        path = Path(raw)
        if not path.is_absolute() or not path.is_file():
            raise AssertionError("GITLEAKS_BIN must be an absolute file")
        cls.gitleaks = path

    def _runtime_token_line(self):
        return ('token = "' + _token_prefix() + _token_suffix(36) + '"\n')

    def _scan_plants(self, payloads, defaults=False):
        tmp = _scratch(self)
        source = tmp / "Música"
        source.mkdir()
        for name, body in payloads:
            target = source / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(body, encoding="utf-8")
        config, report = tmp / "config.toml", tmp / "report.json"
        config.write_text("[extend]\nuseDefault = true\n" if defaults
                          else gate.CONFIG_TEXT, encoding="ascii")
        proc = subprocess.run(
            [str(self.gitleaks), "detect", "--no-git", "--source", ".",
             "--config", str(config), "--redact", "--no-banner",
             "--report-format", "json", "--report-path", str(report)],
            cwd=source, capture_output=True, timeout=120)
        self.assertIn(proc.returncode, (0, 1), "scanner failed")
        findings = json.loads(report.read_text(encoding="utf-8"))
        self.assertEqual(proc.returncode, 1 if findings else 0)
        return findings

    def test_digest_confusable_default_rules(self):
        # Measured hex-compatible keyword prefilters in pinned default rules:
        # acca, eaaa, eaac. Include unconditioned random digests as a control.
        values = [secrets.token_hex(32) for _ in range(2048)]
        for prefix in ("ac" + "ca", "ea" + "aa", "ea" + "ac"):
            values.extend(prefix + secrets.token_hex(30) for _ in range(64))
        body = "\n".join(json.dumps({"sha256": value}) for value in values)
        findings = self._scan_plants([("digests.json", body)], defaults=True)
        self.assertEqual({f["RuleID"] for f in findings},
                         {"square-access-token", "facebook-page-access-token"})

    def test_digest_exceptions_are_exact_and_path_scoped(self):
        paths = ("router/index/SHA256SUMS", "catalogue/router/manifest.json")
        for prefix in ("ea" + "aa", "ea" + "ac"):
            digest = prefix + secrets.token_hex(30)
            payloads = [(name, json.dumps({"sha256": digest}) + "\n"
                         if name.endswith(".json") else digest + "  generated.json\n")
                        for name in paths]
            baseline = self._scan_plants(payloads, defaults=True)
            self.assertEqual(len(baseline), len(paths))
            self.assertEqual(self._scan_plants(payloads), [])
            outside = ("src/checksums.txt", "router/index/SHA256SUMS.bak",
                       "catalogue/router/post/ab.json", "catalogue/router/manifestXjson",
                       "src/catalogue/router/manifest.json")
            findings = self._scan_plants([(name, digest + "\n") for name in outside])
            self.assertEqual(len(findings), len(outside))
            # Uppercase and wrong lengths must not pass the lowercase digest gate.
            for value in (digest.upper(), digest[:-1], digest[:-2]):
                findings = self._scan_plants([(name, value + "\n") for name in paths])
                self.assertEqual(len(findings), len(paths))

    def test_generated_files_still_detect_credentials(self):
        shapes = (
            ("square-access-token-generated-digest-files",
             "sq0" + "atp-" + _token_suffix(32)),
            ("square-access-token-generated-digest-files",
             "EA" + "AA" + "G" + _token_suffix(31)),
            ("facebook-page-access-token-generated-digest-files",
             "EA" + "AC" + "G" + _token_suffix(59)),
            ("github-pat", gate._github_shaped_token()),
            ("private-key", gate._pem_block()),
        )
        for rule, value in shapes:
            with self.subTest(rule=rule):
                paths = ("router/index/SHA256SUMS", "catalogue/router/manifest.json")
                # Raw PEM lines keep the scanner's multiline key syntax intact.
                findings = self._scan_plants([(name, value + "\n") for name in paths])
                self.assertEqual(len(findings), len(paths))
                self.assertEqual({f["RuleID"] for f in findings}, {rule})

    def test_current_repository_release_scan(self):
        tmp = _scratch(self)
        text = _stdout_of(gate.run_history, self.gitleaks, REPO_ROOT, tmp)
        self.assertIn("history: clean", text)

    def test_tool_version_pinned(self):
        proc = subprocess.run(
            [str(self.gitleaks), "version"],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0)
        self.assertIn("8.18.4", proc.stdout)

    def test_real_history_clean_passes(self):
        tmp = _scratch(self)
        repo = tmp / "repo"
        _make_repo(repo, [("note.txt", "clean content\n"),
                          ("doc.md", "# title\n")])
        text = _stdout_of(gate.run_history, self.gitleaks, repo, tmp, base="")
        self.assertIn("history: clean", text)

    def test_real_history_deleted_secret_rejects(self):
        _require_git()
        tmp = _scratch(self)
        repo = tmp / "repo"
        _make_repo(repo, [("key.txt", self._runtime_token_line())])
        env = _git_env()
        self.assertEqual(
            _run_git(repo, "rm", "-q", "key.txt", env=env).returncode, 0)
        self.assertEqual(
            _run_git(repo, "commit", "-qm", "remove", env=env).returncode, 0)
        with self.assertRaises(gate.GateFailure):
            with contextlib.redirect_stdout(io.StringIO()):
                gate.run_history(self.gitleaks, repo, tmp, base="")
        tip = tmp / "tip"
        shutil.copytree(repo, tip, ignore=shutil.ignore_patterns(".git"))
        report = tmp / "tip.json"
        proc = subprocess.run(
            [str(self.gitleaks), "detect", "--no-git", "--source", str(tip),
             "--redact", "--no-banner", "--exit-code", "1",
             "--report-format", "json", "--report-path", str(report)],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, timeout=120)
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(json.loads(report.read_text(encoding="utf-8")), [])

    def _release_git(self, repo, *args):
        result = _run_git(repo, *args, env=_git_env())
        self.assertEqual(result.returncode, 0, "fixture git command failed: " + args[0])
        return result.stdout.strip()

    def _release_fixture(self):
        repo = _scratch(self) / "repo"
        _make_repo(repo, [("note.txt", "clean\n"),
                          (".gitattributes", "side.txt export-ignore\n")])
        base = self._release_git(repo, "rev-parse", "HEAD")
        self._release_git(repo, "checkout", "-qb", "side")
        (repo / "side.txt").write_text(self._runtime_token_line(), encoding="utf-8")
        self._release_git(repo, "add", "side.txt")
        self._release_git(repo, "commit", "-qm", "plant fixture")
        side = self._release_git(repo, "rev-parse", "HEAD")
        self._release_git(repo, "checkout", "-qb", "candidate", base)
        (repo / "note.txt").write_text("clean candidate\n", encoding="utf-8")
        self._release_git(repo, "commit", "-qam", "candidate change")
        return repo, base, side

    def _scan_history(self, repo, *args):
        gate_args = [sys.executable, "-I", str(_HERE / "gate.py"), "history",
                     "--gitleaks", str(self.gitleaks), "--repo", str(repo), *args]
        return subprocess.run(gate_args, cwd=repo, capture_output=True, text=True, timeout=120)

    def _release_scan(self, repo, *args):
        return self._scan_history(repo, *args), _check_public(repo, *args)

    def test_unrelated_branch_passes(self):
        repo, base, _ = self._release_fixture()
        gitleaks, privacy = self._release_scan(repo, "--base", base)
        self.assertEqual(gitleaks.returncode, 0, gitleaks.stdout)
        self.assertIn("tree: clean", gitleaks.stdout)
        self.assertEqual(privacy.returncode, 0, privacy.stderr)

    def test_same_token_in_release_history_rejects_after_deletion(self):
        repo, base, side = self._release_fixture()
        self._release_git(repo, "cherry-pick", side)
        self._release_git(repo, "rm", "side.txt")
        self._release_git(repo, "commit", "-qm", "remove fixture")
        gitleaks, privacy = self._release_scan(repo, "--base", base)
        self.assertEqual(gitleaks.returncode, 1)
        self.assertIn("github-pat", gitleaks.stdout)
        self.assertNotIn("history: clean", gitleaks.stdout)
        self.assertEqual(privacy.returncode, 1)
        self.assertIn("SECRET\thistory:side.txt@", privacy.stdout)

    def test_candidate_tree_rejects_with_empty_history_range(self):
        repo, _, side = self._release_fixture()
        gitleaks, privacy = self._release_scan(repo, "--base", side, "--candidate", side)
        self.assertEqual(gitleaks.returncode, 1)
        self.assertIn("history: no commits in selected range", gitleaks.stdout)
        self.assertIn("github-pat", gitleaks.stdout)
        self.assertEqual(privacy.returncode, 1)
        self.assertIn("SECRET\tside.txt:", privacy.stdout)

    def test_candidate_tree_from_subdirectory_includes_root_blob(self):
        repo, _, candidate = self._release_fixture()
        nested = repo / "nested"
        nested.mkdir()
        revisions = ("--base", candidate, "--candidate", candidate)
        args = [sys.executable, "-I", str(_HERE / "gate.py"), "history",
                "--gitleaks", str(self.gitleaks), "--repo", ".", *revisions]
        scanner = subprocess.run(args, cwd=nested, capture_output=True,
                                 text=True, timeout=120)
        privacy = _check_public(nested, *revisions)
        with self.subTest(gate="gitleaks"):
            self.assertEqual(scanner.returncode, 1, scanner.stdout)
            self.assertIn("history: no commits in selected range", scanner.stdout)
            self.assertIn("github-pat", scanner.stdout)
        with self.subTest(gate="privacy"):
            self.assertEqual(privacy.returncode, 1, privacy.stderr)
            self.assertIn("SECRET\tside.txt:", privacy.stdout)

    def test_history_scans_deleted_binary_classified_text(self):
        repo = _scratch(self) / "repo"
        _make_repo(repo, [(".gitattributes", "fixture.txt -diff\n")])
        base = self._release_git(repo, "rev-parse", "HEAD")
        (repo / "fixture.txt").write_text(self._runtime_token_line())
        self._release_git(repo, "add", "fixture.txt")
        self._release_git(repo, "commit", "-qm", "add fixture")
        self._release_git(repo, "rm", "fixture.txt")
        self._release_git(repo, "commit", "-qm", "remove fixture")
        result = self._scan_history(repo, "--base", base)
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("1 github-pat", result.stdout)

    def test_nul_blob_detected_in_candidate_tree_and_history(self):
        repo = _scratch(self) / "repo"
        _make_repo(repo, [("note.txt", "clean\n")])
        base = self._release_git(repo, "rev-parse", "HEAD")
        (repo / "blob.txt").write_text("\0\n" + self._runtime_token_line())
        self._release_git(repo, "add", "blob.txt")
        self._release_git(repo, "commit", "-qm", "add fixture")
        tree = self._scan_history(repo, "--base", "HEAD")
        self.assertEqual(tree.returncode, 1, tree.stdout)
        self.assertIn("1 github-pat", tree.stdout)
        self._release_git(repo, "rm", "blob.txt")
        self._release_git(repo, "commit", "-qm", "remove fixture")
        history = self._scan_history(repo, "--base", base)
        self.assertEqual(history.returncode, 1, history.stdout)
        self.assertIn("1 github-pat", history.stdout)

    def test_candidate_ignore_file_is_still_scanned_as_content(self):
        repo = _scratch(self) / "repo"
        _make_repo(repo, [(".gitleaksignore", self._runtime_token_line())])
        result = self._scan_history(repo, "--base", "HEAD")
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("history: no commits in selected range", result.stdout)
        self.assertIn("1 github-pat", result.stdout)

    def test_merge_resolution_secret_rejects_after_deletion(self):
        repo = _scratch(self) / "repo"
        _make_repo(repo, [("merge.txt", "initial\n")])
        base = self._release_git(repo, "rev-parse", "HEAD")
        self._release_git(repo, "checkout", "-qb", "side")
        (repo / "merge.txt").write_text("side content\n")
        self._release_git(repo, "commit", "-qam", "side change")
        self._release_git(repo, "checkout", "-qb", "candidate", base)
        (repo / "merge.txt").write_text("candidate content\n")
        self._release_git(repo, "commit", "-qam", "candidate change")
        merge = _run_git(repo, "merge", "--no-commit", "side", env=_git_env())
        self.assertEqual(merge.returncode, 1)
        self.assertEqual(self._release_git(repo, "diff", "--name-only", "--diff-filter=U"),
                         "merge.txt")
        (repo / "merge.txt").write_text(self._runtime_token_line())
        self._release_git(repo, "add", "--", "merge.txt")
        self._release_git(repo, "commit", "-qm", "resolve conflict")
        self._release_git(repo, "rm", "--", "merge.txt")
        self._release_git(repo, "commit", "-qm", "remove fixture")
        scanner, privacy = self._release_scan(repo, "--base", base)
        self.assertEqual(scanner.returncode, 1, scanner.stdout)
        self.assertIn("github-pat", scanner.stdout)
        self.assertEqual(privacy.returncode, 1, privacy.stderr)
        self.assertIn("SECRET\thistory:merge.txt@", privacy.stdout)

    def test_privacy_rejects_restored_base_reachable_blob_after_deletion(self):
        repo = _scratch(self) / "repo"
        _make_repo(repo, [("fixture.txt", self._runtime_token_line())])
        fixture = self._release_git(repo, "rev-parse", "HEAD")
        blob = self._release_git(repo, "rev-parse", f"{fixture}:fixture.txt")
        self._release_git(repo, "rm", "--", "fixture.txt")
        self._release_git(repo, "commit", "-qm", "remove before base")
        base = self._release_git(repo, "rev-parse", "HEAD")
        self._release_git(repo, "checkout", fixture, "--", "fixture.txt")
        self._release_git(repo, "commit", "-qm", "restore fixture")
        self.assertEqual(self._release_git(repo, "rev-parse", "HEAD:fixture.txt"), blob)
        self._release_git(repo, "rm", "--", "fixture.txt")
        self._release_git(repo, "commit", "-qm", "remove after base")
        privacy = _check_public(repo, "--base", base)
        self.assertEqual(privacy.returncode, 1, privacy.stderr)
        self.assertIn(f"SECRET\thistory:fixture.txt@{blob[:10]}:", privacy.stdout)

    def test_supplied_policy_rule_is_enforced_on_both_release_legs(self):
        tmp = _scratch(self)
        repo = tmp / "repo"
        _make_repo(repo, [("note.txt", "clean\n")])
        base = self._release_git(repo, "rev-parse", "HEAD")
        config = tmp / "release policy.toml"
        config.write_text("[extend]\nuseDefault = true\n"
                          "[[rules]]\nid = 'review-witness'\n"
                          "regex = 'review-witness-[a-f0-9]{20}'\n")
        (repo / "fixture.txt").write_text("review-witness-" + secrets.token_hex(10) + "\n")
        self._release_git(repo, "add", "--", "fixture.txt")
        self._release_git(repo, "commit", "-qm", "add witness")
        tip = self._release_git(repo, "rev-parse", "HEAD")
        for leg, release_base in (("history", base), ("tree", tip)):
            with self.subTest(leg=leg):
                default = self._scan_history(repo, "--base", release_base)
                self.assertEqual(default.returncode, 0, default.stdout)
                scanner = self._scan_history(repo, "--base", release_base,
                                             "--config", str(config))
                self.assertEqual(scanner.returncode, 1, scanner.stdout)
                self.assertIn("1 review-witness", scanner.stdout)
                self.assertEqual("history: no commits in selected range" in scanner.stdout,
                                 leg == "tree")

    def test_real_shallow_rejects(self):
        _require_git()
        tmp = _scratch(self)
        src = tmp / "src"
        _make_repo(src, [("note.txt", "clean\n")])
        clone = tmp / "clone"
        proc = subprocess.run(
            ["git", "clone", "-q", "--depth", "1",
             "file://" + str(src), str(clone)],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, timeout=60, env=_git_env())
        self.assertEqual(proc.returncode, 0)
        with self.assertRaises(gate.GateFailure):
            gate.run_history(self.gitleaks, clone, tmp)

    def test_real_controls_all_detected(self):
        tmp = _scratch(self)
        repo = tmp / "repo"
        _write_context_repo(repo)
        text = _stdout_of(gate.run_controls, self.gitleaks, repo, tmp)
        for control in ("github", "entropy", "appended", "pem"):
            self.assertIn("control " + control + ": detected", text)
        self.assertIn("controls: clean", text)

    def test_real_generic_entropy_and_stopword_negative(self):
        tmp = _scratch(self)
        source = tmp / "plant"
        source.mkdir()
        config, report = tmp / "cfg.toml", tmp / "report.json"
        gate.write_default_config(config)
        fixed = gate._entropy_control()
        # Equal-frequency hex with a default-rule stopword still passes the
        # old generator's Shannon floor. Entropy alone never proved detection.
        stopword_hex = "dead" + "".join(format(n, "x") for n in range(16)) * 4
        self.assertGreater(gate._shannon_entropy(stopword_hex), 3.7)
        for value, expected in ((fixed, True), (stopword_hex, False), ("0" * 64, False)):
            (source / "settings.txt").write_text('secret_key = "' + value + '"\n')
            status = gate._run_scanner(
                [str(self.gitleaks), "detect", "--no-git", "--source", str(source),
                 "--config", str(config), "--redact", "--no-banner",
                 "--exit-code", "7", "--report-format", "json",
                 "--report-path", str(report)], report)
            findings = gate.summarize_findings(report)
            self.assertEqual(status, 7 if expected else 0)
            self.assertEqual(findings, [("generic-api-key", 1)] if expected else [])

    def test_real_committed_source_clean(self):
        tmp = _scratch(self)
        owned = {"gate.py", "test_gate.py", "README.md", "secrets.yml",
                 "pre-push"}
        config = tmp / "cfg.toml"
        gate.write_default_config(config)
        for source in (_HERE, WORKFLOW.parent, HOOK.parent):
            report = tmp / (source.name + ".json")
            proc = subprocess.run(
                [str(self.gitleaks), "detect", "--no-git",
                 "--source", str(source), "--config", str(config),
                 "--redact", "--no-banner", "--exit-code", "1",
                 "--report-format", "json", "--report-path", str(report)],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, timeout=120)
            self.assertIn(proc.returncode, (0, 1))
            findings = json.loads(report.read_text(encoding="utf-8"))
            mine = [entry for entry in findings
                    if os.path.basename(entry.get("File", "")) in owned]
            self.assertEqual(mine, [])


if __name__ == "__main__":
    unittest.main()

