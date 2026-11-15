#!/usr/bin/env python3
"""Execution mutant gate. Surviving memory-order mutants fail the gate.
No source patching: Cargo features each remove exactly the named operation.
Logs/receipt go exclusively to the lane scratch directory. Frames stay in memfd.
"""
import hashlib
import re
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

CRATE = Path(__file__).resolve().parents[1]
CASES = [
    ('M-PARITY', 'mut-parity', 'o4_odd_and_crc', 1),
    ('M-FENCE', 'mut-fence', 'writer_fence_stress', 10),
    ('M-PRIVATE', 'mut-private', 'shared_process_wake', 1),
    ('M-SEALCHK', 'mut-sealchk', 'n1_n2_seals', 1),
    ('M-HEADORDER', 'mut-headorder', 'head_publication_order', 1),
    ('M-RI-FENCE', 'mut-ri-fence', 'reader_index_acquire_structure', 1),
]

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--scratch', type=Path, default=Path(os.environ.get('VQA_RING_SCRATCH', str(CRATE.parents[1] / 'build/room_R'))))
    parser.add_argument('--only', choices=[c[0] for c in CASES])
    parser.add_argument('--ri-seconds', type=int, choices=range(1,61), default=3)
    args = parser.parse_args()
    scratch = args.scratch.resolve()
    required = (CRATE.parents[1] / 'build').resolve()
    if scratch != required and required not in scratch.parents:
        parser.error('scratch must be inside build/')
    scratch.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, VQA_STRESS='1', PATH=os.path.expanduser('~/.cargo/bin') + ':' + os.environ.get('PATH', ''))
    base = ['taskset', '-c', '2-4', 'cargo', 'test', '--release', '--offline', '--locked', '--manifest-path', str(CRATE / 'Cargo.toml'), '--target-dir', str(scratch / 'target')]
    rows = []
    for name, feature, test, repeats in CASES:
        if args.only and name != args.only:
            continue
        env['VQA_STRESS_SECONDS'] = str(args.ri_seconds)
        # Nominal oracle must run and pass, before mutated execution counts.
        nominal = subprocess.run(base + [test, '--', '--test-threads=1', '--nocapture', '--include-ignored'], env=env, capture_output=True, timeout=120)
        (scratch / (name + '-nominal.log')).write_bytes(nominal.stdout + nominal.stderr)
        nominal_ok = nominal.returncode == 0 and b'1 passed;' in nominal.stdout
        runs = []
        for i in range(repeats):
            out = subprocess.run(base + ['--features', feature, test, '--', '--test-threads=1', '--nocapture', '--include-ignored'], env=env, capture_output=True, timeout=120)
            log = scratch / f'{name}-{i}.log'
            log.write_bytes(out.stdout + out.stderr)
            # Compilation failure, zero tests, harness timeout are never kills.
            combined = out.stdout + out.stderr
            witness = b'panicked at' in combined
            if name == 'M-FENCE':
                witness = any(int(n) > 0 for n in re.findall(rb'violations=(\d+)', combined))
            elif name == 'M-RI-FENCE':
                witness = b'MISSING_RI_ACQUIRE_BARRIER' in combined
            elif name == 'M-HEADORDER':
                witness = b'head visible before even seq' in combined
            killed = nominal_ok and out.returncode == 101 and b'1 failed;' in out.stdout and witness
            runs.append(dict(rc=out.returncode, killed=killed, log=str(log)))
        row = dict(id=name, nominal_rc=nominal.returncode, kills=sum(r['killed'] for r in runs), runs=repeats, results=runs)
        rows.append(row)
        print(json.dumps(row), flush=True)
    secondary = []
    if args.only in (None, 'M-RI-FENCE'):
        # Secondary hardware evidence never substitutes for the structural gate.
        for mutated in (False, True):
            for i in range(5):
                command = base + (['--features', 'mut-ri-fence'] if mutated else []) + ['ri_fence_stress', '--', '--test-threads=1', '--nocapture', '--include-ignored']
                out = subprocess.run(command, env=env, capture_output=True, timeout=120)
                combined = out.stdout + out.stderr
                log = scratch / f'RI-secondary-{int(mutated)}-{i}.log'
                log.write_bytes(combined)
                counts = [int(n) for n in re.findall(rb'violations=(\d+)', combined)]
                secondary.append(dict(mutated=mutated, rc=out.returncode, violations=counts[-1] if counts else None, log=str(log)))
        print(json.dumps(dict(ri_secondary=secondary)), flush=True)
    source_sha256 = {str(p.relative_to(CRATE)):hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted((CRATE/'src').glob('*.rs'))}
    result = dict(ri_secondary=secondary, source_sha256=source_sha256, status='PASS' if all(r['nominal_rc'] == 0 and r['kills'] > 0 for r in rows) else 'FAIL', mutants=rows)
    if any(r['rc'] != 0 or r['violations'] != 0 for r in secondary if not r['mutated']):
        result['status'] = 'FAIL'
    (scratch / ('mutants-' + args.only + '.json' if args.only else 'mutants.json')).write_text(json.dumps(result, indent=2)+'\n')
    return 0 if result['status'] == 'PASS' else 5

if __name__ == '__main__':
    sys.exit(main())
