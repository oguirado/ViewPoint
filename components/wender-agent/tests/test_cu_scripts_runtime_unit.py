# @module {"id":"wa.tests.cu_scripts_runtime_unit","layer":4,"owner":"LEARN","deps":["wa.learn.script_runtime","wa.tools.cu_live","wa.tools.cu_web","wa.tests.cu_live_unit","wa.tests.test_cu_loop_unit","wa.tests.cu_web_unit","wa.learn.script_integration","wa.scripts.types","wa.tests.scripts.test_store_catalogue","wa.tests.scripts.test_observation_privacy","wa.tests.scripts.test_context_generation","wa.tests.scripts.test_execution","wa.learn.cu_loop","wa.learn.model_client"],"book":"HB-WA-s4-learn"}
"""S11A runtime controls with isolated check-removal mutants.

No production authority or owner-construction certificate is synthesized.
Mutants are isolated in memory and never change repository files.
"""
import contextlib
import ast
from dataclasses import replace
import io
import json
from pathlib import Path
import subprocess
import sys
import types
import unittest
from unittest.mock import Mock, patch

from tools import cu_live, cu_web
from wender.learn import script_runtime as runtime
from wender.learn.cu_loop import run_loop, ReplayDecider
from tests import test_cu_live_unit as _live
from tests import test_cu_loop_unit as _loop
from tests import test_cu_web_unit as _web

_STAGES = ('observe', 'select', 'act', 'verify', 'frame_wait', 'model')


def _metrics():
    return runtime.EpisodeMetrics(None, 0, 0, 0,
        tuple((key, runtime.StageMeasurement(index, 'owner_monotonic', None, False))
              for index, key in enumerate(_STAGES)),
        tuple((key, runtime.StageMeasurement(256, 'owner_accounting', None, False))
              for key in ('before_release', 'after_release')),
        True, (('success', 'terminal_evidence_unavailable'),))


def _serialize(row, **kwargs):
    return row.to_json(max_record_bytes=4096, **kwargs)


def _reject(callback):
    try:
        callback()
    except Exception as error:
        assert type(error) is ValueError, 'public validation raised ' + type(error).__name__
    else:
        raise AssertionError('invalid public metric accepted')


# @anchor:wa.tests.cu_scripts_runtime_unit.TestRuntimeMetrics
class TestRuntimeMetrics(unittest.TestCase):
    # @anchor:wa.tests.cu_scripts_runtime_unit.TestRuntimeMetrics.test_metrics_closed_schema
    def test_metrics_closed_schema(self):
        row = _metrics()
        encoded = _serialize(row)
        parsed = json.loads(encoded)
        self.assertEqual(set(parsed), {'schema', 'success', 'muse_calls', 'script_hits',
            'script_suspends', 'latency_ns', 'resident_script_bytes', 'cleanup_complete',
            'missing_reasons'})
        self.assertEqual(parsed['schema'], 'wender.scripts.episode/v1')
        self.assertTrue(encoded.endswith('\n'))
        self.assertEqual([parsed['latency_ns'][key]['value'] for key in _STAGES], list(range(6)))
        for stage in _STAGES:
            with self.subTest(stage=stage), self.assertRaises(ValueError):
                _serialize(replace(row, latency_ns=tuple(p for p in row.latency_ns if p[0] != stage)))
        for stage in ('before_release', 'after_release'):
            with self.subTest(stage=stage), self.assertRaises(ValueError):
                _serialize(replace(row, resident_script_bytes=tuple(p for p in row.resident_script_bytes
                                                                 if p[0] != stage)))
        with self.assertRaises(ValueError):
            _serialize(replace(row, latency_ns=row.latency_ns + (('extra', row.latency_ns[0][1]),)))
        with self.assertRaises(ValueError):
            _serialize(replace(row, latency_ns=(row.latency_ns[0],) * 6))

    # @anchor:wa.tests.cu_scripts_runtime_unit.TestRuntimeMetrics.test_field_shapes
    def test_field_shapes(self):
        row = _metrics()
        self.assertEqual(json.loads(_serialize(row))['latency_ns']['observe']['value'], 0)
        for bad in (list(row.latency_ns), tuple(list(p) for p in row.latency_ns),
                    (row.latency_ns[0],) * 6):
            _reject(lambda: _serialize(replace(row, latency_ns=bad)))
        impostor = types.SimpleNamespace(duration_ns=0, as_dict=lambda: {})
        _reject(lambda: _serialize(replace(row, latency_ns=(('observe', impostor),) + row.latency_ns[1:])))

    # @anchor:wa.tests.cu_scripts_runtime_unit.TestRuntimeMetrics.test_metrics_quantities
    def test_metrics_quantities(self):
        row = _metrics()
        self.assertEqual(json.loads(_serialize(row))['muse_calls'], 0)
        for field in ('muse_calls', 'script_hits', 'script_suspends'):
            for bad in (True, -1, 0.5, '1'):
                with self.subTest(field=field, bad=bad), self.assertRaises(ValueError):
                    _serialize(replace(row, **{field: bad}))
        for bad in (True, -1, 1.5):
            with self.subTest(stage=bad), self.assertRaises(ValueError):
                runtime.StageMeasurement(bad, 'owner_monotonic', None, False).as_dict()

    # @anchor:wa.tests.cu_scripts_runtime_unit.TestRuntimeMetrics.test_missingness
    def test_missingness(self):
        row = _metrics()
        unavailable = runtime.StageMeasurement(None, None, 'instrumentation_unavailable', True)
        stages = (('observe', unavailable),) + row.latency_ns[1:]
        reason = ('latency_ns.observe', 'instrumentation_unavailable')
        parsed = json.loads(_serialize(replace(row, latency_ns=stages,
                                              missing_reasons=row.missing_reasons + (reason,))))
        self.assertIsNone(parsed['latency_ns']['observe']['value'])
        for reason in (None, '', 'PRIVATE-CANARY'):
            with self.subTest(reason=reason), self.assertRaises(ValueError):
                runtime.StageMeasurement(None, None, reason, False).as_dict()
        with self.assertRaises(ValueError):
            _serialize(replace(row, latency_ns=stages))
        with self.assertRaises(ValueError):
            _serialize(replace(row, missing_reasons=()))
        with self.assertRaises(ValueError):
            _serialize(replace(row, latency_ns=stages, missing_reasons=row.missing_reasons +
                               (('latency_ns.observe', 'accounting_unavailable'),)))
        resident = (('before_release', runtime.StageMeasurement(None, None, 'accounting_unavailable', False)),
                    row.resident_script_bytes[1])
        reason = ('resident_script_bytes.before_release', 'accounting_unavailable')
        self.assertIsNone(json.loads(_serialize(replace(row, resident_script_bytes=resident,
            missing_reasons=row.missing_reasons + (reason,))))['resident_script_bytes']['before_release']['value'])
        with self.assertRaises(ValueError):
            _serialize(replace(row, resident_script_bytes=resident, missing_reasons=row.missing_reasons +
                (('resident_script_bytes.before_release', 'instrumentation_unavailable'),)))
        self.assertIsNone(json.loads(_serialize(replace(row, muse_calls=None, missing_reasons=
            row.missing_reasons + (('muse_calls', 'provider_counter_unavailable'),))))['muse_calls'])
        with self.assertRaises(ValueError):
            _serialize(replace(row, missing_reasons=(('success', 'provider_counter_unavailable'),)))

    # @anchor:wa.tests.cu_scripts_runtime_unit.TestRuntimeMetrics.test_stage_provenance
    def test_stage_provenance(self):
        self.assertEqual(runtime.StageMeasurement(0, 'owner_monotonic', None, False).as_dict()['value'], 0)
        for provenance, reason in ((None, None), ('owner_accounting', None),
                                   ('owner_monotonic', 'instrumentation_unavailable')):
            with self.subTest(provenance=provenance, reason=reason), self.assertRaises(ValueError):
                runtime.StageMeasurement(0, provenance, reason, False).as_dict()
        with self.assertRaises(ValueError):
            runtime.StageMeasurement(None, 'owner_monotonic', 'instrumentation_unavailable', False).as_dict()
        with self.assertRaises(ValueError):
            runtime.StageMeasurement(0, 'owner_accounting', None, False).as_dict(unit='seconds')

    # @anchor:wa.tests.cu_scripts_runtime_unit.TestRuntimeMetrics.test_boolean_fields
    def test_boolean_fields(self):
        self.assertTrue(json.loads(_serialize(_metrics()))['cleanup_complete'])
        for field in ('success', 'cleanup_complete'):
            with self.subTest(field=field), self.assertRaises(ValueError):
                _serialize(replace(_metrics(), **{field: 1}))
        with self.assertRaises(ValueError):
            _serialize(replace(_metrics(), success=1, missing_reasons=()),
                       terminal_owner=types.SimpleNamespace(check_terminal_metric=lambda *a: True),
                       terminal_evidence=object())
        with self.assertRaises(ValueError):
            runtime.StageMeasurement(0, 'owner_monotonic', None, 1).as_dict()

    # @anchor:wa.tests.cu_scripts_runtime_unit.TestRuntimeMetrics.test_success_requires_terminal_evidence
    def test_success_requires_terminal_evidence(self):
        evidence = object()
        owner = types.SimpleNamespace(check_terminal_metric=lambda value, success:
                                      value is evidence and success is True)
        row = replace(_metrics(), success=True, missing_reasons=())
        self.assertTrue(json.loads(_serialize(row, terminal_owner=owner, terminal_evidence=evidence))['success'])
        with self.assertRaises(ValueError):
            _serialize(row, terminal_owner=types.SimpleNamespace(check_terminal_metric=lambda *a: True),
                       terminal_evidence=None)
        for bad in (False, 'yes', None):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                _serialize(row, terminal_owner=types.SimpleNamespace(check_terminal_metric=lambda *a: bad),
                           terminal_evidence=evidence)

    # @anchor:wa.tests.cu_scripts_runtime_unit.TestRuntimeMetrics.test_record_bound
    def test_record_bound(self):
        row = _metrics()
        size = len(_serialize(row).encode())
        self.assertEqual(len(row.to_json(max_record_bytes=size).encode()), size)
        with self.assertRaises(ValueError):
            row.to_json(max_record_bytes=size - 1)
        for bad in (None, True, 0, -1):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                row.to_json(max_record_bytes=bad)

    # @anchor:wa.tests.cu_scripts_runtime_unit.TestRuntimeMetrics.test_resident_provenance_and_units
    def test_resident_provenance_and_units(self):
        values = json.loads(_serialize(_metrics()))['resident_script_bytes']
        self.assertEqual(values['before_release']['value'], values['after_release']['value'])
        self.assertEqual(values['after_release']['unit'], 'bytes')
        self.assertNotIn('duration_ns', values['after_release'])
        for provenance in ('RSS', 'source_length', 'lease_count', 'owner_monotonic'):
            with self.subTest(provenance=provenance), self.assertRaises(ValueError):
                runtime.StageMeasurement(256, provenance, None, False).as_dict(unit='bytes')

    # @anchor:wa.tests.cu_scripts_runtime_unit.TestRuntimeMetrics.test_privacy_canaries
    def test_privacy_canaries(self):
        row = _metrics()
        self.assertIn('owner_monotonic', _serialize(row))
        for canary in ('PRIVATE-LABEL', 'PRIVATE-SLOT', 'PRIVATE-ANSWER', 'PRIVATE-METADATA',
                       'PRIVATE-EXCEPTION', {'nested': 'PRIVATE-LABEL'}):
            for slot in ('provenance', 'reason'):
                with self.subTest(canary=canary, slot=slot), self.assertRaises(ValueError):
                    m = runtime.StageMeasurement(1, 'owner_monotonic', None, False)
                    _serialize(replace(row, latency_ns=(('observe', replace(m, **{slot: canary})),)
                                       + row.latency_ns[1:]))
        with self.assertRaises(ValueError):
            _serialize(replace(row, missing_reasons=(('success', 'PRIVATE-ANSWER'),)))

        owner, host, _, _ = _runtime_fixture()
        action = {'labels': {'innocent': 'PRIVATE-LABEL'}, 'slots': ['PRIVATE-SLOT'],
                  'metadata': {'innocent': 'PRIVATE-METADATA'}}
        authorized = []
        self.assertEqual(owner.dispatch(None, action,
            lambda: authorized.append(action) or 'PRIVATE-ANSWER'), 'PRIVATE-ANSWER')
        self.assertIs(authorized[0], action)
        self.assertIs(owner.verify(action, {'label': 'PRIVATE-LABEL'}, {'answer': 'PRIVATE-ANSWER'}), True)
        serialized = owner.episode_metrics()
        self.assertIn('owner_monotonic', serialized)
        self.assertNotIn('PRIVATE', serialized)

    # @anchor:wa.tests.cu_scripts_runtime_unit.TestRuntimeMetrics.test_muse_attempt_count
    def test_muse_attempt_count(self):
        counter = runtime.MuseAttemptCounter()
        entries = []
        # @anchor:wa.tests.cu_scripts_runtime_unit.TestRuntimeMetrics.test_muse_attempt_count.provider
        def provider(fail=False):
            entries.append('entry')
            if fail:
                raise RuntimeError('PRIVATE-EXCEPTION')
            return 'PRIVATE-ANSWER'
        self.assertEqual(counter.attempts(), 0)
        self.assertEqual(counter.invoke(provider), 'PRIVATE-ANSWER')
        with self.assertRaises(RuntimeError):
            counter.invoke(provider, fail=True)
        self.assertEqual(counter.attempts(), len(entries))
        self.assertEqual(counter.attempts(), 2)
        _reject(lambda: counter.invoke(None))
        self.assertEqual(counter.attempts(), 2)
        self.assertEqual(set(vars(counter)), {'_attempts', '_lock'})


# @anchor:wa.tests.cu_scripts_runtime_unit.TestRuntimeCLI
class TestRuntimeCLI(unittest.TestCase):
    # @anchor:wa.tests.cu_scripts_runtime_unit.TestRuntimeCLI.test_enabled_live_builds_default_graph
    def test_enabled_live_builds_default_graph(self):
        fixture = _live.TestLive()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.argv += ['--scripts', 'on']
        seen = []
        # @anchor:wa.tests.cu_scripts_runtime_unit.TestRuntimeCLI.test_enabled_live_builds_default_graph.launch
        def launch(*args, runtime_factory, **kwargs):
            owner = runtime_factory(mode='live')
            self.assertIs(type(owner), runtime.ScriptRuntime)
            self.assertEqual(len(owner.services.integration.construction_inventory()), 10)
            self.assertFalse(owner.check_ready(), 'unattested construction granted live authority')
            self.assertTrue(owner.close())
            seen.append(owner)
            return fixture.terminal, 0, owner.episode_metrics()
        with patch.object(cu_live, 'launch_scripts', side_effect=launch) as launcher:
            status, summary, calls, loops = fixture.invoke()
        self.assertEqual(status, 0, summary)
        self.assertEqual(len(seen), 1)
        self.assertEqual(calls.call_count, 2)
        loops.assert_not_called()
        launcher.assert_called_once()

    # @anchor:wa.tests.cu_scripts_runtime_unit.TestRuntimeCLI.test_enabled_web_builds_default_graph
    def test_enabled_web_builds_default_graph(self):
        owner, _, _, _ = _runtime_fixture()
        with patch.object(runtime, 'build_runtime', return_value=owner) as builder, \
             patch.object(cu_web, 'MuseBackend'), \
             patch.object(cu_web, 'SrfIdentitySurface'), \
             patch.object(Path, 'read_text', return_value='{}'), \
             patch.object(cu_web, 'run_web_cycle', return_value={'goal_verified': False}) as cycle, \
             contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(cu_web.main(['--scripts', 'on', '--task', 'synthetic', '--executor', 'muse']), 2)
        builder.assert_called_once_with(mode='live')
        self.assertIs(cycle.call_args.kwargs['integration'], owner)
        self.assertTrue(json.loads(output.getvalue().splitlines()[-1])['cleanup_complete'])

    # @anchor:wa.tests.cu_scripts_runtime_unit.TestRuntimeCLI.test_dry_check_no_live_dependencies
    def test_dry_check_no_live_dependencies(self):
        for entry in (cu_live.main, cu_web.main):
            with self.subTest(entry=entry.__module__), \
                 patch.object(cu_live.subprocess, 'run', side_effect=AssertionError('capsule')), \
                 patch.object(cu_live, 'loop_main', side_effect=AssertionError('loop')), \
                 patch.object(cu_live, 'load_task', side_effect=AssertionError('task')), \
                 patch.object(cu_web, 'SrfIdentitySurface', side_effect=AssertionError('surface')), \
                 patch.object(cu_web, 'MuseBackend', side_effect=AssertionError('Muse')), \
                 patch.object(Path, 'mkdir', side_effect=AssertionError('directory')), \
                 patch.object(Path, 'read_text', side_effect=AssertionError('identity or memory')), \
                 contextlib.redirect_stdout(io.StringIO()) as output:
                try:
                    code = entry(['--scripts', 'on', '--dry-check'])
                except SystemExit:
                    code = None
            self.assertIsNotNone(code, 'enabled offline branch entered live argument validation')
            self.assertTrue(output.getvalue(), 'enabled offline branch produced no construction record')
            result = json.loads(output.getvalue())
            self.assertTrue(result.get('cleanup_complete'))
            self.assertTrue(result.get('construction_complete'), 'Complete default offline graph AUSENTE')
            self.assertEqual(code, 0)
            self.assertEqual(set(result['constructed_owners']), {'admission', 'compiler', 'store',
                'ledger', 'act_api', 'wait', 'observation', 'context', 'generation', 'runtime'})

    # @anchor:wa.tests.cu_scripts_runtime_unit.TestRuntimeCLI.test_scripts_off_legacy_bytes
    def test_scripts_off_legacy_bytes(self):
        results = []
        baseline = types.ModuleType('_s11a_baseline_cli')
        baseline.__file__ = cu_live.__file__
        source = subprocess.check_output(['git', 'show', 'HEAD:./tools/cu_live.py'], text=True,
                                         cwd=Path(__file__).resolve().parents[1], timeout=10)
        exec(compile(source, '<baseline-cli>', 'exec'), baseline.__dict__)
        for module, switch in ((baseline, []), (cu_live, []), (cu_live, ['--scripts', 'off'])):
            fixture = _live.TestLive()
            fixture.setUp()
            self.addCleanup(fixture.doCleanups)
            fixture.argv += switch
            with patch.object(runtime, 'build_runtime', side_effect=AssertionError('hidden scripts owner')), \
                 patch.object(_live, 'cu_live', module):
                code, summary, calls, loops = fixture.invoke()
            self.assertEqual(code, 0)
            self.assertNotIn('scripts', summary['requested'])
            self.assertNotIn('dry_check', summary['requested'])
            self.assertNotIn('scripts', summary['effective'])
            self.assertNotIn('dry_check', summary['effective'])
            self.assertEqual(loops.call_args.kwargs, {})
            evidence = [str(loops.call_args)]
            evidence += [(fixture.run_dir / name).read_text() for name in
                         ('summary.json', 'start.json', 'terminal.json', 'cleanup.json')]
            results.append([value.replace(str(fixture.root), 'RAM') for value in evidence])
        self.assertEqual(results[0], results[1])
        self.assertEqual(results[0], results[2])

    # @anchor:wa.tests.cu_scripts_runtime_unit.TestRuntimeCLI.test_web_off_owner_contract
    def test_web_off_owner_contract(self):
        with patch.object(runtime, 'build_runtime', side_effect=AssertionError('hidden scripts owner')), \
             patch.object(cu_web, 'SrfIdentitySurface') as surface, \
             contextlib.redirect_stdout(io.StringIO()) as output:
            surface.return_value.ax.return_value = {'elements': [], 'truncated': False}
            surface.return_value.violations = []
            self.assertEqual(cu_web.main(['--scripts', 'off', '--dry-check']), 0)
        self.assertEqual(json.loads(output.getvalue())['elements'], 0)
        surface.return_value.frame.assert_called_once()

        fixture = _web.WebUnit()
        fixture.setUp()
        clock = _web.Clock()
        for inherited in (False, True):
            surface = _web.FakeSurface()
            existing = surface.integration
            ready = Mock(wraps=existing.check_ready)
            existing.check_ready = ready
            if not inherited:
                surface.integration = None
            factory = Mock(side_effect=AssertionError('off constructed runtime'))
            original = cu_web.run_web_cycle
            # @anchor:wa.tests.cu_scripts_runtime_unit.TestRuntimeCLI.test_web_off_owner_contract.run
            def run(*args, **kwargs):
                self.assertNotIn('integration', kwargs)
                return original(*args, **kwargs, clock=clock, sleep=clock.sleep)
            with patch.object(cu_web, 'SrfIdentitySurface', return_value=surface), \
                 patch.object(cu_web, 'MuseBackend', return_value=_web.FakePlanner()), \
                 patch.object(cu_web, 'LocalBackend', return_value=_web.FakeExecutor()), \
                 patch.object(cu_web, 'record_workflow'), \
                 patch.object(cu_web, 'run_web_cycle', side_effect=run), \
                 patch.object(Path, 'read_text', return_value=json.dumps(fixture.memory)), \
                 contextlib.redirect_stdout(io.StringIO()) as output:
                cu_web.main(['--scripts', 'off', '--task', 'synthetic', '--endpoint', 'http://offline.invalid'],
                            runtime_factory=factory)
            factory.assert_not_called()
            if not inherited:
                ready.assert_not_called()
                self.assertFalse(json.loads(output.getvalue())['goal_verified'])
                self.assertEqual(surface.calls, [])
            else:
                # Frozen baseline inherits an explicitly supplied surface owner.
                # This control records the limitation; off installs no new owner.
                self.assertIs(surface.integration, existing)
                self.assertGreater(ready.call_count, 0)

    # @anchor:wa.tests.cu_scripts_runtime_unit.TestRuntimeCLI.test_live_episode_paths_and_switch_validation
    def test_live_episode_paths_and_switch_validation(self):
        for argv in ([], ['--scripts', 'off'], ['--dry-check'], ['--scripts', 'invalid']):
            with self.subTest(argv=argv), contextlib.redirect_stderr(io.StringIO()), \
                 self.assertRaises(SystemExit) as error:
                cu_live.main(argv)
            self.assertEqual(error.exception.code, 2)


# @anchor:wa.tests.cu_scripts_runtime_unit.TestFrozenAcceptance
class TestFrozenAcceptance(unittest.TestCase):
    # @anchor:wa.tests.cu_scripts_runtime_unit.TestFrozenAcceptance.test_suspend_returns_to_model
    def test_suspend_returns_to_model(self):
        """A drained real runner refusal yields to the model without losing evidence."""
        fixture = _loop.TestCuLoop()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        cap = fixture.capsule(['a', 'b'])
        owner, host, h, _ = _runtime_fixture()
        adapter = _wire_miss(owner, host, h)
        self.addCleanup(adapter.close)
        decider = Mock(wraps=ReplayDecider([_loop.proposal('done')]))
        result = run_loop(cap, _loop.Describer(), decider, goal='synthetic', trace=io.StringIO(),
                          integration=owner, clock=fixture.clock, sleep=fixture.clock.sleep)
        self.assertEqual(result['stop_reason'], 'done')
        self.assertEqual(decider.decide.call_count, 1)
        # @anchor:wa.tests.cu_scripts_runtime_unit.TestFrozenAcceptance.test_suspend_returns_to_model.suspended
        def suspended(runtime_owner, host, session, state, original):
            self.assertIs(type(runtime_owner), runtime.ScriptRuntime)
            self.assertIs(state.outcome.status, _t.TerminalStatus.REFUSED)
            self.assertIs(state.outcome.owner_drained.value, _t.Truth.TRUE)
            decider = Mock(wraps=ReplayDecider([_loop.proposal('done')]))
            # Lookup is already complete: exercise the frozen loop on this real,
            # retained suspended runner, without starting a second script.
            with patch.object(runtime_owner, 'select_script', return_value=session):
                result = run_loop(cap, _loop.Describer(), decider, goal='synthetic', trace=io.StringIO(),
                    integration=runtime_owner, clock=fixture.clock, sleep=fixture.clock.sleep)
            fallback = runtime_owner.fallback_evidence(session)
            self.assertIs(type(fallback), runtime.FallbackEvidence)
            self.assertIs(fallback.drain_evidence.value, _t.Truth.TRUE)
            self.assertIsNone(fallback.fresh_grant_evidence)
            self.assertFalse(runtime_owner.check_script_miss())
            self.assertEqual(result['stop_reason'], 'done',
                'FROZEN_LOOP: drained real-runner suspension ends the episode')
            self.assertEqual(decider.decide.call_count, 1)
            self.assertFalse(result['goal_verified'])
            binding = dict.fromkeys(('selector', 'ownership', 'observation', 'publication',
                'generation', 'geometry', 'facts', 'lease', 'scope', 'authority'), 'registered')
            self.assertFalse(runtime_owner.guard_drift(binding, dict(binding, selector='renamed'),
                owner=types.SimpleNamespace(check=lambda value: True)))
            self.assertTrue(runtime_owner.check_ready())
            self.assertEqual(json.loads(runtime_owner.episode_metrics())['script_suspends'], 1)
            self.assertTrue(runtime_owner.check_ready())
        _registered_runner(self, suspended, suspend=True)

    # @anchor:wa.tests.cu_scripts_runtime_unit.TestFrozenAcceptance.test_uncertain_script_terminal_never_falls_through
    def test_uncertain_script_terminal_never_falls_through(self):
        from wender.learn import cu_loop as loop
        # @anchor:wa.tests.cu_scripts_runtime_unit.TestFrozenAcceptance.test_uncertain_script_terminal_never_falls_through.check
        def check(owner, host, session, state, original):
            self.assertIs(state.input_disposition, _t.InputDisposition.INPUT_UNCERTAIN)
            self.assertIs(state.outcome.input_disposition, _t.InputDisposition.INPUT_UNCERTAIN)
            with patch.object(owner, 'select_script', return_value=session), \
                 self.assertRaisesRegex(ValueError, 'SCRIPT_TERMINAL_UNAVAILABLE'):
                loop._script_before_model(owner, {}, {}, clock=lambda: 0,
                    deadline=100, note_input=lambda value: None)
            self.assertFalse(owner.check_ready())
            self.assertFalse(owner.charge_model())
        _registered_runner(self, check, uncertain=True)

    # @anchor:wa.tests.cu_scripts_runtime_unit.TestFrozenAcceptance.test_uncertain_history_and_terminal_checks
    def test_uncertain_history_and_terminal_checks(self):
        from wender.learn import cu_loop as loop
        # @anchor:wa.tests.cu_scripts_runtime_unit.TestFrozenAcceptance.test_uncertain_history_and_terminal_checks.check
        def check(owner, host, session, state, original):
            self.assertIs(state.input_disposition, _t.InputDisposition.NOT_SENT)
            first = replace(state, outcome=None, input_disposition=_t.InputDisposition.INPUT_UNCERTAIN)
            with patch.object(owner, 'select_script', return_value=session), \
                 patch.object(loop, 'run_script_turn', side_effect=[first, state]), \
                 self.assertRaisesRegex(ValueError, 'SCRIPT_TERMINAL_UNAVAILABLE'):
                loop._script_before_model(owner, {}, {}, clock=lambda: 0,
                    deadline=100, note_input=lambda value: None)
            for delta in ({'reconciliation_required': True},
                          {'failure': _t.FailureClass.EPISODE_DEADLINE},
                          {'failure': _t.FailureClass.UNCLASSIFIED},
                          {'owner_drained': _t.PredicateResult(_t.Truth.UNKNOWN, _t.Reason.MISSING)}):
                unsafe = replace(state, outcome=replace(state.outcome, **delta))
                # Isolate the conductor's check from the runtime's independent fence.
                with patch.object(owner, 'select_script', return_value=session), \
                     patch.object(owner, 'accept_script', return_value=False), \
                     patch.object(owner, 'check_terminal', return_value=True), \
                     patch.object(loop, 'run_script_turn', return_value=unsafe), \
                     self.assertRaisesRegex(ValueError, 'SCRIPT_TERMINAL_UNAVAILABLE'):
                    loop._script_before_model(owner, {}, {}, clock=lambda: 0,
                        deadline=100, note_input=lambda value: None)
        _registered_runner(self, check, suspend=True)

    # @anchor:wa.tests.cu_scripts_runtime_unit.TestFrozenAcceptance.test_model_verification_boolean_through_loop
    def test_model_verification_boolean_through_loop(self):
        fixture = _loop.TestCuLoop()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        for semantic in (True, False, {'ok': True}, 1, None):
            with self.subTest(semantic=semantic):
                owner, host, h, _ = _runtime_fixture()
                adapter = _wire_miss(owner, host, h)
                self.addCleanup(adapter.close)
                owner.services.actions.verify = Mock(return_value=semantic)
                cap = fixture.capsule(['a', 'b'])
                decider = Mock(wraps=ReplayDecider([_loop.proposal(), _loop.proposal('done')]))
                result = run_loop(cap, _loop.Describer(), decider, goal='synthetic', trace=io.StringIO(),
                    integration=owner, clock=fixture.clock, sleep=fixture.clock.sleep)
                self.assertEqual(result['verified_ok'], 1 if semantic is True else 0)
                self.assertEqual(result['stop_reason'], 'done' if semantic is True else 'reconciliation_required')
                self.assertEqual(len(cap.inputs), 1)
                action = owner.services.actions.verify.call_args.args[0]
                if semantic is True:
                    self.assertIs(owner.verify(action, {}, {}), True)
                else:
                    self.assertFalse(owner.check_ready())
                # Dict/truthy results must fail the public adapter even outside the loop.
                other, _, _, _ = _runtime_fixture()
                other.services.actions.verify = lambda *a: semantic
                other.dispatch(None, action, lambda: None)
                self.assertIs(other.verify(action, {}, {}), semantic is True)



# @anchor:wa.tests.cu_scripts_runtime_unit.TestConductorMutations
class TestConductorMutations(unittest.TestCase):
    # @anchor:wa.tests.cu_scripts_runtime_unit.TestConductorMutations.test_fallback_check_removal
    def test_fallback_check_removal(self):
        from wender.learn import cu_loop as loop
        cases = [('fallback', 'test_suspend_returns_to_model'),
                 ('history', 'test_uncertain_history_and_terminal_checks'),
                 ('reconciliation', 'test_uncertain_history_and_terminal_checks'),
                 ('runner_failure', 'test_uncertain_history_and_terminal_checks'),
                 ('drained', 'test_uncertain_history_and_terminal_checks')]
        for kind, name in cases:
            with self.subTest(kind=kind):
                control = unittest.TestResult()
                TestFrozenAcceptance(name).run(control)
                self.assertTrue(control.wasSuccessful(), control.errors or control.failures)
                tree = ast.parse(Path(loop.__file__).read_text())
                function = next(n for n in tree.body if isinstance(n, ast.FunctionDef)
                                and n.name == '_script_before_model')
                changed = []
                # @anchor:wa.tests.cu_scripts_runtime_unit.TestConductorMutations.test_fallback_check_removal.Remove
                class Remove(ast.NodeTransformer):
                    # @anchor:wa.tests.cu_scripts_runtime_unit.TestConductorMutations.test_fallback_check_removal.Remove.visit_Return
                    def visit_Return(self, node):
                        if kind == 'fallback' and ast.unparse(node.value) == 'accepted is True':
                            changed.append(node.lineno)
                            return ast.copy_location(ast.Raise(exc=ast.Call(func=ast.Name(id='ValueError', ctx=ast.Load()),
                                args=[ast.Constant('SCRIPT_TERMINAL_UNAVAILABLE')], keywords=[]), cause=None), node)
                        return self.generic_visit(node)
                    # @anchor:wa.tests.cu_scripts_runtime_unit.TestConductorMutations.test_fallback_check_removal.Remove.visit_BoolOp
                    def visit_BoolOp(self, node):
                        self.generic_visit(node)
                        if kind == 'history' and ast.unparse(node) == "uncertain_input or disposition == 'input_uncertain'":
                            changed.append(node.lineno)
                            return ast.copy_location(ast.Constant(False), node)
                        return node
                    # @anchor:wa.tests.cu_scripts_runtime_unit.TestConductorMutations.test_fallback_check_removal.Remove.visit_Compare
                    def visit_Compare(self, node):
                        text = ast.unparse(node)
                        if (kind == 'runner_failure' and text.startswith("getattr(outcome.failure, 'value', None) in")
                            or kind == 'reconciliation' and text == 'outcome.reconciliation_required is not False'
                            or kind == 'drained' and text == "getattr(outcome.owner_drained.value, 'value', None) != 'TRUE'"):
                            changed.append(node.lineno)
                            return ast.copy_location(ast.Constant(False), node)
                        return self.generic_visit(node)
                function = Remove().visit(function)
                self.assertTrue(changed, kind)
                namespace = dict(vars(loop))
                exec(compile(ast.fix_missing_locations(ast.Module(body=[function], type_ignores=[])),
                    '<isolated-conductor-mutant>', 'exec'), namespace)
                with patch.object(loop, '_script_before_model', namespace['_script_before_model']):
                    result = unittest.TestResult()
                    TestFrozenAcceptance(name).run(result)
                self.assertFalse(result.errors, result.errors)
                self.assertTrue(result.failures, 'surviving conductor mutant: ' + kind)



# @anchor:wa.tests.cu_scripts_runtime_unit.TestMutationAudit
class TestMutationAudit(unittest.TestCase):
    # @anchor:wa.tests.cu_scripts_runtime_unit.TestMutationAudit.test_metric_check_removal
    def test_metric_check_removal(self):
        checks = [(25, 'test_metrics_quantities'), (29, 'test_missingness'),
            (33, 'test_field_shapes'), (34, 'test_field_shapes'), (37, 'test_field_shapes'),
            (52, 'test_stage_provenance'), (53, 'test_boolean_fields'),
            (56, 'test_stage_provenance'), (60, 'test_stage_provenance'),
            (62, 'test_stage_provenance'), (89, 'test_boolean_fields'),
            (90, 'test_boolean_fields'), (101, 'test_success_requires_terminal_evidence'),
            (103, 'test_success_requires_terminal_evidence'), (108, 'test_field_shapes'),
            (116, 'test_missingness'), (118, 'test_missingness'), (120, 'test_missingness'),
            (130, 'test_record_bound'), (149, 'test_muse_attempt_count'), (151, 'test_muse_attempt_count')]
        source = Path(runtime.__file__).read_text()
        for line, name in checks:
            with self.subTest(line=line, test=name):
                control = unittest.TestResult()
                TestRuntimeMetrics(name).run(control)
                self.assertTrue(control.wasSuccessful(), control.errors or control.failures)
                tree = ast.parse(source)
                # @anchor:wa.tests.cu_scripts_runtime_unit.TestMutationAudit.MetricRemove
                class MetricRemove(ast.NodeTransformer):
                    # @anchor:wa.tests.cu_scripts_runtime_unit.TestMutationAudit.MetricRemove.visit_Expr
                    def visit_Expr(self, node):
                        return ast.copy_location(ast.Pass(), node) if node.lineno == line else self.generic_visit(node)
                    # @anchor:wa.tests.cu_scripts_runtime_unit.TestMutationAudit.MetricRemove.visit_AugAssign
                    def visit_AugAssign(self, node):
                        return ast.copy_location(ast.Pass(), node) if node.lineno == line else self.generic_visit(node)
                mutant = types.ModuleType('wender.learn._s11a_mutant')
                with patch.dict(sys.modules, {mutant.__name__: mutant}):
                    exec(compile(ast.fix_missing_locations(MetricRemove().visit(tree)),
                                 '<isolated-metric-mutant>', 'exec'), mutant.__dict__)
                    with patch.object(sys.modules[__name__], 'runtime', mutant):
                        result = unittest.TestResult()
                        TestRuntimeMetrics(name).run(result)
                self.assertFalse(result.errors, result.errors)
                self.assertTrue(result.failures, 'surviving check-removal mutant')

    # @anchor:wa.tests.cu_scripts_runtime_unit.TestMutationAudit.test_cli_check_removal
    def test_cli_check_removal(self):
        cases = [(cu_live, 'test_enabled_live_builds_default_graph', 'activation', None),
                 (cu_web, 'test_enabled_web_builds_default_graph', 'activation', None),
                 (cu_live, 'test_dry_check_no_live_dependencies', 'offline', None),
                 (cu_web, 'test_dry_check_no_live_dependencies', 'offline', None)]
        source = Path(cu_live.__file__).read_text()
        filters = [n.lineno for n in ast.walk(ast.parse(source)) if isinstance(n, ast.Compare)
                   and ast.unparse(n) == "key not in ('scripts', 'dry_check')"]
        self.assertEqual(len(filters), 2)
        cases += [(cu_live, 'test_scripts_off_legacy_bytes', 'legacy_fields', line) for line in filters]
        for module, name, kind, line in cases:
            with self.subTest(module=module.__name__, kind=kind, line=line):
                control = unittest.TestResult()
                TestRuntimeCLI(name).run(control)
                self.assertTrue(control.wasSuccessful(), control.errors or control.failures)
                # @anchor:wa.tests.cu_scripts_runtime_unit.TestMutationAudit.CLIRemove
                class CLIRemove(ast.NodeTransformer):
                    # @anchor:wa.tests.cu_scripts_runtime_unit.TestMutationAudit.CLIRemove.visit_If
                    def visit_If(self, node):
                        if kind == 'activation' and ast.dump(node.test) == ast.dump(ast.parse("args.scripts == 'on' and runtime_factory is None", mode="eval").body):
                            return ast.copy_location(ast.Pass(), node)
                        if kind == 'offline' and ast.dump(node.test) == ast.dump(ast.parse("args.scripts == 'on' and args.dry_check", mode='eval').body):
                            return ast.copy_location(ast.Pass(), node)
                        return self.generic_visit(node)
                    # @anchor:wa.tests.cu_scripts_runtime_unit.TestMutationAudit.CLIRemove.visit_Compare
                    def visit_Compare(self, node):
                        if kind == 'legacy_fields' and node.lineno == line:
                            return ast.copy_location(ast.Constant(True), node)
                        return self.generic_visit(node)
                mutant = types.ModuleType('_s11a_cli_mutant')
                mutant.__file__ = module.__file__
                tree = CLIRemove().visit(ast.parse(Path(module.__file__).read_text()))
                exec(compile(ast.fix_missing_locations(tree), '<isolated-cli-mutant>', 'exec'), mutant.__dict__)
                target = 'cu_live' if module is cu_live else 'cu_web'
                with patch.object(sys.modules[__name__], target, mutant), patch.object(_live, 'cu_live',
                                                                                       mutant if target == 'cu_live' else cu_live):
                    result = unittest.TestResult()
                    TestRuntimeCLI(name).run(result)
                self.assertFalse(result.errors, result.errors)
                self.assertTrue(result.failures, 'surviving CLI check-removal mutant')




# Synthetic registered services are test data, never a default production graph.
from wender.learn import script_integration as _integration
from wender.scripts import types as _t
from tests.scripts import test_store_catalogue as _stores
from tests.scripts import test_observation_privacy as _observations
from tests.scripts import test_context_generation as _generations
from tests.scripts import test_execution as _execution
import time


# @anchor:wa.tests.cu_scripts_runtime_unit._runtime_fixture
def _runtime_fixture(*, session=None, store_host=None, owned=()):
    h = store_host or _stores._Host()
    session = session or _integration.IntegrationSession('episode', h.scope, h.authority,
        1000, 0, _integration.RestartState(True, h.scope, (), object(), None),
        None, None, (), (), object(), ())
    sessions = {id(session): session}
    host = types.SimpleNamespace(store=h.store, resources=object(),
        now=1, authority=session.authority, ready=True, restart=True)
    host.check_session = lambda value: sessions.get(id(value)) is value
    host.register_session = lambda value: sessions.setdefault(id(value), value) is value
    host.check_restart = lambda value: host.restart and host.check_session(value)
    host.stopped = lambda: False
    host.clock = lambda: host.now
    host.current_authority = lambda scope: host.authority
    host.check_services = lambda *a: host.ready
    host.check_budget = lambda *a: True
    host.check_artifact = lambda *a: True
    host.check_custody = lambda *a: True
    host.check_terminal = lambda *a: True
    host.finish_integration = lambda *a: True
    host.select_proposal = lambda session, evidence, policy: _integration.LaneSelection(
        'MUSE', 4, None, 'no_match', evidence.observation, session.authority, ())
    live = {id(value): value for value in owned}
    cleanup = types.SimpleNamespace(
        check_owned=lambda value: live.get(id(value)) is value,
        drain=lambda value: live.pop(id(value), None) is value,
        check_drained=lambda value: id(value) not in live)
    actions = types.SimpleNamespace(possible_input=lambda action: True,
        dispatch=lambda surface, action, callback: callback(),
        verify=lambda *a: True, uncertain=Mock())
    services = runtime.RuntimeServices(host, None, None, actions,
        types.SimpleNamespace(charge=lambda s: True), cleanup, None, None,
        None, None, owned)
    config = runtime.RuntimeConfiguration('offline', (('observation', object()),
        ('selection', object())), 'synthetic-model', session.deadline_ns, 8192, 64)
    result = runtime.ScriptRuntime(config, services, session=session)
    return result, host, h, live


# @anchor:wa.tests.cu_scripts_runtime_unit._observation_host
def _observation_host(scope, authority, observation):
    acquisition = object()
    registered, allocations = {}, {}
    pscope = _observations._scope(principal_id=scope.principal, tenant_id=scope.tenant,
        profile_incarnation=scope.browser_profile_incarnation, top_level_origin=scope.top_level_origin,
        target_frame_origin=scope.target_frame_origin, frame_ancestry=scope.frame_ancestry,
        scope_revision=scope.scope_revision)
    arguments = dict(observation_id='obs', publication_id=observation.publication_identity,
        scope_identity=pscope, raw_elements=(_observations._raw(),), producer_gen16=7,
        srf_64=123, turn_index=0, scope_complete=True, stream_truncated=False, turns=(),
        join_result=None, ax_matches={1: {'level': 'L0_EXACT'}}, transform_meta=_observations._meta(),
        policy_revision=1, budget_port=_observations._Budget())
    # @anchor:wa.tests.cu_scripts_runtime_unit._observation_host.reserve
    def reserve(*unused):
        allocation = object()
        allocations[id(allocation)] = allocation
        return allocation
    # @anchor:wa.tests.cu_scripts_runtime_unit._observation_host.register
    def register(a, bundle, s, revision, allocation):
        evidence = _integration.IntegrationEvidence(s, revision, observation, bundle,
            object(), True, True, object(), (), allocation)
        registered[id(evidence)] = evidence
        return evidence
    # @anchor:wa.tests.cu_scripts_runtime_unit._observation_host.release
    def release(allocation):
        return allocations.pop(id(allocation), None) is allocation
    owner = types.SimpleNamespace(reserve_observation=reserve,
        check_acquisition=lambda a, *unused: a is acquisition,
        observation_arguments=lambda *unused: arguments,
        register_observation=register,
        check_evidence=lambda value: registered.get(id(value)) is value,
        release_observation=release)
    return runtime.ObservationHost(owner, max_registrations=8), acquisition, arguments


# @anchor:wa.tests.cu_scripts_runtime_unit.TestRuntimeOwners
class TestRuntimeOwners(unittest.TestCase):
    # @anchor:wa.tests.cu_scripts_runtime_unit.TestRuntimeOwners.test_restart_and_authority_fence
    def test_restart_and_authority_fence(self):
        owner, host, h, _ = _runtime_fixture()
        self.assertTrue(owner.check_ready())
        self.assertFalse(owner.check_session(replace(owner._session)))
        for field, bad in (('ready', False), ('restart', False), ('authority', object()), ('now', 1000)):
            old = getattr(host, field)
            setattr(host, field, bad)
            self.assertFalse(owner.check_ready(), field)
            self.assertFalse(owner.charge_model(), field)
            setattr(host, field, old)
        self.assertTrue(owner.charge_model())
        owner.uncertain(None)
        owner.uncertain(None)
        self.assertFalse(owner.check_ready())
        owner.services.actions.uncertain.assert_called_once()

        evidence = object()
        original_session = owner._session
        host.check_reconciliation = lambda session, value: value is evidence
        # @anchor:wa.tests.cu_scripts_runtime_unit.TestRuntimeOwners.test_restart_and_authority_fence.renew
        def renew(session, value):
            renewed = replace(session, restart=replace(session.restart, reconciliation=evidence))
            host.register_session(renewed)
            return renewed
        host.renew_session = renew
        host.check_fresh_grant = lambda *a: False
        with self.assertRaises(ValueError):
            owner.reconcile_uncertain(evidence)
        self.assertFalse(owner.check_ready())
        host.check_fresh_grant = lambda session, value: value is evidence
        with self.assertRaises(ValueError):
            owner.reconcile_uncertain(object())
        renewed = owner.reconcile_uncertain(evidence)
        self.assertIsNot(renewed, original_session)
        self.assertEqual(renewed.episode, original_session.episode)
        self.assertTrue(owner.check_ready())
        provider = Mock()
        with self.assertRaises(ValueError):
            owner.invoke_provider(provider)
        provider.assert_not_called()
        self.assertEqual(owner.dispatch(None, {}, lambda: 'fresh'), 'fresh')

    # @anchor:wa.tests.cu_scripts_runtime_unit.TestRuntimeOwners.test_uncertain_never_falls_through
    def test_uncertain_never_falls_through(self):
        owner, host, h, _ = _runtime_fixture()
        events, action = [], {'kind': 'click'}
        self.assertEqual(owner.dispatch(None, action, lambda: events.append('sent') or 'ok'), 'ok')
        with self.assertRaises(ValueError):
            owner.dispatch(None, action, lambda: events.append('repeat'))
        self.assertEqual(events, ['sent'])
        owner, host, h, _ = _runtime_fixture()
        # @anchor:wa.tests.cu_scripts_runtime_unit.TestRuntimeOwners.test_uncertain_never_falls_through.lost
        def lost():
            events.append('uncertain')
            raise RuntimeError('PRIVATE-EXCEPTION')
        with self.assertRaises(RuntimeError):
            owner.dispatch(None, action, lost)
        self.assertFalse(owner.check_ready())
        self.assertFalse(owner.check_script_miss())
        self.assertFalse(owner.charge_model())
        with self.assertRaises(ValueError):
            owner.dispatch(None, {'kind': 'click'}, lambda: events.append('unsafe'))
        self.assertEqual(events, ['sent', 'uncertain'])

    # @anchor:wa.tests.cu_scripts_runtime_unit.TestRuntimeOwners.test_drift_rename_and_move
    def test_drift_rename_and_move(self):
        expected = dict.fromkeys(('selector', 'ownership', 'observation', 'publication',
            'generation', 'geometry', 'facts', 'lease', 'scope', 'authority'), 'registered')
        expected['structural_screen_digest'] = 'unchanged-global-screen'
        guard = types.SimpleNamespace(check=lambda value: True)
        for mutation in ({'selector': 'renamed'}, {'geometry': 'moved'}, {'scope': None}):
            owner, _, _, _ = _runtime_fixture()
            actual = dict(expected, **mutation)
            self.assertFalse(owner.guard_drift(expected, actual, owner=guard))
            with self.assertRaises(ValueError):
                owner.dispatch(None, {}, lambda: self.fail('input after drift'))
        owner, _, _, _ = _runtime_fixture()
        self.assertTrue(owner.guard_drift(expected, dict(expected), owner=guard))
        sent = []
        owner.dispatch(None, {}, lambda: sent.append(1))
        self.assertEqual(sent, [1])

    # @anchor:wa.tests.cu_scripts_runtime_unit.TestRuntimeOwners.test_scope_and_observation_mapping
    def test_scope_and_observation_mapping(self):
        h = _stores._Host()
        adapter, acquisition, arguments = _observation_host(h.scope, h.authority, h.f['observation'])
        options = dict(host=adapter, acquisition=acquisition, scope=h.scope,
            authority=h.authority, policy=object(), resources=object())
        evidence = _integration.observe(**options)
        self.assertIs(evidence.observation, h.f['observation'])
        self.assertIsNot(evidence.bundle.observation_ref, evidence.observation)
        self.assertTrue(adapter.check_evidence(evidence))
        self.assertFalse(adapter.check_evidence(replace(evidence)))
        saved = adapter._owner.check_evidence
        adapter._owner.check_evidence = lambda value: True
        self.assertFalse(adapter.check_evidence(replace(evidence)))
        adapter._owner.check_evidence = lambda value: False
        self.assertFalse(adapter.check_evidence(evidence))
        adapter._owner.check_evidence = saved
        original = arguments['scope_identity']
        for change in ({'principal_id': 'foreign'}, {'profile_incarnation': 'foreign'},
                       {'top_level_origin': 'https://foreign.example',
                        'target_frame_origin': 'https://foreign.example',
                        'frame_ancestry': ('https://foreign.example',)}):
            with self.subTest(scope_fields=tuple(change)):
                arguments['scope_identity'] = replace(original, **change)
                with self.assertRaisesRegex(ValueError, 'SCOPE_MAPPING'):
                    _integration.observe(**options)
        arguments['scope_identity'] = original
        # The acquisition owner independently associates the actual producer
        # generation, rather than substituting a catalogue/context revision.
        adapter._owner.check_acquisition = lambda a, *unused: (a is acquisition
            and arguments['producer_gen16'] == h.f['observation'].producer_generation)
        arguments['producer_gen16'] = 8
        with self.assertRaisesRegex(ValueError, 'ACQUISITION'):
            _integration.observe(**options)
        arguments['producer_gen16'] = 7
        with self.assertRaisesRegex(ValueError, 'SCOPE_ATTESTATION'):
            _integration.observe(**(options | {'scope': replace(h.scope, attestation=None,
                attestation_reason=_t.Reason.MISSING)}))
        self.assertTrue(adapter.close())
        self.assertTrue(adapter.close())
        self.assertFalse(adapter.check_evidence(evidence))

    # @anchor:wa.tests.cu_scripts_runtime_unit.TestRuntimeOwners.test_resident_accounting_release
    def test_resident_accounting_release(self):
        h = _stores._Host(capacity=1)
        cell = h._admit()
        retained_grants = tuple(h.grants.values())
        initial_refunds = tuple(h.refunds)
        cached_charge = h.used
        self.assertGreater(cached_charge, 0)
        lease = h._acquire(cell)
        owner, _, _, live = _runtime_fixture(store_host=h, owned=(lease,))
        accounting = types.SimpleNamespace(resident_script_bytes=lambda store:
            sum(grant.charged_bytes for grant in h.grants.values()
                if grant.request.operation == 'store-admit'))
        owner.services = replace(owner.services, accounting=accounting)
        # @anchor:wa.tests.cu_scripts_runtime_unit.TestRuntimeOwners.test_resident_accounting_release.drain
        def drain(value):
            self.assertIs(value, lease)
            accepted = h.store.release(value)
            if accepted.result is _t.Truth.TRUE:
                live.pop(id(value), None)
                return True
            return False
        # @anchor:wa.tests.cu_scripts_runtime_unit.TestRuntimeOwners.test_resident_accounting_release.drained
        def drained(value):
            try:
                h.store.check_lease(value, value.artifact, value.scope, value.episode_id,
                                    value.process_incarnation, value.retained_owner)
            except _t.ContractError as error:
                return error.reason is _t.Reason.STALE
            return False
        owner.services.cleanup.drain = drain
        owner.services.cleanup.check_drained = drained
        self.assertFalse(drained(lease))
        self.assertEqual(owner.measure_resident('before_release').duration_ns, cached_charge)
        self.assertTrue(owner.close())
        self.assertTrue(drained(lease))
        self.assertEqual(owner.measure_resident('after_release').duration_ns, cached_charge)
        self.assertEqual(tuple(h.refunds), initial_refunds)  # Unpin did not evict.
        h.now = h.store.resolver_policy.idle_grace_ns
        evicted = h.store.evict_idle(now_ns=h.now, max_evictions=1)
        self.assertEqual(evicted, (cell.key,))
        self.assertTrue(all(any(refund is grant for refund in h.refunds) for grant in retained_grants))
        self.assertEqual(owner.measure_resident('after_release').duration_ns, 0)
        accounting.resident_script_bytes = lambda store: True
        with self.assertRaises(ValueError):
            owner.measure_resident('after_release')
        owner.services = replace(owner.services, accounting=None)
        self.assertIsNone(owner.measure_resident('after_release').duration_ns)

    # @anchor:wa.tests.cu_scripts_runtime_unit.TestRuntimeOwners.test_drain_identity_inventory
    def test_drain_identity_inventory(self):
        owned, unrelated = Mock(name='retained_callback'), Mock(name='unrelated_callback')
        owner, host, h, live = _runtime_fixture(owned=(owned,))
        live[id(unrelated)] = unrelated
        owner.services.cleanup.drain = lambda value: True  # Counters lie; holder remains.
        self.assertFalse(owner.close())
        self.assertTrue(owner.owns(owned))
        self.assertFalse(owner.check_ready())
        owner.services.cleanup.drain = lambda value: live.pop(id(value), None) is value
        self.assertTrue(owner.close())
        self.assertTrue(owner.close())
        self.assertFalse(owner.owns(owned))
        self.assertIs(live[id(unrelated)], unrelated)
        owned.assert_not_called()
        unrelated.assert_not_called()

    # @anchor:wa.tests.cu_scripts_runtime_unit.TestRuntimeOwners.test_real_observation_select_miss
    def test_real_observation_select_miss(self):
        owner, host, h, _ = _runtime_fixture()
        cell = h._admit(auxiliary=(('META:fts', ('logical1',)),))
        h._publish()
        adapter, acquisition, args = _observation_host(h.scope, h.authority, h.f['observation'])
        owner.services = replace(owner.services, observation=adapter, acquisition=lambda *a: acquisition)
        host.resolve_request = lambda s, e: h._request(cell, goal_contract='unmatched')
        self.assertIsNone(owner.select_script({}, {}))
        self.assertTrue(owner.check_script_miss())
        args['scope_identity'] = replace(args['scope_identity'], principal_id='foreign')
        with self.assertRaises(ValueError):
            owner.select_script({}, {})
        self.assertFalse(owner.check_script_miss())
        self.assertTrue(adapter.close())

    # @anchor:wa.tests.cu_scripts_runtime_unit.TestRuntimeOwners.test_runtime_metrics_and_provider_boundary
    def test_runtime_metrics_and_provider_boundary(self):
        owner, host, _, _ = _runtime_fixture()
        events = []
        # @anchor:wa.tests.cu_scripts_runtime_unit.TestRuntimeOwners.test_runtime_metrics_and_provider_boundary.provider
        def provider(fail):
            events.append(1)
            host.now += 7
            if fail:
                raise RuntimeError('PRIVATE-CANARY')
            return 'PRIVATE-ANSWER'
        owner.charge_model()
        self.assertIsNone(json.loads(owner.episode_metrics())['muse_calls'])
        self.assertEqual(owner.invoke_provider(provider, False), 'PRIVATE-ANSWER')
        self.assertTrue(owner.charge_model())
        with self.assertRaises(RuntimeError):
            owner.invoke_provider(provider, True)
        with self.assertRaises(ValueError):
            owner.invoke_provider(provider, False)
        owner.close()
        row = json.loads(owner.episode_metrics())
        self.assertEqual(row['muse_calls'], len(events))
        self.assertEqual(row['latency_ns']['model']['value'], 14)
        self.assertTrue(row['cleanup_complete'])
        self.assertNotIn('PRIVATE', owner.episode_metrics())


# @anchor:wa.tests.cu_scripts_runtime_unit.TestGenerationTransport
class TestGenerationTransport(unittest.TestCase):
    def _transport(self, body, *, cancelled=lambda: False, clock=time.monotonic_ns,
                   wire_bytes=4096):
        counter = runtime.MuseAttemptCounter()
        commands = []
        # @anchor:wa.tests.cu_scripts_runtime_unit.TestGenerationTransport._transport.command
        def command(schema, association):
            commands.append((schema, association))
            return (sys.executable, '-c', body)
        transport = runtime.MuseGenerationTransport(command=command,
            decode=lambda raw, association: raw, clock=clock, cancelled=cancelled,
            counter=counter, poll_ns=1000000, join_ns=100000000, wire_bytes=wire_bytes)
        return transport, counter, commands

    # @anchor:wa.tests.cu_scripts_runtime_unit.TestGenerationTransport._exchange
    def _exchange(self, transport, *, host=None, policy=None, association=None):
        policy = policy or _generations.GenerationTests()._policy()
        policy = replace(policy, absolute_deadline_ns=time.monotonic_ns() + 1000000000)
        host = host or types.SimpleNamespace(check=lambda *a: _t.AdmissionCheck(_t.Truth.TRUE, None))
        return transport.exchange(b'passive', schema=b'{"required":["source"]}',
            association=association, policy=policy, host=host)

    # @anchor:wa.tests.cu_scripts_runtime_unit.TestGenerationTransport.test_generation_schema_and_admission
    def test_generation_schema_and_admission(self):
        fixture = _generations.GenerationTests()
        d, provenance, resources, exports, args = fixture._fixture()
        host = _generations._GenerationHost()
        owner, registered, h, _ = _runtime_fixture()
        owner.services = replace(owner.services, generation=host)
        compiler = _generations._compiler_in_scope(args['seal'].scope)
        source = compiler.source_bytes.decode()
        body = 'import sys; sys.stdin.buffer.read(); sys.stdout.write(' + repr(json.dumps({'source': source})) + ')'
        # Fixture clock is deterministic; deadline and brief association use it.
        transport, counter, commands = self._transport(body, clock=lambda: 1, wire_bytes=131072)
        with self.assertRaises(_t.ContractError):
            owner.generate_draft(segment_arguments=args | {'sidecars': object()},
                transport=transport, policy=fixture._policy(), host=host)
        self.assertEqual(counter.attempts(), 0)
        for index, malformed in enumerate(({'action': 'click'}, {'source': 'import os\n'},
                          {'source': 'open("PRIVATE")\n'}, {'source': source, 'status': 'PROMOTED'})):
            with self.subTest(response_fixture=index):
                bad = _generations._Transport(malformed)
                with self.assertRaises(_t.ContractError) as rejected:
                    owner.generate_draft(segment_arguments=args, transport=bad,
                        policy=fixture._policy(), host=host)
                self.assertIs(rejected.exception.reason, _t.Reason.SCHEMA)
        draft = owner.generate_draft(segment_arguments=args, transport=transport,
            policy=fixture._policy(), host=host).value
        self.assertEqual(draft.source, compiler.source_bytes)
        self.assertEqual(json.loads(commands[0][0]).get('required'), ['source'])
        self.assertIs(commands[0][1], draft.brief)
        self.assertEqual(counter.attempts(), 1)
        sh = d.h
        store = type(sh.store)(owner=sh.owner, incarnation=sh.owner.incarnation, generation=1,
            compiler=compiler.owner, validation_registry=compiler.registry,
            store_policy=sh.store.store_policy, resolver_policy=sh.store.resolver_policy,
            resource_policy=sh.policy, slot_policy=sh.store.slot_policy, clock=sh,
            attempts=sh, reservations=sh, cancellation=sh, endpoint=sh.endpoint,
            boundary=sh.boundary, holder_factory=sh._holder, telemetry=lambda: sh.used,
            scope_check=sh._scope, screen_check=compiler.check_screen, export_receipt_port=sh)
        options = dict(admission=compiler.admitter, inputs=compiler.config, registry=compiler.registry,
            store=store, scope=sh.scope, authority_revision=sh.authority,
            screen_check=compiler.check_screen, export_adapter=exports, resources=resources)
        with self.assertRaises(_t.ContractError):
            owner.submit_draft(replace(draft), **options)
        admitted = owner.submit_draft(draft, **options)
        self.assertEqual(admitted.disposition, 'DRAFT')
        self.assertIs(admitted.value.lifecycle, _t.Lifecycle.DRAFT)
        self.assertTrue(transport.close())

    # @anchor:wa.tests.cu_scripts_runtime_unit.TestGenerationTransport.test_transport_bound_deadline_cancel_and_join
    def test_transport_bound_deadline_cancel_and_join(self):
        association = object()
        transport, counter, commands = self._transport(
            'import sys; sys.stdin.buffer.read(); sys.stdout.write("{\\"source\\":\\"passive\\"}")')
        self.assertEqual(json.loads(self._exchange(transport, association=association))['source'], 'passive')
        self.assertIs(commands[0][1], association)
        self.assertEqual(counter.attempts(), 1)
        stale, stale_counter, _ = self._transport(
            'import sys; sys.stdin.buffer.read(); sys.stdout.write("{}")')
        stale_host = types.SimpleNamespace(check=lambda a, p, response: _t.AdmissionCheck(
            _t.Truth.TRUE if response is None else _t.Truth.FALSE, None))
        with self.assertRaises(ValueError):
            self._exchange(stale, host=stale_host, association=association)
        self.assertEqual(stale_counter.attempts(), 1)
        self.assertTrue(stale.close())
        for body, bound in (('import sys; sys.stdout.write("X" * 10000)', 64),
                            ('import sys; sys.stderr.write("PRIVATE" * 10000)', 64),
                            ('import sys; sys.exit(3)', 4096)):
            transport, counter, _ = self._transport(body, wire_bytes=bound)
            with self.assertRaises(ValueError) as error:
                self._exchange(transport)
            self.assertNotIn('PRIVATE', str(error.exception))
            self.assertEqual(counter.attempts(), 1)
            self.assertTrue(transport.close())
        calls = []
        # @anchor:wa.tests.cu_scripts_runtime_unit.TestGenerationTransport.test_transport_bound_deadline_cancel_and_join.cancelled
        def cancelled():
            calls.append(1)
            return len(calls) > 3
        transport, counter, _ = self._transport('import time; time.sleep(5)', cancelled=cancelled)
        with self.assertRaises(ValueError):
            self._exchange(transport)
        self.assertEqual(counter.attempts(), 1)
        self.assertTrue(transport.close())
        transport, counter, _ = self._transport('pass', cancelled=lambda: True)
        with self.assertRaises(ValueError):
            self._exchange(transport)
        self.assertEqual(counter.attempts(), 0)


        transport, counter, _ = self._transport('import time; time.sleep(5)')
        policy = replace(_generations.GenerationTests()._policy(),
                         absolute_deadline_ns=time.monotonic_ns() - 1)
        host = types.SimpleNamespace(check=lambda *a: _t.AdmissionCheck(_t.Truth.TRUE, None))
        with self.assertRaises(ValueError):
            transport.exchange(b'passive', schema=b'{}', association=association, policy=policy, host=host)
        self.assertEqual(counter.attempts(), 0)
        policy = replace(policy, absolute_deadline_ns=time.monotonic_ns() + 30000000)
        started = time.monotonic_ns()
        with self.assertRaises(ValueError):
            transport.exchange(b'passive', schema=b'{}', association=association, policy=policy, host=host)
        self.assertEqual(counter.attempts(), 1)
        self.assertTrue(transport.close())
        self.assertLess(time.monotonic_ns() - started, 1000000000)  # Cleanup-inclusive fixture ceiling [TARGET].


# @anchor:wa.tests.cu_scripts_runtime_unit._start_registered_fixture
def _start_registered_fixture():
    """Compiler fixture setup without another test's completion-only oracle."""
    host = _execution._CompilerHost(b'def run(act, params):\n    return None\n')
    # Trusted fixture supplies the principal attestation before compilation.
    from tests.scripts.test_compiler import _rewrite_header
    from tests.scripts.test_header import _wire_record
    scope = _execution._dc.replace(host.scope, attestation=host.owner, attestation_reason=None)
    memo = {}
    # @anchor:wa.tests.cu_scripts_runtime_unit._start_registered_fixture.rescope
    def rescope(value):
        if type(value) is _t.ScopeIdentity:
            return scope
        if type(value) in (_t.PrivateRef, _t.RegistryRef):
            return value
        if id(value) in memo:
            return memo[id(value)]
        if _execution._dc.is_dataclass(value):
            result = _execution._dc.replace(value, **{field.name: rescope(getattr(value, field.name))
                                            for field in _execution._dc.fields(value)})
        elif type(value) is tuple:
            result = tuple(rescope(item) for item in value)
        else:
            return value
        memo[id(value)] = result
        return result
    host.scope = scope
    host.entry, host.goal = rescope(host.entry), rescope(host.goal)
    host.config = rescope(host.config)
    host.keys = rescope(host.keys)
    host.vocabulary = rescope(host.vocabulary)
    # @anchor:wa.tests.cu_scripts_runtime_unit._start_registered_fixture.rewrite
    def rewrite(value):
        if type(value) is dict:
            if 'principal' in value and 'scope_revision' in value:
                value.update(_wire_record(scope))
            else:
                for item in value.values():
                    rewrite(item)
        elif type(value) is list:
            for item in value:
                rewrite(item)
    _rewrite_header(host, rewrite)
    host.admitter = host.admission(absolute_deadline_ns=1000000000)
    definition = host._direct()
    try:
        plan = _execution._compiler.validate_plan(definition, session=host.session,
            admission=host.admitter, inputs=host.config, registry=host.registry)
        h = _execution._RuntimeHost()
        h.plan, h.scope = plan, plan.scope
        h.registered_goal = plan.terminal_goal
        h.sh.scope = h.scope
        import copy
        h.sh.plans[id(plan)] = (plan, copy.deepcopy(plan))
        cell = h.sh._admit(plan=plan)
        h.lease = h.sh._acquire(cell)
        h.holder_contract = h.lease.holder_contract
        h.sh.holder_grants[id(h.holder_contract)] = (h.holder_contract, h.lease.holder_token)
        h.holder = _execution._dc.replace(h.holder, scope=h.scope)
        h.observation = _execution._dc.replace(h.observation, scope=h.scope)
        h.goal_truth = _t.Truth.TRUE
        state = _execution._r.start(plan, lease=h.lease, session=host.session,
            admission=host.admitter, inputs=host.config, registry=host.registry, runtime=h)
        return state
    finally:
        host.admitter.finish(host.session)


# @anchor:wa.tests.cu_scripts_runtime_unit._registered_runner
def _registered_runner(case, callback, *, suspend=False, uncertain=False):
    """Intercept the existing compiler fixture at its real public runner start."""
    original = _execution._r.start
    # @anchor:wa.tests.cu_scripts_runtime_unit._registered_runner.start
    def start(plan, **kwargs):
        runner = kwargs['runtime']
        if suspend or uncertain:
            runner.goal_truth = _t.Truth.FALSE
        if uncertain:
            prior_register = runner.register
            # @anchor:wa.tests.cu_scripts_runtime_unit._registered_runner.register_uncertain
            def register_uncertain(state):
                result = prior_register(state)
                state.input_disposition = _t.InputDisposition.INPUT_UNCERTAIN
                return result
            runner.register = register_uncertain
        custody = _integration.ExecutionCustody(plan, kwargs['session'], kwargs['admission'],
            kwargs['inputs'], kwargs['registry'], kwargs['lease'], runner, object())
        session = _integration.IntegrationSession('episode', runner.scope, runner.authority,
            kwargs['lease'].absolute_deadline_ns, 0,
            _integration.RestartState(True, runner.scope, (), runner.restart_evidence, None),
            custody, None, (), (), object(), ())
        owner, host, _, _ = _runtime_fixture(session=session, store_host=runner.sh)
        host.now = runner.clock()
        # @anchor:wa.tests.cu_scripts_runtime_unit._registered_runner.advance_renamed
        def advance_renamed(state):
            expected = dict.fromkeys(('selector', 'ownership', 'observation', 'publication',
                'generation', 'geometry', 'facts', 'lease', 'scope', 'authority'), 'registered')
            expected['structural_screen_digest'] = 'same-global-digest'
            case.assertFalse(owner.guard_drift(expected, dict(expected, selector='renamed'),
                owner=types.SimpleNamespace(check=lambda value: True)))
            return state
        with patch.object(_integration, '_runner', types.SimpleNamespace(start=original,
                advance=advance_renamed if suspend else _execution._r.advance)):
            state = owner.advance_session(session)
        case.assertEqual(runner.input_count, 0)
        callback(owner, host, session, state, kwargs)
        return state
    with patch.object(_execution._r, 'start', start):
        _start_registered_fixture()


# @anchor:wa.tests.cu_scripts_runtime_unit.TestRuntimeCustody
class TestRuntimeCustody(unittest.TestCase):
    # @anchor:wa.tests.cu_scripts_runtime_unit.TestRuntimeCustody.test_exact_compiler_custody
    def test_exact_compiler_custody(self):
        seen = []
        # @anchor:wa.tests.cu_scripts_runtime_unit.TestRuntimeCustody.test_exact_compiler_custody.check
        def check(owner, host, session, state, original):
            seen.append(state)
            self.assertIs(owner.runner_state(session), state)
            self.assertTrue(owner.accept_script(session, state))
            self.assertTrue(owner.verified_completion())
            self.assertFalse(owner.check_session(replace(session)))
            self.assertFalse(owner.check_custody(session, replace(session.custody)))
            foreign = replace(session, episode='foreign-episode')
            foreign_owner, _, _, _ = _runtime_fixture(session=foreign, store_host=state.runtime.sh)
            with self.assertRaises(ValueError):
                foreign_owner.advance_session(foreign)
            self.assertIsNone(foreign_owner.runner_state(foreign))
            artifact = next(iter(owner._artifacts.values()))
            for delta in ({'inputs': replace(artifact.inputs)},
                          {'source_session': replace(artifact.source_session)},
                          {'plan': replace(artifact.plan)}):
                with self.assertRaises((ValueError, _t.ContractError)):
                    owner.register_artifact(replace(artifact, **delta))
            self.assertIs(owner.advance_session(session), state)
            self.assertTrue(json.loads(owner.episode_metrics())['success'])
        _registered_runner(self, check)
        self.assertEqual(len(seen), 1)

    # @anchor:wa.tests.cu_scripts_runtime_unit.TestRuntimeCustody.test_unvalidated_source_never_executes
    def test_unvalidated_source_never_executes(self):
        checked = []
        # @anchor:wa.tests.cu_scripts_runtime_unit.TestRuntimeCustody.test_unvalidated_source_never_executes.check
        def check(owner, host, session, state, original):
            artifact = next(iter(owner._artifacts.values()))
            for invalid in (b'import os\n', b'open("PRIVATE")\n', object(), replace(artifact.plan)):
                with self.assertRaises((ValueError, _t.ContractError)):
                    owner.register_artifact(replace(artifact, plan=invalid))
            checked.append(owner.accept_script(session, state))
            self.assertEqual(state.runtime.input_count, 0)
        _registered_runner(self, check)
        self.assertEqual(checked, [True])


# @anchor:wa.tests.cu_scripts_runtime_unit._wire_miss
def _wire_miss(owner, host, h):
    cell = h._admit(auxiliary=(('META:fts', ('logical1',)),))
    h._publish()
    adapter, acquisition, arguments = _observation_host(h.scope, h.authority, h.f['observation'])
    owner.services = replace(owner.services, observation=adapter, acquisition=lambda *a: acquisition,
        cleanup=types.SimpleNamespace(check_owned=lambda value: adapter.check_evidence(value),
            drain=lambda value: adapter.release_observation(value.allocation),
            check_drained=lambda value: not adapter.check_evidence(value)))
    host.resolve_request = lambda s, e: h._request(cell, goal_contract='unmatched')
    return adapter


# @anchor:wa.tests.cu_scripts_runtime_unit.TestRuntimeLauncher
class TestRuntimeLauncher(unittest.TestCase):
    # @anchor:wa.tests.cu_scripts_runtime_unit.TestRuntimeLauncher.test_live_on_injects_runtime
    def test_live_on_injects_runtime(self):
        from wender.learn import cu_loop as loop
        from wender.learn import model_client
        fixture = _loop.TestCuLoop()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        cap = fixture.capsule(['a'])
        owner, host, h, _ = _runtime_fixture()
        adapter = _wire_miss(owner, host, h)
        # @anchor:wa.tests.cu_scripts_runtime_unit.TestRuntimeLauncher.test_live_on_injects_runtime.Capsule
        class Capsule:
            # @anchor:wa.tests.cu_scripts_runtime_unit.TestRuntimeLauncher.test_live_on_injects_runtime.Capsule.__new__
            def __new__(cls, *a, **kw):
                return cap
        # @anchor:wa.tests.cu_scripts_runtime_unit.TestRuntimeLauncher.test_live_on_injects_runtime.Describer
        class Describer:
            # @anchor:wa.tests.cu_scripts_runtime_unit.TestRuntimeLauncher.test_live_on_injects_runtime.Describer.__new__
            def __new__(cls, *a, **kw):
                return _loop.Describer()
        # @anchor:wa.tests.cu_scripts_runtime_unit.TestRuntimeLauncher.test_live_on_injects_runtime.Decider
        class Decider:
            # @anchor:wa.tests.cu_scripts_runtime_unit.TestRuntimeLauncher.test_live_on_injects_runtime.Decider.__new__
            def __new__(cls, *a, **kw):
                return decider
        # @anchor:wa.tests.cu_scripts_runtime_unit.TestRuntimeLauncher.test_live_on_injects_runtime.ClosingReplay
        class ClosingReplay(ReplayDecider):
            # @anchor:wa.tests.cu_scripts_runtime_unit.TestRuntimeLauncher.test_live_on_injects_runtime.ClosingReplay.close
            def close(self):
                self.closed = True
        decider = ClosingReplay([_loop.proposal('done')])
        args = types.SimpleNamespace(max_ms=30000, max_steps=1, no_progress_steps=2,
            mask=[], ve_root=None, effort='medium', muse_timeout=2, keep_crops=False, images='marks')
        factory = Mock(return_value=owner)
        observed = []
        original = loop.run_loop
        # @anchor:wa.tests.cu_scripts_runtime_unit.TestRuntimeLauncher.test_live_on_injects_runtime.run
        def run(*args, **kwargs):
            observed.append(kwargs.get('integration'))
            return original(*args, **kwargs, clock=fixture.clock, sleep=fixture.clock.sleep)
        with patch.object(loop, 'SrfCapsule', Capsule), patch.object(loop, 'BridgeDescriber', Describer), \
             patch.object(loop, 'ModelDecider', Decider), patch.object(model_client, 'MuseBackend'), \
             patch.object(loop, 'run_loop', side_effect=run):
            result, status, metrics = cu_live.launch_scripts(args, 'synthetic', 'test',
                fixture.root / 'scripts-trace.jsonl' if hasattr(fixture, 'root') else
                Path(fixture.tmp.name) / 'scripts-trace.jsonl', runtime_factory=factory)
        self.assertEqual(status, 0, result)
        self.assertEqual(result['stop_reason'], 'done')
        self.assertEqual(observed, [owner])
        self.assertIs(observed[0], owner)
        self.assertIn('observe', owner._latencies)
        self.assertTrue(json.loads(metrics)['cleanup_complete'])
        self.assertTrue(decider.closed)
        self.assertTrue(adapter.close())
        factory.assert_called_once_with(mode='live')

    # @anchor:wa.tests.cu_scripts_runtime_unit.TestRuntimeLauncher.test_web_on_injects_runtime
    def test_web_on_injects_runtime(self):
        fixture = _web.WebUnit()
        fixture.setUp()
        clock = _web.Clock()
        owner, host, h, _ = _runtime_fixture()
        adapter = _wire_miss(owner, host, h)
        observed = []
        original = cu_web.run_web_cycle
        # @anchor:wa.tests.cu_scripts_runtime_unit.TestRuntimeLauncher.test_web_on_injects_runtime.run
        def run(*args, **kwargs):
            observed.append(kwargs.get('integration'))
            return original(*args, **kwargs, clock=clock, sleep=clock.sleep)
        with patch.object(cu_web, 'SrfIdentitySurface', return_value=_web.FakeSurface()), \
             patch.object(cu_web, 'MuseBackend', return_value=_web.FakePlanner()), \
             patch.object(cu_web, 'LocalBackend', return_value=_web.FakeExecutor()), \
             patch.object(cu_web, 'record_workflow'), \
             patch.object(cu_web, 'run_web_cycle', side_effect=run), \
             patch.object(Path, 'read_text', return_value=json.dumps(fixture.memory)), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            status = cu_web.main(['--scripts', 'on', '--task', 'synthetic', '--endpoint', 'http://offline.invalid'],
                                 runtime_factory=lambda **kwargs: owner)
        lines = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(status, 0, lines)
        self.assertEqual(observed, [owner])
        self.assertTrue(lines[0]['goal_verified'])
        self.assertEqual(lines[1]['schema'], 'wender.scripts.episode/v1')
        self.assertTrue(lines[1]['cleanup_complete'])
        self.assertTrue(adapter.close())


# @anchor:wa.tests.cu_scripts_runtime_unit.TestRuntimeMutationAudit
class TestRuntimeMutationAudit(unittest.TestCase):
    # @anchor:wa.tests.cu_scripts_runtime_unit.TestRuntimeMutationAudit.test_runtime_check_removal
    def test_runtime_check_removal(self):
        cases = [
            ('suspension_fence', TestFrozenAcceptance, 'test_suspend_returns_to_model'),
            ('drift_fence', TestFrozenAcceptance, 'test_suspend_returns_to_model'),
            ('privacy_allowlist', TestRuntimeMetrics, 'test_privacy_canaries'),
            ('verify_boolean', TestFrozenAcceptance, 'test_model_verification_boolean_through_loop'),
            ('authority', TestRuntimeOwners, 'test_restart_and_authority_fence'),
            ('fresh_grant', TestRuntimeOwners, 'test_restart_and_authority_fence'),
            ('observation_identity', TestRuntimeOwners, 'test_scope_and_observation_mapping'),
            ('acquisition', TestRuntimeOwners, 'test_scope_and_observation_mapping'),
            ('drift_selector', TestRuntimeOwners, 'test_drift_rename_and_move'),
            ('drift_geometry', TestRuntimeOwners, 'test_drift_rename_and_move'),
            ('uncertain', TestRuntimeOwners, 'test_uncertain_never_falls_through'),
            ('OWNER_DRAIN', TestRuntimeOwners, 'test_drain_identity_inventory'),
            ('COMPILER_PAIRING', TestRuntimeCustody, 'test_unvalidated_source_never_executes'),
            ('ARTIFACT_ALREADY_REGISTERED', TestRuntimeCustody, 'test_exact_compiler_custody'),
            ('accounting', TestRuntimeOwners, 'test_resident_accounting_release'),
            ('MODEL_BUDGET', TestRuntimeOwners, 'test_runtime_metrics_and_provider_boundary'),
            ('GENERATION_WIRE_BOUND', TestGenerationTransport, 'test_transport_bound_deadline_cancel_and_join'),
            ('schema', TestGenerationTransport, 'test_generation_schema_and_admission'),
            ('response_association', TestGenerationTransport, 'test_transport_bound_deadline_cancel_and_join'),
            ('collection_deadline', TestGenerationTransport, 'test_transport_bound_deadline_cancel_and_join')]
        source = Path(runtime.__file__).read_text()
        for kind, case, name in cases:
            with self.subTest(kind=kind):
                control = unittest.TestResult()
                case(name).run(control)
                self.assertTrue(control.wasSuccessful(), control.errors or control.failures)
                changed = []
                method_name = 'accept_script' if kind == 'suspension_fence' else 'guard_drift'
                method = next(n for n in ast.walk(ast.parse(source))
                    if isinstance(n, ast.FunctionDef) and n.name == method_name)
                # @anchor:wa.tests.cu_scripts_runtime_unit.TestRuntimeMutationAudit.test_runtime_check_removal.Remove
                class Remove(ast.NodeTransformer):
                    # @anchor:wa.tests.cu_scripts_runtime_unit.TestRuntimeMutationAudit.test_runtime_check_removal.Remove.visit_Compare
                    def visit_Compare(self, node):
                        if kind == 'verify_boolean' and ast.unparse(node) == 'self.services.actions.verify(action, before, after) is True':
                            changed.append(node.lineno)
                            return ast.copy_location(node.left, node)
                        if kind == 'authority' and ast.unparse(node) == 'self.current_authority(session.scope) == session.authority':
                            changed.append(node.lineno)
                            return ast.copy_location(ast.Constant(True), node)
                        if kind == 'fresh_grant' and 'self._host.check_fresh_grant(' in ast.unparse(node):
                            changed.append(node.lineno)
                            return ast.copy_location(ast.Constant(True), node)
                        if kind == 'acquisition' and 'self._owner.check_acquisition(' in ast.unparse(node):
                            changed.append(node.lineno)
                            return ast.copy_location(ast.Constant(True), node)
                        if kind == 'observation_identity' and ast.unparse(node) == 'self._evidence.get(id(evidence)) is evidence':
                            changed.append(node.lineno)
                            return ast.copy_location(ast.Constant(True), node)
                        return self.generic_visit(node)
                    # @anchor:wa.tests.cu_scripts_runtime_unit.TestRuntimeMutationAudit.test_runtime_check_removal.Remove.visit_Assign
                    def visit_Assign(self, node):
                        text = ast.unparse(node)
                        if (kind in ('suspension_fence', 'drift_fence')
                                and method.lineno <= node.lineno <= method.end_lineno
                                and text.startswith('self._fenced =')):
                            changed.append(node.lineno)
                            node.value = ast.copy_location(ast.Constant(True), node.value)
                        if kind in ('drift_selector', 'drift_geometry') and text.startswith('accepted = _policy.guard_drift'):
                            changed.append(node.lineno)
                            field = 'selector' if kind == 'drift_selector' else 'geometry'
                            node.value.args[1] = ast.Call(func=ast.Name(id='dict', ctx=ast.Load()),
                                args=[ast.Name(id='actual', ctx=ast.Load())], keywords=[ast.keyword(arg=field,
                                value=ast.Subscript(value=ast.Name(id='expected', ctx=ast.Load()),
                                slice=ast.Constant(field), ctx=ast.Load()))])
                        if kind == 'schema' and text.startswith('argv = self._command(schema, association)'):
                            changed.append(node.lineno)
                            node.value.args[0] = ast.Constant(b'{}')
                        return self.generic_visit(node)
                    # @anchor:wa.tests.cu_scripts_runtime_unit.TestRuntimeMutationAudit.test_runtime_check_removal.Remove.visit_While
                    def visit_While(self, node):
                        if kind == 'collection_deadline' and ast.unparse(node.test) == 'selector.get_map()':
                            for index, statement in enumerate(node.body):
                                if isinstance(statement, ast.Expr) and ast.unparse(statement) == 'self._eligible(association, policy, host)':
                                    changed.append(statement.lineno)
                                    node.body[index] = ast.copy_location(ast.Pass(), statement)
                        return self.generic_visit(node)
                    # @anchor:wa.tests.cu_scripts_runtime_unit.TestRuntimeMutationAudit.test_runtime_check_removal.Remove.visit_Expr
                    def visit_Expr(self, node):
                        text = ast.unparse(node)
                        match = (kind == 'privacy_allowlist' and text.startswith((
                                '_code(self.reason,', '_code(self.provenance,',
                                '_need(self.provenance', '_need(self.reason'))
                            or kind == 'response_association' and text == 'self._eligible(association, policy, host, response)'
                            or kind == 'uncertain' and text == 'self.uncertain(action)'
                            or kind == 'accounting' and text == '_quantity(amount)'
                            or kind == 'OWNER_DRAIN' and text.startswith('_need(self.services.cleanup.check_drained')
                            or kind in ('COMPILER_PAIRING', 'ARTIFACT_ALREADY_REGISTERED', 'GENERATION_WIRE_BOUND', 'MODEL_BUDGET')
                            and isinstance(node.value, ast.Call) and isinstance(node.value.func, ast.Name)
                            and node.value.func.id == '_need' and any(isinstance(a, ast.Constant)
                                and a.value == kind for a in node.value.args))
                        if match:
                            changed.append(node.lineno)
                            return ast.copy_location(ast.Pass(), node)
                        return self.generic_visit(node)
                tree = Remove().visit(ast.parse(source))
                self.assertTrue(changed, kind)
                mutant = types.ModuleType('wender.learn._runtime_owner_mutant')
                with patch.dict(sys.modules, {mutant.__name__: mutant}):
                    exec(compile(ast.fix_missing_locations(tree), '<isolated-owner-mutant>', 'exec'), mutant.__dict__)
                    with patch.object(sys.modules[__name__], 'runtime', mutant):
                        result = unittest.TestResult()
                        case(name).run(result)
                self.assertFalse(result.errors, result.errors)
                self.assertTrue(result.failures, 'surviving owner mutant: ' + kind)


# @anchor:wa.tests.cu_scripts_runtime_unit.TestContextAdapter
class TestContextAdapter(unittest.TestCase):
    # @anchor:wa.tests.cu_scripts_runtime_unit.TestContextAdapter.test_context_epoch_custody
    def test_context_epoch_custody(self):
        f = _generations._Fixture()
        adapter = runtime.ContextAdapter(arguments=f.options, max_epochs=1)
        epoch = adapter.open()
        options = dict(request_id='request', resolve_request=f.resolve_request)
        with self.assertRaises(ValueError):
            adapter.pack(replace(epoch), f.evidence, **options)
        packed = adapter.pack(epoch, f.evidence, **options)
        self.assertIsNotNone(packed)
        self.assertTrue(adapter.close())
        self.assertTrue(adapter.close())
        self.assertEqual(f.resources.live, {})
        with self.assertRaises(ValueError):
            adapter.pack(epoch, f.evidence, **options)

    # @anchor:wa.tests.cu_scripts_runtime_unit.TestContextAdapter.test_offline_graph_closed_inventory
    def test_offline_graph_closed_inventory(self):
        owner = runtime.build_offline_runtime(mode='offline')
        self.assertFalse(owner.check_ready())
        inventory = dict(owner.services.integration.construction_inventory())
        self.assertIs(inventory['runtime'], owner)
        self.assertTrue(all(owner.owns(v) for k, v in inventory.items() if k != 'runtime'))
        with self.assertRaises(ValueError):
            inventory['context'].open()
        with self.assertRaises(_t.ContractError):
            inventory['admission'].admit(b'def run(act, params):\n    return None\n')
        self.assertTrue(owner.close())
        self.assertTrue(owner.close())
        self.assertTrue(all(owner.services.cleanup.check_drained(v)
                            for k, v in inventory.items() if k != 'runtime'))
        # @anchor:wa.tests.cu_scripts_runtime_unit.TestContextAdapter.test_offline_graph_closed_inventory.broken
        def broken(*, mode):
            instance = runtime.build_offline_runtime(mode=mode)
            instance.services.cleanup.check_drained = lambda value: False
            return instance
        report = runtime.check_offline_construction(broken)
        self.assertTrue(report['construction_complete'])
        self.assertFalse(report['cleanup_complete'])


# @anchor:wa.tests.cu_scripts_runtime_unit.TestLauncherMutationAudit
class TestLauncherMutationAudit(unittest.TestCase):
    # @anchor:wa.tests.cu_scripts_runtime_unit.TestLauncherMutationAudit.test_web_off_hidden_integration_mutant
    def test_web_off_hidden_integration_mutant(self):
        control = unittest.TestResult()
        TestRuntimeCLI('test_web_off_owner_contract').run(control)
        self.assertTrue(control.wasSuccessful(), control.errors or control.failures)
        tree = ast.parse(Path(cu_web.__file__).read_text())
        changed = []
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id == 'run_web_cycle'
                    and not any(k.arg == 'integration' for k in node.keywords)):
                node.keywords.append(ast.keyword(arg='integration', value=ast.Constant(None)))
                changed.append(node.lineno)
        self.assertTrue(changed)
        mutant = types.ModuleType('_off_integration_mutant')
        mutant.__file__ = cu_web.__file__
        exec(compile(ast.fix_missing_locations(tree), '<isolated-off-contract-mutant>', 'exec'), mutant.__dict__)
        with patch.object(sys.modules[__name__], 'cu_web', mutant):
            result = unittest.TestResult()
            TestRuntimeCLI('test_web_off_owner_contract').run(result)
        self.assertFalse(result.errors, result.errors)
        self.assertTrue(result.failures, 'surviving hidden off integration')

    # @anchor:wa.tests.cu_scripts_runtime_unit.TestLauncherMutationAudit.test_integration_keyword_removal
    def test_integration_keyword_removal(self):
        for module, name in ((cu_live, 'test_live_on_injects_runtime'),
                             (cu_web, 'test_web_on_injects_runtime')):
            with self.subTest(module=module.__name__):
                result = unittest.TestResult()
                TestRuntimeLauncher(name).run(result)
                self.assertTrue(result.wasSuccessful(), result.errors or result.failures)
                changed = []
                # @anchor:wa.tests.cu_scripts_runtime_unit.TestLauncherMutationAudit.test_integration_keyword_removal.Remove
                class Remove(ast.NodeTransformer):
                    # @anchor:wa.tests.cu_scripts_runtime_unit.TestLauncherMutationAudit.test_integration_keyword_removal.Remove.visit_Call
                    def visit_Call(self, node):
                        self.generic_visit(node)
                        if isinstance(node.func, ast.Name) and node.func.id in ('run_loop', 'run_web_cycle'):
                            for keyword in tuple(node.keywords):
                                if keyword.arg == 'integration':
                                    changed.append(node.lineno)
                                    node.keywords.remove(keyword)
                        return node
                tree = Remove().visit(ast.parse(Path(module.__file__).read_text()))
                self.assertTrue(changed)
                mutant = types.ModuleType('_launcher_mutant')
                mutant.__file__ = module.__file__
                exec(compile(ast.fix_missing_locations(tree), '<isolated-launcher-mutant>', 'exec'), mutant.__dict__)
                target = 'cu_live' if module is cu_live else 'cu_web'
                with patch.object(sys.modules[__name__], target, mutant):
                    result = unittest.TestResult()
                    TestRuntimeLauncher(name).run(result)
                self.assertFalse(result.errors, result.errors)
                self.assertTrue(result.failures, 'surviving integration removal')


# @anchor:wa.tests.cu_scripts_runtime_unit.TestUpstreamBoundaryMutations
class TestUpstreamBoundaryMutations(unittest.TestCase):
    # @anchor:wa.tests.cu_scripts_runtime_unit.TestUpstreamBoundaryMutations.test_public_adapter_check_removal
    def test_public_adapter_check_removal(self):
        cases = [('mapping', _integration, TestRuntimeOwners, 'test_scope_and_observation_mapping'),
                 ('attestation', _integration, TestRuntimeOwners, 'test_scope_and_observation_mapping'),
                 ('source_syntax', _generations._g, TestGenerationTransport, 'test_generation_schema_and_admission'),
                 ('response_schema', _generations._g, TestGenerationTransport, 'test_generation_schema_and_admission')]
        for kind, module, case, name in cases:
            with self.subTest(kind=kind):
                result = unittest.TestResult()
                case(name).run(result)
                self.assertTrue(result.wasSuccessful(), result.errors or result.failures)
                changed = []
                # @anchor:wa.tests.cu_scripts_runtime_unit.TestUpstreamBoundaryMutations.test_public_adapter_check_removal.Remove
                class Remove(ast.NodeTransformer):
                    # @anchor:wa.tests.cu_scripts_runtime_unit.TestUpstreamBoundaryMutations.test_public_adapter_check_removal.Remove.visit_Expr
                    def visit_Expr(self, node):
                        text = ast.unparse(node)
                        if not text.startswith('_need('):
                            return self.generic_visit(node)
                        match = (kind == 'mapping' and "'SCOPE_MAPPING'" in text
                            or kind == 'attestation' and "'SCOPE_ATTESTATION'" in text
                            or kind == 'source_syntax' and '(_ast.Import, _ast.ImportFrom)' in text
                            or kind == 'response_schema' and "set(result) <= {'source', 'predecessor_proposal'}" in text)
                        if match:
                            changed.append(node.lineno)
                            return ast.copy_location(ast.Pass(), node)
                        return self.generic_visit(node)
                tree = Remove().visit(ast.parse(Path(module.__file__).read_text()))
                self.assertTrue(changed, kind)
                mutant = types.ModuleType('wender.learn._upstream_boundary_mutant')
                mutant.__package__ = 'wender.learn'
                with patch.dict(sys.modules, {mutant.__name__: mutant}):
                    exec(compile(ast.fix_missing_locations(tree), '<isolated-upstream-boundary-mutant>', 'exec'), mutant.__dict__)
                    with contextlib.ExitStack() as stack:
                        if kind in ('mapping', 'attestation'):
                            stack.enter_context(patch.object(sys.modules[__name__], '_integration', mutant))
                            stack.enter_context(patch.object(runtime, '_integration', mutant))
                        else:
                            stack.enter_context(patch.object(_generations, '_g', mutant))
                            stack.enter_context(patch.object(runtime, '_generation', mutant))
                        result = unittest.TestResult()
                        case(name).run(result)
                self.assertFalse(result.errors, result.errors)
                self.assertTrue(result.failures, 'surviving adapter boundary mutant: ' + kind)


if __name__ == '__main__':
    unittest.main()
