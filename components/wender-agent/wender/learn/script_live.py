# @module {"id":"wa.learn.script_live","layer":4,"owner":"LEARN","deps":["wa.learn.model_client","wa.learn.script_runtime","wa.learn.script_integration","wa.learn.script_generator","wa.learn.script_ledger","wa.learn.script_store","wa.learn.script_promotion","wa.learn.dream_replay","wa.perceive.osc2","wa.perceive.redact","wa.scripts.types","wa.scripts.header","wa.scripts.compiler","wa.scripts.act_api","wa.scripts.frame_wait"],"book":"HB-WA-s4-learn"}
"""Composition owners for CU launchers.

Legacy dispatch preserves the reviewed transport and ActionPolicy checks in
cu_loop/SrfIdentitySurface. It grants no script capability or live readiness.
Live composition is separate from the legacy compatibility owner.
"""
from threading import RLock


# @anchor:wa.learn.script_live.instrument_muse
def instrument_muse(runtime, backend, *, web=False):
    """Count action-provider attempts on the concrete run-owned Muse instance.

    Source generation has its own runtime provider boundary and is deliberately
    not wrapped here. The bound method reads workspace/effort at invocation time.
    Return a release callback so instrumentation cannot outlive this episode.
    """
    from wender.learn.model_client import MuseBackend
    from wender.learn.script_runtime import ScriptRuntime
    if type(runtime) is not ScriptRuntime or type(backend) is not MuseBackend:
        raise ValueError('PROVIDER_OWNER_UNAVAILABLE')
    method = '_web_json' if web else 'generate'
    if method in backend.__dict__:
        raise ValueError('PROVIDER_ALREADY_INSTRUMENTED')
    provider = getattr(backend, method)

    # @anchor:wa.learn.script_live.instrument_muse.generate
    def generate(*args, **kwargs):
        return runtime.invoke_provider(provider, *args, **kwargs)

    # @anchor:wa.learn.script_live.instrument_muse.release
    def release():
        if backend.__dict__.get(method) is generate:
            delattr(backend, method)

    generate._live_provider = (runtime, provider)
    setattr(backend, method, generate)
    return release


# @anchor:wa.learn.script_live.LegacyDispatchOwner
class LegacyDispatchOwner:
    """One explicitly registered surface, no retries after uncertain input.

    Registration is object identity, never a container-name or shape match.
    The callback is installed by the existing transport, after its validation.
    A lock serializes callback admission with uncertainty and final shutdown.
    """

    # @anchor:wa.learn.script_live.LegacyDispatchOwner.__init__
    def __init__(self, surface):
        if surface is None:
            raise ValueError('DISPATCH_OWNER_UNAVAILABLE')
        self._surface = surface
        self._closed = False
        self._active = False
        self._lock = RLock()

    # @anchor:wa.learn.script_live.LegacyDispatchOwner.check_ready
    def check_ready(self):
        with self._lock:
            return self._surface is not None and not self._closed

    # @anchor:wa.learn.script_live.LegacyDispatchOwner.dispatch
    def dispatch(self, surface, action, send):
        with self._lock:
            if self._closed or self._surface is not surface or self._active:
                raise ValueError('DISPATCH_OWNER_UNAVAILABLE')
            self._active = True
            try:
                return send()
            except BaseException:
                # The physical callback may already have crossed the boundary.
                self._closed = True
                raise
            finally:
                self._active = False

    # @anchor:wa.learn.script_live.LegacyDispatchOwner.uncertain
    def uncertain(self, action):
        with self._lock:
            self._closed = True

    # @anchor:wa.learn.script_live.LegacyDispatchOwner.select_script
    def select_script(self, before, description):
        if not self.check_ready():
            raise ValueError('DISPATCH_OWNER_UNAVAILABLE')
        # This scripts-off composition owns no catalogue or executable scripts.
        return None

    # @anchor:wa.learn.script_live.LegacyDispatchOwner.check_script_miss
    def check_script_miss(self):
        return self.check_ready()

    # @anchor:wa.learn.script_live.LegacyDispatchOwner.charge_model
    def charge_model(self):
        # Compatibility permission, deliberately not a provider-call counter.
        return self.check_ready()

    # @anchor:wa.learn.script_live.LegacyDispatchOwner.close
    def close(self):
        with self._lock:
            self._closed = True
            self._surface = None
            return not self._active


from dataclasses import dataclass, fields, replace
import hashlib
import json
from pathlib import Path
import secrets
import os
import stat
import unicodedata
from wender.scripts import types as _t
from wender.learn import script_runtime as _runtime
from wender.learn import script_integration as _integration


# @anchor:wa.learn.script_live._require
def _require(value, reason):
    if value is not True:
        raise ValueError(reason)


# @anchor:wa.learn.script_live.ManifestLimits
@dataclass(frozen=True, slots=True)
class ManifestLimits:
    entries: int
    depth: int
    bytes: int

    # @anchor:wa.learn.script_live.ManifestLimits.__post_init__
    def __post_init__(self):
        _require(all(type(v) is int and v > 0 for v in
                     (self.entries, self.depth, self.bytes)), 'MANIFEST_LIMITS')


# @anchor:wa.learn.script_live.capsule_manifest_digest
def capsule_manifest_digest(root, *, limits):
    """capsule-directory-v1: SHA256 of canonical UTF-8 JSON entry records.

    Entries are [relative_name, 'd'] or [relative_name, 'f', size, sha256],
    sorted by the UTF-8 bytes of relative_name. Empty directories and dotfiles
    participate. All symlinks, special files and noncanonical names refuse.
    Descriptor-relative opens prevent a path replacement from escaping root.
    Trusted configuration must supply the expected digest and every bound.
    """
    _require(type(limits) is ManifestLimits, 'MANIFEST_LIMITS')
    root = Path(root)
    _require(root.absolute() == root.resolve(strict=True), 'MANIFEST_PATH')
    records, total = [], 0

    # @anchor:wa.learn.script_live.capsule_manifest_digest.identity
    def identity(value):
        return (value.st_dev, value.st_ino, value.st_mode, value.st_size,
                value.st_mtime_ns, value.st_ctime_ns)

    # @anchor:wa.learn.script_live.capsule_manifest_digest.walk
    def walk(fd, prefix, depth):
        nonlocal total
        before = os.fstat(fd)
        names = []
        with os.scandir(fd) as entries:
            for entry in entries:
                _require(len(records) + len(names) < limits.entries, 'MANIFEST_LIMITS')
                names.append(entry.name)
        for name in sorted(names, key=lambda value: value.encode('utf-8', 'strict')):
            _require(name == unicodedata.normalize('NFC', name)
                     and name not in ('.', '..') and '/' not in name and '\\' not in name
                     and not any(ord(c) < 32 or ord(c) == 127 for c in name), 'MANIFEST_PATH')
            relative = prefix + name
            _require(depth <= limits.depth and len(records) < limits.entries, 'MANIFEST_LIMITS')
            metadata = os.stat(name, dir_fd=fd, follow_symlinks=False)
            _require(stat.S_ISDIR(metadata.st_mode) or stat.S_ISREG(metadata.st_mode), 'MANIFEST_PATH')
            flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
            if stat.S_ISDIR(metadata.st_mode):
                flags |= os.O_DIRECTORY
            child = os.open(name, flags, dir_fd=fd)
            try:
                opened = os.fstat(child)
                _require(identity(metadata) == identity(opened), 'STALE')
                if stat.S_ISDIR(opened.st_mode):
                    records.append([relative, 'd'])
                    walk(child, relative + '/', depth + 1)
                else:
                    total += opened.st_size
                    _require(total <= limits.bytes, 'MANIFEST_LIMITS')
                    digest, read = hashlib.sha256(), 0
                    while True:
                        block = os.read(child, min(65536, opened.st_size - read + 1))
                        if not block:
                            break
                        read += len(block)
                        _require(read <= opened.st_size, 'STALE')
                        digest.update(block)
                    _require(read == opened.st_size, 'STALE')
                    records.append([relative, 'f', read, digest.hexdigest()])
                _require(identity(os.fstat(child)) == identity(opened) and
                         identity(os.stat(name, dir_fd=fd, follow_symlinks=False)) == identity(opened), 'STALE')
            finally:
                os.close(child)
        _require(identity(os.fstat(fd)) == identity(before), 'STALE')

    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        walk(descriptor, '', 1)
        _require(identity(os.stat(root, follow_symlinks=False)) == identity(os.fstat(descriptor)), 'STALE')
    finally:
        os.close(descriptor)
    records.sort(key=lambda row: row[0].encode('utf-8'))
    wire = json.dumps(['capsule-directory-v1', records], ensure_ascii=False,
                      separators=(',', ':')).encode('utf-8')
    return hashlib.sha256(wire).hexdigest()


# @anchor:wa.learn.script_live.CapsulePin
@dataclass(frozen=True, slots=True, repr=False)
class CapsulePin:
    manifest_digest: str
    app_digest: str
    transport_digest: str
    tasks_digest: str
    container: str
    algorithm: str
    version: str
    mapping: tuple
    task: str

    # @anchor:wa.learn.script_live.CapsulePin.__post_init__
    def __post_init__(self):
        _require(self.algorithm == 'sha256' and self.version == 'capsule-directory-v1', 'MISSING')
        _require(all(type(v) is str and len(v) == 64 and
                     all(c in '0123456789abcdef' for c in v) for v in
                     (self.manifest_digest, self.app_digest, self.transport_digest,
                      self.tasks_digest)), 'MISSING')
        _require(bool(self.container) and bool(self.task), 'MISSING')
        _require(type(self.mapping) is tuple and len(self.mapping) == 7, 'MISSING')
        # S0 validates field types and presence independently.
        _t.ScopeIdentity(*self.mapping, None, _t.Reason.MISSING)


# @anchor:wa.learn.script_live.CapsuleAttestation
@dataclass(frozen=True, slots=True, eq=False, repr=False)
class CapsuleAttestation:
    expected: CapsulePin
    observed: CapsulePin
    issuer: str
    incarnation: str
    handle: object
    valid: bool
    invalidated: bool


# @anchor:wa.learn.script_live.AttestationOwner
class AttestationOwner:
    """Re-read only explicitly supplied identity files; expected pins are external.

    The container and task are supplied by the trusted launcher, not generated
    source. Registrations use object identity even when pin bytes are equal.
    """
    # @anchor:wa.learn.script_live.AttestationOwner.__init__
    def __init__(self, *, pin, paths, container, task, issuer, incarnation, authority,
                 manifest_limits):
        _require(type(pin) is CapsulePin and type(authority) is _t.AuthorityRevision, 'MISSING')
        _require(type(paths) is tuple and len(paths) == 4 and
                 all(isinstance(p, Path) for p in paths), 'MISSING')
        _require(bool(issuer) and bool(incarnation), 'MISSING')
        _require(type(manifest_limits) is ManifestLimits, 'MANIFEST_LIMITS')
        self.manifest_limits = manifest_limits
        self.pin, self.paths = pin, paths
        self.container, self.task = container, task
        self.issuer, self.incarnation, self.authority = issuer, incarnation, authority
        self._records, self._invalidated = {}, False

    # @anchor:wa.learn.script_live.AttestationOwner.attest
    def attest(self):
        _require(not self._invalidated, 'STALE')
        observed = [capsule_manifest_digest(self.paths[0], limits=self.manifest_limits)]
        for path in self.paths[1:]:
            with path.open('rb') as stream:
                observed.append(hashlib.file_digest(stream, 'sha256').hexdigest())
        actual = CapsulePin(*observed, self.container, self.pin.algorithm,
                            self.pin.version, self.pin.mapping, self.task)
        _require(actual == self.pin, 'FOREIGN')
        handle = _t.PrivateRef(self.issuer, self.incarnation, secrets.token_hex(16))
        record = CapsuleAttestation(self.pin, actual, self.issuer, self.incarnation,
                                    handle, True, False)
        self._records[id(record)] = record
        return record

    # @anchor:wa.learn.script_live.AttestationOwner.check
    def check(self, record):
        if (self._invalidated or self._records.get(id(record)) is not record
                or record.valid is not True or record.invalidated is not False):
            return False
        try:
            actual = self.attest()
            self._records.pop(id(actual))
            return actual.observed == record.observed
        except (ValueError, OSError):
            self.invalidate()
            return False

    # @anchor:wa.learn.script_live.AttestationOwner.scope
    def scope(self, record):
        _require(self.check(record), 'FOREIGN')
        return _t.ScopeIdentity(*record.expected.mapping, record.handle, None)

    # @anchor:wa.learn.script_live.AttestationOwner.invalidate
    def invalidate(self):
        self._invalidated = True
        self._records.clear()


# These are architecture ceilings, not production defaults. Every field is
# passed explicitly by EvaluationPolicy.from_mapping (design: budgets table).
_POLICY_TYPES = {name: getattr(_t, name) for name in (
    'SourcePolicy', 'ParserPolicy', 'SlotPolicy', 'StorePolicy', 'ReplayPolicy',
    'ResolverPolicy', 'WaitPolicy', 'ContextPolicy', 'DisplayPolicy')}
_LIMITS = frozenset(('aggregate_host_bytes', 'aggregate_process_bytes',
    'emergency_reserve_bytes', 'peak_bytes', 'active_jobs', 'queued_jobs',
    'queued_bytes', 'principal_rate', 'host_rate', 'rate_window_ns',
    'cancel_ns', 'terminate_ns', 'join_ns', 'generation_request_bytes',
    'generation_response_bytes', 'generation_wire_bytes', 'image_bytes',
    'image_tokens', 'cleanup_reserve_ns', 'p95_ns', 'p99_ns',
    'max_registrations', 'max_record_bytes'))
_STAGES = frozenset(('queue', 'startup', 'acquisition', 'bind', 'send', 'settle', 'seal'))


# @anchor:wa.learn.script_live.EvaluationPolicy
@dataclass(frozen=True, slots=True, repr=False)
class EvaluationPolicy:
    version: str
    policies: tuple
    limits: tuple
    authority: object
    stages: tuple
    absolute_deadline_ns: int
    tokenizer: object
    utility: tuple

    # @anchor:wa.learn.script_live.EvaluationPolicy.__post_init__
    def __post_init__(self):
        _require(type(self.version) is str and bool(self.version), 'POLICY_MISSING')
        pp, ll, ss = dict(self.policies), dict(self.limits), dict(self.stages)
        _require(len(pp) == len(self.policies) and set(pp) == set(_POLICY_TYPES), 'POLICY_MISSING')
        _require(all(type(pp[k]) is v for k, v in _POLICY_TYPES.items()), 'POLICY_MISSING')
        _require(len(ll) == len(self.limits) and set(ll) == _LIMITS, 'POLICY_MISSING')
        _require(len(ss) == len(self.stages) and set(ss) == _STAGES, 'POLICY_MISSING')
        _require(all(type(v) is int and v > 0 for v in (*ll.values(), *ss.values(),
                 self.absolute_deadline_ns)), 'POLICY_MISSING')
        _require(type(self.authority) is _t.AuthorityRevision and
                 callable(self.tokenizer) and bool(self.utility) and
                 all(type(v) in (int, float) and v > 0 for _, v in self.utility), 'POLICY_MISSING')
        _require(bool(pp['ParserPolicy'].supervisor_reference), 'POLICY_MISSING')
        _require(pp['WaitPolicy'].absolute_deadline_ns <= self.absolute_deadline_ns, 'DEADLINE')
        _require(sum(ss.values()) + ll['cleanup_reserve_ns'] < self.absolute_deadline_ns, 'DEADLINE')

    # @anchor:wa.learn.script_live.EvaluationPolicy.from_mapping
    @classmethod
    def from_mapping(cls, *, version, policies, limits, authority, stages,
                     absolute_deadline_ns, tokenizer, utility):
        _require(type(policies) is dict and set(policies) == set(_POLICY_TYPES), 'POLICY_MISSING')
        result = []
        for name, kind in _POLICY_TYPES.items():
            supplied = policies[name]
            _require(type(supplied) is dict and set(supplied) == {f.name for f in fields(kind)},
                     'POLICY_MISSING')
            _require(all(v is not None for v in supplied.values()), 'POLICY_MISSING')
            result.append((name, kind(**supplied)))
        return cls(version, tuple(result), tuple(limits.items()), authority,
                   tuple(stages.items()), absolute_deadline_ns, tokenizer, tuple(utility.items()))


# @anchor:wa.learn.script_live.LiveAcquisition
@dataclass(frozen=True, slots=True, eq=False, repr=False)
class LiveAcquisition:
    frame: object
    description: object
    observation: object
    inventory: tuple
    arguments: dict
    allocation: object
    producer_handle: object
    complete: bool
    truncated: bool


# @anchor:wa.learn.script_live.LiveObservationOwner
class LiveObservationOwner:
    """Registered producer supplies evidence, never inferred counters or IDs."""
    # @anchor:wa.learn.script_live.LiveObservationOwner.__init__
    def __init__(self, *, producer, scope, authority, max_registrations):
        self.producer, self.scope, self.authority = producer, scope, authority
        self.limit = max_registrations
        self._acquisitions, self._allocations, self._evidence, self._cache = {}, {}, {}, {}
        self.latest_evidence = None

    # @anchor:wa.learn.script_live.LiveObservationOwner.acquire
    def acquire(self, frame, description):
        from wender.perceive.osc2 import join_osc2
        _require(len(self._acquisitions) < self.limit, 'BOUNDS')
        join_osc2(frame, description, self._cache)
        acquisition = self.producer.acquire(frame, description, self.scope, self.authority)
        _require(type(acquisition) is LiveAcquisition and acquisition.frame is frame
                 and acquisition.description is description, 'ACQUISITION')
        _require(self.producer.check(acquisition) is True, 'ACQUISITION')
        self._acquisitions[id(acquisition)] = acquisition
        return acquisition

    # @anchor:wa.learn.script_live.LiveObservationOwner.reserve_observation
    def reserve_observation(self, acquisition, policy, resources):
        _require(self._acquisitions.get(id(acquisition)) is acquisition, 'ACQUISITION')
        allocation = self.producer.reserve_observation(acquisition, policy, resources)
        _require(allocation is acquisition.allocation and allocation is not None
                 and id(allocation) not in self._allocations, 'ACQUISITION')
        self._allocations[id(allocation)] = acquisition
        return allocation

    # @anchor:wa.learn.script_live.LiveObservationOwner.check_acquisition
    def check_acquisition(self, acquisition, scope, authority, allocation):
        return (self._acquisitions.get(id(acquisition)) is acquisition
            and self._allocations.get(id(allocation)) is acquisition
            and acquisition.allocation is allocation
            and scope == self.scope and authority == self.authority
            and type(acquisition.observation) is _t.ObservationRef
            and acquisition.observation.scope == scope
            and acquisition.observation.authority == authority
            and acquisition.complete is True and acquisition.truncated is False
            and type(acquisition.inventory) is tuple and bool(acquisition.inventory)
            and self.producer.check(acquisition) is True)

    # @anchor:wa.learn.script_live.LiveObservationOwner.observation_arguments
    def observation_arguments(self, acquisition, scope, authority, allocation):
        from wender.perceive.redact import _ScopeIdentity
        _require(self.check_acquisition(acquisition, scope, authority, allocation), 'ACQUISITION')
        args = dict(acquisition.arguments)
        obs = acquisition.observation
        _require(args['publication_id'] == obs.publication_identity
                 and type(args['producer_gen16']) is int
                 and args['producer_gen16'] == obs.producer_generation
                 and 0 <= obs.producer_generation <= 65535
                 and tuple(args['raw_elements']) == acquisition.inventory
                 and args['scope_complete'] is True and args['stream_truncated'] is False,
                 'ACQUISITION')
        args['scope_identity'] = _ScopeIdentity(principal_id=scope.principal,
            tenant_id=scope.tenant, profile_incarnation=scope.browser_profile_incarnation,
            top_level_origin=scope.top_level_origin, target_frame_origin=scope.target_frame_origin,
            frame_ancestry=scope.frame_ancestry, scope_revision=scope.scope_revision,
            host_attested=True)
        return args

    # @anchor:wa.learn.script_live.LiveObservationOwner.register_observation
    def register_observation(self, acquisition, bundle, scope, authority, allocation):
        _require(self.check_acquisition(acquisition, scope, authority, allocation), 'ACQUISITION')
        evidence = self.producer.register_observation(acquisition, bundle, scope, authority, allocation)
        _require(type(evidence) is _integration.IntegrationEvidence
                 and evidence.observation is acquisition.observation
                 and evidence.inventory is acquisition and evidence.bundle is bundle
                 and evidence.allocation is allocation, 'OBSERVATION_REGISTRATION')
        self._evidence[id(evidence)] = evidence
        self.latest_evidence = evidence
        return evidence

    # @anchor:wa.learn.script_live.LiveObservationOwner.check_evidence
    def check_evidence(self, evidence):
        return (self._evidence.get(id(evidence)) is evidence
                and self.producer.check_evidence(evidence) is True)

    # @anchor:wa.learn.script_live.LiveObservationOwner.release_observation
    def release_observation(self, allocation):
        acquisition = self._allocations.get(id(allocation))
        _require(acquisition is not None and self.producer.release_observation(allocation) is True,
                 'OBSERVATION_DRAIN')
        del self._allocations[id(allocation)]
        self._acquisitions.pop(id(acquisition))
        self._evidence = {k: v for k, v in self._evidence.items() if v.allocation is not allocation}
        return True


# @anchor:wa.learn.script_live.ScopedDispatchOwner
class ScopedDispatchOwner:
    """S11A consumes at the outer crossing; only its scope permits inner input."""
    # @anchor:wa.learn.script_live.ScopedDispatchOwner.__init__
    def __init__(self, runtime, surface):
        _require(type(runtime) is _runtime.ScriptRuntime, 'RUNTIME_CONFIGURATION')
        self.runtime, self.surface = runtime, surface
        self._active = None
        self._lock = RLock()

    # @anchor:wa.learn.script_live.ScopedDispatchOwner.dispatch
    def dispatch(self, surface, action, send):
        # This is installed only on capsule.dispatcher, not services.actions.
        with self._lock:
            active = self._active
            _require(active is not None and active[0] is surface and active[1] is action
                     and active[2] is False and self.runtime.check_ready(), 'INPUT_FENCE')
            active[2] = True
            return send()

    # @anchor:wa.learn.script_live.ScopedDispatchOwner.cross
    def cross(self, surface, action, send):
        with self._lock:
            _require(surface is self.surface and self._active is None, 'INPUT_FENCE')
            self._active = [surface, action, False]
            try:
                return send()
            finally:
                self._active = None


    # @anchor:wa.learn.script_live.ScopedDispatchOwner.close
    def close(self):
        with self._lock:
            _require(self._active is None, 'OWNER_DRAIN')
            surface = self.surface
            if surface is not None:
                if getattr(surface, 'dispatcher', None) is self:
                    surface.dispatcher = None
                if getattr(surface, 'integration', None) in (self, self.runtime):
                    surface.integration = None
                host = self.runtime.services.integration
                if type(host) is _LiveHost and host.surface is surface:
                    host.surface = None
                self.surface = None
            return True


# @anchor:wa.learn.script_live.install_live_dispatch
def install_live_dispatch(runtime, surface):
    """Install the inner crossing while preserving the registered final fence."""
    if type(runtime.services.actions) is _LiveActions:
        adapter = runtime.services.actions.adapter
        _require(adapter.runtime is runtime and adapter.surface is surface, 'INPUT_FENCE')
        return adapter
    adapter = ScopedDispatchOwner(runtime, surface)
    actions = _LiveActions(runtime.services.actions, adapter)
    runtime.services = replace(runtime.services, actions=actions,
        cleanup=_DispatchCleanup(runtime.services.cleanup, adapter))
    runtime.register_owner(adapter)
    surface.dispatcher = adapter
    # Identity surfaces use integration for their inner transport crossing.
    surface.integration = adapter
    return adapter


# @anchor:wa.learn.script_live._LiveActions
class _LiveActions:
    # @anchor:wa.learn.script_live._LiveActions.__init__
    def __init__(self, owner, adapter):
        self.owner, self.adapter = owner, adapter
        self.support, self.describer = None, None

    # @anchor:wa.learn.script_live._LiveActions.__getattr__
    def __getattr__(self, name):
        return getattr(self.owner, name)

    # @anchor:wa.learn.script_live._LiveActions.uncertain
    def uncertain(self, action):
        host = self.adapter.runtime.services.integration
        reconciliation = getattr(host, 'reconciliation', None)
        if type(reconciliation) is ReconciliationOwner:
            reconciliation.mark_uncertain(action)
        return self.owner.uncertain(action)

    # @anchor:wa.learn.script_live._LiveActions.verify
    def verify(self, action, before, after):
        verified = self.owner.verify(action, before, after)
        if self.support is not None and not self.support._missing:
            try:
                runtime = self.adapter.runtime
                observation = runtime.services.observation
                acquisition = observation._owner.acquire(after, self.describer.describe(after, prev=before))
                evidence = _integration.observe(host=observation, acquisition=acquisition,
                    scope=runtime._session.scope, authority=runtime._session.authority,
                    policy=runtime._policy('observation'), resources=runtime._host.resources)
                runtime.register_owner(evidence)
                _require(verified is True, 'SUPPORT_MISSING')
                self.support.after_input(action, _t.InputDisposition.APPLIED,
                                         evidence.observation, evidence)
            except Exception:
                self.support._missing = True
        return verified

    # @anchor:wa.learn.script_live._LiveActions.dispatch
    def dispatch(self, surface, action, callback):
        if self.support is not None and not self.support._missing:
            try:
                evidence = self.adapter.runtime.services.observation._owner.latest_evidence
                _require(evidence is not None, 'SUPPORT_MISSING')
                declared = self.support.provenance.declared_transition(action, evidence)
                self.support.before_input(action, evidence.observation, evidence, declared)
            except Exception:
                self.support._missing = True
        # The existing ActAPI/backend fence still owns physical authorization.
        return self.owner.dispatch(surface, action,
            lambda: self.adapter.cross(surface, action, callback))



# @anchor:wa.learn.script_live.ReconciliationEvidence
@dataclass(frozen=True, slots=True, eq=False, repr=False)
class ReconciliationEvidence:
    scope: object
    actions: tuple
    acquisition: object
    comparisons: tuple
    unresolved: tuple
    drain: object
    fresh_grant: object
    handle: object


# @anchor:wa.learn.script_live.ReconciliationOwner
class ReconciliationOwner:
    """State comparison and fresh grant ports cannot dispatch inputs."""
    # @anchor:wa.learn.script_live.ReconciliationOwner.__init__
    def __init__(self, *, observations, comparator, custody, grants):
        self.observations, self.comparator = observations, comparator
        self.custody, self.grants = custody, grants
        self._records, self._uncertain, self._renewed = {}, {}, {}

    # @anchor:wa.learn.script_live.ReconciliationOwner.mark_uncertain
    def mark_uncertain(self, action):
        self._uncertain[id(action)] = action

    # @anchor:wa.learn.script_live.ReconciliationOwner.collect
    def collect(self, session, actions, frame, description):
        _require(bool(actions) and all(action is not None for action in actions)
                 and {id(a) for a in actions} == set(self._uncertain)
                 and all(self._uncertain.get(id(a)) is a for a in actions), 'RECONCILIATION')
        acquisition = self.observations.acquire(frame, description)
        _require(self.comparator.check_fresh(session, actions, acquisition) is True, 'RECONCILIATION')
        comparisons = tuple(self.comparator.compare(session, action, acquisition) for action in actions)
        unresolved = tuple(action for action, result in zip(actions, comparisons)
                           if result not in ('applied', 'not_applied'))
        _require(not unresolved and all(self.comparator.check(session, action, acquisition, result)
                  is True for action, result in zip(actions, comparisons)), 'RECONCILIATION')
        drain = self.custody.drain_interrupted(session)
        _require(self.custody.check_drain(session, drain) is True, 'RECONCILIATION')
        grant = self.grants.issue(session, acquisition, drain)
        _require(self.grants.check(session, acquisition, drain, grant) is True, 'FRESH_GRANT')
        record = ReconciliationEvidence(session.scope, tuple(actions), acquisition,
            comparisons, unresolved, drain, grant, object())
        self._records[id(record)] = (record, session)
        return record

    # @anchor:wa.learn.script_live.ReconciliationOwner.bind_renewal
    def bind_renewal(self, previous, renewed, record):
        _require(self.check(previous, record) and id(record) not in self._renewed, 'FRESH_GRANT')
        self._renewed[id(record)] = renewed
        self._uncertain.clear()

    # @anchor:wa.learn.script_live.ReconciliationOwner.check
    def check(self, session, record):
        registration = self._records.get(id(record))
        bound = self._renewed.get(id(record))
        return (registration is not None and registration[0] is record
                and (registration[1] is session or bound is session) and record.scope == session.scope
                and not record.unresolved and bool(record.actions)
                and len(record.actions) == len(record.comparisons)
                and (bound is session or set(self._uncertain) == {id(a) for a in record.actions})
                and self.observations.producer.check(record.acquisition) is True
                and self.custody.check_drain(session, record.drain) is True
                and self.grants.check(session, record.acquisition, record.drain,
                                      record.fresh_grant) is True
                and all(self.comparator.check(session, action, record.acquisition, result) is True
                        for action, result in zip(record.actions, record.comparisons)))


# @anchor:wa.learn.script_live.GenerationTransport
class GenerationTransport:
    """Isolated compatibility binding to Muse's existing source-schema client.

    A registered supervisor must certify the client's cancellation/wire bounds;
    _web_json alone does not establish them. The host owns export approval.
    """
    # @anchor:wa.learn.script_live.GenerationTransport.__init__
    def __init__(self, *, backend, runtime, supervisor):
        from wender.learn.model_client import MuseBackend
        _require(type(backend) is MuseBackend and type(runtime) is _runtime.ScriptRuntime,
                 'GENERATION_OWNER')
        self.backend, self.runtime, self.supervisor = backend, runtime, supervisor

    # @anchor:wa.learn.script_live.GenerationTransport.exchange
    def exchange(self, payload, *, schema, association, policy, host):
        from wender.learn.script_generator import GenerationPolicy
        _require(type(policy) is GenerationPolicy and type(payload) is bytes
                 and type(schema) is bytes and len(payload) + len(schema) <= policy.request_bytes,
                 'GENERATION_BOUND')
        decoded_schema = json.loads(schema)
        _require('source' in decoded_schema.get('required', ()) and
                 set(decoded_schema.get('properties', ())) <= {'source', 'predecessor_proposal'},
                 'GENERATION_SCHEMA')
        _require(host is self.runtime.services.generation and
                 self.runtime.clock() < policy.absolute_deadline_ns, 'GENERATION_OWNER')
        _require(host.check(association, policy, None).result is _t.Truth.TRUE, 'PRIVACY')
        _require(not any(v.casefold() in payload.decode('utf-8').casefold()
                         for v in policy.forbidden_substrings), 'PRIVACY')
        _require(self.supervisor.check(self.backend, policy) is True, 'GENERATION_SUPERVISOR')
        # One runtime provider count; _web_json is not instrumented again.
        _require(self.runtime.charge_model() is True, 'MODEL_BUDGET')
        provider = self.backend._web_json
        registration = getattr(provider, '_live_provider', None)
        if registration is not None and registration[0] is self.runtime:
            provider = registration[1]
        result = self.runtime.invoke_provider(self.supervisor.invoke,
            provider, payload.decode('utf-8'), decoded_schema, policy=policy)
        response = json.dumps(result, ensure_ascii=False).encode('utf-8')
        _require(len(response) <= policy.response_bytes and
                 not any(v.casefold() in response.decode('utf-8').casefold()
                         for v in policy.forbidden_substrings), 'PRIVACY')
        _require(host.check(association, policy, response).result is _t.Truth.TRUE, 'PRIVACY')
        return response


# @anchor:wa.learn.script_live.EpisodeSupportOwner
class EpisodeSupportOwner:
    """Prospective RAM custody, never reconstructed from a compact trace."""
    # @anchor:wa.learn.script_live.EpisodeSupportOwner.__init__
    def __init__(self, *, ledger, provenance, terminal, seal_owner, run_owner):
        from wender.learn.script_ledger import LedgerOwner
        _require(type(ledger) is LedgerOwner and type(run_owner) is EvaluationRunOwner,
                 'SUPPORT_OWNER')
        self.ledger, self.provenance, self.terminal = ledger, provenance, terminal
        self.seal_owner, self.run_owner = seal_owner, run_owner
        self._steps, self._pending, self._closed = [], {}, False
        self._missing = False
        self.final_observation = None

    # @anchor:wa.learn.script_live.EpisodeSupportOwner.before_input
    def before_input(self, action, observation, evidence, declared_transition):
        _require(not self._closed and id(action) not in self._pending and
                 self.provenance.check_pre(observation, evidence, declared_transition) is True,
                 'SUPPORT_MISSING')
        self._pending[id(action)] = (action, observation, evidence, declared_transition)

    # @anchor:wa.learn.script_live.EpisodeSupportOwner.after_input
    def after_input(self, action, disposition, observation, evidence):
        pre = self._pending.get(id(action))
        _require(pre is not None and pre[0] is action and
                 disposition is _t.InputDisposition.APPLIED and
                 self.provenance.check_transition(pre, disposition, observation, evidence) is True,
                 'SUPPORT_MISSING')
        self._steps.append((pre, disposition, observation, evidence))
        del self._pending[id(action)]
        self.final_observation = observation

    # @anchor:wa.learn.script_live.EpisodeSupportOwner.complete
    def complete(self, runtime, summary, observation):
        from wender.learn import dream_replay
        _require(not self._closed and not self._missing and summary.get('goal_verified') is True
                 and bool(self._steps) and not self._pending
                 and observation is self.final_observation
                 and self.terminal.verify(observation) is True, 'SUPPORT_MISSING')
        _require(self.seal_owner.seal is not None, 'SUPPORT_MISSING')
        loader = self.provenance.loader_inputs(self.ledger, self.seal_owner.seal, tuple(self._steps))
        episode = dream_replay.load_verified(self.ledger, seal=self.seal_owner.seal,
            abis=self.seal_owner.abis, replay_policy=dict(self.run_owner.policy.policies)['ReplayPolicy'],
            **loader)
        _require(episode.scope == runtime._session.scope and episode.authority == runtime._session.authority, 'FOREIGN')
        self.run_owner.retain_support(self, episode)
        arguments = self.provenance.segment_arguments(episode, tuple(self._steps))
        result = runtime.generate_draft(segment_arguments=arguments,
            transport=self.run_owner.transport, policy=self.run_owner.generation_policy,
            host=runtime.services.generation)
        _require(result.value is not None, 'GENERATION_UNAVAILABLE')
        admitted = runtime.submit_draft(result.value, **self.run_owner.admission_arguments)
        self._closed = True
        return admitted

    # @anchor:wa.learn.script_live.EpisodeSupportOwner.close
    def close(self):
        if self._closed and not self._steps and not self._pending:
            return True
        _require(self.provenance.release(tuple(self._steps), tuple(self._pending.values())) is True,
                 'SUPPORT_DRAIN')
        self._steps.clear()
        self._pending.clear()
        self._closed = True
        return True


# @anchor:wa.learn.script_live.EvaluationRunOwner
class EvaluationRunOwner:
    """Explicit run-scoped concrete inventory; episode cleanup never closes it."""
    # @anchor:wa.learn.script_live.EvaluationRunOwner.__init__
    def __init__(self, *, policy, identity, attestation, services, session, inventory,
                 supervisor, admission_arguments, generation_policy, shutdown, support_ports):
        from wender.scripts.header import SourceAdmission
        from wender.scripts.compiler import CompilerRegistry
        from wender.learn.script_store import ScriptStore
        from wender.learn.script_ledger import LedgerOwner
        from wender.scripts.act_api import ActAPI
        from wender.scripts.frame_wait import FrameWait
        required = {'admission': SourceAdmission, 'compiler': CompilerRegistry,
            'store': ScriptStore, 'ledger': LedgerOwner, 'act_api': ActAPI, 'wait': FrameWait}
        _require(type(policy) is EvaluationPolicy and type(identity) is AttestationOwner
                 and identity.check(attestation), 'FOREIGN')
        _require(type(services) is _runtime.RuntimeServices and
                 type(session) is _integration.IntegrationSession, 'OWNER_REGISTRATION')
        _require(type(inventory) is dict and set(inventory) == set(required)
                 and all(type(inventory[k]) is v for k, v in required.items()), 'OWNER_REGISTRATION')
        _require(services.integration.store is inventory['store'] and
                 all(services.integration.check_construction_owner(k, v) is True
                     for k, v in inventory.items()), 'OWNER_REGISTRATION')
        _require(all(v is not None for v in (services.actions, services.budget, services.cleanup,
            services.context, services.generation, services.accounting, services.telemetry,
            services.observation, services.acquisition, supervisor, shutdown)), 'OWNER_REGISTRATION')
        self.policy, self.identity, self.attestation = policy, identity, attestation
        self.services, self.session, self.inventory = services, session, dict(inventory)
        _require(type(support_ports) is tuple and len(support_ports) == 3
                 and all(value is not None for value in support_ports), 'SUPPORT_OWNER')
        self.supervisor, self.shutdown, self.support_ports = supervisor, shutdown, support_ports
        self.admission_arguments, self.generation_policy = dict(admission_arguments), generation_policy
        self._episodes, self._support, self._closed = {}, {}, False
        self.transport = None

    # @anchor:wa.learn.script_live.EvaluationRunOwner.register_episode
    def register_episode(self, runtime, support):
        _require(not self._closed and type(runtime) is _runtime.ScriptRuntime
                 and type(support) is EpisodeSupportOwner and support.run_owner is self
                 and id(runtime) not in self._episodes
                 and len(self._episodes) < dict(self.policy.limits)['max_registrations'], 'OWNER_REGISTRATION')
        self._episodes[id(runtime)] = (runtime, support)

    # @anchor:wa.learn.script_live.EvaluationRunOwner.check_episode
    def check_episode(self, runtime, support):
        return self._episodes.get(id(runtime)) == (runtime, support) and not self._closed

    # @anchor:wa.learn.script_live.EvaluationRunOwner.retain_support
    def retain_support(self, owner, episode):
        _require(any(pair[1] is owner for pair in self._episodes.values()) and
                 id(episode) not in self._support
                 and len(self._support) < dict(self.policy.limits)['max_registrations'], 'SUPPORT_OWNER')
        _require(not any(existing.seal is episode.seal or existing.episode_id == episode.episode_id
                 or existing.run_id == episode.run_id or existing.seal.provenance == episode.seal.provenance
                 or existing.seal.support_identity == episode.seal.support_identity
                 for existing in self._support.values()), 'SUPPORT_COHORT')
        self._support[id(episode)] = episode

    # @anchor:wa.learn.script_live.EvaluationRunOwner.screen
    def screen(self, index, plan, *, support_identity, window_step_ids, benefit_proven):
        from wender.learn.dream_replay import screen
        _require(not self._closed, 'OWNER_REGISTRATION')
        return screen(index, plan, support_identity=support_identity, replay_runs=2,
                      window_step_ids=window_step_ids, benefit_proven=benefit_proven)

    # @anchor:wa.learn.script_live.EvaluationRunOwner.record_shadow
    def record_shadow(self, trial, *, frozen_support_cohort_ids, existing_trials, ledger_owner):
        from wender.learn.script_promotion import record_shadow
        _require(not self._closed, 'OWNER_REGISTRATION')
        return record_shadow(self.inventory['store'], trial,
            frozen_support_cohort_ids=frozen_support_cohort_ids,
            existing_trials=existing_trials, ledger_owner=ledger_owner)

    # @anchor:wa.learn.script_live.EvaluationRunOwner.transition
    def transition(self, target_lifecycle, **arguments):
        from wender.learn.script_promotion import transition
        _require(not self._closed, 'OWNER_REGISTRATION')
        return transition(self.inventory['store'], target_lifecycle, **arguments)

    # @anchor:wa.learn.script_live.EvaluationRunOwner.close
    def close(self):
        drained = True
        for runtime, support in self._episodes.values():
            for owner in (runtime, support):
                try:
                    if owner.close() is not True:
                        drained = False
                except Exception:
                    drained = False
        _require(drained and self.shutdown.drain(tuple(self.inventory.values()),
                 tuple(self._support.values())) is True, 'OWNER_DRAIN')
        _require(self.shutdown.check_drained() is True, 'OWNER_DRAIN')
        self._episodes.clear()
        self._support.clear()
        self._closed = True
        return True


# @anchor:wa.learn.script_live._LiveHost
class _LiveHost:
    # @anchor:wa.learn.script_live._LiveHost.__init__
    def __init__(self, owner, identity, attestation, policy, clock, surface, services):
        self.owner, self.identity, self.attestation = owner, identity, attestation
        self.policy, self._clock, self.surface = policy, clock, surface
        self._services = services

    # @anchor:wa.learn.script_live._LiveHost.__getattr__
    def __getattr__(self, name):
        return getattr(self.owner, name)

    # @anchor:wa.learn.script_live._LiveHost.clock
    def clock(self):
        return self._clock()

    # @anchor:wa.learn.script_live._LiveHost.current_authority
    def current_authority(self, scope):
        if (self.surface.container == self.identity.pin.container
                and Path(self.surface.binary).resolve() == self.identity.paths[2].resolve()
                and self.identity.check(self.attestation) and scope == self.identity.scope(self.attestation)
                and self.identity.authority == self.policy.authority
                and self.owner.current_authority(scope) == self.policy.authority):
            return self.policy.authority
        return None

    # @anchor:wa.learn.script_live._LiveHost.check_reconciliation
    def check_reconciliation(self, session, evidence):
        reconciliation = getattr(self.owner, 'reconciliation', None)
        return (type(reconciliation) is ReconciliationOwner
                and id(evidence) not in reconciliation._renewed
                and reconciliation.check(session, evidence) is True)

    # @anchor:wa.learn.script_live._LiveHost.renew_session
    def renew_session(self, session, evidence):
        _require(self.check_reconciliation(session, evidence), 'RECONCILIATION')
        renewed = replace(session, authority=self.current_authority(session.scope),
            custody=None, unresolved_actions=(), restart=_integration.RestartState(
                True, session.scope, (), evidence, None))
        _require(self.owner.register_session(renewed) is True, 'SESSION_REGISTRATION')
        self.owner.reconciliation.bind_renewal(session, renewed, evidence)
        return renewed

    # @anchor:wa.learn.script_live._LiveHost.check_fresh_grant
    def check_fresh_grant(self, session, evidence):
        return self.owner.reconciliation.check(session, evidence)

    # @anchor:wa.learn.script_live._LiveHost.check_services
    def check_services(self, services, configuration):
        source = self._services
        actions = services.actions.owner if type(services.actions) is _LiveActions else services.actions
        cleanup = services.cleanup
        while type(cleanup) in (_EpisodeCleanup, _DispatchCleanup):
            cleanup = cleanup.owner
        return (services.integration is self and actions is source.actions and cleanup is source.cleanup
            and all(getattr(services, field.name) is getattr(source, field.name)
                    for field in fields(_runtime.RuntimeServices)
                    if field.name not in ('integration', 'actions', 'cleanup'))
            and self.identity.check(self.attestation)
            and self.owner.check_services(source, configuration) is True)


# @anchor:wa.learn.script_live.build_live_runtime
def build_live_runtime(*, mode, capsule, describer, backend, identity,
                       policy, goal_check, clock, owners):
    _require(mode == 'live' and type(owners) is EvaluationRunOwner
             and owners.policy is policy and owners.identity is identity
             and type(identity) is AttestationOwner and callable(goal_check), 'LIVE_CONFIGURATION')
    _require(not owners._closed and identity.check(owners.attestation), 'FOREIGN')
    _require(type(policy) is EvaluationPolicy and capsule is not None and describer is not None,
             'LIVE_CONFIGURATION')
    scope = identity.scope(owners.attestation)
    session = owners.session
    _require(session.scope == scope and session.authority == policy.authority
             and session.authority == identity.authority and
             clock() + sum(dict(policy.stages).values()) + dict(policy.limits)['cleanup_reserve_ns']
             < session.deadline_ns <= policy.absolute_deadline_ns, 'LIVE_CONFIGURATION')
    host = _LiveHost(owners.services.integration, identity, owners.attestation, policy, clock, capsule, owners.services)
    configuration = _runtime.RuntimeConfiguration(mode='live', policies=(
        ('observation', dict(policy.policies)['DisplayPolicy']),
        ('selection', dict(policy.policies)['ResolverPolicy']), ('evaluation', policy)),
        model_identity=type(backend).__name__, absolute_deadline_ns=policy.absolute_deadline_ns,
        max_record_bytes=dict(policy.limits)['max_record_bytes'],
        max_registrations=dict(policy.limits)['max_registrations'])
    services = replace(owners.services, integration=host)
    runtime = _runtime.ScriptRuntime(configuration, services, session=session)
    try:
        _require(runtime.check_ready(), 'RESTART_UNCERTAIN')
        install_live_dispatch(runtime, capsule)
        owners.transport = GenerationTransport(backend=backend, runtime=runtime, supervisor=owners.supervisor)
        provenance, terminal, seal_owner = owners.support_ports
        support = EpisodeSupportOwner(ledger=owners.inventory['ledger'], provenance=provenance,
            terminal=terminal, seal_owner=seal_owner, run_owner=owners)
        runtime.services = replace(runtime.services, cleanup=_EpisodeCleanup(
            runtime.services.cleanup, support, runtime.services.observation))
        runtime.register_owner(support)
        runtime.episode_owner = support
        owners.register_episode(runtime, support)
        runtime.services.actions.support = support
        runtime.services.actions.describer = describer
    except BaseException:
        runtime.close()
        raise
    return runtime


# @anchor:wa.learn.script_live.complete_model_episode
def complete_model_episode(runtime, *, summary, observation, episode_owner):
    _require(type(runtime) is _runtime.ScriptRuntime, 'RUNTIME_CONFIGURATION')
    if episode_owner is None:
        return None  # No prospective custody means no learning or promotion.
    _require(type(episode_owner) is EpisodeSupportOwner and
             episode_owner.run_owner.check_episode(runtime, episode_owner), 'SUPPORT_OWNER')
    return episode_owner.complete(runtime, summary, observation)


# @anchor:wa.learn.script_live.validate_live_context
def validate_live_context(context, *, pin_path, policy_path, task, tasks_path, app_path):
    """JSON supplies data only; concrete owners must be installed by the host."""
    _require(type(context) is dict and set(context) == {'identity', 'policy', 'owners', 'clock'},
             'LIVE_OWNERS_MISSING')
    _require(type(context['owners']) is EvaluationRunOwner and
             type(context['identity']) is AttestationOwner and
             type(context['policy']) is EvaluationPolicy and callable(context['clock']),
             'LIVE_OWNERS_MISSING')
    _require(pin_path is not None and policy_path is not None and task is not None, 'POLICY_MISSING')
    pin_data = json.loads(Path(pin_path).read_text(encoding='utf-8'))
    policy_data = json.loads(Path(policy_path).read_text(encoding='utf-8'))
    pin_data['mapping'] = tuple(pin_data['mapping'])
    pin_data['mapping'] = (*pin_data['mapping'][:5], tuple(pin_data['mapping'][5]), pin_data['mapping'][6])
    _require(CapsulePin(**pin_data) == context['identity'].pin
             and task == context['identity'].pin.task
             and Path(tasks_path).resolve() == context['identity'].paths[3].resolve()
             and (Path(app_path) / 'holdings-h2l').resolve() == context['identity'].paths[1].resolve(), 'FOREIGN')
    policy_data['authority'] = _t.AuthorityRevision(**policy_data['authority'])
    actual = EvaluationPolicy.from_mapping(**policy_data, tokenizer=context['policy'].tokenizer)
    _require(actual == context['policy'], 'POLICY_MISSING')
    return context


# @anchor:wa.learn.script_live.bind_live_factory
def bind_live_factory(*, capsule, describer, backend, goal_check, context):
    _require(type(context) is dict and set(context) == {'identity', 'policy', 'owners', 'clock'},
             'LIVE_OWNERS_MISSING')
    if goal_check is None:
        _require(type(context['owners']) is EvaluationRunOwner, 'LIVE_OWNERS_MISSING')
        goal_check = context['owners'].support_ports[1].verify

    # @anchor:wa.learn.script_live.bind_live_factory.factory
    def factory(*, mode):
        return build_live_runtime(mode=mode, capsule=capsule, describer=describer,
            backend=backend, identity=context['identity'], policy=context['policy'],
            goal_check=goal_check, clock=context['clock'], owners=context['owners'])
    return factory


# @anchor:wa.learn.script_live._EpisodeCleanup
class _EpisodeCleanup:
    """Drain registered episode custody; shared run owners have a later shutdown."""
    # @anchor:wa.learn.script_live._EpisodeCleanup.__init__
    def __init__(self, owner, support, observations):
        self.owner, self.support, self.observations = owner, support, observations
        self._released = set()

    # @anchor:wa.learn.script_live._EpisodeCleanup.check_owned
    def check_owned(self, value):
        return (value is self.support or
            (type(value) is _integration.IntegrationEvidence and
             self.observations.check_evidence(value) is True) or self.owner.check_owned(value) is True)

    # @anchor:wa.learn.script_live._EpisodeCleanup.drain
    def drain(self, value):
        if value is self.support:
            _require(value.close() is True, 'SUPPORT_DRAIN')
        elif type(value) is _integration.IntegrationEvidence and self.observations.check_evidence(value):
            _require(self.observations.release_observation(value.allocation) is True, 'OBSERVATION_DRAIN')
        else:
            return self.owner.drain(value)
        self._released.add(id(value))
        return True

    # @anchor:wa.learn.script_live._EpisodeCleanup.check_drained
    def check_drained(self, value):
        return id(value) in self._released or self.owner.check_drained(value) is True


# @anchor:wa.learn.script_live._DispatchCleanup
class _DispatchCleanup:
    # @anchor:wa.learn.script_live._DispatchCleanup.__init__
    def __init__(self, owner, adapter):
        self.owner, self.adapter, self._drained = owner, adapter, False

    # @anchor:wa.learn.script_live._DispatchCleanup.check_owned
    def check_owned(self, value):
        return value is self.adapter or self.owner.check_owned(value) is True

    # @anchor:wa.learn.script_live._DispatchCleanup.drain
    def drain(self, value):
        if value is self.adapter:
            self._drained = self.adapter.close() is True
            return self._drained
        return self.owner.drain(value)

    # @anchor:wa.learn.script_live._DispatchCleanup.check_drained
    def check_drained(self, value):
        return (self._drained if value is self.adapter else self.owner.check_drained(value))
