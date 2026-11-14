# @module {"id":"wa.tests.scripts.test_compiler","layer":2,"owner":"LEARN","deps":["wa.scripts.compiler","wa.scripts.header","wa.scripts.types","wa.tests.scripts.support","wa.tests.scripts.test_header"],"book":"HB-WA-s4-learn"}
"""Compiler public-ingress controls with synthetic retained host owners.

Parser trees are constructed only in trusted test setup. No live backend or
production authority certification is claimed by this suite.
"""
import ast as _ast
import copy as _copy
import dataclasses as _dc
import hashlib as _hash
import json as _json
import subprocess as _subprocess
import sys as _sys
import unittest as _unittest
from pathlib import Path as _Path
from unittest import mock as _mock
from wender.scripts import compiler as _c, header as _h, types as _t
from tests.scripts.test_header import _Host as _HeaderHost, _declarations, _source, _wire_record
from tests.scripts.support import verify_routing_manifest as _routing

_TRUE = _t.AdmissionCheck(_t.Truth.TRUE, None)
_BODY = b'''def run(act, params):
    button = act.find("click")
    act.click(button, expect="clicked")
    textbox = act.find("type")
    act.type(textbox, slot="text", expect="typed")
    keyboard = act.find("key")
    act.key(keyboard, keysym="Page-Down", expect="keyed")
    act.wait_until("clicked", timeout_ms=100)
    act.assert_tag("clicked", code="schema")
    act.harvest(button, slot_out="answer", max_bytes=128)
    return None
'''


def _freeze(node):
    children = []
    def field(value):
        if isinstance(value, _ast.AST):
            index = len(children)
            children.append(_freeze(value))
            return ('node', index)
        if type(value) is list:
            return ('list', tuple(field(v) for v in value))
        return ({type(None): 'none', str: 'str', int: 'int', bool: 'bool'}[type(value)], value)
    fields = tuple((name, field(value)) for name, value in _ast.iter_fields(node))
    return type(node).__name__, tuple(children), fields


def _forge(value, **fields):
    clone = _copy.copy(value)
    for name, field in fields.items():
        object.__setattr__(clone, name, field)
    return clone


class _Allocation:
    def __init__(self):
        self.live, self.next = {}, 0
        self.deny = self.refuse_release = False

    # @anchor:wa.tests.scripts.test_compiler._Allocation.reserve
    def reserve(self, size, deadline):
        if self.deny:
            raise _t.ContractError(_t.Reason.CAPACITY)
        self.next += 1
        token = _t.RegistryRef('allocation', 'test', self.next,
                               _t.PrivateRef('allocation', 'test', str(self.next)))
        self.live[id(token)] = (token, size, deadline)
        return token

    # @anchor:wa.tests.scripts.test_compiler._Allocation.check
    def check(self, token, size, deadline):
        row = self.live.get(id(token))
        return _TRUE if row is not None and row[0] is token and row[1:] == (size, deadline) else _t.AdmissionCheck(_t.Truth.FALSE, _t.Reason.FOREIGN)

    # @anchor:wa.tests.scripts.test_compiler._Allocation.release
    def release(self, token):
        if self.refuse_release or id(token) not in self.live:
            return _t.AdmissionCheck(_t.Truth.FALSE, _t.Reason.RECONCILIATION)
        del self.live[id(token)]
        return _TRUE


class _Host(_HeaderHost):
    def __init__(self, body=_BODY):
        super().__init__()
        self.peak = 20000000
        self.cancel = _dc.replace(self.cancel, consumer_byte_ceiling=50000000)
        self.policy = _dc.replace(self.policy, host_bytes=100000000, process_bytes=50000000, cancellation=self.cancel)
        self.tree = _freeze(_ast.parse(body))
        self.allocation, self.retention = _Allocation(), _Allocation()
        self.denial = None
        self.goal = _t.TerminalGoalRef('goal', self.scope, self.owner)
        self.screen = _t.PlanDigest('sha256', 'd' * 64)
        self.episode = 'episode'
        self.queries, self.predicates, self.effects = [], [], []
        for verb, postname in (('click', 'clicked'), ('type', 'typed'), ('key', 'keyed')):
            selector = _t.TargetSelector('button', verb, self.owner, 'main', 'container',
                'turn', None, None, _t.MissingRef(None, _t.Reason.MISSING), _t.ActionKind(verb),
                (_t.MatchLevel.L0_EXACT,), ('role',))
            query = _t.QueryDeclaration(verb, selector, self.scope,
                (_t.PredicateOperator.EXISTS_UNIQUE, _t.PredicateOperator.DECLARED_TRANSITION,
                 _t.PredicateOperator.TASK_GOAL, _t.PredicateOperator.FOCUSED))
            operand = _t.Operand('operand', verb, _t.ValueKind.TEXT, 'transition')
            pre = _t.PredicateSpec(_t.PredicateOperator.EXISTS_UNIQUE, (operand,), (), self.scope,
                (_t.EvidenceSource.HOST,), _t.MissingRef(None, _t.Reason.MISSING))
            post = _dc.replace(pre, operator=_t.PredicateOperator.DECLARED_TRANSITION,
                               transition_reference=_t.MissingRef('transition', None))
            self.queries.append((verb, query))
            self.predicates.append((postname, post))
            self.effects.append((_t.ActionKind(verb), verb, pre, _t.VerificationBudget(100, 2, 500000000)))
        self.entry = self.effects[0][2]
        self.vocabulary = _t.HostKeyVocabulary('test', ('Page-Down',), self.owner)
        text = _t.SlotDescriptor('text', _t.ValueKind.TEXT, _t.Purpose.INPUT, self.owner,
            self.episode, self.scope, 'destination', 'grant', None)
        key = _dc.replace(text, slot_name='keyslot', kind=_t.ValueKind.KEY, key_vocabulary=self.vocabulary)
        self.descriptors, self.keys = (('text', text),), (('Page-Down', key),)
        self.specs = tuple(_t.ArgSpec(name, kind, True, 1, 128, _t.Sensitivity.PRIVATE,
            _t.Purpose.INPUT, 'grant', 'destination', 'wender-args/verbatim-v1')
            for name, kind in (('text', _t.ValueKind.TEXT), ('keyslot', _t.ValueKind.KEY)))
        self.result = _t.ResultDeclaration('answer', _t.ValueKind.TEXT, _t.Purpose.HARVEST,
                                          'result-destination', 128)
        self.auth = _t.RegistryRef('harvest', 'incarnation', 1, self.owner)
        self.harvests = (('answer', self.result, self.auth, self.queries[0][1], self.scope),)
        self.profile = _t.WaitPolicy(100, 1000, 2, 500000000, 50, 100, 20)
        self.config = _c.CompilerInputs(tuple(self.queries), tuple(self.predicates), tuple(self.effects),
            self.descriptors, self.keys, (('clicked', self.profile),), self.harvests,
            self.goal, self.screen, self.episode, self, self.allocation, 2000000)
        self.registry = _c.CompilerRegistry(compiler=self.owner, incarnation='incarnation',
            generation=1, max_entries=4, max_bytes=4000000, entry_bytes=1000000, allocation=self.retention)
        declarations = _declarations()
        for tag, value in declarations:
            if tag == 'LANE_CONFIG':
                value['limits'].update(max_nodes=64, canonical_bytes=262144,
                    absolute_deadline_ns=900000000, max_acquisitions=64, episode_input_bytes=16384,
                    episode_result_bytes=65536)
            elif tag == 'SURGERY_DECLARATION':
                value['entry'] = _wire_record(self.entry)
            elif tag == 'META:args':
                value[:] = _wire_record(self.specs)
        self.source_bytes = _source(declarations, body)
        self.admitter = self.admission(absolute_deadline_ns=1000000000)

    # @anchor:wa.tests.scripts.test_compiler._Host.decode
    def decode(self, wire, policy):
        return self.tree

    def _answer(self, name, valid=True):
        if self.denial == name or not valid:
            return _t.AdmissionCheck(_t.Truth.FALSE, _t.Reason.FOREIGN)
        return _TRUE

    # @anchor:wa.tests.scripts.test_compiler._Host.check_scope
    def check_scope(self, scope, episode):
        return self._answer('scope', scope == self.scope and episode == self.episode)

    # @anchor:wa.tests.scripts.test_compiler._Host.check_query
    def check_query(self, query):
        return self._answer('query', any(query is q for _, q in self.config._queries))

    # @anchor:wa.tests.scripts.test_compiler._Host.check_predicate
    def check_predicate(self, predicate, action, query):
        retained = any(predicate is p for _, p in self.config._predicates) or any(
            predicate is r[2] for r in self.config._effects) or predicate == self.entry
        return self._answer('predicate', retained)

    # @anchor:wa.tests.scripts.test_compiler._Host.check_slot
    def check_slot(self, descriptor, spec, episode):
        return self._answer('slot', descriptor.capability is self.owner and episode == self.episode)

    # @anchor:wa.tests.scripts.test_compiler._Host.check_key
    def check_key(self, symbol, descriptor):
        return self._answer('key', symbol == 'Page-Down' and descriptor is self.keys[0][1]
                            and descriptor.key_vocabulary is self.vocabulary
                            and self.vocabulary.attestation is self.owner)

    # @anchor:wa.tests.scripts.test_compiler._Host.check_harvest
    def check_harvest(self, harvest):
        return self._answer('harvest', harvest.authorization is self.auth)

    # @anchor:wa.tests.scripts.test_compiler._Host.check_terminal
    def check_terminal(self, terminal):
        return self._answer('terminal', terminal is self.goal)

    # @anchor:wa.tests.scripts.test_compiler._Host.check_export
    def check_export(self, selector):
        return self._answer('export', selector.label_export_receipt is self.owner)

    # @anchor:wa.tests.scripts.test_compiler._Host.check_screen
    def check_screen(self, digest):
        return self._answer('screen', digest == self.screen)

    def _compile(self):
        return _c.compile_draft(self.source_bytes, admission=self.admitter,
                               inputs=self.config, registry=self.registry)

    def _direct(self):
        plan = self._compile()
        self.registry.release(plan)
        self.session = _h.admit_source(self.source_bytes, admission=self.admitter)
        return plan.definition


def _rewrite_header(host, change):
    prefix, body = host.source_bytes.split(b'def run', 1)
    declarations = [(line.split(b'] ', 1)[0][3:].decode(),
                     _json.loads(line.split(b'] ', 1)[1])) for line in prefix.splitlines()]
    change(dict(declarations))
    host.source_bytes = _source(declarations, b'def run' + body)



# @anchor:wa.tests.scripts.test_compiler.CompilerControls
class CompilerControls(_unittest.TestCase):
    def _reject(self, host, reason, operation=None):
        error = None
        try:
            (operation or host._compile)()
        except Exception as exc:
            error = exc
        self.assertIs(type(error), _t.ContractError)
        self.assertIs(error.reason, reason)
        self.assertIsNone(error.__cause__)
        self.assertIsNone(error.__context__)
        self.assertEqual(len(host.registry.outstanding()[0]), 0)
        self.assertEqual(host.config._owners.inputs, 0)

    def _direct_reject(self, change, reason):
        host = _Host()
        definition = host._direct()
        try:
            _c.canonical_plan(definition, session=host.session, admission=host.admitter, inputs=host.config)
            valid = _c.validate_plan(definition, session=host.session, admission=host.admitter,
                                     inputs=host.config, registry=host.registry)
            host.registry.release(valid)
            bad = change(definition)
            self._reject(host, reason, lambda: _c.canonical_plan(bad, session=host.session,
                admission=host.admitter, inputs=host.config))
            self._reject(host, reason, lambda: _c.validate_plan(bad, session=host.session,
                admission=host.admitter, inputs=host.config, registry=host.registry))
        finally:
            host.admitter.finish(host.session)

    # @anchor:wa.tests.scripts.test_compiler.CompilerControls.test_g_plan_end_to_end
    def test_g_plan_end_to_end(self):
        host = _Host()
        plan = host._compile()
        self.assertIs(type(plan), _t.ValidatedPlan)
        self.assertEqual(tuple(n.kind for n in plan.nodes), (_t.ActionKind.FIND, _t.ActionKind.CLICK,
            _t.ActionKind.FIND, _t.ActionKind.TYPE, _t.ActionKind.FIND, _t.ActionKind.KEY,
            _t.ActionKind.WAIT_UNTIL, _t.ActionKind.ASSERT_TAG, _t.ActionKind.HARVEST, _t.NodeKind.TERMINAL))
        self.assertEqual(tuple(q.name for q in plan.queries), ('click', 'type', 'key'))
        self.assertEqual(tuple(s.name for s in plan.symbols), ('answer',))
        self.assertIs(plan.nodes[3].effect.arguments[0], host.descriptors[0][1])
        self.assertEqual(host.inputs, 0)
        self.assertEqual(host.grants, [])
        self.assertEqual(len(host.retention.live), 1)
        host.registry.release(plan)
        self.assertEqual(host.retention.live, {})
        self._reject(_Host(_BODY.replace(b'    return None', b'    import os')), _t.Reason.SCHEMA)

    # @anchor:wa.tests.scripts.test_compiler.CompilerControls.test_g_plan_ingress_equivalence
    def test_g_plan_ingress_equivalence(self):
        host = _Host()
        definition = host._direct()
        try:
            canonical = _c.canonical_plan(definition, session=host.session,
                admission=host.admitter, inputs=host.config)
            plan = _c.validate_plan(definition, session=host.session, admission=host.admitter,
                                     inputs=host.config, registry=host.registry)
            self.assertEqual(canonical, plan.canonical_bytes)
            self.assertIs(_c.verify_pairing(plan, session=host.session, admission=host.admitter,
                inputs=host.config, registry=host.registry), plan)
        finally:
            host.admitter.finish(host.session)
        self._direct_reject(lambda d: _forge(d, nodes=list(d.nodes)), _t.Reason.TYPE)
        self._direct_reject(lambda d: _forge(d, nodes=(_forge(d.nodes[0], kind='unknown'),) + d.nodes[1:]), _t.Reason.TYPE)

    # @anchor:wa.tests.scripts.test_compiler.CompilerControls.test_g_args_literals
    def test_g_args_literals(self):
        _Host()._compile()
        for before, after, reason in (
            (b'timeout_ms=100', b'timeout_ms=True', _t.Reason.TYPE),
            (b'code="schema"', b'code="unknown-code"', _t.Reason.SCHEMA),
            (b'slot="text"', b'text="payload"', _t.Reason.SCHEMA),
            (b'expect="clicked"', b'expect="clicked", extra=1', _t.Reason.SCHEMA),
            (b'expect="clicked"', b'', _t.Reason.SCHEMA)):
            with self.subTest(after=after):
                self._reject(_Host(_BODY.replace(before, after)), reason)

    # @anchor:wa.tests.scripts.test_compiler.CompilerControls.test_g_plan_symbols
    def test_g_plan_symbols(self):
        _Host()._compile()
        for before, after, reason in (
            (b'act.click(button', b'act.click(missing', _t.Reason.SCHEMA),
            (b'textbox =', b'button =', _t.Reason.SCHEMA),
            (b'textbox =', b'Button =', _t.Reason.SCHEMA),
            (b'find("click")', b'find("unknown")', _t.Reason.FOREIGN),
            (b'slot_out="answer"', b'slot_out="unknown"', _t.Reason.FOREIGN)):
            self._reject(_Host(_BODY.replace(before, after)), reason)
        duplicate = _BODY.replace(b'    return None', b'    act.harvest(button, slot_out="answer", max_bytes=128)\n    return None')
        self._reject(_Host(duplicate), _t.Reason.CONFLICT)

    # @anchor:wa.tests.scripts.test_compiler.CompilerControls.test_owner_checks
    def test_owner_checks(self):
        _Host()._compile()
        for owner in ('scope', 'query', 'predicate', 'slot', 'key', 'harvest', 'terminal', 'export', 'screen'):
            host = _Host()
            host.denial = owner
            with self.subTest(owner=owner):
                self._reject(host, _t.Reason.FOREIGN)
        for result in (True, _t.AdmissionCheck(_t.Truth.UNKNOWN, _t.Reason.MISSING),
                       _t.AdmissionCheck(_t.Truth.FALSE, _t.Reason.FOREIGN)):
            host = _Host()
            with _mock.patch.object(host, 'check_scope', return_value=result):
                self._reject(host, _t.Reason.TYPE if result is True else result.reason)

    # @anchor:wa.tests.scripts.test_compiler.CompilerControls.test_g_args_descriptors
    def test_g_args_descriptors(self):
        _Host()._compile()
        for fields, reason in (({'episode_id': 'foreign'}, _t.Reason.FOREIGN),
                               ({'scope': _dc.replace(_Host().scope, principal='foreign')}, _t.Reason.FOREIGN),
                               ({'capability': None}, _t.Reason.TYPE),
                               ({'capability': _copy.copy(_Host().owner)}, _t.Reason.FOREIGN),
                               ({'destination_contract': 'foreign'}, _t.Reason.FOREIGN),
                               ({'capability_contract': 'foreign'}, _t.Reason.FOREIGN),
                               ({'purpose': _t.Purpose.HARVEST}, _t.Reason.FOREIGN),
                               ({'kind': _t.ValueKind.INTEGER}, _t.Reason.TYPE)):
            host = _Host()
            descriptor = _forge(host.descriptors[0][1], **fields)
            host.config = _dc.replace(host.config, _descriptors=(('text', descriptor),))
            self._reject(host, reason)

    # @anchor:wa.tests.scripts.test_compiler.CompilerControls.test_g_args_keys
    def test_g_args_keys(self):
        _Host()._compile()
        self._reject(_Host(_BODY.replace(b'Page-Down', b'Unknown')), _t.Reason.FOREIGN)
        for field in ('key_vocabulary', 'capability'):
            host = _Host()
            descriptor = _forge(host.keys[0][1], **{field: None})
            host.config = _dc.replace(host.config, _keys=(('Page-Down', descriptor),))
            self._reject(host, _t.Reason.TYPE if field == 'capability' else _t.Reason.FOREIGN)
        host = _Host()
        host.config = _dc.replace(host.config, _keys=(('Page-Down', host.descriptors[0][1]),))
        self._reject(host, _t.Reason.FOREIGN)
        host = _Host()
        object.__setattr__(host.vocabulary, 'attestation', _copy.copy(host.owner))
        self._reject(host, _t.Reason.FOREIGN)

    # @anchor:wa.tests.scripts.test_compiler.CompilerControls.test_g_plan_harvest
    def test_g_plan_harvest(self):
        _Host()._compile()
        self._reject(_Host(_BODY.replace(b'max_bytes=128', b'max_bytes=129')), _t.Reason.BOUNDS)
        host = _Host()
        row = host.harvests[0]
        host.config = _dc.replace(host.config, _harvests=((row[0], row[1], _copy.copy(row[2]), row[3], row[4]),))
        self._reject(host, _t.Reason.FOREIGN)
        host = _Host()
        row = host.harvests[0]
        host.config = _dc.replace(host.config, _harvests=((row[0], row[1], row[2], host.queries[1][1], row[4]),))
        self._reject(host, _t.Reason.FOREIGN)
        host = _Host()
        row = host.harvests[0]
        host.config = _dc.replace(host.config, _harvests=((row[0], _dc.replace(row[1], name='foreign')) + row[2:],))
        self._reject(host, _t.Reason.FOREIGN)

    # @anchor:wa.tests.scripts.test_compiler.CompilerControls.test_g_plan_terminal
    def test_g_plan_terminal(self):
        self._direct_reject(lambda d: _forge(d, nodes=d.nodes[:-1]), _t.Reason.MISSING)
        self._direct_reject(lambda d: _forge(d, nodes=d.nodes + (_forge(d.nodes[-1], step_id='extra'),)), _t.Reason.SCHEMA)
        self._direct_reject(lambda d: _forge(d, nodes=(d.nodes[-1],) + d.nodes[:-1]), _t.Reason.MISSING)
        host = _Host()
        host.config = _dc.replace(host.config, _terminal=_copy.copy(host.goal))
        self._reject(host, _t.Reason.FOREIGN)

    # @anchor:wa.tests.scripts.test_compiler.CompilerControls.test_g_plan_pairing
    def test_g_plan_pairing(self):
        _Host()._compile()
        self._reject(_Host(_BODY.replace(b'expect="clicked"', b'expect=True')), _t.Reason.FOREIGN)
        host = _Host()
        post = host.predicates[1][1]
        host.config = _dc.replace(host.config, _predicates=(('clicked', post),) + tuple(host.predicates[1:]))
        self._reject(host, _t.Reason.MISSING)
        self._direct_reject(lambda d: _forge(d, nodes=(d.nodes[0], _forge(d.nodes[1], effect=_forge(
            d.nodes[1].effect, postcondition=None))) + d.nodes[2:]), _t.Reason.TYPE)
        host = _Host()
        ready = _dc.replace(host.entry, operator=_t.PredicateOperator.FOCUSED)
        host.config = _dc.replace(host.config, _predicates=(('clicked', ready),) + tuple(host.predicates[1:]))
        self._reject(host, _t.Reason.MISSING)
        host = _Host()
        mixed = _dc.replace(host.entry, operator=_t.PredicateOperator.ANY, operands=(),
                            children=(host.predicates[0][1], host.entry))
        host.config = _dc.replace(host.config, _predicates=(('clicked', mixed),) + tuple(host.predicates[1:]))
        self._reject(host, _t.Reason.MISSING)

    # @anchor:wa.tests.scripts.test_compiler.CompilerControls.test_g_resource_boundaries
    def test_g_resource_boundaries(self):
        _Host()._compile()
        host = _Host()
        host.config = _dc.replace(host.config, _allocation=None)
        self._reject(host, _t.Reason.MISSING)
        host = _Host()
        host.allocation.deny = True
        self._reject(host, _t.Reason.CAPACITY)
        host = _Host()
        host.now = 1000000000
        self._reject(host, _t.Reason.DEADLINE)
        self._reject(_Host(_BODY.replace(b'timeout_ms=100', b'timeout_ms=1001')), _t.Reason.BOUNDS)
        host = _Host()
        host.registry._max_entries = 0
        self._reject(host, _t.Reason.CAPACITY)

    # @anchor:wa.tests.scripts.test_compiler.CompilerControls.test_g_resource_cleanup
    def test_g_resource_cleanup(self):
        host = _Host()
        plan = host._compile()
        self.assertEqual(len(host.retention.live), 1)
        self.assertFalse(host.allocation.live)
        self.assertFalse(host.admitter.outstanding())
        host.registry.release(plan)
        for failure in ('parse', 'decode', 'lower', 'hash', 'publish'):
            host = _Host()
            target, name = {'parse': (host, 'parse'), 'decode': (host.admitter, '_decoder'),
                'lower': (_c, '_lower'), 'hash': (_h, 'canonical_identity'),
                'publish': (host.registry, '_publish')}[failure]
            with _mock.patch.object(target, name, side_effect=RuntimeError('PRIVATE-CANARY')):
                self._reject(host, _t.Reason.UNKNOWN)
            self.assertFalse(host.admitter.outstanding())
            self.assertFalse(host.allocation.live)
        host = _Host()
        host.drain = False
        self._reject(host, _t.Reason.FOREIGN)
        self.assertTrue(host.admitter.outstanding())
        self.assertTrue(host.grants)
        host = _Host()
        host.allocation.refuse_release = True
        self._reject(host, _t.Reason.RECONCILIATION)
        self.assertTrue(host.allocation.live)
        self.assertTrue(host.registry.outstanding()[1])

    # @anchor:wa.tests.scripts.test_compiler.CompilerControls.test_g_pin_recompute
    def test_g_pin_recompute(self):
        host = _Host()
        d = host._direct()
        try:
            canonical = _c.canonical_plan(d, session=host.session, admission=host.admitter, inputs=host.config)
            self.assertEqual(_hash.sha256(b'wender-plan/v1\0' + canonical).hexdigest(), d.identity.plan.full_digest)
            self.assertEqual(_json.dumps(_json.loads(canonical), ensure_ascii=False,
                sort_keys=True, separators=(',', ':')).encode(), canonical)
            self.assertEqual(set(_json.loads(canonical)), {'nodes', 'selectors', 'symbols', 'slots',
                'entry', 'terminal_goal', 'limits', 'queries'})
        finally:
            host.admitter.finish(host.session)
        self._direct_reject(lambda d: _forge(d, source_digest=_t.PlanDigest('sha256', 'e' * 64)), _t.Reason.SCHEMA)
        self._direct_reject(lambda d: _forge(d, identity=_dc.replace(d.identity,
            artifact=_t.ArtifactDigest('sha256', 'e' * 64))), _t.Reason.STALE)

    # @anchor:wa.tests.scripts.test_compiler.CompilerControls.test_g_pin_registry
    def test_g_pin_registry(self):
        host = _Host()
        d = host._direct()
        try:
            plan = _c.validate_plan(d, session=host.session, admission=host.admitter,
                                     inputs=host.config, registry=host.registry)
            self.assertIs(_c.verify_pairing(plan, session=host.session, admission=host.admitter,
                inputs=host.config, registry=host.registry), plan)
            for bad in (_copy.copy(plan), _forge(plan, compiler_handle=_copy.copy(plan.compiler_handle)),
                        _forge(plan, compiler_registry=_dc.replace(plan.compiler_registry, generation=2)),
                        _forge(plan, compiler_registry=_dc.replace(plan.compiler_registry, owner_incarnation='foreign'))):
                with self.assertRaises(_t.ContractError) as raised:
                    _c.verify_pairing(bad, session=host.session, admission=host.admitter,
                        inputs=host.config, registry=host.registry)
                self.assertIs(raised.exception.reason, _t.Reason.FOREIGN)
            host.registry.release(plan)
            self._reject(host, _t.Reason.FOREIGN, lambda: _c.verify_pairing(plan,
                session=host.session, admission=host.admitter, inputs=host.config, registry=host.registry))
        finally:
            host.admitter.finish(host.session)

    # @anchor:wa.tests.scripts.test_compiler.CompilerControls.test_g_ast_bridge
    def test_g_ast_bridge(self):
        _Host()._compile()
        for mutate in (lambda t: t[:2], lambda t: (t[0], t[1], (('unknown', ('none', None)),)),
                       lambda t: (t[0], t[1], (('body', ('list', (('node', 999),))), ('type_ignores', ('list', ()))))):
            host = _Host()
            host.tree = mutate(host.tree)
            self._reject(host, _t.Reason.SCHEMA)
        self._reject(_Host(_BODY.replace(b'button', 'bｕtton'.encode())), _t.Reason.SCHEMA)

    # @anchor:wa.tests.scripts.test_compiler.CompilerControls.test_g_privacy
    def test_g_privacy(self):
        host = _Host(_BODY.replace(b'    return None', b'    # PRIVATE-CANARY\n    return None'))
        plan = host._compile()
        self.assertNotIn(b'PRIVATE-CANARY', plan.canonical_bytes)
        self.assertNotIn('PRIVATE-CANARY', repr(host.config))
        self.assertEqual(host.config._owners.inputs, 0)
        host = _Host(_BODY.replace(b'expect="clicked"', b'expect="PRIVATE-CANARY"'))
        self._reject(host, _t.Reason.FOREIGN)
        with _mock.patch.object(host, 'check_scope', side_effect=RuntimeError('PRIVATE-CANARY')):
            self._reject(host, _t.Reason.UNKNOWN)

    # @anchor:wa.tests.scripts.test_compiler.CompilerControls.test_no_execution
    def test_no_execution(self):
        host = _Host()
        real_eval = eval
        def trusted_eval(code, globals_, locals_):
            self.assertEqual(_sys._getframe(1).f_globals.get('__name__'), 'typing')
            return real_eval(code, globals_, locals_)
        with _mock.patch('builtins.eval', new=trusted_eval), \
             _mock.patch('builtins.exec', side_effect=AssertionError('execution')), \
             _mock.patch('builtins.__import__', side_effect=AssertionError('candidate import')), \
             _mock.patch.object(_ast, 'parse', side_effect=AssertionError('reparse')), \
             _mock.patch.object(_subprocess, 'Popen', side_effect=AssertionError('shell')):
            plan = host._compile()
        self.assertIs(type(plan), _t.ValidatedPlan)
        self._reject(_Host(_BODY.replace(b'    return None', b'    __import__("os")')), _t.Reason.SCHEMA)

    # @anchor:wa.tests.scripts.test_compiler.CompilerControls.test_g_anchor_scope
    def test_g_anchor_scope(self):
        root = _Path(__file__).resolve().parents[2]
        manifest = {'wender/scripts/compiler.py': 'wa.scripts.compiler',
                    'tests/scripts/test_compiler.py': 'wa.tests.scripts.test_compiler'}
        # The positive fixture is this assignment's manifest; foreign paths
        # below remain rejecting controls independently of the integration diff.
        changed = sorted(manifest)
        self.assertTrue(_routing(root, manifest, changed))
        with self.assertRaises(_t.ContractError) as raised:
            _routing(root, manifest, changed + ['wender/scripts/types.py'])
        self.assertIs(raised.exception.reason, _t.Reason.FOREIGN)
        with self.assertRaises(_t.ContractError):
            _routing(root, manifest, changed + ['wender/scripts/../scripts/compiler.py'])

    # @anchor:wa.tests.scripts.test_compiler.CompilerControls.test_g_source_vectors
    def test_g_source_vectors(self):
        minimal = b'def run(act, params):\r\n    return None\r\n'
        host = _Host(minimal)
        host.source_bytes = host.source_bytes.replace(b'\n', b'\r\n').replace(b'\r\r\n', b'\r\n')
        plan = host._compile()
        self.assertEqual(plan.identity.source_digest.full_digest,
                         'c4b7510822545cbace60682cdfd0705435ccf401d481b376fe2403c0d72dfa68')
        for source, reason in ((b'\xef\xbb\xbf', _t.Reason.SCHEMA),
                               (b'\xff', _t.Reason.TYPE), (b'\x00', _t.Reason.SCHEMA)):
            host = _Host()
            host.source_bytes = source + host.source_bytes
            self._reject(host, reason)
        for change, reason in (
            (lambda h: h['LANE_CONFIG']['abis'].update(dsl='wender-dsl/v2'), _t.Reason.SCHEMA),
            (lambda h: h['SCRIPT_TAG'].update(unknown=1), _t.Reason.SCHEMA),
            (lambda h: h['SCRIPT_TAG'].update(revision=True), _t.Reason.TYPE),
            (lambda h: h['META:args'][0].update(normalization_abi='unknown'), _t.Reason.SCHEMA)):
            host = _Host()
            _rewrite_header(host, change)
            self._reject(host, reason)
        host = _Host()
        host.source_bytes = host.source_bytes.replace(b'"revision":1', b'"revision":1,"revision":1')
        self._reject(host, _t.Reason.SCHEMA)
        host = _Host()
        host.backend = None
        self._reject(host, _t.Reason.MISSING)
        self.assertEqual(host.dispatches, 0)
        host = _Host()
        host.admitter._parser = None
        self._reject(host, _t.Reason.MISSING)

    # @anchor:wa.tests.scripts.test_compiler.CompilerControls.test_literal_bindings_and_unicode
    def test_literal_bindings_and_unicode(self):
        host = _Host(_BODY.replace(b'find("click")', b'find({"role": "button"})'))
        host.config = _dc.replace(host.config, _queries=((('dict', (('role', 'button'),)),
            host.queries[0][1]),) + tuple(host.queries[1:]))
        plan = host._compile()
        self.assertIs(plan.nodes[0].find.query, host.queries[0][1])
        bad = _Host(_BODY.replace(b'find("click")', b'find({"role": "button", "unknown": 1})'))
        bad.config = _dc.replace(bad.config, _queries=((('dict', (('role', 'button'),)),
            bad.queries[0][1]),) + tuple(bad.queries[1:]))
        self._reject(bad, _t.Reason.FOREIGN)
        plans = []
        for literal in (' e\u0301 ', ' é '):
            host = _Host(_BODY.replace(b'find("click")', ('find(' + _json.dumps(literal) + ')').encode()))
            host.config = _dc.replace(host.config, _queries=((literal, host.queries[0][1]),) + tuple(host.queries[1:]))
            plans.append(host._compile())
        self.assertEqual(plans[0].canonical_bytes, plans[1].canonical_bytes)
        self.assertNotEqual(plans[0].identity.source_digest, plans[1].identity.source_digest)
        self._reject(_Host(_BODY.replace(b'find("click")', b'find({"role": 1, "role": 2})')), _t.Reason.SCHEMA)

    # @anchor:wa.tests.scripts.test_compiler.CompilerControls.test_independent_mutation_and_cycles
    def test_independent_mutation_and_cycles(self):
        host = _Host()
        d = host._direct()
        try:
            plan = _c.validate_plan(d, session=host.session, admission=host.admitter,
                                    inputs=host.config, registry=host.registry)
            original = plan.compiler_handle
            object.__setattr__(plan, 'compiler_handle', _copy.copy(original))
            with self.assertRaises(_t.ContractError) as raised:
                host.registry.check(plan, host.owner, plan.identity, plan.scope, plan.abis)
            self.assertIs(raised.exception.reason, _t.Reason.STALE)
            object.__setattr__(plan, 'compiler_handle', original)
            old_role = d.selectors[0].role
            object.__setattr__(d.selectors[0], 'role', 'mutated')
            with self.assertRaises(_t.ContractError) as raised:
                host.registry.check(plan, host.owner, plan.identity, plan.scope, plan.abis)
            self.assertIs(raised.exception.reason, _t.Reason.STALE)
            object.__setattr__(d.selectors[0], 'role', old_role)
            host.registry.release(plan)
        finally:
            host.admitter.finish(host.session)
        def cycle(d):
            p = _forge(d.entry, operator=_t.PredicateOperator.NOT, operands=())
            object.__setattr__(p, 'children', (p,))
            return _forge(d, entry=p)
        self._direct_reject(cycle, _t.Reason.SCHEMA)
        self._direct_reject(lambda d: _forge(d, queries=tuple(reversed(d.queries))), _t.Reason.FOREIGN)
        self._direct_reject(lambda d: _forge(d, selectors=tuple(reversed(d.selectors))), _t.Reason.FOREIGN)

    # @anchor:wa.tests.scripts.test_compiler.CompilerControls.test_limit_pairs
    def test_limit_pairs(self):
        for size, accepted in ((31, True), (32, False)):
            host = _Host()
            host.registry._entry_bytes, host.registry._max_bytes = 4000000, 16000000
            post = _dc.replace(host.predicates[0][1], operator=_t.PredicateOperator.ALL,
                               operands=(), children=(host.predicates[0][1],) * size)
            host.config = _dc.replace(host.config, _predicates=(('clicked', post),) + tuple(host.predicates[1:]))
            if accepted:
                host._compile()
            else:
                self._reject(host, _t.Reason.BOUNDS)
        for count, accepted in ((54, True), (55, False)):
            body = _BODY.replace(b'    return None', b'    act.assert_tag("clicked", code="schema")\n' * count + b'    return None')
            host = _Host(body)
            host.registry._entry_bytes, host.registry._max_bytes = 4000000, 16000000
            if accepted:
                self.assertEqual(len(host._compile().nodes), 64)
            else:
                self._reject(host, _t.Reason.BOUNDS)
        host = _Host()
        _rewrite_header(host, lambda h: h['LANE_CONFIG']['limits'].update(canonical_bytes=128))
        self._reject(host, _t.Reason.BOUNDS)
        for field, value in (('timeout_ms', 2001), ('max_acquisitions', 65)):
            host = _Host()
            rules = list(host.config._effects)
            row = rules[0]
            rules[0] = row[:3] + (_forge(row[3], **{field: value}),)
            host.config = _dc.replace(host.config, _effects=tuple(rules))
            self._reject(host, _t.Reason.BOUNDS)

    # @anchor:wa.tests.scripts.test_compiler.CompilerControls.test_publication_failure_revokes
    def test_publication_failure_revokes(self):
        host = _Host()
        host._compile()
        host = _Host()
        with _mock.patch.object(host.retention, 'check', return_value=_t.AdmissionCheck(_t.Truth.FALSE, _t.Reason.FOREIGN)):
            self._reject(host, _t.Reason.FOREIGN)
        self.assertFalse(host.retention.live)
        host = _Host()
        plan = host._compile()
        host.retention.refuse_release = True
        with self.assertRaises(_t.ContractError) as raised:
            host.registry.release(plan)
        self.assertIs(raised.exception.reason, _t.Reason.RECONCILIATION)
        self.assertFalse(host.registry.outstanding()[0])
        self.assertTrue(host.registry.outstanding()[1])
        with self.assertRaises(_t.ContractError) as raised:
            host.registry.check(plan, host.owner, plan.identity, plan.scope, plan.abis)
        self.assertIs(raised.exception.reason, _t.Reason.FOREIGN)

    # @anchor:wa.tests.scripts.test_compiler.CompilerControls.test_effect_rules_and_find_order
    def test_effect_rules_and_find_order(self):
        _Host()._compile()
        host = _Host()
        host.config = _dc.replace(host.config, _effects=host.config._effects + (host.config._effects[0],))
        self._reject(host, _t.Reason.CONFLICT)
        host = _Host()
        host.config = _dc.replace(host.config, _effects=host.config._effects[1:])
        self._reject(host, _t.Reason.MISSING)
        self._direct_reject(lambda d: _forge(d, nodes=d.nodes[1:]), _t.Reason.FOREIGN)
        def click_arguments(d):
            return _forge(d, nodes=(d.nodes[0], _forge(d.nodes[1], effect=_forge(
                d.nodes[1].effect, arguments=d.nodes[3].effect.arguments))) + d.nodes[2:])
        self._direct_reject(click_arguments, _t.Reason.SCHEMA)
        host = _Host()
        generic = _dc.replace(host.predicates[0][1], operator=_t.PredicateOperator.TASK_GOAL)
        host.config = _dc.replace(host.config, _predicates=(('clicked', generic),) + tuple(host.predicates[1:]))
        host.denial = 'predicate'
        self._reject(host, _t.Reason.FOREIGN)

    # @anchor:wa.tests.scripts.test_compiler.CompilerControls.test_exact_literal_types
    def test_exact_literal_types(self):
        host = _Host(_BODY.replace(b'find("click")', b'find(1)'))
        host.config = _dc.replace(host.config, _queries=((1, host.queries[0][1]),) + tuple(host.queries[1:]))
        host._compile()
        host = _Host(_BODY.replace(b'find("click")', b'find(True)'))
        host.config = _dc.replace(host.config, _queries=((1, host.queries[0][1]),) + tuple(host.queries[1:]))
        self._reject(host, _t.Reason.FOREIGN)

    # @anchor:wa.tests.scripts.test_compiler.CompilerControls.test_representation_ceiling_pairs
    def test_representation_ceiling_pairs(self):
        def dimensions(tree):
            stack, count, depth = [(tree, 0)], 0, 0
            while stack:
                current, level = stack.pop()
                count += 1
                depth = max(depth, level)
                stack.extend((c, level + 1) for c in _ast.iter_child_nodes(current))
            return count, depth
        count, depth = dimensions(_ast.parse(_BODY))
        for field, exact in (('ast_nodes', count), ('ast_depth', depth)):
            host = _Host()
            host.admitter._parser = _dc.replace(host.parser, **{field: exact})
            host._compile()
            host = _Host()
            host.admitter._parser = _dc.replace(host.parser, **{field: exact - 1})
            self._reject(host, _t.Reason.BOUNDS)
        for policy_field, field, exact in (('_slots', 'slots', 2), ('_slots', 'input_slot_bytes', 128),
                                           ('_slots', 'harvest_result_bytes', 128),
                                           ('_source', 'header_lines', 8)):
            host = _Host()
            setattr(host.admitter, policy_field, _dc.replace(getattr(host.admitter, policy_field), **{field: exact}))
            host._compile()
            host = _Host()
            setattr(host.admitter, policy_field, _dc.replace(getattr(host.admitter, policy_field), **{field: exact - 1}))
            self._reject(host, _t.Reason.BOUNDS)
        host = _Host()
        host.source_bytes = b'x' * 65537
        self._reject(host, _t.Reason.BOUNDS)
        host = _Host()
        host.source_bytes += b'#' + b'x' * 16384
        self._reject(host, _t.Reason.BOUNDS)
        for deadline, accepted in ((100000000, True), (99999999, False)):
            host = _Host()
            rules = list(host.config._effects)
            row = rules[0]
            rules[0] = row[:3] + (_dc.replace(row[3], absolute_deadline_ns=deadline),)
            host.config = _dc.replace(host.config, _effects=tuple(rules))
            if accepted:
                host._compile()
            else:
                self._reject(host, _t.Reason.DEADLINE)

    # @anchor:wa.tests.scripts.test_compiler.CompilerControls.test_registry_identity_and_capacity
    def test_registry_identity_and_capacity(self):
        for field, value in (('_max_entries', 1), ('_max_bytes', 1000000)):
            host = _Host()
            setattr(host.registry, field, value)
            first = host._compile()
            with self.assertRaises(_t.ContractError) as raised:
                host._compile()
            self.assertIs(raised.exception.reason, _t.Reason.CAPACITY)
            self.assertEqual(len(host.registry.outstanding()[0]), 1)
            host.registry.release(first)
            host._compile()
        host = _Host()
        plan = host._compile()
        with self.assertRaises(_t.ContractError) as raised:
            host.registry.check(plan, _copy.copy(host.owner), plan.identity, plan.scope, plan.abis)
        self.assertIs(raised.exception.reason, _t.Reason.FOREIGN)
        for field in ('_generation', '_incarnation'):
            host = _Host()
            plan = host._compile()
            setattr(host.registry, field, 'stale')
            with self.assertRaises(_t.ContractError) as raised:
                host.registry.check(plan, host.owner, plan.identity, plan.scope, plan.abis)
            self.assertIs(raised.exception.reason, _t.Reason.FOREIGN)

    # @anchor:wa.tests.scripts.test_compiler.CompilerControls.test_privacy_nested_metadata
    def test_privacy_nested_metadata(self):
        host = _Host()
        _rewrite_header(host, lambda h: h['META:args'][0].update(destination_contract='PRIVATE-CANARY'))
        self._reject(host, _t.Reason.FOREIGN)
        host = _Host()
        host.source_bytes = host.source_bytes.replace(b'def run', b'# [META:cli] "PRIVATE-CANARY"\ndef run', 1)
        plan = host._compile()
        self.assertNotIn(b'PRIVATE-CANARY', plan.canonical_bytes)
        self.assertEqual(host.inputs, 0)

    # @anchor:wa.tests.scripts.test_compiler.CompilerControls.test_frozen_projections
    def test_frozen_projections(self):
        host = _Host(b'def run(act, params):\n    return None\n')
        vector = {"capability_contract":"input","destination_contract":"composer","kind":"TEXT",
                  "maximum":16,"minimum":1,"name":"query","normalization_abi":"wender-args/verbatim-v1",
                  "purpose":"INPUT","required":True,"sensitivity":"PRIVATE"}
        _rewrite_header(host, lambda h: (h['META:args'].clear(), h['META:args'].append(vector)))
        plan = host._compile()
        self.assertEqual(_wire_record(plan.slots[0]), vector)
        self.assertEqual(_json.dumps(_wire_record(plan.abis), sort_keys=True, separators=(',', ':')),
            '{"act":"wender-act/v1","compiler":"wender-compiler/v1","dsl":"wender-dsl/v1",'
            '"predicate":"wender-predicate/v1","schema":"wender-schema/v1"}')
        self.assertEqual(_json.loads(plan.canonical_bytes)['entry']['scope'], {
            'browser_profile_incarnation': 'profile', 'frame_ancestry': ['https://example.test'],
            'principal': 'p', 'scope_revision': 1, 'target_frame_origin': 'https://example.test',
            'tenant': 't', 'top_level_origin': 'https://example.test'})
        host.registry.release(plan)
        _rewrite_header(host, lambda h: h['META:args'][0].update(maximum=True))
        self._reject(host, _t.Reason.TYPE)

    # @anchor:wa.tests.scripts.test_compiler.CompilerControls.test_nested_payload_and_semantic_branches
    def test_nested_payload_and_semantic_branches(self):
        def arguments(d):
            return _forge(d, nodes=d.nodes[:3] + (_forge(d.nodes[3], effect=_forge(
                d.nodes[3].effect, arguments=list(d.nodes[3].effect.arguments))),) + d.nodes[4:])
        self._direct_reject(arguments, _t.Reason.TYPE)
        def empty(d):
            p = _forge(d.nodes[1].effect.postcondition, operator=_t.PredicateOperator.ALL, operands=(), children=())
            return _forge(d, nodes=(d.nodes[0], _forge(d.nodes[1], effect=_forge(
                d.nodes[1].effect, postcondition=p))) + d.nodes[2:])
        self._direct_reject(empty, _t.Reason.SCHEMA)
        host = _Host()
        ready = _dc.replace(host.entry, operator=_t.PredicateOperator.FOCUSED)
        for operator, children, accepted in (
            (_t.PredicateOperator.ALL, (ready, host.predicates[0][1]), True),
            (_t.PredicateOperator.ANY, (host.predicates[0][1], host.predicates[0][1]), True),
            (_t.PredicateOperator.NOT, (host.predicates[0][1],), False)):
            fixture = _Host()
            p = _dc.replace(fixture.predicates[0][1], operator=operator, operands=(), children=children)
            fixture.config = _dc.replace(fixture.config, _predicates=(('clicked', p),) + tuple(fixture.predicates[1:]))
            if accepted:
                fixture._compile()
            else:
                self._reject(fixture, _t.Reason.MISSING)


    # @anchor:wa.tests.scripts.test_compiler.CompilerControls.test_old_pins_after_mutation
    def test_old_pins_after_mutation(self):
        for field in ('selector', 'slot', 'ceiling', 'abi'):
            host = _Host()
            definition = host._direct()
            try:
                _c.canonical_plan(definition, session=host.session, admission=host.admitter, inputs=host.config)
                if field == 'selector':
                    object.__setattr__(definition.selectors[0], 'role', 'changed')
                elif field == 'slot':
                    object.__setattr__(definition.slots[0], 'maximum', 127)
                    object.__setattr__(host.session._header.arguments[0], 'maximum', 127)
                elif field == 'ceiling':
                    host.admitter._slots = _dc.replace(host.slots, input_slot_bytes=4095)
                else:
                    object.__setattr__(definition.header.abis, 'dsl', 'wender-dsl/v2')
                    object.__setattr__(host.session._header.abis, 'dsl', 'wender-dsl/v2')
                reason = _t.Reason.SCHEMA if field == 'abi' else _t.Reason.STALE
                self._reject(host, reason, lambda: _c.canonical_plan(definition,
                    session=host.session, admission=host.admitter, inputs=host.config))
            finally:
                host.admitter.finish(host.session)

    # @anchor:wa.tests.scripts.test_compiler.CompilerControls.test_retained_charge_precedes_source_release
    def test_retained_charge_precedes_source_release(self):
        host = _Host()
        finish = host.admitter.finish
        def charged_finish(session):
            self.assertEqual(len(host.retention.live), 1)
            self.assertEqual(len(host.registry.outstanding()[0]), 1)
            self.assertFalse(host.allocation.live)
            return finish(session)
        with _mock.patch.object(host.admitter, 'finish', side_effect=charged_finish):
            host._compile()
        host = _Host()
        host.retention.deny = True
        self._reject(host, _t.Reason.CAPACITY)
        self.assertEqual(host.attempt_count, 1)
        self.assertFalse(host.allocation.live)
        self.assertFalse(host.admitter.outstanding())


    # @anchor:wa.tests.scripts.test_compiler.CompilerControls.test_remaining_exact_boundaries
    def test_remaining_exact_boundaries(self):
        for field, size in (('utf8_bytes', len(_Host().source_bytes)),
                            ('unicode_scalars', len(_Host().source_bytes.decode()))):
            for delta, accepted in ((0, True), (-1, False)):
                host = _Host()
                host.admitter._source = _dc.replace(host.source, **{field: size + delta})
                if accepted:
                    host._compile()
                else:
                    self._reject(host, _t.Reason.BOUNDS)
        original = _Host()._compile()
        exact = len(original.canonical_bytes)
        exact -= len(str(original.limits.canonical_bytes)) - len(str(exact))
        for cap, accepted in ((exact, True), (exact - 1, False)):
            host = _Host()
            _rewrite_header(host, lambda h: h['LANE_CONFIG']['limits'].update(canonical_bytes=cap))
            if accepted:
                self.assertEqual(len(host._compile().canonical_bytes), cap)
            else:
                self._reject(host, _t.Reason.BOUNDS)
        for slots, accepted in ((32, True), (33, False)):
            host = _Host()
            host.registry._entry_bytes, host.registry._max_bytes = 4000000, 16000000
            extra = [_wire_record(_dc.replace(host.specs[0], name='slot' + str(i), maximum=1))
                     for i in range(slots - 2)]
            _rewrite_header(host, lambda h: h['META:args'].extend(extra))
            if accepted:
                self.assertEqual(len(host._compile().slots), slots)
            else:
                self._reject(host, _t.Reason.BOUNDS)
        for maximum, accepted in ((4096, True), (4097, False)):
            host = _Host()
            _rewrite_header(host, lambda h: h['META:args'][0].update(maximum=maximum))
            if accepted:
                host._compile()
            else:
                self._reject(host, _t.Reason.BOUNDS)
        for size, accepted in ((16384, True), (16385, False)):
            host = _Host(_BODY.replace(b'max_bytes=128', ('max_bytes=' + str(size)).encode()))
            result = _dc.replace(host.result, max_bytes=16384)
            row = host.harvests[0]
            host.config = _dc.replace(host.config, _harvests=((row[0], result) + row[2:],))
            if accepted:
                host._compile()
            else:
                self._reject(host, _t.Reason.BOUNDS)
        host = _Host()
        host.admitter._deadline = 3000000000
        _rewrite_header(host, lambda h: h['LANE_CONFIG']['limits'].update(absolute_deadline_ns=3000000000))
        rule = host.config._effects[0]
        verification = _dc.replace(rule[3], timeout_ms=2000, max_acquisitions=64,
                                   absolute_deadline_ns=2500000000)
        host.config = _dc.replace(host.config, _effects=(rule[:3] + (verification,),) + host.config._effects[1:])
        host._compile()
        for timeout, accepted in ((30000, True), (30001, False)):
            host = _Host(_BODY.replace(b'timeout_ms=100', ('timeout_ms=' + str(timeout)).encode()))
            host.admitter._deadline = 40000000000
            _rewrite_header(host, lambda h: h['LANE_CONFIG']['limits'].update(absolute_deadline_ns=40000000000))
            profile = _dc.replace(host.profile, semantic_wait_ms=30000, absolute_deadline_ns=35000000000)
            host.config = _dc.replace(host.config, _waits=(('clicked', profile),))
            if accepted:
                host._compile()
            else:
                self._reject(host, _t.Reason.BOUNDS)


    # @anchor:wa.tests.scripts.test_compiler.CompilerControls.test_publication_transaction_and_callback_mutation
    def test_publication_transaction_and_callback_mutation(self):
        host = _Host()
        host._compile()
        self.assertFalse(host.registry._transactions)
        error = None
        try:
            host.registry._publish(object())
        except Exception as exc:
            error = exc
        self.assertIs(type(error), _t.ContractError)
        self.assertIs(error.reason, _t.Reason.FOREIGN)
        host = _Host()
        reserve = host.retention.reserve
        def mutating_reserve(size, deadline):
            token = reserve(size, deadline)
            object.__setattr__(host.queries[0][1].selector, 'role', 'changed')
            return token
        with _mock.patch.object(host.retention, 'reserve', side_effect=mutating_reserve):
            self._reject(host, _t.Reason.STALE)
        self.assertFalse(host.retention.live)
        self.assertFalse(host.registry._transactions)
        host = _Host()
        release = host.allocation.release
        def mutating_release(token):
            result = release(token)
            object.__setattr__(host.queries[0][1].selector, 'role', 'changed')
            return result
        with _mock.patch.object(host.allocation, 'release', side_effect=mutating_release):
            self._reject(host, _t.Reason.STALE)
        self.assertFalse(host.retention.live)
        host = _Host()
        definition = host._direct()
        original = host.check_screen
        calls = 0
        def mutate_screen(digest):
            nonlocal calls
            calls += 1
            result = original(digest)
            # Mutate after validation, during identity verification. Q omits this field.
            if calls == 2:
                selector = definition.selectors[0]
                object.__setattr__(selector, 'label_export_receipt', _copy.copy(selector.label_export_receipt))
            return result
        try:
            with _mock.patch.object(host, 'check_screen', side_effect=mutate_screen):
                self._reject(host, _t.Reason.STALE, lambda: _c.canonical_plan(definition,
                    session=host.session, admission=host.admitter, inputs=host.config))
        finally:
            host.admitter.finish(host.session)



# @anchor:wa.tests.scripts.test_compiler.MutationControls
class MutationControls(_unittest.TestCase):
    """Remove actual guard calls in memory; leave the worktree byte-identical."""
    # @anchor:wa.tests.scripts.test_compiler.MutationControls.test_compiler_gate_mutants
    def test_compiler_gate_mutants(self):
        import contextlib
        import io
        source = _Path(_c.__file__).read_text()
        tree = _ast.parse(source)
        calls = [node for node in _ast.walk(tree) if isinstance(node, _ast.Call)
                 and isinstance(node.func, _ast.Name) and node.func.id in ('_need', '_check')]
        cases = [
            ('owner-scope', 'check_scope', 'test_owner_checks'),
            ('owner-query', 'check_query', 'test_owner_checks'),
            ('owner-predicate', 'check_predicate', 'test_owner_checks'),
            ('owner-slot', 'check_slot', 'test_owner_checks'),
            ('owner-key', 'check_key', 'test_g_args_keys'),
            ('owner-harvest', 'check_harvest', 'test_g_plan_harvest'),
            ('owner-export', 'check_export', 'test_owner_checks'),
            ('terminal-authority', ('components.terminal_goal is inputs._terminal', 'check_terminal'), 'test_g_plan_terminal'),
            ('effect-rule-uniqueness', 'len(rules) == 1', 'test_effect_rules_and_find_order'),
            ('target-semantic-proof', 'relevant(predicate)', 'test_g_plan_pairing'),
            ('query-table-order', 'zip(components.queries, expected_queries)', 'test_independent_mutation_and_cycles'),
            ('selector-table-order', 'zip(components.selectors, expected_selectors)', 'test_independent_mutation_and_cycles'),
            ('descriptor-episode', 'descriptor.episode_id', 'test_g_args_descriptors'),
            ('unique-result-writer', 'harvest.result.name not in written', 'test_g_plan_symbols'),
            ('find-before-effect', 'len({id(q) for q in candidates}) == 1', 'test_effect_rules_and_find_order'),
            ('click-argument-schema', 'effect.action is not _t.ActionKind.CLICK', 'test_effect_rules_and_find_order'),
            ('work-authority', 'type(inputs._work_bytes) is int and inputs._work_bytes > 0', 'test_g_resource_boundaries'),
            ('registry-entry-capacity', 'len(self._entries) < self._max_entries', 'test_registry_identity_and_capacity'),
            ('single-use-publication', 'completed is not None and completed[0] is transaction', 'test_publication_transaction_and_callback_mutation'),
            ('registry-object-custody', 'row is not None and row[0] is plan', 'test_g_pin_registry'),
            ('registry-mirrored-custody', 'compiler is self._compiler', 'test_registry_identity_and_capacity'),
            ('registry-mutation-witness', '_snapshot(plan, self._entry_bytes) == snapshot', 'test_independent_mutation_and_cycles'),
            ('verification-deadline', 'budget.timeout_ms * 1000000', 'test_representation_ceiling_pairs'),
            ('wait-profile', 'profile.semantic_wait_ms', 'test_g_resource_boundaries'),
        ]
        for label, fragment, test in cases:
            spans = [(call.lineno, call.end_lineno) for call in calls
                     if any(f in _ast.get_source_segment(source, call) for f in
                            (fragment if type(fragment) is tuple else (fragment,)))]
            self.assertTrue(spans, label)
            need, check = _c._need, _c._check
            def removed_need(ok, reason=_t.Reason.SCHEMA):
                caller = _sys._getframe(1)
                if caller.f_code.co_filename == _c.__file__ and any(a <= caller.f_lineno <= b for a, b in spans):
                    return
                return need(ok, reason)
            def removed_check(value):
                caller = _sys._getframe(1)
                if caller.f_code.co_filename == _c.__file__ and any(a <= caller.f_lineno <= b for a, b in spans):
                    return
                return check(value)
            with self.subTest(mutant=label), contextlib.ExitStack() as stack:
                stack.enter_context(_mock.patch.object(_c, '_need', removed_need))
                stack.enter_context(_mock.patch.object(_c, '_check', removed_check))
                result = _unittest.TextTestRunner(stream=io.StringIO()).run(CompilerControls(test))
                self.assertTrue(result.failures, label + ': negative oracle did not fail')
        host = _Host()
        session = _h.admit_source(host.source_bytes, admission=host.admitter)
        checked = _c._checked(session, host.admitter, host.config)
        host.admitter.finish(session)
        functional = (
            ('complete-ast-bridge', '_checked', lambda *a: checked, 'test_g_ast_bridge'),
            ('complete-source-grammar', '_checked', lambda *a: checked, 'test_g_plan_end_to_end'),
            ('execution-syntax-refusal', '_checked', lambda *a: checked, 'test_no_execution'),
            ('private-error-sanitizer', 'compile_draft', _c.compile_draft.__wrapped__, 'test_g_privacy'),
            ('recursive-record-validation', '_walk', lambda *a: 0, 'test_g_plan_ingress_equivalence'),
            ('immutable-transaction-witness', '_snapshot', lambda *a: (), 'test_publication_transaction_and_callback_mutation'),
            ('literal-type-distinction', '_same', lambda a, b: a == b, 'test_exact_literal_types'),
        )
        for label, name, replacement, test in functional:
            with self.subTest(mutant=label), _mock.patch.object(_c, name, replacement):
                result = _unittest.TextTestRunner(stream=io.StringIO()).run(CompilerControls(test))
                self.assertTrue(result.failures, label + ': negative oracle did not fail')

        def unchecked_pins(definition, *, session, admission, screen_check, **kwargs):
            canonical, identity = _h.recompute_identity(definition, session=session,
                admission=admission, screen_check=screen_check)
            return identity, canonical
        source_admit = _h.admit_source
        def permissive_admit(source, *, admission):
            try:
                return source_admit(source, admission=admission)
            except _t.ContractError:
                return source_admit(_Host().source_bytes, admission=admission)
        for label, target, name, replacement, test in (
            ('source-preflight-refusal', _h, 'admit_source', permissive_admit, 'test_g_source_vectors'),
            ('complete-pin-verification', _h, 'verify_pins', unchecked_pins, 'test_g_pin_recompute'),
            ('old-pin-components', _h, 'verify_pins', unchecked_pins, 'test_old_pins_after_mutation'),
            ('authenticated-source-cleanup', _h.SourceAdmission, 'finish', lambda *a: _TRUE, 'test_g_resource_cleanup'),
            ('assigned-write-scope', _sys.modules[__name__], '_routing', lambda *a: True, 'test_g_anchor_scope')):
            with self.subTest(mutant=label), _mock.patch.object(target, name, replacement):
                result = _unittest.TextTestRunner(stream=io.StringIO()).run(CompilerControls(test))
                self.assertTrue(result.failures, label + ': negative oracle did not fail')

        spans = [(call.lineno, call.end_lineno) for call in calls
                 if 'check_screen' in _ast.get_source_segment(source, call)]
        check, header_check = _c._check, _h._check
        def omit_compiler_screen(value):
            caller = _sys._getframe(1)
            if caller.f_code.co_filename == _c.__file__ and any(a <= caller.f_lineno <= b for a, b in spans):
                return
            return check(value)
        def omit_codec_screen(value):
            if _sys._getframe(1).f_code.co_name == '_identity_components':
                return
            return header_check(value)
        with _mock.patch.object(_c, '_check', omit_compiler_screen), _mock.patch.object(_h, '_check', omit_codec_screen):
            result = _unittest.TextTestRunner(stream=io.StringIO()).run(CompilerControls('test_owner_checks'))
            self.assertTrue(result.failures, 'screen-owner: negative oracle did not fail')


    # @anchor:wa.tests.scripts.test_compiler.MutationControls.test_every_resource_ceiling_mutant
    def test_every_resource_ceiling_mutant(self):
        """A shared ceiling-removal mutant is exercised on each named limit.

        These are broad policy mutants, not claims of minimal guard coverage.
        Every original refusal is checked before installing the mutant.
        """
        import contextlib
        def fixture(label):
            host = _Host()
            if label in ('source-bytes', 'source-scalars', 'header-lines'):
                field, bound = {'source-bytes': ('utf8_bytes', len(host.source_bytes) - 1),
                    'source-scalars': ('unicode_scalars', len(host.source_bytes.decode()) - 1),
                    'header-lines': ('header_lines', 7)}[label]
                host.admitter._source = _dc.replace(host.source, **{field: bound})
            elif label in ('ast-occurrences', 'ast-depth'):
                tree = _ast.parse(_BODY)
                stack, count, depth = [(tree, 0)], 0, 0
                while stack:
                    item, level = stack.pop()
                    count += 1
                    depth = max(depth, level)
                    stack.extend((c, level + 1) for c in _ast.iter_child_nodes(item))
                field, bound = ('ast_nodes', count - 1) if label == 'ast-occurrences' else ('ast_depth', depth - 1)
                host.admitter._parser = _dc.replace(host.parser, **{field: bound})
            elif label == 'predicates':
                p = _dc.replace(host.predicates[0][1], operator=_t.PredicateOperator.ALL,
                                 operands=(), children=(host.predicates[0][1],) * 32)
                host.config = _dc.replace(host.config, _predicates=(('clicked', p),) + tuple(host.predicates[1:]))
            elif label == 'plan-nodes':
                body = _BODY.replace(b'    return None', b'    act.assert_tag("clicked", code="schema")\n' * 55 + b'    return None')
                host = _Host(body)
            elif label == 'canonical-bytes':
                _rewrite_header(host, lambda h: h['LANE_CONFIG']['limits'].update(canonical_bytes=128))
            elif label == 'slot-count':
                extra = [_wire_record(_dc.replace(host.specs[0], name='slot' + str(i), maximum=1)) for i in range(31)]
                _rewrite_header(host, lambda h: h['META:args'].extend(extra))
            elif label == 'slot-bytes':
                _rewrite_header(host, lambda h: h['META:args'][0].update(maximum=4097))
            elif label == 'harvest-bytes':
                host = _Host(_BODY.replace(b'max_bytes=128', b'max_bytes=16385'))
            elif label in ('verification-ms', 'verification-acquisitions'):
                row = host.config._effects[0]
                field, value = ('timeout_ms', 2001) if label == 'verification-ms' else ('max_acquisitions', 65)
                host.admitter._deadline = 3000000000
                _rewrite_header(host, lambda h: h['LANE_CONFIG']['limits'].update(absolute_deadline_ns=3000000000))
                budget = _forge(row[3], absolute_deadline_ns=2500000000, **{field: value})
                host.config = _dc.replace(host.config, _effects=(row[:3] + (budget,),) + host.config._effects[1:])
            elif label == 'wait-ms':
                host = _Host(_BODY.replace(b'timeout_ms=100', b'timeout_ms=30001'))
                host.admitter._deadline = 40000000000
                _rewrite_header(host, lambda h: h['LANE_CONFIG']['limits'].update(absolute_deadline_ns=40000000000))
                host.config = _dc.replace(host.config, _waits=(('clicked', _dc.replace(host.profile,
                    semantic_wait_ms=30000, absolute_deadline_ns=35000000000)),))
            host.registry._entry_bytes, host.registry._max_bytes = 4000000, 16000000
            return host
        for label in ('source-bytes', 'source-scalars', 'header-lines', 'ast-occurrences', 'ast-depth',
                      'predicates', 'plan-nodes', 'canonical-bytes', 'slot-count', 'slot-bytes',
                      'harvest-bytes', 'verification-ms', 'verification-acquisitions', 'wait-ms'):
            with self.subTest(ceiling=label):
                control = CompilerControls()
                control._reject(fixture(label), _t.Reason.BOUNDS)
                with contextlib.ExitStack() as stack:
                    from wender.scripts import ast_gate as grammar
                    for target, name in ((_c, '_need'), (_h, '_need'), (grammar, '_need'), (_t, '_require')):
                        original = getattr(target, name)
                        def without_ceiling(ok, reason=_t.Reason.SCHEMA, original=original):
                            if reason is not _t.Reason.BOUNDS:
                                return original(ok, reason)
                        stack.enter_context(_mock.patch.object(target, name, without_ceiling))
                    # The original negative oracle must fail under removed bounds.
                    with self.assertRaises(AssertionError):
                        control._reject(fixture(label), _t.Reason.BOUNDS)
