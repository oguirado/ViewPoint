# HB-VC measurement runner for R-s3-pyramid-select (tracked; JDOCS-2)
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import random, json, time, numpy as np
from visioncore.grid import Grid
from visioncore import layout
from visioncore.pyramid import Pyramid, r_sel
g = Grid(1280, 720); rs = r_sel(); rng = random.Random(7)
worst = 0; viol = 0; cases = 0; maxvis = 0
for trial in range(60):
    tiles = np.zeros(g.N, dtype=[("morton","<u2"),("cls","u1"),("resid","u1")])
    tiles["morton"] = g.mortons
    k = rng.choice([0, 3, 40, 400, 3600])
    for i in rng.sample(range(g.N), k):
        tiles["resid"][i] = rng.randint(0, 255); tiles["cls"][i] = rng.choice([0,2,2,3])
    p = Pyramid(tiles, g)
    hot = {int(m) for m, r in zip(tiles["morton"], tiles["resid"]) if r > rs}
    for B in (1, 4, 16, 64, 256):
        out = p.select(B); cases += 1
        worst = max(worst, len(out) - B); maxvis = max(maxvis, p.last_visits)
        spans = [(n[1], n[1] + 4 ** n[0]) for n in out]
        if len(out) > B or any(not any(a <= m < b for a, b in spans) for m in hot): viol += 1
t = time.perf_counter(); Pyramid(tiles, g); build_ms = (time.perf_counter() - t) * 1000
print(json.dumps({"cases": cases, "violations": viol, "max_excess_over_B": worst, "max_visits": maxvis, "build_ms_720p_dense": round(build_ms, 2)}))
