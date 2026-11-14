# @module {"id":"wa.scripts.header","layer":2,"owner":"LEARN","deps":["wa.scripts.types"],"book":"HB-WA-s4-learn"}
"""Reserved, private source ingress. No result is execution authority.

The attempt and supervisor contracts below are new host adapter interfaces.
Supervisor verification must authenticate retained, current probe evidence for
all frozen denial operations AND a safe round trip under the same runtime.
It is the trusted adapter's job to enforce this boundary; a boolean flag,
cooperative timeout or parser callback alone cannot implement that contract.
LocalParserSupervisor supplies an explicit Linux/seccomp adapter; no fallback
parser or automatic registration is installed.
"""
import ast as _ast
import dataclasses as _dc
import enum as _enum
import hashlib as _hashlib
import json as _json
import re as _re
import types as _types
import typing as _typing
import unicodedata as _unicode
import weakref as _weakref
import os as _os
import selectors as _selectors
import subprocess as _subprocess
import time as _time
import threading as _threading
from functools import wraps as _wraps
from wender.scripts import types as _t


def _need(ok, reason=_t.Reason.SCHEMA):
    if not ok:
        raise _t.ContractError(reason)


def _private_call(fn, *args, **kwargs):
    reason = None
    try:
        return fn(*args, **kwargs)
    except _t.ContractError as exc:
        reason = exc.reason
    except MemoryError:
        reason = _t.Reason.CAPACITY
    except TimeoutError:
        reason = _t.Reason.DEADLINE
    except UnicodeError:
        reason = _t.Reason.TYPE
    except _json.JSONDecodeError:
        reason = _t.Reason.SCHEMA
    except RecursionError:
        reason = _t.Reason.BOUNDS
    except Exception:
        reason = _t.Reason.UNKNOWN
    # Raise outside the handler: neither context nor cause retains raw input.
    raise _t.ContractError(reason)


def _closed(fn):
    @_wraps(fn)
    def wrapped(*args, **kwargs):
        return _private_call(fn, *args, **kwargs)
    return wrapped


# @anchor:wa.scripts.header.AttemptAdmissionPort
class AttemptAdmissionPort(_typing.Protocol):
    """Atomic host-wide check-and-charge, before queueing, across principals/episodes.

    The owner supplies concurrency/queue configuration and never refunds work.
    Missing configuration must return a closed refusal, not select defaults.
    """
    # @anchor:wa.scripts.header.AttemptAdmissionPort.charge
    def charge(self, scope: _t.ScopeIdentity, owner: _t.PrivateRef,
               operation: str, policy: _t.ResourcePolicy, now_ns: int) -> _t.AdmissionCheck:
        ...


# @anchor:wa.scripts.header.SupervisorResolverPort
class SupervisorResolverPort(_typing.Protocol):
    # @anchor:wa.scripts.header.SupervisorResolverPort.resolve
    def resolve(self, reference: str) -> object:
        """Return the exact current host-registered backend; never import a name."""
        ...


# @anchor:wa.scripts.header.SupervisorVerifierPort
class SupervisorVerifierPort(_typing.Protocol):
    # @anchor:wa.scripts.header.SupervisorVerifierPort.verify
    def verify(self, backend: object, reference: str, policy: _t.ParserPolicy,
               deadline_ns: int) -> _t.RegistryRef:
        """Retain evidence binding backend/runtime/incarnation/policy and real probes.

        Required probes: environment, descriptors, home/workspace reads, writes,
        sockets/network, children, tracing/signals, executable loading, memory,
        deadline, terminate/join/drain, and successful fixed parse round trip.
        Missing or incomplete evidence and always-refusing backends must refuse.
        """
        ...

    # @anchor:wa.scripts.header.SupervisorVerifierPort.check
    def check(self, evidence: _t.RegistryRef, backend: object, reference: str,
              policy: _t.ParserPolicy) -> _t.AdmissionCheck:
        """Authenticate exact retained evidence and current backend, not equality."""
        ...


# @anchor:wa.scripts.header.ParserBackendPort
class ParserBackendPort(_typing.Protocol):
    # @anchor:wa.scripts.header.ParserBackendPort.parse
    def parse(self, body: bytes, policy: _t.ParserPolicy, deadline_ns: int,
              grant: _t.ReservationGrant, holder: _t.HolderContract) -> bytes:
        """Bound source/result IPC, memory and deadline inside verified boundary.

        Malformed/oversized output, cancellation and exhaustion must terminate,
        join and drain actual workers; elapsed time alone is not completion.
        No user Python executes, and no in-process parser fallback is allowed.
        """
        ...


@_dc.dataclass(frozen=True, slots=True, eq=False, weakref_slot=True)
class _Session:
    _header: object = _dc.field(repr=False)
    _auxiliary: tuple = _dc.field(repr=False)
    _body: bytes = _dc.field(repr=False)
    _result: tuple = _dc.field(repr=False)


@_dc.dataclass(slots=True, repr=False)
class _Lifetime:
    grant: _t.ReservationGrant
    holder: _t.HolderContract | None
    deadline: int
    live: bool = True
    backend: object = None
    evidence: object = None
    source: bytes | None = None
    canonical_retained: int = 0


def _check(value):
    _need(type(value) is _t.AdmissionCheck, _t.Reason.TYPE)
    _need(value.result is _t.Truth.TRUE and value.reason is None,
          value.reason if value.reason is not None else _t.Reason.MISSING)


# @anchor:wa.scripts.header.SourceAdmission
class SourceAdmission:
    """Host-only coordinator; injected services are never sourced from metadata.

    peak_bound(raw_length, source, parser, slots) supplies a conservative full
    simultaneous Python/child/transport/canonical/scratch peak. telemetry()
    supplies current aggregate occupied bytes. Both are trusted host services.
    result_decoder(wire, policy) validates the host-owned parser wire schema
    before reconstructing an immutable AST tree: (node_kind, children[, fields]),
    where children is a tuple of AST trees, including context/operator nodes.
    The local adapter includes ordered (field_name, value) pairs. Values are
    tagged ('node', child_index), ('list', tuple_of_values), or typed scalars
    ('none', None), ('bool', bool), ('int', int), ('str', str), ('bytes', hex),
    ('float', float.hex()), ('complex', (real.hex(), imag.hex())), and
    ('ellipsis', None). Child indices preserve field associations without
    duplicating AST objects. Other trusted adapters may supply children only.
    Scalars and field containers are not AST objects. The root has edge depth
    zero. The trusted decoder must preserve every AST object and parent edge;
    flattened walks and scalar serialization tuples do not satisfy this contract.
    The backend-specific wire schema remains the trusted adapter's responsibility.
    holder_factory(grant, scope, policy, endpoint, boundary) mints the exact
    cancellation-owner-retained HolderContract and binds it to that grant.
    The factory must be recoverable and idempotent for an exact grant: retry
    returns its retained holder, including after an interrupted acquisition.
    Failed sessions remain discoverable with outstanding() until drained.
    """
    # @anchor:wa.scripts.header.SourceAdmission.__init__
    @_closed
    def __init__(self, *, scope, owner, source_policy, parser_policy, slot_policy,
                 resource_policy, clock, attempts, reservations, cancellation,
                 resolver, verifier, peak_bound, telemetry, result_decoder,
                 endpoint, boundary, holder_factory, accounting_revision, absolute_deadline_ns):
        for value, cls in ((scope, _t.ScopeIdentity), (owner, _t.PrivateRef),
                           (source_policy, _t.SourcePolicy), (parser_policy, _t.ParserPolicy),
                           (slot_policy, _t.SlotPolicy), (resource_policy, _t.ResourcePolicy),
                           (endpoint, _t.RegistryRef), (boundary, _t.RegistryRef)):
            _need(value is not None, _t.Reason.MISSING)
            _need(type(value) is cls, _t.Reason.TYPE)
        _need(all(x is not None for x in (clock, attempts, reservations, cancellation,
                  resolver, verifier, peak_bound, telemetry, result_decoder, holder_factory)), _t.Reason.MISSING)
        _need(type(accounting_revision) is int and accounting_revision >= 0, _t.Reason.TYPE)
        _need(type(absolute_deadline_ns) is int and absolute_deadline_ns > 0, _t.Reason.BOUNDS)
        self._scope, self._owner = scope, owner
        self._source, self._parser, self._slots, self._resource = (
            source_policy, parser_policy, slot_policy, resource_policy)
        self._clock, self._attempts = clock, attempts
        self._reservations, self._cancellation = reservations, cancellation
        self._resolver, self._verifier = resolver, verifier
        self._peak, self._telemetry, self._decoder = peak_bound, telemetry, result_decoder
        self._endpoint, self._boundary = endpoint, boundary
        self._holder_factory = holder_factory
        self._revision, self._deadline = accounting_revision, absolute_deadline_ns
        self._sessions = {}
        self._finished = _weakref.WeakKeyDictionary()

    def _now(self):
        now = self._clock.now_ns()
        _need(type(now) is int and now >= 0, _t.Reason.TYPE)
        return now

    def _lifetime(self, session):
        _need(type(session) is _Session, _t.Reason.FOREIGN)
        _need(session not in self._finished, _t.Reason.STALE)
        entry = self._sessions.get(id(session))
        _need(entry is not None and entry[0] is session, _t.Reason.FOREIGN)
        state = entry[1]
        _need(state.live, _t.Reason.STALE)
        _need(self._now() < state.deadline, _t.Reason.DEADLINE)
        return state

    # @anchor:wa.scripts.header.SourceAdmission.outstanding
    def outstanding(self):
        """Host-private inventory, including failed ingress requiring cleanup."""
        return tuple(s for s, state in self._sessions.values() if state.live)

    # @anchor:wa.scripts.header.SourceAdmission.admit
    @_closed
    def admit(self, source):
        now = self._now()
        _check(self._attempts.charge(self._scope, self._owner, 'source-admission', self._resource, now))
        _need(type(source) is bytes, _t.Reason.TYPE)
        _need(len(source) <= self._source.utf8_bytes, _t.Reason.BOUNDS)
        peak = self._peak(len(source), self._source, self._parser, self._slots)
        used = self._telemetry()
        _need(peak is not None and used is not None, _t.Reason.MISSING)
        _need(type(peak) is int and peak > 0 and type(used) is int and used >= 0, _t.Reason.TYPE)
        _need(peak >= self._parser.incremental_memory_bytes + len(source), _t.Reason.BOUNDS)
        _need(used + peak + self._resource.emergency_reserve_bytes <= self._resource.host_bytes
              and peak <= self._resource.process_bytes
              and peak <= self._resource.cancellation.consumer_byte_ceiling, _t.Reason.CAPACITY)
        deadline = self._deadline
        _need(now < deadline - self._resource.cancellation.join_deadline_ns, _t.Reason.DEADLINE)
        request = _t.ReservationRequest(self._owner, self._owner, self._scope,
            'source-admission', peak, (), (), self._revision, deadline)
        outcome = self._reservations.reserve(request, self._resource)
        _need(type(outcome) is _t.ReservationOutcome, _t.Reason.TYPE)
        _need(outcome.grant is not None, outcome.reason or _t.Reason.MISSING)
        grant = outcome.grant
        _need(grant.request is request and grant.accounting_revision == self._revision,
              _t.Reason.FOREIGN)
        session = _Session(None, (), b'', ())
        state = _Lifetime(grant, None, min(deadline, grant.request.absolute_deadline_ns,
            grant.absolute_deadline_ns) - self._resource.cancellation.join_deadline_ns, source=source)
        self._sessions[id(session)] = (session, state)
        try:
            holder = self._acquire_holder(state)
            header, auxiliary, body = parse_header(source, session=session, admission=self)
            _need(header.scope == self._scope, _t.Reason.FOREIGN)
            authority_deadline = min(self._deadline, grant.request.absolute_deadline_ns,
                                     grant.absolute_deadline_ns, header.limits.absolute_deadline_ns)
            state.deadline = min(
                authority_deadline - self._resource.cancellation.join_deadline_ns,
                self._now() + self._parser.timeout_ms * 1000000)
            self._lifetime(session)
            backend = self._resolver.resolve(self._parser.supervisor_reference)
            _need(backend is not None, _t.Reason.MISSING)
            state.backend = backend
            evidence = self._verifier.verify(backend, self._parser.supervisor_reference,
                                             self._parser, state.deadline)
            _need(type(evidence) is _t.RegistryRef, _t.Reason.MISSING)
            _check(self._verifier.check(evidence, backend,
                                       self._parser.supervisor_reference, self._parser))
            _need(self._resolver.resolve(self._parser.supervisor_reference) is backend,
                  _t.Reason.STALE)
            state.evidence = evidence
            self._lifetime(session)
            wire_limit = min(grant.charged_bytes, header.limits.canonical_bytes)
            local = type(backend) is LocalParserSupervisor
            wire = backend.parse(body, self._parser, state.deadline, grant, holder,
                                 **({'result_byte_limit': wire_limit} if local else {}))
            self._lifetime(session)
            _need(type(wire) is bytes, _t.Reason.TYPE)
            _need(len(wire) <= wire_limit, _t.Reason.BOUNDS)
            result = self._decoder(wire, self._parser,
                **({'result_byte_limit': wire_limit}
                   if local and self._decoder == backend.decode else {}))
            _immutable_result(result, self._parser)
            self._lifetime(session)
            _check(self._verifier.check(evidence, backend,
                                       self._parser.supervisor_reference, self._parser))
            _need(self._resolver.resolve(self._parser.supervisor_reference) is backend,
                  _t.Reason.STALE)
            final = _Session(header, auxiliary, body, result)
            del self._sessions[id(session)]
            self._sessions[id(final)] = (final, state)
            return final
        except Exception:
            # Failure does not imply release. The authenticated owner decides
            # whether workers, IPC and callbacks have actually drained.
            _private_call(self.finish, session)
            raise

    def _acquire_holder(self, state):
        if state.holder is None:
            grant = state.grant
            holder = self._holder_factory(grant, self._scope, self._resource.cancellation,
                                          self._endpoint, self._boundary)
            _need(type(holder) is _t.HolderContract, _t.Reason.TYPE)
            _need(holder.scope == self._scope and holder.retained_bytes == grant.charged_bytes
                  and holder.absolute_deadline_ns == grant.absolute_deadline_ns
                  and holder.policy == self._resource.cancellation
                  and holder.cancellation_endpoint is self._endpoint
                  and holder.supervisor_boundary is self._boundary, _t.Reason.FOREIGN)
            state.holder = holder
        return state.holder

    # @anchor:wa.scripts.header.SourceAdmission.finish
    @_closed
    def finish(self, session, proof=None):
        _need(type(session) is _Session, _t.Reason.FOREIGN)
        entry = self._sessions.get(id(session))
        state = entry[1] if entry is not None and entry[0] is session else self._finished.get(session)
        _need(state is not None, _t.Reason.FOREIGN)
        self._acquire_holder(state)
        if proof is None:
            proof = self._cancellation.request(state.holder)
        _need(type(proof) is _t.CancellationResult, _t.Reason.TYPE)
        _check(self._cancellation.verify(state.holder, proof))
        _need(proof.drained and (proof.acknowledged or (proof.terminated and proof.joined))
              and any(token is state.grant.reservation for token in proof.released_tokens),
              _t.Reason.MISSING)
        _check(self._reservations.release_unused(state.grant, state.grant.charged_bytes, proof))
        state.live, state.source = False, None
        self._sessions.pop(id(session), None)
        # Retry bookkeeping must not keep source/header/parser output alive
        # after the consumer has released it and capacity has been returned.
        self._finished[session] = state
        return _t.AdmissionCheck(_t.Truth.TRUE, None)


def _immutable_result(result, policy):
    # Only AST nodes count. The children container and kind scalar do not.
    stack, count = [(result, 0)], 0
    while stack:
        node, depth = stack.pop()
        _need(type(node) is tuple and len(node) in (2, 3), _t.Reason.TYPE)
        kind, children = node[:2]
        _need(type(kind) is str and kind.isascii() and kind.isidentifier()
              and type(children) is tuple, _t.Reason.TYPE)
        count += 1
        _need(count <= policy.ast_nodes and depth <= policy.ast_depth, _t.Reason.BOUNDS)
        # Refuse before constructing a work list larger than the node budget.
        _need(count + len(stack) + len(children) <= policy.ast_nodes, _t.Reason.BOUNDS)
        if children:
            _need(depth < policy.ast_depth, _t.Reason.BOUNDS)
        stack.extend((child, depth + 1) for child in children)


_TAGS = ('SCRIPT_TAG', 'LANE_CONFIG', 'SURGERY_DECLARATION', 'CAPA_TRILOGIA',
         'IO_CONTRACT', 'MUTANT_PROOF', 'MERKLE_PIN', 'META:args', 'META:fts', 'META:uso', 'META:cli')
_ABIS = _t.ABISet('wender-dsl/v1', 'wender-compiler/v1', 'wender-act/v1',
                  'wender-predicate/v1', 'wender-schema/v1')
_DECL = _re.compile(r'^# \[([^\]\n]+)\] (.*)\n$')
_COOKIE = _re.compile(r'^[ \t\f]*#.*?coding[:=][ \t]*([-\w.]+)', _re.M)
_NAME = _re.compile(r'[A-Za-z][A-Za-z0-9_]*\Z')
_SCHEMA = {
    'SCRIPT_TAG': {'logical_id': str, 'revision': int, 'predecessor': _t.MissingRef,
                   'scope': _t.ScopeIdentity, 'metadata_references': tuple[str, ...]},
    'LANE_CONFIG': {'abis': _t.ABISet, 'limits': _t.PlanLimits},
    'SURGERY_DECLARATION': {'entry': _t.PredicateSpec, 'terminal_goal': str},
    'CAPA_TRILOGIA': {'capabilities': tuple[str, ...]},
    'IO_CONTRACT': {'site_contract': str, 'goal_contract': str, 'signature_contract': str},
    'MUTANT_PROOF': {'nominal': _t.MissingRef, 'mutant': _t.MissingRef},
    'MERKLE_PIN': {'supplied_pins': _t.ArtifactIdentity | None, 'pins_reason': _t.Reason | None},
    'META:args': tuple[_t.ArgSpec, ...], 'META:fts': tuple[str, ...],
    'META:uso': str, 'META:cli': str,
}


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        _need(key not in result)
        result[key] = value
    return result


def _not_number(value):
    raise _t.ContractError(_t.Reason.TYPE)


def _json_load(text, policy, max_depth):
    # Scan before invoking the recursive decoder; strings do not affect depth.
    # Caller derives the bound from the frozen record envelope and trusted
    # predicate depth; declaration-line counts are not a JSON-depth policy.
    depth, quoted, escaped, work = 0, False, False, 0
    for ch in text:
        work += 1
        _need(work <= policy.utf8_bytes, _t.Reason.BOUNDS)
        if quoted:
            if escaped:
                escaped = False
            elif ch == '\\':
                escaped = True
            elif ch == '"':
                quoted = False
        elif ch == '"':
            quoted = True
        elif ch in '[{':
            depth += 1
            _need(depth <= max_depth, _t.Reason.BOUNDS)
        elif ch in ']}':
            depth -= 1
            _need(depth >= 0)
    _need(depth == 0 and not quoted)
    return _json.loads(text, object_pairs_hook=_pairs, parse_float=_not_number,
                       parse_constant=_not_number)


def _decode(value, annotation):
    origin, args = _typing.get_origin(annotation), _typing.get_args(annotation)
    if origin is _types.UnionType:
        if value is None and type(None) in args:
            return None
        for arg in args:
            if type(value) is arg:
                return _decode(value, arg)
            if isinstance(arg, type) and (issubclass(arg, _enum.Enum) and type(value) is str
                    or _dc.is_dataclass(arg) and type(value) is dict):
                return _decode(value, arg)
        raise _t.ContractError(_t.Reason.TYPE)
    if origin is tuple:
        _need(type(value) is list, _t.Reason.TYPE)
        if len(args) == 2 and args[1] is Ellipsis:
            return tuple(_decode(v, args[0]) for v in value)
        _need(len(value) == len(args))
        return tuple(_decode(v, a) for v, a in zip(value, args))
    if isinstance(annotation, type) and issubclass(annotation, _enum.Enum):
        _need(type(value) is str, _t.Reason.TYPE)
        _need(value in annotation._value2member_map_)
        return annotation(value)
    if isinstance(annotation, type) and _dc.is_dataclass(annotation):
        _need(type(value) is dict, _t.Reason.TYPE)
        hints = _typing.get_type_hints(annotation)
        _need(set(value) == {f.name for f in _dc.fields(annotation)})
        return annotation(**{k: _decode(v, hints[k]) for k, v in value.items()})
    _need(type(value) is annotation, _t.Reason.TYPE)
    if annotation is str:
        value.encode('utf-8', 'strict')
    return value


def _decode_tag(value, schema):
    if type(schema) is dict:
        _need(type(value) is dict, _t.Reason.TYPE)
        _need(set(value) == set(schema))
        return {key: _decode(value[key], annotation) for key, annotation in schema.items()}
    return _decode(value, schema)


# @anchor:wa.scripts.header.parse_header
@_closed
def parse_header(source, *, session, admission):
    """Return private (header, immutable auxiliary pairs, normalized body bytes)."""
    _need(type(admission) is SourceAdmission, _t.Reason.FOREIGN)
    state = admission._lifetime(session)
    _need(source is state.source, _t.Reason.FOREIGN)
    policy = admission._source
    _need(type(source) is bytes, _t.Reason.TYPE)
    _need(len(source) <= policy.utf8_bytes, _t.Reason.BOUNDS)
    text = source.decode('utf-8', 'strict')
    _need(not text.startswith('\ufeff') and '\x00' not in text)
    _need(len(text) <= policy.unicode_scalars, _t.Reason.BOUNDS)
    text = text.replace('\r\n', '\n').replace('\r', '\n')
    parts = text.split('\n')
    lines = [part + '\n' for part in parts[:-1]] + [parts[-1]]
    decoded, offset, last = {}, 0, -1
    for line in lines:
        match = _DECL.fullmatch(line)
        if match is None:
            break
        tag, payload = match.groups()
        _need(tag in _TAGS and tag not in decoded)
        index = _TAGS.index(tag)
        _need(index > last and (index >= 8 or index == last + 1))
        _need(len(decoded) < policy.header_lines, _t.Reason.BOUNDS)
        decoded[tag] = _decode_tag(_json_load(payload, policy, 2 * admission._parser.ast_depth + 4), _SCHEMA[tag])
        last, offset = index, offset + len(line)
    _need(all(tag in decoded for tag in _TAGS[:8]))
    body = text[offset:]
    # Declaration JSON is passive data, even when its strings mention coding.
    # Only body comments can introduce an encoding declaration after the prefix.
    _need(not _COOKIE.search(body))
    _need(not _re.search(r'(?m)^\s*#\s*\[', body))
    fields = {}
    for tag in _TAGS[:7]:
        if tag != 'MUTANT_PROOF':
            fields.update(decoded[tag])
    fields['arguments'] = decoded['META:args']
    header = _t.ScriptHeader(**fields)
    _need(header.abis == _ABIS)
    stack, count = [(header.entry, 0)], 0
    while stack:
        predicate, depth = stack.pop()
        count += 1
        _need(count <= admission._parser.predicate_nodes and depth <= admission._parser.ast_depth,
              _t.Reason.BOUNDS)
        stack.extend((child, depth + 1) for child in predicate.children)
    _need(len(header.arguments) <= admission._slots.slots, _t.Reason.BOUNDS)
    _names(tuple(a.name for a in header.arguments))
    _need(all(a.normalization_abi == 'wender-args/verbatim-v1' for a in header.arguments))
    auxiliary = tuple((tag, tuple(decoded[tag].items()) if type(decoded[tag]) is dict
                       else decoded[tag]) for tag in ('MUTANT_PROOF',) + _TAGS[8:] if tag in decoded)
    return header, auxiliary, body.encode('utf-8', 'strict')


_OMIT = {_t.ScopeIdentity: {'attestation', 'attestation_reason'},
         _t.TargetSelector: {'label_export_receipt'}, _t.TerminalGoalRef: {'host_handle'},
         _t.SlotDescriptor: {'capability', 'episode_id'}, _t.HarvestSpec: {'authorization'},
         _t.HostKeyVocabulary: {'attestation'}}


def _canonical_chunks(value, budget, max_depth, depth=0):
    """Bounded tokens, without projected containers or a full JSON text copy."""
    budget[0] -= 1
    _need(budget[0] >= 0 and depth <= max_depth, _t.Reason.BOUNDS)
    if isinstance(value, _enum.Enum):
        value = value.value
    if type(value) is str:
        budget[1] += max(0, 4 * len(value) - 1024)
        yield b'"'
        # JSON escaping and UTF-8 scratch are bounded independently of input size.
        for offset in range(0, len(value), 256):
            yield _json.dumps(value[offset:offset + 256], ensure_ascii=False,
                              allow_nan=False)[1:-1].encode('utf-8', 'strict')
        yield b'"'
    elif value is None:
        yield b'null'
    elif type(value) is bool:
        yield b'true' if value else b'false'
    elif type(value) is int:
        # Reject enormous integers before constructing their decimal string.
        _need(value.bit_length() <= 16384, _t.Reason.BOUNDS)
        yield str(value).encode('ascii')
    elif type(value) in (tuple, list):
        _need(len(value) <= budget[0], _t.Reason.BOUNDS)
        yield b'['
        for index, item in enumerate(value):
            if index:
                yield b','
            yield from _canonical_chunks(item, budget, max_depth, depth + 1)
        yield b']'
    else:
        if type(value) is dict:
            _need(len(value) <= budget[0], _t.Reason.BOUNDS)
            _need(all(type(k) is str for k in value), _t.Reason.TYPE)
            keys = sorted(value)
            get = value.__getitem__
        else:
            _need(_dc.is_dataclass(value) and type(value).__module__ == _t.__name__, _t.Reason.TYPE)
            keys = sorted(f.name for f in _dc.fields(value)
                          if f.name not in _OMIT.get(type(value), ()))
            get = lambda key: getattr(value, key)
        yield b'{'
        for index, key in enumerate(keys):
            if index:
                yield b','
            yield from _canonical_chunks(key, budget, max_depth, depth + 1)
            yield b':'
            yield from _canonical_chunks(get(key), budget, max_depth, depth + 1)
        yield b'}'


def _canonical(value, admission, session, max_bytes=None):
    state = admission._lifetime(session)
    depth = 2 * admission._parser.ast_depth + 8
    # The walk, sorted key lists and bounded encoder scratch must themselves fit
    # before traversal. Each visit is charged conservatively, including keys.
    source_bytes = len(state.source) if state.source is not None else 0
    resident = admission._parser.incremental_memory_bytes + 32 * source_bytes
    scratch = 65536 + 4096 * depth
    available = state.grant.charged_bytes - resident - state.canonical_retained - scratch
    _need(available > 0, _t.Reason.CAPACITY)
    visits = available // 1024
    length = 0
    # Dry run emits at most a bounded token at a time. No complete projected,
    # JSON-text, or UTF-8 representation exists on the refusal path.
    budget = [visits, 0]
    for chunk in _canonical_chunks(value, budget, depth):
        length += len(chunk)
        if max_bytes is not None:
            _need(length <= max_bytes, _t.Reason.BOUNDS)
        # Output is not retained on the dry run, even when larger than grant.
        _need(length <= admission._resource.cancellation.consumer_byte_ceiling,
              _t.Reason.CAPACITY)
    required = resident + state.canonical_retained + scratch + 1024 * (visits - budget[0]) + budget[1] + 3 * length
    if required > state.grant.charged_bytes:
        used = admission._telemetry()
        _need(type(used) is int and used >= 0, _t.Reason.MISSING)
        _need(required <= admission._resource.process_bytes
              and required <= admission._resource.cancellation.consumer_byte_ceiling
              and used + required - state.grant.charged_bytes
                  + admission._resource.emergency_reserve_bytes <= admission._resource.host_bytes,
              _t.Reason.CAPACITY)
        prior = state.grant
        request = _dc.replace(prior.request, peak_bytes=required)
        outcome = admission._reservations.grow(prior, request, admission._resource)
        _need(type(outcome) is _t.ReservationOutcome, _t.Reason.TYPE)
        _need(outcome.grant is not None, outcome.reason or _t.Reason.MISSING)
        grown = outcome.grant
        _need(grown.request is request and grown.reservation is prior.reservation
              and grown.accounting_revision == prior.accounting_revision
              and grown.absolute_deadline_ns <= prior.absolute_deadline_ns,
              _t.Reason.FOREIGN)
        state.grant, state.holder = grown, None
        state.deadline = min(state.deadline, grown.absolute_deadline_ns
                             - admission._resource.cancellation.join_deadline_ns)
        admission._acquire_holder(state)
        admission._lifetime(session)
    # bytearray growth + final immutable bytes + walk/sort/encoding scratch all
    # coexist within the charged peak. Retain the output charge until finish,
    # conservatively even if a caller drops the bytes sooner.
    admission._lifetime(session)
    output = bytearray()
    for chunk in _canonical_chunks(value, [visits, 0], depth):
        output.extend(chunk)
    encoded = bytes(output)
    state.canonical_retained += len(encoded)
    admission._lifetime(session)
    return encoded


def _metadata(header, auxiliary):
    _need(type(header) is _t.ScriptHeader, _t.Reason.TYPE)
    aux = dict(auxiliary)
    _need(len(aux) == len(auxiliary) and 'MUTANT_PROOF' in aux)
    _need(set(aux) <= {'MUTANT_PROOF', *_TAGS[8:]})
    result = {}
    for tag in _TAGS[:7]:
        if tag == 'MERKLE_PIN':
            continue
        if tag == 'MUTANT_PROOF':
            value = dict(aux[tag])
            _need(set(value) == {'nominal', 'mutant'})
            _need(all(type(v) is _t.MissingRef for v in value.values()), _t.Reason.TYPE)
            result[tag] = value
        else:
            result[tag] = {k: getattr(header, k) for k in _SCHEMA[tag]}
    result['META:args'] = header.arguments
    result.update({tag: aux[tag] for tag in _TAGS[8:] if tag in aux})
    return result


# @anchor:wa.scripts.header.canonical_metadata
@_closed
def canonical_metadata(header, auxiliary, *, session, admission):
    """Private C(M); callers gain no export permission."""
    _need(header is session._header and auxiliary is session._auxiliary, _t.Reason.FOREIGN)
    return _canonical(_metadata(header, auxiliary), admission, session)


def _hash(cls, domain, payload):
    return cls('sha256', _hashlib.sha256(domain.encode('ascii') + b'\0' + payload).hexdigest())


def _plan_projection(definition):
    return {name: getattr(definition, name) for name in (
        'nodes', 'selectors', 'symbols', 'slots', 'entry', 'terminal_goal', 'limits', 'queries')}


# @anchor:wa.scripts.header.recompute_identity
@_closed
def recompute_identity(definition, *, session, admission, screen_check):
    """Return (C(P), ArtifactIdentity), independently of supplied identity/pins."""
    admission._lifetime(session)
    _need(type(definition) is _t.PlanDefinition, _t.Reason.TYPE)
    header = session._header
    _need(definition.header == header and header.scope == admission._scope, _t.Reason.FOREIGN)
    return _identity_components(definition, session=session, admission=admission,
                                screen_check=screen_check)


def _identity_components(definition, *, session, admission, screen_check):
    admission._lifetime(session)
    header = session._header
    _need(screen_check is not None, _t.Reason.MISSING)
    _check(screen_check(definition.structural_screen_digest))
    canonical = _canonical(_plan_projection(definition), admission, session, definition.limits.canonical_bytes)
    _need(len(canonical) <= definition.limits.canonical_bytes, _t.Reason.BOUNDS)
    plan = _hash(_t.PlanDigest, 'wender-plan/v1', canonical)
    metadata = _hash(_t.MetadataDigest, 'wender-metadata/v1',
                     canonical_metadata(header, session._auxiliary, session=session, admission=admission))
    source = _hash(_t.PlanDigest, 'wender-source/v1', session._body)
    screen = definition.structural_screen_digest
    # This digest must already have been independently verified by the host;
    # descriptor discovery and whole-tree validation are compiler-owned.
    parser = {f.name: getattr(admission._parser, f.name)
              for f in _dc.fields(admission._parser) if f.name != 'supervisor_reference'}
    artifact_input = dict(plan=plan, metadata=metadata, source_digest=source,
        structural_screen_digest=screen, logical_id=header.logical_id,
        revision=header.revision, scope=header.scope, signature_schema=header.signature_contract,
        goal_contract=header.goal_contract, abis=header.abis, limits=header.limits,
        ceilings=dict(source=admission._source, parser=parser, slots=admission._slots))
    artifact = _hash(_t.ArtifactDigest, 'wender-artifact/v1', _canonical(artifact_input, admission, session))
    identity = _t.ArtifactIdentity(plan, metadata, artifact, header.logical_id, header.revision,
        header.scope, header.signature_contract, screen, header.goal_contract, source, header.abis)
    return canonical, identity


# @anchor:wa.scripts.header.canonical_identity
@_closed
def canonical_identity(*, nodes: tuple[_t.StepPlan, ...],
                       selectors: tuple[_t.TargetSelector, ...],
                       symbols: tuple[_t.ResultDeclaration, ...],
                       slots: tuple[_t.ArgSpec, ...], entry: _t.PredicateSpec,
                       terminal_goal: _t.TerminalGoalRef, limits: _t.PlanLimits,
                       queries: tuple[_t.QueryDeclaration, ...],
                       structural_screen_digest: _t.PlanDigest,
                       session, admission: SourceAdmission, screen_check
                       ) -> tuple[_t.ArtifactIdentity, bytes]:
    """Compute identity before creating an identity-bearing PlanDefinition.

    Inputs are independently validated compiler data, never evidence of that
    validation. No placeholder identity or draft registry object is required.
    Header/body/auxiliary and explicit policies come from retained admission.
    """
    admission._lifetime(session)
    for values, cls in ((nodes, _t.StepPlan), (selectors, _t.TargetSelector),
                        (symbols, _t.ResultDeclaration), (slots, _t.ArgSpec),
                        (queries, _t.QueryDeclaration)):
        _need(type(values) is tuple, _t.Reason.TYPE)
        _need(len(values) <= admission._parser.ast_nodes, _t.Reason.BOUNDS)
        _need(all(type(value) is cls for value in values), _t.Reason.TYPE)
    for value, cls in ((entry, _t.PredicateSpec), (terminal_goal, _t.TerminalGoalRef),
                       (limits, _t.PlanLimits), (structural_screen_digest, _t.PlanDigest)):
        _need(type(value) is cls, _t.Reason.TYPE)
    header = session._header
    _need(header.scope == admission._scope and header.arguments == slots
          and header.entry == entry and header.limits == limits
          and header.terminal_goal == terminal_goal.reference, _t.Reason.CONFLICT)
    components = _types.SimpleNamespace(nodes=nodes, selectors=selectors, symbols=symbols,
        slots=slots, entry=entry, terminal_goal=terminal_goal, limits=limits,
        queries=queries, structural_screen_digest=structural_screen_digest)
    canonical, identity = _identity_components(components, session=session,
        admission=admission, screen_check=screen_check)
    return identity, canonical


# @anchor:wa.scripts.header.verify_pins
@_closed
def verify_pins(definition: _t.PlanDefinition, *, session, admission: SourceAdmission,
                screen_check, plan: _t.ValidatedPlan | None = None,
                compiler: _t.PrivateRef | None = None,
                registry: _t.ValidationRegistryPort | None = None
                ) -> tuple[_t.ArtifactIdentity, bytes]:
    """Recompute complete pins; a supplied plan additionally needs its owner."""
    if plan is not None:
        _need(type(plan) is _t.ValidatedPlan and plan.definition is definition, _t.Reason.FOREIGN)
        _need(compiler is not None and registry is not None, _t.Reason.MISSING)
        checked = verify_registered_plan(plan, compiler=compiler, registry=registry,
            session=session, admission=admission, screen_check=screen_check)
        return checked.identity, checked.canonical_bytes
    canonical, identity = recompute_identity(definition, session=session,
        admission=admission, screen_check=screen_check)
    _need(definition.identity == identity and definition.source_digest == identity.source_digest,
          _t.Reason.STALE)
    pins = session._header.supplied_pins
    _need(pins == identity if pins is not None else session._header.pins_reason is not None,
          _t.Reason.STALE)
    return identity, canonical


# @anchor:wa.scripts.header.verify_registered_plan
@_closed
def verify_registered_plan(plan, *, compiler, registry, session, admission, screen_check):
    _need(type(plan) is _t.ValidatedPlan, _t.Reason.TYPE)
    _need(type(compiler) is _t.PrivateRef, _t.Reason.TYPE)
    canonical, identity = recompute_identity(plan.definition, session=session, admission=admission, screen_check=screen_check)
    _need(plan.canonical_bytes == canonical and plan.identity == identity
          and plan.definition.identity == identity
          and plan.definition.source_digest == identity.source_digest, _t.Reason.STALE)
    for name in ('nodes', 'symbols', 'slots', 'entry', 'terminal_goal', 'limits', 'queries'):
        _need(getattr(plan, name) == getattr(plan.definition, name), _t.Reason.STALE)
    _need(plan.scope == admission._scope and plan.abis == session._header.abis, _t.Reason.STALE)
    pins = session._header.supplied_pins
    _need(pins == identity if pins is not None else session._header.pins_reason is not None,
          _t.Reason.STALE)
    _check(registry.check(plan, compiler, identity, plan.scope, plan.abis))
    return plan


def _names(names):
    _need(all(type(n) is str for n in names), _t.Reason.TYPE)
    folded = tuple(_unicode.normalize('NFKC', n).casefold() for n in names)
    _need(len(set(folded)) == len(folded))
    _need(all(_NAME.fullmatch(n) and n not in ('act', 'params') for n in names))


# @anchor:wa.scripts.header.validate_arguments
@_closed
def validate_arguments(specs, values, *, scope, episode_id, slots, slot_check,
                       vocabulary_check, slot_policy):
    """Validate literal values plus retained grants without retrieving payloads.

    slot_check(descriptor, episode_id, scope) and vocabulary_check(vocabulary)
    are trusted current-owner checks returning exact AdmissionCheck records.
    Return immutable name/value pairs, preserving optional absence and spelling.
    """
    _need(type(specs) is tuple and all(type(s) is _t.ArgSpec for s in specs), _t.Reason.TYPE)
    _need(type(values) is dict and type(slots) is tuple, _t.Reason.TYPE)
    _need(type(slot_policy) is _t.SlotPolicy and type(scope) is _t.ScopeIdentity, _t.Reason.TYPE)
    _need(type(episode_id) is str and bool(episode_id), _t.Reason.MISSING)
    _names(tuple(s.name for s in specs))
    _names(tuple(values))
    _need(set(values) <= {s.name for s in specs})
    _need(len(specs) <= slot_policy.slots, _t.Reason.BOUNDS)
    _need(all(type(s) is _t.SlotDescriptor for s in slots), _t.Reason.TYPE)
    _names(tuple(s.slot_name for s in slots))
    _need(set(s.slot_name for s in slots) == set(values), _t.Reason.MISSING)
    descriptors, output, total = {s.slot_name: s for s in slots}, [], 0
    for spec in specs:
        _need(spec.normalization_abi == 'wender-args/verbatim-v1')
        if spec.name not in values:
            _need(not spec.required, _t.Reason.MISSING)
            continue
        slot = descriptors[spec.name]
        _need((slot.kind, slot.purpose, slot.destination_contract, slot.capability_contract,
               slot.scope, slot.episode_id) == (spec.kind, spec.purpose, spec.destination_contract,
               spec.capability_contract, scope, episode_id), _t.Reason.FOREIGN)
        _need(slot_check is not None, _t.Reason.MISSING)
        _check(slot_check(slot, episode_id, scope))
        value = values[spec.name]
        if spec.kind is _t.ValueKind.INTEGER:
            _need(type(value) is int, _t.Reason.TYPE)
            size, bound = 0, value
        elif spec.kind is _t.ValueKind.BOOLEAN:
            _need(type(value) is bool, _t.Reason.TYPE)
            size, bound = 0, int(value)
        else:
            _need(type(value) is str, _t.Reason.TYPE)
            _need('\x00' not in value)
            ceiling = min(spec.maximum, slot_policy.input_slot_bytes,
                          slot_policy.episode_input_bytes - total)
            # UTF-8 never uses fewer bytes than characters. Reject before any
            # slice or encoding allocation; retained input is caller-owned.
            _need(len(value) <= ceiling, _t.Reason.BOUNDS)
            size = 0
            for start in range(0, len(value), 1024):
                size += len(value[start:start + 1024].encode('utf-8', 'strict'))
                _need(size <= ceiling, _t.Reason.BOUNDS)
            bound = size
            if spec.kind is _t.ValueKind.KEY:
                _need(bool(value) and value.isascii(), _t.Reason.TYPE)
                _need(slot.key_vocabulary is not None and vocabulary_check is not None, _t.Reason.MISSING)
                _check(vocabulary_check(slot.key_vocabulary))
                _need(value in slot.key_vocabulary.symbols, _t.Reason.FOREIGN)
            else:
                _need(spec.kind is _t.ValueKind.TEXT, _t.Reason.TYPE)
        _need(spec.minimum <= bound <= spec.maximum, _t.Reason.BOUNDS)
        total += size
        _need(total <= slot_policy.episode_input_bytes, _t.Reason.BOUNDS)
        output.append((spec.name, value))
    return tuple(output)


# @anchor:wa.scripts.header.admit_source
@_closed
def admit_source(source, *, admission):
    """Retain admitted source custody; this does not validate a semantic plan."""
    _need(type(admission) is SourceAdmission, _t.Reason.FOREIGN)
    return admission.admit(source)


# @anchor:wa.scripts.header.LocalParserSupervisor
class LocalParserSupervisor:
    """Explicit Linux host adapter, with kernel default-deny syscall filtering.

    The trusted host supplies absolute runtime/library paths and a retained
    registration. No discovery, processes or probes occur at module import.
    Runtime and library identities are pinned at registration and rechecked
    before dispatch. The memory policy is conservatively applied as a total
    address-space limit, including startup/runtime memory. Resource reservation,
    arrival limits and authenticated cancellation remain host-owner services.
    A successful synchronous dispatch has reaped its worker and closed IPC;
    outstanding() must be empty before the owner authenticates release.
    """
    # @anchor:wa.scripts.header.LocalParserSupervisor.__init__
    @_closed
    def __init__(self, *, runtime, seccomp_library, probe_path, reference,
                 registration, cleanup_timeout_ns):
        _need(type(registration) is _t.RegistryRef, _t.Reason.TYPE)
        _need(type(reference) is str and bool(reference), _t.Reason.TYPE)
        _need(type(cleanup_timeout_ns) is int and cleanup_timeout_ns > 0, _t.Reason.BOUNDS)
        for path in (runtime, seccomp_library, probe_path):
            _need(type(path) is str and _os.path.isabs(path), _t.Reason.TYPE)
            _need(_os.path.isfile(path), _t.Reason.MISSING)
        self._runtime, self._library = runtime, seccomp_library
        self._probe_path = probe_path
        self._registration, self._cleanup_ns = registration, cleanup_timeout_ns
        self._reference = reference
        self._files = self._fingerprint()
        self._receipt = None
        self._children = {}
        self._lock = _threading.RLock()

    def _fingerprint(self):
        return tuple((_os.path.realpath(path), stat.st_dev, stat.st_ino,
                      stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
                     for path in (self._runtime, self._library)
                     for stat in (_os.stat(path),))

    # @anchor:wa.scripts.header.LocalParserSupervisor.resolve
    @_closed
    def resolve(self, reference):
        return self if reference == self._reference else None

    # @anchor:wa.scripts.header.LocalParserSupervisor.outstanding
    def outstanding(self):
        """Host-private process inventory; never a cancellation certificate."""
        return tuple(self._children.values())

    def _run(self, mode, body, policy, deadline_ns, result_byte_limit=262144):
        _need(self._files == self._fingerprint(), _t.Reason.STALE)
        _need(_time.monotonic_ns() < deadline_ns, _t.Reason.DEADLINE)
        # The frozen canonical byte ceiling bounds transport independently of
        # AST object/depth limits. Admission tightens it to retained custody.
        _need(type(result_byte_limit) is int and 0 < result_byte_limit <= 262144,
              _t.Reason.BOUNDS)
        limit = result_byte_limit
        child = _subprocess.Popen([self._runtime, '-I', '-S', '-c', _LOCAL_CHILD,
            mode, self._library, self._runtime, self._probe_path, '99',
            str(policy.incremental_memory_bytes), str(policy.ast_nodes),
            str(policy.ast_depth), str(limit)], cwd='/', env={'LC_ALL': 'C.UTF-8'},
            close_fds=True, stdin=_subprocess.PIPE, stdout=_subprocess.PIPE,
            stderr=_subprocess.PIPE)
        self._children[id(child)] = child
        output, reason = bytearray(), None
        try:
            # Bounded nonblocking pipes: do not buffer an unbounded child result
            # or diagnostic through communicate(). Deadline includes startup/I/O.
            with _selectors.DefaultSelector() as poll:
                for stream, event in ((child.stdin, _selectors.EVENT_WRITE),
                                      (child.stdout, _selectors.EVENT_READ),
                                      (child.stderr, _selectors.EVENT_READ)):
                    _os.set_blocking(stream.fileno(), False)
                    poll.register(stream, event)
                offset, received = 0, 0
                while poll.get_map():
                    remaining = (deadline_ns - _time.monotonic_ns()) / 1000000000
                    _need(remaining > 0, _t.Reason.DEADLINE)
                    for key, event in poll.select(remaining):
                        stream = key.fileobj
                        if stream is child.stdin:
                            if offset < len(body):
                                try:
                                    offset += _os.write(stream.fileno(), body[offset:offset + 4096])
                                except BrokenPipeError:
                                    offset = len(body)
                            if offset == len(body):
                                poll.unregister(stream)
                                stream.close()
                        else:
                            chunk = _os.read(stream.fileno(), min(4096, limit + 1 - received))
                            if not chunk:
                                poll.unregister(stream)
                                stream.close()
                            else:
                                received += len(chunk)
                                _need(received <= limit, _t.Reason.BOUNDS)
                                if stream is child.stdout:
                                    output.extend(chunk)
                remaining = (deadline_ns - _time.monotonic_ns()) / 1000000000
                _need(remaining > 0, _t.Reason.DEADLINE)
                child.wait(timeout=remaining)
            if child.returncode == 42:
                reason = _t.Reason.CAPACITY
            elif child.returncode == 43:
                reason = _t.Reason.BOUNDS
            elif child.returncode == 44:
                reason = _t.Reason.SCHEMA
            elif child.returncode != 0:
                reason = _t.Reason.MISSING
        except _subprocess.TimeoutExpired:
            reason = _t.Reason.DEADLINE
        finally:
            # No release is inferred from elapsed time. An unjoined process
            # stays in the inventory and prevents authenticated owner release.
            try:
                if child.poll() is None:
                    child.kill()
                child.wait(timeout=self._cleanup_ns / 1000000000)
            finally:
                for stream in (child.stdin, child.stdout, child.stderr):
                    stream.close()
                if child.returncode is not None:
                    self._children.pop(id(child), None)
        if reason is not None:
            raise _t.ContractError(reason)
        _need(_time.monotonic_ns() < deadline_ns, _t.Reason.DEADLINE)
        _need(self._files == self._fingerprint(), _t.Reason.STALE)
        return bytes(output)

    # @anchor:wa.scripts.header.LocalParserSupervisor.verify
    @_closed
    def verify(self, backend, reference, policy, deadline_ns):
        with self._lock:
            _need(backend is self and self.resolve(reference) is self, _t.Reason.FOREIGN)
            _need(type(policy) is _t.ParserPolicy, _t.Reason.TYPE)
            self._receipt = None
            probes = _json.loads(self._run('probes', b'pass\n', policy, deadline_ns))
            expected = {'environment', 'descriptors', 'home-read', 'workspace-read',
                        'write', 'network', 'socket', 'child', 'trace', 'signal',
                        'exec-load', 'executable-memory'}
            _need(set(probes['denied']) == expected and probes['unrelated_closed']
                  and probes['parsed'] == ['Module', 'Pass'], _t.Reason.MISSING)
            for mode, expected_reason in (('exhaust', _t.Reason.CAPACITY),
                                          ('stall', _t.Reason.DEADLINE)):
                reason = None
                try:
                    self._run(mode, b'', policy, min(deadline_ns,
                        _time.monotonic_ns() + policy.timeout_ms * 1000000 // 4))
                except _t.ContractError as exc:
                    reason = exc.reason
                _need(reason is expected_reason and not self.outstanding(), _t.Reason.MISSING)
            wire = self._run('parse', b'pass\n', policy, deadline_ns)
            _need(self.decode(wire, policy) == ('Module', (('Pass', (), ()),),
                (('body', ('list', (('node', 0),))),
                 ('type_ignores', ('list', ())))), _t.Reason.MISSING)
            self._receipt = (policy, reference, self._files, _LOCAL_CHILD)
            return self._registration

    # @anchor:wa.scripts.header.LocalParserSupervisor.check
    @_closed
    def check(self, evidence, backend, reference, policy):
        _need(evidence is self._registration and backend is self, _t.Reason.FOREIGN)
        _need(self._receipt is not None and self._receipt[0] is policy
              and self._receipt[1] == reference and self._receipt[2] == self._fingerprint()
              and self._receipt[3] == _LOCAL_CHILD, _t.Reason.STALE)
        return _t.AdmissionCheck(_t.Truth.TRUE, None)

    # @anchor:wa.scripts.header.LocalParserSupervisor.parse
    @_closed
    def parse(self, body, policy, deadline_ns, grant, holder, *, result_byte_limit=262144):
        with self._lock:
            _need(type(result_byte_limit) is int and 0 < result_byte_limit <= 262144,
                  _t.Reason.BOUNDS)
            _check(self.check(self._registration, self, policy.supervisor_reference, policy))
            _need(type(body) is bytes and len(body) <= 65536, _t.Reason.BOUNDS)
            _need(type(grant) is _t.ReservationGrant and type(holder) is _t.HolderContract,
                  _t.Reason.TYPE)
            _need(holder.retained_bytes == grant.charged_bytes
                  and holder.absolute_deadline_ns == grant.absolute_deadline_ns
                  and holder.scope == grant.request.scope, _t.Reason.FOREIGN)
            return self._run('parse', body, policy, min(deadline_ns,
                grant.absolute_deadline_ns, holder.absolute_deadline_ns,
                _time.monotonic_ns() + policy.timeout_ms * 1000000),
                min(grant.charged_bytes, result_byte_limit))

    # @anchor:wa.scripts.header.LocalParserSupervisor.decode
    @_closed
    def decode(self, wire, policy, *, result_byte_limit=262144):
        _need(type(result_byte_limit) is int and 0 < result_byte_limit <= 262144,
              _t.Reason.BOUNDS)
        _need(type(wire) is bytes and len(wire) <= result_byte_limit, _t.Reason.BOUNDS)
        value = _json_load(wire.decode('utf-8', 'strict'),
                          _types.SimpleNamespace(utf8_bytes=len(wire)), 2 * policy.ast_depth + 8)
        remaining = [policy.ast_nodes]
        def freeze(node, depth=0):
            _need(type(node) is list and len(node) == 3
                  and type(node[0]) is str and type(node[1]) is list
                  and type(node[2]) is list, _t.Reason.TYPE)
            remaining[0] -= 1
            _need(remaining[0] >= 0 and depth <= policy.ast_depth, _t.Reason.BOUNDS)
            cls = vars(_ast).get(node[0])
            _need(isinstance(cls, type) and issubclass(cls, _ast.AST)
                  and cls is not _ast.AST, _t.Reason.SCHEMA)
            _need(len(node[2]) == len(cls._fields), _t.Reason.SCHEMA)
            _need(len(node[1]) <= remaining[0], _t.Reason.BOUNDS)
            next_child = [0]
            def field_value(value, in_list=False):
                _need(type(value) is list and len(value) == 2
                      and type(value[0]) is str, _t.Reason.TYPE)
                tag, scalar = value
                if tag == 'node':
                    _need(type(scalar) is int and scalar == next_child[0]
                          and scalar < len(node[1]), _t.Reason.SCHEMA)
                    next_child[0] += 1
                elif tag == 'list':
                    _need(not in_list and type(scalar) is list, _t.Reason.TYPE)
                    return tag, tuple(field_value(v, True) for v in scalar)
                elif tag in ('none', 'ellipsis'):
                    _need(scalar is None, _t.Reason.TYPE)
                elif tag in ('str', 'int', 'bool'):
                    _need(type(scalar) is {'str': str, 'int': int, 'bool': bool}[tag],
                          _t.Reason.TYPE)
                elif tag == 'bytes':
                    _need(type(scalar) is str and len(scalar) % 2 == 0
                          and _re.fullmatch('[0-9a-f]*', scalar) is not None, _t.Reason.TYPE)
                elif tag in ('float', 'complex'):
                    parts = scalar if tag == 'complex' else [scalar]
                    _need(type(parts) is list and len(parts) == (2 if tag == 'complex' else 1),
                          _t.Reason.TYPE)
                    for part in parts:
                        _need(type(part) is str and float.fromhex(part).hex() == part,
                              _t.Reason.TYPE)
                    if tag == 'complex':
                        scalar = tuple(parts)
                else:
                    raise _t.ContractError(_t.Reason.SCHEMA)
                return tag, scalar
            fields = []
            for expected, pair in zip(cls._fields, node[2]):
                _need(type(pair) is list and len(pair) == 2 and pair[0] == expected,
                      _t.Reason.SCHEMA)
                fields.append((expected, field_value(pair[1])))
            _need(next_child[0] == len(node[1]), _t.Reason.SCHEMA)
            children = tuple(freeze(child, depth + 1) for child in node[1])
            return node[0], children, tuple(fields)
        result = freeze(value)
        _immutable_result(result, policy)
        return result


# Trusted fixed bootstrap, loaded before candidate parsing; all path access is
# denied once the seccomp filter is active. Candidate text is never executed.
_LOCAL_CHILD = r'''
import ast, ctypes, errno, json, os, resource, socket, sys
mode, library, runtime, owner_file, unrelated_fd, memory, nodes, depth, result_limit = sys.argv[1:]
nodes, depth, result_limit = int(nodes), int(depth), int(result_limit)

try:
    os.fstat(int(unrelated_fd))
    unrelated_closed = False
except OSError as error:
    unrelated_closed = error.errno == errno.EBADF
lib = ctypes.CDLL(library, use_errno=True)
libc = ctypes.CDLL(None, use_errno=True)
lib.seccomp_init.argtypes = [ctypes.c_uint32]
lib.seccomp_init.restype = ctypes.c_void_p
lib.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
lib.seccomp_syscall_resolve_name.restype = ctypes.c_int
lib.seccomp_rule_add.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int, ctypes.c_uint]
lib.seccomp_load.argtypes = [ctypes.c_void_p]
lib.seccomp_release.argtypes = [ctypes.c_void_p]
class Cmp(ctypes.Structure):
    _fields_ = [('arg', ctypes.c_uint), ('op', ctypes.c_int),
                ('a', ctypes.c_uint64), ('b', ctypes.c_uint64)]
lib.seccomp_rule_add_array.argtypes = [ctypes.c_void_p, ctypes.c_uint32,
                                     ctypes.c_int, ctypes.c_uint, ctypes.POINTER(Cmp)]
resource.setrlimit(resource.RLIMIT_AS, (int(memory), int(memory)))
resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
ctx = lib.seccomp_init(0x00050000 | errno.EPERM)
assert ctx
for name in ('close', 'fstat', 'lseek', 'munmap', 'brk', 'mremap', 'madvise',
             'rt_sigaction', 'rt_sigprocmask', 'rt_sigreturn', 'sigaltstack',
             'futex', 'clock_gettime', 'getpid', 'gettid', 'exit', 'exit_group'):
    number = lib.seccomp_syscall_resolve_name(name.encode())
    if number >= 0:
        assert lib.seccomp_rule_add(ctx, 0x7fff0000, number, 0) == 0
for name in ('read', 'write'):
    for fd in (0, 1, 2):
        comparison = Cmp(0, 4, fd, 0)  # SCMP_CMP_EQ
        assert lib.seccomp_rule_add_array(ctx, 0x7fff0000,
            lib.seccomp_syscall_resolve_name(name.encode()), 1, ctypes.byref(comparison)) == 0
for name in ('mmap', 'mprotect'):
    comparison = Cmp(2, 7, 4, 0)  # SCMP_CMP_MASKED_EQ: PROT_EXEC absent
    assert lib.seccomp_rule_add_array(ctx, 0x7fff0000,
        lib.seccomp_syscall_resolve_name(name.encode()), 1, ctypes.byref(comparison)) == 0
assert lib.seccomp_load(ctx) == 0
lib.seccomp_release(ctx)
body = sys.stdin.buffer.read(65537)
if mode == 'stall':
    while True:
        pass
if mode == 'exhaust':
    blocks = []
    try:
        while True:
            blocks.append(bytearray(1024 * 1024))
    except MemoryError:
        os.write(1, b'ENFORCED_MEMORY_LIMIT')
        os._exit(42)
if mode == 'probes':
    denied = []
    def probe(name, operation):
        try:
            operation()
        except OSError as error:
            if error.errno == errno.EPERM:
                denied.append(name)
    try:
        os.environ['S1A_TEST_NONSECRET_CANARY']
    except KeyError:
        if set(os.environ) <= {'LC_ALL'}:
            denied.append('environment')
    probe('descriptors', lambda: os.read(int(unrelated_fd), 1))
    probe('home-read', lambda: os.open(owner_file, os.O_RDONLY))
    probe('workspace-read', lambda: os.open(os.path.join(owner_file, '..', 'header.py'), os.O_RDONLY))
    probe('write', lambda: os.open(owner_file, os.O_WRONLY))
    probe('socket', lambda: socket.socket())
    # Actual network syscall using an invalid descriptor: filter must return
    # EPERM rather than the unfiltered EBADF, without contacting a network.
    address = ctypes.create_string_buffer(128)
    def connect():
        if libc.connect(-1, address, len(address)) == -1:
            raise OSError(ctypes.get_errno(), 'connect')
    probe('network', connect)
    def child():
        pid = os.fork()
        if pid == 0:
            os._exit(0)
        os.waitpid(pid, 0)
    probe('child', child)
    def trace():
        if libc.ptrace(0, 0, None, None) == -1:
            raise OSError(ctypes.get_errno(), 'ptrace')
    probe('trace', trace)
    probe('signal', lambda: os.kill(os.getpid(), 0))
    probe('exec-load', lambda: os.execv(runtime, [runtime, '-I', '-c', 'pass']))
    def executable_memory():
        libc.mmap.restype = ctypes.c_void_p
        result = libc.mmap(None, 4096, 7, 0x22, -1, 0)
        if result == ctypes.c_void_p(-1).value:
            raise OSError(ctypes.get_errno(), 'mmap')
    probe('executable-memory', executable_memory)
    tree = ast.parse(body.decode('utf-8'))
    os.write(1, json.dumps({'denied': sorted(denied),
        'unrelated_closed': unrelated_closed,
        'parsed': [type(node).__name__ for node in ast.walk(tree)]}).encode())
else:
    try:
        tree = ast.parse(body.decode('utf-8'))
        stack, count = [(tree, 0)], 0
        while stack:
            node, level = stack.pop()
            count += 1
            if count > nodes or level > depth:
                os._exit(43)
            for item in ast.iter_child_nodes(node):
                if count + len(stack) >= nodes:
                    os._exit(43)
                stack.append((item, level + 1))
        def freeze_tree(node):
            children, fields = [], []
            def value(item):
                if isinstance(item, ast.AST):
                    index = len(children)
                    children.append(freeze_tree(item))
                    return ['node', index]
                if type(item) is list:
                    return ['list', [value(part) for part in item]]
                if item is None:
                    return ['none', None]
                if item is Ellipsis:
                    return ['ellipsis', None]
                if type(item) in (str, int, bool):
                    return [type(item).__name__, item]
                if type(item) is bytes:
                    return ['bytes', item.hex()]
                if type(item) is float:
                    return ['float', item.hex()]
                if type(item) is complex:
                    return ['complex', [item.real.hex(), item.imag.hex()]]
                raise ValueError('unsupported AST scalar')
            for name, item in ast.iter_fields(node):
                fields.append([name, value(item)])
            return [type(node).__name__, children, fields]
        wire = json.dumps(freeze_tree(tree), separators=(',', ':')).encode()
        if len(wire) > result_limit:
            os._exit(43)
        os.write(1, wire)
    except (SyntaxError, UnicodeError, ValueError):
        os._exit(44)
    except MemoryError:
        os._exit(42)
'''
