# @module {"id":"wa.tests.scripts.test_predicates","layer":2,"owner":"LEARN","deps":["wa.scripts.predicates","wa.scripts.types","wa.tests.scripts.support"],"book":"HB-WA-s4-learn"}
"""Discriminating synthetic controls for retained pure predicate evaluation."""
from dataclasses import replace
from itertools import product
import unittest
from unittest.mock import patch
from wender.scripts import predicates as p, types as t
from tests.scripts.support import IdentityRegistry, make_fixture


def _fixture(op=t.PredicateOperator.STATE_FLAG, value='enabled', truth=t.Truth.TRUE):
    f = make_fixture()
    f['registry'] = r = IdentityRegistry(f['handle'], 7)
    f['target'] = r.register(t.PrivateRef('target', 'incarnation', 'private-canary'))
    obs = f['observation']
    before = replace(obs, publication_identity='earlier', producer_generation=6,
                     capture_start_ns=1, capture_end_ns=5)
    r.register(before)
    f['before'] = before
    operand = t.Operand('operand', 'query', t.ValueKind.INTEGER if
                        op is t.PredicateOperator.VALUE_LEN_DELTA else t.ValueKind.TEXT, value)
    transition = t.MissingRef('relation', None)
    node = t.PredicateSpec(op, (operand,), (), f['scope'], (t.EvidenceSource.AX,),
                          transition if op in (t.PredicateOperator.VALUE_LEN_DELTA,
                          t.PredicateOperator.DECLARED_TRANSITION) else t.MissingRef(None, t.Reason.MISSING))
    query = t.QueryDeclaration('query', f['selector'], f['scope'], (op,))
    fact = replace(f['fact'], value=truth, reason=t.MissingReason.ABSENT if truth is t.Truth.UNKNOWN else None)
    association = r.mint('host-association', 'synthetic')
    if op is t.PredicateOperator.EXISTS_UNIQUE:
        payload = p._InventoryEvidence(t.MissingRef('complete-query-inventory', None), ((f['target'], (fact,)),))
        r.register(payload.inventory_reference)
    elif op in (t.PredicateOperator.STATE_FLAG, t.PredicateOperator.FOCUSED):
        payload = p._StateEvidence(f['target'], 'focused' if op is t.PredicateOperator.FOCUSED else value, (fact,))
    elif op is t.PredicateOperator.VALUE_LEN_DELTA:
        payload = p._LengthEvidence(f['target'], before, obs, 10, 10 + value,
                                   'unicode-scalars', 'unicode-scalars', transition, association, (fact,))
    elif op is t.PredicateOperator.DECLARED_TRANSITION:
        payload = p._TransitionEvidence(transition, f['target'], before, obs, association, (fact,))
    else:
        payload = p._SemanticEvidence(value, f['target'], f['scope'], association, (fact,))
    binding = p._PredicateEvidence(node, query, obs, payload)
    for item in (obs, node, query, fact, payload, binding):
        r.register(item)
    f.update(node=node, query=query, fact=fact, payload=payload, binding=binding,
             args=dict(evidence=(binding,), registry=r, owner=f['handle'], generation=7,
                       observation=obs, max_nodes=32, max_evidence_rows=128,
                       max_inventory_rows=64, safety_vetoes=()))
    return f


def _run(f, node=None, **changes):
    return p.evaluate(f['node'] if node is None else node, f['scope'], **(f['args'] | changes))


def _payload(f, payload):
    r = f['registry']
    r.register(payload)
    if type(payload) is p._InventoryEvidence:
        r.register(payload.inventory_reference)
        for target, facts in payload.rows:
            r.register(target)
            for fact in facts:
                r.register(fact)
    else:
        for fact in payload.facts:
            r.register(fact)
    binding = r.register(replace(f['binding'], payload=payload))
    return (binding,)


def _logical(f, op, children):
    return t.PredicateSpec(op, (), children, f['scope'], (t.EvidenceSource.AX,),
                           t.MissingRef(None, t.Reason.MISSING))


# @anchor:wa.tests.scripts.test_predicates.TestPredicates
class TestPredicates(unittest.TestCase):
    def _unknown(self, result, reason):
        self.assertEqual(result, t.PredicateResult(t.Truth.UNKNOWN, reason))

    # @anchor:wa.tests.scripts.test_predicates.TestPredicates.test_operator_controls
    def test_operator_controls(self):
        for op, value in ((t.PredicateOperator.EXISTS_UNIQUE, 'matches'),
                          (t.PredicateOperator.STATE_FLAG, 'enabled'),
                          (t.PredicateOperator.FOCUSED, 'target'),
                          (t.PredicateOperator.VALUE_LEN_DELTA, 2),
                          (t.PredicateOperator.DECLARED_TRANSITION, 'relation'),
                          (t.PredicateOperator.SCOPED_TERMINAL, 'terminal'),
                          (t.PredicateOperator.TASK_GOAL, 'goal')):
            for truth in (t.Truth.TRUE, t.Truth.FALSE):
                with self.subTest(op=op, truth=truth):
                    f = _fixture(op, value, truth)
                    self.assertIs(_run(f).value, truth)
                    self._unknown(_run(f, evidence=()), t.Reason.MISSING)
                    if op in (t.PredicateOperator.VALUE_LEN_DELTA, t.PredicateOperator.DECLARED_TRANSITION):
                        self.assertIs(p.evaluate_transition(f['node'], f['scope'], **f['args']).value, truth)
        f = _fixture()
        self._unknown(p.evaluate_transition(f['node'], f['scope'], **f['args']), t.Reason.TYPE)

    # @anchor:wa.tests.scripts.test_predicates.TestPredicates.test_g_pred
    def test_g_pred(self):
        f = _fixture()
        negation = _logical(f, t.PredicateOperator.NOT, (f['node'],))
        self.assertIs(_run(f).value, t.Truth.TRUE)
        self.assertIs(_run(f, negation).value, t.Truth.FALSE)
        self._unknown(_run(f, negation, evidence=()), t.Reason.MISSING)
        incomplete = replace(f['observation'], completeness=t.MissingRef(None, t.Reason.MISSING))
        self._unknown(_run(f, negation, evidence=(f['registry'].register(replace(
            f['binding'], observation=incomplete)),), observation=incomplete), t.Reason.MISSING)
        expr = _logical(f, t.PredicateOperator.ANY, (f['node'], negation))
        self.assertIs(_run(f, expr).value, t.Truth.TRUE)
        self.assertIs(_run(f, expr, safety_vetoes=(t.PredicateResult(t.Truth.FALSE, None),)).value, t.Truth.FALSE)
        self._unknown(_run(f, expr, safety_vetoes=(t.PredicateResult(t.Truth.UNKNOWN, t.Reason.MISSING),)), t.Reason.MISSING)
        self.assertIs(_run(f, negation, safety_vetoes=(t.PredicateResult(t.Truth.UNKNOWN, t.Reason.MISSING),)).value, t.Truth.FALSE)

    # @anchor:wa.tests.scripts.test_predicates.TestPredicates.test_logic_exhaustive_and_no_short_circuit
    def test_logic_exhaustive_and_no_short_circuit(self):
        for left, right in product(t.Truth, repeat=2):
            f = _fixture(truth=left)
            node = replace(f['node'], operands=(replace(f['node'].operands[0], operand_id='other'),))
            fact = replace(f['fact'], value=right, reason=t.MissingReason.ABSENT if right is t.Truth.UNKNOWN else None)
            payload = replace(f['payload'], facts=(fact,))
            binding = replace(f['binding'], predicate=node, payload=payload)
            for item in (node, fact, payload, binding):
                f['registry'].register(item)
            for op, oracle in ((t.PredicateOperator.ALL, t.truth_all), (t.PredicateOperator.ANY, t.truth_any)):
                expression = _logical(f, op, (f['node'], node))
                with patch.object(f['registry'], 'check', wraps=f['registry'].check) as check:
                    result = _run(f, expression, evidence=(f['binding'], binding))
                self.assertIs(result.value, oracle((left, right)))
                checked = [call.args[0] for call in check.call_args_list]
                self.assertTrue(any(item is binding for item in checked))
                self.assertTrue(any(item is f['binding'] for item in checked))
        for truth in t.Truth:
            f = _fixture(truth=truth)
            negation = _logical(f, t.PredicateOperator.NOT, (f['node'],))
            self.assertIs(_run(f, negation).value, t.truth_not(truth))
            with self.assertRaises(t.ContractError):
                bool(truth)

    # @anchor:wa.tests.scripts.test_predicates.TestPredicates.test_inventory_controls
    def test_inventory_controls(self):
        f = _fixture(t.PredicateOperator.EXISTS_UNIQUE, 'matching')
        payload = f['payload']
        self.assertIs(_run(f).value, t.Truth.TRUE)
        self.assertIs(_run(f, evidence=_payload(f, replace(payload, rows=()))).value, t.Truth.FALSE)
        second = t.PrivateRef('target', 'incarnation', 'other')
        multiple = replace(payload, rows=payload.rows + ((second, (f['fact'],)),))
        self.assertIs(_run(f, evidence=_payload(f, multiple)).value, t.Truth.FALSE)
        self._unknown(_run(f, evidence=_payload(f, replace(payload, rows=payload.rows * 2))), t.Reason.CONFLICT)
        unknown = replace(f['fact'], value=t.Truth.UNKNOWN, reason=t.MissingReason.ABSENT)
        self._unknown(_run(f, evidence=_payload(f, replace(payload, rows=((f['target'], (unknown,)),)))), t.Reason.MISSING)
        self._unknown(_run(f, evidence=_payload(f, replace(payload,
            inventory_reference=t.MissingRef(None, t.Reason.MISSING)))), t.Reason.MISSING)
        self._unknown(_run(f, evidence=_payload(f, multiple), max_inventory_rows=1), t.Reason.BOUNDS)
        # Two sources attest one target: uniqueness counts target identities.
        visual = replace(f['fact'], source=t.EvidenceSource.HOST)
        same = replace(payload, rows=((f['target'], (f['fact'], visual)),))
        self.assertIs(_run(f, evidence=_payload(f, same)).value, t.Truth.TRUE)

    # @anchor:wa.tests.scripts.test_predicates.TestPredicates.test_retention_query_and_provenance
    def test_retention_query_and_provenance(self):
        f = _fixture()
        self.assertIs(_run(f).value, t.Truth.TRUE)
        self._unknown(_run(f, evidence=(replace(f['binding']),)), t.Reason.FOREIGN)
        foreign = IdentityRegistry(f['handle'], 7)
        self._unknown(_run(f, registry=foreign), t.Reason.FOREIGN)
        self._unknown(_run(f, generation=8), t.Reason.STALE)
        self._unknown(_run(f, observation=replace(f['observation'])), t.Reason.STALE)
        self._unknown(_run(f, evidence=(f['binding'], f['binding'])), t.Reason.CONFLICT)
        for query in (replace(f['query'], name='wrong'),
                      replace(f['query'], operators=(t.PredicateOperator.FOCUSED,)),
                      replace(f['query'], selector=replace(f['selector'], container_owner='other'))):
            # Even an equal-looking replacement needs its own retained admission.
            binding = f['registry'].register(replace(f['binding'], query=query))
            self._unknown(_run(f, evidence=(binding,)), t.Reason.FOREIGN)
        wrong = replace(f['node'], operands=(replace(f['node'].operands[0], value='visible'),))
        f['registry'].register(wrong)
        self._unknown(_run(f, wrong), t.Reason.MISSING)
        missing_source = replace(f['fact'], source=t.EvidenceSource.HOST)
        self._unknown(_run(f, evidence=_payload(f, replace(f['payload'], facts=(missing_source,)))), t.Reason.MISSING)
        stale_fact = replace(f['fact'], observation=replace(f['observation'], publication_identity='unrelated'))
        self._unknown(_run(f, evidence=_payload(f, replace(f['payload'], facts=(stale_fact,)))), t.Reason.STALE)
        conflict = replace(f['fact'], value=t.Truth.FALSE)
        self._unknown(_run(f, evidence=_payload(f, replace(f['payload'], facts=(f['fact'], conflict)))), t.Reason.CONFLICT)

    # @anchor:wa.tests.scripts.test_predicates.TestPredicates.test_scope_controls
    def test_scope_controls(self):
        f = _fixture()
        self.assertIs(_run(f).value, t.Truth.TRUE)
        for field, value in (('principal', 'foreign'), ('tenant', 'foreign'),
                             ('browser_profile_incarnation', 'foreign'),
                             ('top_level_origin', 'https://foreign.test'),
                             ('target_frame_origin', 'https://foreign.test'),
                             ('frame_ancestry', ('https://foreign.test',)), ('scope_revision', 2)):
            self._unknown(p.evaluate(f['node'], replace(f['scope'], **{field: value}), **f['args']), t.Reason.STALE)

    # @anchor:wa.tests.scripts.test_predicates.TestPredicates.test_focus_and_semantic_binding
    def test_focus_and_semantic_binding(self):
        f = _fixture(t.PredicateOperator.FOCUSED, 'target')
        self.assertIs(_run(f).value, t.Truth.TRUE)
        self._unknown(_run(f, evidence=_payload(f, replace(f['payload'], state='visible'))), t.Reason.FOREIGN)
        other = t.PrivateRef('foreign', 'foreign', 'foreign')
        self._unknown(_run(f, evidence=_payload(f, replace(f['payload'], target=other))), t.Reason.FOREIGN)
        for op in (t.PredicateOperator.TASK_GOAL, t.PredicateOperator.SCOPED_TERMINAL):
            f = _fixture(op, 'host-semantic')
            self.assertIs(_run(f).value, t.Truth.TRUE)
            self._unknown(_run(f, evidence=_payload(f, replace(f['payload'], identifier='generic-page-terminal'))), t.Reason.FOREIGN)
            self._unknown(_run(f, evidence=_payload(f, replace(f['payload'], scope=replace(f['scope'], tenant='other')))), t.Reason.STALE)
            self._unknown(_run(f, evidence=_payload(f, replace(f['payload'], host_association=replace(f['payload'].host_association)))), t.Reason.FOREIGN)

    # @anchor:wa.tests.scripts.test_predicates.TestPredicates.test_length_and_transition_controls
    def test_length_and_transition_controls(self):
        for delta in (-2, 0, 3):
            f = _fixture(t.PredicateOperator.VALUE_LEN_DELTA, delta)
            self.assertIs(_run(f).value, t.Truth.TRUE)
            self.assertIs(_run(f, evidence=_payload(f, replace(f['payload'], after_length=f['payload'].after_length + 1))).value, t.Truth.FALSE)
            self._unknown(_run(f, evidence=_payload(f, replace(f['payload'], after_unit='bytes'))), t.Reason.SCHEMA)
            with self.assertRaises(t.ContractError) as error:
                replace(f['payload'], before_length=True)
            self.assertIs(error.exception.reason, t.Reason.TYPE)
        for op, value in ((t.PredicateOperator.DECLARED_TRANSITION, 'relation'),
                          (t.PredicateOperator.VALUE_LEN_DELTA, 2)):
            f = _fixture(op, value)
            self.assertIs(_run(f).value, t.Truth.TRUE)
            for change, reason in ((dict(transition_reference=t.MissingRef(None, t.Reason.MISSING)), t.Reason.MISSING),
                                   (dict(transition_reference=t.MissingRef('other', None)), t.Reason.MISSING),
                                   (dict(before=f['observation']), t.Reason.STALE),
                                   (dict(after=replace(f['observation'])), t.Reason.STALE),
                                   (dict(causal_association=replace(f['payload'].causal_association)), t.Reason.FOREIGN)):
                self._unknown(_run(f, evidence=_payload(f, replace(f['payload'], **change))), reason)
            for before in (replace(f['before'], publication_identity=f['observation'].publication_identity),
                           replace(f['before'], producer_identity='other'),
                           replace(f['before'], producer_schema='other'),
                           replace(f['before'], producer_generation=7),
                           replace(f['before'], capture_end_ns=10)):
                f['registry'].register(before)
                self._unknown(_run(f, evidence=_payload(f, replace(f['payload'], before=before))), t.Reason.STALE)
            # Generic state truths are not a registered relation.
            generic = p._StateEvidence(f['target'], 'enabled', (f['fact'], f['fact']))
            self._unknown(_run(f, evidence=_payload(f, generic)), t.Reason.TYPE)

    # @anchor:wa.tests.scripts.test_predicates.TestPredicates.test_bounded_whole_tree_validation
    def test_bounded_whole_tree_validation(self):
        f = _fixture()
        self.assertIs(_run(f, max_nodes=1, max_evidence_rows=2, max_inventory_rows=1).value, t.Truth.TRUE)
        for key in ('max_nodes', 'max_evidence_rows', 'max_inventory_rows'):
            for value in (None, 0, -1, True, '1'):
                self._unknown(_run(f, **{key: value}), t.Reason.BOUNDS)
        self._unknown(_run(f, max_evidence_rows=1), t.Reason.BOUNDS)
        shared = _logical(f, t.PredicateOperator.ALL, (f['node'], f['node']))
        self.assertIs(_run(f, shared, max_nodes=3).value, t.Truth.TRUE)
        self._unknown(_run(f, shared, max_nodes=2), t.Reason.BOUNDS)
        tree = f['node']
        for _ in range(7):
            tree = _logical(f, t.PredicateOperator.NOT, (tree,))
        self.assertIs(_run(f, tree).value, t.Truth.FALSE)
        self._unknown(_run(f, _logical(f, t.PredicateOperator.NOT, (tree,))), t.Reason.BOUNDS)
        wide = _logical(f, t.PredicateOperator.ALL, (f['node'],) * 31)
        self.assertIs(_run(f, wide).value, t.Truth.TRUE)
        self._unknown(_run(f, _logical(f, t.PredicateOperator.ALL, (f['node'],) * 32), max_nodes=100), t.Reason.BOUNDS)
        cycle = _logical(f, t.PredicateOperator.NOT, (f['node'],))
        object.__setattr__(cycle, 'children', (cycle,))
        self._unknown(_run(f, cycle), t.Reason.SCHEMA)

    # @anchor:wa.tests.scripts.test_predicates.TestPredicates.test_malformed_schema_never_negated
    def test_malformed_schema_never_negated(self):
        for field, value, reason in (('operator', 'unknown', t.Reason.TYPE),
                                     ('children', ('not-a-node',), t.Reason.TYPE),
                                     ('operands', (), t.Reason.SCHEMA),
                                     ('children', (make_fixture()['entry'],), t.Reason.SCHEMA)):
            f = _fixture()
            negation = _logical(f, t.PredicateOperator.NOT, (f['node'],))
            self.assertIs(_run(f).value, t.Truth.TRUE)
            object.__setattr__(f['node'], field, value)
            self._unknown(_run(f, negation), reason)
        f = _fixture()
        for op in (t.PredicateOperator.ALL, t.PredicateOperator.ANY, t.PredicateOperator.NOT):
            bad = _logical(f, t.PredicateOperator.NOT, (f['node'],))
            object.__setattr__(bad, 'operator', op)
            object.__setattr__(bad, 'children', ())
            self._unknown(_run(f, bad), t.Reason.SCHEMA)
        for kind, value, reason in ((t.ValueKind.INTEGER, True, t.Reason.TYPE),
                                    (t.ValueKind.TEXT, 'not-a-state', t.Reason.SCHEMA)):
            with self.assertRaises(t.ContractError) as error:
                replace(f['node'], operands=(t.Operand('operand', 'query', kind, value),))
            self.assertIs(error.exception.reason, reason)

    # @anchor:wa.tests.scripts.test_predicates.TestPredicates.test_missing_policy_and_complete_ingress
    def test_missing_policy_and_complete_ingress(self):
        f = _fixture()
        self.assertIs(_run(f).value, t.Truth.TRUE)
        for key in ('max_nodes', 'max_evidence_rows', 'max_inventory_rows'):
            args = dict(f['args'])
            args.pop(key)
            self._unknown(p.evaluate(f['node'], f['scope'], **args), t.Reason.BOUNDS)
        # A malformed later child/veto cannot leave an evaluated prefix.
        bad = replace(f['node'])
        expression = _logical(f, t.PredicateOperator.ANY, (f['node'], bad))
        object.__setattr__(bad, 'source_requirements', ())
        with patch.object(f['registry'], 'check', wraps=f['registry'].check) as check:
            self._unknown(_run(f, expression), t.Reason.SCHEMA)
        check.assert_not_called()
        self._unknown(_run(f, safety_vetoes=(t.PredicateResult(t.Truth.FALSE, None), True)), t.Reason.TYPE)
        # An equal fact copy and an unregistered payload do not inherit admission.
        payload = replace(f['payload'], facts=(replace(f['fact']),))
        binding = f['registry'].register(replace(f['binding'], payload=payload))
        self._unknown(_run(f, evidence=(binding,)), t.Reason.FOREIGN)
        f['registry'].register(payload)
        self._unknown(_run(f, evidence=(binding,)), t.Reason.FOREIGN)
        f['registry'].register(payload.facts[0])
        self.assertIs(_run(f, evidence=(binding,)).value, t.Truth.TRUE)

    # @anchor:wa.tests.scripts.test_predicates.TestPredicates.test_fact_schema_generation_and_association
    def test_fact_schema_generation_and_association(self):
        for field, value in (('source_schema', 'wrong'), ('source_generation', 8),
                             ('association', t.MissingRef('unrelated', None))):
            f = _fixture()
            self.assertIs(_run(f).value, t.Truth.TRUE)
            fact = replace(f['fact'])
            object.__setattr__(fact, field, value)
            self._unknown(_run(f, evidence=_payload(f, replace(f['payload'], facts=(fact,)))), t.Reason.STALE)
        f = _fixture()
        obs = replace(f['observation'], producer_generation=None, generation_reason=t.Reason.MISSING)
        f['registry'].register(obs)
        binding = f['registry'].register(replace(f['binding'], observation=obs))
        self._unknown(_run(f, evidence=(binding,), observation=obs), t.Reason.MISSING)
        # Coverage is bounded before inspecting an oversized fact tuple.
        payload = replace(f['payload'], facts=(f['fact'],) * 128)
        self._unknown(_run(f, evidence=_payload(f, payload), max_evidence_rows=128), t.Reason.BOUNDS)
        self.assertIs(_run(f, evidence=_payload(f, payload), max_evidence_rows=129).value, t.Truth.TRUE)

    # @anchor:wa.tests.scripts.test_predicates.TestPredicates.test_privacy_and_purity
    def test_privacy_and_purity(self):
        f = _fixture()
        with patch('builtins.open', side_effect=AssertionError('IO')), \
             patch('subprocess.run', side_effect=AssertionError('dispatch')), \
             patch('socket.socket', side_effect=AssertionError('network')), \
             patch('time.sleep', side_effect=AssertionError('wait')):
            self.assertIs(_run(f).value, t.Truth.TRUE)
        with patch.object(f['registry'], 'check', side_effect=RuntimeError('private-canary')):
            result = _run(f)
        self._unknown(result, t.Reason.TYPE)
        self.assertNotIn('private-canary', repr(result))
        self.assertNotIn('private-canary', repr(f['payload']))
        object.__setattr__(f['node'], 'operator', 'private-canary')
        self._unknown(_run(f), t.Reason.TYPE)
        self.assertNotIn('private-canary', repr(_run(f)))

    # @anchor:wa.tests.scripts.test_predicates.TestPredicates.test_retained_nested_record_validation
    def test_retained_nested_record_validation(self):
        for reference, field, value, reason in (
                ('completeness', 'value', True, t.Reason.TYPE),
                ('association', 'value', True, t.Reason.TYPE),
                ('projection', 'value', True, t.Reason.TYPE),
                ('completeness', 'reason', t.Reason.MISSING, t.Reason.SCHEMA),
                ('inventory', 'value', True, t.Reason.TYPE),
                ('inventory', 'reason', t.Reason.MISSING, t.Reason.SCHEMA),
                ('target', 'token', True, t.Reason.TYPE),
                ('target', 'owner', True, t.Reason.TYPE),
                ('transition', 'value', True, t.Reason.TYPE),
                ('causal', 'generation', True, t.Reason.TYPE)):
            with self.subTest(reference=reference, field=field):
                op = (t.PredicateOperator.EXISTS_UNIQUE if reference == 'inventory'
                      else t.PredicateOperator.DECLARED_TRANSITION
                      if reference in ('transition', 'causal') else t.PredicateOperator.STATE_FLAG)
                f = _fixture(op, 'relation' if op is t.PredicateOperator.DECLARED_TRANSITION
                             else 'enabled')
                self.assertIs(_run(f).value, t.Truth.TRUE)
                record = (f['payload'].inventory_reference if reference == 'inventory'
                          else f['target'] if reference == 'target'
                          else f['payload'].transition_reference if reference == 'transition'
                          else f['payload'].causal_association if reference == 'causal'
                          else getattr(f['observation'], reference))
                object.__setattr__(record, field, value)
                self._unknown(_run(f), reason)
        # Inventory target rows have the same exact reference contract.
        f = _fixture(t.PredicateOperator.EXISTS_UNIQUE, 'matches')
        self.assertIs(_run(f).value, t.Truth.TRUE)
        object.__setattr__(f['target'], 'incarnation', True)
        self._unknown(_run(f), t.Reason.TYPE)

    # @anchor:wa.tests.scripts.test_predicates.TestPredicates.test_query_bounds_before_s0_traversal
    def test_query_bounds_before_s0_traversal(self):
        f = _fixture()
        with patch.object(t.QueryDeclaration, '__post_init__',
                          autospec=True, side_effect=t.QueryDeclaration.__post_init__) as validate:
            self.assertIs(_run(f, max_nodes=1, max_evidence_rows=2,
                               max_inventory_rows=1).value, t.Truth.TRUE)
            validate.assert_called_once_with(f['query'])
            validate.reset_mock()
            oversized = f['query'].operators * 10000
            object.__setattr__(f['query'], 'operators', oversized)
            with patch.object(t, '_matches', wraps=t._matches) as matches:
                self._unknown(_run(f, max_nodes=1, max_evidence_rows=2,
                                   max_inventory_rows=1), t.Reason.BOUNDS)
            validate.assert_not_called()
            self.assertFalse(any(call.args[0] is oversized for call in matches.call_args_list))

    # @anchor:wa.tests.scripts.test_predicates.TestPredicates.test_transition_operator_callback_rejected
    def test_transition_operator_callback_rejected(self):
        f = _fixture(t.PredicateOperator.DECLARED_TRANSITION, 'relation')
        self.assertIs(p.evaluate_transition(f['node'], f['scope'], **f['args']).value,
                      t.Truth.TRUE)
        equality = unittest.mock.Mock(side_effect=RuntimeError('private-canary'))

        class _MalformedOperator:
            __eq__ = equality

        object.__setattr__(f['node'], 'operator', _MalformedOperator())
        self._unknown(p.evaluate_transition(f['node'], f['scope'], **f['args']), t.Reason.TYPE)
        self._unknown(_run(f), t.Reason.TYPE)
        equality.assert_not_called()

    # @anchor:wa.tests.scripts.test_predicates.TestPredicates.test_declared_relation_identifier
    def test_declared_relation_identifier(self):
        for truth in (t.Truth.TRUE, t.Truth.FALSE):
            f = _fixture(t.PredicateOperator.DECLARED_TRANSITION, 'relation', truth)
            self.assertIs(_run(f).value, truth)
            self.assertIs(p.evaluate_transition(f['node'], f['scope'], **f['args']).value, truth)
            object.__setattr__(f['node'].operands[0], 'value', 'different-relation')
            self._unknown(_run(f), t.Reason.FOREIGN)
            self._unknown(p.evaluate_transition(f['node'], f['scope'], **f['args']), t.Reason.FOREIGN)

    # @anchor:wa.tests.scripts.test_predicates.TestPredicates.test_nested_scopes_before_comparison
    def test_nested_scopes_before_comparison(self):
        for field in ('scope', 'scope_revision', 'attestation'):
            f = _fixture(truth=t.Truth.FALSE)
            negation = _logical(f, t.PredicateOperator.NOT, (f['node'],))
            self.assertIs(_run(f, negation).value, t.Truth.TRUE)
            # An equal copy has no authority to bypass structural validation.
            child_scope = replace(f['scope'])
            object.__setattr__(f['node'], 'scope', child_scope)
            self.assertIs(_run(f, negation).value, t.Truth.TRUE)
            equality = unittest.mock.Mock(side_effect=RuntimeError('private-canary'))

            class _Malformed:
                __eq__ = equality

            if field == 'scope':
                object.__setattr__(f['node'], field, _Malformed())
            elif field == 'attestation':
                object.__setattr__(child_scope, field, _Malformed())
            else:
                object.__setattr__(child_scope, field, True)
                self._unknown(_run(f, negation), t.Reason.TYPE)
                object.__setattr__(child_scope, field, _Malformed())
            self._unknown(_run(f, negation), t.Reason.TYPE)
            equality.assert_not_called()
        # Root nodes receive the same checks even without parent S0 validation.
        f = _fixture()
        object.__setattr__(f['node'], 'scope', replace(f['scope']))
        self.assertIs(_run(f).value, t.Truth.TRUE)
        object.__setattr__(f['node'].scope, 'scope_revision', True)
        self._unknown(_run(f), t.Reason.TYPE)

    # @anchor:wa.tests.scripts.test_predicates.TestPredicates.test_retained_selector_validation
    def test_retained_selector_validation(self):
        for record, field in (('selector', 'container_owner'),
                              ('label_export_receipt', 'owner'),
                              ('header_contract', 'value')):
            f = _fixture()
            self.assertIs(_run(f).value, t.Truth.TRUE)
            selector = f['query'].selector
            target = selector if record == 'selector' else getattr(selector, record)
            object.__setattr__(target, field, True)
            self._unknown(_run(f), t.Reason.TYPE)
        f = _fixture()
        self.assertIs(_run(f).value, t.Truth.TRUE)
        query_scope = replace(f['scope'])
        object.__setattr__(f['query'], 'scope', query_scope)
        object.__setattr__(query_scope, 'scope_revision', True)
        self._unknown(_run(f), t.Reason.TYPE)

    # @anchor:wa.tests.scripts.test_predicates.TestPredicates.test_metadata_bounds_before_traversal
    def test_metadata_bounds_before_traversal(self):
        for location in ('context', 'child', 'observation', 'query', 'semantic', 'before'):
            f = (_fixture(t.PredicateOperator.TASK_GOAL, 'goal') if location == 'semantic'
                 else _fixture(t.PredicateOperator.DECLARED_TRANSITION, 'relation')
                 if location == 'before' else _fixture())
            scope = replace(f['scope'], frame_ancestry=('https://a.test',) * 2)
            node = f['node']
            if location == 'context':
                object.__setattr__(f['scope'], 'frame_ancestry', scope.frame_ancestry)
                scope = f['scope']
            elif location == 'child':
                node = _logical(f, t.PredicateOperator.NOT, (node,))
                object.__setattr__(f['node'], 'scope', scope)
                object.__setattr__(f['scope'], 'frame_ancestry', scope.frame_ancestry)
            else:
                record = (f['payload'] if location == 'semantic' else
                          f['before'] if location == 'before' else f[location])
                object.__setattr__(record, 'scope', scope)
                object.__setattr__(f['scope'], 'frame_ancestry', scope.frame_ancestry)
            expected = t.Truth.FALSE if location == 'child' else t.Truth.TRUE
            self.assertIs(_run(f, node, max_evidence_rows=2).value, expected)
            oversized = ('https://a.test',) * 1000
            object.__setattr__(scope, 'frame_ancestry', oversized)
            with patch.object(t, '_matches', wraps=t._matches) as matches:
                self._unknown(_run(f, node, max_evidence_rows=2), t.Reason.BOUNDS)
            self.assertFalse(any(call.args[0] is oversized for call in matches.call_args_list))
        for field in ('allowed_match_levels', 'structural_identity_requirements'):
            f = _fixture()
            self.assertIs(_run(f, max_evidence_rows=2).value, t.Truth.TRUE)
            oversized = getattr(f['query'].selector, field) * 1000
            object.__setattr__(f['query'].selector, field, oversized)
            with patch.object(t, '_matches', wraps=t._matches) as matches:
                self._unknown(_run(f, max_evidence_rows=2), t.Reason.BOUNDS)
            self.assertFalse(any(call.args[0] is oversized for call in matches.call_args_list))


    # @anchor:wa.tests.scripts.test_predicates.TestPredicates.test_frozen_truth_vectors_literal
    def test_frozen_truth_vectors_literal(self):
        for known in (t.Truth.TRUE, t.Truth.FALSE):
            f = _fixture(truth=known)
            absent = replace(f['node'])
            negation = _logical(f, t.PredicateOperator.NOT, (absent,))
            self._unknown(_run(f, negation), t.Reason.MISSING)
            self.assertEqual(_run(f, _logical(f, t.PredicateOperator.NOT, (f['node'],))),
                             t.PredicateResult(t.Truth.FALSE if known is t.Truth.TRUE
                                               else t.Truth.TRUE, None))
            # Literal expected values, independent of the runtime truth helpers.
            expected = ((t.PredicateOperator.ALL, t.Truth.UNKNOWN, t.Reason.MISSING),
                        (t.PredicateOperator.ANY, t.Truth.TRUE, None)) if known is t.Truth.TRUE else (
                        (t.PredicateOperator.ALL, t.Truth.FALSE, None),
                        (t.PredicateOperator.ANY, t.Truth.UNKNOWN, t.Reason.MISSING))
            for op, value, reason in expected:
                for children in ((f['node'], absent), (absent, f['node'])):
                    self.assertEqual(_run(f, _logical(f, op, children)),
                                     t.PredicateResult(value, reason))

    # @anchor:wa.tests.scripts.test_predicates.TestPredicates.test_veto_precedence_both_entrypoints
    def test_veto_precedence_both_entrypoints(self):
        yes = t.PredicateResult(t.Truth.TRUE, None)
        no = t.PredicateResult(t.Truth.FALSE, None)
        unknown = t.PredicateResult(t.Truth.UNKNOWN, t.Reason.MISSING)
        for op, value in ((t.PredicateOperator.STATE_FLAG, 'enabled'),
                          (t.PredicateOperator.DECLARED_TRANSITION, 'relation'),
                          (t.PredicateOperator.VALUE_LEN_DELTA, 2)):
            f = _fixture(op, value)
            node = (_logical(f, t.PredicateOperator.ANY, (f['node'], replace(f['node'])))
                    if op is t.PredicateOperator.STATE_FLAG else f['node'])
            entries = (p.evaluate,) if op is t.PredicateOperator.STATE_FLAG else (
                p.evaluate, p.evaluate_transition)
            for entry in entries:
                for vetoes, result in (((yes,), yes), ((no,), no), ((unknown,), unknown),
                                      ((no, unknown), no), ((unknown, no), no)):
                    with self.subTest(op=op, entry=entry.__name__, vetoes=vetoes):
                        self.assertEqual(entry(node, f['scope'], **(f['args'] | {
                            'safety_vetoes': vetoes})), result)
                # UNKNOWN safety must also preserve an independently known FALSE.
                false_evidence = _payload(f, replace(f['payload'], facts=(replace(
                    f['fact'], value=t.Truth.FALSE),)))
                self.assertEqual(entry(f['node'], f['scope'], **(f['args'] | {
                    'evidence': false_evidence, 'safety_vetoes': (unknown,)})), no)

    # @anchor:wa.tests.scripts.test_predicates.TestPredicates.test_complete_inventory_counts_targets
    def test_complete_inventory_counts_targets(self):
        f = _fixture(t.PredicateOperator.EXISTS_UNIQUE, 'matches')
        payload = replace(f['payload'], rows=((f['target'], (f['fact'], replace(
            f['fact'], source=t.EvidenceSource.HOST))),))
        self.assertEqual(_run(f, evidence=_payload(f, payload)),
                         t.PredicateResult(t.Truth.TRUE, None))
        incomplete = replace(payload, inventory_reference=t.MissingRef(None, t.Reason.MISSING))
        self._unknown(_run(f, evidence=_payload(f, incomplete)), t.Reason.MISSING)
        self._unknown(_run(f, max_inventory_rows=0), t.Reason.BOUNDS)
        unknown = replace(f['fact'], value=t.Truth.UNKNOWN, reason=t.MissingReason.ABSENT)
        uncertain = replace(payload, rows=((f['target'], (unknown,)),))
        self._unknown(_run(f, evidence=_payload(f, uncertain)), t.Reason.MISSING)

    # @anchor:wa.tests.scripts.test_predicates.TestPredicates.test_length_delta_does_not_prove_goal
    def test_length_delta_does_not_prove_goal(self):
        f = _fixture(t.PredicateOperator.VALUE_LEN_DELTA, 2)
        self.assertEqual(_run(f), t.PredicateResult(t.Truth.TRUE, None))
        goal = _fixture(t.PredicateOperator.TASK_GOAL, 'exact-text-goal')
        # Even an otherwise valid causal delta is unrelated to the goal binding.
        self._unknown(_run(goal, evidence=f['args']['evidence']), t.Reason.MISSING)
        self._unknown(_run(goal, evidence=()), t.Reason.MISSING)
        false_goal = _payload(goal, replace(goal['payload'], facts=(replace(
            goal['fact'], value=t.Truth.FALSE),)))
        self.assertEqual(_run(goal, evidence=false_goal), t.PredicateResult(t.Truth.FALSE, None))
        self.assertEqual(_run(goal), t.PredicateResult(t.Truth.TRUE, None))

    # @anchor:wa.tests.scripts.test_predicates.TestPredicates.test_candidate_data_cannot_supply_evidence
    def test_candidate_data_cannot_supply_evidence(self):
        f = _fixture()
        callback = unittest.mock.Mock(side_effect=AssertionError('candidate invoked'))
        for raw in ({'enabled': True, 'target': f['target']},
                    (f['node'], f['fact']), callback):
            with patch.object(f['registry'], 'check', wraps=f['registry'].check) as check:
                self._unknown(_run(f, evidence=(raw,)), t.Reason.TYPE)
            check.assert_not_called()
        callback.assert_not_called()
        with patch.object(f['registry'], 'check', wraps=f['registry'].check) as check:
            self.assertEqual(_run(f), t.PredicateResult(t.Truth.TRUE, None))
        self.assertTrue(any(call.args[0] is f['binding'] for call in check.call_args_list))

    # @anchor:wa.tests.scripts.test_predicates.TestPredicates.test_complete_input_before_leaf_checks
    def test_complete_input_before_leaf_checks(self):
        f = _fixture()
        later = replace(f['node'])
        expression = _logical(f, t.PredicateOperator.ANY, (f['node'], later))
        callback = unittest.mock.Mock(side_effect=AssertionError('candidate invoked'))
        object.__setattr__(later, 'operator', callback)
        for node, changes in ((expression, {}),
                              (f['node'], {'evidence': (f['binding'], callback)}),
                              (f['node'], {'safety_vetoes': (
                                  t.PredicateResult(t.Truth.TRUE, None), callback)})):
            with patch.object(f['registry'], 'check', wraps=f['registry'].check) as check:
                self._unknown(_run(f, node, **changes), t.Reason.TYPE)
            check.assert_not_called()
        callback.assert_not_called()
        object.__setattr__(later, 'operator', t.PredicateOperator.STATE_FLAG)
        with patch.object(f['registry'], 'check', wraps=f['registry'].check) as check:
            self.assertEqual(_run(f, expression), t.PredicateResult(t.Truth.TRUE, None))
        self.assertTrue(any(call.args[0] is f['binding'] for call in check.call_args_list))
