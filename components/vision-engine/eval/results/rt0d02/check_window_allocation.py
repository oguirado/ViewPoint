"""Negative control: the original implementation allocates full-frame SAD buffers."""
from pathlib import Path
import sys
from unittest.mock import patch
import numpy as np
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from visioncore import change

def main():
    y=np.arange(256*256,dtype=np.int16).reshape(256,256)
    zeros=np.zeros
    def bounded(shape,*args,**kwargs):
        assert shape != y.shape, 'full-frame SAD allocation'
        return zeros(shape,*args,**kwargs)
    with patch.object(np,'zeros',bounded):
        for fn in [change._tile_sad,change._tile_sad_clip]:
            fn(y,y,(3,-5),32,8,8,np.array([1,5]),np.array([2,6]))
    print('PASS: no full-frame SAD zero buffers for requested tiles')
if __name__=='__main__': main()
