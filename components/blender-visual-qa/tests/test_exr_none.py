import struct
import tempfile
import unittest
from pathlib import Path

import numpy as np

from blender_visual_qa.truth import exr_none


def fixture(path, *, compression=0, version=2, order=0, duplicate=False):
    def attr(name, kind, value):
        return name.encode()+b'\0'+kind.encode()+b'\0'+struct.pack('<I',len(value))+value
    channels = b''.join(n.encode()+b'\0'+struct.pack('<iB3xii', t, 0, 1, 1)
                        for n,t in [('B',1),('G',0),('R',2)])+b'\0'
    hdr = struct.pack('<II',20000630,version)
    hdr += attr('channels','chlist',channels)+attr('compression','compression',bytes([compression]))
    hdr += attr('dataWindow','box2i',struct.pack('<4i',2,7,3,8))
    hdr += attr('lineOrder','lineOrder',bytes([order]))+b'\0'
    rows=[]
    ys = [8,7] if order else [7,8]
    for y in ys:
        data=np.array([y,y+1],'<f2').tobytes()+np.array([10,11],'<u4').tobytes()+np.array([20,21],'<f4').tobytes()
        rows.append(struct.pack('<ii',7 if duplicate else y,len(data))+data)
    start=len(hdr)+16
    offsets=[start,start+len(rows[0])]
    if order == 2: offsets.reverse()
    path.write_bytes(hdr+struct.pack('<2Q',*offsets)+b''.join(rows))


class ExrNoneTests(unittest.TestCase):
    def test_mixed_channels_and_decreasing_rows(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'a.exr'
            for order in (0,1,2):
                fixture(p,order=order)
                names,a=exr_none.read(p)
                self.assertEqual(names,['B','G','R'])
                np.testing.assert_array_equal(a[:,:,0],[[7,8],[8,9]])
                self.assertEqual(a.dtype,np.float32)

    def test_fail_closed_variants_and_truncation(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'a.exr'
            for kw in ({'compression':3},{'version':514},{'version':4098},{'duplicate':True},{'order':3}):
                fixture(p,**kw)
                with self.assertRaises(ValueError): exr_none.read(p)
            fixture(p)
            p.write_bytes(p.read_bytes()[:-1])
            with self.assertRaises(ValueError): exr_none.read(p)
