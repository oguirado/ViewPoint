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

OUT = ROOT / 'eval/results/rt0'
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
    sealed = sealed_pipeline()
    proof = {'schema': 'vqa.rt0.proof/v1', 'card_id': 'RT0D01', 'verdict': 'RUNNING',
             'sealed_tag': 've-sealed-1', 'sealed_commit': subprocess.check_output(
                 ['git', 'rev-parse', 've-sealed-1^{commit}'], cwd=ROOT, text=True).strip(),
             'workers': [1, 2, 5], 'rows': [],
             'scope_notes': ['X6/X7 executable inputs AUSENTE in eval/x*_*.py; X9 uses metadata only.',
                             'X3/X4 cover every registered test seed; calibration partitions are excluded.',
                             'GUI DEV: k=0, 320x180, 120 frames per kind (runner defaults).']}

    def check(label, frames, *, hints=None):
        src = source(frames, label, 1)
        expected = digest(sealed.analyze(src, hints=hints))
        arms = {}
        for w in [1, 2, 5]:
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

    timing = {'schema': 'vqa.rt0.timing/v1', 'claim': 'MEASURED',
              'method': 'analyze wall time including fork/S0/S1/S2/S3/S4/S5; frames pre-rendered in RAM; 60 frames; no decode; median of 3 repeats',
              'cpus': CPUS, 'rows': []}
    for W, H in [(1280, 720), (1920, 1080), (3840, 2160)]:
        for kind in ['clean_static', 'scroll']:
            frames, _, _ = syn.render_scenario(kind, 1, W, H, 60)
            check(f'rt_bench/{W}x{H}/{kind}', frames)
            src = source(frames, kind, 1)
            samples = {1: [], 5: []}
            for rep in range(3):
                # Alternate arm ordering to reduce systematic warm-cache bias.
                for w in ([1, 5] if rep % 2 == 0 else [5, 1]):
                    os.sched_setaffinity(0, set(CPUS[:w]))
                    load = os.getloadavg()[0]
                    start = time.perf_counter_ns()
                    run = pipeline.analyze(src, workers=w, cpus=CPUS)
                    elapsed = time.perf_counter_ns() - start
                    samples[w].append(elapsed / 60 / 1e6)
                    timing['rows'].append({'resolution': [W, H], 'scenario': kind,
                        'workers': w, 'repeat': rep, 'load1': load, 'ms_per_frame': samples[w][-1],
                        'output_sha256': sha(json.dumps(digest(run), sort_keys=True).encode())})
            os.sched_setaffinity(0, set(CPUS))
            print('timing', W, H, kind, {w: statistics.median(v) for w, v in samples.items()}, flush=True)
            write(OUT / 'TIMINGS.json', timing)
    proof['verdict'] = 'PASS'
    write(OUT / 'PROOF_SHA_TABLE.json', proof)
    proof_sha = sha((OUT / 'PROOF_SHA_TABLE.json').read_bytes())
    entries = []
    for path in ['visioncore/parallel.py', 'visioncore/pipeline.py']:
        old = subprocess.check_output(['git', 'show', 've-sealed-1:' + path], cwd=ROOT)
        entries.append({'path': path, 'old_sha256': sha(old), 'new_sha256': sha((ROOT / path).read_bytes()),
                        'proof_table_sha256': proof_sha, 'card_id': 'RT0D01'})
    write(OUT / 'EQUIV_RECEIPT.json', {'schema': 'vqa.rt0.equiv/v1', 'card_id': 'RT0D01',
          'proof_table': 'eval/results/rt0/PROOF_SHA_TABLE.json', 'proof_table_sha256': proof_sha, 'files': entries})
    print('PASS', len(proof['rows']), 'byte-identical cases', flush=True)


if __name__ == '__main__':
    main()
