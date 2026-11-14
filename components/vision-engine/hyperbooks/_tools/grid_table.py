# HB-VC runner for R-s0-grid-table (tracked; JDOCS-2)
import json, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from visioncore.grid import Grid
rows = [(w, h, Grid(w, h).s, Grid(w, h).gw, Grid(w, h).gh)
        for w, h in ((1280, 720), (1920, 1080), (3840, 2160), (1024, 1024), (2560, 1440), (8192, 8192), (8192, 16))]
print(json.dumps({"rows": len(rows), "max_cells": max(r[3] * r[4] for r in rows)}, sort_keys=True))
