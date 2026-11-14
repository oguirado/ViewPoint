#!/usr/bin/env python3
"""Drive the real Python v2 implementation. Frame files exist only in /dev/shm.
JSON fixtures persist digests, never payload bytes. --live emits transient bytes
through stdout for Rust's direct byte comparison, without writing them to disk.
"""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import tempfile

sys.dont_write_bytecode = True
CRATE = Path(__file__).resolve().parents[1]
REPO = CRATE.parents[1]
sys.path.insert(0, str(REPO))
from vadapt import rtring
from generate_fixtures import CASES


def sha(data):
    return hashlib.sha256(data).hexdigest()


class Workspace:
    # Storage/quota isolation adapter only: all ring operations use rtring.py.
    def __init__(self, root): self.root = Path(root)
    def path(self, rel):
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        return p
    def reserve(self, size): pass
    def release(self, path): Path(path).unlink(missing_ok=True)
    def account(self): pass


def run(name, slots, ops, live):
    with tempfile.TemporaryDirectory(prefix='vqa-sr1fx-', dir='/dev/shm') as root:
        w = rtring.RingWriter(Workspace(root), slots, 4, 4)
        reader = rtring.RingReader(w.path, root=root)
        phases, reads = [], []
        def record(index, fr, status=None):
            row = dict(r=index, status='frame' if fr is not None else status)
            if fr is not None:
                payload = fr.view.tobytes()
                row.update(crc32=rtring.zlib.crc32(payload), sha256=sha(payload))
            return row
        def snapshot():
            memory = w._mm[:]
            row = dict(header=memory[:64].hex(), slots=memory[64:64+slots*64].hex(),
                       payload_sha256=sha(memory[w.data_off:]), file_sha256=sha(memory))
            if live: row['bytes'] = memory.hex()
            phases.append(row)
        snapshot()
        try:
            for op in ops:
                kind = op['op']
                if kind in ('begin', 'publish'):
                    index = op['r']
                    data = rtring.np.array([(index*131+i*7+19)&255 for i in range(48)], dtype='uint8').reshape(4,4,3)
                    if kind == 'publish': w.publish(data, index)
                    else:
                        view = w.begin(index)
                        view[...] = data
                        del view
                elif kind == 'tear':
                    off = w.data_off + op['r'] % slots * 48
                    w._mm[off] ^= 255
                elif kind == 'read':
                    if op['mode'] == 'acquire':
                        # _try supplies the status that acquire intentionally hides.
                        fr, status = reader._try(op['r'], 8)
                        reads.append(record(op['r'], fr, status))
                        del fr
                        fr = reader.acquire(op['r'])
                    else:
                        fr = reader.poll(latest=op['mode'] == 'latest')
                        if fr is not None: reads.append(record(fr.render_index, fr))
                    del fr
                snapshot()
            return dict(name=name, geometry=dict(slots=slots,width=4,height=4,fmt=0), ops=ops,
                        phases=phases, expect=dict(reads=reads,last=reader.last,overrun=reader.overrun,
                        torn=reader.torn,gaps=reader.gaps))
        finally:
            reader._mm.close()
            w.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--live', action='store_true')
    args = parser.parse_args()
    cases = [run(n,s,o,args.live) for n,s,o in CASES if n != 'bgrx']
    result = dict(rtring_sha256=sha(Path(rtring.__file__).read_bytes()), cases=cases)
    if args.live:
        print(json.dumps(result, separators=(',',':')))
    else:
        target = CRATE / 'fixtures/python_v2'
        target.mkdir(exist_ok=True)
        hashes = {}
        for case in cases:
            p = target / (case['name']+'.json')
            p.write_text(json.dumps(case,sort_keys=True,separators=(',',':'))+'\n')
            hashes[p.name] = sha(p.read_bytes())
        (CRATE / 'fixtures/python_v2_receipt.json').write_text(json.dumps(dict(
            rtring_sha256=result['rtring_sha256'], fixtures=hashes), indent=2,sort_keys=True)+'\n')

if __name__ == '__main__': main()
