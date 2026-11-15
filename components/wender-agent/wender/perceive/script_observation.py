# @module {"id":"wa.perceive.script_observation","layer":2,"owner":"PERCEIVE","deps":["wa.perceive.redact","wa.perceive.step_binding","wa.perceive.disambig"],"book":"HB-WA-s1-perceive","summary":"Bounded immutable observations and text-first image admission."}
"""No screenshots or raw private labels are retained in public observations."""
from collections import Counter as _Counter
from dataclasses import replace as _replace
from . import redact as _r, step_binding as _b, disambig as _d

# @anchor:wa.perceive.script_observation.ObservationBundle
ObservationBundle = _r.ObservationBundle
ScopeIdentity, ObservationRef = _r.ScopeIdentity, _r.ObservationRef


# @anchor:wa.perceive.script_observation.project_cascade
def project_cascade(bundle_or_facts, *, observation_ref=None, expected_scope=None,
                    policy_revision=1, budget_port=None):
    _r._reserve(budget_port, 8192, 'script_observation.project_cascade')
    if isinstance(bundle_or_facts, ObservationBundle):
        observation_ref = bundle_or_facts.observation_ref
        facts = bundle_or_facts.target_facts_table
    else:
        facts = bundle_or_facts
    if observation_ref is None or expected_scope is None:
        raise _r._ScopeAuthorityError('explicit observation and scope required')
    _r._validate_scope_identity(expected_scope)
    if expected_scope != observation_ref.scope_identity:
        raise _r._ScopeAuthorityError('SCOPE_MISMATCH')
    payload, receipts = _r._project_fields(facts, observation_ref, 'cascade', 'script_match', policy_revision)
    _r.validate_public_payload(payload, receipts=receipts, expected_scope=expected_scope, max_bytes=2048)
    view = _r.CascadeView(observation_ref, expected_scope, payload['labels'],
        tuple(sorted(_Counter(payload['roles']).items())), payload['actionable_ids'],
        payload['landmarks'], payload['opaque_refs'], 0, receipts)
    # Fixed point includes the serialized_bytes field itself.
    for _ in range(4):
        size = len(_r._encoded(view))
        if size == view.serialized_bytes:
            break
        view = _replace(view, serialized_bytes=size)
    if view.serialized_bytes > 2048:
        raise _r._PrivacyRedactionError('CASCADE_BOUND')
    return view


# @anchor:wa.perceive.script_observation.build
def build(*, observation_id, publication_id, scope_identity, raw_elements, producer_gen16,
          srf_64, turn_index=0, scope_complete=True, stream_truncated=False, turns=(),
          join_result=None, ax_matches=None, transform_meta=None, policy_revision=1, budget_port=None):
    _r._validate_scope_identity(scope_identity)
    if not observation_id or not publication_id:
        raise ValueError('observation and publication identities required')
    _r._reserve(budget_port, 65536, 'script_observation.build')
    # Bound before constructing facts/serialization; do not drop mandatory evidence.
    if len(raw_elements) > 30:
        raise _r._PrivacyRedactionError('OBSERVATION_ROW_BOUND')
    ev = _d._verify_complete_scope_turn(turns, scope_complete_flag=scope_complete,
                                       stream_truncated=stream_truncated)
    complete = scope_complete is True and not stream_truncated and (not turns or ev.scope_complete and ev.turn_owned)
    facts = _b.normalize_facts(raw_elements, scope_identity=scope_identity, observation_id=observation_id,
        join_result=join_result, ax_matches=ax_matches, policy_revision=policy_revision, budget_port=budget_port)
    # Hash only the sanitized projection, never raw private content.
    digest = _r._digest(_r._encoded(facts))
    ref = ObservationRef(observation_id, scope_identity, producer_gen16, srf_64, digest, complete, turn_index)
    witnesses = []
    joins = _b._joins(join_result, raw_elements)
    for f, raw in zip(facts, raw_elements):
        ok, _, witness = _b.verify_association(f, raw_element=raw, observation_ref=ref,
            expected_scope=scope_identity, join_entry=joins.get(f.element_id), transform_meta=transform_meta)
        if ok:
            witnesses.append(witness)
    view = project_cascade(facts, observation_ref=ref, expected_scope=scope_identity,
                           policy_revision=policy_revision, budget_port=budget_port)
    answer = (_r.FactValue(_r.FactStatus.KNOWN, ev.answer_complete) if ev.answer_complete is not None
              else _r.FactValue(_r.FactStatus.UNKNOWN, reason=ev.reason))
    receipts = tuple(f.declassification_receipt for f in facts if f.declassification_receipt is not None) + view.receipts
    bundle = ObservationBundle(ref, scope_identity, publication_id, ev.turn_owned, answer,
                               facts, tuple(witnesses), view, receipts, 0)
    for _ in range(4):
        size = len(_r._encoded(bundle))
        if size == bundle.serialized_bytes:
            break
        bundle = _replace(bundle, serialized_bytes=size)
    if bundle.serialized_bytes > 16384:
        raise _r._PrivacyRedactionError('OBSERVATION_BOUND')
    _r.project_observation(bundle, destination='publication', purpose='audit_summary',
                          expected_scope=scope_identity, policy_revision=policy_revision, budget_port=budget_port)
    return bundle


# @anchor:wa.perceive.script_observation.image_need
def image_need(bundle, *, cascade_tier, binding=None, visual_verification_requested=False,
               required_target_bboxes=()):
    if type(cascade_tier) is not int or cascade_tier < 0:
        raise ValueError('invalid cascade tier')
    if binding is not None and binding.observation_ref != bundle.observation_ref:
        raise _r._ScopeAuthorityError('SCOPE_MISMATCH')
    if cascade_tier in (0, 1):
        return _r.ImageNeedResult(False, 'ESCALATION_FORBIDDEN_AT_TIER' if visual_verification_requested
                                 else 'NOT_NEEDED_TEXT_FIRST', (), ())
    # Successful text binding always wins, even if visual verification was requested.
    if binding is not None and binding.bound:
        return _r.ImageNeedResult(False, 'NOT_NEEDED_TEXT_FIRST', (), ())
    ambiguous = binding is not None and binding.status in ('AMBIGUOUS', 'UNRESOLVED', 'JOIN_UNCERTIFIED')
    visual = visual_verification_requested is True and len(required_target_bboxes) > 0
    return _r.ImageNeedResult(bool(ambiguous or visual), 'TEXT_AMBIGUOUS' if ambiguous else
        'VISUAL_VERIFICATION' if visual else 'NOT_NEEDED_TEXT_FIRST',
        ('clean', 'som_overlay') if ambiguous or visual else (), tuple(required_target_bboxes) if ambiguous or visual else ())
