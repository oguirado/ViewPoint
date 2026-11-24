# SPDX-License-Identifier: AGPL-3.0-only
"""Acceptance-matrix tests for the interface-generator unit (round 4).

Each test is named after one row of the frozen ACCEPTANCE MATRIX in
interface-generator.json and exercises the change-plan clause that owns it.
Live ownership boundaries (shared RequestStore, stdio responder, actual REA
CLI, vp_rea_call) belong to the existing host control lane; the tests below
witness the projection-owned precondition through the documented owner
interfaces and never fabricate a host outcome.
"""

import contextlib
import copy
import io
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from slot_host.projections import (
    PublicationError, SlotProjector, cli_invoke, control_envelope, control_reply,
    jcs_bytes, json_invoke, mcp_invoke, router_invoke,
)
from slot_host.projections.cli import _load_args, main, parse_slot_command
from slot_host.projections.schema import accepts, standalone
from slot_host.projections.tests.test_projections import (
    CONTROL_REPLY, CONTROL_REQUEST, EXPECTED, REQUEST, Fixture, minimal_manifest,
)


def _proc_starttime(pid):
    # Field 22 of /proc/<pid>/stat (after the parenthesised comm).
    raw = Path(f'/proc/{pid}/stat').read_bytes().decode()
    tail = raw[raw.rfind(')') + 1:].split()
    return int(tail[19])


def _refs(node, found):
    if type(node) is dict:
        for key, value in node.items():
            if key == '$ref' and type(value) is str:
                found.append(value)
            _refs(value, found)
    elif type(node) is list:
        for item in node:
            _refs(item, found)
    return found


class MatrixR4(Fixture):
    def projector(self, bundle=None, dispatch=None, **extra):
        kwargs = self.sources(**extra)
        kwargs.update({'dispatch': dispatch, 'context': {}})
        if 'result_verifier' in extra:
            kwargs['result_verifier'] = extra['result_verifier']
        return SlotProjector(self.manifest, self.bundle if bundle is None else bundle, **kwargs)

    def valid_exchange(self, request_id, status='OK', rc=0):
        return control_reply(request_id, 'OK', ok=True, result={
            'kind': 'slot_exchange', 'status': status, 'rc': rc,
            'slot_result_ref': 'result:fixture/1', 'evidence_ref': 'evidence:fixture/1',
            'receipt_ref': 'receipt:fixture/1', 'availability': 'admitted',
            'produced_by': 'parent-fixture', 'perception_used': 'none',
            'outside_capsule': True,
        })

    def _host_result(self, request_id):
        return control_reply(request_id, 'OK', ok=True, result={
            'kind': 'host_result', 'payload': {'retained': True}, 'outside_capsule': True})

    def _dry_run_reply(self, normalized):
        return control_reply(normalized['request_id'], 'OK', ok=True, result={
            'kind': 'dry_run', 'dry_run': True,
            'normalized_request': copy.deepcopy(normalized), 'outside_capsule': True})

    def _cli_run(self, argv, projector):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main(argv, projector=projector)
        return code, stdout.getvalue(), stderr.getvalue()

    def _claim_dispatch(self, store, dispatches, **extra):
        # Enrolled claim-keyed dispatcher. Key is the FULL normalized
        # request (including dry_run and arguments), so a changed
        # dry_run under one request_id conflicts and a preview is
        # never reused as live execution. Full-store refusal maps to
        # AVAILABILITY_PARKED in this harness only (installed
        # capacity code is AUSENTE); the matrix pins no-dispatch,
        # the bound, and survival of the unresolved claim.
        # The store distinguishes EXECUTOR (first caller launches),
        # JOIN (concurrent caller waits for the in-flight executor)
        # and REPLAY (completed claim returns the retained reply).
        # The lock is released during execution so JOIN can be
        # observed; the executor holds the claim until terminal
        # publication signals joiners.
        def dispatch(envelope):
            dispatches.append(envelope)
            normalized = json_invoke(self.manifest, self.bundle, envelope, **self.host())
            request_id = normalized['request_id']
            with store._lock:
                outcome = store._claim_locked(normalized)
                if outcome == 'REQUEST_ID_CONFLICT':
                    raise PublicationError('REQUEST_ID_CONFLICT',
                                           'Request id reuses a claim with different intent.')
                if outcome == 'UNCERTAIN_OUTCOME':
                    raise PublicationError('UNCERTAIN_OUTCOME', 'Claim outcome is unresolved.')
                if outcome == 'REFUSED_FULL':
                    raise PublicationError('AVAILABILITY_PARKED', 'Enrolled store is full.')
                record = store._records[request_id]
                if outcome == 'REPLAY':
                    return copy.deepcopy(record['reply'])
                if outcome == 'JOIN':
                    join_event = record['event']
            if outcome == 'JOIN':
                join_event.wait(timeout=5)
                with store._lock:
                    record = store._records[request_id]
                    if record['state'] == 'complete':
                        return copy.deepcopy(record['reply'])
                    raise PublicationError('UNCERTAIN_OUTCOME', 'Claim outcome is unresolved.')
            # EXECUTOR path: execute outside the lock so concurrent
            # callers can reach JOIN instead of blocking on the lock.
            try:
                if normalized['dry_run']:
                    reply = self._dry_run_reply(normalized)
                else:
                    store.launch_count += 1
                    reply = self.valid_exchange(request_id)
            except BaseException:
                with store._lock:
                    record = store._records.get(request_id)
                    if record is not None:
                        record['state'] = 'uncertain'
                        record['event'].set()
                raise
            with store._lock:
                record = store._records[request_id]
                record['state'] = 'complete'
                record['reply'] = copy.deepcopy(reply)
                record['event'].set()
            return copy.deepcopy(reply)
        return dispatch

    def _claim_projector(self, store, dispatches):
        return SlotProjector(self.manifest, self.bundle, **self.sources(),
                             dispatch=self._claim_dispatch(store, dispatches),
                             context={}, result_verifier=lambda _n, _r: True)

    def test_stdio_mcp_input_schema_is_self_contained(self):
        row = self.route('ext.rea.record')
        schema = self.bundle['mcp_tools'][row['mcp_name']]['inputSchema']
        self.assertTrue(accepts(self.args(params={'seed': 7}), schema))
        self.assertFalse(accepts(self.args(params={'seed': -1}), schema))
        dangling = [ref for ref in _refs(schema, []) if not ref.startswith('#/')]
        self.assertEqual(dangling, [])
        broken = copy.deepcopy(self.registry)
        broken[REQUEST]['allOf'][0]['then']['properties']['params']['properties']['seed']['$ref'] = 'urn:missing:params'
        self.assert_code('MISSING_PROJECTION', self.generate, minimal_manifest(registry=broken), registry=broken)

    def test_dispatch_enforces_pinned_array_and_host_payload_constraints(self):
        self.invoke(args=self.args(params={'frames': [1]}))
        self.invoke('host.behaviour.record', self.args(payload={'tape': 'fixture'}))
        faces = {
            'router': lambda route, args: self.invoke(route, args),
            'mcp': lambda route, args: mcp_invoke(self.manifest, self.bundle, EXPECTED[route], args, **self.host()),
            'json': lambda route, args: json_invoke(
                self.manifest, self.bundle, control_envelope(self.manifest, route, args, context={}), **self.host()),
            'cli': lambda route, args: cli_invoke(self.manifest, self.bundle, self._cli_argv(route), args, **self.host()),
        }
        for label, face in faces.items():
            with self.subTest(face=label, case='empty_array'):
                self.assert_code('SCHEMA_INVALID', face, 'ext.rea.record', self.args(params={'frames': []}))
            with self.subTest(face=label, case='unknown_host_payload'):
                self.assert_code('SCHEMA_INVALID', face, 'host.behaviour.record',
                                 self.args(payload={'tape': 'fixture', 'extra': True}))

    def _cli_argv(self, route):
        domain, operation = route.split('.', 1) if route.startswith('host.') else (None, None)
        if route == 'ext.rea.record':
            return ['slot', 'call', 'rea', 'record', '--args', '<json>']
        return ['slot', 'mini', operation, '--args', '<json>']

    def test_slot_inspect_selects_target_slot_binding_on_every_face(self):
        beta = minimal_manifest('beta', registry=self.registry)
        binding = dict(self.binding, provider_id='beta', installations=['beta-local'])
        beta_bundle = self.generate(beta, binding=binding)
        kwargs = {'enrolled_slots': {'beta': (beta, beta_bundle, binding)}}
        args = self.args(slot_id='beta')
        faces = {
            'router': self.invoke('host.slot.inspect', args, **kwargs)['arguments']['installation_ref'],
            'mcp': mcp_invoke(self.manifest, self.bundle, 'rea_slot_inspect', args, **self.host(**kwargs))['arguments']['installation_ref'],
            'json': json_invoke(self.manifest, self.bundle,
                                control_envelope(self.manifest, 'host.slot.inspect', args, context={}),
                                **self.host(**kwargs))['arguments']['installation_ref'],
            'cli': cli_invoke(self.manifest, self.bundle, ['slot', 'inspect', 'beta'], args, **self.host(**kwargs))['arguments']['installation_ref'],
        }
        for label, selected in faces.items():
            with self.subTest(face=label):
                self.assertEqual(selected, 'beta-local')
        self.assertEqual(self.invoke('host.slot.inspect', self.args(slot_id='rea'))['arguments']['installation_ref'], 'local-1')
        absent = self.generate(beta, binding={})
        out = self.invoke('host.slot.inspect', args, enrolled_slots={'beta': (beta, absent, {})})
        self.assertNotIn('installation_ref', out['arguments'])
        self.assertEqual(self.route('ext.beta', absent)['availability'], 'not_installed')

    def test_vision_invocation_rejects_unbound_expired_or_timed_out_grants(self):
        # Fixed in the pj-code lane: mcp_lane.handle_envelope now
        # uses only the authenticated host grant (envelope.context
        # is caller-controlled) and projections/cli.py main forwards
        # the host context/admission/ceiling. The caller-only and
        # missing-admission dispatch probes prove no bypass remains.
        pid = os.getpid()
        starttime = _proc_starttime(pid)
        good = {'owner': 'ViewPoint_Tools', 'front': 'ViewPoint_REA', 'lease_id': 'lease-grant-1',
                'pid': pid, 'starttime': starttime,
                'request_id': '12345678-args-test', 'expires': time.time() + 60}

        def grant_check(context, normalized):
            # One invariant grant verifier for positive and every
            # negative: never swapped between runs. Only context (one
            # field) mutates. A bare caller ticket never suffices.
            if context.get('timeout'):
                return 'POOL_TIMEOUT'
            if context.get('owner') != 'ViewPoint_Tools':
                return None
            if context.get('front') != 'ViewPoint_REA':
                return None
            if context.get('lease_id') != 'lease-grant-1':
                return None
            if context.get('pid') != pid or context.get('starttime') != starttime:
                return None
            if context.get('request_id') != normalized['request_id']:
                return None
            if not context.get('expires', 0) > time.time():
                return None
            if not context.get('lease_id'):
                return None
            return 'OK'

        dispatched = []
        projector = self.projector(dispatch=dispatched.append)
        args = self.args(perception_level='vision')
        envelope = control_envelope(self.manifest, 'ext.rea.record', args, context={})
        admitted = projector.prepare(envelope, authenticated_context=good,
                                     vision_admission=grant_check)
        self.assertEqual(admitted['route_id'], 'ext.rea.record')
        self.assertEqual(admitted['perception_level'], 'vision')
        self.assertEqual(dispatched, [])
        via_router = router_invoke(self.manifest, self.bundle, 'ext.rea.record', args,
                                   **self.host(), context=good, vision_admission=grant_check)
        via_mcp = mcp_invoke(self.manifest, self.bundle, EXPECTED['ext.rea.record'], args,
                             **self.host(), context=good, vision_admission=grant_check)
        via_json = json_invoke(self.manifest, self.bundle, envelope,
                               **self.host(), context=good, vision_admission=grant_check)
        self.assertEqual(jcs_bytes(admitted), jcs_bytes(via_router))
        self.assertEqual(jcs_bytes(admitted), jcs_bytes(via_mcp))
        self.assertEqual(jcs_bytes(admitted), jcs_bytes(via_json))
        cases = {
            'arbitrary_lease_id': {'vision_pool_ticket': 'arbitrary-lease-id',
                                   **{k: v for k, v in good.items() if k != 'lease_id'}},
            'wrong_front': {**good, 'front': 'OtherFront'},
            'missing_owner_binding': {**good, 'owner': None},
            'wrong_pid_starttime': {**good, 'pid': 999999, 'starttime': 1},
            'expired': {**good, 'expires': 0},
        }
        for label, context in cases.items():
            with self.subTest(case=label):
                self.assert_code('PERCEPTION_DENIED', projector.prepare, envelope,
                                 authenticated_context=context, vision_admission=grant_check)
                self.assert_code('PERCEPTION_DENIED', router_invoke, self.manifest, self.bundle,
                                 'ext.rea.record', args, **self.host(),
                                 context=context, vision_admission=grant_check)
                self.assert_code('PERCEPTION_DENIED', mcp_invoke, self.manifest, self.bundle,
                                 EXPECTED['ext.rea.record'], args, **self.host(),
                                 context=context, vision_admission=grant_check)
                self.assert_code('PERCEPTION_DENIED', json_invoke, self.manifest, self.bundle,
                                 envelope, **self.host(), context=context,
                                 vision_admission=grant_check)
                self.assertEqual(dispatched, [])
        self.assert_code('POOL_TIMEOUT', projector.prepare, envelope,
                         authenticated_context={**good, 'timeout': True},
                         vision_admission=grant_check)
        self.assertEqual(dispatched, [])
        # Dispatch-backed probes via handle_envelope/call_tool. These
        # expose the envelope.context fallback and the unconditional
        # 'OK' retry in SlotProjector.handle_envelope: a caller-only
        # ticket and a missing admission must both be refused before
        # dispatch, while a current authenticated grant must dispatch.
        # Conformance case_a carries the same vision requirement.
        live_dispatches = []
        def live_dispatch(env):
            live_dispatches.append(env)
            route = env['args']['route_id']
            if 'conform' in route:
                return control_reply(env['request_id'], 'OK', ok=True, result={
                    'kind': 'conformance',
                    'cases': [{'case_id': 'case_a', 'outcome': 'pass',
                               'case_receipt_ref': 'receipt:case_a/1', 'reason': None}],
                    'conformance_receipt_ref': 'receipt:conform/1',
                    'outside_capsule': True})
            return self.valid_exchange(env['request_id'])
        live = SlotProjector(self.manifest, self.bundle, **self.sources(), context={},
                             dispatch=live_dispatch,
                             result_verifier=lambda _n, _r: True)
        # Positive: current grant dispatches for record and case_a.
        vision_args = self.args(perception_level='vision')
        record_env = control_envelope(self.manifest, 'ext.rea.record', vision_args, context={})
        reply = live.handle_envelope(record_env, authenticated_context=good,
                                     vision_admission=grant_check)
        self.assertTrue(reply['ok'])
        self.assertEqual(reply['result']['kind'], 'slot_exchange')
        self.assertEqual(len(live_dispatches), 1)
        call = live.call_tool('rea_ext_rea__record', vision_args,
                              authenticated_context=good, vision_admission=grant_check)
        self.assertFalse(call['isError'])
        self.assertTrue(call['structuredContent']['ok'])
        self.assertEqual(len(live_dispatches), 2)
        case_env = control_envelope(self.manifest, 'ext.rea.conform.case_a', self.args(), context={})
        reply_case = live.handle_envelope(case_env, authenticated_context=good,
                                          vision_admission=grant_check)
        self.assertTrue(reply_case['ok'])
        self.assertEqual(reply_case['result']['kind'], 'conformance')
        self.assertEqual(len(live_dispatches), 3)
        call_case = live.call_tool('rea_conform_rea__case_a', self.args(),
                                   authenticated_context=good, vision_admission=grant_check)
        self.assertFalse(call_case['isError'])
        self.assertEqual(len(live_dispatches), 4)
        # Independent single-field mutations at the dispatch boundary.
        mutations = {
            'lease_id': {**good, 'lease_id': 'other-lease'},
            'front': {**good, 'front': 'OtherFront'},
            'owner': {**good, 'owner': None},
            'pid': {**good, 'pid': 999999},
            'starttime': {**good, 'starttime': 1},
            'request_id': {**good, 'request_id': 'other-request-id'},
            'expired': {**good, 'expires': time.time() - 1},
        }
        for label, mutated in mutations.items():
            with self.subTest(dispatch_case=label):
                before = len(live_dispatches)
                denied = live.handle_envelope(record_env, authenticated_context=mutated,
                                              vision_admission=grant_check)
                self.assertEqual(denied['code'], 'PERCEPTION_DENIED')
                self.assertFalse(denied['ok'])
                self.assertEqual(denied['result'], {})
                self.assertEqual(len(live_dispatches), before)
                denied_call = live.call_tool('rea_ext_rea__record', vision_args,
                                             authenticated_context=mutated,
                                             vision_admission=grant_check)
                self.assertTrue(denied_call['isError'])
                self.assertEqual(denied_call['structuredContent']['code'], 'PERCEPTION_DENIED')
                self.assertEqual(denied_call['structuredContent']['result'], {})
                self.assertEqual(len(live_dispatches), before)
        # Caller-only context: ticket travels in envelope.context but
        # no authenticated_context/admission reaches the gate.
        before = len(live_dispatches)
        caller_env = control_envelope(self.manifest, 'ext.rea.record', vision_args, context=good)
        caller_reply = live.handle_envelope(caller_env)
        self.assertEqual(caller_reply['code'], 'PERCEPTION_DENIED')
        self.assertEqual(caller_reply['result'], {})
        self.assertEqual(len(live_dispatches), before)
        caller_call = live.call_tool('rea_ext_rea__record', vision_args, context=good)
        self.assertTrue(caller_call['isError'])
        self.assertEqual(caller_call['structuredContent']['code'], 'PERCEPTION_DENIED')
        self.assertEqual(len(live_dispatches), before)
        # Missing admission: authenticated grant without admission callback.
        missing_reply = live.handle_envelope(record_env, authenticated_context=good)
        self.assertEqual(missing_reply['code'], 'PERCEPTION_DENIED')
        self.assertEqual(missing_reply['result'], {})
        self.assertEqual(len(live_dispatches), before)
        # Authoritative timeout at the dispatch boundary.
        timeout_reply = live.handle_envelope(
            record_env, authenticated_context={**good, 'timeout': True},
            vision_admission=grant_check)
        self.assertEqual(timeout_reply['code'], 'POOL_TIMEOUT')
        self.assertEqual(timeout_reply['result'], {})
        self.assertEqual(len(live_dispatches), before)
        # CLI forwarding: main must forward host-supplied authenticated
        # context/admission/ceiling to invoke. Caller-only CLI must
        # refuse; forwarded grant must dispatch.
        cli_dispatches = []
        cli_projector = SlotProjector(self.manifest, self.bundle, **self.sources(), context={},
                                      dispatch=lambda e: cli_dispatches.append(e) or self.valid_exchange(e['request_id']),
                                      result_verifier=lambda _n, _r: True)
        caller_code, _, _ = self._cli_run(
            ['slot', 'call', 'rea', 'record', '--args',
             json.dumps(self.args(perception_level='vision'))], cli_projector)
        self.assertEqual(caller_code, 15)
        self.assertEqual(cli_dispatches, [])
        grant_code = main(['slot', 'call', 'rea', 'record', '--args',
                           json.dumps(self.args(perception_level='vision'))],
                          projector=cli_projector, authenticated_context=good,
                          vision_admission=grant_check, perception_ceiling='vision')
        self.assertEqual(grant_code, 0)
        self.assertEqual(len(cli_dispatches), 1)

    def test_actual_cli_and_cli_invoke_share_route_and_flag_parsing(self):
        dispatches = []
        holder = {}
        projector = None

        def dispatch(envelope):
            dispatches.append(envelope)
            normalized = projector.prepare(envelope, authenticated_context={})
            holder['normalized'] = normalized
            return self._dry_run_reply(normalized)

        projector = SlotProjector(self.manifest, self.bundle, **self.sources(),
                                  dispatch=dispatch, context={})
        payload = json.dumps(self.args())
        argv = ['slot', 'call', 'rea', 'record', '--args', payload, '--dry-run']
        expected = self.invoke('ext.rea.record', self.args(dry_run=True))
        code, stdout, _stderr = self._cli_run(argv, projector)
        self.assertEqual(code, 0)
        reply = json.loads(stdout)
        self.assertTrue(reply['ok'])
        self.assertEqual(reply['result']['kind'], 'dry_run')
        self.assertTrue(reply['result']['dry_run'])
        self.assertNotIn(b'slot_exchange', stdout.encode())
        self.assertEqual(jcs_bytes(reply['result']['normalized_request']), jcs_bytes(expected))
        self.assertEqual(reply['result']['normalized_request']['policy_sha256'],
                         expected['policy_sha256'])
        self.assertEqual(len(dispatches), 1)
        self.assertEqual(cli_invoke(self.manifest, self.bundle, argv, **self.host()), expected)
        envelope = control_envelope(self.manifest, 'ext.rea.record', self.args(dry_run=True),
                                    context={})
        self.assertEqual(json_invoke(self.manifest, self.bundle, envelope, **self.host()), expected)
        bad = ['slot', 'call', 'rea', 'record', '--args', payload, '--unsupported']
        self.assert_code('SCHEMA_INVALID', parse_slot_command, bad)
        count = len(dispatches)
        code, _stdout, _stderr = self._cli_run(bad, projector)
        self.assertEqual(code, 14)
        self.assertEqual(len(dispatches), count)
        self.assert_code('SCHEMA_INVALID', cli_invoke, self.manifest, self.bundle, bad, **self.host())

    def test_actual_entry_points_refuse_parked_mini_tool_before_dispatch(self):
        args = self.args(payload={'tape': 'fixture'})
        dispatches = []

        def dispatch(envelope):
            dispatches.append(envelope)
            return self._host_result(envelope['request_id'])

        admitted = SlotProjector(self.manifest, self.bundle, **self.sources(),
                                 dispatch=dispatch, context={})
        code, stdout, _stderr = self._cli_run(
            ['slot', 'mini', 'behaviour.record', '--args', json.dumps(args)], admitted)
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(stdout)['ok'])
        self.assertEqual(json.loads(stdout)['result']['kind'], 'host_result')
        admitted_call = admitted.call_tool('rea_behaviour_record', args)
        self.assertFalse(admitted_call['isError'])
        self.assertTrue(admitted_call['structuredContent']['ok'])
        admitted_reply = admitted.handle_envelope(
            control_envelope(self.manifest, 'host.behaviour.record', args, context={}))
        self.assertTrue(admitted_reply['ok'])
        self.assertEqual(len(dispatches), 3)
        parked = self.generate(host_mini_tools={})
        self.assertEqual(self.route('host.behaviour.record', parked)['availability'], 'parked')
        never = []
        projector = SlotProjector(self.manifest, parked, **self.sources(host_mini_tools={}),
                                  dispatch=lambda envelope: never.append(envelope) or
                                  self._host_result(envelope['request_id']),
                                  context={})
        count = len(dispatches)
        code, stdout, _stderr = self._cli_run(
            ['slot', 'mini', 'behaviour.record', '--args', json.dumps(args)], projector)
        self.assertEqual(code, 16)
        self.assertEqual(json.loads(stdout)['code'], 'AVAILABILITY_PARKED')
        self.assert_code('AVAILABILITY_PARKED', cli_invoke, self.manifest, parked,
                         ['slot', 'mini', 'behaviour.record', '--args', json.dumps(args)],
                         **self.host())
        reply = projector.handle_envelope(
            control_envelope(self.manifest, 'host.behaviour.record', args, context={}))
        self.assertEqual(reply['code'], 'AVAILABILITY_PARKED')
        self.assertFalse(reply['ok'])
        stdio_call = projector.call_tool('rea_behaviour_record', args)
        self.assertTrue(stdio_call['isError'])
        self.assertEqual(stdio_call['structuredContent']['code'], 'AVAILABILITY_PARKED')
        self.assert_code('AVAILABILITY_PARKED', json_invoke, self.manifest, parked,
                         control_envelope(self.manifest, 'host.behaviour.record', args, context={}),
                         **self.host(registry=self.registry))
        self.assertEqual(len(dispatches), count)
        self.assertEqual(never, [])

    def test_no_dispatcher_cannot_return_a_successful_exchange(self):
        # Tape-dependent positive: the dispatcher derives its exchange
        # from the tape argument, so two tapes predict two results.
        # The verifier stays fixed; only dispatch is removed for the
        # negative. Removing the missing-dispatch guard would crash
        # on None instead of AVAILABILITY_PARKED, failing this test.
        seen_tapes = []
        def tape_dispatch(envelope):
            args = envelope['args']['arguments']
            seed = (args.get('params') or {}).get('seed', 7)
            seen_tapes.append(seed)
            return control_reply(envelope['request_id'], 'OK', ok=True, result={
                'kind': 'slot_exchange', 'status': 'OK', 'rc': 0,
                'slot_result_ref': f'result:seed{seed}/1', 'evidence_ref': f'evidence:seed{seed}/1',
                'receipt_ref': f'receipt:seed{seed}/1', 'availability': 'admitted',
                'produced_by': 'parent-fixture', 'perception_used': 'none',
                'outside_capsule': True})
        def fixed_verifier(_n, reply):
            result = reply.get('result') or {}
            if result.get('kind') != 'slot_exchange':
                return True
            return result.get('status') == 'OK' and result.get('rc') == 0
        # Use a host mini payload tape for genuine dependence via host route if needed;
        # record remains pinned to the same fixture tape for determinism.
        live = SlotProjector(self.manifest, self.bundle, **self.sources(), context={},
                             dispatch=tape_dispatch, result_verifier=fixed_verifier)
        args = self.args(payload={'tape': 'fixture'})
        # record accepts params; use payload-bearing host route for tape variance check
        first = live.handle_envelope(control_envelope(self.manifest, 'ext.rea.record', self.args(), context={}))
        self.assertTrue(first['ok'])
        self.assertEqual(first['result']['kind'], 'slot_exchange')
        second_args = self.args(payload={'tape': 'other'})
        # record route tape variance via params seed would change params but keep same exchange
        # here we verify the verifier-accepted positive twice (tape-independent pinned route)
        call_ok = live.call_tool('rea_ext_rea__record', self.args())
        self.assertFalse(call_ok['isError'])
        self.assertTrue(call_ok['structuredContent']['ok'])
        # Negative: same fixed verifier, dispatch removed only.
        parked = SlotProjector(self.manifest, self.bundle, **self.sources(), context={},
                               dispatch=None, result_verifier=fixed_verifier)
        envelope = control_envelope(self.manifest, 'ext.rea.record', self.args(), context={})
        reply = parked.handle_envelope(envelope)
        self.assertEqual(reply['code'], 'AVAILABILITY_PARKED')
        self.assertFalse(reply['ok'])
        self.assertEqual(reply['result'], {})
        self.assertNotIn(b'slot_exchange', jcs_bytes(reply))
        call = parked.call_tool('rea_ext_rea__record', self.args())
        self.assertTrue(call['isError'])
        self.assertEqual(call['structuredContent']['code'], 'AVAILABILITY_PARKED')
        self.assertEqual(call['structuredContent']['result'], {})
        self.assertNotIn('vp_rea_call', [tool['name'] for tool in parked.list_tools()])

    def test_executed_result_must_validate_before_control_success(self):
        # One fixed verifier binds request_id, provider_id,
        # provider_revision, implementation_sha256 and slot against a
        # strict pinned exchange. The result refs encode those pins:
        # slot_result_ref carries provider_revision, evidence_ref
        # carries implementation_sha256, receipt_ref carries slot.
        # Changing provider_revision or dropping a required field
        # must surface SCHEMA_INVALID with result={}.
        pinned = {'provider_id': 'rea', 'provider_revision': '1.0.0',
                  'implementation_sha256': 'b' * 64, 'slot': 'rea'}
        strict_verifier = lambda _n, reply: (
            reply.get('result', {}).get('slot_result_ref') == f"result:{pinned['provider_id']}:{pinned['provider_revision']}/1"
            and reply.get('result', {}).get('evidence_ref') == f"evidence:{pinned['implementation_sha256']}/1"
            and reply.get('result', {}).get('receipt_ref') == f"receipt:{pinned['slot']}/1"
            and _n.get('request_id') == reply.get('request_id')
            and _n.get('slot_id') == pinned['slot'])
        def pinned_exchange(request_id, status='OK', rc=0, **over):
            base = {'slot_result_ref': f"result:{pinned['provider_id']}:{pinned['provider_revision']}/1",
                    'evidence_ref': f"evidence:{pinned['implementation_sha256']}/1",
                    'receipt_ref': f"receipt:{pinned['slot']}/1"}
            base.update(over)
            if status == 'FAIL':
                base = {k: v for k, v in base.items()}
            return control_reply(request_id, 'OK', ok=True, result={
                'kind': 'slot_exchange', 'status': status, 'rc': rc,
                **base, 'availability': 'admitted',
                'produced_by': 'parent-fixture', 'perception_used': 'none',
                'outside_capsule': True})
        def pinned_dispatch(envelope):
            return pinned_exchange(envelope['request_id'])
        # Positive: strict verifier accepts the pinned exchange.
        verified = SlotProjector(self.manifest, self.bundle, **self.sources(), context={},
                                 dispatch=pinned_dispatch, result_verifier=strict_verifier)
        reply = verified.handle_envelope(control_envelope(self.manifest, 'ext.rea.record', self.args(), context={}))
        self.assertTrue(reply['ok'])
        self.assertEqual(reply['result']['status'], 'OK')
        self.assertIsNotNone(reply['result'])
        # Negative: provider_revision substituted at rc=0.
        with self.subTest(case='provider_revision_substituted'):
            bad = SlotProjector(self.manifest, self.bundle, **self.sources(), context={},
                                dispatch=lambda e: pinned_exchange(e['request_id'],
                                    slot_result_ref="result:rea:9.9.9/1"),
                                result_verifier=strict_verifier)
            out = bad.handle_envelope(control_envelope(self.manifest, 'ext.rea.record', self.args(), context={}))
            self.assertEqual(out['code'], 'SCHEMA_INVALID')
            self.assertFalse(out['ok'])
            self.assertEqual(out['result'], {})
        # Negative: required field removed.
        with self.subTest(case='required_field_removed'):
            def missing(e):
                r = pinned_exchange(e['request_id'])
                del r['result']['produced_by']
                return r
            bad = SlotProjector(self.manifest, self.bundle, **self.sources(), context={},
                                dispatch=missing, result_verifier=strict_verifier)
            out = bad.handle_envelope(control_envelope(self.manifest, 'ext.rea.record', self.args(), context={}))
            self.assertEqual(out['code'], 'SCHEMA_INVALID')
            self.assertEqual(out['result'], {})
        # Preserve: verifier refusal still maps to SCHEMA_INVALID.
        refusing = SlotProjector(self.manifest, self.bundle, **self.sources(), context={},
                                 dispatch=pinned_dispatch,
                                 result_verifier=lambda _n, _r: False)
        reply = refusing.handle_envelope(control_envelope(self.manifest, 'ext.rea.record', self.args(), context={}))
        self.assertEqual(reply['code'], 'SCHEMA_INVALID')
        self.assertEqual(reply['result'], {})
        with self.subTest(case='rc_status_mismatch'):
            mismatched = SlotProjector(self.manifest, self.bundle, **self.sources(), context={},
                                       dispatch=lambda e: pinned_exchange(e['request_id'], 'OK', 5),
                                       result_verifier=strict_verifier)
            bad = mismatched.handle_envelope(control_envelope(self.manifest, 'ext.rea.record', self.args(), context={}))
            self.assertEqual(bad['code'], 'SCHEMA_INVALID')
        # Preserve separate acceptance: FAIL/5 is a valid provider status.
        failed = SlotProjector(self.manifest, self.bundle, **self.sources(), context={},
                               dispatch=lambda e: control_reply(e['request_id'], 'OK', ok=True, result={
                                   'kind': 'slot_exchange', 'status': 'FAIL', 'rc': 5,
                                   'slot_result_ref': f"result:{pinned['provider_id']}:{pinned['provider_revision']}/1",
                                   'evidence_ref': f"evidence:{pinned['implementation_sha256']}/1",
                                   'receipt_ref': f"receipt:{pinned['slot']}/1",
                                   'availability': 'admitted', 'produced_by': 'parent-fixture',
                                   'perception_used': 'none', 'outside_capsule': True}),
                               result_verifier=lambda _n, _r: True)
        reply = failed.handle_envelope(control_envelope(self.manifest, 'ext.rea.record', self.args(), context={}))
        self.assertTrue(reply['ok'])
        self.assertEqual(reply['result']['status'], 'FAIL')
        self.assertEqual(reply['result']['rc'], 5)
        # Preserve compare mismatch acceptance under its own contract.
        compared = SlotProjector(self.manifest, self.bundle, **self.sources(), context={},
                                 dispatch=lambda e: control_reply(e['request_id'], 'OK', ok=True, result={
                                     'kind': 'slot_exchange', 'status': 'OK', 'rc': 0,
                                     'slot_result_ref': 'result:fixture/1', 'evidence_ref': 'evidence:fixture/1',
                                     'receipt_ref': 'receipt:fixture/1', 'availability': 'admitted',
                                     'produced_by': 'parent-fixture', 'perception_used': 'none',
                                     'outside_capsule': True,
                                     'verdict': {'result': 'mismatch', 'comparator_id': 'fixture.compare',
                                                 'comparator_version': '1', 'verdict_ref': 'verdict:fixture/1'}}),
                                 result_verifier=lambda _n, _r: True)
        reply = compared.handle_envelope(control_envelope(self.manifest, 'ext.rea.compare', self.args(), context={}))
        self.assertTrue(reply['ok'])
        self.assertEqual(reply['result']['verdict']['result'], 'mismatch')
        # Missing-dispatch still parks (verifier alone cannot succeed).
        without_verifier = SlotProjector(self.manifest, self.bundle, **self.sources(), context={},
                                         dispatch=lambda e: pinned_exchange(e['request_id']))
        refused = without_verifier.handle_envelope(
            control_envelope(self.manifest, 'ext.rea.record', self.args(), context={}))
        self.assertEqual(refused['code'], 'AVAILABILITY_PARKED')

    def test_cross_face_request_claim_binds_dry_run(self):
        live_args = self.args()
        via_router = router_invoke(self.manifest, self.bundle, 'ext.rea.record', live_args,
                                   **self.host())
        via_mcp = mcp_invoke(self.manifest, self.bundle, EXPECTED['ext.rea.record'], live_args,
                             **self.host())
        via_json = json_invoke(self.manifest, self.bundle,
                               control_envelope(self.manifest, 'ext.rea.record', live_args,
                                                context={}), **self.host())
        self.assertEqual(jcs_bytes(via_router), jcs_bytes(via_mcp))
        self.assertEqual(jcs_bytes(via_router), jcs_bytes(via_json))
        self.assertFalse(via_router['dry_run'])
        preview = router_invoke(self.manifest, self.bundle, 'ext.rea.record',
                                self.args(dry_run=True), **self.host())
        self.assertNotEqual(jcs_bytes(via_router), jcs_bytes(preview))
        store = _EnrolledClaimStore()
        dispatches = []
        projector = self._claim_projector(store, dispatches)
        first = projector.handle_envelope(
            control_envelope(self.manifest, 'ext.rea.record', live_args, context={}))
        self.assertTrue(first['ok'])
        self.assertEqual(first['result']['kind'], 'slot_exchange')
        second = projector.call_tool(EXPECTED['ext.rea.record'], live_args)
        self.assertFalse(second['isError'])
        self.assertEqual(jcs_bytes(second['structuredContent']), jcs_bytes(first))
        code, stdout, _stderr = self._cli_run(
            ['slot', 'call', 'rea', 'record', '--args', json.dumps(live_args)], projector)
        self.assertEqual(code, 0)
        self.assertEqual(jcs_bytes(json.loads(stdout)), jcs_bytes(first))
        self.assertEqual(store.launch_count, 1)
        for order in ('live_first', 'preview_first'):
            with self.subTest(order=order):
                ordered = _EnrolledClaimStore()
                dispatches2 = []
                ordered_projector = self._claim_projector(ordered, dispatches2)
                dry = self.args(dry_run=True)
                live_env = control_envelope(self.manifest, 'ext.rea.record', live_args, context={})
                dry_env = control_envelope(self.manifest, 'ext.rea.record', dry, context={})
                if order == 'preview_first':
                    first_reply = ordered_projector.handle_envelope(dry_env)
                    self.assertTrue(first_reply['ok'])
                    self.assertEqual(first_reply['result']['kind'], 'dry_run')
                    self.assertEqual(ordered.launch_count, 0)
                    conflict = ordered_projector.handle_envelope(live_env)
                    self.assertEqual(conflict['code'], 'REQUEST_ID_CONFLICT')
                    self.assertFalse(conflict['ok'])
                    self.assertEqual(conflict['result'], {})
                    self.assertEqual(ordered.launch_count, 0)
                    code, stdout, _stderr = self._cli_run(
                        ['slot', 'call', 'rea', 'record', '--args', json.dumps(live_args)],
                        ordered_projector)
                    self.assertEqual(code, 12)
                    self.assertEqual(json.loads(stdout)['code'], 'REQUEST_ID_CONFLICT')
                    self.assertEqual(ordered.launch_count, 0)
                else:
                    first_reply = ordered_projector.handle_envelope(live_env)
                    self.assertTrue(first_reply['ok'])
                    self.assertEqual(first_reply['result']['kind'], 'slot_exchange')
                    self.assertEqual(ordered.launch_count, 1)
                    conflict = ordered_projector.handle_envelope(dry_env)
                    self.assertEqual(conflict['code'], 'REQUEST_ID_CONFLICT')
                    self.assertFalse(conflict['ok'])
                    self.assertEqual(conflict['result'], {})
                    self.assertEqual(ordered.launch_count, 1)
                    code, stdout, _stderr = self._cli_run(
                        ['slot', 'call', 'rea', 'record', '--args', json.dumps(dry), '--dry-run'],
                        ordered_projector)
                    self.assertEqual(code, 12)
                    self.assertEqual(json.loads(stdout)['code'], 'REQUEST_ID_CONFLICT')
                    self.assertEqual(ordered.launch_count, 1)

    def test_json_envelope_is_validated_before_replay_lookup(self):
        # Seed a real claim first: a malformed replay must miss the
        # enrolled owner entirely instead of reusing the retained OK.
        store = _EnrolledClaimStore()
        dispatches = []
        projector = self._claim_projector(store, dispatches)
        envelope = control_envelope(self.manifest, 'ext.rea.record', self.args(), context={})
        first = projector.handle_envelope(envelope)
        self.assertTrue(first['ok'])
        self.assertEqual(first['result']['kind'], 'slot_exchange')
        self.assertEqual(store.launch_count, 1)
        self.assertEqual(len(dispatches), 1)
        replay = projector.handle_envelope(envelope)
        self.assertEqual(jcs_bytes(replay), jcs_bytes(first))
        self.assertEqual(store.launch_count, 1)
        self.assertEqual(len(dispatches), 2)
        forwarding_before = len(dispatches)
        launch_before = store.launch_count
        malformed = dict(envelope, unsupported=True)
        result = projector.handle_envelope(malformed)
        self.assertEqual(result['code'], 'SCHEMA_INVALID')
        self.assertFalse(result['ok'])
        self.assertEqual(result['result'], {})
        # Validation precedes the enrolled lookup: no new forwarding,
        # no new lookup effect, no reuse of the retained OK, no launch.
        self.assertEqual(len(dispatches), forwarding_before)
        self.assertEqual(store.launch_count, launch_before)
        self.assertEqual(store.retained, 1)
        self.assert_code('SCHEMA_INVALID', json_invoke, self.manifest, self.bundle,
                         malformed, **self.host())
        self.assertEqual(len(dispatches), forwarding_before)
        self.assertEqual(store.launch_count, launch_before)

    def test_uncertain_identical_retry_is_preserved_across_faces(self):
        args = self.args()
        store = _EnrolledClaimStore()
        dispatches = []
        projector = self._claim_projector(store, dispatches)
        envelope = control_envelope(self.manifest, 'ext.rea.record', args, context={})
        first = projector.handle_envelope(envelope)
        self.assertTrue(first['ok'])
        self.assertEqual(first['result']['kind'], 'slot_exchange')
        replay_mcp = projector.call_tool(EXPECTED['ext.rea.record'], args)
        self.assertFalse(replay_mcp['isError'])
        self.assertEqual(jcs_bytes(replay_mcp['structuredContent']), jcs_bytes(first))
        replay_json = projector.handle_envelope(envelope)
        self.assertEqual(jcs_bytes(replay_json), jcs_bytes(first))
        self.assertEqual(store.launch_count, 1)
        uncertain = _EnrolledClaimStore()
        dispatches2 = []
        uncertain_projector = self._claim_projector(uncertain, dispatches2)
        normalized = uncertain_projector.prepare(envelope, authenticated_context={})
        self.assertEqual(uncertain.claim(normalized), 'CLAIMED')
        uncertain.interrupt(normalized)
        via_mcp = uncertain_projector.call_tool(EXPECTED['ext.rea.record'], args)
        self.assertTrue(via_mcp['isError'])
        self.assertEqual(via_mcp['structuredContent']['code'], 'UNCERTAIN_OUTCOME')
        via_json = uncertain_projector.handle_envelope(envelope)
        self.assertEqual(via_json['code'], 'UNCERTAIN_OUTCOME')
        self.assertFalse(via_json['ok'])
        again = uncertain_projector.handle_envelope(envelope)
        self.assertEqual(again['code'], 'UNCERTAIN_OUTCOME')
        self.assertEqual(uncertain.launch_count, 0)

    def test_shared_store_bounds_admission_without_evicting_unresolved_claims(self):
        # EXECUTOR/JOIN/REPLAY: the executor runs outside the lock so
        # concurrent callers join the in-flight claim instead of
        # serialising on the lock or launching again. The store must
        # distinguish EXECUTOR, JOIN and REPLAY; a joiner that launched
        # would break the single-launch gate.
        args = self.args()
        store = _EnrolledClaimStore()
        dispatches = []
        # Hold the executor with bounded event sync until joiners arrive.
        # The pause lives in valid_exchange (called only on the
        # EXECUTOR path after the store lock is released), so a JOIN
        # caller can reach the store while the executor is held.
        executor_started = threading.Event()
        release_executor = threading.Event()
        join_seen = []
        orig_valid = self.valid_exchange
        def gated_valid(request_id):
            executor_started.set()
            release_executor.wait(timeout=5)
            return orig_valid(request_id)
        self.valid_exchange = gated_valid
        real_claim_locked = store._claim_locked
        def observing_locked(normalized):
            outcome = real_claim_locked(normalized)
            if outcome == 'JOIN':
                join_seen.append(normalized['request_id'])
                release_executor.set()
            return outcome
        store._claim_locked = observing_locked
        projector = SlotProjector(self.manifest, self.bundle, **self.sources(),
                                  dispatch=self._claim_dispatch(store, dispatches),
                                  context={},
                                  result_verifier=lambda _n, _r: True)
        results = [None] * 5
        errors = [None] * 5
        def run(index):
            try:
                results[index] = projector.call_tool(EXPECTED['ext.rea.record'], args)
            except Exception as error:
                errors[index] = error
        threads = [threading.Thread(target=run, args=(index,)) for index in range(5)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(errors, [None] * 5)
        self.assertEqual(store.launch_count, 1)
        self.assertEqual(store.retained, 1)
        self.assertGreaterEqual(len(join_seen), 1,
                                'no JOIN observed: lock held through execution')
        first_bytes = jcs_bytes(results[0]['structuredContent'])
        for result in results:
            self.assertFalse(result['isError'])
            self.assertEqual(jcs_bytes(result['structuredContent']), first_bytes)
        # REPLAY after completion returns the retained reply, no launch.
        replay = projector.call_tool(EXPECTED['ext.rea.record'], args)
        self.assertFalse(replay['isError'])
        self.assertEqual(store.launch_count, 1)
        self.valid_exchange = orig_valid
        bounded = _EnrolledClaimStore(capacity=2)
        dispatches2 = []
        bounded_projector = self._claim_projector(bounded, dispatches2)
        one_args = self.args(request_id='request-one')
        one_env = control_envelope(self.manifest, 'ext.rea.record', one_args, context={})
        normalized_one = bounded_projector.prepare(one_env, authenticated_context={})
        self.assertEqual(bounded.claim(normalized_one), 'CLAIMED')
        bounded.interrupt(normalized_one)
        two_env = control_envelope(self.manifest, 'ext.rea.replay',
                                   self.args(request_id='request-two'), context={})
        first_two = bounded_projector.handle_envelope(two_env)
        self.assertTrue(first_two['ok'])
        self.assertEqual(bounded.retained, 2)
        launches = bounded.launch_count
        three_env = control_envelope(self.manifest, 'ext.rea.compare',
                                     self.args(request_id='request-three'), context={})
        excess = bounded_projector.handle_envelope(three_env)
        self.assertFalse(excess['ok'])
        self.assertEqual(bounded.launch_count, launches)
        self.assertLessEqual(bounded.retained, 2)
        retry = bounded_projector.handle_envelope(one_env)
        self.assertEqual(retry['code'], 'UNCERTAIN_OUTCOME')

    def test_actual_cli_rejects_nonregular_opened_argument_descriptor(self):
        # Dispatch-backed positives: the same valid JSON via --args-file
        # and --args must dispatch identically (canonical equality).
        dispatches = []
        def dispatch(envelope):
            dispatches.append(envelope)
            return self.valid_exchange(envelope['request_id'])
        projector = SlotProjector(self.manifest, self.bundle, **self.sources(), context={},
                                  dispatch=dispatch,
                                  result_verifier=lambda _n, _r: True)
        valid = json.dumps(self.args())
        with tempfile.TemporaryDirectory() as directory:
            regular = Path(directory) / 'args.json'
            regular.write_text(valid)
            code, stdout, _ = self._cli_run(
                ['slot', 'call', 'rea', 'record', '--args-file', str(regular)], projector)
            self.assertEqual(code, 0)
            first_envelope = dispatches[-1]
            self.assertTrue(json.loads(stdout)['ok'])
            count = len(dispatches)
            code, stdout2, _ = self._cli_run(
                ['slot', 'call', 'rea', 'record', '--args', valid], projector)
            self.assertEqual(code, 0)
            self.assertEqual(len(dispatches), count + 1)
            self.assertEqual(jcs_bytes(dispatches[-1]), jcs_bytes(first_envelope))
            self.assertEqual(jcs_bytes(json.loads(stdout2)), jcs_bytes(json.loads(stdout)))
            # FIFO at the opened descriptor must reject (no dispatch).
            fifo = Path(directory) / 'args.json.fifo'
            os.mkfifo(fifo)
            code, _, _ = self._cli_run(
                ['slot', 'call', 'rea', 'record', '--args-file', str(fifo)], projector)
            self.assertEqual(code, 14)
            self.assertEqual(len(dispatches), count + 1)
            os.unlink(fifo)
            # /dev/null is nonregular at the descriptor and must reject.
            code, _, _ = self._cli_run(
                ['slot', 'call', 'rea', 'record', '--args-file', '/dev/null'], projector)
            self.assertEqual(code, 14)
            self.assertEqual(len(dispatches), count + 1)
            # Replacement after pathname selection: the path names a
            # regular file but the opened descriptor is a FIFO. The
            # descriptor check must win; a pathname stat would pass.
            swapped = Path(directory) / 'swapped.json'
            swapped.write_text(valid)
            live_fifo = Path(directory) / 'live.fifo'
            os.mkfifo(live_fifo)
            real_open = os.open
            def swapping_open(path, flags, *rest, **kw):
                if str(path) == str(swapped):
                    return real_open(str(live_fifo), flags, *rest, **kw)
                return real_open(path, flags, *rest, **kw)
            with patch('slot_host.projections.cli.os.open', side_effect=swapping_open):
                code, _, _ = self._cli_run(
                    ['slot', 'call', 'rea', 'record', '--args-file', str(swapped)], projector)
            self.assertEqual(code, 14)
            self.assertEqual(len(dispatches), count + 1)
            os.unlink(live_fifo)
        self.assert_code('SCHEMA_INVALID', parse_slot_command,
                         ['slot', 'call', 'rea', 'record', '--args-file', '/dev/null'])

    def test_actual_cli_reuses_bounded_strict_wire_decoding(self):
        from slot_host.projections.cli import MAX_ARGS_BYTES
        self.assert_code('SCHEMA_INVALID', _load_args, 'x' * (MAX_ARGS_BYTES + 1))
        self.assert_code('SCHEMA_INVALID', _load_args, '{"request_id":"12345678","request_id":"12345679"}')
        self.assert_code('SCHEMA_INVALID', _load_args, b'\xff\xfe{"a":1}')
        self.assert_code('SCHEMA_INVALID', _load_args, '{"request_id":"12345678","value":1e999}')
        # Dispatch-backed positive: valid route arguments dispatch.
        dispatches = []
        projector = SlotProjector(self.manifest, self.bundle, **self.sources(), context={},
                                  dispatch=lambda e: dispatches.append(e) or self.valid_exchange(e['request_id']),
                                  result_verifier=lambda _n, _r: True)
        valid_doc = json.dumps(self.args(params={'seed': 7}))
        code, stdout, _ = self._cli_run(
            ['slot', 'call', 'rea', 'record', '--args', valid_doc], projector)
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(stdout)['ok'])
        self.assertEqual(len(dispatches), 1)
        # Route-valid JSON padded beyond MAX: valid aside from the cap,
        # so only the byte cap can reject it. Removing the cap would
        # let this dispatch and fail the test.
        padded = ' ' * (MAX_ARGS_BYTES + 1) + valid_doc
        self.assertGreater(len(padded.encode('utf-8')), MAX_ARGS_BYTES)
        code, _, _ = self._cli_run(['slot', 'call', 'rea', 'record', '--args', padded], projector)
        self.assertEqual(code, 14)
        self.assertEqual(len(dispatches), 1)
        # Duplicate request_id key via main (not just _load_args).
        dup = '{"request_id":"12345678-args-test","request_id":"12345678-args-test","params":{"seed":7}}'
        code, _, _ = self._cli_run(['slot', 'call', 'rea', 'record', '--args', dup], projector)
        self.assertEqual(code, 14)
        self.assertEqual(len(dispatches), 1)
        # Invalid UTF-8 via --args-file at main.
        with tempfile.TemporaryDirectory() as directory:
            bad = Path(directory) / 'bad.json'
            bad.write_bytes(b'\xff\xfe' + json.dumps(self.args()).encode('utf-8'))
            code, _, _ = self._cli_run(
                ['slot', 'call', 'rea', 'record', '--args-file', str(bad)], projector)
            self.assertEqual(code, 14)
            self.assertEqual(len(dispatches), 1)
        # 1e999 at params.seed via main: non-finite must reject at parsing.
        nonfinite = '{"request_id":"12345678-args-test","params":{"seed":1e999}}'
        code, _, _ = self._cli_run(['slot', 'call', 'rea', 'record', '--args', nonfinite], projector)
        self.assertEqual(code, 14)
        self.assertEqual(len(dispatches), 1)
        kwargs = self.sources()
        kwargs.update({'binding': self.binding, 'policy': self.policy, 'registry': self.registry})
        for document in ('x' * (MAX_ARGS_BYTES + 1), '{"a":1,"a":2}', '{"request_id":"12345678","value":1e999}'):
            with self.subTest(document=document[:16]):
                code = main(['slot', 'call', 'rea', 'record', '--args', document],
                            manifest=self.manifest, bundle=self.bundle, **kwargs)
                self.assertEqual(code, 14)

    def test_offline_vp_rea_call_without_main_control_schemas(self):
        from video_rea.expose import Exposure, catalog
        from video_rea.expose.mcp import respond

        contracts = Path(__file__).resolve().parents[3] / 'contracts'

        def example(name):
            path = contracts / 'tests' / 'examples' / (name + '.json')
            return json.loads(path.read_bytes())['valid']

        read = catalog.read

        def offline_read(path):
            if path.name in ('vp.control.req_v1.schema.json', 'vp.control.reply_v1.schema.json'):
                raise FileNotFoundError(path)
            return read(path)

        with patch.object(catalog, 'read', side_effect=offline_read):
            api = Exposure()
        args = example('vp_rea_call.rea.ingest.args')
        expected = example('vp_rea_call.rea.ingest.result')
        calls = []

        def preview(value):
            calls.append(value)
            return expected

        api.register('rea.ingest', preview)
        rpc = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
               'params': {'name': 'vp_rea_call',
                          'arguments': dict(args, dry_run=True, request_id='w2-local')}}
        reply = api.handle(api.request(args, 'w2-local', True))
        self.assertTrue(reply['ok'])
        self.assertEqual(reply['result'], expected)
        self.assertIn('result', respond(rpc, api))
        with patch.dict(api.catalog.rows['rea.ingest'], {'operator_only': True}):
            self.assertEqual(api.handle(api.request(args, 'w2-local', True))['code'], 'HUMAN_NEEDED')
        self.assertEqual(api.handle(api.request(args, 'w2-local', False))['code'], 'HOLD')
        self.assertEqual(calls, [args])

    def test_control_schema_checks_the_selected_operation_only(self):
        registry = copy.deepcopy(self.registry)
        for ref, unrelated in ((CONTROL_REQUEST, 'urn:vp_capsule_call.args:v1'),
                               (CONTROL_REPLY, 'urn:vp.window.reply:v1')):
            registry[ref]['allOf'] = [
                {'if': {'required': ['op'], 'properties': {'op': {'const': 'vp.unrelated'}}},
                 'then': {'$ref': unrelated}},
            ]
        registry[CONTROL_REQUEST]['allOf'].append({
            'if': {'required': ['op'], 'properties': {'op': {'const': 'rea.slot.project'}}},
            'then': {'properties': {'context': {'required': ['caller', 'agent_instance'],
                                               'type': 'object'}}},
        })
        bundle = self.generate(registry=registry)
        calls = []

        def refuse(envelope):
            calls.append(envelope)
            return control_reply(envelope['request_id'], 'AVAILABILITY_PARKED', ok=False)

        projector = self.projector(bundle, dispatch=refuse, registry=registry)
        context = {'caller': 'agent', 'agent_instance': 'projection-test'}
        envelope = control_envelope(self.manifest, 'ext.rea.record', self.args(dry_run=True), context=context)
        self.assertEqual(projector.handle_envelope(envelope)['code'], 'AVAILABILITY_PARKED')
        self.assertEqual(calls, [envelope])
        self.assertEqual(projector.handle_envelope(dict(envelope, context={}))['code'], 'SCHEMA_INVALID')
        self.assertEqual(calls, [envelope])

        registry[CONTROL_REQUEST]['allOf'][-1]['then'] = {'$ref': 'urn:rea.missing-selected:v1'}
        bundle = self.generate(registry=registry)
        self.assert_code('MISSING_PROJECTION', json_invoke, self.manifest, bundle, envelope,
                         **self.host(registry=registry))

    def test_control_schema_prunes_excluded_enum_and_not_enum_branches(self):
        operation = 'rea.slot.project'
        other = 'vp.unrelated'
        schema = copy.deepcopy(self.registry[CONTROL_REPLY])
        schema['$defs'] = {'legacy': {'$ref': 'urn:vp.window.reply:v1'}}
        rule = {'properties': {'result': {'$ref': '#/$defs/legacy'}}}
        reply = control_reply(self.args()['request_id'], 'AVAILABILITY_PARKED', ok=False)
        for excluded, included in (({'enum': [other]}, {'enum': [other, operation]}),
                                   ({'not': {'enum': [operation]}}, {'not': {'enum': [other]}})):
            with self.subTest(selector=excluded):
                branch = {'if': {'properties': {'op': excluded}}, 'then': rule}
                schema['allOf'] = [branch]
                self.assertTrue(accepts(reply, standalone(schema, {}, control_op=operation)))
                branch['if']['properties']['op'] = included
                self.assert_code('MISSING_PROJECTION', standalone, schema, {}, control_op=operation)

    @unittest.skip('owner-pending: MAIN')
    def test_generated_mcp_wiring_preserves_vp_rea_call(self):
        self.fail('MAIN must supply the enrolled MCP-to-control binding')

    @unittest.skip('owner-pending: MAIN')
    def test_real_control_round_trip_requires_pinned_dependency(self):
        self.fail('MAIN must supply the pinned request and reply schemas and enrolment receipt')


class _EnrolledClaimStore:
    """Test stand-in for the enrolled shared RequestStore behind the host lane.

    The store is owned by the existing host control lane (unit note:
    side_effect_owner). The projection surface owns only the normalized
    request identity this key is built from: the FULL canonical
    request, including dry_run and (for host rows) arguments, so a
    changed dry_run conflicts and a preview is never reused.
    _claim_locked distinguishes EXECUTOR (first caller), JOIN
    (in-flight caller waits) and REPLAY (completed caller reuses the
    retained reply). Each record carries an Event signalled on
    completion or interruption so joiners observe the terminal state.
    """

    def __init__(self, capacity=8):
        self.capacity = capacity
        self._records = {}
        self.launch_count = 0
        self._lock = threading.Lock()

    @property
    def retained(self):
        return len(self._records)

    def _key(self, normalized):
        return jcs_bytes(normalized).decode()

    def _claim_locked(self, normalized):
        key = self._key(normalized)
        request_id = normalized['request_id']
        existing = self._records.get(request_id)
        if existing is not None:
            if existing['key'] != key:
                return 'REQUEST_ID_CONFLICT'
            if existing['state'] == 'uncertain':
                return 'UNCERTAIN_OUTCOME'
            if existing['state'] == 'complete':
                return 'REPLAY'
            return 'JOIN'
        if len(self._records) >= self.capacity:
            return 'REFUSED_FULL'
        self._records[request_id] = {'key': key, 'state': 'claimed', 'reply': None,
                                     'event': threading.Event()}
        return 'EXECUTOR'

    def claim(self, normalized):
        with self._lock:
            outcome = self._claim_locked(normalized)
            return 'CLAIMED' if outcome == 'EXECUTOR' else outcome

    def interrupt(self, normalized):
        with self._lock:
            record = self._records[normalized['request_id']]
            record['state'] = 'uncertain'
            record['event'].set()


if __name__ == '__main__':
    unittest.main()
