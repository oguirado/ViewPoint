"""DIAGNOSTIC only: original Python versus exact acceleration at 1080p.

Three fixed DEV transitions, without content caching. This is not a throughput
claim, and short flicker sequences do not measure the tracker after warmup.
"""
import itertools
import json
import os

import numpy as np
from eval.gui_synth.gen import Src
from visioncore.grid import Grid
from .baseline import load_dev, selection
from .native import SCRATCH
from .postpass import PostPass
from .run import OUT, identity, implementation_digest, write_json


def main():
    if not os.sched_getaffinity(0) <= {2, 3, 4}:
        raise ValueError("invoke with taskset -c 2-4")
    verified = json.loads((SCRATCH/"baseline_verified.json").read_text())
    if not verified["baseline_match"]:
        raise ValueError("baseline not proven")
    rows = []
    scenes = selection(load_dev())
    for kind in ("static_clean", "scroll_large", "caret_move"):
        scene = next(s for s in scenes if s["w"] == 1920 and s["kind"] == kind and s["k"] == 0)
        tiles = np.load(SCRATCH/(identity(scene)+".tiles"), allow_pickle=False)
        frames = list(itertools.islice(Src(scene), 2))
        outputs, timings = {}, {}
        for accelerated in (False, True):
            adapter = PostPass(Grid(scene["w"], scene["h"]), accelerated=accelerated, cache_size=0)
            adapter.update(tiles[0], frames[0].rgb)
            before = dict(adapter.wall_seconds)
            outputs[accelerated], _ = adapter.update(tiles[1], frames[1].rgb)
            timings["exact_accelerated" if accelerated else "original_python"] = {
                name: (adapter.wall_seconds[name]-before[name])*1000 for name in before}
        for variant in outputs[False]:
            for field in outputs[False][variant]:
                np.testing.assert_array_equal(outputs[False][variant][field], outputs[True][variant][field])
        row = {"scene_id": scene["scene_id"], "kind": kind, "frame": 1,
               "resolution": [1920, 1080], "tag": "DIAGNOSTIC",
               "unit": "wall_ms_per_frame", "output_equivalent": True, "timing": timings}
        rows.append(row)
        print(json.dumps(row), flush=True)
        write_json(OUT/"diagnostic.json", {"tag": "DIAGNOSTIC", "rows": rows,
                   "implementation_sha256": implementation_digest(),
                   "scope": "Prototype computation; spatial passes include luma conversion; common adapter output copies, source, engine and scorer excluded."})


if __name__ == "__main__":
    main()
