# HB-VC measurement runner for R-s5-gop-rle-bound (tracked; JDOCS-2)
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import random, json
from visioncore import gop
random.seed(7)
viol = 0; worst = 0; cases = 0
def chk(d):
    global viol, worst, cases
    e = gop.rle_encode(d); cases += 1
    assert gop.rle_decode(e, len(d)) == d
    b = gop.rle_bound(len(d))
    if len(e) > b: viol += 1
    worst = max(worst, len(e) - len(d))
chk(b"\x01"*4); chk(b"\x01"*512); chk(b"\x07\x00"*4096); chk(b"\x00"*4096); chk(b"")
tight4 = len(gop.rle_encode(b"\x01"*4)); tight512 = len(gop.rle_encode(b"\x01"*512))
for _ in range(2000):
    n = random.randint(1, 3000); p = random.random()
    chk(bytes(0 if random.random() < p else random.randint(1,255) for _ in range(n)))
print(json.dumps({"cases":cases,"violations":viol,"max_overhead_B":worst,"n4_encoded":tight4,"n512_encoded":tight512,
 "bound_n4":gop.rle_bound(4),"bound_n512":gop.rle_bound(512),"max_gop_bytes_4096_20":gop.max_gop_bytes(4096,20,64,64),
 "rle_bound_131072":gop.rle_bound(131072)}, sort_keys=True))
