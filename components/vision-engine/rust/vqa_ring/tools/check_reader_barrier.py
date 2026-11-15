#!/usr/bin/env python3
"""Fail closed unless the release ri != r recheck has an acquire barrier.
Checks the isolated recheck boundary, not the unrelated initial Acquire load.
"""
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

CRATE = Path(__file__).resolve().parents[1]
SCRATCH = Path(os.environ.get('VQA_RING_SCRATCH', str(CRATE.parents[1] / 'build/room_R'))) / 'reader-barrier'


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--mutant', action='store_true')
    args = parser.parse_args()
    SCRATCH.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, PATH=os.path.expanduser('~/.cargo/bin')+':'+os.environ.get('PATH',''))
    base = ['cargo', 'rustc', '--release', '--offline', '--locked', '--lib', '--manifest-path', str(CRATE/'Cargo.toml'), '--target-dir', str(SCRATCH/'target')]
    if args.mutant: base += ['--features','mut-ri-fence']
    # Always retain assembly as an auditable fallback, including when objdump exists.
    build = subprocess.run(base+['--','--emit=asm'], env=env, capture_output=True, timeout=120)
    (SCRATCH/'build.log').write_bytes(build.stdout+build.stderr)
    if build.returncode: return 2
    binary = SCRATCH/'target/release/libvqa_ring.rlib'
    dump_tool = shutil.which('objdump') or shutil.which('llvm-objdump')
    if dump_tool:
        dump = subprocess.run([dump_tool,'-Cd',str(binary)],capture_output=True,text=True,timeout=30)
        if dump.returncode: return 2
        text = dump.stdout
        match = re.search(r'^[0-9a-f]+ <vqa_ring_index_recheck>:\n(.*?)(?=\n\n|\Z)',text,re.M|re.S)
        block = match.group(1) if match else ''
        # A fence in an unused helper is insufficient: reader code must call it.
        relocation = subprocess.run([dump_tool,'-Cr',str(binary)],capture_output=True,text=True,timeout=30)
        if relocation.returncode: return 2
        callers = relocation.stdout
        reader_sections = re.findall(r'RELOCATION RECORDS FOR \[([^\]]+)\]:\n(.*?)(?=\n\n|\Z)',callers,re.S)
        used = any('Reader' in name and 'vqa_ring_index_recheck' in body for name,body in reader_sections)
        (SCRATCH/('mutant.disassembly' if args.mutant else 'nominal.disassembly')).write_text(text)
    else:
        candidates = sorted((SCRATCH/'target/release/deps').glob('vqa_ring-*.s'), key=lambda p:p.stat().st_mtime_ns)
        if not candidates: return 2
        text = candidates[-1].read_text()
        match = re.search(r'^vqa_ring_index_recheck:\n(.*?)(?=^\s*\.Lfunc_end|\Z)',text,re.M|re.S)
        block = match.group(1) if match else ''
        used = bool(re.search(r'\bbl\s+vqa_ring_index_recheck',text))
    # On aarch64 DMB ISHLD or an acquiring final load orders ri before seq.
    architecture = os.uname().machine
    barrier = bool(re.search(r'\bdmb\s+ish(?:ld)?\b',block))
    ok = architecture == 'aarch64' and used and bool(block) and barrier
    print(json.dumps(dict(oracle='release-ri-branch-acquire',mutant=args.mutant,architecture=architecture,
                         caller_verified=used, acquire_barrier=barrier, instructions=block.strip(),ok=ok)))
    if not ok:
        print('MISSING_RI_ACQUIRE_BARRIER' if block and used and not barrier else 'BARRIER_HARNESS_ERROR',file=sys.stderr)
    return 0 if ok else (1 if block and used and not barrier else 2)

if __name__ == '__main__': sys.exit(main())
