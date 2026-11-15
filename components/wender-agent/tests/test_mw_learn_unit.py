# @module {"id":"wa.tests.mw_learn_unit","layer":4,"owner":"LEARN","deps":["wa.learn.mw_learn","wa.tools.mw_learn","wa.learn.mw_guidance","wa.scripts.types"],"book":"HB-WA-s4-learn"}
"""Synthetic RAM recordings, genuine validation, and public-API removal mutants."""
from dataclasses import fields, replace
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import contextlib
import numpy as np
import sys

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools import memoryweb_gate as g
from tools import mw_learn as cli
from wender.learn import mw_learn as w
from wender.learn import demonstrations as d
from wender.learn import mw_guidance as guidance
from wender.scripts.types import AdmissionCheck, Truth, Reason
from unittest.mock import Mock
from tests.test_demo_learning_unit import png
from tests.test_memoryweb_gate_unit import _fixture, _step, _pred, _explanation, _accept


# In-memory removal mutants; each source anchor must occur exactly once.
MUTANTS = {
    'owners': ('type(owners) is LearnOwners', 'True', 'test_input_prerequisites'),
    'explanation': ('_pass(gate.check_explanation(Path(inputs.explanation)))', 'True', 'test_explanation_gate'),
    'seal': ('current[2:] == (identity, digest)', 'True', 'test_custody_and_seals'),
    'containment': ('path == path.resolve()', 'True', 'test_custody_and_seals'),
    'projection': ("1 <= association['demo_seq'] <= len(pack.steps)", 'True', 'test_sparse_association'),
    'frame': ("association['frame_sha256'] == evidence.before[1]", 'True', 'test_candidate_predicates'),
    'mark': ("selected.get('missing_reason') is None", 'True', 'test_t2_missingness_not_bypassed'),
    'ocr': ("mark['kind'] == target['mark_kind'] and mark.get('ocr_text') is not None and\n                mark['ocr_text'] == target['ocr_label']", 'True', 'test_t2_fallback_independent_grounding'),
    'box': ('rx0 <= box[0] and ry0 <= box[1] and box[2] <= rx1 and box[3] <= ry1', 'True', 'test_t2_search_region_containment'),
    'integer': ("all(type(mark.get(k)) is int for k in ('x', 'y', 'w', 'h', 'score_ppm'))", 'True', 'test_integer_evidence'),
    'geometry': ("0 <= mark['x'] < mark['x'] + mark['w'] <= width and\n              0 <= mark['y'] < mark['y'] + mark['h'] <= height and 0 <= mark['score_ppm'] <= 1000000", 'True', 'test_integer_evidence'),
    'author_closed': ("type(obj) is dict and set(obj) == {'workflow', 'site_markdown', 'step_evidence'} and\n          type(obj['workflow']) is dict and type(obj['site_markdown']) is str and type(obj['step_evidence']) is list", 'True', 'test_strict_decoding'),
    'draft': ("workflow['status'] == 'draft' and workflow['admission_basis'] == 'operator_demo'\n          and workflow['script_ref'] is None and workflow['script_sha256'] is None", 'True', 'test_candidate_predicates'),
    'stats': ("all(value is None for value in workflow['stats'].values())", 'True', 'test_candidate_predicates'),
    'oracle': ("oracle['postconditions'] and oracle['mutants_killed'] == 0 and oracle['mutants_total'] == 0\n          and not oracle['receipt_refs'] and not oracle['planted_mutant_ids']", 'True', 'test_candidate_predicates'),
    'constraints': ("workflow['execution_policy'] == constraints['execution_policy'] and\n          workflow['text_slots'] == constraints['text_slots'] and\n          oracle['postconditions'] == constraints['postconditions']", 'True', 'test_candidate_predicates'),
    'protected': ("all(any(s['seq'] == seq and s['protected'] is True for s in workflow['steps'])\n              for seq in constraints['protected'])", 'True', 'test_candidate_predicates'),
    'semantic_target': ("target.get('rung') in ('T2', 'T3')", 'True', 'test_candidate_predicates'),
    'verify': ("step['verify']['mode'] in ('state_signal', 'side_effect') and step['verify']['predicate'] is not None", 'True', 'test_candidate_predicates'),
    'artifact': ("len(_bytes(workflow)) <= 65536 and len(reply.site_markdown.encode('utf-8')) <= 8192", 'True', 'test_candidate_predicates'),
    'privacy': ('_pass(owners.disclosure(hashlib.sha256(raw).hexdigest(), owners.scope,\n                                owners.authority, destination, purpose, owners.revision))', 'True', 'test_privacy_revision_and_resource_fences'),
    'revision': ('_pass(owners.revision_check(owners.revision))', 'True', 'test_privacy_revision_and_resource_fences'),
    'cancel': ('owners.cancelled() is False', 'True', 'test_privacy_revision_and_resource_fences'),
    'deadline': ('clock() < deadline', 'True', 'test_privacy_revision_and_resource_fences'),
    'peak': ('policy.memory_bytes >= peak', 'True', 'test_limits_pins_and_fences'),
    'reserve': ("\n    _need(_pass(owners.resources.reserve(peak)), 'CAPACITY', 2)", "\n    _need(True, 'CAPACITY', 2)", 'test_privacy_revision_and_resource_fences'),
    'router_query': ("inputs.router_query is None or len(inputs.router_query.encode('utf-8')) <= 512", 'True', 'test_router_bounds_and_omission'),
    'repair': ('attempt == 0', 'True', 'test_sparse_association'),
    'input_pin': ("manifest['steps_file'] == 'steps.json' and manifest['steps_sha256'] == hashlib.sha256(projection.payload).hexdigest()", 'True', 'test_limits_pins_and_fences'),
    'collision': ('not destination.exists() and not destination.is_symlink()', 'True', 'test_artifact_limits_and_publication'),
    'context': ('type(context) is gate.ValidationContext and Path(context.root).resolve() == Path(staging).resolve()', 'True', 'test_context_binding'),
    'child_bound': ('total <= cap', 'True', 'test_public_transport_limits_and_drain'),
    'strict_json': ('return gate.strict_load(raw)', "return __import__('json').loads(raw)", 'test_strict_decoding'),
    'genuine_gate': ('result = gate.validate_directory(Path(staging), _json(pack.schema), context=context)', "result = gate.GateResult(0, 'PASS')", 'test_candidate_predicates'),
    'exact_callback': ("type(value) is gate.GateResult and type(value.rc) is int\n            and value.rc == 0 and value.category == 'PASS'", 'bool(value)', 'test_privacy_revision_and_resource_fences'),
    'atomic': ('os.fsencode(destination), 1)', 'os.fsencode(destination), 0)', 'test_atomic_collision_race'),
    'response_limit': ('type(answer) is bytes and len(answer) <= policy.response_bytes', 'type(answer) is bytes', 'test_boundary_sizes_and_backend_effort'),
    'ocr_digest': ("type(value) is dict and value.get('frame_sha256') == digest", 'True', 'test_observed_digest_binding'),
    'ram_after': ('after_ref = (str(recording.root / after), hashes[after].hex()) if after in hashes else None', 'after_ref = (str(recording.root / before), hashes[before].hex())', 'test_missing_after_and_symbolic_slots'),
    'drain': ("\n            _need(_pass(owners.resources.drain(policy.drain_ns)), 'DRAIN')", "\n            _need(True, 'DRAIN')", 'test_privacy_revision_and_resource_fences'),
    'no_effect': ('        peak = _policy(policy, owners)\n        _need(_pass(owners.resources.reserve(peak))', "        __import__('wender.learn.demo_replay', fromlist=['replay']).replay(None, ())\n        peak = _policy(policy, owners)\n        _need(_pass(owners.resources.reserve(peak))", 'test_positive_genuine_gate_and_no_effects'),
    'pixel_export': ('                        _write(adjacent, artifacts)', "                        shutil.copyfile(pack.images[0][0], adjacent / 'frame.png')\n                        _write(adjacent, artifacts)", 'test_positive_genuine_gate_and_no_effects'),
    'policy': ('type(getattr(policy, f.name)) is int and getattr(policy, f.name) > 0', 'True', 'test_limits_pins_and_fences'),
    'input_capacity': ('stat.S_ISREG(before.st_mode) and before.st_size <= cap', 'stat.S_ISREG(before.st_mode)', 'test_boundary_sizes_and_backend_effort'),
    'schema_pin': ('hashlib.sha256(schema).hexdigest() == gate.SCHEMA_PIN', 'True', 'test_private_response_export_and_input_schema'),
    'input_schema': ("gate.conforms(manifest, defs['demo'], defs) and gate.conforms(site, defs['site'], defs)", 'True', 'test_private_response_export_and_input_schema'),
    'projection_cap': ('len(projection.payload) <= 65536', 'True', 'test_remaining_spec_ceiling_controls'),
    'router_cap': ('len(data) <= 2000', 'True', 'test_remaining_spec_ceiling_controls'),
    'router_shape': ("type(obj) is dict and type(obj.get('hits')) is list", 'True', 'test_optional_router_passive_controls'),
    'gate_result': ('type(result) is gate.GateResult and type(result.rc) is int', 'True', 'test_output_escape_and_validator_result'),
    'cli_references': ("'references': list(result.references)", "'references': result.references", 'test_cli_injected_owners_success'),
    'cli_refusal_references': ("'references': list(result.references)", "'references': result.references", 'test_cli_injected_owners_refusals'),
    'cli_rc': ('return result.rc', 'return 0', 'test_cli_injected_owners_refusals'),
    'cli_ve_root': ("parser.add_argument('--ve-root', type=Path)", "parser.add_argument('--ve-root')", 'test_cli_injected_owners_success'),
    'unique': ('count == 1', 'count >= 1', 'test_t2_search_region_uniqueness'),
    'primary': ("type(association['mark_id']) is str and\n                      association['mark_id'] == str(mark['id']) and association['missing_reason'] is None", 'True', 'test_t2_primary_association_binding'),
    'diagnostic_constant': ('diagnostic = _repair_diagnostic(validation.category)', "diagnostic = 'CANDIDATE_REJECTED'", 'test_repair_bound_and_frozen_prompt'),
    'diagnostic_passthrough': ("category if type(category) is str and category in _REPAIR_DIAGNOSTICS else 'CANDIDATE_REJECTED'", 'category', 'test_repair_diagnostic_allowlist'),
    'equality_only': ('rx0 <= box[0] and ry0 <= box[1] and box[2] <= rx1 and box[3] <= ry1', 'tuple(region) == box', 'test_t2_search_region_containment'),
    'shared_id': ("if target is step['target']:", 'if True:', 'test_t2_fallback_independent_grounding'),
    'shared_destination': ("if target is step['target']:", 'if True:', 'test_t2_destination_independent_grounding'),
    'region_type': ('type(region) is list and len(region) == 4 and\n          all(type(v) is int and 0 <= v <= 10000 for v in region)', 'True', 'test_t2_floor_boundary_and_region_types'),
    'region_order': ('rx0 < rx1 and ry0 < ry1', 'True', 'test_t2_floor_boundary_and_region_types'),
    'floor': ("(mark['y'] + mark['h']) * 10000 // evidence.height", "((mark['y'] + mark['h']) * 10000 + evidence.height - 1) // evidence.height", 'test_t2_floor_boundary_and_region_types'),
    'high_downgrade': ("'--reasoning-effort', policy.effort", "'--reasoning-effort', 'medium'", 'test_high_effort_author_transport'),
    'high_base': ("effort='medium' if effort == 'high' else effort", 'effort=effort', 'test_high_effort_author_transport'),
    'high_mismatch': ("self.effort == policy.effort and self.effort in ('medium', 'high', 'xhigh')", "self.effort in ('medium', 'high', 'xhigh')", 'test_high_effort_author_transport'),
    'cli_high': ("choices=('medium', 'high', 'xhigh')", "choices=('medium', 'xhigh')", 'test_cli_high_effort'),
    'closed_association': ("type(association) is dict and set(association) == {'seq', 'demo_seq', 'frame_sha256', 'mark_id', 'missing_reason'}", 'type(association) is dict', 'test_grounding_inventory_and_closed_association'),
    'request_limit': ("_need(len(raw) <= policy.request_bytes, 'REQUEST_LIMIT')", "_need(True, 'REQUEST_LIMIT')", 'test_repair_admission_and_budget_controls'),
    'nonretry': ("validation.category not in ('PRIVACY', 'CONTAINMENT', 'CAPACITY', 'CONTEXT_REQUIRED', 'INPUT_PIN', 'CAPABILITY_MISSING', 'EVIDENCE_MISSING', 'REVIEW_MISSING', 'EVIDENCE_DENIED', 'EVIDENCE_OWNER')", 'True', 'test_repair_admission_and_budget_controls'),
    'missing_ambiguity': ("if not (mark['kind'] == target['mark_kind']", "if mark.get('missing_reason') is not None or not (mark['kind'] == target['mark_kind']", 'test_t2_missingness_not_bypassed'),
    'inventory_decode': ("mark = _ground_t2(target, evidence, marks)", "mark = _ground_t2(target, evidence, _json(evidence.ocr).get('marks', []))", 'test_grounding_inventory_and_closed_association'),
}

MUTANTS.update({
    'guidance_ref_length': ('type(value) is str and len(value) <= 400', 'True', 'test_guidance_reference_schema'),
    'guidance_ref_numbers': ("len(nums) == 6 and all(re.fullmatch(r'0|[1-9][0-9]*', n) for n in nums)\n          and int(step) > 0 and re.fullmatch('[0-9a-f]{64}', digest) is not None", 'True', 'test_guidance_reference_schema'),
    'guidance_ref_box_class': ("classification in ('before_input', 'outside_expected_after_quiet') and\n          0 <= coords[0] < coords[2] <= 2147483647 and\n          0 <= coords[1] < coords[3] <= 2147483647", 'True', 'test_guidance_reference_schema'),
    'guidance_ref_recorder': ("re.fullmatch(r'[a-zA-Z0-9_-]{1,80}', recorder[0]) is not None and recorder[0] != 'None'", 'True', 'test_guidance_reference_schema'),
    'guidance_ref_suffix': ('bool(recorder)', 'True', 'test_guidance_missing_demo_is_non_evidence'),
    'guidance_owner_shape_all': ("all(type(x) is str and re.fullmatch('[0-9a-f]{64}', x) for x in\n              (owner.journal_sha256, owner.source_workflow_sha256)) and\n          type(owner.step_count) is int and owner.step_count > 0 and type(owner.bindings) is tuple and\n          all(callable(x) for x in (owner.validate_payload, owner.check,\n              getattr(owner.journal, 'read', None), getattr(owner.journal, 'authenticate', None)))", 'True', 'test_guidance_owner_controls'),
    'guidance_binding_shape': ("type(b) is GuidanceBinding and type(b.step_seq) is int and\n              1 <= b.step_seq <= owner.step_count and type(b.demo_seq) is int and b.demo_seq > 0 and\n              type(b.demo_ref) is str and re.fullmatch(r'[a-zA-Z0-9_-]{1,80}', b.demo_ref)", 'True', 'test_guidance_owner_controls'),
    'guidance_event_keys': ("set(event) == keys | ({'guidance'} if kind == 'operator_guidance' else set())", 'True', 'test_guidance_journal_integrity'),
    'guidance_event_chain': ("type(event['seq']) is int and event['seq'] == seq and event['prev'] == prev and\n                  type(event['digest']) is str and re.fullmatch('[0-9a-f]{64}', event['digest']) and\n                  line == _bytes(event)", 'True', 'test_guidance_journal_integrity'),
    'guidance_event_state': ("kind in ('intent', 'complete', 'verified', 'satisfied_skip', 'condition_skip', 'failed', 'operator_guidance') and\n                  type(key) is int and 1 <= key <= owner.step_count and\n                  event['phase'] == ('delivery' if kind in ('intent', 'complete') else 'terminal')", 'True', 'test_guidance_journal_state_controls'),
    'guidance_intent_unique': ('key not in outstanding', 'True', 'test_guidance_journal_state_controls'),
    'guidance_complete_pair': ("_need(key in outstanding, 'GUIDANCE_JOURNAL', 3)\n                outstanding.remove(key)", 'outstanding.discard(key)', 'test_guidance_journal_state_controls'),
    'guidance_terminal_intent': ("_need(not outstanding, 'GUIDANCE_JOURNAL', 3)\n        metadata =", 'pass\n        metadata =', 'test_guidance_journal_state_controls'),
    'guidance_auth': ('type(auth) is AdmissionCheck and auth.result is Truth.TRUE and auth.reason is None', 'True', 'test_guidance_journal_authentication'),
    'guidance_validator': ('owner.validate_payload(payload) is True', 'True', 'test_guidance_payload_validator_is_real'),
    'guidance_recorder': ("payload['demo_ref'] is not None", 'True', 'test_guidance_missing_demo_is_non_evidence'),
    'guidance_association': ("event['step_seq'] == seq and payload['time_ns'] == stamp and payload['box_px'] == box and\n              payload['classification'] == classification and payload['demo_ref'] == demo_ref", 'True', 'test_guidance_ref_accepted_as_repair_evidence'),
    'guidance_source': ("_pass(checked)", 'True', 'test_guidance_source_and_recorder_custody'),
    'guidance_limits': ("_need(len(raw) <= policy.request_bytes, 'CAPACITY', 2)", "_need(True, 'CAPACITY', 2)", 'test_guidance_capacity_before_producer'),
    'guidance_recheck': ("owners.guidance.journal_sha256 == pack.guidance_journal_sha256", 'True', 'test_guidance_revalidation'),
    'guidance_candidate': ("len(steps) == 1 and len(associations) == 1 and\n              associations[0].get('demo_seq') == guided.demo_seq", 'True', 'test_guidance_candidate_association'),
})

MUTANTS.update({
    'cli_guidance_fields': ("if k != 'guidance_refs'", 'if True', 'test_cli_injected_owners_success'),
    'guidance_validator_identity': ('owner.validate_payload is mw_guidance.validate_payload', 'True', 'test_guidance_validator_identity'),
    'guidance_layer': ('"layer":4', '"layer":2', 'test_guidance_layer_contract'),
    'guidance_dependency': ('"wa.learn.mw_guidance",', '', 'test_guidance_layer_contract'),
    'guidance_default': ('guidance_refs: tuple[str, ...] = ()', "guidance_refs: tuple[str, ...] = ('unexpected',)", 'test_guidance_inputs_cli_compatibility'),
    'guidance_iteration': ('class LearnInputs:', "class LearnInputs(metaclass=type('_HiddenFields', (type,), {'__setattr__': lambda cls, name, value: type.__setattr__(cls, name, type('_Fields', (dict,), {'__iter__': lambda obj: (k for k in dict.__iter__(obj) if k != 'guidance_refs')})(value) if name == '__dataclass_fields__' else value)})):", 'test_guidance_inputs_cli_compatibility'),
    'guidance_candidate_steps_unique': ('len(steps) == 1', 'bool(steps)', 'test_guidance_candidate_conflicts'),
    'guidance_candidate_rows_unique': ('len(associations) == 1', 'bool(associations)', 'test_guidance_candidate_conflicts'),
    'guidance_grammar': ("len(parts) in (6, 7) and parts[0] == 'operator_guidance'", 'True', 'test_guidance_reference_schema'),
    'guidance_owner_type': ("type(owner) is GuidanceIntakeOwner", 'True', 'test_guidance_owner_controls'),
    'guidance_owner_shape': ("type(owner.step_count) is int and owner.step_count > 0", 'True', 'test_guidance_owner_controls'),
    'guidance_binding_unique': ("b.step_seq not in seen", 'True', 'test_guidance_owner_controls'),
    'guidance_refs_unique': ("len(set(refs)) == len(refs)", 'True', 'test_guidance_duplicate_groups'),
    'guidance_boxes': ("box not in boxes", 'True', 'test_guidance_duplicate_groups'),
    'guidance_pairs': ("pair not in pairs", 'True', 'test_guidance_duplicate_groups'),
    'guidance_snapshot': ("raw and raw.endswith(b'\\n') and hashlib.sha256(raw).hexdigest() == owner.journal_sha256", 'True', 'test_guidance_journal_integrity'),
    'guidance_event_count': ("len(lines) <= policy.demo_limits.events", 'True', 'test_guidance_capacity_before_producer'),
    'guidance_ref_count': ("len(refs) <= policy.demo_limits.events", 'True', 'test_guidance_capacity_before_producer'),
    'guidance_binding_count': ("len(owner.bindings) <= policy.demo_limits.events", 'True', 'test_guidance_capacity_before_producer'),
    'guidance_metadata': ("metadata <= policy.request_bytes", 'True', 'test_guidance_capacity_before_producer'),
    'guidance_revalidation_removed': ("if not pack.guidance:\n        return", 'if True:\n        return', 'test_guidance_revalidation'),
})

for _name, _base, _test in (
    ('guidance_oracle', 'oracle', 'test_guidance_draft_oracle_unchanged'),
    ('guidance_privacy', 'privacy', 'test_guidance_privacy_and_cleanup'),
    ('guidance_cancel', 'cancel', 'test_guidance_fences'),
    ('guidance_revision', 'revision', 'test_guidance_fences'),
    ('guidance_deadline', 'deadline', 'test_guidance_fences'),
):
    MUTANTS[_name] = (*MUTANTS[_base][:2], _test)

if os.environ.get("MWL1_MUTANT"):
    old, new, _ = MUTANTS[os.environ["MWL1_MUTANT"]]
    target = cli if os.environ['MWL1_MUTANT'].startswith('cli_') else w
    source = Path(target.__file__).read_text()
    if source.count(old) != 1:
        raise RuntimeError("mutant anchor absent")
    source = source.replace(old, new, 1)
    MUTATED_SOURCE = source
    if os.environ['MWL1_MUTANT'] == 'input_capacity':
        source = source.replace("_need(len(raw) <= cap, 'CAPACITY', 2)", 'pass')
        source = source.replace('min(65536, cap + 1 - len(raw))', '65536')
    exec(compile(source, target.__file__, "exec"), target.__dict__)


# @anchor:wa.learn.mwl1_tests.Resources
class Resources:
    # @anchor:wa.learn.mwl1_tests.Resources.__init__
    def __init__(self):
        self.held = 0
        self.denied = False
        self.drained = True

    # @anchor:wa.learn.mwl1_tests.Resources.reserve
    def reserve(self, amount):
        if self.denied or self.held:
            return g.GateResult(5, 'DENIED')
        self.held = amount
        return _accept()

    # @anchor:wa.learn.mwl1_tests.Resources.release
    def release(self):
        self.held = 0

    # @anchor:wa.learn.mwl1_tests.Resources.drain
    def drain(self, deadline):
        return _accept() if self.drained else g.GateResult(5, 'DENIED')


# @anchor:wa.learn.mwl1_tests.Backend
class Backend:
    # @anchor:wa.learn.mwl1_tests.Backend.__init__
    def __init__(self, fixture):
        self.fixture, self.calls, self.answers, self.hook = fixture, [], [], None

    # @anchor:wa.learn.mwl1_tests.Backend.author
    def author(self, request, **kwargs):
        self.calls.append(copy.deepcopy(request))
        if self.hook:
            self.hook()
        answer = self.answers.pop(0) if self.answers else self.fixture.envelope
        return answer if type(answer) is bytes else w._bytes(answer)


# @anchor:wa.learn.mwl1_tests.Describer
class Describer:
    # @anchor:wa.learn.mwl1_tests.Describer.describe
    def describe(self, frame, *, prev=None):
        raw = Path(frame['path']).read_bytes()
        digest = hashlib.sha256(d._png(raw, d.DemoLimits()).tobytes()).hexdigest()
        return dict(frame_sha256=digest, width=192, height=120, marks=[dict(
            id=9, x=80, y=40, w=40, h=30, kind='button', score_ppm=900000,
            ocr_text='Continue', ocr_score_promille=950, missing_reason=None)])


# @anchor:wa.s4.mwl1g_tests.GuidanceJournal
class GuidanceJournal:
    """Test-only canonical hash framing, never a production framing claim."""
    # @anchor:wa.s4.mwl1g_tests.GuidanceJournal.__init__
    def __init__(self, events):
        self.events = [copy.deepcopy(event) for event in events]
        self.reads = 0
        self.auth = None
        self.raw = b''
        self.seal()

    # @anchor:wa.s4.mwl1g_tests.GuidanceJournal.seal
    def seal(self):
        prev = None
        for seq, event in enumerate(self.events, 1):
            event.update(seq=seq, prev=prev)
            event.pop('digest', None)
            event['digest'] = hashlib.sha256(b'test-only-guidance\0' + w._bytes(event)).hexdigest()
            prev = event['digest']
        self.raw = b''.join(w._bytes(e) for e in self.events)

    # @anchor:wa.s4.mwl1g_tests.GuidanceJournal.read
    def read(self, budget):
        self.reads += 1
        return self.raw

    # @anchor:wa.s4.mwl1g_tests.GuidanceJournal.authenticate
    def authenticate(self, event):
        if self.auth is not None:
            return self.auth
        unsigned = dict(event)
        digest = unsigned.pop('digest')
        valid = hashlib.sha256(b'test-only-guidance\0' + w._bytes(unsigned)).hexdigest() == digest
        return AdmissionCheck(Truth.TRUE if valid else Truth.FALSE, None)


# @anchor:wa.learn.mwl1_tests.LearnTests
class LearnTests(unittest.TestCase):
    # @anchor:wa.learn.mwl1_tests.LearnTests.setUp
    def setUp(self):
        parent = Path('/dev/shm/vqa-ramws')
        parent.mkdir(exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=parent, prefix='mwl1-test-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.demo = self.root / 'recording'
        self.demo.mkdir()
        before = np.random.default_rng(2).integers(0, 256, (120, 192, 3), dtype=np.uint8)
        after = before.copy()
        after[45:55, 90:100] = 255 - after[45:55, 90:100]
        self.row = dict(seq=7, mono_ns=1000, window={'id': 'w', 'w': 192, 'h': 120}, kind='click',
                        x=96, y=50, button=1, x2=None, y2=None, dir=None, keysym=None, text_len=None, modifiers=[])
        (self.demo / 'events.jsonl').write_text(json.dumps(self.row) + '\n')
        (self.demo / '7_before.png').write_bytes(png(before))
        (self.demo / '7_after.png').write_bytes(png(after))
        (self.demo / 'final.png').write_bytes(png(after))
        self.projection = d.project_demo_steps(d.segment_demo(d.load_demo(self.demo, limits=d.DemoLimits())), demo_id='demo', site_id='site')
        (self.root / 'EXPLANATION.md').write_bytes(_explanation())
        (self.root / 'guide.md').write_text('Reviewed authoring guide. All observations are passive.')
        self.output = self.root / 'output'
        self.output.mkdir()
        demo = _fixture('demo')
        demo.update(demo_id='demo', site_id='site', steps_file='steps.json',
                    steps_sha256=hashlib.sha256(self.projection.payload).hexdigest())
        demo['explanation']['sha256'] = hashlib.sha256(_explanation()).hexdigest()
        demo['provenance']['source_ref'] = 'demos/demo/EXPLANATION.md'
        site = _fixture('site')
        site.update(id='site', status='draft')
        site['provenance']['source_ref'] = 'SITE.md'
        workflow = _fixture('workflow')
        workflow.update(id='work', site_id='site', status='draft', steps=[_step()])
        workflow['fallback'] = {'kind': 'ask_operator', 'workflow_id': None}
        workflow['provenance']['source_ref'] = 'demos/demo/EXPLANATION.md'
        workflow['oracle']['postconditions'] = [_pred()]
        self.constraints = w._bytes(dict(execution_policy=workflow['execution_policy'], postconditions=workflow['oracle']['postconditions'],
                                         protected=[], text_slots=[]))
        (self.root / 'manifest.json').write_bytes(w._bytes(demo))
        (self.root / 'site.json').write_bytes(w._bytes(site))
        self.inputs = w.LearnInputs(self.demo, self.root / 'EXPLANATION.md', self.root / 'manifest.json',
                                    self.root / 'site.json', self.root / 'guide.md', g.DOCS / 'mw.v1.schema.json', self.output, 'site', 'work')
        self.policy = w.LearnPolicy(131072, 131072, 262144, 4*1024**3, 4, 4, 1, 6,
                                    10**11, 10**6, 10**9, d.DemoLimits(memory_bytes=256*1024**2, retained_bytes=128*1024**2))
        self.resources = Resources()
        self.approvals = []
        self.owners = w.LearnOwners('scope', 'authority', 'revision', self.constraints,
                                    self.context, self.approve, self.resources, _accept, lambda: False)
        digest = hashlib.sha256((self.demo / '7_before.png').read_bytes()).hexdigest()
        self.envelope = dict(workflow=workflow, site_markdown=_explanation().decode(), step_evidence=[dict(
            seq=1, demo_seq=1, frame_sha256=digest, mark_id=None, missing_reason='NOT_REQUIRED')])
        self.backend = Backend(self)
        self.describer = Describer()

    # @anchor:wa.learn.mwl1_tests.LearnTests.context
    def context(self, root):
        return g.ValidationContext(root, capability_check=_accept, evidence_check=_accept, review_check=_accept)

    # @anchor:wa.learn.mwl1_tests.LearnTests.approve
    def approve(self, *args):
        self.approvals.append(args)
        return _accept()

    def _learn(self, **kwargs):
        args = dict(policy=self.policy, owners=self.owners, backend=self.backend, describer=self.describer, clock=lambda: 100)
        args.update(kwargs)
        result = w.learn(self.inputs, **args)
        self.assertEqual(self.resources.held, 0)
        self.assertFalse(list(self.output.glob('sites/.mw-staging-*')))
        self.assertNotIn('CANARY', repr(result))
        return result

    def _fail(self, category, calls=0, **kwargs):
        result = self._learn(**kwargs)
        self.assertEqual(result.category, category, result)
        self.assertEqual(result.model_calls, calls)
        self.assertFalse((self.output / 'sites/site').exists())
        return result

    # @anchor:wa.learn.mwl1_tests.LearnTests.test_positive_genuine_gate_and_no_effects
    def test_positive_genuine_gate_and_no_effects(self):
        with patch('wender.learn.demo_replay.replay', side_effect=AssertionError('effect')):
            result = self._learn()
        self.assertEqual((result.rc, result.model_calls), (0, 1), result)
        destination = self.output / 'sites/site'
        self.assertEqual(g.validate_directory(destination, g.strict_load(self.inputs.schema.read_bytes()), context=self.context(destination)), _accept())
        files = sorted(str(p.relative_to(destination)) for p in destination.rglob('*') if p.is_file())
        self.assertEqual(files, ['SITE.md', 'demos/demo/EXPLANATION.md', 'demos/demo/manifest.json', 'demos/demo/steps.json', 'site.json', 'workflows/work.json'])
        self.assertNotIn(str(self.demo), ''.join(p.read_text() for p in destination.rglob('*') if p.is_file()))
        self.assertFalse(list(self.root.glob('mw-*')))

    # @anchor:wa.learn.mwl1_tests.LearnTests.test_input_prerequisites
    def test_input_prerequisites(self):
        for field in ('guide', 'schema', 'demo_manifest'):
            with self.subTest(field=field):
                original = self.inputs
                self.inputs = replace(original, **{field: self.root / 'absent'})
                self._fail('INPUT_MISSING')
                self.inputs = original
        self._fail('OWNERS_MISSING', owners=None)
        self.assertEqual(self._learn().rc, 0)

    # @anchor:wa.learn.mwl1_tests.LearnTests.test_explanation_gate
    def test_explanation_gate(self):
        path = self.inputs.explanation
        for raw in (b'## Intent\n\n', _explanation().replace(b'## Intent', b'## Exceptions'), _explanation().replace(b'Operator scoped evidence for intent.', b'')):
            path.write_bytes(raw)
            self._fail('EXPLANATION')
        path.write_bytes(_explanation())
        self.assertEqual(self._learn().rc, 0)

    # @anchor:wa.learn.mwl1_tests.LearnTests.test_custody_and_seals
    def test_custody_and_seals(self):
        original = self.inputs
        self.inputs = replace(original, demo_directory=Path(w.__file__).parent)
        self._fail('FILE_CUSTODY')
        self.inputs = original
        self.inputs = replace(original, guide=self.root / 'link')
        self.inputs.guide.symlink_to(original.guide)
        self._fail('CONTAINMENT')
        self.inputs = original
        self.backend.hook = lambda: self.inputs.guide.write_text('changed')
        self._fail('INPUT_CHANGED', 1)

    # @anchor:wa.learn.mwl1_tests.LearnTests.test_sparse_association
    def test_sparse_association(self):
        pack = w.build_evidence(self.inputs, policy=self.policy, owners=self.owners, describer=self.describer, clock=lambda: 100)
        self.assertEqual((pack.steps[0].seq, pack.steps[0].recorder_seq), (1, 7))
        self.envelope['step_evidence'][0]['demo_seq'] = 7
        self._fail('REPAIR_EXHAUSTED', 2)
        self.envelope['step_evidence'][0]['demo_seq'] = 1
        self.assertEqual(self._learn().rc, 0)

    def _target(self):
        self.envelope['workflow']['steps'][0]['target'] = dict(rung='T2', mark_kind='button', ocr_label='Continue', locale='en',
                                                             rel_box_bps=[4166, 3333, 6250, 5833], capability_id='synthetic-v1')
        self.envelope['step_evidence'][0].update(mark_id='9', missing_reason=None)

    # @anchor:wa.learn.mwl1_tests.LearnTests.test_ocr_grounding
    def test_ocr_grounding(self):
        self._target()
        good = copy.deepcopy(self.envelope)
        mutations = [lambda e: e['step_evidence'][0].update(mark_id='999'),
                     lambda e: e['step_evidence'][0].update(frame_sha256='a'*64),
                     lambda e: e['workflow']['steps'][0]['target'].update(ocr_label='Imagined'),
                     lambda e: e['workflow']['steps'][0]['target'].update(rel_box_bps=[0, 0, 1, 1]),
                     lambda e: e['workflow']['steps'][0]['target'].update(mark_kind='field')]
        for mutation in mutations:
            self.envelope = copy.deepcopy(good)
            mutation(self.envelope)
            self._fail('REPAIR_EXHAUSTED', 2)
        self.envelope = good
        self.assertEqual(self._learn().rc, 0)

    # @anchor:wa.learn.mwl1_tests.LearnTests.test_integer_evidence
    def test_integer_evidence(self):
        original = self.describer.describe
        for field, value in (('score_ppm', 0.9), ('x', True), ('w', -1)):
            def bad(frame, *, prev=None):
                obj = original(frame)
                obj['marks'][0][field] = value
                return obj
            with patch.object(self.describer, 'describe', bad):
                self._fail('INTEGER_EVIDENCE')
        self.assertEqual(self._learn().rc, 0)

    # @anchor:wa.learn.mwl1_tests.LearnTests.test_strict_decoding
    def test_strict_decoding(self):
        for raw in (b'{"workflow":{},"workflow":{}}', b'{"x":NaN}', b'{}{}', b'{"x":0.1}', b'\xff'):
            self.backend.answers = [raw]
            self._fail('STRICT_JSON', 1)
        self.backend.answers = [b'{"unexpected":true}']
        self._fail('AUTHOR_ENVELOPE', 1)
        self.assertEqual(self._learn().rc, 0)

    # @anchor:wa.learn.mwl1_tests.LearnTests.test_frozen_schema_and_semantic_verification
    def test_frozen_schema_and_semantic_verification(self):
        good = copy.deepcopy(self.envelope)
        self._target()
        self.envelope['workflow']['steps'][0]['target']['mark_id'] = '9'
        self._fail('REPAIR_EXHAUSTED', 2)
        self.envelope = copy.deepcopy(good)
        self.envelope['workflow']['steps'][0]['verify']['predicate'] = None
        self._fail('REPAIR_EXHAUSTED', 2)
        self.envelope = good
        bad_context = lambda root: replace(self.context(root), capability_check=None)
        self._fail('CAPABILITY_MISSING', 0, owners=replace(self.owners, context_factory=bad_context))
        self.assertEqual(self._learn().rc, 0)

    # @anchor:wa.learn.mwl1_tests.LearnTests.test_draft_and_constraints
    def test_draft_and_constraints(self):
        good = copy.deepcopy(self.envelope)
        changes = [lambda v: v.update(status='shadow'), lambda v: v['stats'].update(support_n=1),
                   lambda v: v['oracle'].update(receipt_refs=['fake']),
                   lambda v: v['execution_policy'].update(min_notice_s=9),
                   lambda v: v['oracle'].update(postconditions=[])]
        for change in changes:
            self.envelope = copy.deepcopy(good)
            change(self.envelope['workflow'])
            self._fail('REPAIR_EXHAUSTED', 2)
        self.envelope = good
        self.assertEqual(self._learn().rc, 0)

    # @anchor:wa.learn.mwl1_tests.LearnTests.test_repair_bound_and_frozen_prompt
    def test_repair_bound_and_frozen_prompt(self):
        bad = copy.deepcopy(self.envelope)
        bad['workflow']['status'] = 'shadow'
        self.backend.answers = [bad, self.envelope]
        result = self._learn()
        self.assertEqual((result.rc, result.model_calls), (0, 2))
        a, b = self.backend.calls
        for key in ('trusted_instructions', 'guide', 'schema', 'constraints', 'passive_data'):
            self.assertEqual(a[key], b[key])
        self.assertEqual(b['diagnostic'], 'DRAFT_ONLY')
        self.assertEqual(a['output_identity'], b['output_identity'])

    # @anchor:wa.learn.mwl1_tests.LearnTests.test_privacy_revision_and_resource_fences
    def test_privacy_revision_and_resource_fences(self):
        self._fail('PRIVACY', owners=replace(self.owners, disclosure=lambda *a: True))
        self._fail('REVISION', owners=replace(self.owners, revision_check=lambda *a: g.GateResult(5, 'DENY')))
        self.resources.denied = True
        with self.assertRaises(w._Stop) as error:
            w.build_evidence(self.inputs, policy=self.policy, owners=self.owners, describer=self.describer, clock=lambda:100)
        self.assertEqual(error.exception.category,'CAPACITY')
        self._fail('CAPACITY')
        self.resources.denied = False
        self._fail('CANCELLED', owners=replace(self.owners, cancelled=lambda: True))
        self._fail('DEADLINE', clock=iter([0, 0, self.policy.deadline_ns]).__next__)
        self.resources.drained = False
        with self.assertRaises(w._Stop) as error:
            w.build_evidence(self.inputs,policy=self.policy,owners=self.owners,describer=self.describer,clock=lambda:100)
        self.assertEqual(error.exception.category,'DRAIN')
        self.backend.answers = [b'{}']
        self._fail('DRAIN', 1)
        self.resources.drained = True
        self.assertEqual(self._learn().rc, 0)

    # @anchor:wa.learn.mwl1_tests.LearnTests.test_router_bounds_and_omission
    def test_router_bounds_and_omission(self):
        original = self.inputs
        self.inputs = replace(original, router_query='CANARY'*100, router_root=self.root)
        self._fail('ROUTER_QUERY')
        self.inputs = replace(original, router_query='query', router_root=self.root)
        pack = w.build_evidence(self.inputs, policy=self.policy, owners=self.owners, describer=self.describer, clock=lambda: 100)
        self.assertIsNone(pack.router)
        self.assertIn((None, 'ROUTER_UNAVAILABLE'), pack.omissions)
        self.assertEqual(self._learn().rc, 0)

    # @anchor:wa.learn.mwl1_tests.LearnTests.test_artifact_limits_and_publication
    def test_artifact_limits_and_publication(self):
        good = self.envelope['site_markdown']
        self.envelope['site_markdown'] = 'x'*8193
        self._fail('REPAIR_EXHAUSTED', 2)
        self.envelope['site_markdown'] = good
        with patch.object(w, '_publish', side_effect=OSError('CANARY')):
            self._fail('PIPELINE_REFUSED', 1)
        self.assertEqual(self._learn().rc, 0)
        result = self._learn()
        self.assertEqual((result.category, result.model_calls), ('DESTINATION_EXISTS', 0))

    # @anchor:wa.s4.mwl1h_tests.LearnTests._cli_argv
    def _cli_argv(self):
        argv = []
        for name in w.LearnInputs.__dataclass_fields__:
            if name == 'guidance_refs':
                continue
            value = getattr(self.inputs, name)
            if value is not None:
                argv += ['--'+name.replace('_', '-'), str(value)]
        for name in w.LearnPolicy.__dataclass_fields__:
            if name not in ('demo_limits', 'effort'):
                argv += ['--'+name.replace('_', '-'), str(getattr(self.policy, name))]
        return argv

    # @anchor:wa.learn.mwl1_tests.LearnTests.test_cli_missing_owners_and_bounded_diagnostic
    def test_cli_missing_owners_and_bounded_diagnostic(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            rc = cli.main(self._cli_argv())
        self.assertEqual(rc, 2)
        self.assertEqual(json.loads(output.getvalue())['category'], 'OWNERS_MISSING')
        self.assertNotIn(str(self.root), output.getvalue())

    # @anchor:wa.learn.mwl1_tests.LearnTests.test_cli_injected_owners_success
    def test_cli_injected_owners_success(self):
        output = io.StringIO()
        argv = self._cli_argv() + ['--ve-root', str(self.root)]
        with contextlib.redirect_stdout(output), patch.object(cli, 'learn', wraps=w.learn) as invoked, \
                patch.object(g, 'canon', wraps=g.canon) as canonical:
            rc = cli.main(argv, owners=self.owners, backend=self.backend, describer=self.describer)
        self.assertIs(type(canonical.call_args.args[0]['references']), list)
        self.assertEqual(rc, 0, output.getvalue())
        self.assertEqual(json.loads(output.getvalue()), dict(
            rc=0, category='DRAFT_WRITTEN', references=['sites/site/'], model_calls=1, usage=None))
        self.assertEqual(invoked.call_args.args[0].ve_root, self.root)
        self.assertIsInstance(invoked.call_args.args[0].ve_root, Path)
        self.assertEqual(len(self.backend.calls), 1)
        self.assertTrue((self.output / 'sites/site/workflows/work.json').is_file())
        self.assertNotIn(str(self.root), output.getvalue())
        self.assertEqual(self.resources.held, 0)

    # @anchor:wa.learn.mwl1_tests.LearnTests.test_cli_injected_owners_refusals
    def test_cli_injected_owners_refusals(self):
        for fixture, expected_rc, category, calls in (
                ('missing', 2, 'INPUT_MISSING', 0),
                ('pin', 3, 'INPUT_PIN', 0),
                ('backend', 5, 'PIPELINE_REFUSED', 1)):
            with self.subTest(fixture=fixture), contextlib.ExitStack() as stack:
                self.backend.calls.clear()
                original = self.inputs.demo_manifest.read_bytes()
                if fixture == 'missing':
                    stack.enter_context(patch.object(self, 'inputs', replace(
                        self.inputs, guide=self.root / 'CANARY_MISSING_GUIDE')))
                elif fixture == 'pin':
                    manifest = json.loads(original)
                    manifest['steps_sha256'] = '0' * 64
                    self.inputs.demo_manifest.write_bytes(w._bytes(manifest))
                    stack.callback(self.inputs.demo_manifest.write_bytes, original)
                else:
                    stack.enter_context(patch.object(self.backend, 'author', side_effect=RuntimeError('CANARY_BACKEND')))
                output = io.StringIO()
                with contextlib.redirect_stdout(output), patch.object(g, 'canon', wraps=g.canon) as canonical:
                    rc = cli.main(self._cli_argv(), owners=self.owners,
                                  backend=self.backend, describer=self.describer)
                self.assertIs(type(canonical.call_args.args[0]['references']), list)
                self.assertEqual(rc, expected_rc, output.getvalue())
                self.assertEqual(json.loads(output.getvalue()), dict(
                    rc=expected_rc, category=category, references=[], model_calls=calls, usage=None))
                self.assertFalse((self.output / 'sites/site').exists())
                self.assertNotIn('CANARY', output.getvalue())
                self.assertNotIn(str(self.root), output.getvalue())
                self.assertEqual(self.resources.held, 0)

    def _validate(self, envelope=None, *, pack=None):
        pack = pack or w.build_evidence(self.inputs, policy=self.policy, owners=self.owners, describer=self.describer, clock=lambda: 100)
        with tempfile.TemporaryDirectory(dir=self.root) as directory:
            staging = Path(directory) / 'site'
            staging.mkdir()
            return w.validate_candidate(w.AuthorReply(**(envelope or self.envelope)), pack=pack, inputs=self.inputs,
                                        policy=self.policy, owners=self.owners, staging=staging)

    # @anchor:wa.learn.mwl1_tests.LearnTests.test_candidate_predicates
    def test_candidate_predicates(self):
        self._target()
        pack = w.build_evidence(self.inputs, policy=self.policy, owners=self.owners, describer=self.describer, clock=lambda: 100)
        good = copy.deepcopy(self.envelope)
        cases = [
            ('DRAFT_BINDING', lambda v: v['workflow'].update(id='other')),
            ('DRAFT_ONLY', lambda v: v['workflow'].update(status='promoted')),
            ('DRAFT_ONLY', lambda v: v['workflow'].update(admission_basis='dream')),
            ('DRAFT_ONLY', lambda v: v['workflow'].update(script_ref='script.py')),
            ('UNKNOWN_STATS', lambda v: v['workflow']['stats'].update(support_n=1)),
            ('DRAFT_ORACLE', lambda v: v['workflow']['oracle'].update(receipt_refs=['fabricated'])),
            ('FROZEN_CONSTRAINTS', lambda v: v['workflow']['execution_policy'].update(min_notice_s=99)),
            ('FROZEN_CONSTRAINTS', lambda v: v['workflow'].update(text_slots=[{}])),
            ('FROZEN_CONSTRAINTS', lambda v: v['workflow']['oracle'].update(postconditions=[dict(kind='other')])),
            ('ASSOCIATION', lambda v: v.update(step_evidence=[])),
            ('ASSOCIATION', lambda v: v['step_evidence'][0].update(demo_seq=7)),
            ('ASSOCIATION', lambda v: v['step_evidence'][0].update(frame_sha256='b'*64)),
            ('OCR_GROUNDING', lambda v: v['step_evidence'][0].update(mark_id='404')),
            ('OCR_GROUNDING', lambda v: v['workflow']['steps'][0]['target'].update(ocr_label='imaginary')),
            ('OCR_GROUNDING', lambda v: v['workflow']['steps'][0]['target'].update(rel_box_bps=[0,0,1,1])),
            ('SEMANTIC_TARGET', lambda v: v['workflow']['steps'][0].update(target={'rung':'T1'})),
            ('SEMANTIC_TARGET', lambda v: v['workflow']['steps'][0].update(target={'rung':'T0'})),
            ('SEMANTIC_VERIFY', lambda v: v['workflow']['steps'][0]['verify'].update(predicate=None)),
            ('ARTIFACT_LIMIT', lambda v: v.update(site_markdown='x'*8193)),
            ('SCHEMA', lambda v: v['workflow']['steps'][0]['target'].update(mark_id='9')),
        ]
        for category, mutate in cases:
            value = copy.deepcopy(good)
            mutate(value)
            with self.subTest(category=category):
                self.assertEqual(self._validate(value, pack=pack).category, category)
        self.assertEqual(self._validate(good, pack=pack), _accept())
        constrained = replace(pack, constraints=w._bytes({**json.loads(self.constraints), 'protected':[1]}))
        self.assertEqual(self._validate(good, pack=constrained).category, 'PROTECTED')
        good['workflow']['steps'][0]['protected'] = True
        self.assertEqual(self._validate(good, pack=constrained), _accept())
        for key in ('ocr_text', 'missing_reason'):
            ocr = json.loads(pack.steps[0].ocr)
            ocr['marks'][0][key] = None if key == 'ocr_text' else 'OCR_UNAVAILABLE'
            unavailable = replace(pack, steps=(replace(pack.steps[0], ocr=w._bytes(ocr)),))
            self.assertEqual(self._validate(good, pack=unavailable).category, 'OCR_GROUNDING')

    # @anchor:wa.learn.mwl1_tests.LearnTests.test_slot_privacy_and_exact_artifact_bound
    def test_slot_privacy_and_exact_artifact_bound(self):
        # A private recorder-side slot file is outside the loader allowlist.
        (self.demo / 'slot-private.txt').write_text('CANARY_PRIVATE_SLOT')
        self.envelope['site_markdown'] += ' '*(8192-len(self.envelope['site_markdown'].encode()))
        result = self._learn()
        self.assertEqual(result.rc, 0, result)
        self.assertNotIn('CANARY_PRIVATE_SLOT', repr(self.backend.calls))
        self.assertNotIn('CANARY_PRIVATE_SLOT', ''.join(p.read_text() for p in (self.output/'sites/site').rglob('*') if p.is_file()))

    # @anchor:wa.learn.mwl1_tests.LearnTests.test_author_transport_and_ocr_adapter
    def test_author_transport_and_ocr_adapter(self):
        pack = w.build_evidence(self.inputs, policy=self.policy, owners=self.owners, describer=self.describer, clock=lambda: 100)
        backend = w.AuthorBackend(effort='medium')
        def transport(argv, **kwargs):
            if '--image' in argv:
                self.assertIn(str(self.demo / '7_before.png'), argv)
            self.assertNotIn('CANARY_PRIVATE_SLOT', repr(argv))
            self.assertTrue(Path(argv[argv.index('--prompt-file')+1]).is_relative_to('/dev/shm/vqa-ramws'))
            return w._bytes({'payload_type':'run.output.delta','payload':{'text':json.dumps(self.envelope)}})
        with patch.object(w, '_run', transport):
            reply = w.author_draft(pack, inputs=self.inputs, policy=self.policy, owners=self.owners, backend=backend, clock=lambda: 100)
        self.assertEqual(reply.workflow['id'], 'work')
        for text in ('{"workflow":{},"workflow":{}}', '{}{}', '{"v":NaN}'):
            with patch.object(w, '_run', return_value=w._bytes({'payload_type':'run.output.delta','payload':{'text':text}})):
                with self.assertRaisesRegex(w._Stop, ''):
                    w.author_draft(pack, inputs=self.inputs, policy=self.policy, owners=self.owners, backend=backend, clock=lambda: 100)
        with patch.object(w, 'SubprocessVisionEngine') as engine:
            engine.return_value.python = 'python3'
            engine.return_value.ve_root = self.root
            adapter = w.OCRDescriber(policy=self.policy, owners=self.owners, clock=lambda: 100, deadline=10**12, ve_root=self.root)
            self.addCleanup(lambda: adapter.close() if adapter.directory.exists() else None)
            from wender.perceive.vision_bridge import DescribeResult, Mark
            decoded = self.describer.describe({'path':str(self.demo/'7_before.png')})['frame_sha256']
            def describe_transport(argv, **kwargs):
                self.assertIn('--ocr-marks', argv)
                marks = Path(argv[argv.index('--marks-out')+1])
                marks.write_bytes(b'owned')
                return b'{}'
            answer = DescribeResult('ve.cu_describe/v1', decoded, 192, 120, None, (), (), 1,
                                    marks=(Mark(9,80,40,40,30,'button',0.9,'Continue',950,None,None),),
                                    marks_png=str(adapter.directory/'marks.png'))
            with patch.object(w, '_run', side_effect=describe_transport) as runner, patch.object(w, '_parse_description', return_value=answer):
                a = adapter.describe({'path':str(self.demo/'7_before.png')})
                b = adapter.describe({'path':str(self.demo/'7_before.png')})
                self.assertEqual(a,b)
                self.assertEqual(runner.call_count,1)
                self.assertEqual(a['marks'][0]['score_ppm'],900000)
                self.assertFalse((adapter.directory/'marks.png').exists())
            adapter.close()
            self.assertFalse(adapter.directory.exists())

    # @anchor:wa.learn.mwl1_tests.LearnTests.test_bounded_child_transport
    def test_bounded_child_transport(self):
        import sys
        args = dict(policy=self.policy, owners=self.owners, clock=lambda: 100, deadline=10**12, cap=100)
        self.assertEqual(w._run([sys.executable,'-c','print("ok")'], **args), b'ok\n')
        with self.assertRaises(w._Stop) as error:
            w._run([sys.executable,'-c','import sys;sys.stderr.write("x"*1000)'], **args)
        self.assertEqual(error.exception.category, 'TRANSPORT_LIMIT')
        clock = iter([0,10**12]).__next__
        with self.assertRaises(w._Stop) as error:
            w._run([sys.executable,'-c','import time;time.sleep(30)'], **{**args,'clock':clock})
        self.assertEqual(error.exception.category, 'DEADLINE')

    # @anchor:wa.learn.mwl1_tests.LearnTests.test_public_transport_limits_and_drain
    def test_public_transport_limits_and_drain(self):
        pack = w.build_evidence(self.inputs, policy=self.policy, owners=self.owners, describer=self.describer, clock=lambda: 100)
        executable = self.root / 'fake-muse'
        executable.write_text('#!/usr/bin/python3\nimport json\nprint('+repr(json.dumps({'payload_type':'run.output.delta','payload':{'text':json.dumps(self.envelope)}}))+')\n')
        executable.chmod(0o700)
        backend = w.AuthorBackend(effort='medium', executable=str(executable))
        child_factory = w.subprocess.Popen
        children = []
        def spawn(*args, **kwargs):
            child = child_factory(*args, **kwargs)
            children.append(child)
            return child
        with patch.object(w.subprocess, 'Popen', spawn):
            good = w.author_draft(pack, inputs=self.inputs, policy=self.policy, owners=self.owners, backend=backend, clock=lambda: 100)
            self.assertEqual(good.workflow['id'],'work')
            executable.write_text('#!/usr/bin/python3\nimport sys\nsys.stderr.write("CANARY_PRIVATE_SLOT"*100000)\n')
            with self.assertRaises(w._Stop) as error:
                w.author_draft(pack, inputs=self.inputs, policy=self.policy, owners=self.owners, backend=backend, clock=lambda: 100)
            self.assertEqual(error.exception.category,'TRANSPORT_LIMIT')
            executable.write_text('#!/usr/bin/python3\nimport time\ntime.sleep(30)\n')
            clock = iter([100,100,10**12]).__next__
            with self.assertRaises(w._Stop) as error:
                w.author_draft(pack, inputs=self.inputs, policy=self.policy, owners=self.owners, backend=backend, clock=clock)
            self.assertEqual(error.exception.category,'DEADLINE')
        self.assertTrue(all(child.poll() is not None for child in children))

    # @anchor:wa.learn.mwl1_tests.LearnTests.test_limits_pins_and_fences
    def test_limits_pins_and_fences(self):
        for policy in (replace(self.policy, effort='auto'), replace(self.policy, images=True),
                       replace(self.policy, memory_bytes=1), replace(self.policy, artifacts=5)):
            self._fail('POLICY' if policy.effort == 'auto' or policy.images is True else 'CAPACITY', policy=policy)
        self.inputs.explanation.write_bytes(b'x'*16385)
        self._fail('CAPACITY')
        self.inputs.explanation.write_bytes(_explanation())
        manifest = json.loads(self.inputs.demo_manifest.read_bytes())
        manifest['steps_sha256']='0'*64
        self.inputs.demo_manifest.write_bytes(w._bytes(manifest))
        self._fail('INPUT_PIN')
        manifest['steps_sha256']=hashlib.sha256(self.projection.payload).hexdigest()
        self.inputs.demo_manifest.write_bytes(w._bytes(manifest))
        # Revoke exactly at publication, after a valid response.
        owner = self.owners
        def deny_publication(*args):
            return g.GateResult(5,'DENY') if args[4]=='publish' else _accept()
        self._fail('PRIVACY',1,owners=replace(owner,disclosure=deny_publication))
        def change_revision():
            object.__setattr__(owner,'revision_check',lambda *a:g.GateResult(5,'DENY'))
        self.backend.hook=change_revision
        self._fail('REVISION',1)
        object.__setattr__(owner,'revision_check',_accept)
        self.backend.hook=None
        self.assertEqual(self._learn().rc,0)

    # @anchor:wa.learn.mwl1_tests.LearnTests.test_context_binding
    def test_context_binding(self):
        self._fail('VALIDATOR_CONTEXT', owners=replace(self.owners,context_factory=lambda root: self.context(self.root)))
        self.assertEqual(self._learn().rc,0)

    # @anchor:wa.learn.mwl1_tests.LearnTests.test_optional_router_passive_controls
    def test_optional_router_passive_controls(self):
        self.inputs=replace(self.inputs,router_root=self.root,router_query='query')
        for raw in (b'not json', b'{"hits":false}', b'{"error":"missing"}'):
            with patch.object(w,'_run',return_value=raw):
                pack=w.build_evidence(self.inputs,policy=self.policy,owners=self.owners,describer=self.describer,clock=lambda:100)
            self.assertIsNone(pack.router)
            self.assertIn((None,'ROUTER_UNAVAILABLE'),pack.omissions)
        with patch.object(w,'_run',return_value=b'{"hits":[],"calibrated":false}'):
            pack=w.build_evidence(self.inputs,policy=self.policy,owners=self.owners,describer=self.describer,clock=lambda:100)
        self.assertEqual(json.loads(pack.router)['hits'],[])
        self.assertEqual(self._learn().rc,0)

    # @anchor:wa.learn.mwl1_tests.LearnTests.test_missing_after_and_symbolic_slots
    def test_missing_after_and_symbolic_slots(self):
        row=copy.deepcopy(self.row)
        row.update(kind='text',x=None,y=None,button=None,text_len=6,before='7_before.png',after=None)
        (self.demo/'events.jsonl').write_text(json.dumps(row)+'\n')
        (self.demo/'7_after.png').unlink()
        projection=d.project_demo_steps(d.segment_demo(d.load_demo(self.demo,limits=self.policy.demo_limits)),demo_id='demo',site_id='site')
        manifest=json.loads(self.inputs.demo_manifest.read_bytes())
        manifest['steps_sha256']=hashlib.sha256(projection.payload).hexdigest()
        self.inputs.demo_manifest.write_bytes(w._bytes(manifest))
        pack=w.build_evidence(self.inputs,policy=self.policy,owners=self.owners,describer=self.describer,clock=lambda:100)
        step=pack.steps[0]
        self.assertIsNone(step.after)
        self.assertEqual(step.slots,(('slot-7','paste',6),))
        self.assertIn('AFTER_UNAVAILABLE',step.missingness)
        self.assertEqual(len(pack.images),1)

    # @anchor:wa.learn.mwl1_tests.LearnTests.test_output_escape_and_validator_result
    def test_output_escape_and_validator_result(self):
        (self.output/'sites').symlink_to(self.root,target_is_directory=True)
        self._fail('CONTAINMENT')
        (self.output/'sites').unlink()
        with patch.object(g,'validate_directory',return_value=True):
            self._fail('VALIDATOR_RESULT',1)
        self.assertEqual(self._learn().rc,0)

    # @anchor:wa.learn.mwl1_tests.LearnTests.test_boundary_sizes_and_backend_effort
    def test_boundary_sizes_and_backend_effort(self):
        for effort in ('auto','low','ultra'):
            with self.assertRaises(w._Stop) as error:
                w.AuthorBackend(effort=effort)
            self.assertEqual(error.exception.category,'POLICY')
        for field,cap in (('demo_manifest',32768),('guide',24576)):
            path=getattr(self.inputs,field)
            original=path.read_bytes()
            path.write_bytes(b'x'*(cap+1))
            self._fail('CAPACITY')
            path.write_bytes(original)
        self._fail('CAPACITY',policy=replace(self.policy,request_bytes=1))
        self.backend.answers=[b'x'*(self.policy.response_bytes+1)]
        self._fail('RESPONSE_LIMIT',1)
        self.assertEqual(self._learn().rc,0)

    # @anchor:wa.learn.mwl1_tests.LearnTests.test_atomic_collision_race
    def test_atomic_collision_race(self):
        publish=w._publish
        def race(source,destination):
            destination.mkdir()
            publish(source,destination)
        with patch.object(w,'_publish',side_effect=race):
            result=self._learn()
        self.assertEqual((result.category,result.model_calls),('PUBLICATION',1))
        self.assertEqual(list((self.output/'sites/site').iterdir()),[])

    # @anchor:wa.learn.mwl1_tests.LearnTests.test_observed_digest_binding
    def test_observed_digest_binding(self):
        describe=self.describer.describe
        def wrong(frame,**kwargs):
            answer=describe(frame,**kwargs)
            answer['frame_sha256']='f'*64
            return answer
        with patch.object(self.describer,'describe',wrong):
            self._fail('FRAME_IDENTITY')
        self.assertEqual(self._learn().rc,0)

    # @anchor:wa.learn.mwl1_tests.LearnTests.test_private_response_export_and_input_schema
    def test_private_response_export_and_input_schema(self):
        canary='CANARY_PRIVATE_SLOT'
        self.envelope['site_markdown']+=canary
        denied=hashlib.sha256(self.envelope['site_markdown'].encode()).hexdigest()
        def disclosure(digest,*args):
            return g.GateResult(5,'DENY') if digest==denied else _accept()
        self._fail('PRIVACY',1,owners=replace(self.owners,disclosure=disclosure))
        self.assertNotIn(canary,repr(self.approvals))
        self.envelope['site_markdown']=_explanation().decode()
        original=self.inputs
        altered=self.root/'altered-schema.json'
        altered.write_bytes(self.inputs.schema.read_bytes()+b' ')
        self.inputs=replace(original,schema=altered)
        self._fail('SCHEMA_PIN')
        self.inputs=original
        raw=self.inputs.demo_manifest.read_bytes()
        self.inputs.demo_manifest.write_bytes(b'null')
        self._fail('INPUT_SCHEMA')
        self.inputs.demo_manifest.write_bytes(raw)
        self.assertEqual(self._learn().rc,0)

    # @anchor:wa.learn.mwl1_tests.LearnTests.test_remaining_spec_ceiling_controls
    def test_remaining_spec_ceiling_controls(self):
        self._target()
        good=copy.deepcopy(self.envelope)
        self.envelope['workflow']['trigger_sig']['keywords']=['x'*400]*200
        self.assertEqual(self._validate().category,'ARTIFACT_LIMIT')
        self.envelope=good
        projection=replace(self.projection,payload=self.projection.payload+b' '*65536)
        with patch.object(d,'project_demo_steps',return_value=projection):
            self._fail('CAPACITY')
        self.inputs=replace(self.inputs,router_root=self.root,router_query='q'*512)
        with patch.object(w,'_run',return_value=w._bytes({'hits':[],'extra':'x'*2001})):
            pack=w.build_evidence(self.inputs,policy=self.policy,owners=self.owners,describer=self.describer,clock=lambda:100)
        self.assertIsNone(pack.router)
        bad_context=lambda root:replace(self.context(root),capability_check=lambda *a:True)
        self._fail('EVIDENCE_DENIED',1,owners=replace(self.owners,context_factory=bad_context))
        self.assertEqual(self._learn().rc,0)

    # @anchor:wa.learn.mwl1_tests.LearnTests.test_repair_and_publication_fences
    def test_repair_and_publication_fences(self):
        bad=copy.deepcopy(self.envelope)
        bad['workflow']['status']='shadow'
        self.backend.answers=[bad,self.envelope]
        author_checks=0
        def invalidate_at_repair(*args):
            nonlocal author_checks
            if args[4]=='author':
                author_checks+=1
                if author_checks==2:
                    self.inputs.guide.unlink()
            return _accept()
        self._fail('INPUT_CHANGED',1,owners=replace(self.owners,disclosure=invalidate_at_repair))
        self.inputs.guide.write_text('Reviewed authoring guide.')
        self.backend.answers=[]
        revoked=False
        def invalidate_at_publication(*args):
            nonlocal revoked
            if args[4]=='publish':
                revoked=True
            return _accept()
        owner=replace(self.owners,disclosure=invalidate_at_publication,
                      revision_check=lambda *a:g.GateResult(5,'DENY') if revoked else _accept())
        self._fail('REVISION',1,owners=owner)
        self.assertEqual(self._learn().rc,0)


    # @anchor:wa.learn.mwl1_tests.LearnTests._ground_pack
    def _ground_pack(self, changes=None):
        pack = w.build_evidence(self.inputs, policy=self.policy, owners=self.owners,
                                describer=self.describer, clock=lambda: 100)
        ocr = json.loads(pack.steps[0].ocr)
        if changes:
            changes(ocr['marks'])
        return replace(pack, steps=(replace(pack.steps[0], ocr=w._bytes(ocr)),))

    # @anchor:wa.learn.mwl1_tests.LearnTests.test_repair_diagnostic_allowlist
    def test_repair_diagnostic_allowlist(self):
        approved = {'DRAFT_BINDING', 'DRAFT_ONLY', 'UNKNOWN_STATS', 'DRAFT_ORACLE',
                    'FROZEN_CONSTRAINTS', 'PROTECTED', 'ASSOCIATION', 'SEMANTIC_TARGET',
                    'OCR_GROUNDING', 'SEMANTIC_VERIFY', 'ARTIFACT_LIMIT', 'CANDIDATE_SCHEMA', 'SCHEMA'}
        self.assertIsInstance(w._REPAIR_DIAGNOSTICS, frozenset)
        self.assertEqual(w._REPAIR_DIAGNOSTICS, approved)
        validate = w.validate_candidate
        # @anchor:wa.learn.mwl1_tests.LearnTests.test_repair_diagnostic_allowlist.Token
        class Token(str):
            pass
        # Unknown categories cross the public learn/CLI seam; nominal retry uses the real validator.
        for category in [*sorted(approved), 'CANARY/private/path', None, 1, [], Token('SCHEMA')]:
            self.backend.calls.clear()
            self.inputs = replace(self.inputs, output_root=self.root / ('out-' + str(len(self.approvals))))
            self.inputs.output_root.mkdir()
            calls = 0
            # @anchor:wa.learn.mwl1_tests.LearnTests.test_repair_diagnostic_allowlist.first_refusal
            def first_refusal(*args, **kwargs):
                nonlocal calls
                calls += 1
                return g.GateResult(2, category) if calls == 1 else validate(*args, **kwargs)
            output = io.StringIO()
            with patch.object(w, 'validate_candidate', side_effect=first_refusal), contextlib.redirect_stdout(output):
                rc = cli.main(self._cli_argv(), owners=self.owners, backend=self.backend, describer=self.describer)
            self.assertEqual(rc, 0, output.getvalue())
            expected = category if type(category) is str and category in approved else 'CANDIDATE_REJECTED'
            self.assertEqual(self.backend.calls[1]['diagnostic'], expected)
            self.assertNotIn('CANARY', repr(self.backend.calls) + output.getvalue())
            self.assertEqual(self.resources.held, 0)

    # @anchor:wa.learn.mwl1_tests.LearnTests.test_t2_search_region_containment
    def test_t2_search_region_containment(self):
        self._target()
        target = self.envelope['workflow']['steps'][0]['target']
        for region in ([0, 0, 10000, 10000], [4000, 3000, 7000, 6000], [4166, 3333, 6250, 5833]):
            target['rel_box_bps'] = region
            self.assertEqual(self._validate(), _accept())
        for region in ([0, 0, 1, 1], [4200, 3333, 6250, 5833], [4166, 3333, 6200, 5833]):
            target['rel_box_bps'] = region
            self.assertEqual(self._validate(), g.GateResult(2, 'OCR_GROUNDING'))
        self._fail('REPAIR_EXHAUSTED', 2)

    # @anchor:wa.learn.mwl1_tests.LearnTests.test_t2_search_region_uniqueness
    def test_t2_search_region_uniqueness(self):
        self._target()
        for reverse in (False, True):
            # @anchor:wa.learn.mwl1_tests.LearnTests.test_t2_search_region_uniqueness.outside
            def outside(marks):
                marks.append(dict(marks[0], id=10, x=0, y=0))
                if reverse:
                    marks.reverse()
            self.assertEqual(self._validate(pack=self._ground_pack(outside)), _accept())
            # @anchor:wa.learn.mwl1_tests.LearnTests.test_t2_search_region_uniqueness.duplicate
            def duplicate(marks):
                marks.append(dict(marks[0], id=10))
                if reverse:
                    marks.reverse()
            self.assertEqual(self._validate(pack=self._ground_pack(duplicate)), g.GateResult(2, 'OCR_GROUNDING'))

    # @anchor:wa.learn.mwl1_tests.LearnTests.test_t2_floor_boundary_and_region_types
    def test_t2_floor_boundary_and_region_types(self):
        self._target()
        target = self.envelope['workflow']['steps'][0]['target']
        self.assertEqual(self._validate(), _accept())
        for region in ([4167,3333,6250,5833], [4166,3334,6250,5833], [4166,3333,6249,5833],
                       [4166,3333,6250,5832], [False,0,10000,10000], [0,True,10000,10000],
                       [0,0,10000,True], [7000,0,4000,10000], [0,7000,10000,4000],
                       [0,0,0,10000], [0,0,10001,10000], [-1,0,10000,10000],
                       [0,0,'10000',10000], [0,0,10000], None, 'box', (0,0,10000,10000)):
            target['rel_box_bps'] = region
            self.assertEqual(self._validate(), g.GateResult(2, 'OCR_GROUNDING'), region)
        del target['rel_box_bps']
        self.assertNotEqual(self._validate().rc, 0)
        # Reject malformed region ordering before touching even unusable mark geometry.
        target['rel_box_bps'] = [7000,0,4000,10000]
        # @anchor:wa.learn.mwl1_tests.LearnTests.test_t2_floor_boundary_and_region_types.unusable
        def unusable(items):
            items[0]['x'] = 'unusable'
        self.assertEqual(self._validate(pack=self._ground_pack(unusable)), g.GateResult(2, 'OCR_GROUNDING'))

    # @anchor:wa.learn.mwl1_tests.LearnTests._alternative
    def _alternative(self, destination=False):
        self._target()
        step = self.envelope['workflow']['steps'][0]
        other = dict(step['target'], mark_kind='field', ocr_label='Name', rel_box_bps=[0,0,4000,3000])
        if destination:
            step['action'].update(kind='drag', destination=other)
        else:
            step['fallback_targets'] = [other]
        # @anchor:wa.learn.mwl1_tests.LearnTests._alternative.marks
        def marks(items):
            items.append(dict(items[0], id=10, x=10, y=10, w=20, h=20, kind='field', ocr_text='Name'))
        return other, self._ground_pack(marks)

    # @anchor:wa.learn.mwl1_tests.LearnTests.test_t2_fallback_independent_grounding
    def test_t2_fallback_independent_grounding(self):
        other, pack = self._alternative()
        self.assertEqual(self._validate(pack=pack), _accept())
        for changes in ({'ocr_label':'Absent'}, {'mark_kind':'button'}, {'rel_box_bps':[5000,5000,10000,10000]}):
            bad = copy.deepcopy(self.envelope)
            bad['workflow']['steps'][0]['fallback_targets'][0].update(changes)
            self.assertEqual(self._validate(bad, pack=pack), g.GateResult(2, 'OCR_GROUNDING'))
        ocr = json.loads(pack.steps[0].ocr)
        ocr['marks'].append(dict(ocr['marks'][1], id=11))
        ambiguous = replace(pack, steps=(replace(pack.steps[0], ocr=w._bytes(ocr)),))
        self.assertEqual(self._validate(pack=ambiguous), g.GateResult(2, 'OCR_GROUNDING'))
        # T3 and null primaries cannot donate association identity to alternatives.
        for primary in (None, dict(rung='T3', predicate=_pred())):
            self.envelope['workflow']['steps'][0]['target'] = primary
            self.envelope['workflow']['steps'][0]['action']['kind'] = 'wait_signal'
            self.envelope['step_evidence'][0].update(mark_id=None, missing_reason='NOT_REQUIRED')
            self.assertEqual(self._validate(pack=pack), _accept())

    # @anchor:wa.learn.mwl1_tests.LearnTests.test_t2_destination_independent_grounding
    def test_t2_destination_independent_grounding(self):
        other, pack = self._alternative(destination=True)
        self.assertEqual(self._validate(pack=pack), _accept())
        for changes in ({'ocr_label':'Absent'}, {'rel_box_bps':[5000,5000,10000,10000]}):
            bad = copy.deepcopy(self.envelope)
            bad['workflow']['steps'][0]['action']['destination'].update(changes)
            self.assertEqual(self._validate(bad, pack=pack), g.GateResult(2, 'OCR_GROUNDING'))
        ocr = json.loads(pack.steps[0].ocr)
        ocr['marks'].append(dict(ocr['marks'][1], id=11))
        self.assertEqual(self._validate(pack=replace(pack, steps=(replace(pack.steps[0], ocr=w._bytes(ocr)),))),
                         g.GateResult(2, 'OCR_GROUNDING'))

    # @anchor:wa.learn.mwl1_tests.LearnTests.test_t2_primary_association_binding
    def test_t2_primary_association_binding(self):
        _, pack = self._alternative()
        self.assertEqual(self._validate(pack=pack), _accept())
        # @anchor:wa.learn.mwl1_tests.LearnTests.test_t2_primary_association_binding.MarkId
        class MarkId(str):
            pass
        for value in ('10', 'foreign', 9, True, None, MarkId('9')):
            self.envelope['step_evidence'][0]['mark_id'] = value
            self.assertEqual(self._validate(pack=pack), g.GateResult(2, 'OCR_GROUNDING'))
        self.envelope['step_evidence'][0].update(mark_id='9', missing_reason='OCR_UNAVAILABLE')
        self.assertEqual(self._validate(pack=pack), g.GateResult(2, 'OCR_GROUNDING'))

    # @anchor:wa.learn.mwl1_tests.LearnTests.test_t2_missingness_not_bypassed
    def test_t2_missingness_not_bypassed(self):
        self._target()
        self.assertEqual(self._validate(), _accept())
        for field, value in (('missing_reason','OCR_UNAVAILABLE'), ('ocr_text',None)):
            # @anchor:wa.learn.mwl1_tests.LearnTests.test_t2_missingness_not_bypassed.missing
            def missing(items):
                items[0][field] = value
            self.assertEqual(self._validate(pack=self._ground_pack(missing)), g.GateResult(2, 'OCR_GROUNDING'))
        # @anchor:wa.learn.mwl1_tests.LearnTests.test_t2_missingness_not_bypassed.hidden
        def hidden(items):
            items.append(dict(items[0], id=10, missing_reason='OCR_UNAVAILABLE'))
        self.assertEqual(self._validate(pack=self._ground_pack(hidden)), g.GateResult(2, 'OCR_GROUNDING'))

    # @anchor:wa.learn.mwl1_tests.LearnTests.test_high_effort_author_transport
    def test_high_effort_author_transport(self):
        self.policy = replace(self.policy, effort='high')
        pack = self._ground_pack()
        try:
            backend = w.AuthorBackend(effort='high', timeout=17, executable='captured-muse', workspace=self.root)
        except ValueError:
            self.fail('AuthorBackend must accept the supported high effort')
        self.assertEqual((backend.effort, backend.effort_policy, backend.timeout, backend.executable),
                         ('high', 'high', 17, 'captured-muse'))
        self.assertEqual(backend.workspace, str(self.root))
        self.assertTrue(backend.session_id)
        # @anchor:wa.learn.mwl1_tests.LearnTests.test_high_effort_author_transport.transport
        def transport(argv, **kwargs):
            self.assertEqual(argv[argv.index('--reasoning-effort')+1], 'high')
            return w._bytes({'payload_type':'run.output.delta','payload':{'text':json.dumps(self.envelope)}})
        with patch.object(w, '_run', side_effect=transport) as run:
            reply = w.author_draft(pack, inputs=self.inputs, policy=self.policy, owners=self.owners,
                                   backend=backend, clock=lambda:100)
            self.assertEqual(reply.workflow['id'], 'work')
            self.assertEqual(run.call_count, 1)
            with self.assertRaises(w._Stop) as error:
                w.author_draft(pack, inputs=self.inputs, policy=replace(self.policy, effort='medium'),
                               owners=self.owners, backend=backend, clock=lambda:100)
            self.assertEqual(error.exception.category, 'POLICY')
        for effort in ('medium','high','xhigh'):
            configured = w.AuthorBackend(effort=effort)
            self.assertEqual((configured.effort,configured.effort_policy), (effort,effort))
        with self.assertRaises(ValueError):
            w.AuthorBackend(effort='high', timeout=0)
        self._fail('POLICY', policy=replace(self.policy, effort='ultra'))

    # @anchor:wa.learn.mwl1_tests.LearnTests.test_cli_high_effort
    def test_cli_high_effort(self):
        # @anchor:wa.learn.mwl1_tests.LearnTests.test_cli_high_effort.transport
        def transport(argv, **kwargs):
            self.assertEqual(argv[argv.index('--reasoning-effort')+1], 'high')
            return w._bytes({'payload_type':'run.output.delta','payload':{'text':json.dumps(self.envelope)}})
        for effort, owners, expected in (('ultra',self.owners,'USAGE_SCHEMA'), ('high',None,'OWNERS_MISSING'),
                                         ('high',self.owners,'DRAFT_WRITTEN')):
            output = io.StringIO()
            with contextlib.redirect_stdout(output), patch.object(w, '_run', side_effect=transport):
                rc = cli.main(self._cli_argv()+['--effort',effort], owners=owners, describer=self.describer)
            self.assertEqual(json.loads(output.getvalue())['category'], expected)
            self.assertEqual(rc, 0 if expected == 'DRAFT_WRITTEN' else 2)
            self.assertEqual(self.resources.held, 0)


    # @anchor:wa.learn.mwl1_tests.LearnTests.test_repair_admission_and_budget_controls
    def test_repair_admission_and_budget_controls(self):
        for category in ('PRIVACY', 'CONTAINMENT', 'CAPACITY', 'CONTEXT_REQUIRED', 'INPUT_PIN',
                         'CAPABILITY_MISSING', 'EVIDENCE_MISSING', 'REVIEW_MISSING',
                         'EVIDENCE_DENIED', 'EVIDENCE_OWNER'):
            with patch.object(w, 'validate_candidate', return_value=g.GateResult(2, category)):
                self._fail(category, 1)
        self.backend.calls.clear()
        self.envelope['workflow']['status'] = 'shadow'
        self._fail('REPAIR_EXHAUSTED', 2)
        initial = len(w._bytes(self.backend.calls[0]))
        self.backend.calls.clear()
        self._fail('REQUEST_LIMIT', 1, policy=replace(self.policy, request_bytes=initial))
        self.assertEqual(len(self.backend.calls), 1)
        self.envelope['workflow']['status'] = 'draft'
        self.assertEqual(self._learn().rc, 0)

    # @anchor:wa.learn.mwl1_tests.LearnTests.test_grounding_inventory_and_closed_association
    def test_grounding_inventory_and_closed_association(self):
        _, pack = self._alternative()
        original = w._json
        # A single inventory decode supplies all targets for this workflow step.
        with patch.object(w, '_json', wraps=original) as decode:
            self.assertEqual(self._validate(pack=pack), _accept())
        self.assertEqual(sum(call.args[0] == pack.steps[0].ocr for call in decode.call_args_list), 1)
        self.envelope['step_evidence'][0]['fallback_mark_id'] = '10'
        self.assertEqual(self._validate(pack=pack), g.GateResult(2, 'ASSOCIATION'))
        del self.envelope['step_evidence'][0]['fallback_mark_id']
        self.assertEqual(self._validate(pack=pack), _accept())

    # @anchor:wa.learn.mwl1_tests.LearnTests.test_high_transport_failure_has_no_downgrade
    def test_high_transport_failure_has_no_downgrade(self):
        with patch.object(w, '_run', side_effect=OSError('transport refused')) as run:
            self._fail('PIPELINE_REFUSED', 1, backend=w.AuthorBackend(effort='high'),
                       policy=replace(self.policy, effort='high'))
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args.args[0][run.call_args.args[0].index('--reasoning-effort')+1], 'high')

    # @anchor:wa.s4.mwl1g_tests.LearnTests._guidance
    def _guidance(self, *, payload=None, events=None, step=1, demo_seq=1):
        payload = payload if payload is not None else dict(time_ns=12, box_px=[1, 2, 9, 10],
            classification='outside_expected_after_quiet', before_sequence=3, after_sequence=4, demo_ref='mini-demo')
        self.journal = GuidanceJournal(events if events is not None else [dict(
            kind='operator_guidance', step_seq=step, phase='terminal', guidance=payload)])
        event = [e for e in self.journal.events if e['kind'] == 'operator_guidance'][-1]
        payload = event['guidance']
        good = dict(time_ns=12, box_px=[1, 2, 9, 10], classification='outside_expected_after_quiet',
                    before_sequence=3, after_sequence=4, demo_ref='mini-demo')
        if guidance.validate_payload(payload) and payload['demo_ref'] is not None:
            good = payload
        ev = guidance.GuidanceEvidence(step, **dict(good, box_px=tuple(good['box_px'])))
        self.ref = guidance.evidence_reference(ev, event['digest'])
        self.validator = guidance.validate_payload
        self.custody = Mock(side_effect=self._guidance_check)
        self.guidance_frame_pin = hashlib.sha256((self.demo / '7_before.png').read_bytes()).hexdigest()
        owner = w.GuidanceIntakeOwner(self.journal, hashlib.sha256(self.journal.raw).hexdigest(),
            'a' * 64, max(step, 2), (w.GuidanceBinding(step, demo_seq, 'mini-demo'),), self.validator, self.custody)
        self.inputs = replace(self.inputs, guidance_refs=(self.ref,))
        self.owners = replace(self.owners, guidance=owner)
        self.backend.calls.clear()
        self.backend.hook = None
        return owner

    # @anchor:wa.s4.mwl1g_tests.LearnTests._guidance_check
    def _guidance_check(self, ref, **kw):
        expected = {'event', 'demo_step', 'manifest', 'identities', 'scope', 'authority', 'revision',
                    'site_id', 'workflow_id', 'workflow_sha256', 'deadline_ns'}
        valid = (set(kw) == expected and kw['scope'] == 'scope' and kw['authority'] == 'authority' and
                 kw['revision'] == 'revision' and kw['site_id'] == 'site' and kw['workflow_id'] == 'work' and
                 kw['workflow_sha256'] == 'a' * 64 and kw['demo_step'].seq == 1 and
                 kw['demo_step'].recorder_seq == 7 and kw['demo_step'].before[1] == self.guidance_frame_pin and kw['manifest']['steps_sha256'] ==
                 hashlib.sha256(self.projection.payload).hexdigest() and
                 dict((Path(i[0]).name, i[3]) for i in kw['identities'])['7_before.png'] == kw['demo_step'].before[1])
        return _accept() if valid else g.GateResult(2, 'DENIED')

    # @anchor:wa.s4.mwl1g_tests.LearnTests._guidance_fail
    def _guidance_fail(self, category, calls=0, rc=2, **kwargs):
        before = len(self.backend.calls)
        result = self._fail(category, calls, **kwargs)
        self.assertEqual(result.rc, rc)
        self.assertEqual(len(self.backend.calls) - before, calls)
        self.assertFalse(list(self.output.rglob('.mw-staging-*')))
        return result

    # @anchor:wa.s4.mwl1g_tests.LearnTests.test_guidance_ref_accepted_as_repair_evidence
    def test_guidance_ref_accepted_as_repair_evidence(self):
        self._guidance()
        self.inputs = replace(self.inputs, guidance_refs=(self.ref.replace(':1:12:', ':2:12:'),))
        self._guidance_fail('GUIDANCE_ASSOCIATION')
        self.inputs = replace(self.inputs, guidance_refs=(self.ref.replace(':1:12:', ':1:13:'),))
        self._guidance_fail('GUIDANCE_ASSOCIATION')
        self.inputs = replace(self.inputs, guidance_refs=(self.ref,))
        self.assertEqual(self._learn().rc, 0)
        row = self.backend.calls[-1]['passive_data']['repair_evidence'][0]
        self.assertEqual(row, dict(step_seq=1, demo_seq=1, **self.journal.events[0]['guidance']))
        self.assertEqual(self.custody.call_args.kwargs['demo_step'].recorder_seq, 7)
        self.assertNotIn('mini-demo', ''.join(p.read_text() for p in (self.output / 'sites/site').rglob('*') if p.is_file()))

    # @anchor:wa.s4.mwl1g_tests.LearnTests.test_guidance_before_input_class_accepted
    def test_guidance_before_input_class_accepted(self):
        self._guidance()
        payload = dict(self.journal.events[0]['guidance'], classification='unknown')
        self._guidance(payload=payload)
        self._guidance_fail('GUIDANCE_SCHEMA')
        self._guidance(payload=dict(payload, classification='before_input'))
        self.assertEqual(self._learn().rc, 0)

    # @anchor:wa.s4.mwl1g_tests.LearnTests.test_guidance_missing_demo_is_non_evidence
    def test_guidance_missing_demo_is_non_evidence(self):
        self._guidance()
        self.inputs = replace(self.inputs, guidance_refs=(self.ref.rsplit(':', 1)[0],))
        self._guidance_fail('GUIDANCE_NON_EVIDENCE')
        self._guidance(payload=dict(self.journal.events[0]['guidance'], demo_ref=None))
        # The binding check is independently permissive, isolating the null-recorder predicate.
        self._guidance_fail('GUIDANCE_NON_EVIDENCE')
        self._guidance()
        self.assertEqual(self._learn().rc, 0)

    # @anchor:wa.s4.mwl1g_tests.LearnTests.test_guidance_reference_schema
    def test_guidance_reference_schema(self):
        self._guidance()
        ref = self.ref
        bad = ['', ref.replace('operator_guidance', 'other'), ref + ':extra', ref.replace(':1:12:', ':+1:12:'),
               ref.replace(':1:12:', ':01:12:'), ref.replace(':1:12:', ':0:12:'),
               ref.replace(':1:12:', ':1:-12:'), ref.replace(':1:12:', ':1:' + '1'*401 + ':'), ref.replace('1,2,9,10', '9,2,1,10'),
               ref.replace('1,2,9,10', '1,2,9'), ref.replace(self.journal.events[0]['digest'], 'A'*64),
               ref.rsplit(':', 1)[0] + ':', ref.rsplit(':', 1)[0] + ':None',
               ref.rsplit(':', 1)[0] + ':/private/path', ref.rsplit(':', 1)[0] + ':' + 'x'*81, 'x'*401]
        for value in bad:
            with self.subTest(value=value):
                self.inputs = replace(self.inputs, guidance_refs=(value,))
                reads = self.journal.reads
                self._guidance_fail('GUIDANCE_SCHEMA')
                self.assertEqual(self.journal.reads, reads)
        self.inputs = replace(self.inputs, guidance_refs=(ref,))
        self.assertEqual(self._learn().rc, 0)

    # @anchor:wa.s4.mwl1h_tests.LearnTests.test_guidance_layer_contract
    def test_guidance_layer_contract(self):
        source = globals().get('MUTATED_SOURCE', Path(w.__file__).read_text())
        header = json.loads(source.splitlines()[0].split('@module ', 1)[1])
        self.assertEqual(header['layer'], 4)
        self.assertIn('wa.learn.mw_guidance', header['deps'])
        self.assertEqual(header['book'], 'HB-WA-s4-learn')
        for module in (guidance, d, w.MuseBackend, w.SubprocessVisionEngine, g):
            import inspect
            path = Path(inspect.getfile(module))
            dependency = json.loads(path.read_text().splitlines()[0].split('@module ', 1)[1])
            self.assertLessEqual(dependency['layer'], header['layer'])
        for path in (Path(cli.__file__), Path(__file__),
                     Path(__file__).with_name('test_mw_guidance_unit.py')):
            consumer = json.loads(path.read_text().splitlines()[0].split('@module ', 1)[1])
            self.assertGreaterEqual(consumer['layer'], header['layer'])

    # @anchor:wa.s4.mwl1h_tests.LearnTests.test_guidance_validator_identity
    def test_guidance_validator_identity(self):
        owner = self._guidance()
        self.assertIs(guidance.validate_payload(self.journal.events[0]['guidance']), True)
        self._guidance_fail('GUIDANCE_OWNER', owners=replace(self.owners,
            guidance=replace(owner, validate_payload=lambda payload: True)))
        self.assertEqual(self.journal.reads, 0)
        self.custody.assert_not_called()
        self.assertEqual(self._learn().category, 'DRAFT_WRITTEN')

    # @anchor:wa.s4.mwl1h_tests.LearnTests.test_guidance_validator_spoofing
    def test_guidance_validator_spoofing(self):
        owner = self._guidance()
        wrapper = lambda payload: guidance.validate_payload(payload)
        wrapper.__wrapped__ = guidance.validate_payload
        same_code = type(guidance.validate_payload)(guidance.validate_payload.__code__,
                                                   guidance.validate_payload.__globals__)
        equal = Mock(return_value=True)
        equal.__eq__ = Mock(return_value=True)
        self.assertEqual(equal, guidance.validate_payload)
        validators = (lambda payload: False, Mock(name='CANARY/private/validator', wraps=guidance.validate_payload),
                      wrapper, same_code, equal)
        for validator in validators:
            with self.subTest(validator=type(validator).__name__):
                self._guidance_fail('GUIDANCE_OWNER', owners=replace(self.owners,
                    guidance=replace(owner, validate_payload=validator)))
                self.assertEqual(self.journal.reads, 0)
                self.custody.assert_not_called()
        self.assertEqual(self._learn().category, 'DRAFT_WRITTEN')

    # @anchor:wa.s4.mwl1g_tests.LearnTests.test_guidance_payload_validator_is_real
    def test_guidance_payload_validator_is_real(self):
        self._guidance()
        good = self.journal.events[0]['guidance']
        bad = [dict(good, extra=1), dict(good, time_ns=True), dict(good, before_sequence=4)]
        missing = dict(good); missing.pop('after_sequence'); bad.append(missing)
        for payload in bad:
            self._guidance(payload=payload)
            self._guidance_fail('GUIDANCE_SCHEMA')
            self.assertIs(self.validator, guidance.validate_payload)
            self.assertIs(self.validator(payload), False)
        self._guidance()
        self.assertEqual(self._learn().rc, 0)
        self.assertIs(self.validator, guidance.validate_payload)
        self.assertIs(self.validator(self.journal.events[0]['guidance']), True)
        self.custody.assert_called()

    # @anchor:wa.s4.mwl1g_tests.LearnTests.test_guidance_journal_authentication
    def test_guidance_journal_authentication(self):
        self._guidance()
        for auth in (AdmissionCheck(Truth.FALSE, None), AdmissionCheck(Truth.UNKNOWN, Reason.MISSING), True, _accept()):
            self.journal.auth = auth
            self._guidance_fail('GUIDANCE_JOURNAL', rc=3)
        self.journal.auth = None
        original = self.journal.raw
        self.journal.raw = original.replace(b'"time_ns":12', b'"time_ns":13')
        self.owners = replace(self.owners, guidance=replace(self.owners.guidance,
                              journal_sha256=hashlib.sha256(self.journal.raw).hexdigest()))
        self._guidance_fail('GUIDANCE_JOURNAL', rc=3)
        self._guidance()
        self.assertEqual(self._learn().rc, 0)

    # @anchor:wa.s4.mwl1g_tests.LearnTests.test_guidance_journal_integrity
    def test_guidance_journal_integrity(self):
        self._guidance()
        self._guidance_fail('GUIDANCE_JOURNAL', rc=3, owners=replace(self.owners,
            guidance=replace(self.owners.guidance, journal_sha256='f'*64)))
        good = self.journal.raw
        variants = [b'', good[:-1], w._bytes(dict(w._json(good), extra=1)), good.replace(b'"seq":1', b'"seq":2'),
                    good.replace(b'"prev":null', b'"prev":"' + b'a'*64 + b'"'),
                    good.replace(b'"seq":1', b'"seq":1,"seq":1'), b' ' + good]
        for raw in variants:
            self.journal.auth = AdmissionCheck(Truth.TRUE, None)
            self.journal.raw = raw
            self.owners = replace(self.owners, guidance=replace(self.owners.guidance,
                                  journal_sha256=hashlib.sha256(raw).hexdigest()))
            self._guidance_fail('GUIDANCE_JOURNAL', rc=3)
        self._guidance()
        event = copy.deepcopy(self.journal.events[0])
        for prior in (dict(kind='intent', step_seq=1, phase='delivery'),
                      dict(kind='operator_guidance', step_seq=1, phase='terminal', guidance={'bad': 1})):
            self._guidance(events=[prior, event])
            self._guidance_fail('GUIDANCE_JOURNAL', rc=3)
        self._guidance()
        self.assertEqual(self._learn().rc, 0)

    # @anchor:wa.s4.mwl1g_tests.LearnTests.test_guidance_source_and_recorder_custody
    def test_guidance_source_and_recorder_custody(self):
        owner = self._guidance()
        for changed in (replace(self.owners, scope='foreign'),
                        replace(self.owners, guidance=replace(owner, source_workflow_sha256='b'*64))):
            self._guidance_fail('GUIDANCE_ASSOCIATION', owners=changed)
        for value in (True, g.GateResult(2, 'DENIED')):
            self.custody.side_effect = None; self.custody.return_value = value
            self._guidance_fail('GUIDANCE_ASSOCIATION')
        self.custody.side_effect = self._guidance_check
        self._guidance_fail('GUIDANCE_ASSOCIATION', owners=replace(self.owners, guidance=replace(owner, bindings=())))
        self._guidance_fail('GUIDANCE_ASSOCIATION', owners=replace(self.owners,
            guidance=replace(owner, bindings=(w.GuidanceBinding(1, 7, 'mini-demo'),))))
        self.guidance_frame_pin = 'f' * 64
        self._guidance_fail('GUIDANCE_ASSOCIATION')
        self.guidance_frame_pin = hashlib.sha256((self.demo / '7_before.png').read_bytes()).hexdigest()
        self._guidance_fail('GUIDANCE_ASSOCIATION', owners=replace(self.owners, guidance=replace(owner,
            bindings=(w.GuidanceBinding(1, 1, 'different-recording'),))))
        original_inputs = self.inputs
        self.inputs = replace(self.inputs, workflow_id='foreign')
        self._guidance_fail('GUIDANCE_ASSOCIATION')
        self.inputs = original_inputs
        self.assertEqual(self._learn().rc, 0)

    # @anchor:wa.s4.mwl1g_tests.LearnTests.test_guidance_duplicate_groups
    def test_guidance_duplicate_groups(self):
        self._guidance()
        self.inputs = replace(self.inputs, guidance_refs=(self.ref, self.ref))
        self._guidance_fail('GUIDANCE_ASSOCIATION')
        event = copy.deepcopy(self.journal.events[0])
        self._guidance(events=[event, event])
        self._guidance_fail('GUIDANCE_JOURNAL', rc=3)
        middle = copy.deepcopy(event); middle['guidance']['before_sequence'] = 5; middle['guidance']['after_sequence'] = 6
        self._guidance(events=[event, middle, event])
        self._guidance_fail('GUIDANCE_JOURNAL', rc=3)
        distinct = copy.deepcopy(event); distinct['guidance']['box_px'] = [2, 3, 8, 9]
        self._guidance(events=[event, distinct])
        self.assertEqual(self._learn().rc, 0)

    # @anchor:wa.s4.mwl1g_tests.LearnTests.test_guidance_candidate_association
    def test_guidance_candidate_association(self):
        self._guidance(step=2)
        # Valid source step is absent from the candidate; ordinary grounding alone accepts it.
        self._guidance_fail('GUIDANCE_ASSOCIATION', calls=1)
        self._guidance()
        second = dict(self.row, seq=8, mono_ns=2000)
        with (self.demo / 'events.jsonl').open('a') as stream:
            stream.write(json.dumps(second) + '\n')
        for suffix in ('before', 'after'):
            (self.demo / f'8_{suffix}.png').write_bytes((self.demo / f'7_{suffix}.png').read_bytes())
        self.projection = d.project_demo_steps(d.segment_demo(d.load_demo(self.demo, limits=d.DemoLimits())),
                                               demo_id='demo', site_id='site')
        manifest = w._json(self.inputs.demo_manifest.read_bytes())
        manifest['steps_sha256'] = hashlib.sha256(self.projection.payload).hexdigest()
        self.inputs.demo_manifest.write_bytes(w._bytes(manifest))
        self.envelope['step_evidence'][0]['demo_seq'] = 2
        self._guidance_fail('GUIDANCE_ASSOCIATION', calls=1)
        self.envelope['step_evidence'][0]['demo_seq'] = 1
        self.assertEqual(self._learn().rc, 0)

    # @anchor:wa.s4.mwl1g_tests.LearnTests.test_guidance_inputs_cli_compatibility
    def test_guidance_inputs_cli_compatibility(self):
        inputs = self.inputs
        names = tuple(w.LearnInputs.__dataclass_fields__)
        self.assertIs(type(w.LearnInputs.__dataclass_fields__), dict)
        self.assertEqual(len(names), 13)
        self.assertEqual(names, ('demo_directory', 'explanation', 'demo_manifest', 'site_metadata',
            'guide', 'schema', 'output_root', 'site_id', 'workflow_id', 'router_root',
            'router_query', 've_root', 'guidance_refs'))
        self.assertEqual(names, tuple(f.name for f in fields(inputs)))
        self.assertEqual(names[-1], 'guidance_refs')
        cli_names = tuple(name for name in names if name != 'guidance_refs')
        self.assertEqual(inputs.guidance_refs, ())
        self.assertIn('guidance_refs', w.LearnInputs.__dataclass_fields__)
        self.assertIn('guidance_refs', dict(w.LearnInputs.__dataclass_fields__.items()))
        self.assertIn('guidance_refs', [f.name for f in fields(inputs)])
        self.assertEqual(w.LearnInputs.__dataclass_fields__['guidance_refs'].default, ())
        refs = ('programmatic-reference',)
        positional = w.LearnInputs(*(getattr(inputs, name) for name in cli_names), refs)
        keyword = w.LearnInputs(**{name: getattr(inputs, name) for name in cli_names}, guidance_refs=refs)
        self.assertEqual(positional, keyword)
        self.assertEqual(replace(inputs, guidance_refs=refs), keyword)
        self.assertEqual(replace(keyword).guidance_refs, refs)
        with patch.object(self, 'inputs', keyword):
            argv = self._cli_argv()
        self.assertNotIn('--guidance-refs', argv)
        self.assertNotIn(refs[0], ' '.join(argv))
        for name in cli_names:
            value = getattr(inputs, name)
            if value is not None:
                self.assertEqual(argv[argv.index('--' + name.replace('_', '-')) + 1], str(value))
        self.assertEqual(self._learn().rc, 0)

    # @anchor:wa.s4.mwl1g_tests.LearnTests.test_guidance_candidate_conflicts
    def test_guidance_candidate_conflicts(self):
        self._guidance()
        original = copy.deepcopy(self.envelope)
        for duplicate_step, duplicate_row, demo_seq in (
                (False, True, 2), (False, True, 1),
                (True, False, 1), (True, True, 2), (True, True, 1)):
            with self.subTest(step=duplicate_step, row=duplicate_row, demo_seq=demo_seq):
                self.envelope = copy.deepcopy(original)
                if duplicate_step:
                    self.envelope['workflow']['steps'].append(copy.deepcopy(original['workflow']['steps'][0]))
                if duplicate_row:
                    self.envelope['step_evidence'].append(dict(original['step_evidence'][0], demo_seq=demo_seq))
                self._guidance_fail('GUIDANCE_ASSOCIATION', calls=1)
        self.envelope = original
        self.assertEqual(self._learn().rc, 0)

    # @anchor:wa.s4.mwl1g_tests.LearnTests.test_guidance_capacity_before_producer
    def test_guidance_capacity_before_producer(self):
        self._guidance()
        self.resources.denied = True
        self._guidance_fail('CAPACITY')
        self.assertEqual(self.journal.reads, 0)
        self.resources.denied = False
        # Many bounded, authenticated records exceed byte capacity without exceeding the count ceiling.
        event = copy.deepcopy(self.journal.events[0])
        self._guidance(events=[event] + [dict(kind='verified', step_seq=1, phase='terminal') for _ in range(800)])
        self._guidance_fail('CAPACITY')
        self._guidance()
        self._guidance_fail('CAPACITY', policy=replace(self.policy, demo_limits=replace(self.policy.demo_limits, events=1)),
            owners=replace(self.owners, guidance=replace(self.owners.guidance,
                bindings=(w.GuidanceBinding(1, 1, 'mini-demo'), w.GuidanceBinding(2, 1, 'other')))))
        self._guidance()
        low = replace(self.policy, demo_limits=replace(self.policy.demo_limits, events=1))
        self._guidance(events=[dict(kind='verified', step_seq=1, phase='terminal'), event])
        self._guidance_fail('CAPACITY', policy=low)
        self._guidance()
        self.inputs = replace(self.inputs, guidance_refs=(self.ref, self.ref))
        self._guidance_fail('CAPACITY', policy=low)
        self._guidance()
        # The exact journal byte boundary passes; one byte less refuses before OCR.
        # A larger canonical snapshot keeps the schema below the journal-byte boundary.
        records = [dict(kind='verified', step_seq=1, phase='terminal') for _ in range(300)] + [event]
        self._guidance(events=records)
        cap = len(self.journal.raw)
        with patch.object(self.describer, 'describe', wraps=self.describer.describe) as describe:
            self._guidance_fail('CAPACITY', policy=replace(self.policy, request_bytes=cap - 1))
            describe.assert_not_called()
        pack = w.build_evidence(self.inputs, policy=replace(self.policy, request_bytes=cap),
                                owners=self.owners, describer=self.describer, clock=lambda: 100)
        self.assertEqual(pack.guidance_journal_sha256, hashlib.sha256(self.journal.raw).hexdigest())
        self.assertIsInstance(pack.guidance[0].payload, bytes)
        self.assertEqual(self.resources.held, 0)
        self._guidance(payload=dict(event['guidance'], demo_ref='m' * 80))
        self.owners = replace(self.owners, guidance=replace(self.owners.guidance,
            bindings=(w.GuidanceBinding(1, 1, 'm' * 80),)))
        cap = len(self.journal.raw)
        with patch.object(w, '_read', wraps=w._read) as read:
            self._guidance_fail('CAPACITY', policy=replace(self.policy, request_bytes=cap))
            read.assert_not_called()
        self._guidance()
        rows = [dict(kind='operator_guidance', step_seq=1, phase='terminal',
                     guidance=dict(event['guidance'], box_px=[n, 2, n + 1, 10], demo_ref='m'*80))
                for n in range(1, 151)]
        owner = self._guidance(events=rows)
        refs = tuple(guidance.evidence_reference(guidance.GuidanceEvidence(1,
            **dict(e['guidance'], box_px=tuple(e['guidance']['box_px']))), e['digest']) for e in self.journal.events)
        self.inputs = replace(self.inputs, guidance_refs=refs)
        self.owners = replace(self.owners, guidance=replace(owner, step_count=150,
            bindings=tuple(w.GuidanceBinding(n, 1, 'm'*80) for n in range(1, 151))))
        cap = (sum(len(r.encode()) for r in refs) + len(w._bytes([vars(b) for b in self.owners.guidance.bindings])) +
               sum(len(w._bytes(e['guidance'])) for e in self.journal.events))
        self._guidance_fail('CAPACITY', policy=replace(self.policy, request_bytes=cap - 1))
        pack = w.build_evidence(self.inputs, policy=replace(self.policy, request_bytes=cap),
                                owners=self.owners, describer=self.describer, clock=lambda: 100)
        self.assertEqual(tuple(e.receipt_ref for e in pack.guidance), refs)
        self.assertEqual(self.resources.held, 0)
        self._guidance()
        one = replace(self.policy, demo_limits=replace(self.policy.demo_limits, events=1))
        self.assertEqual(len(w.build_evidence(self.inputs, policy=one, owners=self.owners,
                                             describer=self.describer, clock=lambda: 100).guidance), 1)
        self.assertEqual(self._learn().rc, 0)

    # @anchor:wa.s4.mwl1g_tests.LearnTests.test_guidance_revalidation
    def test_guidance_revalidation(self):
        owner = self._guidance()
        self.backend.hook = lambda: setattr(self.journal, 'raw', self.journal.raw[:-1])
        self._guidance_fail('GUIDANCE_CHANGED', calls=1, rc=3)
        owner = self._guidance()
        # Changing the trusted digest after admission must be detected even if content is unchanged.
        def changed_snapshot():
            self.journal.events.append(dict(kind='verified', step_seq=1, phase='terminal'))
            self.journal.seal()
            object.__setattr__(owner, 'journal_sha256', hashlib.sha256(self.journal.raw).hexdigest())
        self.backend.hook = changed_snapshot
        self._guidance_fail('GUIDANCE_CHANGED', calls=1, rc=3)
        self._guidance()
        self.backend.hook = lambda: setattr(self.custody, 'side_effect', lambda *a, **k: g.GateResult(2, 'REVOKED'))
        self._guidance_fail('GUIDANCE_CHANGED', calls=1, rc=3)
        self._guidance()
        self.backend.hook = lambda: (self.demo / '7_before.png').write_bytes(b'changed')
        original = (self.demo / '7_before.png').read_bytes()
        self._guidance_fail('GUIDANCE_CHANGED', calls=1, rc=3)
        (self.demo / '7_before.png').write_bytes(original)
        self._guidance()
        self.assertEqual(self._learn().rc, 0)

    # @anchor:wa.s4.mwl1g_tests.LearnTests.test_guidance_draft_oracle_unchanged
    def test_guidance_draft_oracle_unchanged(self):
        self._guidance()
        self.envelope['workflow']['oracle']['receipt_refs'] = [self.ref]
        self._guidance_fail('REPAIR_EXHAUSTED', calls=2, rc=5)
        self.assertEqual(self.backend.calls[-1]['diagnostic'], 'DRAFT_ORACLE')
        self.envelope['workflow']['oracle']['receipt_refs'] = []
        self.assertEqual(self._learn().rc, 0)
        self.assertEqual(self.backend.calls[-1]['constraints'], w._json(self.constraints))

    # @anchor:wa.s4.mwl1g_tests.LearnTests.test_guidance_privacy_and_cleanup
    def test_guidance_privacy_and_cleanup(self):
        self._guidance()
        self.custody.side_effect = RuntimeError('CANARY/private/journal')
        self._guidance_fail('GUIDANCE_ASSOCIATION')
        self.custody.side_effect = self._guidance_check
        self._guidance_fail('PRIVACY', rc=5, owners=replace(self.owners, disclosure=lambda *a: g.GateResult(5, 'DENIED')))
        self.resources.drained = False
        self._guidance_fail('DRAIN', rc=5, owners=replace(self.owners, cancelled=lambda: True))
        self.resources.drained = True
        self.assertEqual(self._learn().rc, 0)

    # @anchor:wa.s4.mwl1g_tests.LearnTests.test_guidance_empty_compatibility
    def test_guidance_empty_compatibility(self):
        self._guidance()
        self._guidance_fail('GUIDANCE_OWNER', owners=replace(self.owners, guidance=None))
        self.inputs = replace(self.inputs, guidance_refs=())
        self.journal.read = Mock(side_effect=AssertionError('unused'))
        self.validator = Mock(side_effect=AssertionError('unused'))
        self.custody.side_effect = AssertionError('unused')
        self.owners = replace(self.owners, guidance=replace(self.owners.guidance, validate_payload=self.validator))
        self.assertEqual(self._learn().rc, 0)
        self.journal.read.assert_not_called()
        self.validator.assert_not_called()
        self.custody.assert_not_called()

    # @anchor:wa.s4.mwl1g_tests.LearnTests.test_guidance_owner_controls
    def test_guidance_owner_controls(self):
        owner = self._guidance()
        from types import SimpleNamespace
        bad = [None, SimpleNamespace(**vars(owner)), replace(owner, journal_sha256='invalid'),
               replace(owner, source_workflow_sha256='invalid'), replace(owner, step_count=True),
               replace(owner, step_count=0), replace(owner, bindings=[]), replace(owner, validate_payload=None),
               replace(owner, check=None), replace(owner, journal=object()),
               replace(owner, bindings=(w.GuidanceBinding(True, 1, 'mini-demo'),)),
               replace(owner, bindings=(w.GuidanceBinding(1, True, 'mini-demo'),))]
        for value in bad:
            self._guidance_fail('GUIDANCE_OWNER', owners=replace(self.owners, guidance=value))
        self._guidance_fail('GUIDANCE_ASSOCIATION', owners=replace(self.owners,
            guidance=replace(owner, bindings=owner.bindings * 2)))
        self.assertEqual(self.journal.reads, 0)
        self.assertEqual(self._learn().rc, 0)

    # @anchor:wa.s4.mwl1g_tests.LearnTests.test_guidance_nonidentity_binding
    def test_guidance_nonidentity_binding(self):
        owner = self._guidance(step=2)
        second = copy.deepcopy(self.envelope['workflow']['steps'][0]); second['seq'] = 2
        self.envelope['workflow']['steps'].append(second)
        second_evidence = dict(self.envelope['step_evidence'][0], seq=2)
        self.envelope['step_evidence'].append(second_evidence)
        self.assertEqual(self._learn().rc, 0)
        row = self.backend.calls[-1]['passive_data']['repair_evidence'][0]
        self.assertEqual((row['step_seq'], row['demo_seq']), (2, 1))

    # @anchor:wa.s4.mwl1g_tests.LearnTests.test_guidance_journal_state_controls
    def test_guidance_journal_state_controls(self):
        self._guidance()
        event = copy.deepcopy(self.journal.events[0])
        invalid = [dict(kind='complete', step_seq=1, phase='delivery'),
                   dict(kind='intent', step_seq=1, phase='terminal'),
                   dict(kind='verified', step_seq=True, phase='terminal'),
                   dict(kind='unknown', step_seq=1, phase='terminal'),
                   dict(kind='verified', step_seq=3, phase='terminal')]
        for row in invalid:
            self._guidance(events=[event, row])
            self._guidance_fail('GUIDANCE_JOURNAL', rc=3)
        self._guidance(events=[dict(kind='intent', step_seq=1, phase='delivery'),
                               dict(kind='intent', step_seq=1, phase='delivery'),
                               dict(kind='complete', step_seq=1, phase='delivery'), event])
        self._guidance_fail('GUIDANCE_JOURNAL', rc=3)
        self._guidance(events=[event, dict(kind='intent', step_seq=1, phase='delivery')])
        self._guidance_fail('GUIDANCE_JOURNAL', rc=3)
        self._guidance(events=[dict(kind='intent', step_seq=1, phase='delivery'),
                               dict(kind='complete', step_seq=1, phase='delivery'), event])
        self.assertEqual(self._learn().rc, 0)

    # @anchor:wa.s4.mwl1g_tests.LearnTests.test_guidance_fences
    def test_guidance_fences(self):
        self._guidance()
        self._guidance_fail('CANCELLED', rc=5, owners=replace(self.owners, cancelled=lambda: True))
        self._guidance_fail('REVISION', rc=3, owners=replace(self.owners, revision_check=lambda *a: g.GateResult(3, 'STALE')))
        ticking = iter([0, 10**12])
        self._guidance_fail('DEADLINE', rc=5, clock=lambda: next(ticking, 10**12))
        self.backend.hook = lambda: object.__setattr__(self.owners, 'cancelled', lambda: True)
        self._guidance_fail('CANCELLED', calls=1, rc=5)
        self.owners = replace(self.owners, cancelled=lambda: False)
        self.backend.hook = None
        old_revision = self.owners.revision_check
        self.backend.hook = lambda: object.__setattr__(self.owners, 'revision_check', lambda *a: g.GateResult(3, 'STALE'))
        self._guidance_fail('REVISION', calls=1, rc=3)
        self.owners = replace(self.owners, revision_check=old_revision)
        now = [100]
        self.backend.hook = lambda: now.__setitem__(0, 10**12)
        self._guidance_fail('DEADLINE', calls=1, rc=5, clock=lambda: now[0])
        self.backend.hook = None
        self.assertEqual(self._learn().rc, 0)


if __name__ == '__main__':
    import sys
    if '--audit-mutants' in sys.argv:
        import subprocess
        survivors = []
        baselines = {}
        for name, (old, _, test) in MUTANTS.items():
            command = [sys.executable, '-m', 'unittest', 'tests.test_mw_learn_unit.LearnTests.'+test]
            env = {'PATH': os.defpath, 'PYTHONDONTWRITEBYTECODE': '1'}
            if test not in baselines:
                baseline = subprocess.run(command, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
                baselines[test] = baseline.returncode == 0
            target = cli if name.startswith('cli_') else w
            anchor_unique = Path(target.__file__).read_text().count(old) == 1
            if not baselines[test] or not anchor_unique:
                survivors.append(name)
                print(json.dumps({'mutant': name, 'test': test, 'killed': False,
                    'baseline_pass': baselines[test], 'anchor_unique': anchor_unique}), flush=True)
                continue
            completed = subprocess.run(command, env={**env, 'MWL1_MUTANT': name},
                                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            text = completed.stdout.decode(errors='replace')
            killed = completed.returncode != 0 and 'FAIL:' in text and 'ERROR:' not in text
            print(json.dumps({'mutant': name, 'test': test, 'killed': killed,
                'baseline_pass': True, 'anchor_unique': True, 'rc': completed.returncode}), flush=True)
            if not killed:
                survivors.append(name)
                print(text[-1600:], flush=True)
        sys.exit(bool(survivors))
    else:
        unittest.main()
