"""Run one short offline timing calibration against the integrated P7 fixture."""

from contextlib import ExitStack
from hashlib import sha256
import json
from pathlib import Path
import sys
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from benchmark import ORDER, TimedStream, TransitionVerifier, context, host_sample, now, placement, verify_frame
from score import check_wake, digest, percentile_ns, require

from admission import AdmissionGate, AdmissionPlan, UsageSnapshot
from perception.receipts.tests import test_receipts as p7_fixtures


def sample(owner, state, gate, verifier):
    if state == "ASLEEP":
        verifier.call(owner, "wake")
        verifier.call(owner, "sleep")

    placement()
    before = host_sample()
    submit = now()
    with gate.reserve(AdmissionPlan(owner.surface, state, "offline-fixture", False)) as decision:
        require(decision["admitted"], "Offline fixture admission refused")
        admitted = now()
        p4, p7, states, observation = verifier.call(owner, "wake")
        first = owner.stream.first_byte_ns
        verify_frame(owner, observation)
        complete = now()
        verifier.call(owner, "off")
    row = dict(wake_admission=decision, p4=p4, p7=p7, broker_states=states)
    check_wake(row, state, owner.surface, (submit, admitted, first, complete), set())
    return dict(row, state=state, outcome="verified", host_before=before, host_after=host_sample(),
                markers=dict(t_submit=submit, t_admitted=admitted, t_first_byte=first, t_complete=complete))


def main():
    runs = []
    fixture_sha = sha256(Path(p7_fixtures.__file__).read_bytes()).hexdigest()
    workload_sha = digest({"fixture": fixture_sha, "warmup": "OFF", "cycle_states": ["ASLEEP", "OFF"]})
    for index, arm in enumerate(ORDER):
        header = context(arm, index, workload="offline-p4-p6-p7-timing-fixture",
                         fixture_sha256=fixture_sha, warmups=1, samples=2)
        header["workload_sha256"] = workload_sha
        with ExitStack() as stack:
            fixture = p7_fixtures.ReceiptTests()
            stack.callback(fixture.doCleanups)
            verifier = TransitionVerifier()
            with (patch.object(p7_fixtures, "PerceptionStream", TimedStream),
                  patch.object(fixture, "publish", verifier)):
                fixture.setUp()
            owner = fixture.lifecycle
            gate = AdmissionGate(lambda: UsageSnapshot({owner.surface: None}),
                                 scopes=[("offline", "/fixture")], read_memory=lambda: (100_000, 75_000),
                                 inventory=lambda _: {"complete": True, "charged_bytes": 100})
            sample(owner, "OFF", gate, verifier)
            rows = [sample(owner, state, gate, verifier) for state in ("ASLEEP", "OFF")]
        header["after"] = host_sample()
        runs.append(dict(header, cycles=rows, outcomes={"verified": len(rows), "failed": 0, "refused": 0}))

    markers = [row["markers"] for run in runs for row in run["cycles"]]
    percentiles = {}
    for name, start, end in (("queue", "t_submit", "t_admitted"),
                             ("end_to_end", "t_submit", "t_complete")):
        values = [row[end] - row[start] for row in markers]
        percentiles[name] = dict(p50=percentile_ns(values, 50), p95=percentile_ns(values, 95))
    for state in ("ASLEEP", "OFF"):
        values = [row["markers"]["t_first_byte"] - row["markers"]["t_admitted"]
                  for run in runs for row in run["cycles"] if row["state"] == state]
        percentiles["wake_" + state.lower()] = dict(p50=percentile_ns(values, 50),
                                                      p95=percentile_ns(values, 95))
    receipt = {"tag": "MEASURED", "workload": "offline fixture, identical baseline/candidate code",
               "percentiles_ns": percentiles, "runs": runs,
               "raw_coordinate": "components/vp-direct/accept/t1/tests/calibration.json"}
    print(json.dumps(receipt, sort_keys=True, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
