# SPDX-License-Identifier: AGPL-3.0-only
"""Disposable independent observations for the ret2 runtime tests."""

import hashlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from slot_host.runtime import evidence_projection as projection, runner
from slot_host.runtime.tests.binding_fixtures import fixture_policy


def install_capture(fixture, payload="offline fixture octets"):
    output = {
        "schema": "rea.slot_result/v1", "status": "OK", "rc": 0, "result_payload": payload,
        "sha256": hashlib.sha256(payload.encode()).hexdigest(),
    }
    raw = (json.dumps(output, ensure_ascii=False) + "\n").encode()
    program = f"""#!{sys.executable}
# SPDX-License-Identifier: AGPL-3.0-only
from pathlib import Path
import sys
with Path({str(fixture.marker)!r}).open("ab") as marker:
    marker.write(b"1")
sys.stdout.buffer.write({raw!r})
"""
    # Preserve the registered fixture file mode and rebind the exact new bytes.
    fixture.exe.write_text(program, encoding="utf-8")
    fixture.manifest["provider_ref"]["implementation_sha256"] = hashlib.sha256(program.encode()).hexdigest()
    fixture.policy = fixture_policy(fixture.manifest) | {"perception_ceiling": "none"}
    return raw


class FixtureObserver:
    """Observe a test-owned child and compare its capture against the known fixture."""

    def __init__(self, raw, start_check=None, *, recordings=None, media=None, timeline=None):
        self.raw = raw
        self.recordings = recordings
        self.media = media
        self.timeline = timeline
        self.start_check = start_check
        self.launches = []
        self.report_digest = None

    def track(self):
        subprocess = runner.subprocess

        def observe(argv, **kwargs):
            if self.start_check is not None:
                self.start_check(argv, kwargs)
            fd, = kwargs["pass_fds"]
            digest = hashlib.sha256()
            with open(f"/proc/self/fd/{fd}", "rb") as executable:
                digest.update(executable.read(1 << 20))
            child = subprocess.Popen(argv, **kwargs)
            self.launches.append((child.pid, runner._proc_starttime(child.pid), digest.hexdigest(), argv[0]))
            return child

        return patch.object(runner, "subprocess", SimpleNamespace(
            Popen=observe, PIPE=subprocess.PIPE, DEVNULL=subprocess.DEVNULL,
        ))

    def __call__(self, admission, started, facts):
        # Keep import deferred so parent regressions can exercise the old launch.
        from slot_host.runtime.retention import TerminalEvidence

        start_sha = projection.record_digest(started)
        common = dict(
            start_sha256=start_sha,
            artifact_observer_id="fixture/capture-observer",
            artifact_observation_id=f"capture/{admission['attempt_id']}",
            cleanup_observer_id="fixture/cleanup-observer",
            cleanup_observation_id=f"cleanup/{admission['attempt_id']}",
        )
        if admission["operation"] == "compare":
            if facts["comparison_sha256"] != self.report_digest:
                raise ValueError("fixture report digest differs")
            return TerminalEvidence(
                **common, captured_sha256=self.report_digest,
                cleanup_retirement_sha256=projection.record_digest({"host-comparison": start_sha}),
            )
        if not self.launches:
            raise ValueError("no fixture launch was observed")
        pid, ticks, executable_sha, executed_path = self.launches[-1]
        captured = hashlib.sha256(self.raw).hexdigest()
        if (facts["process_identity"] != (pid, ticks) or facts["captured_sha256"] != captured
                or executable_sha != admission["implementation_sha256"]):
            raise ValueError("fixture capture did not come from the observed executable and process")
        retirement = None
        if (facts["cleanup_observed"] and not Path(started["command"]["cwd"]).exists()
                and runner._proc_starttime(pid) != ticks):
            retirement = projection.record_digest({"pid": pid, "starttime": ticks, "start": start_sha})
        return TerminalEvidence(
            **common, process={"pid": pid, "starttime_ticks": ticks,
                               "start_id": f"fixture/start/{ticks}", "job_id": f"fixture/job/{pid}"},
            launch_observation={"observer_id": "fixture/launch-observer", "observation_id": f"fixture/launch/{pid}",
                                "receipt_sha256": projection.record_digest(self.launches[-1]),
                                "executable_sha256": executable_sha, "executed_path": executed_path},
            captured_sha256=captured, attribution_receipt_sha256=captured,
            cleanup_retirement_sha256=retirement, recordings=self.recordings,
            media=self.media, timeline=self.timeline,
        )
