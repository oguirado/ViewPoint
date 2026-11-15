# @module {"id":"wa.tests.cu_web3_unit","layer":2,"owner":"LEARN","deps":["wa.learn.cu_loop","wa.learn.cu_tasks","wa.perceive.srf_identity","wa.tests.cu_web_unit","wa.tools.cu_web"],"book":"HB-WA-s4-learn"}
"""Final identity CLI contracts, using fake processes and RAM fixtures only."""
import copy
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from tools.cu_web import main
from tests.test_cu_web_unit import FakeSurface, FakeExecutor, FakePlanner, element, completed
from wender.learn.cu_tasks import web_predicate
from wender.perceive.srf_identity import (
    BINARY, CLI_MAPPING, SrfIdentitySurface, SurfaceError, SurfaceNotReady,
    SurfaceRefused, ax_options, last_answer_container, load_identity_env)

FRAME = dict(path='/dev/shm/fake.png', seq=12, sha256_rgb24='a' * 64,
             width=1280, height=900, capture_mono_ns=100, generation=1,
             window=dict(id='w1', x=0, y=0, w=1280, h=900, title='Ask',
                         url='https://www.perplexity.ai/'))
AX = dict(window=FRAME['window'], frame_seq=12, scroll={'x': 0, 'y': 0},
          elements=[element(container='ab')], truncated=False, skipped_frames=0)
INPUT = dict(focus_verified=True, input_mono_ns=101)


class FinalCLIUnit(unittest.TestCase):
    def test_every_argv_and_attach_without_lifecycle_process(self):
        payloads = [FRAME, AX, dict(AX, text={'container_id': 'ab', 'bytes': 2, 'text': 'é'}),
                    INPUT, INPUT, INPUT, INPUT]
        responses = [subprocess.CompletedProcess([], 0, json.dumps(p).encode()) for p in payloads]
        with patch('wender.perceive.srf_identity.load_identity_env', return_value={'SRF_PROXY_REGISTRY': 'fake', 'SRF_IDENTITY_STORE_ROOT': 'fake'}), patch('wender.perceive.srf_identity.subprocess.run', side_effect=responses) as run:
            surface = SrfIdentitySurface(container='capsule')
            with surface:
                run.assert_not_called()
                surface.frame()
                surface.ax()
                self.assertEqual(surface.text('ab'), 'é')
                surface.input({'kind': 'click', 'element': AX['elements'][0]})
                surface.input({'kind': 'click', 'element': AX['elements'][0], 'button': 'right'})
                surface.input({'kind': 'type', 'text': 'ASCII $(literal)'})
                surface.input({'kind': 'key', 'keysym': 'Return'})
            common = ['--container', 'capsule', '--window', 'w1']
            expected = [[BINARY, 'frame', *common], [BINARY, 'ax', *common],
                        [BINARY, 'ax', *common, '--text-container', 'ab'],
                        [BINARY, 'input', *common, '--click', '30,30'],
                        [BINARY, 'input', *common, '--click', '30,30', '--button', 'right'],
                        [BINARY, 'input', *common, '--type', 'ASCII $(literal)'],
                        [BINARY, 'input', *common, '--key', 'Return']]
            self.assertEqual([c.args[0] for c in run.call_args_list], expected)
            self.assertEqual(set(CLI_MAPPING), {'frame', 'ax', 'text', 'click', 'type', 'key', 'scroll', 'drag'})
            for call in run.call_args_list:
                self.assertFalse(call.kwargs['check'])
                self.assertEqual(set(call.kwargs['env']), {'SRF_PROXY_REGISTRY', 'SRF_IDENTITY_STORE_ROOT', 'PATH'})   # PATH only, for docker/xdotool

    def test_all_exit_codes_and_no_replay(self):
        for code, label, error in ((2, 'usage', SurfaceError), (3, 'config_proxy', SurfaceError),
                                   (4, 'not_ready', SurfaceNotReady), (5, 'malformed_capture', SurfaceError),
                                   (6, 'refused_focus', SurfaceRefused), (99, 'producer_failure', SurfaceError)):
            with self.subTest(code=code), patch('wender.perceive.srf_identity.load_identity_env', return_value={}), patch('wender.perceive.srf_identity.subprocess.run', return_value=subprocess.CompletedProcess([], code, b'private', b'private')) as run:
                with self.assertRaises(error) as caught:
                    SrfIdentitySurface().input({'kind': 'key', 'keysym': 'Return'})
                self.assertEqual(caught.exception.code, label)
                self.assertNotIn('private', str(caught.exception))
                run.assert_called_once()

    def test_env_allowlist_literal_and_missing_config(self):
        with tempfile.TemporaryDirectory(dir='/dev/shm') as directory:
            path = Path(directory) / 'fake.env'
            path.write_text('# comment\nSRF_IDENTITY_STORE_ROOT="/fake/store"\nSRF_PROXY_REGISTRY=\'/fake/$(literal)\'\nIGNORED=fake-secret\n')
            self.assertEqual(load_identity_env(path), {'SRF_IDENTITY_STORE_ROOT': '/fake/store', 'SRF_PROXY_REGISTRY': '/fake/$(literal)'})
            path.write_text('SRF_IDENTITY_STORE_ROOT=/fake\n')
            with self.assertRaises(SurfaceError): load_identity_env(path)

    def test_json_line_and_object_fail_closed(self):
        for body in (b'', b'{}\n{}', b'[]', b'bad'):
            with self.subTest(body=body), patch('wender.perceive.srf_identity.load_identity_env', return_value={}), patch('wender.perceive.srf_identity.subprocess.run', return_value=subprocess.CompletedProcess([], 0, body)):
                with self.assertRaises(SurfaceError): SrfIdentitySurface()._call('ax')

    def test_frame_before_ax_and_one_recovery_then_closed(self):
        for recover in (True, False):
            calls = []
            def transport(op, payload):
                calls.append(op)
                if op == 'frame': return copy.deepcopy(FRAME)
                seq = 12 if recover and calls.count('ax') == 2 else 11
                return dict(copy.deepcopy(AX), frame_seq=seq)
            trace = io.StringIO()
            surface = SrfIdentitySurface(transport=transport, trace=trace)
            if recover:
                self.assertEqual(surface.ax()['frame_seq'], 12)
            else:
                with self.assertRaises(SurfaceError): surface.ax()
            self.assertEqual(calls, ['frame', 'ax', 'frame', 'ax'])
            self.assertIn('frame_seq_mismatch', trace.getvalue())
            self.assertNotIn('Ask', trace.getvalue())

    def test_all_actionable_boxes_inside_window_even_blocked_controls(self):
        for box in ([-1, 0, 10, 10], [0, -1, 10, 10], [1270, 0, 11, 10],
                    [0, 890, 10, 11], [0, 0, 0, 1], [0, 0, float('nan'), 1], None):
            with self.subTest(box=box):
                ax = copy.deepcopy(AX)
                ax['elements'][0].update(name='Settings', box=box)
                surface = SrfIdentitySurface(transport=lambda op, p: FRAME if op == 'frame' else ax)
                with self.assertRaises(SurfaceError): surface.ax()
                self.assertEqual(surface.violations[-1]['code'], 'actionable_box_outside_window')

    def test_window_mismatch(self):
        ax = dict(AX, window=dict(FRAME['window'], id='w2'))
        surface = SrfIdentitySurface(transport=lambda op, p: FRAME if op == 'frame' else ax)
        with self.assertRaises(SurfaceError): surface.ax()
        self.assertEqual(surface.violations[-1]['code'], 'window_mismatch')

    def test_text_nested_bytes_hex_and_sequence_alignment(self):
        for text in ({'container_id': 'ab', 'bytes': 1, 'text': 'é'},
                     {'container_id': 'cd', 'bytes': 2, 'text': 'é'},
                     {'container_id': 'ab', 'bytes': 65538, 'text': 'é' * 32769}, 'raw'):
            surface = SrfIdentitySurface(transport=lambda op, p: FRAME if op == 'frame' else dict(AX, text=text))
            with self.assertRaises(SurfaceError): surface.text('ab')
        surface = SrfIdentitySurface(transport=lambda *a: self.fail('unexpected call'))
        for bad in ('', 'answer', '--flag'):
            with self.assertRaises(ValueError): surface.text(bad)

    def test_false_or_missing_focus_is_refused(self):
        for value in ({}, {'focus_verified': False, 'input_mono_ns': 100}):
            surface = SrfIdentitySurface(transport=lambda *a: value)
            with self.assertRaises(SurfaceRefused): surface.input({'kind': 'type', 'text': 'fake'})
        surface = SrfIdentitySurface(transport=lambda *a: {'focus_verified': True})
        with self.assertRaises(SurfaceError): surface.input({'kind': 'key', 'keysym': 'Return'})

    def test_projection_null_container_and_states_predicates(self):
        row = element(role='textbox', value_len=2)
        row['container'] = None
        row['focused'] = True
        row['states'] = dict(enabled=True, focused=True, expanded=None, checked=False, selected=None)
        options, _ = ax_options({'elements': [row]})
        self.assertEqual(options[0]['value_len'], 2)
        self.assertEqual(options[0]['states'], row['states'])
        self.assertIsNone(options[0]['container_ordinal'])
        ax = dict(AX, elements=[row])
        self.assertTrue(web_predicate({'kind': 'value_nonempty', 'role': 'textbox', 'states': {'focused': True}}, ax))
        self.assertTrue(web_predicate({'kind': 'url', 'pattern': 'perplexity'}, ax))
        self.assertTrue(web_predicate({'kind': 'title', 'pattern': '^Ask'}, ax))
        self.assertTrue(web_predicate({'kind': 'container_count_increased'}, {'elements': [row] + completed()}, before={'elements': [row]}))

    def test_projection_accepts_null_states(self):
        row = element()
        row['states'] = None
        options, targets = ax_options({'elements': [row]})
        self.assertEqual(options[0]['name'], 'Ask')
        self.assertTrue(all(value is None for value in options[0]['states'].values()))
        self.assertEqual(targets, {'e0': row})

    def test_unknown_state_is_not_evidence_of_absence(self):
        for states in ({}, {'focused': None}):
            row = element()
            row['states'] = states
            for kind in ('element_present', 'element_absent', 'value_nonempty'):
                self.assertIsNone(web_predicate({'kind': kind, 'states': {'focused': True}}, {'elements': [row]}))

    # @anchor:wa.tests.cu_web3_unit.FinalCLIUnit.test_final_adapter_cycle_with_fake_producer
    def test_final_adapter_cycle_with_fake_producer(self):
        from tools.cu_web import MEMORY
        from wender.learn.cu_loop import run_web_cycle
        fake, calls = FakeSurface(), []
        seq, busy_reads = 0, 0
        answer = 'ANSWER_CANARY complete paragraph with repeated evidence.'
        def transport(op, payload):
            nonlocal seq, busy_reads
            calls.append(op)
            if op == 'frame':
                seq += 1
                window = dict(FRAME['window'], w=640, h=450, url='https://www.perplexity.ai/search/current' if fake.phase == 'done' else 'https://www.perplexity.ai/')
                return dict(copy.deepcopy(FRAME), seq=seq, width=640, height=450, window=window)
            if op in ('ax', 'text'):
                rows = [e for e in fake.ax()['elements'] if e['role'] == 'textbox']
                rows[0].update(name='Ask anything', box=[20, 380, 600, 50])
                rows[0]['container'] = {'id': 'cd', 'ordinal': 0}
                if fake.phase == 'done':
                    if busy_reads < 2:
                        rows.append(element('Stop response', container='ab'))
                        busy_reads += 1
                    else:
                        rows += [element(' '.join(fake.submitted.split()), role='heading', container='ab', actionable=False),
                                 element('Finished 2s', container='ab'),
                                 element('Copy', container='ab'), element('Helpful', container='ab')]
                window = dict(FRAME['window'], w=640, h=450, url='https://www.perplexity.ai/search/current' if fake.phase == 'done' else 'https://www.perplexity.ai/')
                result = dict(copy.deepcopy(AX), elements=rows, frame_seq=seq, window=window)
                if op == 'text':
                    self.assertEqual(payload['text_container'], 'ab')
                    result['text'] = {'container_id': 'ab', 'bytes': len(answer.encode()), 'text': answer}
                return result
            fake.input(dict(kind=op, **payload))
            return INPUT
        surface = SrfIdentitySurface(transport=transport)
        trace = io.StringIO()
        from tests.test_cu_web_unit import _TrustedWebIntegration
        result = run_web_cycle(surface, FakePlanner(), FakeExecutor(), json.loads(MEMORY.read_text()), task='fake', trace=trace,
                               integration=_TrustedWebIntegration())
        self.assertTrue(result['goal_verified'], result)
        self.assertEqual(result['harvested'], answer)
        self.assertTrue(result['prompt_matches'])
        for key in ('t_home_ready', 't_composer_found', 't_typed', 't_sent_verified',
                    't_done_detected', 't_harvest_done', 'agent_ms', 'answer_wait_ms', 'harvest_ms'):
            self.assertGreaterEqual(result[key], 0)
        self.assertEqual(result['model_calls'], 5)
        self.assertEqual(result['inputs_sent'], 7)
        self.assertEqual(result['wrong_or_refused_inputs'], 0)
        self.assertEqual(result['recoveries'], 0)
        for i, op in enumerate(calls):
            if op == 'ax':
                self.assertEqual(calls[i - 1], 'frame')
        self.assertEqual(calls.count('text'), 1)
        self.assertNotIn('PRIVATE HISTORY', trace.getvalue())
        self.assertNotIn(answer, trace.getvalue())

    def test_answer_ordinal_beats_traversal_order(self):
        newer = [dict(e, container={'id': 'bb', 'ordinal': 9}) for e in completed()]
        older = [dict(e, container={'id': 'aa', 'ordinal': 2}) for e in completed()]
        self.assertEqual(last_answer_container({'elements': newer + older}), 'bb')
        self.assertIsNone(last_answer_container({'elements': newer[:1] + older}))

    def test_dry_check_only_frame_ax_no_models_memory_or_lifecycle(self):
        surface = SrfIdentitySurface(transport=lambda op, p: FRAME if op == 'frame' else AX)
        with patch('tools.cu_web.SrfIdentitySurface', return_value=surface) as constructor, patch('tools.cu_web.MuseBackend') as model, patch('sys.stdout', new_callable=io.StringIO) as output:
            self.assertEqual(main(['--dry-check', '--container', 'custom', '--window', 'w1', '--identity-env', '/fake/env']), 0)
            model.assert_not_called()
            self.assertEqual(constructor.call_args.kwargs['container'], 'custom')
            self.assertEqual(constructor.call_args.kwargs['identity_env'], Path('/fake/env'))
            counts = json.loads(output.getvalue())
            self.assertEqual(counts['elements'], 1)
            self.assertEqual(counts['actionable'], 1)
            self.assertFalse(surface.started)

    def test_dry_check_mismatch_never_refetches(self):
        calls = []
        def transport(op, p):
            calls.append(op)
            return FRAME if op == 'frame' else dict(AX, frame_seq=11)
        with patch('tools.cu_web.SrfIdentitySurface', return_value=SrfIdentitySurface(transport=transport)), patch('sys.stdout', new_callable=io.StringIO):
            self.assertEqual(main(['--dry-check']), 2)
        self.assertEqual(calls, ['frame', 'ax'])

    def test_refused_input_terminates_cycle_without_retry(self):
        from tools.cu_web import MEMORY
        from wender.learn.cu_loop import run_web_cycle
        surface = FakeSurface()
        def refused(action):
            surface.calls.append(action)
            raise SurfaceRefused('refused_focus', 'click')
        surface.input = refused
        result = run_web_cycle(surface, FakePlanner(), FakeExecutor(), json.loads(MEMORY.read_text()), task='fake', trace=io.StringIO())
        self.assertFalse(result['goal_verified'])
        self.assertEqual(result['failure']['error_class'], 'SurfaceRefused')
        self.assertEqual(len(surface.calls), 1)
        self.assertTrue(surface.stopped)


if __name__ == '__main__':
    unittest.main()
