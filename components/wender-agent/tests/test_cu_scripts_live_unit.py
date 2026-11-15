# @module {"id":"wa.tests.cu_scripts_live_unit","layer":4,"owner":"LEARN","deps":["wa.learn.script_live","wa.learn.script_runtime","wa.learn.cu_loop","wa.learn.model_client","wa.tools.cu_live","wa.tools.cu_web","wa.tests.test_cu_loop_unit","wa.tests.cu_web_unit","wa.tests.cu_live_unit","wa.tests.cu_scripts_runtime_unit"],"book":"HB-WA-s4-learn"}
"""S11B legacy composition regression controls; all transports stay synthetic.

These tests do not certify the still-missing attested live composition.
"""
import ast
import contextlib
import io
import json
from pathlib import Path
import subprocess
import types
import unittest
from unittest.mock import Mock, patch

from tools import cu_live, cu_web
from wender.learn import cu_loop as loop, script_live
from wender.learn.script_runtime import build_offline_runtime
from wender.learn.model_client import MuseBackend
from tests import test_cu_loop_unit as fixtures
from tests import test_cu_web_unit as web
from tests import test_cu_live_unit as live
from tests import test_cu_scripts_runtime_unit as runtime_fixtures


# @anchor:wa.tests.cu_scripts_live_unit.TestLegacyDispatch
class TestLegacyDispatch(unittest.TestCase):
    # @anchor:wa.tests.cu_scripts_live_unit.TestLegacyDispatch.setUp
    def setUp(self):
        self.surface = loop.SrfCapsule('synthetic', 'capsule')
        self.owner = script_live.LegacyDispatchOwner(self.surface)
        self.surface.dispatcher = self.owner
        self.addCleanup(self.owner.close)
        self.action = {'kind': 'click', 'x': 2, 'y': 3}

    # @anchor:wa.tests.cu_scripts_live_unit.TestLegacyDispatch.test_dispatch_single_crossing
    def test_dispatch_single_crossing(self):
        receipt = {'input_mono_ns': 10}
        with patch.object(loop, '_json_command', return_value=receipt) as transport:
            self.assertIs(self.surface.input(self.action), receipt)
        transport.assert_called_once_with(['capsule', 'input', '--container', 'synthetic',
            '--click', '2,3', '--button', '1'], timeout=30)

    # @anchor:wa.tests.cu_scripts_live_unit.TestLegacyDispatch.test_missing_dispatcher_refuses
    def test_missing_dispatcher_refuses(self):
        self.surface.dispatcher = None
        with patch.object(loop, '_json_command', return_value={'input_mono_ns': 10}) as transport:
            with self.assertRaisesRegex(ValueError, 'DISPATCH_OWNER_UNAVAILABLE'):
                self.surface.input(self.action)
            transport.assert_not_called()

    # @anchor:wa.tests.cu_scripts_live_unit.TestLegacyDispatch.test_foreign_surface_refuses
    def test_foreign_surface_refuses(self):
        foreign = loop.SrfCapsule('synthetic', 'capsule', dispatcher=self.owner)
        with patch.object(loop, '_json_command') as transport:
            with self.assertRaisesRegex(ValueError, 'DISPATCH_OWNER_UNAVAILABLE'):
                foreign.input(self.action)
            transport.assert_not_called()
        self.test_dispatch_single_crossing()

    # @anchor:wa.tests.cu_scripts_live_unit.TestLegacyDispatch.test_recursive_callback_refuses
    def test_recursive_callback_refuses(self):
        physical = Mock()
        with self.assertRaisesRegex(ValueError, 'DISPATCH_OWNER_UNAVAILABLE'):
            self.owner.dispatch(self.surface, self.action,
                lambda: self.owner.dispatch(self.surface, self.action, physical))
        physical.assert_not_called()
        self.assertFalse(self.owner.check_ready())

    # @anchor:wa.tests.cu_scripts_live_unit.TestLegacyDispatch.test_uncertainty_never_replays
    def test_uncertainty_never_replays(self):
        physical = Mock(side_effect=TimeoutError('lost completion'))
        with self.assertRaises(TimeoutError):
            self.owner.dispatch(self.surface, self.action, physical)
        physical.assert_called_once()
        next_send = Mock()
        with self.assertRaisesRegex(ValueError, 'DISPATCH_OWNER_UNAVAILABLE'):
            self.owner.dispatch(self.surface, self.action, next_send)
        next_send.assert_not_called()
        self.assertFalse(self.owner.charge_model())
        self.assertFalse(self.owner.check_script_miss())
        with self.assertRaisesRegex(ValueError, 'DISPATCH_OWNER_UNAVAILABLE'):
            self.owner.select_script({}, {})

    # @anchor:wa.tests.cu_scripts_live_unit.TestLegacyDispatch.test_explicit_uncertainty_and_close
    def test_explicit_uncertainty_and_close(self):
        self.assertTrue(self.owner.check_ready())
        self.assertIsNone(self.owner.select_script({}, {}))
        self.assertTrue(self.owner.check_script_miss())
        self.assertTrue(self.owner.charge_model())
        self.owner.uncertain(self.action)
        with patch.object(loop, '_json_command') as transport:
            with self.assertRaisesRegex(ValueError, 'DISPATCH_OWNER_UNAVAILABLE'):
                self.surface.input(self.action)
            transport.assert_not_called()
        self.assertTrue(self.owner.close())
        self.assertTrue(self.owner.close())
        self.assertFalse(self.owner.check_ready())
        self.assertIsNone(self.owner._surface)


# @anchor:wa.tests.cu_scripts_live_unit.TestLauncherRegression
class TestLauncherRegression(unittest.TestCase):
    # @anchor:wa.tests.cu_scripts_live_unit.TestLauncherRegression.setUp
    def setUp(self):
        self.fixture = fixtures.TestCuLoop()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root

    # @anchor:wa.tests.cu_scripts_live_unit.TestLauncherRegression._click_run
    def _click_run(self, entry, *, baseline=False):
        cap = self.fixture.capsule(['a', 'b'])
        responses = [fixtures.proposal('click', x=2, y=3), fixtures.proposal('done')]
        replay = self.root / 'decisions.json'
        replay.write_text(json.dumps(responses))
        trace = self.root / 'trace.jsonl'
        trace.write_text('')
        physical = []
        clock = fixtures.Clock()
        original_run = loop.run_loop
        original_init = loop.SrfCapsule.__init__

        # @anchor:wa.tests.cu_scripts_live_unit.TestLauncherRegression._click_run.command
        def command(argv, **kwargs):
            if argv[1] == 'frame':
                return dict(cap.frame(), capture_mono_ns=100)
            physical.append(list(argv))
            cap.input({'kind': 'click', 'x': 2, 'y': 3})
            return {'input_mono_ns': 10}

        # @anchor:wa.tests.cu_scripts_live_unit.TestLauncherRegression._click_run.run
        def run(*args, **kwargs):
            return original_run(*args, **kwargs, clock=clock, sleep=clock.sleep)

        # @anchor:wa.tests.cu_scripts_live_unit.TestLauncherRegression._click_run.init
        def init(surface, *args, **kwargs):
            original_init(surface, *args, **kwargs)
            if baseline:
                surface.dispatcher = web._TrustedWebIntegration()

        argv = ['--goal', 'test', '--capsule', 'srf:synthetic', '--decider',
            'replay:' + str(replay), '--trace', str(trace), '--capsule-binary', 'capsule']
        with patch.object(loop, '_json_command', side_effect=command), \
             patch.object(loop.SrfCapsule, '__init__', init), \
             patch.object(loop, 'SubprocessVisionEngine', return_value=Mock(timeout=30)), \
             patch.object(loop.BridgeDescriber, 'describe', return_value={
                 'schema': 've.cu_describe/v1', 'frame_sha256': 'a' * 64,
                 'width': 16, 'height': 16, 'changed': False,
                 'changed_regions': [], 'crops': []}), \
             patch.object(loop, 'run_loop', side_effect=run), \
             contextlib.redirect_stdout(io.StringIO()) as out:
            status = entry(argv)
        return status, out.getvalue(), trace.read_bytes(), physical

    # @anchor:wa.tests.cu_scripts_live_unit.TestLauncherRegression.test_off_click_episode_and_equivalence
    def test_off_click_episode_and_equivalence(self):
        baseline = self._click_run(loop.main, baseline=True)
        composed = self._click_run(cu_live.loop_main)
        self.assertEqual(composed[0], 0, composed[1])
        self.assertEqual(json.loads(composed[1])['steps'], 1)
        self.assertEqual(json.loads(composed[1])['verified_ok'], 1)
        self.assertEqual(len(composed[3]), 1)
        self.assertEqual(composed, baseline)

    # @anchor:wa.tests.cu_scripts_live_unit.TestLauncherRegression.test_unbound_cli_fails_before_physical_input
    def test_unbound_cli_fails_before_physical_input(self):
        status, output, trace, physical = self._click_run(loop.main)
        self.assertEqual(status, 2)
        self.assertEqual(json.loads(output)['failure']['stage'], 'input')
        self.assertEqual(physical, [])

    # @anchor:wa.tests.cu_scripts_live_unit.TestLauncherRegression.test_web_off_installs_legacy_owner
    def test_web_off_installs_legacy_owner(self):
        surface = web.FakeSurface()
        del surface.integration
        clock = web.Clock()
        original = cu_web.run_web_cycle
        dispatched = []
        original_dispatch = script_live.LegacyDispatchOwner.dispatch
        def _dispatch(owner, target, action, send):
            self.assertIs(owner, surface.integration)
            self.assertIs(target, surface)
            dispatched.append(action)
            return original_dispatch(owner, target, action, send)
        # @anchor:wa.tests.cu_scripts_live_unit.TestLauncherRegression.test_web_off_installs_legacy_owner.run
        def run(*args, **kwargs):
            return original(*args, **kwargs, clock=clock, sleep=clock.sleep)
        with patch.object(cu_web, 'SrfIdentitySurface', return_value=surface), \
             patch.object(cu_web, 'MuseBackend', return_value=web.FakePlanner()), \
             patch.object(cu_web, 'LocalBackend', return_value=web.FakeExecutor()), \
             patch.object(cu_web, 'record_workflow'), \
             patch.object(cu_web, 'run_web_cycle', side_effect=run), \
             patch.object(script_live.LegacyDispatchOwner, 'dispatch', _dispatch), \
             contextlib.redirect_stdout(io.StringIO()) as out:
            code = cu_web.main(['--scripts', 'off', '--task', 'Question',
                '--endpoint', 'http://offline.invalid'])
        self.assertEqual(code, 0, out.getvalue())
        self.assertTrue(json.loads(out.getvalue())['goal_verified'])
        self.assertEqual([a['kind'] for a in surface.calls],
                         ['key', 'type', 'key', 'click', 'type', 'click', 'key'])
        self.assertEqual(dispatched, surface.calls)
        self.assertFalse(surface.integration.check_ready())
        self.assertIsNone(surface.integration._surface)

    # @anchor:wa.tests.cu_scripts_live_unit.TestLauncherRegression.test_web_unregistered_owner_refuses
    def test_web_unregistered_owner_refuses(self):
        surface = web.FakeSurface()
        del surface.integration
        result = loop.run_web_cycle(surface, web.FakePlanner(), web.FakeExecutor(),
            json.loads(cu_web.MEMORY.read_text()), task='Question', trace=io.StringIO())
        self.assertFalse(result['goal_verified'])
        self.assertEqual(surface.calls, [])

    # @anchor:wa.tests.cu_scripts_live_unit.TestLauncherRegression.test_dry_check_remains_offline
    def test_dry_check_remains_offline(self):
        for main in (cu_live.main, cu_web.main):
            with self.subTest(main=main.__module__), \
                 patch.object(loop, 'SrfCapsule') as capsule, \
                 patch.object(cu_web, 'SrfIdentitySurface') as surface, \
                 patch.object(cu_web, 'MuseBackend') as muse, \
                 contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(main(['--scripts', 'on', '--dry-check']), 0)
                report = json.loads(out.getvalue())
                self.assertTrue(report['construction_complete'])
                self.assertTrue(report['cleanup_complete'])
                capsule.assert_not_called()
                surface.assert_not_called()
                muse.assert_not_called()
        runtime = build_offline_runtime(mode='offline')
        self.assertFalse(runtime.check_ready())
        self.assertTrue(runtime.close())

    # @anchor:wa.tests.cu_scripts_live_unit.TestLauncherRegression.test_metrics_written_on_failure
    def test_metrics_written_on_failure(self):
        fixture = live.TestLive()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.argv += ['--scripts', 'on']
        runtime, host, h, unused = runtime_fixtures._runtime_fixture()
        adapter = runtime_fixtures._wire_miss(runtime, host, h)
        self.addCleanup(adapter.close)
        self.addCleanup(runtime.close)
        cap = self.fixture.capsule(['a'])

        # @anchor:wa.tests.cu_scripts_live_unit.TestLauncherRegression.test_metrics_written_on_failure.lifecycle
        def lifecycle(argv, **kwargs):
            result = [fixture.slot] if argv[1] == 'start' else {}
            return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(result))

        with patch.object(cu_live.subprocess, 'run', side_effect=lifecycle), \
             patch.object(loop, '_json_command', side_effect=lambda *a, **k: cap.frame()), \
             patch.object(loop, 'SubprocessVisionEngine', return_value=Mock(timeout=30)), \
             patch.object(loop.BridgeDescriber, 'describe', return_value={
                 'schema': 've.cu_describe/v1', 'width': 16, 'height': 16,
                 'changed': False, 'changed_regions': [], 'crops': []}), \
             patch.object(MuseBackend, 'generate', side_effect=TimeoutError('provider timeout')) as provider, \
             contextlib.redirect_stdout(io.StringIO()) as out:
            status = cu_live.main(fixture.argv, runtime_factory=lambda **kwargs: runtime)
        self.assertEqual(status, 2)
        provider.assert_called_once()
        metric_path = fixture.run_dir / 'script_metrics.jsonl'
        self.assertTrue(metric_path.is_file())
        metric_bytes = metric_path.read_text()
        self.assertEqual(out.getvalue().splitlines()[-1] + '\n', metric_bytes)
        metrics = json.loads(metric_bytes)
        self.assertEqual(metrics['muse_calls'], 1)
        self.assertIsNone(metrics['success'])
        self.assertTrue(metrics['cleanup_complete'])
        self.assertIsNotNone(metrics['latency_ns']['model']['value'])
        self.assertEqual(metrics['latency_ns']['model']['provenance'], 'owner_monotonic')
        self.assertIn('terminal_evidence_unavailable', str(metrics['missing_reasons']))
        summary = json.loads((fixture.run_dir / 'summary.json').read_text())
        self.assertFalse(summary['goal_verified'])
        self.assertTrue(summary['cleanup']['ok'])

    # @anchor:wa.tests.cu_scripts_live_unit.TestLauncherRegression.test_scripts_off_main_click_and_no_instrumentation
    def test_scripts_off_main_click_and_no_instrumentation(self):
        fixture = live.TestLive()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.argv += ['--scripts', 'off']
        cap = self.fixture.capsule(['a', 'b'])
        physical = []
        clock = fixtures.Clock()
        original_run = loop.run_loop

        # @anchor:wa.tests.cu_scripts_live_unit.TestLauncherRegression.test_scripts_off_main_click_and_no_instrumentation.lifecycle
        def lifecycle(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 0,
                stdout=json.dumps([fixture.slot] if argv[1] == 'start' else {}))

        # @anchor:wa.tests.cu_scripts_live_unit.TestLauncherRegression.test_scripts_off_main_click_and_no_instrumentation.command
        def command(argv, **kwargs):
            if argv[1] == 'frame':
                return dict(cap.frame(), capture_mono_ns=100)
            physical.append(argv)
            cap.input({'kind': 'click', 'x': 2, 'y': 3})
            return {'input_mono_ns': 10}

        # @anchor:wa.tests.cu_scripts_live_unit.TestLauncherRegression.test_scripts_off_main_click_and_no_instrumentation.run
        def run(*args, **kwargs):
            self.assertNotIn('integration', kwargs)
            return original_run(*args, **kwargs, clock=clock, sleep=clock.sleep)

        factory = Mock(side_effect=AssertionError('off constructed runtime'))
        with patch.object(cu_live.subprocess, 'run', side_effect=lifecycle), \
             patch.object(loop, '_json_command', side_effect=command), \
             patch.object(loop, 'SubprocessVisionEngine', return_value=Mock(timeout=30)), \
             patch.object(loop.BridgeDescriber, 'describe', return_value={
                 'schema': 've.cu_describe/v1', 'width': 16, 'height': 16,
                 'changed': False, 'changed_regions': [], 'crops': []}), \
             patch.object(MuseBackend, 'generate', side_effect=[
                 fixtures.proposal('click', x=2, y=3), fixtures.proposal('done')]), \
             patch.object(script_live, 'instrument_muse') as instrument, \
             patch.object(loop, 'run_loop', side_effect=run), \
             contextlib.redirect_stdout(io.StringIO()) as out:
            code = cu_live.main(fixture.argv, runtime_factory=factory)
        self.assertEqual(code, 0, out.getvalue())
        self.assertEqual(len(physical), 1)
        instrument.assert_not_called()
        factory.assert_not_called()
        self.assertFalse((fixture.run_dir / 'script_metrics.jsonl').exists())
        summary = json.loads(out.getvalue())
        self.assertEqual(summary['terminal_summary']['steps'], 1)
        self.assertEqual(summary['terminal_summary']['verified_ok'], 1)
        for section in ('requested', 'effective'):
            self.assertNotIn('scripts', summary[section])
            self.assertNotIn('dry_check', summary[section])


# @anchor:wa.tests.cu_scripts_live_unit.TestMuseInstrumentation
class TestMuseInstrumentation(unittest.TestCase):
    # @anchor:wa.tests.cu_scripts_live_unit.TestMuseInstrumentation.test_live_nested_dispatch_stays_closed
    def test_live_nested_dispatch_stays_closed(self):
        """Record the unresolved live bridge prerequisite without weakening S9."""
        runtime, host, h, unused = runtime_fixtures._runtime_fixture()
        self.addCleanup(runtime.close)
        capsule = loop.SrfCapsule('synthetic', dispatcher=runtime)
        action = {'kind': 'click', 'x': 2, 'y': 3}
        with patch.object(loop, '_json_command') as physical:
            with self.assertRaisesRegex(ValueError, 'INPUT_CONSUMED|INPUT_FENCE'):
                runtime.dispatch(capsule, action, lambda: capsule.input(action))
            physical.assert_not_called()
        self.assertFalse(runtime.check_ready())

    # @anchor:wa.tests.cu_scripts_live_unit.TestMuseInstrumentation.test_shape_only_provider_refuses
    def test_shape_only_provider_refuses(self):
        with self.assertRaisesRegex(ValueError, 'PROVIDER_OWNER_UNAVAILABLE'):
            script_live.instrument_muse(object(), MuseBackend())
        runtime, host, h, unused = runtime_fixtures._runtime_fixture()
        self.addCleanup(runtime.close)
        with self.assertRaisesRegex(ValueError, 'PROVIDER_OWNER_UNAVAILABLE'):
            script_live.instrument_muse(runtime, types.SimpleNamespace(generate=lambda request: {}))

    # @anchor:wa.tests.cu_scripts_live_unit.TestMuseInstrumentation.test_effective_workspace_and_release
    def test_effective_workspace_and_release(self):
        runtime, host, h, unused = runtime_fixtures._runtime_fixture()
        self.addCleanup(runtime.close)
        backend = MuseBackend(effort='medium')
        decider = loop.ModelDecider(backend)
        self.addCleanup(decider.close)
        with patch.object(MuseBackend, 'generate', autospec=True,
                          side_effect=lambda backend, request: backend.workspace) as provider:
            release = script_live.instrument_muse(runtime, backend)
            self.addCleanup(release)
            self.assertIs(type(backend), MuseBackend)
            decider.start_episode()
            self.assertIsNotNone(backend.workspace)
            self.assertTrue(runtime.charge_model())
            self.assertIsNone(json.loads(runtime.episode_metrics())['muse_calls'])
            self.assertEqual(backend.generate({}), backend.workspace)
            provider.assert_called_once_with(backend, {})
            self.assertEqual(json.loads(runtime.episode_metrics())['muse_calls'], 1)
            with self.assertRaisesRegex(ValueError, 'PROVIDER_ALREADY_INSTRUMENTED'):
                script_live.instrument_muse(runtime, backend)
            release()
            release()
            self.assertNotIn('generate', backend.__dict__)

    # @anchor:wa.tests.cu_scripts_live_unit.TestMuseInstrumentation.test_missing_budget_never_calls_provider
    def test_missing_budget_never_calls_provider(self):
        runtime, host, h, unused = runtime_fixtures._runtime_fixture()
        self.addCleanup(runtime.close)
        backend = MuseBackend()
        with patch.object(MuseBackend, 'generate') as provider:
            release = script_live.instrument_muse(runtime, backend)
            self.addCleanup(release)
            with self.assertRaisesRegex(ValueError, 'MODEL_BUDGET'):
                backend.generate({})
            provider.assert_not_called()
            self.assertIsNone(json.loads(runtime.episode_metrics())['muse_calls'])


# @anchor:wa.tests.cu_scripts_live_unit.TestLegacyMutants
class TestLegacyMutants(unittest.TestCase):
    # @anchor:wa.tests.cu_scripts_live_unit.TestLegacyMutants.test_provider_instrumentation_mutant
    def test_provider_instrumentation_mutant(self):
        method = 'test_effective_workspace_and_release'
        control = unittest.TestResult()
        TestMuseInstrumentation(method).run(control)
        self.assertTrue(control.wasSuccessful(), control.errors or control.failures)
        source = Path(script_live.__file__).read_text()
        target = 'return runtime.invoke_provider(provider, *args, **kwargs)'
        self.assertEqual(source.count(target), 1)
        namespace = {}
        exec(compile(source.replace(target, 'return provider(*args, **kwargs)'),
                     '<provider-counter-bypass>', 'exec'), namespace)
        result = unittest.TestResult()
        with patch.object(script_live, 'instrument_muse', namespace['instrument_muse']):
            TestMuseInstrumentation(method).run(result)
        self.assertEqual(result.errors, [], result.errors)
        self.assertTrue(result.failures)

    # @anchor:wa.tests.cu_scripts_live_unit.TestLegacyMutants.test_check_removal_mutants
    def test_check_removal_mutants(self):
        source = Path(script_live.__file__).read_text()
        cases = (
            ('self._surface is not surface', 'test_foreign_surface_refuses'),
            ('self._active', 'test_recursive_callback_refuses'),
            ('self._closed', 'test_explicit_uncertainty_and_close'),
        )
        for predicate, method in cases:
            with self.subTest(predicate=predicate):
                control = unittest.TestResult()
                TestLegacyDispatch(method).run(control)
                self.assertTrue(control.wasSuccessful(), control.errors or control.failures)
                tree = ast.parse(source)
                dispatch = next(n for n in ast.walk(tree)
                    if isinstance(n, ast.FunctionDef) and n.name == 'dispatch')
                check = next(n for n in ast.walk(dispatch) if isinstance(n, ast.If))
                self.assertIsInstance(check.test, ast.BoolOp)
                removed = [v for v in check.test.values if ast.unparse(v) == predicate]
                self.assertEqual(len(removed), 1)
                check.test.values = [v for v in check.test.values if ast.unparse(v) != predicate]
                namespace = {}
                exec(compile(ast.fix_missing_locations(tree), '<legacy-check-removal>', 'exec'), namespace)
                mutant = unittest.TestResult()
                with patch.object(script_live, 'LegacyDispatchOwner', namespace['LegacyDispatchOwner']):
                    TestLegacyDispatch(method).run(mutant)
                self.assertFalse(mutant.wasSuccessful())
                self.assertEqual(mutant.errors, [], mutant.errors)
                self.assertTrue(mutant.failures)

    # @anchor:wa.tests.cu_scripts_live_unit.TestLegacyMutants.test_launcher_check_removal_mutants
    def test_launcher_check_removal_mutants(self):
        cases = ((cu_live, 'capsule.dispatcher',
                  'test_off_click_episode_and_equivalence'),
                 (cu_web, 'surface.integration', 'test_web_off_installs_legacy_owner'))
        for module, target, method in cases:
            with self.subTest(module=module.__name__):
                control = unittest.TestResult()
                TestLauncherRegression(method).run(control)
                self.assertTrue(control.wasSuccessful(), control.errors or control.failures)
                tree = ast.parse(Path(module.__file__).read_text())
                changed = []
                for node in ast.walk(tree):
                    if (isinstance(node, ast.Assign) and any(ast.unparse(t) == target for t in node.targets)
                            and ast.unparse(node.value) != 'runtime'):
                        node.value = ast.Constant(None)
                        changed.append(node.lineno)
                self.assertEqual(len(changed), 1)
                mutant = types.ModuleType('_s11b_launcher_mutant')
                mutant.__file__ = module.__file__
                exec(compile(ast.fix_missing_locations(tree), '<launcher-registration-removal>', 'exec'),
                     mutant.__dict__)
                result = unittest.TestResult()
                with patch.object(module, 'loop_main' if module is cu_live else 'main',
                                  mutant.loop_main if module is cu_live else mutant.main):
                    TestLauncherRegression(method).run(result)
                self.assertEqual(result.errors, [], result.errors)
                self.assertTrue(result.failures)


if __name__ == '__main__':
    unittest.main()


from dataclasses import asdict as _asdict, replace as _replace
import hashlib as _hashlib
import tempfile
from wender.learn import script_runtime as _runtime
from wender.learn import script_live as _live
from wender.learn import script_integration as _si
from wender.scripts import types as _st


# @anchor:wa.tests.cu_scripts_live_unit._policy
def _policy(authority):
    values = {
        'SourcePolicy': dict(unicode_scalars=16384, utf8_bytes=65536, header_lines=64),
        'ParserPolicy': dict(ast_nodes=1024, ast_depth=8, predicate_nodes=32,
            timeout_ms=1000, incremental_memory_bytes=134217728, supervisor_reference='test-supervisor'),
        'SlotPolicy': dict(slots=32, input_slot_bytes=4096, harvest_result_bytes=16384,
            episode_input_bytes=8192, episode_result_bytes=32768),
        'StorePolicy': dict(artifact_bytes=16777216, resident_artifacts=64,
            catalogue_bytes=4194304, pinned_generations=4, static_meta_tokens=800),
        'ReplayPolicy': dict(support_bytes=33554432, screening_jobs=1, event_bytes=8388608, collector_spools=16),
        'ResolverPolicy': dict(distinct_terms=16, candidates=64, idle_grace_ns=30000000000),
        'WaitPolicy': dict(effect_verification_ms=2000, semantic_wait_ms=30000,
            acquisition_attempts=64, absolute_deadline_ns=1000, quiet_window_ms=50,
            settle_deadline_ms=2000, acquisition_spacing_ms=20),
        'ContextPolicy': dict(z1_tokens=1024, z2_tokens=2000, z3_tokens=1200),
        'DisplayPolicy': dict(scanned_cards=64, selected_cards=8, prompt_rows=30,
            recency_candidates=15, recent_receipts=3, step_tokens=120, step_bytes=4096,
            failure_tokens=40, failure_bytes=512, resolver_tokens=300)}
    limits = {key: 1 for key in _live._LIMITS}
    limits.update(max_registrations=64, max_record_bytes=8192)
    return _live.EvaluationPolicy.from_mapping(version='test-policy', policies=values,
        limits=limits, authority=authority, stages={key: 1 for key in _live._STAGES},
        absolute_deadline_ns=1000, tokenizer=lambda text: len(text.split()), utility={'gain': 1})


# @anchor:wa.tests.cu_scripts_live_unit._live_fixture
def _live_fixture(case, store_host=None, generation=None):
    from tests.test_cu_scripts_runtime_unit import _runtime_fixture
    from wender.learn.script_runtime import build_offline_runtime
    from wender.learn.model_client import MuseBackend
    old, host, h, _ = _runtime_fixture(store_host=store_host)
    folder = tempfile.TemporaryDirectory(dir='/dev/shm')
    case.addCleanup(folder.cleanup)
    paths = tuple(Path(folder.name) / name for name in ('manifest', 'app', 'transport', 'tasks'))
    paths[0].mkdir()
    (paths[0] / '.included').write_bytes(b'manifest')
    for path in paths[1:]:
        path.write_bytes(path.name.encode())
    scope = h.scope
    mapping = (scope.principal, scope.tenant, scope.browser_profile_incarnation,
        scope.top_level_origin, scope.target_frame_origin, scope.frame_ancestry, scope.scope_revision)
    manifest_limits = _live.ManifestLimits(entries=16, depth=4, bytes=1024)
    pin = _live.CapsulePin(_live.capsule_manifest_digest(paths[0], limits=manifest_limits),
        *(_hashlib.sha256(p.read_bytes()).hexdigest() for p in paths[1:]),
        'test-container', 'sha256', 'capsule-directory-v1', mapping, 'test-task')
    identity = _live.AttestationOwner(pin=pin, paths=paths, container='test-container',
        task='test-task', issuer='test-issuer', incarnation='test-incarnation', authority=h.authority,
        manifest_limits=manifest_limits)
    attestation = identity.attest()
    scope = identity.scope(attestation)
    session = _replace(old._session, scope=scope, restart=_si.RestartState(True, scope, (), object(), None))
    host.register_session(session)
    host.current_authority = lambda s: h.authority
    offline = build_offline_runtime(mode='offline')
    case.addCleanup(offline.close)
    inventory = dict(offline.services.integration.construction_inventory())
    inventory = {name: inventory[name] for name in ('admission', 'compiler', 'ledger', 'act_api', 'wait')}
    inventory['store'] = h.store
    host.check_construction_owner = lambda k, v: inventory.get(k) is v
    services = _replace(old.services, observation=object(), acquisition=object(),
        context=object(), generation=object() if generation is None else generation, accounting=object(), telemetry=object())
    policy = _policy(h.authority)
    run = _live.EvaluationRunOwner(policy=policy, identity=identity, attestation=attestation,
        services=services, session=session, inventory=inventory, supervisor=object(),
        admission_arguments={}, generation_policy=object(), shutdown=object(),
        support_ports=(types.SimpleNamespace(release=lambda *a: True),
            types.SimpleNamespace(verify=lambda obs: True), types.SimpleNamespace(seal=None, abis=None)))
    capsule = types.SimpleNamespace(container=pin.container, binary=str(paths[2]))
    backend = MuseBackend(workspace=folder.name)
    runtime = _live.build_live_runtime(mode='live', capsule=capsule, describer=object(),
        backend=backend, identity=identity, policy=policy, goal_check=lambda f: True,
        clock=lambda: 1, owners=run)
    case.addCleanup(runtime.close)
    return runtime, capsule, identity, run, host, backend


# @anchor:wa.tests.cu_scripts_live_unit.TestLiveComposition
class TestLiveComposition(unittest.TestCase):
    # @anchor:wa.tests.cu_scripts_live_unit.TestLiveComposition.test_live_ready_attested_capsule
    def test_live_ready_attested_capsule(self):
        runtime, capsule, identity, run, host, backend = _live_fixture(self)
        self.assertIs(type(runtime), _runtime.ScriptRuntime)
        self.assertTrue(runtime.check_ready())
        forged = _replace(run.attestation)
        self.assertFalse(identity.check(forged))
        run.attestation = forged
        with self.assertRaisesRegex(ValueError, 'FOREIGN'):
            _live.build_live_runtime(mode='live', capsule=capsule, describer=object(),
                backend=backend, identity=identity, policy=run.policy, goal_check=lambda f: True,
                clock=lambda: 1, owners=run)


    # @anchor:wa.tests.cu_scripts_live_unit.TestLiveComposition.test_foreign_capsule
    def test_foreign_capsule(self):
        runtime, capsule, identity, run, host, backend = _live_fixture(self)
        self.assertTrue(runtime.check_ready())
        (identity.paths[0] / '.included').write_bytes(b'foreign-manifest')
        with self.assertRaisesRegex(ValueError, 'FOREIGN'):
            identity.attest()
        sends = []
        with self.assertRaisesRegex(ValueError, 'INPUT_FENCE'):
            runtime.dispatch(capsule, {}, lambda: sends.append(True))
        self.assertFalse(runtime.check_ready())
        self.assertEqual(sends, [])

    # @anchor:wa.tests.cu_scripts_live_unit.TestLiveComposition.test_authority_invalidation
    def test_authority_invalidation(self):
        for kind in ('tasks', 'container', 'authority', 'surface'):
            with self.subTest(kind=kind):
                runtime, capsule, identity, run, host, backend = _live_fixture(self)
                self.assertTrue(runtime.check_ready())
                action, sends = {}, []
                runtime.possible_input(action)
                if kind == 'tasks':
                    identity.paths[3].write_bytes(b'changed-task')
                elif kind == 'container':
                    identity.container = 'foreign-container'
                elif kind == 'surface':
                    capsule.container = 'foreign-surface'
                else:
                    identity.authority = _replace(identity.authority, policy=identity.authority.policy + 1)
                with self.assertRaisesRegex(ValueError, 'INPUT_FENCE'):
                    runtime.dispatch(capsule, action, lambda: sends.append(True))
                self.assertEqual(sends, [])

    # @anchor:wa.tests.cu_scripts_live_unit.TestLiveComposition.test_dispatch_single_crossing
    def test_dispatch_single_crossing(self):
        runtime, capsule, identity, run, host, backend = _live_fixture(self)
        action, calls = {}, []
        with self.assertRaisesRegex(ValueError, 'INPUT_FENCE'):
            capsule.dispatcher.dispatch(capsule, action, lambda: calls.append('direct'))
        runtime.dispatch(capsule, action, lambda: capsule.dispatcher.dispatch(
            capsule, action, lambda: calls.append('physical')))
        self.assertEqual(calls, ['physical'])
        with self.assertRaisesRegex(ValueError, 'INPUT_FENCE|INPUT_CONSUMED'):
            runtime.dispatch(capsule, action, lambda: calls.append('replay'))
        other = {}
        # @anchor:wa.tests.cu_scripts_live_unit.TestLiveComposition.test_dispatch_single_crossing.recursive
        def recursive():
            capsule.dispatcher.dispatch(capsule, other, lambda: calls.append('recursive'))
        with self.assertRaisesRegex(ValueError, 'INPUT_FENCE'):
            runtime.dispatch(capsule, other, lambda: capsule.dispatcher.dispatch(capsule, other, recursive))
        self.assertEqual(calls, ['physical'])

    # @anchor:wa.tests.cu_scripts_live_unit.TestLiveComposition.test_policy_missing_refuses
    def test_policy_missing_refuses(self):
        runtime, capsule, identity, run, host, backend = _live_fixture(self)
        policy = run.policy
        for name, value in policy.limits:
            with self.subTest(limit=name), self.assertRaisesRegex(ValueError, 'POLICY_MISSING'):
                _replace(policy, limits=tuple(pair for pair in policy.limits if pair[0] != name))
        for name, value in policy.policies:
            for field in _asdict(value):
                values = {k: _asdict(v) for k, v in policy.policies}
                del values[name][field]
                with self.subTest(policy=name, field=field), self.assertRaisesRegex(ValueError, 'POLICY_MISSING'):
                    _live.EvaluationPolicy.from_mapping(version=policy.version, policies=values,
                        limits=dict(policy.limits), authority=policy.authority, stages=dict(policy.stages),
                        absolute_deadline_ns=1000, tokenizer=policy.tokenizer, utility={'gain': 1})
        for delta in ({'tokenizer': None}, {'version': ''}, {'authority': None},
                      {'stages': ()}, {'utility': ()}, {'absolute_deadline_ns': 0}):
            with self.subTest(field=tuple(delta)), self.assertRaisesRegex(ValueError, 'POLICY_MISSING'):
                _replace(policy, **delta)

    # @anchor:wa.tests.cu_scripts_live_unit.TestLiveComposition.test_generation_uses_muse_schema_transport
    def test_generation_uses_muse_schema_transport(self):
        from wender.learn.script_generator import GenerationPolicy
        host_port = types.SimpleNamespace(check=lambda *a: _st.AdmissionCheck(_st.Truth.TRUE, None))
        runtime, capsule, identity, run, host, backend = _live_fixture(self, generation=host_port)
        supervisor = types.SimpleNamespace(check=lambda *a: True,
            invoke=lambda provider, *a, policy: provider(*a))
        transport = _live.GenerationTransport(backend=backend, runtime=runtime, supervisor=supervisor)
        policy = GenerationPolicy(dict(run.policy.policies)['SourcePolicy'], 4096, 4096, 900, ('PRIVATE_CANARY',))
        schema = b'{"type":"object","required":["source"],"properties":{"source":{"type":"string"}}}'
        with patch.object(backend, '_web_json', return_value={'source': 'checkpoint()'}) as provider:
            response = transport.exchange(b'{"public":"button"}', schema=schema,
                association=object(), policy=policy, host=host_port)
            self.assertEqual(json.loads(response), {'source': 'checkpoint()'})
            self.assertIn('source', provider.call_args.args[1]['properties'])
            self.assertEqual(runtime._counter.attempts(), 1)
            with self.assertRaisesRegex(ValueError, 'GENERATION_SCHEMA'):
                transport.exchange(b'{}', schema=b'{"required":["action"]}',
                    association=object(), policy=policy, host=host_port)
            self.assertEqual(provider.call_count, 1)

    # @anchor:wa.tests.cu_scripts_live_unit.TestLiveComposition.test_private_canary_no_export
    def test_private_canary_no_export(self):
        from wender.learn.script_generator import GenerationPolicy
        host_port = types.SimpleNamespace(check=lambda *a: _st.AdmissionCheck(_st.Truth.TRUE, None))
        runtime, capsule, identity, run, host, backend = _live_fixture(self, generation=host_port)
        supervisor = types.SimpleNamespace(check=lambda *a: True,
            invoke=lambda provider, *a, policy: provider(*a))
        transport = _live.GenerationTransport(backend=backend, runtime=runtime, supervisor=supervisor)
        policy = GenerationPolicy(dict(run.policy.policies)['SourcePolicy'], 4096, 4096, 900, ('PRIVATE_CANARY',))
        schema = b'{"required":["source"],"properties":{"source":{"type":"string"}}}'
        with patch.object(backend, '_web_json', return_value={'source': 'public_button()'}) as provider:
            transport.exchange(b'public button', schema=schema, association=object(), policy=policy, host=host_port)
            with self.assertRaisesRegex(ValueError, 'PRIVACY'):
                transport.exchange(b'PRIVATE_CANARY', schema=schema, association=object(), policy=policy, host=host_port)
            self.assertEqual(provider.call_count, 1)
            provider.return_value = {'source': '# META PRIVATE_CANARY'}
            with self.assertRaisesRegex(ValueError, 'PRIVACY'):
                transport.exchange(b'public button', schema=schema, association=object(), policy=policy, host=host_port)
        self.assertNotIn('PRIVATE_CANARY', repr(run.attestation))

    # @anchor:wa.tests.cu_scripts_live_unit.TestLiveComposition.test_scripts_off_byte_identical
    def test_scripts_off_byte_identical(self):
        case = TestLauncherRegression('test_off_click_episode_and_equivalence')
        result = unittest.TestResult()
        case.run(result)
        self.assertTrue(result.wasSuccessful(), result.errors + result.failures)


# @anchor:wa.tests.cu_scripts_live_unit._reconciliation
def _reconciliation(runtime, host):
    acquisition = object()
    observation_owner = types.SimpleNamespace(acquire=lambda *a: acquisition,
        producer=types.SimpleNamespace(check=lambda a: a is acquisition))
    comparator = types.SimpleNamespace(result='applied', check_fresh=lambda *a: True)
    comparator.compare = lambda *a: comparator.result
    comparator.check = lambda s, a, obs, result: obs is acquisition and result == comparator.result and result in ('applied', 'not_applied')
    drain, grant = object(), object()
    custody = types.SimpleNamespace(drain_interrupted=lambda s: drain,
        check_drain=lambda s, d: d is drain)
    grants = types.SimpleNamespace(issue=lambda *a: grant, check=lambda s, a, d, g: g is grant and d is drain)
    owner = _live.ReconciliationOwner(observations=observation_owner, comparator=comparator,
        custody=custody, grants=grants)
    host.reconciliation = owner
    return owner, comparator


# @anchor:wa.tests.cu_scripts_live_unit.TestLiveEvidence
class TestLiveEvidence(unittest.TestCase):
    # @anchor:wa.tests.cu_scripts_live_unit.TestLiveEvidence.test_reconciliation_never_replays
    def test_reconciliation_never_replays(self):
        runtime, capsule, identity, run, host, backend = _live_fixture(self)
        owner, comparator = _reconciliation(runtime, host)
        action, calls = {}, []
        # @anchor:wa.tests.cu_scripts_live_unit.TestLiveEvidence.test_reconciliation_never_replays.lost
        def lost():
            calls.append('applied')
            raise TimeoutError('completion lost')
        with self.assertRaises(TimeoutError):
            runtime.dispatch(capsule, action, lambda: capsule.dispatcher.dispatch(capsule, action, lost))
        self.assertFalse(runtime.check_ready())
        evidence = owner.collect(run.session, (action,), object(), object())
        runtime.reconcile_uncertain(evidence)
        self.assertTrue(runtime.check_ready())
        self.assertEqual(calls, ['applied'])
        with self.assertRaisesRegex(ValueError, 'INPUT_FENCE|INPUT_CONSUMED'):
            runtime.dispatch(capsule, action, lambda: calls.append('replayed'))
        self.assertEqual(calls, ['applied'])
        with self.assertRaisesRegex(ValueError, 'RECONCILIATION'):
            runtime.reconcile_uncertain(evidence)
        self.assertFalse(runtime.check_ready())

    # @anchor:wa.tests.cu_scripts_live_unit.TestLiveEvidence.test_ambiguous_reconciliation_closed
    def test_ambiguous_reconciliation_closed(self):
        runtime, capsule, identity, run, host, backend = _live_fixture(self)
        owner, comparator = _reconciliation(runtime, host)
        action = {}
        runtime.uncertain(action)
        comparator.check_fresh = lambda *a: False
        with self.assertRaisesRegex(ValueError, 'RECONCILIATION'):
            owner.collect(run.session, (action,), object(), object())
        comparator.check_fresh = lambda *a: True
        comparator.result = 'unknown'
        with self.assertRaisesRegex(ValueError, 'RECONCILIATION'):
            owner.collect(run.session, (action,), object(), object())
        self.assertFalse(runtime.check_ready())
        comparator.result = 'not_applied'
        evidence = owner.collect(run.session, (action,), object(), object())
        with self.assertRaisesRegex(ValueError, 'RECONCILIATION'):
            runtime.reconcile_uncertain(_replace(evidence))
        runtime.reconcile_uncertain(evidence)
        self.assertTrue(runtime.check_ready())

    # @anchor:wa.tests.cu_scripts_live_unit.TestLiveEvidence.test_observation_mapping
    def test_observation_mapping(self):
        from tests.test_cu_scripts_runtime_unit import _observation_host
        runtime, capsule, identity, run, host, backend = _live_fixture(self)
        from tests.scripts.test_store_catalogue import _Host
        h = _Host()
        obs = _replace(h.f['observation'], scope=run.session.scope, authority=run.session.authority)
        old, _, arguments = _observation_host(run.session.scope, run.session.authority, obs)
        allocation = object()
        frame, description = {'lane': {}}, {}
        record = _live.LiveAcquisition(frame, description, obs,
            tuple(arguments['raw_elements']), arguments, allocation, object(), True, False)
        producer = types.SimpleNamespace(acquire=lambda *a: record,
            check=lambda value: value is record, reserve_observation=lambda *a: allocation,
            release_observation=lambda a: a is allocation)
        evidence_table = {}
        # @anchor:wa.tests.cu_scripts_live_unit.TestLiveEvidence.test_observation_mapping.register
        def register(a, bundle, scope, authority, alloc):
            ev = _si.IntegrationEvidence(scope, authority, a.observation, bundle,
                a, True, True, object(), (), alloc)
            evidence_table[id(ev)] = ev
            return ev
        producer.register_observation = register
        producer.check_evidence = lambda ev: evidence_table.get(id(ev)) is ev
        owner = _live.LiveObservationOwner(producer=producer, scope=run.session.scope,
            authority=run.session.authority, max_registrations=8)
        adapter = _runtime.ObservationHost(owner, max_registrations=8)
        owner.acquire(frame, description)
        options = dict(host=adapter, acquisition=record, scope=run.session.scope,
            authority=run.session.authority, policy=object(), resources=object())
        evidence = _si.observe(**options)
        self.assertIs(evidence.observation, obs)
        self.assertEqual(evidence.inventory.inventory, tuple(arguments['raw_elements']))
        self.assertTrue(adapter.close())
        for delta in ({'producer_gen16': 8}, {'scope_complete': False}, {'stream_truncated': True}):
            saved = dict(arguments)
            arguments.update(delta)
            owner.acquire(frame, description)
            with self.assertRaisesRegex(ValueError, 'ACQUISITION|INTEGRATION_UNAVAILABLE'):
                _si.observe(**options)
            arguments.clear()
            arguments.update(saved)
        owner.acquire(frame, description)
        with self.assertRaisesRegex(ValueError, 'ACQUISITION'):
            _si.observe(**(options | {'scope': _replace(run.session.scope, principal='foreign')}))


# @anchor:wa.tests.cu_scripts_live_unit.TestLiveLifecycle
class TestLiveLifecycle(unittest.TestCase):
    # @anchor:wa.tests.cu_scripts_live_unit.TestLiveLifecycle._fixture
    def _fixture(self):
        from tests.scripts.test_dream_lifecycle import DreamTests
        d = DreamTests()
        d.setUp()
        runtime, capsule, identity, run, host, backend = _live_fixture(self, store_host=d.h)
        return d, run

    # @anchor:wa.tests.cu_scripts_live_unit.TestLiveLifecycle._transition
    def _transition(self, d, run, target, evidence):
        return run.transition(target, policy=d.policy, evidence=evidence,
            index=d.index, tokenizer=lambda text: len(text.split()), export_receipts=d.h._receipts())

    # @anchor:wa.tests.cu_scripts_live_unit.TestLiveLifecycle.test_draft_not_executable
    def test_draft_not_executable(self):
        d, run = self._fixture()
        with self.assertRaises(_st.ContractError):
            d.h._acquire(d.cell, consumer_kind='dispatch')
        self._transition(d, run, _st.Lifecycle.SHADOW, d.ev)
        with self.assertRaises(_st.ContractError):
            d.h._acquire(d.cell, consumer_kind='dispatch')
        self._transition(d, run, _st.Lifecycle.PROMOTED, d._promotion())
        lease = d.h._acquire(d.cell, consumer_kind='dispatch')
        self.assertIsNotNone(lease)
        d.h.store.release(lease)

    # @anchor:wa.tests.cu_scripts_live_unit.TestLiveLifecycle.test_promotion_cohorts
    def test_promotion_cohorts(self):
        d, run = self._fixture()
        with self.assertRaises(_st.ContractError):
            d._index(d.eps + (d.eps[0],))
        self._transition(d, run, _st.Lifecycle.SHADOW, d.ev)
        trials = d._trials()
        reused = _replace(trials[0], episode_id=d.eps[0].episode_id, run_id=d.eps[0].run_id, seal=d.eps[0].seal)
        with self.assertRaises(_st.ContractError):
            run.record_shadow(reused, frozen_support_cohort_ids=d.cert.support_cohort_ids,
                existing_trials=(), ledger_owner=d.owners[d.eps[0].episode_id])
        with self.assertRaises(_st.ContractError):
            run.record_shadow(trials[0], frozen_support_cohort_ids=d.cert.support_cohort_ids,
                existing_trials=(trials[0],), ledger_owner=d.owners[trials[0].episode_id])
        with self.assertRaises(_st.ContractError):
            self._transition(d, run, _st.Lifecycle.PROMOTED, d._promotion(trials[:2]))
        with self.assertRaises(_st.ContractError):
            _replace(trials[0], live_input_dispatched=True)
        self.assertEqual(self._transition(d, run, _st.Lifecycle.PROMOTED,
            d._promotion(trials)).new_lifecycle, _st.Lifecycle.PROMOTED)

    # @anchor:wa.tests.cu_scripts_live_unit.TestLiveLifecycle.test_utility_and_probe_required
    def test_utility_and_probe_required(self):
        d, run = self._fixture()
        for kind in ('missing', 'regression'):
            with self.subTest(kind=kind):
                before = d.h.store.store_lifecycle_revision
                with self.assertRaises(_st.ContractError):
                    if kind == 'missing':
                        bad = _replace(d.ev, paired_utilities=())
                    else:
                        costs = tuple(_replace(u, candidate_cost=_replace(u.candidate_cost,
                            value=u.baseline_cost.value + 1)) for u in d.ev.paired_utilities)
                        bad = _replace(d.ev, paired_utilities=costs)
                    self._transition(d, run, _st.Lifecycle.SHADOW, bad)
                self.assertEqual(d.h.store.store_lifecycle_revision, before)
        tie = tuple(_replace(u, candidate_cost=u.baseline_cost) for u in d.ev.paired_utilities)
        with self.assertRaises(_st.ContractError):
            self._transition(d, run, _st.Lifecycle.SHADOW, _replace(d.ev, paired_utilities=tie))
        self._transition(d, run, _st.Lifecycle.SHADOW, d.ev)
        evidence = d._promotion()
        before = d.h.store.store_lifecycle_revision
        with self.assertRaises(_st.ContractError):
            self._transition(d, run, _st.Lifecycle.PROMOTED, _replace(evidence, probes=()))
        self.assertEqual(d.h.store.store_lifecycle_revision, before)
        self.assertEqual(self._transition(d, run, _st.Lifecycle.PROMOTED,
            evidence).new_lifecycle, _st.Lifecycle.PROMOTED)


# @anchor:wa.tests.cu_scripts_live_unit.TestLiveFinalFence
class TestLiveFinalFence(unittest.TestCase):
    # @anchor:wa.tests.cu_scripts_live_unit.TestLiveFinalFence.test_dispatch_final_authority
    def test_dispatch_final_authority(self):
        runtime, capsule, identity, run, host, backend = _live_fixture(self)
        action, sent = {}, []
        runtime.dispatch(capsule, action, lambda: capsule.dispatcher.dispatch(
            capsule, action, lambda: sent.append('positive')))
        action = {}
        # @anchor:wa.tests.cu_scripts_live_unit.TestLiveFinalFence.test_dispatch_final_authority.change_then_input
        def change_then_input():
            identity.paths[3].write_bytes(b'changed-after-outer-fence')
            return capsule.dispatcher.dispatch(capsule, action, lambda: sent.append('stale'))
        with self.assertRaisesRegex(ValueError, 'INPUT_FENCE'):
            runtime.dispatch(capsule, action, change_then_input)
        self.assertEqual(sent, ['positive'])

    # @anchor:wa.tests.cu_scripts_live_unit.TestLiveFinalFence.test_rename_suspends_before_input
    def test_rename_suspends_before_input(self):
        expected = dict.fromkeys(('selector', 'ownership', 'observation', 'publication',
            'generation', 'geometry', 'facts', 'lease', 'scope', 'authority'), 'registered')
        guard = types.SimpleNamespace(check=lambda value: True)
        runtime, capsule, identity, run, host, backend = _live_fixture(self)
        self.assertTrue(runtime.guard_drift(expected, dict(expected), owner=guard))
        sent = []
        runtime.dispatch(capsule, {}, lambda: sent.append('positive'))
        self.assertFalse(runtime.guard_drift(expected, dict(expected, selector='renamed'), owner=guard))
        with self.assertRaisesRegex(ValueError, 'INPUT_FENCE'):
            runtime.dispatch(capsule, {}, lambda: sent.append('unsafe'))
        self.assertEqual(sent, ['positive'])
        self.assertFalse(runtime.charge_model(), 'no registered safe fallback proof')


# @anchor:wa.tests.cu_scripts_live_unit.TestLiveMutants
class TestLiveMutants(unittest.TestCase):
    # @anchor:wa.tests.cu_scripts_live_unit.TestLiveMutants._run
    def _run(self, case):
        result = unittest.TestResult()
        case.run(result)
        return result

    # @anchor:wa.tests.cu_scripts_live_unit.TestLiveMutants._mutate
    def _mutate(self, method, predicate):
        import inspect
        import textwrap
        import sys
        source = textwrap.dedent(inspect.getsource(method))
        tree = ast.parse(source)
        changed = []
        # @anchor:wa.tests.cu_scripts_live_unit.TestLiveMutants._mutate.Remove
        class Remove(ast.NodeTransformer):
            # @anchor:wa.tests.cu_scripts_live_unit.TestLiveMutants._mutate.Remove.visit_Compare
            def visit_Compare(self, node):
                if predicate(node):
                    changed.append(node.lineno)
                    return ast.copy_location(ast.Constant(True), node)
                return self.generic_visit(node)
            # @anchor:wa.tests.cu_scripts_live_unit.TestLiveMutants._mutate.Remove.visit_Expr
            def visit_Expr(self, node):
                if predicate(node):
                    changed.append(node.lineno)
                    return ast.copy_location(ast.Pass(), node)
                return self.generic_visit(node)
        tree = Remove().visit(tree)
        self.assertTrue(changed)
        namespace = dict(vars(sys.modules[method.__module__]))
        exec(compile(ast.fix_missing_locations(tree), '<check-removal-mutant>', 'exec'), namespace)
        return namespace[method.__name__]

    # @anchor:wa.tests.cu_scripts_live_unit.TestLiveMutants._killed
    def _killed(self, case_type, method, target, name, mutant):
        control = self._run(case_type(method))
        self.assertTrue(control.wasSuccessful(), control.errors or control.failures)
        with patch.object(target, name, mutant):
            result = self._run(case_type(method))
        self.assertFalse(result.errors, result.errors)
        self.assertTrue(result.failures, 'surviving check-removal mutant')

    # @anchor:wa.tests.cu_scripts_live_unit.TestLiveMutants.test_attestation_mutant
    def test_attestation_mutant(self):
        self._killed(TestLiveComposition, 'test_live_ready_attested_capsule',
            _live.AttestationOwner, 'check', lambda *a: True)

    # @anchor:wa.tests.cu_scripts_live_unit.TestLiveMutants.test_reconciliation_mutant
    def test_reconciliation_mutant(self):
        mutant = self._mutate(_live.ReconciliationOwner.collect,
            lambda node: isinstance(node, ast.Expr) and ast.unparse(node).startswith('_require(not unresolved'))
        self._killed(TestLiveEvidence, 'test_ambiguous_reconciliation_closed',
            _live.ReconciliationOwner, 'collect', mutant)

    # @anchor:wa.tests.cu_scripts_live_unit.TestLiveMutants.test_no_replay_mutant
    def test_no_replay_mutant(self):
        predicate = lambda node: isinstance(node, ast.Compare) and ast.unparse(node) == 'id(action) not in self._consumed'
        possible = self._mutate(_runtime.ScriptRuntime.possible_input, predicate)
        dispatch = self._mutate(_runtime.ScriptRuntime.dispatch, predicate)
        control = self._run(TestLiveEvidence('test_reconciliation_never_replays'))
        self.assertTrue(control.wasSuccessful(), control.errors or control.failures)
        with patch.object(_runtime.ScriptRuntime, 'possible_input', possible), \
             patch.object(_runtime.ScriptRuntime, 'dispatch', dispatch):
            result = self._run(TestLiveEvidence('test_reconciliation_never_replays'))
        self.assertFalse(result.errors, result.errors)
        self.assertTrue(result.failures)

    # @anchor:wa.tests.cu_scripts_live_unit.TestLiveMutants.test_lifecycle_mutant
    def test_lifecycle_mutant(self):
        from wender.learn.script_store import ScriptStore
        mutant = self._mutate(ScriptStore.acquire,
            lambda node: isinstance(node, ast.Compare) and ast.unparse(node) == 'c.lifecycle in eligible[consumer_kind]')
        self._killed(TestLiveLifecycle, 'test_draft_not_executable', ScriptStore, 'acquire', mutant)

    # @anchor:wa.tests.cu_scripts_live_unit.TestLiveMutants.test_final_fence_mutant
    def test_final_fence_mutant(self):
        mutant = self._mutate(_live.ScopedDispatchOwner.dispatch,
            lambda node: isinstance(node, ast.Expr) and ast.unparse(node).startswith('_require('))
        self._killed(TestLiveFinalFence, 'test_dispatch_final_authority',
            _live.ScopedDispatchOwner, 'dispatch', mutant)

    # @anchor:wa.tests.cu_scripts_live_unit.TestLiveMutants.test_privacy_mutant
    def test_privacy_mutant(self):
        mutant = self._mutate(_live.GenerationTransport.exchange,
            lambda node: isinstance(node, ast.Expr) and ast.unparse(node).startswith('_require(not any('))
        self._killed(TestLiveComposition, 'test_private_canary_no_export',
            _live.GenerationTransport, 'exchange', mutant)


# @anchor:wa.tests.cu_scripts_live_unit.TestProspectiveCustody
class TestProspectiveCustody(unittest.TestCase):
    # @anchor:wa.tests.cu_scripts_live_unit.TestProspectiveCustody.test_model_support_is_prospective
    def test_model_support_is_prospective(self):
        runtime, capsule, identity, run, host, backend = _live_fixture(self)
        support = runtime.episode_owner
        observed, evidence, transition, action = object(), object(), object(), {}
        support.provenance.check_pre = lambda o, e, t: (o, e, t) == (observed, evidence, transition)
        support.provenance.check_transition = lambda pre, disp, o, e: o is observed and e is evidence
        with self.assertRaisesRegex(ValueError, 'SUPPORT_MISSING'):
            _live.complete_model_episode(runtime, summary={'goal_verified': True},
                observation=observed, episode_owner=support)
        support.before_input(action, observed, evidence, transition)
        with self.assertRaisesRegex(ValueError, 'SUPPORT_MISSING'):
            support.after_input(action, _st.InputDisposition.NOT_SENT, observed, evidence)
        support.after_input(action, _st.InputDisposition.APPLIED, observed, evidence)
        self.assertIs(support.final_observation, observed)
        for summary in ({'stop_reason': 'done'}, {'goal_verified': False}):
            with self.assertRaisesRegex(ValueError, 'SUPPORT_MISSING'):
                _live.complete_model_episode(runtime, summary=summary,
                    observation=observed, episode_owner=support)
        support.terminal.verify = lambda o: False
        with self.assertRaisesRegex(ValueError, 'SUPPORT_MISSING'):
            _live.complete_model_episode(runtime, summary={'goal_verified': True},
                observation=observed, episode_owner=support)
        support.terminal.verify = lambda o: True
        with self.assertRaisesRegex(ValueError, 'SUPPORT_MISSING'):
            _live.complete_model_episode(runtime, summary={'goal_verified': True},
                observation=observed, episode_owner=support)
        # Positive S8 control uses authenticated ledger/seal/sidecars. This lower
        # boundary control does not assert end-to-end live episode completion.
        from tests.scripts.test_context_generation import GenerationTests
        result = unittest.TestResult()
        GenerationTests('test_g_generate').run(result)
        self.assertTrue(result.wasSuccessful(), result.errors or result.failures)

    # @anchor:wa.tests.cu_scripts_live_unit.TestProspectiveCustody.test_shared_run_owner
    def test_shared_run_owner(self):
        from tests.scripts.test_dream_lifecycle import DreamTests
        d = DreamTests()
        d.setUp()
        runtime, capsule, identity, run, host, backend = _live_fixture(self, store_host=d.h)
        promoted = d.h._admit(201)
        self.assertTrue(runtime.close())
        self.assertIs(run.inventory['store'], d.h.store)
        lease = d.h._acquire(promoted, consumer_kind='dispatch')
        self.assertIsNotNone(lease)
        d.h.store.release(lease)
        remaining = {id(value): value for value in run.inventory.values()}
        attempts = []
        # @anchor:wa.tests.cu_scripts_live_unit.TestProspectiveCustody.test_shared_run_owner.drain
        def drain(inventory, supports):
            attempts.append(inventory)
            return True
        run.shutdown = types.SimpleNamespace(drain=drain, check_drained=lambda: not remaining)
        with self.assertRaisesRegex(ValueError, 'OWNER_DRAIN'):
            run.close()
        self.assertFalse(run._closed)
        # @anchor:wa.tests.cu_scripts_live_unit.TestProspectiveCustody.test_shared_run_owner.release
        def release(inventory, supports):
            self.assertEqual({id(v) for v in inventory}, set(remaining))
            remaining.clear()
            return True
        run.shutdown.drain = release
        self.assertTrue(run.close())
        self.assertEqual(remaining, {})
        self.assertTrue(run._closed)
        with self.assertRaisesRegex(ValueError, 'LIVE_CONFIGURATION|FOREIGN'):
            _live.build_live_runtime(mode='live', capsule=capsule, describer=object(),
                backend=backend, identity=identity, policy=run.policy, goal_check=lambda f: True,
                clock=lambda: 1, owners=run)


# @anchor:wa.tests.cu_scripts_live_unit.TestCapsuleManifest
class TestCapsuleManifest(unittest.TestCase):
    # @anchor:wa.tests.cu_scripts_live_unit.TestCapsuleManifest.setUp
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(dir='/dev/shm')
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.limits = _live.ManifestLimits(entries=16, depth=4, bytes=1024)
        (self.root / 'empty').mkdir()
        (self.root / '.included').write_bytes(b'bytes')

    # @anchor:wa.tests.cu_scripts_live_unit.TestCapsuleManifest.test_canonical_manifest
    def test_canonical_manifest(self):
        digest = _hashlib.sha256(b'bytes').hexdigest()
        expected = _hashlib.sha256(json.dumps(['capsule-directory-v1', [
            ['.included', 'f', 5, digest], ['empty', 'd']]],
            ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
        self.assertEqual(_live.capsule_manifest_digest(self.root, limits=self.limits), expected)
        (self.root / '.included').rename(self.root / '.renamed')
        self.assertNotEqual(_live.capsule_manifest_digest(self.root, limits=self.limits), expected)
        (self.root / '.renamed').rename(self.root / '.included')
        self.assertEqual(_live.capsule_manifest_digest(self.root, limits=self.limits), expected)
        (self.root / '.included').write_bytes(b'changed')
        self.assertNotEqual(_live.capsule_manifest_digest(self.root, limits=self.limits), expected)

    # @anchor:wa.tests.cu_scripts_live_unit.TestCapsuleManifest.test_manifest_bounds
    def test_manifest_bounds(self):
        self.assertEqual(len(_live.capsule_manifest_digest(self.root, limits=self.limits)), 64)
        for limits in (_replace(self.limits, entries=1), _replace(self.limits, bytes=4)):
            with self.subTest(limits=limits), self.assertRaisesRegex(ValueError, 'MANIFEST_LIMITS'):
                _live.capsule_manifest_digest(self.root, limits=limits)
        (self.root / 'empty' / 'nested').mkdir()
        with self.assertRaisesRegex(ValueError, 'MANIFEST_LIMITS'):
            _live.capsule_manifest_digest(self.root, limits=_replace(self.limits, depth=1))
        self.assertEqual(len(_live.capsule_manifest_digest(self.root, limits=self.limits)), 64)

    # @anchor:wa.tests.cu_scripts_live_unit.TestCapsuleManifest.test_symlinks_and_noncanonical_names
    def test_symlinks_and_noncanonical_names(self):
        for target in (self.root / '.included', self.root / 'empty', self.root.parent):
            link = self.root / 'link'
            link.symlink_to(target)
            with self.subTest(target=target), self.assertRaisesRegex(ValueError, 'MANIFEST_PATH'):
                _live.capsule_manifest_digest(self.root, limits=self.limits)
            link.unlink()
        for name in ('e\u0301', 'ambiguous\\name', 'control\nname'):
            path = self.root / name
            path.write_bytes(b'x')
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, 'MANIFEST_PATH'):
                _live.capsule_manifest_digest(self.root, limits=self.limits)
            path.unlink()
        self.assertEqual(len(_live.capsule_manifest_digest(self.root, limits=self.limits)), 64)

    # @anchor:wa.tests.cu_scripts_live_unit.TestCapsuleManifest.test_manifest_limits_mutant
    def test_manifest_limits_mutant(self):
        mutant = TestLiveMutants()._mutate(_live.capsule_manifest_digest,
            lambda node: isinstance(node, ast.Expr) and ast.unparse(node).startswith(
                '_require(total <= limits.bytes,'))
        TestLiveMutants()._killed(TestCapsuleManifest, 'test_manifest_bounds',
            _live, 'capsule_manifest_digest', mutant)

    # @anchor:wa.tests.cu_scripts_live_unit.TestCapsuleManifest.test_manifest_attestation_mutant
    def test_manifest_attestation_mutant(self):
        mutant = TestLiveMutants()._mutate(_live.AttestationOwner.attest,
            lambda node: isinstance(node, ast.Expr) and ast.unparse(node).startswith(
                '_require(actual == self.pin,'))
        TestLiveMutants()._killed(TestLiveComposition, 'test_foreign_capsule',
            _live.AttestationOwner, 'attest', mutant)


# @anchor:wa.tests.cu_scripts_live_unit.TestPublicLauncherContract
class TestPublicLauncherContract(unittest.TestCase):
    # @anchor:wa.tests.cu_scripts_live_unit.TestPublicLauncherContract.test_signatures
    def test_signatures(self):
        import inspect
        self.assertEqual(str(inspect.signature(cu_live.launch_scripts)),
            '(args, container, goal, trace_path, *, runtime_factory, goal_check=None, goal_distance=None, metrics_sink=None)')
        for main in (cu_live.main, cu_web.main):
            with self.subTest(main=main):
                self.assertEqual(str(inspect.signature(main)), '(argv=None, *, runtime_factory=None)')
                with self.assertRaises(TypeError):
                    main([], live_context={})

    # @anchor:wa.tests.cu_scripts_live_unit.TestPublicLauncherContract.test_missing_factory_closed
    def test_missing_factory_closed(self):
        with patch.object(loop, 'SrfCapsule', side_effect=AssertionError('physical construction')):
            with self.assertRaisesRegex(ValueError, 'LIVE_SERVICE_INSTALLATION_MISSING'):
                cu_live.launch_scripts(types.SimpleNamespace(max_ms=1000), 'not-started',
                                       'goal', Path('unused'), runtime_factory=None)
        result = unittest.TestResult()
        TestLauncherRegression('test_metrics_written_on_failure').run(result)
        self.assertTrue(result.wasSuccessful(), result.errors or result.failures)

    # @anchor:wa.tests.cu_scripts_live_unit.TestPublicLauncherContract.test_live_main_missing_installation
    def test_live_main_missing_installation(self):
        # The default CLI binds the unready S11A graph; authority stays in runtime.
        runtime_fixtures.TestRuntimeCLI('test_enabled_live_builds_default_graph').debug()

    # @anchor:wa.tests.cu_scripts_live_unit.TestPublicLauncherContract.test_web_main_missing_installation
    def test_web_main_missing_installation(self):
        seen = []
        # @anchor:wa.tests.cu_scripts_live_unit.TestPublicLauncherContract.test_web_main_missing_installation.cycle
        def cycle(surface, planner, executor, memory, *, integration, **kwargs):
            self.assertIs(type(integration), _runtime.ScriptRuntime)
            self.assertFalse(integration.check_ready())
            physical = Mock()
            with self.assertRaisesRegex(ValueError, 'INPUT_FENCE'):
                integration.dispatch(surface, {'kind': 'click'}, physical)
            physical.assert_not_called()
            seen.append(integration)
            return {'goal_verified': False}
        with patch.object(cu_web, 'SrfIdentitySurface'), \
             patch.object(cu_web, 'MuseBackend'), \
             patch.object(Path, 'read_text', return_value='{}'), \
             patch.object(cu_web, 'run_web_cycle', side_effect=cycle), \
             contextlib.redirect_stdout(io.StringIO()) as out:
            status = cu_web.main(['--scripts', 'on', '--task', 'synthetic',
                                  '--executor', 'muse'])
        self.assertEqual(status, 2)
        self.assertEqual(len(seen), 1)
        self.assertTrue(json.loads(out.getvalue().splitlines()[-1])['cleanup_complete'])

    # @anchor:wa.tests.cu_scripts_live_unit.TestPublicLauncherContract.test_unready_runtime_blocks_script_inputs
    def test_unready_runtime_blocks_script_inputs(self):
        owner, host, unused, owned = runtime_fixtures._runtime_fixture()
        self.addCleanup(owner.close)
        physical = Mock(return_value='sent')
        host.ready = False
        self.assertFalse(owner.check_ready())
        with self.assertRaisesRegex(ValueError, 'RESTART_UNCERTAIN'):
            owner.select_script({}, {})
        self.assertFalse(owner.check_script_miss())
        self.assertFalse(owner.charge_model())
        with self.assertRaisesRegex(ValueError, 'INPUT_FENCE'):
            owner.dispatch(None, {'kind': 'click'}, physical)
        physical.assert_not_called()
        host.ready = True
        self.assertTrue(owner.check_ready())
        self.assertEqual(owner.dispatch(None, {'kind': 'click'}, physical), 'sent')
        physical.assert_called_once()


# @anchor:wa.tests.cu_scripts_live_unit.TestNarrowedScopeMutants
class TestNarrowedScopeMutants(unittest.TestCase):
    # @anchor:wa.tests.cu_scripts_live_unit.TestNarrowedScopeMutants.test_installation_guard_mutants
    def test_installation_guard_mutants(self):
        for module, method in ((cu_live, 'test_live_main_missing_installation'),
                               (cu_web, 'test_web_main_missing_installation')):
            with self.subTest(module=module.__name__):
                tree = ast.parse(Path(module.__file__).read_text())
                guards = [n for n in ast.walk(tree) if isinstance(n, ast.If)
                          and ast.unparse(n.test) == "args.scripts == 'on' and runtime_factory is None"]
                self.assertEqual(len(guards), 1)
                guards[0].body = [ast.Pass()]
                mutant = types.ModuleType('_s11b_installation_mutant')
                mutant.__file__ = module.__file__
                exec(compile(ast.fix_missing_locations(tree), '<default-factory-binding-removal>', 'exec'),
                     mutant.__dict__)
                TestLiveMutants()._killed(TestPublicLauncherContract, method,
                                         module, 'main', mutant.main)

    # @anchor:wa.tests.cu_scripts_live_unit.TestNarrowedScopeMutants.test_runtime_readiness_mutant
    def test_runtime_readiness_mutant(self):
        TestLiveMutants()._killed(TestPublicLauncherContract,
            'test_unready_runtime_blocks_script_inputs',
            _runtime.ScriptRuntime, 'check_ready', lambda self: True)

    # @anchor:wa.tests.cu_scripts_live_unit.TestNarrowedScopeMutants.test_policy_completeness_mutant
    def test_policy_completeness_mutant(self):
        mutant = TestLiveMutants()._mutate(_live.EvaluationPolicy.__post_init__,
            lambda node: isinstance(node, ast.Expr) and ast.unparse(node).startswith(
                '_require(type(self.version)'))
        TestLiveMutants()._killed(TestLiveComposition, 'test_policy_missing_refuses',
                                 _live.EvaluationPolicy, '__post_init__', mutant)

    # @anchor:wa.tests.cu_scripts_live_unit.TestNarrowedScopeMutants.test_run_drain_mutant
    def test_run_drain_mutant(self):
        mutant = TestLiveMutants()._mutate(_live.EvaluationRunOwner.close,
            lambda node: isinstance(node, ast.Expr) and ast.unparse(node).startswith(
                '_require(self.shutdown.check_drained()'))
        TestLiveMutants()._killed(TestProspectiveCustody, 'test_shared_run_owner',
                                 _live.EvaluationRunOwner, 'close', mutant)

    # @anchor:wa.tests.cu_scripts_live_unit.TestNarrowedScopeMutants.test_missing_dispatcher_mutant
    def test_missing_dispatcher_mutant(self):
        import inspect
        import textwrap
        tree = ast.parse(textwrap.dedent(inspect.getsource(loop.SrfCapsule.input)))
        guards = [n for n in ast.walk(tree) if isinstance(n, ast.If)
                  and ast.unparse(n.test) == 'self.dispatcher is None']
        self.assertEqual(len(guards), 1)
        guards[0].body = ast.parse('return _receipt(_json_command(args, timeout=self.timeout))').body
        namespace = {}
        exec(compile(ast.fix_missing_locations(tree), '<implicit-dispatch-fallback>', 'exec'),
             vars(loop), namespace)
        TestLiveMutants()._killed(TestLegacyDispatch, 'test_missing_dispatcher_refuses',
                                 loop.SrfCapsule, 'input', namespace['input'])

    # @anchor:wa.tests.cu_scripts_live_unit.TestNarrowedScopeMutants.test_legacy_send_once_mutant
    def test_legacy_send_once_mutant(self):
        import inspect
        import textwrap
        source = textwrap.dedent(inspect.getsource(script_live.LegacyDispatchOwner.dispatch))
        self.assertEqual(source.count('return send()'), 1)
        source = source.replace('return send()', 'send(); return send()')
        namespace = {}
        exec(compile(source, '<duplicate-physical-send>', 'exec'), vars(script_live), namespace)
        TestLiveMutants()._killed(TestLegacyDispatch, 'test_dispatch_single_crossing',
                                 script_live.LegacyDispatchOwner, 'dispatch', namespace['dispatch'])

    # @anchor:wa.tests.cu_scripts_live_unit.TestNarrowedScopeMutants.test_factory_guard_mutant
    def test_factory_guard_mutant(self):
        import inspect
        import textwrap
        source = textwrap.dedent(inspect.getsource(cu_live.launch_scripts))
        target = "if not callable(runtime_factory):"
        self.assertEqual(source.count(target), 1)
        source = source.replace(target, 'if False:')
        namespace = {}
        exec(compile(source, '<missing-factory-guard>', 'exec'), vars(cu_live), namespace)
        TestLiveMutants()._killed(TestPublicLauncherContract, 'test_missing_factory_closed',
                                 cu_live, 'launch_scripts', namespace['launch_scripts'])

    # @anchor:wa.tests.cu_scripts_live_unit.TestNarrowedScopeMutants.test_uncertain_callback_mutant
    def test_uncertain_callback_mutant(self):
        import inspect
        import textwrap
        source = textwrap.dedent(inspect.getsource(script_live.LegacyDispatchOwner.dispatch))
        target = 'self._closed = True'
        self.assertEqual(source.count(target), 1)
        namespace = {}
        exec(compile(source.replace(target, 'pass'), '<uncertainty-fence-removal>', 'exec'),
             vars(script_live), namespace)
        TestLiveMutants()._killed(TestLegacyDispatch, 'test_uncertainty_never_replays',
                                 script_live.LegacyDispatchOwner, 'dispatch', namespace['dispatch'])
