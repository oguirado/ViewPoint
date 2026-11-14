# HB-VC runner for R-s1-desc-bench (tracked; JDOCS-2). Timings are host dependent.
import json, os, statistics, sys, time
import numpy as np
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from visioncore.grid import Grid
from visioncore.desc import describe
res={}
for W,H in ((1280,720),(1920,1080)):
    g=Grid(W,H); f=np.random.RandomState(0).randint(0,256,(H,W,3)).astype(np.uint8)
    f2=f.copy(); f2[:64,:64]^=7
    describe(f,g)
    prev=g.pad(f); _,_=describe(f,g); t0,_=describe(f,g)
    ts=[]
    for _ in range(9):
        a=time.perf_counter(); describe(f2,g,words_prev=prev,prev_xh64=t0['xh64']); ts.append((time.perf_counter()-a)*1000)
    ts.sort(); res[f"{W}x{H}"]=(statistics.median(ts),ts[-1])
    tc=[]
    for _ in range(5):
        a=time.perf_counter(); describe(f,g); tc.append((time.perf_counter()-a)*1000)
    tc.sort(); res[f"{W}x{H}_cold"]=(statistics.median(tc),tc[-1])
m={}
for k,(p50,mx) in res.items(): m[f"describe_ms_p50_{k}"]=round(p50,1); m[f"describe_ms_max_{k}"]=round(mx,1)
print(json.dumps(m, sort_keys=True))
