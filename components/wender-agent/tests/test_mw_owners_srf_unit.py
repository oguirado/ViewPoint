# @module {"id":"wa.tests.mw_owners_srf_unit","layer":4,"owner":"LEARN","deps":["wa.learn.mw_owners_srf","wa.learn.mw_workflow","wa.tools.mw_run","wa.tests.mw_run_unit","wa.act.actor"],"book":"HB-WA-s4-learn"}
"""Synthetic public composition controls; no producer, capsule or live vision.

MWP7_MUTANT removes a named check in memory, preserving every source file.
Frozen-runner controls are independently exercised in test_mw_run_unit.
"""
import ast
import contextlib
from dataclasses import fields, replace
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import types
import unittest
from unittest.mock import patch

from tools import mw_run as cli
from wender.learn import mw_owners_srf as m
from wender.learn import mw_workflow as w
from wender.act.actor import ActorLock
from tests import test_mw_run_unit as base

YES, NO = base.YES, base.NO
MUTANTS = {
    'ram_publication': ("if value.output_kind == 'ram' and value.value is not None:", 'if False:'),
    'ram_missing': ("value.output_kind == 'ram' and value.value is not None", "value.output_kind == 'ram'"),
    'ram_charge': ('return bound + 128', 'return bound'),
    'observe_dependency': ("elif not callable(getattr(p.capabilities, 'observe_verifier', None)):", 'elif False:'),
    'ram_dependency': ("if p.ram is None or p.replay_policy is None:", 'if False:'),
    'fallback_dependency': ("verifiers.extend(spec['fallback_verifiers'])", 'pass'),
    'verifier_registration': ("return self._observations._register(value)", 'return value'),
    'verifier_state': ("self._state = state\n        value = self._ports.capabilities.observe_verifier", "value = self._ports.capabilities.observe_verifier"),
    'invocation': ("and bool(invocation.live_ack.strip())", ''),
    'announcement': ("_admit(ports.announce_gate(invocation, scope=ports.scope, authority=ports.authority,\n                                 now_ns=ports.clock(), absolute_deadline_ns=ports.limits.deadline_ns))", 'pass'),
    'read_policy': ("getattr(self, f.name) > 0", 'True'),
    'budget': ("getattr(ports.limits, f.name) > 0", 'True'),
    'deadline': ("ports.clock() < ports.limits.deadline_ns", 'True'),
    'canonical_actor': ("_need(key not in _CANONICAL)", 'pass'),
    'dispatch': ("self._surface is not surface", 'False'),
    'right_button': ("(action['kind'] == 'right_click' and action['button'] != 3)", 'False'),
    'scroll_direction': ("plan.payload.get('dir') in ('up', 'down', 'left', 'right')", 'True'),
    'scroll_modifier': ("plan.payload.get('modifier') in (None, 'ctrl', 'shift')", 'True'),
    'double': ("action['kind'] == 'double_click'", 'False'),
    'private_ready': ("if not _yes(p.private_paste.check_ready(self._invocation, p.scope, p.authority)):", 'if False:'),
    'private_transport': ("value.transport == 'owned_clipboard'", 'True'),
    'unicode_bytes': ("len(value.value.encode('utf-8')) <= min(max_bytes, p.limits.slot_bytes)", 'True'),
    'download_shape': ("set(row) == {'name', 'bytes', 'mtime'}", 'True'),
    'download_duplicate': ("_need(name not in names)", 'pass'),
    'download_type': ("type(row['bytes']) is int", "isinstance(row['bytes'], int)"),
    'download_bound': ("len(rows) <= self._policy.rows", 'True'),
    'download_identity': ("return frozenset(self._names[n] for n in names)", "return frozenset(str(row) for row in rows)"),
    'publication': ("self._retained.get(value.publication) is value", 'True'),
    'publication_scope': ("value.scope == p.scope and value.authority == p.authority", 'True'),
    'publication_complete': ("value.complete is True", 'True'),
    'publication_time': ("value.capture_end_ns <= p.clock()", 'True'),
    'publication_bound': ("len(self._retained) < p.native_read.policy.publications", 'True'),
    'native_bound': ("_need(len(buffers[index]) + len(chunk) <= caps[index])", 'pass'),
    'native_reserve': ("_admit(self._resources.reserve('native_read', amount))", 'pass'),
    'native_join': ("_need(joined)", 'pass'),
    'native_type': ("operation in ('frame', 'click', 'key', 'scroll', 'drag', 'downloads')", 'True'),
    'ram': ("source.resolve().is_relative_to('/dev/shm')", 'True'),
    'ram_symlink': ("not any(p.is_symlink() for p in (source, *source.parents))", 'True'),
    'drain': ("clean = operation() is True and clean", 'operation()\n                clean = True'),
    'target_registration': ("_admit(self._ports.capabilities.validate_target(target, value, state))", 'pass'),
    'native_processes': ("len(self._active) < self.policy.processes", 'True'),
    'publication_metadata': ("_publication_bound(value, p.native_read.policy) is not None", 'True'),
    'ram_custody': ("_admit(admission)", 'pass'),
    'transport_lifetime': ("_need(not self._closed)", 'pass'),
    'transport_error': ("return self._send(action, target)\n            except Exception:\n                raise ValueError('OUT_OF_SUPPORT') from None", "return self._send(action, target)\n            except Exception:\n                raise"),
    'cli_arguments': ("if args.output is None or not all(type(v) is str and v.strip() and not v.startswith('-')\n                    for v in (args.identity, args.container, args.window, args.live_ack)) or args.identity_env is None:", 'if False:'),
    'cli_ports': ("if live_ports is None:", 'if False:'),
    'cli_owners': ("if type(owners) is RunOwners:", 'if False:'),
    'cli_diagnostics': ("raise ValueError('USAGE_SCHEMA') from None", 'return super().error(message)'),
    'operation_deadline': ("self._capabilities._action_deadlines.pop(id(action), p.limits.deadline_ns)", 'p.limits.deadline_ns'),
    'key_policy': ("_admit(p.capabilities.action(action, target))", 'pass'),
    'frozen_click_point': ("payload.get('point') == ','.join(map(str, target.point))", 'True'),
    'frozen_key': ("payload.get('keysym') == action['key_name']", 'True'),
    'frozen_right': ("payload['button'] == 3", 'True'),
    'final_authority': ("_admit(ports.authorization.check('send', capabilities.workflow, ports.scope, ports.authority))", 'pass'),
}
MUTANTS.update({
    'builder_validate': ('_validate(invocation, ports)\n        return ports', 'return ports'),
    'builder_methods': ("_need(all(all(callable(getattr(getattr(ports, name), m, None)) for m in names) for name, names in methods.items()))", 'pass'),
    'stdin_probe': ("result == {\n                        'error': {'code': 2, 'detail': 'choose exactly one input operation'}}", 'True'),
    'stdin_argv': ("*flags, '--timeout'", "*flags, '--type', bytes(payload).decode('utf-8'), '--timeout'"),
    'stdin_echo': ("raise ValueError('OUT_OF_SUPPORT') from None", "raise ValueError('PRIVATE_CANARY') from None"),
    'stdin_frame': ("pending.payload.extend(b'\\r\\n')", 'pass'),
    'stdin_cap': ('len(payload) + 2 <= 16384', 'True'),
    'stdin_deadline': ('deadline = min(deadline_ns, pending.deadline_ns)', 'deadline = self._deadline'),
    'stdin_stage_expiry': ('_need(self._clock() < expiry)', 'pass'),
    'stdin_no_stage': ('_need(pending is not None)', 'pending = pending or _StagedPaste(action["text_slot"], bytearray(b"fake"), self._deadline)'),
    'stdin_slot': ("pending.slot_id == action['text_slot']", 'True'),
    'stdin_pending': ('self._pending is None)', 'True)'),
    'stdin_reserve': ("_admit(self._resources.reserve('private_pipe', amount))", 'pass'),
    'stdin_stage_reserve': ("_admit(self._resources.reserve('private_stage', 2 * (len(payload) + 2)))", 'pass'),
    'stdin_output': ('_need(len(buffers[index]) + len(chunk) <= caps[index])', 'pass'),
    'stdin_identity': ('_need(self._support == identity)', 'pass'),
    'stdin_identity_spawn': ('_need(self._identity() == self._support)', 'pass'),
    'stdin_receipt': ("self._surface._validated_input(result, 'paste')['input_mono_ns']", "result['input_mono_ns']"),
    'stdin_erase': ('self._pending = None\n\n    # @anchor:wa.learn.mw_owners_srf.StdinPrivatePaste.clear', 'pass\n\n    # @anchor:wa.learn.mw_owners_srf.StdinPrivatePaste.clear'),
    'stdin_prepare': ('_admit(answer)\n                _admit(self._ports.private_paste.stage', 'pass\n                _admit(self._ports.private_paste.stage'),
    'stdin_propagation': ('deadline_ns=self._local.deadline)', 'deadline_ns=p.limits.deadline_ns)'),
    'stdin_drain': ('lambda: _yes(ports.private_paste.drain())', 'lambda: True'),
    'actor_acquire': ('ports.actor_lock.acquire(ports.actor_identity)', 'pass'),
    'workflow_aggregate': ('state.slot_bytes + len(payload) <= owners.limits.aggregate_slot_bytes', 'True'),
    'stdin_returncode': ('_need(child.returncode == expected_rc)', 'pass'),
    'stdin_release': ('if clean:\n            with _CANONICAL_MUTEX:', 'if True:\n            with _CANONICAL_MUTEX:'),
})
SPEC_MUTANT_CASES = {
    'builder_validate': 'test_builder_inventory',
    'builder_methods': 'test_builder_inventory',
    'stdin_probe': 'test_probe_and_binding',
    'stdin_argv': 'test_fidelity_privacy_once',
    'stdin_echo': 'test_identity_and_receipt',
    'stdin_frame': 'test_fidelity_privacy_once',
    'stdin_cap': 'test_staging_bounds_and_expiry',
    'stdin_deadline': 'test_deadline_and_pipes',
    'stdin_stage_expiry': 'test_staging_bounds_and_expiry',
    'stdin_no_stage': 'test_fidelity_privacy_once',
    'stdin_slot': 'test_staging_bounds_and_expiry',
    'stdin_pending': 'test_staging_bounds_and_expiry',
    'stdin_reserve': 'test_reservations',
    'stdin_stage_reserve': 'test_reservations',
    'stdin_output': 'test_probe_timeout_and_output_caps',
    'stdin_identity': 'test_identity_and_receipt',
    'stdin_identity_spawn': 'test_executable_recheck_at_spawn',
    'stdin_receipt': 'test_identity_and_receipt',
    'stdin_erase': 'test_fidelity_privacy_once',
    'stdin_prepare': 'test_failed_prepare_and_custody',
    'stdin_propagation': 'test_propagation_and_clipboard_failure',
    'stdin_drain': 'test_failed_prepare_and_custody',
    'actor_acquire': ('ports.actor_lock.acquire(ports.actor_identity)', 'pass'),
    'workflow_aggregate': ('state.slot_bytes + len(payload) <= owners.limits.aggregate_slot_bytes', 'True'),
    'stdin_returncode': ('_need(child.returncode == expected_rc)', 'pass'),
    'stdin_release': 'test_failed_prepare_and_custody',
    'native_processes': 'test_shared_process_ceiling',
    'announcement': 'test_announcement_before_probe',
    'workflow_aggregate': 'test_workflow_slot_budgets',
    'stdin_returncode': 'test_native_failure_no_retry',
    'native_join': 'test_join_and_poison',
    'actor_acquire': 'test_announcement_before_probe',
    'read_policy': 'TestComposition.test_policy_explicit',
    'budget': 'TestComposition.test_budget_deadline_ports',
    'deadline': 'TestComposition.test_budget_deadline_ports',
    'unicode_bytes': 'TestComposition.test_private_transport_and_bytes',
    'private_transport': 'TestComposition.test_private_transport_and_bytes',
    'cli_arguments': 'TestComposition.test_cli_composition',
    'cli_ports': 'test_cli_real_builder',
}
if os.environ.get('MWP7_MUTANT'):
    name = os.environ['MWP7_MUTANT']
    old, new = MUTANTS[name]
    if name == 'dispatch':
        module = sys.modules[base.LegacyDispatchOwner.__module__]
        source = Path(module.__file__).read_text()
        if old not in source:
            raise RuntimeError('MUTANT_ANCHOR_ABSENT')
        tree = ast.parse(source.replace(old, new))
        owner = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == 'LegacyDispatchOwner')
        node = next(node for node in owner.body if isinstance(node, ast.FunctionDef) and node.name == 'dispatch')
        namespace = vars(module).copy()
        exec(compile(ast.Module(body=[node], type_ignores=[]), module.__file__, 'exec'), namespace)
        base.LegacyDispatchOwner.dispatch = namespace['dispatch']
    elif name.startswith(('frozen_', 'workflow_')):
        source = Path(w.__file__).read_text()
        if old not in source:
            raise RuntimeError('MUTANT_ANCHOR_ABSENT')
        tree = ast.parse(source.replace(old, new))
        node = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == ('_action' if name.startswith('workflow_') else '_identity_payload'))
        namespace = vars(w).copy()
        exec(compile(ast.Module(body=[node], type_ignores=[]), w.__file__, 'exec'), namespace)
        if name.startswith('workflow_'):
            w._action = namespace['_action']
        else:
            w._identity_payload = namespace['_identity_payload']
    else:
        original_module = cli if name.startswith('cli_') else m
        source = Path(original_module.__file__).read_text()
        if old not in source:
            raise RuntimeError('MUTANT_ANCHOR_ABSENT')
        mutant = types.ModuleType(original_module.__name__ + '_mutant')
        mutant.__file__ = original_module.__file__
        sys.modules[mutant.__name__] = mutant
        source = source.replace(old, new)
        if name == 'download_bound':
            # Remove cardinality at both array and retained-map layers.
            source = source.replace('len(self._names) + len(new) <= self._policy.rows', 'True')
        exec(compile(source, original_module.__file__, 'exec'), mutant.__dict__)
        if original_module is cli:
            cli = mutant
        else:
            m = mutant
            cli.LiveInvocation = m.LiveInvocation
            cli.open_srf_owners = m.open_srf_owners
            cli.build_live_ports = m.build_live_ports


def _policy(**changes):
    return m.NativeReadPolicy(**dict(dict(stdout_bytes=4096, stderr_bytes=1024, rows=8,
        string_bytes=128, processes=1, timeout_ns=100_000_000, join_ns=20_000_000, publications=100), **changes))


class _Native:
    # @anchor:wa.tests.mw_owners_srf_unit._Native.__init__
    def __init__(self, clock):
        self.policy = _policy()
        self.clock = clock
        self.calls = []
        self.deadlines = []
        self.clean = True

    # @anchor:wa.tests.mw_owners_srf_unit._Native.bind
    def bind(self, invocation, **kwargs):
        return self

    # @anchor:wa.tests.mw_owners_srf_unit._Native.read
    def read(self, operation, payload, deadline):
        self.calls.append((operation, payload))
        self.deadlines.append(deadline)
        if operation == 'frame':
            return dict(path='/dev/shm/synthetic', seq=1, width=100, height=100, window={'id': 'w1', 'w': 100, 'h': 100})
        return dict(focus_verified=True, input_mono_ns=self.clock())

    # @anchor:wa.tests.mw_owners_srf_unit._Native.drain
    def drain(self):
        return YES if self.clean else NO


class _Surface(base.SrfIdentitySurface):
    # @anchor:wa.tests.mw_owners_srf_unit._Surface.ax
    def ax(self, **kwargs):
        raise AssertionError('AX_TRAP')

    # @anchor:wa.tests.mw_owners_srf_unit._Surface.text
    def text(self, *args):
        raise AssertionError('AX_TRAP')


class _Private:
    # @anchor:wa.tests.mw_owners_srf_unit._Private.__init__
    def __init__(self, clock):
        self.clock = clock
        self.ready = YES
        self.sent = 0
        self.fail = False

    # @anchor:wa.tests.mw_owners_srf_unit._Private.check_ready
    def check_ready(self, invocation, scope, authority):
        return self.ready

    # @anchor:wa.tests.mw_owners_srf_unit._Private.send
    def send(self, action, target):
        self.sent += 1
        if self.fail:
            raise ValueError('PRIVATE_MARKER')
        return self.clock()


# @anchor:wa.tests.mw_owners_srf_unit.TestComposition
class TestComposition(unittest.TestCase):
    # @anchor:wa.tests.mw_owners_srf_unit.TestComposition.setUp
    def setUp(self):
        self.b = base.TestWorkflow('test_nominal_real_gate')
        self.b.setUp()
        self.addCleanup(self.b.doCleanups)
        b = self.b
        self.native = _Native(b.clock)
        self.invocation = m.LiveInvocation(b.path, b.owners.output, 'replay', 'synthetic', 'synthetic', 'w1',
                                           b.root / 'unused.env', 'opaque-proof')
        self.ports = m.LivePorts(scope=b.owners.scope, authority=b.owners.authority, run_id='run', limits=b.limits,
            validation_context=b.ctx, artifact=b.artifact, authorization=b.authorization, capabilities=b.capabilities,
            observations=b.observations, ram=None, slots=None, resources=b.resources, journal=b.journal,
            replay_policy=None, slot_length_bindings=(), actor_lock=ActorLock(), actor_identity='registered-actor',
            announce_gate=self._announcement, clock=b.clock, sleep=b.clock.sleep, native_read=self.native, private_paste=None)
        self.factory_calls = []
        # Unique test capsule key, stable within a test. No live capsule exists.
        self.invocation = replace(self.invocation, container=str(b.root))
        self.addCleanup(self._recover_fake_lease)

    def _recover_fake_lease(self):
        # Explicit test-only recovery after a deliberately failed drain. Never
        # make the production context manager drop an unresolved lease.
        key = (self.invocation.container, self.invocation.window)
        lock = m._CANONICAL.pop(key, None)
        if lock is not None and lock.holder is not None:
            lock.release(lock.holder)

    def _announcement(self, invocation, **kwargs):
        return YES if invocation == self.invocation and kwargs['scope'] == 'scope' and kwargs['authority'] == 'authority' else NO

    def _surface(self, **kwargs):
        self.factory_calls.append(kwargs)
        return _Surface(**kwargs)

    def _open(self, invocation=None, ports=None):
        return m.open_srf_owners(invocation or self.invocation, ports=ports or self.ports, surface_factory=self._surface)

    def _run(self):
        b = self.b
        b._write()
        with self._open() as owners:
            self.built = owners
            return w.run_workflow(b.path, bundle=base.BUNDLE, owners=owners, limits=owners.limits)

    def _action(self, kind):
        b = self.b
        b._click()
        action = b.workflow['steps'][0]['action']
        action.update(kind=kind, path='coordinates', button=3 if kind == 'right_click' else (1 if kind in ('click', 'double_click', 'drag') else None))
        if kind == 'scroll':
            action.update(dx_px=0, dy_px=1)
        return action

    def _dependencies(self):
        result = {f.name: getattr(self.ports, f.name) for f in fields(m.LivePorts)
                  if f.name != 'native_read'}
        result['native_policy'] = self.ports.native_read.policy
        return result

    def _cli_args(self):
        inv = self.invocation
        return [str(inv.workflow), '--mode', inv.mode, '--output', str(inv.output),
                '--identity', inv.identity, '--container', inv.container, '--window', inv.window,
                '--identity-env', str(inv.identity_env), '--live-ack', inv.live_ack]

    # @anchor:wa.tests.mw_owners_srf_unit.TestComposition.test_factory_arguments
    def test_factory_arguments(self):
        for changes in ({'live_ack': ''}, {'identity': ''}, {'container': '--bad'}, {'window': ''},
                        {'mode': 'repair'}, {'workflow': 'not-a-Path'}):
            with self.subTest(changes=changes), self.assertRaisesRegex(ValueError, '^OUT_OF_SUPPORT$'):
                with self._open(replace(self.invocation, **changes), replace(self.ports, announce_gate=lambda *a, **k: YES)):
                    pass
        self.assertEqual(self.factory_calls, [])
        with self._open() as owners:
            self.assertIs(type(owners), w.RunOwners)
            self.assertIs(type(owners.dispatch), base.LegacyDispatchOwner)
        self.assertEqual(self.factory_calls[0]['identity'], 'synthetic')
        self.assertIsNone(self.factory_calls[0]['trace'])
        self.assertIsNone(self.ports.actor_lock.holder)

    # @anchor:wa.tests.mw_owners_srf_unit.TestComposition.test_announcement
    def test_announcement(self):
        for invocation, ports in ((replace(self.invocation, live_ack='arbitrary'), self.ports),
                                   (self.invocation, replace(self.ports, announce_gate=lambda *a, **k: NO)),
                                   (self.invocation, replace(self.ports, scope='foreign'))):
            with self.assertRaises(ValueError), self._open(invocation, ports):
                pass
        self.assertEqual(self.factory_calls, [])
        expiry = self.b.clock()
        def gate(invocation, **kwargs):
            return YES if invocation == self.invocation and kwargs['now_ns'] < expiry else NO
        with self.assertRaises(ValueError), self._open(ports=replace(self.ports, announce_gate=gate)):
            pass
        self.assertEqual(self.factory_calls, [])
        expiry = self.b.clock() + 100
        with self._open(ports=replace(self.ports, announce_gate=gate)):
            pass

    # @anchor:wa.tests.mw_owners_srf_unit.TestComposition.test_budget_deadline_ports
    def test_budget_deadline_ports(self):
        for limits in (replace(self.ports.limits, steps=0), replace(self.ports.limits, steps=True),
                       replace(self.ports.limits, deadline_ns=self.b.clock()), replace(self.ports.limits, workflow_bytes=65537)):
            with self.assertRaises(ValueError), self._open(ports=replace(self.ports, limits=limits)):
                pass
        with self.assertRaises(ValueError), self._open(ports=replace(self.ports, authorization=None)):
            pass
        lookalike = types.SimpleNamespace(**self.ports.limits.__dict__)
        with self.assertRaises(ValueError), self._open(ports=replace(self.ports, limits=lookalike)):
            pass
        self.assertEqual(self.factory_calls, [])
        with self._open():
            pass

    # @anchor:wa.tests.mw_owners_srf_unit.TestComposition.test_actor_exclusion
    def test_actor_exclusion(self):
        with self._open() as owners:
            for lock in (self.ports.actor_lock, ActorLock()):
                with self.assertRaises(Exception), self._open(ports=replace(self.ports, actor_lock=lock)):
                    pass
            called = []
            with self.assertRaises(ValueError):
                owners.dispatch.dispatch(object(), {}, lambda: called.append(1))
            owners.dispatch.dispatch(owners.surface, {}, lambda: called.append(1))
            self.assertEqual(called, [1])
        with self._open():
            pass
        self.assertEqual(len(self.factory_calls), 2)

    # @anchor:wa.tests.mw_owners_srf_unit.TestComposition.test_drain_retains_custody
    def test_drain_retains_custody(self):
        self.b.resources.bad_cleanup = True
        with self.assertRaises(ValueError), self._open():
            pass
        self.assertEqual(self.ports.actor_lock.holder, 'registered-actor')
        with self.assertRaises(ValueError), self._open(ports=replace(self.ports, actor_lock=ActorLock())):
            pass
        self.b.resources.bad_cleanup = False
        self._recover_fake_lease()
        with self._open():
            pass
        self.assertIsNone(self.ports.actor_lock.holder)

    # @anchor:wa.tests.mw_owners_srf_unit.TestComposition.test_click_and_scope
    def test_click_and_scope(self):
        self._action('click')
        self.assertEqual(self._run().rc, 0)
        self.assertEqual(self.native.calls, [('click', {'point': '10,10', 'button': 1})])
        self.b.journal.raw = b''
        self.native.calls.clear()
        self.b.authorization.denied = 'send'
        self.assertNotEqual(self._run().rc, 0)
        self.assertEqual(self.native.calls, [])

    # @anchor:wa.tests.mw_owners_srf_unit.TestComposition.test_final_authority
    def test_final_authority(self):
        self._action('click')
        with self._open() as owners:
            self.assertTrue(m._yes(owners.capabilities.supports(self.b.workflow)))
            target = owners.capabilities.target(None, None, self.b.limits.deadline_ns)
            plan = owners.transport._ordinary.planner
            def revoke(action, target):
                value = plan(action, target)
                self.b.authorization.denied = 'send'
                return value
            owners.transport._ordinary = w.IdentityTransport(owners.surface, revoke)
            with self.assertRaises(Exception):
                owners.transport.send(self.b.workflow['steps'][0]['action'], target)
            self.assertEqual(self.native.calls, [])
            self.b.authorization.denied = None
            owners.transport._ordinary = w.IdentityTransport(owners.surface, plan)
            owners.transport.send(self.b.workflow['steps'][0]['action'], target)
            self.assertEqual(len(self.native.calls), 1)

    # @anchor:wa.tests.mw_owners_srf_unit.TestComposition.test_click_mapping
    def test_click_mapping(self):
        action = self._action('right_click')
        action['button'] = 1
        with self._open() as owners:
            self.assertFalse(m._yes(owners.capabilities.supports(self.b.workflow)))
        self.assertNotEqual(self._run().rc, 0)
        self.assertEqual(self.native.calls, [])
        self.b.journal.raw = b''
        action['button'] = 3
        self.assertEqual(self._run().rc, 0)
        self.assertEqual(self.native.calls[-1][1]['button'], 3)

    # @anchor:wa.tests.mw_owners_srf_unit.TestComposition.test_double_and_path
    def test_double_and_path(self):
        action = self._action('double_click')
        with self._open() as owners:
            self.assertFalse(m._yes(owners.capabilities.supports(self.b.workflow)))
            action['kind'] = 'click'
            action['path'] = 'ax'
            self.assertFalse(m._yes(owners.capabilities.supports(self.b.workflow)))
            action['path'] = 'coordinates'
            self.assertTrue(m._yes(owners.capabilities.supports(self.b.workflow)))
        self.assertEqual(self.native.calls, [])

    # @anchor:wa.tests.mw_owners_srf_unit.TestComposition.test_key_focus
    def test_key_focus(self):
        action = self._action('key')
        action['key_name'] = 'Return'
        self.b.capabilities.no_focus = True
        self.assertNotEqual(self._run().rc, 0)
        self.assertEqual(self.native.calls, [])
        self.b.journal.raw = b''
        self.b.capabilities.no_focus = False
        self.assertEqual(self._run().rc, 0)
        self.assertEqual(self.native.calls, [('key', {'keysym': 'Return'})])

    # @anchor:wa.tests.mw_owners_srf_unit.TestComposition.test_scroll_grounding
    def test_scroll_grounding(self):
        self._action('scroll')
        self.b.workflow['steps'][0]['target'] = None
        payload = {'point': '10,10', 'dir': 'down'}
        self.b.capabilities.plan = lambda *a: w.IdentityPlan('scroll', dict(payload), YES)
        for change in ({'point': '11,10'}, {'dir': ''}, {'modifier': 'alt'}):
            good = dict(payload)
            payload.update(change)
            with self.subTest(change=change):
                self.assertNotEqual(self._run().rc, 0)
                self.assertEqual(self.native.calls, [])
            payload.clear()
            payload.update(good)
            self.b.journal.raw = b''
        self.assertEqual(self._run().rc, 0)
        self.assertEqual(self.native.calls[-1], ('scroll', payload))

    # @anchor:wa.tests.mw_owners_srf_unit.TestComposition.test_drag_geometry
    def test_drag_geometry(self):
        action = self._action('drag')
        action.update(dx_px=-2, dy_px=3)
        self.assertEqual(self._run().rc, 0)
        self.assertEqual(self.native.calls[-1], ('drag', {'span': '10,10:8,13', 'button': 1}))
        self.b.journal.raw = b''
        self.native.calls.clear()
        action.update(destination=base._target())
        self.assertNotEqual(self._run().rc, 0)
        self.assertEqual(self.native.calls, [])

    def _paste(self):
        self.b._paste()
        self.b.slots.transport = 'owned_clipboard'
        self.b.workflow['steps'][0]['action']['path'] = 'coordinates'
        self.b.workflow['steps'][0]['action']['delivery_budget_ms'] = 40
        self.private = _Private(self.b.clock)
        self.ports = replace(self.ports, slots=self.b.slots, private_paste=self.private,
                              slot_length_bindings=self.b.owners.slot_length_bindings)

    # @anchor:wa.tests.mw_owners_srf_unit.TestComposition.test_private_admission
    def test_private_admission(self):
        self._paste()
        for provider in (None, object(), lambda *a: None):
            with self._open(ports=replace(self.ports, private_paste=provider)) as owners:
                self.assertFalse(m._yes(owners.capabilities.supports(self.b.workflow)))
        with self._open() as owners:
            self.private.ready = NO
            self.assertFalse(m._yes(owners.capabilities.supports(self.b.workflow)))
            self.private.ready = YES
            self.assertTrue(m._yes(owners.capabilities.supports(self.b.workflow)))
        self.assertEqual(self.native.calls, [])

    # @anchor:wa.tests.mw_owners_srf_unit.TestComposition.test_private_transport_and_bytes
    def test_private_transport_and_bytes(self):
        self._paste()
        rejected = []
        with self._open() as owners:
            for transport, value, ceiling in (('private_pipe', 'é', 4096), ('owned_clipboard', 'é', 1)):
                self.b.slots.transport, self.b.slots.value = transport, value
                try:
                    owners.slots.read(self.b.workflow['text_slots'][0], None, ceiling)
                    rejected.append(False)
                except ValueError:
                    rejected.append(True)
            self.b.slots.transport = 'owned_clipboard'
            accepted = owners.slots.read(self.b.workflow['text_slots'][0], None, 2).value
        # Keep assertions outside the redacting owner context so a mutant fails
        # its predicate rather than being translated to an unrelated error.
        self.assertEqual(rejected, [True, True])
        self.assertEqual(accepted, 'é')

    # @anchor:wa.tests.mw_owners_srf_unit.TestComposition.test_private_cleanup_and_privacy
    def test_private_cleanup_and_privacy(self):
        self._paste()
        self.b.slots.value = 'PRIVATE_MARKER_é'
        self.b.observations.values['length'] = [len(self.b.slots.value)]
        events = []
        for name in ('prepare', 'verify', 'clear', 'cleared'):
            original = getattr(self.b.slots, name)
            def logged(*args, _name=name, _original=original):
                events.append(_name)
                return _original(*args)
            setattr(self.b.slots, name, logged)
        send = self.private.send
        def logged_send(*args):
            events.append('send')
            return send(*args)
        self.private.send = logged_send
        self.assertEqual(self._run().rc, 0)
        self.assertEqual(events, ['prepare', 'send', 'verify', 'clear', 'cleared'])
        self.assertEqual(self.private.sent, 1)
        self.assertEqual(self.native.calls, [])
        self.assertGreater(self.b.slots.cleared_count, 0)
        for path in self.b.root.rglob('*'):
            if path.is_file():
                self.assertNotIn(self.b.slots.value.encode(), path.read_bytes())
        self.b.journal.raw = b''
        self.private.fail = True
        result = self._run()
        self.assertNotEqual(result.rc, 0)
        self.assertNotIn('PRIVATE_MARKER', repr(result))
        self.assertEqual(self.private.sent, 2)  # One attempt per invocation.

    # @anchor:wa.tests.mw_owners_srf_unit.TestComposition.test_observation_registration
    def test_observation_registration(self):
        with self._open() as owners:
            value = owners.observations.acquire(base._predicate('signal'), self.b.limits.deadline_ns)
            self.assertTrue(m._yes(owners.observations.validate(value)))
            self.assertFalse(m._yes(owners.observations.validate(replace(value))))
            self.assertFalse(m._yes(owners.observations.validate(replace(value, publication='forged'))))
            self.b.observations.denied = True  # Trusted registration/freshness owner refuses stale evidence.
            with self.assertRaises(ValueError):
                owners.observations.acquire(base._predicate('stale'), self.b.limits.deadline_ns)
            self.b.observations.denied = False
            for bad in (replace(value, scope='foreign'), replace(value, complete=False),
                        replace(value, capture_end_ns=self.b.limits.deadline_ns + 1), replace(value, missing_reason='missing')):
                self.b.observations.values['bad'] = [replace(bad, capability_id='bad', publication='invalid:' + str(id(bad)))]
                with self.assertRaises(ValueError):
                    owners.observations.acquire(base._predicate('bad'), self.b.limits.deadline_ns)
        self.native.policy = _policy(publications=1)
        with self._open() as owners:
            owners.observations.acquire(base._predicate('one'), self.b.limits.deadline_ns)
            with self.assertRaises(ValueError):
                owners.observations.acquire(base._predicate('two'), self.b.limits.deadline_ns)

    # @anchor:wa.tests.mw_owners_srf_unit.TestComposition.test_vision_only
    def test_vision_only(self):
        self._action('click')
        self.assertEqual(self._run().rc, 0)
        self.assertFalse(any(op in ('ax', 'text') for op, _ in self.native.calls))
        with self._open() as owners:
            for op in ('ax', 'text', 'type'):
                with self.assertRaises(ValueError):
                    owners.surface._call(op, text='PRIVATE_MARKER', paste=True)

    # @anchor:wa.tests.mw_owners_srf_unit.TestComposition.test_cli_composition
    def test_cli_composition(self):
        self.b._write()
        args = [str(self.b.path), '--mode', 'replay', '--output', str(self.invocation.output),
                '--identity', self.invocation.identity, '--container', self.invocation.container,
                '--window', 'w1', '--identity-env', str(self.invocation.identity_env), '--live-ack', self.invocation.live_ack]
        real_open = m.open_srf_owners
        def fake_open(invocation, *, ports):
            return real_open(invocation, ports=ports, surface_factory=self._surface)
        with patch.object(cli, 'open_srf_owners', side_effect=fake_open) as factory, contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(cli.main(args[:-2], live_ports=self.ports), 2)
            self.assertEqual(self.factory_calls, [])
            # Unavailable provisioning remains refused; TestStdin.test_cli_real_builder
            # exercises admitted shell composition using the real builder.
            self.assertEqual(cli.main(args), 5)
            self.assertEqual(self.factory_calls, [])
            factory.assert_not_called()
            self.assertEqual(cli.main(args, live_ports=self.ports), 0)
            factory.assert_called_once()
            self.assertEqual(cli.main([str(self.b.path), '--mode', 'validate'], owners=self.b.owners), 0)
            self.assertEqual(cli.main([str(self.b.path), '--mode', 'shadow', '--output', str(self.b.owners.output)], owners=self.b.owners), 2)
            self.b.journal.raw = b''
            self.assertEqual(cli.main([str(self.b.path), '--mode', 'replay', '--output', str(self.b.owners.output)], owners=self.b.owners), 0)
            factory.assert_called_once()
        self.assertNotIn(self.invocation.live_ack, out.getvalue())

    # @anchor:wa.tests.mw_owners_srf_unit.TestComposition.test_length_controls
    def test_length_controls(self):
        self._paste()
        self.b.slots.value = 'é文'
        for bad in (3, True, 4):
            self.b.observations.values['length'] = [bad]
            result = self._run()
            self.assertEqual(result.verdict_code, 'CHANGE_MISMATCH')
            self.b.journal.raw = b''
        self.b.observations.values['length'] = [2]
        self.assertEqual(self._run().rc, 0)

    # @anchor:wa.tests.mw_owners_srf_unit.TestComposition.test_cleanup_failures
    def test_cleanup_failures(self):
        self._paste()
        slot = self.b.workflow['text_slots'][0]
        with self._open() as owners:
            self.b.slots.prepare = lambda *a: (_ for _ in ()).throw(ValueError('PRIVATE_MARKER'))
            with self.assertRaises(ValueError):
                owners.slots.prepare(slot, b'PRIVATE_MARKER', None)
            # Preparation partially failed; the scoped wrapper still owns clear.
        self.assertGreater(self.b.slots.cleared_count, 0)
        self.b.slots.clean = False
        with self.assertRaises(ValueError), self._open() as owners:
            owners.slots.prepare(slot, b'PRIVATE_MARKER', None)
        self.assertIsNotNone(self.ports.actor_lock.holder)
        self.b.slots.clean = True
        self._recover_fake_lease()
        with self._open():
            pass

    # @anchor:wa.tests.mw_owners_srf_unit.TestComposition.test_download_observations
    def test_download_observations(self):
        rows = [dict(name='PRIVATE_DOWNLOAD', bytes=10, mtime=1)]
        self.native.read = lambda *a: list(rows)
        owner = m.DownloadsObservations(self.invocation, ports=self.ports, capability_id='downloads',
                                         revision='v1', unit='identities', finished_gate=lambda *a: YES)
        with self.assertRaises(ValueError):
            owner.acquire(base._predicate('foreign'), self.b.limits.deadline_ns)
        first = owner.acquire(base._predicate('downloads'), self.b.limits.deadline_ns)
        self.assertTrue(m._yes(owner.validate(first)))
        self.assertFalse(m._yes(owner.validate(replace(first))))
        rows[0]['mtime'] = 2
        second = owner.acquire(base._predicate('downloads'), self.b.limits.deadline_ns)
        self.assertEqual(second.value, first.value)
        rows.append(dict(name='new', bytes=1, mtime=2))
        third = owner.acquire(base._predicate('downloads'), self.b.limits.deadline_ns)
        self.assertTrue(third.value - first.value)
        self.assertNotIn('PRIVATE_DOWNLOAD', repr(third))
        owner._gate = lambda *a: NO
        with self.assertRaises(ValueError):
            owner.acquire(base._predicate('downloads'), self.b.limits.deadline_ns)

    # @anchor:wa.tests.mw_owners_srf_unit.TestComposition.test_cli_private_errors
    def test_cli_private_errors(self):
        for args in (['--unknown', 'PRIVATE_MARKER'], ['unused', '--mode', 'PRIVATE_MARKER']):
            with contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()) as err:
                self.assertEqual(cli.main(args), 2)
            self.assertEqual(err.getvalue(), '')
            self.assertNotIn('PRIVATE_MARKER', out.getvalue())
            self.assertEqual(base.g.strict_load(out.getvalue().encode())['verdict_code'], 'USAGE_SCHEMA')

    # @anchor:wa.tests.mw_owners_srf_unit.TestComposition.test_vision_target_registration
    def test_vision_target_registration(self):
        with self._open() as owners:
            self.b.capabilities.validate_target = lambda *a: NO  # AX-derived source denied by site profile.
            with self.assertRaises(ValueError):
                owners.capabilities.target(None, None, self.b.limits.deadline_ns)
            self.b.capabilities.validate_target = lambda *a: YES  # Registered RAM visual source.
            value = owners.capabilities.target(None, None, self.b.limits.deadline_ns)
            self.assertIs(value.support_owner, owners.capabilities)
            self.assertTrue(m._yes(owners.observations.validate(value.observation)))

    # @anchor:wa.tests.mw_owners_srf_unit.TestComposition.test_planner_payload_binding
    def test_planner_payload_binding(self):
        action = self._action('click')
        with self._open() as owners:
            self.assertTrue(m._yes(owners.capabilities.supports(self.b.workflow)))
            target = owners.capabilities.target(None, None, self.b.limits.deadline_ns)
            for kind, bad, good in (
                ('click', {'point': '11,10', 'button': 1}, {'point': '10,10', 'button': 1}),
                ('right_click', {'point': '10,10', 'button': 1}, {'point': '10,10', 'button': 3}),
                ('key', {'keysym': 'Escape'}, {'keysym': 'Return'}),
            ):
                action.update(kind=kind, key_name='Return' if kind == 'key' else None,
                              button=3 if kind == 'right_click' else (1 if kind == 'click' else None))
                operation = 'key' if kind == 'key' else 'click'
                owners.transport._ordinary = w.IdentityTransport(owners.surface,
                    lambda *a: w.IdentityPlan(operation, dict(bad), YES))
                before = len(self.native.calls)
                with self.assertRaises(Exception):
                    owners.transport.send(action, target)
                self.assertEqual(len(self.native.calls), before)
                owners.transport._ordinary = w.IdentityTransport(owners.surface,
                    lambda *a: w.IdentityPlan(operation, dict(good), YES))
                owners.transport.send(action, target)
                self.assertEqual(len(self.native.calls), before + 1)
            self.b.capabilities.action = lambda *a: NO
            with self.assertRaises(ValueError):
                owners.transport.send(action, target)
            self.b.capabilities.action = lambda *a: YES
            owners.transport.send(action, target)

    # @anchor:wa.tests.mw_owners_srf_unit.TestComposition.test_ram_custody
    def test_ram_custody(self):
        admitted = [False]
        publication = replace(self.b.observations.acquire(base._predicate('ram'), self.b.limits.deadline_ns),
                              output_kind='ram', value=object())
        frame, descriptor = object(), object()
        owner = types.SimpleNamespace(t0=lambda *a: (frame, descriptor, publication, YES if admitted[0] else NO),
                                      change=lambda *a: ('before', 'after', descriptor, YES))
        with self._open(ports=replace(self.ports, ram=owner)) as owners:
            with self.assertRaises(ValueError):
                owners.ram.t0(None, None, None, self.b.limits.deadline_ns)
            admitted[0] = True
            self.assertEqual(owners.ram.t0(None, None, None, self.b.limits.deadline_ns),
                             (frame, descriptor, publication, YES))
            self.assertTrue(m._yes(owners.observations.validate(publication)))
            self.assertEqual(owners.ram.change(None, None, self.b.limits.deadline_ns), ('before', 'after', descriptor, YES))

    # @anchor:wa.tests.mw_owners_srf_unit.TestComposition.test_ram_changed_workflow
    def test_ram_changed_workflow(self):
        step = self.b.workflow['steps'][0]
        predicate = base._predicate('pixels', 'changed', 'before')
        step['capture_before'] = [{'name': 'before', 'observe': base._predicate('pixels')}]
        step['verify']['predicate'] = predicate
        original = self.b.observations.acquire
        payload = [object()]
        seen = []
        def acquire(spec, deadline):
            value = original(spec, deadline)
            return replace(value, output_kind='ram', value=payload[0]) if spec['capability_id'] == 'pixels' else value
        def changed(spec, before, after):
            seen.append((before, after))
            return NO if denied[0] else YES
        self.b.observations.acquire = acquire
        self.b.capabilities.changed = changed
        denied = [True]
        self.assertNotEqual(self._run().rc, 0)  # RAM comparator denies the change.
        self.assertTrue(seen)
        self.b.journal.raw = b''
        denied[0] = False
        payload[0] = None
        self.assertNotEqual(self._run().rc, 0)  # Missing RAM is never evidence.
        self.b.journal.raw = b''
        payload[0] = object()
        self.assertEqual(self._run().rc, 0)
        before, after = seen[-1]
        self.assertEqual((before.output_kind, after.output_kind), ('ram', 'ram'))
        self.assertGreater(after.capture_start_ns, before.capture_end_ns)
        self.assertTrue(all(type(amount) is int and amount > 0 for kind, amount in
                            self.b.resources.reservations if kind == 'observation_registration'))
        for value in (before, after):
            metadata = (value.publication, value.capability_id, value.capability_revision, value.output_kind, value.unit)
            self.assertIn(('observation_registration', 384 + sum(4 * len(v) for v in metadata)),
                          self.b.resources.reservations)

    # @anchor:wa.tests.mw_owners_srf_unit.TestComposition.test_visual_verifier_registration
    def test_visual_verifier_registration(self):
        self._action('click')
        spec = self.b.workflow['steps'][0]['verify']
        seen = []
        bad = [True]
        def observe(spec, state, deadline):
            seen.append((spec, state, deadline))
            value = self.b.observations.acquire(spec['predicate'], deadline)
            return replace(value, complete=False) if bad[0] else value
        self.b.capabilities.observe_verifier = observe
        for mode, masks in (('after_crop', [[0, 0, 1, 1]]), ('reacted_mask', [[0, 0, 1, 1]])):
            spec.update(mode=mode, mask_boxes_px=masks, ram_evidence_ref='synthetic-ram')
            bad[0] = True
            self.b.journal.raw = b''
            self.assertNotEqual(self._run().rc, 0)
            bad[0] = False
            self.b.journal.raw = b''
            self.assertEqual(self._run().rc, 0)
            value_spec, state, deadline = seen[-1]
            self.assertEqual(value_spec, spec)
            self.assertEqual(deadline, state.verify_deadline_ns)
            self.assertIs(self.built.capabilities._state, state)
        with self._open() as owners:
            state = object()
            value = owners.capabilities.observe_verifier(spec, state, self.b.limits.deadline_ns)
            self.assertIs(owners.capabilities._state, state)
            self.assertTrue(m._yes(owners.observations.validate(value)))

    # @anchor:wa.tests.mw_owners_srf_unit.TestComposition.test_visual_support_dependencies
    def test_visual_support_dependencies(self):
        self._action('click')
        step = self.b.workflow['steps'][0]
        primary = step['verify']
        visual = dict(primary, mode='after_crop', mask_boxes_px=[[0, 0, 1, 1]],
                      ram_evidence_ref='synthetic-ram', fallback_verifiers=[])
        for fallback in (False, True):
            for mode, masks in (('after_crop', [[0, 0, 1, 1]]), ('reacted_mask', [])):
                visual.update(mode=mode, mask_boxes_px=masks)
                step['verify'] = dict(primary, fallback_verifiers=[dict(visual)]) if fallback else dict(visual)
                for missing in (None, object()):
                    self.b.capabilities.observe_verifier = missing
                    with self._open() as owners:
                        self.assertFalse(m._yes(owners.capabilities.supports(self.b.workflow)))
                    self.b.journal.raw = b''
                    self.assertEqual(self._run().verdict_code, 'OUT_OF_SUPPORT')
                    self.assertEqual(self.native.calls, [])
                self.b.capabilities.observe_verifier = lambda *a: None
                with self._open() as owners:
                    self.assertTrue(m._yes(owners.capabilities.supports(self.b.workflow)))

    # @anchor:wa.tests.mw_owners_srf_unit.TestComposition.test_ram_support_dependencies
    def test_ram_support_dependencies(self):
        self._action('click')
        step = self.b.workflow['steps'][0]
        primary, target = step['verify'], step['target']
        visual = dict(primary, mode='after_crop', mask_boxes_px=[], ram_evidence_ref='synthetic-ram')
        ram = types.SimpleNamespace(t0=lambda *a: None, change=lambda *a: None)
        policy = object()
        for use in ('target', 'destination', 'primary', 'fallback'):
            step['target'] = dict(target, rung='T0') if use == 'target' else target
            step['action']['destination'] = dict(target, rung='T0') if use == 'destination' else None
            step['verify'] = visual if use == 'primary' else dict(primary,
                fallback_verifiers=[visual] if use == 'fallback' else [])
            for owner, replay in ((None, None), (ram, None), (None, policy), (ram, policy)):
                with self._open(ports=replace(self.ports, ram=owner, replay_policy=replay)) as owners:
                    self.assertEqual(m._yes(owners.capabilities.supports(self.b.workflow)),
                                     owner is not None and replay is not None)
        self.assertEqual(self.native.calls, [])

    # @anchor:wa.tests.mw_owners_srf_unit.TestComposition.test_observation_bounds
    def test_observation_bounds(self):
        with self._open() as owners:
            good = self.b.observations.acquire(base._predicate('bounded'), self.b.limits.deadline_ns)
            for bad in (replace(good, publication='x' * 129), replace(good, capability_revision='x' * 129),
                        replace(good, output_kind='set', value=frozenset(str(i) for i in range(9)))):
                self.b.observations.values['bounded'] = [bad]
                with self.assertRaises(ValueError):
                    owners.observations.acquire(base._predicate('bounded'), self.b.limits.deadline_ns)
            self.b.observations.values['bounded'] = [good]
            self.assertIs(owners.observations.acquire(base._predicate('bounded'), self.b.limits.deadline_ns), good)

    # @anchor:wa.tests.mw_owners_srf_unit.TestComposition.test_policy_explicit
    def test_policy_explicit(self):
        for item in fields(m.NativeReadPolicy):
            with self.assertRaises(ValueError):
                replace(_policy(), **{item.name: 0})
        with self.assertRaises(TypeError):
            m.NativeReadPolicy(stdout_bytes=1)
        with self.assertRaises(ValueError):
            _policy(processes=True)
        self.assertIs(type(_policy()), m.NativeReadPolicy)

    # @anchor:wa.tests.mw_owners_srf_unit.TestComposition.test_transport_exception_privacy
    def test_transport_exception_privacy(self):
        self._paste()
        with self._open() as owners:
            self.assertTrue(m._yes(owners.capabilities.supports(self.b.workflow)))
            self.private.fail = True
            with self.assertRaisesRegex(ValueError, '^OUT_OF_SUPPORT$'):
                owners.transport.send(self.b.workflow['steps'][0]['action'], None)
            self.assertEqual(self.private.sent, 1)
            self.private.fail = False
            self.assertIs(type(owners.transport.send(self.b.workflow['steps'][0]['action'], None)), int)
            self.assertEqual(self.private.sent, 2)
            self.assertEqual(self.native.calls, [])

    # @anchor:wa.tests.mw_owners_srf_unit.TestComposition.test_operation_deadline
    def test_operation_deadline(self):
        action = self._action('click')
        action['delivery_budget_ms'] = 1
        with self._open() as owners:
            self.assertTrue(m._yes(owners.capabilities.supports(self.b.workflow)))
            state = types.SimpleNamespace(deadline_ns=self.b.limits.deadline_ns)
            target = owners.capabilities.target(None, state, self.b.limits.deadline_ns)
            state.deadline_ns = self.b.clock() + 100000
            owners.capabilities.action(action, target)
            self.b.clock.sleep(100001)
            with self.assertRaises(ValueError):
                owners.transport.send(action, target)
            self.assertEqual(self.native.calls, [])
            state.deadline_ns = self.b.clock() + 200000
            owners.capabilities.action(action, target)
            owners.transport.send(action, target)
            self.assertEqual(self.native.deadlines[-1], state.deadline_ns)

    # @anchor:wa.tests.mw_owners_srf_unit.TestComposition.test_transport_lifetime
    def test_transport_lifetime(self):
        action = self._action('click')
        with self._open() as owners:
            self.assertTrue(m._yes(owners.capabilities.supports(self.b.workflow)))
            target = owners.capabilities.target(None, None, self.b.limits.deadline_ns)
            owners.transport.send(action, target)
        self.assertEqual(len(self.native.calls), 1)
        with self.assertRaises(ValueError):
            owners.transport.send(action, target)
        self.assertEqual(len(self.native.calls), 1)
        with self._open() as fresh:
            self.assertTrue(m._yes(fresh.capabilities.supports(self.b.workflow)))
            target = fresh.capabilities.target(None, None, self.b.limits.deadline_ns)
            fresh.transport.send(action, target)
        self.assertEqual(len(self.native.calls), 2)


class _Process:
    """Pipe-only fake; no process or native producer is launched."""
    # @anchor:wa.tests.mw_owners_srf_unit._Process.__init__
    def __init__(self, stdout=b'{}', stderr=b'', *, stalled=False, unjoined=False, **kwargs):
        self.stdout, self._out = self._pipe(stdout, stalled)
        self.stderr, self._err = self._pipe(stderr, stalled)
        self.returncode = None if stalled else 0
        self.unjoined = unjoined
        self.waits = 0
        self.killed = False

    def _pipe(self, data, stalled):
        read, write = os.pipe()
        if data:
            os.write(write, data)
        if not stalled:
            os.close(write)
            write = None
        return os.fdopen(read, 'rb'), write

    # @anchor:wa.tests.mw_owners_srf_unit._Process.poll
    def poll(self):
        return self.returncode

    # @anchor:wa.tests.mw_owners_srf_unit._Process.wait
    def wait(self, timeout):
        self.waits += 1
        if (self.unjoined and self.waits > 1) or self.returncode is None:
            raise subprocess.TimeoutExpired('fake', timeout)
        return self.returncode

    # @anchor:wa.tests.mw_owners_srf_unit._Process.kill
    def kill(self):
        self.killed = True
        for fd in (self._out, self._err):
            if fd is not None:
                os.close(fd)
        self._out = self._err = None
        if not self.unjoined:
            self.returncode = -9


# @anchor:wa.tests.mw_owners_srf_unit.TestNative
class TestNative(TestComposition):
    # Only native-specific methods are loaded by this subclass's suite below.
    def _reader(self, child, policy=None):
        reader = m.BoundedNativeRead(policy or _policy(), process_factory=lambda *a, **k: child)
        reader.bind(self.invocation, resources=self.b.resources, clock=self.b.clock, deadline_ns=self.b.limits.deadline_ns)
        self.addCleanup(child.stdout.close)
        self.addCleanup(child.stderr.close)
        return reader

    # @anchor:wa.tests.mw_owners_srf_unit.TestNative.test_native_bounds
    def test_native_bounds(self):
        with patch.object(m, 'load_identity_env', return_value={}):
            for kwargs in ({'stdout': b'{"data":"' + b'x' * 129 + b'"}'}, {'stderr': b'x' * 65}, {'stdout': b'\xff'},
                           {'stdout': b'[]'}, {'stalled': True}, {'unjoined': True}):
                with self.subTest(kwargs=kwargs):
                    child = _Process(**kwargs)
                    reader = self._reader(child, _policy(stdout_bytes=128, stderr_bytes=64))
                    with self.assertRaises(ValueError):
                        reader.read('key', {'keysym': 'Return'}, self.b.limits.deadline_ns)
                    if kwargs.get('unjoined'):
                        self.assertFalse(m._yes(reader.drain()))
                    else:
                        self.assertTrue(m._yes(reader.drain()))
            child = _Process(stdout=b'{"focus_verified":true,"input_mono_ns":1}')
            reader = self._reader(child)
            self.assertTrue(reader.read('key', {'keysym': 'Return'}, self.b.limits.deadline_ns)['focus_verified'])
            self.assertTrue(m._yes(reader.drain()))

    # @anchor:wa.tests.mw_owners_srf_unit.TestNative.test_native_reserve_and_no_retry
    def test_native_reserve_and_no_retry(self):
        children = []
        def spawn(*a, **k):
            children.append(_Process(stdout=b'bad'))
            return children[-1]
        reader = m.BoundedNativeRead(_policy(), process_factory=spawn)
        reader.bind(self.invocation, resources=self.b.resources, clock=self.b.clock, deadline_ns=self.b.limits.deadline_ns)
        with patch.object(m, 'load_identity_env', return_value={}):
            self.b.resources.denied = 'native_read'
            with self.assertRaises(ValueError):
                reader.read('downloads', {}, self.b.limits.deadline_ns)
            self.assertEqual(children, [])
            self.b.resources.denied = None
            with self.assertRaises(ValueError):
                reader.read('downloads', {}, self.b.limits.deadline_ns)
            self.assertEqual(len(children), 1)

    # @anchor:wa.tests.mw_owners_srf_unit.TestNative.test_native_storage
    def test_native_storage(self):
        with tempfile.TemporaryDirectory(dir='/dev/shm') as ram, patch.object(m, 'load_identity_env', return_value={}):
            actual = Path(ram) / 'synthetic'
            actual.touch()
            link = Path(ram) / 'link'
            link.symlink_to(actual)
            for path, passes in ((str(self.b.root / 'frame'), False), (str(link), False), (str(actual), True)):
                child = _Process(stdout=base.g.canon({'path': path}).encode())
                reader = self._reader(child)
                if passes:
                    self.assertEqual(reader.read('frame', {}, self.b.limits.deadline_ns)['path'], path)
                else:
                    with self.assertRaises(ValueError):
                        reader.read('frame', {}, self.b.limits.deadline_ns)

    # @anchor:wa.tests.mw_owners_srf_unit.TestNative.test_download_codec
    def test_download_codec(self):
        codec = m.DownloadsCodec(_policy(rows=2), resources=self.b.resources)
        row = dict(name='PRIVATE_DOWNLOAD', bytes=10, mtime=1)
        self.assertEqual(codec.decode([]), frozenset())
        for rows in ({}, [dict(row, extra=1)], [dict(row, bytes=True)], [dict(row, bytes=-1)],
                     [row, row], [dict(row, name=str(i)) for i in range(3)]):
            with self.assertRaises(ValueError):
                codec.decode(rows)
        old = codec.decode([row])
        self.assertEqual(codec.decode([dict(row, bytes=20, mtime=2)]), old)
        self.assertEqual(codec.decode([row]), old)
        new = codec.decode([row, dict(row, name='genuine-new')])
        self.assertTrue(new - old)
        self.assertNotIn('PRIVATE_DOWNLOAD', repr(new))

    # @anchor:wa.tests.mw_owners_srf_unit.TestNative.test_native_privacy
    def test_native_privacy(self):
        seen = []
        def spawn(argv, **kwargs):
            seen.append((argv, kwargs['env']))
            return _Process(stdout=b'PRIVATE_MARKER', stderr=b'PRIVATE_MARKER')
        reader = m.BoundedNativeRead(_policy(), process_factory=spawn)
        reader.bind(self.invocation, resources=self.b.resources, clock=self.b.clock, deadline_ns=self.b.limits.deadline_ns)
        with patch.object(m, 'load_identity_env', return_value={}):
            with self.assertRaises(ValueError) as exc:
                reader.read('key', {'keysym': 'Return'}, self.b.limits.deadline_ns)
            self.assertEqual(str(exc.exception), 'OUT_OF_SUPPORT')
            self.assertNotIn('PRIVATE_MARKER', repr(seen))
            with self.assertRaises(ValueError):
                reader.read('type', {'text': 'PRIVATE_MARKER', 'paste': True}, self.b.limits.deadline_ns)
            self.assertEqual(len(seen), 1)
        def failed_spawn(*a, **k):
            raise ValueError('PRIVATE_MARKER')
        reader = m.BoundedNativeRead(_policy(), process_factory=failed_spawn)
        reader.bind(self.invocation, resources=self.b.resources, clock=self.b.clock, deadline_ns=self.b.limits.deadline_ns)
        with patch.object(m, 'load_identity_env', return_value={}), self.assertRaisesRegex(ValueError, '^OUT_OF_SUPPORT$'):
            reader.read('key', {'keysym': 'Return'}, self.b.limits.deadline_ns)

    # @anchor:wa.tests.mw_owners_srf_unit.TestNative.test_native_process_limit
    def test_native_process_limit(self):
        ready = threading.Event()
        children = []
        errors = []
        def spawn(*a, **k):
            child = _Process(stalled=True)
            children.append(child)
            ready.set()
            return child
        reader = m.BoundedNativeRead(_policy(timeout_ns=200_000_000), process_factory=spawn)
        reader.bind(self.invocation, resources=self.b.resources, clock=self.b.clock, deadline_ns=self.b.limits.deadline_ns)
        def first():
            try:
                reader.read('key', {'keysym': 'Return'}, self.b.limits.deadline_ns)
            except ValueError:
                errors.append(1)
        with patch.object(m, 'load_identity_env', return_value={}):
            thread = threading.Thread(target=first)
            thread.start()
            self.assertTrue(ready.wait(timeout=1))
            try:
                with self.assertRaises(ValueError):
                    reader.read('key', {'keysym': 'Return'}, self.b.limits.deadline_ns)
                self.assertEqual(len(children), 1)
            finally:
                thread.join(timeout=2)
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [1])
            self.assertTrue(m._yes(reader.drain()))
            reader._spawn = lambda *a, **k: _Process()
            self.assertEqual(reader.read('key', {'keysym': 'Return'}, self.b.limits.deadline_ns), {})

    # @anchor:wa.tests.mw_owners_srf_unit.TestNative.test_aggregate_reservations
    def test_aggregate_reservations(self):
        held = [0]
        calls = []
        policy = _policy()
        ceiling = 400000
        def reserve(kind, amount):
            if held[0] + amount > ceiling:
                return NO
            held[0] += amount
            return YES
        self.b.resources.reserve = reserve
        def spawn(*a, **k):
            calls.append(1)
            return _Process()
        reader = m.BoundedNativeRead(policy, process_factory=spawn)
        reader.bind(self.invocation, resources=self.b.resources, clock=self.b.clock, deadline_ns=self.b.limits.deadline_ns)
        with patch.object(m, 'load_identity_env', return_value={}):
            reader.read('key', {'keysym': 'Return'}, self.b.limits.deadline_ns)
            self.assertTrue(m._yes(reader.drain()))
            self.assertGreater(held[0], 0)  # Join alone never refunds ownership.
            with self.assertRaises(ValueError):
                reader.read('key', {'keysym': 'Return'}, self.b.limits.deadline_ns)
            self.assertEqual(len(calls), 1)
            held[0] = 0  # Explicit trusted resource-drain positive control.
            reader.read('key', {'keysym': 'Return'}, self.b.limits.deadline_ns)
            self.assertEqual(len(calls), 2)

    # @anchor:wa.tests.mw_owners_srf_unit.TestNative.test_download_native_wire
    def test_download_native_wire(self):
        calls = []
        responses = iter((b'{}', b'[', b'[]', b'[{"name":"private","bytes":1,"mtime":2}]'))
        def spawn(argv, **kwargs):
            calls.append(argv)
            return _Process(stdout=next(responses))
        reader = m.BoundedNativeRead(_policy(), process_factory=spawn)
        reader.bind(self.invocation, resources=self.b.resources, clock=self.b.clock, deadline_ns=self.b.limits.deadline_ns)
        with patch.object(m, 'load_identity_env', return_value={}):
            for _ in range(2):
                with self.assertRaises(ValueError):
                    reader.read('downloads', {}, self.b.limits.deadline_ns)
            self.assertEqual(reader.read('downloads', {}, self.b.limits.deadline_ns), [])
            self.assertEqual(len(reader.read('downloads', {}, self.b.limits.deadline_ns)), 1)
        self.assertEqual(calls, [[m.BINARY, 'downloads', '--container', self.invocation.container]] * 4)


# @anchor:wa.tests.mw_owners_srf_unit._PasteProcess
class _PasteProcess(_Process):
    """Private stdin pipe drained by a fake child thread; no executable is run."""
    # @anchor:wa.tests.mw_owners_srf_unit._PasteProcess.__init__
    def __init__(self, *args, read_input=True, broken=False, rc=0, **kwargs):
        super().__init__(*args, **kwargs)
        self.returncode = None if kwargs.get('stalled') else rc
        reader, writer = os.pipe()
        self.stdin = os.fdopen(writer, 'wb', buffering=0)
        self.received = bytearray()
        self._input_reader = reader
        self._thread = None
        if broken:
            os.close(reader)
            self._input_reader = None
        elif read_input:
            self._thread = threading.Thread(target=self._consume)
            self._thread.start()

    def _consume(self):
        while True:
            data = os.read(self._input_reader, 127)
            if not data:
                break
            self.received.extend(data)
        os.close(self._input_reader)
        self._input_reader = None

    # @anchor:wa.tests.mw_owners_srf_unit._PasteProcess.wait
    def wait(self, timeout):
        if self._thread is not None:
            self._thread.join(timeout=max(0, timeout))
            if self._thread.is_alive():
                raise subprocess.TimeoutExpired('fake', timeout)
        return super().wait(timeout)

    # @anchor:wa.tests.mw_owners_srf_unit._PasteProcess.kill
    def kill(self):
        self.stdin.close()
        if self._thread is None and self._input_reader is not None:
            os.close(self._input_reader)
            self._input_reader = None
        super().kill()


# @anchor:wa.tests.mw_owners_srf_unit.TestStdin
class TestStdin(TestComposition):
    # @anchor:wa.tests.mw_owners_srf_unit.TestStdin.setUp
    def setUp(self):
        # The adapter fingerprints the producer binary with os.stat; spawns are faked, so any
        # stable regular file stands in for srf-identity-surface (no host build required).
        super().setUp()
        fd, path = tempfile.mkstemp(prefix='srf-identity-surface-')
        os.close(fd)
        self.addCleanup(os.unlink, path)
        patcher = patch.object(m, 'BINARY', path)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _adapter(self, *, probe=None, probe_rc=2, probe_options=None, send_rc=0, send=None, policy=None, **child_options):
        self.children, self.spawns = [], []
        def spawn(argv, **kwargs):
            probing = '--key' in argv
            raw = (base.g.canon({'error': {'code': 2, 'detail': 'choose exactly one input operation'}}).encode()
                   if probe is None else probe) if probing else (
                   base.g.canon({'focus_verified': True, 'input_mono_ns': self.b.clock()}).encode()
                   if send is None else send)
            child = _PasteProcess(stdout=raw, rc=probe_rc if probing else send_rc,
                                  **((probe_options or {}) if probing else child_options))
            self.spawns.append((argv, kwargs))
            self.children.append(child)
            return child
        adapter = m.StdinPrivatePaste(policy or _policy(), resources=self.b.resources,
            clock=self.b.clock, deadline_ns=self.b.limits.deadline_ns, process_factory=spawn)
        self.addCleanup(adapter.drain)
        return adapter

    def _bound(self, **kwargs):
        adapter = self._adapter(**kwargs)
        self.surface = _Surface(identity='synthetic', transport=lambda *a: None, trace=None)
        adapter.bind(self.invocation, scope=self.ports.scope, authority=self.ports.authority, surface=self.surface)
        return adapter

    def _stage(self, adapter, payload=b'PRIVATE_CANARY', *, deadline=None, slot_id='prompt'):
        self.slot = dict(slot_id=slot_id, transport='clipboard_paste', budget_ms=50)
        return adapter.stage(self.slot, payload, types.SimpleNamespace(
            deadline_ns=self.b.limits.deadline_ns if deadline is None else deadline))

    def _send(self, adapter, *, slot='prompt', deadline=None):
        return adapter.send_at(dict(kind='paste', text_slot=slot), None,
            deadline_ns=self.b.limits.deadline_ns if deadline is None else deadline)

    def _ready(self, adapter):
        return m._yes(adapter.check_ready(self.invocation, self.ports.scope, self.ports.authority))

    # @anchor:wa.tests.mw_owners_srf_unit.TestStdin.test_builder_inventory
    def test_builder_inventory(self):
        dependencies = self._dependencies()
        for name in dependencies:
            if name in ('ram', 'slots', 'replay_policy', 'private_paste'):
                continue
            with self.subTest(dependency=name), self.assertRaises(ValueError):
                m.build_live_ports(self.invocation, **dict(dependencies, **{name: None}))
            ports = m.build_live_ports(self.invocation, **dependencies)
            self.assertIs(type(ports), m.LivePorts)
            for field in fields(m.LivePorts):
                if field.name != 'native_read':
                    self.assertIs(getattr(ports, field.name), getattr(self.ports, field.name))
        inventory = dict(artifact=('pin', 'check'), authorization=('inventory', 'check'),
            capabilities=('supports', 'target', 'validate_target', 'focus', 'action', 'verifier', 'equivalent', 'satisfaction', 'final', 'changed'),
            observations=('acquire', 'validate'), resources=('reserve', 'cancel', 'drain'),
            journal=('read', 'digest', 'authenticate', 'recover', 'reserve', 'append', 'stage', 'validate', 'select', 'context'))
        for owner, methods in inventory.items():
            for method in methods:
                with self.subTest(owner=owner, method=method):
                    with patch.object(dependencies[owner], method, None), self.assertRaises(ValueError):
                        m.build_live_ports(self.invocation, **dependencies)
                    self.assertIs(type(m.build_live_ports(self.invocation, **dependencies)), m.LivePorts)
        for method in ('bind', 'read', 'drain'):
            with patch.object(m.BoundedNativeRead, method, None), self.assertRaises(ValueError):
                m.build_live_ports(self.invocation, **dependencies)
            self.assertIs(type(m.build_live_ports(self.invocation, **dependencies)), m.LivePorts)
        self.assertEqual(self.factory_calls, [])
        self.assertEqual(self.native.calls, [])
        self.assertIsNone(self.ports.actor_lock.holder)

    # @anchor:wa.tests.mw_owners_srf_unit.TestStdin.test_cli_real_builder
    def test_cli_real_builder(self):
        self.b._write()
        real_open = m.open_srf_owners
        def opened(invocation, *, ports):
            return real_open(invocation, ports=ports, surface_factory=self._surface)
        with patch.object(cli, 'trusted_live_dependencies', return_value=self._dependencies()) as provision, \
             patch.object(cli, 'open_srf_owners', side_effect=opened), \
             patch.object(m.BoundedNativeRead, 'read', side_effect=self.native.read), \
             contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(cli.main(self._cli_args()), 0)
            provision.assert_called_once()
            self.assertEqual(len(self.factory_calls), 1)
            self.b.journal.raw = b''
            with patch.object(self.b.authorization, 'check', None):
                self.assertEqual(cli.main(self._cli_args()), 5)
            self.assertEqual(len(self.factory_calls), 1)
        self.assertNotIn(self.invocation.live_ack, out.getvalue())

    # @anchor:wa.tests.mw_owners_srf_unit.TestStdin.test_probe_and_binding
    def test_probe_and_binding(self):
        with patch.object(m, 'load_identity_env', return_value={}), patch.object(m.os, 'stat', return_value=types.SimpleNamespace(
                st_dev=1, st_ino=2, st_size=3, st_mtime_ns=4, st_ctime_ns=5, st_mode=0o40755)):
            for probe, rc in ((b'{}', 2), (b'bad', 2), (b'{"error":{"code":2,"detail":"unknown flag"}}', 2),
                              (b'{"error":{"code":true,"detail":"choose exactly one input operation"}}', 2), (None, 0)):
                a = self._bound(probe=probe, probe_rc=rc)
                self.assertFalse(self._ready(a))
                self.assertEqual(bytes(self.children[0].received), b'')
            a = self._bound()
            self.assertFalse(m._yes(a.check_ready(replace(self.invocation), 'scope', 'authority')))
            self.assertFalse(m._yes(a.check_ready(self.invocation, 'foreign', 'authority')))
            self.assertTrue(self._ready(a))
            self.assertTrue(self._ready(a))
            self.assertEqual(len(self.spawns), 1)
            self.assertIn('--key', self.spawns[0][0])
            self.assertIn('--type-stdin', self.spawns[0][0])

    # @anchor:wa.tests.mw_owners_srf_unit.TestStdin.test_fidelity_privacy_once
    def test_fidelity_privacy_once(self):
        with patch.object(m, 'load_identity_env', return_value={}), patch.object(m.StdinPrivatePaste, '_identity', return_value=(1,)):
            for text in ('PRIVATE_CANARY', 'é文', 'x\n', 'x\r', 'x\r\n', ''):
                a = self._bound()
                self.assertTrue(self._ready(a))
                self._stage(a, text.encode())
                self.assertIs(type(self._send(a)), int)
                wire = bytes(self.children[-1].received)
                self.assertEqual(wire.removesuffix(b'\n').removesuffix(b'\r'), text.encode())
                self.assertNotIn('PRIVATE_CANARY', repr(self.spawns))
                self.assertTrue(self.spawns[-1][1]['start_new_session'])
                self.assertEqual(set(self.spawns[-1][1]['env']), {'PATH'})
                self.assertIs(self.spawns[-1][1]['stdin'], subprocess.PIPE)
                with self.assertRaises(ValueError):
                    self._send(a)
                self.assertEqual(len(self.children), 2)
                self.assertIsNone(a._pending)
                self.assertTrue(m._yes(a.drain()))
                with self.assertRaises(ValueError):
                    self._stage(a)

    # @anchor:wa.tests.mw_owners_srf_unit.TestStdin.test_staging_bounds_and_expiry
    def test_staging_bounds_and_expiry(self):
        with patch.object(m, 'load_identity_env', return_value={}), patch.object(m.StdinPrivatePaste, '_identity', return_value=(1,)):
            a = self._bound()
            self.assertTrue(self._ready(a))
            for payload in (b'x' * 16383, ('é' * 8192).encode(), b'\xff'):
                with self.assertRaises(ValueError):
                    self._stage(a, payload)
            self.assertEqual(len(self.children), 1)
            self._stage(a, b'x' * 16382)
            with self.assertRaises(ValueError):
                self._stage(a, b'second')
            self.assertIs(type(self._send(a)), int)
            self.assertEqual(len(self.children[-1].received), 16384)
            self._stage(a)
            with self.assertRaises(ValueError):
                self._send(a, slot='foreign')
            self.assertIsNone(a._pending)
            with self.assertRaises(ValueError):
                self._stage(a, deadline=self.b.clock())
            self._stage(a)
            self.b.clock.sleep(51_000_000)
            with self.assertRaises(ValueError):
                self._send(a)
            self.assertEqual(len(self.children), 2)

    # @anchor:wa.tests.mw_owners_srf_unit.TestStdin.test_deadline_and_pipes
    def test_deadline_and_pipes(self):
        with patch.object(m, 'load_identity_env', return_value={}), patch.object(m.StdinPrivatePaste, '_identity', return_value=(1,)):
            a = self._bound()
            self.assertTrue(self._ready(a))
            self._stage(a)
            with self.assertRaises(ValueError):
                self._send(a, deadline=self.b.clock())
            self.assertEqual(len(self.children), 1)
            self._stage(a, b'x' * 12000)
            real_write = os.write
            def partial(fd, data):
                return real_write(fd, data[:31]) if any(child.stdin.fileno() == fd for child in self.children if not child.stdin.closed) else real_write(fd, data)
            with patch.object(m.os, 'write', side_effect=partial):
                self.assertIs(type(self._send(a)), int)
            self.assertEqual(len(self.children[-1].received), 12002)
            for options in (dict(read_input=False, stalled=True), dict(stdout_unused=True)):
                if options.get('stdout_unused'):
                    a = self._bound(send=base.g.canon(dict(focus_verified=True, input_mono_ns=self.b.clock(), padding='x' * 4200)).encode())
                else:
                    a = self._bound(**options)
                self.assertTrue(self._ready(a))
                self._stage(a, b'x' * 12000)
                with self.assertRaises(ValueError):
                    self._send(a)
                self.assertTrue(self.children[-1].stdin.closed)
                self.assertIsNone(a._pending)
                self.assertTrue(m._yes(a.drain()))

    # @anchor:wa.tests.mw_owners_srf_unit.TestStdin.test_reservations
    def test_reservations(self):
        with patch.object(m, 'load_identity_env', return_value={}), patch.object(m.StdinPrivatePaste, '_identity', return_value=(1,)):
            a = self._bound()
            self.b.resources.denied = 'private_pipe'
            self.assertFalse(self._ready(a))
            self.assertEqual(self.children, [])
            self.b.resources.denied = None
            self.assertTrue(self._ready(a))
            self.b.resources.denied = 'private_stage'
            with self.assertRaises(ValueError):
                self._stage(a)
            self.b.resources.denied = None
            self._stage(a)
            self.b.resources.denied = 'private_pipe'
            with self.assertRaises(ValueError):
                self._send(a)
            self.assertEqual(len(self.children), 1)
            self.b.resources.denied = None
            self._stage(a)
            self.assertIs(type(self._send(a)), int)

    # @anchor:wa.tests.mw_owners_srf_unit.TestStdin.test_identity_and_receipt
    def test_identity_and_receipt(self):
        with patch.object(m, 'load_identity_env', return_value={}), patch.object(m.StdinPrivatePaste, '_identity', return_value=(1,)) as identity:
            a = self._bound()
            self.assertTrue(self._ready(a))
            self._stage(a)
            identity.return_value = (2,)
            self.assertFalse(self._ready(a))
            with self.assertRaises(ValueError):
                self._send(a)
            self.assertEqual(len(self.children), 1)
            identity.return_value = (1,)
            self._stage(a)
            self.assertIs(type(self._send(a)), int)
            for response in (b'{"focus_verified":false,"input_mono_ns":1}',
                             b'{"focus_verified":true,"input_mono_ns":true}', b'PRIVATE_CANARY'):
                a = self._bound(send=response)
                self.assertTrue(self._ready(a))
                self._stage(a)
                with self.assertRaisesRegex(ValueError, '^OUT_OF_SUPPORT$') as exc:
                    self._send(a)
                self.assertNotIn('PRIVATE_CANARY', repr(exc.exception))
                self.assertIsNone(a._pending)

    # @anchor:wa.tests.mw_owners_srf_unit.TestStdin.test_workflow_and_cleanup
    def test_workflow_and_cleanup(self):
        self._paste()
        self.b.slots.value = 'PRIVATE_CANARYé'
        self.b.observations.values['length'] = [len(self.b.slots.value)]
        with patch.object(m, 'load_identity_env', return_value={}), patch.object(m.StdinPrivatePaste, '_identity', return_value=(1,)):
            a = self._adapter()
            self.ports = replace(self.ports, private_paste=a)
            result = self._run()
            self.assertEqual(result.rc, 0)
            self.assertNotIn('PRIVATE_CANARY', repr(result))
            self.assertEqual(bytes(self.children[-1].received), self.b.slots.value.encode() + b'\r\n')
            self.assertNotIn('PRIVATE_CANARY', repr(self.spawns))
            self.assertNotIn('PRIVATE_CANARY', repr(self.factory_calls))
            self.assertNotIn('PRIVATE_CANARY', repr(self.b.journal.raw))
            self.assertIsNone(self.ports.actor_lock.holder)
            self.assertGreater(self.b.slots.cleared_count, 0)
            self.assertIsNone(a._pending)

    # @anchor:wa.tests.mw_owners_srf_unit.TestStdin.test_failed_prepare_and_custody
    def test_failed_prepare_and_custody(self):
        self._paste()
        with patch.object(m, 'load_identity_env', return_value={}), patch.object(m.StdinPrivatePaste, '_identity', return_value=(1,)):
            a = self._adapter()
            self.ports = replace(self.ports, private_paste=a)
            with self._open() as owners:
                denied = False
                with patch.object(self.b.slots, 'prepare', return_value=NO):
                    try:
                        owners.slots.prepare(self.b.workflow['text_slots'][0], b'PRIVATE_CANARY',
                            types.SimpleNamespace(deadline_ns=self.b.limits.deadline_ns))
                    except ValueError:
                        denied = True
                empty = a._pending is None
            self.assertTrue(denied)
            self.assertTrue(empty)
            self.assertGreater(self.b.slots.cleared_count, 0)
            a = self._adapter(broken=True)
            self.ports = replace(self.ports, private_paste=a)
            self.b.journal.raw = b''
            self.assertEqual(self._run().rc, 5)
            self.assertEqual(len(self.children), 2)
            self.assertIsNone(a._pending)
            self.assertIsNone(self.ports.actor_lock.holder)
            a = self._adapter(unjoined=True)
            self.ports = replace(self.ports, private_paste=a)
            with self.assertRaises(ValueError):
                with self._open() as owners:
                    self.assertTrue(self._ready(a))
                    self._stage(a)
                    with self.assertRaises(ValueError):
                        self._send(a)
            self.assertIsNotNone(self.ports.actor_lock.holder)
            self.assertFalse(m._yes(a.drain()))

    # @anchor:wa.tests.mw_owners_srf_unit.TestStdin.test_announcement_before_probe
    def test_announcement_before_probe(self):
        with patch.object(m, 'load_identity_env', return_value={}), patch.object(m.StdinPrivatePaste, '_identity', return_value=(1,)):
            for change in ('notice', 'ack', 'busy', 'foreign'):
                a = self._adapter()
                ports = replace(self.ports, private_paste=a)
                inv = self.invocation
                if change == 'notice':
                    ports = replace(ports, announce_gate=lambda *a, **k: NO)
                elif change == 'ack':
                    inv = replace(inv, live_ack='arbitrary')
                elif change == 'foreign':
                    ports = replace(ports, authority='foreign')
                else:
                    ports.actor_lock.acquire('other')
                try:
                    with self.assertRaises(Exception):
                        with self._open(inv, ports):
                            self._ready(a)
                    self.assertEqual(self.spawns, [])
                finally:
                    if ports.actor_lock.holder == 'other':
                        ports.actor_lock.release('other')
            a = self._adapter()
            with self._open(ports=replace(self.ports, private_paste=a)):
                self.assertTrue(self._ready(a))
            self.assertEqual(len(self.spawns), 1)

    # @anchor:wa.tests.mw_owners_srf_unit.TestStdin.test_probe_timeout_and_output_caps
    def test_probe_timeout_and_output_caps(self):
        with patch.object(m, 'load_identity_env', return_value={}), patch.object(m.StdinPrivatePaste, '_identity', return_value=(1,)):
            for options in (dict(stalled=True), dict(stderr=b'PRIVATE_CANARY'), dict(stderr=b'x' * 1025)):
                a = self._bound(probe_options=options)
                self.assertFalse(self._ready(a))
                self.assertEqual(len(self.children), 1)
                self.assertTrue(m._yes(a.drain()))
            for options in (dict(stderr=b'x' * 1025), dict(stalled=True)):
                a = self._bound(**options)
                self.assertTrue(self._ready(a))
                self._stage(a)
                with self.assertRaisesRegex(ValueError, '^OUT_OF_SUPPORT$'):
                    self._send(a)
                self.assertIsNone(a._pending)
                self.assertEqual(len(self.children), 2)
            a = self._bound(stderr=b'PRIVATE_CANARY')
            self.assertTrue(self._ready(a))
            self._stage(a)
            self.assertIs(type(self._send(a)), int)
            self.assertNotIn('PRIVATE_CANARY', repr(self.spawns))

    # @anchor:wa.tests.mw_owners_srf_unit.TestStdin.test_shared_process_ceiling
    def test_shared_process_ceiling(self):
        native = m.BoundedNativeRead(_policy(), process_factory=lambda *a, **k: None)
        native.bind(self.invocation, resources=self.b.resources, clock=self.b.clock, deadline_ns=self.b.limits.deadline_ns)
        a = self._adapter()
        surface = _Surface(identity='synthetic', transport=lambda *a: None, trace=None)
        a.bind(self.invocation, scope='scope', authority='authority', surface=surface, native_read=native)
        with patch.object(m, 'load_identity_env', return_value={}), patch.object(m.StdinPrivatePaste, '_identity', return_value=(1,)):
            native._active.add('held-child')
            self.assertFalse(self._ready(a))
            self.assertEqual(self.children, [])
            native._active.clear()
            self.assertTrue(self._ready(a))
            self._stage(a)
            native._active.add('held-child')
            with self.assertRaises(ValueError):
                self._send(a)
            self.assertEqual(len(self.children), 1)
            native._active.clear()
            self._stage(a)
            self.assertIs(type(self._send(a)), int)
            self.assertTrue(m._yes(native.drain()))

    # @anchor:wa.tests.mw_owners_srf_unit.TestStdin.test_propagation_and_clipboard_failure
    def test_propagation_and_clipboard_failure(self):
        self._paste()
        with patch.object(m, 'load_identity_env', return_value={}), patch.object(m.StdinPrivatePaste, '_identity', return_value=(1,)):
            a = self._adapter()
            self.ports = replace(self.ports, private_paste=a)
            with self._open() as owners:
                self.assertTrue(m._yes(owners.capabilities.supports(self.b.workflow)))
                action = self.b.workflow['steps'][0]['action']
                state = types.SimpleNamespace(deadline_ns=self.b.clock() + 30_000_000)
                owners.capabilities.focus(self.b.workflow['steps'][0], state)
                self.assertTrue(m._yes(owners.capabilities.action(action, None)))
                self.assertTrue(m._yes(owners.slots.prepare(self.b.workflow['text_slots'][0], b'PRIVATE_CANARY',
                    types.SimpleNamespace(deadline_ns=self.b.limits.deadline_ns))))
                self.assertIs(type(owners.transport.send(action, None)), int)
                argv = self.spawns[-1][0]
                remaining = int(argv[argv.index('--timeout') + 1][:-2])
            self.assertLessEqual(remaining, 30_000_000)
            a = self._adapter()
            self.ports = replace(self.ports, private_paste=a)
            self.b.slots.clean = False
            with self.assertRaises(ValueError):
                with self._open() as owners:
                    self.assertTrue(m._yes(owners.slots.prepare(self.b.workflow['text_slots'][0], b'PRIVATE_CANARY',
                        types.SimpleNamespace(deadline_ns=self.b.limits.deadline_ns))))
                    self.assertTrue(m._yes(owners.slots.clear(self.b.workflow['text_slots'][0])))
                    self.assertFalse(m._yes(owners.slots.cleared(self.b.workflow['text_slots'][0])))
                    self.assertIsNone(a._pending)
            self.assertIsNotNone(self.ports.actor_lock.holder)
            self.assertIsNone(a._pending)

    # @anchor:wa.tests.mw_owners_srf_unit.TestStdin.test_executable_recheck_at_spawn
    def test_executable_recheck_at_spawn(self):
        with patch.object(m, 'load_identity_env', return_value={}), patch.object(m.StdinPrivatePaste, '_identity', return_value=(1,)) as identity:
            a = self._bound()
            self.assertTrue(self._ready(a))
            self._stage(a)
            identity.side_effect = [(1,), (2,)]
            with self.assertRaises(ValueError):
                self._send(a)
            self.assertEqual(len(self.children), 1)
            identity.side_effect = None
            self._stage(a)
            self.assertIs(type(self._send(a)), int)

    # @anchor:wa.tests.mw_owners_srf_unit.TestStdin.test_workflow_slot_budgets
    def test_workflow_slot_budgets(self):
        self._paste()
        self.b.slots.value = 'éé'
        self.b.observations.values['length'] = [2]
        with patch.object(m, 'load_identity_env', return_value={}), patch.object(m.StdinPrivatePaste, '_identity', return_value=(1,)):
            for slot_bytes, aggregate, rc in ((3, 4, 5), (4, 3, 5), (4, 4, 0)):
                self.b.journal.raw = b''
                a = self._adapter()
                self.ports = replace(self.ports, private_paste=a,
                    limits=replace(self.b.limits, slot_bytes=slot_bytes, aggregate_slot_bytes=aggregate))
                self.assertEqual(self._run().rc, rc)
                self.assertEqual(len(self.children), 1 if rc else 2)
                self.assertIsNone(self.ports.actor_lock.holder)

    # @anchor:wa.tests.mw_owners_srf_unit.TestStdin.test_native_failure_no_retry
    def test_native_failure_no_retry(self):
        with patch.object(m, 'load_identity_env', return_value={}), patch.object(m.StdinPrivatePaste, '_identity', return_value=(1,)):
            a = self._bound(send_rc=5)
            self.assertTrue(self._ready(a))
            self._stage(a)
            with self.assertRaisesRegex(ValueError, '^OUT_OF_SUPPORT$'):
                self._send(a)
            self.assertEqual(len(self.children), 2)
            self.assertIsNone(a._pending)
            with self.assertRaises(ValueError):
                self._send(a)
            self.assertEqual(len(self.children), 2)
            a = self._bound()
            self.assertTrue(self._ready(a))
            self._stage(a)
            self.assertIs(type(self._send(a)), int)

    # @anchor:wa.tests.mw_owners_srf_unit.TestStdin.test_join_and_poison
    def test_join_and_poison(self):
        with patch.object(m, 'load_identity_env', return_value={}), patch.object(m.StdinPrivatePaste, '_identity', return_value=(1,)):
            a = self._bound(unjoined=True)
            self.assertTrue(self._ready(a))
            self._stage(a)
            with self.assertRaisesRegex(ValueError, '^OUT_OF_SUPPORT$'):
                self._send(a)
            self.assertFalse(m._yes(a.drain()))
            self.assertFalse(self._ready(a))
            self.assertIsNone(a._pending)
            with self.assertRaises(ValueError):
                self._stage(a)
            self.assertEqual(len(self.children), 2)

    # @anchor:wa.tests.mw_owners_srf_unit.TestStdin.test_anchor_negative_controls
    def test_anchor_negative_controls(self):
        original_read = Path.read_text
        anchor = '# @anchor:wa.tests.mw_owners_srf_unit.TestNative.test_native_bounds'
        def without_anchor(path, *args, **kwargs):
            text = original_read(path, *args, **kwargs)
            return text.replace(anchor, '# missing anchor') if path == Path(__file__) else text
        result = unittest.TestResult()
        with patch.object(Path, 'read_text', without_anchor):
            TestStdin('test_anchor_discovery').run(result)
        self.assertEqual(len(result.failures), 1)
        self.assertEqual(result.errors, [])
        original_loader = load_tests
        def omit_test(*args):
            suite = original_loader(*args)
            suite._tests.pop()
            return suite
        result = unittest.TestResult()
        with patch.object(sys.modules[__name__], 'load_tests', side_effect=omit_test):
            TestStdin('test_anchor_discovery').run(result)
        self.assertEqual(len(result.failures), 1)
        self.assertEqual(result.errors, [])
        result = unittest.TestResult()
        TestStdin('test_anchor_discovery').run(result)
        self.assertTrue(result.wasSuccessful())

    # @anchor:wa.tests.mw_owners_srf_unit.TestStdin.test_spec_mutant_controls
    def test_spec_mutant_controls(self):
        if os.environ.get('MWP7_MUTANT'):
            return
        for mutant, name in SPEC_MUTANT_CASES.items():
            with self.subTest(mutant=mutant):
                result = subprocess.run([sys.executable, '-m', 'unittest', __name__ + '.' + (name if '.' in name else 'TestStdin.' + name)],
                    env=dict(os.environ, MWP7_MUTANT=mutant, PYTHONDONTWRITEBYTECODE='1'),
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=30, check=False)
                output = result.stdout.decode('utf-8', errors='replace')
                self.assertEqual(result.returncode, 1, mutant + ': ' + output[-2000:])
                self.assertIn('FAILED (failures=', output, mutant + ': ' + output[-2000:])
                self.assertNotIn('ERROR:', output, mutant + ': ' + output[-2000:])

    # @anchor:wa.tests.mw_owners_srf_unit.TestStdin.test_anchor_discovery
    def test_anchor_discovery(self):
        source = Path(__file__).read_text()
        tree = ast.parse(source)
        expected = set()
        for cls in (n for n in tree.body if isinstance(n, ast.ClassDef) and n.name.startswith('Test')):
            for node in cls.body:
                if isinstance(node, ast.FunctionDef) and node.name.startswith('test_'):
                    anchor = '# @anchor:wa.tests.mw_owners_srf_unit.' + cls.name + '.' + node.name
                    self.assertEqual(source.splitlines()[node.lineno - 2].strip(), anchor)
                    expected.add(cls.name + '.' + node.name)
        def flatten(suite):
            for entry in suite:
                if isinstance(entry, unittest.TestSuite):
                    yield from flatten(entry)
                else:
                    yield entry.__class__.__name__ + '.' + entry._testMethodName
        self.assertEqual(set(flatten(load_tests(unittest.TestLoader(), None, None))), expected)


# @anchor:wa.tests.mw_owners_srf_unit.load_tests
def load_tests(loader, tests, pattern):
    suite = unittest.TestSuite()
    suite.addTests(loader.loadTestsFromTestCase(TestComposition))
    for name in ('test_native_bounds', 'test_native_reserve_and_no_retry', 'test_native_storage', 'test_download_codec', 'test_native_privacy', 'test_native_process_limit', 'test_aggregate_reservations', 'test_download_native_wire'):
        suite.addTest(TestNative(name))
    for name in TestStdin.__dict__:
        if name.startswith('test_'):
            suite.addTest(TestStdin(name))
    return suite
