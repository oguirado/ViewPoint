# @module {"id":"wa.tests.scripts.test_language","layer":2,"owner":"LEARN","deps":["wa.scripts.types","wa.tests.scripts.support"],"book":"HB-WA-s4-learn"}
"""S0 contract predicates, rejection fixtures and positive controls.

Owner registries/fences are instrumented fakes: these controls certify shape
and boundary vocabulary, not downstream production authorization/evaluation.
"""
import ast as _ast
import dataclasses as _dc
import inspect as _inspect
import json as _json
from pathlib import Path as _Path
import subprocess as _subprocess
import sys as _sys
import tempfile as _tempfile
import unittest as _unittest
from typing import get_type_hints as _hints
from wender.scripts import types as _t
from tests.scripts.support import (make_fixture as _fixture, IdentityRegistry as _Registry,
                                    FakeClock as _Clock, CountedCallback as _Callback,
                                    LockTrace as _Locks, TrustedAuthorityChecker as _AuthorityChecker,
                                    verify_routing_manifest as _verify_routing, ReservationOwner as _ReservationOwner)
from tests.scripts.support import TrustedExportReceiptChecker as _ExportChecker

_REPO = _Path(__file__).resolve().parents[2]


# @anchor:wa.tests.scripts.test_language.ContractControls
class ContractControls(_unittest.TestCase):
    # @anchor:wa.tests.scripts.test_language.ContractControls.setUp
    def setUp(self):
        self.f = _fixture()

    def _reject(self, reason, operation):
        with self.assertRaises(_t.ContractError) as caught:
            operation()
        self.assertIs(caught.exception.reason, reason)
        self.assertEqual(str(caught.exception), reason.value)

    def _replan(self, plan, **changes):
        definition = plan.definition
        nodes = changes.get('nodes', plan.nodes)
        slots = changes.get('slots', plan.slots)
        selectors = tuple(dict.fromkeys(
            tuple(query.selector for query in definition.queries)
            + tuple(node.effect.selector for node in nodes if node.effect)))
        definition = _dc.replace(definition, nodes=nodes, slots=slots,
            symbols=changes.get('symbols', plan.symbols), selectors=selectors,
            header=_dc.replace(definition.header, arguments=slots))
        return _dc.replace(plan, definition=definition, **changes)

    # @anchor:wa.tests.scripts.test_language.ContractControls.test_immutability_and_ingress_copy
    def test_immutability_and_ingress_copy(self):
        # Predicate: no caller-owned container survives the explicit ingress copy.
        for mutable in ({'secret': 'canary'}, ['canary'], ({'nested': []},)):
            self._reject(_t.Reason.TYPE, lambda: _t.MetadataEntry('name', mutable))
        external = {'name': ['static', {'nested': 'approved'}]}
        copied = _t.freeze_entries(external.items())
        external['name'][1]['nested'] = 'mutated'
        self.assertEqual(copied[0].value[1][0].value, 'approved')
        with self.assertRaises(_dc.FrozenInstanceError):
            self.f['plan'].nodes = ()
        self.assertFalse(hasattr(self.f['plan'], '__dict__'))
        self._reject(_t.Reason.TYPE, lambda: _t.MetadataEntry('bad', '\ud800'))
        self._reject(_t.Reason.SCHEMA, lambda: _t.freeze_entries([('a', 1), ('ａ', 2)]))
        self._reject(_t.Reason.SCHEMA, lambda: _t.freeze_entries([('a', 1), ('a', 2)]))
        self._reject(_t.Reason.SCHEMA, lambda: _t.freeze_entries(
            [('z', 1), ('a', 2), ('a', 3)]))
        header = self.f['plan'].definition.header
        self.assertEqual(_dc.replace(header), header)
        self.assertIsInstance(header.supplied_pins, _t.ArtifactIdentity)
        self._reject(_t.Reason.TYPE, lambda: _dc.replace(header,
            supplied_pins=(_t.MetadataEntry('z', 1), _t.MetadataEntry('a', 2))))

    # @anchor:wa.tests.scripts.test_language.ContractControls.test_plan_structure_requires_checkpoint_and_terminal
    def test_plan_structure_requires_checkpoint_and_terminal(self):
        effect, plan = self.f['effect'], self.f['plan']
        self.assertEqual(plan.nodes[0].effect.postcondition, self.f['post'])
        self.assertIs(plan.nodes[-1].terminal, plan.terminal_goal)
        self._reject(_t.Reason.TYPE, lambda: _dc.replace(plan.nodes[0], kind='EFFECT'))
        self._reject(_t.Reason.TYPE, lambda: _dc.replace(effect, postcondition=None))
        self._reject(_t.Reason.MISSING, lambda: _dc.replace(effect, postcondition=self.f['entry']))
        self._reject(_t.Reason.MISSING, lambda: _dc.replace(plan, nodes=plan.nodes[:1]))
        self._reject(_t.Reason.TYPE, lambda: _dc.replace(plan, terminal_goal=None))
        self._reject(_t.Reason.MISSING, lambda: _dc.replace(plan.nodes[0], predicate=self.f['entry']))
        self._reject(_t.Reason.BOUNDS, lambda: _dc.replace(plan, limits=_dc.replace(plan.limits, max_nodes=1)))
        self._reject(_t.Reason.SCHEMA, lambda: _dc.replace(plan, nodes=(plan.nodes[0], plan.nodes[0], plan.nodes[1])))
        self._reject(_t.Reason.TYPE, lambda: _dc.replace(self.f['post'], operator='TRUE'))

    # @anchor:wa.tests.scripts.test_language.ContractControls.test_exact_keys_integers_bounds_and_symbolic_slots
    def test_exact_keys_integers_bounds_and_symbolic_slots(self):
        for bad in (True, 1.0, '1'):
            self._reject(_t.Reason.TYPE, lambda: _dc.replace(self.f['authority'], policy=bad))
        self._reject(_t.Reason.BOUNDS, lambda: _dc.replace(self.f['authority'], policy=-1))
        values = [('owner', 'test'), ('incarnation', 'test'), ('token', 'private')]
        self.assertEqual(_t.record_from_entries(_t.PrivateRef, values).owner, 'test')
        for bad in (values[:-1], values + [('extra', 'x')], values + [('ｏｗｎｅｒ', 'x')]):
            self._reject(_t.Reason.SCHEMA, lambda: _t.record_from_entries(_t.PrivateRef, bad))
        arg = _t.ArgSpec('input', _t.ValueKind.TEXT, True, 1, 16, _t.Sensitivity.PRIVATE,
                         _t.Purpose.INPUT, 'trusted-actuator', 'form', 'norm-test')
        self._reject(_t.Reason.BOUNDS, lambda: _dc.replace(arg, minimum=17))
        self._reject(_t.Reason.SCHEMA, lambda: _dc.replace(self.f['plan'], slots=(arg, _dc.replace(arg, name='ｉｎｐｕｔ'))))
        slot = _t.SlotDescriptor('input', _t.ValueKind.TEXT, _t.Purpose.INPUT,
                                 self.f['handle'], 'episode', self.f['scope'], 'form', 'trusted-actuator', None)
        selector = _dc.replace(self.f['selector'], action=_t.ActionKind.TYPE)
        effect = _dc.replace(self.f['effect'], action=_t.ActionKind.TYPE, selector=selector, arguments=(slot,))
        self._reject(_t.Reason.TYPE, lambda: _dc.replace(effect, arguments=('literal private text',)))
        self._reject(_t.Reason.FOREIGN, lambda: _dc.replace(effect, arguments=(_dc.replace(slot, purpose=_t.Purpose.HARVEST),)))
        self._reject(_t.Reason.FOREIGN, lambda: _dc.replace(effect, arguments=(_dc.replace(slot, scope=_dc.replace(slot.scope, principal='other')),)))
        registry, boundary = _Registry(self.f['handle'], 7), _Callback()
        registry.register(slot)
        payload = 'private-test-canary'
        self._reject(_t.Reason.FOREIGN, lambda: registry.check(_dc.replace(slot), self.f['handle'], 7))
        self.assertEqual(boundary.calls, [])
        if registry.check(slot, self.f['handle'], 7):
            boundary(payload)
        self.assertEqual(boundary.calls, [(payload,)])
        self.assertNotIn(payload, repr(effect))

    # @anchor:wa.tests.scripts.test_language.ContractControls.test_missingness_and_tristate
    def test_missingness_and_tristate(self):
        for truth in _t.Truth:
            result = _t.PredicateResult(truth, _t.Reason.MISSING if truth is _t.Truth.UNKNOWN else None)
            self.assertIs(_t.Truth(result.value.value), truth)
        self._reject(_t.Reason.MISSING, lambda: _t.PredicateResult(_t.Truth.FALSE, _t.Reason.MISSING))
        self._reject(_t.Reason.MISSING, lambda: bool(_t.Truth.UNKNOWN))
        self._reject(_t.Reason.TYPE, lambda: _t.PredicateResult(False, None))
        self._reject(_t.Reason.MISSING, lambda: _t.MissingRef(None, None))
        self._reject(_t.Reason.MISSING, lambda: _t.MeasuredValue(0, 'bytes', _t.MissingRef(None, _t.Reason.MISSING), None))
        unknown = _t.PredicateResult(_t.Truth.UNKNOWN, _t.Reason.MISSING)
        # Contract expectation only: S1 owns evaluation of NOT, no Python coercion.
        negation = {_t.Truth.TRUE: _t.Truth.FALSE, _t.Truth.FALSE: _t.Truth.TRUE,
                    _t.Truth.UNKNOWN: _t.Truth.UNKNOWN}
        self.assertIs(negation[unknown.value], _t.Truth.UNKNOWN)
        self.assertEqual(_t.MeasuredValue(0, 'bytes', _t.MissingRef('counter', None), None).value, 0)

    # @anchor:wa.tests.scripts.test_language.ContractControls.test_registry_identity_incarnation_and_generation
    def test_registry_identity_incarnation_and_generation(self):
        plan, owner = self.f['plan'], self.f['handle']
        registry = _Registry(owner, 7)
        registry.register(plan)
        self.assertTrue(registry.check(plan, owner, 7))
        self.assertEqual(_dc.replace(plan), plan)
        self._reject(_t.Reason.FOREIGN, lambda: registry.check(_dc.replace(plan), owner, 7))
        self._reject(_t.Reason.FOREIGN, lambda: registry.check(plan, _dc.replace(owner, incarnation='restart'), 7))
        self._reject(_t.Reason.STALE, lambda: registry.check(plan, owner, 8))
        registry.generation = 8
        self._reject(_t.Reason.FOREIGN, lambda: registry.check(plan, owner, 8))
        self.assertNotIn(owner.token, repr(plan))

    # @anchor:wa.tests.scripts.test_language.ContractControls.test_scope_and_authority_are_exact
    def test_scope_and_authority_are_exact(self):
        f = self.f
        binding = _t.TargetBinding(f['selector'], f['handle'], f['observation'], f['witness'],
                                   f['identity'], 'effect', f['scope'], f['authority'], f['handle'])
        self.assertEqual(binding.scope, f['scope'])
        for field in ('principal', 'tenant', 'browser_profile_incarnation', 'target_frame_origin', 'top_level_origin'):
            scope = _dc.replace(f['scope'], **{field: 'other'})
            self._reject(_t.Reason.STALE, lambda: _dc.replace(binding, scope=scope))
        self._reject(_t.Reason.STALE, lambda: _dc.replace(binding, authority=_dc.replace(f['authority'], scope=2)))
        # Predicate: trusted current scope and every revision gate the callback.
        current = _dc.replace(f['authority'], calibration_certificate=1, calibration_reason=None)
        intent = _t.StepIntent(f['identity'], 'effect', 0, f['effect'].action,
                               f['selector'], (), f['post'], f['scope'], current)
        callback = _Callback()
        checker = _AuthorityChecker(f['scope'], current, callback)
        for field in ('policy', 'capability_set', 'schema', 'scope',
                      'environment_compatibility', 'calibration_certificate'):
            changed = _dc.replace(current, **{field: 2})
            # Advancing trusted current state keeps the request itself well shaped.
            checker.authority = changed
            self._reject(_t.Reason.STALE, lambda: checker.admit(intent))
            self.assertEqual(callback.calls, [])
            if field != 'scope':
                checker.authority = current
                self._reject(_t.Reason.STALE, lambda: checker.admit(
                    _dc.replace(intent, authority=changed)))
                self.assertEqual(callback.calls, [])
        checker.authority = f['authority']  # Certificate removal also invalidates.
        self._reject(_t.Reason.STALE, lambda: checker.admit(intent))
        checker.authority = current
        for field in ('principal', 'tenant', 'browser_profile_incarnation',
                      'target_frame_origin', 'top_level_origin', 'frame_ancestry',
                      'scope_revision'):
            replacement = (('https://other.test',) if field == 'frame_ancestry'
                           else 2 if field == 'scope_revision' else 'other')
            checker.scope = _dc.replace(f['scope'], **{field: replacement})
            self._reject(_t.Reason.STALE, lambda: checker.admit(intent))
            self.assertEqual(callback.calls, [])
        checker.scope = f['scope']
        self.assertIs(checker.admit(intent).result, _t.Truth.TRUE)
        self.assertEqual(callback.calls, [(intent,)])
        missing_callback = _Callback()
        missing_checker = _AuthorityChecker(f['scope'], f['authority'], missing_callback)
        missing_intent = _dc.replace(intent, authority=f['authority'])
        self.assertIs(missing_checker.admit(missing_intent).result, _t.Truth.TRUE)
        self.assertEqual(missing_callback.calls, [(missing_intent,)])
        unattested = _dc.replace(f['scope'], attestation=None, attestation_reason=_t.Reason.MISSING)
        obs = _dc.replace(f['observation'], scope=unattested)
        self._reject(_t.Reason.MISSING, lambda: _dc.replace(f['witness'], scope=unattested, observation=obs))

    # @anchor:wa.tests.scripts.test_language.ContractControls.test_evidence_layout_and_generation
    def test_evidence_layout_and_generation(self):
        self.assertEqual(self.f['witness'].producer_generation, 7)
        self._reject(_t.Reason.TYPE, lambda: _dc.replace(self.f['fact'], source='visible'))
        self._reject(_t.Reason.TYPE, lambda: _dc.replace(self.f['witness'], producer_layout='FOCUSED/READ/ACKED'))
        self._reject(_t.Reason.TYPE, lambda: _dc.replace(self.f['witness'], producer_layout=7))
        self._reject(_t.Reason.STALE, lambda: _dc.replace(self.f['witness'],
                     producer_layout=_dc.replace(self.f['witness'].producer_layout, schema='display')))
        self._reject(_t.Reason.MISSING, lambda: _dc.replace(self.f['observation'], producer_generation=None))
        absent = _dc.replace(self.f['observation'], producer_generation=None, generation_reason=_t.Reason.MISSING)
        self._reject(_t.Reason.STALE, lambda: _dc.replace(self.f['witness'], observation=absent))
        self._reject(_t.Reason.STALE, lambda: _dc.replace(self.f['witness'], producer_schema='display-schema'))
        self._reject(_t.Reason.STALE, lambda: _dc.replace(self.f['witness'], facts=_dc.replace(self.f['facts'], visible=_dc.replace(self.f['fact'], source_generation=8))))
        self._reject(_t.Reason.BOUNDS, lambda: _dc.replace(self.f['observation'], capture_end_ns=9))

    # @anchor:wa.tests.scripts.test_language.ContractControls.test_export_destination_identity_and_private_repr
    def test_export_destination_identity_and_private_repr(self):
        f = self.f
        receipt = _t.ExportReceipt(f['handle'], f['observation'], f['scope'], 'static-label', None,
                                   ('host-approved',), 'test-content', _t.ExportDestination.CATALOGUE_CARD, _t.Purpose.QUERY, 1)
        registry = _Registry(f['handle'], 1)
        registry.register(receipt)
        self.assertTrue(registry.check(receipt, f['handle'], 1))
        for changed in (_dc.replace(receipt, destination=_t.ExportDestination.MODEL_PAYLOAD),
                        _dc.replace(receipt, transform_chain=('unapproved',)), _dc.replace(receipt)):
            self._reject(_t.Reason.FOREIGN, lambda: registry.check(changed, f['handle'], 1))
        # A private nested canary has no approval record; serializable != exportable.
        private = _t.freeze_entries([('metadata', {'answer': 'private-canary'})])
        self._reject(_t.Reason.FOREIGN, lambda: registry.check(private, f['handle'], 1))
        self.assertNotIn('test-content', repr(receipt))
        self.assertNotIn('synthetic-token', repr(receipt))
        self._reject(_t.Reason.TYPE, lambda: _dc.replace(receipt, destination='*'))
        self.assertFalse(hasattr(receipt, 'asdict'))

    # @anchor:wa.tests.scripts.test_language.ContractControls.test_export_receipt_binds_requested_context
    def test_export_receipt_binds_requested_context(self):
        f = self.f
        receipt = _t.ExportReceipt(f['handle'], f['observation'], f['scope'],
            'static-label', None, ('host-approved',), 'test-content',
            _t.ExportDestination.CATALOGUE_CARD, _t.Purpose.QUERY, 1)
        checker = _ExportChecker(f['handle'], 1)
        self.assertIs(checker.register(receipt), receipt)
        port: _t.ExportReceiptPort = checker
        callback = _Callback()
        context = dict(source=f['observation'], transform_chain=('host-approved',),
            content_identity='test-content', destination=_t.ExportDestination.CATALOGUE_CARD,
            purpose=_t.Purpose.QUERY, privacy_policy_revision=1)

        def export(request):
            admission = port.check(receipt, **request)
            self.assertIsInstance(admission, _t.AdmissionCheck)
            self.assertIs(admission.result, _t.Truth.TRUE)
            self.assertIsNone(admission.reason)
            callback(receipt, request)

        mismatches = dict(source=_dc.replace(f['observation'],
                publication_identity='other-publication'),
            transform_chain=('unapproved',), content_identity='other-content',
            destination=_t.ExportDestination.MODEL_PAYLOAD, purpose=_t.Purpose.INPUT,
            privacy_policy_revision=2)
        for field, changed in mismatches.items():
            with self.subTest(field=field):
                # Keep the exact retained object; vary only the requested context.
                self._reject(_t.Reason.FOREIGN,
                    lambda: export(dict(context, **{field: changed})))
                self.assertEqual(callback.calls, [])
        export(context)
        self.assertEqual(callback.calls, [(receipt, context)])

    # @anchor:wa.tests.scripts.test_language.ContractControls.test_reservations_cancellation_and_required_policy
    def test_reservations_cancellation_and_required_policy(self):
        f = self.f
        cancel = _t.CancellationPolicy(1000, 'escalate', 'join', 'supervisor', 2000, 3000, 4, 4096)
        policy = _t.ResourcePolicy(4096, 2048, 128, 10, 1000, 256, 64, cancel, 1)
        self.assertEqual(policy.host_bytes, 4096)
        self._reject(_t.Reason.TYPE, lambda: _dc.replace(policy, host_bytes=None))
        self._reject(_t.Reason.BOUNDS, lambda: _dc.replace(policy, emergency_reserve_bytes=4096))
        reservation = _t.Reservation(f['handle'], f['scope'], f['scope'].principal, 'test',
                                      _t.Ownership.RETAINED, f['handle'], 128, 1000, 1)
        self._reject(_t.Reason.FOREIGN, lambda: _dc.replace(reservation, principal='foreign'))
        pending = _t.CancellationProof(f['handle'], False, False, False, False, None, _t.Reason.DEADLINE)
        clock = _Clock()
        clock.advance(1001)
        self.assertIs(pending.reason, _t.Reason.DEADLINE)
        self._reject(_t.Reason.MISSING, lambda: _dc.replace(pending, evidence=f['handle'], reason=None))
        complete = _dc.replace(pending, acknowledged=True, drained=True, evidence=f['handle'], reason=None)
        self.assertTrue(complete.drained)
        joined = _dc.replace(pending, supervisor_terminated=True, joined=True, evidence=f['handle'], reason=None)
        self.assertTrue(joined.joined)
        registry = _Registry(f['handle'], 1)
        registry.register(reservation)
        self._reject(_t.Reason.FOREIGN, lambda: registry.check(_dc.replace(reservation, owner=_dc.replace(f['handle'], token='foreign')), f['handle'], 1))
        self.assertTrue(registry.check(reservation, f['handle'], 1))
        self.assertGreater(clock.now_ns, reservation.absolute_deadline_ns)
        self.assertEqual(reservation.peak_bytes, 128)  # timeout did not release ownership

    # @anchor:wa.tests.scripts.test_language.ContractControls.test_lock_order_instrumentation
    def test_lock_order_instrumentation(self):
        locks, callback = _Locks(), _Callback()
        self._reject(_t.Reason.FOREIGN, locks.enter_store)
        locks.enter_fence()
        locks.actuate(callback)
        locks.enter_store()
        self._reject(_t.Reason.FOREIGN, locks.enter_fence)
        self._reject(_t.Reason.FOREIGN, lambda: locks.actuate(callback))
        self.assertEqual(locks.events, ['fence', 'input', 'store'])
        self.assertEqual(len(callback.calls), 1)
        self.assertIn('Host input fence → short store-owner transaction.', _t.__doc__)

    # @anchor:wa.tests.scripts.test_language.ContractControls.test_import_has_no_services_or_backend_dependencies
    def test_import_has_no_services_or_backend_dependencies(self):
        script = '''import sys
import importlib.abc
class Guard(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.startswith(('wender.act', 'wender.perceive', 'wender.learn', 'vision', 'vnative')):
            raise RuntimeError('backend initialization')
sys.meta_path.insert(0, Guard())
def audit(event, args):
    if event in ('socket.__new__', 'subprocess.Popen', 'os.system', 'builtins.input'):
        raise RuntimeError('service or input initialization')
sys.addaudithook(audit)
import wender.scripts
import wender.scripts.types
assert not [name for name in vars(wender.scripts) if not name.startswith('_') and name != 'types']
'''
        for suffix, expected in (('', 0), ('\nimport wender.act.actor\n', 1)):
            proc = _subprocess.run([_sys.executable, '-B', '-c', script + suffix], cwd=_REPO,
                                    capture_output=True, text=True, timeout=20)
            self.assertEqual(proc.returncode, expected, proc.stderr)
            if expected:
                self.assertIn('backend initialization', proc.stderr)
        tree = _ast.parse(_inspect.getsource(_t))
        imports = [node for node in tree.body if isinstance(node, (_ast.Import, _ast.ImportFrom))]
        self.assertTrue(all(isinstance(node, _ast.ImportFrom) and node.module in
                            ('dataclasses', 'enum', 'types', 'typing', 'unicodedata') for node in imports))

    # @anchor:wa.tests.scripts.test_language.ContractControls.test_ports_are_structural_and_typed
    def test_ports_are_structural_and_typed(self):
        ports = ('FrameWaitPort', 'ValidationRegistryPort', 'BindingRegistryPort',
                 'AuthorityCheckPort', 'SlotCapabilityPort', 'ResultCapabilityPort',
                 'ExportReceiptPort', 'ReservationPort', 'CancellationPort')
        for name in ports:
            port = getattr(_t, name)
            self.assertTrue(port._is_protocol)
            with self.assertRaises(TypeError):
                port()
        signature = _inspect.signature(_t.FrameWaitPort.next_observation)
        self.assertEqual(tuple(signature.parameters), ('self', 'after_identity', 'absolute_deadline_ns', 'cancel'))
        self.assertIs(signature.return_annotation, _t.AcquisitionResult)
        self._reject(_t.Reason.MISSING, lambda: _t.AcquisitionResult(None, None,
            _t.AcquisitionDisposition.REFUSED, None, None, _t.MissingReason.ABSENT, _t.InputDisposition.NOT_SENT))
        self.assertIs(_t.AcquisitionResult(None, _t.Reason.DEADLINE,
            _t.AcquisitionDisposition.REFUSED, None, None, _t.MissingReason.ABSENT, _t.InputDisposition.NOT_SENT).reason, _t.Reason.DEADLINE)

    # @anchor:wa.tests.scripts.test_language.ContractControls.test_routing_to_stage_and_owned_paths
    def test_routing_to_stage_and_owned_paths(self):
        from tools import build_code_index as builder, route_code as router
        source = _inspect.getsource(_t)
        tree = _ast.parse(source)
        self.assertEqual(_json.loads(source.splitlines()[0].split('@module ', 1)[1])['book'], 'HB-WA-s4-learn')
        missing = [row for row in builder.check_missing_anchors(_REPO)
                   if row['file'] in ('wender/scripts/types.py', 'tests/scripts/support.py', 'tests/scripts/test_language.py')]
        self.assertEqual(missing, [])
        # The index is owned temporary state; the generated repository index is untouched.
        index = builder.build_memory_index(_REPO)
        anchors = [row for row in index['anchors'] if row['file'] == 'wender/scripts/types.py']
        self.assertTrue(anchors)
        self.assertEqual(len({row['id'] for row in anchors}), len(anchors))
        with _tempfile.TemporaryDirectory() as temp:
            path = _Path(temp) / 'owned-index'
            path.mkdir()
            for name, raw in index['files'].items():
                (path / name).write_bytes(raw)
            loaded = router.load_index(_REPO, path)
            for row in anchors:
                self.assertEqual(row['stage'], 'HB-WA-s4-learn')
                self.assertTrue(row['id'].startswith('wa.scripts.types.'))
                result = router.lookup_anchor(loaded, row['id'])
                self.assertIn('wender/scripts/types.py', str(result))
        owned = ('wender/scripts/types.py', 'wender/scripts/__init__.py', 'tests/scripts/support.py',
                 'tests/scripts/test_language.py', 'tests/scripts/__init__.py')
        for rel in owned:
            path = _REPO / rel
            self.assertTrue(path.is_file())
            self.assertFalse(path.is_symlink())
            self.assertEqual(path.resolve(), _REPO.resolve() / rel)
        manifest = {rel: _json.loads((_REPO / rel).read_text().splitlines()[0]
                                    .split('@module ', 1)[1])['id'] for rel in owned}
        # The positive fixture is this assignment's manifest; foreign paths
        # below remain rejecting controls independently of the integration diff.
        changed = sorted(manifest)
        self.assertTrue(_verify_routing(_REPO, manifest, changed))
        # Each mutation reaches the same rejecting gate as the canonical manifest.
        with _tempfile.TemporaryDirectory() as temp:
            root = _Path(temp)
            target = root / 'wender/scripts/types.py'
            target.parent.mkdir(parents=True)
            mini = {'wender/scripts/types.py': 'wa.scripts.types'}
            mutations = (
                (source + '\ndef missing_public(): pass\n', _t.Reason.MISSING),
                (source.replace('# @anchor:wa.scripts.types.ScopeIdentity',
                                '# @anchor:wa.scripts.types.AuthorityRevision'), _t.Reason.SCHEMA),
                (source.replace('"owner":"LEARN"', '"owner":"PERCEIVE"'), _t.Reason.SCHEMA),
                (source.replace('"book":"HB-WA-s4-learn"',
                                '"book":"HB-WA-s1-perceive"'), _t.Reason.SCHEMA),
            )
            for mutated, reason in mutations:
                target.write_text(mutated)
                self._reject(reason, lambda: _verify_routing(root, mini, tuple(mini)))
            target.write_text(source)
            self.assertTrue(_verify_routing(root, mini, tuple(mini)))
            self._reject(_t.Reason.FOREIGN, lambda: _verify_routing(
                root, mini, ('unexpected.py',)))
            alias = root / 'alias.py'
            alias.symlink_to(target)
            self._reject(_t.Reason.FOREIGN, lambda: _verify_routing(
                root, {'alias.py': 'wa.scripts.types'}, ('alias.py',)))
            directory_alias = root / 'aliasdir'
            directory_alias.symlink_to(target.parent, target_is_directory=True)
            self._reject(_t.Reason.FOREIGN, lambda: _verify_routing(
                root, {'aliasdir/types.py': 'wa.scripts.types'}, ('aliasdir/types.py',)))


    # @anchor:wa.tests.scripts.test_language.ContractControls.test_budget_policy_units_and_missing_configuration
    def test_budget_policy_units_and_missing_configuration(self):
        # Predicate: units and ceilings are explicit, never defaulted or coerced.
        cases = (
            (_t.SourcePolicy(16384, 65536, 64), 'utf8_bytes', 65537),
            (_t.ParserPolicy(1024, 8, 32, 1000, 134217728, 'supervisor'), 'ast_depth', 9),
            (_t.SlotPolicy(32, 4096, 16384, 8192, 32768), 'slots', 33),
            (_t.StorePolicy(16777216, 64, 4194304, 4, 800), 'static_meta_tokens', 801),
            (_t.ReplayPolicy(33554432, 1, 8388608, 16), 'screening_jobs', 2),
            (_t.ResolverPolicy(16, 64, 30000000000), 'candidates', 65),
            (_t.WaitPolicy(2000, 30000, 64, 1000000000, 50, 2000, 20), 'semantic_wait_ms', 30001),
            (_t.ContextPolicy(1024, 2000, 1200), 'z2_tokens', 2001),
            (_t.DisplayPolicy(64, 8, 30, 15, 3, 120, 4096, 40, 512, 300), 'step_bytes', 4097),
        )
        for record, field, excessive in cases:
            with self.subTest(record=type(record).__name__):
                self.assertGreater(getattr(record, field), 0)
                self._reject(_t.Reason.BOUNDS, lambda: _dc.replace(record, **{field: excessive}))
                self._reject(_t.Reason.TYPE, lambda: _dc.replace(record, **{field: True}))
                self._reject(_t.Reason.SCHEMA, lambda: _t.record_from_entries(type(record), []))
        wait = cases[6][0]
        self._reject(_t.Reason.BOUNDS, lambda: _dc.replace(wait, acquisition_spacing_ms=19))
        self._reject(_t.Reason.BOUNDS, lambda: _dc.replace(wait, quiet_window_ms=49))
        self._reject(_t.Reason.BOUNDS, lambda: _dc.replace(self.f['limits'], max_nodes=65))
        self._reject(_t.Reason.BOUNDS, lambda: _t.VerificationBudget(2001, 1, 1))
        self.assertEqual(_t.PerformanceObjectives(1000, 990000).tail_latency_ns, 1000)
        self._reject(_t.Reason.BOUNDS, lambda: _t.PerformanceObjectives(1000, 1000001))

    # @anchor:wa.tests.scripts.test_language.ContractControls.test_digests_predicate_shapes_and_declared_slots
    def test_digests_predicate_shapes_and_declared_slots(self):
        for digest_type in (_t.PlanDigest, _t.MetadataDigest, _t.ArtifactDigest):
            self.assertEqual(digest_type('sha256', 'a' * 64).full_digest, 'a' * 64)
            self._reject(_t.Reason.SCHEMA, lambda: digest_type('sha256', 'abc'))
            self._reject(_t.Reason.SCHEMA, lambda: digest_type('sha256', 'g' * 64))
        delta = _t.Operand('delta', 'query', _t.ValueKind.INTEGER, -1)
        self.assertEqual(delta.value, -1)  # Signed semantic delta is not a count.
        self.assertEqual(_t.freeze_entries([('signed', -1)])[0].value, -1)
        self._reject(_t.Reason.TYPE, lambda: _dc.replace(delta, value=True))
        post = self.f['post']
        self._reject(_t.Reason.MISSING, lambda: _dc.replace(post, transition_reference=_t.MissingRef(None, _t.Reason.MISSING)))
        self._reject(_t.Reason.SCHEMA, lambda: _dc.replace(post, operator=_t.PredicateOperator.NOT))
        negated = _dc.replace(post, operator=_t.PredicateOperator.NOT, operands=(), children=(post,))
        self.assertEqual(len(negated.children), 1)
        self._reject(_t.Reason.MISSING, lambda: _dc.replace(self.f['effect'], postcondition=negated))
        arg = _t.ArgSpec('input', _t.ValueKind.TEXT, True, 1, 16, _t.Sensitivity.PRIVATE,
                         _t.Purpose.INPUT, 'trusted-actuator', 'form', 'norm-test')
        slot = _t.SlotDescriptor('input', _t.ValueKind.TEXT, _t.Purpose.INPUT,
                                 self.f['handle'], 'episode', self.f['scope'], 'form', 'trusted-actuator', None)
        selector = _dc.replace(self.f['selector'], action=_t.ActionKind.TYPE)
        effect = _dc.replace(self.f['effect'], action=_t.ActionKind.TYPE, selector=selector, arguments=(slot,))
        nodes = (_dc.replace(self.f['plan'].nodes[0], kind=effect.action, effect=effect), self.f['plan'].nodes[1])
        self._reject(_t.Reason.FOREIGN, lambda: _dc.replace(self.f['plan'], nodes=nodes))
        self.assertEqual(self._replan(self.f['plan'], nodes=nodes, slots=(arg,)).slots, (arg,))
        self._reject(_t.Reason.FOREIGN, lambda: _dc.replace(self.f['plan'], nodes=nodes,
                     slots=(_dc.replace(arg, capability_contract='foreign'),)))
        key_slot = _dc.replace(slot, kind=_t.ValueKind.KEY)
        key_selector = _dc.replace(selector, action=_t.ActionKind.KEY)
        self._reject(_t.Reason.MISSING, lambda: _dc.replace(effect, action=_t.ActionKind.KEY,
                     selector=key_selector, arguments=(key_slot,)))
        vocabulary = _t.HostKeyVocabulary('host-v1', ('host-fixture-key',), self.f['handle'])
        key_effect = _dc.replace(effect, action=_t.ActionKind.KEY, selector=key_selector,
                                 arguments=(_dc.replace(key_slot, key_vocabulary=vocabulary),))
        self.assertIs(key_effect.arguments[0].key_vocabulary, vocabulary)
        self._reject(_t.Reason.SCHEMA, lambda: _dc.replace(vocabulary, symbols=('duplicate', 'duplicate')))

    # @anchor:wa.tests.scripts.test_language.ContractControls.test_results_keep_absence_and_uncertain_input_explicit
    def test_results_keep_absence_and_uncertain_input_explicit(self):
        unknown = _t.PredicateResult(_t.Truth.UNKNOWN, _t.Reason.MISSING)
        measured = _t.MeasuredValue(None, 'ns', _t.MissingRef(None, _t.Reason.MISSING), _t.Reason.MISSING)
        result = _t.StepResult(None, None, _t.Reason.MISSING, None, _t.InputDisposition.NOT_SENT,
                               unknown, unknown, measured, (), _t.Reason.MISSING)
        self.assertIsNone(result.duration.value)
        self._reject(_t.Reason.MISSING, lambda: _dc.replace(result, usage_reason=None))
        self._reject(_t.Reason.MISSING, lambda: _dc.replace(result, input_disposition=_t.InputDisposition.APPLIED))
        outcome = _t.RuntimeOutcome('episode', self.f['identity'], self.f['scope'],
                                    _t.TerminalStatus.INPUT_UNCERTAIN, _t.InputDisposition.INPUT_UNCERTAIN,
                                    unknown, unknown, _t.FailureClass.UNCLASSIFIED, True, (), _t.Reason.MISSING, unknown)
        self.assertTrue(outcome.reconciliation_required)
        self._reject(_t.Reason.RECONCILIATION, lambda: _dc.replace(outcome, reconciliation_required=False))
        self._reject(_t.Reason.MISSING, lambda: _dc.replace(outcome, telemetry_reason=None))
        success = _t.PredicateResult(_t.Truth.TRUE, None)
        complete = _dc.replace(outcome, status=_t.TerminalStatus.COMPLETED,
                                input_disposition=_t.InputDisposition.APPLIED, checkpoint=success,
                                task_goal=success, failure=None, reconciliation_required=False, owner_drained=success)
        self.assertIs(complete.task_goal.value, _t.Truth.TRUE)
        self._reject(_t.Reason.SCHEMA, lambda: _dc.replace(complete, task_goal=unknown))
        self._reject(_t.Reason.SCHEMA, lambda: _dc.replace(complete, owner_drained=unknown))
        self._reject(_t.Reason.RECONCILIATION, lambda: _dc.replace(complete, input_disposition=_t.InputDisposition.INPUT_UNCERTAIN))
        context = _t.ContextRequest(self.f['identity'].metadata, self.f['scope'], self.f['authority'],
                                    1000, _t.MissingRef(None, _t.Reason.RECONCILIATION))
        changed = _dc.replace(context, current_authority=_dc.replace(self.f['authority'], policy=2))
        self.assertEqual(context.prefix_identity, changed.prefix_identity)
        self.assertNotEqual(context.current_authority, changed.current_authority)


    # @anchor:wa.tests.scripts.test_language.ContractControls.test_normalized_named_identifiers
    def test_normalized_named_identifiers(self):
        # Predicate: NFKC/casefold collisions close with SCHEMA, distinct names pass.
        f = self.f
        symbol = _t.ResultDeclaration('answer', _t.ValueKind.TEXT, _t.Purpose.HARVEST, 'consumer', 128)
        for alias in ('ａｎｓｗｅｒ', 'ANSWER', 'answer'):
            self._reject(_t.Reason.SCHEMA, lambda: _dc.replace(
                f['plan'], symbols=(symbol, _dc.replace(symbol, name=alias))))
        symbols = (symbol, _dc.replace(symbol, name='other'))
        self.assertEqual(self._replan(f['plan'], symbols=symbols).symbols, symbols)
        operand = _dc.replace(f['entry'].operands[0], operand_id='a')
        for alias in ('ａ', 'A', 'a'):
            self._reject(_t.Reason.SCHEMA, lambda: _dc.replace(
                f['entry'], operands=(operand, _dc.replace(operand, operand_id=alias))))
        operands = (operand, _dc.replace(operand, operand_id='b'))
        self._reject(_t.Reason.SCHEMA, lambda: _dc.replace(f['entry'], operands=operands))
        self.assertEqual(_dc.replace(f['entry'], operands=(operand,)).operands, (operand,))
        node = f['plan'].nodes[0]
        self._reject(_t.Reason.SCHEMA, lambda: _dc.replace(f['plan'], nodes=(
            node, _dc.replace(node, step_id='ＥＦＦＥＣＴ'), f['plan'].nodes[-1])))
        self.assertEqual(self._replan(f['plan'], nodes=(node, _dc.replace(
            node, step_id='other'), f['plan'].nodes[-1])).nodes[1].step_id, 'other')
        slot = _t.SlotDescriptor('input', _t.ValueKind.TEXT, _t.Purpose.INPUT,
                                 f['handle'], 'episode', f['scope'], 'form', 'actuator', None)
        self._reject(_t.Reason.SCHEMA, lambda: _dc.replace(f['effect'], arguments=(
            slot, _dc.replace(slot, slot_name='ＩＮＰＵＴ'))))
        slots = (slot, _dc.replace(slot, slot_name='other'))
        self.assertEqual(_dc.replace(f['effect'], arguments=slots).arguments, slots)
        key = _t.HostKeyVocabulary('v1', ('ENTER', 'TAB'), f['handle'])
        self.assertEqual(key.symbols, ('ENTER', 'TAB'))
        self._reject(_t.Reason.SCHEMA, lambda: _dc.replace(key, symbols=('ENTER', 'ＥＮＴＥＲ')))

    # @anchor:wa.tests.scripts.test_language.ContractControls.test_owner_exchange_annotations
    def test_owner_exchange_annotations(self):
        # Predicate: external records stay owner supplied, with typed check ports.
        import typing
        names = ('CandidateRef', 'ScriptLease', 'CatalogueSnapshot', 'RunEvent',
                 'EpisodeSeal', 'DreamCertificate')
        methods = ('candidate', 'lease', 'snapshot', 'event', 'seal', 'certificate')
        for name, method in zip(names, methods):
            self.assertIn(name, _t.__doc__)
            self.assertFalse(hasattr(_t, name))  # No duplicate authoritative class.
            hints = typing.get_type_hints(getattr(_t.OwnerExchangePort, 'check_' + method))
            self.assertEqual(hints[method].__name__, '_' + name)
            self.assertIs(hints['return'], _t.AdmissionCheck)
        with self.assertRaises(TypeError):
            _t.OwnerExchangePort()


    # @anchor:wa.tests.scripts.test_language.ContractControls.test_truth_algebra_and_bounded_freezer
    def test_truth_algebra_and_bounded_freezer(self):
        # Predicate: known veto/witness survives UNKNOWN; malformed truth rejects.
        import itertools
        for a, b in itertools.product(_t.TruthValue, repeat=2):
            expected_all = (_t.Truth.FALSE if _t.Truth.FALSE in (a, b) else
                            _t.Truth.UNKNOWN if _t.Truth.UNKNOWN in (a, b) else _t.Truth.TRUE)
            expected_any = (_t.Truth.TRUE if _t.Truth.TRUE in (a, b) else
                            _t.Truth.UNKNOWN if _t.Truth.UNKNOWN in (a, b) else _t.Truth.FALSE)
            self.assertIs(_t.truth_all((a, b)), expected_all)
            self.assertIs(_t.truth_any((a, b)), expected_any)
        self.assertIs(_t.truth_not(_t.Truth.UNKNOWN), _t.Truth.UNKNOWN)
        self.assertIs(_t.truth_not(_t.Truth.TRUE), _t.Truth.FALSE)
        self.assertIs(_t.truth_not(_t.Truth.FALSE), _t.Truth.TRUE)
        self._reject(_t.Reason.TYPE, lambda: _t.truth_not(False))
        self._reject(_t.Reason.TYPE, lambda: _t.truth_all((_t.Truth.FALSE, False)))
        self._reject(_t.Reason.MISSING, lambda: _t.truth_any(()))
        self._reject(_t.Reason.BOUNDS, lambda: _t.freeze_entries([('large', 'x' * 262145)]))
        self._reject(_t.Reason.BOUNDS, lambda: _t.freeze_entries([('many', [1] * 1025)]))
        recursive = []
        recursive.append(recursive)
        self._reject(_t.Reason.BOUNDS, lambda: _t.freeze_entries([('cycle', recursive)]))
        deep = 'leaf'
        for _ in range(9):
            deep = [deep]
        self._reject(_t.Reason.BOUNDS, lambda: _t.freeze_entries([('deep', deep)]))
        self._reject(_t.Reason.TYPE, lambda: _t.freeze_entries([('object', object())]))
        self.assertEqual(_t.freeze_entries([('valid', ['value'])])[0].value, ('value',))
        # Non-enum exceptions remain bounded even if a caller supplies private text.
        self.assertEqual(str(_t.ContractError('private-canary')), _t.Reason.TYPE.value)
        for level in (_t.MatchLevel.L2_NAME, _t.MatchLevel.L3_VARIANT, _t.MatchLevel.L4_UNIQUE_ROLE):
            self._reject(_t.Reason.FOREIGN, lambda: _dc.replace(self.f['selector'], allowed_match_levels=(level,)))
        self.assertIs(_dc.replace(self.f['selector'], allowed_match_levels=(_t.MatchLevel.L1_STABLE,)).allowed_match_levels[0], _t.MatchLevel.L1_STABLE)

    # @anchor:wa.tests.scripts.test_language.ContractControls.test_judge_fix_records_and_ports
    def test_judge_fix_records_and_ports(self):
        # Predicate: no timeout, undercharge or empty restart can assert release.
        f = self.f
        ref = _t.RegistryRef('reservation', 'incarnation', 1, f['handle'])
        registry = _Registry(f['handle'], 1)
        registry.register(ref)
        self.assertTrue(registry.check(ref, f['handle'], 1))
        self._reject(_t.Reason.FOREIGN, lambda: registry.check(_dc.replace(ref), f['handle'], 1))
        self.assertNotIn(f['handle'].token, repr(ref))
        self._reject(_t.Reason.TYPE, lambda: _dc.replace(ref, generation=True))
        request = _t.ReservationRequest(f['handle'], f['handle'], f['scope'], 'parse', 128, (), (), 1, 1000)
        grant = _t.ReservationGrant(request, ref, 128, 1, 10, 1000)
        self.assertEqual(grant.charged_bytes, request.peak_bytes)
        self._reject(_t.Reason.BOUNDS, lambda: _dc.replace(grant, charged_bytes=127))
        self._reject(_t.Reason.STALE, lambda: _dc.replace(grant, accounting_revision=2))
        self._reject(_t.Reason.BOUNDS, lambda: _dc.replace(grant, absolute_deadline_ns=1001))
        self._reject(_t.Reason.TYPE, lambda: _dc.replace(request, peak_bytes=True))
        policy = _t.CancellationPolicy(100, 'escalate', 'join', 'supervisor', 200, 300, 4, 256)
        holder = _t.HolderContract('consumer', _t.Purpose.QUERY, f['scope'], 128, 1000, ref, ref, policy)
        self.assertEqual(holder.retained_bytes, 128)
        self._reject(_t.Reason.BOUNDS, lambda: _dc.replace(holder, retained_bytes=257))
        self._reject(_t.Reason.BOUNDS, lambda: _dc.replace(policy, escalation_deadline_ns=99))
        pending = _t.CancellationResult(True, False, False, False, False, (), None, _t.MissingReason.INCOMPLETE)
        self._reject(_t.Reason.MISSING, lambda: _dc.replace(pending, released_tokens=(ref,)))
        released = _dc.replace(pending, acknowledged=True, drained=True, released_tokens=(ref,), evidence=ref, reason=None)
        self.assertEqual(released.released_tokens, (ref,))
        self._reject(_t.Reason.MISSING, lambda: _dc.replace(released, drained=False))
        closed = _t.RestartBarrierView(True, f['scope'], (), None, _t.MissingReason.ABSENT)
        self._reject(_t.Reason.RECONCILIATION, lambda: _dc.replace(closed, closed=False))
        opened = _dc.replace(closed, closed=False, reconciliation=ref, reason=None)
        self.assertFalse(opened.closed)
        self._reject(_t.Reason.RECONCILIATION, lambda: _dc.replace(opened, unresolved_actions=(ref,)))
        failure = _t.FailureClassification('episode', f['scope'], 'environment', _t.FailureClass.ARTIFACT_DEFECT, (ref,), 1)
        self.assertIs(failure.cause, _t.FailureClass.ARTIFACT_DEFECT)
        self._reject(_t.Reason.MISSING, lambda: _dc.replace(failure, evidence=()))
        evidence = _t.FactEvidence(_t.Truth.TRUE, _t.EvidenceSource.AX, 'test-schema', 7, f['observation'], f['observation'].association, None)
        self.assertIs(evidence.value, _t.Truth.TRUE)
        self._reject(_t.Reason.STALE, lambda: _dc.replace(evidence, source_generation=8))
        self._reject(_t.Reason.MISSING, lambda: _dc.replace(evidence, association=_t.MissingRef(None, _t.Reason.MISSING)))
        for port in (_t.ClockPort, _t.CancellationStatePort, _t.StepAuthorityPort, _t.EventAuthorityPort):
            with self.assertRaises(TypeError):
                port()
        self.assertIs(_inspect.signature(_t.FrameWaitPort.wait_until).parameters['predicate'].annotation, _t.PredicateSpec)

    # @anchor:wa.tests.scripts.test_language.ContractControls.test_descriptive_plan_and_canonical_association
    def test_descriptive_plan_and_canonical_association(self):
        # Predicate: descriptions are separate from registry-held canonical bytes.
        f = self.f
        header = _t.ScriptHeader('logical', 1, _t.MissingRef(None, _t.Reason.MISSING),
            f['scope'], 'site', 'goal', 'signature', f['abis'], (), (), f['limits'],
            f['entry'], f['terminal'].reference, (), f['identity'], None)
        plan = _t.PlanDefinition(header, f['plan'].nodes, (f['selector'],), (), (),
            f['entry'], f['terminal'], f['limits'], f['identity'], f['identity'].structural_screen_digest, f['identity'].source_digest, f['plan'].queries)
        self.assertEqual(plan.header.logical_id, 'logical')
        self._reject(_t.Reason.FOREIGN, lambda: _dc.replace(plan, selectors=()))
        self._reject(_t.Reason.TYPE, lambda: _dc.replace(plan, nodes=list(plan.nodes)))
        ref = _t.RegistryRef('compiler', 'incarnation', 1, f['handle'])
        association = _t.CanonicalPlanAssociation(plan, b'canonical-test-only', ref)
        self.assertIs(association.plan, plan)
        self._reject(_t.Reason.BOUNDS, lambda: _dc.replace(association, canonical_bytes=b''))
        self._reject(_t.Reason.BOUNDS, lambda: _dc.replace(association, canonical_bytes=b'x' * 1025))
        self._reject(_t.Reason.STALE, lambda: _dc.replace(f['plan'], definition=_dc.replace(plan, nodes=(
            _dc.replace(plan.nodes[0], step_id='changed'), plan.nodes[1]))))
        self._reject(_t.Reason.BOUNDS, lambda: _dc.replace(f['plan'], canonical_bytes=b''))
        self._reject(_t.Reason.STALE, lambda: _dc.replace(plan, identity=_dc.replace(plan.identity, scope=_dc.replace(f['scope'], principal='foreign'))))
        self._reject(_t.Reason.SCHEMA, lambda: _dc.replace(plan, source_digest=f['identity'].plan))
        registry = _Registry(f['handle'], 1)
        self._reject(_t.Reason.FOREIGN, lambda: registry.check(association, f['handle'], 1))
        registry.register(association)
        self.assertTrue(registry.check(association, f['handle'], 1))
        self._reject(_t.Reason.FOREIGN, lambda: registry.check(_dc.replace(association), f['handle'], 1))

    # @anchor:wa.tests.scripts.test_language.ContractControls.test_fixture_inventory_and_pending_callbacks
    def test_fixture_inventory_and_pending_callbacks(self):
        registry = _Registry(self.f['handle'], 1)
        self.assertEqual(registry.outstanding(), ())
        handle = registry.mint('test-only', 'private-canary')
        self.assertEqual(registry.outstanding(), (handle,))
        self.assertTrue(registry.check(handle, self.f['handle'], 1))
        self._reject(_t.Reason.FOREIGN, lambda: registry.check(_dc.replace(handle), self.f['handle'], 1))
        callback = _Callback()
        callback.enqueue('pending')
        self.assertEqual(callback.calls, [])
        self.assertEqual(callback.pending_callbacks, [('pending',)])
        callback.drain()
        self.assertEqual(callback.pending_callbacks, [])
        self.assertEqual(callback.calls, [('pending',)])





    # @anchor:wa.tests.scripts.test_language.ContractControls.test_operator_specific_schemas
    def test_operator_specific_schemas(self):
        # Predicate: each closed leaf has exactly one permitted typed operand.
        f = self.f
        for op in _t.PredicateOperator:
            with self.subTest(operator=op.value):
                if op in (_t.PredicateOperator.ALL, _t.PredicateOperator.ANY, _t.PredicateOperator.NOT):
                    valid = _dc.replace(f['entry'], operator=op, operands=(), children=(f['entry'],))
                    self.assertEqual(valid.children, (f['entry'],))
                    self._reject(_t.Reason.SCHEMA, lambda: _dc.replace(valid, children=()))
                    self._reject(_t.Reason.SCHEMA, lambda: _dc.replace(valid, operands=f['entry'].operands))
                    if op is _t.PredicateOperator.NOT:
                        self._reject(_t.Reason.SCHEMA, lambda: _dc.replace(valid, children=(f['entry'], f['entry'])))
                    continue
                integer = op is _t.PredicateOperator.VALUE_LEN_DELTA
                operand = _t.Operand('operand', 'test-query',
                    _t.ValueKind.INTEGER if integer else _t.ValueKind.TEXT,
                    -1 if integer else 'enabled' if op is _t.PredicateOperator.STATE_FLAG else 'approved-enum')
                valid = _dc.replace(f['post'], operator=op, operands=(operand,))
                self.assertIs(valid.operator, op)
                self._reject(_t.Reason.SCHEMA, lambda: _dc.replace(valid, operands=()))
                extra = _dc.replace(operand, operand_id='extra')
                self._reject(_t.Reason.SCHEMA, lambda: _dc.replace(valid, operands=(operand, extra)))
                for kind, value in ((_t.ValueKind.TEXT, 'enabled'), (_t.ValueKind.INTEGER, 1),
                                    (_t.ValueKind.BOOLEAN, True), (_t.ValueKind.KEY, 'Enter')):
                    if kind is operand.kind:
                        continue
                    wrong = _dc.replace(operand, kind=kind, value=value)
                    self._reject(_t.Reason.TYPE, lambda: _dc.replace(valid, operands=(wrong,)))
        state = _dc.replace(f['entry'], operator=_t.PredicateOperator.STATE_FLAG,
                            operands=(_dc.replace(f['entry'].operands[0], value='enabled'),))
        self._reject(_t.Reason.SCHEMA, lambda: _dc.replace(state,
            operands=(_dc.replace(state.operands[0], value='arbitrary-flag'),)))

    # @anchor:wa.tests.scripts.test_language.ContractControls.test_all_step_payloads
    def test_all_step_payloads(self):
        # Predicate: every StepKind has its own closed indivisible payload.
        f = self.f
        wait = _t.WaitPolicy(100, 1500, 2, 10000, 50, 100, 20)
        result = _t.ResultDeclaration('answer', _t.ValueKind.TEXT, _t.Purpose.HARVEST, 'consumer', 128)
        authorization = _t.RegistryRef('harvest', 'test-incarnation', 7, f['handle'])
        payloads = {
            _t.StepKind.FIND: ('find', _t.FindSpec(f['plan'].queries[0])),
            _t.StepKind.WAIT_UNTIL: ('wait', _t.WaitSpec(f['entry'], wait)),
            _t.StepKind.ASSERT_TAG: ('assertion', _t.AssertSpec(f['entry'], _t.Reason.MISSING)),
            _t.StepKind.HARVEST: ('harvest', _t.HarvestSpec('test-query', result, authorization, f['scope'], 128)),
        }
        for kind in (_t.StepKind.CLICK, _t.StepKind.TYPE, _t.StepKind.KEY):
            selector = _dc.replace(f['selector'], action=kind)
            args = ()
            if kind is not _t.StepKind.CLICK:
                vocabulary = _t.HostKeyVocabulary('test', ('Enter',), f['handle']) if kind is _t.StepKind.KEY else None
                args = (_t.SlotDescriptor('input', _t.ValueKind.KEY if vocabulary else _t.ValueKind.TEXT,
                    _t.Purpose.INPUT, f['handle'], 'episode', f['scope'], 'form', 'actuator', vocabulary),)
            payloads[kind] = ('effect', _dc.replace(f['effect'], action=kind, selector=selector, arguments=args))
        for kind, (field, payload) in payloads.items():
            with self.subTest(kind=kind.value):
                valid = _t.StepPlan('step', kind, None, None, None, None, **{field: payload}) if field != 'effect' else _t.StepPlan('step', kind, payload, None, None, None)
                self.assertIs(valid.kind, kind)
                self._reject(_t.Reason.MISSING, lambda: _dc.replace(valid, **{field: None}))
                self._reject(_t.Reason.MISSING, lambda: _dc.replace(valid, terminal=f['terminal']))
        self._reject(_t.Reason.SCHEMA, lambda: _dc.replace(f['plan'].nodes[0], kind=_t.NodeKind.EFFECT))
        self._reject(_t.Reason.SCHEMA, lambda: _dc.replace(f['plan'].nodes[0], kind=_t.StepKind.TYPE))
        self._reject(_t.Reason.BOUNDS, lambda: _dc.replace(result, max_bytes=0))
        self._reject(_t.Reason.BOUNDS, lambda: _dc.replace(result, max_bytes=16385))
        self._reject(_t.Reason.TYPE, lambda: _dc.replace(result, max_bytes=True))
        self._reject(_t.Reason.BOUNDS, lambda: _dc.replace(payloads[_t.StepKind.HARVEST][1], max_bytes=129))
        self._reject(_t.Reason.FOREIGN, lambda: _dc.replace(payloads[_t.StepKind.HARVEST][1],
            authorization=_dc.replace(authorization, kind='unrelated')))
        self._reject(_t.Reason.TYPE, lambda: _dc.replace(payloads[_t.StepKind.WAIT_UNTIL][1], budget=None))
        self._reject(_t.Reason.TYPE, lambda: _dc.replace(payloads[_t.StepKind.ASSERT_TAG][1], code='private-code'))

    # @anchor:wa.tests.scripts.test_language.ContractControls.test_plan_reference_tables
    def test_plan_reference_tables(self):
        # Predicate: references resolve to exact scope-compatible declarations.
        f = self.f
        plan = f['plan'].definition
        query = plan.queries[0]
        result = _t.ResultDeclaration('answer', _t.ValueKind.TEXT, _t.Purpose.HARVEST, 'consumer', 128)
        authorization = _t.RegistryRef('harvest', 'test-incarnation', 7, f['handle'])
        harvest = _t.HarvestSpec(query.name, result, authorization, f['scope'], 128)
        node = _t.StepPlan('harvest', _t.StepKind.HARVEST, None, None, None, None, harvest=harvest)
        valid = _dc.replace(plan, nodes=(plan.nodes[0], node, plan.nodes[-1]), symbols=(result,))
        self.assertEqual(valid.nodes[1].harvest.result, valid.symbols[0])
        self._reject(_t.Reason.FOREIGN, lambda: _dc.replace(valid, symbols=()))
        self._reject(_t.Reason.FOREIGN, lambda: _dc.replace(valid, symbols=(_dc.replace(result, max_bytes=64),)))
        self._reject(_t.Reason.FOREIGN, lambda: _dc.replace(valid, queries=()))
        self._reject(_t.Reason.FOREIGN, lambda: _dc.replace(valid, queries=(_dc.replace(query, operators=(_t.PredicateOperator.FOCUSED,)),)))
        self._reject(_t.Reason.STALE, lambda: _dc.replace(valid,
            queries=(_dc.replace(query, scope=_dc.replace(f['scope'], principal='foreign')),)))
        unknown = _dc.replace(f['entry'], operands=(_dc.replace(f['entry'].operands[0], query_id='undeclared'),))
        self._reject(_t.Reason.FOREIGN, lambda: _dc.replace(plan, entry=unknown, header=_dc.replace(plan.header, entry=unknown)))
        nested = _dc.replace(f['entry'], operator=_t.PredicateOperator.NOT, operands=(), children=(unknown,))
        self._reject(_t.Reason.FOREIGN, lambda: _dc.replace(plan, entry=nested, header=_dc.replace(plan.header, entry=nested)))
        foreign = _dc.replace(node, harvest=_dc.replace(harvest,
            scope=_dc.replace(f['scope'], principal='foreign')))
        self._reject(_t.Reason.STALE, lambda: _dc.replace(valid,
            nodes=(plan.nodes[0], foreign, plan.nodes[-1])))
        smaller = _dc.replace(valid.limits, episode_result_bytes=64)
        self._reject(_t.Reason.BOUNDS, lambda: _dc.replace(valid, limits=smaller,
            header=_dc.replace(valid.header, limits=smaller)))
        changed = _dc.replace(node, harvest=_dc.replace(harvest, query_id='undeclared'))
        self._reject(_t.Reason.FOREIGN, lambda: _dc.replace(valid, nodes=(plan.nodes[0], changed, plan.nodes[-1])))
        find = _t.StepPlan('find', _t.StepKind.FIND, None, None, None, None, find=_t.FindSpec(query))
        self.assertEqual(_dc.replace(plan, nodes=(find, plan.nodes[-1])).nodes[0], find)
        wrong_find = _dc.replace(find, find=_t.FindSpec(_dc.replace(query, name='undeclared')))
        self._reject(_t.Reason.FOREIGN, lambda: _dc.replace(plan, nodes=(wrong_find, plan.nodes[-1])))
        for field, kind, payload in (
                ('wait', _t.StepKind.WAIT_UNTIL, _t.WaitSpec(unknown, _t.WaitPolicy(100, 1500, 2, 10000, 50, 100, 20))),
                ('assertion', _t.StepKind.ASSERT_TAG, _t.AssertSpec(unknown, _t.Reason.MISSING))):
            good_payload = _dc.replace(payload, predicate=f['entry'])
            good = _t.StepPlan('good', kind, None, None, None, None, **{field: good_payload})
            self.assertEqual(_dc.replace(plan, nodes=(good, plan.nodes[-1])).nodes[0], good)
            bad = _t.StepPlan('bad', kind, None, None, None, None, **{field: payload})
            self._reject(_t.Reason.FOREIGN, lambda: _dc.replace(plan, nodes=(bad, plan.nodes[-1])))
        # Independently constructed ValidatedPlan cannot drop declaration tables.
        self._reject(_t.Reason.FOREIGN, lambda: _dc.replace(f['plan'], queries=()))

    # @anchor:wa.tests.scripts.test_language.ContractControls.test_evidence_provenance_and_incomplete_observations
    def test_evidence_provenance_and_incomplete_observations(self):
        # Predicate: known facts bind exact observation, association and source layout.
        f = self.f
        self.assertIs(f['witness'].facts.visible.observation, f['observation'])
        foreign_source = _dc.replace(f['fact'], source=_t.EvidenceSource.HOST)
        self._reject(_t.Reason.STALE, lambda: _dc.replace(f['witness'], facts=_dc.replace(f['facts'], visible=foreign_source)))
        weak = _t.Fact(_t.Truth.TRUE, _t.EvidenceSource.AX, 7, None)
        self._reject(_t.Reason.TYPE, lambda: _dc.replace(f['facts'], visible=weak))
        other_obs = _dc.replace(f['observation'], publication_identity='other-publication')
        other_fact = _dc.replace(f['fact'], observation=other_obs)
        self._reject(_t.Reason.STALE, lambda: _dc.replace(f['witness'], facts=_dc.replace(f['facts'], visible=other_fact)))
        self._reject(_t.Reason.STALE, lambda: _dc.replace(f['fact'], association=_t.MissingRef('foreign', None)))
        for field in ('association', 'completeness'):
            missing = _t.MissingRef(None, _t.Reason.MISSING)
            obs = _dc.replace(f['observation'], **{field: missing})
            self._reject(_t.Reason.MISSING, lambda: _dc.replace(f['fact'], observation=obs,
                association=obs.association))
            unknown = _dc.replace(f['fact'], value=_t.Truth.UNKNOWN, reason=_t.MissingReason.INCOMPLETE,
                                  observation=obs, association=obs.association)
            facts = _t.TargetFacts(*(unknown for _ in range(7)))
            witness = _dc.replace(f['witness'], observation=obs, facts=facts)
            self.assertIs(witness.facts.visible.value, _t.Truth.UNKNOWN)
        self._reject(_t.Reason.STALE, lambda: _dc.replace(f['fact'], source_schema='foreign-schema'))

    # @anchor:wa.tests.scripts.test_language.ContractControls.test_composite_artifact_and_authority_associations
    def test_composite_artifact_and_authority_associations(self):
        # Predicate: composites cannot carry another principal's artifact.
        f = self.f
        foreign = _dc.replace(f['identity'], scope=_dc.replace(f['scope'], principal='foreign'))
        binding = _t.TargetBinding(f['selector'], f['handle'], f['observation'], f['witness'],
            f['identity'], 'effect', f['scope'], f['authority'], f['handle'])
        intent = _t.StepIntent(f['identity'], 'effect', 0, _t.StepKind.CLICK,
            f['selector'], (), f['post'], f['scope'], f['authority'])
        true = _t.PredicateResult(_t.Truth.TRUE, None)
        outcome = _t.RuntimeOutcome('episode', f['identity'], f['scope'], _t.TerminalStatus.COMPLETED,
            _t.InputDisposition.NOT_SENT, true, true, None, False, (), _t.Reason.MISSING, true)
        for record in (binding, intent, outcome):
            self.assertEqual(record.artifact.scope, record.scope)
            self._reject(_t.Reason.STALE, lambda: _dc.replace(record, artifact=foreign))
        for field in ('policy', 'capability_set', 'schema', 'environment_compatibility'):
            self._reject(_t.Reason.STALE, lambda: _dc.replace(binding,
                authority=_dc.replace(f['authority'], **{field: 2})))
        self._reject(_t.Reason.STALE, lambda: _dc.replace(binding,
            authority=_dc.replace(f['authority'], calibration_certificate=2, calibration_reason=None)))

    # @anchor:wa.tests.scripts.test_language.ContractControls.test_acquisition_disposition_and_ownership
    def test_acquisition_disposition_and_ownership(self):
        # Predicate: waits report acquisition and retained ownership without APPLIED input.
        f = self.f
        acquired = _t.AcquisitionResult(f['observation'], None, _t.AcquisitionDisposition.ACQUIRED,
            _t.Ownership.RETAINED, f['handle'], None, _t.InputDisposition.NOT_SENT)
        self.assertIs(acquired.owner_token, f['handle'])
        for ownership in _t.Ownership:
            self.assertIs(_dc.replace(acquired, ownership=ownership).ownership, ownership)
        self._reject(_t.Reason.MISSING, lambda: _dc.replace(acquired, owner_token=None))
        self._reject(_t.Reason.SCHEMA, lambda: _dc.replace(acquired, disposition=_t.AcquisitionDisposition.REFUSED))
        for disposition in (_t.AcquisitionDisposition.REFUSED, _t.AcquisitionDisposition.CANCELLED):
            refused = _t.AcquisitionResult(None, _t.Reason.CANCELLED, disposition, None, None,
                _t.MissingReason.ABSENT, _t.InputDisposition.NOT_SENT)
            self.assertIsNone(refused.owner_token)
            retained = _dc.replace(refused, ownership=_t.Ownership.RETAINED,
                                   owner_token=f['handle'], ownership_reason=None)
            self.assertIs(retained.owner_token, f['handle'])  # Refusal does not release outstanding ownership.
            self._reject(_t.Reason.MISSING, lambda: _dc.replace(refused, ownership_reason=None))
        self._reject(_t.Reason.SCHEMA, lambda: _dc.replace(acquired, input_disposition=_t.InputDisposition.APPLIED))
        self._reject(_t.Reason.TYPE, lambda: _dc.replace(acquired, disposition='acquired'))
        self.assertNotIn(f['handle'].token, repr(acquired))

    # @anchor:wa.tests.scripts.test_language.ContractControls.test_direct_constructor_diagnostic_privacy
    def test_direct_constructor_diagnostic_privacy(self):
        # Predicate: all record constructors bound errors without caller keys/values.
        canary = 'private-canary-history-title'
        records = [obj for obj in vars(_t).values() if isinstance(obj, type)
                   and issubclass(obj, _t._Record) and obj is not _t._Record]
        for record in records:
            with self.subTest(record=record.__name__):
                with self.assertRaises(_t.ContractError) as caught:
                    record(**{canary: canary})
                self.assertIs(caught.exception.reason, _t.Reason.SCHEMA)
                self.assertNotIn(canary, str(caught.exception))
                self.assertNotIn(canary, repr(caught.exception))
                self.assertIsNone(caught.exception.__context__)
        self.assertEqual(_t.PrivateRef('owner', 'incarnation', canary).owner, 'owner')
        self._reject(_t.Reason.SCHEMA, lambda: _t.PrivateRef('owner', 'incarnation', canary, canary))
        private = _t.freeze_entries([(canary, {'history': [canary]})])
        self.assertEqual(private[0].value[0].value[0], canary)
        self.assertNotIn(canary, repr(private))
        self.assertNotIn(canary, repr(_t.MetadataEntry(canary, canary)))
        self._reject(_t.Reason.BOUNDS, lambda: _t.freeze_entries([('large', 'a' * 262145)]))
        self.assertEqual(_t.freeze_entries([('small', 'a' * 10)])[0].value, 'a' * 10)

    # @anchor:wa.tests.scripts.test_language.ContractControls.test_query_selector_table_closure
    def test_query_selector_table_closure(self):
        # Predicate: FIND and nested predicates resolve only declared selectors.
        f = self.f
        plan = f['plan'].definition
        query = plan.queries[0]
        find = _t.StepPlan('find', _t.StepKind.FIND, None, None, None, None,
                           find=_t.FindSpec(query))
        positive = _dc.replace(plan, nodes=(find,) + plan.nodes)
        self.assertEqual(_dc.replace(f['plan'], definition=positive,
            nodes=positive.nodes).definition, positive)
        foreign_selector = _dc.replace(query.selector, role='undeclared-role')
        foreign_query = _dc.replace(query, selector=foreign_selector)
        bad_find = _dc.replace(find, find=_t.FindSpec(foreign_query))
        for nodes in (plan.nodes, (bad_find,) + plan.nodes):
            self._reject(_t.Reason.FOREIGN, lambda: _dc.replace(plan,
                nodes=nodes, queries=(foreign_query,)))
        # Unused declarations must be closed too, not just traversed operands.
        unused = _dc.replace(foreign_query, name='unused')
        self._reject(_t.Reason.FOREIGN, lambda: _dc.replace(plan,
            queries=plan.queries + (unused,)))
        nested = _dc.replace(f['entry'], operator=_t.PredicateOperator.NOT,
                             operands=(), children=(f['entry'],))
        self.assertEqual(_dc.replace(plan, entry=nested, header=_dc.replace(plan.header, entry=nested)).entry, nested)
        self._reject(_t.Reason.FOREIGN, lambda: _dc.replace(plan,
            entry=nested, header=_dc.replace(plan.header, entry=nested), queries=(foreign_query,)))

    # @anchor:wa.tests.scripts.test_language.ContractControls.test_intent_action_argument_pairing
    def test_intent_action_argument_pairing(self):
        # Predicate: direct intents use the same action/slot rules as effects.
        f = self.f
        intent = _t.StepIntent(f['identity'], 'effect', 0, _t.ActionKind.CLICK,
            f['selector'], (), f['post'], f['scope'], f['authority'])
        slot = _t.SlotDescriptor('input', _t.ValueKind.TEXT, _t.Purpose.INPUT,
            f['handle'], 'episode', f['scope'], 'form', 'actuator', None)
        for action in (_t.ActionKind.TYPE, _t.ActionKind.KEY):
            selector = _dc.replace(f['selector'], action=action)
            self._reject(_t.Reason.MISSING, lambda: _dc.replace(intent,
                action=action, selector=selector))
            valid_slot = slot if action is _t.ActionKind.TYPE else _dc.replace(slot,
                kind=_t.ValueKind.KEY,
                key_vocabulary=_t.HostKeyVocabulary('test', ('ENTER',), f['handle']))
            positive = _dc.replace(intent, action=action, selector=selector,
                                   arguments=(valid_slot,))
            self.assertEqual(positive.arguments, (valid_slot,))
            self._reject(_t.Reason.MISSING, lambda: _dc.replace(positive,
                arguments=(valid_slot, _dc.replace(valid_slot, slot_name="other"))))
            if action is _t.ActionKind.KEY:
                self._reject(_t.Reason.TYPE, lambda: _dc.replace(positive, arguments=(slot,)))
                self._reject(_t.Reason.MISSING, lambda: _dc.replace(positive,
                    arguments=(_dc.replace(valid_slot, key_vocabulary=None),)))

    # @anchor:wa.tests.scripts.test_language.ContractControls.test_plan_identity_contract_consistency
    def test_plan_identity_contract_consistency(self):
        # Predicate: all shared header/artifact identity fields agree.
        plan = self.f['plan'].definition
        self.assertEqual(_dc.replace(self.f['plan']).definition, plan)
        for field, value in (('goal_contract', 'other-goal'),
                             ('signature_contract', 'other-signature'),
                             ('logical_id', 'other-logical'), ('revision', 2),
                             ('supplied_pins', _dc.replace(plan.identity, goal_contract='other'))):
            with self.subTest(field=field):
                reason = _t.Reason.SCHEMA if field in ('logical_id', 'revision') else _t.Reason.STALE
                self._reject(reason, lambda: _dc.replace(plan,
                    header=_dc.replace(plan.header, **{field: value})))
        self.assertIsNone(_dc.replace(plan, header=_dc.replace(plan.header,
            supplied_pins=None, pins_reason=_t.Reason.MISSING)).header.supplied_pins)

    # @anchor:wa.tests.scripts.test_language.ContractControls.test_unicode_exception_context_privacy
    def test_unicode_exception_context_privacy(self):
        # Predicate: malformed Unicode cannot survive in chained exceptions.
        canary = 'private-history-canary'
        for operation in (
            lambda: _t.MetadataEntry('name', canary + '\ud800'),
            lambda: _t.PrivateRef('owner', 'incarnation', canary + '\udfff'),
            lambda: _t.freeze_entries([('name', canary + '\ud800')])):
            with self.assertRaises(_t.ContractError) as caught:
                operation()
            error = caught.exception
            self.assertIs(error.reason, _t.Reason.TYPE)
            self.assertIsNone(error.__context__)
            self.assertIsNone(error.__cause__)
            self.assertNotIn(canary, repr(error))
        self.assertEqual(_t.MetadataEntry('name', canary + 'é').value, canary + 'é')

    # @anchor:wa.tests.scripts.test_language.ContractControls.test_named_ingress_stops_unknown_stream
    def test_named_ingress_stops_unknown_stream(self):
        # Predicate: unknown keys fail before consuming or retaining the stream.
        consumed = []
        def entries(first):
            for index in range(10000):
                consumed.append(index)
                yield (first if index == 0 else 'private-unknown', 'private-value')
        for key in ('x', 'private-canary' * 10000, 'ｏｗｎｅｒ'):
            consumed.clear()
            self._reject(_t.Reason.SCHEMA, lambda: _t.record_from_entries(
                _t.PrivateRef, entries(key)))
            self.assertEqual(consumed, [0])
        valid = [('owner', 'owner'), ('incarnation', 'incarnation'), ('token', 'private')]
        self.assertEqual(_t.record_from_entries(_t.PrivateRef, iter(valid)).owner, 'owner')
        consumed.clear()
        def overflow():
            yield from valid
            yield from entries('owner')
        self._reject(_t.Reason.SCHEMA, lambda: _t.record_from_entries(_t.PrivateRef, overflow()))
        self.assertEqual(consumed, [0])



    # @anchor:wa.tests.scripts.test_language.ContractControls.test_header_entry_binding
    def test_header_entry_binding(self):
        # Predicate: header and executable entry are one typed declaration.
        plan = self.f['plan'].definition
        self.assertEqual(plan.header.entry, plan.entry)
        self._reject(_t.Reason.TYPE, lambda: _dc.replace(plan.header, entry='undeclared'))
        changed = _dc.replace(plan.entry, operator=_t.PredicateOperator.NOT,
                              operands=(), children=(plan.entry,))
        self._reject(_t.Reason.CONFLICT, lambda: _dc.replace(plan, entry=changed))
        self._reject(_t.Reason.CONFLICT, lambda: _dc.replace(plan,
            header=_dc.replace(plan.header, entry=changed)))
        valid = _dc.replace(plan, entry=changed, header=_dc.replace(plan.header, entry=changed))
        self.assertEqual(valid.entry, valid.header.entry)
        unknown = _dc.replace(plan.entry, operands=(
            _dc.replace(plan.entry.operands[0], query_id='undeclared'),))
        self._reject(_t.Reason.FOREIGN, lambda: _dc.replace(plan, entry=unknown,
            header=_dc.replace(plan.header, entry=unknown)))

    # @anchor:wa.tests.scripts.test_language.ContractControls.test_nested_private_reference_repr
    def test_nested_private_reference_repr(self):
        # Predicate: private references remain usable but never appear in repr.
        for field in ('association', 'completeness', 'projection'):
            canary = 'synthetic-private-' + field
            ref = _t.MissingRef(canary, None)
            observation = _dc.replace(self.f['observation'], **{field: ref})
            self.assertEqual(getattr(observation, field).value, canary)
            self.assertNotIn(canary, repr(ref))
            self.assertNotIn(canary, repr(observation))
            # Enclosing frozen results must not re-expose nested references.
            predicate = _t.PredicateResult(_t.Truth.UNKNOWN, _t.Reason.MISSING)
            duration = _t.MeasuredValue(None, 'ns', _t.MissingRef(None, _t.Reason.MISSING), _t.Reason.MISSING)
            result = _t.StepResult(observation, observation, None, None,
                _t.InputDisposition.NOT_SENT, predicate, predicate, duration, (), _t.Reason.MISSING)
            self.assertNotIn(canary, repr(result))
        self.assertIn(_t.Reason.MISSING.value, repr(_t.MissingRef(None, _t.Reason.MISSING)))

    # @anchor:wa.tests.scripts.test_language.ContractControls.test_reservation_cancellation_port_exchange
    def test_reservation_cancellation_port_exchange(self):
        # Predicate: frozen ports exchange minted grants and exact release evidence.
        for port in (_t.ReservationPort, _t.CancellationPort):
            for name in ('reserve', 'grow', 'release_unused') if port is _t.ReservationPort else ('request', 'verify'):
                self.assertEqual(_hints(getattr(port, name)), _hints(getattr(_ReservationOwner, name)))
        f = self.f
        owner = _ReservationOwner(f['handle'], 256)
        request = _t.ReservationRequest(f['handle'], f['handle'], f['scope'],
            'parse', 128, (), (), 1, 1000)
        cancellation_policy = _t.CancellationPolicy(1000, 'escalate', 'join', 'supervisor', 2000, 3000, 4, 4096)
        policy = _t.ResourcePolicy(4096, 2048, 128, 10, 1000, 256, 64, cancellation_policy, 1)
        self.assertIs(owner.reserve(request, None).reason, _t.Reason.MISSING)
        self.assertEqual(owner.charged, 0)
        outcome = owner.reserve(request, policy)
        grant = outcome.grant
        self.assertIsNone(outcome.reason)
        self.assertTrue(owner.registry.check(grant.reservation, f['handle'], 1))
        self._reject(_t.Reason.MISSING, lambda: _t.ReservationOutcome(None, None))
        self._reject(_t.Reason.MISSING, lambda: _t.ReservationOutcome(grant, _t.Reason.CAPACITY))
        self.assertIs(owner.grow(_dc.replace(grant), request, policy).reason, _t.Reason.FOREIGN)
        self.assertIs(owner.grow(grant, _dc.replace(request, peak_bytes=257), policy).reason, _t.Reason.CAPACITY)
        self.assertEqual(owner.charged, 128)
        grown = owner.grow(grant, _dc.replace(request, peak_bytes=192), policy).grant
        self.assertIs(grown.reservation, grant.reservation)
        self.assertEqual(grown.accounting_revision, request.accounting_revision)
        self.assertEqual(grown.absolute_deadline_ns, request.absolute_deadline_ns)
        self.assertEqual(owner.charged, 192)
        cancellation_policy = _t.CancellationPolicy(100, 'escalate', 'join', 'supervisor', 200, 300, 4, 256)
        holder = _t.HolderContract('consumer', _t.Purpose.QUERY, f['scope'], 192,
            1000, grown.reservation, grown.reservation, cancellation_policy)
        pending = owner.request(holder)
        self.assertIs(owner.release_unused(grown, 192, pending).result, _t.Truth.FALSE)
        self.assertEqual(owner.charged, 192)
        completed = owner.complete()
        self.assertIs(owner.verify(holder, completed).result, _t.Truth.TRUE)
        self.assertIs(owner.verify(_dc.replace(holder), completed).result, _t.Truth.FALSE)
        forged = _dc.replace(completed, released_tokens=(_dc.replace(grown.reservation),))
        self.assertIs(owner.release_unused(grown, 192, forged).result, _t.Truth.FALSE)
        self.assertIs(owner.release_unused(grant, 192, completed).result, _t.Truth.FALSE)
        self.assertIs(owner.release_unused(grown, 192, completed).result, _t.Truth.TRUE)
        self.assertEqual(owner.charged, 0)
        self.assertIs(owner.release_unused(grown, 192, completed).result, _t.Truth.TRUE)
        self.assertEqual(owner.charged, 0)

    # @anchor:wa.tests.scripts.test_language.ContractControls.test_authenticated_release_requires_exact_token
    def test_authenticated_release_requires_exact_token(self):
        # Predicate: authenticated completion releases only its exact grant token.
        f = self.f
        for case in ('reconstructed', 'foreign'):
            with self.subTest(token=case):
                owner = _ReservationOwner(f['handle'], 256)
                cancellation = _t.CancellationPolicy(
                    100, 'escalate', 'join', 'supervisor', 200, 300, 4, 256)
                policy = _t.ResourcePolicy(
                    4096, 2048, 128, 10, 1000, 256, 64, cancellation, 1)
                request = _t.ReservationRequest(f['handle'], f['handle'],
                    f['scope'], 'parse', 128, (), (), 1, 1000)
                grant = owner.reserve(request, policy).grant
                holder = _t.HolderContract('consumer', _t.Purpose.QUERY,
                    f['scope'], 128, 1000, grant.reservation,
                    grant.reservation, cancellation)
                owner.request(holder)
                if case == 'reconstructed':
                    token = _dc.replace(grant.reservation)
                    self.assertEqual(token, grant.reservation)
                else:
                    foreign = _Registry(_dc.replace(f['handle'], incarnation='foreign'), 1)
                    token = foreign.mint('reservation', 'synthetic-foreign-token')
                    self.assertNotEqual(token, grant.reservation)
                self.assertIsNot(token, grant.reservation)
                completed = owner.complete((token,))
                # Both identity gates pass; only released-token identity is wrong.
                self.assertIs(owner.grant, grant)
                self.assertIs(owner.completion, completed)
                self.assertTrue(owner.registry.check(grant.reservation, f['handle'], 1))
                self.assertIs(owner.verify(holder, completed).result, _t.Truth.TRUE)
                self.assertTrue(owner.registry.check(completed.evidence, f['handle'], 1))
                charged = owner.charged
                rejected = owner.release_unused(grant, charged, completed)
                self.assertIs(rejected.result, _t.Truth.FALSE)
                self.assertIs(rejected.reason, _t.Reason.FOREIGN)
                self.assertEqual(owner.charged, charged)
                self.assertIs(owner.grant, grant)
                self.assertIs(owner.completion, completed)
                # Positive control: the same grant releases with its exact token.
                valid = owner.complete()
                self.assertIs(owner.verify(holder, valid).result, _t.Truth.TRUE)
                self.assertIs(owner.release_unused(grant, charged, valid).result, _t.Truth.TRUE)
                self.assertEqual(owner.charged, 0)

    # @anchor:wa.tests.scripts.test_language.ContractControls.test_cancellation_proof_request_binding
    def test_cancellation_proof_request_binding(self):
        # Predicate: only the originating current holder/request/grant can release.
        f = self.f
        for case in ('cross_holder', 'cross_scope', 'superseded_request', 'grown_grant'):
            with self.subTest(binding=case):
                owner = _ReservationOwner(f['handle'], 256)
                self._reject(_t.Reason.MISSING, owner.complete)
                cancellation = _t.CancellationPolicy(
                    100, 'escalate', 'join', 'supervisor', 200, 300, 4, 256)
                policy = _t.ResourcePolicy(
                    4096, 2048, 128, 10, 1000, 256, 64, cancellation, 1)
                request = _t.ReservationRequest(f['handle'], f['handle'],
                    f['scope'], 'parse', 128, (), (), 1, 1000)
                grant = owner.reserve(request, policy).grant
                holder = _t.HolderContract('consumer', _t.Purpose.QUERY,
                    f['scope'], 128, 1000, grant.reservation,
                    grant.reservation, cancellation)
                owner.request(holder)
                completed = owner.complete()
                self.assertIs(owner.verify(holder, completed).result, _t.Truth.TRUE)
                if case == 'cross_holder':
                    holder = _dc.replace(holder, consumer_identity='other-consumer')
                    owner.request(holder)
                elif case == 'cross_scope':
                    holder = _dc.replace(holder, scope=_dc.replace(f['scope'], scope_revision=2))
                    owner.request(holder)
                elif case == 'superseded_request':
                    owner.request(holder)  # Even the identical holder starts a new request.
                else:
                    grant = owner.grow(grant, _dc.replace(request, peak_bytes=192), policy).grant
                charged = owner.charged
                # Must-FAIL: prior authenticated evidence cannot follow reassociation.
                rejected = owner.verify(holder, completed)
                self.assertIs(rejected.result, _t.Truth.FALSE)
                self.assertIs(rejected.reason, _t.Reason.FOREIGN)
                rejected = owner.release_unused(grant, 64, completed)
                self.assertIs(rejected.result, _t.Truth.FALSE)
                self.assertIs(rejected.reason, _t.Reason.FOREIGN)
                self.assertEqual(owner.charged, charged)
                if case == 'cross_scope':
                    # Fresh evidence cannot authenticate a foreign scope either.
                    foreign = owner.complete()
                    self.assertIs(owner.verify(holder, foreign).result, _t.Truth.FALSE)
                    self.assertIs(owner.release_unused(grant, 64, foreign).reason, _t.Reason.FOREIGN)
                    self.assertEqual(owner.charged, charged)
                    holder = _dc.replace(holder, scope=f['scope'])
                owner.request(holder)
                valid = owner.complete()
                # Positive control: matching evidence authenticates and refunds once.
                self.assertIs(owner.verify(holder, valid).result, _t.Truth.TRUE)
                self.assertIs(owner.release_unused(grant, 64, valid).result, _t.Truth.TRUE)
                self.assertEqual(owner.charged, charged - 64)

    # @anchor:wa.tests.scripts.test_language.ContractControls.test_release_retry_is_idempotent
    def test_release_retry_is_idempotent(self):
        # Predicate: exact retries succeed once; altered release identities fail.
        f = self.f
        owner = _ReservationOwner(f['handle'], 256)
        cancellation = _t.CancellationPolicy(
            100, 'escalate', 'join', 'supervisor', 200, 300, 4, 256)
        policy = _t.ResourcePolicy(
            4096, 2048, 128, 10, 1000, 256, 64, cancellation, 1)
        request = _t.ReservationRequest(f['handle'], f['handle'],
            f['scope'], 'parse', 128, (), (), 1, 1000)
        grant = owner.reserve(request, policy).grant
        holder = _t.HolderContract('consumer', _t.Purpose.QUERY,
            f['scope'], 128, 1000, grant.reservation, grant.reservation, cancellation)
        owner.request(holder)
        completed = owner.complete()
        first = owner.release_unused(grant, 64, completed)
        self.assertIs(first.result, _t.Truth.TRUE)
        self.assertEqual(owner.charged, 64)
        # Positive control: retain both authenticated evidence and exact outcome.
        self.assertIs(owner.verify(holder, completed).result, _t.Truth.TRUE)
        self.assertIs(owner.release_unused(grant, 64, completed), first)
        self.assertEqual(owner.charged, 64)
        for amount in (32, 128, True):
            rejected = owner.release_unused(grant, amount, completed)
            self.assertIs(rejected.result, _t.Truth.FALSE)
            self.assertIs(rejected.reason, _t.Reason.BOUNDS)
            self.assertEqual(owner.charged, 64)
        self.assertIs(owner.release_unused(_dc.replace(grant), 64, completed).reason, _t.Reason.FOREIGN)
        forged = _dc.replace(completed)
        self.assertIs(owner.release_unused(grant, 64, forged).reason, _t.Reason.FOREIGN)
        self.assertEqual(owner.charged, 64)
        self.assertIs(owner.release_unused(grant, 64, completed), first)
        self.assertEqual(owner.charged, 64)

        # Must-FAIL: fresh authenticated evidence for this request cannot refund twice.
        renewed = owner.complete()
        self.assertIsNot(renewed, completed)
        self.assertIsNot(renewed.evidence, completed.evidence)
        self.assertIs(owner.verify(holder, renewed).result, _t.Truth.TRUE)
        retried = owner.release_unused(grant, 64, renewed)
        self.assertEqual(owner.charged, 64)
        self.assertIs(retried, first)
        # Positive control: both same-proof and successive-proof retries reuse the outcome.
        self.assertIs(owner.release_unused(grant, 64, renewed), first)
        self.assertEqual(owner.charged, 64)
        for amount in (32, 128, True):
            renewed = owner.complete()
            self.assertIs(owner.verify(holder, renewed).result, _t.Truth.TRUE)
            rejected = owner.release_unused(grant, amount, renewed)
            self.assertIs(rejected.result, _t.Truth.FALSE)
            self.assertIs(rejected.reason, _t.Reason.BOUNDS)
            self.assertEqual(owner.charged, 64)

if __name__ == '__main__':
    _unittest.main()
