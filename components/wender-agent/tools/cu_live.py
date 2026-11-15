# @module {"id":"wa.tools.cu_live","layer":4,"owner":"ARCH","deps":["wa.learn.cu_loop","wa.learn.cu_tasks","wa.learn.model_client","wa.learn.script_runtime","wa.learn.script_live"],"book":"HB-WA-s4-learn"}
"""Run a bounded Holdings episode with reviewable ownership and goal evidence.

Producer identity and READY format: OpenSuperficies/src/holdingscap/holdingscap.go.
Unknown ownership requires operational reconciliation before another launch.
"""
import argparse
import contextlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import uuid
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from wender.learn.cu_tasks import (DEFAULT_TASKS_FILE, load_task, parse_predicate,
                                   render_predicate, evaluate_predicate, extract_status,
                                   predicate_distance, task_family_hint)

_BINARY = os.environ.get('SRF_HOLDINGS_CAPSULE_BINARY', os.path.join(os.environ.get('OPENSUPERFICIES_ROOT', str(Path(__file__).resolve().parents[2] / 'OpenSuperficies')), 'build', 'w8_install', 'srf-holdings-capsule'))
_CONTAINER = re.compile(r'srf-ng-hcap-hc-[0-9a-f]{12}-[0-9]{1,2}\Z')


# @anchor:wa.tools.cu_live.loop_main
def loop_main(argv=None, *, goal_check=None, goal_distance=None):
    """Legacy loop CLI composition with an explicitly registered dispatcher.

    Mirrors the delegated cu_loop CLI's transport arguments and output contract.
    The sealed CLI has no constructor injection port; no globals are patched.
    """
    from wender.learn import cu_loop as loop
    from wender.learn.script_live import LegacyDispatchOwner
    parser = argparse.ArgumentParser(description=loop.__doc__)
    for name in ('goal', 'capsule', 'decider', 'trace'):
        parser.add_argument('--' + name, required=True)
    for name, default in (('max-steps', 20), ('no-progress-steps', 2),
                          ('max-ms', 30000), ('after-timeout-ms', 1500)):
        parser.add_argument('--' + name, type=int, default=default)
    parser.add_argument('--capsule-binary', default='srf-holdings-capsule')
    parser.add_argument('--no-state', action='store_true')
    parser.add_argument('--ve-root')
    parser.add_argument('--mask', action='append', default=[])
    parser.add_argument('--effort', choices=('auto', 'low', 'medium', 'xhigh'), default='auto')
    parser.add_argument('--images', choices=('marks', 'full'), default='marks')
    parser.add_argument('--muse-timeout', type=float, default=240)
    parser.add_argument('--muse-workspace', default=None)
    parser.add_argument('--keep-crops', action='store_true')
    parser.add_argument('--no-marks', action='store_true')
    args = parser.parse_args(argv)
    owner = decider = None
    try:
        if args.capsule.startswith('srf:'):
            capsule = loop.SrfCapsule(args.capsule[4:], args.capsule_binary,
                state_available=not args.no_state, timeout=max(.001, args.max_ms / 1000))
            owner = LegacyDispatchOwner(capsule)
            capsule.dispatcher = owner
        elif args.capsule.startswith('fake:'):
            capsule = loop.FakeCapsule(args.capsule[5:])
        else:
            raise ValueError('capsule must be srf:<container> or fake:<dir>')
        masks = [tuple(map(int, mask.split(','))) for mask in args.mask]
        describer = loop.BridgeDescriber(args.ve_root, masks=masks,
            marks=not args.no_marks and isinstance(capsule, loop.SrfCapsule))
        if args.decider.startswith('replay:'):
            backend = loop.ReplayBackend(args.decider[7:])
        elif args.decider == 'muse':
            backend = loop.MuseBackend(args.muse_workspace, effort=args.effort, timeout=args.muse_timeout)
        else:
            raise ValueError('decider must be replay:<file> or muse')
        decider = loop.ModelDecider(backend, keep_crops=args.keep_crops, images=args.images)
        try:
            with open(args.trace, 'a', encoding='utf-8') as trace:
                summary = loop.run_loop(capsule, describer, decider, goal=args.goal, trace=trace,
                    max_steps=args.max_steps, max_ms=args.max_ms,
                    after_timeout_ms=args.after_timeout_ms, pixel_only=args.no_state,
                    goal_check=goal_check, goal_distance=goal_distance,
                    no_progress_steps=args.no_progress_steps)
        finally:
            decider.close()
        print(json.dumps(summary))
        return 0 if summary['stop_reason'] in ('done', 'goal_reached') else 2
    except (ValueError, KeyError, OSError, subprocess.SubprocessError,
            loop.BackendError, loop.VisionBridgeError) as exc:
        print(json.dumps({'stop_reason': 'failure', 'goal_verified': False,
            'failure': {'stage': 'setup', 'error_class': type(exc).__name__,
                        'input_effect_unknown': False}}))
        return 2
    finally:
        if owner is not None:
            owner.close()


def _save(directory, name, value):
    (directory / name).write_text(json.dumps(value, sort_keys=True) + '\n')


def _json(text):
    def pairs(items):
        value = {}
        for key, item in items:
            if key in value:
                raise ValueError('duplicate JSON field')
            value[key] = item
        return value
    def constant(value):
        raise ValueError('nonfinite JSON value')
    return json.loads(text, object_pairs_hook=pairs, parse_constant=constant)


def _text(value):
    return value.decode('utf-8', errors='replace') if isinstance(value, bytes) else value or ''


def _owned(slots):
    entries = slots if isinstance(slots, list) else [slots]
    owned = []
    unknown = not isinstance(slots, list) or not entries
    for entry in entries:
        name = entry.get('container') if isinstance(entry, dict) else None
        if isinstance(name, str) and _CONTAINER.fullmatch(name):
            if name not in owned:
                owned.append(name)
        else:
            unknown = True
    return owned, unknown


def _validate_start(slots, seed):
    if not isinstance(slots, list) or len(slots) != 1 or not isinstance(slots[0], dict):
        raise ValueError('start must return one slot in a JSON array')
    slot = slots[0]
    if slot.get('ready_line') != f'HOLDINGS_READY {seed} 0':
        raise ValueError('READY line does not match requested seed')
    for key in ('run_id', 'display', 'lane_socket', 'frames_dir', 'container'):
        if not isinstance(slot.get(key), str) or not slot[key]:
            raise ValueError('incomplete start response: ' + key)
    for key in ('slot', 'width', 'height', 'ready_ms'):
        if type(slot.get(key)) is not int or slot[key] < (0 if key == 'ready_ms' else 1):
            raise ValueError('invalid start response: ' + key)
    if slot['slot'] != 1 or not _CONTAINER.fullmatch(slot['container']):
        raise ValueError('invalid capsule identity')
    if not Path(slot['frames_dir']).resolve().is_relative_to('/dev/shm'):
        raise ValueError('frames must stay in RAM')


# @anchor:wa.tools.cu_live.launch_scripts
def launch_scripts(args, container, goal, trace_path, *, runtime_factory,
                   goal_check=None, goal_distance=None, metrics_sink=None):
    """Construct the existing live loop dependencies and inject one exact owner.

    runtime_factory is the S11A composition port. The default construction
    remains unready until live services are installed; runtime gates fence input.
    """
    from wender.learn.cu_loop import SrfCapsule, BridgeDescriber, ModelDecider, run_loop
    from wender.learn.model_client import MuseBackend
    from wender.learn.script_runtime import ScriptRuntime
    from wender.learn.script_live import (instrument_muse, install_live_dispatch,
        complete_model_episode)
    if not callable(runtime_factory):
        raise ValueError('LIVE_SERVICE_INSTALLATION_MISSING')
    with tempfile.TemporaryDirectory(prefix='wender-scripts-', dir='/dev/shm') as ram:
        capsule = SrfCapsule(container, _BINARY, timeout=max(.001, args.max_ms / 1000))
        masks = [tuple(map(int, value.split(','))) for value in args.mask]
        describer = BridgeDescriber(args.ve_root, masks=masks, marks=True)
        backend = MuseBackend(ram, effort=args.effort, timeout=args.muse_timeout)
        runtime = runtime_factory(mode='live')
        if type(runtime) is not ScriptRuntime:
            raise ValueError('RUNTIME_CONFIGURATION')
        decider = release_provider = None
        try:
            install_live_dispatch(runtime, capsule)
            runtime.measure_resident('before_release')
            # Only the concrete Muse backend carries this provider measurement.
            if type(backend) is MuseBackend:
                release_provider = instrument_muse(runtime, backend)
            decider = ModelDecider(backend, keep_crops=args.keep_crops, images=args.images)
            with trace_path.open('a', encoding='utf-8') as trace:
                summary = run_loop(capsule, describer, decider, goal=goal, trace=trace,
                    max_steps=args.max_steps, max_ms=args.max_ms,
                    goal_check=goal_check, goal_distance=goal_distance,
                    no_progress_steps=args.no_progress_steps, integration=runtime)
            evaluation_policy = dict(runtime.configuration.policies).get('evaluation')
            if evaluation_policy is not None:
                summary['scripts_policy_version'] = evaluation_policy.version
            try:
                support = getattr(runtime, 'episode_owner', None)
                complete_model_episode(runtime, summary=summary,
                    observation=getattr(support, 'final_observation', None), episode_owner=support)
            except (ValueError, OSError):
                # Learning failure cannot change independently verified task evidence.
                summary['learning_complete'] = False
            verified_script = (summary.get('stop_reason') == 'script_completed'
                and summary.get('goal_verified') is True
                and runtime.verified_completion())
            status = 0 if summary.get('stop_reason') in ('done', 'goal_reached') or verified_script else 2
        finally:
            try:
                if release_provider is not None:
                    release_provider()
                if decider is not None:
                    decider.close()
            finally:
                drained = runtime.close()
                runtime.measure_resident('after_release')
                metrics = runtime.episode_metrics()
                if metrics_sink is not None:
                    metrics_sink(metrics)
        return summary, status if drained else 2, metrics


# @anchor:wa.tools.cu_live.main
def main(argv=None, *, runtime_factory=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--app')
    parser.add_argument('--scenario')
    parser.add_argument('--seed', type=int)
    parser.add_argument('--goal')
    parser.add_argument('--task')
    parser.add_argument('--tasks-file', default=DEFAULT_TASKS_FILE)
    parser.add_argument('--ve-root')
    parser.add_argument('--scripts', choices=('on', 'off'), default='off')
    parser.add_argument('--dry-check', action='store_true')
    parser.add_argument('--scripts-pin', type=Path)
    parser.add_argument('--scripts-policy', type=Path)
    parser.add_argument('--effort', choices=('auto', 'low', 'medium', 'xhigh'), default='auto')
    parser.add_argument('--images', choices=('marks', 'full'), default='marks')
    parser.add_argument('--muse-timeout', type=float, default=240)
    parser.add_argument('--muse-workspace',
                        help='Deprecated compatibility option; the loop owns an empty RAM workspace')
    parser.add_argument('--run-dir')
    parser.add_argument('--max-steps', type=int)
    parser.add_argument('--no-progress-steps', type=int, default=2)
    parser.add_argument('--max-ms', type=int, default=300000)
    parser.add_argument('--mask', action='append', default=[])
    parser.add_argument('--keep-crops', action='store_true')
    args = parser.parse_args(argv)
    if args.scripts == 'on' and args.dry_check:
        from wender.learn.script_runtime import check_offline_construction, build_offline_runtime
        report = check_offline_construction(runtime_factory or build_offline_runtime)
        print(json.dumps(report, sort_keys=True))
        return 0 if report['construction_complete'] and report['cleanup_complete'] else 2
    if args.dry_check:
        parser.error('--dry-check requires --scripts on')
    if args.app is None or args.ve_root is None:
        parser.error('--app and --ve-root are required for an episode')
    directory = (Path(args.run_dir) if args.run_dir else
                 Path('/dev/shm/vqa-cu/live') / uuid.uuid4().hex).resolve()
    # Never overwrite the earlier episode's evidence.
    if directory.exists() and (not directory.is_dir() or any(directory.iterdir())):
        print(json.dumps({'stage': 'preflight', 'error_class': 'RunDirectoryNotEmpty'}))
        return 2
    directory.mkdir(parents=True, exist_ok=True)
    requested = {key: value for key, value in vars(args).items()
                 if key not in ('scripts', 'dry_check') and key not in ('scripts_pin', 'scripts_policy')}
    requested['run_dir'] = str(directory)
    summary = {'requested': requested, 'terminal_summary': None,
               'transport_completed': False, 'model_claimed_done': False,
               'goal_evidence': 'not_reached',
               'goal_verified': False,
               'primary_outcome': None}
    start = {'attempted': False, 'stdout': '', 'returncode': None}
    cleanup = {'ownership': 'not_started', 'containers': [], 'results': [], 'ok': False}
    owned = []
    stage = 'preflight'
    status = 2
    episode_record = None
    episode_records = []
    output = io.StringIO()
    try:
        goal_check = None
        goal_distance = None
        if args.task:
            task = load_task(args.task, args.tasks_file)
            predicate = parse_predicate(task['success_predicate'])
            goal_distance = lambda frame: predicate_distance(predicate, extract_status(frame))
            goal_check = lambda frame: evaluate_predicate(predicate, extract_status(frame))
            for key, task_key in (('goal', 'goal'), ('scenario', 'scenario'),
                                  ('seed', 'seed'), ('max_steps', 'max_actions')):
                if getattr(args, key) is None:
                    setattr(args, key, task[task_key])
            summary['task'] = task
        elif any(getattr(args, key) is None for key in ('goal', 'scenario', 'seed')):
            raise ValueError('provide --task or --goal, --scenario and --seed')
        if args.max_steps is None:
            args.max_steps = 20
        prompt_goal = args.goal
        if args.task:
            prompt_goal += '\n' + render_predicate(predicate)
            hint = task_family_hint(args.task)
            if hint:
                prompt_goal += '\nTask-family hint: ' + hint
        for key in ('app', 've_root'):
            path = Path(getattr(args, key)).resolve(strict=True)
            if not path.is_dir():
                raise ValueError(key + ' must resolve to a directory')
            setattr(args, key, str(path))
        if not (Path(args.app) / 'holdings-h2l').is_file():
            raise ValueError('app must contain holdings-h2l')
        if args.no_progress_steps <= 0 or args.seed < 0 or args.max_steps <= 0 or args.max_ms <= 0 or not args.goal.strip():
            raise ValueError('invalid experiment parameters')
        summary['effective'] = dict({key: value for key, value in vars(args).items()
                                     if key not in ('scripts', 'dry_check') and key not in ('scripts_pin', 'scripts_policy')}, capsule_binary=_BINARY)
        if args.scripts == 'on' and runtime_factory is None:
            from wender.learn.script_runtime import build_runtime
            runtime_factory = build_runtime
        stage = 'start'
        start['attempted'] = True
        cleanup['ownership'] = 'unknown'
        timed_out = False
        try:
            result = subprocess.run([_BINARY, 'start', '--slots', '1', '--app', args.app,
                                     '--scenario', args.scenario, '--seed', str(args.seed)],
                                    capture_output=True, text=True, check=False, timeout=120)
            start.update(stdout=result.stdout, returncode=result.returncode)
        except subprocess.TimeoutExpired as exc:
            timed_out = True
            start.update(stdout=_text(exc.stdout), error_class='TimeoutExpired')
        # Recover ownership before checking readiness or return status.
        try:
            slots = _json(start['stdout'])
        except ValueError:
            if timed_out:
                raise subprocess.TimeoutExpired('capsule start', 120) from None
            raise
        start['response'] = slots
        owned, unknown = _owned(slots)
        cleanup.update(containers=owned, ownership='unknown' if unknown or timed_out else 'known')
        _save(directory, 'start.json', start)
        if timed_out:
            raise subprocess.TimeoutExpired('capsule start', 120)
        if start['returncode'] != 0:
            raise RuntimeError('capsule start failed')
        _validate_start(slots, args.seed)
        if unknown:
            raise ValueError('capsule ownership unknown')
        stage = 'loop'
        command = ['--capsule', 'srf:' + owned[0], '--capsule-binary', _BINARY,
                   '--decider', 'muse', '--goal', prompt_goal,
                   '--trace', str(directory / 'trace.jsonl'), '--effort', args.effort,
                   '--images', args.images, '--muse-timeout', str(args.muse_timeout),
                   '--ve-root', args.ve_root,
                   '--no-progress-steps', str(args.no_progress_steps),
                   '--max-steps', str(args.max_steps), '--max-ms', str(args.max_ms)]
        for mask in args.mask:
            command += ['--mask', mask]
        if args.keep_crops:
            command += ['--keep-crops']
        with contextlib.redirect_stdout(output):
            if args.scripts == 'on':
                terminal, status, episode_record = launch_scripts(args, owned[0], prompt_goal,
                    directory / 'trace.jsonl', runtime_factory=runtime_factory,
                    goal_check=goal_check, goal_distance=goal_distance, metrics_sink=episode_records.append)
                print(json.dumps(terminal))
            else:
                status = (loop_main(command, goal_check=goal_check, goal_distance=goal_distance) if goal_check is not None
                          else loop_main(command))
        terminal = _json(output.getvalue())
        if not isinstance(terminal, dict):
            raise ValueError('loop summary must be an object')
        summary['terminal_summary'] = terminal  # Preserve F-LOOP evidence without renaming fields.
        summary['model_claimed_done'] = terminal.get('stop_reason') == 'done'
        summary['transport_completed'] = status == 0 and (summary['model_claimed_done'] or
            terminal.get('stop_reason') == 'goal_reached' or (args.scripts == 'on' and
            terminal.get('stop_reason') == 'script_completed' and terminal.get('goal_verified') is True))
        if status == 0 and not summary['transport_completed']:
            status = 2
        summary['primary_outcome'] = {'stage': stage, 'exit_code': status}
    except Exception as exc:
        status = 2
        summary['primary_outcome'] = {'stage': stage, 'exit_code': status,
                                      'error_class': type(exc).__name__}
        if type(exc) is ValueError and str(exc) == 'LIVE_SERVICE_INSTALLATION_MISSING':
            summary['primary_outcome']['error'] = 'LIVE_SERVICE_INSTALLATION_MISSING'
    finally:
        # Output emitted before an exception is still evidence, never a replacement outcome.
        if summary['terminal_summary'] is None and output.getvalue().strip():
            try:
                terminal = _json(output.getvalue())
                if isinstance(terminal, dict):
                    summary['terminal_summary'] = terminal
            except ValueError:
                pass
        terminal = summary['terminal_summary']
        if isinstance(terminal, dict):
            summary['model_claimed_done'] = terminal.get('stop_reason') == 'done'
        summary['goal_verified'] = bool(args.task and isinstance(terminal, dict) and
                                        terminal.get('goal_verified') is True)
        summary['goal_evidence'] = 'verified' if summary['goal_verified'] else 'not_reached'
        for container in owned:
            record = {'container': container, 'ok': False}
            try:
                result = subprocess.run([_BINARY, 'stop', '--container', container],
                                        check=False, capture_output=True, text=True, timeout=120)
                record['returncode'] = result.returncode
                record['ok'] = result.returncode == 0
            except Exception as exc:
                record['error_class'] = type(exc).__name__
            cleanup['results'].append(record)
        cleanup['ok'] = cleanup['ownership'] == 'known' and bool(owned) and all(
            record['ok'] for record in cleanup['results'])
        cleanup['recovery_required'] = start['attempted'] and not cleanup['ok']
        summary['cleanup'] = cleanup
        summary['exit_code'] = status if status else (0 if cleanup['ok'] else 2)
        _save(directory, 'start.json', start)
        _save(directory, 'terminal.json', summary['terminal_summary'])
        _save(directory, 'cleanup.json', cleanup)
        _save(directory, 'summary.json', summary)
        if episode_records or episode_record is not None:
            (directory / 'script_metrics.jsonl').write_text(
                episode_records[-1] if episode_records else episode_record, encoding='utf-8')
    print(json.dumps(summary))
    if episode_records:
        print(episode_records[-1], end='')
    elif episode_record is not None:
        print(episode_record, end='')
    return summary['exit_code']


if __name__ == '__main__':
    raise SystemExit(main())
