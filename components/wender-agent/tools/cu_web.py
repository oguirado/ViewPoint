# @module {"id":"wa.tools.cu_web","layer":4,"owner":"ARCH","deps":["wa.learn.cu_loop","wa.learn.model_client","wa.perceive.srf_identity","wa.learn.script_runtime","wa.learn.script_live","wa.learn.web_goal"],"book":"HB-WA-s4-learn"}
"""One web cycle attached to an operator-started identity capsule.

--endpoint is the full OpenAI chat/completions URL, as in fastexec/run_model.py.
No credentials are discovered. Only reviewed workflow reproduction metadata is
persisted; observations and answer text remain in RAM or stdout.
"""
import argparse
import json
import math
from pathlib import Path
import signal
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from wender.learn.cu_loop import run_web_cycle
from wender.learn.model_client import LocalBackend, MuseBackend
from wender.perceive.srf_identity import SrfIdentitySurface, IDENTITY_ENV

MEMORY = Path(__file__).resolve().parents[1] / 'wender/learn/executor_memory/perplexity.json'


# @anchor:wa.tools.cu_web.record_workflow
def record_workflow(path, memory, summary):
    """Draft memory only: retain verified reproductions, never task/AX/answer data."""
    if summary.get('goal_verified') is not True:
        return
    memory = json.loads(json.dumps(memory))
    memory['verified_reproductions'] = memory.get('verified_reproductions', 0) + 1
    memory['last_verified_intents'] = summary['completed']
    # Status stays draft; this is evidence accumulation, not skill admission.
    path = Path(path)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, delete=False,
                                         prefix='.' + path.name, encoding='utf-8') as handle:
            temporary = Path(handle.name)
            json.dump(memory, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write('\n')
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


# @anchor:wa.tools.cu_web.main
def main(argv=None, *, runtime_factory=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--surface', choices=('srf-identity',), default='srf-identity')
    parser.add_argument('--identity', default='perplexity-1')
    parser.add_argument('--container', default='srf-id-perplexity-1')
    parser.add_argument('--window', default='w1')
    parser.add_argument('--identity-env', type=Path, default=Path(IDENTITY_ENV))
    parser.add_argument('--dry-check', action='store_true')
    parser.add_argument('--scripts', choices=('on', 'off'), default='off')
    parser.add_argument('--goal')
    parser.add_argument('--vision-only', action='store_true',
                        help='goal mode on capsules without AX (no debugging pipe): targets from vision-engine marks only')
    parser.add_argument('--max-steps', type=int, default=20)
    parser.add_argument('--save-frame', type=Path)
    parser.add_argument('--ve-root', type=Path)
    parser.add_argument('--task')
    parser.add_argument('--executor', choices=('qwen', 'muse'), default='qwen')
    parser.add_argument('--endpoint', help='FULL OpenAI-compatible chat inference URL')
    parser.add_argument('--model', default='Qwen3.5-2B')
    parser.add_argument('--memory', type=Path, default=MEMORY)
    parser.add_argument('--trace', type=Path)
    parser.add_argument('--max-seconds', type=float, default=300)
    parser.add_argument('--analyze', action='store_true')
    parser.add_argument('--fast-path', action='store_true',
                        help='memory tier: reviewed plan + unique grounded option without a model call')
    args = parser.parse_args(argv)
    if args.goal is not None:
        if not args.goal.strip() or not args.goal.isascii():
            parser.error('--goal must be nonempty ASCII')
        if args.task is not None or args.scripts == 'on' or args.analyze or args.dry_check:
            parser.error('--goal conflicts with task, scripts, analyze or dry-check')
        if args.max_steps <= 0 or not math.isfinite(args.max_seconds) or args.max_seconds <= 0:
            parser.error('goal budgets must be finite and positive')
        from wender.learn.web_goal import run_goal
        from wender.perceive.srf_identity import GoalIdentitySurface
        import io
        import time
        start = time.monotonic()
        surface = GoalIdentitySurface(args.identity, container=args.container,
            window=args.window, identity_env=args.identity_env,
            timeout=min(30, args.max_seconds))
        with (args.trace.open('w', encoding='utf-8') if args.trace else io.StringIO()) as trace:
            remaining_ms = int((args.max_seconds - (time.monotonic() - start)) * 1000)
            if remaining_ms <= 0:
                parser.error('goal setup exceeded wall budget')
            summary = run_goal(surface, goal=args.goal, trace=trace, max_steps=args.max_steps,
                max_ms=remaining_ms, ve_root=args.ve_root, save_frame=args.save_frame,
                vision_only=args.vision_only)
        print(json.dumps(summary, sort_keys=True))
        return 0 if summary['goal_verified'] else 2
    if args.save_frame is not None:
        parser.error('--save-frame requires --goal')
    if args.scripts == 'on' and args.dry_check:
        from wender.learn.script_runtime import check_offline_construction, build_offline_runtime
        report = check_offline_construction(runtime_factory or build_offline_runtime)
        print(json.dumps(report, sort_keys=True))
        return 0 if report['construction_complete'] and report['cleanup_complete'] else 2
    if not args.dry_check and not args.task:
        parser.error('--task is required for a web cycle')
    if not args.dry_check and args.executor == 'qwen' and not args.endpoint:
        parser.error('--endpoint is required for qwen')
    if args.max_seconds <= 0:
        parser.error('--max-seconds must be positive')
    episode_record = None
    legacy_owner = None
    previous = signal.getsignal(signal.SIGTERM)
    def terminate(signum, frame):
        raise KeyboardInterrupt('surface termination requested')
    signal.signal(signal.SIGTERM, terminate)
    try:
        if args.scripts == 'on' and runtime_factory is None:
            from wender.learn.script_runtime import build_runtime
            runtime_factory = build_runtime
        if args.dry_check:
            surface = SrfIdentitySurface(args.identity, container=args.container,
                                         window=args.window, identity_env=args.identity_env,
                                         timeout=min(30, args.max_seconds))
            surface.frame()
            # No sequence recovery in smoke mode: exactly one frame + one AX call.
            ax = surface.ax(refetch=False)
            print(json.dumps({'elements': len(ax['elements']),
                              'actionable': sum(e.get('actionable') is True for e in ax['elements']),
                              'truncated': ax.get('truncated'),
                              'alignment_violations': len(surface.violations)}))
            return 0
        memory = json.loads(args.memory.read_text(encoding='utf-8'))
        with tempfile.TemporaryDirectory(prefix='wender-web-', dir='/dev/shm') as ram:
            planner = MuseBackend(workspace=ram, timeout=min(240, args.max_seconds))
            executor = (LocalBackend(args.endpoint, memory, model=args.model,
                                     timeout=min(30, args.max_seconds))
                        if args.executor == 'qwen' else planner)
            surface = SrfIdentitySurface(args.identity, ram_dir=ram,
                                         container=args.container, window=args.window,
                                         identity_env=args.identity_env,
                                         timeout=min(30, args.max_seconds))
            trace_path = args.trace or Path(ram) / 'trace.jsonl'
            with trace_path.open('a+', encoding='utf-8') as trace:
                trace.seek(0, 2)
                if trace.tell():
                    raise FileExistsError('trace file must be empty: ' + str(trace_path))
                if args.scripts == 'on':
                    from wender.learn.script_runtime import ScriptRuntime
                    from wender.learn.script_live import instrument_muse
                    runtime = runtime_factory(mode='live')
                    if type(runtime) is not ScriptRuntime:
                        raise ValueError('RUNTIME_CONFIGURATION')
                    surface.integration = runtime
                    release_provider = None
                    try:
                        if type(planner) is MuseBackend:
                            release_provider = instrument_muse(runtime, planner, web=True)
                        runtime.measure_resident('before_release')
                        summary = run_web_cycle(surface, planner, executor, memory, task=args.task,
                            trace=trace, max_seconds=args.max_seconds, integration=runtime)
                    finally:
                        if release_provider is not None:
                            release_provider()
                        drained = runtime.close()
                        runtime.measure_resident('after_release')
                        episode_record = runtime.episode_metrics()
                    if not drained:
                        raise ValueError('RUNTIME_DRAIN')
                else:
                    from wender.learn.script_live import LegacyDispatchOwner
                    # Preserve an explicitly supplied (including closed) owner.
                    # The production identity surface has no integration port
                    # installed until this composition root registers one.
                    if not hasattr(surface, 'integration'):
                        legacy_owner = LegacyDispatchOwner(surface)
                        surface.integration = legacy_owner
                    summary = run_web_cycle(surface, planner, executor, memory, task=args.task,
                                            trace=trace, max_seconds=args.max_seconds,
                                            fast_path=args.fast_path)
            record_workflow(args.memory, memory, summary)
            if args.analyze and summary['goal_verified']:
                summary['analysis'] = planner.analyze(summary['harvested'])
            print(json.dumps(summary, ensure_ascii=False))
            if args.scripts == 'on':
                print(episode_record, end='')
            return 0 if summary['goal_verified'] else 2
    except (Exception, KeyboardInterrupt) as exc:
        error = {'goal_verified': False, 'error_class': type(exc).__name__}
        if type(exc) is ValueError and str(exc) == 'LIVE_SERVICE_INSTALLATION_MISSING':
            error['error'] = 'LIVE_SERVICE_INSTALLATION_MISSING'
        if isinstance(exc, FileExistsError):
            error['error'] = str(exc)
        print(json.dumps(error))
        if episode_record is not None:
            print(episode_record, end='')
        return 2
    finally:
        if legacy_owner is not None:
            legacy_owner.close()
        signal.signal(signal.SIGTERM, previous)


if __name__ == '__main__':
    raise SystemExit(main())
