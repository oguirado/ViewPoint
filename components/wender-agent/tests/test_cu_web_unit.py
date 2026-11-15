# @module {"id":"wa.tests.cu_web_unit","layer":2,"owner":"LEARN","deps":["wa.learn.cu_loop","wa.learn.cu_tasks","wa.learn.model_client","wa.learn.memory","wa.perceive.disambig","wa.perceive.srf_identity","wa.tools.cu_web"],"book":"HB-WA-s4-learn"}
"""Offline web adapter/cascade contracts. Every external call is fake."""
import hashlib
import copy
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import ANY, patch

from wender.perceive.srf_identity import (SrfIdentitySurface, ax_options,
                                         last_answer_container, CLI_MAPPING, BINARY, SurfaceError)
from wender.learn.model_client import LocalBackend, MuseBackend, BackendError, ActionValidationError, MuseOutput
from wender.learn.cu_tasks import web_predicate
from wender.learn.cu_loop import run_web_cycle
from tools.cu_web import main, record_workflow, MEMORY


def element(name='Ask', role='button', region='main', container='control', ordinal=1, **kw):
    kw.setdefault('visible', True)
    kw.setdefault('actionable', True)
    return dict(id=hashlib.sha256((name + role + container).encode()).hexdigest()[:12], role=role, name=name, region=region,
                container={'id': container, 'ordinal': ordinal}, box=[10, 20, 40, 20],
                states={'enabled': True}, **kw)


FRAME = dict(path='/dev/shm/fake.png', seq=12, sha256_rgb24='a' * 64,
             width=1280, height=900, capture_mono_ns=100, generation=1,
             window=dict(id='w1', x=0, y=0, w=1280, h=900, title='Ask',
                         url='https://www.perplexity.ai/'))

def completed(container='answer'):
    return [element('Investigado 2s', container=container), element('Copiar', container=container)]


class _TrustedWebIntegration:
    """Explicit synthetic owner; never installed by a production constructor."""
    def __init__(self):
        self.closed = False

    # @anchor:wa.tests.cu_web_unit._TrustedWebIntegration.check_ready
    def check_ready(self):
        return not self.closed

    # @anchor:wa.tests.cu_web_unit._TrustedWebIntegration.select_script
    def select_script(self, before, description):
        return None

    # @anchor:wa.tests.cu_web_unit._TrustedWebIntegration.check_script_miss
    def check_script_miss(self):
        return not self.closed

    # @anchor:wa.tests.cu_web_unit._TrustedWebIntegration.charge_model
    def charge_model(self):
        return not self.closed

    # @anchor:wa.tests.cu_web_unit._TrustedWebIntegration.dispatch
    def dispatch(self, surface, action, send):
        if self.closed:
            raise BackendError('RESTART_UNCERTAIN')
        return send()

    # @anchor:wa.tests.cu_web_unit._TrustedWebIntegration.uncertain
    def uncertain(self, action):
        self.closed = True


# @anchor:wa.tests.cu_web_unit.FakeSurface
# @anchor:wa.tests.cu_web_unit.FakeSurface
class FakeSurface:
    # @anchor:wa.tests.cu_web_unit.FakeSurface.__init__
    def __init__(self, *, stuck=False, unknown=False, fail_start=False):
        self.integration = _TrustedWebIntegration()
        self.phase = 'new'
        self.focused = False
        self.address = False
        self.typed = ''
        self.submitted = ''
        self.calls = []
        self.stopped = False
        self.stuck, self.unknown, self.fail_start = stuck, unknown, fail_start
    def start(self):
        if self.fail_start:
            raise OSError('fake partial start')
    def stop(self):
        self.stopped = True
    def frame(self):
        return {}
    # @anchor:wa.tests.cu_web_unit.FakeSurface.ax
    def ax(self):
        rows = [] if self.unknown else [element('Nuevo', 'link', 'navigation'),
                element('Pregunta lo que sea…', 'textbox', container='composer', value_len=len(self.typed), focused=self.focused)]
        if self.phase == 'done':
            rows += [element(self.submitted, role='heading', container='question', actionable=False)] + completed()
        # This title must never be seen by either model or any trace.
        rows += [element('PRIVATE HISTORY TITLE', 'link', 'navigation')]
        return {'url': 'https://www.perplexity.ai/search/current' if self.phase == 'done' else 'https://www.perplexity.ai/', 'title': 'Private title', 'elements': rows, 'truncated': False}
    def input(self, action):
        self.calls.append(action)
        kind = action['kind']
        if kind == 'click':
            self.focused = True
        elif kind == 'type':
            if not self.address and not self.stuck:
                self.typed += action['text']
                self.phase = 'typed'
        elif kind == 'key':
            if action['keysym'] == 'ctrl+l':
                self.address = True
            elif action['keysym'] == 'shift+Return':
                self.typed += '\n'
            elif action['keysym'] == 'Return':
                if self.address:
                    self.address, self.typed, self.phase = False, '', 'new'
                else:
                    self.submitted = self.typed
                    self.typed, self.phase = '', 'done'
        return {'ok': True}
    # @anchor:wa.tests.cu_web_unit.FakeSurface.text
    def text(self, container):
        if container == element(self.submitted, role='heading', container='question')['id']:
            return self.submitted
        assert container == 'answer'
        return 'Answer with arxiv+2'


class FakePlanner:
    def __init__(self):
        self.plans, self.choices = 0, []
    def plan(self, task, memory):
        self.plans += 1
        return copy.deepcopy(memory['workflow']['steps'])
    def choose(self, intent, options, history):
        self.choices.append((intent, options))
        return options[0]['id'] if options else 'abstain'


class FakeExecutor(FakePlanner):
    def __init__(self, abstain=False):
        super().__init__()
        self.abstain = abstain
    def choose(self, intent, options, history):
        super().choose(intent, options, history)
        return 'abstain' if self.abstain else options[0]['id']


class Clock:
    def __init__(self): self.now = 0
    def __call__(self): return self.now
    def sleep(self, seconds): self.now += seconds


class WebUnit(unittest.TestCase):
    def setUp(self):
        self.memory = json.loads(MEMORY.read_text())
    # @anchor:wa.tests.cu_web_unit.WebUnit.cycle
    def cycle(self, surface=None, executor=None, **kwargs):
        surface, executor = surface or FakeSurface(), executor or FakeExecutor()
        planner, trace, clock = FakePlanner(), io.StringIO(), Clock()
        result = run_web_cycle(surface, planner, executor, self.memory, task='Question',
                               trace=trace, clock=clock, sleep=clock.sleep,
                               postcheck_seconds=.1, wait_seconds=40, **kwargs)
        self.assertTrue(surface.stopped)
        self.assertNotIn('PRIVATE HISTORY', trace.getvalue())
        self.assertNotIn('PRIVATE HISTORY', json.dumps(executor.choices + planner.choices))
        return result, planner, executor, trace.getvalue()

    def test_options_privacy_blocking_order_bounds(self):
        rows = [element('PRIVATE HISTORY TITLE', 'link', 'navigation'),
                element('Nuevo', 'link', 'navigation'), element('Mejorar plan'),
                element('Settings'), element('Account'), element('Logout'),
                element('Hidden', visible=False), element('Disabled', states_override=False)]
        rows[-1]['states']['enabled'] = False
        rows += [element('x' * 180) for _ in range(80)]
        opts, targets = ax_options({'elements': rows})
        self.assertEqual(len(opts), 60)
        self.assertEqual(opts[0]['region'], 'main')
        self.assertLessEqual(max(len(o['name']) for o in opts), 120)
        self.assertNotIn('PRIVATE HISTORY', json.dumps(opts))
        self.assertFalse(any(o['name'] in ('Mejorar plan', 'Settings', 'Account', 'Logout', 'Hidden', 'Disabled') for o in opts))
        self.assertNotIn('navigation', targets)
        self.assertEqual(opts[-1]['name'], 'Navigation/history')

    def test_blocked_controls_match_whole_words_only(self):
        benign = ('explanation', 'planet', 'airplane', 'accountability',
                  'upgraded', 'mejorarlo', 'cuentagotas', 'reajustes',
                  'configuraciones', 'logoutside', 'log outside', 'cerrar sesiones')
        for name in benign:
            with self.subTest(benign=name):
                options, targets = ax_options({'elements': [element(name)]})
                self.assertEqual([option['name'] for option in options], [name])
                self.assertEqual(len(targets), 1)
        blocked = ('Upgrade', 'Mejorar', 'Plan', 'Account', 'Cuenta', 'Settings',
                   'Ajustes', 'Configuración', 'Configuracion', 'Logout',
                   'Log out', 'Cerrar sesión', 'Cerrar sesion')
        for label in blocked:
            for name in (label, f'Open {label} now', f'({label.upper()})'):
                with self.subTest(blocked=name):
                    self.assertEqual(ax_options({'elements': [element(name)]}), ([], {}))

    def test_last_answer_requires_same_container_and_order(self):
        rows = completed('old') + [element('Investigado 1s', container='new')]
        self.assertIsNone(last_answer_container({'elements': rows}))
        rows.append(element('Copiar', container='new'))
        self.assertEqual(last_answer_container({'elements': rows}), 'new')
        self.assertFalse(web_predicate({'kind': 'answer_complete'}, {'elements': rows}, before={'elements': rows}))
        self.assertFalse(web_predicate({'kind': 'answer_complete'}, {'elements': rows + [element('Detener la respuesta (Esc)')]}))

    def test_adapter_attach_detach_and_ram(self):
        calls = []
        surface = SrfIdentitySurface('test', transport=lambda op, p: calls.append(op))
        with surface:
            self.assertTrue(surface.started)
        self.assertEqual(calls, [])
        self.assertFalse(surface.started)
        with self.assertRaises(ValueError): SrfIdentitySurface('test', ram_dir='/tmp')
        surface.transport = lambda op, p: dict(FRAME, path='/tmp/frame.png')
        with self.assertRaises(SurfaceError) as caught:
            surface.frame()
        self.assertEqual(caught.exception.code, 'malformed_capture')
        self.assertIsNone(surface.last_frame)
        surface.transport = lambda op, p: copy.deepcopy(FRAME)
        self.assertEqual(surface.frame(), FRAME)
        self.assertEqual(surface.last_frame, FRAME)

    def test_failed_start_cleanup(self):
        self.cycle(FakeSurface(fail_start=True))

    def test_input_and_text_bounds(self):
        calls = []
        def transport(op, payload):
            calls.append((op, payload))
            if op == 'frame':
                return copy.deepcopy(FRAME)
            if op == 'ax':
                return dict(window=FRAME['window'], frame_seq=FRAME['seq'], elements=[element()])
            return {'focus_verified': True, 'input_mono_ns': 101}
        surface = SrfIdentitySurface('test', transport=transport)
        surface.frame()
        surface.ax()
        calls.clear()
        surface.input({'kind': 'click', 'element': element()})
        surface.input({'kind': 'type', 'text': 'ASCII'})
        surface.input({'kind': 'key', 'keysym': 'Return'})
        self.assertEqual(calls, [('click', {'point': '30,30'}), ('type', {'text': 'ASCII'}), ('key', {'keysym': 'Return'})])
        with self.assertRaises(ValueError): surface.input({'kind': 'click', 'element': element('Upgrade')})
        self.assertEqual(len(calls), 3)
        text = 'é' * 32768
        surface.transport = lambda op, p: dict(
            window=FRAME['window'], frame_seq=FRAME['seq'], elements=[],
            text={'container_id': 'ab', 'bytes': len(text.encode('utf-8')), 'text': text})
        self.assertEqual(surface.text('ab'), text)
        text += 'é'
        with self.assertRaises(SurfaceError) as caught:
            surface.text('ab')
        self.assertEqual(caught.exception.code, 'malformed_capture')
        for container in ('', 'answer'):
            with self.subTest(container=container), self.assertRaises(ValueError):
                surface.text(container)

    def test_cli_mapping_single_boundary(self):
        with patch('wender.perceive.srf_identity.load_identity_env', return_value={}), patch(
                'wender.perceive.srf_identity.subprocess.run',
                return_value=subprocess.CompletedProcess([], 0, json.dumps(FRAME).encode())) as run:
            surface = SrfIdentitySurface('test', container='capsule', window='w1')
            surface.start()
            run.assert_not_called()
            self.assertEqual(surface.frame(), FRAME)
            surface.stop()
        run.assert_called_once_with(
            [BINARY, 'frame', '--container', 'capsule', '--window', 'w1'],
            capture_output=True, timeout=30, check=False, env={'PATH': ANY})   # only PATH is inherited
        self.assertEqual(set(CLI_MAPPING), {'frame', 'ax', 'text', 'click', 'type', 'key', 'scroll', 'drag'})

    def test_local_schema_prefix_usage_and_no_images(self):
        payloads = []
        def transport(payload):
            payloads.append(payload)
            return {'choices': [{'message': {'content': '{"id":"e0"}'}}], 'usage': {'prompt_tokens': 10, 'completion_tokens': 4}}
        backend = LocalBackend('http://localhost/inference', self.memory, transport=transport)
        opts = [{'id': 'e0', 'name': 'Ask'}]
        self.assertEqual(backend.choose('type_question', opts), 'e0')
        backend.choose('submit_question', opts)
        self.assertEqual(payloads[0]['messages'][0], payloads[1]['messages'][0])
        self.assertEqual(payloads[0]['response_format']['json_schema']['schema']['properties']['id']['enum'], ['e0', 'abstain'])
        self.assertTrue(payloads[0]['response_format']['json_schema']['strict'])
        self.assertEqual(payloads[0]['temperature'], 0)
        self.assertEqual(payloads[0]['max_tokens'], 32)
        self.assertEqual(backend.last_model['input_tokens'], 10)
        self.assertNotIn('image', json.dumps(payloads))
        self.assertGreaterEqual(backend.last_model['latency_ms'], 0)

    def test_local_rejects_invalid_and_missing_usage(self):
        def response(text): return lambda payload: {'choices': [{'message': {'content': text}}]}
        backend = LocalBackend('http://localhost/inference', {}, transport=response('{"id":"bad"}'))
        with self.assertRaises((ActionValidationError, BackendError)): backend.choose('ask', [{'id': 'e0'}])
        backend.transport = response('{"id":"abstain"}')
        self.assertEqual(backend.choose('ask', []), 'abstain')
        self.assertIsNone(backend.last_model['input_tokens'])
        with self.assertRaises(ValueError): LocalBackend('http://user:pass@localhost/path', {})

    def test_local_null_and_non_dict_usage_preserve_results_and_errors(self):
        for usage in (None, [], 'invalid', 7):
            with self.subTest(usage=usage):
                response = {'choices': [{'message': {'content': '{"id":"abstain"}'}}],
                            'usage': usage}
                backend = LocalBackend('http://localhost/inference', {},
                                       transport=lambda payload: response)
                self.assertEqual(backend.choose('ask', []), 'abstain')
                self.assertIsNone(backend.last_model['input_tokens'])
                self.assertIsNone(backend.last_model['output_tokens'])
                response['choices'] = []
                with self.assertRaises(BackendError):
                    backend.choose('ask', [])
                self.assertIsNone(backend.last_model['input_tokens'])

    def test_web_predicates_unknown_and_negative(self):
        self.assertIsNone(web_predicate({'kind': 'element_absent'}, {}))
        self.assertIsNone(web_predicate({'kind': 'value_nonempty'}, {'elements': [element()]}))
        self.assertIsNone(web_predicate({'kind': 'url', 'pattern': 'perplexity'}, {}))
        self.assertTrue(web_predicate({'kind': 'url', 'pattern': 'perplexity'}, {'url': 'https://perplexity.ai'}))
        self.assertTrue(web_predicate({'kind': 'title', 'pattern': '^Ask'}, {'title': 'Ask here'}))
        self.assertFalse(web_predicate({'kind': 'text_nonempty'}, {}, harvested='  '))
        self.assertTrue(web_predicate({'kind': 'container_count_increased'}, {'elements': completed()}, before={'elements': []}))
        self.assertIsNone(web_predicate({'kind': 'container_count_increased'}, {'elements': []}))

    def test_cycle_plan_once_harvest_and_cleanup(self):
        result, planner, executor, trace = self.cycle()
        self.assertTrue(result['goal_verified'])
        self.assertEqual(result['harvested'], 'Answer with arxiv+2')
        self.assertEqual(planner.plans, 1)
        self.assertEqual(len(executor.choices), 4)
        self.assertEqual(result['escalations'], [])

    def test_abstention_escalation(self):
        result, planner, _, trace = self.cycle(executor=FakeExecutor(abstain=True))
        self.assertTrue(result['goal_verified'])
        self.assertEqual(len(planner.choices), 4)
        self.assertTrue(all(r['reason'] == 'abstain' for r in result['escalations']))

    # @anchor:wa.tests.cu_web_unit.WebUnit.test_twice_failed_postcheck_escalation
    def test_twice_failed_postcheck_escalation(self):
        surface = FakeSurface(stuck=True)
        result, planner, _, trace = self.cycle(surface)
        self.assertFalse(result['goal_verified'])
        self.assertEqual(result['escalations'], [])
        self.assertEqual(len(planner.choices), 0)
        self.assertEqual(sum(action['kind'] == 'type' for action in surface.calls), 2)

    def test_unknown_screen_escalation(self):
        result, planner, executor, _ = self.cycle(FakeSurface(unknown=True))
        self.assertFalse(result['goal_verified'])
        self.assertEqual(result['escalations'][0]['reason'], 'unknown_screen')
        self.assertEqual(executor.choices, [])

    def test_cleanup_failure_is_failure(self):
        surface = FakeSurface()
        def fail():
            surface.stopped = True
            raise OSError('stop failed')
        surface.stop = fail
        result, _, _, _ = self.cycle(surface)
        self.assertEqual(result['stop_reason'], 'cleanup_failed')
        self.assertFalse(result['goal_verified'])

    def test_muse_plan_and_choice_contract(self):
        backend = MuseBackend('/dev/shm')
        intents = [s['intent'] for s in self.memory['workflow']['steps']]
        with patch.object(backend, '_web_json', return_value={'intents': intents}):
            plan = backend.plan('Question', self.memory)
        self.assertEqual(plan[1]['args']['text'], 'Question')
        with patch.object(backend, '_web_json', return_value={'intents': intents[:-1]}):
            with self.assertRaises(BackendError): backend.plan('Question', self.memory)
        with patch.object(backend, '_web_json', return_value={'id': 'e0'}):
            self.assertEqual(backend.choose('ask', [{'id': 'e0'}]), 'e0')

    def test_muse_uses_ram_and_lean_no_images(self):
        backend = MuseBackend('/dev/shm')
        with patch('wender.learn.model_client._run', return_value='unused') as run, patch('wender.learn.model_client.parse_muse_output', return_value=MuseOutput({'id': 'e0'}, 0)):
            backend.choose('ask', [{'id': 'e0'}])
        argv = run.call_args.args[0]
        self.assertTrue(argv[argv.index('--prompt-file') + 1].startswith('/dev/shm/'))
        self.assertNotIn('--image', argv)
        self.assertIn('--disable-web-tools', argv)

    def test_draft_memory_persists_only_verified_reproduction(self):
        result, _, _, _ = self.cycle()
        with tempfile.TemporaryDirectory(dir='/dev/shm') as directory:
            path = Path(directory) / 'memory.json'
            record_workflow(path, self.memory, result)
            saved = json.loads(path.read_text())
            self.assertEqual(saved['verified_reproductions'], 1)
            self.assertEqual(saved['workflow']['status'], 'draft')
            self.assertNotIn('Question', path.read_text())
            self.assertNotIn('Answer with', path.read_text())
            record_workflow(path, saved, {'goal_verified': False})
            self.assertEqual(json.loads(path.read_text())['verified_reproductions'], 1)

    # @anchor:wa.tests.cu_web_unit.WebUnit.test_cli_fake_end_to_end
    def test_cli_fake_end_to_end(self):
        clock = Clock()
        cycle = lambda *args, **kwargs: run_web_cycle(*args, **kwargs, clock=clock, sleep=clock.sleep)
        with tempfile.TemporaryDirectory(dir='/dev/shm') as directory:
            path = Path(directory) / 'memory.json'
            path.write_text(json.dumps(self.memory))
            with patch('tools.cu_web.run_web_cycle', side_effect=cycle), patch('tools.cu_web.MuseBackend', return_value=FakePlanner()), patch('tools.cu_web.LocalBackend', return_value=FakeExecutor()), patch('tools.cu_web.SrfIdentitySurface', return_value=FakeSurface()), patch('sys.stdout', new_callable=io.StringIO) as out:
                rc = main(['--surface', 'srf-identity', '--identity', 'test', '--task', 'Question', '--executor', 'qwen', '--endpoint', 'http://localhost/inference', '--memory', str(path)])
            self.assertEqual(rc, 0)
            self.assertTrue(json.loads(out.getvalue())['goal_verified'])

    def test_cli_trace_accepts_empty_and_refuses_nonempty(self):
        for initial in (None, '', 'existing trace\n'):
            with self.subTest(initial=initial), tempfile.TemporaryDirectory(dir='/dev/shm') as directory:
                path = Path(directory) / 'memory.json'
                path.write_text(json.dumps(self.memory))
                trace = Path(directory) / 'trace.jsonl'
                if initial is not None:
                    trace.write_text(initial)
                summary = {'goal_verified': False}
                def cycle(*args, **kwargs):
                    kwargs['trace'].write('{"event":"test"}\n')
                    return summary
                with patch('tools.cu_web.MuseBackend'), patch('tools.cu_web.SrfIdentitySurface'), patch('tools.cu_web.run_web_cycle', side_effect=cycle) as run, patch('sys.stdout', new_callable=io.StringIO) as out:
                    rc = main(['--surface', 'srf-identity', '--identity', 'test',
                               '--task', 'Question', '--executor', 'muse',
                               '--memory', str(path), '--trace', str(trace)])
                self.assertEqual(rc, 2)
                if initial:
                    run.assert_not_called()
                    self.assertEqual(trace.read_text(), initial)
                    self.assertIn('trace file must be empty', json.loads(out.getvalue())['error'])
                else:
                    run.assert_called_once()
                    self.assertEqual(json.loads(out.getvalue()), summary)
                    self.assertEqual(trace.read_text(), '{"event":"test"}\n')

    def test_invalid_plan_and_captcha_stop_before_input(self):
        surface, planner = FakeSurface(), FakePlanner()
        planner.plan = lambda task, memory: memory['workflow']['steps'][:-1]
        result = run_web_cycle(surface, planner, FakeExecutor(), self.memory, task='Q', trace=io.StringIO())
        self.assertFalse(result['goal_verified'])
        self.assertTrue(surface.stopped)
        self.assertEqual(surface.calls, [])
        surface = FakeSurface()
        surface.ax = lambda: {'elements': [element('CAPTCHA verify you are human')]}
        result, _, _, _ = self.cycle(surface)
        self.assertFalse(result['goal_verified'])
        self.assertEqual(surface.calls, [])

    def test_deadline_after_model_prevents_input(self):
        clock, surface, planner, executor = Clock(), FakeSurface(), FakePlanner(), FakeExecutor()
        def slow(intent, options, history):
            clock.now += 20
            return options[0]['id']
        executor.choose = slow
        result = run_web_cycle(surface, planner, executor, self.memory, task='Q', trace=io.StringIO(),
                               max_seconds=10, clock=clock, sleep=clock.sleep)
        self.assertEqual(result['stop_reason'], 'timeout')
        self.assertEqual(len(surface.calls), 3)  # reviewed navigation precedes executor
        self.assertTrue(surface.stopped)

    def test_escalation_survives_muse_failure(self):
        surface, planner, trace = FakeSurface(), FakePlanner(), io.StringIO()
        def broken(*args): raise BackendError('model failed')
        planner.choose = broken
        result = run_web_cycle(surface, planner, FakeExecutor(abstain=True), self.memory,
                               task='Q', trace=trace)
        self.assertEqual(result['escalations'][0]['reason'], 'abstain')
        self.assertIn('"event":"escalation"', trace.getvalue())
        self.assertTrue(surface.stopped)

    def test_harvest_selects_only_last_answer(self):
        surface = FakeSurface()
        original = surface.ax
        def ax():
            value = original()
            if surface.phase == 'done':
                value['elements'] = [dict(e, container={'id': 'old', 'ordinal': 0}) for e in completed('old')] + value['elements']
            return value
        surface.ax = ax
        result, _, executor, _ = self.cycle(surface)
        self.assertTrue(result['goal_verified'])
        options = executor.choices[-1][1]
        self.assertEqual(len(options), 1)
        self.assertEqual(options[0]['name'], 'Copiar')

    def test_empty_harvest_fails_postcheck(self):
        surface = FakeSurface()
        surface.text = lambda container: '  '
        result, _, _, _ = self.cycle(surface)
        self.assertFalse(result['goal_verified'])
        self.assertIsNone(result['harvested'])
        self.assertEqual(result['escalations'], [])

    def _wpplx(self, surface=None, *, task='Question', planner=None, memory=None):
        surface = surface or FakeSurface()
        planner = planner or FakePlanner()
        trace, clock = io.StringIO(), Clock()
        result = run_web_cycle(surface, planner, FakeExecutor(), memory or self.memory,
                               task=task, trace=trace, clock=clock, sleep=clock.sleep,
                               postcheck_seconds=.1, wait_seconds=40)
        return result, surface, planner, trace.getvalue()

    def test_wpplx_entry_ascii_and_size(self):
        for task in (None, '', 7, 'Café', 'emoji \U0001f600', 'e\u0301', 'x' * 65537):
            with self.subTest(task_type=type(task).__name__):
                result, surface, planner, _ = self._wpplx(task=task)
                self.assertFalse(result['goal_verified'])
                self.assertEqual(surface.calls, [])
                self.assertEqual(planner.plans, 0)
                self.assertEqual(result['failure']['error_class'], 'BackendError')
        for task in ('ASCII', 'x' * 65536):
            result, _, _, _ = self._wpplx(task=task)
            self.assertTrue(result['goal_verified'])

    def test_wpplx_navigation_sequence_and_plan_custody(self):
        result, surface, planner, trace = self._wpplx()
        self.assertTrue(result['goal_verified'])
        self.assertEqual(surface.calls[:3], [dict(kind='key', keysym='ctrl+l'),
            dict(kind='type', text='https://www.perplexity.ai/'), dict(kind='key', keysym='Return')])
        self.assertEqual(planner.plans, 1)
        self.assertEqual(planner.choices, [])
        original_choose = FakeExecutor.choose
        def guarded_choose(backend, intent, options, history):
            self.assertNotEqual(intent, 'new_thread')
            return original_choose(backend, intent, options, history)
        with patch.object(FakeExecutor, 'choose', guarded_choose):
            self.assertTrue(self._wpplx()[0]['goal_verified'])
        for mutate in ('destination', 'action', 'empty'):
            planner = FakePlanner()
            def plan(task, memory):
                steps = copy.deepcopy(memory['workflow']['steps'])
                if mutate == 'empty':
                    return []
                steps[0]['action'] = {'kind': 'navigate_home', 'destination': 'https://evil.invalid/'} if mutate == 'destination' else {'kind': 'key', 'keysym': 'Return'}
                return steps
            planner.plan = plan
            result, surface, _, _ = self._wpplx(planner=planner)
            self.assertFalse(result['goal_verified'])
            self.assertEqual(surface.calls, [])
        self.assertNotIn('perplexity.ai', trace)

    def test_wpplx_largest_composer_and_ambiguity(self):
        for mode in ('small_first', 'hidden', 'equal', 'duplicate', 'disabled', 'crowded'):
            surface = FakeSurface()
            original = surface.ax
            def ax():
                value = original()
                composer = next(e for e in value['elements'] if e['role'] == 'textbox')
                composer['box'] = [20, 200, 500, 50]
                other = element('Sidebar search', 'textbox', container='other', value_len=0)
                other['box'] = [20, 100, 20, 20]
                if mode == 'hidden':
                    other.update(visible=False, box=[0, 0, 900, 400])
                if mode == 'equal':
                    other['box'] = list(composer['box'])
                if mode == 'duplicate':
                    other['id'] = composer['id']
                if mode == 'disabled':
                    other.update(box=[0, 0, 900, 400], states={'disabled': True})
                value['elements'].insert(0, other)
                if mode == 'crowded':
                    value['elements'] = [element('Control ' + str(i)) for i in range(80)] + value['elements']
                return value
            surface.ax = ax
            result, _, _, _ = self._wpplx(surface)
            with self.subTest(mode=mode):
                self.assertEqual(result['goal_verified'], mode not in ('equal', 'duplicate'))
                if result['goal_verified']:
                    clicks = [a['element'] for a in surface.calls if a['kind'] == 'click']
                    self.assertTrue(all(e['container']['id'] == 'composer' for e in clicks))
                else:
                    self.assertEqual(surface.calls, [])

    # @anchor:wa.tests.cu_web_unit.WebUnit.test_wpplx_focus_and_length_are_target_specific
    def test_wpplx_focus_and_length_are_target_specific(self):
        for mode in ('no_focus', 'wrong_length', 'other_growth'):
            surface = FakeSurface()
            original = surface.ax
            def ax():
                value = original()
                composer = next(e for e in value['elements'] if e['role'] == 'textbox')
                if mode == 'no_focus':
                    composer['focused'] = False
                if mode == 'wrong_length' and surface.phase == 'typed':
                    composer['value_len'] = 1
                if mode == 'other_growth':
                    composer['value_len'] = 0
                    other = element('Other', 'textbox', container='other', value_len=100)
                    other['box'] = [0, 0, 1, 1]
                    value['elements'].append(other)
                return value
            surface.ax = ax
            result, surface, _, _ = self._wpplx(surface)
            self.assertFalse(result['goal_verified'], mode)
            self.assertFalse(any(a.get('keysym') == 'Return' for a in surface.calls[3:]))
            self.assertEqual(sum(a['kind'] == 'click' for a in surface.calls), 1)
        for additive in (False, True):
            surface = FakeSurface()
            original = surface.ax
            def ax():
                value = original()
                composer = next(e for e in value['elements'] if e['role'] == 'textbox')
                composer.pop('visible', None)
                if not surface.typed:
                    composer['value_len'] = 1
                elif additive:
                    composer['value_len'] += 1
                return value
            surface.ax = ax
            # Both measured producer length conventions retain the exact typed length at submit.
            self.assertTrue(self._wpplx(surface)[0]['goal_verified'])
        self.assertTrue(self._wpplx()[0]['goal_verified'])

    def test_wpplx_multiline_chunks_and_one_submit(self):
        prompt = 'a' * 3100 + '\nsecond\n'
        result, surface, _, _ = self._wpplx(task=prompt)
        self.assertTrue(result['goal_verified'])
        physical = surface.calls[3:]
        chunks = [a['text'] for a in physical if a['kind'] == 'type']
        self.assertEqual(chunks, ['a' * 1500, 'a' * 1500, 'a' * 100, 'second'])
        self.assertTrue(all('\n' not in chunk for chunk in chunks))
        self.assertEqual(sum(a.get('keysym') == 'shift+Return' for a in physical), 2)
        self.assertEqual(sum(a.get('keysym') == 'Return' for a in physical), 1)
        # Deliberately faulty transport submits if a literal newline crosses it.
        surface = FakeSurface()
        original = surface.input
        def faulty(action):
            if action['kind'] == 'type' and '\n' in action['text']:
                raise AssertionError('premature submission')
            return original(action)
        surface.input = faulty
        self.assertTrue(self._wpplx(surface, task=prompt)[0]['goal_verified'])

    def test_wpplx_generation_and_owned_english_completion(self):
        from wender.perceive.disambig import capture_turn_boundary
        ownership = capture_turn_boundary({'elements': []}, task_text='Question')
        base = [element('Finished 2s', container='ab'), element('Copy', container='ab'),
                element('Helpful', container='ab')]
        def select(rows):
            return last_answer_container({'elements': rows, 'truncated': False}, ownership=ownership)
        self.assertEqual(select(base), 'ab')
        newer = [dict(e, container={'id': 'ab', 'ordinal': 9}) for e in base]
        older = [dict(e, container={'id': 'old', 'ordinal': 2}) for e in base]
        self.assertEqual(select(newer + older), 'ab')
        ordinal_boundary = capture_turn_boundary({'elements': [dict(e, container={'id': 'old', 'ordinal': 2})
                                                            for e in completed('old')]}, task_text='Question')
        self.assertIsNone(last_answer_container({'elements': base, 'truncated': False}, ownership=ordinal_boundary))
        variants = [base[:1], base[:2], [base[0], base[2]],
                    base + [element('Stop response')],
                    [base[0], base[1], dict(base[2], container={'id': 'old', 'ordinal': 0})],
                    [base[0], base[1], dict(base[2], box=[10, 25, 40, 20])],
                    base + [element('Finished 1s', container='other')],
                    [dict(e, container={'id': None, 'ordinal': 1}) for e in base],
                    [base[0], dict(base[1], parent_id='code'), base[2],
                     dict(element('code', role='code'), id='code')]]
        for rows in variants:
            self.assertIsNone(select(rows))
        self.assertEqual(select(base + [element('Question', role='heading', container='ab')]), 'ab')
        self.assertEqual(select(base + [element('Question', role='paragraph', container='ab')]), 'ab')
        previous = capture_turn_boundary({'elements': base}, task_text='Question')
        self.assertIsNone(last_answer_container({'elements': base, 'truncated': False}, ownership=previous))
        for mode in ('terminal', 'finished_only', 'stop_forever'):
            surface = FakeSurface()
            original = surface.ax
            def ax():
                value = original()
                value['truncated'] = False
                if surface.phase == 'done':
                    value['elements'] = [e for e in value['elements'] if not e['name'].startswith(('Investigado', 'Copiar'))]
                    value['elements'] += base if mode != 'finished_only' else base[:1]
                    if mode == 'stop_forever':
                        value['elements'].append(element('Stop response'))
                return value
            surface.ax = ax
            surface.text = lambda container: 'Complete current answer'
            result, _, _, trace = self._wpplx(surface)
            self.assertEqual(result['goal_verified'], mode == 'terminal')
            self.assertFalse(any(a.get('keysym') in ('Esc', 'Escape') for a in surface.calls))
            self.assertFalse(any(a['kind'] == 'click' and a['element']['name'] == 'Stop response' for a in surface.calls))

    # @anchor:wa.tests.cu_web_unit.WebUnit.test_wpplx_spanish_followup_selection
    def test_wpplx_spanish_followup_selection(self):
        from wender.perceive.disambig import capture_turn_boundary
        rows = completed('ab')
        ownership = capture_turn_boundary({'elements': []}, task_text='Question')
        previous = capture_turn_boundary({'elements': rows}, task_text='Question')
        self.assertEqual(last_answer_container({'elements': rows}, ownership=ownership), 'ab')
        self.assertIsNone(last_answer_container({'elements': rows}, ownership=previous))
        for busy in ('Detener', 'Investigando 3s', 'Investigando', 'Stop response', 'Generating 14s'):
            snapshot = {'elements': rows + [element(busy)]}
            # Stateless legacy lookup can still inspect the preceding Spanish answer.
            self.assertEqual(last_answer_container(snapshot), 'ab')
            self.assertIsNone(last_answer_container(snapshot, ownership=previous))
            if busy != 'Generating 14s':
                self.assertIsNone(last_answer_container(snapshot, ownership=ownership))

    def test_wpplx_harvest_integrity_and_cleanup(self):
        for text in ('Question', '# Heading only', ' ', 'x' * 65537):
            surface = FakeSurface()
            surface.text = lambda container: text
            result, _, _, _ = self._wpplx(surface)
            self.assertFalse(result['goal_verified'])
            self.assertIsNone(result['harvested'])
        result, _, _, _ = self._wpplx()
        self.assertTrue(result['goal_verified'])
        surface = FakeSurface()
        surface.stop = lambda: (_ for _ in ()).throw(OSError('private cleanup detail'))
        result, _, _, trace = self._wpplx(surface)
        self.assertFalse(result['goal_verified'])
        self.assertIsNone(result['harvested'])
        self.assertNotIn('private cleanup detail', trace)

    # @anchor:wa.tests.cu_web_unit.WebUnit.test_wpplx_reconciliation_every_physical_input
    def test_wpplx_reconciliation_every_physical_input(self):
        for fail_at in (1, 2, 3, 4, 5, 6, 7):
            surface = FakeSurface()
            original = surface.input
            def uncertain(action):
                original(action)
                if len(surface.calls) == fail_at:
                    raise OSError('PRIVATE_PRODUCER_ERROR')
            surface.input = uncertain
            witnesses = []
            original_uncertain = surface.integration.uncertain
            def record(action):
                witnesses.append(action)
                original_uncertain(action)
            surface.integration.uncertain = record
            result, _, _, trace = self._wpplx(surface)
            self.assertFalse(result['goal_verified'])
            self.assertEqual(len(surface.calls), fail_at)
            self.assertEqual(witnesses, [surface.calls[-1]])
            self.assertTrue(surface.integration.closed)
            self.assertNotIn('PRIVATE_PRODUCER_ERROR', trace)
        # Admission closes between typing chunks: the next chunk cannot cross.
        surface = FakeSurface()
        original = surface.input
        dispatch = surface.integration.dispatch
        closed_dispatches = []
        def admitted(srf, action, send):
            if surface.integration.closed:
                closed_dispatches.append(action['kind'])
            return dispatch(srf, action, send)
        surface.integration.dispatch = admitted
        def close(action):
            original(action)
            if action['kind'] == 'type' and action['text'] == 'x' * 1500:
                surface.integration.closed = True
        surface.input = close
        result, _, _, _ = self._wpplx(surface, task='x' * 3000)
        self.assertFalse(result['goal_verified'])
        self.assertEqual(sum(a['kind'] == 'type' for a in surface.calls), 2)
        self.assertEqual(closed_dispatches, [])
        self.assertTrue(self._wpplx()[0]['goal_verified'])

    def test_wpplx_adapter_ascii_and_target_custody(self):
        calls, rows = [], [element(container='ab')]
        def transport(op, payload):
            calls.append((op, payload))
            if op == 'frame':
                return copy.deepcopy(FRAME)
            if op == 'ax':
                return dict(window=FRAME['window'], frame_seq=FRAME['seq'], elements=rows, truncated=False)
            return dict(focus_verified=True, input_mono_ns=101)
        surface = SrfIdentitySurface(transport=transport)
        for text in ('Café', '\U0001f600', 'e\u0301'):
            with self.assertRaises(ValueError):
                surface.input({'kind': 'type', 'text': text})
        self.assertEqual(calls, [])
        surface.input({'kind': 'type', 'text': 'ASCII'})
        surface.ax()
        original = copy.deepcopy(rows[0])
        surface.input({'kind': 'click', 'element': original})
        count = sum(op == 'click' for op, _ in calls)
        for target in (dict(original, id='forged'), dict(original, box=[100, 100, 40, 20])):
            with self.assertRaises(ValueError):
                surface.input({'kind': 'click', 'element': target})
        rows.append(copy.deepcopy(rows[0]))
        surface.ax()
        with self.assertRaises(ValueError):
            surface.input({'kind': 'click', 'element': original})
        rows.pop()
        surface.ax()
        surface.frame()
        with self.assertRaises(ValueError):
            surface.input({'kind': 'click', 'element': original})
        self.assertEqual(sum(op == 'click' for op, _ in calls), count)
        surface.stop()
        self.assertIsNone(surface._latest_ax)

    def test_wpplx_escape_and_panel_focus_refuse_without_evidence(self):
        from wender.perceive.srf_identity import SurfaceRefused
        calls = []
        rows = [element('Stop response', container='ab')]
        def transport(op, payload):
            if op == 'frame':
                return copy.deepcopy(FRAME)
            if op == 'ax':
                return dict(window=FRAME['window'], frame_seq=FRAME['seq'], elements=rows, truncated=False)
            calls.append(op)
            return dict(focus_verified=True, input_mono_ns=101)
        surface = SrfIdentitySurface(transport=transport)
        surface.ax()
        for key in ('Esc', 'Escape'):
            with self.assertRaises(SurfaceRefused):
                surface.input(dict(kind='key', keysym=key))
        with self.assertRaises(SurfaceRefused):
            surface.input(dict(kind='click', element=rows[0]))
        rows[:] = [element('Thread', container='ab')]
        surface.ax()
        with self.assertRaises(SurfaceRefused):
            surface.input(dict(kind='click', element=rows[0], purpose='thread_focus'))
        self.assertEqual(calls, [])
        surface.input(dict(kind='key', keysym='Home'))
        self.assertEqual(calls, ['key'])

    def test_wpplx_memory_validation(self):
        from wender.learn.memory import validate_memory, control_map_digest, MemoryRejected
        validate_memory(self.memory)
        self.assertEqual(self.memory['control_map_digest'], control_map_digest(self.memory['control_map']))
        for mode in ('url', 'selector', 'digest'):
            memory = copy.deepcopy(self.memory)
            if mode == 'url':
                memory['lessons'][0] = 'Never navigate to https://www.perplexity.ai/'
            elif mode == 'selector':
                memory['control_map']['composer']['container_rule'] = 'largest'
                memory['control_map_digest'] = control_map_digest(memory['control_map'])
            else:
                memory['control_map_digest'] = '0' * 64
            with self.assertRaises(MemoryRejected):
                validate_memory(memory)

    def test_wpplx_privacy_canaries(self):
        surface, planner = FakeSurface(), FakePlanner()
        planner.last_model = {'answer': 'ANSWER_CANARY', 'id': 'PRODUCER_CANARY', 'input_tokens': 7}
        surface.text = lambda container: 'ANSWER_CANARY with complete response'
        original = surface.ax
        def ax():
            value = original()
            for row in value['elements']:
                row['id'] = 'PRODUCER_CANARY' + row['id']
            return value
        surface.ax = ax
        result, surface, _, trace = self._wpplx(surface, task='PROMPT_CANARY', planner=planner)
        self.assertTrue(result['goal_verified'])
        for canary in ('PROMPT_CANARY', 'ANSWER_CANARY', 'PRODUCER_CANARY', 'PRIVATE HISTORY'):
            self.assertNotIn(canary, trace)
        self.assertIn('ANSWER_CANARY', result['harvested'])

    def _paged_surface(self, mode='valid', *, english=False, question='Question'):
        surface = FakeSurface()
        surface.page = -1
        original_ax, original_input = surface.ax, surface.input
        def panel():
            row = element('Thread', role='article', container='answer')
            row.update(id='fa', actionable=False, box=[0, 0, 640, 370])
            return row
        def paragraph(ident, name, y):
            row = element(name, role='paragraph', container='answer')
            row.update(id=ident, actionable=False, box=[20, y, 500, 30])
            return row
        def terminal():
            return ([element('Finished 2s', container='answer'), element('Copy', container='answer'),
                     element('Helpful', container='answer')] if english else completed())
        def ax():
            value = original_ax()
            value.update(truncated=False, scroll={'x': 0, 'y': 0})
            if surface.phase != 'done':
                return value
            value['elements'] = [e for e in value['elements'] if not e['name'].startswith(('Investigado', 'Copiar'))]
            if mode != 'missing_anchor':
                value['elements'].append(panel())
            if surface.page < 0:
                value['elements'] += terminal()
            elif surface.page == 0:
                if mode != 'no_top':
                    value['elements'] += [paragraph('00', 'ABOVE QUESTION CANARY', 0),
                                          element(' '.join(question.split()), role='heading', container='answer')]
                value['elements'] += [paragraph('01', ' '.join(question.split()), 10), paragraph('02', 'Searching sources', 15),
                                       paragraph('03', 'Sources', 20), paragraph('aa', 'First…', 40),
                                       paragraph('bb', 'Repeated paragraph', 120)]
                value['elements'].append(dict(paragraph('04', 'OLD ANSWER CANARY', 25),
                                              container={'id': 'old', 'ordinal': 0}))
                if mode == 'node_limit':
                    value['elements'] += [paragraph(hex(1000 + i)[2:], 'Body', 120) for i in range(4097)]
            else:
                y = 120 if mode == 'no_movement' else 120 - surface.page
                value['elements'] += [paragraph('bb', 'Changed' if mode == 'changed_node' else 'Repeated paragraph', y),
                                      paragraph('cc', 'Repeated paragraph', 160)]
                if mode != 'page_limit':
                    value['elements'] += terminal()[1:2] if mode == 'missing_helpful' else terminal()[1:]
            return value
        def physical(action):
            result = original_input(action)
            if action.get('keysym') == 'Home':
                surface.page = 0
            elif action.get('keysym') == 'Page_Down':
                surface.page += 1
            return result
        surface.ax, surface.input = ax, physical
        surface.text = lambda cid: ('x' * 65537 if mode == 'overflow' else 'First complete paragraph') if cid == 'aa' else None
        return surface

    def test_wpplx_pagination_order_identity_and_bounds(self):
        result, surface, _, _ = self._wpplx(self._paged_surface())
        self.assertTrue(result['goal_verified'], result)
        self.assertEqual(result['harvested'], 'First complete paragraph\nRepeated paragraph\nRepeated paragraph')
        keys = [a.get('keysym') for a in surface.calls]
        self.assertIn('Home', keys)
        self.assertEqual(keys.count('Page_Down'), 1)
        for mode in ('missing_anchor', 'no_top', 'node_limit', 'overflow', 'no_movement', 'page_limit', 'changed_node'):
            result, surface, _, _ = self._wpplx(self._paged_surface(mode))
            with self.subTest(mode=mode):
                self.assertFalse(result['goal_verified'])
                self.assertIsNone(result['harvested'])
                self.assertLessEqual(sum(a.get('keysym') == 'Page_Down' for a in surface.calls), 59)

    def test_wpplx_panel_margin_positive_and_rejections(self):
        from wender.perceive.srf_identity import SurfaceRefused
        panel = element('Thread', role='article', container='ab')
        panel.update(actionable=False, box=[0, 0, 640, 370])
        composer = element('Ask anything', role='textbox', container='cd', value_len=0)
        composer['box'] = [20, 380, 600, 50]
        for mode in ('valid', 'missing_anchor', 'stale', 'link', 'truncated', 'changed'):
            rows, calls = [copy.deepcopy(panel), copy.deepcopy(composer)], []
            if mode == 'missing_anchor':
                rows.pop(0)
            if mode == 'link':
                link = element('Reference', role='link', container='ab')
                link['box'] = [0, 360, 20, 10]
                rows.append(link)
            def transport(op, payload):
                if op == 'frame':
                    return copy.deepcopy(FRAME)
                if op == 'ax':
                    return dict(window=FRAME['window'], frame_seq=FRAME['seq'], elements=rows,
                                truncated=mode == 'truncated')
                calls.append((op, payload))
                return dict(focus_verified=True, input_mono_ns=101)
            surface = SrfIdentitySurface(transport=transport)
            surface.ax()
            target = copy.deepcopy(panel)
            if mode == 'stale':
                surface.frame()
            if mode == 'changed':
                target['box'] = [0, 0, 600, 300]
            action = dict(kind='click', element=target, purpose='thread_focus')
            if mode == 'valid':
                surface.input(action)
                self.assertEqual(calls, [('click', {'point': '6,364'})])
            else:
                with self.assertRaises((ValueError, SurfaceRefused)):
                    surface.input(action)
                self.assertEqual(calls, [])

    def test_wpplx_injected_escape_never_crosses(self):
        for key in ('Esc', 'Escape'):
            memory = copy.deepcopy(self.memory)
            memory['workflow']['steps'][2]['action']['keysym'] = key
            result, surface, _, _ = self._wpplx(memory=memory)
            self.assertFalse(result['goal_verified'])
            self.assertFalse(any(a.get('keysym') == key for a in surface.calls))
        self.assertTrue(self._wpplx()[0]['goal_verified'])

    def test_wpplx_deadline_between_chunks(self):
        surface, clock = FakeSurface(), Clock()
        original = surface.input
        def slow(action):
            result = original(action)
            if action['kind'] == 'type' and action['text'] == 'x' * 1500:
                clock.now += 10
            return result
        surface.input = slow
        result = run_web_cycle(surface, FakePlanner(), FakeExecutor(), self.memory,
            task='x' * 3000, trace=io.StringIO(), max_seconds=5, clock=clock, sleep=clock.sleep)
        self.assertEqual(result['stop_reason'], 'timeout')
        self.assertEqual(sum(a['kind'] == 'type' for a in surface.calls), 2)
        self.assertTrue(surface.integration.closed)
        self.assertTrue(self._wpplx()[0]['goal_verified'])

    def test_wpplx_container_scope_change_refuses_result(self):
        surface = FakeSurface()
        original_ax = surface.ax
        changed = False
        def ax():
            result = original_ax()
            if changed:
                result['url'] = 'https://unrelated.invalid/'
            return result
        def text(cid):
            nonlocal changed
            changed = True
            return 'Current answer'
        surface.ax, surface.text = ax, text
        self.assertFalse(self._wpplx(surface)[0]['goal_verified'])
        self.assertTrue(self._wpplx()[0]['goal_verified'])

    def test_wpplx_submission_requires_bound_nonempty_composer(self):
        for mode in ('changed', 'cleared', 'generating'):
            surface = FakeSurface()
            original = surface.ax
            reads = 0
            def ax():
                nonlocal reads
                value = original()
                if surface.phase == 'typed':
                    reads += 1
                    # First read verifies typing. Subsequent reads bind submission.
                    if reads >= 2:
                        composer = next(e for e in value['elements'] if e['role'] == 'textbox')
                        if mode == 'changed':
                            composer['id'] = 'NEW_COMPOSER'
                        elif mode == 'cleared':
                            composer['value_len'] = 0
                        else:
                            value['elements'].append(element('Stop response'))
                return value
            surface.ax = ax
            result, surface, _, _ = self._wpplx(surface)
            self.assertFalse(result['goal_verified'], mode)
            self.assertFalse(any(a.get('keysym') == 'Return' for a in surface.calls[3:]))
        self.assertTrue(self._wpplx()[0]['goal_verified'])

    # @anchor:wa.tests.cu_web_unit.WebUnit.test_wpplx_cycle_summary_telemetry
    def test_wpplx_cycle_summary_telemetry(self):
        stages = ('t_home_ready', 't_composer_found', 't_typed', 't_sent_verified',
                  't_done_detected', 't_harvest_done')
        for mode in ('valid', 'mismatch', 'missing', 'old_heading', 'refused', 'uncertain', 'fallback', 'cleanup'):
            with self.subTest(mode=mode):
                surface, planner, executor, clock = FakeSurface(), FakePlanner(), FakeExecutor(mode == 'fallback'), Clock()
                original_ax, original_input = surface.ax, surface.input
                def ax():
                    clock.now += .002
                    value = original_ax()
                    value['elements'].append(element('Settings'))
                    if mode == 'old_heading':
                        if surface.phase == 'done':
                            value['elements'] = [e for e in value['elements'] if e['role'] != 'heading']
                        value['elements'].append(element('Question second line', role='heading', container='old'))
                    if mode in ('mismatch', 'missing'):
                        value['elements'] = [dict(e, name='Different question') if e['role'] == 'heading' else e
                                             for e in value['elements'] if mode != 'missing' or e['role'] != 'heading']
                    return value
                def physical(action):
                    clock.now += .003
                    result = original_input(action)
                    if mode == 'uncertain':
                        raise OSError('private actuator error')
                    return result
                surface.ax, surface.input = ax, physical
                if mode == 'cleanup':
                    surface.stop = lambda: (_ for _ in ()).throw(OSError('private cleanup'))
                trace = io.StringIO()
                result = run_web_cycle(surface, planner, executor, self.memory,
                                       task='Café' if mode == 'refused' else 'Question\nsecond line',
                                       trace=trace, clock=clock, sleep=clock.sleep)
                self.assertTrue(set(stages + ('model_calls', 'inputs_sent', 'wrong_or_refused_inputs',
                                                    'recoveries', 'blocked_controls', 'prompt_matches',
                                                    'agent_ms', 'answer_wait_ms', 'harvest_ms')) <= result.keys())
                self.assertEqual(result['model_calls'], planner.plans + len(planner.choices) + len(executor.choices))
                self.assertEqual(result['inputs_sent'], len(surface.calls))
                self.assertEqual(result['recoveries'], len(result['escalations']))
                self.assertEqual(result['wrong_or_refused_inputs'], int(mode in ('refused', 'uncertain')))
                self.assertEqual(result['blocked_controls'], {'blocked_control_present': 0, 'by_reason': {}}
                                 if mode in ('refused', 'uncertain') else
                                 {'blocked_control_present': 3 if mode == 'mismatch' else 4,
                                  'by_reason': {'blocked_control': 3 if mode == 'mismatch' else 4}})
                self.assertIs(result['prompt_matches'], mode in ('valid', 'fallback', 'cleanup'))
                self.assertEqual(result['agent_ms'], result['t_sent_verified'])
                if mode in ('refused', 'uncertain'):
                    for stage in stages:
                        self.assertIsNone(result[stage])
                    self.assertIsNone(result['agent_ms'])
                    self.assertIsNone(result['answer_wait_ms'])
                    self.assertIsNone(result['harvest_ms'])
                    self.assertFalse(result['goal_verified'])
                else:
                    self.assertEqual(result['goal_verified'], mode in ('valid', 'fallback'), result)
                    if mode == 'cleanup':
                        self.assertIsNone(result['harvested'])
                    values = [result[k] for k in stages if result[k] is not None]
                    self.assertEqual(values, sorted(values))
                    if mode == 'mismatch':
                        self.assertIsNone(result['t_done_detected'])
                        self.assertIsNone(result['t_harvest_done'])
                        self.assertIsNone(result['answer_wait_ms'])
                        self.assertIsNone(result['harvest_ms'])
                        continue
                    self.assertGreater(values[0], 0)
                    self.assertGreater(result['answer_wait_ms'], 0)
                    if mode in ('missing', 'old_heading'):
                        self.assertIsNone(result['harvest_ms'])
                        self.assertIsNone(result['t_harvest_done'])
                        continue
                    self.assertGreater(result['harvest_ms'], 0)
                    self.assertEqual(result['answer_wait_ms'], result['t_done_detected'] - result['t_sent_verified'])
                    self.assertEqual(result['harvest_ms'], result['t_harvest_done'] - result['t_done_detected'])
                self.assertNotIn('second line', trace.getvalue())

    def test_wpplx_english_pagination_question_boundary(self):
        question = 'Question\nsecond line'
        for mode in ('valid', 'no_top', 'missing_helpful'):
            result, _, _, _ = self._wpplx(self._paged_surface(mode, english=True, question=question), task=question)
            with self.subTest(mode=mode):
                self.assertEqual(result['goal_verified'], mode == 'valid', result)
                if mode == 'valid':
                    self.assertEqual(result['harvested'], 'First complete paragraph\nRepeated paragraph\nRepeated paragraph')
                    self.assertNotIn('ABOVE QUESTION CANARY', result['harvested'])
                else:
                    self.assertIsNone(result['harvested'])

    # @anchor:wa.tests.cu_web_unit.WebUnit.test_wpplx_viewport_transcripts
    def test_wpplx_viewport_transcripts(self):
        # No panel node and no snapshot containing both Finished and the bar.
        for mode in ('long', 'long_prompt', 'truncated_prompt', 'static_question', 'wrong_top', 'normalized_prompt', 'composer_after', 'composer_first', 'bar_behind', 'text_behind', 'minute', 'early_bar', 'below_fold', 'draft', 'draft_stuck', 'inactive_stop', 'overlap', 'stop', 'wrong_heading', 'changed_url', 'no_top', 'inactive_copy', 'ambiguous_bar', 'no_thread',
                     'no_bar', 'no_marker', 'bad_duration', 'composer_no_container', 'question_after', 'full_missing', 'overflow', 'page_limit', 'changed_node'):
            with self.subTest(mode=mode):
                surface = FakeSurface()
                surface.page, surface.at_end = -1, mode != 'below_fold'
                surface.selected = False
                original_ax, original_input = surface.ax, surface.input
                full = 'LONG_ANSWER_CANARY ' + 'z' * 180
                def leaf(ident, name, y=180):
                    return dict(element(name, 'StaticText', container='current', actionable=False),
                                id=ident, box=[20, y, 540, 30])
                def ax():
                    value = original_ax()
                    composer = next(e for e in value['elements'] if e['role'] == 'textbox')
                    composer.pop('visible', None)
                    composer['box'] = [20, 390, 580, 40]
                    if mode in ('draft', 'draft_stuck') and surface.phase == 'new':
                        composer['value_len'] = len(surface.typed) or 1
                    if surface.phase != 'done':
                        return value
                    value['scroll'] = {'x': 0, 'y': 0}
                    value['url'] = 'https://www.perplexity.ai/' if mode == 'no_thread' else 'https://www.perplexity.ai/search/current'
                    if mode == 'changed_url' and surface.page >= 1:
                        value['url'] = 'https://www.perplexity.ai/search/foreign'
                    value['elements'] = []
                    if (surface.page == 0 and mode != 'no_top') or (mode == 'wrong_heading' and surface.page < 0):
                        value['elements'].append(element('FOREIGN' if mode == 'wrong_heading' else surface.submitted,
                                                         'heading', container='question', actionable=False))
                        if mode == 'static_question':
                            value['elements'][-1]['role'] = 'StaticText'
                        if mode == 'wrong_top':
                            value['elements'][-1]['name'] = 'Foreign question'
                        if mode == 'truncated_prompt':
                            value['elements'][-1].update(name='Question prefix', truncated=True)
                        if mode == 'long_prompt':
                            value['elements'][-1]['name'] = surface.submitted[:120]
                        if mode == 'normalized_prompt':
                            value['elements'][-1]['name'] = surface.submitted.lower().replace('-', ' ')
                    if surface.page == 0:
                        value['elements'] += [leaf('01', 'Missing' if mode == 'no_marker' else 'Finished'),
                                              leaf('02', 'Missing' if mode == 'bad_duration' else '1m 2s' if mode == 'minute' else '2s', 210),
                                              leaf('aa', full[:120], 250),
                                              dict(leaf('dd', 'NON_TEXT_CANARY', 280), role='paragraph')]
                    elif surface.page >= 1:
                        if mode == 'overlap' and surface.page == 1:
                            value['elements'] += [leaf('01', 'Finished'), leaf('02', '2s', 200)]
                        value['elements'] += [leaf('bb', 'Middle answer page', 220)]
                        if mode == 'changed_node':
                            value['elements'].append(leaf('aa', 'Changed answer', 250))
                    else:
                        value['elements'] += [leaf('ee', 'Last answer page', 220)]
                    if mode == 'page_limit' and surface.page >= 1:
                        value['elements'].append(leaf('page-' + str(surface.page), 'New page', 220))
                    if surface.page == 2 or (surface.page < 0 and surface.at_end):
                        value['elements'].append(leaf('cc', 'Last answer page', 270))
                        if mode != 'no_bar' and (mode != 'page_limit' or surface.page < 0):
                            value['elements'] += [dict(element('Copy', container='current'), box=[20, 320, 40, 20]),
                                                  dict(element('Helpful', container='current'), box=[80, 320, 40, 20])]
                    if mode == 'early_bar' and surface.page == 0:
                        value['elements'] += [dict(element('Copy', container='current'), box=[20, 320, 40, 20]),
                                              dict(element('Helpful', container='current'), box=[80, 320, 40, 20])]
                    if mode == 'inactive_copy':
                        for row in value['elements']:
                            if row['name'] == 'Copy':
                                row['actionable'] = False
                    if mode == 'ambiguous_bar' and surface.page == 2:
                        value['elements'] += [dict(element('Copy', container='foreign'), box=[20, 350, 40, 20]),
                                              dict(element('Helpful', container='foreign'), box=[80, 350, 40, 20])]
                    if not surface.at_end and surface.page < 0:
                        value['elements'].append(element('Scroll to end'))
                    if mode in ('stop', 'inactive_stop'):
                        value['elements'].append(element('Stop response (Esc)', actionable=mode == 'stop'))
                    if mode in ('composer_after', 'composer_first'):
                        banner = dict(leaf('banner', 'COMPOSER_CANARY', 300), container={'id': 'thread-composer'})
                        composer['container'] = {'id': 'thread-composer'}
                        if composer in value['elements']:
                            value['elements'].remove(composer)
                        if mode == 'composer_after':
                            # A later unrelated node must also be cut by document order.
                            pos = len(value['elements'])
                            value['elements'][pos:pos] = [banner, composer, leaf('after-composer', 'AFTER_COMPOSER_CANARY', 310)]
                        else:
                            value['elements'][0:0] = ([banner, composer] if surface.page >= 1 else [composer])
                    if mode == 'composer_no_container':
                        composer['container'] = None
                    if mode == 'bar_behind':
                        for row in value['elements']:
                            if row['name'] in ('Copy', 'Helpful'):
                                row['box'][1] = 400
                    if mode == 'text_behind':
                        for row in value['elements']:
                            if row['role'] == 'StaticText':
                                row['box'][1] = 400
                    if composer not in value['elements']:
                        value['elements'].append(composer)
                    if mode == 'question_after' and surface.page == 0:
                        headings = [e for e in value['elements'] if e['role'] == 'heading']
                        value['elements'] = [e for e in value['elements'] if e not in headings] + headings
                    return value
                def physical(action):
                    result = original_input(action)
                    if action.get('keysym') == 'Return' and mode in ('draft', 'draft_stuck') and surface.phase == 'new':
                        surface.typed = 'restored private draft'
                    if action.get('keysym') == 'ctrl+a':
                        surface.selected = True
                    if action.get('keysym') == 'BackSpace' and surface.selected and mode != 'draft_stuck':
                        surface.typed = ''
                    if action.get('keysym') == 'Home':
                        surface.page = 0
                    if action.get('keysym') == 'Page_Down':
                        surface.page += 1
                    if action['kind'] == 'click' and action['element']['name'] == 'Scroll to end':
                        surface.at_end = True
                    return result
                surface.ax, surface.input = ax, physical
                surface.text = lambda ident: surface.submitted if mode in ('long_prompt', 'truncated_prompt') and ident not in ('aa',) else (None if mode == 'full_missing' else ('x' * 65537 if mode == 'overflow' else full))
                result, _, _, trace = self._wpplx(surface, task=('Long question ' * 12 if mode == 'long_prompt' else 'Question-With Punctuation!' if mode == 'normalized_prompt' else 'Question'))
                ok = mode in ('long', 'long_prompt', 'truncated_prompt', 'static_question', 'normalized_prompt', 'composer_after', 'bar_behind', 'text_behind', 'minute', 'early_bar', 'below_fold', 'draft', 'inactive_stop', 'overlap', 'no_bar', 'inactive_copy', 'ambiguous_bar')
                self.assertEqual(result['goal_verified'], ok, result)
                self.assertNotIn('LONG_ANSWER_CANARY', trace)
                self.assertNotIn('restored private draft', trace)
                self.assertFalse(any(a.get('keysym') in ('Esc', 'Escape', 'End', 'Page_Up', 'Up', 'Down') for a in surface.calls))
                if ok:
                    self.assertEqual(result['harvested'], full if mode == 'early_bar' else full + '\nMiddle answer page\nLast answer page')
                    self.assertEqual(result['model_calls'], 4)
                    self.assertTrue(result['prompt_matches'])
                    self.assertEqual(sum(a.get('keysym') == 'Page_Down' for a in surface.calls), 0 if mode == 'early_bar' else 4 if mode in ('no_bar', 'inactive_copy', 'ambiguous_bar') else 2)
                    self.assertEqual(sum(a.get('keysym') == 'Home' for a in surface.calls), 1)
                    if mode == 'below_fold':
                        self.assertFalse(any(a['kind'] == 'click' and a['element']['name'] == 'Scroll to end' for a in surface.calls))
                    if mode == 'draft':
                        keys = [a.get('keysym') for a in surface.calls]
                        self.assertIn('ctrl+a', keys)
                        self.assertIn('BackSpace', keys)
                else:
                    self.assertIsNone(result['harvested'])
                    if mode in ('wrong_heading', 'stop', 'no_thread'):
                        self.assertNotIn('wait_done', result['completed'])
                    if mode == 'changed_url':
                        self.assertEqual(sum(a.get('keysym') == 'Page_Down' for a in surface.calls), 1)
                    if mode == 'overflow':
                        self.assertFalse(any(a.get('keysym') == 'Page_Down' for a in surface.calls))

    # @anchor:wa.tests.cu_web_unit.WebUnit.test_wpplx_viewport_margin
    def test_wpplx_viewport_margin(self):
        from wender.perceive.srf_identity import SurfaceRefused
        for mode in ('valid', 'behind', 'obstructed', 'foreign', 'busy', 'truncated', 'region'):
            rows = [dict(element('Body', 'StaticText', container='ab', actionable=False), box=[20, 200, 500, 30]),
                    dict(element('Ask', 'textbox', container='composer'), box=[20, 390, 580, 40])]
            if mode == 'behind':
                rows[0]['box'][1] = 400
            if mode == 'region':
                rows[0]['region'] = 'navigation'
            if mode == 'obstructed':
                rows.append(dict(element('Link', 'link'), box=[0, 250, 20, 30]))
            if mode == 'busy':
                rows.append(element('Stop response'))
            calls = []
            def transport(op, payload):
                if op == 'frame':
                    return copy.deepcopy(FRAME)
                if op == 'ax':
                    window = dict(FRAME['window'], url='https://www.perplexity.ai/' + ('search/thread' if mode != 'foreign' else ''))
                    return dict(window=window, frame_seq=FRAME['seq'], elements=rows, truncated=mode == 'truncated')
                calls.append((op, payload))
                return dict(focus_verified=True, input_mono_ns=101)
            surface = SrfIdentitySurface(transport=transport)
            surface.ax()
            action = dict(kind='click', element=rows[0], purpose='viewport_thread_focus')
            if mode in ('valid', 'behind'):
                surface.input(action)
                self.assertEqual(calls[0][1]['point'], '6,266')   # integer pixels: the producer CLI rejects floats (rc 2) [MEASURED]
            else:
                with self.assertRaises(SurfaceRefused):
                    surface.input(action)
                self.assertEqual(calls, [])

    # @anchor:wa.tests.cu_web_unit.WebUnit.test_wpplx_idle_consecutive
    def test_wpplx_idle_consecutive(self):
        for mode in ('reset', 'never', 'forever', 'short_budget'):
            surface, clock = FakeSurface(), Clock()
            original = surface.ax
            polls = 0
            def ax():
                nonlocal polls
                value = original()
                if surface.phase == 'done':
                    polls += 1
                    # Submit verification and step observation precede the wait polls.
                    if mode == 'forever' or (mode == 'reset' and polls in (3, 5)):
                        value['elements'].append(element('Stop response'))
                return value
            surface.ax = ax
            result = run_web_cycle(surface, FakePlanner(), FakeExecutor(), self.memory,
                task='Question', trace=io.StringIO(), clock=clock, sleep=clock.sleep,
                postcheck_seconds=.1, wait_seconds=20 if mode == 'short_budget' else 40)
            self.assertEqual(result['goal_verified'], mode in ('reset', 'never'), result)
            if mode in ('reset', 'never'):
                self.assertEqual(result['answer_wait_ms'], 4000 if mode == 'reset' else 30000)
            else:
                self.assertNotIn('wait_done', result['completed'])
            self.assertFalse(any(a.get('keysym') in ('Esc', 'Escape') or
                a.get('element', {}).get('name') in ('Stop response', 'Scroll to end') for a in surface.calls))

    # @anchor:wa.tests.cu_web_unit.WebUnit.test_wpplx_submit_latency
    def test_wpplx_submit_latency(self):
        for delay in (3, 21):
            surface, clock = FakeSurface(), Clock()
            original = surface.ax
            def ax():
                value = original()
                if surface.phase == 'done' and clock.now < delay:
                    value['url'] = 'https://www.perplexity.ai/'
                    value['elements'] = [element('Ask', 'textbox', container='composer',
                        value_len=len(surface.submitted), focused=True)]
                return value
            surface.ax = ax
            result = run_web_cycle(surface, FakePlanner(), FakeExecutor(), self.memory,
                task='Question', trace=io.StringIO(), clock=clock, sleep=clock.sleep,
                postcheck_seconds=.1, wait_seconds=40)
            self.assertEqual(result['goal_verified'], delay == 3, result)
            if delay == 3:
                self.assertGreaterEqual(result['t_sent_verified'], 3000)
            else:
                self.assertIsNone(result['t_sent_verified'])
            self.assertEqual(sum(a.get('keysym') == 'Return' for a in surface.calls), 2)

    # @anchor:wa.tests.cu_web_unit.WebUnit.test_wpplx_closed_failure_codes
    def test_wpplx_closed_failure_codes(self):
        for message in ('HARVEST_INVALIDATED', 'private page text', 'A', 'X' * 65):
            surface = FakeSurface()
            surface.ax = lambda: (_ for _ in ()).throw(BackendError(message))
            result, _, _, trace = self._wpplx(surface)
            self.assertEqual(result['failure']['detail'],
                message if message == 'HARVEST_INVALIDATED' else 'BackendError')
            if message != 'HARVEST_INVALIDATED':
                self.assertNotIn(message, trace if len(message) > 1 else result['failure']['detail'])

    # @anchor:wa.tests.cu_web_unit.WebUnit.test_wpplx_thread_switch
    def test_wpplx_thread_switch(self):
        for switch_at in (4, 5):
            surface = FakeSurface()
            original = surface.ax
            polls = 0
            def ax():
                nonlocal polls
                value = original()
                if surface.phase == 'done':
                    polls += 1
                    value['url'] = 'https://www.perplexity.ai/search/' + ('foreign' if polls >= switch_at else 'current')
                    value['elements'] = [e for e in value['elements'] if not e['name'].startswith('Investigado')]
                    value['elements'] += [dict(element('Copy', container='answer'), box=[20, 320, 40, 20]),
                                          dict(element('Helpful', container='answer'), box=[80, 320, 40, 20]),
                                          dict(element('Body', 'StaticText', container='answer', actionable=False), box=[20, 220, 500, 30])]
                return value
            surface.ax = ax
            result, _, _, _ = self._wpplx(surface)
            self.assertFalse(result['goal_verified'])
            if switch_at == 4:
                self.assertNotIn('wait_done', result['completed'])
            self.assertFalse(any(a.get('keysym') == 'Home' for a in surface.calls))
        self.assertTrue(self._wpplx()[0]['goal_verified'])

    # @anchor:wa.tests.cu_web_unit.WebUnit.test_wpplx_read_retries
    def test_wpplx_read_retries(self):
        from wender.perceive.srf_identity import SurfaceNotReady, SurfaceRefused
        for exhausted in (False, True):
            surface = FakeSurface()
            original = surface.ax
            errors = [SurfaceNotReady, SurfaceRefused] if not exhausted else [SurfaceNotReady] * 15
            def ax():
                if surface.phase == 'typed' and errors:
                    raise errors.pop(0)('transient', 'ax')
                return original()
            surface.ax = ax
            result, _, _, trace = self._wpplx(surface)
            self.assertEqual(result['goal_verified'], not exhausted)
            self.assertEqual([a['text'] for a in surface.calls if a['kind'] == 'type'],
                             ['https://www.perplexity.ai/', 'Question'])
            if exhausted:
                self.assertTrue(surface.integration.closed)
                self.assertEqual(result['failure']['error_class'], 'SurfaceNotReady')
            self.assertNotIn('transient', trace)

    # @anchor:wa.tests.cu_web_unit.WebUnit.test_wpplx_mutant_controls
    def test_wpplx_mutant_controls(self):
        import sys
        import types
        import wender.learn.cu_loop as loop
        import wender.perceive.srf_identity as identity
        import wender.learn.memory as memory_module
        cases = [
            (loop, "not task.isascii()", 'False', 'test_wpplx_entry_ascii_and_size'),
            (loop, "len(task) > 65536", 'False', 'test_wpplx_entry_ascii_and_size'),
            (loop, "if not script_completed and (not isinstance(plan, list) or len(plan) != len(canonical)):", 'if False:', 'test_wpplx_navigation_sequence_and_plan_custody'),
            (loop, "'https://www.perplexity.ai/'", "'https://evil.invalid/'", 'test_wpplx_navigation_sequence_and_plan_custody'),
            (loop, "if len(largest) != 1:", 'if False:', 'test_wpplx_largest_composer_and_ambiguity'),
            (loop, "return target if sum(e.get('id') == target['id'] for e in ax['elements']) == 1 else None", 'return target', 'test_wpplx_largest_composer_and_ambiguity'),
            (loop, "t.get('focused') is True or (t.get('states') or {}).get('focused') is True", 'True', 'test_wpplx_focus_and_length_are_target_specific'),
            (loop, "t.get('value_len') in {len(task), initial + len(task)}", 'True', 'test_wpplx_focus_and_length_are_target_specific'),
            (loop, "line[offset:offset + 1500]", 'line', 'test_wpplx_multiline_chunks_and_one_submit'),
            (loop, "task.split('\\n')", '[task]', 'test_wpplx_multiline_chunks_and_one_submit'),
            (loop, "if integration.check_ready() is not True:", 'if False:', 'test_wpplx_reconciliation_every_physical_input'),
            (loop, "if verify_harvested_text(text, ownership)[0] is not True:", 'if False:', 'test_wpplx_harvest_integrity_and_cleanup'),
            (loop, "len(text.encode('utf-8')) > 65536", 'False', 'test_wpplx_harvest_integrity_and_cleanup'),
            (loop, "if len(panels) != 1:\n        raise BackendError('THREAD_ANCHOR_UNAVAILABLE')", "if not panels:\n        panels = [ax['elements'][0]]", 'test_wpplx_pagination_order_identity_and_bounds'),
            (loop, "if len(headings) != 1:\n                    raise BackendError('HARVEST_TOP_UNPROVEN')\n                top = headings[0]", 'top = headings[0] if headings else -1', 'test_wpplx_pagination_order_identity_and_bounds'),
            (loop, "if len(seen) >= 4096:", 'if False:', 'test_wpplx_pagination_order_identity_and_bounds'),
            (loop, "if total > 65536:", 'if False:', 'test_wpplx_pagination_order_identity_and_bounds'),
            (loop, "if not moved:", 'if False:', 'test_wpplx_pagination_order_identity_and_bounds'),
            (loop, "if seen[ident] != value:", 'if False:', 'test_wpplx_pagination_order_identity_and_bounds'),
            (loop, "'harvested': harvested if reason == 'goal_reached' else None", "'harvested': harvested", 'test_wpplx_harvest_integrity_and_cleanup'),
            (loop, "raw = getattr(backend, 'last_model', {})", "raw = getattr(backend, 'last_model', {})\n        return raw", 'test_wpplx_privacy_canaries'),
            (identity, "if _generating(ax) and (ownership is not None or", "if False and (ownership is not None or", 'test_wpplx_generation_and_owned_english_completion'),
            (identity, "if ownership is not None and (cid in ownership.boundary_containers or", "if False and (cid in ownership.boundary_containers or", 'test_wpplx_generation_and_owned_english_completion'),
            (identity, "if len(helpful) != 1:\n        return None", "if len(helpful) != 1:\n        return len(elements)", 'test_wpplx_generation_and_owned_english_completion'),
            (identity, "if len(copies) != 1:\n        return None", "if len(copies) != 1:\n        return len(elements)", 'test_wpplx_generation_and_owned_english_completion'),
            (identity, "abs(boxes[0][1] - boxes[1][1]) <= 4", 'True', 'test_wpplx_generation_and_owned_english_completion'),
            (identity, "and _plain_answer_control(e, elements)", 'and True', 'test_wpplx_generation_and_owned_english_completion'),
            (identity, "or not action['text'].isascii()", 'or False', 'test_wpplx_adapter_ascii_and_target_custody'),
            (identity, "if len(matches) != 1 or {k: v for k, v in matches[0].items() if k != 'value'} != {k: v for k, v in supplied.items() if k != 'value'}:\n                raise ValueError('target custody')\n            target = matches[0]", 'target = supplied', 'test_wpplx_adapter_ascii_and_target_custody'),
            (identity, "self._latest_ax = None\n        frame =", 'frame =', 'test_wpplx_adapter_ascii_and_target_custody'),
            (identity, "ax.get('truncated') is not False or target.get('role')", "False or target.get('role')", 'test_wpplx_panel_margin_positive_and_rejections'),
            (identity, "if bounds[0] <= x <= bounds[0] + bounds[2] and bounds[1] <= y <= bounds[1] + bounds[3]:", 'if False:', 'test_wpplx_panel_margin_positive_and_rejections'),
            (identity, "if action['keysym'].casefold() in ('esc', 'escape')", "if False", 'test_wpplx_escape_and_panel_focus_refuse_without_evidence'),
        ]
        cases.extend([
            (identity, "if not isinstance(elements, list) or ax.get('truncated') is True:",
             "if not isinstance(elements, list) or _generating(ax) or ax.get('truncated') is True:",
             'test_wpplx_spanish_followup_selection'),
            (loop, "not isinstance(task, str)", 'False', 'test_wpplx_entry_ascii_and_size'),
            (loop, "or not task or len(task)", 'or False or len(task)', 'test_wpplx_entry_ascii_and_size'),
            (loop, "if page == 59:", 'if False:', 'test_wpplx_pagination_order_identity_and_bounds'),
            (identity, "if len(candidates) != 1:", 'if False:', 'test_wpplx_generation_and_owned_english_completion'),
            (loop, "if target['id'] != bound_composer:", 'if False:', 'test_wpplx_submission_requires_bound_nonempty_composer'),
            (loop, "if composer_at(focused, ident).get('value_len') != bound_length or _generating(focused):", 'if False:', 'test_wpplx_submission_requires_bound_nonempty_composer'),
            (memory_module, "RAW_LEAK_PATTERNS = re.compile(", "RAW_LEAK_PATTERNS = re.compile(r'(?!)') #", 'test_wpplx_memory_validation'),
            (memory_module, "rule in ALLOWED_CONTAINER_RULES", 'True', 'test_wpplx_memory_validation'),
            (loop, "if clock() >= deadline:", 'if False:', 'test_wpplx_deadline_between_chunks'),
            (loop, "(fresh.get('window') or {}).get('url', fresh.get('url')) != origin", 'False', 'test_wpplx_container_scope_change_refuses_result'),
            (memory_module, "if out.get('control_map_digest') != control_map_digest(out['control_map']):", 'if False:', 'test_wpplx_memory_validation'),
        ])
        cases.extend([
            (loop, "last_dispatched = physical", 'last_dispatched = None', 'test_wpplx_reconciliation_every_physical_input'),
            (loop, "failure = {'error_class': type(exc).__name__, 'detail': (str(exc) if isinstance(exc, BackendError) and re.fullmatch(r'[A-Z][A-Z0-9_]{2,63}', str(exc)) else type(exc).__name__)}", "failure = {'error_class': type(exc).__name__, 'detail': str(exc)}", 'test_wpplx_closed_failure_codes'),
        ])
        for key in ('t_home_ready', 't_composer_found', 't_typed', 't_sent_verified',
                    't_done_detected', 't_harvest_done'):
            cases.append((loop, '**stages, **counters,',
                          "**{k: v for k, v in stages.items() if k != " + repr(key) + "}, **counters,",
                          'test_wpplx_cycle_summary_telemetry'))
        for key in ('model_calls', 'inputs_sent', 'wrong_or_refused_inputs', 'recoveries'):
            cases.append((loop, "counters['" + key + "'] += 1", 'pass', 'test_wpplx_cycle_summary_telemetry'))
        cases.extend([
            (loop, 'english=english', 'english=False', 'test_wpplx_english_pagination_question_boundary'),
            (loop, "if ident in seen:", 'if False:', 'test_wpplx_pagination_order_identity_and_bounds'),
            (loop, "if node.get('truncated') is True or value.endswith(('…', '...')):", 'if False:', 'test_wpplx_pagination_order_identity_and_bounds'),
            (loop, "and e.get('region') == 'main' and (e.get('container') or {}).get('id') == cid\n                    and e.get('role') in", "and e.get('region') == 'main'\n                    and e.get('role') in", 'test_wpplx_pagination_order_identity_and_bounds'),
            (identity, 'latest = max(ordinal(pair) for pair in markers)', 'latest = min(ordinal(pair) for pair in markers)', 'test_wpplx_generation_and_owned_english_completion'),
            (loop, "'blocked_controls': block_filter.counts()", "'blocked_controls': {}", 'test_wpplx_cycle_summary_telemetry'),
            (identity, "terminal = _terminal_bar(ax, cid)", "terminal = None if any(e.get('role') == 'heading' for e in elements) else _terminal_bar(ax, cid)", 'test_wpplx_generation_and_owned_english_completion'),
            (loop, "(e.get('container') or {}).get('id') not in ownership.boundary_containers and", '', 'test_wpplx_cycle_summary_telemetry'),
            (loop, "'prompt_matches': prompt_matches", "'prompt_matches': True", 'test_wpplx_cycle_summary_telemetry'),
            (loop, "'agent_ms': stages['t_sent_verified']", "'agent_ms': 0", 'test_wpplx_cycle_summary_telemetry'),
            (loop, "stages['t_done_detected'] - stages['t_sent_verified']", '0', 'test_wpplx_cycle_summary_telemetry'),
            (loop, "stages['t_harvest_done'] - stages['t_done_detected']", '0', 'test_wpplx_cycle_summary_telemetry'),
            (loop, "(clock() - cycle_start) * 1000", '0', 'test_wpplx_cycle_summary_telemetry'),
            (loop, 'if i > top and (terminal', 'if (terminal', 'test_wpplx_english_pagination_question_boundary'),
            (loop, "_web_prompt_matches(surface, e, task)", "e.get('name') == task", 'test_wpplx_english_pagination_question_boundary'),
        ])
        cases.extend([
            (loop, "idle_polls >= 2", "idle_polls >= 1", 'test_wpplx_idle_consecutive'),
            (loop, "if any(not _web_prompt_matches(surface, e, task) for e in headings):", "if False:", 'test_wpplx_viewport_transcripts'),
            (loop, "if thread_url is not None and url != thread_url:", "if False:", 'test_wpplx_thread_switch'),
            (loop, "max(postcheck_seconds, 20)", "postcheck_seconds", 'test_wpplx_submit_latency'),
            (loop, "not stop_seen and clock() - sent_at >= 30", "not stop_seen", 'test_wpplx_idle_consecutive'),
            (loop, "node.get('role') != 'StaticText'", "False", 'test_wpplx_viewport_transcripts'),
            (loop, "if not started:", "if False:", 'test_wpplx_viewport_transcripts'),
            (loop, "if not duration:", "if False:", 'test_wpplx_viewport_transcripts'),
            (loop, "if len(value) >= 120 or node.get('truncated') is True:", "if False:", 'test_wpplx_viewport_transcripts'),
            (loop, "if total > 65536 or len(seen) >= 4096:", "if False:", 'test_wpplx_viewport_transcripts'),
            (loop, "if page == 59:", "if False:", 'test_wpplx_viewport_transcripts'),
            (loop, "if seen[ident] != value:", "if False:", 'test_wpplx_viewport_transcripts'),
            (loop, "(bar is not None and started) or stagnant >= 2", "bar is not None and started", 'test_wpplx_viewport_transcripts'),
            (loop, "send({'kind': 'key', 'keysym': 'BackSpace'})", "pass", 'test_wpplx_viewport_transcripts'),
            (loop, "send({'kind': 'key', 'keysym': 'Home'})", "pass", 'test_wpplx_viewport_transcripts'),
            (loop, "'detail': (str(exc) if isinstance(exc, BackendError) and re.fullmatch(r'[A-Z][A-Z0-9_]{2,63}', str(exc)) else type(exc).__name__)", "'detail': str(exc)", 'test_wpplx_closed_failure_codes'),
        ])
        cases.extend([
            (loop, 'except (_SurfaceNotReady, _SurfaceRefused):', 'except ():', 'test_wpplx_read_retries'),
            (loop, 'if attempt == 14 or clock() >= deadline:', 'if clock() >= deadline:', 'test_wpplx_read_retries'),
        ])
        cases.extend([
            (loop, "send({'kind': 'key', 'keysym': 'ctrl+a'})", 'pass', 'test_wpplx_viewport_transcripts'),
            (identity, "if bounds[0] <= x <= bounds[0] + bounds[2] and bounds[1] <= y <= bounds[1] + bounds[3]:", 'if False:', 'test_wpplx_viewport_margin'),
            (identity, "or target.get('region') != 'main'", "or False", 'test_wpplx_viewport_margin'),
        ])
        cases.extend([
            (loop, "e.get('visible', True)", "e.get('visible', False)", 'test_wpplx_focus_and_length_are_target_specific'),
            (loop, "_EMPTY_COMPOSER = (0, 1)", "_EMPTY_COMPOSER = (0,)", 'test_wpplx_focus_and_length_are_target_specific'),
            (loop, "send({'kind': 'key', 'keysym': 'Page_Down'})", 'pass', 'test_wpplx_viewport_transcripts'),
            (loop, "if page == 0 and not any(", "if False and not any(", 'test_wpplx_viewport_transcripts'),
            (loop, "(current.get('window') or {}).get('url', current.get('url')) != origin", 'False', 'test_wpplx_viewport_transcripts'),
            (loop, "if ident in preamble:", "if False:", 'test_wpplx_viewport_transcripts'),
            (loop, "all(_eligible(e) for e in rows if", "all(True for e in rows if", 'test_wpplx_viewport_transcripts'),
            (loop, "candidates[0] if len(candidates) == 1 else None", "candidates[0] if candidates else None", 'test_wpplx_viewport_transcripts'),
        ])
        cases.append((loop, 'not stop_seen and clock() - sent_at >= 30',
                      'False', 'test_wpplx_idle_consecutive'))
        cases.extend([
            (loop, "if len(value) >= 120 or node.get('truncated') is True:", 'if False:', 'test_wpplx_viewport_transcripts'),
            (loop, "re.sub(r'[^a-z0-9]', '', value.lower()) == normalized", 'value == task', 'test_wpplx_viewport_transcripts'),
            (loop, 'stagnant >= 2', 'stagnant >= 1', 'test_wpplx_viewport_transcripts'),
            (loop, "rows = rows[:cut]", 'rows = rows', 'test_wpplx_viewport_transcripts'),
        ])
        cases.append((loop, 'return _generating(ax)', 'return False', 'test_wpplx_viewport_transcripts'))
        cases.extend([
            (loop, "if (bar is not None and started) or stagnant >= 2:",
             "if stagnant >= 2:", 'test_wpplx_viewport_transcripts'),
            (loop, "node.get('actionable') is True or not isinstance(node.get('box'), list) or",
             "node.get('actionable') is True or not isinstance(node.get('box'), list) or node['box'][1] >= composer['box'][1] or",
             'test_wpplx_viewport_transcripts'),
            (identity, "y = (143 + min(frame['height'], min((e['box'][1] for e in composers), default=143))) // 2",
             "y = box[1] + box[3] / 2", 'test_wpplx_viewport_margin'),
            (loop, "send({'kind': 'key', 'keysym': 'Page_Down'})",
             "send({'kind': 'key', 'keysym': 'End'})", 'test_wpplx_viewport_transcripts'),
        ])
        cases.extend([
            (loop, "if action['kind'] == 'harvest' and thread_url is not None and",
             "if False and thread_url is not None and", 'test_wpplx_viewport_transcripts'),
            (loop, "if not isinstance(composer_cid, str) or not composer_cid:",
             "if False:", 'test_wpplx_viewport_transcripts'),
        ])
        cases.extend([
            (loop, "current_ids = {e.get('id') for e in rows if e.get('role') == 'StaticText'} - {None}",
             "current_ids = set()", 'test_wpplx_viewport_transcripts'),
            (loop, "bar = _web_viewport_bar(dict(current, elements=rows))",
             "bar = _web_viewport_bar(dict(current, elements=[e for e in rows if e['box'][1] < composer['box'][1]]))",
             'test_wpplx_viewport_transcripts'),
            (loop, "re.fullmatch(r'(?:\\d+m )?\\d+s', value)",
             "re.fullmatch(r'\\d+s', value)", 'test_wpplx_viewport_transcripts'),
        ])
        for index, (module, old, new, test) in enumerate(cases):
            with self.subTest(control=index, test=test):
                source = Path(module.__file__).read_text()
                self.assertIn(old, source)
                mutant = types.ModuleType(module.__name__ + '_wpplx_mutant')
                mutant.__package__ = module.__package__
                sys.modules[mutant.__name__] = mutant
                try:
                    exec(compile(source.replace(old, new), '<wpplx-mutant>', 'exec'), mutant.__dict__)
                    outcome = unittest.TestResult()
                    if module is loop:
                        with patch(__name__ + '.run_web_cycle', mutant.run_web_cycle):
                            WebUnit(test).run(outcome)
                    elif module is memory_module:
                        with patch.multiple(memory_module, validate_memory=mutant.validate_memory,
                                            MemoryRejected=mutant.MemoryRejected):
                            WebUnit(test).run(outcome)
                    else:
                        with patch(__name__ + '.SrfIdentitySurface', mutant.SrfIdentitySurface), patch(
                                __name__ + '.last_answer_container', mutant.last_answer_container), patch.multiple(
                                    identity, last_answer_container=mutant.last_answer_container,
                                    _terminal_bar=mutant._terminal_bar, _generating=mutant._generating,
                                    SurfaceRefused=mutant.SurfaceRefused):
                            WebUnit(test).run(outcome)
                    self.assertTrue(outcome.failures, 'mutant survived or only errored: ' + str(outcome.errors))
                    print('WPPLX MUTANT', index, 'KILLED', test)
                finally:
                    sys.modules.pop(mutant.__name__, None)

    def test_local_http_request_without_network(self):
        from unittest.mock import MagicMock
        response = MagicMock()
        response.__enter__.return_value.read.return_value = b'{"choices":[{"message":{"content":"{\\"id\\":\\"abstain\\"}"}}]}'
        opener = MagicMock()
        opener.open.return_value = response
        with patch('urllib.request.build_opener', return_value=opener):
            backend = LocalBackend('http://localhost/full/path', self.memory)
            self.assertEqual(backend.choose('ask', []), 'abstain')
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, 'http://localhost/full/path')
        self.assertEqual(json.loads(request.data)['max_tokens'], 32)
        self.assertNotIn('Authorization', request.headers)

    # @anchor:wa.tests.cu_web_unit.WebUnit.test_cli_muse_and_analysis
    def test_cli_muse_and_analysis(self):
        planner = FakePlanner()
        planner.analyze = lambda answer: 'Analysis of answer'
        clock = Clock()
        cycle = lambda *args, **kwargs: run_web_cycle(*args, **kwargs, clock=clock, sleep=clock.sleep)
        with tempfile.TemporaryDirectory(dir='/dev/shm') as directory:
            path = Path(directory) / 'memory.json'
            path.write_text(json.dumps(self.memory))
            with patch('tools.cu_web.run_web_cycle', side_effect=cycle), patch('tools.cu_web.MuseBackend', return_value=planner), patch('tools.cu_web.SrfIdentitySurface', return_value=FakeSurface()), patch('sys.stdout', new_callable=io.StringIO) as out:
                rc = main(['--surface', 'srf-identity', '--identity', 'test', '--task', 'Question', '--executor', 'muse', '--memory', str(path), '--analyze'])
            self.assertEqual(rc, 0)
            self.assertEqual(json.loads(out.getvalue())['analysis'], 'Analysis of answer')

    def test_wrong_site_is_unknown_without_input(self):
        surface = FakeSurface()
        original = surface.ax
        def ax():
            value = original()
            value['url'] = 'https://unrelated.invalid/'
            return value
        surface.ax = ax
        result, _, _, _ = self.cycle(surface)
        self.assertFalse(result['goal_verified'])
        self.assertEqual(result['escalations'][0]['reason'], 'unknown_screen')
        self.assertEqual(surface.calls, [])
