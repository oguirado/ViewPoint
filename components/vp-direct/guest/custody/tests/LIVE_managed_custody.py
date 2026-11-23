#!/usr/bin/env python3
"""Operator-only readback across app exit and normal H5 teardown.

Arm after the workload reservation and before operator-driven app exit.
No launch, release, signal, or host display access is performed by this probe.
"""
import argparse
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from guest.custody import inventory


def check(registry, reservation_id, workload_id, timeout):
    directory = inventory.private_dir(registry)
    marker = directory / "held-session.json"
    held = inventory.read_private(marker)
    held_digest = inventory.digest(held)
    database = directory / "guest-custody.sqlite3"
    inventory.private_file(database.lstat())
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        def reservation():
            row = db.execute("SELECT * FROM reservations WHERE reservation_id=? AND workload_id=?",
                             (reservation_id, workload_id)).fetchone()
            inventory.need(row is not None and row["held_digest"] == held_digest, "foreign reservation")
            return row

        row = reservation()
        root = inventory.private_dir(row["root"])
        inventory.need(inventory.decode(held)["root"] == str(root) and row["proof_sha256"] is None,
                       "expected a held managed workload")
        plan = inventory.decode(inventory.read_private(root / "PLAN.json"))
        def exited():
            return any(item["event"] == "app_exited_closure_unproven"
                       for item in inventory.event_inventory(root, plan))

        inventory.need(not exited(), "arm before app exit")
        deadline = time.monotonic() + timeout
        def wait():
            inventory.need(time.monotonic() < deadline, "timed out awaiting operator evidence")
            time.sleep(0.05)

        while not exited():
            wait()
        inventory.need(inventory.read_private(marker) == held and reservation()["proof_sha256"] is None,
                       "unchanged marker and held reservation required after visual exit")
        while (digest := reservation()["proof_sha256"]) is None:
            wait()
        matches = [p for p in directory.glob(f'reconciled-{row["run_id"]}-*.json')
                   if inventory.digest(inventory.read_private(p)) == digest]
        inventory.need(len(matches) == 1, "matching independent reconciliation archive required")
        proof = inventory.decode(inventory.read_private(matches[0]))
        inventory.need(proof["held_sha256"] == held_digest and proof["run_id"] == row["run_id"],
                       "closure proof differs from the initial reservation")
        return {"reservation_id": reservation_id, "workload_id": workload_id,
                "held_sha256": held_digest, "proof_sha256": digest}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", required=True, type=Path)
    parser.add_argument("--reservation-id", required=True)
    parser.add_argument("--workload-id", required=True)
    parser.add_argument("--timeout-seconds", required=True, type=int)
    args = parser.parse_args()
    print(json.dumps(check(args.registry, args.reservation_id, args.workload_id, args.timeout_seconds)))
