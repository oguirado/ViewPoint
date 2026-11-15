#!/usr/bin/env python3
"""v3 batch oracle plus real Python v2 snapshot provenance.
Persist only digests and operations; Python frame files stay in /dev/shm.
"""
import hashlib
import json
from pathlib import Path
import struct
import zlib
import sys
sys.dont_write_bytecode = True

def fixture(name, slots, ops, seed=19, fmt=0):
    width, height = 4, 4
    size = width * height * (3 if fmt == 0 else 4)
    off = (64 + 64 * slots + 4095) & ~4095
    memory = bytearray(off + size * slots)
    struct.pack_into('<8sIIIIQQQ', memory, 0, b'VQARING1', 3, slots, width, height, size, off, 0)
    struct.pack_into('<IIII', memory, 48, 0, 0, fmt, 3)
    last, head = None, 0
    reads, overrun, torn, gaps = [], [], [], []
    active = None
    def data(r):
        return bytes((r * 131 + i * 7 + seed) & 255 for i in range(size))
    def begin(r):
        nonlocal active
        slot = 64 + (r % slots) * 64
        seq = struct.unpack_from('<Q', memory, slot)[0]
        struct.pack_into('<QQ', memory, slot, seq + 1, r)
        payload = data(r)
        memory[off + r % slots * size:off + (r % slots + 1) * size] = payload
        active = r, seq, slot
    def read(r):
        slot = 64 + r % slots * 64
        seq, ri, crc = struct.unpack_from('<QQI', memory, slot)
        status = 'absent' if seq == 0 else 'torn' if seq & 1 else ('lapped' if ri > r else 'absent' if ri < r else 'frame')
        payload = memory[off + r % slots * size:off + (r % slots + 1) * size]
        if status == 'frame' and zlib.crc32(payload) != crc:
            status = 'torn'
        result = {'r': r, 'status': status}
        if status == 'frame':
            result.update(crc32=zlib.crc32(payload), sha256=hashlib.sha256(payload).hexdigest())
        reads.append(result)
        return status
    for op in ops:
        kind = op['op']
        if kind in ('publish', 'begin'):
            begin(op['r'])
            if kind == 'publish':
                r, seq, slot = active
                payload = memory[off + r % slots * size:off + (r % slots + 1) * size]
                struct.pack_into('<I', memory, slot + 16, zlib.crc32(payload))
                struct.pack_into('<Q', memory, slot, seq + 2)
                head = r + 1
                struct.pack_into('<Q', memory, 40, head)
                active = None
        elif kind == 'tear':
            memory[off + op['r'] % slots * size] ^= 255
        elif kind == 'read':
            if op['mode'] == 'acquire':
                r = op['r']
                status = read(r)
                if status == 'frame':
                    next_r = 0 if last is None else last + 1
                    if r > next_r: gaps.append([next_r, r - 1])
                    last = r if last is None else max(last, r)
                if status == 'lapped': overrun.append(r)
                if status == 'torn': torn.append(r)
                continue
            r = 0 if last is None else last + 1
            if head <= r:
                continue
            if op['mode'] == 'latest' and head - 1 > r:
                gaps.append([r, head - 2])
                r = head - 1
            if head - r > slots:
                end = head - slots
                gaps.append([r, end - 1])
                r = end
            while r < head:
                status = read(r)
                if status == 'lapped': overrun.append(r)
                if status == 'torn': torn.append(r)
                if status == 'absent': gaps.append([r, r])
                last = r
                r += 1
    if fmt == 0:
        struct.pack_into('<I', memory, 8, 2)
        memory[48:64] = bytes(16)
    return dict(name=name, geometry=dict(slots=slots,width=width,height=height,fmt=fmt), frames=dict(gen='lcg',seed=seed),ops=ops,expect=dict(head=head,reads=reads,overrun=overrun,torn=torn,gaps=gaps,file_sha256=hashlib.sha256(memory).hexdigest()))

def p(r): return dict(op='publish',r=r)
def a(r): return dict(op='read',mode='acquire',r=r)
def poll(): return dict(op='read',mode='poll')
CASES = [
    ('empty', 2, [poll()]),
    ('one', 2, [p(0),poll()]),
    ('two', 2, [p(0),p(1),poll()]),
    ('wrap', 2, [p(i) for i in range(8)] + [poll()]),
    ('lap', 2, [p(0),p(2),a(0)]),
    ('gap', 4, [p(0),poll(),p(3),poll()]),
    ('absent', 2, [p(0),a(5)]),
    ('latest', 4, [p(0),p(1),p(2),dict(op='read',mode='latest')]),
    ('odd', 2, [dict(op='begin',r=0),a(0)]),
    ('crc_corrupt', 2, [p(0),dict(op='tear',r=0),a(0)]),
    ('poll_torn', 2, [p(0),dict(op='begin',r=2),poll()]),
    ('repeated_poll', 2, [p(0),poll(),poll(),p(1),poll()]),
    ('wrap3', 3, [p(i) for i in range(11)] + [a(0),a(10),poll()]),
    ('bgrx', 2, [p(0),p(1),poll()]),
]
if __name__ == '__main__':
    target = Path(__file__).resolve().parents[1] / 'fixtures/conformance'
    import python_reference
    for name, slots, ops in CASES:
        value = fixture(name, slots, ops, fmt=int(name == 'bgrx'))
        if name != 'bgrx':
            actual = python_reference.run(name, slots, ops, False)
            digest = actual['phases'][-1]['file_sha256']
            assert value['expect']['file_sha256'] == digest, name
            value['python_reference'] = dict(rtring_sha256=python_reference.sha(Path(python_reference.rtring.__file__).read_bytes()), file_sha256=digest)
        (target / (name + '.json')).write_text(json.dumps(value,sort_keys=True,separators=(',',':'))+'\n')
    # Regenerate matching single-frame v2 reader fixtures and their source receipt.
    python_reference.main()
