# SPDX-License-Identifier: AGPL-3.0-only
"""Regression tests for pj-code triage fixes (triage_projections.json)."""

import contextlib
import copy
import io
import json
import unittest

from slot_host.projections import PublicationError, SlotProjector, control_envelope, control_reply
from slot_host.projections.cli import main
from slot_host.projections.tests.test_projections import Fixture, minimal_manifest

_DESCRIBE_KEYS = {
    'schema', 'slot_id', 'status', 'manifest_digest', 'upstream', 'provider_ref',
    'capabilities', 'perception', 'record_replay', 'comparison', 'evidence',
    'conformance', 'discovery', 'operations',
}


def _complete_slot(slot_id):
    return {
        'schema': 'rea.slot_manifest/v2', 'slot_id': slot_id, 'status': 'registered',
        'manifest_digest': 'a' * 64, 'upstream': {}, 'provider_ref': {},
        'capabilities': {}, 'perception': {}, 'record_replay': {}, 'comparison': {},
        'evidence': {}, 'conformance': {}, 'discovery': {}, 'operations': [],
    }


class FixCode(Fixture):
    def _projector(self, bundle=None, dispatch=None, enrolled=None, **extra):
        kwargs = self.sources(**extra)
        kwargs.update({'dispatch': dispatch, 'context': {}})
        if enrolled is not None:
            kwargs['enrolled_slots'] = enrolled
        if 'result_verifier' not in kwargs:
            kwargs['result_verifier'] = lambda _n, _r: True
        if 'result_verifier' in extra:
            kwargs['result_verifier'] = extra['result_verifier']
        return SlotProjector(self.manifest, self.bundle if bundle is None else bundle, **kwargs)

    def _exchange(self, request_id):
        return control_reply(request_id, 'OK', ok=True, result={
            'kind': 'slot_exchange', 'status': 'OK', 'rc': 0,
            'slot_result_ref': 'result:fixture/1', 'evidence_ref': 'evidence:fixture/1',
            'receipt_ref': 'receipt:fixture/1', 'availability': 'admitted',
            'produced_by': 'parent-fixture', 'perception_used': 'none',
            'outside_capsule': True,
        })

    def _vision_projector(self, dispatch):
        return self._projector(dispatch=dispatch)

    def test_vision_bypass_via_envelope_context_refused(self):
        dispatches = []

        def dispatch(envelope):
            dispatches.append(envelope)
            return self._exchange(envelope['request_id'])

        projector = self._vision_projector(dispatch)
        args = self.args(perception_level='vision')
        caller_grant = {'owner': 'ViewPoint_Tools', 'front': 'ViewPoint_REA',
                        'lease_id': 'lease-grant-1', 'pid': 1, 'starttime': 1,
                        'request_id': args['request_id'], 'expires': 9999999999}
        envelope = control_envelope(self.manifest, 'ext.rea.record', args, context=caller_grant)
        reply = projector.handle_envelope(envelope)
        self.assertFalse(reply['ok'])
        self.assertEqual(reply['code'], 'PERCEPTION_DENIED')
        self.assertEqual(reply['result'], {})
        self.assertEqual(dispatches, [])

    def test_missing_admission_vision_refused_no_dispatch(self):
        dispatches = []

        def dispatch(envelope):
            dispatches.append(envelope)
            return self._exchange(envelope['request_id'])

        projector = self._vision_projector(dispatch)
        envelope = control_envelope(self.manifest, 'ext.rea.record',
                                    self.args(perception_level='vision'), context={})
        reply = projector.handle_envelope(envelope)
        self.assertFalse(reply['ok'])
        self.assertEqual(reply['code'], 'PERCEPTION_DENIED')
        self.assertEqual(reply['result'], {})
        self.assertEqual(dispatches, [])
        reply2 = projector.handle_envelope(envelope, authenticated_context=None, vision_admission=None)
        self.assertEqual(reply2['code'], 'PERCEPTION_DENIED')
        self.assertEqual(dispatches, [])

    def test_authenticated_vision_positive_dispatches(self):
        dispatches = []

        def dispatch(envelope):
            dispatches.append(envelope)
            return self._exchange(envelope['request_id'])

        projector = self._vision_projector(dispatch)
        args = self.args(perception_level='vision')
        grant = {'owner': 'ViewPoint_Tools', 'front': 'ViewPoint_REA',
                 'lease_id': 'lease-grant-1', 'pid': 1, 'starttime': 1,
                 'request_id': args['request_id'], 'expires': 9999999999}
        envelope = control_envelope(self.manifest, 'ext.rea.record', args, context={})
        reply = projector.handle_envelope(envelope, authenticated_context=grant,
                                          vision_admission=lambda _c, _n: 'OK')
        self.assertTrue(reply['ok'])
        self.assertEqual(len(dispatches), 1)

    def test_cli_forwards_authenticated_grant(self):
        dispatches = []

        def dispatch(envelope):
            dispatches.append(envelope)
            return self._exchange(envelope['request_id'])

        projector = self._vision_projector(dispatch)
        args = self.args(perception_level='vision')
        grant = {'owner': 'ViewPoint_Tools', 'front': 'ViewPoint_REA',
                 'lease_id': 'lease-grant-1', 'pid': 1, 'starttime': 1,
                 'request_id': args['request_id'], 'expires': 9999999999}
        argv = ['slot', 'call', 'rea', 'record', '--args', json.dumps(args)]
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main(argv, projector=projector, authenticated_context=grant,
                        vision_admission=lambda _c, _n: 'OK')
        self.assertEqual(code, 0)
        self.assertEqual(len(dispatches), 1)
        stdout2, stderr2 = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout2), contextlib.redirect_stderr(stderr2):
            code2 = main(argv, projector=projector, context={})
        self.assertEqual(code2, 15)
        self.assertEqual(len(dispatches), 1)
        self.assertIn('PERCEPTION_DENIED', stdout2.getvalue() + stderr2.getvalue())

    def test_describe_list_exact_keys_reject_outside(self):
        desc_slot = _complete_slot('rea')
        projector = self._projector(
            dispatch=lambda env: control_reply(env['request_id'], 'OK', ok=True, result={
                'kind': 'slot_describe', 'slot': desc_slot, 'outside_capsule': True}))
        reply = projector.handle_envelope(
            control_envelope(self.manifest, 'ext.rea', self.args(op_id='record'), context={}))
        self.assertFalse(reply['ok'])
        self.assertEqual(reply['code'], 'SCHEMA_INVALID')

        admitted = copy.deepcopy(self.route('ext.rea.record'))
        projector2 = self._projector(
            dispatch=lambda env: control_reply(env['request_id'], 'OK', ok=True, result={
                'kind': 'slot_list', 'rows': [admitted], 'outside_capsule': True}))
        reply2 = projector2.handle_envelope(
            control_envelope(self.manifest, 'host.slot.list', self.args(), context={}))
        self.assertFalse(reply2['ok'])
        self.assertEqual(reply2['code'], 'SCHEMA_INVALID')

    def test_list_partial_row_rejected(self):
        projector = self._projector(
            dispatch=lambda env: control_reply(env['request_id'], 'OK', ok=True, result={
                'kind': 'slot_list', 'rows': [{'id': 'ext.rea.conform'}]}))
        reply = projector.handle_envelope(
            control_envelope(self.manifest, 'host.slot.list', self.args(), context={}))
        self.assertFalse(reply['ok'])
        self.assertEqual(reply['code'], 'SCHEMA_INVALID')

    def test_list_parked_row_rejected_when_not_all(self):
        parked = copy.deepcopy(self.route('ext.rea.conform.case_a'))
        parked['availability'] = 'parked'
        row = copy.deepcopy(self.route('host.slot.list'))
        rows = sorted([parked, row], key=lambda item: item['id'])
        projector = self._projector(
            dispatch=lambda env: control_reply(env['request_id'], 'OK', ok=True, result={
                'kind': 'slot_list', 'rows': rows}))
        reply = projector.handle_envelope(
            control_envelope(self.manifest, 'host.slot.list', self.args(), context={}))
        self.assertFalse(reply['ok'])
        self.assertEqual(reply['code'], 'SCHEMA_INVALID')

    def test_list_enrolled_beta_row_accepted_when_all(self):
        beta = minimal_manifest('beta', registry=self.registry)
        binding = dict(self.binding, provider_id='beta', installations=['beta-local'])
        beta_bundle = self.generate(beta, binding=binding)
        enrolled = {'beta': (beta, beta_bundle, binding)}
        beta_row = copy.deepcopy(self.route('ext.beta.record', beta_bundle))
        projector = self._projector(
            dispatch=lambda env: control_reply(env['request_id'], 'OK', ok=True, result={
                'kind': 'slot_list', 'rows': [beta_row]}), enrolled=enrolled)
        reply = projector.handle_envelope(
            control_envelope(self.manifest, 'host.slot.list', self.args(all=True), context={}))
        self.assertTrue(reply['ok'], reply)
        self.assertEqual(reply['result']['rows'][0]['id'], 'ext.beta.record')

    def test_describe_slot_empty_rejected_both_faces(self):
        projector = self._projector(
            dispatch=lambda env: control_reply(env['request_id'], 'OK', ok=True, result={
                'kind': 'slot_describe', 'slot': {}}))
        reply = projector.handle_envelope(
            control_envelope(self.manifest, 'ext.rea', self.args(op_id='record'), context={}))
        self.assertFalse(reply['ok'])
        self.assertEqual(reply['code'], 'SCHEMA_INVALID')

        projector2 = self._projector(
            dispatch=lambda env: control_reply(env['request_id'], 'OK', ok=True, result={
                'kind': 'host_result',
                'payload': {'slot': {}, 'installation_refs': [],
                            'selected_installation_ref': None,
                            'conformance_receipt_ref': None,
                            'unresolved_conformance_refs': []},
                'outside_capsule': True}))
        reply2 = projector2.handle_envelope(
            control_envelope(self.manifest, 'host.slot.inspect', self.args(slot_id='rea'), context={}))
        self.assertFalse(reply2['ok'])
        self.assertEqual(reply2['code'], 'SCHEMA_INVALID')

    def test_describe_slot_complete_accepted_both_faces(self):
        slot = _complete_slot('rea')
        projector = self._projector(
            dispatch=lambda env: control_reply(env['request_id'], 'OK', ok=True, result={
                'kind': 'slot_describe', 'slot': slot}))
        reply = projector.handle_envelope(
            control_envelope(self.manifest, 'ext.rea', self.args(op_id='record'), context={}))
        self.assertTrue(reply['ok'], reply)

        projector2 = self._projector(
            dispatch=lambda env: control_reply(env['request_id'], 'OK', ok=True, result={
                'kind': 'host_result',
                'payload': {'slot': slot, 'installation_refs': [],
                            'selected_installation_ref': None,
                            'conformance_receipt_ref': None,
                            'unresolved_conformance_refs': []},
                'outside_capsule': True}))
        reply2 = projector2.handle_envelope(
            control_envelope(self.manifest, 'host.slot.inspect', self.args(slot_id='rea'), context={}))
        self.assertTrue(reply2['ok'], reply2)

    def test_conformance_not_installed_preserved_under_low_ceiling(self):
        low = dict(self.policy, perception_ceiling='none')
        publication = self.generate(binding={}, policy=low)
        self.assertEqual(self.route('ext.rea.conform', publication)['availability'], 'not_installed')
        self.assertEqual(self.route('ext.rea.conform.case_a', publication)['availability'], 'not_installed')
        publication2 = self.generate(binding=self.binding, policy=low)
        self.assertEqual(self.route('ext.rea.conform', publication2)['availability'], 'blocked')
        self.assertEqual(self.route('ext.rea.conform.case_a', publication2)['availability'], 'blocked')
        publication3 = self.generate(binding={}, policy=None) if False else self.generate(binding={})
        self.assertEqual(self.route('ext.rea.conform', publication3)['availability'], 'not_installed')
        self.assertEqual(self.route('ext.rea.conform.case_a', publication3)['availability'], 'not_installed')

    def test_conformance_vision_case_denied_live(self):
        self.assertEqual(self.route('ext.rea.conform.case_a')['availability'], 'admitted')
        context = {'front': 'ViewPoint_REA'}

        def decision(authenticated, request):
            return 'OK'

        self.assert_code('PERCEPTION_DENIED', self.invoke, 'ext.rea.conform.case_a',
                         context=context, vision_admission=decision, perception_ceiling='none')


if __name__ == '__main__':
    unittest.main()
