"""Check RT0D01 sealed hashes, per-file output equality, and every measured timing target."""
import hashlib
import json
from pathlib import Path
import statistics

ROOT = Path(__file__).resolve().parents[3]
HERE = Path(__file__).resolve().parent


def main():
    receipt = json.loads((HERE / 'EQUIV_RECEIPT.json').read_text())
    proof_bytes = (HERE / 'PROOF_SHA_TABLE.json').read_bytes()
    assert receipt['proof_table_sha256'] == hashlib.sha256(proof_bytes).hexdigest()
    for entry in receipt['files']:
        assert entry['new_sha256'] == hashlib.sha256((ROOT / entry['path']).read_bytes()).hexdigest()
    proof = json.loads(proof_bytes)
    assert proof['verdict'] == 'PASS' and proof['rows']
    for row in proof['rows']:
        assert set(row['after']) == {'1', '2', '5'}
        assert all(files == row['sealed_serial'] for files in row['after'].values())
    timing = json.loads((HERE / 'TIMINGS.json').read_text())['rows']
    passed = []
    for resolution in ([1280, 720], [1920, 1080], [3840, 2160]):
        for scenario in ('clean_static', 'scroll'):
            arms = {}
            for workers in (1, 5):
                rows = [r for r in timing if r['resolution'] == resolution and
                        r['scenario'] == scenario and r['workers'] == workers]
                assert len(rows) == 3 and {r['repeat'] for r in rows} == {0, 1, 2}
                assert all(r['ms_per_frame'] > 0 and r['load1'] >= 0 for r in rows)
                arms[workers] = statistics.median(r['ms_per_frame'] for r in rows)
            assert arms[5] <= arms[1], (resolution, scenario, arms)
            passed.append({'resolution': resolution, 'scenario': scenario, 'speedup': arms[1] / arms[5]})
    print(json.dumps({'verdict': 'PASS', 'provenance': 'MEASURED', 'equivalent_inputs': len(proof['rows']),
                      'performance_targets': passed}, sort_keys=True))


if __name__ == '__main__':
    main()
