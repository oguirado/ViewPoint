# @module {"id":"wa.tests.mw_locate_ai_unit","layer":4,"owner":"LEARN","deps":["wa.learn.mw_owners_srf","wa.tests.mw_owners_srf_unit","wa.learn.mw_locate_ai","wa.learn.model_client","wa.learn.mw_workflow","wa.perceive.vision_bridge","wa.scripts.types"],"book":"HB-WA-s4-learn"}
"""Public-API controls for the item-local locator; real runner and synthetic composition controls."""
from dataclasses import replace
import json
import inspect
import textwrap
import os
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from wender.learn import mw_locate_ai as ai
from wender.learn import mw_owners_srf as srf
from wender.learn import mw_workflow as workflow
from tests import test_mw_owners_srf_unit as composition
from wender.learn.model_client import MuseOutput, parse_muse_output
from wender.learn.mw_workflow import ObservationValue, GroundedTarget, RunState, StepState
from wender.perceive.vision_bridge import DescribeResult, Mark
from wender.scripts.types import AdmissionCheck, Truth, Reason, ContractError

YES = AdmissionCheck(Truth.TRUE, None)
NO = AdmissionCheck(Truth.FALSE, Reason.CONFLICT)


def _policy():
    return ai.LocatePolicy(3, 8, 2000, 20000, 20000, 1000, 10000, 200000, 10000,
                           1_000_000_000, 1_000_000, 100_000_000, 100_000_000, 10**20)


class _Owner:
    scope = 'scope'
    authority = 'authority'
    run_deadline_ns = 1000

    def __init__(self):
        self.capability_owner = object()
        self.classification = 'miss'
        self.policy_ok = self.publication_ok = self.support_ok = self.release_ok = YES
        self.acquired = self.released = 0
        observation = ObservationValue(self.scope, self.authority, 'current', 10, 11,
                                        'cap', 'rev', 'marks', 'px', None, True, None)
        description = DescribeResult('fixture', 'digest', 100, 100, False, (), (), 0,
                                     (Mark(7, 20, 30, 10, 8, 'button', 1.0),))
        self.evidence = ai.LocateEvidence(observation, description, None, None, 'binding',
                                          'Submit', YES, YES, object())
        self.forge = False

    # @anchor:wa.learn.mw_locate_ai._Owner.classify
    def classify(self, target, state):
        return self.classification

    # @anchor:wa.learn.mw_locate_ai._Owner.validate_policy
    def validate_policy(self, policy, state):
        return self.policy_ok

    # @anchor:wa.learn.mw_locate_ai._Owner.acquire
    def acquire(self, target, state, policy, deadline):
        self.acquired += 1
        return self.evidence

    # @anchor:wa.learn.mw_locate_ai._Owner.revalidate
    def revalidate(self, evidence, state, deadline):
        return self.publication_ok

    # @anchor:wa.learn.mw_locate_ai._Owner.ground
    def ground(self, target, evidence, mark, point, state, deadline):
        return GroundedTarget(point, 'destination', evidence.observation, self.scope, self.authority,
                              YES, object() if self.forge else self.capability_owner, deadline, (0, 0))

    # @anchor:wa.learn.mw_locate_ai._Owner.validate_target
    def validate_target(self, target, value, evidence, state):
        return self.support_ok

    # @anchor:wa.learn.mw_locate_ai._Owner.release
    def release(self, evidence, deadline):
        self.released += 1
        return self.release_ok


class _Transport:
    def __init__(self):
        self.calls = self.drains = 0
        self.output = MuseOutput({'mark_id': 7}, 0, {'input_tokens': 3, 'output_tokens': 2})
        self.drain_ok = YES
        self.callback = lambda: None

    # @anchor:wa.learn.mw_locate_ai._Transport.generate
    def generate(self, payload, evidence, policy, deadline, clock):
        self.calls += 1
        self.payload = payload
        self.callback()
        if isinstance(self.output, Exception):
            raise self.output
        return self.output

    # @anchor:wa.learn.mw_locate_ai._Transport.cancel_and_drain
    def cancel_and_drain(self, policy, clock):
        self.drains += 1
        return self.drain_ok


# @anchor:wa.learn.mw_locate_ai.TestLocate
class TestLocate(unittest.TestCase):
    # @anchor:wa.learn.mw_locate_ai.TestLocate.setUp
    def setUp(self):
        self.now = 20
        self.run = RunState(b'', 'digest', {}, 'scope', 'authority', ())
        self.state = StepState(0, self.run, 1000, 900, 1000, start_ns=10)
        self.run.current_step = self.state
        self.owner, self.transport = _Owner(), _Transport()
        self.policy, self.accounting = _policy(), ai.LocateAccounting(self.run)
        self.target = {'rung': 'T1', 'recorded_point': [99, 99], 'private': 'PRIVATE_CANARY'}

    def _call(self):
        return ai.locate_once(self.target, step_state=self.state, policy=self.policy,
                              evidence_owner=self.owner, transport=self.transport,
                              accounting=self.accounting, clock=lambda: self.now)

    # @anchor:wa.learn.mw_locate_ai.TestLocate.test_positive_relayout_and_accounting
    def test_positive_relayout_and_accounting(self):
        for classification in ('miss', 'ambiguity', 'invariant'):
            self.setUp()
            self.owner.classification = classification
            result = self._call()
            self.assertEqual(result.grounded_target.point, (25, 34))
            self.assertIs(result.grounded_target.support_owner, self.owner.capability_owner)
            self.assertEqual(result.accounting_snapshot.model_calls, 1)
            self.assertEqual(result.accounting_snapshot.tokens_used, 5)
            self.assertEqual(self.owner.released, 1)
            self.assertNotIn('PRIVATE_CANARY', json.dumps(self.transport.payload))
            self.assertEqual(self.target['recorded_point'], [99, 99])

    def _fixture(self, label):
        e = self.owner.evidence
        if label == 'configuration': self.policy = replace(self.policy, max_calls=True)
        elif label == 'resource_bounds': self.policy = replace(self.policy, peak_bytes=1)
        elif label == 'account_owner': self.accounting.run = object()
        elif label == 'predispatch': self.run.uncertain = True
        elif label == 'deadline': self.now = 900
        elif label == 'deadlines': self.state.deadline_ns = True
        elif label == 'ineligible': self.owner.classification = 'privacy'
        elif label == 'budget': self.accounting.attempted_steps.add(0)
        elif label == 'envelope': self.owner.policy_ok = NO
        elif label == 'approved_evidence': self.owner.evidence = replace(e, export_admission=NO)
        elif label == 'freshness': self.run.last_input_ns = 10
        elif label == 'inventory': self.owner.evidence = replace(e, description=replace(e.description, mark_cap_reached=True))
        elif label == 'geometry': self.owner.evidence = replace(e, description=replace(e.description, marks=(replace(e.description.marks[0], w=-1),)))
        elif label == 'semantic': self.owner.evidence = replace(e, semantic_target='')
        elif label == 'publication': self.owner.publication_ok = NO
        elif label == 'request_bound': self.policy = replace(self.policy, request_bytes=1)
        elif label == 'answer': self.transport.output = MuseOutput({'mark_id': True}, 0)
        elif label == 'refused': self.transport.output = MuseOutput({'mark_id': None}, 0)
        elif label == 'membership': self.transport.output = MuseOutput({'mark_id': 999}, 0)
        elif label == 'grounding': self.owner.forge = True
        elif label == 'independent_support': self.owner.support_ok = NO
        elif label == 'drain': self.transport.drain_ok = NO
        elif label == 'release': self.owner.release_ok = NO
        else: raise AssertionError(label)

    def _assert_rejected(self, label):
        self._fixture(label)
        result = self._call()
        self.assertIsNone(result.grounded_target)
        self.assertEqual(result.choice.detail, 'cleanup' if label in ('drain', 'release') else label)
        if label in ('drain', 'release'):
            self.assertTrue(self.accounting.poisoned)
            self.assertIs(self.accounting.retained_evidence, self.owner.evidence)
        if label == 'drain': self.assertEqual(self.owner.released, 0)

    # @anchor:wa.learn.mw_locate_ai.TestLocate.test_gates_and_check_removal_mutants
    def test_gates_and_check_removal_mutants(self):
        labels = ('configuration', 'resource_bounds', 'account_owner', 'predispatch', 'deadline',
                  'deadlines', 'ineligible', 'budget', 'envelope', 'approved_evidence', 'freshness',
                  'inventory', 'geometry', 'semantic', 'publication', 'request_bound', 'answer',
                  'refused', 'membership', 'grounding', 'independent_support', 'drain', 'release')
        original = ai._need
        for label in labels:
            with self.subTest(gate=label):
                self.setUp()
                self._assert_rejected(label)
                self.setUp()
                def mutant(condition, detail, reason=Reason.UNKNOWN):
                    if detail != label:
                        original(condition, detail, reason)
                with patch.object(ai, '_need', mutant):
                    with self.assertRaises(AssertionError, msg='surviving check-removal mutant: ' + label):
                        self._assert_rejected(label)

    # @anchor:wa.learn.mw_locate_ai.TestLocate.test_safe_state_and_eligibility_matrix
    def test_safe_state_and_eligibility_matrix(self):
        for change in (lambda: setattr(self.state, 'dispatch_disposition', 'UNKNOWN'),
                       lambda: setattr(self.state, 'input_ns', 12),
                       lambda: setattr(self.run, 'current_step', None)):
            self.setUp(); change()
            self.assertEqual(self._call().choice.detail, 'predispatch')
            self.assertEqual(self.owner.acquired, 0)
        for classification in ('success', 'privacy', 'custody', 'authority', 'schema', 'unsupported'):
            self.setUp(); self.owner.classification = classification
            self.assertIsNone(self._call().grounded_target)
            self.assertEqual(self.transport.calls, 0)

    # @anchor:wa.learn.mw_locate_ai.TestLocate.test_closed_answer_and_parser
    def test_closed_answer_and_parser(self):
        for value, extras in (({'mark_id': '7'}, 0), ({'mark_id': 7, 'x': 1}, 0),
                              ({'mark_id': 7}, 1), ({}, 0)):
            self.setUp(); self.transport.output = MuseOutput(value, extras)
            self.assertEqual(self._call().choice.detail, 'answer')
        for text in ('{"mark_id":7,"mark_id":8}', 'garbage', '', '{"mark_id":7}\ninstructions'):
            self.setUp()
            def parse():
                return parse_muse_output(json.dumps({'payload_type': 'run.output.delta', 'payload': {'text': text}}))
            self.transport.generate = lambda *args: parse()
            self.assertIsNone(self._call().grounded_target)
        # JSON whitespace, including a trailing newline, is legal; a newline in an ID is not.
        self.setUp(); self.transport.output = MuseOutput({'mark_id': '7\n'}, 0)
        self.assertEqual(self._call().choice.detail, 'answer')

    # @anchor:wa.learn.mw_locate_ai.TestLocate.test_inventory_and_publication_controls
    def test_inventory_and_publication_controls(self):
        # A once-valid ID from an earlier publication is absent from this frame.
        earlier = replace(self.owner.evidence.description,
            marks=(replace(self.owner.evidence.description.marks[0], id=8),))
        self.assertEqual(earlier.marks[0].id, 8)
        self.transport.output = MuseOutput({'mark_id': earlier.marks[0].id}, 0)
        self.assertEqual(self._call().choice.detail, 'membership')
        for marks in ((Mark(7, 0, 0, 1, 1, 'b', 1),) * 2,
                      (Mark(7, 99, 0, 2, 1, 'b', 1),),
                      (Mark(True, 0, 0, 1, 1, 'b', 1),)):
            self.setUp(); e = self.owner.evidence
            self.owner.evidence = replace(e, description=replace(e.description, marks=marks))
            self.assertEqual(self._call().choice.detail, 'geometry')
        self.setUp()
        self.transport.callback = lambda: setattr(self.owner, 'publication_ok', NO)
        self.assertEqual(self._call().choice.detail, 'publication')
        self.setUp(); e = self.owner.evidence
        self.owner.evidence = replace(e, observation=replace(e.observation, scope='foreign'))
        self.assertEqual(self._call().choice.detail, 'freshness')

    # @anchor:wa.learn.mw_locate_ai.TestLocate.test_extended_evidence_and_accounting_controls
    def test_extended_evidence_and_accounting_controls(self):
        for observation in (replace(self.owner.evidence.observation, capture_end_ns=21),
                            replace(self.owner.evidence.observation, complete=False),
                            replace(self.owner.evidence.observation, authority='foreign')):
            self.setUp()
            self.owner.evidence = replace(self.owner.evidence, observation=observation)
            self.assertEqual(self._call().choice.detail, 'freshness')
            self.assertEqual(self.transport.calls, 0)
        for field, value in (('custody_admission', NO), ('allocation', None), ('frame_binding', None),
                             ('export_admission', SimpleNamespace(result=Truth.TRUE, reason=None))):
            self.setUp()
            self.owner.evidence = replace(self.owner.evidence, **{field:value})
            self.assertEqual(self._call().choice.detail, 'approved_evidence')
        self.setUp()
        self.transport.output = MuseOutput({'mark_id':7}, 0,
            {'input_tokens':3, 'output_tokens':2, 'cached_tokens':3, 'reasoning_tokens':2})
        self.assertEqual(self._call().accounting_snapshot.tokens_used, 5)
        self.state.seq = 1
        self.assertEqual(self._call().accounting_snapshot.tokens_used, 10)
        self.setUp()
        self.transport.drain_ok = NO
        self.assertEqual(self._call().choice.detail, 'cleanup')
        self.assertEqual(self._call().choice.detail, 'account_owner')
        self.assertEqual(self.transport.calls, 1)

    # @anchor:wa.learn.mw_locate_ai.TestLocate.test_budget_polling_and_unknown_usage
    def test_budget_polling_and_unknown_usage(self):
        self.transport.output = MuseOutput({'mark_id': None}, 0)
        self.assertEqual(self._call().choice.detail, 'refused')
        self.assertEqual(self.accounting.attempts, 1)
        self.assertIsNone(self.accounting.snapshot().tokens_used)
        self.state.iteration += 1
        self.assertEqual(self._call().choice.detail, 'budget')
        self.assertEqual(self.transport.calls, 1)
        self.state.seq = 1
        self.transport.output = MuseOutput({'mark_id': 7}, 0, {'input_tokens': 3, 'output_tokens': 2})
        self.assertIsNotNone(self._call().grounded_target)
        self.assertIsNone(self.accounting.snapshot().tokens_used)
        self.state.seq = 2
        self.transport.output = RuntimeError('PRIVATE_CANARY')
        self.assertNotIn('PRIVATE_CANARY', repr(self._call()))
        self.state.seq = 3
        self.assertEqual(self._call().choice.detail, 'budget')
        self.assertEqual(self.accounting.attempts, 3)

    # @anchor:wa.learn.mw_locate_ai.TestLocate.test_concurrency_and_post_response_deadline
    def test_concurrency_and_post_response_deadline(self):
        self.accounting._lock.acquire()
        try:
            self.assertEqual(self._call().choice.detail, 'concurrent')
            self.assertEqual(self.transport.calls, 0)
        finally:
            self.accounting._lock.release()
        self.transport.callback = lambda: setattr(self, 'now', 901)
        self.assertEqual(self._call().choice.reason, Reason.DEADLINE)
        self.assertEqual(self.transport.drains, 1)

    # @anchor:wa.learn.mw_locate_ai.TestLocate.test_membership_semantic_mutant
    def test_membership_semantic_mutant(self):
        # Disable both the explicit guard and the lookup's implicit membership gate.
        source = textwrap.dedent(inspect.getsource(ai.locate_once))
        source = source.replace("output['mark_id'] in marks", 'True')
        source = source.replace("mark = marks[output['mark_id']]", 'mark = next(iter(marks.values()))')
        namespace = dict(vars(ai))
        exec(compile(source, '<membership-mutant>', 'exec'), namespace)
        with patch.object(ai, 'locate_once', namespace['locate_once']):
            with self.assertRaises(AssertionError):
                self._assert_rejected('membership')

    # @anchor:wa.learn.mw_locate_ai.TestLocate.test_accounting_and_relayout_implementation_mutants
    def test_accounting_and_relayout_implementation_mutants(self):
        cases = (
            (ai, 'locate_once', 'accounting.attempts += 1', 'accounting.attempts += 0',
             'test_budget_polling_and_unknown_usage'),
            (ai.LocateAccounting, 'snapshot', 'self.known_tokens if self.usage_complete else None',
             'self.known_tokens', 'test_budget_polling_and_unknown_usage'),
            (ai, 'locate_once', 'point = (mark.x + mark.w // 2, mark.y + mark.h // 2)',
             "point = tuple(target['recorded_point'])", 'test_positive_relayout_and_accounting'),
        )
        for owner, method, old, new, test in cases:
            source = textwrap.dedent(inspect.getsource(getattr(owner, method)))
            self.assertIn(old, source)
            namespace = dict(vars(ai))
            exec(compile(source.replace(old, new), '<locate-mutant>', 'exec'), namespace)
            with patch.object(owner, method, namespace[method]):
                result = unittest.TestResult()
                TestLocate(test).run(result)
            self.assertTrue(result.failures or result.errors, 'surviving mutant: '+old)


# @anchor:wa.learn.mw_locate_ai.TestTransport
class TestTransport(unittest.TestCase):
    # @anchor:wa.learn.mw_locate_ai.TestTransport.test_ram_paths_cli_bounds_and_cleanup
    def test_ram_paths_cli_bounds_and_cleanup(self):
        # Synthetic RAM-only assets. This executable never contacts a model.
        with tempfile.TemporaryDirectory(dir='/dev/shm') as directory:
            root = Path(directory)
            for name in ('frame', 'marks'):
                (root / name).write_bytes(b'fixture')
                (root / name).chmod(0o600)
            owner = _Owner()
            evidence = replace(owner.evidence, approved_frame_path=root/'frame', approved_marks_path=root/'marks')
            transport = ai.MuseLocateTransport(root, executable='/usr/bin/python3')
            self.addCleanup(transport.cancel_and_drain, _policy(), time.monotonic_ns)
            policy = replace(_policy(), absolute_deadline_ns=time.monotonic_ns()+10**10)
            real_popen = ai.subprocess.Popen
            seen = []
            def launch(argv, **kwargs):
                seen.extend(argv)
                prompt = Path(argv[argv.index('--prompt-file')+1])
                schema = Path(argv[argv.index('--output-schema')+1])
                self.assertEqual(json.loads(schema.read_text()), ai._SCHEMA)
                self.assertNotIn('PRIVATE_CANARY', prompt.read_text())
                event = json.dumps({'payload_type': 'run.output.delta', 'payload': {'text': '{"mark_id":7}'}})
                return real_popen(['/usr/bin/python3', '-c', 'print('+repr(event)+')'], **kwargs)
            with patch.object(ai.subprocess, 'Popen', launch):
                output = transport.generate(ai._payload(evidence), evidence, policy,
                                             time.monotonic_ns()+10**9, time.monotonic_ns)
            self.assertEqual(output['mark_id'], 7)
            self.assertEqual(transport.cancel_and_drain(policy, time.monotonic_ns), YES)
            with self.assertRaises(ai._Refusal) as caught:
                transport.generate({'history': 'PRIVATE_CANARY'}, evidence, policy,
                                   time.monotonic_ns()+10**9, time.monotonic_ns)
            self.assertEqual(caught.exception.detail, 'transport_export')
            for flag, expected in (('--model','muse-spark-1.3'), ('--reasoning-effort','medium'),
                                   ('--max-model-steps','1')):
                self.assertEqual(seen[seen.index(flag)+1], expected)
            self.assertIn('--disable-write', seen)
            self.assertIn('--disable-web-tools', seen)
            self.assertEqual(transport.cancel_and_drain(policy, time.monotonic_ns), YES)
            self.assertEqual(sorted(p.name for p in root.iterdir()), ['frame','marks'])
            def flood(argv, **kwargs):
                return real_popen(['/usr/bin/python3', '-c', 'import os; os.write(1,b"x"*100000)'], **kwargs)
            with patch.object(ai.subprocess, 'Popen', flood):
                with self.assertRaises(ai._Refusal) as caught:
                    transport.generate(ai._payload(evidence), evidence, policy, time.monotonic_ns()+10**9, time.monotonic_ns)
            self.assertEqual(caught.exception.detail, 'output_bound')
            self.assertEqual(transport.cancel_and_drain(policy, time.monotonic_ns), YES)
            (root/'escape').symlink_to('/etc/hosts')
            with self.assertRaises(ai._Refusal) as caught:
                transport.generate(ai._payload(evidence), replace(evidence, approved_frame_path=root/'escape'), policy,
                                   time.monotonic_ns()+10**9, time.monotonic_ns)
            self.assertEqual(caught.exception.detail, 'ram_containment')
            self.assertEqual(transport.cancel_and_drain(policy, time.monotonic_ns), YES)
        with tempfile.TemporaryDirectory() as persistent:
            with self.assertRaises(ai._Refusal):
                ai.MuseLocateTransport(persistent, executable='unused')

    # @anchor:wa.learn.mw_locate_ai.TestTransport.test_timeout_joins_owned_process
    def test_timeout_joins_owned_process(self):
        with tempfile.TemporaryDirectory(dir='/dev/shm') as directory:
            root = Path(directory)
            asset = root/'asset'; asset.write_bytes(b'x'); asset.chmod(0o600)
            evidence = replace(_Owner().evidence, approved_frame_path=asset, approved_marks_path=asset)
            transport = ai.MuseLocateTransport(root, executable='/usr/bin/python3')
            real_popen = ai.subprocess.Popen
            def launch(argv, **kwargs):
                return real_popen(['/usr/bin/python3', '-c', 'import time; time.sleep(10)'], **kwargs)
            with patch.object(ai.subprocess, 'Popen', launch):
                with self.assertRaises(ai._Refusal):
                    transport.generate(ai._payload(evidence), evidence, _policy(), time.monotonic_ns()+50_000_000, time.monotonic_ns)
            process = transport._process
            self.assertEqual(transport.cancel_and_drain(_policy(), time.monotonic_ns), YES)
            self.assertIsNotNone(process.poll())
            self.assertIsNone(transport._directory)

    # @anchor:wa.learn.mw_locate_ai.TestTransport.test_failed_join_retains_resources
    def test_failed_join_retains_resources(self):
        with tempfile.TemporaryDirectory(dir='/dev/shm') as directory:
            transport = ai.MuseLocateTransport(directory, executable='unused')
            process = SimpleNamespace(poll=lambda: None, terminate=lambda: None,
                                      kill=lambda: None,
                                      wait=lambda **kwargs: (_ for _ in ()).throw(ai.subprocess.TimeoutExpired('owned', 1)))
            transport._process = process
            self.assertEqual(transport.cancel_and_drain(_policy(), time.monotonic_ns).result, Truth.FALSE)
            self.assertIs(transport._process, process)
            transport._process = None  # synthetic process only

    # @anchor:wa.learn.mw_locate_ai.TestTransport.test_cli_and_transport_implementation_mutants
    def test_cli_and_transport_implementation_mutants(self):
        original = ai.MuseLocateTransport.generate
        source = textwrap.dedent(inspect.getsource(original))
        mutants = {
            'effort': ("'--reasoning-effort', 'medium'", "'--reasoning-effort', 'low'"),
            'steps': ("'--max-model-steps', '1'", "'--max-model-steps', '2'"),
            'writes': ("'--disable-write'", "'--enable-write'"),
            'web': ("'--disable-web-tools'", "'--enable-web-tools'"),
            'transport_export': ("payload == _payload(evidence)", 'True'),
            'output_bound': ("_need(len(buffers[index]) + len(chunk) <= limit, 'output_bound', Reason.CAPACITY)", 'pass'),
        }
        for name, (old, new) in mutants.items():
            with self.subTest(mutant=name):
                self.assertIn(old, source)
                namespace = dict(vars(ai))
                exec(compile(source.replace(old, new), '<locate-mutant>', 'exec'), namespace)
                with patch.object(ai.MuseLocateTransport, 'generate', namespace['generate']):
                    result = unittest.TestResult()
                    TestTransport('test_ram_paths_cli_bounds_and_cleanup').run(result)
                self.assertTrue(result.failures or result.errors, 'surviving transport mutant: '+name)
        for label in ('ram_containment', 'ram_filesystem', 'ram_ownership', 'image_bound'):
            original_need = ai._need
            def bypass(condition, detail, reason=Reason.UNKNOWN):
                if detail != label:
                    original_need(condition, detail, reason)
            with tempfile.TemporaryDirectory(dir='/dev/shm') as directory:
                root = Path(directory)
                asset = root/'asset'; asset.write_bytes(b'xx'); asset.chmod(0o600)
                transport = ai.MuseLocateTransport(root, executable='unused')
                evidence = replace(_Owner().evidence, approved_frame_path=asset, approved_marks_path=asset)
                def check():
                    current = evidence
                    policy = _policy()
                    if label == 'ram_containment':
                        (root/'link').symlink_to(asset)
                        current = replace(evidence, approved_frame_path=root/'link')
                    elif label == 'ram_filesystem':
                        with tempfile.TemporaryDirectory() as persistent:
                            ai.MuseLocateTransport(persistent, executable='unused')
                        return
                    elif label == 'ram_ownership':
                        asset.chmod(0o644)
                    else:
                        policy = replace(policy, image_bytes=1)
                    transport.generate(ai._payload(current), current, policy,
                                       time.monotonic_ns()+10**9, time.monotonic_ns)
                try:
                    with self.assertRaises(ai._Refusal) as caught:
                        check()
                    self.assertEqual(caught.exception.detail, label)
                    if label == 'ram_containment': (root/'link').unlink()
                    with patch.object(ai, '_need', bypass), patch.object(ai.subprocess, 'Popen',
                            side_effect=ai._Refusal('transport_reached')):
                        with self.assertRaises(AssertionError):
                            with self.assertRaises(ai._Refusal) as mutant_error:
                                check()
                            self.assertEqual(mutant_error.exception.detail, label)
                finally:
                    transport.cancel_and_drain(_policy(), time.monotonic_ns)

    # @anchor:wa.learn.mw_locate_ai.TestTransport.test_failed_join_implementation_mutant
    def test_failed_join_implementation_mutant(self):
        source = textwrap.dedent(inspect.getsource(ai.MuseLocateTransport.cancel_and_drain))
        old = 'return AdmissionCheck(Truth.FALSE, Reason.CAPACITY)'
        self.assertIn(old, source)
        namespace = dict(vars(ai))
        exec(compile(source.replace(old, 'return AdmissionCheck(Truth.TRUE, None)'), '<locate-mutant>', 'exec'), namespace)
        with patch.object(ai.MuseLocateTransport, 'cancel_and_drain', namespace['cancel_and_drain']):
            result = unittest.TestResult()
            TestTransport('test_failed_join_retains_resources').run(result)
        self.assertTrue(result.failures or result.errors)


# @anchor:wa.learn.mw_locate_ai.TestOwnerLocator
class TestOwnerLocator(unittest.TestCase):
    # @anchor:wa.learn.mw_locate_ai.TestOwnerLocator.setUp
    def setUp(self):
        self.fixture = composition.TestComposition('test_factory_arguments')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        f = self.fixture
        self.addCleanup(lambda: srf._LOCATOR_CUSTODY.pop((f.invocation.container, f.invocation.window), None))
        self.owner, self.transport = _Owner(), _Transport()
        self.owner.run_deadline_ns = f.ports.limits.deadline_ns
        self.state = f.b._verification_state()
        self.state.input_ns = None
        self.state.run.current_step = self.state
        self.owner.evidence = replace(self.owner.evidence, observation=replace(
            self.owner.evidence.observation, capture_start_ns=f.b.clock(), capture_end_ns=f.b.clock(),
            output_kind='boolean', value=True))
        self.accounting = ai.LocateAccounting(self.state.run)
        self.target = {'rung': 'T1', 'recorded_point': [99, 99]}

    def _open(self, ports=None):
        f = self.fixture
        return srf.open_srf_owners_with_locator(f.invocation, ports=ports or f.ports,
            locator_policy=_policy(), evidence_owner=self.owner, locate_transport=self.transport,
            accounting=self.accounting, surface_factory=f._surface)

    def _miss(self):
        self.fixture.b.capabilities.target = lambda *args: None

    # @anchor:wa.learn.mw_locate_ai.TestOwnerLocator.test_ordinary_success_bypasses_ai
    def test_ordinary_success_bypasses_ai(self):
        with self._open() as owners:
            value = owners.capabilities.target(self.target, self.state, self.state.locate_deadline_ns)
            self.assertEqual(value.point, (10, 10))
            self.assertEqual(self.transport.calls, 0)
            self.assertEqual(self.owner.acquired, 0)
            self.assertEqual(self.accounting.attempts, 0)
            self.assertEqual(owners.observations.validate(value.observation), YES)
            with patch.object(self.fixture.b.capabilities, 'validate_target', return_value=NO) as inner:
                self.assertEqual(owners.capabilities.validate_target(self.target, value, self.state), NO)
                self.assertIs(inner.call_args.args[1].support_owner, self.fixture.b.capabilities)

    # @anchor:wa.learn.mw_locate_ai.TestOwnerLocator.test_ai_registration_and_custody
    def test_ai_registration_and_custody(self):
        self._miss()
        with self._open() as owners:
            value = owners.capabilities.target(self.target, self.state, self.state.locate_deadline_ns)
            self.assertIs(value.support_owner, owners.capabilities)
            self.assertEqual(owners.observations.validate(value.observation), YES)
            self.assertEqual(owners.capabilities.validate_target(self.target, value, self.state), YES)
            self.assertEqual(self.owner.released, 0)
            self.assertNotEqual(owners.capabilities.validate_target(self.target, value, replace(self.state)), YES)
            self.owner.support_ok = NO
            self.assertEqual(owners.capabilities.validate_target(self.target, value, self.state), NO)
            self.owner.support_ok = YES
            self.fixture.b.observations.denied = True
            self.assertNotEqual(owners.capabilities.validate_target(self.target, value, self.state), YES)
            self.fixture.b.observations.denied = False
            self.assertNotEqual(owners.capabilities.validate_target(self.target, replace(value), self.state), YES)
            self.assertNotEqual(owners.capabilities.validate_target(self.target,
                replace(value, observation=replace(value.observation)), self.state), YES)
            with self.assertRaises(ValueError):
                owners.capabilities.validate_target(self.target, replace(value, support_owner=object()), self.state)
        self.assertEqual(self.owner.released, 1)
        self.assertIsNone(self.fixture.ports.actor_lock.holder)
        self.assertNotEqual(owners.capabilities.validate_target(self.target, value, self.state), YES)

    # @anchor:wa.learn.mw_locate_ai.TestOwnerLocator.test_registration_refusal_retains_until_cleanup
    def test_registration_refusal_retains_until_cleanup(self):
        self._miss()
        self.fixture.b.observations.denied = True
        with self._open() as owners:
            with self.assertRaises(ValueError):
                owners.capabilities.target(self.target, self.state, self.state.locate_deadline_ns)
            self.assertEqual(self.owner.released, 0)
        self.assertEqual(self.owner.released, 1)

    # @anchor:wa.learn.mw_locate_ai.TestOwnerLocator.test_ineligible_failure_never_calls_ai
    def test_ineligible_failure_never_calls_ai(self):
        self._miss()
        with self._open() as owners:
            for failure in ('privacy', 'custody', 'authority', 'schema', 'unsupported'):
                self.owner.classification = failure
                with self.assertRaises(ValueError):
                    owners.capabilities.target(self.target, self.state, self.state.locate_deadline_ns)
                self.assertEqual(self.transport.calls, 0)
            self.owner.classification = 'miss'
            self.assertEqual(owners.capabilities.target(self.target, self.state,
                self.state.locate_deadline_ns).point, (25, 34))
            self.assertEqual(self.transport.calls, 1)

    # @anchor:wa.learn.mw_locate_ai.TestOwnerLocator.test_ambiguity_requires_independent_support
    def test_ambiguity_requires_independent_support(self):
        self._miss()
        self.owner.classification = 'ambiguity'
        e = self.owner.evidence
        # Both current controls plausibly match the same semantics. Selecting
        # either model ID provides no independent disambiguating evidence.
        self.owner.evidence = replace(e, description=replace(e.description,
            marks=e.description.marks + (replace(e.description.marks[0], id=8, x=60),)))
        self.owner.support_ok = NO
        with self._open() as owners:
            with self.assertRaises(ValueError):
                owners.capabilities.target(self.target, self.state, self.state.locate_deadline_ns)
            self.assertEqual(self.transport.calls, 1)
            self.assertEqual(self.owner.released, 1)
            # A new step is needed: a failed attempt never renews the old key.
            self.state.seq += 1
            self.owner.support_ok = YES
            self.assertEqual(owners.capabilities.target(self.target, self.state,
                self.state.locate_deadline_ns).point, (25, 34))

    # @anchor:wa.learn.mw_locate_ai.TestOwnerLocator.test_refusal_never_returns_a_dispatch_target
    def test_refusal_never_returns_a_dispatch_target(self):
        for output in (MuseOutput({'mark_id': None}, 0), MuseOutput({}, 0),
                       RuntimeError('PRIVATE_CANARY'), TimeoutError('PRIVATE_CANARY')):
            with self.subTest(output=type(output).__name__):
                case = TestOwnerLocator('test_ordinary_success_bypasses_ai')
                case.setUp()
                try:
                    case._miss()
                    case.transport.output = output
                    with case._open() as owners:
                        with self.assertRaisesRegex(ValueError, '^OUT_OF_SUPPORT$'):
                            owners.capabilities.target(case.target, case.state, case.state.locate_deadline_ns)
                        self.assertEqual(case.accounting.attempts, 1)
                        self.assertEqual(case.transport.calls, 1)
                        with self.assertRaises(ValueError):
                            owners.capabilities.target(case.target, case.state, case.state.locate_deadline_ns)
                        self.assertEqual(case.transport.calls, 1)
                        self.assertEqual(case.fixture.native.calls, [])
                finally:
                    case.doCleanups()

    # @anchor:wa.learn.mw_locate_ai.TestOwnerLocator.test_postcondition_independence
    def test_postcondition_independence(self):
        for rung in ('T1', 'T2'):
            for passing in (False, True):
                with self.subTest(rung=rung, passing=passing):
                    # Independent synthetic composition for every frozen deadline.
                    case = TestOwnerLocator('test_ordinary_success_bypasses_ai')
                    case.setUp()
                    try:
                        case._miss()
                        target = dict(case.target, rung=rung, locale='en')
                        b = case.fixture.b
                        step = b.workflow['steps'][0]
                        original = json.dumps(step['verify'], sort_keys=True)
                        b.observations.values[step['verify']['predicate']['capability_id']] = [passing]
                        with case._open() as owners:
                            case.state.target = workflow.locate_target(target, step_state=case.state,
                                owners=owners, deadline_ns=case.state.locate_deadline_ns)
                            self.assertEqual(case.state.target.point, (25, 34))
                            self.assertEqual(case.transport.calls, 1)
                            if passing:
                                self.assertEqual(workflow.verify_step(step, state=case.state, owners=owners), YES)
                            else:
                                with self.assertRaises(Exception):
                                    workflow.verify_step(step, state=case.state, owners=owners)
                            self.assertEqual(json.dumps(step['verify'], sort_keys=True), original)
                    finally:
                        case.doCleanups()

    # @anchor:wa.learn.mw_locate_ai.TestOwnerLocator.test_cleanup_retains_actor_until_verified
    def test_cleanup_retains_actor_until_verified(self):
        self._miss()
        with self.assertRaises(ValueError):
            with self._open() as owners:
                owners.capabilities.target(self.target, self.state, self.state.locate_deadline_ns)
                self.transport.drain_ok = NO
        self.assertEqual(self.fixture.ports.actor_lock.holder, 'registered-actor')
        self.assertEqual(self.owner.released, 0)
        key = (self.fixture.invocation.container, self.fixture.invocation.window)
        self.assertIs(srf._LOCATOR_CUSTODY[key].owner, self.owner)
        with self.assertRaises(ValueError):
            with self._open():
                pass
        # Explicit reconciliation of this synthetic failed transport/lease.
        self.transport.drain_ok = YES
        self.assertTrue(srf._LOCATOR_CUSTODY[key]._drain())
        self.fixture._recover_fake_lease()
        srf._LOCATOR_CUSTODY.pop(key)
        with self._open():
            pass
        self.assertIsNone(self.fixture.ports.actor_lock.holder)
        self.assertEqual(self.owner.released, 1)

    # @anchor:wa.learn.mw_locate_ai.TestOwnerLocator.test_cleanup_exception_is_sanitized
    def test_cleanup_exception_is_sanitized(self):
        self._miss()
        with self.assertRaisesRegex(ValueError, '^OUT_OF_SUPPORT$'):
            with self._open() as owners:
                owners.capabilities.target(self.target, self.state, self.state.locate_deadline_ns)
                self.transport.cancel_and_drain = lambda *args: (_ for _ in ()).throw(RuntimeError('PRIVATE_CANARY'))
        self.assertEqual(self.fixture.ports.actor_lock.holder, 'registered-actor')
        self.assertEqual(self.owner.released, 0)

    # @anchor:wa.learn.mw_locate_ai.TestOwnerLocator.test_release_failure_retains_actor
    def test_release_failure_retains_actor(self):
        self._miss()
        with self.assertRaises(ValueError):
            with self._open() as owners:
                owners.capabilities.target(self.target, self.state, self.state.locate_deadline_ns)
                self.owner.release_ok = NO
        self.assertEqual(self.fixture.ports.actor_lock.holder, 'registered-actor')
        key = (self.fixture.invocation.container, self.fixture.invocation.window)
        self.assertTrue(srf._LOCATOR_CUSTODY[key]._retained)

    # @anchor:wa.learn.mw_locate_ai.TestOwnerLocator.test_composition_gates
    def test_composition_gates(self):
        self.fixture.ports = replace(self.fixture.ports, announce_gate=lambda *a, **k: NO)
        with self.assertRaises(ValueError):
            with self._open():
                pass
        self.assertEqual(self.fixture.factory_calls, [])
        self.assertIsNone(self.fixture.ports.actor_lock.holder)

    # @anchor:wa.learn.mw_locate_ai.TestOwnerLocator.test_adapter_implementation_mutants
    def test_adapter_implementation_mutants(self):
        cases = (
            (srf._Capabilities, 'target', 'self._state = state',
             'self._state = state; self._locator._recover(target, state, deadline, self)',
             'test_ordinary_success_bypasses_ai'),
            (srf._Locator, '_recover', 'capabilities._observations._register(value.observation)',
             'pass', 'test_ai_registration_and_custody'),
            (srf._Locator, '_validate', 'return self.owner.validate_target(target, original, evidence, state)',
             'return AdmissionCheck(Truth.TRUE, None)', 'test_ai_registration_and_custody'),
            (srf._Capabilities, 'validate_target', 'if not _yes(self._observations.validate(value.observation)):',
             'if False:', 'test_ai_registration_and_custody'),
            (srf._Capabilities, 'validate_target', 'e.observation.publication == value.observation.publication',
             'False', 'test_ai_registration_and_custody'),
            (srf, '_open_srf_owners', '_need(locator._drain())', 'locator._drain()',
             'test_cleanup_retains_actor_until_verified'),
            (srf._Locator, '_drain', 'except Exception:\n        return False', 'except Exception:\n        return True',
             'test_cleanup_exception_is_sanitized'),
            (srf._Locator, '_drain', 'if not _yes(self.owner.release(evidence, deadline)) or self.clock() > deadline:',
             'if False:', 'test_release_failure_retains_actor'),
            (srf, '_open_srf_owners', '_admit(ports.announce_gate(invocation, scope=ports.scope, authority=ports.authority,\n                                 now_ns=ports.clock(), absolute_deadline_ns=ports.limits.deadline_ns))',
             'pass', 'test_composition_gates'),
            (srf._Capabilities, 'validate_target', 'return self._ports.capabilities.validate_target(target, replace(value, support_owner=self._ports.capabilities), state)',
             'return AdmissionCheck(Truth.TRUE, None)', 'test_ordinary_success_bypasses_ai'),
            (ai, 'locate_once', "_need(_admitted(evidence_owner.validate_target(target, value, evidence, state)),", "_need(True,",
             'test_ambiguity_requires_independent_support'),
            (workflow, 'verify_step', 'if truth is Truth.TRUE:', 'if True:', 'test_postcondition_independence'),
        )
        for owner, method, old, new, test in cases:
            with self.subTest(mutant=method+':'+test):
                source = textwrap.dedent(inspect.getsource(getattr(owner, method)))
                self.assertIn(old, source)
                namespace = dict(vars(workflow if owner is workflow else ai if owner is ai else srf))
                exec(compile(source.replace(old, new), '<owner-locator-mutant>', 'exec'), namespace)
                destination = srf if owner is ai else owner
                with patch.object(destination, method, namespace[method]):
                    result = unittest.TestResult()
                    TestOwnerLocator(test).run(result)
                self.assertTrue(result.failures or result.errors, 'surviving adapter mutant: '+test)

    # @anchor:wa.learn.mw_locate_ai.TestOwnerLocator._case
    def _case(self):
        case = TestOwnerLocator('test_ordinary_success_bypasses_ai')
        case.setUp()
        self.addCleanup(case.doCleanups)
        return case

    # @anchor:wa.learn.mw_locate_ai.TestOwnerLocator._run_receipt
    def _run_receipt(self, *, miss=True, locator=True, steps=1, configure=None):
        f, b = self.fixture, self.fixture.b
        f._action('click')
        if steps > 1:
            import copy
            b.workflow['steps'] = [copy.deepcopy(b.workflow['steps'][0]) for _ in range(steps)]
            for seq, step in enumerate(b.workflow['steps'], 1):
                step['seq'] = seq
        if miss:
            self._miss()
        # Acquire current evidence at the runner's locate boundary, not setup time.
        original = self.owner.acquire
        def acquire(target, state, policy, deadline):
            self.owner.evidence = replace(self.owner.evidence, observation=replace(
                self.owner.evidence.observation, publication='ai-'+str(state.seq),
                capture_start_ns=b.clock(), capture_end_ns=b.clock()))
            return original(target, state, policy, deadline)
        self.owner.acquire = acquire
        self.accounting = ai.LocateAccounting(None)
        b._write()
        context = self._open() if locator else f._open()
        with context as owners:
            self.built = owners
            if configure is not None:
                configure(owners)
            result = workflow.run_workflow(b.path, bundle=composition.base.BUNDLE,
                                           owners=owners, limits=owners.limits)
        if result.summary is not None:
            self.assertEqual(result.summary, b.journal.summary)
            self.assertEqual(result.summary, json.loads((b.root / 'inactive.json').read_text()))
            self.assertEqual(b.journal.selected, str(b.root / 'inactive.json'))
        return result

    # @anchor:wa.learn.mw_locate_ai.TestOwnerLocator.test_receipt_totals
    def test_receipt_totals(self):
        result = self._run_receipt()
        self.assertEqual(result.rc, 0, result)
        self.assertEqual((result.summary['model_calls'], result.summary['tokens_used']), (1, 5))
        self.assertEqual(self.transport.calls, 1)
        self.assertEqual(len(self.fixture.native.calls), 1)
        self.assertIs(self.accounting.run, self.built.capabilities._state.run)
        for private in ('Submit', '/dev/shm', 'PRIVATE_CANARY', 'semantic_target', 'mark_id'):
            self.assertNotIn(private, json.dumps(result.summary))
        case = self._case()
        result = case._run_receipt(steps=2)
        self.assertEqual(result.rc, 0, result)
        self.assertEqual((result.summary['model_calls'], result.summary['tokens_used']), (2, 10))

    # @anchor:wa.learn.mw_locate_ai.TestOwnerLocator.test_receipt_refused_attempt
    def test_receipt_refused_attempt(self):
        self.transport.output = MuseOutput({'mark_id': None}, 0, {'input_tokens': 3, 'output_tokens': 2})
        result = self._run_receipt()
        self.assertNotEqual(result.rc, 0)
        self.assertIsNotNone(result.summary, result)
        self.assertEqual((result.summary['model_calls'], result.summary['tokens_used']), (1, 5))
        self.assertEqual(self.fixture.native.calls, [])
        case = self._case()
        case.transport.output = RuntimeError('PRIVATE_CANARY')
        result = case._run_receipt()
        self.assertNotEqual(result.rc, 0)
        self.assertEqual((result.summary['model_calls'], result.summary['tokens_used']), (1, None))
        self.assertNotIn('PRIVATE_CANARY', json.dumps(result.summary) + str(result.diagnostic))
        self.assertEqual(case.fixture.native.calls, [])

    # @anchor:wa.learn.mw_locate_ai.TestOwnerLocator.test_receipt_unknown_usage
    def test_receipt_unknown_usage(self):
        for usage, expected in ((None, None), ({'input_tokens': 3}, None),
                                ({'input_tokens': 8, 'output_tokens': 2}, 10)):
            with self.subTest(usage=usage):
                case = self._case()
                case.transport.output = MuseOutput({'mark_id': 7}, 0, usage)
                result = case._run_receipt()
                self.assertEqual(result.rc, 0, result)
                self.assertEqual(result.summary['model_calls'], 1)
                self.assertEqual(result.summary['tokens_used'], expected)

    # @anchor:wa.learn.mw_locate_ai.TestOwnerLocator.test_receipt_sticky_unknown
    def test_receipt_sticky_unknown(self):
        self.transport.output = MuseOutput({'mark_id': 7}, 0, None)
        def later():
            if self.transport.calls == 2:
                self.transport.output = MuseOutput({'mark_id': 7}, 0,
                                                   {'input_tokens': 8, 'output_tokens': 2})
        self.transport.callback = later
        result = self._run_receipt(steps=2)
        self.assertEqual(result.rc, 0, result)
        self.assertEqual((result.summary['model_calls'], result.summary['tokens_used']), (2, None))
        self.assertEqual(self.accounting.known_tokens, 10)

    # @anchor:wa.learn.mw_locate_ai.TestOwnerLocator.test_receipt_legacy_and_unused
    def test_receipt_legacy_and_unused(self):
        for locator, expected in ((False, (None, None)), (True, (0, 0))):
            with self.subTest(locator=locator):
                case = self._case()
                result = case._run_receipt(miss=False, locator=locator)
                self.assertEqual(result.rc, 0, result)
                self.assertEqual((result.summary['model_calls'], result.summary['tokens_used']), expected)
                self.assertEqual((case.transport.calls, case.owner.acquired, case.accounting.attempts), (0, 0, 0))

    # @anchor:wa.learn.mw_locate_ai.TestOwnerLocator.test_nonlocation_exception_identity
    def test_nonlocation_exception_identity(self):
        for error in (ContractError(Reason.PRIVACY), ContractError(Reason.STALE),
                      RuntimeError('PRIVATE_CANARY'), ValueError('OUT_OF_SUPPORT'),
                      ValueError('LOW_CONFIDENCE'), TimeoutError('PRIVATE_CANARY'),
                      MemoryError('PRIVATE_CANARY')):
            with self.subTest(error=type(error).__name__):
                case = self._case()
                def fail(*args):
                    raise error
                case.fixture.b.capabilities.target = fail
                with case._open() as owners, self.subTest(boundary="composition"):
                    with self.assertRaises(type(error)) as raised:
                        owners.capabilities.target(case.target, case.state, case.state.locate_deadline_ns)
                    self.assertIs(raised.exception, error)
                self.assertEqual((case.transport.calls, case.owner.acquired, case.accounting.attempts), (0, 0, 0))
                self.assertEqual(case.fixture.native.calls, [])

    # @anchor:wa.learn.mw_locate_ai.TestOwnerLocator.test_closed_location_failure
    def test_closed_location_failure(self):
        for code in ('LOW_CONFIDENCE', 'AMBIGUOUS_TARGET', 'LAYOUT_INVARIANT'):
            case = self._case()
            error = workflow.LocationFailure(code)
            self.assertEqual(vars(error), {'code': code})
            self.assertEqual(str(error), code)
            def fail(*args):
                raise error
            case.fixture.b.capabilities.target = fail
            with case._open() as owners, self.subTest(boundary="composition"):
                value = owners.capabilities.target(case.target, case.state, case.state.locate_deadline_ns)
                self.assertIs(type(value), GroundedTarget)
                self.assertEqual(owners.capabilities.validate_target(case.target, value, case.state), YES)
            self.assertEqual(case.transport.calls, 1)
        for invalid in ('PRIVATE_CANARY', 'AMBIGUOUS', None, True, [], {'code': 'LOW_CONFIDENCE'}):
            with self.subTest(invalid=invalid), self.assertRaisesRegex(ValueError, '^OUT_OF_SUPPORT$'):
                workflow.LocationFailure(invalid)

    # @anchor:wa.learn.mw_locate_ai.TestOwnerLocator.test_primary_refusal_preserved
    def test_primary_refusal_preserved(self):
        for locator in (False, True):
            for code in ('LOW_CONFIDENCE', 'AMBIGUOUS_TARGET', 'LAYOUT_INVARIANT'):
                case = self._case()
                error = workflow.LocationFailure(code)
                def fail(*args):
                    raise error
                case.fixture.b.capabilities.target = fail
                case.transport.output = MuseOutput({'mark_id': None}, 0, None)
                with (case._open() if locator else case.fixture._open()) as owners, self.subTest(boundary="composition"):
                    with self.assertRaises(Exception) as raised:
                        owners.capabilities.target(case.target, case.state, case.state.locate_deadline_ns)
                    self.assertIs(raised.exception, error)
                    self.assertEqual(raised.exception.code, code)
                    if locator:
                        self.assertTrue(raised.exception.__suppress_context__)
                self.assertEqual(case.transport.calls, int(locator))

    # @anchor:wa.learn.mw_locate_ai.TestOwnerLocator.test_runner_refusal_and_privacy
    def test_runner_refusal_and_privacy(self):
        for error, diagnostic in ((workflow.LocationFailure('LAYOUT_INVARIANT'), 'DEADLINE'),
                                  (RuntimeError('PRIVATE_CANARY'), 'OWNER_FAILURE'),
                                  (ContractError(Reason.PRIVACY), 'OWNER_FAILURE'),
                                  (ContractError(Reason.STALE), 'OWNER_FAILURE')):
            case = self._case()
            def configure(owners):
                def fail(*args):
                    raise error
                case.fixture.b.capabilities.target = fail
            case.transport.output = MuseOutput({'mark_id': None}, 0, None)
            result = case._run_receipt(configure=configure)
            self.assertNotEqual(result.rc, 0)
            self.assertEqual(result.diagnostic, diagnostic)
            self.assertEqual(result.verdict_code, 'DEADLINE' if isinstance(error, workflow.LocationFailure) else 'OUT_OF_SUPPORT')
            self.assertIsNotNone(result.summary)
            self.assertNotIn('PRIVATE_CANARY', json.dumps(result.summary) + str(result.diagnostic))
            self.assertEqual(case.fixture.native.calls, [])
            if not isinstance(error, workflow.LocationFailure):
                self.assertEqual((case.transport.calls, case.owner.acquired, case.accounting.attempts), (0, 0, 0))

    # @anchor:wa.learn.mw_locate_ai.TestOwnerLocator.test_run_binding_guards
    def test_run_binding_guards(self):
        cases = [('foreign', None), ('scope', None), ('authority', None), ('busy', None),
                 ('attempts', 1), ('known_tokens', 1), ('attempted_steps', {1}),
                 ('poisoned', True), ('retained_evidence', object()), ('usage_complete', False)]
        for name, value in cases:
            with self.subTest(gate=name):
                case = self._case()
                def configure(owners):
                    if name == 'foreign':
                        case.accounting.run = case.state.run  # Same scope, authority and textual run id.
                    elif name in ('scope', 'authority'):
                        owners.capabilities._ports = replace(case.fixture.ports, **{name: 'foreign'})
                    elif name == 'busy':
                        case.accounting._lock.acquire()
                    else:
                        setattr(case.accounting, name, value)
                try:
                    result = case._run_receipt(configure=configure)
                finally:
                    if name == 'busy':
                        case.accounting._lock.release()
                self.assertNotEqual(result.rc, 0, name)
                self.assertEqual(case.fixture.native.calls, [])
                self.assertEqual(case.transport.calls, 0)
                self.assertEqual(case.fixture.b.journal.selected, 'old')
                if name not in ('foreign', 'scope', 'authority', 'busy', 'retained_evidence'):
                    self.assertEqual(getattr(case.accounting, name), value)
        # A same-run binding is idempotent even after counters have accrued.
        def same_run(owners):
            original = owners.capabilities._bind_locator_run
            def bind(state):
                original(state)
                case.accounting.attempts = 1
                original(state)
            owners.capabilities._bind_locator_run = bind
        case = self._case()
        result = case._run_receipt(miss=False, configure=same_run)
        self.assertEqual(result.rc, 0, result)
        self.assertEqual(result.summary['model_calls'], 1)

    # @anchor:wa.learn.mw_locate_ai.TestOwnerLocator.test_hook_contract_guards
    def test_hook_contract_guards(self):
        invalid = [False, 1, [], (), (1,), [1, 2], (1, 2, 3),
                   (-1, 0), (True, 0), ('1', 0), (1.5, 0),
                   (1, -1), (1, True), (1, '2'), (1, 2.5)]
        def fail(*args):
            raise RuntimeError('PRIVATE_CANARY')
        for hook in (None, 7, fail, *(lambda state, value=value: value for value in invalid)):
            case = self._case()
            result = case._run_receipt(miss=False, configure=lambda owners:
                setattr(owners.capabilities, '_locate_usage', hook))
            self.assertNotEqual(result.rc, 0, result)
            self.assertIsNone(result.summary)
            self.assertEqual(case.fixture.b.journal.selected, 'old')
            self.assertNotIn('PRIVATE_CANARY', str(result.diagnostic))
        for hook in (None, 7, fail):
            case = self._case()
            result = case._run_receipt(miss=False, configure=lambda owners:
                setattr(owners.capabilities, '_bind_locator_run', hook))
            self.assertNotEqual(result.rc, 0)
            self.assertEqual(case.fixture.native.calls, [])
        for usage in (None, (0, 0), (1, None), (2, 13)):
            case = self._case()
            result = case._run_receipt(miss=False, configure=lambda owners:
                setattr(owners.capabilities, '_locate_usage', lambda state: usage))
            self.assertEqual(result.rc, 0, result)
            self.assertEqual((result.summary['model_calls'], result.summary['tokens_used']),
                             (None, None) if usage is None else usage)

    # @anchor:wa.learn.mw_locate_ai.TestOwnerLocator.test_snapshot_guards
    def test_snapshot_guards(self):
        for snapshot in (SimpleNamespace(model_calls=True, tokens_used=0, usage_complete=True),
                         SimpleNamespace(model_calls=-1, tokens_used=0, usage_complete=True),
                         SimpleNamespace(model_calls=1, tokens_used=True, usage_complete=True),
                         SimpleNamespace(model_calls=1, tokens_used=-1, usage_complete=True),
                         SimpleNamespace(model_calls=1, tokens_used=0, usage_complete=1)):
            case = self._case()
            def configure(owners):
                case.accounting.snapshot = lambda: snapshot
            result = case._run_receipt(miss=False, configure=configure)
            self.assertNotEqual(result.rc, 0, result)
            self.assertIsNone(result.summary)
        for gate in ('scope', 'authority', 'identity', 'busy'):
            case = self._case()
            def configure(owners):
                def corrupt():
                    if gate in ('scope', 'authority'):
                        owners.capabilities._ports = replace(case.fixture.ports, **{gate: 'foreign'})
                    elif gate == 'identity':
                        case.accounting.run = case.state.run
                    else:
                        case.accounting._lock.acquire()
                    return YES
                case.fixture.b.capabilities.final = lambda *args: corrupt()
            try:
                result = case._run_receipt(miss=False, configure=configure)
            finally:
                if gate == 'busy':
                    case.accounting._lock.release()
            self.assertNotEqual(result.rc, 0, result)
            self.assertIsNone(result.summary)
        case = self._case()
        def incomplete(owners):
            case.accounting.snapshot = lambda: SimpleNamespace(model_calls=1, tokens_used=99, usage_complete=False)
        result = case._run_receipt(miss=False, configure=incomplete)
        self.assertEqual(result.rc, 0, result)
        self.assertEqual((result.summary['model_calls'], result.summary['tokens_used']), (1, None))

    # @anchor:wa.learn.mw_locate_ai.TestOwnerLocator.test_real_receipt_schema
    def test_real_receipt_schema(self):
        result = self._run_receipt()
        b, g = self.fixture.b, composition.base.g
        for field in ('model_calls', 'tokens_used'):
            for invalid in (-1, True, '3', 1.5):
                with self.subTest(field=field, invalid=invalid):
                    summary = dict(result.summary, **{field: invalid})
                    path = b.journal.stage(summary, b.journal.inventory, b.owners.output)
                    self.assertNotEqual(g.validate_file(path, composition.base.BUNDLE,
                                                       context=b.journal.context()).rc, 0)
            for valid in (None, 0, 3):
                summary = dict(result.summary, **{field: valid})
                path = b.journal.stage(summary, b.journal.inventory, b.owners.output)
                self.assertEqual(g.validate_file(path, composition.base.BUNDLE,
                                                 context=b.journal.context()).rc, 0)

    # @anchor:wa.learn.mw_locate_ai.TestOwnerLocator.test_run_postcondition_independence
    def test_run_postcondition_independence(self):
        case = self._case()
        case.fixture.b.capabilities.no_final = True
        result = case._run_receipt()
        self.assertNotEqual(result.rc, 0)
        self.assertEqual(result.summary['model_calls'], 1)
        self.assertEqual(result.verdict_code, 'CHANGE_MISMATCH')
        case = self._case()
        predicate = case.fixture.b.workflow['steps'][0]['verify']['predicate']['capability_id']
        case.fixture.b.observations.values[predicate] = [False]
        result = case._run_receipt()
        self.assertNotEqual(result.rc, 0)
        self.assertEqual(result.summary['model_calls'], 1)
        self.assertEqual(len(case.fixture.native.calls), 1)
        result = self._run_receipt()
        self.assertEqual(result.rc, 0, result)

    # @anchor:wa.learn.mw_locate_ai.TestOwnerLocator.test_invalid_primary_and_recovery_errors
    def test_invalid_primary_and_recovery_errors(self):
        case = self._case()
        case.fixture.b.capabilities.target = lambda *args: (10, 10)
        with case._open() as owners, self.subTest(boundary="composition"):
            with self.assertRaises(ValueError):
                owners.capabilities.target(case.target, case.state, case.state.locate_deadline_ns)
        self.assertEqual((case.transport.calls, case.owner.acquired), (0, 0))
        case = self._case()
        error = RuntimeError('PRIVATE_CANARY')
        primary = workflow.LocationFailure('LOW_CONFIDENCE')
        def fail(*args):
            raise primary
        case.fixture.b.capabilities.target = fail
        with case._open() as owners, self.subTest(boundary="composition"):
            with patch.object(owners.capabilities._observations, '_register', side_effect=error):
                with self.assertRaises(Exception) as raised:
                    owners.capabilities.target(case.target, case.state, case.state.locate_deadline_ns)
                self.assertIs(raised.exception, error)
        self.assertEqual(case.owner.released, 1)

    # @anchor:wa.learn.mw_locate_ai.TestOwnerLocator.test_location_failure_translation
    def test_location_failure_translation(self):
        for code in ('LOW_CONFIDENCE', 'AMBIGUOUS_TARGET', 'LAYOUT_INVARIANT'):
            case = self._case()
            def fail(*args):
                raise workflow.LocationFailure(code)
            case.fixture.b.capabilities.target = fail
            with case.fixture._open() as owners, self.subTest(boundary="composition"):
                with self.assertRaises(Exception) as raised:
                    workflow.locate_target(case.target, step_state=case.state, owners=owners,
                                           deadline_ns=case.state.locate_deadline_ns)
                self.assertEqual(raised.exception.code, 'LOW_CONFIDENCE')
                self.assertEqual(raised.exception.diagnostic, code)

    # @anchor:wa.learn.mw_locate_ai.TestOwnerLocator.test_composed_accounting_interface
    def test_composed_accounting_interface(self):
        # Reserved hooks are exercised on capabilities returned by the public
        # composition API; no private adapter or accounting implementation stub.
        with self._open() as owners, self.subTest(boundary="composition"):
            caps, run = owners.capabilities, self.state.run
            caps._bind_locator_run(run)
            self.assertEqual(caps._locate_usage(run), (0, 0))
            for scope in ('scope', 'authority'):
                bad = replace(run, **{scope: 'foreign'})
                with self.assertRaises(ValueError):
                    caps._bind_locator_run(bad)
                with self.assertRaises(ValueError):
                    caps._locate_usage(bad)
            with self.assertRaises(ValueError):
                caps._locate_usage(replace(run))
            for field, invalid in (('attempts', True), ('attempts', -1),
                                   ('known_tokens', True), ('known_tokens', -1),
                                   ('usage_complete', 1)):
                original = getattr(self.accounting, field)
                setattr(self.accounting, field, invalid)
                try:
                    with self.assertRaises(ValueError):
                        caps._locate_usage(run)
                    self.assertFalse(self.accounting._lock.locked())
                finally:
                    setattr(self.accounting, field, original)
            self.accounting._lock.acquire()
            try:
                with self.assertRaises(ValueError):
                    caps._bind_locator_run(run)
                with self.assertRaises(ValueError):
                    caps._locate_usage(run)
            finally:
                self.accounting._lock.release()
        # Installed primary owners cannot impersonate the reserved hooks.
        self.fixture.b.capabilities._bind_locator_run = lambda *a: self.fail('delegated bind')
        self.fixture.b.capabilities._locate_usage = lambda *a: self.fail('delegated usage')
        with self.fixture._open() as owners:
            self.assertIsNone(owners.capabilities._bind_locator_run(self.state.run))
            self.assertIsNone(owners.capabilities._locate_usage(self.state.run))

    # @anchor:wa.learn.mw_locate_ai.TestOwnerLocator.test_mwl2f_control_mutants
    def test_mwl2f_control_mutants(self):
        rows = [
            (srf._Capabilities, 'target', 'except LocationFailure as exc:', 'except Exception as exc:',
             'test_nonlocation_exception_identity'),
            (workflow, '_summary', 'model_calls=model_calls, tokens_used=tokens_used',
             'model_calls=None, tokens_used=None', 'test_receipt_totals'),
            (srf._Capabilities, '_locate_usage', 'snapshot.tokens_used if snapshot.usage_complete else None',
             'snapshot.tokens_used if snapshot.usage_complete else 0', 'test_receipt_unknown_usage'),
            (srf._Capabilities, 'target', 'raise primary_refusal from None', 'raise',
             'test_primary_refusal_preserved'),
            (ai, 'locate_once', 'accounting.usage_complete = previous_complete and known',
             'accounting.usage_complete = known', 'test_receipt_sticky_unknown'),
            (ai, 'locate_once', 'accounting.attempts += 1', 'accounting.attempts += int(False)',
             'test_receipt_refused_attempt'),
            (srf._Capabilities, '_locate_usage', 'return None', 'return (0, 0)',
             'test_receipt_legacy_and_unused'),
            (srf._Capabilities, '_locate_usage', 'return (snapshot.model_calls, snapshot.tokens_used if snapshot.usage_complete else None)',
             'return None', 'test_receipt_legacy_and_unused'),
            (workflow, 'run_workflow', 'bind_hook(state)', 'pass', 'test_receipt_totals'),
            (workflow, 'locate_target', "raise _Stop('LOW_CONFIDENCE', diagnostic=exc.code) from None",
             "raise _Stop('LOW_CONFIDENCE') from None", 'test_location_failure_translation'),
            (srf._Capabilities, 'target', '_need(type(value) is GroundedTarget and value.support_owner is self._ports.capabilities)',
             'if type(value) is not GroundedTarget:\n        return self._locator._recover(target, state, deadline, self)',
             'test_invalid_primary_and_recovery_errors'),
            (srf._Capabilities, 'target', 'except _LocateRefused:', 'except Exception:',
             'test_invalid_primary_and_recovery_errors'),
            (workflow.LocationFailure, '__init__', "code not in frozenset((\n            'LOW_CONFIDENCE', 'AMBIGUOUS_TARGET', 'LAYOUT_INVARIANT'))", 'False',
             'test_closed_location_failure'),
            (srf._Locator, '_recover', 'raise _LocateRefused()', "raise ValueError('OUT_OF_SUPPORT')",
             'test_primary_refusal_preserved'),
            (workflow, '_summary', "getattr(owners.capabilities, '_locate_usage', missing)",
             "getattr(owners.capabilities, '_locate_usage', missing) or missing", 'test_hook_contract_guards'),
            (workflow, 'run_workflow', "getattr(owners.capabilities, '_bind_locator_run', missing)",
             "getattr(owners.capabilities, '_bind_locator_run', missing) or missing", 'test_hook_contract_guards'),
            (workflow, '_summary', 'type(usage) is tuple and len(usage) == 2',
             'len(usage) == 2', 'test_hook_contract_guards'),
            (workflow, '_summary', 'type(model_calls) is int and model_calls >= 0',
             'True', 'test_summary_scalar_contract'),
            (workflow, '_summary', 'tokens_used is None or (type(tokens_used) is int and tokens_used >= 0)',
             'True', 'test_summary_scalar_contract'),
            (srf._Capabilities, '_locate_usage', 'type(snapshot.model_calls) is int and snapshot.model_calls >= 0',
             'True', 'test_composed_accounting_interface'),
            (srf._Capabilities, '_locate_usage', 'snapshot.tokens_used is None or\n              (type(snapshot.tokens_used) is int and snapshot.tokens_used >= 0)',
             'True', 'test_composed_accounting_interface'),
            (srf._Capabilities, '_locate_usage', 'type(snapshot.usage_complete) is bool',
             'True', 'test_composed_accounting_interface'),
            (srf._Capabilities, '_locate_usage', 'accounting.run is state', 'True',
             'test_composed_accounting_interface'),
            (srf._Capabilities, '_bind_locator_run', 'if accounting.run is state:', 'if False:',
             'test_run_binding_guards'),
        ]
        for method, test in (('_bind_locator_run', 'test_run_binding_guards'),
                             ('_locate_usage', 'test_snapshot_guards')):
            for condition in ('state.scope == p.scope', 'state.authority == p.authority'):
                rows.append((srf._Capabilities, method, condition, 'True', test))
        for condition in ('accounting.run is None', 'accounting.attempts == 0', 'accounting.known_tokens == 0',
                          'not accounting.attempted_steps', 'accounting.poisoned is False',
                          'accounting.retained_evidence is None', 'accounting.usage_complete is True'):
            rows.append((srf._Capabilities, '_bind_locator_run', condition, 'True', 'test_run_binding_guards'))
        for method in ('_bind_locator_run', '_locate_usage'):
            rows.append((srf._Capabilities, method, '_need(accounting._lock.acquire(blocking=False))',
                         '_need(accounting._lock.acquire(blocking=False) or True)', 'test_accounting_lock_refusal'))
        rows.append((srf._Capabilities, '_locate_usage', 'snapshot.tokens_used if snapshot.usage_complete else None',
                     'snapshot.tokens_used', 'test_snapshot_guards'))
        g = composition.base.g
        rows.extend((
            (g, 'conforms', "'type' in schema and type(value) is not TYPES[schema['type']]", 'False',
             'test_real_receipt_schema'),
            (g, 'conforms', "'minimum' in schema and value < schema['minimum']", 'False',
             'test_real_receipt_schema'),
        ))
        for owner, method, old, new, test in rows:
            with self.subTest(method=method, condition=old, test=test):
                module = owner if inspect.ismodule(owner) else workflow if owner is workflow.LocationFailure else srf
                source = textwrap.dedent(inspect.getsource(getattr(owner, method)))
                self.assertIn(old, source)
                namespace = dict(vars(module))
                mutated = source.replace(old, new)
                if owner is workflow.LocationFailure:
                    mutated = mutated.replace('super().__init__(code)', 'ValueError.__init__(self, code)')
                exec(compile(mutated, '<mwl2f-control-mutant>', 'exec'), namespace)
                destination = srf if owner is ai else owner
                with patch.object(destination, method, namespace[method]):
                    result = unittest.TestResult()
                    TestOwnerLocator(test).run(result)
                self.assertTrue(result.failures, 'no assertion killed mutant: '+method+':'+old+' '+str(result.errors))

    # @anchor:wa.learn.mw_locate_ai.TestOwnerLocator.test_summary_scalar_contract
    def test_summary_scalar_contract(self):
        for pair in ((True, 0), (-1, 0), ('1', 0), (1.5, 0),
                     (1, True), (1, -1), (1, '2'), (1, 2.5)):
            case = self._case()
            staged = []
            def configure(owners):
                owners.capabilities._locate_usage = lambda state: pair
                original = owners.journal.stage
                def stage(*args):
                    staged.append(args[0])
                    return original(*args)
                owners.journal.stage = stage
            result = case._run_receipt(miss=False, configure=configure)
            self.assertNotEqual(result.rc, 0, result)
            self.assertEqual(staged, [], 'invalid accounting reached persistence')

    # @anchor:wa.learn.mw_locate_ai.TestOwnerLocator.test_accounting_lock_refusal
    def test_accounting_lock_refusal(self):
        with self._open() as owners, self.subTest(boundary="composition"):
            for name in ('_bind_locator_run', '_locate_usage'):
                self.accounting._lock.acquire()
                try:
                    with self.assertRaises(ValueError):
                        getattr(owners.capabilities, name)(self.state.run)
                finally:
                    if self.accounting._lock.locked():
                        self.accounting._lock.release()
            self.assertEqual(owners.capabilities._locate_usage(self.state.run), (0, 0))
