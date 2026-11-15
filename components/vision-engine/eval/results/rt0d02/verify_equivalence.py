"""RT0D01 RAM-only proof and whole-pipeline benchmark; run with taskset -c 5-9.
Evidence is written only in eval/results/rt0; sealed source extraction stays in build/room_D.
"""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import statistics
import subprocess
import sys
import tarfile
import time

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from visioncore import pipeline
from vadapt.synth import synthetic as syn

OUT = ROOT / 'eval/results/rt0d02'
SCRATCH = ROOT / 'build/room_D'
CPUS = list(range(5, 10))


def sha(data):
    return hashlib.sha256(data).hexdigest()


def write(path, data):
    path.write_text(json.dumps(data, sort_keys=True, indent=2) + '\n')


def sealed_pipeline():
    base = SCRATCH / 'sealed'
    base.mkdir(parents=True, exist_ok=True)
    archive = subprocess.run(['git', 'archive', 've-sealed-1', 'visioncore'], cwd=ROOT,
                             check=True, capture_output=True).stdout
    import io
    with tarfile.open(fileobj=io.BytesIO(archive)) as files:
        files.extractall(base, filter='data')
    spec = importlib.util.spec_from_file_location('sealed_rt0', base / 'visioncore/__init__.py',
                                                submodule_search_locations=[str(base / 'visioncore')])
    mod = importlib.util.module_from_spec(spec)
    sys.modules['sealed_rt0'] = mod
    spec.loader.exec_module(mod)
    from sealed_rt0 import pipeline as baseline
    return baseline


def digest(run):
    return {name: sha(blob) for name, blob in sorted(run._blobs().items())}


def source(frames, name, seed):
    return syn.SynthSource(frames, syn.build_manifest(frames, name, seed))


def main():
    if set(os.sched_getaffinity(0)) != set(CPUS):
        raise RuntimeError('requires CPUs 5-9')
    OUT.mkdir(parents=True, exist_ok=True)
    candidate_sha = sha((ROOT/'visioncore/change.py').read_bytes())
    sealed = sealed_pipeline()
    prior_path = ROOT / 'eval/results/rt0/PROOF_SHA_TABLE.json'
    prior = json.loads(prior_path.read_text())
    assert prior['verdict'] == 'PASS'
    prior_rows = {r['input']: r for r in prior['rows']}
    reference = subprocess.check_output(['git', 'show', 'a4d95ac:visioncore/change.py'], cwd=ROOT)
    sealed_change = subprocess.check_output(['git', 'show', 've-sealed-1:visioncore/change.py'], cwd=ROOT)
    assert reference == sealed_change, 'RT0D01 and sealed S2 must match'

    proof = {'schema': 'vqa.rt0.proof/v1', 'card_id': 'RT0D02', 'verdict': 'RUNNING',
             'sealed_tag': 've-sealed-1', 'sealed_commit': subprocess.check_output(
                 ['git', 'rev-parse', 've-sealed-1^{commit}'], cwd=ROOT, text=True).strip(),
             'workers': [1, 5], 'rows': [],
             'candidate_change_sha256': candidate_sha,
             'prior_proof_sha256': sha(prior_path.read_bytes()),
             'reference_rt_commit': 'a4d95ac',
             'reference_change_sha256': sha(reference),
             'method': 'Rerun every D01 input at W=1,5 against its sealed byte hashes, with identical input digest; new edge sources run sealed directly.',
             'scope_notes': ['X6/X7 executable inputs AUSENTE in eval/x*_*.py; X9 uses metadata only.',
                             'X3/X4 cover every registered test seed; calibration partitions are excluded.',
                             'GUI DEV: k=0, 320x180, 120 frames per kind (runner defaults).']}

    def check(label, frames, *, hints=None):
        src = source(frames, label, 1)
        input_sha = sha(b''.join(bytes.fromhex(f.sha256) for f in src))
        if label in prior_rows:
            prior_row = prior_rows[label]
            assert prior_row['match'] and prior_row['input_sha256'] == input_sha, label + ' input differs'
            expected = prior_row['sealed_serial']
        else:
            expected = digest(sealed.analyze(src, hints=hints))
        arms = {}
        for w in [1, 5]:
            got = digest(pipeline.analyze(src, hints=hints, workers=w, cpus=CPUS))
            arms[str(w)] = got
            if got != expected:
                raise AssertionError(label + ' differs at workers=' + str(w))
        proof['rows'].append({'input': label, 'input_sha256': sha(b''.join(
            bytes.fromhex(f.sha256) for f in src)), 'frames': len(frames),
            'sealed_serial': expected, 'after': arms, 'match': True})
        if len(proof['rows']) % 25 == 0:
            write(OUT / 'PROOF_SHA_TABLE.json', proof)
            print('proof cases', len(proof['rows']), flush=True)

    # All default X1/X2 captures, X8 items, and the seven tests/golden synth inputs.
    for seed in [1, 2, 3, 4]:
        for kind in syn.SCENARIOS:
            frames, _, hints = syn.render_scenario(kind, seed)
            check(f'X1-X8-golden/{kind}/{seed}', frames)
            if seed == 1:
                check(f'golden-hinted/{kind}', frames, hints=hints)
    from eval.x2_crossrun import _jitter, PAIRS
    for kind, seed in PAIRS:
        frames, _, _ = syn.render_scenario(kind, seed)
        check(f'X2-jitter/{kind}/{seed}', _jitter(frames, seed))
    from eval.x3_power import MUTANTS, build_trial, trial_spec
    from eval.x4_fp import CONTROLS, build_control
    manifest = json.loads((ROOT / 'eval/splits/x4_v3.json').read_text())
    for seed in manifest['partition']['test']:
        for kind in MUTANTS:
            for level in range(3):
                frames, _ = build_trial(trial_spec(kind, level, seed))
                check(f'X3/{kind}/{level}/{seed}', frames)
        for kind in CONTROLS:
            frames = build_control(kind, seed, W=640, H=400, n=40)
            check(f'X4/{kind}/{seed}', frames)
    for W, H in [(1280, 720), (1920, 1080)]:
        frames, _, _ = syn.render_scenario('gpu_jitter', 1, W, H, 200)
        check(f'X5/{W}x{H}', frames)
    for kind in ['clean_static', 'gpu_jitter', 'flicker']:
        frames, _, hints = syn.render_scenario(kind, 1, 1280, 720, 200)
        check('X10/' + kind, frames, hints=hints)
    from eval.gui_synth.scenes import KINDS, build
    from eval.gui_synth.gen import render
    for kind in KINDS:
        scene = build(kind, 0, w=320, h=180, n=120, split='DEV')
        check('gui_DEV/' + kind, [rgb for _, rgb in render(scene)])

    import numpy as np
    for W, H in [(1280, 720), (1920, 1080), (3840, 2160)]:
        for kind in ['clean_static', 'scroll']:
            frames, _, _ = syn.render_scenario(kind, 1, W, H, 60)
            check(f'rt_bench/{W}x{H}/{kind}', frames)
            write(OUT / 'PROOF_SHA_TABLE.json', proof)
            print('bench proof', W, H, kind, flush=True)
    rng = np.random.default_rng(912)
    odd = rng.integers(0, 256, (63, 95, 3), dtype=np.uint8)
    check('edge/odd_95x63', [np.roll(odd, (k*2,k*3), axis=(0,1)) for k in range(20)])
    base = syn.base_scene(23, 640, 400)
    from visioncore.grid import Grid
    size = Grid(640, 400).s
    check('edge/large_horizontal_scroll', [np.roll(base, (size+2)*k, axis=1) for k in range(20)])
    check('edge/large_vertical_scroll', [np.roll(base, (size+2)*k, axis=0) for k in range(20)])
    assert len(proof['rows']) == len(prior['rows']) + 3
    assert candidate_sha == sha((ROOT/'visioncore/change.py').read_bytes()), 'candidate changed during proof'
    proof['verdict'] = 'PASS'
    write(OUT / 'PROOF_SHA_TABLE.json', proof)
    proof_sha = sha((OUT / 'PROOF_SHA_TABLE.json').read_bytes())
    entries = []
    for path in ['visioncore/change.py']:
        old = subprocess.check_output(['git', 'show', 've-sealed-1:' + path], cwd=ROOT)
        entries.append({'path': path, 'old_sha256': sha(old), 'new_sha256': sha((ROOT / path).read_bytes()),
                        'proof_table_sha256': proof_sha, 'card_id': 'RT0D02'})
    write(OUT / 'EQUIV_RECEIPT.json', {'schema': 'vqa.rt0.equiv/v1', 'card_id': 'RT0D02',
          'proof_table': 'eval/results/rt0d02/PROOF_SHA_TABLE.json', 'proof_table_sha256': proof_sha, 'files': entries})
    print('PASS', len(proof['rows']), 'byte-identical cases', flush=True)


if __name__ == '__main__':
    main()
