# @module {"id":"wa.perceive.step_binding","layer":2,"owner":"PERCEIVE","deps":["wa.perceive.redact","wa.perceive.element_table","wa.perceive.disambig"],"book":"HB-WA-s1-perceive","summary":"Producer facts and fail-closed target witnesses."}
"""Script target binding never uses ElementTable presentation defaults."""
from . import redact as _r, element_table as _et, disambig as _d

FactStatus, FactValue = _r.FactStatus, _r.FactValue
ScopeIdentity, ObservationRef = _r.ScopeIdentity, _r.ObservationRef
TargetFacts, ExecutionWitness, TargetBinding = _r.TargetFacts, _r.ExecutionWitness, _r.TargetBinding


def _fact(value, reason='MISSING_PRODUCER_FACT', unavailable=False):
    return (FactValue(FactStatus.KNOWN, value) if value is not None else
            FactValue(FactStatus.UNAVAILABLE if unavailable else FactStatus.UNKNOWN, reason=reason))


def _joins(result, raw_elements=()):
    if result is None:
        return {}
    if not isinstance(result, dict):
        raise ValueError('join result must be a mapping')
    if 'pairs' in result:
        joined = {}
        for raw in raw_elements:
            backend = raw.get('backend_id', raw.get('backendDOMNodeId', raw.get('backend_node_id')))
            eid = raw.get('element_id', raw.get('id'))
            entries = [p for p in result['pairs'] if backend is not None and p.get('node_id') == str(backend)]
            if len(entries) != 1:
                continue
            pair = entries[0]
            box, mark = raw.get('bbox'), raw.get('mark_bbox')
            center = (box is not None and mark is not None and len(box) == len(mark) == 4
                and all(type(v) is int for v in (*box, *mark)) and min(*box[2:], *mark[2:]) > 0
                and 2*box[0] <= 2*mark[0]+mark[2] < 2*(box[0]+box[2])
                and 2*box[1] <= 2*mark[1]+mark[3] < 2*(box[1]+box[3]))
            den, num = pair.get('iou_den'), pair.get('iou_num')
            joined[eid] = dict(mark_id=pair.get('mark_id'), backend_id=backend,
                matched=pair.get('verdict') == 'matched', center_in_box=bool(center),
                iou=num/den if type(num) is int and type(den) is int and den > 0 else None)
        return joined
    return result.get('by_element_id', result)


# @anchor:wa.perceive.step_binding.normalize_facts
def normalize_facts(raw_elements, *, scope_identity, observation_id, join_result=None,
                    ax_matches=None, policy_revision=1, budget_port=None):
    _r._validate_scope_identity(scope_identity)
    raw = _et._extract_raw_script_evidence(raw_elements, join_map=_joins(join_result, raw_elements), ax_matches=ax_matches)
    out = []
    for e in raw:
        role = _r._normalize_pinned_utf8_nfc_casefold(e.role) if type(e.role) is str else None
        if role not in _r._SCHEMA_ROLES:
            role = None
        landmark = (_r._normalize_pinned_utf8_nfc_casefold(e.landmark_role)
                    if type(e.landmark_role) is str else None)
        if landmark not in _r._SCHEMA_ROLES:
            landmark = None
        container = (_r._normalize_pinned_utf8_nfc_casefold(str(e.container_key))
                     if e.container_key is not None else None)
        label, opaque, receipt = _r.sanitize_text(e.raw_label, scope_identity=scope_identity,
            source_observation_id=observation_id, field_name=f'element:{e.element_id}:label',
            classification=e.label_source or 'unknown', policy_revision=policy_revision, budget_port=budget_port)
        out.append(TargetFacts(e.element_id, _fact(e.role_code), _fact(role),
            *(_fact(v) for v in (e.explicit_visible, e.explicit_enabled, e.explicit_actionable,
                                 e.explicit_focused, e.explicit_value_nonempty)),
            _fact(e.bbox), _fact(e.click_point), _fact(e.mark_id), _fact(e.backend_node_id),
            _fact(e.match_level, 'UNMEASURED_MATCH_LEVEL'), _fact(e.gen16, 'MISSING_GEN16', True),
            _fact(landmark), _fact(container), label, opaque, receipt))
    return tuple(out)


# @anchor:wa.perceive.step_binding.verify_association
def verify_association(target_facts, *, raw_element, observation_ref, expected_scope,
                       join_entry=None, transform_meta=None, require_mark_join=False):
    _r._validate_scope_identity(expected_scope)
    ref, f = observation_ref, target_facts
    if ref.scope_identity != expected_scope:
        return False, 'SCOPE_MISMATCH', None
    if ref.scope_complete is not True:
        return False, 'PARTIAL_SCOPE_UNVERIFIED', None
    # Re-read producer record, so a caller cannot substitute a fact from another snapshot.
    match = {f.element_id: {'level': f.match_level.value}} if f.match_level.is_known() else None
    actual = normalize_facts((raw_element,), scope_identity=expected_scope,
        observation_id=ref.observation_id, join_result={f.element_id: join_entry} if join_entry else None,
        ax_matches=match)[0]
    if (actual != f or not all(v.is_known() and v.value is True for v in (f.visible, f.enabled, f.actionable))
            or not f.gen16.is_known() or f.gen16.value != ref.producer_gen16
            or not f.match_level.is_known() or f.match_level.value not in ('L0_EXACT', 'L1_NORM')):
        return False, 'INCOMPLETE_OR_FALSE_EXECUTION_FACTS', None
    meta = transform_meta or {}
    if (meta.get('stale_epoch') is True or type(meta.get('transform_epoch')) is not int
            or meta['transform_epoch'] != ref.turn_index
            or meta.get('coord_space') not in ('viewport_css', 'device_px')):
        return False, 'STALE_TRANSFORM_EPOCH', None
    certified = False
    if require_mark_join or f.mark_id.is_known():
        j = join_entry or {}
        certified = (j.get('matched') is True and type(j.get('iou')) in (int, float)
            and 0.5 <= j['iou'] <= 1.0 and j.get('center_in_box') is True
            and f.backend_node_id.is_known() and j.get('backend_id', j.get('node_id')) == f.backend_node_id.value
            and f.mark_id.is_known() and j.get('mark_id') == f.mark_id.value)
        if not certified:
            return False, 'JOIN_UNCERTIFIED', None
    if not f.bbox.is_known() or not f.click_point.is_known():
        return False, 'INCOMPLETE_OR_FALSE_EXECUTION_FACTS', None
    x, y, w, h = f.bbox.value
    cx, cy = f.click_point.value
    if not (x <= cx < x+w and y <= cy < y+h):
        return False, 'INCOMPLETE_OR_FALSE_EXECUTION_FACTS', None
    word = _et._extract_raw_script_evidence((raw_element,))[0].ax_state_word
    if word is None:
        # An execution witness needs the actual wire word, not synthesized bits.
        return False, 'INCOMPLETE_OR_FALSE_EXECUTION_FACTS', None
    return True, 'OK', ExecutionWitness(ref.observation_id, expected_scope, f.element_id,
        f.backend_node_id, f.mark_id, f.gen16.value, word, f.bbox.value, f.click_point.value,
        meta['coord_space'], meta['transform_epoch'], certified, f.match_level.value)


# @anchor:wa.perceive.step_binding.bind_target
def bind_target(selector, *, facts_table, raw_elements, observation_ref, expected_scope,
                turns=(), join_result=None, transform_meta=None, require_mark_join=False):
    ref = observation_ref
    def result(status, reason, candidates=(), fact=None, witness=None):
        return TargetBinding(status == 'BOUND', status, reason, ref, fact, witness,
                             tuple(f.element_id for f in candidates), status == 'BOUND')
    _r._validate_scope_identity(expected_scope)
    if ref.scope_identity != expected_scope or ref.scope_complete is not True:
        return result('SCOPE_MISMATCH', 'SCOPE_MISMATCH')
    turn_ev = None
    if turns or selector.get('require_answer_turn'):
        turn_ev = _d._verify_complete_scope_turn(turns, expected_turn_id=selector.get('turn_id'),
                                                scope_complete_flag=ref.scope_complete)
        if not turn_ev.turn_owned:
            return result('SCOPE_MISMATCH', turn_ev.reason)
    label = selector.get('public_label')
    if label is not None and selector.get('classification', 'public_ui_constant') not in (
            'public_ui_constant', 'schema_enum', 'declassified_public'):
        return result('PRIVACY_BLOCKED', 'PRIVATE_SELECTOR_LABEL_FORBIDDEN')
    raw = {e.get('element_id', e.get('id')): e for e in raw_elements}
    candidates = []
    private = False
    for f in facts_table:
        e = raw.get(f.element_id)
        if e is None:
            continue
        if turn_ev and e.get('turn_id') != turn_ev.selected_turn_id:
            continue
        if any(not getattr(f, field).is_known() or getattr(f, field).value != _r._normalize_pinned_utf8_nfc_casefold(selector[key])
               for key, field in (('role', 'role_name'), ('landmark_role', 'landmark_role')) if key in selector):
            continue
        if 'role_code' in selector and (not f.role_code.is_known() or f.role_code.value != selector['role_code']):
            continue
        if 'container_key' in selector and (not f.container_key.is_known() or f.container_key.value !=
                _r._normalize_pinned_utf8_nfc_casefold(str(selector['container_key']))):
            continue
        if 'opaque_label_ref' in selector and f.opaque_label_ref != selector['opaque_label_ref']:
            continue
        if 'landmark_role' in selector:
            containers = [c for c in raw_elements if type(c.get('role')) is str and
                _r._normalize_pinned_utf8_nfc_casefold(c['role']) ==
                _r._normalize_pinned_utf8_nfc_casefold(selector['landmark_role'])]
            if not any(_d._verify_landmark_containment(e, c)[0] for c in containers):
                continue
        if 'ordinal' in selector:
            ordinal = selector['ordinal']
            # Ordinals must be producer evidence; never use iteration order.
            if type(ordinal) is not int or e.get('ordinal') != ordinal:
                continue
        if label is not None and (not f.public_label.is_known() or f.public_label.value !=
                _r._normalize_pinned_utf8_nfc_casefold(label)):
            private |= f.opaque_label_ref is not None and not f.public_label.is_known()
            continue
        candidates.append(f)
    if not candidates:
        return result('PRIVACY_BLOCKED' if private else 'UNRESOLVED', 'NO_MATCH')
    if len(candidates) != 1:
        return result('AMBIGUOUS', 'MULTIPLE_CANDIDATES', candidates)
    f = candidates[0]
    ok, reason, witness = verify_association(f, raw_element=raw[f.element_id], observation_ref=ref,
        expected_scope=expected_scope, join_entry=_joins(join_result, raw_elements).get(f.element_id),
        transform_meta=transform_meta, require_mark_join=require_mark_join)
    status = {'STALE_TRANSFORM_EPOCH': 'STALE_EPOCH', 'JOIN_UNCERTIFIED': 'JOIN_UNCERTIFIED',
              'SCOPE_MISMATCH': 'SCOPE_MISMATCH'}.get(reason, 'NOT_ACTIONABLE')
    return result('BOUND' if ok else status, reason, candidates, f, witness)
