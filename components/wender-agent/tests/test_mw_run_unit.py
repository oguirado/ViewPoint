# @module {"id":"wa.tests.mw_run_unit","layer":4,"owner":"LEARN","deps":["wa.learn.mw_workflow","wa.tools.mw_run","wa.tools.memoryweb_gate"],"book":"HB-WA-s4-learn"}
"""RAM-only synthetic owners; no claim of installed/live authorization.

MWP3_MUTANT selects an in-memory source mutation for discriminating public API
test runs. No source file, upstream test or journal framing is modified by it.
"""
import contextlib
import copy
from dataclasses import replace
import hashlib
import io
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

from tools import memoryweb_gate as g
from tools import mw_run as cli
from wender.learn import mw_workflow as w
from wender.scripts.types import AdmissionCheck, Truth, Reason
from wender.learn.script_live import LegacyDispatchOwner
from wender.learn import demo_replay as replay
from wender.learn import demonstrations as demos
from wender.perceive.srf_identity import SrfIdentitySurface
from tests.test_memoryweb_gate_unit import _fixture, _step, _pred, _explanation

BUNDLE = g.strict_load((g.DOCS / 'mw.v1.schema.json').read_bytes())
YES = AdmissionCheck(Truth.TRUE, None)
NO = AdmissionCheck(Truth.UNKNOWN, Reason.MISSING)

# Each replacement removes one check. Tests exercise public APIs, not these
# private helpers. The external audit runner records nonzero assertion failures.
MUTANTS = {
    'canonical': ("raw == (gate.canon(workflow) + '\\n').encode()", 'True'),
    'bound': ('len(raw) <= min(limits.workflow_bytes, 65536)', 'True'),
    'pin': ('hashlib.sha256(raw).hexdigest() == pin.sha256 and _admitted(owners.artifact.check(pin))', 'True'),
    'immutable': ('type(pin) is PinnedArtifact and _admitted(pin.immutable)', 'type(pin) is PinnedArtifact'),
    'callbacks': ("type(value) is gate.GateResult and type(value.rc) is int and value.rc == 0 and value.category == 'PASS'", 'bool(value)'),
    'authorization': ('_admitted(owners.authorization.check(phase, state.workflow, state.scope, state.authority))', 'True'),
    'missingness': ('value.complete is True and value.missing_reason is None', 'True'),
    'typed': ("observation.output_kind == 'integer' and type(value) is int and type(old.value) is int", "observation.output_kind == 'integer'"),
    'units': ('_compatible(baseline.observation, observation)', 'True'),
    'download': ('answer = bool(value - old.value)', 'answer = bool(value)'),
    'protected': ("not step['protected'] and not any(ss.seq in members for members in state.required_segments.values())", 'True'),
    'satisfaction': ('_admitted(owners.capabilities.satisfaction(step, ss))', 'True'),
    'unknown': ("_need(truth is not Truth.UNKNOWN, 'LOW_CONFIDENCE')", 'pass'),
    'repeat': ("_need(ss.iteration < repeat['max_iterations'], 'STUCK')", 'pass'),
    'stuck': ('now - state.run.last_progress_ns >= 3 * owners.limits.no_progress_ns', 'False'),
    'busy': ("if busy is not None and not state.busy_seen:", 'if False:'),
    'causal': ('value.capture_start_ns <= after', 'False'),
    'reserve': ("_admitted(owners.journal.reserve(3 * owners.limits.event_bytes, 3))", 'True'),
    'target': ("_grounded(value, target, step_state, owners, deadline_ns)", 'True'),
    'locale': ("target['locale'] is not None", 'True'),
    'fallback': ("_admitted(owners.capabilities.equivalent(spec, child))", 'True'),
    'focus': ("_admitted(owners.capabilities.focus(step, ss))", 'True'),
    'cleanup': ("_admitted(owners.resources.cancel(limits.cancel_ns)) and _admitted(owners.resources.drain(limits.drain_ns))", 'True'),
    'restart': ("_need(not outstanding, 'INPUT_UNCERTAIN')", 'pass'),
    'torn': ("if raw and not raw.endswith(b'\\n'):", 'if False:'),
    'chain': ('and _admitted(owners.journal.authenticate(event))', ''),
    'final': ("_admitted(owners.capabilities.final(workflow))", 'True'),
    'inventory': ("_passed(gate.validate_file(Path(staged), bundle, context=owners.journal.context()))", 'True'),
    'atomic': ("_admitted(owners.journal.validate(staged, inventory))", 'True'),
    'retry': ('retries_exhausted=attempt == 3', 'retries_exhausted=False'),
    'private_admission': ('_admitted(owners.slots.admit(slot, ss))', 'True'),
    'private_verify': ('_admitted(owners.slots.verify(slot, ss, len(private.value), private.initial_length))', 'True'),
    'private_clear': ('_admitted(owners.slots.clear(slot)) and _admitted(owners.slots.cleared(slot))', 'True'),
    'identity': ("receipt = surface._validated_input(result, action['kind'])", 'receipt = result'),
    'local_journal': ('state.journal_seq + 3 <= owners.limits.event_count', 'True'),
    'capability': ('_admitted(owners.capabilities.supports(workflow))', 'True'),
    'pixel_mode': ("_admitted(owners.capabilities.verifier(spec))", 'True'),
    'final_predicate': ("for predicate in workflow['oracle']['postconditions']:", 'for predicate in ():'),
    'observation_scope': ('value.scope == owners.scope and value.authority == owners.authority', 'True'),
    'target_scope': ('value.scope == owners.scope and value.authority == owners.authority', 'True'),
    'snapshot_count': ("name not in state.snapshots and len(state.snapshots) < owners.limits.snapshots", 'True'),
    'acquisition': ('state.acquisitions < owners.limits.acquisitions', 'True'),
    'resource': ('_admitted(owners.resources.reserve(kind, amount))', 'True'),
    'input_stamp': ('boundary_ns <= receipt', '0 <= receipt'),
    'event_shape': ("type(key) is int and 1 <= key <= len(state.workflow['steps']) and\n              event['phase'] == ('delivery' if event['kind'] in ('intent', 'complete') else 'terminal')", 'True'),
    'native_rc': ('rc = disposition.rc if disposition.rc is not None else 5', 'rc = 5'),
    'identity_path': ("action['path'] == 'coordinates' and kind in", 'kind in'),
}
MUTANTS.update({
    'busy_clear': ("                state.consecutive = 0\n                _pause(owners, spec['poll_interval_ms'] * 1_000_000, state.verify_deadline_ns)\n                continue\n            else:", "            else:"),
    'fallback_depth': ("not child['fallback_verifiers'] and", 'True and'),
    'fallback_deadline': ("('busy_predicate', 'deadline_ms')", "('busy_predicate',)"),
    'fallback_baseline': ("(child['predicate'] or {}).get('snapshot_ref') == (spec['predicate'] or {}).get('snapshot_ref')", 'True'),
    'fallback_busy': ("('busy_predicate', 'deadline_ms')", "('deadline_ms',)"),
    'fallback_unknown': ("            if truth is Truth.TRUE:\n                passed = True", "            if truth is Truth.UNKNOWN:\n                break\n            if truth is Truth.TRUE:\n                passed = True"),
    'fallback_privacy': ("_admitted(owners.capabilities.verifier(spec))", 'True'),
    'observation_privacy': ("validation.reason in\n               (Reason.PRIVACY, Reason.SCHEMA, Reason.TYPE, Reason.BOUNDS)", 'False'),
    'changed': ("return result.result if type(result) is AdmissionCheck else Truth.UNKNOWN", 'return Truth.TRUE'),
    'after_crop': ("if result.verified is not True:", 'if False:'),
    'targetless_crop': ("state.target.translation if state.target is not None else (0, 0)", 'state.target.translation'),
    'fresh_step': ('value.observation.capture_start_ns >= state.start_ns', 'True'),
    'fresh_input': ('state.run.last_input_ns is None or value.observation.capture_start_ns > state.run.last_input_ns', 'True'),
    'chip_order': ("not prior or state.chip_keys[refs[pos - 1]] < state.chip_keys[ss.seq]", 'True'),
    'chip_verified': ("all(x is not None and x.classification == 'verified' and x.target is not None for x in prior)", 'True'),
    'paste_chips': ("all(state.classifications.get(n) == 'verified' and n in state.completed_steps and\n                  state.completed_steps[n].target is not None for n in slot['chip_clear_steps'])", 'True'),
    'repeat_exit': ('if ss.iteration == 0:', 'if True:'),
    'repeat_zero': ("busy_predicate=None, fallback_verifiers=[]", "busy_predicate=step['verify']['busy_predicate'], fallback_verifiers=[]"),
    'repeat_capture': ('            ss.start_ns = _time(owners)', '            _capture(step, state, owners)\n            ss.start_ns = _time(owners)'),
    'failed_class': ("state.classifications[ss.seq] = 'failed'", "state.classifications[ss.seq] = 'not_reached'"),
    'failed_restart': ("'condition_skip', 'failed')", "'condition_skip')"),
    'failed_count': ("sum(v == 'failed' for v in classifications.values())", '0'),
    'not_reached_count': ("sum(v == 'not_reached' for v in classifications.values())", '0'),
})
MUTANTS.update({
    't0_custody': ('_admitted(custody) and _valid_observation(publication, owners)', 'True'),
    't0_ambiguous': ('not any(c[0] == best[0] and c[2].point != best[2].point for c in candidates[1:])', 'True'),
    'target_validation': ('_admitted(owners.capabilities.validate_target(target, value, state))', 'True'),
    'inert': ("            _need(workflow['schema'] == 'mw.workflow.v1'", "            if workflow.get('script_ref'):\n                exec((root / workflow['script_ref']).read_bytes())\n            _need(workflow['schema'] == 'mw.workflow.v1'"),
    'storage': ("            frozen = owners.authorization.inventory(workflow)", "            (root / 'frame.png').write_bytes(b'pixels')\n            frozen = owners.authorization.inventory(workflow)"),
})
MUTANTS.update({
    'preconditions': ("_need(_predicate(predicate, state, owners, ss.deadline_ns) is Truth.TRUE, 'LOW_CONFIDENCE')", 'pass'),
    'truth': ('Truth.TRUE if value else Truth.FALSE', 'Truth.TRUE'),
    'delta': ('answer = value > old.value', 'answer = value >= old.value'),
})
MUTANTS.update({
    'atomic_order': ('            staged = owners.journal.stage(summary, inventory, owners.output)',
                     '            owners.journal.select(owners.output)\n            staged = owners.journal.stage(summary, inventory, owners.output)'),
})
MUTANTS.update({
    'action_contract': ('_admitted(owners.capabilities.action(action, ss.target))', 'True'),
    'append': ('_admitted(owners.journal.append(raw))', 'True'),
})
# MWP3F control audit: each entry removes a specific enforcement condition.
MUTANTS.update({
    'f_resume': ("_need(not state.resume_blocked, 'OUT_OF_SUPPORT')", 'pass'),
    'f_resume_precedence': ("_need(not outstanding, 'INPUT_UNCERTAIN')", "_need(not state.resume_blocked, 'OUT_OF_SUPPORT')"),
    'f_length': ("observed.value in allowed", 'True'),
    'f_convention': ("for c in slot['post_type_len_allowed']", "for c in ('exact_n', 'initial_plus_n')"),
    'f_unicode': ("len(private.value) if c == 'exact_n'", "len(private.value.encode()) if c == 'exact_n'"),
    'f_length_validity': ("_valid_observation(observed, owners)", 'True'),
    'f_length_type': ("observed.output_kind == 'integer' and type(observed.value) is int and observed.value >= 0", 'True'),
    'f_length_revision': ("observed.capability_revision == binding.capability_revision", 'True'),
    'f_length_unit': ("observed.unit == binding.unit", 'True'),
    'f_length_capability': ("observed.capability_id == binding.predicate['capability_id']", 'True'),
    'f_length_causal': ("observed.capture_start_ns > ss.input_ns", 'True'),
    'f_bindings': ("                _length_bindings(state, owners)", "                state.length_bindings = {b.slot_id: b for b in owners.slot_length_bindings}"),
    'f_binding_revision': ("(b.slot_id, b.capability_revision, b.unit, p.get('capability_id'))", "(b.slot_id, b.unit, p.get('capability_id'))"),
    'f_binding_unit': ("(b.slot_id, b.capability_revision, b.unit, p.get('capability_id'))", "(b.slot_id, b.capability_revision, p.get('capability_id'))"),
    'f_binding_descriptor': ("p.get('comparison') == 'truth' and p.get('snapshot_ref') is None", 'True'),
    'f_descriptor_retained': ("state.retained + bound <= owners.limits.retained_bytes", 'True'),
    'f_descriptor_reserve': ("_reserve(owners, 'length_descriptor', bound * 3)", 'pass'),
    'f_descriptor_shape': ("len(p['capability_id']) <= 80 and _length_predicate_shape(p)", 'True'),
    'f_descriptor_copy': ("gate.strict_load(gate.canon(p).encode())", 'p'),
    'f_length_acquire': ("state.acquisitions < owners.limits.acquisitions", 'True'),
    'f_length_deadline': ("min(slot_deadline, ss.verify_deadline_ns, owners.limits.deadline_ns)", 'owners.limits.deadline_ns'),
    'f_runlevel': ("state is None or not state.journal_ready or state.uncertain", "state is None or state.current_step is None or not state.journal_ready or state.uncertain"),
    'f_failure_fence': ("if state is None or not state.journal_ready or state.uncertain or code in ('TORN', 'INPUT_UNCERTAIN', 'SEAL_MISMATCH'):", "if state is None or not state.journal_ready:"),
    'f_failure_cancel': ("_admitted(owners.resources.cancel(owners.limits.cancel_ns))", 'True'),
    'f_failure_drain': ("_admitted(owners.resources.drain(owners.limits.drain_ns))", 'True'),
    'f_failure_authorize': ("_authorize('commit', state, owners)", 'pass'),
    'f_failure_gate': ("_passed(gate.validate_file(Path(staged), bundle, context=owners.journal.context()))", 'True'),
    'f_failure_validation': ("_admitted(owners.journal.validate(staged, inventory))", 'True'),
    'f_failure_select': ("_admitted(owners.journal.select(staged))", 'True'),
    'f_drag_span': ("payload.get('span') == start + ':' + ','.join(map(str, endpoint))", 'True'),
    'f_scroll_point': ("payload.get('point') == start", 'True'),
    'f_scroll_ground': ("_grounded(ss.target, None, ss, owners, ss.locate_deadline_ns)", 'True'),
    'f_scroll_reserve': ("_reserve(owners, 'target_peak', owners.limits.peak_bytes)", 'pass'),
    'f_chip_order': ("not prior or state.chip_keys[refs[pos - 1]] < state.chip_keys[ss.seq]", 'True'),
    'f_chip_auth': ("_passed(ctx.evidence_check('chip_geometry', step, (view, state.workflow)))", 'True'),
    'f_chip_fields': ("type(view.get('chip_id')) is str and 0 < len(view['chip_id']) <= owners.limits.workflow_bytes", "type(view.get('chip_id')) in (str, type(None))"),
    'f_preboundary': ("prepared = _identity_payload(owners.surface, owners.transport.planner(action, ss.target), action, ss.target)", "prepared = owners.transport.planner(action, ss.target)"),
    'f_chip_reserve': ("_reserve(owners, 'chip_identity', owners.limits.workflow_bytes)", 'pass'),
    'f_chip_retained': ("state.retained + amount <= owners.limits.retained_bytes", 'True'),
    'f_prepared_deadline': ("        _time(owners, delivery_deadline)\n        if ss.target is not None:", "        if ss.target is not None:"),
    'f_prepared_ground': ("_grounded(ss.target, step['target'], ss, owners, ss.locate_deadline_ns)", 'True'),
    'f_cli': ("print(gate.canon({'rc': 2, 'verdict_code': 'USAGE_SCHEMA'}))", 'pass'),
})
if os.environ.get('MWP3_MUTANT'):
    old, new = MUTANTS[os.environ['MWP3_MUTANT']]
    target_module = cli if os.environ['MWP3_MUTANT'] == 'f_cli' else w
    source = Path(target_module.__file__).read_text()
    if old not in source:
        raise RuntimeError('MUTANT_ANCHOR_ABSENT')
    module = types.ModuleType(target_module.__name__ + '_mutant')
    module.__file__ = target_module.__file__
    sys.modules[module.__name__] = module
    source = source.replace(old, new)
    if os.environ['MWP3_MUTANT'] == 'pin':
        source = source.replace('_admitted(owners.artifact.check(state.pin))', 'True')
    if os.environ['MWP3_MUTANT'] == 'retry':
        source = source.replace('range(4)', 'range(5)').replace('(100, 250, 500)[attempt]', '(100, 250, 500)[min(attempt, 2)]')
    if os.environ['MWP3_MUTANT'] == 'snapshot_count':
        source = source.replace('state.retained + amount <= owners.limits.retained_bytes', 'True')
    if os.environ['MWP3_MUTANT'] == 'f_chip_fields':
        source = source.replace("len(view['chip_id'])", "len(str(view.get('chip_id')))")
        source = source.replace("ss.target.point[1], view['chip_id']", "ss.target.point[1], str(view.get('chip_id'))")
    exec(compile(source, w.__file__, 'exec'), module.__dict__)
    if target_module is cli:
        cli = module
    else:
        w = module
        cli.run_workflow = w.run_workflow
        cli.RunOwners = w.RunOwners
    if os.environ['MWP3_MUTANT'] == 'canonical':
        # Remove the same redundant canonical check in the dependency as well.
        w.gate._parse = w.gate.strict_load


class _Clock:
    def __init__(self):
        self.now = 1_000_000

    def __call__(self):
        return self.now

    def sleep(self, ns):
        self.now += ns


class _Artifact:
    def __init__(self):
        self.immutable = YES
        self.changed = False
        self.pin_override = None
        self.active = False

    @contextlib.contextmanager
    def pin(self, path):
        self.active = True
        try:
            yield w.PinnedArtifact(path, self.pin_override or hashlib.sha256(path.read_bytes()).hexdigest(), self.immutable)
        finally:
            self.active = False

    def check(self, pin):
        return YES if self.active and not self.changed and hashlib.sha256(pin.path.read_bytes()).hexdigest() == pin.sha256 else NO


class _Authorization:
    def __init__(self):
        self.denied = None
        self.segments = {'task': [1]}
        self.calls = []

    def inventory(self, workflow):
        return copy.deepcopy(self.segments)

    def check(self, phase, workflow, scope, authority):
        self.calls.append(phase)
        return NO if self.denied == phase or self.denied == 'all' else YES


class _Observations:
    def __init__(self, clock):
        self.clock = clock
        self.values = {}
        self.denied = False
        self.causal = True
        self.calls = []

    def acquire(self, predicate, deadline):
        self.clock.sleep(1_000_000)
        key = predicate['capability_id']
        self.calls.append(key)
        script = self.values.get(key, [True])
        value = script.pop(0) if len(script) > 1 else script[0]
        if isinstance(value, w.NativeReadFailure):
            raise value
        if type(value) is w.ObservationValue:
            return value
        kind = {bool: 'boolean', int: 'integer', frozenset: 'set'}[type(value)]
        t = self.clock() if self.causal else 0
        return w.ObservationValue('scope', 'authority', str(len(self.calls)), t, t, key, 'v1', kind, 'count', value, True, None)

    def validate(self, value):
        return NO if self.denied else YES


class _Capabilities:
    def __init__(self, observations):
        self.observations = observations
        self.bad_target = False
        self.bad_scope = False
        self.no_focus = False
        self.no_satisfaction = False
        self.no_final = False
        self.no_equivalent = False
        self.no_support = False
        self.no_verifier = False
        self.targets = 0

    def supports(self, workflow):
        return NO if self.no_support else YES

    def target(self, target, state, deadline):
        self.targets += 1
        value = self.observations.acquire(_predicate('target'), deadline)
        if self.bad_target:
            return (10, 10)  # Recorded coordinates are deliberately insufficient.
        return w.GroundedTarget((10, 10), None, value, 'other' if self.bad_scope else 'scope', 'authority', YES, self, deadline, (0, 0))

    def validate_target(self, target, value, state):
        return YES

    def verifier(self, spec):
        return NO if self.no_verifier else YES

    def equivalent(self, a, b):
        return NO if self.no_equivalent else YES

    def focus(self, step, state):
        return NO if self.no_focus else YES

    def action(self, action, target):
        return YES

    def satisfaction(self, step, state):
        return NO if self.no_satisfaction else YES

    def final(self, workflow):
        return NO if self.no_final else YES

    def changed(self, predicate, before, after):
        return NO


class _Resources:
    def __init__(self):
        self.denied = None
        self.bad_cleanup = False
        self.reservations = []
        self.drains = 0

    def reserve(self, kind, amount):
        self.reservations.append((kind, amount))
        return NO if self.denied == kind else YES

    def cancel(self, timeout):
        return YES

    def drain(self, timeout):
        self.drains += 1
        return NO if self.bad_cleanup else YES


class _Journal:
    def __init__(self, root):
        self.root = root
        self.raw = b''
        self.denied = False
        self.bad_chain = False
        self.bad_stage = False
        self.forge_inventory = False
        self.fail_complete = False
        self.selected = 'old'
        self.quarantine = None
        self.inventory = None
        self.summary = None
        self.reservations = 0

    def read(self, budget):
        return self.raw

    def digest(self, raw, prev):
        # Synthetic owner's explicit framing; no production framing is assumed.
        return hashlib.sha256(b'MWP3_TEST_DOMAIN\x00' + raw).hexdigest()

    def authenticate(self, event):
        payload = {k: v for k, v in event.items() if k != 'digest'}
        return YES if not self.bad_chain and event['digest'] == self.digest(g.canon(payload).encode(), event['prev']) else NO

    def recover(self, prefix, suffix):
        self.quarantine = suffix
        return YES

    def reserve(self, amount, events):
        self.reservations += 1
        return NO if self.denied and self.reservations == 2 else YES

    def append(self, raw):
        if self.fail_complete and g.strict_load(raw)['kind'] == 'complete':
            return NO
        self.raw += raw
        return YES

    def stage(self, summary, inventory, output):
        self.summary, self.inventory = copy.deepcopy(summary), copy.deepcopy(inventory)
        path = self.root / 'inactive.json'
        path.write_text(g.canon(summary) + '\n')
        return path

    def context(self):
        def load(kind, value, related):
            view = copy.deepcopy(self.inventory)
            if self.forge_inventory:
                view['steps'][0]['predicate_pass'] = not view['steps'][0]['predicate_pass']
            return view

        def authenticate(kind, value, related):
            # Compare supplied summary/inventory with the owner's frozen records;
            # this is intentionally not an unconditional evidence PASS callback.
            return g.GateResult(0, 'PASS') if kind == 'run_inventory' and value == self.summary and related[0] == self.inventory else g.GateResult(5, 'FORGED')
        return g.ValidationContext(self.root, evidence_load=load, evidence_check=authenticate)

    def validate(self, staged, inventory):
        return NO if self.bad_stage else YES

    def select(self, staged):
        self.selected = str(staged)
        return YES


class _Transport:
    def __init__(self, clock):
        self.clock = clock
        self.sent = []
        self.explode = False
        self.stale = False

    def send(self, action, target):
        self.sent.append(action['kind'])
        if self.explode:
            raise OSError('synthetic private exception never logged')
        return 0 if self.stale else self.clock()

    def send_private(self, action, target, payload):
        self.sent.append('private_pipe')
        return self.clock()


class _Slots:
    def __init__(self):
        self.denied = False
        self.accepted = True
        self.clean = True
        self.cleared_count = 0
        self.transport = 'private_pipe'
        self.value = 'synthetic input'
        self.initial_length = 0

    def admit(self, slot, state):
        return NO if self.denied else YES

    def read(self, slot, state, max_bytes):
        return w.PrivateSlot(self.value, self.initial_length, 'scope', 'authority', self.transport, YES)

    def prepare(self, slot, payload, state):
        return YES

    def verify(self, slot, state, length, initial):
        return YES if self.accepted else NO

    def clear(self, slot):
        self.cleared_count += 1
        return YES

    def cleared(self, slot):
        return YES if self.clean else NO


def _predicate(name='synthetic-v1', comparison='truth', snapshot=None):
    return dict(_pred(), capability_id=name, comparison=comparison, snapshot_ref=snapshot)


def _target():
    target = _fixture('t1')
    target.update(anchor_ref='anchor', required_anchor_refs=['anchor'], frame_kind='element',
                  consistency_tolerance_px=0, offset_dx_px=0, offset_dy_px=0, relative_xy_bps=None)
    return target


# @anchor:wa.tests.mw_run_unit.TestWorkflow
class TestWorkflow(unittest.TestCase):
    # @anchor:wa.tests.mw_run_unit.TestWorkflow.setUp
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.clock = _Clock()
        self.artifact = _Artifact()
        self.authorization = _Authorization()
        self.observations = _Observations(self.clock)
        self.capabilities = _Capabilities(self.observations)
        self.resources = _Resources()
        self.journal = _Journal(self.root)
        self.transport = _Transport(self.clock)
        self.surface = object()
        self.dispatch = LegacyDispatchOwner(self.surface)
        self.limits = w.WorkflowLimits(65536, 16, 8, 1000, 8192, 4096, 1024, 100, 100000, 4096, 8192,
                                       1000000, 1000000, 100000000, 1000000000)
        self.path = self.root / 'flow.json'
        self.workflow = _fixture('workflow')
        self.workflow.update(id='flow', site_id='site', steps=[_step()], status='draft', baseline_id=None,
                             text_slots=[], script_ref=None, script_sha256=None)
        self.workflow['stats'] = {k: None for k in self.workflow['stats']}
        self.workflow['oracle'].update(postconditions=[_predicate('final')], planted_mutant_ids=[], mutants_total=0, mutants_killed=0)
        self.workflow['evaluation_profile']['kind'] = 'initial_task_v1'
        self.workflow['evaluation_profile']['baseline_workflow_sha256'] = None
        self.workflow['provenance'].update(source='human', source_ref='EXPLANATION.md', quote_sha256=None)
        (self.root / 'EXPLANATION.md').write_bytes(_explanation())
        self.ctx = g.ValidationContext(self.root, capability_check=lambda *a: g.GateResult(0, 'PASS'),
                  evidence_check=lambda *a: g.GateResult(0, 'PASS'), review_check=lambda *a: g.GateResult(0, 'PASS'))
        self.owners = w.RunOwners(self.artifact, self.ctx, self.authorization, self.capabilities, self.observations,
                     None, self.transport, None, self.resources, self.journal, self.dispatch, self.surface, None,
                     self.clock, self.clock.sleep, self.limits, 'scope', 'authority', 'run', 'replay', self.root / 'result.json')

    def _write(self, raw=None):
        self.path.write_bytes(raw if raw is not None else (g.canon(self.workflow) + '\n').encode())

    def _run(self, raw=None):
        self._write(raw)
        return w.run_workflow(self.path, bundle=BUNDLE, owners=self.owners, limits=self.limits)

    def _click(self):
        self.workflow['steps'][0]['action']['kind'] = 'click'
        self.workflow['steps'][0]['target'] = _target()
        self.workflow['steps'][0]['locate_wait_ms'] = 100

    def _reject(self, result, code=None, sends=0):
        self.assertNotEqual(result.rc, 0, result)
        if code:
            self.assertEqual(result.verdict_code, code, result)
        self.assertEqual(len(self.transport.sent), sends)
        if result.summary is None and self.journal.selected != 'old':
            self.assertNotEqual(self.journal.summary['rc'], 0)
        elif result.summary is None:
            self.assertEqual(self.journal.selected, 'old')
        else:
            self.assertEqual(result.summary['rc'], result.rc)
            self.assertEqual(sum(result.summary[k] for k in ('steps_verified', 'steps_skipped_satisfied',
                             'steps_skipped_condition', 'steps_failed', 'steps_not_reached')), result.summary['steps_total'])

    # @anchor:wa.tests.mw_run_unit.TestWorkflow.test_nominal_real_gate
    def test_nominal_real_gate(self):
        result = self._run()
        self.assertEqual(result.rc, 0, result)
        self.assertEqual(result.summary['steps_verified'], 1)
        self.assertEqual(result.summary['reproduced_ppm'], 1000000)
        self.assertEqual(result.summary['visibility'], 'local')
        self.assertIsNone(result.summary['evaluation'])
        self.assertEqual(set(result.summary), set(BUNDLE['$defs']['run']['required']))
        self.assertNotEqual(self.journal.selected, 'old')
        self.assertTrue(self.journal.raw.endswith(b'\n'))
        self.assertGreater(self.resources.drains, 0)

    # @anchor:wa.tests.mw_run_unit.TestWorkflow.test_strict_schema
    def test_strict_schema(self):
        self.assertEqual(self._run().rc, 0)
        self.journal.selected = 'old'
        self.journal.raw = b''
        self._click()
        raw = (g.canon(self.workflow) + '\n').encode()
        for bad in (b'{"id":"flow",' + raw[1:], raw.replace(b'"seq":1', b'"seq":1.0'),
                    raw.replace(b'"seq":1', b'"seq":NaN'), b'\xff',
                    raw.replace(b'"seq":1', b'"seq":true'), raw.replace(b'"id":"flow"', b'"id":"flow\\n"'),
                    raw[:-2] + b',"extra":1}\n'):
            with self.subTest(raw=bad[:30]):
                self._reject(self._run(bad))

    # @anchor:wa.tests.mw_run_unit.TestWorkflow.test_canonical
    def test_canonical(self):
        self._click()
        self._reject(self._run((g.canon(self.workflow) + ' \n').encode()), 'USAGE_SCHEMA')
        self.assertEqual(self._run().rc, 0)

    # @anchor:wa.tests.mw_run_unit.TestWorkflow.test_artifact_bound
    def test_artifact_bound(self):
        raw = (g.canon(self.workflow) + '\n').encode()
        # Lower explicit host bound discriminates our check from upstream's cap.
        self.limits = replace(self.limits, workflow_bytes=len(raw) - 1)
        self.owners = replace(self.owners, limits=self.limits)
        self._reject(self._run(), 'USAGE_SCHEMA')
        self.limits = replace(self.limits, workflow_bytes=65536)
        self.owners = replace(self.owners, limits=self.limits)
        self._reject(self._run(b' ' * 65537), 'USAGE_SCHEMA')
        self.assertEqual(self._run().rc, 0)

    # @anchor:wa.tests.mw_run_unit.TestWorkflow.test_pin
    def test_pin(self):
        self.artifact.pin_override = '0' * 64
        self._reject(self._run(), 'SEAL_MISMATCH')
        self.artifact.pin_override = None
        self.assertEqual(self._run().rc, 0)

    # @anchor:wa.tests.mw_run_unit.TestWorkflow.test_immutable
    def test_immutable(self):
        self.artifact.immutable = NO
        self._reject(self._run(), 'SEAL_MISMATCH')
        self.artifact.immutable = YES
        self.assertEqual(self._run().rc, 0)

    # @anchor:wa.tests.mw_run_unit.TestWorkflow.test_callbacks
    def test_callbacks(self):
        self._write()
        self.assertNotEqual(w.run_workflow(self.path, bundle=BUNDLE, owners=None, limits=self.limits).rc, 0)
        with patch.object(w.gate, 'validate_file', return_value=True):
            self._reject(self._run())
        self.assertEqual(self._run().rc, 0)

    # @anchor:wa.tests.mw_run_unit.TestWorkflow.test_authorization
    def test_authorization(self):
        self._click()
        self.authorization.denied = 'admission'
        self._reject(self._run(), 'OUT_OF_SUPPORT')
        self.authorization.denied = None
        self.assertEqual(self._run().rc, 0)

    # @anchor:wa.tests.mw_run_unit.TestWorkflow.test_send_fence
    def test_send_fence(self):
        self._click()
        self.authorization.denied = 'send'
        self._reject(self._run(), 'OUT_OF_SUPPORT')
        self.assertIn('step', self.authorization.calls)
        self.assertIn('send', self.authorization.calls)
        self.assertNotEqual(self.journal.raw, b'')

    # @anchor:wa.tests.mw_run_unit.TestWorkflow.test_resource_reservation
    def test_resource_reservation(self):
        self._click()
        self.journal.denied = True
        self._reject(self._run(), 'OUT_OF_SUPPORT')
        self.assertNotIn(b'"kind":"intent"', self.journal.raw)
        self.journal.raw = b''
        self.journal.denied = False
        self.assertEqual(self._run().rc, 0)

    # @anchor:wa.tests.mw_run_unit.TestWorkflow.test_peak_reservation
    def test_peak_reservation(self):
        self.resources.denied = 'terminal_evidence'
        self._reject(self._run(), 'OUT_OF_SUPPORT')
        self.resources.denied = None
        self.assertEqual(self._run().rc, 0)

    def _observation(self, value=True, **kwargs):
        defaults = dict(scope='scope', authority='authority', publication='test', capture_start_ns=1, capture_end_ns=1,
                        capability_id='synthetic-v1', capability_revision='v1', output_kind='boolean', unit='count',
                        value=value, complete=True, missing_reason=None)
        defaults.update(kwargs)
        return w.ObservationValue(**defaults)

    # @anchor:wa.tests.mw_run_unit.TestWorkflow.test_missingness
    def test_missingness(self):
        for value in (False, 0, frozenset()):
            obs = self._observation(value, complete=False, missing_reason='MISSING')
            self.assertIs(w.evaluate_predicate(_predicate(), observation=obs, snapshots={}, owners=self.owners), Truth.UNKNOWN)
        self.assertIs(w.evaluate_predicate(_predicate(), observation=self._observation(), snapshots={}, owners=self.owners), Truth.TRUE)

    # @anchor:wa.tests.mw_run_unit.TestWorkflow.test_observation_scope
    def test_observation_scope(self):
        for kwargs in ({'scope': 'other-account'}, {'authority': 'changed-policy'}):
            self.assertIs(w.evaluate_predicate(_predicate(), observation=self._observation(**kwargs), snapshots={}, owners=self.owners), Truth.UNKNOWN)
        self.assertIs(w.evaluate_predicate(_predicate(), observation=self._observation(), snapshots={}, owners=self.owners), Truth.TRUE)

    # @anchor:wa.tests.mw_run_unit.TestWorkflow.test_snapshot_units_and_bool
    def test_snapshot_units_and_bool(self):
        p = _predicate(comparison='delta_gt', snapshot='base')
        old = self._observation(0, output_kind='integer')
        snapshots = {'base': w.CapturedSnapshot('base', old, 1)}
        good = self._observation(1, output_kind='integer', capture_start_ns=2, capture_end_ns=2)
        self.assertIs(w.evaluate_predicate(p, observation=good, snapshots=snapshots, owners=self.owners), Truth.TRUE)
        self.assertIs(w.evaluate_predicate(p, observation=replace(good, value=0), snapshots=snapshots, owners=self.owners), Truth.FALSE)
        for bad in (replace(good, unit='different'), replace(good, value=True)):
            self.assertIs(w.evaluate_predicate(p, observation=bad, snapshots=snapshots, owners=self.owners), Truth.UNKNOWN)

    # @anchor:wa.tests.mw_run_unit.TestWorkflow.test_duplicate_download
    def test_duplicate_download(self):
        step = self.workflow['steps'][0]
        step['capture_before'] = [{'name': 'downloads', 'observe': _predicate('downloads')}]
        step['verify']['predicate'] = _predicate('downloads', 'set_grew', 'downloads')
        self.observations.values['downloads'] = [frozenset({'old'})]
        self._reject(self._run(), 'DEADLINE')
        self.clock.now = 1000000
        self.journal.raw = b''
        self.observations.values['downloads'] = [frozenset({'old'}), frozenset({'old', 'new'})]
        self.assertEqual(self._run().rc, 0)
        self.assertEqual(self.observations.calls.count('downloads') > 2, True)

    # @anchor:wa.tests.mw_run_unit.TestWorkflow.test_quiet_incomplete
    def test_quiet_incomplete(self):
        self.observations.values['synthetic-v1'] = [False]
        self._reject(self._run(), 'DEADLINE')
        self.clock.now = 1000000
        self.observations.values['synthetic-v1'] = [True]
        self.assertEqual(self._run().rc, 0)

    # @anchor:wa.tests.mw_run_unit.TestWorkflow.test_busy_ordering
    def test_busy_ordering(self):
        self.workflow['steps'][0]['verify'].update(busy_predicate=_predicate('busy'), consecutive_polls=1)
        for script in ([False], [False, True, True]):
            self.clock.now = 1000000
            self.journal.raw = b''
            self.observations.values['busy'] = list(script)
            self._reject(self._run(), 'DEADLINE')
        self.clock.now = 1000000
        self.journal.raw = b''
        self.observations.calls.clear()
        self.observations.values['busy'] = [False, True, False]
        self.assertEqual(self._run().rc, 0)
        self.assertEqual(self.observations.calls.count('busy'), 3)
        self.assertEqual(self.observations.values['busy'], [False])

    # @anchor:wa.tests.mw_run_unit.TestWorkflow.test_stuck_spinner
    def test_stuck_spinner(self):
        self.workflow['steps'][0]['verify'].update(busy_predicate=_predicate('busy'), deadline_ms=900)
        self.observations.values['busy'] = [True]
        self.observations.values['synthetic-v1'] = [False]
        self._reject(self._run(), 'STUCK')
        self.assertLess(self.clock.now, 900000000)
        self.clock.now = 1000000
        self.observations.values['busy'] = [True, False]
        self.observations.values['synthetic-v1'] = [False, True]
        self.assertEqual(self._run().rc, 0)

    # @anchor:wa.tests.mw_run_unit.TestWorkflow.test_read_retry
    def test_read_retry(self):
        self.workflow['steps'][0]['verify']['deadline_ms'] = 950
        self.limits = replace(self.limits, no_progress_ns=1000000000)
        self.owners = replace(self.owners, limits=self.limits)
        failure = w.NativeReadFailure('producer', 'read', 4, 'render')
        self.observations.values['synthetic-v1'] = [failure, failure, failure, failure, True]
        self._reject(self._run(), 'OUT_OF_SUPPORT')
        self.assertEqual(self.observations.calls.count('synthetic-v1'), 4)
        self.clock.now = 1000000
        self.observations.calls.clear()
        self.observations.values['synthetic-v1'] = [failure, failure, failure, True]
        self.assertEqual(self._run().rc, 0)
        self.assertGreaterEqual(self.clock(), 850000000)
        self.assertEqual(self.transport.sent, [])

    # @anchor:wa.tests.mw_run_unit.TestWorkflow.test_native_unknown
    def test_native_unknown(self):
        self.observations.values['synthetic-v1'] = [w.NativeReadFailure('producer', 'read', 4, 'unknown')]
        self._reject(self._run(), 'OUT_OF_SUPPORT')
        self.assertEqual(len(self.observations.calls), 1)
        self.clock.now = 1000000
        self.observations.values['synthetic-v1'] = [True]
        self.assertEqual(self._run().rc, 0)

    # @anchor:wa.tests.mw_run_unit.TestWorkflow.test_native_translation
    def test_native_translation(self):
        cases = [(w.NativeReadFailure('vision-engine', 'observe', 2, 'usage'), 2, 'USAGE_SCHEMA'),
                 (w.NativeReadFailure('vision-engine', 'observe', 4, 'stale'), 3, 'SEAL_MISMATCH'),
                 (w.NativeReadFailure('vision-engine', 'observe', 0, 'OK'), 5, 'OUT_OF_SUPPORT'),
                 (w.NativeReadFailure('producer', 'read', 99, 'unknown', True), 4, 'HUMAN_HANDOFF')]
        for failure, rc, verdict in cases:
            self.clock.now = 1000000
            self.observations.values['synthetic-v1'] = [failure]
            result = self._run()
            self._reject(result, verdict)
            self.assertEqual(result.rc, rc)
        self.clock.now = 1000000
        self.observations.values['synthetic-v1'] = [True]
        self.assertEqual(self._run().rc, 0)

    # @anchor:wa.tests.mw_run_unit.TestWorkflow.test_satisfaction
    def test_satisfaction(self):
        self._click()
        self.workflow['steps'][0]['already_satisfied'] = _predicate('satisfied')
        self.capabilities.no_satisfaction = True
        self._reject(self._run(), 'LOW_CONFIDENCE')
        self.capabilities.no_satisfaction = False
        result = self._run()
        self.assertEqual(result.rc, 0)
        self.assertEqual(result.summary['steps_skipped_satisfied'], 1)
        self.assertEqual(self.transport.sent, [])

    # @anchor:wa.tests.mw_run_unit.TestWorkflow.test_required_preconditions
    def test_required_preconditions(self):
        self._click()
        for container in (self.workflow, self.workflow['steps'][0]):
            container['preconditions'] = [_predicate('required')]
            for value in (False, self._observation(capability_id='required', complete=False)):
                self.observations.values['required'] = [value]
                self._reject(self._run(), 'LOW_CONFIDENCE')
                self.journal.raw = b''
                self.journal.selected = 'old'
            container['preconditions'] = []
        self.assertEqual(self._run().rc, 0)
        self.assertEqual(self.transport.sent, ['click'])

    # @anchor:wa.tests.mw_run_unit.TestWorkflow.test_unknown_satisfaction
    def test_unknown_satisfaction(self):
        self._click()
        self.workflow['steps'][0]['already_satisfied'] = _predicate('satisfied')
        self.observations.values['satisfied'] = [self._observation(capability_id='satisfied', complete=False)]
        self._reject(self._run(), 'LOW_CONFIDENCE')
        self.journal.raw = b''
        self.journal.selected = 'old'
        self.observations.values['satisfied'] = [True]
        result = self._run()
        self.assertEqual(result.rc, 0)
        self.assertEqual(result.summary['steps_skipped_satisfied'], 1)
        self.assertEqual(self.transport.sent, [])

    # @anchor:wa.tests.mw_run_unit.TestWorkflow.test_optional_cleanup
    def test_optional_cleanup(self):
        self.workflow['steps'][0]['when'] = _predicate('guard')
        self.observations.values['guard'] = [False]
        self._reject(self._run(), 'OUT_OF_SUPPORT')  # Required-segment skip.
        self.authorization.segments = {}
        result = self._run()
        self.assertEqual(result.rc, 0)
        self.assertEqual(result.summary['steps_skipped_condition'], 1)
        self.assertIsNone(result.summary['reproduced_ppm'])

    # @anchor:wa.tests.mw_run_unit.TestWorkflow.test_unknown_guard
    def test_unknown_guard(self):
        self.workflow['steps'][0]['when'] = _predicate('guard')
        self.authorization.segments = {}
        self.observations.values['guard'] = [self._observation(False, capability_id='guard', complete=False)]
        self._reject(self._run(), 'LOW_CONFIDENCE')
        self.observations.values['guard'] = [False]
        self.assertEqual(self._run().rc, 0)

    # @anchor:wa.tests.mw_run_unit.TestWorkflow.test_repeat
    def test_repeat(self):
        self.workflow['steps'][0]['repeat'] = {'while_predicate': _predicate('while'), 'max_iterations': 1}
        self.observations.values['while'] = [True, True, False]
        self._reject(self._run(), 'STUCK')
        self.journal.raw = b''
        self.observations.values['while'] = [True, False]
        result = self._run()
        self.assertEqual(result.rc, 0)
        self.assertEqual(result.summary['steps_total'], 1)
        self.assertEqual(result.summary['steps_verified'], 1)

    # @anchor:wa.tests.mw_run_unit.TestWorkflow.test_focus
    def test_focus(self):
        step = self.workflow['steps'][0]
        step['action'].update(kind='key', key_name='Return')
        step['preconditions'] = [_predicate('focus')]
        self.capabilities.no_focus = True
        self._reject(self._run(), 'OUT_OF_SUPPORT')
        self.capabilities.no_focus = False
        self.assertEqual(self._run().rc, 0)

    # @anchor:wa.tests.mw_run_unit.TestWorkflow.test_target_raw_point
    def test_target_raw_point(self):
        self._click()
        self.capabilities.bad_target = True
        self._reject(self._run(), 'DEADLINE')
        self.capabilities.bad_target = False
        self.assertEqual(self._run().rc, 0)
        self.assertEqual(self.transport.sent, ['click'])

    # @anchor:wa.tests.mw_run_unit.TestWorkflow.test_target_scope
    def test_target_scope(self):
        self._click()
        self.capabilities.bad_scope = True
        self._reject(self._run(), 'DEADLINE')
        self.capabilities.bad_scope = False
        self.assertEqual(self._run().rc, 0)

    # @anchor:wa.tests.mw_run_unit.TestWorkflow.test_t2_locale
    def test_t2_locale(self):
        self._click()
        target = _fixture('t2')
        target['rel_box_bps'] = [0, 0, 100, 100]
        self.workflow['steps'][0]['target'] = target
        self._reject(self._run(), 'OUT_OF_SUPPORT')
        target['locale'] = 'en'
        self.assertEqual(self._run().rc, 0)

    # @anchor:wa.tests.mw_run_unit.TestWorkflow.test_action_shape
    def test_action_shape(self):
        self._click()
        self.workflow['steps'][0]['target'] = _fixture('t3')
        self._reject(self._run())
        self._click()
        self.workflow['steps'][0]['action']['kind'] = 'drag'
        self._reject(self._run())
        self.workflow['steps'][0]['action'].update(dx_px=1, dy_px=1, destination=_target())
        self._reject(self._run())
        self.workflow['steps'][0]['action']['destination'] = None
        self.assertEqual(self._run().rc, 0)

    # @anchor:wa.tests.mw_run_unit.TestWorkflow.test_scroll_contract
    def test_scroll_contract(self):
        self.workflow['steps'][0]['action'].update(kind='scroll', dx_px=17, dy_px=23)
        self.workflow['steps'][0]['locate_wait_ms'] = 100
        self.capabilities.action = lambda *args: NO  # No installed mapping for these displacement semantics.
        self._reject(self._run(), 'OUT_OF_SUPPORT')
        self.journal.raw = b''
        self.journal.selected = 'old'
        self.capabilities.action = lambda *args: YES
        self.assertEqual(self._run().rc, 0)
        self.assertEqual(self.transport.sent, ['scroll'])

    # @anchor:wa.tests.mw_run_unit.TestWorkflow.test_click_count_contract
    def test_click_count_contract(self):
        self._click()
        for kind in ('right_click', 'double_click'):
            self.workflow['steps'][0]['action']['kind'] = kind
            self.capabilities.action = lambda *args: NO
            self._reject(self._run(), 'OUT_OF_SUPPORT')
            self.journal.raw = b''
            self.journal.selected = 'old'
            self.capabilities.action = lambda *args: YES
            self.assertEqual(self._run().rc, 0)
            self.assertEqual(self.transport.sent, [kind])
            self.transport.sent.clear()
            self.journal.raw = b''
            self.journal.selected = 'old'

    # @anchor:wa.tests.mw_run_unit.TestWorkflow.test_locate_appearance
    def test_locate_appearance(self):
        self._click()
        target = self.capabilities.target
        attempts = []
        def appearing(spec, state, deadline):
            attempts.append(deadline)
            value = target(spec, state, deadline)
            return value if len(attempts) >= 3 else None
        self.capabilities.bad_target = True
        self._reject(self._run(), 'DEADLINE')
        self.clock.now = 1000000
        self.journal.raw = b''
        self.capabilities.bad_target = False
        self.capabilities.target = appearing
        self.assertEqual(self._run().rc, 0)
        self.assertEqual(len(attempts), 3)
        self.assertEqual(len(set(attempts)), 1)
        self.assertEqual(self.transport.sent, ['click'])

    # @anchor:wa.tests.mw_run_unit.TestWorkflow.test_fallback
    def test_fallback(self):
        spec = self.workflow['steps'][0]['verify']
        child = copy.deepcopy(spec)
        spec.update(mode='edge_ncc', ram_evidence_ref='registered-primary')
        spec['fallback_verifiers'] = [child]
        self.capabilities.observe_verifier = lambda candidate, state, deadline: self._observation(False)
        self.observations.values['synthetic-v1'] = [True]
        self.capabilities.no_equivalent = True
        self._reject(self._run(), 'OUT_OF_SUPPORT')
        self.journal.raw = b''
        self.journal.selected = 'old'
        self.capabilities.no_equivalent = False
        self.assertEqual(self._run().rc, 0)
        self.assertIn('synthetic-v1', self.observations.calls)
        self.assertLess(self.clock(), 101000000)

    # @anchor:wa.tests.mw_run_unit.TestWorkflow.test_causal
    def test_causal(self):
        self._click()
        original_acquire = self.observations.acquire
        def stale_completion(predicate, deadline):
            value = original_acquire(predicate, deadline)
            return value if predicate['capability_id'] == 'target' else replace(value, capture_start_ns=0, capture_end_ns=0)
        self.observations.acquire = stale_completion
        self._reject(self._run(), 'DEADLINE', sends=1)
        # Owner-validated current publications pass on a fresh actor/run.
        self.clock.now = 1000000
        self.journal.raw = b''
        self.transport.sent.clear()
        self.observations.acquire = original_acquire
        self.assertEqual(self._run().rc, 0)

    # @anchor:wa.tests.mw_run_unit.TestWorkflow.test_input_uncertain
    def test_input_uncertain(self):
        self._click()
        second = copy.deepcopy(self.workflow['steps'][0])
        second['seq'] = 2
        self.workflow['steps'].append(second)
        self.transport.explode = True
        result = self._run()
        self._reject(result, 'INPUT_UNCERTAIN', sends=1)
        self.assertFalse(self.dispatch.check_ready())
        self.assertNotIn('private', str(result))

    # @anchor:wa.tests.mw_run_unit.TestWorkflow.test_input_timestamp
    def test_input_timestamp(self):
        self._click()
        self.transport.stale = True
        self._reject(self._run(), 'INPUT_UNCERTAIN', sends=1)
        self.journal.raw = b''
        self.transport.sent.clear()
        self.transport.stale = False
        self.dispatch = LegacyDispatchOwner(self.surface)
        self.owners = replace(self.owners, dispatch=self.dispatch)
        self.assertEqual(self._run().rc, 0)

    # @anchor:wa.tests.mw_run_unit.TestWorkflow.test_append_after_send
    def test_append_after_send(self):
        self._click()
        self.journal.fail_complete = True
        self._reject(self._run(), 'INPUT_UNCERTAIN', sends=1)
        self.assertFalse(self.dispatch.check_ready())

    # @anchor:wa.tests.mw_run_unit.TestWorkflow.test_intent_append
    def test_intent_append(self):
        self._click()
        append = self.journal.append
        self.journal.append = lambda raw: NO
        self._reject(self._run(), 'TORN')
        self.assertEqual(self.journal.raw, b'')
        self.journal.append = append
        self.owners = replace(self.owners, dispatch=LegacyDispatchOwner(self.surface))
        self.assertEqual(self._run().rc, 0)
        self.assertEqual(self.transport.sent, ['click'])

    # @anchor:wa.tests.mw_run_unit.TestWorkflow.test_cleanup
    def test_cleanup(self):
        self.resources.bad_cleanup = True
        self._reject(self._run(), 'OUT_OF_SUPPORT')
        self.resources.bad_cleanup = False
        self.journal.raw = b''
        self.assertEqual(self._run().rc, 0)

    def _journal_event(self, kind='intent'):
        event = dict(seq=1, prev=None, kind=kind, step_seq=1, phase='delivery' if kind in ('intent', 'complete') else 'terminal')
        event['digest'] = self.journal.digest(g.canon(event).encode(), None)
        return (g.canon(event) + '\n').encode()

    # @anchor:wa.tests.mw_run_unit.TestWorkflow.test_restart
    def test_restart(self):
        self.journal.raw = self._journal_event()
        self._reject(self._run(), 'INPUT_UNCERTAIN')
        self.journal.raw = b''
        self.assertEqual(self._run().rc, 0)

    # @anchor:wa.tests.mw_run_unit.TestWorkflow.test_torn
    def test_torn(self):
        self.journal.raw = self._journal_event('verified')[:-1]
        self._reject(self._run(), 'TORN')
        self.assertIsNotNone(self.journal.quarantine)
        self.journal.raw = b''
        self.assertEqual(self._run().rc, 0)

    # @anchor:wa.tests.mw_run_unit.TestWorkflow.test_chain
    def test_chain(self):
        self.journal.raw = self._journal_event('verified')
        self.journal.bad_chain = True
        self._reject(self._run(), 'TORN')
        self.journal.bad_chain = False
        self._reject(self._run(), 'OUT_OF_SUPPORT')

    # @anchor:wa.tests.mw_run_unit.TestWorkflow.test_journal_shape
    def test_journal_shape(self):
        for changes in ({'step_seq': 99}, {'step_seq': True}, {'phase': 'delivery'}):
            event = g.strict_load(self._journal_event('verified'))
            event.update(changes)
            del event['digest']
            event['digest'] = self.journal.digest(g.canon(event).encode(), event['prev'])
            self.journal.raw = (g.canon(event) + '\n').encode()
            self._reject(self._run(), 'TORN')
        self.journal.raw = self._journal_event('verified')
        self._reject(self._run(), 'OUT_OF_SUPPORT')

    # @anchor:wa.tests.mw_run_unit.TestWorkflow.test_final
    def test_final(self):
        self.capabilities.no_final = True
        self._reject(self._run(), 'CHANGE_MISMATCH')
        self.capabilities.no_final = False
        self.journal.raw = b''
        self.assertEqual(self._run().rc, 0)

    # @anchor:wa.tests.mw_run_unit.TestWorkflow.test_final_predicate
    def test_final_predicate(self):
        self.observations.values['final'] = [False]
        self._reject(self._run(), 'CHANGE_MISMATCH')
        self.observations.values['final'] = [True]
        self.journal.raw = b''
        self.assertEqual(self._run().rc, 0)

    # @anchor:wa.tests.mw_run_unit.TestWorkflow.test_forged_inventory
    def test_forged_inventory(self):
        self.journal.forge_inventory = True
        self._reject(self._run(), 'TORN')
        self.journal.forge_inventory = False
        self.journal.raw = b''
        self.assertEqual(self._run().rc, 0)

    # @anchor:wa.tests.mw_run_unit.TestWorkflow.test_atomic_selection
    def test_atomic_selection(self):
        self.journal.bad_stage = True
        self._reject(self._run(), 'TORN')
        self.assertEqual(self.journal.selected, 'old')
        self.journal.bad_stage = False
        self.journal.raw = b''
        self.assertEqual(self._run().rc, 0)

    # @anchor:wa.tests.mw_run_unit.TestWorkflow.test_atomic_crash
    def test_atomic_crash(self):
        stage = self.journal.stage
        def interrupted(summary, inventory, output):
            stage(summary, inventory, output)
            raise OSError('synthetic metadata interruption')
        self.journal.stage = interrupted
        self._reject(self._run(), 'OUT_OF_SUPPORT')
        self.assertEqual(self.journal.selected, 'old')
        self.assertTrue((self.root / 'inactive.json').exists())
        self.journal.stage = stage
        self.journal.raw = b''
        self.assertEqual(self._run().rc, 0)
        self.assertNotEqual(self.journal.selected, 'old')

    # @anchor:wa.tests.mw_run_unit.TestWorkflow.test_slots_refused_before_input
    def test_slots_refused_before_input(self):
        self.workflow['text_slots'] = [_fixture('slot')]
        self._reject(self._run(), 'OUT_OF_SUPPORT')
        self.workflow['text_slots'] = []
        self.assertEqual(self._run().rc, 0)

    def _paste(self):
        slot = _fixture('slot')
        slot.update(slot_id='prompt', max_length=100, sensitivity='private_runtime_only', transport='clipboard_paste',
                    empty_len_allowed=[0, 1], post_type_len_allowed=['exact_n'], clear_first=True, budget_ms=50)
        self.workflow['text_slots'] = [slot]
        step = self.workflow['steps'][0]
        step['preconditions'] = [_predicate('focus')]
        step['action'].update(kind='paste', text_slot='prompt')
        self.slots = _Slots()
        self.owners = replace(self.owners, slots=self.slots,
                              slot_length_bindings=(w._SlotLengthBinding('prompt', _predicate('length'), 'v1', 'count'),))
        self.observations.values['length'] = [len(self.slots.value)]

    # @anchor:wa.tests.mw_run_unit.TestWorkflow.test_private_transport
    def test_private_transport(self):
        self._paste()
        self.slots.denied = True  # No clipboard-history/clear or chip support.
        self._reject(self._run(), 'OUT_OF_SUPPORT')
        self.slots.denied = False
        result = self._run()
        self.assertEqual(result.rc, 0, result)
        self.assertEqual(self.transport.sent, ['private_pipe'])
        for path in self.root.rglob('*'):
            if path.is_file():
                self.assertNotIn(self.slots.value.encode(), path.read_bytes())
        self.assertNotIn(self.slots.value.encode(), self.journal.raw)
        self.assertNotIn(self.slots.value, repr(result))
        self.assertNotIn(self.slots.value, repr(self.slots.read(None, None, 4096)))

    # @anchor:wa.tests.mw_run_unit.TestWorkflow.test_altered_prompt
    def test_altered_prompt(self):
        self._paste()
        self.slots.accepted = False  # Matching length/pixels never attests content.
        self._reject(self._run(), 'CHANGE_MISMATCH', sends=1)
        self.transport.sent.clear()
        self.journal.raw = b''
        self.slots.accepted = True
        self.assertEqual(self._run().rc, 0)

    # @anchor:wa.tests.mw_run_unit.TestWorkflow.test_clipboard_cleanup
    def test_clipboard_cleanup(self):
        self._paste()
        self.slots.transport = 'owned_clipboard'
        self.slots.clean = False
        self._reject(self._run(), 'OUT_OF_SUPPORT', sends=1)
        self.assertEqual(self.slots.cleared_count, 1)
        self.transport.sent.clear()
        self.journal.raw = b''
        self.slots.clean = True
        self.dispatch = LegacyDispatchOwner(self.surface)
        self.owners = replace(self.owners, dispatch=self.dispatch)
        self.assertEqual(self._run().rc, 0)

    # @anchor:wa.tests.mw_run_unit.TestWorkflow.test_storage
    def test_storage(self):
        before = {p.relative_to(self.root) for p in self.root.rglob('*')}
        self.assertEqual(self._run().rc, 0)
        new = {p.relative_to(self.root) for p in self.root.rglob('*')} - before
        self.assertEqual(new, {Path('flow.json'), Path('inactive.json')})
        self.assertFalse(any(p.suffix in ('.png', '.npy', '.jpg') for p in self.root.rglob('*')))

    # @anchor:wa.tests.mw_run_unit.TestWorkflow.test_inert_script
    def test_inert_script(self):
        folder = self.root / 'scripts'
        folder.mkdir()
        caps = _fixture('caps')
        source = ('# MW-CAPS: ' + g.canon(caps) + '\nfrom pathlib import Path\nPath(' + repr(str(self.root / 'sentinel')) + ').write_text("IMPORTED")\n').encode()
        (folder / 'inert.py').write_bytes(source)
        self.workflow.update(script_ref='scripts/inert.py', script_sha256=hashlib.sha256(source).hexdigest())
        self.assertEqual(self._run().rc, 0)
        self.assertFalse((self.root / 'sentinel').exists())
        self.workflow['script_sha256'] = '0' * 64
        self.journal.selected = 'old'
        self._reject(self._run())

    # @anchor:wa.tests.mw_run_unit.TestWorkflow.test_snapshot_discipline
    def test_snapshot_discipline(self):
        step = self.workflow['steps'][0]
        capture = {'name': 'base', 'observe': _predicate('count')}
        step['capture_before'] = [capture, capture]
        self._reject(self._run())
        step['capture_before'] = []
        step['verify']['predicate'] = _predicate('count', 'delta_gt', 'base')
        self._reject(self._run())
        step['capture_before'] = [capture]
        self.observations.values['count'] = [1, 2]
        self.assertEqual(self._run().rc, 0)

    # @anchor:wa.tests.mw_run_unit.TestWorkflow.test_symlink
    def test_symlink(self):
        self._write()
        linked = self.root / 'linked.json'
        linked.symlink_to(self.path)
        self._reject(w.run_workflow(linked, bundle=BUNDLE, owners=self.owners, limits=self.limits), 'SEAL_MISMATCH')
        self.assertEqual(self._run().rc, 0)

    # @anchor:wa.tests.mw_run_unit.TestWorkflow.test_identity_adapter
    def test_identity_adapter(self):
        self._click()
        calls = []
        focus = [False]

        def native(operation, payload):
            calls.append((operation, payload))
            return dict(focus_verified=focus[0], input_mono_ns=self.clock())
        surface = SrfIdentitySurface(identity='synthetic', container='synthetic', window='w1', transport=native, ram_dir='/dev/shm')

        def plan(action, target):
            return w.IdentityPlan('click', {'point': ','.join(map(str, target.point)), 'button': 1}, YES)
        self.owners = replace(self.owners, surface=surface, dispatch=LegacyDispatchOwner(surface), transport=w.IdentityTransport(surface, plan))
        self._reject(self._run(), 'OUT_OF_SUPPORT')
        self.assertEqual(calls, [])
        self.workflow['steps'][0]['action']['path'] = 'coordinates'
        self._reject(self._run(), 'INPUT_UNCERTAIN')
        self.assertEqual(len(calls), 1)
        self.journal.raw = b''
        self.owners = replace(self.owners, dispatch=LegacyDispatchOwner(surface))
        focus[0] = True
        self.assertEqual(self._run().rc, 0)
        self.assertEqual(len(calls), 2)

    # @anchor:wa.tests.mw_run_unit.TestWorkflow.test_local_journal_capacity
    def test_local_journal_capacity(self):
        self._click()
        self.limits = replace(self.limits, event_count=2)
        self.owners = replace(self.owners, limits=self.limits)
        self._reject(self._run(), 'OUT_OF_SUPPORT')
        self.limits = replace(self.limits, event_count=100)
        self.owners = replace(self.owners, limits=self.limits)
        self.assertEqual(self._run().rc, 0)

    # @anchor:wa.tests.mw_run_unit.TestWorkflow.test_acquisition_bound
    def test_acquisition_bound(self):
        self.limits = replace(self.limits, acquisitions=1)
        self.owners = replace(self.owners, limits=self.limits)
        self._reject(self._run(), 'OUT_OF_SUPPORT')
        self.limits = replace(self.limits, acquisitions=1000)
        self.owners = replace(self.owners, limits=self.limits)
        self.assertEqual(self._run().rc, 0)

    # @anchor:wa.tests.mw_run_unit.TestWorkflow.test_snapshot_bound
    def test_snapshot_bound(self):
        step = self.workflow['steps'][0]
        step['capture_before'] = [{'name': 'a', 'observe': _predicate('count')}, {'name': 'b', 'observe': _predicate('count')}]
        self.observations.values['count'] = [1]
        self.limits = replace(self.limits, snapshots=1, retained_bytes=8192)
        self.owners = replace(self.owners, limits=self.limits)
        self._reject(self._run(), 'OUT_OF_SUPPORT')
        self.limits = replace(self.limits, snapshots=2)
        self.owners = replace(self.owners, limits=self.limits)
        self.assertEqual(self._run().rc, 0)

    # @anchor:wa.tests.mw_run_unit.TestWorkflow.test_support
    def test_support(self):
        self.capabilities.no_support = True
        self._reject(self._run(), 'OUT_OF_SUPPORT')
        self.capabilities.no_support = False
        self.assertEqual(self._run().rc, 0)

    # @anchor:wa.tests.mw_run_unit.TestWorkflow.test_pixel_evidence
    def test_pixel_evidence(self):
        spec = self.workflow['steps'][0]['verify']
        spec.update(mode='reacted_mask', mask_boxes_px=[[0, 0, 100, 100]], ram_evidence_ref='ram-evidence')
        self.capabilities.observe_verifier = lambda verifier, state, deadline: self.observations.acquire(verifier['predicate'], deadline)
        self.capabilities.no_verifier = True  # Registered owner finds no remaining support.
        self._reject(self._run(), 'OUT_OF_SUPPORT')
        self.capabilities.no_verifier = False
        spec['mask_boxes_px'] = [[0, 0, 1, 1]]
        self.assertEqual(self._run().rc, 0)

    # @anchor:wa.tests.mw_run_unit.TestWorkflow.test_t0_ram
    def test_t0_ram(self):
        import numpy as np
        frame = np.random.default_rng(27).integers(0, 256, (64, 64, 3), dtype=np.uint8)
        small = demos.TemplateCrop((28, 28, 36, 36), (4, 4), frame[28:36, 28:36].copy())
        context = demos.TemplateCrop((24, 24, 40, 40), (8, 8), frame[24:40, 24:40].copy())
        descriptor = demos.TargetDescriptor((32, 32), small, context)
        self._click()
        self.workflow['steps'][0]['target'] = dict(rung='T0', demo_id='demo', instances=[dict(seq=1, crop_box_px=[28, 28, 36, 36],
                   crop_sha256=hashlib.sha256(small.pixels.tobytes()).hexdigest())], match_mode='pixel_ncc', min_confidence_bps=9000)
        ram = types.SimpleNamespace(good=False)

        def resolve(target, instance, state, deadline):
            # Synthetic RAM owner attests session custody and recorded crop pin.
            pin_ok = hashlib.sha256(small.pixels.tobytes()).hexdigest() == instance['crop_sha256']
            return frame, descriptor, self.observations.acquire(_predicate('target'), deadline), YES if ram.good and pin_ok else NO
        ram.t0 = resolve
        self.limits = replace(self.limits, peak_bytes=1_000_000, retained_bytes=1_000_000)
        self.owners = replace(self.owners, limits=self.limits, ram=ram,
                             replay_policy=replay.ReplayPolicy(displacement=3, limits=replace(demos.DemoLimits(), memory_bytes=1_000_000, retained_bytes=1_000_000)))
        self._reject(self._run(), 'OUT_OF_SUPPORT')
        ram.good = True
        result = self._run()
        self.assertEqual(result.rc, 0, result)
        self.assertEqual(self.transport.sent, ['click'])

    # @anchor:wa.tests.mw_run_unit.TestWorkflow.test_t0_digest_and_ambiguity
    def test_t0_digest_and_ambiguity(self):
        import numpy as np
        frame = np.random.default_rng(71).integers(0, 256, (64, 64, 3), dtype=np.uint8)
        def descriptor(x):
            return demos.TargetDescriptor((x + 4, x + 4),
                demos.TemplateCrop((x, x, x + 8, x + 8), (4, 4), frame[x:x+8, x:x+8].copy()),
                demos.TemplateCrop((x - 4, x - 4, x + 12, x + 12), (8, 8), frame[x-4:x+12, x-4:x+12].copy()))
        descriptors = [descriptor(8), descriptor(28)]
        instances = [dict(seq=i + 1, crop_box_px=list(d.small.bbox_xyxy),
                          crop_sha256=hashlib.sha256(d.small.pixels.tobytes()).hexdigest()) for i, d in enumerate(descriptors)]
        spec = dict(rung='T0', demo_id='demo', instances=instances[:1], match_mode='pixel_ncc', min_confidence_bps=9000)
        def resolve(target, instance, state, deadline):
            d = descriptors[instance['seq'] - 1]
            bound = hashlib.sha256(d.small.pixels.tobytes()).hexdigest() == instance['crop_sha256']
            return frame, d, self.observations.acquire(_predicate('target'), deadline), YES if bound else NO
        self.limits = replace(self.limits, peak_bytes=1_000_000, retained_bytes=1_000_000)
        self.owners = replace(self.owners, limits=self.limits, ram=types.SimpleNamespace(t0=resolve),
                 replay_policy=replay.ReplayPolicy(displacement=3, limits=replace(demos.DemoLimits(), memory_bytes=1_000_000, retained_bytes=1_000_000)))
        instances[0]['crop_sha256'] = '0' * 64
        with self.assertRaises(Exception) as caught:
            w.locate_target(spec, step_state=self._verification_state(), owners=self.owners, deadline_ns=self.clock() + 50_000_000)
        self.assertEqual(caught.exception.code, 'OUT_OF_SUPPORT')
        instances[0]['crop_sha256'] = hashlib.sha256(descriptors[0].small.pixels.tobytes()).hexdigest()
        spec['instances'] = instances
        with self.assertRaises(Exception) as caught:
            w.locate_target(spec, step_state=self._verification_state(), owners=self.owners, deadline_ns=self.clock() + 50_000_000)
        self.assertEqual(caught.exception.code, 'LOW_CONFIDENCE')
        spec['instances'] = instances[:1]
        result = w.locate_target(spec, step_state=self._verification_state(), owners=self.owners, deadline_ns=self.clock() + 50_000_000)
        self.assertEqual(result.point, (12, 12))

    # @anchor:wa.tests.mw_run_unit.TestWorkflow.test_t1_t2_registered_evidence
    def test_t1_t2_registered_evidence(self):
        for rung, missing in (('T1', 'disagreeing-anchors'), ('T2', 'duplicate-label'),
                              ('T2', 'truncated-marks'), ('T2', 'unsupported-locale')):
            with self.subTest(evidence=missing):
                spec = _target() if rung == 'T1' else dict(_fixture('t2'), locale='en')
                self.capabilities.validate_target = lambda *args: NO
                with self.assertRaises(Exception) as caught:
                    w.locate_target(spec, step_state=self._verification_state(), owners=self.owners, deadline_ns=self.clock() + 50_000_000)
                self.assertEqual(caught.exception.code, 'LOW_CONFIDENCE')
                self.capabilities.validate_target = lambda *args: YES
                self.assertIsInstance(w.locate_target(spec, step_state=self._verification_state(), owners=self.owners,
                                      deadline_ns=self.clock() + 50_000_000), w.GroundedTarget)

    def _verification_state(self):
        run = w.RunState(b'', 'a' * 64, self.workflow, 'scope', 'authority', {},
                         last_progress_ns=self.clock())
        deadline = self.clock() + 50_000_000
        return w.StepState(1, run, deadline, deadline, deadline, input_ns=self.clock(), start_ns=self.clock())

    # @anchor:wa.tests.mw_run_unit.TestWorkflow.test_fallback_contract
    def test_fallback_contract(self):
        step = self.workflow['steps'][0]
        spec = step['verify']
        child = copy.deepcopy(spec)
        child['predicate'] = _predicate('fallback')
        spec['fallback_verifiers'] = [child]
        self.observations.values['synthetic-v1'] = [self._observation(complete=False)]
        for field, bad in (('fallback_verifiers', [copy.deepcopy(child)]), ('deadline_ms', spec['deadline_ms'] + 1),
                           ('busy_predicate', _predicate('other-busy')), ('predicate', _predicate('fallback', 'changed', 'other-base'))):
            with self.subTest(field=field):
                original = child[field]
                child[field] = bad
                with self.assertRaises(Exception) as caught:
                    w.verify_step(step, state=self._verification_state(), owners=self.owners)
                self.assertEqual(caught.exception.code, 'OUT_OF_SUPPORT')
                self.assertNotIn('fallback', self.observations.calls)
                child[field] = original
        ss = self._verification_state()
        try:
            result = w.verify_step(step, state=ss, owners=self.owners)
        except Exception as exc:
            self.fail('Equivalent fallback refused: ' + type(exc).__name__)
        self.assertIs(result.result, Truth.TRUE)
        self.assertIn('fallback', self.observations.calls)  # UNKNOWN primary really fell back.
        self.assertEqual(ss.verify_deadline_ns, ss.deadline_ns)
        self.assertLess(self.clock(), ss.verify_deadline_ns)

    # @anchor:wa.tests.mw_run_unit.TestWorkflow.test_fallback_privacy_support
    def test_fallback_privacy_support(self):
        step = self.workflow['steps'][0]
        spec = step['verify']
        child = copy.deepcopy(spec)
        child['predicate'] = _predicate('fallback')
        spec['fallback_verifiers'] = [child]
        self.capabilities.verifier = lambda candidate: AdmissionCheck(Truth.FALSE, Reason.PRIVACY) if candidate is spec else YES
        with self.assertRaises(Exception) as caught:
            w.verify_step(step, state=self._verification_state(), owners=self.owners)
        self.assertEqual(caught.exception.code, 'OUT_OF_SUPPORT')
        self.assertNotIn('fallback', self.observations.calls)
        self.capabilities.verifier = lambda candidate: YES
        self.observations.validate = lambda value: AdmissionCheck(Truth.UNKNOWN, Reason.PRIVACY)
        with self.assertRaises(Exception) as caught:
            w.verify_step(step, state=self._verification_state(), owners=self.owners)
        self.assertEqual(caught.exception.code, 'OUT_OF_SUPPORT')
        self.assertNotIn('fallback', self.observations.calls)
        self.observations.validate = lambda value: YES
        self.observations.values['synthetic-v1'] = [False]
        self.assertIs(w.verify_step(step, state=self._verification_state(), owners=self.owners).result, Truth.TRUE)

    # @anchor:wa.tests.mw_run_unit.TestWorkflow.test_fallback_original_deadline
    def test_fallback_original_deadline(self):
        step = self.workflow['steps'][0]
        spec = step['verify']
        child = copy.deepcopy(spec)
        child['predicate'] = _predicate('fallback')
        spec['fallback_verifiers'] = [child]
        acquire = self.observations.acquire
        def slow(predicate, deadline):
            if predicate['capability_id'] == 'synthetic-v1':
                self.clock.sleep(50_000_000)
            return acquire(predicate, deadline)
        self.observations.acquire = slow
        with self.assertRaises(Exception) as caught:
            w.verify_step(step, state=self._verification_state(), owners=self.owners)
        self.assertEqual(caught.exception.code, 'DEADLINE')
        self.assertNotIn('fallback', self.observations.calls)
        self.observations.acquire = acquire
        self.observations.values['synthetic-v1'] = [False]
        self.assertIs(w.verify_step(step, state=self._verification_state(), owners=self.owners).result, Truth.TRUE)

    # @anchor:wa.tests.mw_run_unit.TestWorkflow.test_changed_predicate
    def test_changed_predicate(self):
        predicate = _predicate(comparison='changed', snapshot='base')
        old = self._observation(object(), output_kind='ram')
        current = replace(old, value=object(), capture_start_ns=2, capture_end_ns=2)
        snapshots = {'base': w.CapturedSnapshot('base', old, 1)}
        self.capabilities.changed = lambda *args: AdmissionCheck(Truth.FALSE, None)
        self.assertIs(w.evaluate_predicate(predicate, observation=current, snapshots=snapshots, owners=self.owners), Truth.FALSE)
        self.capabilities.changed = lambda *args: YES
        self.assertIs(w.evaluate_predicate(predicate, observation=current, snapshots=snapshots, owners=self.owners), Truth.TRUE)
        for bad in (replace(current, unit='wrong'), replace(current, capture_start_ns=1), replace(current, complete=False)):
            self.assertIs(w.evaluate_predicate(predicate, observation=bad, snapshots=snapshots, owners=self.owners), Truth.UNKNOWN)
        self.capabilities.changed = lambda *args: True
        self.assertIs(w.evaluate_predicate(predicate, observation=current, snapshots=snapshots, owners=self.owners), Truth.UNKNOWN)

    # @anchor:wa.tests.mw_run_unit.TestWorkflow.test_after_crop_unmasked
    def test_after_crop_unmasked(self):
        import numpy as np
        before = np.zeros((16, 16, 3), dtype=np.uint8)
        after = before.copy()
        after[4:12, 4:12] = 255
        expected = demos.ExpectedChange((4, 4, 12, 12), np.ones((8, 8), dtype=bool), after[4:12, 4:12].copy(),
                                        before_crop=before[4:12, 4:12].copy())
        step = self.workflow['steps'][0]
        step['verify'].update(mode='after_crop', ram_evidence_ref='ram-pair', mask_boxes_px=[])
        self.limits = replace(self.limits, peak_bytes=1_000_000, retained_bytes=1_000_000)
        policy = replay.ReplayPolicy(limits=replace(demos.DemoLimits(), memory_bytes=1_000_000, retained_bytes=1_000_000))
        frames = [before, before]
        ram = types.SimpleNamespace(change=lambda *args: (frames[0], frames[1], expected, YES))
        self.owners = replace(self.owners, limits=self.limits, replay_policy=policy, ram=ram)
        with patch.object(w, 'verify_change', wraps=replay.verify_change) as spy:
            with self.assertRaises(Exception) as caught:
                w.verify_step(step, state=self._verification_state(), owners=self.owners)
            self.assertEqual(getattr(caught.exception, 'code', None), 'DEADLINE')
            self.assertGreater(spy.call_count, 0)
            frames[1] = after
            try:
                result = w.verify_step(step, state=self._verification_state(), owners=self.owners)
            except Exception as exc:
                self.fail('Valid targetless crop refused: ' + type(exc).__name__)
            self.assertIs(result.result, Truth.TRUE)
            self.assertEqual(spy.call_args.kwargs['translation'], (0, 0))
            self.assertIs(spy.call_args.kwargs['policy'], policy)

    # @anchor:wa.tests.mw_run_unit.TestWorkflow.test_repeat_no_second_verification
    def test_repeat_no_second_verification(self):
        step = self.workflow['steps'][0]
        step['repeat'] = {'while_predicate': _predicate('while'), 'max_iterations': 2}
        step['verify']['consecutive_polls'] = 2
        self.observations.values['while'] = [True, False]
        self.observations.values['synthetic-v1'] = [True, True, False]
        result = self._run()
        self.assertEqual(result.rc, 0, result)
        self.assertEqual(self.observations.calls.count('synthetic-v1'), 2)
        self.assertEqual(self.observations.values['synthetic-v1'], [False])

    # @anchor:wa.tests.mw_run_unit.TestWorkflow.test_repeat_zero_busy_crop
    def test_repeat_zero_busy_crop(self):
        step = self.workflow['steps'][0]
        step['repeat'] = {'while_predicate': _predicate('while'), 'max_iterations': 1}
        step['verify']['busy_predicate'] = _predicate('busy')
        self.observations.values['while'] = [False]
        self.observations.values['busy'] = [False]
        self.observations.values['synthetic-v1'] = [False]
        self._reject(self._run(), 'DEADLINE')
        self.clock.now = 1000000
        self.journal.raw = b''
        self.observations.values['synthetic-v1'] = [True]
        result = self._run()
        self.assertEqual(result.rc, 0, result)
        self.assertNotIn('busy', self.observations.calls)
        self.assertEqual(self.transport.sent, [])
        # A zero-iteration crop verifier must use its independent semantic proof.
        step['verify'].update(mode='after_crop', ram_evidence_ref='unused-pair')
        self.owners = replace(self.owners, ram=object(), replay_policy=object())
        self.journal.raw = b''
        self.assertEqual(self._run().rc, 0)

    # @anchor:wa.tests.mw_run_unit.TestWorkflow.test_repeat_baseline_once_unknown
    def test_repeat_baseline_once_unknown(self):
        step = self.workflow['steps'][0]
        step['capture_before'] = [{'name': 'base', 'observe': _predicate('count')}]
        step['verify'].update(predicate=_predicate('count', 'delta_gt', 'base'), consecutive_polls=1)
        step['repeat'] = {'while_predicate': _predicate('while'), 'max_iterations': 2}
        self.observations.values['count'] = [0, 1, 2]
        self.observations.values['while'] = [True, True, self._observation(capability_id='while', complete=False)]
        self._reject(self._run(), 'LOW_CONFIDENCE')
        self.assertEqual(self.observations.calls.count('count'), 3)
        self.clock.now = 1000000
        self.journal.raw = b''
        self.journal.selected = 'old'
        self.observations.calls.clear()
        self.observations.values['count'] = [0, 1, 2]
        self.observations.values['while'] = [True, True, False]
        self.assertEqual(self._run().rc, 0)
        self.assertEqual(self.observations.calls.count('count'), 3)

    def _chip_workflow(self):
        self._paste()
        paste = copy.deepcopy(self.workflow['steps'][0])
        paste['seq'] = 3
        self._click()
        close = copy.deepcopy(self.workflow['steps'][0])
        close['action']['text_slot'] = None
        close['preconditions'] = [_predicate('chip-present')]
        close['verify']['predicate'] = _predicate('removed')
        left = copy.deepcopy(close)
        left['seq'] = 2
        close['target']['offset_dx_px'] = 20
        left['target']['offset_dx_px'] = 10
        self.workflow['steps'] = [close, left, paste]
        self.workflow['text_slots'][0]['chip_clear_steps'] = [1, 2]
        self.ctx = replace(self.ctx, evidence_load=lambda kind, step, related:
                           dict(center_x_px=30 - 10 * step['seq'], center_y_px=10,
                                chip_id='chip-' + str(step['seq']), guard_pass=True, postcondition_pass=True))
        self.owners = replace(self.owners, validation_context=self.ctx)
        original = self.capabilities.target
        def target(spec, state, deadline):
            value = original(spec, state, deadline)
            return replace(value, point=(spec['offset_dx_px'], 10)) if type(value) is w.GroundedTarget else value
        self.capabilities.target = target

    # @anchor:wa.tests.mw_run_unit.TestWorkflow.test_chip_order
    def test_chip_order(self):
        self._chip_workflow()
        self.workflow['steps'][1]['target']['offset_dx_px'] = 30
        self._reject(self._run(), 'LOW_CONFIDENCE', sends=1)
        self.journal.raw = b''
        self.journal.selected = 'old'
        self.transport.sent.clear()
        self.workflow['steps'][1]['target']['offset_dx_px'] = 10
        self.assertEqual(self._run().rc, 0)
        self.assertEqual(self.transport.sent, ['click', 'click', 'private_pipe'])

    # @anchor:wa.tests.mw_run_unit.TestWorkflow.test_chip_grounding
    def test_chip_grounding(self):
        self._chip_workflow()
        self.capabilities.bad_target = True
        self._reject(self._run(), 'DEADLINE')
        self.clock.now = 1000000
        self.journal.raw = b''
        self.capabilities.bad_target = False
        self.assertEqual(self._run().rc, 0)

    # @anchor:wa.tests.mw_run_unit.TestWorkflow.test_chip_removal_and_focus
    def test_chip_removal_and_focus(self):
        self._chip_workflow()
        self.observations.values['removed'] = [False]
        self._reject(self._run(), 'DEADLINE', sends=1)
        self.clock.now = 1000000
        self.journal.raw = b''
        self.transport.sent.clear()
        self.observations.values['removed'] = [True]
        self.capabilities.no_focus = True
        self._reject(self._run(), 'OUT_OF_SUPPORT', sends=2)
        self.journal.raw = b''
        self.journal.selected = 'old'
        self.transport.sent.clear()
        self.capabilities.no_focus = False
        self.assertEqual(self._run().rc, 0)

    # @anchor:wa.tests.mw_run_unit.TestWorkflow.test_chip_unverified_reference
    def test_chip_unverified_reference(self):
        self._chip_workflow()
        self.authorization.segments = {}
        self.workflow['steps'][0]['when'] = _predicate('guard')
        self.observations.values['guard'] = [False]
        self._reject(self._run(), 'OUT_OF_SUPPORT')
        self.journal.raw = b''
        self.journal.selected = 'old'
        self.observations.values['guard'] = [True]
        self.assertEqual(self._run().rc, 0)

    # @anchor:wa.tests.mw_run_unit.TestWorkflow.test_chip_prefix_consistency
    def test_chip_prefix_consistency(self):
        self._chip_workflow()
        target = self.capabilities.target
        def inconsistent(spec, state, deadline):
            value = target(spec, state, deadline)
            if state.seq == 2:
                state.run.completed_steps[1].classification = 'condition_skip'
            return value
        self.capabilities.target = inconsistent
        self._reject(self._run(), 'OUT_OF_SUPPORT', sends=1)
        self.journal.raw = b''
        self.journal.selected = 'old'
        self.transport.sent.clear()
        self.capabilities.target = target
        self.assertEqual(self._run().rc, 0)

    # @anchor:wa.tests.mw_run_unit.TestWorkflow.test_prompt_growth_relocation
    def test_prompt_growth_relocation(self):
        self._paste()
        paste = copy.deepcopy(self.workflow['steps'][0])
        self._click()
        submit = copy.deepcopy(self.workflow['steps'][0])
        submit['seq'] = 2
        submit['action']['text_slot'] = None
        self.workflow['steps'] = [paste, submit]
        original = self.capabilities.target
        stale = self._observation(capability_id='target')
        def old_target(spec, state, deadline):
            return replace(original(spec, state, deadline), observation=stale)
        self.capabilities.target = old_target
        self._reject(self._run(), 'DEADLINE', sends=1)
        self.clock.now = 1000000
        self.journal.raw = b''
        self.transport.sent.clear()
        points = []
        send = self.transport.send
        def moved_send(action, target):
            points.append(target.point)
            return send(action, target)
        self.transport.send = moved_send
        self.capabilities.target = lambda spec, state, deadline: replace(original(spec, state, deadline), point=(30, 40))
        self.assertEqual(self._run().rc, 0)
        self.assertEqual(self.transport.sent, ['private_pipe', 'click'])
        self.assertEqual(points, [(30, 40)])

    # @anchor:wa.tests.mw_run_unit.TestWorkflow.test_repeat_fresh_target
    def test_repeat_fresh_target(self):
        self._click()
        step = self.workflow['steps'][0]
        step['repeat'] = {'while_predicate': _predicate('while'), 'max_iterations': 2}
        self.observations.values['while'] = [True, True, False]
        original = self.capabilities.target
        cache = []
        def retained(spec, state, deadline):
            if not cache:
                cache.append(original(spec, state, deadline))
            return cache[0]
        self.capabilities.target = retained
        self._reject(self._run(), 'DEADLINE', sends=1)
        self.clock.now = 1000000
        self.journal.raw = b''
        self.transport.sent.clear()
        self.observations.values['while'] = [True, True, False]
        self.capabilities.target = original
        self.assertEqual(self._run().rc, 0)
        self.assertEqual(self.transport.sent, ['click', 'click'])

    # @anchor:wa.tests.mw_run_unit.TestWorkflow.test_target_input_floor
    def test_target_input_floor(self):
        ss = self._verification_state()
        original = self.capabilities.target
        captured = original(_target(), ss, ss.locate_deadline_ns)
        ss.run.last_input_ns = self.clock()
        self.capabilities.target = lambda *args: captured
        with self.assertRaises(Exception) as caught:
            w.locate_target(_target(), step_state=ss, owners=self.owners, deadline_ns=ss.locate_deadline_ns)
        self.assertEqual(caught.exception.code, 'LOW_CONFIDENCE')
        self.capabilities.target = original
        result = w.locate_target(_target(), step_state=ss, owners=self.owners, deadline_ns=ss.locate_deadline_ns)
        self.assertGreater(result.observation.capture_start_ns, ss.run.last_input_ns)
        ss.run.last_input_ns = None
        captured = result
        self.clock.sleep(1_000_000)
        ss.start_ns = self.clock()
        self.capabilities.target = lambda *args: captured
        with self.assertRaises(Exception) as caught:
            w.locate_target(_target(), step_state=ss, owners=self.owners, deadline_ns=ss.locate_deadline_ns)
        self.assertEqual(caught.exception.code, 'LOW_CONFIDENCE')
        self.capabilities.target = original
        self.assertIsInstance(w.locate_target(_target(), step_state=ss, owners=self.owners,
                                             deadline_ns=ss.locate_deadline_ns), w.GroundedTarget)
        ss.start_ns = None
        with self.assertRaises(Exception) as caught:
            w.locate_target(_target(), step_state=ss, owners=self.owners, deadline_ns=ss.locate_deadline_ns)
        self.assertEqual(caught.exception.code, 'LOW_CONFIDENCE')

    # @anchor:wa.tests.mw_run_unit.TestWorkflow.test_chip_skip_before_paste
    def test_chip_skip_before_paste(self):
        self._chip_workflow()
        self.authorization.segments = {}
        paste = self.workflow['steps'].pop()
        self.workflow['steps'].pop()
        paste['seq'] = 2
        self.workflow['steps'].append(paste)
        self.workflow['text_slots'][0]['chip_clear_steps'] = [1]
        self.workflow['steps'][0]['when'] = _predicate('guard')
        self.observations.values['guard'] = [False]
        self._reject(self._run(), 'OUT_OF_SUPPORT')
        self.journal.raw = b''
        self.journal.selected = 'old'
        self.observations.values['guard'] = [True]
        self.assertEqual(self._run().rc, 0)

    # @anchor:wa.tests.mw_run_unit.TestWorkflow.test_failed_inventory
    def test_failed_inventory(self):
        self._click()
        second = copy.deepcopy(self.workflow['steps'][0])
        second['seq'] = 2
        second['action'].update(kind='key', key_name='Return')
        second['target'] = None
        second['preconditions'] = [_predicate('focus')]
        third = copy.deepcopy(second)
        third['seq'] = 3
        self.workflow['steps'].extend([second, third])
        self.capabilities.no_focus = True
        result = self._run()
        self._reject(result, 'OUT_OF_SUPPORT', sends=1)
        self.assertIsNotNone(result.summary)
        self.assertEqual([result.summary[k] for k in ('steps_verified', 'steps_failed', 'steps_not_reached')], [1, 1, 1])
        self.assertEqual(result.summary['failed_step_seq'], 2)
        self.assertEqual([s['disposition'] for s in self.journal.inventory['steps']], ['verified', 'failed', 'not_reached'])
        self.assertFalse(self.journal.inventory['final_task_pass'])
        self.assertEqual(g.validate_file(Path(self.journal.selected), BUNDLE, context=self.journal.context()), g.GateResult(0, 'PASS'))
        self.capabilities.no_focus = False
        self.transport.sent.clear()
        self._reject(self._run(), 'OUT_OF_SUPPORT')
        self.journal.raw = b''
        self.assertEqual(self._run().rc, 0)  # Fresh invocation of corrected workflow.

    # @anchor:wa.tests.mw_run_unit.TestWorkflow.test_cli_unbound
    def test_cli_unbound(self):
        self._write()
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main([str(self.path), '--mode', 'replay', '--output', str(self.root / 'out')]), 5)
            self.assertEqual(cli.main([str(self.path), '--mode', 'validate'], owners=self.owners), 0)
        self.assertEqual(self.transport.sent, [])

    # @anchor:wa.tests.mw_run_unit.TestWorkflow.test_resume_refusal
    def test_resume_refusal(self):
        self._click()
        for kind in ('verified', 'satisfied_skip', 'condition_skip'):
            with self.subTest(kind=kind):
                self.journal.raw = self._journal_event(kind)
                result = self._run()
                self._reject(result, 'OUT_OF_SUPPORT')
                self._failed_run(result)
                self.assertEqual(self.observations.calls, [])
        self.journal.raw = b''
        self.assertEqual(self._run().rc, 0)
        self.assertEqual(self.transport.sent, ['click'])

    # @anchor:wa.tests.mw_run_unit.TestWorkflow.test_independent_length
    def test_independent_length(self):
        self._paste()
        self.observations.values['length'] = [len(self.slots.value) + 1]
        self._reject(self._run(), 'CHANGE_MISMATCH', sends=1)
        self.journal.raw = b''
        self.transport.sent.clear()
        self.observations.values['length'] = [len(self.slots.value)]
        self.assertEqual(self._run().rc, 0)

    # @anchor:wa.tests.mw_run_unit.TestWorkflow.test_runlevel_receipt
    def test_runlevel_receipt(self):
        self.workflow['preconditions'] = [_predicate('pre')]
        self.observations.values['pre'] = [False]
        result = self._run()
        self._reject(result, 'LOW_CONFIDENCE')
        self.assertIsNotNone(result.summary)
        self.assertEqual(result.summary['steps_not_reached'], 1)
        self.assertEqual(result.summary['steps_failed'], 0)
        self.assertIsNone(result.summary['failed_step_seq'])
        self.assertEqual(g.validate_file(Path(self.journal.selected), BUNDLE,
                         context=self.journal.context()), g.GateResult(0, 'PASS'))

    # @anchor:wa.tests.mw_run_unit.TestWorkflow.test_gesture_binding
    def test_gesture_binding(self):
        self._click()
        action = self.workflow['steps'][0]['action']
        action.update(kind='drag', path='coordinates', dx_px=-2, dy_px=3)
        calls = []
        def native(operation, payload):
            calls.append((operation, payload))
            return dict(focus_verified=True, input_mono_ns=self.clock())
        surface = SrfIdentitySurface(identity='synthetic', container='synthetic', window='w1',
                                     transport=native, ram_dir='/dev/shm')
        payload = {'span': '11,10:8,13', 'button': 1}
        def plan(action, target):
            return w.IdentityPlan(action['kind'], payload, YES)
        self.owners = replace(self.owners, surface=surface, dispatch=LegacyDispatchOwner(surface),
                              transport=w.IdentityTransport(surface, plan))
        result = self._run()
        self._reject(result, 'OUT_OF_SUPPORT')
        self.assertEqual(calls, [])
        self.assertFalse(any(g.strict_load(x)['kind'] == 'intent' for x in self.journal.raw.splitlines()))
        self.journal.raw = b''
        self.owners = replace(self.owners, dispatch=LegacyDispatchOwner(surface))
        payload['span'] = '10,10:8,13'
        self.assertEqual(self._run().rc, 0)
        self.assertEqual(calls[0][1]['span'], '10,10:8,13')

    # @anchor:wa.tests.mw_run_unit.TestWorkflow.test_cli_mismatch
    def test_cli_mismatch(self):
        self._write()
        for mode, output in (('shadow', self.owners.output), ('replay', self.root / 'other')):
            with contextlib.redirect_stdout(io.StringIO()) as stream:
                rc = cli.main([str(self.path), '--mode', mode, '--output', str(output)], owners=self.owners)
            self.assertEqual(rc, 2)
            self.assertEqual(stream.getvalue(), g.canon({'rc': 2, 'verdict_code': 'USAGE_SCHEMA'}) + '\n')
        self.assertEqual(self.transport.sent, [])

    def _fresh_invocation(self):
        self.journal.raw = b''
        self.journal.selected = 'old'
        self.transport.sent.clear()
        self.observations.calls.clear()
        self.clock.now = 1_000_000
        self.owners = replace(self.owners, dispatch=LegacyDispatchOwner(self.owners.surface))

    def _failed_run(self, result):
        self.assertIsNotNone(result.summary, result)
        self.assertEqual(result.summary['steps_not_reached'], len(self.workflow['steps']))
        self.assertEqual(result.summary['steps_failed'], 0)
        self.assertIsNone(result.summary['failed_step_seq'])
        self.assertFalse(self.journal.inventory['final_task_pass'])
        self.assertEqual(result.summary['visibility'], 'local')
        for key in ('duration_ms', 'model_calls', 'tokens_used'):
            self.assertIsNone(result.summary[key])
        self.assertEqual(g.validate_file(Path(self.journal.selected), BUNDLE,
                         context=self.journal.context()), g.GateResult(0, 'PASS'))

    # @anchor:wa.tests.mw_run_unit.TestWorkflow.test_resume_precedence
    def test_resume_precedence(self):
        self._click()
        history = self._journal_event('verified')
        self.journal.raw = history + b'torn'
        result = self._run()
        self._reject(result, 'TORN')
        self.assertEqual(self.journal.quarantine, b'torn')
        self.assertIsNone(result.summary)
        event = g.strict_load(self._journal_event('intent'))
        event.update(seq=2, prev=g.strict_load(history)['digest'])
        del event['digest']
        event['digest'] = self.journal.digest(g.canon(event).encode(), event['prev'])
        self.journal.raw = history + (g.canon(event) + '\n').encode()
        result = self._run()
        self._reject(result, 'INPUT_UNCERTAIN')
        self.assertIsNone(result.summary)
        self.journal.raw = history
        result = self._run()
        self._reject(result, 'OUT_OF_SUPPORT')
        self._failed_run(result)
        self.assertEqual(self.observations.calls, [])
        self.journal.raw = self._journal_event('failed')
        self.assertEqual(self._run().rc, 0)
        self.assertEqual(self.transport.sent, ['click'])

    # @anchor:wa.tests.mw_run_unit.TestWorkflow.test_length_conventions_unicode
    def test_length_conventions_unicode(self):
        self._paste()
        self.slots.value = 'é🙂'
        self.slots.initial_length = 1
        self.observations.values['length'] = [3]
        self._reject(self._run(), 'CHANGE_MISMATCH', sends=1)
        self._fresh_invocation()
        self.workflow['text_slots'][0]['post_type_len_allowed'] = ['initial_plus_n']
        self.assertEqual(self._run().rc, 0)
        self._fresh_invocation()
        self.workflow['text_slots'][0]['post_type_len_allowed'] = ['exact_n']
        self.observations.values['length'] = [len(self.slots.value.encode())]
        self._reject(self._run(), 'CHANGE_MISMATCH', sends=1)
        self._fresh_invocation()
        self.observations.values['length'] = [len(self.slots.value)]
        self.assertEqual(self._run().rc, 0)

    # @anchor:wa.tests.mw_run_unit.TestWorkflow.test_length_evidence_validity
    def test_length_evidence_validity(self):
        self._paste()
        n = len(self.slots.value)
        for changes in ({'value': True}, {'value': -1}, {'capture_start_ns': 0},
                        {'complete': False}, {'scope': 'foreign'}, {'authority': 'foreign'},
                        {'capability_id': 'foreign'}, {'capability_revision': 'v2'}, {'unit': 'bytes'},
                        {'output_kind': 'boolean'}, {'missing_reason': 'MISSING'}):
            with self.subTest(changes=changes):
                self._fresh_invocation()
                acquire = self.observations.acquire
                def observed(predicate, deadline):
                    value = acquire(predicate, deadline)
                    return replace(value, **changes) if predicate['capability_id'] == 'length' else value
                with patch.object(self.observations, 'acquire', observed):
                    self._reject(self._run(), 'CHANGE_MISMATCH', sends=1)
        self._fresh_invocation()
        self.observations.values['length'] = [n]
        self.assertEqual(self._run().rc, 0)

    # @anchor:wa.tests.mw_run_unit.TestWorkflow.test_length_binding_contract
    def test_length_binding_contract(self):
        self._paste()
        binding = self.owners.slot_length_bindings[0]
        invalid = [(), (binding, binding), (replace(binding, capability_revision=''),),
                   (replace(binding, unit=''),),
                   (replace(binding, predicate=dict(binding.predicate, capability_id='')),),
                   (replace(binding, predicate=dict(binding.predicate, comparison='delta_gt')),),
                   (replace(binding, predicate=dict(binding.predicate, snapshot_ref='old')),),
                   (replace(binding, predicate=dict(binding.predicate, bindings=[{'name': 'x', 'type': 'integer', 'value': True}])),),
                   (replace(binding, predicate=dict(binding.predicate, evidence_refs=[True])),)]
        for bindings in invalid:
            with self.subTest(bindings=bindings):
                self._fresh_invocation()
                self.owners = replace(self.owners, slot_length_bindings=bindings)
                self._reject(self._run(), 'OUT_OF_SUPPORT')
        self._fresh_invocation()
        self.owners = replace(self.owners, slot_length_bindings=(binding,))
        self.assertEqual(self._run().rc, 0)

    # @anchor:wa.tests.mw_run_unit.TestWorkflow.test_length_descriptor_budget_copy
    def test_length_descriptor_budget_copy(self):
        self._paste()
        binding = self.owners.slot_length_bindings[0]
        self.resources.denied = 'length_descriptor'
        self._reject(self._run(), 'OUT_OF_SUPPORT')
        self.resources.denied = None
        self._fresh_invocation()
        self.limits = replace(self.limits, retained_bytes=1)
        self.owners = replace(self.owners, limits=self.limits)
        self._reject(self._run(), 'OUT_OF_SUPPORT')
        self._fresh_invocation()
        self.limits = replace(self.limits, retained_bytes=8192)
        self.owners = replace(self.owners, limits=self.limits,
            slot_length_bindings=(replace(binding, predicate=dict(binding.predicate, evidence_refs=['x' * 70000])),))
        self._reject(self._run(), 'OUT_OF_SUPPORT')
        self._fresh_invocation()
        self.owners = replace(self.owners, slot_length_bindings=(binding,))
        send = self.transport.send_private
        def mutate(action, target, payload):
            binding.predicate['capability_id'] = 'tampered'
            return send(action, target, payload)
        self.transport.send_private = mutate
        self.assertEqual(self._run().rc, 0)
        self.assertIn('length', self.observations.calls)
        self.assertNotIn('tampered', self.observations.calls)

    # @anchor:wa.tests.mw_run_unit.TestWorkflow.test_length_acquisition_deadline
    def test_length_acquisition_deadline(self):
        self._paste()
        self.slots.transport = 'owned_clipboard'
        self.limits = replace(self.limits, acquisitions=2)
        self.owners = replace(self.owners, limits=self.limits)
        self._reject(self._run(), 'OUT_OF_SUPPORT', sends=1)
        self.assertEqual(self.slots.cleared_count, 1)
        self._fresh_invocation()
        self.limits = replace(self.limits, acquisitions=1000)
        self.owners = replace(self.owners, limits=self.limits)
        self.workflow['text_slots'][0]['budget_ms'] = 1
        self._reject(self._run(), 'DEADLINE', sends=1)
        self.assertEqual(self.slots.cleared_count, 2)
        self._fresh_invocation()
        self.workflow['text_slots'][0]['budget_ms'] = 50
        self.assertEqual(self._run().rc, 0)
        self.assertEqual(self.slots.cleared_count, 3)

    # @anchor:wa.tests.mw_run_unit.TestWorkflow.test_admission_failure_receipt
    def test_admission_failure_receipt(self):
        for failure in ('admission', 'capability', 'false', 'unknown'):
            with self.subTest(failure=failure):
                self._fresh_invocation()
                self.authorization.denied = 'admission' if failure == 'admission' else None
                self.capabilities.no_support = failure == 'capability'
                self.workflow['preconditions'] = []
                if failure in ('false', 'unknown'):
                    self.workflow['preconditions'] = [_predicate('pre')]
                    self.observations.values['pre'] = [False if failure == 'false' else self._observation(complete=False)]
                result = self._run()
                self._reject(result, 'LOW_CONFIDENCE' if failure in ('false', 'unknown') else 'OUT_OF_SUPPORT')
                self._failed_run(result)
        self._fresh_invocation()
        self.workflow['preconditions'] = []
        self.assertEqual(self._run().rc, 0)

    # @anchor:wa.tests.mw_run_unit.TestWorkflow.test_failure_receipt_transaction
    def test_failure_receipt_transaction(self):
        self.workflow['preconditions'] = [_predicate('pre')]
        self.observations.values['pre'] = [False]
        for failure in ('cancel', 'drain', 'commit', 'validation', 'gate', 'selection'):
            with self.subTest(failure=failure):
                self._fresh_invocation()
                self.authorization.denied = 'commit' if failure == 'commit' else None
                self.journal.bad_stage = failure == 'validation'
                self.journal.forge_inventory = failure == 'gate'
                with contextlib.ExitStack() as stack:
                    if failure == 'cancel':
                        stack.enter_context(patch.object(self.resources, 'cancel', lambda *a: NO))
                    if failure == 'drain':
                        stack.enter_context(patch.object(self.resources, 'drain', lambda *a: NO))
                    if failure == 'selection':
                        stack.enter_context(patch.object(self.journal, 'select', lambda *a: NO))
                    result = self._run()
                self.assertEqual(result.verdict_code, 'LOW_CONFIDENCE')
                self.assertIsNone(result.summary)
                self.assertEqual(self.journal.selected, 'old')
        self._fresh_invocation()
        self.authorization.denied = None
        self.journal.bad_stage = self.journal.forge_inventory = False
        # Commit must occur during the artifact lease, including admission failure.
        pin = self.artifact.pin
        active = [False]
        @contextlib.contextmanager
        def leased(path):
            with pin(path) as value:
                active[0] = True
                try:
                    yield value
                finally:
                    active[0] = False
        check = self.artifact.check
        with patch.object(self.artifact, 'pin', leased), patch.object(self.artifact, 'check',
                lambda value: check(value) if active[0] else NO):
            self._failed_run(self._run())

    # @anchor:wa.tests.mw_run_unit.TestWorkflow.test_failure_fences_no_writes
    def test_failure_fences_no_writes(self):
        self._click()
        for failure in ('torn', 'seal', 'uncertain'):
            with self.subTest(failure=failure):
                self._fresh_invocation()
                self.journal.raw = b'torn' if failure == 'torn' else b''
                self.transport.explode = failure == 'uncertain'
                if failure == 'seal':
                    self.authorization.check = lambda *a: NO
                    with patch.object(self.artifact, 'check', side_effect=[YES, NO]):
                        result = self._run()
                else:
                    result = self._run()
                self.assertEqual(result.verdict_code, {'torn': 'TORN', 'seal': 'SEAL_MISMATCH', 'uncertain': 'INPUT_UNCERTAIN'}[failure])
                self.assertIsNone(result.summary)
                self.assertEqual(self.journal.selected, 'old')
                self.assertNotIn(b'"kind":"failed"', self.journal.raw)
                self.authorization.check = _Authorization().check
        self.transport.explode = False
        self._fresh_invocation()
        self.assertEqual(self._run().rc, 0)

    def _identity_fixture(self, kind, payload, *, target=True, destination=False):
        self._click()
        step = self.workflow['steps'][0]
        step['action'].update(kind=kind, path='coordinates', dx_px=-2, dy_px=3)
        if destination:
            step['action'].update(destination=_target(), dx_px=None, dy_px=None)
        if not target:
            step['target'] = None
        calls = []
        def native(operation, value):
            calls.append((operation, value))
            return dict(focus_verified=True, input_mono_ns=self.clock())
        surface = SrfIdentitySurface(identity='synthetic', container='synthetic', window='w1',
                                     transport=native, ram_dir='/dev/shm')
        def planner(action, target):
            calls.append(('plan', None))
            return w.IdentityPlan(kind, payload, YES)
        self.owners = replace(self.owners, surface=surface, dispatch=LegacyDispatchOwner(surface),
                              transport=w.IdentityTransport(surface, planner))
        return calls

    # @anchor:wa.tests.mw_run_unit.TestWorkflow.test_drag_endpoint_binding
    def test_drag_endpoint_binding(self):
        for destination in (False, True):
            with self.subTest(destination=destination):
                self._fresh_invocation()
                payload = {'span': '10,10:99,99', 'button': 1}
                calls = self._identity_fixture('drag', payload, destination=destination)
                self._reject(self._run(), 'OUT_OF_SUPPORT')
                self.assertEqual(calls, [('plan', None)])
                self.assertNotIn(b'"kind":"intent"', self.journal.raw)
                self._fresh_invocation()
                payload['span'] = '10,10:10,10' if destination else '10,10:8,13'
                self.assertEqual(self._run().rc, 0)
                self.assertEqual(calls, [('plan', None), ('plan', None), ('drag', dict(payload))])

    # @anchor:wa.tests.mw_run_unit.TestWorkflow.test_scroll_point_binding
    def test_scroll_point_binding(self):
        for target in (False, True):
            with self.subTest(target=target):
                self._fresh_invocation()
                payload = {'point': '11,10', 'dir': 'down'}
                calls = self._identity_fixture('scroll', payload, target=target)
                self._reject(self._run(), 'OUT_OF_SUPPORT')
                self.assertEqual(calls, [('plan', None)])
                self.assertNotIn(b'"kind":"intent"', self.journal.raw)
                self._fresh_invocation()
                payload['point'] = '10,10'
                self.assertEqual(self._run().rc, 0)
                self.assertEqual(calls[-1], ('scroll', dict(payload)))

    # @anchor:wa.tests.mw_run_unit.TestWorkflow.test_targetless_scroll_grounding
    def test_targetless_scroll_grounding(self):
        payload = {'point': '10,10', 'dir': 'down'}
        calls = self._identity_fixture('scroll', payload, target=False)
        original = self.capabilities.target
        for failure in ('missing', 'foreign', 'stale', 'owner', 'expiry'):
            with self.subTest(failure=failure):
                self._fresh_invocation()
                def target(spec, state, deadline):
                    value = original(spec, state, deadline)
                    if failure == 'missing':
                        return None
                    if failure == 'foreign':
                        return replace(value, scope='foreign')
                    if failure == 'stale':
                        return replace(value, observation=replace(value.observation, capture_start_ns=0))
                    if failure == 'owner':
                        return replace(value, support_owner=object())
                    return replace(value, expires_ns=0)
                with patch.object(self.capabilities, 'target', target):
                    self._reject(self._run(), 'OUT_OF_SUPPORT')
                self.assertEqual(calls, [])
        self._fresh_invocation()
        with patch.object(self.capabilities, 'validate_target', lambda *a: NO):
            self._reject(self._run(), 'OUT_OF_SUPPORT')
        self.assertEqual(calls, [])
        self._fresh_invocation()
        self.resources.denied = 'target_peak'
        self._reject(self._run(), 'OUT_OF_SUPPORT')
        self._fresh_invocation()
        self.resources.denied = None
        self.assertEqual(self._run().rc, 0)
        self.assertEqual(calls, [('plan', None), ('scroll', dict(payload))])

    # @anchor:wa.tests.mw_run_unit.TestWorkflow.test_chip_ties_authenticated
    def test_chip_ties_authenticated(self):
        self._chip_workflow()
        original = self.capabilities.target
        # Admission geometry remains ordered; runtime must use current grounding.
        for points, ids, verdict in (((10, 11), ('a', 'b'), 0), ((11, 10), ('a', 'b'), 5),
                                     ((10, 10), ('a', 'b'), 0), ((10, 10), ('b', 'a'), 5),
                                     ((10, 10), ('a', 'a'), 5)):
            with self.subTest(points=points, ids=ids):
                self._fresh_invocation()
                loads = []
                def load(kind, step, related):
                    loads.append(step['seq'])
                    return dict(center_x_px=30 - 10 * step['seq'], center_y_px=10,
                                chip_id=ids[step['seq'] - 1], guard_pass=True, postcondition_pass=True)
                self.owners = replace(self.owners, validation_context=replace(self.ctx, evidence_load=load))
                def target(spec, state, deadline):
                    value = original(spec, state, deadline)
                    return replace(value, point=(10, points[state.seq - 1]))
                with patch.object(self.capabilities, 'target', target):
                    result = self._run()
                if verdict:
                    self._reject(result, 'LOW_CONFIDENCE', sends=1)
                else:
                    self.assertEqual(result.rc, 0, result)
                    # Gate admission loads both, then runtime loads each only once.
                    self.assertEqual(loads, [1, 2, 1, 2])

    # @anchor:wa.tests.mw_run_unit.TestWorkflow.test_chip_runtime_evidence
    def test_chip_runtime_evidence(self):
        self._chip_workflow()
        original_load = self.ctx.evidence_load
        for failure in ('missing', 'authentication', 'identity', 'geometry', 'guard', 'postcondition'):
            with self.subTest(failure=failure):
                self._fresh_invocation()
                loads = [0]
                def load(kind, step, related):
                    loads[0] += 1
                    if loads[0] <= 2:
                        return original_load(kind, step, related)
                    view = original_load(kind, step, related)
                    if failure == 'missing':
                        return None
                    if failure == 'identity':
                        view['chip_id'] = None
                    if failure == 'geometry':
                        view['center_x_px'] = True
                    if failure == 'guard':
                        view['guard_pass'] = False
                    if failure == 'postcondition':
                        view['postcondition_pass'] = False
                    return view
                def check(*args):
                    return g.GateResult(5, 'FORGED') if failure == 'authentication' and loads[0] > 2 else g.GateResult(0, 'PASS')
                self.owners = replace(self.owners, validation_context=replace(self.ctx, evidence_load=load, evidence_check=check))
                self._reject(self._run(), 'OUT_OF_SUPPORT')
        self._fresh_invocation()
        self.owners = replace(self.owners, validation_context=self.ctx)
        self.assertEqual(self._run().rc, 0)

    # @anchor:wa.tests.mw_run_unit.TestWorkflow.test_identity_direct_binding
    def test_identity_direct_binding(self):
        payload = {'span': '11,10:8,13', 'button': 1}
        calls = self._identity_fixture('drag', payload)
        target = self.capabilities.target(_target(),
            w.StepState(1, None, self.limits.deadline_ns, self.limits.deadline_ns, self.limits.deadline_ns),
            self.limits.deadline_ns)
        action = self.workflow['steps'][0]['action']
        with self.assertRaises(w._Stop):
            self.owners.transport.send(action, target)
        self.assertEqual(calls, [('plan', None)])
        payload['span'] = '10,10:8,13'
        self.assertIsInstance(self.owners.transport.send(action, target), int)
        self.assertEqual(calls[-1], ('drag', dict(payload)))

    # @anchor:wa.tests.mw_run_unit.TestWorkflow.test_length_read_retry
    def test_length_read_retry(self):
        self._paste()
        self.workflow['text_slots'][0]['budget_ms'] = 500
        self.workflow['steps'][0]['verify']['deadline_ms'] = 500
        self.observations.values['length'] = [w.NativeReadFailure('producer', 'read', 4, 'render'), len(self.slots.value)]
        self.assertEqual(self._run().rc, 0)
        self.assertEqual(self.transport.sent, ['private_pipe'])
        self.assertEqual(self.observations.calls.count('length'), 2)
        self._fresh_invocation()
        self.observations.values['length'] = [w.NativeReadFailure('producer', 'read', 4, 'render')]
        self._reject(self._run(), 'DEADLINE', sends=1)
        self.assertEqual(self.transport.sent, ['private_pipe'])

    # @anchor:wa.tests.mw_run_unit.TestWorkflow.test_chip_key_budget
    def test_chip_key_budget(self):
        self._chip_workflow()
        self.resources.denied = 'chip_identity'
        self._reject(self._run(), 'OUT_OF_SUPPORT')
        self._fresh_invocation()
        self.resources.denied = None
        original = self.capabilities.target
        def exhausted(spec, state, deadline):
            value = original(spec, state, deadline)
            state.run.retained = self.limits.retained_bytes
            return value
        with patch.object(self.capabilities, 'target', exhausted):
            self._reject(self._run(), 'OUT_OF_SUPPORT')
        self._fresh_invocation()
        self.assertEqual(self._run().rc, 0)

    # @anchor:wa.tests.mw_run_unit.TestWorkflow.test_prepared_grounding_deadline
    def test_prepared_grounding_deadline(self):
        calls = self._identity_fixture('scroll', {'point': '10,10', 'dir': 'down'}, target=False)
        original = self.owners.transport
        with patch.object(self.capabilities, 'validate_target', side_effect=[YES, NO]):
            self._reject(self._run(), 'OUT_OF_SUPPORT')
        self.assertEqual(calls, [('plan', None)])
        self.assertNotIn(b'"kind":"intent"', self.journal.raw)
        self._fresh_invocation()
        calls.clear()
        self.workflow['steps'][0]['action']['delivery_budget_ms'] = 1
        def expired(action, target):
            plan = original.planner(action, target)
            self.clock.sleep(2_000_000)
            return plan
        self.owners = replace(self.owners, transport=replace(original, planner=expired))
        self._reject(self._run(), 'DEADLINE')
        self.assertEqual(calls, [('plan', None)])
        self.assertNotIn(b'"kind":"intent"', self.journal.raw)
        self._fresh_invocation()
        self.owners = replace(self.owners, transport=original)
        calls.clear()
        self.assertEqual(self._run().rc, 0)
        self.assertEqual(calls, [('plan', None), ('scroll', {'point': '10,10', 'dir': 'down'})])


if __name__ == '__main__':
    unittest.main()
