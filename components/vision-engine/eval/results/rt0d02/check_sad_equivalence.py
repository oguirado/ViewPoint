"""Independent sealed helper check, including int16 extremes and empty requests."""
import importlib.util, subprocess, sys
from pathlib import Path
import numpy as np
ROOT=Path(__file__).resolve().parents[3]
sys.path.insert(0,str(ROOT))
from visioncore import change

def main():
    path=ROOT/'build/room_D/sealed_change.py'
    path.write_bytes(subprocess.check_output(['git','show','ve-sealed-1:visioncore/change.py'],cwd=ROOT))
    spec=importlib.util.spec_from_file_location('visioncore._rt0d02_sealed_change',path)
    sealed=importlib.util.module_from_spec(spec);spec.loader.exec_module(sealed)
    rng=np.random.default_rng(722)
    exhaustive = "--exhaustive" in sys.argv
    checks=0
    for s in (16,32,64):
        gh,gw=3,4
        cur=rng.integers(-32768,32768,(s*gh,s*gw),dtype=np.int16)
        prev=rng.integers(-32768,32768,cur.shape,dtype=np.int16)
        vectors=[(0,0),(s+2,-s-2),(-gw*s,gh*s),(gw*s+1,-gh*s-1)]
        vectors += ([(dx,dy) for dy in range(-s-2,s+3) for dx in range(-s-2,s+3)]
                    if exhaustive else [tuple(map(int,v)) for v in rng.integers(-s-2,s+3,(100,2))])
        for vec in vectors:
            reqs=[(np.array([3,0,1,3,-1]),np.array([2,0,1,2,-1]))]
            if not exhaustive: reqs.append((np.array([],int),np.array([],int)))
            for tx,ty in reqs:
                for name in ('_tile_sad','_tile_sad_clip'):
                    a=getattr(change,name)(cur,prev,vec,s,gh,gw,tx,ty)
                    b=getattr(sealed,name)(cur,prev,vec,s,gh,gw,tx,ty)
                    for x,y in zip(a,b): np.testing.assert_array_equal(x,y)
                    checks+=1
    print('PASS',checks,'sealed helper comparisons')
if __name__=='__main__': main()
