"""LIVE operator-driven Blender check. Run only inside the campaign's capped scope.

Usage: python3 -I -B LIVE_goals.py PRIVATE_CAPTURE_ROOT NEW_GOAL_DATABASE

Drive the private capsule through its authorized broker. Each prompt names
the next action and reads one JSON object on stdin. Paste actual receipts
and observation readbacks. A capture is {directory, blend_sha256,
receipt_sha256} under PRIVATE_CAPTURE_ROOT. U5 verifies its files and scores
the committed render evidence locally. The script never launches a GUI or
issues input. A lost executor reply leaves the durable claim for reconciliation.
"""
import argparse
import json
from pathlib import Path
import sys

DIRECT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(DIRECT), str(DIRECT.parent / "blender-visual-qa")]

from rewind.machine import GoalMachine, Refused
from vendor.rewind import schema as rw
from vqa3d_bridge import evaluate_capture


def ask(prompt):
    print(prompt, file=sys.stderr, flush=True)
    return json.loads(input())


def check(condition, message):
    if not condition:
        raise ValueError(message)


def capture(root, row):
    directory = (root / row["directory"]).resolve(strict=True)
    check(directory.is_relative_to(root), "capture outside selected private workload")
    return evaluate_capture(directory, blend_sha256=row["blend_sha256"],
                            receipt_sha256=row["receipt_sha256"])


def observe(machine):
    row = ask("Acquire a fresh observation. Enter {observation_id, generation}.")
    machine.observed(row["observation_id"], row["generation"])


def restore(claim, state):
    row = ask(f"Broker: execute {claim} once for {state['goal_id']}. "
              "Enter {control, receipt} from the broker and restore backend.")
    control = rw.validate_record(row["control"])
    check(control["rec"] == "control" and control["op"] == "rewind" and
          control["by"] == "broker" and control["to"] == state["checkpoint"]["last_good"] and
          control["scene_epoch_after"] == state["scene_epoch"] + 1 and control["digest_match"],
          "broker rewind readback mismatch")
    return row["receipt"]


def judge(machine, root, attempt):
    row = ask(f"Execute the claimed edit {attempt} once. Enter {{post_state_sha256, capture}}.")
    return machine.judge(attempt, row["post_state_sha256"], capture(root, row["capture"]))


def check_goal(root, database, exhaust):
    label = "exhaustion" if exhaust else "polishing"
    start = ask(f"Open the {label} goal at p0. Enter {{session_id, binding, checkpoint, capture, "
                "observation_id, generation, epsilon, action_quantum}. "
                "For exhaustion, set the attempt budget to one.")
    point = start["checkpoint"]
    check(point["backend"] == "blender_rw1", "this live check requires a Blender checkpoint")
    budget = point["budget"]
    if exhaust:
        check(budget["attempts_limit"] == 1, "exhaustion requires one allowed attempt")
    else:
        check(budget["attempts_limit"] >= 2 and budget["rewinds_limit"] >= 1,
              "polishing requires two attempts and a rewind")
    baseline = capture(root, start["capture"])
    check(baseline.accepted, "p0 failed the U5 hard predicates")
    machine = GoalMachine.create(database, goal_id=point["goal_id"], session_id=start["session_id"],
                                 binding=start["binding"], checkpoint=point, score=baseline.score,
                                 steps=1, duplicates=3, epsilon=start["epsilon"],
                                 action_quantum=start["action_quantum"], observation_id=start["observation_id"],
                                 generation=start["generation"])
    bad = ask("Enter the bad edit proposal as {attempt, action}.")
    check(machine.propose(**bad, observation=start["observation_id"]) == "EXECUTING", "bad edit refused")
    check(judge(machine, root, bad["attempt"]) == "JUDGE_REJECT", "bad edit escaped rejection")
    if exhaust:
        check(machine.run_restore(restore) == "HARD_STOP_ESCALATED", "missing hard stop")
        stopped = machine.replay()
        check(stopped["reason"] == "JUDGE_REJECT" and stopped["scene_epoch"] == point["scene_epoch"] + 1,
              "cleanup did not verify the restore")
        try:
            machine.propose(**bad, observation=start["observation_id"])
        except Refused as exc:
            check(exc.code == "NO_LEASE", "incorrect terminal refusal")
        else:
            raise ValueError("hard stop admitted a proposal")
    else:
        observe(machine)
        alt = ask("Enter the novel improvement proposal as {attempt, action}.")
        check(machine.propose(**alt, supersedes=bad["attempt"], retry=True,
                              observation=machine.status()["observation"]) == "REWINDING", "retry refused")
        check(machine.run_restore(restore) == "READY", "restore failed")
        machine = GoalMachine(database, point["goal_id"])
        machine.replay()
        observe(machine)
        check(machine.propose(**alt, supersedes=bad["attempt"],
                              observation=machine.status()["observation"]) == "EXECUTING", "edit refused")
        check(judge(machine, root, alt["attempt"]) == "JUDGING", "improvement failed U5 gate")
        receipt = ask("Mark the improved state. Enter its vp.rewind_receipt/v1 checkpoint receipt.")
        check(machine.commit(alt["attempt"], receipt) == "COMMITTED", "mark refused")
    state = machine.replay()
    print(json.dumps({k: state[k] for k in ("goal_id", "phase", "t", "r", "d", "best", "scene_epoch")}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture_root", type=Path)
    parser.add_argument("goal_database", type=Path)
    args = parser.parse_args()
    for exhaustion in (False, True):
        check_goal(args.capture_root.resolve(strict=True), args.goal_database, exhaustion)
