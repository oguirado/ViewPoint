from pathlib import Path
import subprocess
import sys
import unittest


DRIVER = Path(__file__).resolve()
RUNNER = DRIVER.with_name("LIVE_channel.py")
LEGACY_ENTRY = RUNNER.parent.parent / RUNNER.name
EXPECTED_REQUESTS = ["attach", "observe", "act", "act", "assert", "detach"]


def open_fixture(backend):
    # The real harness must make vp-direct imports available under python -I.
    from channel.core import TargetContext

    target = TargetContext(
        session={"session_id": "fixture-session", "incarnation": 1, "profile_id": "fixture",
                 "display": "xpra:7", "window": "fixture-window", "lease": "fixture-lease"},
        surface_id="fixture-surface", surface_kind="private", private_backends=frozenset({backend}),
    )

    class FixtureChannel:
        def __init__(self):
            self.requests = []

        def _target(self, request, agent_id):
            return target

        def dispatch(self, request, *, agent_id):
            op = request["op"]
            self.requests.append(op)
            if op == "detach" and self.requests != EXPECTED_REQUESTS:
                raise AssertionError(self.requests)
            return {"ok": True, "ax_sha256": "before" if op == "observe" else "after"}

    def request_for(op):
        return {"backend": backend, "op": op, "dry_run": False, "action": "click"}

    return FixtureChannel(), "fixture-agent", request_for


class LiveChannelEntryTests(unittest.TestCase):
    def run_entry(self, entry, backend):
        return subprocess.run(
            [sys.executable, "-I", "-B", *entry, str(DRIVER), "--backend", backend],
            capture_output=True, text=True, timeout=15, check=False,
        )

    def assert_success(self, result, backend):
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, f"LIVE {backend}: PASS\n")

    def test_relocated_and_driver_entrypoints_execute_both_backend_flows(self):
        driver_entry = (
            "-c", 'import runpy, sys; runpy.run_path(sys.argv.pop(1), run_name="__main__")',
            str(LEGACY_ENTRY),
        )
        for entry in ((str(RUNNER),), driver_entry):
            for backend in ("cdp", "atspi"):
                with self.subTest(entry=entry, backend=backend):
                    self.assert_success(self.run_entry(entry, backend), backend)

    def test_both_entrypoints_refuse_an_unrecognized_backend(self):
        for entry in (RUNNER, LEGACY_ENTRY):
            with self.subTest(entry=entry):
                result = self.run_entry((str(entry),), "unsupported")
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn("invalid choice", result.stderr)
                self.assertEqual(result.stdout, "")

    def test_mutant_loading_runner_without_main_fails_execution_oracle(self):
        source = LEGACY_ENTRY.read_text()
        anchor = 'run_name="__main__"'
        self.assertEqual(source.count(anchor), 1)
        mutant = source.replace(anchor, 'run_name="__fixture_mutant__"')
        # Preserve the launcher's path so the mutant reaches the real runner.
        command = f"__file__ = {str(LEGACY_ENTRY)!r}\n{mutant}"
        result = self.run_entry(("-c", command), "cdp")
        self.assertEqual(result.returncode, 0, result.stderr)
        with self.assertRaises(AssertionError):
            self.assert_success(result, "cdp")
