"""Integer tile indices must not underflow when source offsets are subtracted."""
import importlib.util,subprocess,sys
from pathlib import Path
import numpy as np
ROOT=Path(__file__).resolve().parents[3];sys.path.insert(0,str(ROOT))
from visioncore import change

def main():
    path=ROOT/'build/room_D/index_sealed_change.py'
    path.write_bytes(subprocess.check_output(['git','show','ve-sealed-1:visioncore/change.py'],cwd=ROOT))
    spec=importlib.util.spec_from_file_location('visioncore._index_sealed_change',path)
    sealed=importlib.util.module_from_spec(spec);spec.loader.exec_module(sealed)
    rng=np.random.default_rng(772)
    checks=0
    for s in [16,32,64]:
        cur=rng.integers(-32768,32768,(2*s,3*s),dtype=np.int16)
        prev=rng.integers(-32768,32768,cur.shape,dtype=np.int16)
        for dtype in [np.int8,np.int16,np.int32,np.int64,np.uint8,np.uint16,np.uint32,np.uint64]:
            tx=np.array([0,1,2,0,2],dtype=dtype);ty=np.array([0,1,0,1,1],dtype=dtype)
            for vec in [(3,1),(-3,-1),(s+2,-s-2),(-s-2,s+2),(0,0),(4*s,0)]:
                for name in ['_tile_sad','_tile_sad_clip']:
                    expected=getattr(sealed,name)(cur,prev,vec,s,2,3,tx,ty)
                    actual=getattr(change,name)(cur,prev,vec,s,2,3,tx,ty)
                    for a,b in zip(expected,actual):np.testing.assert_array_equal(a,b)
                    checks+=1
    s=16;cur=rng.integers(0,256,(32,4096),dtype=np.int16)
    for dtype in [np.int8,np.uint8,np.int64,np.uint64]:
        tx=np.array([0,1,127],dtype=dtype);ty=np.array([0,1,0],dtype=dtype)
        for name in ['_tile_sad','_tile_sad_clip']:
            expected=getattr(sealed,name)(cur,cur,(3,1),s,2,256,tx,ty)
            actual=getattr(change,name)(cur,cur,(3,1),s,2,256,tx,ty)
            for a,b in zip(expected,actual):np.testing.assert_array_equal(a,b)
            checks+=1
    print('PASS',checks,'signed/unsigned index comparisons against sealed helpers')
if __name__=='__main__':main()
