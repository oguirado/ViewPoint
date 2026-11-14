# @module {"id":"wa.scripts.predicates","layer":2,"owner":"LEARN","deps":["wa.scripts.types"],"book":"HB-WA-s4-learn"}
"""Pure predicate evaluation over host-retained, operator-specific associations.

Private records are adapter contracts, not serialized language extensions. The
caller supplies current authority, cancellation/deadlines and the complete veto
set. This module acquires no observations and retains no inputs after return.

Authority belongs to the retained association of predicate, admitted query,
current observation and typed payload. Retaining unrelated components or making
identity-equivalent copies cannot confer authority. Inventories count targets,
not facts; states are target-specific; semantic goals require host associations;
length changes and declared relations require causal before/after observations.
Presentation flags, OCR, prompt rows and generated callables are never evidence.

Whole-tree validation counts occurrences (including shared subtrees), rejects
cycles and validates the complete bounded evidence/veto input before registry
checks. Evaluation is exhaustive even after a decisive logical branch. The
caller owns admission, registry deadlines, lifecycle revocation and reconciliation;
these local bounds do not certify aggregate admission or production latency.
"""
from dataclasses import dataclass as _dataclass
from wender.scripts import types as _t


def _require(condition, reason=_t.Reason.TYPE):
    if not condition:
        raise _t.ContractError(reason)


def _types(*pairs):
    for value, kind in pairs:
        _require(type(value) is kind)


def _text(value):
    _require(type(value) is str and bool(value))


def _facts_shape(facts):
    _types((facts, tuple))


def _record(value, kind):
    """Revalidate retained fixed-shape records before reading their authority."""
    _types((value, kind))
    try:
        value.__post_init__()
    except _t.ContractError as exc:
        if kind is _t.MissingRef and exc.reason is _t.Reason.MISSING:
            raise _t.ContractError(_t.Reason.SCHEMA) from None
        raise


def _scope_shape(scope, max_metadata_rows):
    # Bound variable metadata before S0's tuple type/scalar traversal.
    _types((scope, _t.ScopeIdentity))
    _types((scope.frame_ancestry, tuple))
    _require(len(scope.frame_ancestry) <= max_metadata_rows, _t.Reason.BOUNDS)
    _record(scope, _t.ScopeIdentity)
    if scope.attestation is not None:
        _record(scope.attestation, _t.PrivateRef)


def _registry_shape(reference):
    _record(reference, _t.RegistryRef)
    _record(reference.handle_identity, _t.PrivateRef)


def _observation_shape(obs, max_metadata_rows):
    _types((obs, _t.ObservationRef))
    _scope_shape(obs.scope, max_metadata_rows)
    _record(obs.authority, _t.AuthorityRevision)
    for reference in (obs.association, obs.completeness, obs.projection):
        _record(reference, _t.MissingRef)
    obs.__post_init__()


@_dataclass(frozen=True, slots=True, repr=False)
class _PredicateEvidence:
    """Exact retained predicate/query/current-observation/payload association."""

    predicate: _t.PredicateSpec
    query: _t.QueryDeclaration
    observation: _t.ObservationRef
    payload: object

    def __post_init__(self):
        _types((self.predicate, _t.PredicateSpec), (self.query, _t.QueryDeclaration),
               (self.observation, _t.ObservationRef))
        _require(type(self.payload) in (_InventoryEvidence, _StateEvidence,
                 _LengthEvidence, _SemanticEvidence, _TransitionEvidence))


@_dataclass(frozen=True, slots=True, repr=False)
class _InventoryEvidence:
    """Complete admitted query inventory; each row is one target and its facts."""

    inventory_reference: _t.MissingRef
    rows: tuple

    def __post_init__(self):
        _types((self.inventory_reference, _t.MissingRef), (self.rows, tuple))


@_dataclass(frozen=True, slots=True, repr=False)
class _StateEvidence:
    """Exact target-specific state, supported by source-tagged facts."""

    target: _t.PrivateRef
    state: str
    facts: tuple

    def __post_init__(self):
        _types((self.target, _t.PrivateRef), (self.state, str))
        _require(self.state in ('visible', 'enabled', 'actionable', 'editable',
                               'focused', 'busy', 'blocked'), _t.Reason.SCHEMA)
        _facts_shape(self.facts)


@_dataclass(frozen=True, slots=True, repr=False)
class _LengthEvidence:
    """Causal lengths in declared units; proves a delta, never text equality."""

    target: _t.PrivateRef
    before: _t.ObservationRef
    after: _t.ObservationRef
    before_length: int
    after_length: int
    before_unit: str
    after_unit: str
    transition_reference: _t.MissingRef
    causal_association: _t.RegistryRef
    facts: tuple

    def __post_init__(self):
        _types((self.target, _t.PrivateRef), (self.before, _t.ObservationRef),
               (self.after, _t.ObservationRef), (self.before_length, int),
               (self.after_length, int), (self.transition_reference, _t.MissingRef),
               (self.causal_association, _t.RegistryRef))
        _require(self.before_length >= 0 and self.after_length >= 0, _t.Reason.BOUNDS)
        _text(self.before_unit)
        _text(self.after_unit)
        _facts_shape(self.facts)


@_dataclass(frozen=True, slots=True, repr=False)
class _SemanticEvidence:
    """Host-retained semantic identifier and target in the exact scope."""

    identifier: str
    target: _t.PrivateRef
    scope: _t.ScopeIdentity
    host_association: _t.RegistryRef
    facts: tuple

    def __post_init__(self):
        _text(self.identifier)
        _types((self.target, _t.PrivateRef), (self.scope, _t.ScopeIdentity),
               (self.host_association, _t.RegistryRef))
        _facts_shape(self.facts)


@_dataclass(frozen=True, slots=True, repr=False)
class _TransitionEvidence:
    """Declared causal relation with advancing before/after observations."""

    transition_reference: _t.MissingRef
    target: _t.PrivateRef
    before: _t.ObservationRef
    after: _t.ObservationRef
    causal_association: _t.RegistryRef
    facts: tuple

    def __post_init__(self):
        _types((self.transition_reference, _t.MissingRef), (self.target, _t.PrivateRef),
               (self.before, _t.ObservationRef), (self.after, _t.ObservationRef),
               (self.causal_association, _t.RegistryRef))
        _facts_shape(self.facts)


def _unknown(reason):
    return _t.PredicateResult(_t.Truth.UNKNOWN, reason)


def _result(value, reason=_t.Reason.MISSING):
    return _t.PredicateResult(value, reason if value is _t.Truth.UNKNOWN else None)


def _tree(predicate, scope, max_nodes, max_metadata_rows):
    _types((predicate, _t.PredicateSpec), (scope, _t.ScopeIdentity))
    _scope_shape(scope, max_metadata_rows)
    order, stack, active = [], [(predicate, 1, False)], set()
    admitted = min(max_nodes, 32)  # Architecture ceiling [CITED in task specification].
    pending = 1
    while stack:
        node, depth, leaving = stack.pop()
        if leaving:
            active.remove(id(node))
            continue
        pending -= 1
        _types((node, _t.PredicateSpec))
        _require(id(node) not in active, _t.Reason.SCHEMA)
        _require(depth <= 8 and len(order) < admitted, _t.Reason.BOUNDS)
        _types((node.operator, _t.PredicateOperator), (node.scope, _t.ScopeIdentity),
               (node.operands, tuple), (node.children, tuple),
               (node.source_requirements, tuple), (node.transition_reference, _t.MissingRef))
        _scope_shape(node.scope, max_metadata_rows)
        _require(node.scope == scope, _t.Reason.STALE)
        _require(len(node.children) + len(order) + pending + 1 <= admitted,
                 _t.Reason.BOUNDS)
        _require(len(node.operands) <= 1 and
                 len(node.source_requirements) <= len(_t.EvidenceSource), _t.Reason.SCHEMA)
        for operand in node.operands:
            _types((operand, _t.Operand))
            operand.__post_init__()
        _require(all(type(s) is _t.EvidenceSource for s in node.source_requirements))
        _require(len(set(node.source_requirements)) == len(node.source_requirements),
                 _t.Reason.SCHEMA)
        node.transition_reference.__post_init__()
        # Arity and closed operator/state rules are the frozen S0 contract.
        # Child types must be checked before S0 reads their scopes.
        _require(all(type(c) is _t.PredicateSpec for c in node.children))
        for child in node.children:
            _scope_shape(child.scope, max_metadata_rows)
        try:
            node._validate()
        except _t.ContractError as exc:
            # Missing structural fields are a malformed program, not leaf evidence.
            if exc.reason is _t.Reason.MISSING:
                raise _t.ContractError(_t.Reason.SCHEMA) from None
            raise
        order.append(node)
        active.add(id(node))
        stack.append((node, depth, True))
        for child in reversed(node.children):
            stack.append((child, depth + 1, False))
            pending += 1
    return order


def _ingress(evidence, vetoes, max_evidence_rows, max_inventory_rows):
    _types((evidence, tuple), (vetoes, tuple))
    _require(len(evidence) + len(vetoes) <= max_evidence_rows, _t.Reason.BOUNDS)
    rows, inventory = len(evidence) + len(vetoes), 0
    seen = set()
    for veto in vetoes:
        _types((veto, _t.PredicateResult))
        veto.__post_init__()
    for binding in evidence:
        _types((binding, _PredicateEvidence))
        binding.__post_init__()
        _require(id(binding.predicate) not in seen, _t.Reason.CONFLICT)
        seen.add(id(binding.predicate))
        payload = binding.payload
        payload.__post_init__()
        if type(payload) is _InventoryEvidence:
            _record(payload.inventory_reference, _t.MissingRef)
            inventory += len(payload.rows)
            _require(inventory <= max_inventory_rows, _t.Reason.BOUNDS)
            for row in payload.rows:
                _require(type(row) is tuple and len(row) == 2)
                _types((row[0], _t.PrivateRef), (row[1], tuple))
                _record(row[0], _t.PrivateRef)
                rows += len(row[1])
                _require(rows <= max_evidence_rows, _t.Reason.BOUNDS)
                for fact in row[1]:
                    _types((fact, _t.FactEvidence))
                    _record(fact.association, _t.MissingRef)
                    _observation_shape(fact.observation, max_evidence_rows)
                    fact.__post_init__()
        else:
            _record(payload.target, _t.PrivateRef)
            if type(payload) is _SemanticEvidence:
                _scope_shape(payload.scope, max_evidence_rows)
                _registry_shape(payload.host_association)
            elif type(payload) in (_LengthEvidence, _TransitionEvidence):
                _record(payload.transition_reference, _t.MissingRef)
                _registry_shape(payload.causal_association)
            rows += len(payload.facts)
            _require(rows <= max_evidence_rows, _t.Reason.BOUNDS)
            for fact in payload.facts:
                _types((fact, _t.FactEvidence))
                _record(fact.association, _t.MissingRef)
                _observation_shape(fact.observation, max_evidence_rows)
                fact.__post_init__()


def _retained(registry, value, owner, generation):
    _require(registry.check(value, owner, generation) is True, _t.Reason.FOREIGN)


def _observation(obs, scope, registry, owner, generation, max_metadata_rows):
    _observation_shape(obs, max_metadata_rows)
    _require(obs.scope == scope, _t.Reason.STALE)
    _require(obs.producer_generation is not None and obs.association.value is not None
             and obs.completeness.value is not None and scope.attestation is not None,
             _t.Reason.MISSING)
    _retained(registry, obs, owner, generation)


def _facts(facts, node, obs, registry, owner, generation):
    sources, values = set(), set()
    missing = False
    for fact in facts:
        _retained(registry, fact, owner, generation)
        _require(fact.observation is obs, _t.Reason.STALE)
        _require(fact.source_schema == obs.producer_schema and
                 fact.source_generation == obs.producer_generation and
                 fact.association == obs.association, _t.Reason.STALE)
        sources.add(fact.source)
        if fact.value is _t.Truth.UNKNOWN:
            missing = True
        else:
            values.add(fact.value)
    _require(len(values) <= 1, _t.Reason.CONFLICT)
    _require(set(node.source_requirements) <= sources and not missing and bool(values),
             _t.Reason.MISSING)
    return next(iter(values))


def _transition(payload, node, obs, scope, registry, owner, generation, max_metadata_rows):
    _require(payload.after is obs, _t.Reason.STALE)
    before = payload.before
    _observation(before, scope, registry, owner, generation, max_metadata_rows)
    _require(before is not obs and before.publication_identity != obs.publication_identity
             and before.producer_identity == obs.producer_identity
             and before.producer_schema == obs.producer_schema
             and before.producer_generation < obs.producer_generation
             and before.capture_end_ns < obs.capture_start_ns, _t.Reason.STALE)
    _require(payload.transition_reference.value is not None and
             payload.transition_reference == node.transition_reference, _t.Reason.MISSING)
    _retained(registry, payload.causal_association, owner, generation)


def _leaf(node, scope, bindings, registry, owner, generation, observation, max_metadata_rows):
    try:
        binding = bindings.get(id(node))
        _require(binding is not None, _t.Reason.MISSING)
        _require(registry is not None and owner is not None and
                 type(generation) is int and generation >= 0, _t.Reason.MISSING)
        _retained(registry, binding, owner, generation)
        _retained(registry, node, owner, generation)
        query, payload = binding.query, binding.payload
        _retained(registry, query, owner, generation)
        # S0 traverses tuple annotations: cap the closed vocabulary first.
        _types((query.operators, tuple))
        _require(len(query.operators) <= len(_t.PredicateOperator) - 3,
                 _t.Reason.BOUNDS)
        _scope_shape(query.scope, max_metadata_rows)
        selector = query.selector
        _types((selector, _t.TargetSelector))
        _types((selector.allowed_match_levels, tuple),
               (selector.structural_identity_requirements, tuple))
        _require(len(selector.allowed_match_levels) <= len(_t.MatchLevel) and
                 len(selector.structural_identity_requirements) <= max_metadata_rows,
                 _t.Reason.BOUNDS)
        _record(selector.label_export_receipt, _t.PrivateRef)
        _record(selector.header_contract, _t.MissingRef)
        _record(selector, _t.TargetSelector)
        query.__post_init__()
        operand = node.operands[0]
        _require(query.name == operand.query_id and node.operator in query.operators,
                 _t.Reason.FOREIGN)
        _require(query.scope == scope, _t.Reason.STALE)
        _require(binding.observation is observation, _t.Reason.STALE)
        _observation(observation, scope, registry, owner, generation, max_metadata_rows)
        _retained(registry, payload, owner, generation)
        op = node.operator
        if op is _t.PredicateOperator.EXISTS_UNIQUE:
            _types((payload, _InventoryEvidence))
            _require(payload.inventory_reference.value is not None, _t.Reason.MISSING)
            _retained(registry, payload.inventory_reference, owner, generation)
            targets, matches = set(), 0
            for target, facts in payload.rows:
                _retained(registry, target, owner, generation)
                _require(target not in targets, _t.Reason.CONFLICT)
                targets.add(target)
                value = _facts(facts, node, observation, registry, owner, generation)
                matches += value is _t.Truth.TRUE
            return _result(_t.Truth.TRUE if matches == 1 else _t.Truth.FALSE)
        _retained(registry, payload.target, owner, generation)
        if op in (_t.PredicateOperator.STATE_FLAG, _t.PredicateOperator.FOCUSED):
            _types((payload, _StateEvidence))
            state = 'focused' if op is _t.PredicateOperator.FOCUSED else operand.value
            _require(payload.state == state, _t.Reason.FOREIGN)
        elif op in (_t.PredicateOperator.TASK_GOAL, _t.PredicateOperator.SCOPED_TERMINAL):
            _types((payload, _SemanticEvidence))
            _require(payload.identifier == operand.value, _t.Reason.FOREIGN)
            _require(payload.scope == scope, _t.Reason.STALE)
            _retained(registry, payload.host_association, owner, generation)
        else:
            expected = (_LengthEvidence if op is _t.PredicateOperator.VALUE_LEN_DELTA
                        else _TransitionEvidence)
            _types((payload, expected))
            _transition(payload, node, observation, scope, registry, owner, generation,
                        max_metadata_rows)
            if op is _t.PredicateOperator.DECLARED_TRANSITION:
                _require(operand.value == payload.transition_reference.value,
                         _t.Reason.FOREIGN)
        value = _facts(payload.facts, node, observation, registry, owner, generation)
        if op is _t.PredicateOperator.VALUE_LEN_DELTA:
            _require(payload.before_unit == payload.after_unit, _t.Reason.SCHEMA)
            value = _t.truth_all((value, _t.Truth.TRUE if
                payload.after_length - payload.before_length == operand.value else _t.Truth.FALSE))
        return _result(value)
    except _t.ContractError as exc:
        return _unknown(exc.reason)
    except Exception:
        # Host failures never project exception text, private values or handles.
        return _unknown(_t.Reason.TYPE)


# @anchor:wa.scripts.predicates.evaluate
def evaluate(predicate, scope, *, evidence, registry, owner, generation,
             observation, max_nodes=None, max_evidence_rows=None, max_inventory_rows=None,
             safety_vetoes):
    """Validate the whole bounded input, evaluate every leaf, then apply vetoes.

    All three bounds must be explicit positive exact integers; None refuses
    with BOUNDS. max_nodes applies to occurrences under the frozen node/depth
    ceilings. max_evidence_rows bounds bindings, vetoes and nested facts, and
    each ancestry/selector metadata tuple before S0 traversal. Inventory rows
    are bounded in aggregate by max_inventory_rows before they are scanned.

    The injected registry must retain the complete typed association and its
    authority components for this owner/incarnation/generation. Missing facts,
    sources or completeness yield UNKNOWN/MISSING; stale observations or
    provenance yield STALE; foreign associations yield FOREIGN; contradictory
    known facts or duplicate bindings/targets yield CONFLICT. Malformed inputs
    yield TYPE/SCHEMA and exceeded or absent bounds yield BOUNDS. Diagnostics
    expose only closed reasons, never private values or exception text.

    Every leaf is evaluated before applying the caller's complete independent
    safety set: any FALSE veto forces FALSE; otherwise UNKNOWN veto evidence
    prevents success while preserving a known FALSE expression. All TRUE vetoes
    preserve the expression result. This evaluator cannot discover omitted
    safety checks or grant safety authority to source-authored predicates.
    """
    try:
        for bound in (max_nodes, max_evidence_rows, max_inventory_rows):
            _require(type(bound) is int and bound > 0, _t.Reason.BOUNDS)
        order = _tree(predicate, scope, max_nodes, max_evidence_rows)
        _ingress(evidence, safety_vetoes, max_evidence_rows, max_inventory_rows)
        bindings = {id(b.predicate): b for b in evidence}
        results = {}
        for node in reversed(order):
            if not node.children:
                results[id(node)] = _leaf(node, scope, bindings, registry, owner,
                                          generation, observation, max_evidence_rows)
                continue
            children = tuple(results[id(child)] for child in node.children)
            truths = tuple(child.value for child in children)
            if node.operator is _t.PredicateOperator.NOT:
                value = _t.truth_not(truths[0])
            elif node.operator is _t.PredicateOperator.ALL:
                value = _t.truth_all(truths)
            else:
                value = _t.truth_any(truths)
            reason = next((c.reason for c in children if c.value is _t.Truth.UNKNOWN),
                          _t.Reason.MISSING)
            results[id(node)] = _result(value, reason)
        result = results[id(predicate)]
        unsafe = missing = False
        for veto in safety_vetoes:
            unsafe |= veto.value is _t.Truth.FALSE
            missing |= veto.value is _t.Truth.UNKNOWN
        if unsafe:
            return _result(_t.Truth.FALSE)
        if missing and result.value is not _t.Truth.FALSE:
            return _unknown(_t.Reason.MISSING)
        return result
    except _t.ContractError as exc:
        return _unknown(exc.reason)
    except Exception:
        return _unknown(_t.Reason.TYPE)


# @anchor:wa.scripts.predicates.evaluate_transition
def evaluate_transition(predicate, scope, *, evidence, registry, owner, generation,
                        observation, max_nodes=None, max_evidence_rows=None, max_inventory_rows=None,
                        safety_vetoes):
    """Evaluate only declared relations and causal length changes.

    Forward every bound, veto and retained-authority argument to evaluate
    unchanged. The same UNKNOWN reasons and independent veto precedence apply.
    """
    try:
        _types((predicate, _t.PredicateSpec))
        _types((predicate.operator, _t.PredicateOperator))
        _require(predicate.operator in (_t.PredicateOperator.DECLARED_TRANSITION,
                                       _t.PredicateOperator.VALUE_LEN_DELTA))
    except Exception:
        return _unknown(_t.Reason.TYPE)
    return evaluate(predicate, scope, evidence=evidence, registry=registry, owner=owner,
                    generation=generation, observation=observation, max_nodes=max_nodes,
                    max_evidence_rows=max_evidence_rows, max_inventory_rows=max_inventory_rows,
                    safety_vetoes=safety_vetoes)
