# @module {"id":"wa.tests.scripts.test_routing_integration","layer":3,"owner":"LEARN","deps":["wa.learn.calibrate","wa.learn.cascade","wa.learn.cu_loop","wa.learn.cu_memory","wa.learn.script_integration","wa.learn.script_metrics","wa.learn.script_policy","wa.tests.cu_web_unit","wa.tests.scripts.test_context_generation","wa.tests.scripts.test_execution","wa.tests.scripts.test_ledger_trace","wa.tests.scripts.test_observation_privacy","wa.tests.scripts.test_store_catalogue","wa.tests.test_cascade","wa.tests.test_cu_loop_unit","wa.tools.anchors","wa.tools.build_code_index","wa.tools.reseal","wa.tools.route_code"],"book":"HB-WA-s3-remember"}
"""Synthetic public boundary controls; mutants run in RAM without disk edits."""
import dataclasses as _dc
import sys as _sys
import types as _types
import unittest as _unittest
from contextlib import nullcontext as _nullcontext
from fractions import Fraction as _F
from pathlib import Path as _Path
from unittest.mock import patch as _patch
from wender.learn import script_policy as _p, script_metrics as _m
from wender.learn import script_integration as _i, cascade as _c, calibrate as _cal
from tests import test_cascade as _cascade_tests
from tests.scripts import test_execution as _execution
from wender.learn import cu_loop as _loop, cu_memory as _memory
from tests import test_cu_loop_unit as _loop_tests
from tests.scripts import test_ledger_trace as _ledger_tests
from tests.scripts import test_store_catalogue as _store_tests
from tests.scripts import test_observation_privacy as _observation_tests
from tests import test_cu_web_unit as _web_tests
import io as _io
import ast as _ast
import textwrap as _textwrap
from tests.scripts import test_context_generation as _context_tests

_KEY = ('synthetic-backend', 'model', 'config', 'route', 'surface', 'window')


def _artifact():
    registration = object()
    artifact = _p.CalibrationArtifact('artifact', 'ironbcc/seb-9b', 'tokenizer',
        (('grammar_mask', ('A', 'B', 'C')), ('logprobs_mode', 'processed_logprobs')), 'held-out-split', ('sample-1', 'sample-2'), ('fit-1',),
        (('t_star_num', 1000), ('t_star_den', 1000), ('q_hat_promille', 200)),
        (('error', _F(1, 100)), ('coverage', _F(9, 10)), ('tau_default_promille', 800),
         ('tau_destructive_promille', 950), ('margin_promille', 150)), 'policy', 'environment', registration)
    owner = _types.SimpleNamespace(check=lambda value, **kwargs: value is artifact)
    return dict(artifact=artifact, owner=owner, current=(artifact.model, artifact.tokenizer,
        artifact.serving, artifact.split, artifact.policy_revision, artifact.environment_revision),
        min_samples=2, max_error=_F(1, 20), min_coverage=_F(4, 5))


def _tier_for(context):
    a = context['artifact']
    return _dc.replace(_cascade_tests._tier(model=a.model),
        serving_gates=dict(a.serving) | dict(model_sha=a.model, tokenizer_sha=a.tokenizer))


def _check_controls(case, names):
    for name in names:
        result = _unittest.TestResult()
        case(name).run(result)
        if result.errors:
            raise RuntimeError(result.errors)
        if result.failures or result.skipped:
            raise AssertionError(result.failures or result.skipped)


def _selection_fixture():
    h = _store_tests._Host()
    plan = h._plan()
    cell = h._admit(plan=plan, auxiliary=(('META:fts', (plan.identity.logical_id,)),))
    h._publish()
    session = _session(scope=h.scope, authority=h.authority,
        restart=_i.RestartState(True, h.scope, (), object(), None))
    evidence = _i.IntegrationEvidence(h.scope, h.authority, h.f['observation'], None,
        'inventory', True, True, 'binding', (), 'allocation')
    owners = _owners(session)
    owners.store = h.store
    owners.current_authority = lambda scope: h.authority
    owners.check_evidence = lambda value: value is evidence
    owners.check_budget = lambda *args: True
    return h, cell, session, evidence, owners


def _model(costs=(_F(2), _F(10)), probabilities=(_F(1, 2),), **changes):
    return _dc.replace(_p.CostModel(('currency', 'USD', 'window'), 'conditional', costs,
                                   probabilities, 10, 'synthetic-counter', None), **changes)


def _governor(**changes):
    policy = _p.AdmissionPolicy(100, 10, 4, 4, 40, 5, 2, 100,
                               (('queue', 5), ('send', 5)), 1000, 10, 5)
    proof = object()
    return _p.AdmissionGovernor(_dc.replace(policy, **changes), clock=lambda: 1,
        telemetry=lambda: 0, drain_owner=_types.SimpleNamespace(check=lambda owner, value: value is proof)), proof


def _session(**changes):
    restart = _i.RestartState(True, 'scope', (), object(), None)
    return _dc.replace(_i.IntegrationSession('episode', 'scope', 'authority', 1000, 0,
        restart, None, None, (), (), object(), ()), **changes)


def _owners(session):
    return _types.SimpleNamespace(check_session=lambda value: value is session,
        check_restart=lambda value: True, stopped=lambda: False,
        current_authority=lambda scope: 'authority', clock=lambda: 1)


# @anchor:wa.tests.scripts.test_routing_integration.TestRouting
class TestRouting(_unittest.TestCase):
    # @anchor:wa.tests.scripts.test_routing_integration.TestRouting.test_calibration_provenance
    def test_calibration_provenance(self):
        context = _artifact()
        self.assertTrue(_p.validate_calibration_artifact(**context))
        a = context['artifact']
        label_only = _types.SimpleNamespace(**{f.name: getattr(a, f.name) for f in _dc.fields(a)}, status='CALIBRATED')
        self.assertFalse(_p.validate_calibration_artifact(**(context | {'artifact': label_only,
            'owner': _types.SimpleNamespace(check=lambda *a, **kw: True)})))
        for delta in ({'split': ''}, {'sample_refs': ()}, {'fitting_refs': ('sample-1',)},
                      {'tokenizer': 'changed'}, {'serving': ()}, {'registration': None},
                      {'sample_refs': ('sample-1',)}, {'sample_refs': ('sample-1', 'sample-1')}):
            with self.subTest(fixture=delta):
                altered = _dc.replace(a, **delta)
                registered = _types.SimpleNamespace(check=lambda value, **kw: value is altered)
                self.assertFalse(_p.validate_calibration_artifact(**(context | {'artifact': altered, 'owner': registered})))
        empty_split = _dc.replace(a, split='')
        self.assertFalse(_p.validate_calibration_artifact(**(context | {'artifact': empty_split,
            'owner': _types.SimpleNamespace(check=lambda *a, **kw: True),
            'current': (a.model, a.tokenizer, a.serving, '', a.policy_revision, a.environment_revision)})))
        self.assertFalse(_p.validate_calibration_artifact(**(context | {'artifact': {'status': 'CALIBRATED'}})))
        self.assertFalse(_p.validate_calibration_artifact(**(context | {'artifact': _dc.replace(a)})))
        receipt = _cascade_tests._tier()
        distribution = _cal.verify_distribution(receipt.mapping, receipt.logprobs_content, receipt.generated_key)
        self.assertEqual(_cal.calibrate_decision(distribution, automatic=True).verdict, _cal.ABSTAIN)
        self.assertEqual(_cal.calibrate_decision(distribution, automatic=True,
                         artifact_context=context).verdict, _cal.CALIBRATED)
        for q in (0, 1000):
            bad_set = _dc.replace(a, parameters=(('t_star_num', 1000), ('t_star_den', 1000), ('q_hat_promille', q)))
            set_context = context | {'artifact': bad_set, 'owner': _types.SimpleNamespace(check=lambda *a, **k: True)}
            self.assertEqual(_cal.calibrate_decision(distribution, automatic=True,
                             artifact_context=set_context).failure_code, 'NON_SINGLETON_CONFORMAL_SET')

    # @anchor:wa.tests.scripts.test_routing_integration.TestRouting.test_immutable_records
    def test_immutable_records(self):
        artifact = _artifact()['artifact']
        with self.assertRaises(_dc.FrozenInstanceError):
            artifact.split = 'changed'
        with self.assertRaises(TypeError):
            _dc.replace(artifact, sample_refs=(['mutable-canary'],))
        with self.assertRaises(TypeError):
            _dc.replace(_session(), consumed_actions=(['mutable'],))
        with self.assertRaises(TypeError):
            _m.CostEvidence((['mutable'],), 'ns', 1, 1, 'clock', None)

    # @anchor:wa.tests.scripts.test_routing_integration.TestRouting.test_cost
    def test_cost(self):
        self.assertEqual(_p.expected_cost(_model()), _F(7))
        unconditional = _model(probabilities=(_F(1), _F(1, 2)), convention='unconditional')
        self.assertEqual(_p.expected_cost(unconditional), _F(7))
        self.assertIsNone(_p.expected_cost(_model(costs=(_F(2), None), missing_reason='UNMEASURED')))
        with self.assertRaises(ValueError):
            _p.expected_cost(_model(costs=(_F(-1), _F(2))))
        with self.assertRaises(ValueError):
            _p.expected_cost(_model(probabilities=(_F(2),)))
        routes = (('unsafe', _model(costs=(_F(0), _F(0))), 'paired'),
                  ('safe', _model(), 'paired'),
                  ('unknown', _model(costs=(None,), probabilities=(), missing_reason='UNMEASURED'), 'paired'))
        self.assertEqual(_m.compare_routes(routes, eligible={'safe', 'unknown'}, pair_key='paired'), (('safe', _F(7)),))
        with self.assertRaises(ValueError):
            _m.compare_routes((('a', _model(), 'paired'), ('b', _model(metric=('latency', 'ns')), 'paired')),
                              eligible={'a', 'b'}, pair_key='paired')
        with self.assertRaises(ValueError):
            _m.compare_routes((('safe', _model(), 'other'),), eligible={'safe'}, pair_key='paired')

    # @anchor:wa.tests.scripts.test_routing_integration.TestRouting.test_metrics
    def test_metrics(self):
        row = _m.ProviderUsage(_KEY, True, True, True, 100, 25, 'provider-counter', None)
        script = _m.ProviderUsage(_KEY, False, None, None, None, None, None, 'NO_MODEL_CALL')
        result = _m.cache_metrics((row, script), key=_KEY, max_rows=2)
        self.assertEqual(result['provider_calls'], 1)
        self.assertEqual(result['provider_cached_fraction'], _F(1, 4))
        no_usage = _dc.replace(row, input_tokens=None, cached_input_tokens=None, reason='USAGE_MISSING')
        self.assertIsNone(_m.cache_metrics((no_usage,), key=_KEY, max_rows=1)['provider_cached_fraction'])
        equal = _dc.replace(row, cached_input_tokens=0)
        self.assertEqual(_m.cache_metrics((equal,), key=_KEY, max_rows=1)['provider_cached_fraction'], 0)
        missing_prefix = _m.cache_metrics((_dc.replace(row, prefix_equal=None),), key=_KEY, max_rows=1)
        self.assertIsNone(missing_prefix['prefix_equal_calls'])
        self.assertEqual(missing_prefix['prefix_reason'], 'PREFIX_MISSING')
        for bad in (_dc.replace(row, cached_input_tokens=101), _dc.replace(script, input_tokens=1)):
            with self.assertRaises(ValueError):
                _m.cache_metrics((bad,), key=_KEY, max_rows=1)
        metric = _m.CostEvidence(_KEY, 'USD', None, 1, None, 'UNMEASURED')
        self.assertIsNone(metric.value())
        self.assertEqual(_dc.replace(metric, numerator=3, denominator=2, provenance='counter', reason=None).value(), _F(3, 2))
        with self.assertRaises(ValueError):
            _dc.replace(metric, numerator=-1, provenance='counter').value()

    # @anchor:wa.tests.scripts.test_routing_integration.TestRouting.test_latency
    def test_latency(self):
        row = _m.RequestTiming(_KEY, 10, 30, 40, 50, 60, 70, 90, 80, False, 'clock', None)
        measured = _m.latency_metrics((row,), key=_KEY, max_rows=1)
        self.assertEqual(measured['mean_arrival_to_seal_ns'], 80)
        self.assertEqual(measured['deadline_misses'], 1)
        for bad in (_dc.replace(row, queue_end_ns=9), _dc.replace(row, arrival_ns=-1)):
            with self.assertRaises(ValueError):
                _m.latency_metrics((bad,), key=_KEY, max_rows=1)
        censored = _dc.replace(row, seal_ns=None, censored=True, reason='CANCELLED')
        result = _m.latency_metrics((row, censored), key=_KEY, max_rows=2)
        self.assertEqual(result['deadline_denominator'], 2)
        self.assertEqual(result['censored'], 1)
        self.assertIsNone(result['mean_arrival_to_seal_ns'])
        for name, timing in (('cold', (0, 1, 10, 11, 12, 13, 14)),
                             ('warm', (0, 1, 2, 3, 4, 5, 6)),
                             ('saturated', (0, 20, 21, 22, 23, 24, 25)),
                             ('recovery', (0, 1, 2, 3, 4, 5, None))):
            with self.subTest(profile=name):
                sample = _m.RequestTiming(_KEY, *timing, 20, name == 'recovery',
                    'synthetic-clock', 'CANCELLED' if name == 'recovery' else None)
                metrics = _m.latency_metrics((sample,), key=_KEY, max_rows=1)
                self.assertEqual(metrics['requests'], 1)
                self.assertEqual(metrics['deadline_denominator'], 1)
                self.assertEqual(metrics['deadline_misses'], int(name == 'saturated'))
                if name == 'recovery':
                    self.assertIsNone(metrics['mean_arrival_to_seal_ns'])
                else:
                    self.assertEqual(metrics['mean_arrival_to_seal_ns'], timing[-1] - timing[0])
        _check_controls(_execution.TestRuntime, ('test_fence_revision_restart_deadline',))

    # @anchor:wa.tests.scripts.test_routing_integration.TestRouting.test_drift
    def test_drift(self):
        expected = {k: 'registered-' + k for k in ('selector', 'ownership', 'observation',
            'publication', 'generation', 'geometry', 'facts', 'lease', 'scope', 'authority')}
        owner = _types.SimpleNamespace(check=lambda value: True,
                                      checkpoint=lambda proof, old, new: proof == 'checkpoint')
        self.assertTrue(_p.guard_drift(expected, dict(expected), owner=owner))
        for key in expected:
            for value in ('changed', None):
                with self.subTest(field=key, value=value):
                    self.assertFalse(_p.guard_drift(expected, expected | {key: value}, owner=owner))
        moved = expected | {'geometry': 'moved'}
        self.assertFalse(_p.guard_drift(expected, moved, owner=owner, planned_transition=True))
        self.assertTrue(_p.guard_drift(expected, moved, owner=owner,
                                      checkpoint='checkpoint', planned_transition=True))

    # @anchor:wa.tests.scripts.test_routing_integration.TestRouting.test_eligibility_determinism
    def test_eligibility_determinism(self):
        deterministic = _p.RouteEligibility('SCRIPT', True, None, 's', 'a', 'o', ('lifecycle',))
        inference = _dc.replace(deterministic, lane='TEXT')
        owner = _types.SimpleNamespace(check=lambda route, inference: not inference)
        for rows in ((deterministic, inference), (inference, deterministic)):
            self.assertEqual(_p.eligible_routes(rows, scope='s', authority='a', observation='o', owner=owner), (deterministic,))
        for row in (_dc.replace(deterministic, eligible=None), _dc.replace(deterministic, scope='other'),
                    _dc.replace(deterministic, evidence=())):
            self.assertEqual(_p.eligible_routes((row,), scope='s', authority='a', observation='o', owner=owner), ())
        with self.assertRaises(ValueError):
            _p.eligible_routes((deterministic, _dc.replace(deterministic, eligible=False)),
                               scope='s', authority='a', observation='o', owner=owner)

    # @anchor:wa.tests.scripts.test_routing_integration.TestRouting.test_aggregate_and_holder
    def test_aggregate_and_holder(self):
        _check_controls(_store_tests.StoreControls, ('test_g_aggregate_preallocation',
            'test_g_aggregate_process_consumer_and_missing_telemetry'))
        g, proof = _governor()
        first, second = object(), object()
        with self.assertRaises(ValueError):
            g.reserve(first, peak_bytes=None, queued_bytes=1, deadline_ns=500)
        with self.assertRaises(ValueError):
            g.reserve(first, peak_bytes=-1, queued_bytes=1, deadline_ns=500)
        token = g.reserve(first, peak_bytes=60, queued_bytes=1, deadline_ns=500)
        with self.assertRaises(ValueError):
            g.reserve(second, peak_bytes=31, queued_bytes=1, deadline_ns=500)
        with self.assertRaises(ValueError):
            g.grow(first, token, extra_bytes=31)
        self.assertEqual(g.outstanding(), ((first, 60),))
        with self.assertRaises(ValueError):
            g.release(first, token, proof=object())
        self.assertEqual(g.outstanding(), ((first, 60),))
        g.grow(first, token, extra_bytes=30)
        with self.assertRaises(ValueError):
            g.reserve(first, peak_bytes=1, queued_bytes=0, deadline_ns=500)
        g.release(first, token, proof=proof)
        self.assertEqual(g.outstanding(), ())
        g._telemetry = lambda: None
        with self.assertRaises(ValueError):
            g.reserve(second, peak_bytes=1, queued_bytes=0, deadline_ns=500)
        for key in ('aggregate_bytes', 'safety_reserve_bytes', 'concurrency', 'cancellation_ns', 'safety_response_ns'):
            with self.assertRaises(ValueError):
                _governor(**{key: None})

    # @anchor:wa.tests.scripts.test_routing_integration.TestRouting.test_admission_flood
    def test_admission_flood(self):
        _check_controls(_store_tests.StoreControls, ('test_g_admission_flood',))
        g, proof = _governor()
        g.charge('same')
        g.charge('same')
        with self.assertRaises(ValueError):
            g.charge('same')
        g.charge('new-principal-1')
        g.charge('new-principal-2')
        with self.assertRaises(ValueError):
            g.charge('new-episode-new-principal')
        # Work admitted before the flood can still drain under independent proof.
        owner = object()
        token = g.reserve(owner, peak_bytes=10, queued_bytes=0, deadline_ns=500)
        g.release(owner, token, proof=proof)
        self.assertEqual(g.outstanding(), ())

    # @anchor:wa.tests.scripts.test_routing_integration.TestRouting.test_endurance
    def test_endurance(self):
        g, proof = _governor()
        for cycle in range(100):
            owner = object()
            token = g.reserve(owner, peak_bytes=90, queued_bytes=1, deadline_ns=500)
            if cycle % 3 == 0:
                with self.assertRaises(ValueError):
                    g.release(owner, token, proof=None)
                self.assertEqual(g.outstanding(), ((owner, 90),))
            g.release(owner, token, proof=proof)
            self.assertEqual(g.outstanding(), ())
            f = _context_tests._Fixture(); epoch = f.open()
            packed = f.pack(epoch).packed
            self.assertIsNotNone(packed)
            if cycle % 3 == 0:
                f.host.fail_cancel = True
                with self.assertRaises(_execution._t.ContractError):
                    _context_tests._r.close_epoch(epoch, host=f.host, reason=_execution._t.Reason.CANCELLED)
                self.assertTrue(f.resources.live)
                f.host.fail_cancel = False
            elif cycle % 3 == 1:
                # Ordinary catalogue replacement leaves the exact live epoch pinned.
                f.sh._admit(2)
                f.sh._publish()
            _context_tests._r.close_epoch(epoch, host=f.host, reason=_execution._t.Reason.CANCELLED)
            self.assertEqual(f.resources.live, {})
            self.assertEqual(f.host.requests, {})
            self.assertFalse(f.sh.store._snapshot_pin_tokens)
            self.assertTrue(all(count == 0 for count in f.sh.store._snapshot_pins.values()))

    # @anchor:wa.tests.scripts.test_routing_integration.TestRouting.test_restart_and_authority
    def test_restart_and_authority(self):
        session = _session()
        evidence = _i.IntegrationEvidence('scope', 'authority', 'obs', None, 'inventory', True, True,
                                          'binding', (), 'allocation')
        owners = _owners(session)
        owners.check_evidence = lambda value: value is evidence
        owners.check_budget = lambda *args: True
        # Prove the positive path reaches request validation, rather than a blanket refusal.
        with self.assertRaisesRegex(ValueError, 'REQUEST_SCOPE'):
            _i.select_lane(session, evidence, request=None, owners=owners, policy=None)
        for restart in (_dc.replace(session.restart, open=False),
                        _dc.replace(session.restart, unresolved=('admitted-before-crash',)),
                        _dc.replace(session.restart, reconciliation=None)):
            closed = _dc.replace(session, restart=restart)
            closed_owners = _owners(closed)
            closed_owners.check_evidence = lambda value: value is evidence
            closed_owners.check_budget = lambda *args: True
            with self.assertRaisesRegex(ValueError, 'RESTART_UNCERTAIN'):
                _i.select_lane(closed, evidence, request=None, owners=closed_owners, policy=None)
        owners.current_authority = lambda scope: 'revoked'
        with self.assertRaisesRegex(ValueError, 'STALE_AUTHORITY'):
            _i.select_lane(session, evidence, request=None, owners=owners, policy=None)
        owners = _owners(session)
        owners.check_reconciliation = lambda *args: False
        with self.assertRaisesRegex(ValueError, 'RECONCILIATION'):
            _i.reconcile_uncertain(session, evidence=object(), owners=owners)
        renewed = _dc.replace(session, model_budget=object())
        owners.check_reconciliation = lambda *args: True
        owners.renew_session = lambda *args: renewed
        owners.check_session = lambda value: value is session or value is renewed
        owners.check_fresh_grant = lambda *args: True
        self.assertIs(_i.reconcile_uncertain(session, evidence=object(), owners=owners), renewed)

    # @anchor:wa.tests.scripts.test_routing_integration.TestRouting.test_cascade_blockers_and_calibration
    def test_cascade_blockers_and_calibration(self):
        table = _cascade_tests._table()
        step = dict(intent='go', action=dict(control='go'))
        controls = dict(go=dict(role='button', canon_name='Go'))
        self.assertEqual(_c.intent_to_action('go', step, controls, table)['target_id'], 'e0')
        del table['blocked_control_present']
        self.assertIsNone(_c.intent_to_action('go', step, controls, table))
        self.assertEqual(_c.CascadePolicy().decide('go', step, controls, table).action, 'abstain')
        calls = []
        integration = _types.SimpleNamespace(check_selection=lambda table: True, select_script=lambda table: None)
        muse = _types.SimpleNamespace(choose=lambda *a, **kw: calls.append('MUSE') or 'e0')
        policy = _c.CascadePolicy(automatic=True, integration=integration, muse=muse)
        self.assertEqual(policy.decide('go', {}, {}, _cascade_tests._table()).action, 'abstain')
        self.assertEqual(calls, [])
        context = _artifact()
        policy = _c.CascadePolicy(automatic=True, integration=integration, calibration_context=context)
        result = policy.decide('go', {}, {}, _cascade_tests._table(), tier1=_tier_for(context))
        self.assertEqual((result.tier, result.source, result.target_id), (1, 'text', 'e0'))
        integration.select_script = lambda table: 'registered-artifact-ref'
        result = policy.decide('go', {}, {}, _cascade_tests._table())
        self.assertEqual((result.tier, result.source), (0, 'script'))
        self.assertEqual(calls, [])

    # @anchor:wa.tests.scripts.test_routing_integration.TestRouting.test_real_runner_registration
    def test_real_runner_registration(self):
        original = _execution._r.start
        calls = []
        def through_integration(plan, **kwargs):
            runtime = kwargs['runtime']
            goal_evaluate = runtime.goal_owner.evaluate
            evaluations = []
            def goal_after_query(*args, **kw):
                evaluations.append(1)
                runtime.goal_truth = (_execution._t.Truth.TRUE if len(evaluations) > 1
                                      else _execution._t.Truth.FALSE)
                return goal_evaluate(*args, **kw)
            runtime.goal_owner.evaluate = goal_after_query
            custody = _i.ExecutionCustody(plan, kwargs['session'], kwargs['admission'],
                kwargs['inputs'], kwargs['registry'], kwargs['lease'], runtime, object())
            session = _session(scope=runtime.scope, authority=runtime.authority, custody=custody,
                deadline_ns=kwargs['lease'].absolute_deadline_ns,
                restart=_i.RestartState(True, runtime.scope, (), runtime.restart_evidence, None))
            owners = _owners(session)
            owners.current_authority = lambda scope: runtime.authority
            owners.check_custody = lambda s, value: value is custody
            owners.execution_guard = lambda s: _nullcontext()
            states = []
            owners.runner_state = lambda s: states[0] if states else None
            owners.retain_runner = lambda s, state: states.append(state)
            owners.check_terminal = lambda s, outcome: outcome.owner_drained.value is _execution._t.Truth.TRUE
            owners.finish_integration = lambda *args: calls.append('cleanup')
            runner = _types.SimpleNamespace(start=original, advance=_execution._r.advance)
            with _patch.object(_i, '_runner', runner):
                coordinator = _types.SimpleNamespace(advance_session=lambda s: _i.advance_session(s, owners=owners),
                    check_ready=lambda: True, runner_state=owners.runner_state)
                coordinator.select_script = lambda *args: session
                coordinator.accept_script = lambda s, state: (
                    state.outcome.status is _execution._t.TerminalStatus.COMPLETED)
                if lane == 'loop':
                    fixture = _loop_tests.TestCuLoop(); fixture.setUp()
                    self.addCleanup(fixture.doCleanups)
                    cap = fixture.capsule(['a'])
                    result = _loop.run_loop(cap, _loop_tests.Describer(), _loop.ReplayDecider([]),
                        goal='synthetic', trace=_io.StringIO(), integration=coordinator,
                        clock=fixture.clock, sleep=fixture.clock.sleep)
                    self.assertEqual(result['stop_reason'], 'script_completed')
                    self.assertTrue(result['goal_verified'])
                    self.assertEqual(cap.inputs, [])
                else:
                    surface, planner = _web_tests.FakeSurface(), _web_tests.FakePlanner()
                    result = _loop.run_web_cycle(surface, planner, _web_tests.FakeExecutor(),
                        {'workflow': {'steps': []}}, task='synthetic', trace=_io.StringIO(), integration=coordinator)
                    self.assertTrue(result['goal_verified'], result)
                    self.assertEqual((planner.plans, surface.calls), (0, []))
                state = states[0]
                self.assertGreater(state.cursor, 0)
                self.assertIs(_i.advance_session(session, owners=owners), state)
            self.assertEqual(len(states), 1)
            return state
        compiler_host = _execution._CompilerHost
        def query_host(body):
            return compiler_host(b'def run(act, params):\n    button = act.find("click")\n    return None\n')
        for lane in ('loop', 'web'):
            with self.subTest(lane=lane):
                with _patch.object(_execution, '_CompilerHost', query_host), _patch.object(_execution._r, 'start', through_integration):
                    _execution.TestRuntime('test_start_registered_pairing').test_start_registered_pairing()
        self.assertTrue(calls)

    # @anchor:wa.tests.scripts.test_routing_integration.TestRouting.test_real_action_fence
    def test_real_action_fence(self):
        # Real ActionPolicy and interlock, not a success-returning transport mock.
        h = _execution._Harness()
        self.assertEqual(h.send()['status'], 'admitted')
        self.assertEqual(h.fixture.actuator.opened, 1)
        h = _execution._Harness()
        h.interlock.invalidate(lambda: None)
        with self.assertRaises(_execution._p.PolicyRefused):
            h.send()
        self.assertEqual(h.fixture.actuator.opened, 0)
        h = _execution._Harness()
        inputs = []
        def uncertain(*args):
            inputs.append(1)
            raise RuntimeError('private-canary')
        with self.assertRaises(_execution._p.PolicyRefused):
            h.send(uncertain)
        self.assertEqual(h.send(uncertain)['status'], 'uncertain')
        self.assertEqual(inputs, [1])

    # @anchor:wa.tests.scripts.test_routing_integration.TestRouting.test_live_dispatch_registration
    def test_live_dispatch_registration(self):
        action = {'kind': 'click', 'x': 1, 'y': 2}
        with _patch.object(_loop, '_json_command', return_value={'input_mono_ns': 1}) as transport:
            with self.assertRaisesRegex(ValueError, 'DISPATCH_OWNER_UNAVAILABLE'):
                _loop.SrfCapsule('synthetic').input(action)
            self.assertEqual(transport.call_count, 0)
            dispatcher = _loop_tests._TrustedDispatcher()
            self.assertEqual(_loop.SrfCapsule('synthetic', dispatcher=dispatcher).input(action), {'input_mono_ns': 1})
            self.assertEqual(transport.call_count, 1)

    # @anchor:wa.tests.scripts.test_routing_integration.TestRouting.test_loop_semantics_and_budget
    def test_loop_semantics_and_budget(self):
        fixture = _loop_tests.TestCuLoop()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        events = []
        owner = _types.SimpleNamespace(check_ready=lambda: True, check_script_miss=lambda: True,
            select_script=lambda *args: None,
            charge_model=lambda: True, possible_input=lambda action: events.append('possible'),
            dispatch=lambda surface, action, send: events.append('send') or send(),
            verify=lambda *args: False, uncertain=lambda action: events.append('uncertain'))
        cap = fixture.capsule(['a', 'b', 'c'])
        result = _loop.run_loop(cap, _loop_tests.Describer(),
            _loop.ReplayDecider([_loop_tests.proposal(), _loop_tests.proposal()]),
            goal='test', trace=_io.StringIO(), integration=owner,
            clock=fixture.clock, sleep=fixture.clock.sleep)
        self.assertEqual(result['stop_reason'], 'reconciliation_required')
        self.assertEqual(result['verified_ok'], 0)
        self.assertEqual(len(cap.inputs), 1)
        self.assertEqual(events, ['possible', 'send', 'uncertain'])
        events.clear()
        owner.charge_model = lambda: False
        cap = fixture.capsule(['a', 'b'])
        result = _loop.run_loop(cap, _loop_tests.Describer(),
            _loop.ReplayDecider([_loop_tests.proposal()]), goal='test', trace=_io.StringIO(),
            integration=owner, clock=fixture.clock, sleep=fixture.clock.sleep)
        self.assertEqual(cap.inputs, [])
        self.assertEqual(events, [])
        self.assertEqual(result['failure']['stage'], 'decision')
        owner.charge_model, owner.verify = lambda: True, lambda *args: True
        cap = fixture.capsule(['a', 'b'])
        result = _loop.run_loop(cap, _loop_tests.Describer(),
            _loop.ReplayDecider([_loop_tests.proposal()]), max_steps=1, goal='test',
            trace=_io.StringIO(), integration=owner, clock=fixture.clock, sleep=fixture.clock.sleep)
        self.assertEqual(result['verified_ok'], 1)
        self.assertEqual(len(cap.inputs), 1)
        self.assertEqual(events, ['possible', 'send'])

    # @anchor:wa.tests.scripts.test_routing_integration.TestRouting.test_script_receipt_adapter
    def test_script_receipt_adapter(self):
        fixture = _ledger_tests.LedgerTraceTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        token = fixture._reserve()
        event = fixture._append(token, _ledger_tests.T.EventState.ADMITTED)
        destination = next(iter(_ledger_tests.T.ExportDestination))
        purpose = next(iter(_ledger_tests.T.Purpose))
        args = dict(destination=destination, purpose=purpose,
                    approve=_ledger_tests._yes, tokenizer=lambda text: 30)
        result = _memory.project_script_receipt(fixture.owner, event, **args)
        self.assertIs(type(result), bytes)
        self.assertIn(b'event_ref', result)
        for changes in ({'approve': _ledger_tests._no}, {'tokenizer': lambda text: 121}):
            with self.assertRaises(_ledger_tests.T.ContractError):
                _memory.project_script_receipt(fixture.owner, event, **(args | changes))
        with self.assertRaises(_ledger_tests.T.ContractError):
            _memory.project_script_receipt(fixture.owner, _dc.replace(event), **args)

    # @anchor:wa.tests.scripts.test_routing_integration.TestRouting.test_real_store_selection
    def test_real_store_selection(self):
        h = _store_tests._Host()
        plan = h._plan()
        cell = h._admit(plan=plan, auxiliary=(('META:fts', (plan.identity.logical_id,)),))
        h._publish()
        session = _session(scope=h.scope, authority=h.authority,
            restart=_i.RestartState(True, h.scope, (), object(), None))
        evidence = _i.IntegrationEvidence(h.scope, h.authority, h.f['observation'], None,
            'complete-inventory', True, True, 'binding', (), 'allocation')
        owners = _owners(session)
        owners.store = h.store
        owners.current_authority = lambda scope: h.authority
        owners.check_evidence = lambda value: value is evidence
        owners.check_budget = lambda *args: True
        owners.execution_ttl = lambda *args: 100
        owners.consumer_identity = lambda s: 'integration-consumer'
        custody_rows = []
        def custody(s, candidate, lease):
            runtime = _types.SimpleNamespace(scope=s.scope, authority=s.authority, episode_id=s.episode,
                                             deadline_ns=s.deadline_ns, observation=evidence.observation)
            row = _i.ExecutionCustody(plan, object(), object(), object(), object(), lease, runtime, object())
            custody_rows.append(row)
            return row
        owners.execution_custody = custody
        owners.check_custody = lambda s, row: any(row is r for r in custody_rows)
        owners.abandon_lease = lambda s, lease: h.store.release(lease)
        selection = _i.select_lane(session, evidence, request=h._request(cell), owners=owners, policy=None)
        self.assertEqual((selection.lane, selection.tier), ('SCRIPT', 0))
        self.assertIs(selection.custody.plan, plan)
        h.store.release(selection.custody.lease)
        for changes in ({'blockers_clear': None}, {'inventory': None}, {'binding': None}):
            altered = _dc.replace(evidence, **changes)
            owners.check_evidence = lambda value: value is altered
            with self.assertRaisesRegex(ValueError, 'SAFETY_EVIDENCE'):
                _i.select_lane(session, altered, request=h._request(cell), owners=owners, policy=None)
        owners.check_evidence = lambda value: value is evidence
        owners.check_custody = lambda *args: False
        with self.assertRaisesRegex(ValueError, 'EXECUTION_CUSTODY'):
            _i.select_lane(session, evidence, request=h._request(cell), owners=owners, policy=None)

    # @anchor:wa.tests.scripts.test_routing_integration.TestRouting.test_observation_adapter
    def test_observation_adapter(self):
        f = _store_tests._Host().f
        scope, authority, observation = f['scope'], f['authority'], f['observation']
        pscope = _observation_tests._scope(principal_id=scope.principal, tenant_id=scope.tenant,
            profile_incarnation=scope.browser_profile_incarnation, top_level_origin=scope.top_level_origin,
            target_frame_origin=scope.target_frame_origin, frame_ancestry=scope.frame_ancestry,
            scope_revision=scope.scope_revision)
        allocation, acquisition = object(), object()
        args = dict(observation_id='obs', publication_id=observation.publication_identity,
            scope_identity=pscope, raw_elements=(_observation_tests._raw(),), producer_gen16=7,
            srf_64=123, turn_index=0, scope_complete=True, stream_truncated=False, turns=(),
            join_result=None, ax_matches={1: {'level': 'L0_EXACT'}}, transform_meta=_observation_tests._meta(),
            policy_revision=1, budget_port=_observation_tests._Budget())
        releases, registered = [], []
        def register(a, bundle, s, revision, reserved):
            evidence = _i.IntegrationEvidence(s, revision, observation, bundle, object(), True,
                                              True, object(), (), reserved)
            registered.append(evidence)
            return evidence
        host = _types.SimpleNamespace(reserve_observation=lambda *args: allocation,
            check_acquisition=lambda a, *args: a is acquisition,
            observation_arguments=lambda *unused: args, register_observation=register,
            check_evidence=lambda value: any(value is row for row in registered),
            release_observation=lambda reserved: releases.append(reserved))
        evidence = _i.observe(host=host, acquisition=acquisition, scope=scope,
                              authority=authority, policy=object(), resources=object())
        self.assertIs(evidence.allocation, allocation)
        self.assertEqual(releases, [])
        bad_scope = _dc.replace(scope, attestation=None, attestation_reason=_execution._t.Reason.MISSING)
        with self.assertRaisesRegex(ValueError, 'SCOPE_ATTESTATION'):
            _i.observe(host=host, acquisition=acquisition, scope=bad_scope,
                       authority=authority, policy=object(), resources=object())
        self.assertEqual(releases, [allocation])
        args['scope_identity'] = _dc.replace(pscope, principal_id='another-principal')
        with self.assertRaisesRegex(ValueError, 'SCOPE_MAPPING'):
            _i.observe(host=host, acquisition=acquisition, scope=scope,
                       authority=authority, policy=object(), resources=object())
        def private_exception(*args):
            raise RuntimeError('NESTED_PRIVATE_CANARY')
        host.check_acquisition = private_exception
        with self.assertRaisesRegex(ValueError, 'INTEGRATION_UNAVAILABLE') as raised:
            _i.observe(host=host, acquisition=acquisition, scope=scope,
                       authority=authority, policy=object(), resources=object())
        self.assertNotIn('CANARY', str(raised.exception))
        self.assertIsNone(raised.exception.__cause__)
        self.assertIsNone(raised.exception.__context__)

    # @anchor:wa.tests.scripts.test_routing_integration.TestRouting.test_web_dispatch_and_no_retry
    def test_web_dispatch_and_no_retry(self):
        fixture = _web_tests.WebUnit()
        fixture.setUp()
        surface = _web_tests.FakeSurface()
        surface.integration = None
        result = _loop.run_web_cycle(surface, _web_tests.FakePlanner(), _web_tests.FakeExecutor(),
            fixture.memory, task='synthetic', trace=_io.StringIO())
        self.assertFalse(result['goal_verified'])
        self.assertEqual(surface.calls, [])
        self.assertTrue(surface.stopped)
        surface = _web_tests.FakeSurface(stuck=True)
        clock = _web_tests.Clock()
        result = _loop.run_web_cycle(surface, _web_tests.FakePlanner(), _web_tests.FakeExecutor(),
            fixture.memory, task='synthetic', trace=_io.StringIO(), clock=clock,
            sleep=clock.sleep, postcheck_seconds=.1, wait_seconds=.2)
        self.assertFalse(result['goal_verified'])
        self.assertEqual([action['text'] for action in surface.calls if action['kind'] == 'type'],
                         ['https://www.perplexity.ai/', 'synthetic'])
        self.assertEqual(result['failure']['detail'], 'RECONCILIATION_REQUIRED')
        self.assertTrue(surface.integration.closed)
        before = len(surface.calls)
        with self.assertRaises(_web_tests.BackendError):
            surface.integration.dispatch(surface, {'kind': 'click'}, lambda: surface.calls.append('repeated'))
        self.assertEqual(len(surface.calls), before)
        surface = _web_tests.FakeSurface()
        result = _loop.run_web_cycle(surface, _web_tests.FakePlanner(), _web_tests.FakeExecutor(),
            fixture.memory, task='synthetic', trace=_io.StringIO(), clock=clock, sleep=clock.sleep)
        self.assertTrue(result['goal_verified'])

    # @anchor:wa.tests.scripts.test_routing_integration.TestRouting.test_automatic_receipt_identity
    def test_automatic_receipt_identity(self):
        for model in ('Qwen3-VL-2B', 'Laya-30B-A3B', 'new-owner-calibrated-model'):
            context = _artifact()
            a = _dc.replace(context['artifact'], model=model)
            context = context | {'artifact': a, 'owner': _types.SimpleNamespace(check=lambda *a, **kw: True),
                'current': (a.model, a.tokenizer, a.serving, a.split, a.policy_revision, a.environment_revision)}
            policy = _c.CascadePolicy(automatic=True, calibration_context=context,
                integration=_types.SimpleNamespace(check_selection=lambda *a: True, select_script=lambda *a: None))
            table = _cascade_tests._table()
            receipt = _tier_for(context)
            good = policy.decide('go', {}, {}, table, tier1=receipt)
            self.assertEqual((good.target_id, good.tier), ('e0', 1))
            for changes in ({'model': 'foreign'}, {'serving_gates': None},
                            {'serving_gates': receipt.serving_gates | {'model_sha': 'foreign'}},
                            {'serving_gates': receipt.serving_gates | {'tokenizer_sha': 'foreign'}},
                            {'serving_gates': receipt.serving_gates | {'temperature': 1}}):
                with self.subTest(model=model, metadata=changes):
                    result = policy.decide('go', {}, {}, table, tier1=_dc.replace(receipt, **changes))
                    self.assertIsNone(result.target_id)
            dist = _cal.verify_distribution(receipt.mapping, receipt.logprobs_content, receipt.generated_key)
            self.assertEqual(_cal.calibrate_decision(dist, automatic=True, artifact_context=context,
                model_sha=a.model, tokenizer_sha=a.tokenizer, split_sha=a.split,
                serving_flags=dict(a.serving)).verdict, _cal.CALIBRATED)
            for field in ('model_sha', 'tokenizer_sha', 'split_sha', 'serving_flags'):
                for wrong in (({},) if field == 'serving_flags' else ('foreign', '')):
                    self.assertEqual(_cal.calibrate_decision(dist, automatic=True, artifact_context=context,
                        **{field: wrong}).verdict, _cal.ABSTAIN)

    # @anchor:wa.tests.scripts.test_routing_integration.TestRouting.test_select_lane_misses
    def test_select_lane_misses(self):
        h, cell, session, evidence, owners = _selection_fixture()
        calls = []
        request = _dc.replace(h._request(cell), logical_id='absent-logical-id')
        for lane in ('STRUCTURAL', 'TEXT', 'VISUAL', 'MUSE'):
            owners.select_proposal = lambda *args: calls.append(lane) or _i.LaneSelection(
                lane, 1, 'proposed-only', None, evidence.observation, session.authority, ('binding',))
            selection = _i.select_lane(session, evidence, request=request, owners=owners, policy=None)
            self.assertEqual(selection.lane, lane)
            self.assertIsNone(selection.custody)
        self.assertEqual(calls, ['STRUCTURAL', 'TEXT', 'VISUAL', 'MUSE'])
        # An overflow miss cannot be re-labelled as an ordinary lookup miss.
        request = _dc.replace(request, query_terms=tuple('term' + str(n) for n in range(17)))
        with self.assertRaisesRegex(ValueError, 'SCRIPT_NOT_ORDINARY_MISS'):
            _i.select_lane(session, evidence, request=request, owners=owners, policy=None)
        self.assertEqual(len(calls), 4)

    # @anchor:wa.tests.scripts.test_routing_integration.TestRouting.test_script_input_and_terminal
    def test_script_input_and_terminal(self):
        fixture = _loop_tests.TestCuLoop(); fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        t = _execution._t
        for disposition in (t.InputDisposition.APPLIED, t.InputDisposition.INPUT_UNCERTAIN):
            for raises in (False, True):
                events, advances = [], []
                state = _types.SimpleNamespace(input_disposition=t.InputDisposition.NOT_SENT, outcome=None)
                session = _types.SimpleNamespace(custody=_types.SimpleNamespace(plan=_types.SimpleNamespace(nodes=(1, 2, 3))))
                def advance(s):
                    advances.append(1)
                    state.input_disposition = disposition
                    if len(advances) > 1:
                        if raises:
                            raise ValueError('SYNTHETIC_DRAIN_FAILURE')
                        state.outcome = object()
                    return state
                owner = _types.SimpleNamespace(check_ready=lambda: True, select_script=lambda *a: session,
                    advance_session=advance, runner_state=lambda s: state, accept_script=lambda *a: False,
                    uncertain=lambda action: events.append('uncertain'), charge_model=lambda: self.fail('MODEL_AFTER_SCRIPT'))
                cap = fixture.capsule(['a'])
                result = _loop.run_loop(cap, _loop_tests.Describer(), _loop.ReplayDecider([]),
                    goal='synthetic', trace=_io.StringIO(), integration=owner,
                    clock=fixture.clock, sleep=fixture.clock.sleep)
                self.assertEqual(len(advances), 2)
                self.assertEqual(result['stop_reason'], 'failure')
                self.assertTrue(result['failure']['input_effect_unknown'])
                self.assertEqual(events, ['uncertain'])
                self.assertEqual(cap.inputs, [])
                advances.clear(); events.clear()
                state.outcome = None
                state.input_disposition = t.InputDisposition.NOT_SENT
                surface, planner = _web_tests.FakeSurface(), _web_tests.FakePlanner()
                web = _loop.run_web_cycle(surface, planner, _web_tests.FakeExecutor(), {},
                    task='synthetic', trace=_io.StringIO(), integration=owner,
                    clock=fixture.clock, sleep=fixture.clock.sleep)
                self.assertEqual(web['stop_reason'], 'failure')
                self.assertEqual((len(advances), events), (2, ['uncertain']))
                self.assertEqual((planner.plans, surface.calls), (0, []))

    # @anchor:wa.tests.scripts.test_routing_integration.TestRouting.test_script_state_unavailable
    def test_script_state_unavailable(self):
        fixture = _loop_tests.TestCuLoop(); fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        events = []
        def unavailable(*args):
            raise ValueError('OWNER_STOPPED')
        session = _types.SimpleNamespace(custody=_types.SimpleNamespace(plan=_types.SimpleNamespace(nodes=(1,))))
        owner = _types.SimpleNamespace(check_ready=lambda: True, select_script=lambda *a: session,
            advance_session=unavailable, runner_state=unavailable, uncertain=lambda action: events.append(action))
        result = _loop.run_loop(fixture.capsule(['a']), _loop_tests.Describer(), _loop.ReplayDecider([]),
            goal='synthetic', trace=_io.StringIO(), integration=owner, clock=fixture.clock, sleep=fixture.clock.sleep)
        self.assertTrue(result['failure']['input_effect_unknown'])
        self.assertEqual(len(events), 1)

    # @anchor:wa.tests.scripts.test_routing_integration.TestRouting.test_script_advancement_bounds
    def test_script_advancement_bounds(self):
        for deadline in (False, True, 'late_terminal'):
            fixture = _loop_tests.TestCuLoop(); fixture.setUp()
            self.addCleanup(fixture.doCleanups)
            state = _types.SimpleNamespace(input_disposition=_execution._t.InputDisposition.APPLIED, outcome=None)
            session = _types.SimpleNamespace(custody=_types.SimpleNamespace(plan=_types.SimpleNamespace(nodes=(1, 2))))
            advances, uncertain = [], []
            def advance(s):
                advances.append(1)
                if deadline:
                    fixture.clock.now = 2
                if deadline == 'late_terminal':
                    state.outcome = object()
                return state
            owner = _types.SimpleNamespace(check_ready=lambda: True, select_script=lambda *a: session,
                advance_session=advance, runner_state=lambda s: state, accept_script=lambda *a: True,
                uncertain=lambda action: uncertain.append(action))
            result = _loop.run_loop(fixture.capsule(['a']), _loop_tests.Describer(), _loop.ReplayDecider([]),
                goal='synthetic', trace=_io.StringIO(), integration=owner, max_ms=1000,
                clock=fixture.clock, sleep=fixture.clock.sleep)
            self.assertEqual(result['stop_reason'], 'max_ms' if deadline else 'failure')
            self.assertEqual(len(advances), 1 if deadline else 3)
            self.assertEqual(len(uncertain), 1)
            self.assertTrue(result['failure']['input_effect_unknown'])

    # @anchor:wa.tests.scripts.test_routing_integration.TestRouting.test_web_restart_and_script_miss
    def test_web_restart_and_script_miss(self):
        for ready, ordinary in ((False, True), (True, False), (True, True)):
            surface, planner = _web_tests.FakeSurface(), _web_tests.FakePlanner()
            starts = []
            surface.start = lambda: starts.append('start')
            surface.integration.check_ready = lambda: ready
            surface.integration.check_script_miss = lambda: ordinary
            clock = _web_tests.Clock()
            import json
            memory = json.loads(_web_tests.MEMORY.read_text())
            result = _loop.run_web_cycle(surface, planner, _web_tests.FakeExecutor(), memory,
                task='synthetic', trace=_io.StringIO(), clock=clock, sleep=clock.sleep)
            if ready and ordinary:
                self.assertTrue(result['goal_verified'], result)
                self.assertEqual(planner.plans, 1)
            else:
                self.assertFalse(result['goal_verified'])
                self.assertEqual((planner.plans, surface.calls), (0, []))
                self.assertEqual(starts, ['start'] if ready else [])

    # @anchor:wa.tests.scripts.test_routing_integration.TestRouting.test_cross_lane_post_input
    def test_cross_lane_post_input(self):
        for mode in ('lost_completion', 'applied_postcheck_failed'):
            runtime = _execution._RuntimeHost()
            if mode == 'lost_completion':
                runtime.raise_send = True
            else:
                runtime.semantic_ok = False
            _execution._r.advance(runtime.state)
            self.assertEqual(runtime.input_count, 1)
            self.assertIn(runtime.state.input_disposition,
                (_execution._t.InputDisposition.APPLIED, _execution._t.InputDisposition.INPUT_UNCERTAIN))
            h, cell, session, evidence, owners = _selection_fixture()
            session = _dc.replace(session, unresolved_actions=('consumed-action',))
            owners.check_session = lambda s: s is session
            later = []
            for lane in ('SCRIPT', 'STRUCTURAL', 'TEXT', 'VISUAL', 'MUSE'):
                owners.select_proposal = lambda *a: later.append(lane)
                with self.assertRaisesRegex(ValueError, 'RESTART_UNCERTAIN'):
                    _i.select_lane(session, evidence, request=h._request(cell), owners=owners, policy=None)
            self.assertEqual(later, [])
            _execution._r.advance(runtime.state)
            self.assertEqual(runtime.input_count, 1)

    # @anchor:wa.tests.scripts.test_routing_integration.TestRouting.test_restart_all_lanes_recovery
    def test_restart_all_lanes_recovery(self):
        fixture = _ledger_tests.LedgerTraceTests(); fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        token = fixture._reserve()
        fixture._append(token, _ledger_tests.T.EventState.ADMITTED)
        fixture.input()  # Crash before an applied/audit completion can be recorded.
        self.assertEqual(len(fixture.input.calls), 1)
        fixture.owner = fixture._owner()
        with self.assertRaises(_ledger_tests.T.ContractError):
            fixture.owner.recover(scope=fixture.f['scope'], evidence=None, reconciliation=None)
        for recovery, reconciliation in ((object(), fixture.reconciled), (fixture.resolved, object())):
            with self.assertRaises(_ledger_tests.T.ContractError):
                fixture.owner.recover(scope=fixture.f['scope'], evidence=recovery, reconciliation=reconciliation)
        proposals = []
        for readmitted in (False, True):
            h, cell, session, evidence, owners = _selection_fixture()
            if readmitted:
                h._admit(2)
                h._publish()
            closed = _dc.replace(session, restart=_dc.replace(session.restart, open=False,
                unresolved=('uncertain-action',), reconciliation=None),
                consumed_actions=('uncertain-action',), unresolved_actions=('uncertain-action',))
            owners.check_session = lambda s: s is closed
            for lane in ('SCRIPT', 'STRUCTURAL', 'TEXT', 'VISUAL', 'MUSE'):
                owners.select_proposal = lambda *a: proposals.append(lane)
                with self.assertRaisesRegex(ValueError, 'RESTART_UNCERTAIN'):
                    _i.select_lane(closed, evidence, request=h._request(cell), owners=owners, policy=None)
            self.assertEqual(proposals, [])
        self.assertIs(fixture.owner.recover(scope=fixture.f['scope'], evidence=fixture.resolved,
            reconciliation=fixture.reconciled).result, _ledger_tests.T.Truth.TRUE)
        renewed = _dc.replace(session, consumed_actions=closed.consumed_actions)
        owners.check_session = lambda s: s is closed or s is renewed
        owners.check_reconciliation = lambda s, proof: proof is fixture.reconciled
        owners.renew_session = lambda s, proof: renewed
        owners.check_fresh_grant = lambda s, proof: proof is fixture.reconciled
        with self.assertRaisesRegex(ValueError, 'RECONCILIATION'):
            _i.reconcile_uncertain(closed, evidence=object(), owners=owners)
        self.assertIs(_i.reconcile_uncertain(closed, evidence=fixture.reconciled, owners=owners), renewed)
        owners.select_proposal = lambda *a: _i.LaneSelection('TEXT', 1, 'different-next-action', None,
            evidence.observation, renewed.authority, ('fresh-grant',))
        next_request = _dc.replace(h._request(cell), logical_id='different-next-action')
        selection = _i.select_lane(renewed, evidence, request=next_request, owners=owners, policy=None)
        self.assertNotIn(selection.target, renewed.consumed_actions)
        boundary = _execution._Harness()
        self.assertEqual(boundary.send()['status'], 'admitted')
        self.assertEqual(boundary.fixture.actuator.opened, 1)
        self.assertEqual(len(fixture.input.calls), 1)  # The interrupted action never repeats.

    # @anchor:wa.tests.scripts.test_routing_integration.TestRouting.test_authority_each_field
    def test_authority_each_field(self):
        t = _execution._t
        for field in ('policy', 'capability_set', 'schema', 'calibration_certificate',
                      'calibration_reason', 'scope', 'environment_compatibility'):
            changes = {field: 2}
            if field == 'calibration_certificate':
                changes['calibration_reason'] = None
            elif field == 'calibration_reason':
                changes[field] = t.Reason.UNKNOWN
            for timing in ('before_accept', 'after_accept'):
                with self.subTest(field=field, timing=timing):
                    f = _context_tests._Fixture(); epoch = f.open(); packed = f.pack(epoch).packed
                    response = object(); f.host.responses[id(packed)] = response
                    router = _context_tests._r
                    original = f.sh.authority
                    # The public registration captures a live authority guard.
                    with _patch.object(_execution._Harness, 'guard', lambda *a:
                            _execution._YES if (f.sh.scope == epoch.configuration.scope and
                                f.sh.store.current_authority_revision(f.sh.scope) == original) else _execution._NO):
                        boundary = _execution._Harness()
                    admission, context = boundary.admission, boundary.context
                    if timing == 'after_accept':
                        self.assertEqual(router.validate_response(epoch, packed, response, host=f.host).status, 'PROPOSAL')
                    if field == 'scope':
                        # The authority owner rejects a scope revision without its matching grant.
                        with self.assertRaises(t.ContractError):
                            f.sh.store.invalidate_authority(f.sh.scope, _dc.replace(original, **changes))
                        # A new attested scope has no authority in the old partition.
                        f.sh.scope = _dc.replace(f.sh.scope, scope_revision=2)
                    else:
                        f.sh.store.invalidate_authority(f.sh.scope, _dc.replace(original, **changes))
                    with self.assertRaises(t.ContractError):
                        router.validate_response(epoch, packed, response, host=f.host)
                    with self.assertRaises(_execution._p.PolicyRefused):
                        boundary.fixture.policy.dispatch(admission, boundary.fixture.actuator, script_context=context)
                    self.assertEqual(boundary.fixture.actuator.opened, 0)
                    if field == 'scope':
                        # Restore the old partition only to acknowledge its cancellation/drain.
                        f.sh.scope = epoch.configuration.scope
                    router.close_epoch(epoch, host=f.host, reason=t.Reason.STALE)
        # Healthy exact leases survive ordinary catalogue replacement.
        _check_controls(_store_tests.StoreControls, ('test_g_reload_no_splice',))
        _check_controls(_context_tests.ContextTests, ('test_g_invalidation_barriers',))
        boundary = _execution._Harness()
        self.assertEqual(boundary.send()['status'], 'admitted')
        self.assertEqual(boundary.fixture.actuator.opened, 1)

    # @anchor:wa.tests.scripts.test_routing_integration.TestRouting.test_classification_owner_controls
    def test_classification_owner_controls(self):
        _check_controls(_store_tests.StoreControls, ('test_g_classify_episode_only',
            'test_g_classify_quarantine_cas_and_defect', 'test_g_classify_outage_environment_scope',
            'test_g_defect_digest_never_readmitted'))
        _check_controls(_execution.TestRuntime, ('test_classification_and_holder_proof',))

    # @anchor:wa.tests.scripts.test_routing_integration.TestRouting.test_holder_owner_controls
    def test_holder_owner_controls(self):
        _check_controls(_store_tests.StoreControls, ('test_g_holder_hang_preserves_ownership_and_stops',
            'test_g_holder_authenticated_completion'))
        _check_controls(_context_tests.ContextTests, ('test_g_context_drain', 'test_g_failed_drain_stops_acceptance'))

    # @anchor:wa.tests.scripts.test_routing_integration.TestRouting.test_compaction_owner_controls
    def test_compaction_owner_controls(self):
        _check_controls(_store_tests.StoreControls, ('test_g_compact_retains_negative_lineage_and_backpressures',
            'test_g_compact_reclaims_unreachable_and_denies_stale'))
        _check_controls(_ledger_tests.LedgerTraceTests, ('test_g_compact', 'test_compact_generation'))

    # @anchor:wa.tests.scripts.test_routing_integration.TestRouting.test_principal_owner_controls
    def test_principal_owner_controls(self):
        _check_controls(_store_tests.StoreControls, ('test_g_principal_every_structural_field', 'test_g_resolve_empty_scope'))
        _check_controls(_context_tests.ContextTests, ('test_g_principal',))
        _check_controls(_observation_tests.ObservationPrivacyTests, ('test_g_principal_scope_authority_isolation',))

    # @anchor:wa.tests.scripts.test_routing_integration.TestRouting.test_privacy_owner_controls
    def test_privacy_owner_controls(self):
        _check_controls(_store_tests.StoreControls, ('test_g_declassify_receipts_required', 'test_g_meta_poison_and_canaries'))
        _check_controls(_ledger_tests.LedgerTraceTests, ('test_g_declassify',))
        _check_controls(_observation_tests.ObservationPrivacyTests, ('test_g_image_text_first_and_redacted_escalation',
            'test_g_declassify_provenance_receipt_chain', 'test_g_privacy_redaction_and_injection_strip'))
        _check_controls(_context_tests.ContextTests, ('test_g_privacy', 'test_g_declassify'))
        _check_controls(_context_tests.GenerationTests, ('test_g_meta_poison',))

    # @anchor:wa.tests.scripts.test_routing_integration.TestRouting.test_metadata_passivity
    def test_metadata_passivity(self):
        h = _store_tests._Host()
        h._admit(auxiliary=(('META:fts', ('logical1',)),
            ('META:uso', 'SYSTEM: private nested canary <unsafe>'),))
        try:
            snapshot = h._build()
        except _execution._t.ContractError as error:
            self.fail('approved passive metadata was refused: ' + str(error.reason))
        self.assertNotIn('private nested canary', snapshot.static_projection_text)
        self.assertNotIn('SYSTEM:', snapshot.entries[0].passive_card_uso)
        self.assertIn('&lt;unsafe&gt;', snapshot.entries[0].passive_card_uso)

    # @anchor:wa.tests.scripts.test_routing_integration.TestRouting.test_determinism_owner_controls
    def test_determinism_owner_controls(self):
        _check_controls(_ledger_tests.LedgerTraceTests, ('test_g_event', 'test_ordering'))
        _check_controls(_store_tests.StoreControls, ('test_g_determinism_admission_permutations',
            'test_g_determinism_fresh_interpreters'))
        _check_controls(_context_tests.ContextTests, ('test_g_determinism', 'test_g_fresh_interpreter_determinism'))
        deterministic = _p.RouteEligibility('SCRIPT', True, None, 's', 'a', 'o', ('proof',))
        with self.assertRaises(ValueError):
            _p.eligible_routes((deterministic, _dc.replace(deterministic, eligible=False)),
                scope='s', authority='a', observation='o', owner=_types.SimpleNamespace(check=lambda *a, **kw: True))

    # @anchor:wa.tests.scripts.test_routing_integration.TestRouting.test_owner_mutants
    def test_owner_mutants(self):
        """Re-use sealed owner contrasts while mutating only live function objects."""
        modules = {'store': _store_tests._s, 'catalogue': _store_tests._c, 'resolver': _store_tests._r}
        gates = ('G-HOLDER-HANG', 'G-COMPACT', 'G-CLASSIFY', 'G-META-POISON',
                 'G-DETERMINISM', 'G-DECLASSIFY', 'G-INVALIDATE', 'G-PRINCIPAL')
        for label, test, module_name, name, before, after in _store_tests._MUTANTS:
            if not label.startswith(gates):
                continue
            module = modules[module_name]
            owner = module.ScriptStore if module_name == 'store' and hasattr(module.ScriptStore, name) else module
            source = _Path(module.__file__).read_text()
            tree = _ast.parse(source)
            nodes = (next(n for n in tree.body if isinstance(n, _ast.ClassDef) and n.name == 'ScriptStore').body
                     if owner is not module else tree.body)
            function = next(n for n in nodes if isinstance(n, _ast.FunctionDef) and n.name == name)
            start = min([function.lineno] + [d.lineno for d in function.decorator_list]) - 1
            snippet = _textwrap.dedent('\n'.join(source.splitlines()[start:function.end_lineno]))
            if owner is not module:
                before, after = before.replace('\n    ', '\n'), after.replace('\n    ', '\n')
            with self.subTest(gate=label):
                self.assertIn(before, snippet)
                baseline = _unittest.TestResult(); _store_tests.StoreControls(test).run(baseline)
                self.assertTrue(baseline.wasSuccessful(), baseline.errors or baseline.failures)
                namespace = dict(module.__dict__)
                exec(compile(snippet.replace(before, after, 1), '<owner-mutant>', 'exec'), namespace)
                with _patch.object(owner, name, namespace[name]):
                    result = _unittest.TestResult()
                    if label == 'G-META-POISON passive metadata excluded from Z2':
                        TestRouting('test_metadata_passivity').run(result)
                    else:
                        _store_tests.StoreControls(test).run(result)
                self.assertTrue(result.failures, 'mutant survived: ' + label)
                self.assertFalse(result.errors, result.errors)
                print('MUTANT ' + label + ' KILLED ' + test)

    # @anchor:wa.tests.scripts.test_routing_integration.TestRouting.test_lifecycle_mutants
    def test_lifecycle_mutants(self):
        cases = (
            (_ledger_tests.L, 'LedgerOwner', 'recover',
             '_host_check(self._check_recovery, scope, evidence)', 'None', 'test_restart_all_lanes_recovery'),
            (_ledger_tests.L, 'LedgerOwner', 'recover',
             '_host_check(self._check_reconciliation, scope, reconciliation)', 'None', 'test_restart_all_lanes_recovery'),
            (_ledger_tests.L, 'LedgerOwner', 'append',
             '_need(event.state in transitions.get(prior, ()), _t.Reason.CONFLICT)', 'None', 'test_determinism_owner_controls'),
            (_execution._a, '_InputInterlock', 'send_once',
             '_true(context.guard(admission, request, decision))', 'None', 'test_authority_each_field'),
            (_ledger_tests.L, 'LedgerOwner', 'retire',
             '_host_check(check_drain, self._producer, proof)', 'None', 'test_compaction_owner_controls'),
            (_ledger_tests.L, 'LedgerOwner', 'retire',
             'type(generation) is int and generation == self._generation', 'True', 'test_compaction_owner_controls'),
            (_context_tests._r, None, 'close_epoch',
             '_check(epoch.store.release_snapshot_pin(epoch.pin_token))', 'None', 'test_endurance'),
            (_context_tests._r, None, 'validate_response',
             '_live(epoch)', 'None', 'test_authority_each_field'),
        )
        for module, class_name, name, old, new, test in cases:
            with self.subTest(function=name, control=old):
                source = _Path(module.__file__).read_text()
                tree = _ast.parse(source)
                nodes = tree.body if class_name is None else next(n for n in tree.body
                    if isinstance(n, _ast.ClassDef) and n.name == class_name).body
                function = next(n for n in nodes if isinstance(n, _ast.FunctionDef) and n.name == name)
                start = min([function.lineno] + [d.lineno for d in function.decorator_list]) - 1
                snippet = _textwrap.dedent('\n'.join(source.splitlines()[start:function.end_lineno]))
                self.assertIn(old, snippet)
                namespace = dict(module.__dict__)
                exec(compile(snippet.replace(old, new, 1), '<lifecycle-mutant>', 'exec'), namespace)
                owner = module if class_name is None else getattr(module, class_name)
                with _patch.object(owner, name, namespace[name]):
                    result = _unittest.TestResult(); TestRouting(test).run(result)
                self.assertTrue(result.failures, 'mutant survived: ' + old)
                self.assertFalse(result.errors, result.errors)
                print('MUTANT lifecycle-' + test + ' KILLED ' + old)

    # @anchor:wa.tests.scripts.test_routing_integration.TestRouting.test_mutants
    def test_mutants(self):
        cases = (
            (_p, "type(artifact) is not CalibrationArtifact or owner is None", 'owner is None', 'test_calibration_provenance'),
            (_p, "not a.split", 'False', 'test_calibration_provenance'),
            (_cal, "keys = conformal_set(probs, q_hat_promille)", 'keys = [receipt.generated_key]', 'test_calibration_provenance'),
            (_m, "cached += row.cached_input_tokens", 'cached += row.input_tokens if row.prefix_equal else row.cached_input_tokens', 'test_metrics'),
            (_p, "type(peak_bytes) is int and peak_bytes > 0", 'type(peak_bytes) is int', 'test_aggregate_and_holder'),
            (_loop, "if clock() >= deadline:\n                raise TimeoutError('episode budget exhausted')", "if False:\n                raise TimeoutError('episode budget exhausted')", 'test_script_advancement_bounds'),
            (_p, "_need(route.lane not in accepted, 'DUPLICATE_ROUTE')", 'None', 'test_eligibility_determinism'),
            (_cal, "(model_sha is not None and model_sha != a.model)", 'False', 'test_automatic_receipt_identity'),
            (_cal, "(tokenizer_sha is not None and tokenizer_sha != a.tokenizer)", 'False', 'test_automatic_receipt_identity'),
            (_cal, "(split_sha is not None and split_sha != a.split)", 'False', 'test_automatic_receipt_identity'),
            (_cal, "(serving_flags is not None and serving_flags != dict(a.serving))", 'False', 'test_automatic_receipt_identity'),
            (_c, "gates.get('model_sha') != registered.model", 'False', 'test_automatic_receipt_identity'),
            (_c, "gates.get('tokenizer_sha') != registered.tokenizer", 'False', 'test_automatic_receipt_identity'),
            (_c, "!= dict(registered.serving)", '!= {k: v for k, v in gates.items() if k not in (\'model_sha\', \'tokenizer_sha\')}', 'test_automatic_receipt_identity'),
            (_i, "resolved.detail == 'no_match'", 'True', 'test_select_lane_misses'),
            (_i, "owners.check_reconciliation(session, evidence) is True", 'True', 'test_restart_all_lanes_recovery'),
            (_loop, "while remaining:", "while remaining == len(session.custody.plan.nodes) + 1:", 'test_real_runner_registration'),
            (_loop, "input_started = input_started or possible", 'input_started = False', 'test_script_input_and_terminal'),
            (_loop, "if integration.check_ready() is not True:", 'if False:', 'test_web_restart_and_script_miss'),
            (_loop, "if owners.check_script_miss() is not True:", 'if False:', 'test_web_restart_and_script_miss'),
            (_p, "current != (a.model, a.tokenizer, a.serving, a.split,\n                   a.policy_revision, a.environment_revision)", 'False', 'test_calibration_provenance'),
            (_p, "len(a.sample_refs) < min_samples", 'False', 'test_calibration_provenance'),
            (_p, "return owner.check(a, current=current, min_samples=min_samples,\n                           max_error=max_error, min_coverage=min_coverage) is True", 'return True', 'test_calibration_provenance'),
            (_p, "cost + probability * result", 'probability * (cost + probability * result)', 'test_cost'),
            (_p, "c * p for c, p in zip(model.costs, model.probabilities)", 'c * p * p for c, p in zip(model.costs, model.probabilities)', 'test_cost'),
            (_p, "return None", 'return _Fraction(0)', 'test_cost'),
            (_p, "any(expected[k] != actual[k] for k in required)", 'False', 'test_drift'),
            (_p, "max(measured, self._charged) + peak_bytes <=", 'False or 0 <=', 'test_aggregate_and_holder'),
            (_p, "max(measured, self._charged) + extra_bytes <=", '0 <=', 'test_aggregate_and_holder'),
            (_p, "_need(self._drain_owner.check(owner, proof) is True, 'OWNER_NOT_DRAINED')", 'pass', 'test_aggregate_and_holder'),
            (_p, "self._attempts <= self.policy.host_attempts", 'True', 'test_admission_flood'),
            (_p, "count <= self.policy.principal_attempts", 'True', 'test_admission_flood'),
            (_m, "calls += 1", 'calls += 2', 'test_metrics'),
            (_m, "None if missing or not tokens else _Fraction(cached, tokens)", '_Fraction(cached, tokens) if tokens else _FRACTION_ZERO', 'test_metrics'),
            (_m, "row.seal_ns - row.arrival_ns", 'row.seal_ns - row.startup_end_ns', 'test_latency'),
            (_m, "all(a <= b for a, b in zip(known, known[1:]))", 'True', 'test_latency'),
            (_m, "if lane not in eligible:", 'if False:', 'test_cost'),
            (_i, "session.restart.open is True", 'True', 'test_restart_and_authority'),
            (_i, "owners.current_authority(session.scope) == session.authority", 'True', 'test_restart_and_authority'),
            (_i, "scope.attestation is not None and scope.attestation_reason is None", 'True', 'test_observation_adapter'),
            (_i, "and owners.check_custody(session, custody) is True", 'and True', 'test_real_store_selection'),
            (_c, "type(element_table.get('blocked_control_present')) is not int\n            or element_table['blocked_control_present'] != 0", "element_table.get('blocked_control_present', 0) != 0", 'test_cascade_blockers_and_calibration'),
            (_loop, "if self.dispatcher is None:\n            raise ValueError('DISPATCH_OWNER_UNAVAILABLE')", "if self.dispatcher is None:\n            return _receipt(_json_command(args, timeout=self.timeout))", 'test_live_dispatch_registration'),
            (_loop, "input_started = True", 'input_started = False', 'test_loop_semantics_and_budget'),
            (_loop, "'ok': semantic is True", "'ok': True", 'test_loop_semantics_and_budget'),
            (_loop, "integration.charge_model() is not True", 'False', 'test_loop_semantics_and_budget'),
            (_loop, "if possible_input:\n                    raise BackendError('RECONCILIATION_REQUIRED')", "if False:\n                    raise BackendError('RECONCILIATION_REQUIRED')", 'test_web_dispatch_and_no_retry'),
        )
        for module, old, new, test in cases:
            with self.subTest(module=module.__name__, control=old):
                source = _Path(module.__file__).read_text()
                self.assertIn(old, source)
                # Missingness mutant explicitly assigns zero: a defined value,
                # so only a failed assertion can kill it, never NameError.
                mutant = _types.ModuleType(module.__name__ + '_routing_mutant')
                mutant.__package__ = module.__package__
                mutant.__dict__['_FRACTION_ZERO'] = _F(0)
                _sys.modules[mutant.__name__] = mutant
                try:
                    exec(compile(source.replace(old, new), '<routing-mutant>', 'exec'), mutant.__dict__)
                    result = _unittest.TestResult()
                    alias = {_p: '_p', _m: '_m', _i: '_i', _c: '_c', _loop: '_loop', _cal: '_cal'}[module]
                    with _patch(__name__ + '.' + alias, mutant):
                        TestRouting(test).run(result)
                    self.assertTrue(result.failures, 'mutant survived: ' + old)
                    self.assertFalse(result.errors, result.errors)
                    print('MUTANT routing-' + test + ' KILLED ' + old.splitlines()[0])
                finally:
                    del _sys.modules[mutant.__name__]


# S10 module contracts are frozen independently of the index under test.
_S10_MODULES = (('wender/perceive/script_observation.py', 'wa.perceive.script_observation', 'HB-WA-s1-perceive', 'PERCEIVE', 2, 's10-perceive-script-observation'), ('wender/perceive/step_binding.py', 'wa.perceive.step_binding', 'HB-WA-s1-perceive', 'PERCEIVE', 2, 's10-perceive-step-binding'), ('wender/perceive/redact.py', 'wa.perceive.redact', 'HB-WA-s1-perceive', 'PERCEIVE', 2, 's10-perceive-redact'), ('wender/perceive/element_table.py', 'wa.perceive.element_table', 'HB-WA-s1-perceive', 'PERCEIVE', 2, 's10-perceive-element-table'), ('wender/perceive/disambig.py', 'wa.perceive.disambig', 'HB-WA-s1-perceive', 'PERCEIVE', 1, 's10-perceive-disambig'), ('wender/scripts/act_api.py', 'wa.scripts.act_api', 'HB-WA-s2-act', 'ACT', 3, 's10-scripts-act-api'), ('wender/scripts/frame_wait.py', 'wa.scripts.frame_wait', 'HB-WA-s2-act', 'ACT', 3, 's10-scripts-frame-wait'), ('wender/act/proto1_action_policy.py', 'wa.act.proto1_action_policy', 'HB-WA-s2-act', 'ACT', 3, 's10-act-proto1-action-policy'), ('wender/learn/script_ledger.py', 'wa.learn.script_ledger', 'HB-WA-s3-remember', 'LEARN', 2, 's10-learn-script-ledger'), ('wender/learn/script_integration.py', 'wa.learn.script_integration', 'HB-WA-s3-remember', 'LEARN', 3, 's10-learn-script-integration'), ('wender/learn/dream_journal.py', 'wa.learn.dream_journal', 'HB-WA-s3-remember', 'LEARN', 2, 's10-learn-dream-journal'), ('wender/learn/cu_memory.py', 'wa.learn.cu_memory', 'HB-WA-s3-remember', 'LEARN', 2, 's10-learn-cu-memory'), ('wender/learn/cu_loop.py', 'wa.learn.cu_loop', 'HB-WA-s3-remember', 'LEARN', 2, 's10-learn-cu-loop'), ('wender/scripts/types.py', 'wa.scripts.types', 'HB-WA-s4-learn', 'LEARN', 2, 's10-scripts-types'), ('wender/scripts/header.py', 'wa.scripts.header', 'HB-WA-s4-learn', 'LEARN', 2, 's10-scripts-header'), ('wender/scripts/ast_gate.py', 'wa.scripts.ast_gate', 'HB-WA-s4-learn', 'LEARN', 2, 's10-scripts-ast-gate'), ('wender/scripts/compiler.py', 'wa.scripts.compiler', 'HB-WA-s4-learn', 'LEARN', 2, 's10-scripts-compiler'), ('wender/scripts/predicates.py', 'wa.scripts.predicates', 'HB-WA-s4-learn', 'LEARN', 2, 's10-scripts-predicates'), ('wender/scripts/runner.py', 'wa.scripts.runner', 'HB-WA-s4-learn', 'LEARN', 3, 's10-scripts-runner'), ('wender/learn/script_store.py', 'wa.learn.script_store', 'HB-WA-s4-learn', 'LEARN', 2, 's10-learn-script-store'), ('wender/learn/catalogue.py', 'wa.learn.catalogue', 'HB-WA-s4-learn', 'LEARN', 2, 's10-learn-catalogue'), ('wender/learn/resolver.py', 'wa.learn.resolver', 'HB-WA-s4-learn', 'LEARN', 2, 's10-learn-resolver'), ('wender/learn/dream_replay.py', 'wa.learn.dream_replay', 'HB-WA-s4-learn', 'LEARN', 2, 's10-learn-dream-replay'), ('wender/learn/script_promotion.py', 'wa.learn.script_promotion', 'HB-WA-s4-learn', 'LEARN', 2, 's10-learn-script-promotion'), ('wender/learn/context_router.py', 'wa.learn.context_router', 'HB-WA-s4-learn', 'LEARN', 2, 's10-learn-context-router'), ('wender/learn/script_generator.py', 'wa.learn.script_generator', 'HB-WA-s4-learn', 'LEARN', 2, 's10-learn-script-generator'), ('wender/learn/script_policy.py', 'wa.learn.script_policy', 'HB-WA-s4-learn', 'LEARN', 2, 's10-learn-script-policy'), ('wender/learn/script_metrics.py', 'wa.learn.script_metrics', 'HB-WA-s4-learn', 'LEARN', 2, 's10-learn-script-metrics'), ('wender/learn/cascade.py', 'wa.learn.cascade', 'HB-WA-s4-learn', 'LEARN', 2, 's10-learn-cascade'), ('wender/learn/calibrate.py', 'wa.learn.calibrate', 'HB-WA-s4-learn', 'LEARN', 2, 's10-learn-calibrate'), ('wender/learn/memory.py', 'wa.learn.memory', 'HB-WA-s4-learn', 'LEARN', 2, 's10-learn-memory'), ('wender/learn/model_client.py', 'wa.learn.model_client', 'HB-WA-s4-learn', 'LEARN', 2, 's10-learn-model-client'))

import hashlib as _s10_hash
import json as _s10_json
import tokenize as _s10_tokenize
import tempfile as _s10_tempfile
import re as _s10_re
from tools import build_code_index as _s10_builder, route_code as _s10_router, reseal as _s10_seal, anchors as _s10_anchors

_S10_ROOT = _Path(__file__).resolve().parents[2]
_S10_OUTPUTS = frozenset(('anchors.jsonl', 'hb.json', 'cols.json', 'post.json', 'manifest.json', 'ID'))
_S10_GATES = frozenset(('G-SOURCE G-AST G-PLAN G-PIN G-RESOURCE G-ARGS G-PRED '
    'G-FACTS G-SCOPE G-JOIN G-FENCE G-RISK G-POST G-NORETRY G-END '
    'G-LEASE G-IDLE G-CAPACITY G-RELOAD G-RESOLVE G-PUBLISH G-EVENT G-SPOOL G-JOURNAL G-RECOVER '
    'G-REPLAY G-UTILITY G-SUPPORT G-PROMOTE G-SUSPEND G-SHADOW G-FRAME G-RELEASE G-WAIT G-DEADLINE G-TICKS '
    'G-IMAGE G-PRIVACY G-PREFIX G-PAGER G-DELTA G-GENERATE G-CALIBRATE G-METRICS G-COST G-DRIFT '
    'G-CASCADE G-ANCHOR G-DRAIN G-PRINCIPAL G-DECLASSIFY G-META-POISON G-PARSER-CAPS G-AGGREGATE '
    'G-ADMISSION-FLOOD G-HOLDER-HANG G-COMPACT G-E2E-LATENCY G-WAIT-PACING G-RESTART-UNCERTAIN '
    'G-INVALIDATE G-DETERMINISM G-CLASSIFY').split())


# Gate contracts are immutable. Empty external test IDs mean AUSENTE, never a pass.
# Fixture labels are exact rejecting expressions in the mapped test source.
_S10_GATE_SPECS = _types.MappingProxyType({
    'G-ADMISSION-FLOOD': ('ROUTE', ('tests.scripts.test_routing_integration.TestRouting.test_admission_flood',), (('tests.scripts.test_routing_integration.TestRouting.test_admission_flood', ("g.charge('new-episode-new-principal')",)),), False),
    'G-AGGREGATE': ('STORE', ('tests.scripts.test_store_catalogue.StoreControls.test_g_aggregate_preallocation',), (('tests.scripts.test_store_catalogue.StoreControls.test_g_aggregate_preallocation', ('h.used = h.policy.host_bytes - h.policy.emergency_reserve_bytes',)),), False),
    'G-ANCHOR': ('ROUTE', ('tests.scripts.test_routing_integration.TestS10Routing.test_g_anchor_coordinates',), (('tests.scripts.test_routing_integration.TestS10Routing.test_g_anchor_coordinates', ("('file', 'unrelated.py')",)),), False),
    'G-ARGS': ('LANG', ('tests.scripts.test_header.HeaderControls.test_g_args_verbatim',), (('tests.scripts.test_header.HeaderControls.test_g_args_verbatim', ("({'query': None}, _t.Reason.TYPE)",)),), False),
    'G-AST': ('LANG', ('tests.scripts.test_ast_gate._GateTests.test_wrapper_and_poisoned_suffix',), (('tests.scripts.test_ast_gate._GateTests.test_wrapper_and_poisoned_suffix', ("'import os'",)),), False),
    'G-CALIBRATE': ('ROUTE', ('tests.scripts.test_routing_integration.TestRouting.test_calibration_provenance',), (('tests.scripts.test_routing_integration.TestRouting.test_calibration_provenance', ('label_only',)),), False),
    'G-CAPACITY': ('STORE', ('tests.scripts.test_store_catalogue.StoreControls.test_g_capacity_decoded_and_support',), (('tests.scripts.test_store_catalogue.StoreControls.test_g_capacity_decoded_and_support', ('support_bytes=16777217',)),), False),
    'G-CASCADE': ('ROUTE', ('tests.scripts.test_routing_integration.TestRouting.test_cross_lane_post_input',), (('tests.scripts.test_routing_integration.TestRouting.test_cross_lane_post_input', ("'lost_completion'",)),), False),
    'G-CLASSIFY': ('DREAM', ('tests.scripts.test_dream_lifecycle.DreamTests.test_g_suspend_and_g_classify_scoped_denial_and_reinstatement',), (('tests.scripts.test_dream_lifecycle.DreamTests.test_g_suspend_and_g_classify_scoped_denial_and_reinstatement', ('_t.FailureClass.UNCLASSIFIED',)),), False),
    'G-COMPACT': ('STORE', ('tests.scripts.test_store_catalogue.StoreControls.test_g_compact_retains_negative_lineage_and_backpressures',), (('tests.scripts.test_store_catalogue.StoreControls.test_g_compact_retains_negative_lineage_and_backpressures', ('_t.Reason.CAPACITY',)),), False),
    'G-COST': ('ROUTE', ('tests.scripts.test_routing_integration.TestRouting.test_cost',), (('tests.scripts.test_routing_integration.TestRouting.test_cost', ('costs=(_F(-1), _F(2))',)),), False),
    'G-DEADLINE': ('FRAME', ('tests.scripts.test_frame_wait.TestFrameWait.test_capture_deadline',), (('tests.scripts.test_frame_wait.TestFrameWait.test_capture_deadline', ('end=101*_MS',)),), False),
    'G-DECLASSIFY': ('OBS', ('tests.scripts.test_observation_privacy.ObservationPrivacyTests.test_g_declassify_provenance_receipt_chain',), (('tests.scripts.test_observation_privacy.ObservationPrivacyTests.test_g_declassify_provenance_receipt_chain', ("'content_sha256':'0'*64",)),), False),
    'G-DELTA': ('CONTEXT', ('tests.scripts.test_context_generation.ContextTests.test_g_delta',), (('tests.scripts.test_context_generation.ContextTests.test_g_delta', ('omit_deletion',)),), False),
    'G-DETERMINISM': ('CONTEXT', ('tests.scripts.test_context_generation.ContextTests.test_g_determinism',), (('tests.scripts.test_context_generation.ContextTests.test_g_determinism', ("payload=b'conflict'",)),), False),
    'G-DRAIN': ('ROUTE', ('tests.scripts.test_routing_integration.TestRouting.test_endurance',), (('tests.scripts.test_routing_integration.TestRouting.test_endurance', ('proof=None',)),), False),
    'G-DRIFT': ('ROUTE', ('tests.scripts.test_routing_integration.TestRouting.test_drift',), (('tests.scripts.test_routing_integration.TestRouting.test_drift', ("for value in ('changed', None):",)),), False),
    'G-E2E-LATENCY': ('ROUTE', ('tests.scripts.test_routing_integration.TestRouting.test_latency',), (('tests.scripts.test_routing_integration.TestRouting.test_latency', ('queue_end_ns=9',)),), False),
    'G-END': ('ACT', ('tests.scripts.test_execution.TestRuntime.test_goal_and_no_input_ack',), (('tests.scripts.test_execution.TestRuntime.test_goal_and_no_input_ack', ('for truth in (_t.Truth.FALSE, _t.Truth.UNKNOWN, _t.Truth.TRUE):',)),), False),
    'G-EVENT': ('AUDIT', ('tests.scripts.test_ledger_trace.LedgerTraceTests.test_g_event',), (('tests.scripts.test_ledger_trace.LedgerTraceTests.test_g_event', ('altered = replace(events[-1], score_q16=2)',)),), False),
    'G-FACTS': ('OBS', ('tests.scripts.test_observation_privacy.ObservationPrivacyTests.test_g_facts_no_synthesized_actionability_or_gen16',), (('tests.scripts.test_observation_privacy.ObservationPrivacyTests.test_g_facts_no_synthesized_actionability_or_gen16', ('self.assertEqual(getattr(f, key).status, r.FactStatus.UNKNOWN)',)),), False),
    'G-FENCE': ('ACT', ('tests.scripts.test_execution.TestRuntime.test_fence_revision_restart_deadline',), (('tests.scripts.test_execution.TestRuntime.test_fence_revision_restart_deadline', ('h.restart_ok = False',)),), False),
    'G-FRAME': ('VEFRAME', (), (), True),
    'G-GENERATE': ('CONTEXT', ('tests.scripts.test_context_generation.GenerationTests.test_g_generate',), (('tests.scripts.test_context_generation.GenerationTests.test_g_generate', ("for changes, reason in [({'sidecars': object()}, _t.Reason.FOREIGN),",)),), False),
    'G-HOLDER-HANG': ('STORE', ('tests.scripts.test_store_catalogue.StoreControls.test_g_holder_hang_preserves_ownership_and_stops',), (('tests.scripts.test_store_catalogue.StoreControls.test_g_holder_hang_preserves_ownership_and_stops', ('_t.Reason.CANCELLED',)),), False),
    'G-IDLE': ('STORE', ('tests.scripts.test_store_catalogue.StoreControls.test_g_idle_holders_and_full_grace',), (('tests.scripts.test_store_catalogue.StoreControls.test_g_idle_holders_and_full_grace', ('self._reject(_t.Reason.MISSING, h._acquire, c)',)),), False),
    'G-IMAGE': ('OBS', ('tests.scripts.test_observation_privacy.ObservationPrivacyTests.test_g_image_text_first_and_redacted_escalation',), (('tests.scripts.test_observation_privacy.ObservationPrivacyTests.test_g_image_text_first_and_redacted_escalation', ("self.assertEqual((result.image_needed,result.reason_code), (False,'ESCALATION_FORBIDDEN_AT_TIER'))",)),), False),
    'G-INVALIDATE': ('CONTEXT', ('tests.scripts.test_context_generation.ContextTests.test_g_invalidate',), (('tests.scripts.test_context_generation.ContextTests.test_g_invalidate', ("values['calibration_reason'] = None",)),), False),
    'G-JOIN': ('OBS', ('tests.scripts.test_observation_privacy.ObservationPrivacyTests.test_g_join_certified_mark_node_and_transform_epoch',), (('tests.scripts.test_observation_privacy.ObservationPrivacyTests.test_g_join_certified_mark_node_and_transform_epoch', ('self.assertFalse(_binding((_raw(mark_bbox=(500,500,100,40)),),join_result=native,require_mark_join=True).bound)',)),), False),
    'G-JOURNAL': ('AUDIT', ('tests.scripts.test_ledger_trace.LedgerTraceTests.test_g_journal',), (('tests.scripts.test_ledger_trace.LedgerTraceTests.test_g_journal', ("for invalid in ('absent', None, True, 'PRIVATE_CANARY'):",)),), False),
    'G-LEASE': ('STORE', ('tests.scripts.test_store_catalogue.StoreControls.test_g_lease_exact_token_retain_release',), (('tests.scripts.test_store_catalogue.StoreControls.test_g_lease_exact_token_retain_release', ('self._reject(_t.Reason.FOREIGN, s.check_lease, _dc.replace(first), first.artifact, first.scope,',)),), False),
    'G-META-POISON': ('DREAM', ('tests.scripts.test_dream_lifecycle.DreamTests.test_g_meta_poison_blocked_at_promotion_and_catalogue',), (('tests.scripts.test_dream_lifecycle.DreamTests.test_g_meta_poison_blocked_at_promotion_and_catalogue', ('self._reject(_t.Reason.PRIVACY,self._transition,_t.Lifecycle.SHADOW,',)),), False),
    'G-METRICS': ('ROUTE', ('tests.scripts.test_routing_integration.TestRouting.test_metrics',), (('tests.scripts.test_routing_integration.TestRouting.test_metrics', ('cached_input_tokens=101',)),), False),
    'G-NORETRY': ('ACT', ('tests.scripts.test_execution.TestRuntime.test_postconditions_and_no_retry',), (('tests.scripts.test_execution.TestRuntime.test_postconditions_and_no_retry', ('setattr(h, flag, False)',)),), False),
    'G-PAGER': ('CONTEXT', ('tests.scripts.test_context_generation.ContextTests.test_g_pager',), (('tests.scripts.test_context_generation.ContextTests.test_g_pager', ('self._reject(_t.Reason.MISSING, f.pack, epoch,',)),), False),
    'G-PARSER-CAPS': ('LANG', ('tests.scripts.test_header.HeaderControls.test_g_parser_caps',), (('tests.scripts.test_header.HeaderControls.test_g_parser_caps', ('if allow_socket:',)),), False),
    'G-PIN': ('LANG', ('tests.scripts.test_header.HeaderControls.test_g_pin_mutations',), (('tests.scripts.test_header.HeaderControls.test_g_pin_mutations', ('screen_check=None',)),), False),
    'G-PLAN': ('LANG', ('tests.scripts.test_compiler.CompilerControls.test_g_plan_end_to_end',), (('tests.scripts.test_compiler.CompilerControls.test_g_plan_end_to_end', ("self._reject(_Host(_BODY.replace(b'    return None', b'    import os')), _t.Reason.SCHEMA)",)),), False),
    'G-POST': ('ACT', ('tests.scripts.test_execution.TestRuntime.test_postconditions_and_no_retry',), (('tests.scripts.test_execution.TestRuntime.test_postconditions_and_no_retry', ('setattr(h, flag, False)',)),), False),
    'G-PRED': ('LANG', ('tests.scripts.test_predicates.TestPredicates.test_g_pred',), (('tests.scripts.test_predicates.TestPredicates.test_g_pred', ('self._unknown(_run(f, negation, evidence=()), t.Reason.MISSING)',)),), False),
    'G-PREFIX': ('CONTEXT', ('tests.scripts.test_context_generation.ContextTests.test_g_prefix',), (('tests.scripts.test_context_generation.ContextTests.test_g_prefix', ('self._reject(_t.Reason.FOREIGN, _r.open_epoch,',)),), False),
    'G-PRINCIPAL': ('OBS', ('tests.scripts.test_observation_privacy.ObservationPrivacyTests.test_g_principal_scope_authority_isolation',), (('tests.scripts.test_observation_privacy.ObservationPrivacyTests.test_g_principal_scope_authority_isolation', ("for delta in ({'host_attested':False},{'top_level_origin':'https://evil.example/path?a=1'},",)),), False),
    'G-PRIVACY': ('OBS', ('tests.scripts.test_observation_privacy.ObservationPrivacyTests.test_g_privacy_redaction_and_injection_strip',), (('tests.scripts.test_observation_privacy.ObservationPrivacyTests.test_g_privacy_redaction_and_injection_strip', ('self.assertIsNone(receipt)',)),), False),
    'G-PROMOTE': ('DREAM', ('tests.scripts.test_dream_lifecycle.DreamTests.test_g_promote_full_certificate_chain_and_mutant_rejection',), (('tests.scripts.test_dream_lifecycle.DreamTests.test_g_promote_full_certificate_chain_and_mutant_rejection', ('self._reject(_t.Reason.CONFLICT,_p.evaluate,self.policy,_dc.replace(self.ev,mutant_proof=accepted),index=self.index)',)),), False),
    'G-PUBLISH': ('STORE', ('tests.scripts.test_store_catalogue.StoreControls.test_g_publish_negative_probes_and_positive_digest',), (('tests.scripts.test_store_catalogue.StoreControls.test_g_publish_negative_probes_and_positive_digest', ("bad = _dc.replace(positive, kind=kind, probe_id='bad', expected_digest=None)",)),), False),
    'G-RECOVER': ('AUDIT', ('tests.scripts.test_ledger_trace.LedgerTraceTests.test_g_recover',), (('tests.scripts.test_ledger_trace.LedgerTraceTests.test_g_recover', ('self._reject(T.Reason.FOREIGN, lambda: self._load(seal))',)),), False),
    'G-RELEASE': ('VEFRAME+FRAME', ('tests.scripts.test_frame_wait.TestFrameWait.test_release_failure',), (('tests.scripts.test_frame_wait.TestFrameWait.test_release_failure', ('self.assertIs(result.reason, _t.Reason.RECONCILIATION)',)),), True),
    'G-RELOAD': ('STORE', ('tests.scripts.test_store_catalogue.StoreControls.test_g_reload_no_splice',), (('tests.scripts.test_store_catalogue.StoreControls.test_g_reload_no_splice', ('expected_predecessor_revision=0',)),), False),
    'G-REPLAY': ('DREAM', ('tests.scripts.test_dream_lifecycle.DreamTests.test_g_replay_supported_only_and_repeated_determinism',), (('tests.scripts.test_dream_lifecycle.DreamTests.test_g_replay_supported_only_and_repeated_determinism', ("'off-support'",)),), False),
    'G-RESOLVE': ('STORE', ('tests.scripts.test_store_catalogue.StoreControls.test_g_resolve_exact_l0_l1_and_spam',), (('tests.scripts.test_store_catalogue.StoreControls.test_g_resolve_exact_l0_l1_and_spam', ("'no_match'",)),), False),
    'G-RESOURCE': ('LANG', ('tests.scripts.test_header.HeaderControls.test_g_resource_admission',), (('tests.scripts.test_header.HeaderControls.test_g_resource_admission', ('missing=name',)),), False),
    'G-RESTART-UNCERTAIN': ('ROUTE', ('tests.scripts.test_routing_integration.TestRouting.test_restart_all_lanes_recovery',), (('tests.scripts.test_routing_integration.TestRouting.test_restart_all_lanes_recovery', ('evidence=None',)),), False),
    'G-RISK': ('ACT', ('tests.scripts.test_execution.TestRuntime.test_risk_facts_principal',), (('tests.scripts.test_execution.TestRuntime.test_risk_facts_principal', ("'foreign_principal'",)),), False),
    'G-SCOPE': ('OBS', ('tests.scripts.test_observation_privacy.ObservationPrivacyTests.test_g_scope_complete_turn_and_landmark_containment',), (('tests.scripts.test_observation_privacy.ObservationPrivacyTests.test_g_scope_complete_turn_and_landmark_containment', ("'streaming': True",)),), False),
    'G-SHADOW': ('DREAM', ('tests.scripts.test_dream_lifecycle.DreamTests.test_g_shadow_prediction_only_no_live_capabilities',), (('tests.scripts.test_dream_lifecycle.DreamTests.test_g_shadow_prediction_only_no_live_capabilities', ('live_input_dispatched=True',)),), False),
    'G-SOURCE': ('LANG', ('tests.scripts.test_header.HeaderControls.test_g_source_wire',), (('tests.scripts.test_header.HeaderControls.test_g_source_wire', ("'META:exec'",)),), False),
    'G-SPOOL': ('AUDIT', ('tests.scripts.test_ledger_trace.LedgerTraceTests.test_g_spool',), (('tests.scripts.test_ledger_trace.LedgerTraceTests.test_g_spool', ("'a-poison'",)),), False),
    'G-SUPPORT': ('DREAM', ('tests.scripts.test_dream_lifecycle.DreamTests.test_g_support_independent_cohorts_no_replay_credit',), (('tests.scripts.test_dream_lifecycle.DreamTests.test_g_support_independent_cohorts_no_replay_credit', ('self.eps+(self.eps[0],)',)),), False),
    'G-SUSPEND': ('DREAM', ('tests.scripts.test_dream_lifecycle.DreamTests.test_g_suspend_and_g_classify_scoped_denial_and_reinstatement',), (('tests.scripts.test_dream_lifecycle.DreamTests.test_g_suspend_and_g_classify_scoped_denial_and_reinstatement', ('_t.FailureClass.UNCLASSIFIED',)),), False),
    'G-TICKS': ('FRAME', ('tests.scripts.test_frame_wait.TestFrameWait.test_ticks',), (('tests.scripts.test_frame_wait.TestFrameWait.test_ticks', ('(2, 1)',)),), False),
    'G-UTILITY': ('DREAM', ('tests.scripts.test_dream_lifecycle.DreamTests.test_g_utility_paired_rational_no_regression',), (('tests.scripts.test_dream_lifecycle.DreamTests.test_g_utility_paired_rational_no_regression', ('candidate_cost=missing',)),), False),
    'G-WAIT': ('FRAME', ('tests.scripts.test_frame_wait.TestFrameWait.test_semantics',), (('tests.scripts.test_frame_wait.TestFrameWait.test_semantics', ('_t.Truth.UNKNOWN',)),), False),
    'G-WAIT-PACING': ('FRAME', ('tests.scripts.test_frame_wait.TestFrameWait.test_pacing',), (('tests.scripts.test_frame_wait.TestFrameWait.test_pacing', ("_sample('a', _t.Truth.FALSE)",)),), False),
})


def _s10_require(value, reason):
    if not value:
        raise AssertionError(reason)


_S10_LEGACY_PUBLIC = frozenset(('PolicyRefused', 'parse_envelope', 'parse_adapter_input', 'RouteEvidence',
    'PixelCalibration', 'Decision', 'Admission', 'make_response', 'encode_response', 'client_rc'))


def _s10_source(src, rel):
    """Independent AST plus COMMENT tokens: strings never create definitions or anchors."""
    tree = _ast.parse(src)
    lines = src.splitlines()
    nodes = {}
    public = set()
    def visit(body, prefix=''):
        for node in body:
            if isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef, _ast.ClassDef)):
                start = min([node.lineno] + [d.lineno for d in node.decorator_list])
                nodes[start] = (node.name, prefix + node.name)
                if not prefix and not node.name.startswith('_'):
                    if rel != 'wender/act/proto1_action_policy.py' or node.name not in _S10_LEGACY_PUBLIC:
                        public.add(start)
                if isinstance(node, _ast.ClassDef):
                    visit(node.body, prefix + node.name + '.')
            elif isinstance(node, (_ast.Assign, _ast.AnnAssign)):
                target = node.targets[0] if isinstance(node, _ast.Assign) else node.target
                if isinstance(target, _ast.Name):
                    nodes[node.lineno] = (target.id, prefix + target.id)
    visit(tree.body)
    found, starts = {}, set()
    for token in _s10_tokenize.generate_tokens(_io.StringIO(src).readline):
        if token.type != _s10_tokenize.COMMENT or lines[token.start[0]-1].strip() != token.string:
            continue
        match = _s10_re.fullmatch(r'# @anchor:([A-Za-z0-9_.]+)', token.string)
        if not match:
            continue
        line = token.start[0]
        next_line = line + 1
        while next_line <= len(lines) and (not lines[next_line-1].strip() or lines[next_line-1].lstrip().startswith('#')):
            next_line += 1
        _s10_require(next_line in nodes, 'attachment')
        symbol, qualified = nodes[next_line]
        anchor = match.group(1)
        # Historical constant anchors are aliases; their actual assignment symbol is authoritative.
        if next_line in public or '.' in qualified:
            _s10_require(anchor.rsplit('.', 1)[-1] == symbol, 'symbol')
        _s10_require(anchor not in found and next_line not in starts, 'duplicate')
        found[anchor] = (rel, symbol, line)
        starts.add(next_line)
    _s10_require(public <= starts, 'missing')
    return found


def _s10_expected(root):
    expected = {}
    for rel, module, book, owner, layer, section in _S10_MODULES:
        src = (root / rel).read_text()
        meta = _s10_json.loads(src.splitlines()[0].removeprefix('# @module '))
        _s10_require((meta['id'], meta['book'], meta['owner'], meta['layer']) ==
                     (module, book, owner, layer), 'metadata')
        for anchor, (path, symbol, line) in _s10_source(src, rel).items():
            _s10_require(anchor not in expected, 'duplicate')
            expected[anchor] = (path, symbol, line, module, book, owner, layer, section)
    return expected


def _s10_records(expected, ix):
    indexed = {}
    for row in ix['anchors']:
        _s10_require(row['id'] not in indexed, 'duplicate')
        indexed[row['id']] = row
    for anchor, (path, symbol, line, module, book, owner, layer, section) in expected.items():
        _s10_require(anchor in indexed, 'missing')
        row = indexed[anchor]
        _s10_require((row['file'], row['symbol'], row['line'], row['module_id'], row['stage'], row['owner'], row['layer']) ==
                     (path, symbol, line, module, book, owner, layer), 'coordinates')
        _s10_require(book + '#' + section in row['hb_sections'], 'section')


def _s10_books(expected, hb):
    for anchor, (_, _, _, _, book, _, _, section) in expected.items():
        matches = [s for s in hb['sections'] if s['book'] == book and s['section_anchor'] == section]
        _s10_require(len(matches) == 1 and anchor in matches[0]['code_anchors'], 'book')


def _s10_graph(rows):
    by_id = {m['id']: m for m in rows}
    _s10_require(len(by_id) == len(rows), 'module_duplicate')
    for module in rows:
        for dep in module['deps']:
            _s10_require(dep in by_id, 'dependency_missing')
            _s10_require(by_id[dep]['layer'] <= module['layer'], 'dependency_upward')
    pending, done = set(), set()
    def walk(key):
        _s10_require(key not in pending, 'dependency_cycle')
        if key in done:
            return
        pending.add(key)
        for dep in by_id[key]['deps']:
            walk(dep)
        pending.remove(key)
        done.add(key)
    for key in by_id:
        walk(key)


def _s10_fixture(root):
    path = root / 'wender' / 'scripts' / 'sample.py'
    path.parent.mkdir(parents=True)
    path.write_text('# @module {"id":"wa.scripts.sample","layer":0,"owner":"LEARN","deps":[],"book":"HB-WA-s4-learn"}\n'
                    '# @anchor:wa.scripts.sample.example\ndef example():\n    """A structural fixture."""\n    return 1\n')
    return path


def _s10_static_inventory(required, specs):
    """Validate static routing contracts without running mapped suites."""
    import inspect
    _s10_require(bool(required) and set(specs) == set(required), 'gate_inventory')
    external_absent = []
    for gate, contract in specs.items():
        if type(contract) is not tuple or len(contract) != 4:
            _s10_require(False, 'static_record')
            continue
        owner, ids, fixtures, external = contract
        _s10_require(isinstance(owner, str) and bool(owner) and type(ids) is tuple and
                     type(fixtures) is tuple and type(external) is bool, 'static_record')
        _s10_require(bool(ids) or external, 'test_inventory')
        _s10_require(len(ids) == len(set(ids)) and
                     tuple(test_id for test_id, _ in fixtures) == ids, 'fixture_inventory')
        for test_id, labels in fixtures:
            suite = _unittest.defaultTestLoader.loadTestsFromName(test_id)
            cases = []
            def collect(item):
                if isinstance(item, _unittest.TestSuite):
                    for child in item:
                        collect(child)
                else:
                    cases.append(item)
            collect(suite)
            if not (len(cases) == 1 and cases[0].id() == test_id and
                    isinstance(cases[0], _unittest.TestCase) and
                    cases[0]._testMethodName.startswith('test')):
                _s10_require(False, 'test_loadable')
                continue
            case = cases[0]
            method = getattr(type(case), case._testMethodName, None)
            _s10_require(inspect.isfunction(method), 'test_method')
            source = inspect.getsource(method)
            _s10_require(type(labels) is tuple and bool(labels) and
                         all(isinstance(label, str) and label and label in source for label in labels),
                         'fixture_source')
        if external:
            external_absent.append((gate, owner, 'external_evidence_absent'))
    return tuple(external_absent)


def _s10_import_discrepancies(source, rel, modules, paths):
    """Declare module imports; deferred imports still obey layer direction."""
    meta = _s10_json.loads(source.splitlines()[0][10:])
    package = rel[:-3].split('/')[:-1]
    discrepancies = []
    tree = _ast.parse(source)
    static_imports = set()
    def collect_static(node):
        if isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef, _ast.Lambda)):
            return
        if isinstance(node, (_ast.Import, _ast.ImportFrom)):
            static_imports.add(node)
        for child in _ast.iter_child_nodes(node):
            collect_static(child)
    collect_static(tree)
    for node in _ast.walk(tree):
        if isinstance(node, _ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, _ast.ImportFrom):
            base = '.'.join(package[:len(package)-node.level+1] + ([node.module] if node.module else [])) if node.level else (node.module or '')
            names = [base] if base in paths else [base+'.'+a.name for a in node.names]
        else:
            continue
        for name in names:
            if name in paths and paths[name]['id'] != meta['id']:
                dep = paths[name]['id']
                _s10_require(modules[dep]['layer'] <= meta['layer'], 'dependency_upward')
                if node in static_imports and dep not in meta['deps']:
                    discrepancies.append((rel, node.lineno, dep))
    _s10_require(not discrepancies, 'dependency_undeclared: ' + repr(discrepancies))


def _s10_response(response, raw, *, exact=False):
    """A response must correspond to the requested raw identities, including clipping."""
    _s10_require(bool(response['r']), 'response_empty')
    _s10_require(not exact or len(response['r']) == 1, 'response_identity')
    for row in response['r']:
        matches = [item for item in raw if all(row.get(key) == _s10_router.clip(item[key], _s10_router.LIM[key])
                   for key in ('id', 'file', 'stage')) and row.get('line') == item['line']]
        _s10_require(len(matches) == 1, 'response_identity')
        item = matches[0]
        for key, size in _s10_router.LIM.items():
            if key in row and key in item and key != 'why':
                _s10_require(row[key] == _s10_router.clip(item[key], size), 'response_clip')
        if exact:
            _s10_require(row.get('why') == ['exact_anchor'], 'response_identity')



# @anchor:wa.tests.scripts.test_routing_integration.TestS10Routing
class TestS10Routing(_unittest.TestCase):
    # @anchor:wa.tests.scripts.test_routing_integration.TestS10Routing.setUpClass
    @classmethod
    def setUpClass(cls):
        cls.expected = _s10_expected(_S10_ROOT)
        cls.ix = _s10_builder.build_memory_index(_S10_ROOT)

    # @anchor:wa.tests.scripts.test_routing_integration.TestS10Routing.test_g_anchor_inventory
    def test_g_anchor_inventory(self):
        source = '# @anchor:wa.fixture.good\ndef good():\n    return 1\n'
        self.assertEqual(_s10_source(source, 'fixture.py'), {'wa.fixture.good': ('fixture.py', 'good', 1)})
        for bad in (source.replace('# @anchor:wa.fixture.good\n', ''),
                    source + source, '"""\n# @anchor:wa.fixture.good\n"""\ndef good(): pass\n',
                    source.replace('def good', 'def unrelated')):
            with self.subTest(source=bad), self.assertRaises(AssertionError):
                _s10_source(bad, 'fixture.py')
        _s10_records(self.expected, self.ix)

    # @anchor:wa.tests.scripts.test_routing_integration.TestS10Routing.test_g_anchor_coordinates
    def test_g_anchor_coordinates(self):
        _s10_records(self.expected, self.ix)
        anchor = next(iter(self.expected))
        for field, value in (('file', 'unrelated.py'), ('symbol', 'unrelated'), ('line', -1),
                             ('stage', 'HB-WA-s2-act'), ('owner', 'WRONG'), ('layer', -1)):
            altered = dict(self.ix, anchors=[dict(r, **{field:value}) if r['id'] == anchor else r for r in self.ix['anchors']])
            with self.subTest(field=field), self.assertRaisesRegex(AssertionError, 'coordinates'):
                _s10_records(self.expected, altered)
        altered = dict(self.ix, anchors=[dict(r, hb_sections=[]) if r['id'] == anchor else r for r in self.ix['anchors']])
        with self.assertRaisesRegex(AssertionError, 'section'):
            _s10_records(self.expected, altered)
        for anchor, expected in self.expected.items():
            response, rc = _s10_router.lookup_anchor(self.ix, anchor)
            self.assertEqual(rc, 0)
            _s10_response(response, [r for r in self.ix['anchors'] if r['id'] == anchor], exact=True)
            # Full ID/coordinates are checked in raw records; response fields may be clipped.
        response, rc = _s10_router.route_query(self.ix, 'FrameWait', stage_filter='HB-WA-s2-act', no_abstain=True)
        self.assertEqual(rc, 0)
        intended = [r for r in self.ix['anchors'] if r['file'] == 'wender/scripts/frame_wait.py' and r['symbol'] == 'FrameWait']
        self.assertTrue(any(r['file'] == 'wender/scripts/frame_wait.py' for r in response['r']))
        ranked, _ = _s10_router.rank_query(self.ix, 'FrameWait', 'HB-WA-s2-act')
        _s10_response(response, ranked[:5])
        exact, rc = _s10_router.lookup_anchor(self.ix, intended[0]['id'])
        _s10_response(exact, intended, exact=True)
        with self.assertRaisesRegex(AssertionError, 'response_identity'):
            _s10_response(dict(exact, r=exact['r'] * 2), intended, exact=True)
        for field, value in (('file', 'unrelated.py'), ('stage', 'HB-WA-s1-perceive'),
                             ('id', 'wa.unrelated.neighbour'), ('symbol', 'Unrelated')):
            altered = dict(exact, r=[dict(exact['r'][0], **{field: value})])
            with self.subTest(lexical_neighbour=field), self.assertRaises(AssertionError):
                _s10_response(altered, intended, exact=True)

    # @anchor:wa.tests.scripts.test_routing_integration.TestS10Routing.test_g_anchor_book_coverage
    def test_g_anchor_book_coverage(self):
        _s10_books(self.expected, self.ix['hb'])
        anchor, expected = next(iter(self.expected.items()))
        for wrong_book in (False, True):
            hb = _s10_json.loads(_s10_json.dumps(self.ix['hb']))
            section = next(s for s in hb['sections'] if s['book'] == expected[4] and s['section_anchor'] == expected[7])
            if wrong_book:
                section['book'] = 'HB-WA-s2-act'
            else:
                section['code_anchors'].remove(anchor)
            with self.assertRaisesRegex(AssertionError, 'book'):
                _s10_books(self.expected, hb)

    # @anchor:wa.tests.scripts.test_routing_integration.TestS10Routing.test_g_anchor_dependency_controls
    def test_g_anchor_dependency_controls(self):
        rows = [dict(id='base',layer=0,deps=[]), dict(id='consumer',layer=1,deps=['base'])]
        _s10_graph(rows)
        for bad in ([rows[1]], rows + [rows[0]],
                    [dict(id='base',layer=2,deps=[]), rows[1]],
                    [dict(id='a',layer=0,deps=['b']),dict(id='b',layer=0,deps=['a'])]):
            with self.assertRaises(AssertionError):
                _s10_graph(bad)

        modules = {'wa.base': dict(id='wa.base', layer=0, deps=[])}
        paths = {'wender.base': modules['wa.base']}
        source = '# @module {"id":"wa.consumer","layer":1,"deps":["wa.base"]}\nimport wender.base\n'
        _s10_import_discrepancies(source, 'wender/consumer.py', modules, paths)
        omitted_real_import = source.replace('["wa.base"]', '[]')
        with self.assertRaisesRegex(AssertionError, 'dependency_undeclared'):
            _s10_import_discrepancies(omitted_real_import, 'wender/consumer.py', modules, paths)

        # Deferred cycle-breaking imports are not static DAG edges.
        header = '# @module {"id":"wa.consumer","layer":1,"deps":[]}\n'
        for body in ('def deferred():\n    import wender.base\n',
                     'async def deferred():\n    from wender import base\n',
                     'class Container:\n    def deferred(self):\n        import wender.base\n'):
            _s10_import_discrepancies(header + body, 'wender/consumer.py', modules, paths)
        # Must-FAIL: module-level compound statements cannot hide real imports.
        for body in ('if True:\n    import wender.base\n',
                     'try:\n    from wender import base\nexcept ImportError:\n    pass\n',
                     'with manager():\n    import wender.base\n',
                     'class Container:\n    import wender.base\n'):
            with self.subTest(module_import=body), self.assertRaisesRegex(AssertionError, 'dependency_undeclared'):
                _s10_import_discrepancies(header + body, 'wender/consumer.py', modules, paths)
        # Must-FAIL: a deferred import may not cross upward through layers.
        higher = {'wa.base': dict(id='wa.base', layer=2, deps=[])}
        with self.assertRaisesRegex(AssertionError, 'dependency_upward'):
            _s10_import_discrepancies(header + 'def deferred():\n    import wender.base\n',
                                     'wender/consumer.py', higher, {'wender.base': higher['wa.base']})

    # @anchor:wa.tests.scripts.test_routing_integration.TestS10Routing.test_g_anchor_dependencies
    def test_g_anchor_dependencies(self):
        modules, paths = {}, {}
        for path in sorted((_S10_ROOT/'wender').rglob('*.py')):
            lines = path.read_text().splitlines()
            if lines and lines[0].startswith('# @module '):
                meta = _s10_json.loads(lines[0][10:])
                self.assertNotIn(meta['id'], modules)
                modules[meta['id']] = meta
                paths[path.relative_to(_S10_ROOT).as_posix()[:-3].replace('/', '.')] = meta
        for rel, *_ in _S10_MODULES:
            _s10_import_discrepancies((_S10_ROOT/rel).read_text(), rel, modules, paths)
        source = '# @module {"id":"wa.fixture","layer":2,"deps":["wa.scripts.types"]}\nfrom wender.scripts import types\n'
        _s10_import_discrepancies(source, 'wender/fixture.py', modules, paths)
        omitted_real_import = source.replace('["wa.scripts.types"]', '[]')
        with self.assertRaisesRegex(AssertionError, 'dependency_undeclared'):
            _s10_import_discrepancies(omitted_real_import, 'wender/fixture.py', modules, paths)
        needed = set()
        def include(key):
            self.assertIn(key, modules)
            if key not in needed:
                needed.add(key)
                for dep in modules[key]['deps']:
                    include(dep)
        for row in _S10_MODULES:
            include(row[1])
        _s10_graph([modules[key] for key in sorted(needed)])
        # Deferred catalogue -> resolver imports obey layers but are not static edges.

    # @anchor:wa.tests.scripts.test_routing_integration.TestS10Routing.test_g_anchor_stale_index
    def test_g_anchor_stale_index(self):
        for mode in ('add', 'delete', 'source', 'payload', 'book'):
            with self.subTest(mode=mode), _s10_tempfile.TemporaryDirectory() as directory:
                root = _Path(directory)
                source = _s10_fixture(root)
                if mode == 'book':
                    hb = root/'hyperbooks'
                    hb.mkdir()
                    (hb/'COMPANION_FORMAT.json').write_text('{"expected_books":["HB-WA-s4-learn"]}')
                    book = hb/'HB-WA-s4-learn'
                    book.mkdir()
                    (book/'book.md').write_text('Structural book\n')
                    (book/'mathlinks.jsonl').write_text('')
                    (book/'manifest.json').write_text(_s10_seal.dump(_s10_seal.manifest_for(book)))
                out = root/'index'
                _s10_builder.build(root, out)
                _s10_router.load_index(root, out)
                if mode == 'add':
                    (source.parent/'extra.py').write_text('# Additional passive source\n')
                elif mode == 'delete':
                    source.unlink()
                elif mode == 'source':
                    source.write_text(source.read_text()+'\n# Changed\n')
                elif mode == 'book':
                    (book/'book.md').write_text('Changed structural book\n')
                else:
                    (out/'post.json').write_text('{}\n')
                with self.assertRaises(_s10_router.IndexError):
                    _s10_router.load_index(root, out)
                with _patch('sys.stdout', _io.StringIO()):
                    self.assertEqual(_s10_builder.main(['--root',str(root),'--out',str(out),'--check-index']),5)

    # @anchor:wa.tests.scripts.test_routing_integration.TestS10Routing.test_g_anchor_determinism
    def test_g_anchor_determinism(self):
        with _s10_tempfile.TemporaryDirectory() as directory:
            root = _Path(directory)
            source = _s10_fixture(root)
            _s10_builder.build(root,root/'first')
            _s10_builder.build(root,root/'second')
            for name in _S10_OUTPUTS:
                self.assertEqual((root/'first'/name).read_bytes(),(root/'second'/name).read_bytes())
            source.write_text(source.read_text().replace('return 1','return 2'))
            _s10_builder.build(root,root/'changed')
            self.assertNotEqual((root/'first/manifest.json').read_bytes(),(root/'changed/manifest.json').read_bytes())
        for name, payload in self.ix['files'].items():
            self.assertLessEqual(len(payload), _s10_builder.CAP)
        self.assertEqual(set(self.ix['files']), _S10_OUTPUTS)

    # @anchor:wa.tests.scripts.test_routing_integration.TestS10Routing.test_g_anchor_seals
    def test_g_anchor_seals(self):
        for book in sorted({row[2] for row in _S10_MODULES} | {'HB-WA-a0-arch'}):
            directory = _S10_ROOT/'hyperbooks'/book
            self.assertEqual((directory/'manifest.json').read_text(),_s10_seal.dump(_s10_seal.manifest_for(directory)))
        with _s10_tempfile.TemporaryDirectory() as directory:
            book = _Path(directory)
            (book/'book.md').write_text('sealed')
            seal = _s10_seal.dump(_s10_seal.manifest_for(book))
            (book/'book.md').write_text('altered')
            self.assertNotEqual(seal,_s10_seal.dump(_s10_seal.manifest_for(book)))

    # @anchor:wa.tests.scripts.test_routing_integration.TestS10Routing.test_g_gate_schema_controls
    def test_g_gate_schema_controls(self):
        gate = 'G-ANCHOR'
        good = {gate: _S10_GATE_SPECS[gate]}
        self.assertEqual(_s10_static_inventory({gate}, good), ())
        owner, ids, fixtures, external = good[gate]
        deleted_test = ids[0] + '_deleted'
        missing_label = ((ids[0], ('MUST_FAIL_LABEL_ABSENT',)),)
        fabricated_zero_input_record = _m.GateResult(gate, 'negative', 'reject', 'reject', 'fake', 0, (), ids[0])
        controls = (
            ('test_loadable', {gate:(owner,(deleted_test,),((deleted_test, fixtures[0][1]),),external)}),
            ('fixture_source', {gate:(owner,ids,missing_label,external)}),
            ('gate_inventory', {}),
            ('fixture_inventory', {gate:(owner,ids,(),external)}),
            ('static_record', {gate:fabricated_zero_input_record}),
        )
        for label, broken in controls:
            with self.subTest(fixture=label), self.assertRaisesRegex(AssertionError, label):
                _s10_static_inventory({gate}, broken)

    # @anchor:wa.tests.scripts.test_routing_integration.TestS10Routing.test_g_owner_evidence_controls
    def test_g_owner_evidence_controls(self):
        # Owner evidence is a static mapping; a completion object cannot replace it.
        self.test_g_gate_schema_controls()

    # @anchor:wa.tests.scripts.test_routing_integration.TestS10Routing.test_g_owner_evidence_mutants
    def test_g_owner_evidence_mutants(self):
        for test in ('test_g_owner_evidence_controls', 'test_g_owner_execution_controls',
                     'test_g_inventory_no_false_pass'):
            baseline = _unittest.TestResult()
            TestS10Routing(test).run(baseline)
            self.assertTrue(baseline.wasSuccessful(), baseline.errors or baseline.failures)
            mutant = _unittest.TestResult()
            with _patch(__name__+'._s10_static_inventory', return_value=()):
                TestS10Routing(test).run(mutant)
            self.assertTrue(mutant.failures, 'static inventory bypass survived')
            print('MUTANT S10 static_inventory ' + test + ' KILLED')

    # @anchor:wa.tests.scripts.test_routing_integration.TestS10Routing.test_g_owner_execution_controls
    def test_g_owner_execution_controls(self):
        # Loading methods must not invoke their bodies or runtime evidence hooks.
        with _patch.object(TestRouting, 'run', side_effect=AssertionError('owner execution forbidden')):
            self.assertEqual(_s10_static_inventory({'G-CALIBRATE'},
                {'G-CALIBRATE': _S10_GATE_SPECS['G-CALIBRATE']}), ())
        self.test_g_gate_schema_controls()

    # @anchor:wa.tests.scripts.test_routing_integration.TestS10Routing.test_g_gate_inventory
    def test_g_gate_inventory(self):
        external = _s10_static_inventory(_S10_GATES, _S10_GATE_SPECS)
        self.assertEqual({row[0] for row in external}, {'G-FRAME', 'G-RELEASE'})
        with self.assertRaises(TypeError):
            _S10_GATE_SPECS['G-INVENTED'] = ()
        print('S10 external VE acceptance: AUSENTE', external)

    # @anchor:wa.tests.scripts.test_routing_integration.TestS10Routing.test_g_inventory_no_false_pass
    def test_g_inventory_no_false_pass(self):
        self.test_g_gate_schema_controls()
        with self.assertRaisesRegex(AssertionError, 'gate_inventory'):
            _s10_static_inventory(set(), {})

    # @anchor:wa.tests.scripts.test_routing_integration.TestS10Routing.test_g_anchor_privacy
    def test_g_anchor_privacy(self):
        with _s10_tempfile.TemporaryDirectory() as directory:
            root = _Path(directory)
            source = _s10_fixture(root)
            source.write_text(source.read_text().replace('A structural fixture.',
                'Safe structural fixture. sk-' 'S10SyntheticPrivateCanary'))
            ix = _s10_builder.build_memory_index(root)
            payload = b''.join(ix['files'].values())
            self.assertNotIn(b'sk-' b'S10SyntheticPrivateCanary',payload)
            self.assertIn(b'Safe structural fixture',payload)

    # @anchor:wa.tests.scripts.test_routing_integration.TestS10Routing.test_g_test_anchor_routes
    def test_g_test_anchor_routes(self):
        # Positive: both public scanners include the serialized ROUTE test file.
        with _s10_tempfile.TemporaryDirectory() as directory:
            root = _Path(directory)
            path = root/'tests/scripts/test_routing_integration.py'
            path.parent.mkdir(parents=True)
            path.write_text('# @module {"id":"wa.tests.fixture","layer":0,"owner":"LEARN","deps":[],"book":"HB-WA-s3-remember"}\n'
                            '# @anchor:wa.tests.fixture.accept\ndef accept(): return True\n')
            ix = _s10_builder.build_memory_index(root)
            response, rc = _s10_router.lookup_anchor(ix, 'wa.tests.fixture.accept')
            self.assertEqual(rc, 0, 'test source excluded from index')
            self.assertEqual(response['r'][0]['file'], 'tests/scripts/test_routing_integration.py')
            with _patch.object(_s10_anchors, 'ROOT', root):
                rows, _ = _s10_anchors.scan()
            self.assertEqual([r['id'] for r in rows], ['wa.tests.fixture.accept'])
            # Must-FAIL: an unregistered source path cannot masquerade as ROUTE.
            path.rename(path.with_name('unregistered.py'))
            missing = _s10_builder.build_memory_index(root)
            _, rc = _s10_router.lookup_anchor(missing, 'wa.tests.fixture.accept')
            self.assertNotEqual(rc, 0)
        expected = _s10_source((_S10_ROOT/'tests/scripts/test_routing_integration.py').read_text(),
                               'tests/scripts/test_routing_integration.py')
        for anchor in expected:
            if '.TestS10Routing' in anchor or '._S10EvidenceControl' in anchor:
                row = next(r for r in self.ix['anchors'] if r['id'] == anchor)
                self.assertIn('HB-WA-s3-remember#s10-routing-acceptance', row['hb_sections'])

    # @anchor:wa.tests.scripts.test_routing_integration.TestS10Routing.test_g_anchor_removal_mutants
    def test_g_anchor_removal_mutants(self):
        # Every local mapping is independently discriminating through the public test.
        baseline = _unittest.TestResult()
        TestS10Routing('test_g_gate_inventory').run(baseline)
        self.assertTrue(baseline.wasSuccessful(), baseline.errors or baseline.failures)
        for gate, spec in _S10_GATE_SPECS.items():
            owner, ids, fixtures, external = spec
            changes = [('deleted_gate', {g:v for g,v in _S10_GATE_SPECS.items() if g != gate})]
            if ids:
                broken = dict(_S10_GATE_SPECS)
                broken[gate] = (owner, ids, tuple((test_id, ('MUST_FAIL_LABEL_REMOVED',))
                                                for test_id in ids), external)
                changes.append(('missing_label', broken))
            for label, mapping in changes:
                result = _unittest.TestResult()
                with _patch(__name__+'._S10_GATE_SPECS', _types.MappingProxyType(mapping)):
                    TestS10Routing('test_g_gate_inventory').run(result)
                self.assertTrue(result.failures, gate + ': ' + label + ' survived')
                self.assertFalse(result.errors, result.errors)
                print('MUTANT S10 static_mapping ' + gate + ' ' + label + ' KILLED')
        controls = [('missing','test_g_anchor_inventory'),('duplicate','test_g_anchor_inventory'),
            ('symbol','test_g_anchor_inventory'),('coordinates','test_g_anchor_coordinates'),
            ('section','test_g_anchor_coordinates'),('book','test_g_anchor_book_coverage'),
            ('gate_inventory','test_g_gate_schema_controls'),('fixture_inventory','test_g_gate_schema_controls'),
            ('static_record','test_g_gate_schema_controls'),('test_loadable','test_g_gate_schema_controls'),
            ('fixture_source','test_g_gate_schema_controls'),
            ('dependency_undeclared','test_g_anchor_dependency_controls'),
            ('dependency_upward','test_g_anchor_dependency_controls'),
            ('response_identity','test_g_anchor_coordinates'),('response_clip','test_g_anchor_coordinates')]
        original = _s10_require
        for removed, test in controls:
            with self.subTest(removed=removed):
                baseline = _unittest.TestResult()
                TestS10Routing(test).run(baseline)
                self.assertTrue(baseline.wasSuccessful(),baseline.errors or baseline.failures)
                def bypass(value, reason):
                    if not reason.startswith(removed):
                        original(value, reason)
                result = _unittest.TestResult()
                with _patch(__name__+'._s10_require', bypass):
                    TestS10Routing(test).run(result)
                self.assertTrue(result.failures, 'removal survived: '+removed)
                self.assertFalse(result.errors,result.errors)
                print('MUTANT S10 '+removed+' KILLED')
        import inspect
        namespace = dict(globals())
        mutated = inspect.getsource(_s10_import_discrepancies).replace(
            "node in static_imports and dep not in meta['deps']", "dep not in meta['deps']")
        exec(compile(mutated, '<deferred-import-mutant>', 'exec'), namespace)
        result = _unittest.TestResult()
        with _patch(__name__+'._s10_import_discrepancies', namespace['_s10_import_discrepancies']):
            TestS10Routing('test_g_anchor_dependency_controls').run(result)
        self.assertTrue(result.failures, 'deferred-import static-edge mutant survived')
        self.assertFalse(result.errors, result.errors)
        print('MUTANT S10 deferred_import_static_edge KILLED')
        additional = [(_sys.modules[__name__], '_s10_graph', lambda rows:None,
                       'test_g_anchor_dependency_controls'),
                      (_s10_builder, 'SRC_GLOBS', tuple(g for g in _s10_builder.SRC_GLOBS if not g.startswith('tests/')), 'test_g_test_anchor_routes'),
                      (_s10_anchors, 'SRC_GLOBS', tuple(g for g in _s10_anchors.SRC_GLOBS if not g.startswith('tests/')), 'test_g_test_anchor_routes')]
        cached = {}
        original_load = _s10_router.load_index
        def stale(root, out=None):
            key = str(root)
            if key not in cached:
                cached[key] = original_load(root,out)
            return cached[key]
        additional.append((_s10_router,'load_index',stale,'test_g_anchor_stale_index'))
        manifests = {}
        original_seal = _s10_seal.manifest_for
        def stale_seal(book):
            key = str(book)
            if key not in manifests:
                manifests[key] = original_seal(book)
            return manifests[key]
        additional.append((_s10_seal,'manifest_for',stale_seal,'test_g_anchor_seals'))
        indexes = {}
        original_build = _s10_builder.build_memory_index
        def stale_build(root):
            key = str(root)
            if key not in indexes:
                indexes[key] = original_build(root)
            return indexes[key]
        additional.append((_s10_builder,'build_memory_index',stale_build,'test_g_anchor_determinism'))
        additional.append((_s10_builder,'_text',lambda value:value,'test_g_anchor_privacy'))
        for owner, name, replacement, test in additional:
            baseline = _unittest.TestResult()
            TestS10Routing(test).run(baseline)
            self.assertTrue(baseline.wasSuccessful(), baseline.errors or baseline.failures)
            result = _unittest.TestResult()
            with _patch.object(owner,name,replacement):
                TestS10Routing(test).run(result)
            self.assertTrue(result.failures, 'removal survived: '+name)
            self.assertFalse(result.errors,result.errors)
            print('MUTANT S10 '+name+' KILLED')


# @anchor:wa.tests.scripts.test_routing_integration._S10EvidenceControl
class _S10EvidenceControl(_unittest.TestCase):
    # @anchor:wa.tests.scripts.test_routing_integration._S10EvidenceControl.test_controls
    def test_controls(self):
        with _s10_tempfile.TemporaryDirectory() as directory:
            root = _Path(directory)
            _s10_fixture(root)
            ix = _s10_builder.build_memory_index(root)
            good, rc = _s10_router.lookup_anchor(ix, 'wa.scripts.sample.example')
            self.assertEqual(rc, 0)
            self.assertEqual(good['r'][0]['file'], 'wender/scripts/sample.py')
            bad, rc = _s10_router.lookup_anchor(ix, 'wa.scripts.sample.absent')
            self.assertNotEqual(rc, 0)
            self.assertEqual(bad['abstain']['reason'], 'anchor_not_found')
