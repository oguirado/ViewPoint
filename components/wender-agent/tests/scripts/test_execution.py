# @module {"id":"wa.tests.scripts.test_execution","layer":3,"owner":"ACT","deps":["wa.scripts.act_api","wa.act.proto1_action_policy","wa.scripts.types","wa.scripts.runner","wa.scripts.predicates","wa.scripts.frame_wait","wa.learn.script_ledger","wa.tests.scripts.test_store_catalogue","wa.tests.scripts.support","wa.tests.scripts.test_compiler","wa.scripts.compiler"],"book":"HB-WA-s2-act"}
"""Public ActionPolicy boundary controls with real W8UFence fixtures.

These controls certify the synthetic installation owner's contract only. They
cannot certify production invalidator coverage or application/goal evidence.
"""
import ast as _ast
import dataclasses as _dc
from pathlib import Path as _Path
import sys as _sys
import threading as _threading
import types as _types
import unittest as _unittest
from unittest import mock as _mock
from wender.scripts import act_api as _a, types as _t
from wender.act import proto1_action_policy as _p
from tests import test_p1_wa_action_policy_unit as _legacy

_YES = _t.AdmissionCheck(_t.Truth.TRUE, None)
_NO = _t.AdmissionCheck(_t.Truth.FALSE, _t.Reason.MISSING)


# @anchor:wa.tests.scripts.test_execution._Installation
class _Installation:
    # @anchor:wa.tests.scripts.test_execution._Installation.__init__
    def __init__(self):
        self.covered = self.bounded = True
        self.proof = object()

    # @anchor:wa.tests.scripts.test_execution._Installation.check
    def check(self, owner):
        return _YES if self.covered and self.bounded else _NO

    # @anchor:wa.tests.scripts.test_execution._Installation.check_drained
    def check_drained(self, context, proof):
        return _YES if proof is self.proof else _NO


# @anchor:wa.tests.scripts.test_execution._Harness
class _Harness:
    # @anchor:wa.tests.scripts.test_execution._Harness.__init__
    def __init__(self):
        self.fixture = _legacy.ActionPolicyGate()
        self.fixture.setUp()
        self.installation = _Installation()
        self.interlock = _a._InputInterlock(installation=self.installation, max_pending=4)
        self.fixture.policy = _p.ActionPolicy(fence=self.fixture.fence,
            clock=self.fixture.clock, script_interlock=self.interlock)
        self.admission = self.fixture.admit()
        self.checks = 0
        self.current = True
        self.context = self.interlock.register(self.admission, guard=self.guard)

    # @anchor:wa.tests.scripts.test_execution._Harness.guard
    def guard(self, *args):
        self.checks += 1
        return _YES if self.current else _NO

    # @anchor:wa.tests.scripts.test_execution._Harness.send
    def send(self, callback=None, context=None):
        return self.fixture.policy.dispatch(self.admission,
            self.fixture.actuator if callback is None else callback,
            script_context=self.context if context is None else context)


# @anchor:wa.tests.scripts.test_execution.TestExecution
class TestExecution(_unittest.TestCase):
    # @anchor:wa.tests.scripts.test_execution.TestExecution.test_once
    def test_once(self):
        h = _Harness()
        first = h.send()
        self.assertEqual(first['status'], 'admitted')
        self.assertEqual(h.fixture.actuator.opened, 1)
        self.assertEqual(h.checks, 1)
        self.assertEqual(h.send(), first)
        self.assertEqual(h.fixture.actuator.opened, 1)
        self.assertEqual(len(h.interlock.outstanding()), 1)

    # @anchor:wa.tests.scripts.test_execution.TestExecution.test_invalidation_first
    def test_invalidation_first(self):
        for invalidator in ('revoke', 'stop', 'scope', 'focus', 'target', 'generation', 'deadline', 'restart'):
            with self.subTest(invalidator=invalidator):
                h = _Harness()
                h.interlock.invalidate(lambda: None)
                with self.assertRaises(_p.PolicyRefused):
                    h.send()
                self.assertEqual(h.fixture.actuator.opened, 0)
                self.assertEqual(h.send()['status'], 'refused')
                self.assertEqual(h.fixture.actuator.opened, 0)
        h = _Harness()
        self.assertEqual(h.send()['status'], 'admitted')

    # @anchor:wa.tests.scripts.test_execution.TestExecution.test_guard
    def test_guard(self):
        h = _Harness()
        h.current = False
        with self.assertRaises(_p.PolicyRefused):
            h.send()
        self.assertEqual(h.checks, 1)
        self.assertEqual(h.fixture.actuator.opened, 0)
        h = _Harness()
        self.assertEqual(h.send()['status'], 'admitted')

    # @anchor:wa.tests.scripts.test_execution.TestExecution.test_installation
    def test_installation(self):
        for flag in ('covered', 'bounded'):
            owner = _Installation()
            setattr(owner, flag, False)
            with self.assertRaises(_t.ContractError):
                _a._InputInterlock(installation=owner, max_pending=1)
        _a._InputInterlock(installation=_Installation(), max_pending=1)
        h = _Harness()
        h.installation.covered = False
        with self.assertRaises(_p.PolicyRefused):
            h.send()
        self.assertEqual(h.fixture.actuator.opened, 0)

    # @anchor:wa.tests.scripts.test_execution.TestExecution.test_context_identity
    def test_context_identity(self):
        for make in (lambda h: _dc.replace(h.context),
                     lambda h: object(),
                     lambda h: h.interlock.register(object(), guard=h.guard)):
            h = _Harness()
            with self.assertRaises(_p.PolicyRefused):
                h.send(context=make(h))
            self.assertEqual(h.fixture.actuator.opened, 0)
        h = _Harness()
        self.assertEqual(h.send()['status'], 'admitted')

    # @anchor:wa.tests.scripts.test_execution.TestExecution.test_missing_interlock
    def test_missing_interlock(self):
        h = _Harness()
        h.fixture.policy.script_interlock = None
        with self.assertRaises(_p.PolicyRefused):
            h.send()
        self.assertEqual(h.fixture.actuator.opened, 0)
        h = _Harness()
        self.assertEqual(h.send()['status'], 'admitted')

    # @anchor:wa.tests.scripts.test_execution.TestExecution.test_uncertain_privacy
    def test_uncertain_privacy(self):
        h = _Harness()
        calls = []
        def callback(request, decision):
            calls.append(1)
            raise RuntimeError('private-canary-slot-answer-label')
        try:
            h.send(callback)
        except _p.PolicyRefused as error:
            self.assertNotIn('canary', repr(error))
            self.assertIsNone(error.__cause__)
            self.assertIsNone(error.__context__)
        else:
            self.fail('uncertain send must raise')
        self.assertEqual(calls, [1])
        result = h.send(callback)
        self.assertEqual(result['status'], 'uncertain')
        self.assertEqual(calls, [1])
        h = _Harness()
        self.assertEqual(h.send()['status'], 'admitted')

    # @anchor:wa.tests.scripts.test_execution.TestExecution.test_admission_first
    def test_admission_first(self):
        h = _Harness()
        entered = _threading.Event()
        released = _threading.Event()
        invalidated = _threading.Event()
        observed = []
        def callback(request, decision):
            entered.set()
            self.assertTrue(released.wait(2))
            observed.append('input')
            return h.fixture.actuator(request, decision)
        def change():
            observed.append('invalidation')
            invalidated.set()
        send_errors = []
        def send():
            try:
                h.send(callback)
            except Exception as error:
                send_errors.append(type(error).__name__)
        thread = _threading.Thread(target=send)
        thread.start()
        self.assertTrue(entered.wait(2))
        mutator = _threading.Thread(target=lambda: h.interlock.invalidate(change))
        mutator.start()
        self.assertFalse(invalidated.wait(0.01))
        released.set()
        thread.join(2)
        mutator.join(2)
        self.assertFalse(thread.is_alive() or mutator.is_alive())
        self.assertEqual(send_errors, [])
        self.assertEqual(observed, ['input', 'invalidation'])
        self.assertEqual(h.fixture.actuator.opened, 1)

    # @anchor:wa.tests.scripts.test_execution.TestExecution.test_bounds_release
    def test_bounds_release(self):
        for bound in (True, 0, None):
            with self.assertRaises(_t.ContractError):
                _a._InputInterlock(installation=_Installation(), max_pending=bound)
        owner = _Installation()
        interlock = _a._InputInterlock(installation=owner, max_pending=1)
        context = interlock.register(object(), guard=lambda *args: _YES)
        with self.assertRaises(_t.ContractError):
            interlock.register(object(), guard=lambda *args: _YES)
        with self.assertRaises(_t.ContractError):
            interlock.release(context, drained=object())
        self.assertEqual(interlock.outstanding(), (context,))
        interlock.release(context, drained=owner.proof)
        self.assertEqual(interlock.outstanding(), ())
        interlock.register(object(), guard=lambda *args: _YES)

    # @anchor:wa.tests.scripts.test_execution.TestExecution.test_interlock_consume
    def test_interlock_consume(self):
        owner = _Installation()
        interlock = _a._InputInterlock(installation=owner, max_pending=1)
        decision = object()
        admission = _types.SimpleNamespace(decision=decision)
        context = interlock.register(admission, guard=lambda *args: _YES)
        calls = []
        interlock.send_once(admission, object(), decision, lambda: calls.append(1), context=context)
        with self.assertRaises(_t.ContractError):
            interlock.send_once(admission, object(), decision, lambda: calls.append(1), context=context)
        self.assertEqual(calls, [1])

    # @anchor:wa.tests.scripts.test_execution.TestExecution.test_foreign_context_cannot_poison_or_read_cache
    def test_foreign_context_cannot_poison_or_read_cache(self):
        h = _Harness()
        for context in (_dc.replace(h.context), object()):
            with self.assertRaises(_p.PolicyRefused):
                h.send(context=context)
            self.assertEqual(h.fixture.policy._results, {})
            self.assertEqual(h.fixture.policy._dispatching, set())
        self.assertEqual(h.send()['status'], 'admitted')
        with self.assertRaises(_p.PolicyRefused):
            h.send(context=_dc.replace(h.context))
        with self.assertRaises(_p.PolicyRefused):
            h.fixture.policy.dispatch(h.admission, h.fixture.actuator)
        self.assertEqual(h.fixture.actuator.opened, 1)
        self.assertEqual(h.send()['status'], 'admitted')

    # @anchor:wa.tests.scripts.test_execution.TestExecution.test_local_script_refusal_does_not_poison
    def test_local_script_refusal_does_not_poison(self):
        h = _Harness()
        local = h.fixture.admit(h.fixture.body('wait', {'duration_ms': 0}, action_id='local', action_seq=1), adapter=h.fixture.adapter(action_id='local'))
        context = h.interlock.register(local, guard=h.guard)
        with self.assertRaises(_p.PolicyRefused):
            h.fixture.policy.dispatch(local, h.fixture.actuator, script_context=context)
        self.assertNotIn(('run-A', 'local'), h.fixture.policy._results)
        self.assertEqual(h.fixture.actuator.opened, 0)
        self.assertEqual(h.send()['status'], 'admitted')

    # @anchor:wa.tests.scripts.test_execution.TestExecution.test_context_check_does_not_hold_policy_lock
    def test_context_check_does_not_hold_policy_lock(self):
        h = _Harness()
        workers = []
        def check(owner):
            acquired = _threading.Event()
            def inspect():
                with h.fixture.policy._lock:
                    acquired.set()
            thread = _threading.Thread(target=inspect, daemon=True)
            workers.append(thread)
            thread.start()
            return _YES if acquired.wait(1) else _NO
        try:
            with _mock.patch.object(h.installation, 'check', side_effect=check):
                self.assertEqual(h.send()['status'], 'admitted')
            self.assertEqual(h.fixture.actuator.opened, 1)
        finally:
            for worker in workers:
                worker.join(1)
                self.assertFalse(worker.is_alive())

    # @anchor:wa.tests.scripts.test_execution.TestExecution.test_mutants
    def test_mutants(self):
        source = _Path(_a.__file__).read_text()
        cases = (
            ('revision', '_need(context.revision == self._revision, _t.Reason.STALE)', 'pass', 'test_invalidation_first'),
            ('guard', '_true(context.guard(admission, request, decision))', 'pass', 'test_guard'),
            ('install', '_true(installation.check(self))', 'pass', 'test_installation'),
            ('identity', 'row[0] is context and context.admission is admission', 'True', 'test_context_identity'),
            ('capacity', '_need(len(self._contexts) < self._limit, _t.Reason.CAPACITY)', 'pass', 'test_bounds_release'),
            ('drain', '_true(self._installation.check_drained(context, drained))', 'pass', 'test_bounds_release'),
            ('consume', 'row[1] = True', 'row[1] = False', 'test_interlock_consume'),
        )
        for label, old, new, test in cases:
            with self.subTest(mutant=label):
                self.assertGreater(source.count(old), 0)
                module = _types.ModuleType('wender.scripts._act_api_mutant')
                module.__package__ = 'wender.scripts'
                _sys.modules[module.__name__] = module
                try:
                    exec(compile(source.replace(old, new), '<act-mutant>', 'exec'), module.__dict__)
                    result = _unittest.TestResult()
                    with _mock.patch(__name__ + '._a', module):
                        TestExecution(test).run(result)
                    self.assertTrue(result.failures, label + ' did not fail assertions')
                    self.assertFalse(result.errors, result.errors)
                    print('MUTANT act-' + label + ' KILLED ' + test)
                finally:
                    del _sys.modules[module.__name__]
        source = _Path(_p.__file__).read_text()
        old = 'return self.script_interlock.send_once(\n                            admission, request, admission.decision, physical_send,\n                            context=script_context)'
        self.assertIn(old, source)
        tree = _ast.parse(source.replace(old, 'return physical_send()'))
        cls = next(n for n in tree.body if isinstance(n, _ast.ClassDef) and n.name == 'ActionPolicy')
        method = next(n for n in cls.body if isinstance(n, _ast.FunctionDef) and n.name == 'dispatch')
        namespace = dict(vars(_p))
        exec(compile(_ast.Module(body=[method], type_ignores=[]), '<policy-mutant>', 'exec'), namespace)
        result = _unittest.TestResult()
        with _mock.patch.object(_p.ActionPolicy, 'dispatch', namespace['dispatch']):
            TestExecution('test_invalidation_first').run(result)
        self.assertTrue(result.failures)
        self.assertFalse(result.errors, result.errors)
        print('MUTANT policy-final-hook KILLED test_invalidation_first')
        tree = _ast.parse(source.replace('if context_rejected:', 'if False:'))
        cls = next(n for n in tree.body if isinstance(n, _ast.ClassDef) and n.name == 'ActionPolicy')
        method = next(n for n in cls.body if isinstance(n, _ast.FunctionDef) and n.name == 'dispatch')
        namespace = dict(vars(_p))
        exec(compile(_ast.Module(body=[method], type_ignores=[]), '<context-mutant>', 'exec'), namespace)
        result = _unittest.TestResult()
        with _mock.patch.object(_p.ActionPolicy, 'dispatch', namespace['dispatch']):
            TestExecution('test_foreign_context_cannot_poison_or_read_cache').run(result)
        self.assertTrue(result.failures)
        self.assertFalse(result.errors, result.errors)
        print('MUTANT policy-context-before-cache KILLED test_foreign_context_cannot_poison_or_read_cache')


from wender.scripts import runner as _r, predicates as _pred, frame_wait as _fw
from wender.learn import script_ledger as _ledger
from tests.scripts.test_store_catalogue import _Host as _StoreHost
from tests.scripts.support import IdentityRegistry as _Registry
from tests import test_p1_wa_settle_clock_unit as _settle_tests
from tests.scripts.test_compiler import _Host as _CompilerHost
from wender.scripts import compiler as _compiler


# @anchor:wa.tests.scripts.test_execution._RuntimeHost
class _RuntimeHost:
    """Synthetic retained owners around real store/ledger/W8U/settle services."""
    # @anchor:wa.tests.scripts.test_execution._RuntimeHost.__init__
    def __init__(self, *, action=_t.ActionKind.CLICK, label='approved-static-label', artifact_n=1):
        self.sh = _StoreHost()
        self.f = self.sh.f
        self.sc = _settle_tests.AreaBControls()
        self.sc.setUp()
        self.clock = self.sc.clock.now
        self.transport = _legacy.ActionPolicyGate()
        self.transport.setUp()
        self.scope, self.authority = self.f['scope'], self.f['authority']
        self.episode_id, self.deadline_ns = 'episode', 1000
        self.restart_evidence = object()
        self.valid = self.risk_ok = self.registry_ok = self.post_ok = True
        self.application_ok = self.semantic_ok = self.drain_ok = self.goal_ok = True
        self.restart_ok = self.classify_ok = self.no_input_ack = self.seal_ok = True
        self.fail_append = self.raise_send = self.deny_resources = False
        self.spoof_binding = self.foreign_principal = False
        self.slots_seen, self.retained, self.tokens = [], [], []
        self.input_count = self.attempts = 0
        self.goal_truth = _t.Truth.FALSE
        self.registered_goal = None
        self.plan = self.sh._plan(n=artifact_n)
        arguments, declarations = (), ()
        if action in (_t.ActionKind.TYPE, _t.ActionKind.KEY):
            kind = _t.ValueKind.TEXT if action is _t.ActionKind.TYPE else _t.ValueKind.KEY
            vocabulary = (_t.HostKeyVocabulary('test', ('Enter', 'Tab'), self.f['handle'])
                          if action is _t.ActionKind.KEY else None)
            arguments = (_t.SlotDescriptor('private', kind, _t.Purpose.INPUT,
                self.f['handle'], self.episode_id, self.scope, 'actuator', 'capability', vocabulary),)
            declarations = (_t.ArgSpec('private', kind, True, 1, 128, _t.Sensitivity.PRIVATE,
                _t.Purpose.INPUT, 'capability', 'actuator', 'wender-args/verbatim-v1'),)
        selector = _dc.replace(self.plan.nodes[0].effect.selector, action=action,
                               canonical_label_reference=label)
        postcondition = self.plan.nodes[0].effect.postcondition
        postcondition = _dc.replace(postcondition, operands=(_dc.replace(postcondition.operands[0],
            value=postcondition.transition_reference.value),))
        effect = _dc.replace(self.plan.nodes[0].effect, action=action, selector=selector,
            arguments=arguments, postcondition=postcondition,
            verification=_t.VerificationBudget(100, 16, 1000))
        node = _dc.replace(self.plan.nodes[0], kind=action, effect=effect)
        nodes = (node, self.plan.nodes[-1])
        header = _dc.replace(self.plan.definition.header, arguments=declarations)
        queries = tuple(_dc.replace(query, selector=selector) for query in self.plan.queries)
        definition = _dc.replace(self.plan.definition, nodes=nodes, slots=declarations,
            header=header, selectors=(selector,), queries=queries)
        self.plan = _dc.replace(self.plan, nodes=nodes, slots=declarations, definition=definition, queries=queries)
        import copy
        self.sh.plans[id(self.plan)] = (self.plan, copy.deepcopy(self.plan))
        self.cell = self.sh._admit(plan=self.plan)
        self.lease = self.sh._acquire(self.cell)
        self.store = self.sh.store
        self.holder_contract = self.lease.holder_contract
        # The store owns this cancellation registration, including the exact token.
        self.sh.holder_grants[id(self.holder_contract)] = (self.holder_contract, self.lease.holder_token)
        self.holder = _t.Holder('runner', _t.Purpose.QUERY, self.scope, 100,
            self.f['handle'], 1000, self.f['handle'], self.sh.cancel)
        self.observation = self.f['observation']
        self.post = _dc.replace(self.observation, publication_identity='after',
            producer_generation=8, capture_start_ns=30, capture_end_ns=40)
        self.binding = _t.TargetBinding(node.effect.selector, self.f['handle'], self.observation,
            _dc.replace(self.f['witness'], supported_actions=(action,)), self.plan.identity, node.step_id, self.scope,
            self.authority, self.f['handle'])
        self.contexts = self
        self.cancellation = _types.SimpleNamespace(request=self._cancel, verify=self.sh.verify)
        install = _types.SimpleNamespace(check=lambda owner: _YES,
            check_drained=lambda context, proof: _YES if id(proof) in self.sh.proofs
                and self.transport.writer.reason(self.transport.handle) is None else _NO)
        self.interlock = _a._InputInterlock(installation=install, max_pending=4)
        self.policy = _p.ActionPolicy(fence=self.transport.fence, clock=self.transport.clock,
                                    script_interlock=self.interlock)
        self.frames = _fw.FrameWait(observations=_types.SimpleNamespace(
            acquire=self._acquire, release=self._release, check=lambda *a, **k: _YES),
            scheduler=_types.SimpleNamespace(wait_until_ns=self._wait),
            control=_types.SimpleNamespace(check=lambda *a: _YES if self.valid else _NO),
            cancellation=self.cancellation, holder_contract=self.holder_contract,
            contexts=self, quiet=_types.SimpleNamespace(check=lambda *a, **k: _YES),
            boundary=_types.SimpleNamespace(check_installation=lambda *a: _YES, check_settle=lambda *a: _YES),
            clock=self.clock, settle_coordinator=self.sc.coordinator)
        self.frame_wait = self.frames
        self.ledger = _ledger.LedgerOwner(producer=self.f['handle'], run_id='run',
            episode_id='episode', artifact=self.plan.identity, authority=self.authority,
            policy=_ledger.LedgerPolicy(8388608, 8, 262144, 32768, 4194304,
                32, 128, 64, 8192, 16, 1000), clock=self.clock,
            check_intent=lambda intent: _YES,
            check_event=lambda event: _NO if self.fail_append else _YES,
            check_seal=lambda seal: _YES if self.seal_ok else _NO,
            check_recovery=lambda *a: _NO, check_reconciliation=lambda *a: _NO)
        self.act_api = _a.ActAPI(store=self.store, ledger=self.ledger,
            binding_owner=_types.SimpleNamespace(bind=self._bind),
            binding_registry=_types.SimpleNamespace(check=self._binding_check),
            contexts=self, risk=_types.SimpleNamespace(check=self._risk),
            authority=_types.SimpleNamespace(check_final=self._final),
            wire=_types.SimpleNamespace(prepare=self._wire, actuator=lambda *a: self,
                accept_input=lambda private, **k: _YES if private is self.f['handle'] else _NO),
            application=_types.SimpleNamespace(confirm=self._confirm, check_post=self._causal),
            interlock=self.interlock, slots=_types.SimpleNamespace(consume_input=self._consume),
            frame_wait=self.frames, clock=self.clock, cancellation=self.cancellation,
            reservations=_types.SimpleNamespace(reserve=self._reserve, release_unused=self.sh.release_unused),
            action_policy=self.policy,
            control=_types.SimpleNamespace(check=self._control, retain_post=self._retain_post),
            requests=_types.SimpleNamespace(request=self._resource_request, holder=self._effect_holder),
            resource_policy=self.sh.policy,
            policy=_a._ExecutionPolicy(4, 4, 4, 1000000, 131072, 131072, 100, 100, 100, 100, 100))
        self.registered_goal = self.plan.terminal_goal
        self.goal_owner = _types.SimpleNamespace(evaluate=self._goal)
        self.seal_owner = _types.SimpleNamespace(finish=self._seal)
        self.classifier = _types.SimpleNamespace(classify=self._classify, acknowledge=self._classification_ack)
        self.find_owner = _types.SimpleNamespace(resolve=lambda spec, **k: _YES)
        self.harvest_owner = _types.SimpleNamespace(acquire=lambda spec, **k: self.f['handle'])
        self.result_consumer = object()
        self.results = _types.SimpleNamespace(deliver=lambda *a: _YES)
        self.state = _r.RunnerState(self.plan, self.lease, self.episode_id, self.scope,
            self.authority, 1000, self, self.observation, self.holder,
            self.holder_contract, self.restart_evidence)
        self.registered = [self.state]

    # @anchor:wa.tests.scripts.test_execution._RuntimeHost._control
    def _control(self, state, **kwargs):
        return _YES if self.valid and self.restart_ok and state.plan is self.plan and state in self.registered else _NO

    # @anchor:wa.tests.scripts.test_execution._RuntimeHost._bind
    def _bind(self, selector, **kwargs):
        if self.spoof_binding:
            return object()
        return self.binding

    # @anchor:wa.tests.scripts.test_execution._RuntimeHost._binding_check
    def _binding_check(self, binding, observation, scope, authority, owner, generation):
        return _YES if self.registry_ok and not self.foreign_principal and binding is self.binding else _NO

    # @anchor:wa.tests.scripts.test_execution._RuntimeHost._risk
    def _risk(self, *args, **kwargs):
        return _YES if self.risk_ok else _t.AdmissionCheck(_t.Truth.FALSE, _t.Reason.CONFLICT)

    # @anchor:wa.tests.scripts.test_execution._RuntimeHost._final
    def _final(self, *args):
        return _YES if self.valid and self.restart_ok else _NO

    # @anchor:wa.tests.scripts.test_execution._RuntimeHost.context
    def context(self, predicate, observation):
        registry = _Registry(self.f['handle'], 7)
        truth = _t.Truth.TRUE if self.semantic_ok or observation is self.observation else _t.Truth.FALSE
        fact = _dc.replace(self.f['fact'], observation=observation,
            source_generation=observation.producer_generation, value=truth,
            source=predicate.source_requirements[0])
        query = _t.QueryDeclaration(predicate.operands[0].query_id, self.binding.selector,
            self.scope, (predicate.operator,))
        association = registry.mint('causal', 'test')
        if predicate.operator is _t.PredicateOperator.EXISTS_UNIQUE:
            inventory = _t.MissingRef('complete', None)
            registry.register(inventory)
            payload = _pred._InventoryEvidence(inventory, ((self.f['handle'], (fact,)),))
        else:
            payload = _pred._TransitionEvidence(predicate.transition_reference,
                self.f['handle'], self.observation, observation, association, (fact,))
        binding = _pred._PredicateEvidence(predicate, query, observation, payload)
        for item in (predicate, observation, self.observation, query, fact, payload, binding, self.f['handle']):
            registry.register(item)
        return dict(evidence=(binding,), registry=registry, owner=self.f['handle'], generation=7,
            observation=observation, max_nodes=32, max_evidence_rows=128,
            max_inventory_rows=64, safety_vetoes=())

    # @anchor:wa.tests.scripts.test_execution._RuntimeHost._resource_request
    def _resource_request(self, intent, binding, *, deadline_ns, policy):
        return _t.ReservationRequest(self.f['handle'], self.f['handle'], self.scope,
            'effect', 1000, (), (), 1, deadline_ns)

    # @anchor:wa.tests.scripts.test_execution._RuntimeHost._effect_holder
    def _effect_holder(self, intent, grant, *, deadline_ns):
        holder = _dc.replace(self.holder_contract, consumer_identity='effect',
                             absolute_deadline_ns=deadline_ns)
        self.sh.holder_grants[id(holder)] = (holder, grant.reservation)
        return holder

    # @anchor:wa.tests.scripts.test_execution._RuntimeHost._reserve
    def _reserve(self, request, policy):
        return _t.ReservationOutcome(None, _t.Reason.CAPACITY) if self.deny_resources else self.sh.reserve(request, policy)

    # @anchor:wa.tests.scripts.test_execution._RuntimeHost._wire
    def _wire(self, intent, binding, *, episode_id, deadline_ns):
        f = self.transport
        body = (f.body('type', dict(target='text-1', text='private-canary-input'))
                if intent.action is _t.ActionKind.TYPE else
                f.body('key', dict(target='text-1', operation='press', key='Tab', modifiers=[]))
                if intent.action is _t.ActionKind.KEY else f.body())
        return _a._WireEffect(body, f.adapter(), f.observation, f.routes,
                             f.frame_evidence, self.f['handle'], self._send)

    # @anchor:wa.tests.scripts.test_execution._RuntimeHost._send
    def _send(self, request, decision):
        self.input_count += 1
        if self.raise_send:
            raise RuntimeError('private-canary-answer-slot-label')
        return self.transport.actuator(request, decision)

    # @anchor:wa.tests.scripts.test_execution._RuntimeHost._consume
    def _consume(self, slot, episode, scope, actuator):
        if slot.episode_id != episode or slot.scope != scope or actuator is not self:
            raise _t.ContractError(_t.Reason.PRIVACY)
        self.slots_seen.append(slot)
        return self.f['handle']

    # @anchor:wa.tests.scripts.test_execution._RuntimeHost._confirm
    def _confirm(self, token, **kwargs):
        if not self.application_ok:
            return None
        return _a._AppliedEffect(self.sc.adapter, self.sc.profile,
            _dc.replace(self.sc.config, max_polls=3), self.sc.snapshot)

    # @anchor:wa.tests.scripts.test_execution._RuntimeHost._causal
    def _causal(self, evidence, post, **kwargs):
        return _YES if self.post_ok and post is self.post else _NO

    # @anchor:wa.tests.scripts.test_execution._RuntimeHost._acquire
    def _acquire(self, after_identity, **kwargs):
        self.attempts += 1
        token = _t.PrivateRef('observation', 'test', str(self.attempts))
        self.tokens.append(token)
        return _t.AcquisitionResult(self.post, None, _t.AcquisitionDisposition.ACQUIRED,
            _t.Ownership.TRANSIENT, token, None, _t.InputDisposition.NOT_SENT)

    # @anchor:wa.tests.scripts.test_execution._RuntimeHost._release
    def _release(self, token):
        if not any(token is item for item in self.tokens):
            return _NO
        self.tokens = [item for item in self.tokens if item is not token]
        return _YES

    # @anchor:wa.tests.scripts.test_execution._RuntimeHost._wait
    def _wait(self, boundary_ns, **kwargs):
        self.sc.clock.set(boundary_ns)

    # @anchor:wa.tests.scripts.test_execution._RuntimeHost._cancel
    def _cancel(self, contract):
        return self.sh.request(contract) if self.drain_ok else _t.CancellationResult(
            True, False, False, False, False, (), None, _t.MissingReason.INCOMPLETE)

    # @anchor:wa.tests.scripts.test_execution._RuntimeHost._retain_post
    def _retain_post(self, state, post, token):
        self.retained.append(post)
        return _YES

    # @anchor:wa.tests.scripts.test_execution._RuntimeHost._goal
    def _goal(self, terminal, **kwargs):
        return (_t.PredicateResult(self.goal_truth, None) if terminal is self.registered_goal and self.goal_ok
                and self.goal_truth is not _t.Truth.UNKNOWN else _a._unknown())

    # @anchor:wa.tests.scripts.test_execution._RuntimeHost._seal
    def _seal(self, outcome, **kwargs):
        if outcome.input_disposition is _t.InputDisposition.NOT_SENT:
            return _YES if self.no_input_ack else _NO
        events, head, _, _ = self.ledger.snapshot()
        return _ledger.EpisodeSeal('wa.script.seal/v1', self.f['handle'], 'run', 'episode',
            self.plan.identity, self.scope, len(events), head, True, self.plan.terminal_goal,
            self.authority, (self.plan.identity.plan,) * 5, self.f['handle'], self.f['handle'])

    # @anchor:wa.tests.scripts.test_execution._RuntimeHost._classify
    def _classify(self, state, *, reason):
        cause = (_t.FailureClass.EPISODE_DEADLINE if reason is _t.Reason.DEADLINE else
                 _t.FailureClass.EPISODE_CANCELLATION if reason is _t.Reason.CANCELLED else
                 _t.FailureClass.ARTIFACT_DEFECT)
        return self.sh._classification(self.cell, cause)

    # @anchor:wa.tests.scripts.test_execution._RuntimeHost._classification_ack
    def _classification_ack(self, classification, artifact):
        if not self.classify_ok:
            return _NO
        self.classification = classification
        if classification.cause in (_t.FailureClass.EPISODE_DEADLINE, _t.FailureClass.EPISODE_CANCELLATION):
            return _YES
        sequence = self.store.revoke(classification, target_digest=artifact.artifact)
        if type(sequence) is not int or sequence <= 0:
            return _NO
        return _YES

    # @anchor:wa.tests.scripts.test_execution._RuntimeHost.register
    def register(self, state):
        self.registered.append(state)
        return _YES

    # @anchor:wa.tests.scripts.test_execution._RuntimeHost.check
    def check(self, state):
        return self._control(state)

    # @anchor:wa.tests.scripts.test_execution._RuntimeHost.retain_observation
    def retain_observation(self, state, observation):
        if observation in self.retained:
            return _YES
        if self.tokens and observation is self.post:
            self.retained.append(observation)
            return _YES
        return _NO

    # @anchor:wa.tests.scripts.test_execution._RuntimeHost.audit_read
    def audit_read(self, state, node, result):
        return _YES

    # @anchor:wa.tests.scripts.test_execution._RuntimeHost.drain
    def drain(self, state, *, proof):
        if not self.drain_ok:
            return _NO
        for token in state.owned_resources:
            if self._release(token).result is not _t.Truth.TRUE:
                return _NO
        state.owned_resources.clear()
        return _YES if not self.tokens else _NO


# @anchor:wa.tests.scripts.test_execution.TestRuntime
class TestRuntime(_unittest.TestCase):
    # @anchor:wa.tests.scripts.test_execution.TestRuntime.test_verified_effect
    def test_verified_effect(self):
        h = _RuntimeHost()
        result = h.act_api.dispatch_effect(h.state, h.act_api.prepare_effect(h.state, h.plan.nodes[0]))
        self.assertIs(result.checkpoint.value, _t.Truth.TRUE)
        self.assertIs(result.input_disposition, _t.InputDisposition.APPLIED)
        self.assertEqual(h.input_count, 1)
        self.assertEqual(h.attempts, 1)
        self.assertEqual(h.tokens, [])
        self.assertEqual(h.act_api.outstanding(), ())
        self.assertEqual(h.interlock.outstanding(), ())
        self.assertEqual([e.state for e in h.ledger.snapshot()[0]],
                         [_t.EventState.ADMITTED, _t.EventState.APPLIED, _t.EventState.VERIFIED])

    # @anchor:wa.tests.scripts.test_execution.TestRuntime.test_risk_facts_principal
    def test_risk_facts_principal(self):
        for flag, reason in (('risk_ok', _t.Reason.CONFLICT),
                             ('registry_ok', _t.Reason.MISSING),
                             ('foreign_principal', _t.Reason.MISSING)):
            h = _RuntimeHost()
            setattr(h, flag, flag == 'foreign_principal')
            with self.assertRaises(_t.ContractError) as caught:
                h.act_api.prepare_effect(h.state, h.plan.nodes[0])
            self.assertIs(caught.exception.reason, reason)
            self.assertEqual(h.input_count, 0)
            self.assertIsNone(h.state.pending_effect)
        for name, truth in (('visible', _t.Truth.FALSE), ('enabled', _t.Truth.UNKNOWN),
                            ('actionable', _t.Truth.FALSE), ('busy', _t.Truth.TRUE), ('blocked', _t.Truth.TRUE)):
            h = _RuntimeHost()
            fact = _dc.replace(getattr(h.binding.witness.facts, name), value=truth,
                reason=_t.MissingReason.INCOMPLETE if truth is _t.Truth.UNKNOWN else None)
            facts = _dc.replace(h.binding.witness.facts, **{name: fact})
            h.binding = _dc.replace(h.binding, witness=_dc.replace(h.binding.witness, facts=facts))
            with self.assertRaises(_t.ContractError) as caught:
                h.act_api.prepare_effect(h.state, h.plan.nodes[0])
            self.assertIs(caught.exception.reason, _t.Reason.CONFLICT)
            self.assertEqual(h.input_count, 0)
        self.test_verified_effect()

    # @anchor:wa.tests.scripts.test_execution.TestRuntime.test_postconditions_and_no_retry
    def test_postconditions_and_no_retry(self):
        for flag in ('application_ok', 'post_ok', 'semantic_ok'):
            h = _RuntimeHost()
            setattr(h, flag, False)
            p = h.act_api.prepare_effect(h.state, h.plan.nodes[0])
            result = h.act_api.dispatch_effect(h.state, p)
            self.assertIsNot(result.checkpoint.value, _t.Truth.TRUE)
            self.assertIs(result.input_disposition, _t.InputDisposition.INPUT_UNCERTAIN
                          if flag == 'application_ok' else _t.InputDisposition.APPLIED)
            self.assertTrue(h.state.stopped)
            self.assertIs(h.act_api.dispatch_effect(h.state, p), result)
            self.assertEqual(h.input_count, 1)
        self.test_verified_effect()

    # @anchor:wa.tests.scripts.test_execution.TestRuntime.test_callback_and_audit_failure
    def test_callback_and_audit_failure(self):
        for mode in ('send', 'append'):
            h = _RuntimeHost()
            p = h.act_api.prepare_effect(h.state, h.plan.nodes[0])
            if mode == 'send':
                h.raise_send = True
            else:
                original = h.transport.actuator
                def callback(*args):
                    h.fail_append = True
                    return original(*args)
                h.transport.actuator = callback
            result = h.act_api.dispatch_effect(h.state, p)
            self.assertIsNot(result.checkpoint.value, _t.Truth.TRUE)
            self.assertNotIn('canary', repr(result))
            self.assertTrue(h.state.stopped)
            self.assertIs(h.act_api.dispatch_effect(h.state, p), result)
            self.assertEqual(h.input_count, 1)
        self.test_verified_effect()

    # @anchor:wa.tests.scripts.test_execution.TestRuntime.test_fence_revision_restart_deadline
    def test_fence_revision_restart_deadline(self):
        for mode in ('revoke', 'restart', 'deadline', 'stop', 'binding'):
            h = _RuntimeHost()
            p = h.act_api.prepare_effect(h.state, h.plan.nodes[0])
            if mode == 'deadline':
                h.sc.clock.set(1001)
            elif mode == 'restart':
                h.restart_ok = False
            elif mode == 'stop':
                h.state.stopped = True
            elif mode == 'binding':
                h.binding = _dc.replace(h.binding)
            else:
                h.interlock.invalidate(lambda: setattr(h, 'valid', False))
            result = h.act_api.dispatch_effect(h.state, p)
            self.assertIsNot(result.checkpoint.value, _t.Truth.TRUE)
            self.assertEqual(h.input_count, 0)
        self.test_verified_effect()

    # @anchor:wa.tests.scripts.test_execution.TestRuntime.test_resource_and_argument_bounds
    def test_resource_and_argument_bounds(self):
        h = _RuntimeHost()
        h.deny_resources = True
        with self.assertRaises(_t.ContractError) as caught:
            h.act_api.prepare_effect(h.state, h.plan.nodes[0])
        self.assertIs(caught.exception.reason, _t.Reason.CAPACITY)
        self.assertEqual(h.input_count, 0)
        self.assertEqual(h.ledger.snapshot()[0], ())
        for value in (True, 0, None):
            with self.assertRaises(_t.ContractError):
                _a._ExecutionPolicy(value, 4, 4, 1000, 1000, 1000, 100, 100, 100, 100, 100)
        self.test_verified_effect()

    # @anchor:wa.tests.scripts.test_execution.TestRuntime.test_goal_and_no_input_ack
    def test_goal_and_no_input_ack(self):
        for truth in (_t.Truth.FALSE, _t.Truth.UNKNOWN, _t.Truth.TRUE):
            h = _RuntimeHost()
            h.state.checkpoint = _t.PredicateResult(_t.Truth.TRUE, None)
            h.goal_truth = truth
            result = _r.finish(h.state)
            self.assertEqual(result.status is _t.TerminalStatus.COMPLETED, truth is _t.Truth.TRUE)
            self.assertEqual(h.input_count, 0)
            self.assertEqual(h.ledger.snapshot()[0], ())
        for flag in ('no_input_ack', 'goal_ok', 'drain_ok'):
            h = _RuntimeHost()
            h.state.checkpoint = _t.PredicateResult(_t.Truth.TRUE, None)
            h.goal_truth = _t.Truth.TRUE
            setattr(h, flag, False)
            self.assertIsNot(_r.finish(h.state).status, _t.TerminalStatus.COMPLETED)

    # @anchor:wa.tests.scripts.test_execution.TestRuntime.test_runner_trace_and_reload
    def test_runner_trace_and_reload(self):
        h = _RuntimeHost()
        old = h.state.plan
        published = h.store.publish_default(h.cell.key, scope=h.scope, expected_predecessor_revision=0, certificate=h.sh.cert)
        successor_plan = h.sh._plan(n=2, logical=old.identity.logical_id, revision=2)
        h.latest_plan = successor_plan
        successor = h.sh._admit(plan=successor_plan)
        successor = h.store.publish_default(successor.key, scope=h.scope,
            expected_predecessor_revision=published.lifecycle_revision, certificate=h.sh.cert)
        self.assertIs(_r.advance(h.state), h.state)
        self.assertEqual(h.state.cursor, 1)
        self.assertIs(h.state.plan, old)
        h.goal_truth = _t.Truth.TRUE
        outcome = _r.advance(h.state)
        self.assertIs(outcome.status, _t.TerminalStatus.COMPLETED)
        self.assertEqual(h.input_count, 1)
        self.assertIs(_r.advance(h.state), outcome)
        self.assertEqual(h.input_count, 1)
        self.assertNotEqual(successor.identity, old.identity)
        fresh = h.sh._acquire(successor)
        self.assertEqual(fresh.artifact, successor.identity)
        self.assertIs(h.store.release(fresh, fresh.holder_token).result, _t.Truth.TRUE)

    # @anchor:wa.tests.scripts.test_execution.TestRuntime.test_classification_and_holder_proof
    def test_classification_and_holder_proof(self):
        h = _RuntimeHost()
        result = _r.abort(h.state, _t.Reason.DEADLINE)
        self.assertIs(result.failure, _t.FailureClass.EPISODE_DEADLINE)
        self.assertIs(result.owner_drained.value, _t.Truth.TRUE)
        # Episode deadline must not revoke the artifact globally.
        fresh = h.sh._acquire(h.cell)
        self.assertIs(h.store.check_lease(fresh, fresh.artifact, h.scope, 'episode',
            fresh.process_incarnation, fresh.retained_owner).result, _t.Truth.TRUE)
        h = _RuntimeHost()
        h.drain_ok = False
        outcome = _r.abort(h.state, _t.Reason.CANCELLED)
        self.assertTrue(h.act_api.service_stopped)
        self.assertIsNot(outcome.owner_drained.value, _t.Truth.TRUE)
        h = _RuntimeHost()
        h.classify_ok = False
        _r.abort(h.state, _t.Reason.CONFLICT)
        self.assertTrue(h.act_api.service_stopped)

    # @anchor:wa.tests.scripts.test_execution.TestRuntime.test_forged_pairing
    def test_forged_pairing(self):
        h = _RuntimeHost()
        with self.assertRaises(_t.ContractError):
            _r.start(h.plan, lease=h.lease, session=None, admission=None,
                     inputs=None, registry=None, runtime=h)
        self.assertEqual(h.input_count, 0)

    # @anchor:wa.tests.scripts.test_execution.TestRuntime.test_start_registered_pairing
    def test_start_registered_pairing(self):
        host = _CompilerHost(b'def run(act, params):\n    return None\n')
        # Trusted fixture supplies the principal attestation before compilation.
        from tests.scripts.test_compiler import _rewrite_header
        from tests.scripts.test_header import _wire_record
        scope = _dc.replace(host.scope, attestation=host.owner, attestation_reason=None)
        memo = {}
        def rescope(value):
            if type(value) is _t.ScopeIdentity:
                return scope
            if type(value) in (_t.PrivateRef, _t.RegistryRef):
                return value
            if id(value) in memo:
                return memo[id(value)]
            if _dc.is_dataclass(value):
                result = _dc.replace(value, **{field.name: rescope(getattr(value, field.name))
                                                for field in _dc.fields(value)})
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
            plan = _compiler.validate_plan(definition, session=host.session,
                admission=host.admitter, inputs=host.config, registry=host.registry)
            h = _RuntimeHost()
            h.plan, h.scope = plan, plan.scope
            h.registered_goal = plan.terminal_goal
            h.sh.scope = h.scope
            import copy
            h.sh.plans[id(plan)] = (plan, copy.deepcopy(plan))
            cell = h.sh._admit(plan=plan)
            h.lease = h.sh._acquire(cell)
            h.holder_contract = h.lease.holder_contract
            h.sh.holder_grants[id(h.holder_contract)] = (h.holder_contract, h.lease.holder_token)
            h.holder = _dc.replace(h.holder, scope=h.scope)
            h.observation = _dc.replace(h.observation, scope=h.scope)
            h.goal_truth = _t.Truth.TRUE
            state = _r.start(plan, lease=h.lease, session=host.session,
                admission=host.admitter, inputs=host.config, registry=host.registry, runtime=h)
            self.assertIs(state.outcome.status, _t.TerminalStatus.COMPLETED)
            self.assertIs(state.input_disposition, _t.InputDisposition.NOT_SENT)
            self.assertEqual(h.ledger.snapshot()[0], ())
            self.assertEqual(h.input_count, 0)
        finally:
            host.admitter.finish(host.session)

    # @anchor:wa.tests.scripts.test_execution.TestRuntime.test_consumed_node_cannot_remint
    def test_consumed_node_cannot_remint(self):
        h = _RuntimeHost()
        prepared = h.act_api.prepare_effect(h.state, h.plan.nodes[0])
        self.assertIs(h.act_api.dispatch_effect(h.state, prepared).checkpoint.value, _t.Truth.TRUE)
        before = len(h.sh.grants)
        with self.assertRaises(_t.ContractError) as caught:
            h.act_api.prepare_effect(h.state, h.plan.nodes[0])
        self.assertIs(caught.exception.reason, _t.Reason.RECONCILIATION)
        self.assertIsNone(h.state.pending_effect)
        self.assertEqual(len(h.sh.grants), before)
        self.assertEqual(h.input_count, 1)

    # @anchor:wa.tests.scripts.test_execution.TestRuntime.test_final_append_ack_required
    def test_final_append_ack_required(self):
        h = _RuntimeHost()
        prepared = h.act_api.prepare_effect(h.state, h.plan.nodes[0])
        append = h.ledger.append
        calls = []
        def missing_ack(reservation, event):
            result = append(reservation, event)
            calls.append(event.state)
            return _NO if event.state is _t.EventState.VERIFIED else result
        with _mock.patch.object(h.ledger, 'append', side_effect=missing_ack):
            result = h.act_api.dispatch_effect(h.state, prepared)
        self.assertIsNot(result.checkpoint.value, _t.Truth.TRUE)
        self.assertIs(result.input_disposition, _t.InputDisposition.APPLIED)
        self.assertTrue(h.state.stopped)
        self.assertEqual(h.input_count, 1)
        self.test_verified_effect()

    # @anchor:wa.tests.scripts.test_execution.TestRuntime.test_missing_settle_prevents_checkpoint
    def test_missing_settle_prevents_checkpoint(self):
        for mode in ('busy', 'missing', 'wrong-turn', 'visual', 'dom_layout', 'scene_tick'):
            h = _RuntimeHost()
            snapshot = h.sc.snapshot
            def signal_reader():
                signals = snapshot(inflight=1 if mode == 'busy' else 0,
                                   quiet={mode: False} if mode in ('visual', 'dom_layout', 'scene_tick') else None)
                if mode == 'missing':
                    signals.pop(next(iter(signals)))
                elif mode == 'wrong-turn':
                    key = next(iter(signals))
                    signals[key] = _dc.replace(signals[key], action_id='other-turn')
                return signals
            with _mock.patch.object(h.sc, 'snapshot', side_effect=signal_reader):
                p = h.act_api.prepare_effect(h.state, h.plan.nodes[0])
                result = h.act_api.dispatch_effect(h.state, p)
            self.assertIsNot(result.checkpoint.value, _t.Truth.TRUE)
            self.assertIs(result.input_disposition, _t.InputDisposition.APPLIED)
            self.assertEqual(h.input_count, 1)
        self.test_verified_effect()

    # @anchor:wa.tests.scripts.test_execution.TestRuntime.test_prepared_pin_and_revoked_lease
    def test_prepared_pin_and_revoked_lease(self):
        h = _RuntimeHost()
        p = h.act_api.prepare_effect(h.state, h.plan.nodes[0])
        p.plan = h.sh._plan(n=2)
        with self.assertRaises(_t.ContractError) as caught:
            h.act_api.dispatch_effect(h.state, p)
        self.assertIs(caught.exception.reason, _t.Reason.FOREIGN)
        self.assertEqual(h.input_count, 0)
        h = _RuntimeHost()
        p = h.act_api.prepare_effect(h.state, h.plan.nodes[0])
        h.interlock.invalidate(lambda: h.store.revoke(
            h.sh._classification(h.cell, _t.FailureClass.ARTIFACT_DEFECT),
            target_digest=h.plan.identity.artifact))
        result = h.act_api.dispatch_effect(h.state, p)
        self.assertIsNot(result.checkpoint.value, _t.Truth.TRUE)
        self.assertEqual(h.input_count, 0)
        self.test_verified_effect()

    # @anchor:wa.tests.scripts.test_execution.TestRuntime.test_aggregate_acquisition_budget
    def test_aggregate_acquisition_budget(self):
        h = _RuntimeHost()
        h.state.acquisitions_remaining = 1
        p = h.act_api.prepare_effect(h.state, h.plan.nodes[0])
        result = h.act_api.dispatch_effect(h.state, p)
        self.assertIsNot(result.checkpoint.value, _t.Truth.TRUE)
        self.assertEqual(h.attempts, 1)
        self.assertEqual(h.state.acquisitions_remaining, 0)
        self.test_verified_effect()

    # @anchor:wa.tests.scripts.test_execution.TestRuntime.test_type_key_capabilities_and_host_risk
    def test_type_key_capabilities_and_host_risk(self):
        for action in (_t.ActionKind.TYPE, _t.ActionKind.KEY):
            h = _RuntimeHost(action=action)
            result = h.act_api.dispatch_effect(h.state, h.act_api.prepare_effect(h.state, h.plan.nodes[0]))
            self.assertIs(result.checkpoint.value, _t.Truth.TRUE)
            self.assertEqual(h.input_count, 1)
            self.assertEqual(len(h.slots_seen), 1)
            self.assertIs(h.slots_seen[0], h.plan.nodes[0].effect.arguments[0])
            self.assertNotIn('canary', repr(result))
            h = _RuntimeHost(action=action)
            with _mock.patch.object(h.act_api.slots, 'consume_input', side_effect=_t.ContractError(_t.Reason.PRIVACY)):
                result = h.act_api.dispatch_effect(h.state, h.act_api.prepare_effect(h.state, h.plan.nodes[0]))
            self.assertIsNot(result.checkpoint.value, _t.Truth.TRUE)
            self.assertEqual(h.input_count, 0)
        for label, action in (('harmless-delete', _t.ActionKind.CLICK),
                              ('harmless-submit', _t.ActionKind.CLICK),
                              ('billing-account', _t.ActionKind.CLICK),
                              ('enter-submission', _t.ActionKind.KEY)):
            h = _RuntimeHost(action=action, label=label)
            h.risk_ok = False
            with self.assertRaises(_t.ContractError) as caught:
                h.act_api.prepare_effect(h.state, h.plan.nodes[0])
            self.assertIs(caught.exception.reason, _t.Reason.CONFLICT)
            self.assertEqual(h.input_count, 0)
        for name in ('editable', 'focused'):
            h = _RuntimeHost(action=_t.ActionKind.TYPE)
            facts = h.binding.witness.facts
            facts = _dc.replace(facts, **{name: _dc.replace(getattr(facts, name), value=_t.Truth.FALSE)})
            h.binding = _dc.replace(h.binding, witness=_dc.replace(h.binding.witness, facts=facts))
            with self.assertRaises(_t.ContractError):
                h.act_api.prepare_effect(h.state, h.plan.nodes[0])
            self.assertEqual(h.input_count, 0)

    # @anchor:wa.tests.scripts.test_execution.TestRuntime.test_read_only_interpreter_and_result_privacy
    def test_read_only_interpreter_and_result_privacy(self):
        for mode in ('good', 'find-denied', 'assert-denied', 'harvest-overflow',
                     'harvest-wrong-turn', 'harvest-missing-terminal', 'consumer-denied'):
            h = _RuntimeHost()
            query = h.plan.queries[0]
            result = _t.ResultDeclaration('answer', _t.ValueKind.TEXT, _t.Purpose.HARVEST, 'consumer', 128)
            authorization = _t.RegistryRef('harvest', 'test-incarnation', 1, h.f['handle'])
            harvest = _t.HarvestSpec(query.name, result, authorization, h.scope, 128)
            wait = _t.WaitSpec(h.plan.entry, _t.WaitPolicy(100, 100, 1, 1000, 50, 100, 20))
            nodes = (
                _t.StepPlan('find', _t.ActionKind.FIND, None, None, None, None, find=_t.FindSpec(query)),
                _t.StepPlan('wait', _t.ActionKind.WAIT_UNTIL, None, None, None, None, wait=wait),
                _t.StepPlan('assert', _t.ActionKind.ASSERT_TAG, None, None, None, None,
                    assertion=_t.AssertSpec(h.plan.entry, _t.Reason.CONFLICT)),
                _t.StepPlan('harvest', _t.ActionKind.HARVEST, None, None, None, None, harvest=harvest),
                h.plan.nodes[-1])
            limits = _dc.replace(h.plan.limits, max_nodes=8)
            header = _dc.replace(h.plan.definition.header, limits=limits)
            definition = _dc.replace(h.plan.definition, nodes=nodes, symbols=(result,), limits=limits, header=header)
            h.plan = _dc.replace(h.plan, nodes=nodes, symbols=(result,), definition=definition, limits=limits)
            h.state.plan = h.plan
            if mode == 'find-denied':
                h.find_owner.resolve = lambda *a, **k: _NO
            if mode == 'assert-denied':
                h.audit_read = lambda *a: _NO
            if mode.startswith('harvest-'):
                def deny(*a, **k):
                    raise _t.ContractError(_t.Reason.PRIVACY)
                h.harvest_owner.acquire = deny
            delivered = []
            def deliver(private, episode, scope, purpose, consumer):
                if mode == 'consumer-denied':
                    return _NO
                self.assertIs(private, h.f['handle'])
                self.assertEqual((episode, scope, purpose), ('episode', h.scope, _t.Purpose.HARVEST))
                self.assertIs(consumer, h.result_consumer)
                delivered.append(private)
                return _YES
            h.results.deliver = deliver
            for _ in nodes[:-1]:
                value = _r.advance(h.state)
                if h.state.outcome is not None:
                    break
            if mode == 'good':
                self.assertEqual(len(delivered), 1)
                self.assertEqual(h.state.cursor, 4)
                h.goal_truth = _t.Truth.TRUE
                self.assertIs(_r.advance(h.state).status, _t.TerminalStatus.COMPLETED)
            else:
                self.assertIs(h.state.outcome.status, _t.TerminalStatus.REFUSED)
            self.assertEqual(h.input_count, 0)
            self.assertEqual(h.tokens, [])
            self.assertNotIn('canary', repr(h.state.outcome))

    # @anchor:wa.tests.scripts.test_execution.TestRuntime.test_old_frame_and_length_is_not_equality
    def test_old_frame_and_length_is_not_equality(self):
        h = _RuntimeHost()
        h.post = h.observation
        p = h.act_api.prepare_effect(h.state, h.plan.nodes[0])
        result = h.act_api.dispatch_effect(h.state, p)
        self.assertIsNot(result.checkpoint.value, _t.Truth.TRUE)
        self.assertIs(result.input_disposition, _t.InputDisposition.APPLIED)
        h = _RuntimeHost()
        original = h.context
        def context(predicate, observation):
            value = original(predicate, observation)
            if observation is h.post:
                binding = value['evidence'][0]
                payload = binding.payload
                length = _pred._LengthEvidence(payload.target, payload.before, payload.after,
                    0, 1, 'characters', 'characters', payload.transition_reference,
                    payload.causal_association, payload.facts)
                value['registry'].register(length)
                binding = value['registry'].register(_dc.replace(binding, payload=length))
                value['evidence'] = (binding,)
            return value
        with _mock.patch.object(h, 'context', side_effect=context):
            p = h.act_api.prepare_effect(h.state, h.plan.nodes[0])
            result = h.act_api.dispatch_effect(h.state, p)
        self.assertIsNot(result.checkpoint.value, _t.Truth.TRUE)
        self.assertIs(result.input_disposition, _t.InputDisposition.APPLIED)
        self.test_verified_effect()

    # @anchor:wa.tests.scripts.test_execution.TestRuntime.test_late_writer_acknowledgement
    def test_late_writer_acknowledgement(self):
        h = _RuntimeHost()
        prepared = h.act_api.prepare_effect(h.state, h.plan.nodes[0])
        original = h.transport.actuator
        def late(request, decision):
            value = original(request, decision)
            h.sc.clock.set(1001)
            return value
        h.transport.actuator = late
        result = h.act_api.dispatch_effect(h.state, prepared)
        self.assertIsNot(result.checkpoint.value, _t.Truth.TRUE)
        self.assertIs(result.input_disposition, _t.InputDisposition.INPUT_UNCERTAIN)
        self.assertIs(result.checkpoint.reason, _t.Reason.DEADLINE)
        self.assertEqual(h.input_count, 1)
        self.assertIs(h.act_api.dispatch_effect(h.state, prepared), result)
        self.assertEqual(h.input_count, 1)
        self.test_verified_effect()

    # @anchor:wa.tests.scripts.test_execution.TestRuntime.test_step_close_ack_and_exact_drain
    def test_step_close_ack_and_exact_drain(self):
        for mode in ('close', 'drain'):
            h = _RuntimeHost()
            p = h.act_api.prepare_effect(h.state, h.plan.nodes[0])
            if mode == 'close':
                close = h.store.end_step
                def end(*args):
                    close(*args)
                    return _NO
                with _mock.patch.object(h.store, 'end_step', side_effect=end):
                    result = h.act_api.dispatch_effect(h.state, p)
            else:
                h.drain_ok = False
                result = h.act_api.dispatch_effect(h.state, p)
            self.assertIsNot(result.checkpoint.value, _t.Truth.TRUE)
            self.assertTrue(h.state.stopped)
            self.assertTrue(h.act_api.service_stopped)
            self.assertEqual(h.input_count, 1)
            self.assertEqual(h.act_api.outstanding(), (p,))
            self.assertTrue(h.sh.grants)
        self.test_verified_effect()

    # @anchor:wa.tests.scripts.test_execution.TestRuntime.test_control_mutants
    def test_control_mutants(self):
        cases = (
            ('lower-custody', _a, 'prepared.disposition = _t.InputDisposition.INPUT_UNCERTAIN\n            response', 'response', 'test_final_guard_preserves_lower_custody_uncertainty'),
            ('reload-cursor', _r, 'node = state.plan.nodes[state.cursor]\n            if type(interpreted)', 'node = state.runtime.latest_plan.nodes[state.cursor]\n            if type(interpreted)', 'test_runner_trace_and_reload'),
            ('private-exception', _a, 'return call', 'return function', 'test_private_owner_exceptions_stay_closed'),
            ('deadline-classification', _r, '_act._need(classification.cause is _t.FailureClass.EPISODE_DEADLINE, _t.Reason.CONFLICT)', 'pass', 'test_deadline_classification_cannot_globalize'),
            ('risk', _a, '_true(api.risk.check(intent, binding, episode_id=state.episode_id, scope=state.scope,\n                             authority=state.authority, deadline_ns=deadline))', 'pass', 'test_risk_facts_principal'),
            ('facts', _a, 'fact.value is expected', 'True', 'test_risk_facts_principal'),
            ('principal', _a, '_true(api.binding_registry.check(binding, state.observation, state.scope,\n                                    state.authority, api.binding_owner, state.observation.producer_generation))', 'pass', 'test_risk_facts_principal'),
            ('causal-post', _a, '_true(api.application.check_post(evidence, post, action_token=prepared.wire.action_token,\n                pre=pre, scope=state.scope, deadline_ns=prepared.deadline_ns))', 'pass', 'test_postconditions_and_no_retry'),
            ('semantic-post', _a, '_passed(predicate)', 'pass', 'test_postconditions_and_no_retry'),
            ('settle', _a, 'api.frame_wait.settle_effect(evidence.adapter, evidence.profile, settle_config,\n                evidence.signal_reader, cancel=state.holder, scope=state.scope)', 'None', 'test_missing_settle_prevents_checkpoint'),
            ('audit-ack', _a, '_true(self.ledger.append(prepared.reservation, event))', 'self.ledger.append(prepared.reservation, event)', 'test_final_append_ack_required'),
            ('step-ack', _a, '_true(api.store.end_step(prepared.permit, result, state.scope, state.authority))', 'api.store.end_step(prepared.permit, result, state.scope, state.authority)', 'test_step_close_ack_and_exact_drain'),
            ('no-remint', _a, '_need(action_key not in api._actions, _t.Reason.RECONCILIATION)', 'pass', 'test_consumed_node_cannot_remint'),
            ('pinned-plan', _a, 'prepared.plan is state.plan', 'True', 'test_prepared_pin_and_revoked_lease'),
            ('restart', _a, '_true(self.control.check(state, intent=intent, permit=permit, policy=self.policy))', 'pass', 'test_restart_preparation_before_binding'),
            ('private-capability', _a, 'private = api.slots.consume_input(slot, state.episode_id, state.scope, actuator)', 'private = slot.capability', 'test_type_key_capabilities_and_host_risk'),
            ('bool-budget', _a, 'type(value) is int and value > 0', 'isinstance(value, int) and value > 0', 'test_resource_and_argument_bounds'),
            ('shared-acquisitions', _a, '_need(evidence.config.max_polls <= state.acquisitions_remaining, _t.Reason.BOUNDS)', 'pass', 'test_aggregate_acquisition_budget'),
            ('independent-goal', _r, 'goal = _goal(state)', 'goal = state.checkpoint', 'test_goal_and_no_input_ack'),
            ('no-input-ack', _r, '_act._true(ack)', 'pass', 'test_goal_and_no_input_ack'),
            ('classification-ack', _r, '_act._true(state.runtime.classifier.acknowledge(classification, state.plan.identity))', 'pass', 'test_classification_and_holder_proof'),
        )
        for label, original, old, new, test in cases:
            with self.subTest(mutant=label):
                source = _Path(original.__file__).read_text()
                self.assertEqual(source.count(old), 1)
                module = _types.ModuleType('wender.scripts._runtime_mutant')
                module.__package__ = 'wender.scripts'
                _sys.modules[module.__name__] = module
                try:
                    exec(compile(source.replace(old, new, 1), '<runtime-mutant>', 'exec'), module.__dict__)
                    result = _unittest.TestResult()
                    alias = '._a' if original is _a else '._r'
                    with _mock.patch(__name__ + alias, module):
                        TestRuntime(test).run(result)
                    self.assertTrue(result.failures, label + ' survived')
                    self.assertFalse(result.errors, result.errors)
                    print('MUTANT runtime-' + label + ' KILLED ' + test)
                finally:
                    del _sys.modules[module.__name__]

    # @anchor:wa.tests.scripts.test_execution.TestRuntime.test_restart_preparation_before_binding
    def test_restart_preparation_before_binding(self):
        h = _RuntimeHost()
        h.restart_ok = False
        with _mock.patch.object(h.act_api.binding_owner, 'bind', wraps=h.act_api.binding_owner.bind) as bind:
            with self.assertRaises(_t.ContractError):
                h.act_api.prepare_effect(h.state, h.plan.nodes[0])
        self.assertEqual(bind.call_count, 0)
        self.assertEqual(h.input_count, 0)
        self.assertIsNone(h.state.pending_effect)
        h.restart_ok = True
        self.assertIs(h.act_api.dispatch_effect(h.state,
            h.act_api.prepare_effect(h.state, h.plan.nodes[0])).checkpoint.value, _t.Truth.TRUE)

    # @anchor:wa.tests.scripts.test_execution.TestRuntime.test_prepare_faults_release_only_acquired_owners
    def test_prepare_faults_release_only_acquired_owners(self):
        for stage in ('reserve', 'ledger', 'permit', 'wire', 'admit', 'register'):
            h = _RuntimeHost()
            ports = {'reserve': (h.act_api.reservations, 'reserve'), 'ledger': (h.ledger, 'reserve'),
                'permit': (h.store, 'begin_step'), 'wire': (h.act_api.wire, 'prepare'),
                'admit': (h.policy, 'admit'), 'register': (h.interlock, 'register')}
            owner, method = ports[stage]
            with _mock.patch.object(owner, method, side_effect=RuntimeError('private-canary-prepare')):
                with self.assertRaises(_t.ContractError) as caught:
                    h.act_api.prepare_effect(h.state, h.plan.nodes[0])
            self.assertNotIn('canary', repr(caught.exception))
            self.assertIsNone(caught.exception.__context__)
            outcome = _r.abort(h.state, caught.exception.reason)
            self.assertIs(outcome.owner_drained.value, _t.Truth.TRUE, stage)
            self.assertEqual(h.act_api.outstanding(), (), stage)
            self.assertEqual(h.interlock.outstanding(), (), stage)
            self.assertEqual(h.input_count, 0)
            # Store's own resident artifacts stay charged; every effect grant refunds.
            self.assertFalse(any(grant.request.operation == 'effect' for grant in h.sh.grants.values()))
        self.test_verified_effect()

    # @anchor:wa.tests.scripts.test_execution.TestRuntime.test_final_guard_preserves_lower_custody_uncertainty
    def test_final_guard_preserves_lower_custody_uncertainty(self):
        h = _RuntimeHost()
        p = h.act_api.prepare_effect(h.state, h.plan.nodes[0])
        # Binding changes only at the final guard: W8U has already marked custody.
        h.binding = _dc.replace(h.binding)
        result = h.act_api.dispatch_effect(h.state, p)
        self.assertEqual(h.input_count, 0)
        self.assertIs(result.input_disposition, _t.InputDisposition.INPUT_UNCERTAIN)
        self.assertIsNotNone(h.transport.writer.reason(h.transport.handle))
        self.assertTrue(h.act_api.service_stopped)
        self.assertEqual(h.act_api.outstanding(), (p,))
        self.assertTrue(h.interlock.outstanding())
        self.test_verified_effect()

    # @anchor:wa.tests.scripts.test_execution.TestRuntime.test_current_authority_revisions
    def test_current_authority_revisions(self):
        for name in ('policy', 'capability_set', 'schema', 'environment_compatibility', 'calibration_certificate'):
            h = _RuntimeHost()
            p = h.act_api.prepare_effect(h.state, h.plan.nodes[0])
            revision = _dc.replace(h.authority, **({name: 1, 'calibration_reason': None}
                if name == 'calibration_certificate' else {name: getattr(h.authority, name) + 1}))
            h.interlock.invalidate(lambda: h.store.invalidate_authority(h.scope, revision))
            result = h.act_api.dispatch_effect(h.state, p)
            self.assertIsNot(result.checkpoint.value, _t.Truth.TRUE)
            self.assertEqual(h.input_count, 0)
            self.assertIs(result.checkpoint.reason, _t.Reason.STALE)
        for field in ('principal', 'tenant', 'browser_profile_incarnation', 'top_level_origin', 'target_frame_origin'):
            h = _RuntimeHost()
            h.state.scope = _dc.replace(h.scope, **{field: 'foreign'})
            with self.assertRaises(_t.ContractError):
                h.act_api.prepare_effect(h.state, h.plan.nodes[0])
            self.assertEqual(h.input_count, 0)
        self.test_verified_effect()

    # @anchor:wa.tests.scripts.test_execution.TestRuntime.test_private_owner_exceptions_stay_closed
    def test_private_owner_exceptions_stay_closed(self):
        h = _RuntimeHost()
        with _mock.patch.object(h.act_api.binding_owner, 'bind', side_effect=RuntimeError('private-canary-label')):
            with self.assertRaises(Exception) as caught:
                h.act_api.prepare_effect(h.state, h.plan.nodes[0])
        self.assertIs(type(caught.exception), _t.ContractError)
        self.assertNotIn('canary', repr(caught.exception))
        self.assertIsNone(caught.exception.__context__)
        self.assertEqual(h.input_count, 0)
        h = _RuntimeHost()
        h.spoof_binding = True
        with self.assertRaises(_t.ContractError) as caught:
            h.act_api.authorize_target(h.state, h.plan.nodes[0])
        self.assertIs(caught.exception.reason, _t.Reason.TYPE)
        self.test_verified_effect()

    # @anchor:wa.tests.scripts.test_execution.TestRuntime.test_failed_effect_cannot_be_relabelled_complete
    def test_failed_effect_cannot_be_relabelled_complete(self):
        h = _RuntimeHost()
        h.semantic_ok = False
        outcome = _r.advance(h.state)
        self.assertIsNot(outcome.status, _t.TerminalStatus.COMPLETED)
        h.goal_truth = _t.Truth.TRUE
        self.assertIs(_r.finish(h.state), outcome)
        self.assertFalse(h.ledger.snapshot()[0][-1].verification is _t.Truth.TRUE)
        with self.assertRaises(_t.ContractError):
            h.sh._acquire(h.cell)
        h = _RuntimeHost()
        h.state.checkpoint = _t.PredicateResult(_t.Truth.TRUE, None)
        h.goal_truth = _t.Truth.TRUE
        object.__setattr__(h.plan, 'terminal_goal', _dc.replace(h.plan.terminal_goal))
        self.assertIsNot(_r.finish(h.state).status, _t.TerminalStatus.COMPLETED)
        self.assertEqual(h.input_count, 0)

    # @anchor:wa.tests.scripts.test_execution.TestRuntime.test_canaries_in_private_metadata_and_labels
    def test_canaries_in_private_metadata_and_labels(self):
        h = _RuntimeHost(action=_t.ActionKind.TYPE, label='private-canary-nested-label')
        result = h.act_api.dispatch_effect(h.state, h.act_api.prepare_effect(h.state, h.plan.nodes[0]))
        self.assertIs(result.checkpoint.value, _t.Truth.TRUE)
        self.assertNotIn('canary', repr(result))
        self.assertNotIn('canary', repr(h.state))
        self.assertEqual(len(h.slots_seen), 1)
        self.assertEqual(h.input_count, 1)

    # @anchor:wa.tests.scripts.test_execution.TestRuntime.test_public_effect_commits_cumulative_disposition
    def test_public_effect_commits_cumulative_disposition(self):
        h = _RuntimeHost()
        p = h.act_api.prepare_effect(h.state, h.plan.nodes[0])
        result = h.act_api.dispatch_effect(h.state, p)
        self.assertIs(h.state.input_disposition, _t.InputDisposition.APPLIED)
        self.assertIs(h.state.checkpoint, result.checkpoint)
        self.assertEqual(h.state.verified_effects, 1)
        self.assertIs(h.state.observation, p.post)
        self.assertIs(h.act_api.dispatch_effect(h.state, p), result)
        self.assertEqual(h.state.verified_effects, 1)
        h.goal_truth = _t.Truth.TRUE
        outcome = _r.finish(h.state)
        self.assertIs(outcome.status, _t.TerminalStatus.COMPLETED)
        self.assertIs(outcome.input_disposition, _t.InputDisposition.APPLIED)

    # @anchor:wa.tests.scripts.test_execution.TestRuntime.test_deadline_classification_cannot_globalize
    def test_deadline_classification_cannot_globalize(self):
        h = _RuntimeHost()
        with _mock.patch.object(h.classifier, 'classify', return_value=h.sh._classification(
                h.cell, _t.FailureClass.ARTIFACT_DEFECT)):
            outcome = _r.abort(h.state, _t.Reason.DEADLINE)
        self.assertIs(outcome.failure, _t.FailureClass.UNCLASSIFIED)
        self.assertTrue(h.act_api.service_stopped)
        fresh = h.sh._acquire(h.cell)
        self.assertIs(h.store.release(fresh, fresh.holder_token).result, _t.Truth.TRUE)

    # @anchor:wa.tests.scripts.test_execution.TestRuntime.test_publish_successor_during_wait
    def test_publish_successor_during_wait(self):
        import copy
        h = _RuntimeHost()
        plan = h.sh._plan(n=9)
        wait = _t.WaitSpec(plan.entry, _t.WaitPolicy(100, 100, 2, 1000, 50, 100, 20))
        nodes = (_t.StepPlan('wait', _t.ActionKind.WAIT_UNTIL, None, None, None, None, wait=wait), plan.nodes[-1])
        plan = _dc.replace(plan, nodes=nodes, definition=_dc.replace(plan.definition, nodes=nodes))
        h.sh.plans[id(plan)] = (plan, copy.deepcopy(plan))
        h.plan, h.cell = plan, h.sh._admit(plan=plan)
        h.lease = h.sh._acquire(h.cell)
        h.holder_contract = h.lease.holder_contract
        h.sh.holder_grants[id(h.holder_contract)] = (h.holder_contract, h.lease.holder_token)
        h.registered_goal = plan.terminal_goal
        h.state = _r.RunnerState(plan, h.lease, 'episode', h.scope, h.authority,
            1000, h, h.observation, h.holder, h.holder_contract, h.restart_evidence,
            checkpoint=_t.PredicateResult(_t.Truth.TRUE, None))
        h.register(h.state)
        # Construct the reader with this exact run-holder association.
        frame = h.frames
        h.frames = h.frame_wait = _fw.FrameWait(observations=frame._observations,
            scheduler=frame._scheduler, control=frame._control, cancellation=h.cancellation,
            holder_contract=h.holder_contract, contexts=h, quiet=frame._quiet,
            boundary=frame._boundary, clock=h.clock, settle_coordinator=h.sc.coordinator)
        h.act_api.frame_wait = h.frames
        acquire = h.frames._observations.acquire
        publications = []
        def publish_during_wait(*args, **kwargs):
            old = h.store.publish_default(h.cell.key, scope=h.scope, expected_predecessor_revision=0,
                                         certificate=h.sh.cert)
            successor = h.sh._plan(n=10, logical=plan.identity.logical_id, revision=2)
            cell = h.sh._admit(plan=successor)
            cell = h.store.publish_default(cell.key, scope=h.scope,
                expected_predecessor_revision=old.lifecycle_revision, certificate=h.sh.cert)
            publications.append((successor, cell))
            return acquire(*args, **kwargs)
        with _mock.patch.object(h.frames._observations, 'acquire', side_effect=publish_during_wait):
            self.assertIs(_r.advance(h.state), h.state)
        self.assertEqual(len(publications), 1)
        self.assertIs(h.state.plan, plan)
        self.assertEqual(h.state.cursor, 1)
        successor, cell = publications[0]
        h.state.plan = successor
        with self.assertRaises(_t.ContractError):
            _r.interpret(h.state)
        h.state.plan = plan
        h.goal_truth = _t.Truth.TRUE
        self.assertIs(_r.advance(h.state).status, _t.TerminalStatus.COMPLETED)
        fresh = h.sh._acquire(cell)
        self.assertEqual(fresh.artifact, successor.identity)
        self.assertIs(h.store.release(fresh, fresh.holder_token).result, _t.Truth.TRUE)
        self.assertEqual(h.input_count, 0)
        self.assertEqual(h.tokens, [])

    # @anchor:wa.tests.scripts.test_execution.TestRuntime.test_restart_uncertain_new_artifact
    def test_restart_uncertain_new_artifact(self):
        old = _RuntimeHost()
        old.raise_send = True
        pending = old.act_api.prepare_effect(old.state, old.plan.nodes[0])
        result = old.act_api.dispatch_effect(old.state, pending)
        self.assertIs(result.input_disposition, _t.InputDisposition.INPUT_UNCERTAIN)
        self.assertIsNotNone(old.transport.writer.reason(old.transport.handle))
        fresh = _RuntimeHost(artifact_n=2)
        fresh.transport = old.transport
        fresh.restart_ok = False
        fresh.policy = _p.ActionPolicy(fence=old.transport.fence, clock=old.transport.clock,
                                      script_interlock=fresh.interlock)
        fresh.act_api.action_policy = fresh.policy
        original_wire = fresh._wire
        def new_action(intent, binding, **kwargs):
            wire = original_wire(intent, binding, **kwargs)
            return _dc.replace(wire, body=fresh.transport.body(action_id='act-B'),
                               adapter_input=fresh.transport.adapter(action_id='act-B'))
        fresh.act_api.wire.prepare = new_action
        with self.assertRaises(_t.ContractError):
            fresh.act_api.prepare_effect(fresh.state, fresh.plan.nodes[0])
        self.assertEqual(fresh.input_count, 0)
        self.assertNotEqual(old.plan.identity, fresh.plan.identity)
        # Independently authenticated custody acknowledgement; never resends old input.
        old.transport.fence.reconcile(pending.admission.prepared, acknowledged=True)
        self.assertIsNone(old.transport.writer.reason(old.transport.handle))
        fresh.restart_ok = True
        result = fresh.act_api.dispatch_effect(fresh.state,
            fresh.act_api.prepare_effect(fresh.state, fresh.plan.nodes[0]))
        self.assertIs(result.checkpoint.value, _t.Truth.TRUE)
        self.assertEqual(old.input_count, 1)
        self.assertEqual(fresh.input_count, 1)
        self.assertTrue(old.state.stopped)

    # @anchor:wa.tests.scripts.test_execution.TestRuntime.test_oversized_grant_retains_owner_evidence
    def test_oversized_grant_retains_owner_evidence(self):
        h = _RuntimeHost()
        original = h.sh.reserve
        def oversized(request, policy):
            outcome = original(request, policy)
            grant = _dc.replace(outcome.grant, charged_bytes=h.act_api.policy.retained_bytes + 1)
            h.sh.grants.pop(id(outcome.grant))
            h.sh.grants[id(grant)] = grant
            h.sh.used += grant.charged_bytes - outcome.grant.charged_bytes
            return _t.ReservationOutcome(grant, None)
        with _mock.patch.object(h.act_api.reservations, 'reserve', side_effect=oversized):
            with self.assertRaises(_t.ContractError) as caught:
                h.act_api.prepare_effect(h.state, h.plan.nodes[0])
        self.assertIs(caught.exception.reason, _t.Reason.CAPACITY)
        pending = h.state.pending_effect
        self.assertIs(h.sh.grants[id(pending.grant)], pending.grant)
        outcome = _r.abort(h.state, caught.exception.reason)
        self.assertTrue(h.act_api.service_stopped)
        self.assertIsNot(outcome.owner_drained.value, _t.Truth.TRUE)
        self.assertEqual(h.act_api.outstanding(), (pending,))
        self.assertIs(h.sh.grants[id(pending.grant)], pending.grant)
        self.assertEqual(h.input_count, 0)
        self.test_verified_effect()
