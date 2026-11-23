"""Offline self-test of the shared host/bpy twins. Live calibration is never claimed here."""
# @module {"id":"bv.selftest","layer":4,"owner":"BINT"}
from __future__ import annotations

from blender_visual_qa import contracts
from blender_visual_qa.drive import journal
from bpy_side import canon

# R-B9 golden vector (T-BI-SEED): values pinned from the SplitMix64 formula.
SEED_TRIPLES = ((0, 0, 0), (0, 1, 0), (0, 0, 1), (1, 0, 0), (7, 3, 5), (0, 1048575, 1048575))


# @anchor:bv.selftest.run
def run():
    rows = []
    for t in SEED_TRIPLES:
        rows.append({"id": "seed_twin:%d.%d.%d" % t, "ok": canon.cycles_seed(*t) == journal.cycles_seed(*t)})
    rows.append({"id": "seed_injective", "ok": len({canon.cycles_seed(*t) for t in SEED_TRIPLES}) == len(SEED_TRIPLES)})
    return rows


def main(argv=None):
    rows = run()
    bad = [r["id"] for r in rows if not r["ok"]]
    rc = 5 if bad else 3  # live calibration stages are not part of this offline check: INCOMPLETE, never PASS
    print(contracts.canonical_json({"code": "E_GATE" if bad else "E_INCOMPLETE", "rc": rc, "failed": bad, "rows": len(rows), "reason": "offline_only"}), end="")
    return rc
