# @module {"id":"wa.tests.scripts.test_observation_mutants","layer":2,"owner":"PERCEIVE","deps":[],"book":"HB-WA-s1-perceive"}
"""Remove one safety check at a time; the public API suite must reject each mutant.

Mutations stay in RAM, and each original function is restored by patch.object.
"""
import types
import inspect
import textwrap
import unittest
from unittest.mock import patch
from wender.perceive import redact as r, step_binding as b, script_observation as o
from wender.perceive import disambig as d, element_table as et
from tests.scripts import test_observation_privacy as _p

# ID, module, function, exact removed guard, replacement, detecting public API test.
_MUTANTS = (
    ('binding-private-overreach',b,'bind_target','    private = False','    private = label is not None','binding'),
    ('binding-private-evidence',b,'bind_target','f.opaque_label_ref is not None and not f.public_label.is_known()','not f.public_label.is_known()','binding'),
    ('binding-private-refusal',b,'bind_target','private |= f.opaque_label_ref is not None and not f.public_label.is_known()','private = False','binding'),
    ('binding-role-filter',b,'bind_target','if any(not getattr(f, field).is_known() or getattr(f, field).value != _r._normalize_pinned_utf8_nfc_casefold(selector[key])','if any(False','binding'),
    ('binding-role-code-filter',b,'bind_target',"if 'role_code' in selector and",'if False and','binding'),
    ('binding-container-filter',b,'bind_target',"if 'container_key' in selector and",'if False and','binding'),
    ('binding-ordinal-filter',b,'bind_target',"if type(ordinal) is not int or e.get('ordinal') != ordinal:",'if False:','binding'),
    ('binding-containment-before-privacy',b,'bind_target','if not any(_d._verify_landmark_containment(e, c)[0] for c in containers):','if False:','binding'),
    ('binding-normalize-landmark-fact',b,'normalize_facts','_r._normalize_pinned_utf8_nfc_casefold(e.landmark_role)','e.landmark_role','binding'),
    ('binding-normalize-container-fact',b,'normalize_facts','_r._normalize_pinned_utf8_nfc_casefold(str(e.container_key))','str(e.container_key)','binding'),
    ('binding-normalize-container-selector',b,'bind_target',"_r._normalize_pinned_utf8_nfc_casefold(str(selector['container_key']))","str(selector['container_key'])",'binding'),
    ('binding-normalize-landmark-selector',b,'bind_target',"_r._normalize_pinned_utf8_nfc_casefold(selector['landmark_role'])","selector['landmark_role']",'binding'),
    ('binding-normalize-container-role',b,'bind_target',"_r._normalize_pinned_utf8_nfc_casefold(c['role'])","c['role']",'binding'),
    ('records-recursive-immutability',r,'_immutable',"raise TypeError('record requires recursively immutable values')",'return','facts'),
    ('normalization-casefold',r,'_normalize_pinned_utf8_nfc_casefold',"_ud.normalize('NFC', _ud.normalize('NFC', text).casefold())","_ud.normalize('NFC', text)",'declassify'),
    ('normalization-nfc',r,'_normalize_pinned_utf8_nfc_casefold',"_ud.normalize('NFC', _ud.normalize('NFC', text).casefold())","text.casefold()",'declassify'),
    ('normalization-whitespace',r,'_normalize_pinned_utf8_nfc_casefold',"return ' '.join(text.split())","return text",'declassify'),
    ('facts-wire-schema',et,'_extract_raw_script_evidence',"e.get('wire_schema') == 'ax_wire_v1'","True",'facts'),
    ('facts-gen-range',et,'_extract_raw_script_evidence','not 0 <= gen <= 65535','False','facts'),
    ('facts-missing-gen',et,'_extract_raw_script_evidence',"gen = integer('gen16', 'generation')","gen = integer('gen16', 'generation') or 7",'facts'),
    ('facts-visible',b,'verify_association','(f.visible, f.enabled, f.actionable)','(f.enabled, f.actionable)','facts'),
    ('facts-enabled',b,'verify_association','(f.visible, f.enabled, f.actionable)','(f.visible, f.actionable)','facts'),
    ('facts-actionable',b,'verify_association','(f.visible, f.enabled, f.actionable)','(f.visible, f.enabled)','facts'),
    ('facts-generation-equality',b,'verify_association','f.gen16.value != ref.producer_gen16','False','facts'),
    ('scope-partial',b,'bind_target','ref.scope_complete is not True','False','scope'),
    ('scope-streaming',d,'_verify_complete_scope_turn',"turn.get('streaming') is not False","False",'scope'),
    ('scope-complete',d,'_verify_complete_scope_turn',"turn.get('complete') is not True","False",'scope'),
    ('scope-truncated',d,'_verify_complete_scope_turn',"t.get('truncated') is True","False",'scope'),
    ('scope-partial-subtree',d,'_verify_complete_scope_turn',"t.get('partial_scope') is True","False",'scope'),
    ('scope-turn-ownership',d,'_verify_complete_scope_turn',"turn['turn_id'] != expected_turn_id","False",'scope'),
    ('scope-geometric',d,'_verify_landmark_containment','linked and geometric','linked','scope'),
    ('scope-structural',d,'_verify_landmark_containment','linked and geometric','geometric','scope'),
    ('scope-candidate-turn',b,'bind_target',"if turn_ev and e.get('turn_id') != turn_ev.selected_turn_id:","if False:",'scope'),
    ('join-iou',b,'verify_association',"0.5 <= j['iou'] <= 1.0","True",'join'),
    ('join-center',b,'verify_association',"j.get('center_in_box') is True","True",'join'),
    ('join-matched',b,'verify_association',"j.get('matched') is True","True",'join'),
    ('join-backend',b,'verify_association',"j.get('backend_id', j.get('node_id')) == f.backend_node_id.value","True",'join'),
    ('join-stale',b,'verify_association',"meta.get('stale_epoch') is True","False",'join'),
    ('join-epoch',b,'verify_association',"meta['transform_epoch'] != ref.turn_index","False",'join'),
    ('join-coord-space',b,'verify_association',"meta.get('coord_space') not in ('viewport_css', 'device_px')","False",'join'),
    ('join-click-point',b,'verify_association','if not (x <= cx < x+w and y <= cy < y+h):','if False:','join'),
    ('image-tier',o,'image_need','if cascade_tier in (0, 1):','if False:','image'),
    ('image-text-first',o,'image_need','if binding is not None and binding.bound:','if False:','image'),
    ('image-parent',r,'project_image','if not parent_publication_id or not source_observation_id:','if False:','image'),
    ('image-required-evidence',r,'project_image','if any(not allowed(x, y) for y in range(box[1], box[1]+box[3])','if any(False for y in range(box[1], box[1]+box[3])','image'),
    ('image-private-mask',r,'project_image','and not any(\n            _inside(x, y, b) for b in private_regions)','and True','image'),
    ('image-allowlist',r,'project_image','any(_inside(x, y, b) for b in approved_regions)','True','image'),
    ('privacy-sensitive',r,'sanitize_text','if _SECRET_OR_INJECTION_PATTERNS.search(text):','if False:','privacy'),
    ('privacy-classification',r,'sanitize_text',"if classification not in ('public_ui_constant', 'schema_enum', 'declassified_public'):",'if False:','privacy'),
    ('principal-host-attestation',r,'_validate_scope_identity','scope.host_attested is not True','False','principal'),
    ('principal-origin-path',r,'_validate_scope_identity','or parsed.path or parsed.query','or False or False','principal'),
    ('principal-binding',b,'bind_target','ref.scope_identity != expected_scope','False','principal'),
    ('principal-projection',r,'project_observation','if bundle.scope_identity != expected_scope or bundle.observation_ref.scope_identity != expected_scope:','if False:','principal'),
    ('declassify-digest',r,'validate_public_payload',"r.content_sha256 == _digest(value.encode()) and r.field_or_region == path","r.field_or_region == path",'declassify'),
    ('declassify-revision',r,'_receipt_valid','receipt.policy_revision >= 1','True','declassify'),
    ('declassify-scope',r,'_receipt_valid','receipt.scope_identity == scope','True','declassify'),
    ('declassify-field',r,'validate_public_payload','and r.field_or_region == path','','declassify'),
    ('declassify-chain',r,'_receipt_valid','bool(receipt.transform_chain)','True','declassify'),
    ('declassify-private-derived',r,'_project_fields',"raise _PrivacyRedactionError('LABEL_PROVENANCE_MISMATCH')","pass",'declassify'),
    ('declassify-reservation',r,'_reserve',"port.reserve(size, stage=stage) is False","False",'declassify'),
    ('declassify-raw-cap',r,'sanitize_text','if len(raw_text) > 4096:','if False:','declassify'),
    ('declassify-image-cap',r,'project_image','or len(raw_pixels) > min(max_bytes, 262144)','or False','declassify'),
    ('bounds-public-label',r,'sanitize_text',"len(text.encode('utf-8')) > 128","False",'bounds'),
    ('bounds-utf8-bytes',r,'sanitize_text','if len(data) > 4096:','if False:','bounds'),
    ('bounds-cascade',o,'project_cascade','if view.serialized_bytes > 2048:','if False:','bounds'),
)


# @anchor:wa.perceive.script_observation.tests.ObservationMutationTests
class ObservationMutationTests(unittest.TestCase):
    # @anchor:wa.perceive.script_observation.tests.ObservationMutationTests.test_each_removed_guard_is_detected
    def test_each_removed_guard_is_detected(self):
        names = {key: next(name for name in dir(_p.ObservationPrivacyTests) if name.startswith('test_g_'+key+'_'))
                 for key in ('facts','scope','join','image','privacy','principal','declassify')}
        names['binding'] = 'test_binding_structural_privacy_and_normalization'
        for identifier,module,function,old,new,gate in _MUTANTS:
            with self.subTest(mutant=identifier):
                original=getattr(module,function)
                source=textwrap.dedent(inspect.getsource(original))
                self.assertEqual(source.count(old),1,identifier)
                namespace=dict(vars(module))
                exec(compile(source.replace(old,new),'<S4-mutant:'+identifier+'>','exec'),namespace)
                mutated=namespace[function]
                live=types.FunctionType(mutated.__code__,vars(module),function,mutated.__defaults__,mutated.__closure__)
                live.__kwdefaults__=mutated.__kwdefaults__
                case = _p.ObservationBoundsTests('test_public_caps_and_invalid_ingress') if gate == 'bounds' else _p.ObservationPrivacyTests(names[gate])
                outcome=unittest.TestResult()
                with patch.object(module,function,live):
                    case.run(outcome)
                self.assertTrue(outcome.failures,identifier+': mutant must cause an assertion failure, not merely an exception; '+str(outcome.errors))
                print('MUTANT_KILLED '+identifier+' [MEASURED]')
