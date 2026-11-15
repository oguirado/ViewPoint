# @module {"id":"wa.tools.cu_offline_e2e","layer":2,"owner":"ARCH","deps":["wa.learn.cu_loop"],"book":"HB-WA-s4-learn"}
"""Exercise the real C2 bridge against recorded RAM-only Holdings frames."""
import argparse
import json
from pathlib import Path
import shutil
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from wender.learn.cu_loop import main as loop_main


# @anchor:wa.tools.cu_offline_e2e.main
def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--scenario', required=True)
    parser.add_argument('--ve-root', required=True)
    args = parser.parse_args(argv)
    if Path(args.scenario).name != args.scenario or args.scenario in ('.', '..'):
        raise ValueError('scenario must be a directory name')
    source = Path('/dev/shm/vqa-ramws/opensuperficies/os-cu-0/recorded_holdings_v1.json')
    data = json.loads(source.read_text())
    if data['schema'] != 'os-cu.holdings-recorded-frames/v1':
        raise ValueError('unsupported recording schema')
    frames = sorted((r for r in data['frames'] if r['scenario'] == args.scenario),
                    key=lambda r: r['leg'])
    if not frames or frames[0]['leg'] != -1 or len({r['leg'] for r in frames}) != len(frames):
        raise ValueError('recording needs one baseline and unique legs')
    directory = Path('/dev/shm/vqa-cu/offline') / args.scenario
    directory.mkdir(parents=True, exist_ok=True)
    rows = []
    for seq, frame in enumerate(frames):
        path = Path(frame['path']).resolve()
        if not path.is_relative_to('/dev/shm'):
            raise ValueError('recorded frame must be in RAM')
        name = f'f{seq:06d}.png'
        shutil.copyfile(path, directory / name)
        rows.append(dict(seq=seq, png=name, state_sha256=frame['state_sha256'], tick=frame['tick']))
    (directory / 'index.json').write_text(json.dumps(rows))
    action = {'schema': 'cu.action/v1', 'observation': 'advance recorded leg',
              'action': {'kind': 'key', 'keysym': 'Tab'},
              'expectation': {'kind': 'state_change'}, 'confidence': 1}
    actions = [action] * (len(frames) - 1) + [dict(action, action={'kind': 'done'},
                                                        expectation={'kind': 'none'})]
    replay = directory / 'replay.json'
    replay.write_text(json.dumps(actions))
    trace = directory / 'trace.jsonl'
    trace.write_text('')
    # Capture the CLI summary to enforce the scenario's verification oracle.
    import contextlib
    import io
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        status = loop_main(['--goal', 'Replay Holdings ' + args.scenario,
                            '--capsule', 'fake:' + str(directory),
                            '--decider', 'replay:' + str(replay), '--ve-root', args.ve_root,
                            '--trace', str(trace), '--max-steps', str(len(frames)),
                            '--max-ms', '300000'])
    if status:
        return status
    summary = json.loads(output.getvalue())
    print(json.dumps(summary))
    return 0 if (summary['verified_ok'] == len(frames) - 1 and
                 summary['stop_reason'] == 'done' and summary['failures'] == 0) else 1


if __name__ == '__main__':
    raise SystemExit(main())
