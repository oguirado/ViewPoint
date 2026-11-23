"""LIVE U4b check for an operator-provisioned Blender rw1 capsule.

Run inside the capped scope with python3 -I -B and a trusted fixture file.
Its live_fixture() returns keyword arguments for run(), with a backend on an
already attached private capsule. genesis is the mark kwargs for admitted
inputs. advance_stage() makes a permitted edit and returns the next mark
kwargs. read_scene() reads the live rw1 digest independently of the backend
result. restore_budget is the goal's budget after authorizing the rewind.
"""

import argparse
import json
from pathlib import Path
import runpy
import sys


DIRECT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(DIRECT), str(DIRECT.parent / "blender-visual-qa")]

from schemas.validation import validate_contract


def check(condition, reason):
    if not condition:
        raise RuntimeError(reason)


def run(*, backend, genesis, advance_stage, read_scene, restore_budget):
    base = backend.mark(**genesis)
    stage = backend.mark(**advance_stage())
    check(base.receipt["last_good"] == "p0" and stage.receipt["last_good"] == "p1",
          "the fixture did not mark the admitted inputs and a subsequent stage")
    expected = base.checkpoint.record["digest"]
    changed = read_scene()
    check(changed == stage.checkpoint.record["digest"] and changed != expected,
          "the live scene did not diverge from the base")
    check(stage.checkpoint.quality != base.checkpoint.quality, "the fixture did not change acceptance evidence")
    result = backend.restore(
        "p0", by="broker", scene_epoch=stage.receipt["scene_epoch"],
        authority_epoch=stage.receipt["authority_epoch"], budget=restore_budget,
    )
    receipt = result.receipt
    validate_contract("vp.rewind_receipt/v1", receipt)
    check(receipt["ok"] and receipt["digest_match"] and
          receipt["scene_epoch"] == stage.receipt["scene_epoch"] + 1 and
          receipt["state_sha256"] == base.receipt["state_sha256"] and read_scene() == expected,
          "the live scene or restore receipt did not match the base")
    check(result.checkpoint == base.checkpoint and backend.current() == base.checkpoint,
          "the restored artifacts and acceptance state did not match the base")
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("fixture", type=Path)
    args = parser.parse_args()
    fixture = runpy.run_path(str(args.fixture.resolve(strict=True)))["live_fixture"]
    print(json.dumps(run(**fixture()), sort_keys=True))


if __name__ == "__main__":
    main()
