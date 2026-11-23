"""LIVE only. Verify every receipt in the existing P4 private Chrome cycle.

Use the arguments documented by LIVE_chrome_cycle.py, with python3 -I -B and
a capped scope. The fixture is already provisioned, synthetic and private.
The runner forwards P4's own identity and display checks. Supply the runner's
existing private journal directory through --lineage-dir.
"""

from hashlib import sha256
import json
from pathlib import Path
import sys

DIRECT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(DIRECT))
from perception.lifecycle.tests import LIVE_chrome_cycle
from bus.journal import Journal
from bus.journal.journal import FILENAME, GENESIS
from perception.receipts import ReceiptError, append_transition


def run(args):
    journal = Journal(args.lineage_dir)
    journal.verify()
    published = []

    def publish(receipt, proof):
        reference = append_transition(journal, receipt, proof, agent="broker")
        published.append((receipt, proof, reference))

    result = LIVE_chrome_cycle.run(args, on_transition=publish)
    if not published or [receipt for receipt, _, _ in published] != result["receipts"]:
        raise RuntimeError("P7 did not publish every acknowledged transition")

    reopened = Journal(args.lineage_dir)
    events = reopened.verify()
    path = args.lineage_dir / FILENAME
    raw = path.read_bytes()
    lines = raw.splitlines(keepends=True)
    for receipt, proof, reference in published:
        index = reference["lamport"] - 1
        previous = GENESIS if index == 0 else sha256(lines[index - 1]).hexdigest()
        if (not lines[index].endswith(b"\n") or events[index]["payload"] != receipt
                or events[index]["prev_sha256"] != previous or reference != {
                    "lamport": events[index]["lamport"], "prev_hash": previous,
                    "event_hash": sha256(lines[index]).hexdigest(),
                } or append_transition(reopened, receipt, proof, agent="broker") != reference):
            raise RuntimeError("P7 lineage readback or replay failed")
    if path.read_bytes() != raw:
        raise RuntimeError("P7 replay changed the journal")
    return dict(result, lineage=[reference for _, _, reference in published],
                w8u=ReceiptError.runtime_state, worker_ui=ReceiptError.worker_ui)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lineage-dir", type=Path, required=True)
    for name in ("session", "binding", "agent-demand", "synthetic-profile"):
        parser.add_argument("--" + name, type=Path, required=True)
    for name in ("browser-pid", "xvfb-pid", "fixture-port", "max-frame-bytes"):
        parser.add_argument("--" + name, type=int, required=True)
    print(json.dumps(run(parser.parse_args()), sort_keys=True))
