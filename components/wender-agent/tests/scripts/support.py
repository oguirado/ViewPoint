# @module {"id":"wa.tests.scripts.support","layer":2,"owner":"LEARN","deps":["wa.scripts.types"],"book":"HB-WA-s4-learn"}
"""Trusted synthetic fixtures; no production authority registry is instantiated."""
from dataclasses import replace as _replace
from wender.scripts import types as _t


# @anchor:wa.tests.scripts.support.FakeClock
class FakeClock:
    def __init__(self, now_ns=0):
        self.now_ns = now_ns

    # @anchor:wa.tests.scripts.support.FakeClock.advance
    def advance(self, elapsed_ns):
        if type(elapsed_ns) is not int or elapsed_ns < 0:
            raise _t.ContractError(_t.Reason.BOUNDS)
        self.now_ns += elapsed_ns
        return self.now_ns


# @anchor:wa.tests.scripts.support.IdentityRegistry
class IdentityRegistry:
    """Retains objects, rejecting equality copies and foreign/stale owner state."""
    def __init__(self, owner, generation):
        self.owner, self.generation = owner, generation
        self._retained = []

    # @anchor:wa.tests.scripts.support.IdentityRegistry.register
    def register(self, value):
        self._retained.append((value, self.owner, self.generation))
        return value

    # @anchor:wa.tests.scripts.support.IdentityRegistry.mint
    def mint(self, kind, token):
        return self.register(_t.RegistryRef(kind, self.owner.incarnation, self.generation,
                                           _t.PrivateRef(self.owner.owner, self.owner.incarnation, token)))

    # @anchor:wa.tests.scripts.support.IdentityRegistry.outstanding
    def outstanding(self):
        return tuple(item for item, _, _ in self._retained)

    # @anchor:wa.tests.scripts.support.IdentityRegistry.check
    def check(self, value, owner, generation):
        if owner != self.owner:
            raise _t.ContractError(_t.Reason.FOREIGN)
        if generation != self.generation:
            raise _t.ContractError(_t.Reason.STALE)
        if not any(value is item and owner == registered_owner and generation == registered_generation
                   for item, registered_owner, registered_generation in self._retained):
            raise _t.ContractError(_t.Reason.FOREIGN)
        return True


# @anchor:wa.tests.scripts.support.CountedCallback
class CountedCallback:
    def __init__(self):
        self.calls = []
        self.pending_callbacks = []

    # @anchor:wa.tests.scripts.support.CountedCallback.enqueue
    def enqueue(self, *args):
        self.pending_callbacks.append(args)

    # @anchor:wa.tests.scripts.support.CountedCallback.drain
    def drain(self):
        while self.pending_callbacks:
            self(*self.pending_callbacks.pop(0))

    def __call__(self, *args):
        self.calls.append(args)


# @anchor:wa.tests.scripts.support.TrustedExportReceiptChecker
class TrustedExportReceiptChecker:
    """Trusted fake binding retained receipts to the requested export context."""
    def __init__(self, owner, generation):
        self.registry = IdentityRegistry(owner, generation)

    # @anchor:wa.tests.scripts.support.TrustedExportReceiptChecker.register
    def register(self, receipt):
        return self.registry.register(receipt)

    # @anchor:wa.tests.scripts.support.TrustedExportReceiptChecker.check
    def check(self, receipt: _t.ExportReceipt, source: _t.ObservationRef,
              transform_chain: tuple[str, ...], content_identity: str,
              destination: _t.ExportDestination, purpose: _t.Purpose,
              privacy_policy_revision: int) -> _t.AdmissionCheck:
        self.registry.check(receipt, self.registry.owner, self.registry.generation)
        approved = (receipt.source_observation, receipt.transform_chain,
                    receipt.content_identity, receipt.destination, receipt.purpose,
                    receipt.privacy_policy_revision)
        requested = (source, transform_chain, content_identity, destination, purpose,
                     privacy_policy_revision)
        if requested != approved:
            raise _t.ContractError(_t.Reason.FOREIGN)
        return _t.AdmissionCheck(_t.Truth.TRUE, None)


# @anchor:wa.tests.scripts.support.LockTrace
class LockTrace:
    def __init__(self):
        self.events = []
        self.fence = self.store = False

    # @anchor:wa.tests.scripts.support.LockTrace.enter_fence
    def enter_fence(self):
        if self.store:
            raise _t.ContractError(_t.Reason.FOREIGN)
        self.fence = True
        self.events.append('fence')

    # @anchor:wa.tests.scripts.support.LockTrace.enter_store
    def enter_store(self):
        if not self.fence:
            raise _t.ContractError(_t.Reason.FOREIGN)
        self.store = True
        self.events.append('store')

    # @anchor:wa.tests.scripts.support.LockTrace.actuate
    def actuate(self, callback):
        if self.store or not self.fence:
            raise _t.ContractError(_t.Reason.FOREIGN)
        callback()
        self.events.append('input')


# @anchor:wa.tests.scripts.support.make_fixture
def make_fixture():
    """Synthetic attestation and digests; never usable with a production owner."""
    handle = _t.PrivateRef('test-owner', 'test-incarnation', 'synthetic-token')
    scope = _t.ScopeIdentity('test-principal', 'test-tenant', 'test-profile',
                             'https://example.test', 'https://example.test',
                             ('https://example.test',), 1, handle, None)
    authority = _t.AuthorityRevision(1, 1, 1, None, _t.Reason.MISSING, 1, 1)
    observation = _t.ObservationRef(scope, 'test-publication', 'test-producer', 7,
                                  'test-schema', 10, 20,
                                  *(_t.MissingRef('test-evidence', None) for _ in range(3)),
                                  'test-provenance', None, authority)
    fact = _t.FactEvidence(_t.Truth.TRUE, _t.EvidenceSource.AX, 'test-schema', 7,
                           observation, observation.association, None)
    negative = _replace(fact, value=_t.Truth.FALSE)
    facts = _t.TargetFacts(fact, fact, fact, fact, fact, negative, negative)
    selector = _t.TargetSelector('button', 'approved-static-label', handle, 'main',
                                'test-container', 'test-turn', None, None,
                                _t.MissingRef(None, _t.Reason.MISSING), _t.ActionKind.CLICK,
                                (_t.MatchLevel.L0_EXACT,), ('owned-role',))
    operand = _t.Operand('test-operand', 'test-query', _t.ValueKind.TEXT, 'approved-enum')
    entry = _t.PredicateSpec(_t.PredicateOperator.EXISTS_UNIQUE, (operand,), (), scope,
                             (_t.EvidenceSource.AX,), _t.MissingRef(None, _t.Reason.MISSING))
    post = _replace(entry, operator=_t.PredicateOperator.DECLARED_TRANSITION,
                    transition_reference=_t.MissingRef('expected-transition', None))
    effect = _t.EffectSpec(_t.ActionKind.CLICK, selector, (), entry, post,
                           _t.VerificationBudget(100, 2, 1000))
    terminal = _t.TerminalGoalRef('independent-host-goal', scope, handle)
    abis = _t.ABISet('dsl-test', 'compiler-test', 'act-test', 'predicate-test', 'schema-test')
    identity = _t.ArtifactIdentity(_t.PlanDigest('sha256', 'a' * 64),
                                    _t.MetadataDigest('sha256', 'b' * 64),
                                    _t.ArtifactDigest('sha256', 'c' * 64),
        'logical', 1, scope, 'signature', _t.PlanDigest('sha256', 'd' * 64),
        'goal', _t.PlanDigest('sha256', 'e' * 64), abis)
    limits = _t.PlanLimits(4, 1024, 1000, 4, 128, 128)
    nodes = (_t.StepPlan('effect', _t.StepKind.CLICK, effect, None, None, None),
             _t.StepPlan('terminal', _t.NodeKind.TERMINAL, None, None, None, terminal))
    header = _t.ScriptHeader('logical', 1, _t.MissingRef(None, _t.Reason.MISSING),
        scope, 'site', 'goal', 'signature', abis, (), (), limits, entry, terminal.reference,
        (), identity, None)
    queries = (_t.QueryDeclaration('test-query', selector, scope,
        (_t.PredicateOperator.EXISTS_UNIQUE, _t.PredicateOperator.DECLARED_TRANSITION)),)
    definition = _t.PlanDefinition(header, nodes, (selector,), (), (), entry, terminal,
        limits, identity, identity.structural_screen_digest, identity.source_digest, queries)
    compiler = _t.RegistryRef('compiler', 'test-incarnation', 7, handle)
    plan = _t.ValidatedPlan(nodes, (), (), entry, terminal, limits, abis, identity, scope,
                           handle, definition, b'synthetic-canonical-bytes', compiler, queries)
    layout = _t.ProducerLayout(_t.EvidenceSource.AX, 'test-schema', handle)
    witness = _t.ExecutionWitness(scope, observation, 'test-schema', layout, 7,
                                   'test-container', (_t.ActionKind.CLICK,), facts,
                                   handle, 1000, _t.MissingRef(None, _t.Reason.MISSING),
                                   _t.MissingRef(None, _t.Reason.MISSING), handle)
    return dict(handle=handle, scope=scope, authority=authority, observation=observation,
                fact=fact, facts=facts, selector=selector, entry=entry, post=post,
                effect=effect, terminal=terminal, identity=identity, abis=abis,
                limits=limits, plan=plan, witness=witness)


# @anchor:wa.tests.scripts.support.TrustedAuthorityChecker
class TrustedAuthorityChecker:
    """Injected trusted current state; admission gates the counted boundary."""
    def __init__(self, scope, authority, callback):
        self.scope, self.authority, self.callback = scope, authority, callback

    # @anchor:wa.tests.scripts.support.TrustedAuthorityChecker.admit
    def admit(self, intent):
        if intent.scope != self.scope or intent.authority != self.authority:
            raise _t.ContractError(_t.Reason.STALE)
        self.callback(intent)
        return _t.AdmissionCheck(_t.Truth.TRUE, None)


# @anchor:wa.tests.scripts.support.verify_routing_manifest
def verify_routing_manifest(root, manifest, changed):
    """Common routing/write-scope gate for canonical files and mutated fixtures.

    manifest maps relative file names to their exact module identities. The
    existing index builder supplies the public-definition anchor coverage gate.
    """
    import json
    import re
    from pathlib import Path
    from tools import build_code_index as builder
    root = Path(root).resolve()
    if not set(changed) <= set(manifest):
        raise _t.ContractError(_t.Reason.FOREIGN)
    anchors = []
    for rel, module_id in manifest.items():
        path = root / rel
        if path.is_symlink() or path.resolve() != path.absolute() or not path.is_file():
            raise _t.ContractError(_t.Reason.FOREIGN)
        source = path.read_text()
        meta = json.loads(source.splitlines()[0].split('@module ', 1)[1])
        if (meta.get('id'), meta.get('owner'), meta.get('book')) != (
                module_id, 'LEARN', 'HB-WA-s4-learn'):
            raise _t.ContractError(_t.Reason.SCHEMA)
        ids = re.findall(r'^\s*# @anchor:(\S+)\s*$', source, re.MULTILINE)
        if any(not anchor.startswith(module_id + '.') for anchor in ids):
            raise _t.ContractError(_t.Reason.SCHEMA)
        anchors.extend(ids)
    if len(anchors) != len(set(anchors)):
        raise _t.ContractError(_t.Reason.SCHEMA)
    if any(row['file'] in manifest for row in builder.check_missing_anchors(root)):
        raise _t.ContractError(_t.Reason.MISSING)
    return True


# @anchor:wa.tests.scripts.support.ReservationOwner
class ReservationOwner:
    """Counted synthetic owner; no production allocation or cancellation."""
    def __init__(self, owner, capacity):
        self.registry = IdentityRegistry(owner, 1)
        self.capacity = capacity
        self.grant = self.holder = self.completion = None
        self.charged = 0
        self._request = self._binding = None
        self._releases = []

    # @anchor:wa.tests.scripts.support.ReservationOwner.reserve
    def reserve(self, request: _t.ReservationRequest, policy: _t.ResourcePolicy) -> _t.ReservationOutcome:
        if policy is None:
            return _t.ReservationOutcome(None, _t.Reason.MISSING)
        if request.peak_bytes > self.capacity or self.grant is not None:
            return _t.ReservationOutcome(None, _t.Reason.CAPACITY)
        token = self.registry.mint('reservation', 'synthetic-accounting-token')
        self.grant = _t.ReservationGrant(request, token, request.peak_bytes,
            request.accounting_revision, 1, request.absolute_deadline_ns)
        self.charged = request.peak_bytes
        return _t.ReservationOutcome(self.grant, None)

    # @anchor:wa.tests.scripts.support.ReservationOwner.grow
    def grow(self, grant: _t.ReservationGrant, request: _t.ReservationRequest,
             policy: _t.ResourcePolicy) -> _t.ReservationOutcome:
        if grant is not self.grant:
            return _t.ReservationOutcome(None, _t.Reason.FOREIGN)
        if (_replace(request, peak_bytes=grant.request.peak_bytes) != grant.request):
            return _t.ReservationOutcome(None, _t.Reason.STALE)
        if policy is None or request.peak_bytes < self.charged:
            return _t.ReservationOutcome(None, _t.Reason.MISSING)
        if request.peak_bytes > self.capacity:
            return _t.ReservationOutcome(None, _t.Reason.CAPACITY)
        self.grant = _replace(grant, request=request, charged_bytes=request.peak_bytes)
        self.charged = request.peak_bytes
        return _t.ReservationOutcome(self.grant, None)

    # @anchor:wa.tests.scripts.support.ReservationOwner.request
    def request(self, holder: _t.HolderContract) -> _t.CancellationResult:
        self.holder = holder
        self._request = object()
        self.completion = self._binding = None
        return _t.CancellationResult(True, False, False, False, False, (), None,
                                     _t.MissingReason.INCOMPLETE)

    # @anchor:wa.tests.scripts.support.ReservationOwner.complete
    def complete(self, released_tokens=None):
        """Authenticate synthetic drain evidence, including adversarial tokens."""
        if self._request is None or self.grant is None:
            raise _t.ContractError(_t.Reason.MISSING)
        if released_tokens is None:
            released_tokens = (self.grant.reservation,)
        self.completion = _t.CancellationResult(True, True, False, False, True,
            released_tokens, self.registry.mint('completion', 'synthetic-drain'), None)
        self._binding = (self._request, self.holder, self.grant)
        return self.completion

    # @anchor:wa.tests.scripts.support.ReservationOwner.verify
    def verify(self, holder: _t.HolderContract, proof: _t.CancellationResult) -> _t.AdmissionCheck:
        if (holder is not self.holder or proof is not self.completion or proof is None
                or self._binding is None
                or self._binding[0] is not self._request
                or self._binding[1] is not holder
                or self._binding[2] is not self.grant
                or holder.scope != self.grant.request.scope):
            return _t.AdmissionCheck(_t.Truth.FALSE, _t.Reason.FOREIGN)
        return _t.AdmissionCheck(_t.Truth.TRUE, None)

    # @anchor:wa.tests.scripts.support.ReservationOwner.release_unused
    def release_unused(self, grant: _t.ReservationGrant, unused_bytes: int,
                       proof: _t.CancellationResult) -> _t.AdmissionCheck:
        if (grant is not self.grant
                or self.verify(self.holder, proof).result is not _t.Truth.TRUE
                or not any(token is grant.reservation for token in proof.released_tokens)):
            return _t.AdmissionCheck(_t.Truth.FALSE, _t.Reason.FOREIGN)
        if type(unused_bytes) is not int:
            return _t.AdmissionCheck(_t.Truth.FALSE, _t.Reason.BOUNDS)
        for released_holder, released_request, released_grant, released_bytes, outcome in self._releases:
            if (self.holder is released_holder and self._request is released_request
                    and grant is released_grant):
                if unused_bytes != released_bytes:
                    return _t.AdmissionCheck(_t.Truth.FALSE, _t.Reason.BOUNDS)
                return outcome
        if not 0 < unused_bytes <= self.charged:
            return _t.AdmissionCheck(_t.Truth.FALSE, _t.Reason.BOUNDS)
        self.charged -= unused_bytes
        outcome = _t.AdmissionCheck(_t.Truth.TRUE, None)
        self._releases.append((self.holder, self._request, grant, unused_bytes, outcome))
        return outcome
