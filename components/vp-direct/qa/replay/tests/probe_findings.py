"""Run the minimized P4/P7 publication recovery oracle against a pinned revision."""

from contextlib import ExitStack
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fixture import LifecycleFixture, rows
from perception.lifecycle import LifecycleError


def states(fixture):
    return [row["payload"]["to"] for row in fixture.journal.verify()]


def discontinuity(cut=None):
    with ExitStack() as cleanup:
        root = Path(cleanup.enter_context(TemporaryDirectory(prefix="rep-finding-", dir="/dev/shm")))
        fixture = LifecycleFixture(root, cleanup)
        original = fixture.observe
        cuts = []

        def interrupted(receipt, proof):
            if receipt["to"] != "ASLEEP":
                original(receipt, proof)
                return
            cuts.append(proof.broker_records[-1]["perception"]["state"])
            if cut == "after_append":
                original(receipt, proof)
            raise OSError(f"publication interrupted {cut}")

        if cut is not None:
            fixture.observe = interrupted
            try:
                fixture.call("sleep")
            except LifecycleError as refused:
                if refused.code != "HOLD" or cuts != ["ASLEEP"]:
                    raise
            else:
                raise AssertionError("fault at the publication boundary did not activate")
        else:
            fixture.call("sleep")

        before = states(fixture)
        expected_before = ["ACTIVE"] if cut == "before_append" else ["ACTIVE", "ASLEEP"]
        if before != expected_before:
            raise AssertionError(f"interruption had unexpected persisted effects: {before!r}")
        prefix = (root / "lineage.jsonl").read_bytes()
        broker_path = "broker/events.jsonl"
        rec = next(row for row in rows(root / broker_path) if row.get("perception", {}).get("state") == "ASLEEP")
        fixture.observe = original
        try:
            fixture.reopen()
            recovered = fixture.call("status")
            after_recovered_status = states(fixture)
            fixture.call("wake")
        except LifecycleError as refused:
            if cut is not None and refused.code in ("HOLD", "NO_LEASE"):
                recovered_states = states(fixture)
                if recovered_states in (["ACTIVE"], ["ACTIVE", "ASLEEP"]) and (
                    fixture.broker.perception_state(fixture.binding["surface_id"])["state"] == "ASLEEP"
                ):
                    return None
            raise
        if recovered["to"] != "ASLEEP":
            raise AssertionError(f"unexpected recovered state {recovered['to']!r}")
        after = states(fixture)
        if not (root / "lineage.jsonl").read_bytes().startswith(prefix):
            raise AssertionError("recovery rewrote the durable lineage prefix")
        if after == ["ACTIVE", "ASLEEP", "ACTIVE"]:
            return None
        if cut != "before_append" or after != ["ACTIVE", "ACTIVE"]:
            raise AssertionError(f"unrecognized lineage projection before={before!r} after={after!r}")
        transition = fixture.journal.verify()[1]
        return {
            "id": "REP-P4-P7-001",
            "status": "FAIL",
            "owner": "components/vp-direct/perception/lifecycle/",
            "affected": "components/vp-direct/perception/receipts/",
            "subject_commit": os.environ["REP_REVISION"],
            "seed": 0,
            "schedule": ["wake", "sleep and interrupt before lineage append",
                         "reopen broker, demand, goal, and lifecycle with surviving producer",
                         "read status", "wake"],
            "first_divergent_record": {
                "expected": {"journal": "lineage.jsonl", "lamport": 2, "to": "ASLEEP"},
                "actual": {"journal": "lineage.jsonl", "lamport": transition["lamport"],
                           "from": transition["payload"]["from"], "to": transition["payload"]["to"]},
                "durable_broker": {"journal": broker_path, "seq": rec["seq"],
                                   "state": rec["perception"]["state"],
                                   "stream_generation": rec["perception"]["stream_generation"]},
            },
            "lineage_before_reopen": before,
            "lineage_after_recovered_status": after_recovered_status,
            "lineage_after_wake": after,
            "recovered_status": {"to": recovered["to"], "complete": recovered["complete"]},
            "goal_attempts_after_wake": fixture.ledger()["t"],
            "oracle": "Replay the missing transition receipt or keep new transitions fenced.",
            "observation_boundary": "Lifecycle owner replacement in one Python process. The independent P3 producer survives.",
            "evidence_tag": "MEASURED",
        }


if __name__ == "__main__":
    if discontinuity() is not None:
        raise AssertionError("the control without an interruption missed a transition")
    if discontinuity("after_append") is not None:
        raise AssertionError("the control after the append lost the transition")
    result = discontinuity("before_append")
    print(json.dumps([] if result is None else [result], sort_keys=True, indent=2))
    sys.exit(int(result is not None))
