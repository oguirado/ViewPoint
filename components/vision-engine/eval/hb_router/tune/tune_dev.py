#!/usr/bin/env python3
"""DEV-only wrapper around hbtools_v2/hb_tune.py for the sealed blind DEV set (FIND + ABSENT rows only).
Only change: stage 4 (IMPACT) has no DEV rows, so hb_tune's mean would divide by zero; here it keeps the default IMPACT config and says so.
Stage 1 (tau_rel drift suite) is skipped with --skip-drift: its suite targets tools/route_v2.py of the original HB-v2 subject, absent here; tau_rel stays at the default. Stage 5 (visual) is skipped by hb_tune itself when out/visual_DEV.json is absent. Everything else (guard/g_blind, stages 1-3) is hb_tune unchanged.
usage: tune_dev.py <hb_tune.py args>   (run with HBV2_* env of the runbook)"""
import os, sys
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.environ["HBV2_SUBJECT"], "hbtools_v2"))
import hb_tune as T  # noqa: E402

_stage4 = T.stage4_impact


def stage4_or_default(h, rows, grid, find_cfg):
    if not any(T.E.row_class(r) == "IMPACT" for r in rows):
        spec = grid["impact"]
        dflt = {k: T.Q.I.DEFAULT_IMPACT[k] for k in spec["order"]}
        return dflt, [], 0
    return _stage4(h, rows, grid, find_cfg)


T.stage4_impact = stage4_or_default
if __name__ == "__main__":
    sys.exit(T.main(sys.argv[1:] + ["--grid", os.path.join(HERE, "grid_hb.json"), "--skip-drift"]))
