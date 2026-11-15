# @module {"id":"wa.learn.mw_profiles","layer":4,"owner":"LEARN","deps":["wa.tools.memoryweb_gate","wa.learn.mw_owners_srf","wa.learn.mw_live_evidence","wa.learn.mw_locate_ai","wa.learn.mw_workflow","wa.learn.mw_handlers","wa.learn.demonstrations","wa.act.actor","wa.perceive.srf_identity","wa.perceive.vision_bridge","wa.scripts.types"],"book":"HB-WA-s4-learn"}
"""Private named configuration; never a source of execution authority.

Item-local MWP8H contracts: closed schema, trusted_notice_v1, binary pinned ack,
profile/ack caps 16384/4096 bytes, scalar/coordinate caps 4096/256 bytes,
and 128 path components [TARGET]. Legacy profiles are explicitly inactive.
New profiles require a guarded export directory and an independently activated
SHA-256 pin. Runtime token custody includes inode and mutation metadata.
None selects invocation-local default
provisioning after binding validation. The default context composes configured
trusted owners, jointly validates budgets, verifies private RAM custody, rereads
acknowledgements and binds the surface workspace explicitly. Wave-29 capacity
targets and durable notice/flock custody are defined locally. Corrected capacities
pass the unchanged aggregate gates; unsupported semantic contracts refuse. A private item-local journal
owns digest framing, durable append, recovery and atomic local selection. No policy is inferred
from profile JSON; failed capacity admission refuses before allocation.
Explicit trusted ProfileServices remain supported. This is composition support,
not production runtime certification.
Identity contents are never read here. Descriptor guards detect mutation until
handoff; the frozen Path-based surface cannot prevent later same-user changes.
"""
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, fields, replace
from contextvars import ContextVar
import copy
import fcntl
import hmac
import hashlib
import inspect
import io
import os
from pathlib import Path
import re
import selectors
import signal
import subprocess
import stat
import sys
import shutil
import tempfile
import threading
import time
import uuid
import weakref

from tools import memoryweb_gate as gate
from wender.learn.mw_handlers import SignalHandlers, SignalFrame, SignalRefused, parse_signal
from wender.learn.mw_owners_srf import LiveInvocation, NativeReadPolicy, build_live_ports, DownloadsObservations
from wender.learn.mw_live_evidence import LiveEvidencePolicy, LiveEvidenceTrust, LiveOCRDescriber, run_live_workflow, _asset
from wender.learn.mw_locate_ai import LocatePolicy, _ram_path
from wender.learn.mw_workflow import PinnedArtifact, WorkflowLimits, ObservationValue, GroundedTarget
from wender.learn.demonstrations import DemoLimits, _png
from wender.act.actor import ActorLock
from wender.perceive.srf_identity import BINARY, SrfIdentitySurface
from wender.perceive.vision_bridge import DescribeResult, Mark
from wender.scripts.types import AdmissionCheck, Reason, Truth


# @anchor:wa.s4.mw_profiles.Profile
@dataclass(frozen=True, repr=False)
class Profile:
    site: str
    capsule: str
    container: str
    window: str
    identity_env: Path
    announce_gate: str
    live_ack_source: Path
    allowed_modes: tuple[str, ...]
    export_dir: Path | None = None
    ack_sha256: str | None = None


# @anchor:wa.s4.mw_profiles.ProfileServices
@dataclass(frozen=True, repr=False)
class ProfileServices:
    dependencies: object
    locator_arguments: object


# @anchor:wa.s4.mw_profiles.ProfileError
class ProfileError(ValueError):
    # @anchor:wa.s4.mw_profiles.ProfileError.__init__
    def __init__(self, rc=5, *, reason=None):
        self.reason = reason
        self.rc = 2 if rc == 2 else 5
        self.category = 'USAGE_SCHEMA' if self.rc == 2 else 'OUT_OF_SUPPORT'
        super().__init__(self.category)


def _need(ok, label, rc=5):
    if not ok:
        raise ProfileError(rc) from None


def _text(value, cap=4096, coordinate=False):
    _need(type(value) is str, 'string_type', 2)
    try:
        size = len(value.encode('utf-8', errors='strict'))
    except UnicodeError:
        raise ProfileError(2) from None
    _need(0 < size <= cap and not any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in value), 'scalar', 2)
    _need(not coordinate or (value.strip() and not value.startswith('-')), 'coordinate', 2)
    return value


def _path(value):
    _text(value)
    _need(value.startswith('/') and '..' not in value.split('/'), 'absolute_path', 2)
    return Path(value)


def _metadata(s):
    return (s.st_dev, s.st_ino, s.st_mode, s.st_uid, s.st_size, s.st_mtime_ns, s.st_ctime_ns)


@contextmanager
def _guard(path, *, directory=False):
    """Keep the entire no-follow descriptor chain alive and check its bindings."""
    fds, links = [], []
    try:
        path = _path(str(path))
        parts = path.parts[1:]
        _need(0 < len(parts) <= 128, 'components')
        fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        fds.append(fd)
        for index, part in enumerate(parts):
            parent_stat = os.fstat(fd)
            sticky = bool(parent_stat.st_mode & stat.S_ISVTX)
            _need(not parent_stat.st_mode & 0o022 or sticky, 'directory_mode')
            _need(parent_stat.st_uid in (0, os.geteuid()), 'directory_owner')
            try:
                os.stat('.git', dir_fd=fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                _need(False, 'git_tree')
            leaf = index == len(parts) - 1
            flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
            if not leaf or directory:
                flags |= os.O_DIRECTORY
            child = os.open(part, flags, dir_fd=fd)
            fds.append(child)
            before = os.fstat(child)
            links.append((fd, part, child, before))
            if leaf:
                if directory:
                    _need(stat.S_ISDIR(before.st_mode), 'export_type')
                    _need(before.st_uid == os.geteuid(), 'export_owner')
                    _need(not before.st_mode & 0o022, 'export_mode')
                else:
                    _need(stat.S_ISREG(before.st_mode), 'regular')
                    _need(before.st_uid == os.geteuid(), 'file_owner')
                    _need(stat.S_IMODE(before.st_mode) == 0o600, 'file_mode')
            fd = child
        yield fd
        for parent, name, child, before in links:
            current = os.fstat(child)
            named = os.stat(name, dir_fd=parent, follow_symlinks=False)
            width = 7 if stat.S_ISREG(before.st_mode) else 4
            _need(_metadata(current)[:width] == _metadata(before)[:width] and
                  _metadata(named)[:width] == _metadata(before)[:width], 'mutation')
    except ProfileError:
        raise
    except (OSError, ValueError, TypeError, UnicodeError):
        raise ProfileError() from None
    finally:
        for fd in reversed(fds):
            os.close(fd)


def _read(fd, limit):
    _need(os.fstat(fd).st_size <= limit, 'read_size')
    raw = bytearray()
    while len(raw) <= limit:
        chunk = os.read(fd, limit + 1 - len(raw))
        if not chunk:
            break
        raw.extend(chunk)
    _need(len(raw) <= limit, 'read_overflow')
    return bytes(raw)


# @anchor:wa.s4.mw_profiles.load_profile
def load_profile(site, *, profile_dir=None) -> Profile:
    _need(type(site) is str and re.fullmatch(r'[a-z0-9](?:[a-z0-9_-]{0,62}[a-z0-9])?', site) is not None, 'site', 2)
    directory = Path.home() / '.config/memoryweb/profiles' if profile_dir is None else Path(profile_dir)
    with _guard(directory / (site + '.json')) as fd:
        raw = _read(fd, 16384)
    try:
        value = gate.strict_load(raw)
    except Exception:
        raise ProfileError(2) from None
    finally:
        del raw
    fields = {'capsule', 'container', 'window', 'identity_env', 'announce_gate', 'live_ack_source', 'allowed_modes'}
    _need(type(value) is dict and set(value) in (fields, fields | {'export_dir', 'ack_sha256'}), 'root_schema', 2)
    legacy = set(value) == fields
    export = None if legacy else _path(value['export_dir'])
    digest = None if legacy else value['ack_sha256']
    _need(digest is None or (type(digest) is str and re.fullmatch(r'[0-9a-f]{64}', digest) is not None),
          'digest_schema', 2)
    coordinates = [_text(value[k], 256, True) for k in ('capsule', 'container', 'window')]
    identity = _path(value['identity_env'])
    kind = _text(value['announce_gate'])
    _need(kind == 'trusted_notice_v1', 'gate_kind')
    source = value['live_ack_source']
    _need(type(source) is dict and set(source) == {'kind', 'path'}, 'source_schema', 2)
    _text(source['kind'])
    _need(source['kind'] == 'file', 'source_kind')
    ack = _path(source['path'])
    modes = value['allowed_modes']
    _need(type(modes) is list and bool(modes) and all(type(x) is str for x in modes), 'modes_type', 2)
    _need(len(set(modes)) == len(modes) and set(modes) <= {'replay', 'shadow', 'promoted', 'repair'}, 'modes_schema', 2)
    with ExitStack() as stack:
        stack.enter_context(_guard(identity))
        if export is not None:
            stack.enter_context(_guard(export, directory=True))
        if digest is not None:
            stack.enter_context(_guard(ack))
        return Profile(site, *coordinates, identity, kind, ack, tuple(modes), export, digest)


# Invocation identities hold only a weak reference and guarded metadata, never tokens.
_TOKEN_BINDINGS = {}


def _token_digest(profile):
    _need(type(profile.ack_sha256) is str and
          re.fullmatch(r'[0-9a-f]{64}', profile.ack_sha256) is not None, 'profile_active')
    with _guard(profile.live_ack_source) as fd:
        raw = _read(fd, 4096)
        _need(32 <= len(raw) <= 4096, 'ack_empty')  # [TARGET] byte bounds
        digest = hashlib.sha256(raw).hexdigest()
        del raw
        _need(hmac.compare_digest(digest, profile.ack_sha256), 'token_pin')
        metadata = _metadata(os.fstat(fd))
    return digest, metadata


def _validate_token(profile, invocation):
    digest, metadata = _token_digest(profile)
    binding = _TOKEN_BINDINGS.get(id(invocation))
    _need(binding is not None and binding[0]() is invocation and binding[1] == metadata,
          'token_binding')
    _need(hmac.compare_digest(digest, invocation.live_ack), 'notice_ack')
    return digest


# @anchor:wa.s4.mw_profiles.make_invocation
def make_invocation(profile, *, workflow, output, mode) -> LiveInvocation:
    _need(type(profile) is Profile, 'profile_type')
    _need(mode in profile.allowed_modes and mode in ('replay', 'shadow'), 'mode')
    _need(profile.ack_sha256 is not None and profile.export_dir is not None, 'profile_active')
    with _guard(profile.identity_env), _guard(profile.export_dir, directory=True):
        digest, metadata = _token_digest(profile)
        invocation = LiveInvocation(workflow=Path(workflow), output=Path(output), mode=mode,
            identity=profile.capsule, container=profile.container, window=profile.window,
            identity_env=profile.identity_env, live_ack=digest)
        key = id(invocation)
        _TOKEN_BINDINGS[key] = (weakref.ref(invocation, lambda ref: _TOKEN_BINDINGS.pop(key, None)), metadata)
        return invocation


# @anchor:wa.s4.mw_profiles._PinnedAuthorization
class _PinnedAuthorization:
    """Recheck the independent operator pin at every delegated effect boundary."""
    # @anchor:wa.s4.mw_profiles._PinnedAuthorization.__init__
    def __init__(self, profile, invocation, delegate):
        self._profile, self._invocation, self._delegate = profile, invocation, delegate

    # @anchor:wa.s4.mw_profiles._PinnedAuthorization.inventory
    def inventory(self, workflow):
        _validate_token(self._profile, self._invocation)
        return self._delegate.inventory(workflow)

    # @anchor:wa.s4.mw_profiles._PinnedAuthorization.check
    def check(self, phase, workflow, scope, authority):
        try:
            _validate_token(self._profile, self._invocation)
            return self._delegate.check(phase, workflow, scope, authority)
        except Exception:
            return AdmissionCheck(Truth.FALSE, Reason.STALE)


def _arguments(value, function, excluded):
    parameters = inspect.signature(function).parameters
    allowed = {k for k, p in parameters.items() if k not in excluded}
    required = {k for k in allowed if parameters[k].default is inspect.Parameter.empty}
    _need(type(value) is dict and required <= set(value) <= allowed, 'arguments')
    return value


# Host installation is a trusted Python callback, never a profile/argv import.
# No installed provider or deployment policy is present in this corpus.
_HOST_CONFIGURATION = ContextVar('mw_profile_host', default=None)
_RAM_ROOT = Path('/dev/shm/vqa-cu')


# @anchor:wa.s4.mw_profiles.profile_host
@contextmanager
def profile_host(configuration):
    """Bind an explicit trusted host provider to this invocation context.

    This embedding API does not load executable code, budgets or authority from
    a profile. Defaults provide custody; unsupported semantic contracts refuse.
    """
    _need(callable(configuration), 'host_installed')
    token = _HOST_CONFIGURATION.set(configuration)
    try:
        yield
    finally:
        _HOST_CONFIGURATION.reset(token)


# @anchor:wa.s4.mw_profiles._yes
def _yes(value):
    return type(value) is AdmissionCheck and value.result is Truth.TRUE and value.reason is None


# @anchor:wa.s4.mw_profiles._HostDurations
@dataclass(frozen=True, repr=False)
class _HostDurations:
    """Conductor duration targets [TARGET]; step/generation consumers AUSENTE."""
    run_ns: int = 1_800_000_000_000
    step_ns: int = 60_000_000_000
    generation_wait_ns: int = 420_000_000_000
    locate_calls: int = 8
    locate_call_ns: int = 30_000_000_000
    ocr_control_ns: int = 10_000_000_000
    drain_ns: int = 10_000_000_000


_LEASE_ROOT = Path('/dev/shm/vqa-leases')


# @anchor:wa.s4.mw_profiles._UnsupportedContracts
class _UnsupportedContracts:
    """Deny unsupported semantic contracts without manufacturing observations."""
    # @anchor:wa.s4.mw_profiles._UnsupportedContracts.inventory
    def inventory(self, *args, **kwargs):
        return AdmissionCheck(Truth.FALSE, Reason.MISSING)

    # @anchor:wa.s4.mw_profiles._UnsupportedContracts.check
    def check(self, *args, **kwargs):
        return AdmissionCheck(Truth.FALSE, Reason.MISSING)

    # @anchor:wa.s4.mw_profiles._UnsupportedContracts.supports
    def supports(self, *args, **kwargs):
        return AdmissionCheck(Truth.FALSE, Reason.MISSING)

    # @anchor:wa.s4.mw_profiles._UnsupportedContracts.target
    def target(self, *args, **kwargs):
        return AdmissionCheck(Truth.FALSE, Reason.MISSING)

    # @anchor:wa.s4.mw_profiles._UnsupportedContracts.validate_target
    def validate_target(self, *args, **kwargs):
        return AdmissionCheck(Truth.FALSE, Reason.MISSING)

    # @anchor:wa.s4.mw_profiles._UnsupportedContracts.focus
    def focus(self, *args, **kwargs):
        return AdmissionCheck(Truth.FALSE, Reason.MISSING)

    # @anchor:wa.s4.mw_profiles._UnsupportedContracts.action
    def action(self, *args, **kwargs):
        return AdmissionCheck(Truth.FALSE, Reason.MISSING)

    # @anchor:wa.s4.mw_profiles._UnsupportedContracts.verifier
    def verifier(self, *args, **kwargs):
        return AdmissionCheck(Truth.FALSE, Reason.MISSING)

    # @anchor:wa.s4.mw_profiles._UnsupportedContracts.equivalent
    def equivalent(self, *args, **kwargs):
        return AdmissionCheck(Truth.FALSE, Reason.MISSING)

    # @anchor:wa.s4.mw_profiles._UnsupportedContracts.satisfaction
    def satisfaction(self, *args, **kwargs):
        return AdmissionCheck(Truth.FALSE, Reason.MISSING)

    # @anchor:wa.s4.mw_profiles._UnsupportedContracts.final
    def final(self, *args, **kwargs):
        return AdmissionCheck(Truth.FALSE, Reason.MISSING)

    # @anchor:wa.s4.mw_profiles._UnsupportedContracts.changed
    def changed(self, *args, **kwargs):
        return AdmissionCheck(Truth.FALSE, Reason.MISSING)

    # @anchor:wa.s4.mw_profiles._UnsupportedContracts.acquire
    def acquire(self, *args, **kwargs):
        return AdmissionCheck(Truth.FALSE, Reason.MISSING)

    # @anchor:wa.s4.mw_profiles._UnsupportedContracts.validate
    def validate(self, *args, **kwargs):
        return AdmissionCheck(Truth.FALSE, Reason.MISSING)


# @anchor:wa.s4.mw_profiles._ProfileAuthorization
class _ProfileAuthorization:
    """Run-local authorization; every validated step is conservatively required.

    This grants lifecycle scope, never semantic capability or final evidence.
    The frozen runner validates the workflow before requesting its inventory.
    """

    # @anchor:wa.s4.mw_profiles._ProfileAuthorization.__init__
    def __init__(self, record):
        self.record = record
        self.workflow = None
        self.pin = None
        self.segments = None

    # @anchor:wa.s4.mw_profiles._ProfileAuthorization._current
    def _current(self):
        r = self.record
        p, invocation = r.profile, r.invocation
        _validate_token(p, invocation)
        _need(r.active and time.monotonic_ns() < r.config.limits.deadline_ns,
              'authorization_active')
        _need(invocation.mode in p.allowed_modes and invocation.mode in ('replay', 'shadow'),
              'authorization_mode')
        _need((invocation.identity, invocation.container, invocation.window, invocation.identity_env) ==
              (p.capsule, p.container, p.window, p.identity_env), 'authorization_profile')
        _need(_yes(r.config.lease(invocation=invocation, scope=r.scope, authority=r.authority)),
              'authorization_lease')
        with _guard(invocation.workflow) as fd:
            before = _metadata(os.fstat(fd))
            raw = _read(fd, r.config.limits.workflow_bytes)
            _need(before == _metadata(os.fstat(fd)), 'authorization_workflow')
        return raw, before

    # @anchor:wa.s4.mw_profiles._ProfileAuthorization.inventory
    def inventory(self, workflow):
        raw, metadata = self._current()
        _need(type(workflow) is dict and workflow.get('schema') == 'mw.workflow.v1' and
              raw == (gate.canon(workflow) + '\n').encode(), 'authorization_workflow')
        steps = workflow.get('steps')
        _need(type(steps) is list and 0 < len(steps) <= self.record.config.limits.steps and
              all(type(step) is dict and type(step.get('seq')) is int and step['seq'] == index and
                  type(step.get('protected')) is bool for index, step in enumerate(steps, 1)),
              'authorization_segments')
        if self.workflow is None:
            self.workflow = workflow
            self.pin = (hashlib.sha256(raw).hexdigest(), metadata)
            self.segments = tuple(('step_' + str(step['seq']), (step['seq'],)) for step in steps)
        _need(self.pin == (hashlib.sha256(raw).hexdigest(), metadata),
              'authorization_pin')
        return dict(self.segments)

    # @anchor:wa.s4.mw_profiles._ProfileAuthorization.check
    def check(self, phase, workflow, scope, authority):
        try:
            raw, metadata = self._current()
            _need(scope is self.record.scope and authority is self.record.authority,
                  'authorization_scope')
            _need(phase in ('admission', 'step', 'send', 'commit'), 'authorization_phase')
            _need(self.workflow is not None and
                  raw == (gate.canon(workflow) + '\n').encode() and
                  self.pin == (hashlib.sha256(raw).hexdigest(), metadata), 'authorization_pin')
            return AdmissionCheck(Truth.TRUE, None)
        except Exception:
            return AdmissionCheck(Truth.FALSE, Reason.STALE)


# @anchor:wa.s4.mw_profiles._ProducerEvidence
@dataclass(frozen=True, repr=False)
class _ProducerEvidence:
    producer: str
    digest: str | None
    operations: frozenset
    observed_ns: int
    output_bytes: int | None
    valid: bool
    reason: object


# @anchor:wa.s4.mw_profiles._ProducerProbeContract
@dataclass(frozen=True, repr=False)
class _ProducerProbeContract:
    """Trusted capabilities wire contract, never supplied by a profile."""
    arguments: tuple
    header: str


_PRODUCTION_PROBE = _ProducerProbeContract(('capabilities',), 'srf-identity-surface capabilities v1')
_SIGNAL_IDS = frozenset(('mw.ocr.text_present.v1', 'mw.ocr.text_absent.v1', 'mw.frame.stable.v1'))
_ADAPTER_VERSION = '1.0.0'


# @anchor:wa.s4.mw_profiles._producer_evidence
def _producer_evidence(clock, *, record=None, contract=None):
    child = None
    reason = Reason.MISSING
    try:
        _need(record is not None and type(contract) is _ProducerProbeContract and
              type(contract.arguments) is tuple and bool(contract.arguments) and
              all(type(v) is str and v for v in contract.arguments) and
              type(contract.header) is str and contract.header, 'probe_contract')
        policy = record.config.native_policy
        deadline = min(record.config.limits.deadline_ns, clock() + policy.timeout_ns)
        if clock() >= deadline:
            reason = Reason.DEADLINE
            raise ProfileError()
        # Combined output plus decoding/tokenization headroom, before Popen.
        if not _yes(record.resources.reserve('producer_probe', 64 * 65536)):
            reason = Reason.CAPACITY
            raise ProfileError()
        buffer = bytearray(65536)
        used = 0
        stderr_seen = False
        child = subprocess.Popen([BINARY, *contract.arguments], shell=False,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            start_new_session=True)
        with selectors.DefaultSelector() as selector:
            for pipe in (child.stdout, child.stderr):
                os.set_blocking(pipe.fileno(), False)
                selector.register(pipe, selectors.EVENT_READ, pipe is child.stderr)
            while selector.get_map():
                if clock() >= deadline:
                    reason = Reason.DEADLINE
                    raise ProfileError()
                for key, _ in selector.select(min(0.05, max(0, deadline - clock()) / 1e9)):
                    chunk = os.read(key.fileobj.fileno(), min(4096, 65537 - used))
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    if used + len(chunk) > 65536:
                        reason = Reason.CAPACITY
                        raise ProfileError()
                    stderr_seen = stderr_seen or key.data
                    buffer[used:used + len(chunk)] = chunk
                    used += len(chunk)
        remaining = deadline - clock()
        if remaining <= 0:
            reason = Reason.DEADLINE
            raise ProfileError()
        try:
            child.wait(timeout=remaining / 1e9)
        except subprocess.TimeoutExpired:
            reason = Reason.DEADLINE
            raise ProfileError() from None
        _need(child.returncode == 0 and clock() < deadline and not stderr_seen, 'probe_status')
        raw = bytes(memoryview(buffer)[:used])
        text = raw.decode('utf-8', errors='strict')
        lines = text.split('\n')
        _need(len(lines) == 3 and lines[2] == '' and lines[0] == contract.header and
              all(32 <= ord(c) < 127 for c in lines[1]), 'probe_format')
        tokens = lines[1].split()
        vocabulary = {'--paste': 'paste', '--type-stdin': 'type-stdin',
                      '--scroll': 'scroll', '--drag': 'drag', 'downloads': 'downloads'}
        _need(bool(tokens) and len(tokens) == len(set(tokens)) and
              all(token in vocabulary for token in tokens), 'probe_tokens')
        return _ProducerEvidence(BINARY, hashlib.sha256(raw).hexdigest(),
            frozenset(vocabulary[token] for token in tokens), clock(), used, True, None)
    except Exception:
        return _ProducerEvidence(BINARY, None, frozenset(), clock(), None, False, reason)
    finally:
        if child is not None:
            # No reader threads survive; kill and join the entire isolated group.
            cleanup = min(record.config.limits.deadline_ns, clock() + record.config.native_policy.join_ns)
            # Guard: a non-int (mocked) or <= 1 pid makes killpg act on every process (kill(-1)); never signal our own group.
            if type(child.pid) is int and child.pid > 1 and child.pid != os.getpgid(0):
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            try:
                child.wait(timeout=max(0, cleanup - clock()) / 1e9)
            except subprocess.TimeoutExpired:
                # Unproven cleanup stops composition, even if probe evidence was positive.
                raise ProfileError(reason=Reason.DEADLINE) from None
            finally:
                for pipe in (child.stdout, child.stderr):
                    pipe.close()


# @anchor:wa.s4.mw_profiles._ProfileCapabilities
class _ProfileCapabilities:
    """Frozen consumers dispatch only explicitly registered trusted semantics.

    handlers[(method, capability_id)] = (operation, callback, required_ports).
    This table is trusted Python composition state, never workflow/profile data.
    No arbitrary production semantic detector or final oracle is registered.
    """

    # @anchor:wa.s4.mw_profiles._ProfileCapabilities.__init__
    def __init__(self, authorization, producer):
        self.authorization, self.producer = authorization, producer
        self.handlers = {}
        self.observations = None
        self.targets = {}
        self.final_proof = None

    # @anchor:wa.s4.mw_profiles._ProfileCapabilities.operation
    def operation(self, name):
        p, r = self.producer, self.authorization.record
        valid = (type(p) is _ProducerEvidence and p.valid is True and p.reason is None and
                 p.producer == BINARY and type(p.digest) is str and
                 re.fullmatch(r'[0-9a-f]{64}', p.digest) is not None and
                 type(p.output_bytes) is int and 0 < p.output_bytes <= 65536 and
                 type(p.observed_ns) is int and 0 < p.observed_ns <= time.monotonic_ns() <
                 r.config.limits.deadline_ns and type(p.operations) is frozenset and
                 p.operations <= frozenset(('paste', 'type-stdin', 'scroll', 'drag', 'downloads')) and
                 (name is None or name in p.operations))
        return AdmissionCheck(Truth.TRUE, None) if valid else AdmissionCheck(Truth.UNKNOWN, Reason.MISSING)

    # @anchor:wa.s4.mw_profiles._ProfileCapabilities._invoke
    def _invoke(self, method, spec, *args):
        try:
            a, r = self.authorization, self.authorization.record
            _need(_yes(a.check('step', a.workflow, r.scope, r.authority)), 'capability_authorization')
            key = spec.get('capability_id', spec.get('id')) if type(spec) is dict else None
            if method == 'action' and type(spec) is dict and key is None:
                key = spec.get('kind')
            handler = self.handlers.get((method, key))
            if handler is None and getattr(self.observations, 'signals', None) is not None:
                _need(_yes(self.operation(None)), 'producer_evidence')
                return self._production(method, spec, *args)
            _need(type(handler) is tuple and len(handler) == 3 and callable(handler[1]), 'capability_handler')
            operation, callback, required = handler
            _need(_yes(self.operation(operation)), 'capability_operation')
            _need(self.observations is r.owners.observations and self.observations.ports is r.ports and
                  r.ports is not None and type(required) is tuple and
                  all(name in ('ram', 'slots', 'private_paste', 'native_read') and
                      getattr(r.ports, name) is not None for name in required), 'capability_owners')
            _need((operation not in ('paste', 'type-stdin') or r.ports.private_paste is not None) and
                  (operation != 'paste' or _yes(self.operation('type-stdin'))) and
                  (operation != 'scroll' or (method == 'action' and callable(getattr(self, 'plan', None)) and args and
                   type(args[0]) is GroundedTarget and args[0].support_owner is self and
                   id(args[0]) in self.targets and self.targets[id(args[0])][0] is args[0] and
                   _yes(self.observations.validate(args[0].observation)) and
                   time.monotonic_ns() < args[0].expires_ns <= r.config.limits.deadline_ns)), 'capability_transport')
            if method == 'action' or operation in ('paste', 'scroll', 'type-stdin'):
                self.observations.invalidate()
            return callback(spec, *args)
        except Exception:
            return AdmissionCheck(Truth.FALSE, Reason.MISSING)

    # @anchor:wa.s4.mw_profiles._ProfileCapabilities._signal
    def _signal(self, predicate):
        r = self.authorization.record
        return parse_signal(predicate, metadata_bytes=r.config.evidence_policy.metadata_bytes,
                            string_bytes=r.config.native_policy.string_bytes)

    # @anchor:wa.s4.mw_profiles._ProfileCapabilities.capability_check
    def capability_check(self, obj, predicate):
        try:
            _need(_yes(self.operation(None)), 'producer_evidence')
            _need(type(obj) is dict, 'capability_shape')
            if predicate is not None:
                self._signal(predicate)
            else:
                _need(obj.get('engine') == 'wender-agent' and
                      obj.get('version_constraint') == '==' + _ADAPTER_VERSION and
                      type(obj.get('required_capabilities')) is list and bool(obj['required_capabilities']) and
                      all(v in _SIGNAL_IDS for v in obj['required_capabilities']), 'adapter_version')
            return gate.GateResult(0, 'PASS')
        except Exception:
            return gate.GateResult(5, 'CAPABILITY_MISSING')

    # @anchor:wa.s4.mw_profiles._ProfileCapabilities.evidence_check
    def evidence_check(self, kind, summary, related):
        try:
            if kind == 'provenance':
                _need(type(summary) is dict and summary.get('schema') == 'mw.workflow.v1' and
                      summary.get('status') == 'draft' and
                      summary.get('provenance', {}).get('source') == 'human' and
                      summary in related, 'local_provenance')
                self.authorization.inventory(summary)
                _need(_yes(self.authorization.check('admission', summary,
                    self.authorization.record.scope, self.authorization.record.authority)), 'provenance_authority')
                return gate.GateResult(0, 'PASS')
            if kind == 'ordering':
                _need(type(summary) is dict and summary.get('action', {}).get('kind') == 'wait_signal' and
                      summary.get('when') is None and summary.get('repeat') is None and
                      summary.get('verify', {}).get('busy_predicate') is not None, 'ordering_shape')
                workflow = next(v for v in related if type(v) is dict and v.get('schema') == 'mw.workflow.v1')
                _need(summary in workflow['steps'], 'ordering_owner')
                self.authorization.inventory(workflow)
                _need(_yes(self.authorization.check('admission', workflow,
                    self.authorization.record.scope, self.authorization.record.authority)), 'ordering_authority')
                self._signal(summary['verify']['busy_predicate'])
                self._signal(summary['verify']['predicate'])
                return gate.GateResult(0, 'PASS')
            _need(kind == 'run_inventory' and type(summary) is dict and summary.get('rc') == 0 and
                  summary.get('verdict_code') == 'VERIFIED' and related and
                  type(related[0]) is dict and related[0].get('final_task_pass') is True and
                  self.final_proof is not None and
                  self.final_proof[0] == self.authorization.pin and
                  self.final_proof[1] == hashlib.sha256((gate.canon(self.authorization.workflow) + '\n').encode()).hexdigest() and
                  time.monotonic_ns() < self.final_proof[2], 'run_semantics')
            return gate.GateResult(0, 'PASS')
        except Exception:
            return gate.GateResult(5, 'EVIDENCE_DENIED')

    # @anchor:wa.s4.mw_profiles._ProfileCapabilities._action_support
    def _action_support(self, action):
        kind = action.get('kind')
        if kind == 'wait_signal':
            return
        _need(kind in ('paste', 'scroll'), 'action_support')
        handler = self.handlers.get(('action', kind))
        _need(type(handler) is tuple and len(handler) == 3 and handler[0] == kind and
              callable(handler[1]) and type(handler[2]) is tuple and
              _yes(self.operation(kind)), 'native_handler')
        ports = self.authorization.record.ports
        _need(ports is not None and all(name in ('ram', 'slots', 'private_paste', 'native_read') and
              getattr(ports, name) is not None for name in handler[2]), 'native_ports')
        if kind == 'paste':
            _need(_yes(self.operation('type-stdin')) and ports.private_paste is not None and
                  ports.slots is not None and
                  type(self.handlers.get(('focus', None))) is tuple and
                  callable(self.handlers['focus', None][1]), 'paste_support')
        else:
            _need(callable(getattr(self, 'plan', None)), 'scroll_planner')

    # @anchor:wa.s4.mw_profiles._ProfileCapabilities._production
    def _production(self, method, spec, *args):
        r, observations = self.authorization.record, self.observations
        _need(observations is r.owners.observations and observations.ports is r.ports and
              r.ports is not None, 'capability_owners')
        if method in ('supports', 'final'):
            _need(gate.canon(spec) == gate.canon(self.authorization.workflow), 'workflow_contract')
        if method == 'verifier':
            _need(spec.get('mode') == 'state_signal', 'verifier_mode')
            self._signal(spec.get('predicate'))
        elif method == 'equivalent':
            _need(len(args) == 1 and spec.get('mode') == args[0].get('mode') == 'state_signal' and
                  self._signal(spec.get('predicate')) == self._signal(args[0].get('predicate')) and
                  all(spec.get(k) == args[0].get(k) for k in ('busy_predicate', 'deadline_ms')) and
                  not args[0].get('fallback_verifiers'), 'equivalent')
        elif method == 'action':
            _need(spec.get('kind') == 'wait_signal', 'action_support')
        elif method == 'supports':
            _need(spec.get('schema') == 'mw.workflow.v1' and bool(spec.get('engine_compat')), 'workflow_shape')
            for engine in spec['engine_compat']:
                _need(self.capability_check(engine, None) == gate.GateResult(0, 'PASS'), 'engine_support')
            def walk(value):
                if type(value) is dict:
                    if 'comparison' in value:
                        self._signal(value)
                    elif 'mode' in value and 'predicate' in value:
                        _need(_yes(self._production('verifier', value)), 'verifier_support')
                    elif 'delivery_budget_ms' in value:
                        self._action_support(value)
                    elif 'capability_id' in value:
                        _need(self.capability_check(value, None) == gate.GateResult(0, 'PASS'), 'capability_support')
                    for child in value.values():
                        walk(child)
                elif type(value) is list:
                    for child in value:
                        walk(child)
            walk(spec)
        elif method == 'final':
            predicates = spec.get('oracle', {}).get('postconditions')
            _need(type(predicates) is list and bool(predicates), 'final_predicates')
            values = []
            for predicate in predicates:
                self._signal(predicate)
                identity = gate.canon(predicate)
                candidates = [value for publication, value in observations.registered.items()
                              if observations.predicates.get(publication) == identity]
                _need(bool(candidates), 'final_publication')
                value = candidates[-1]
                _need(_yes(observations.validate(value)) and value.value is True and
                      value.output_kind == 'boolean' and value.unit == 'truth', 'final_semantics')
                values.append(value)
            self._final_expiry = min(observations.expiries[v.publication] for v in values)
            return values[-1]
        else:
            raise ProfileError()
        return AdmissionCheck(Truth.TRUE, None)

    # @anchor:wa.s4.mw_profiles._ProfileCapabilities.supports
    def supports(self, workflow):
        return self._invoke('supports', workflow)

    # @anchor:wa.s4.mw_profiles._ProfileCapabilities.target
    def target(self, target, state, deadline):
        value = self._invoke('target', target, state, deadline)
        r = self.authorization.record
        if (type(value) is GroundedTarget and value.support_owner is self and
                value.scope is r.scope and value.authority is r.authority and
                _yes(self.observations.validate(value.observation)) and
                time.monotonic_ns() < value.expires_ns <= min(deadline, r.config.limits.deadline_ns) and
                len(self.targets) < r.config.limits.acquisitions):
            self.targets[id(value)] = (value, gate.canon(target), state)
            return value
        return None

    # @anchor:wa.s4.mw_profiles._ProfileCapabilities.validate_target
    def validate_target(self, target, value, state):
        row = self.targets.get(id(value))
        if (row is None or row[0] is not value or row[1] != gate.canon(target) or row[2] is not state or
                time.monotonic_ns() >= value.expires_ns or not _yes(self.observations.validate(value.observation))):
            return AdmissionCheck(Truth.FALSE, Reason.MISSING)
        return self._invoke('validate_target', target, value, state)

    # @anchor:wa.s4.mw_profiles._ProfileCapabilities.focus
    def focus(self, step, state):
        return self._invoke('focus', step, state)

    # @anchor:wa.s4.mw_profiles._ProfileCapabilities.action
    def action(self, action, target):
        return self._invoke('action', action, target)

    # @anchor:wa.s4.mw_profiles._ProfileCapabilities.verifier
    def verifier(self, spec):
        return self._invoke('verifier', spec)

    # @anchor:wa.s4.mw_profiles._ProfileCapabilities.equivalent
    def equivalent(self, spec, child):
        return self._invoke('equivalent', spec, child)

    # @anchor:wa.s4.mw_profiles._ProfileCapabilities.satisfaction
    def satisfaction(self, step, state):
        return self._invoke('satisfaction', step, state)

    # @anchor:wa.s4.mw_profiles._ProfileCapabilities.final
    def final(self, workflow):
        self.final_proof = None
        value = self._invoke('final', workflow)
        valid = (type(value) is ObservationValue and self.observations is not None and
                 _yes(self.observations.validate(value)) and value.output_kind == 'boolean' and value.value is True)
        if valid and type(value) is ObservationValue:
            # The runner drains capture custody before journal publication. Keep
            # only an expiring attestation, never OCR or predicate associations.
            self.final_proof = (self.authorization.pin,
                hashlib.sha256((gate.canon(workflow) + '\n').encode()).hexdigest(),
                getattr(self, '_final_expiry', self.observations.expiries[value.publication]))
        return AdmissionCheck(Truth.TRUE, None) if valid else AdmissionCheck(Truth.FALSE, Reason.MISSING)

    # @anchor:wa.s4.mw_profiles._ProfileCapabilities.changed
    def changed(self, predicate, old, observation):
        if self.observations is None or not all(_yes(self.observations.validate(v)) for v in (old, observation)):
            return AdmissionCheck(Truth.FALSE, Reason.MISSING)
        return self._invoke('changed', predicate, old, observation)


# @anchor:wa.s4.mw_profiles._GuardedDownloadRead
class _GuardedDownloadRead:
    # @anchor:wa.s4.mw_profiles._GuardedDownloadRead.__init__
    def __init__(self, record, native):
        self.record, self.native = record, native
        self.policy = replace(native.policy, rows=min(256, native.policy.rows))

    # @anchor:wa.s4.mw_profiles._GuardedDownloadRead.read
    def read(self, operation, payload, deadline):
        r = self.record
        _need(operation == 'downloads' and type(payload) is dict and not payload, 'downloads_operation')
        _need(_yes(r._export(invocation=r.invocation, scope=r.scope, authority=r.authority,
            deadline_ns=deadline, operation=operation, destination=r.profile.export_dir)), 'downloads_admission')
        _need(_yes(r.resources.reserve('download_inventory', 64 * 262144)), 'downloads_reservation')
        rows = self.native.read('downloads', {}, deadline)
        _need(type(rows) is list and len(rows) <= 256, 'downloads_rows')
        _need(len(gate.canon(rows).encode('utf-8')) <= 262144, 'downloads_bytes')
        return rows


# @anchor:wa.s4.mw_profiles._ProfileObservations
class _ProfileObservations:
    """Run-local OCR publications; semantic detectors are explicit trusted seams.

    handlers[id] = (output_kind, unit, detector(description)); downloads[id] =
    (unit, finished_gate). Neither table is populated from workflow/profile text.
    Capture/OCR alone cannot establish a boolean or terminal workflow goal.
    """
    # @anchor:wa.s4.mw_profiles._ProfileObservations.__init__
    def __init__(self, record):
        self.record = record
        self.ports = self.surface = self.describer = None
        self.handlers, self.downloads, self.download_owners = {}, {}, {}
        self.registered, self.paths = {}, {}
        self.evidence = {}
        self.sequence, self.closed, self.retained = None, False, 0
        self.expiries = {}
        self.mutex = threading.RLock()
        self.signals = None
        self.predicates = {}

    # @anchor:wa.s4.mw_profiles._ProfileObservations.invalidate
    def invalidate(self):
        if self.signals is not None:
            self.signals.invalidate()
        self.predicates.clear()

    # @anchor:wa.s4.mw_profiles._ProfileObservations.bind
    def bind(self, ports, describer):
        _need(self.ports is None and ports.scope is self.record.scope and
              ports.authority is self.record.authority, 'observation_binding')
        self.ports, self.describer = ports, describer

    # @anchor:wa.s4.mw_profiles._ProfileObservations._path
    def _path(self):
        fd, name = tempfile.mkstemp(prefix='observation-', dir=self.record.workspace)
        try:
            info = os.fstat(fd)
            self.paths[Path(name)] = (info.st_dev, info.st_ino)
        finally:
            os.close(fd)
        return Path(name)

    # @anchor:wa.s4.mw_profiles._ProfileObservations.acquire
    def acquire(self, predicate, deadline):
        with self.mutex:
            r, p = self.record, self.ports
            _need(not self.closed and r.active and p is not None and type(predicate) is dict and
                  type(deadline) is int and p.clock() < deadline <= p.limits.deadline_ns, 'observation_active')
            _need(len(self.registered) < min(p.limits.acquisitions, p.native_read.policy.publications),
                  'observation_publications')
            capability = predicate.get('capability_id')
            _need(type(capability) is str and 0 < len(capability.encode()) <= p.native_read.policy.string_bytes,
                  'observation_capability')
            if capability in self.downloads:
                unit, finished_gate = self.downloads[capability]
                _need(callable(finished_gate), 'downloads_completion')
                if capability not in self.download_owners:
                    guarded = replace(p, native_read=_GuardedDownloadRead(r, p.native_read))
                    self.download_owners[capability] = DownloadsObservations(r.invocation, ports=guarded,
                        capability_id=capability, revision=r.config.capability_revision,
                        unit=unit, finished_gate=finished_gate)
                value = replace(self.download_owners[capability].acquire(predicate, deadline),
                    publication='profile:' + r.run_id + ':' + str(len(self.registered)))
                self.registered[value.publication] = value
                self.expiries[value.publication] = deadline
                return value
            signal = None
            if self.signals is not None and capability in _SIGNAL_IDS:
                _need(_yes(p.resources.reserve('signal_metadata',
                      8 * r.config.evidence_policy.metadata_bytes + 1024)), 'signal_reservation')
                signal = parse_signal(predicate, metadata_bytes=r.config.evidence_policy.metadata_bytes,
                                      string_bytes=p.native_read.policy.string_bytes)
                _need(len(gate.canon(predicate).encode()) <= r.config.evidence_policy.metadata_bytes,
                      'signal_identity_budget')
                handler = ('boolean', 'truth', lambda d: None)
            else:
                handler = self.handlers.get(capability)
            _need(type(handler) is tuple and len(handler) == 3 and callable(handler[2]) and
                  handler[0] in ('boolean', 'integer', 'set') and type(handler[1]) is str,
                  'observation_handler')
            amount = 2 * r.config.locator_policy.image_bytes + r.config.evidence_policy.metadata_bytes + 1024
            _need(self.retained + amount <= p.limits.retained_bytes and
                  _yes(p.resources.reserve('profile_observation', amount +
                      r.config.evidence_policy.decode_limits.memory_bytes)), 'observation_reservation')
            self.retained += amount
            start, end, payload, complete, reason = p.clock(), p.clock(), None, False, Reason.MISSING
            mark_cap, missing = None, None
            try:
                _need(self.surface is not None and self.describer is not None and
                      self.surface.window == r.invocation.window, 'observation_surface')
                frame = self.surface.frame()
                end = p.clock()
                _need(type(frame) is dict and type(frame.get('seq')) is int and frame['seq'] >= 0 and
                      (self.sequence is None or frame['seq'] > self.sequence), 'observation_sequence')
                self.sequence = frame['seq']
                window = frame.get('window')
                _need(type(window) is dict and window.get('id') == r.invocation.window and
                      all(type(frame.get(k)) is int and frame[k] > 0 for k in ('width', 'height')) and
                      (window.get('w'), window.get('h')) == (frame['width'], frame['height']), 'observation_dimensions')
                _need(start <= end <= p.clock() < deadline, 'observation_interval')
                raw, identity = _asset(Path(frame['path']), r.workspace, r.config.locator_policy.image_bytes)
                pinned, marks = self._path(), self._path()
                rgb = _png(raw, r.config.evidence_policy.decode_limits, deadline_ns=deadline)
                _need((rgb.shape[1], rgb.shape[0]) == (frame['width'], frame['height']),
                      'observation_full_dimensions')
                full_sha256 = hashlib.sha256(rgb.tobytes()).hexdigest()
                describe_rgb, describe_raw = rgb, raw
                x0 = y0 = 0
                if signal is not None and signal.region_px is not None:
                    x0, y0, x1, y1 = signal.region_px
                    _need(0 <= x0 < x1 <= frame['width'] and 0 <= y0 < y1 <= frame['height'],
                          'observation_region')
                    describe_rgb = rgb[y0:y1, x0:x1]
                    from PIL import Image
                    with io.BytesIO() as encoded:
                        Image.fromarray(describe_rgb).save(encoded, format='PNG')
                        _need(encoded.tell() <= r.config.locator_policy.image_bytes, 'observation_crop_bytes')
                        describe_raw = encoded.getvalue()
                pinned.write_bytes(describe_raw)
                d = self.describer.describe(pinned, marks_out=marks, ocr_marks=True, deadline_ns=deadline)
                if type(d) is DescribeResult:
                    mark_cap = d.mark_cap_reached if type(d.mark_cap_reached) is bool else None
                    if type(d.marks) is tuple and len(d.marks) <= 64:
                        missing = any(type(mark) is not Mark or mark.missing_reason is not None for mark in d.marks)
                _need(type(d) is DescribeResult and
                      (type(d.mark_cap_reached) is bool if signal is not None else d.mark_cap_reached is False) and
                      type(d.marks) is tuple and
                      (len(d.marks) > 0 or (signal is not None and
                          (signal.k is not None or signal.region_px is not None))) and
                      len(d.marks) <= min(64, r.config.locator_policy.max_marks) and
                      (d.width, d.height) == (describe_rgb.shape[1], describe_rgb.shape[0]) and d.marks_png == str(marks),
                      'observation_ocr')
                ids = set()
                for mark in d.marks:
                    _need(type(mark) is Mark and mark.missing_reason is None and
                          type(mark.ocr_text) is str and type(mark.ocr_score_promille) is int and
                          0 <= mark.ocr_score_promille <= 1000 and
                          all(type(v) is int for v in (mark.id, mark.x, mark.y, mark.w, mark.h)) and
                          mark.id >= 0 and mark.id not in ids and mark.x >= 0 and mark.y >= 0 and
                          mark.w > 0 and mark.h > 0 and mark.x + mark.w <= d.width and mark.y + mark.h <= d.height,
                          'observation_missingness')
                    ids.add(mark.id)
                _need(sum(len(mark.ocr_text.encode()) + len(mark.kind.encode()) + 256 for mark in d.marks)
                      <= r.config.evidence_policy.metadata_bytes, 'observation_metadata')
                _need(hashlib.sha256(describe_rgb.tobytes()).hexdigest() == d.frame_sha256 and
                      _asset(pinned, r.workspace, r.config.locator_policy.image_bytes)[0] == describe_raw,
                      'observation_rgb')
                _asset(marks, r.workspace, r.config.locator_policy.image_bytes)
                if signal is not None:
                    projection = SignalFrame(frame['seq'], start, end, frame['width'], frame['height'],
                                             full_sha256, tuple(replace(mark, x=mark.x + x0, y=mark.y + y0)
                                                                for mark in d.marks),
                                             not d.mark_cap_reached)
                    payload = self.signals.evaluate(signal, projection, now_ns=p.clock(), deadline_ns=deadline)
                else:
                    payload = handler[2](d)
                kind = handler[0]
                _need((kind == 'boolean' and type(payload) is bool) or
                      (kind == 'integer' and type(payload) is int and payload.bit_length() <= 64) or
                      (kind == 'set' and type(payload) is frozenset and len(payload) <= 256 and
                       all(type(v) is str and len(v.encode()) <= p.native_read.policy.string_bytes for v in payload)),
                      'observation_payload')
                _need(p.clock() < deadline, 'observation_expiry')
                complete, reason = True, None
            except Exception:
                if self.signals is not None:
                    self.signals.invalidate()
                payload = None
            publication = 'profile:' + r.run_id + ':' + str(len(self.registered))
            value = ObservationValue(r.scope, r.authority, publication, start, end, capability,
                r.config.capability_revision, handler[0], handler[1], payload, complete, reason)
            self.registered[publication], self.expiries[publication] = value, deadline
            self.evidence[publication] = (mark_cap, missing, complete)
            if signal is not None:
                self.predicates[publication] = gate.canon(predicate)
            return value

    # @anchor:wa.s4.mw_profiles._ProfileObservations.validate
    def validate(self, value):
        with self.mutex:
            r, p = self.record, self.ports
            valid = (not self.closed and r.active and p is not None and type(value) is ObservationValue and
                     self.registered.get(value.publication) is value and value.scope is r.scope and
                     value.authority is r.authority and value.capability_revision == r.config.capability_revision and
                     value.capability_id in (self.handlers.keys() | self.downloads.keys() |
                         (_SIGNAL_IDS if self.signals is not None else frozenset())) and
                     value.complete is True and value.missing_reason is None and
                     type(value.capture_start_ns) is int and type(value.capture_end_ns) is int and
                     value.capture_start_ns <= value.capture_end_ns <= p.clock() <
                     min(self.expiries.get(value.publication, 0), p.limits.deadline_ns))
            return AdmissionCheck(Truth.TRUE, None) if valid else AdmissionCheck(Truth.FALSE, Reason.STALE)

    # @anchor:wa.s4.mw_profiles._ProfileObservations.drain
    def drain(self):
        with self.mutex:
            try:
                for path, identity in tuple(self.paths.items()):
                    info = path.lstat()
                    _need((info.st_dev, info.st_ino) == identity and stat.S_ISREG(info.st_mode), 'observation_cleanup')
                    path.unlink()
                    del self.paths[path]
                self.invalidate()
                self.registered.clear()
                self.expiries.clear()
                self.evidence.clear()
                self.download_owners.clear()
                self.closed = True
                return AdmissionCheck(Truth.TRUE, None)
            except Exception:
                return AdmissionCheck(Truth.FALSE, Reason.STALE)


# @anchor:wa.s4.mw_profiles._DefaultHost
class _DefaultHost:
    """Durable metadata and cross-process custody, independent of ActorLock.

    No CLI capability flag constitutes semantic or disclosure evidence.
    """
    # @anchor:wa.s4.mw_profiles._DefaultHost.__init__
    def __init__(self, profile, invocation):
        self.profile, self.invocation = profile, invocation
        self.fd = None
        self.run_id = None
        self.started = False

    # @anchor:wa.s4.mw_profiles._DefaultHost.acquire
    def acquire(self, run_id):
        _validate_token(self.profile, self.invocation)
        _need(all(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,255}', value)
                  for value in (self.invocation.container, self.invocation.window)), 'lease_name')
        _LEASE_ROOT.mkdir(mode=0o700, exist_ok=True)
        with _guard(_LEASE_ROOT, directory=True) as parent:
            _need(stat.S_IMODE(os.fstat(parent).st_mode) == 0o700, 'lease_directory')
            fd = os.open(self.invocation.container + '.' + self.invocation.window + '.lock',
                os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=parent)
            try:
                meta = os.fstat(fd)
                _need(stat.S_ISREG(meta.st_mode) and meta.st_uid == os.geteuid() and
                      stat.S_IMODE(meta.st_mode) == 0o600, 'lease_file')
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.fd, self.run_id = fd, run_id
            except BaseException:
                os.close(fd)
                raise

    # @anchor:wa.s4.mw_profiles._DefaultHost.release
    def release(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    # @anchor:wa.s4.mw_profiles._DefaultHost.lease
    def lease(self, **kwargs):
        return AdmissionCheck(Truth.TRUE, None) if self.fd is not None else AdmissionCheck(Truth.FALSE, Reason.STALE)

    # @anchor:wa.s4.mw_profiles._DefaultHost.append_notice
    def append_notice(self, phase):
        _need(phase in ('start', 'end') and self.fd is not None, 'notice_phase')
        directory = Path.home()
        for component in ('.local', 'state', 'memoryweb'):
            with _guard(directory, directory=True) as parent:
                try:
                    os.mkdir(component, mode=0o700, dir_fd=parent)
                except FileExistsError:
                    pass
            directory = directory / component
        with _guard(directory, directory=True) as parent:
            _need(stat.S_IMODE(os.fstat(parent).st_mode) == 0o700, 'notice_directory')
            fd = os.open('notices.jsonl', os.O_WRONLY | os.O_APPEND | os.O_CREAT |
                         os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=parent)
            try:
                meta = os.fstat(fd)
                _need(stat.S_ISREG(meta.st_mode) and meta.st_uid == os.geteuid() and
                      stat.S_IMODE(meta.st_mode) == 0o600, 'notice_file')
                fcntl.flock(fd, fcntl.LOCK_EX)
                meta = os.fstat(fd)
                # Detect torn records without reading any private token material.
                if meta.st_size:
                    read_fd = os.open('notices.jsonl', os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent)
                    try:
                        _need(os.fstat(read_fd).st_ino == meta.st_ino and
                              os.pread(read_fd, 1, meta.st_size - 1) == b'\n', 'notice_torn')
                    finally:
                        os.close(read_fd)
                line = gate.canon(dict(run_id=self.run_id, site=self.profile.site,
                                       capsule=self.profile.capsule, phase=phase)) + '\n'
                raw = line.encode()
                _need(len(raw) <= 16384, 'notice_bound')  # [TARGET]
                _need(os.write(fd, raw) == len(raw), 'notice_append')
                os.fsync(fd)
                os.fsync(parent)
                print(line, end='', flush=True)
            finally:
                os.close(fd)

    # @anchor:wa.s4.mw_profiles._DefaultHost.notice
    def notice(self, **kwargs):
        _validate_token(self.profile, self.invocation)
        if not self.started:
            self.append_notice('start')
            self.started = True
        return self.lease()

    # @anchor:wa.s4.mw_profiles._DefaultHost.owners
    def owners(self, **kwargs):
        authorization = _ProfileAuthorization(self.record)
        capabilities = _ProfileCapabilities(authorization, _producer_evidence(kwargs['clock'], record=self.record, contract=_PRODUCTION_PROBE))
        observations = _ProfileObservations(self.record)
        observations.signals = SignalHandlers(max_marks=min(64, self.record.config.locator_policy.max_marks),
                                              metadata_bytes=self.record.config.evidence_policy.metadata_bytes)
        capabilities.observations = observations
        return _HostOwners(authorization, capabilities, observations,
                           gate.ValidationContext(self.invocation.workflow.parent,
                                                  capability_check=capabilities.capability_check,
                                                  evidence_check=capabilities.evidence_check))

    # @anchor:wa.s4.mw_profiles._DefaultHost.export_check
    def export_check(self, **kwargs):
        """Check download-directory custody; never grant image disclosure."""
        try:
            with _guard(self.profile.export_dir, directory=True):
                pass
            return AdmissionCheck(Truth.TRUE, None)
        except Exception:
            return AdmissionCheck(Truth.FALSE, Reason.PRIVACY)

    # @anchor:wa.s4.mw_profiles._DefaultHost.custody_check
    def custody_check(self, **kwargs):
        """Verify RAM custody for the run and any supplied derivative paths."""
        try:
            workspace = _RAM_ROOT / self.run_id
            _workspace_check(workspace)
            for key in ('frame_path', 'marks_path'):
                if key in kwargs:
                    _ram_path(kwargs[key], workspace, self.image_bytes)
            return self.lease()
        except Exception:
            return AdmissionCheck(Truth.FALSE, Reason.PRIVACY)

    # @anchor:wa.s4.mw_profiles._DefaultHost.support_check
    def support_check(self, **kwargs):
        """Deny grounding without independent semantic producer evidence."""
        return AdmissionCheck(Truth.UNKNOWN, Reason.MISSING)

    # @anchor:wa.s4.mw_profiles._DefaultHost.preflight
    def preflight(self, *, ai_locate):
        root = os.environ.get('VQA_VE_ROOT')
        if not root or not Path(root).is_dir():
            raise ProfileError(reason='VQA_VE_ROOT must name an existing directory')
        if not shutil.which('tesseract'):
            raise ProfileError(reason='tesseract executable missing')
        if ai_locate and not shutil.which('muse'):
            raise ProfileError(reason='muse executable missing for AI locate')

    # @anchor:wa.s4.mw_profiles._DefaultHost.quiescent
    def quiescent(self, timeout):
        return AdmissionCheck(Truth.TRUE, None)


# @anchor:wa.s4.mw_profiles._default_capacity_configuration
def _default_capacity_configuration(profile, invocation, *, bundle):
    """Return the corrected conductor vector; admission remains independently checked."""
    return _capacity_targets(profile, invocation)


# @anchor:wa.s4.mw_profiles._capacity_targets
def _capacity_targets(profile, invocation):
    """Wave-29 capacities [TARGET], independently checked before allocation.

    Unspecified subordinate bounds are conservative local targets. The frozen
    aggregate peak gate still includes the separate VE child allowance.
    """
    mib, second = 1024 * 1024, 1_000_000_000
    host = _DefaultHost(profile, invocation)
    host.image_bytes = 3 * mib
    # Ceiling construction has no invocation and never provisions a transport.
    ai_locate = False
    if invocation is not None:
        # Workflow bytes are guarded and bounded; only explicit T2 targets enable
        # the model transport. Later authorization pins and validates these bytes.
        with _guard(invocation.workflow) as fd:
            before = _metadata(os.fstat(fd))
            workflow = gate.strict_load(_read(fd, 65536))
            _need(before == _metadata(os.fstat(fd)), 'capacity_workflow')
        _need(type(workflow) is dict, 'capacity_workflow')
        steps = workflow.get('steps', [])
        _need(type(steps) is list and all(type(step) is dict for step in steps), 'capacity_workflow')
        ai_locate = any(type(target) is dict and target.get('rung') == 'T2'
                        for step in steps
                        for target in (step.get('target'), *step.get('fallback_targets', [])))
    return _HostConfiguration(
        limits=WorkflowLimits(workflow_bytes=65536, steps=256, snapshots=64,
            acquisitions=1024, retained_bytes=64 * mib, peak_bytes=2048 * mib,
            event_bytes=16384, event_count=4096, journal_bytes=64 * mib,
            slot_bytes=256 * 1024, aggregate_slot_bytes=mib, cancel_ns=10 * second,
            drain_ns=10 * second, no_progress_ns=420 * second, deadline_ns=1800 * second),
        native_policy=NativeReadPolicy(stdout_bytes=mib, stderr_bytes=65536,
            rows=256, string_bytes=256 * 1024, processes=1, timeout_ns=60 * second,
            join_ns=10 * second, publications=1024),
        locator_policy=LocatePolicy(max_calls=8, max_marks=64, semantic_bytes=16384,
            request_bytes=256 * 1024, response_bytes=65536, stderr_bytes=65536,
            image_bytes=3 * mib, peak_bytes=128 * mib, retained_bytes=6 * mib + 65536,
            call_ns=30 * second, poll_ns=100_000_000, cancel_ns=10 * second,
            drain_ns=10 * second, absolute_deadline_ns=1800 * second),
        evidence_policy=LiveEvidencePolicy(decode_limits=DemoLimits(events=256,
            event_line=16384, event_file=4 * mib, png_bytes=3 * mib, axis=4096,
            pixels=8_000_000, retained_bytes=64 * mib, memory_bytes=128 * mib,
            candidates=10000, comparisons=256_000_000, acquisitions=64,
            operation_ns=60 * second), ve_stdout_bytes=mib, ve_stderr_bytes=65536,
            ve_process_bytes=1024 * mib, metadata_bytes=65536, ocr_tasks_max=32,
            ocr_control_ns=10 * second),
        resource_capacity=2048 * mib, executable=shutil.which('muse') or '',
        capability_revision='srf-identity-surface', supported_locales=('en',),
        destination=object(), owners=host.owners, notice=host.notice, lease=host.lease,
        export_check=host.export_check, custody_check=host.custody_check, support_check=host.support_check,
        cancel=host.quiescent, drain=host.quiescent, ai_locate=ai_locate)


# @anchor:wa.s4.mw_profiles._trusted_default_configuration
def _trusted_default_configuration(profile, invocation, *, bundle, start_ns):
    _need(type(start_ns) is int and start_ns > 0, 'host_clock')
    config = _default_capacity_configuration(profile, invocation, bundle=bundle)
    _need(type(config) is _HostConfiguration, 'host_configuration')
    durations = _HostDurations()
    host = getattr(config.support_check, '__self__', None)
    if type(host) is _DefaultHost:
        host.preflight(ai_locate=config.ai_locate)
    _need(type(config.ai_locate) is bool, 'host_ai_locate')
    executable = shutil.which('muse') if config.ai_locate else ''
    _need(not config.ai_locate or (type(executable) is str and executable.startswith('/')), 'host_executable')
    evidence = config.evidence_policy
    if any(f.name == 'ocr_control_ns' for f in fields(evidence)):
        evidence = replace(evidence, ocr_control_ns=durations.ocr_control_ns)
    return replace(config, durations=durations, executable=executable,
        limits=replace(config.limits, deadline_ns=start_ns + durations.run_ns,
                       drain_ns=durations.drain_ns),
        native_policy=replace(config.native_policy, join_ns=durations.drain_ns),
        locator_policy=replace(config.locator_policy, max_calls=durations.locate_calls,
            call_ns=durations.locate_call_ns, drain_ns=durations.drain_ns,
            absolute_deadline_ns=start_ns + durations.run_ns), evidence_policy=evidence)


# @anchor:wa.s4.mw_profiles._HostConfiguration
@dataclass(frozen=True, repr=False)
class _HostConfiguration:
    limits: WorkflowLimits
    native_policy: NativeReadPolicy
    locator_policy: LocatePolicy
    evidence_policy: LiveEvidencePolicy
    resource_capacity: int
    executable: str
    capability_revision: str
    supported_locales: tuple
    destination: object
    owners: object
    notice: object
    lease: object
    export_check: object
    custody_check: object
    support_check: object
    cancel: object
    drain: object
    durations: _HostDurations = _HostDurations()
    ai_locate: bool = True


# @anchor:wa.s4.mw_profiles._DisabledLocateTransport
class _DisabledLocateTransport:
    """Trusted configuration disables provider dispatch independently of argv."""

    # @anchor:wa.s4.mw_profiles._DisabledLocateTransport.generate
    def generate(self, payload, evidence, policy, deadline, clock):
        raise ProfileError(reason='AI locate disabled')

    # @anchor:wa.s4.mw_profiles._DisabledLocateTransport.cancel_and_drain
    def cancel_and_drain(self, policy, clock):
        return AdmissionCheck(Truth.TRUE, None)


# @anchor:wa.s4.mw_profiles._HostOwners
@dataclass(frozen=True, repr=False)
class _HostOwners:
    """Fresh evidence-backed backends supplied by trusted host code only.

    Production implementations of these backends are AUSENTE. The factory must
    not manufacture execution observations, effect grants or final-oracle evidence.
    Replay, private slots and operator-guidance journal events are explicitly
    unsupported in this composition.
    """
    authorization: object
    capabilities: object
    observations: object
    validation_context: object


# @anchor:wa.s4.mw_profiles._capacity_ceilings
def _capacity_ceilings():
    """Keep approved image maxima separate from the jointly admissible defaults."""
    reference = _capacity_targets(None, None)
    image_bytes = 8 * 1024 * 1024
    return replace(reference,
        locator_policy=replace(reference.locator_policy, image_bytes=image_bytes,
            retained_bytes=2 * image_bytes + reference.evidence_policy.metadata_bytes),
        evidence_policy=replace(reference.evidence_policy,
            decode_limits=replace(reference.evidence_policy.decode_limits, png_bytes=image_bytes)))


# @anchor:wa.s4.mw_profiles._capacity_bounds
def _capacity_bounds(config, now):
    reference = _capacity_ceilings()
    for key in ('limits', 'native_policy', 'locator_policy', 'evidence_policy'):
        obj, cap = getattr(config, key), getattr(reference, key)
        for field in fields(cap):
            if field.name == 'decode_limits':
                for nested in fields(cap.decode_limits):
                    _need(getattr(obj.decode_limits, nested.name) <= getattr(cap.decode_limits, nested.name),
                          'capacity_' + nested.name)
            elif field.name in ('deadline_ns', 'absolute_deadline_ns'):
                _need(getattr(obj, field.name) - now <= reference.durations.run_ns,
                      'capacity_' + field.name)
            else:
                _need(getattr(obj, field.name) <= getattr(cap, field.name), 'capacity_' + field.name)
    for field in fields(reference.durations):
        _need(getattr(config.durations, field.name) <= getattr(reference.durations, field.name),
              'capacity_duration_' + field.name)
    _need(config.resource_capacity <= reference.resource_capacity, 'capacity_resources')


# @anchor:wa.s4.mw_profiles._host_policy
def _host_policy(config, now):
    _need(type(config) is _HostConfiguration, 'host_configuration')
    w, n, p, c = config.limits, config.native_policy, config.locator_policy, config.evidence_policy
    _need(type(w) is WorkflowLimits and type(n) is NativeReadPolicy and
          type(p) is LocatePolicy and type(c) is LiveEvidencePolicy and type(c.decode_limits) is DemoLimits, 'host_policy_types')
    # Validate all nested fields together; no independent defaults or bools.
    _need(all(type(getattr(obj, f.name)) is int and getattr(obj, f.name) > 0
              for obj in (w, n, p, c.decode_limits) for f in fields(obj)) and
          all(type(getattr(c, f.name)) is int and getattr(c, f.name) > 0
              for f in fields(c) if f.name != 'decode_limits') and
          type(config.resource_capacity) is int and config.resource_capacity > 0 and
          type(config.durations) is _HostDurations and
          all(type(getattr(config.durations, f.name)) is int and getattr(config.durations, f.name) > 0
              for f in fields(config.durations)), 'host_policy_integers')
    _need(type(now) is int and now > 0, 'host_clock')
    _capacity_bounds(config, now)
    _need(w.workflow_bytes <= gate.LIMITS["workflow"] and p.poll_ns <= p.call_ns and p.retained_bytes <= p.peak_bytes and
          2 * p.image_bytes + p.request_bytes + 3 * p.response_bytes + p.stderr_bytes + p.retained_bytes <= p.peak_bytes and
          2 * p.image_bytes + c.metadata_bytes <= p.retained_bytes and
          p.max_calls * p.retained_bytes <= w.retained_bytes and
          p.peak_bytes + 8 * p.image_bytes + c.decode_limits.memory_bytes + c.ve_process_bytes +
          c.ve_stdout_bytes + c.ve_stderr_bytes + c.metadata_bytes + (p.max_calls - 1) * p.retained_bytes <= w.peak_bytes and
          c.decode_limits.png_bytes <= p.image_bytes and p.max_calls <= min(w.acquisitions, n.publications) and
          w.peak_bytes <= config.resource_capacity, 'host_policy_bounds')
    _need(type(now) is int and now > 0, 'host_clock')
    remaining = w.deadline_ns - now
    _need(type(now) is int and 0 < now < p.absolute_deadline_ns <= w.deadline_ns and
          all(value <= remaining for value in (p.call_ns, p.cancel_ns, p.drain_ns, n.timeout_ns,
              n.join_ns, w.cancel_ns, w.drain_ns, w.no_progress_ns, c.decode_limits.operation_ns,
              *([c.ocr_control_ns] if hasattr(c, 'ocr_control_ns') else []))), 'host_policy_deadline')
    _need(type(config.ai_locate) is bool, 'host_ai_locate')
    _need(type(config.executable) is str and
          (not config.ai_locate or config.executable.startswith('/')) and
          type(config.capability_revision) is str and bool(config.capability_revision) and
          type(config.supported_locales) is tuple and bool(config.supported_locales) and
          all(type(x) is str and bool(x) for x in config.supported_locales) and
          all(callable(getattr(config, k)) for k in ('owners', 'notice', 'lease', 'export_check',
              'custody_check', 'support_check', 'cancel', 'drain')), 'host_callbacks')


# @anchor:wa.s4.mw_profiles._Artifact
class _Artifact:
    # @anchor:wa.s4.mw_profiles._Artifact.__init__
    def __init__(self, invocation, limits):
        self._invocation, self._limits, self._pins = invocation, limits, {}

    # @anchor:wa.s4.mw_profiles._Artifact.pin
    @contextmanager
    def pin(self, path):
        _need(path == self._invocation.workflow, 'artifact_binding')
        with ExitStack() as stack:
            # Guard construction before yielding, then retain a duplicate inode
            # descriptor. Staleness after handoff is returned by check().
            with _guard(path) as guarded:
                fd = os.dup(guarded)
            stack.callback(os.close, fd)
            raw = _read(fd, self._limits.workflow_bytes)
            value = gate.strict_load(raw)
            _need(raw == (gate.canon(value) + '\n').encode(), 'artifact_canonical')
            pin = PinnedArtifact(path, hashlib.sha256(raw).hexdigest(), AdmissionCheck(Truth.TRUE, None))
            self._pins[id(pin)] = (pin, fd, _metadata(os.fstat(fd)))
            try:
                yield pin
            finally:
                self._pins.pop(id(pin), None)

    # @anchor:wa.s4.mw_profiles._Artifact.check
    def check(self, pin):
        try:
            row = self._pins.get(id(pin))
            _need(row is not None and row[0] is pin, 'artifact_registry')
            _, fd, before = row
            with _guard(pin.path) as current:
                _need(_metadata(os.fstat(current)) == before, 'artifact_mutation')
            os.lseek(fd, 0, os.SEEK_SET)
            raw = _read(fd, self._limits.workflow_bytes)
            _need(_metadata(os.fstat(fd)) == before and
                  _metadata(os.stat(pin.path, follow_symlinks=False)) == before and
                  hashlib.sha256(raw).hexdigest() == pin.sha256, 'artifact_mutation')
            return AdmissionCheck(Truth.TRUE, None)
        except Exception:
            return AdmissionCheck(Truth.FALSE, Reason.STALE)


# @anchor:wa.s4.mw_profiles._Resources
class _Resources:
    # @anchor:wa.s4.mw_profiles._Resources.__init__
    def __init__(self, config):
        self._config, self._used, self._cancelled = config, 0, False
        self._lock, self._joins = threading.RLock(), []

    # @anchor:wa.s4.mw_profiles._Resources.reserve
    def reserve(self, kind, amount):
        with self._lock:
            # Frozen runner peak reservations describe the already validated
            # aggregate working-set ceiling, including retained evidence. They
            # are admission checks, not allocations retained on every poll.
            peaks = ('acquisition_peak', 'target_peak', 'verifier_peak', 'terminal_evidence')
            if type(kind) is str and kind in peaks:
                valid = (not self._cancelled and type(amount) is int and
                         0 < amount <= self._config.limits.peak_bytes <= self._config.resource_capacity)
                return AdmissionCheck(Truth.TRUE, None) if valid else AdmissionCheck(Truth.FALSE, Reason.CAPACITY)
            if (self._cancelled or type(kind) is not str or not kind or type(amount) is not int or
                    amount <= 0 or self._used + amount > self._config.resource_capacity):
                return AdmissionCheck(Truth.FALSE, Reason.CAPACITY)
            self._used += amount
            return AdmissionCheck(Truth.TRUE, None)

    # @anchor:wa.s4.mw_profiles._Resources.cancel
    def cancel(self, timeout):
        with self._lock:
            self._cancelled = True
            if type(timeout) is not int or not 0 < timeout <= self._config.limits.cancel_ns:
                return AdmissionCheck(Truth.FALSE, Reason.DEADLINE)
            try:
                return self._config.cancel(timeout)
            except Exception:
                return AdmissionCheck(Truth.FALSE, Reason.UNKNOWN)

    # @anchor:wa.s4.mw_profiles._Resources.drain
    def drain(self, timeout):
        with self._lock:
            try:
                _need(self._cancelled and type(timeout) is int and 0 < timeout <= self._config.limits.drain_ns,
                      'resource_drain_binding')
                _need(all(_yes(join()) for join in self._joins) and _yes(self._config.drain(timeout)),
                      'resource_join')
                self._used = 0
                return AdmissionCheck(Truth.TRUE, None)
            except Exception:
                return AdmissionCheck(Truth.FALSE, Reason.RECONCILIATION)


# @anchor:wa.s4.mw_profiles._Journal
class _Journal:
    """Private durable metadata; no pixels, input text or public receipts.

    Digest framing is SHA256(domain || tagged_nullable_predecessor || uint64be(length)
    || canonical_event_bytes), domain b"MW_PROFILE_JOURNAL_V1\0". A per-store
    random key MAC authenticates that digest; hashing alone is not authority.
    LF commits records. Store locking spans the entire owner lifetime. Output
    selection uses same-directory replace after fsync, leaving the old receipt
    selected on any admission or pre-replace I/O failure.
    """
    # @anchor:wa.s4.mw_profiles._Journal.__init__
    def __init__(self, record, bundle):
        self._record, self._bundle = record, bundle
        self._root = record.invocation.output.parent
        self._store = self._root / (record.invocation.output.name + '.private-journal')
        self._fd, self._key, self._staged = None, None, {}
        self._credit_bytes = self._credit_events = 0
        self._lock = threading.RLock()

    # @anchor:wa.s4.mw_profiles._Journal._open
    def _open(self):
        if self._fd is not None:
            return
        _need(self._root.is_absolute() and not any(p.is_symlink() for p in
              (self._root, *self._root.parents)), 'journal_root')
        meta = self._root.stat()
        _need(meta.st_uid == os.geteuid() and stat.S_IMODE(meta.st_mode) == 0o700,
              'journal_root')
        self._store.mkdir(mode=0o700, exist_ok=True)
        meta = self._store.lstat()
        _need(not self._store.is_symlink() and stat.S_ISDIR(meta.st_mode) and
              meta.st_uid == os.geteuid() and stat.S_IMODE(meta.st_mode) == 0o700, 'journal_root')
        fd = os.open(self._store / 'owner.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            keyfile = self._store / 'auth.key'
            if not keyfile.exists():
                self._write(keyfile, os.urandom(32))
            with _guard(keyfile) as keyfd:
                key = _read(keyfd, 32)
            _need(len(key) == 32, 'journal_key')
            self._fd, self._key = fd, key
        except Exception:
            os.close(fd)
            raise

    # @anchor:wa.s4.mw_profiles._Journal._write
    def _write(self, path, raw):
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(fd, 'wb', closefd=False) as stream:
                _need(stream.write(raw) == len(raw), 'journal_write')
                stream.flush()
                os.fsync(fd)
        finally:
            os.close(fd)
        self._sync(path.parent)

    # @anchor:wa.s4.mw_profiles._Journal._sync
    def _sync(self, parent):
        fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    # @anchor:wa.s4.mw_profiles._Journal.read
    def read(self, budget):
        with self._lock:
            self._open()
            _need(type(budget) is int and 0 < budget <= self._record.config.limits.journal_bytes,
                  'journal_budget')
            path = self._store / 'events.jsonl'
            if not path.exists():
                return b''
            with _guard(path) as fd:
                return _read(fd, budget)

    # @anchor:wa.s4.mw_profiles._Journal.digest
    def digest(self, raw, prev):
        _need(type(raw) is bytes and (prev is None or type(prev) is str and
              re.fullmatch('[0-9a-f]{64}', prev) is not None), 'journal_framing')
        return hashlib.sha256(b'MW_PROFILE_JOURNAL_V1\0' + (b'\0' if prev is None else b'\1' + bytes.fromhex(prev)) +
                              len(raw).to_bytes(8, 'big') + raw).hexdigest()

    # @anchor:wa.s4.mw_profiles._Journal._chain
    def _chain(self, raw):
        _need(not raw or raw.endswith(b'\n'), 'journal_lf')
        prev, events = None, []
        for line in raw.splitlines(keepends=True):
            event = gate.strict_load(line)
            _need(type(event) is dict and line == (gate.canon(event) + '\n').encode() and
                  len(line) <= self._record.config.limits.event_bytes and
                  len(events) < self._record.config.limits.event_count, 'journal_event')
            _need(type(event.get('seq')) is int and event['seq'] == len(events) + 1 and
                  event.get('prev') == prev and _yes(self.authenticate(event)), 'journal_chain')
            prev = event['digest']
            events.append(event)
        return events

    # @anchor:wa.s4.mw_profiles._Journal.authenticate
    def authenticate(self, event):
        try:
            self._open()
            payload = {k: v for k, v in event.items() if k != 'digest'}
            expected = self.digest(gate.canon(payload).encode(), event['prev'])
            _need(hmac.compare_digest(expected, event['digest']), 'journal_digest')
            macfile = self._store / (expected + '.mac')
            with _guard(macfile) as fd:
                mac = _read(fd, 64)
            _need(hmac.compare_digest(mac, hmac.digest(self._key, expected.encode(), 'sha256').hex().encode()),
                  'journal_authentication')
            return AdmissionCheck(Truth.TRUE, None)
        except Exception:
            return AdmissionCheck(Truth.FALSE, Reason.STALE)

    # @anchor:wa.s4.mw_profiles._Journal.reserve
    def reserve(self, amount, events):
        try:
            with self._lock:
                limits = self._record.config.limits
                raw = self.read(limits.journal_bytes)
                chain = self._chain(raw)
                _need(type(amount) is int and type(events) is int and amount > 0 and events > 0 and
                      len(raw) + self._credit_bytes + amount <= limits.journal_bytes and
                      len(chain) + self._credit_events + events <= limits.event_count, 'journal_capacity')
                self._credit_bytes += amount
                self._credit_events += events
                return AdmissionCheck(Truth.TRUE, None)
        except Exception:
            return AdmissionCheck(Truth.FALSE, Reason.CAPACITY)

    # @anchor:wa.s4.mw_profiles._Journal.append
    def append(self, raw):
        try:
            with self._lock:
                _need(type(raw) is bytes and raw.endswith(b'\n') and raw.count(b'\n') == 1 and
                      len(raw) <= min(self._credit_bytes, self._record.config.limits.event_bytes) and
                      self._credit_events > 0, 'journal_append')
                prefix = self.read(self._record.config.limits.journal_bytes)
                chain = self._chain(prefix)
                event = gate.strict_load(raw)
                payload = {k: v for k, v in event.items() if k != 'digest'}
                expected = self.digest(gate.canon(payload).encode(), event['prev'])
                _need(raw == (gate.canon(event) + '\n').encode() and event['digest'] == expected and
                      type(event['seq']) is int and event['seq'] == len(chain) + 1 and
                      event['prev'] == (chain[-1]['digest'] if chain else None), 'journal_chain')
                # Only closed metadata fields are durable; no arbitrary strings.
                _need(set(event) == {'seq', 'prev', 'kind', 'step_seq', 'phase', 'digest'} and
                      event['kind'] in ('intent', 'complete', 'verified', 'satisfied_skip', 'condition_skip', 'failed') and
                      type(event['step_seq']) is int and event['step_seq'] > 0 and
                      event['phase'] == ('delivery' if event['kind'] in ('intent', 'complete') else 'terminal'),
                      'journal_privacy')
                macfile = self._store / (expected + '.mac')
                if not macfile.exists():
                    self._write(macfile, hmac.digest(self._key, expected.encode(), 'sha256').hex().encode())
                candidate = self._store / ('events-' + uuid.uuid4().hex)
                self._write(candidate, prefix + raw)
                os.replace(candidate, self._store / 'events.jsonl')
                self._sync(self._store)
                self._credit_bytes -= len(raw)
                self._credit_events -= 1
                return AdmissionCheck(Truth.TRUE, None)
        except Exception:
            return AdmissionCheck(Truth.FALSE, Reason.RECONCILIATION)

    # @anchor:wa.s4.mw_profiles._Journal.recover
    def recover(self, prefix, suffix):
        try:
            with self._lock:
                raw = self.read(self._record.config.limits.journal_bytes)
                _need(type(prefix) is bytes and type(suffix) is bytes and suffix and
                      not suffix.endswith(b'\n') and raw == prefix + suffix, 'journal_recovery')
                self._chain(prefix)
                generation = uuid.uuid4().hex
                self._write(self._store / ('torn-' + generation), suffix)
                # Preserve the complete original as well as its torn suffix.
                self._write(self._store / ('before-recovery-' + generation), raw)
                candidate = self._store / ('recovered-' + generation)
                self._write(candidate, prefix)
                self._write(self._store / ('recovery-' + generation + '.json'),
                    (gate.canon(dict(prefix_sha256=hashlib.sha256(prefix).hexdigest(),
                                     suffix_sha256=hashlib.sha256(suffix).hexdigest())) + '\n').encode())
                os.replace(candidate, self._store / 'events.jsonl')
                self._sync(self._store)
                return AdmissionCheck(Truth.TRUE, None)
        except Exception:
            return AdmissionCheck(Truth.FALSE, Reason.RECONCILIATION)

    # @anchor:wa.s4.mw_profiles._Journal.stage
    def stage(self, summary, inventory, output):
        with self._lock:
            record = self._record
            _need(output == record.invocation.output, 'journal_output')
            events = self._chain(self.read(record.config.limits.journal_bytes))
            with _guard(record.invocation.workflow) as fd:
                raw = _read(fd, record.config.limits.workflow_bytes)
            workflow = gate.strict_load(raw)
            disposition = {step['seq']: 'not_reached' for step in workflow['steps']}
            outstanding = set()
            for event in events:
                seq, kind = event['step_seq'], event['kind']
                _need(seq in disposition, 'journal_inventory')
                if kind == 'intent':
                    _need(seq not in outstanding, 'journal_inventory')
                    outstanding.add(seq)
                elif kind == 'complete':
                    _need(seq in outstanding, 'journal_inventory')
                    outstanding.remove(seq)
                elif kind in ('verified', 'satisfied_skip', 'condition_skip', 'failed'):
                    disposition[seq] = kind
            expected = dict(workflow_sha256=hashlib.sha256(raw).hexdigest(),
                steps=[dict(seq=s['seq'], disposition=disposition[s['seq']],
                            predicate_pass=disposition[s['seq']] in ('verified', 'satisfied_skip'),
                            optional=not s['protected'], guard_false=disposition[s['seq']] == 'condition_skip')
                       for s in workflow['steps']],
                required_segments={k: list(v) for k, v in record.owners.authorization.inventory(workflow).items()},
                final_task_pass=summary.get('rc') == 0)
            _need(not outstanding and inventory == expected and summary.get('visibility') == 'local' and
                  summary.get('run_id') == record.run_id, 'journal_inventory')
            _need(_yes(record.owners.authorization.check('commit', workflow, record.scope, record.authority)),
                  'journal_authority')
            # The trusted evidence callback must attest the final task oracle.
            # Event hashes and caller-provided summaries alone cannot do so.
            ctx = record.owners.validation_context
            proof = ctx.evidence_check('run_inventory', copy.deepcopy(summary), (copy.deepcopy(expected),))
            _need(type(proof) is gate.GateResult and proof.rc == 0 and proof.category == 'PASS',
                  'journal_evidence')
            raw_summary = (gate.canon(summary) + '\n').encode()
            _need(len(raw_summary) <= record.config.limits.journal_bytes, 'journal_budget')
            staged = self._root / ('.mw-inactive-' + uuid.uuid4().hex + '.json')
            self._write(staged, raw_summary)
            self._staged[staged] = (raw_summary, copy.deepcopy(expected), copy.deepcopy(summary),
                                    hashlib.sha256(self.read(record.config.limits.journal_bytes)).digest())
            return staged

    # @anchor:wa.s4.mw_profiles._Journal.context
    def context(self):
        def load(kind, value, related):
            for raw, inventory, summary, chain in self._staged.values():
                if kind == 'run_inventory' and value == summary:
                    return copy.deepcopy(inventory)
            raise ProfileError()

        def authenticate(kind, value, related):
            valid = any(kind == 'run_inventory' and value == summary and related and related[0] == inventory
                        for raw, inventory, summary, chain in self._staged.values())
            return gate.GateResult(0, 'PASS') if valid else gate.GateResult(5, 'FORGED')
        return gate.ValidationContext(self._root, evidence_load=load, evidence_check=authenticate,
                                      journal_read_budget=self._record.config.limits.journal_bytes)

    # @anchor:wa.s4.mw_profiles._Journal.validate
    def validate(self, staged, inventory):
        try:
            saved = self._staged.get(staged)
            _need(saved is not None and inventory == saved[1], 'journal_stage')
            with _guard(staged) as fd:
                _need(_read(fd, self._record.config.limits.journal_bytes) == saved[0], 'journal_stage')
            _need(hashlib.sha256(self.read(self._record.config.limits.journal_bytes)).digest() == saved[3],
                  'journal_stage')
            record = self._record
            with _guard(record.invocation.workflow) as fd:
                workflow_raw = _read(fd, record.config.limits.workflow_bytes)
            _need(hashlib.sha256(workflow_raw).hexdigest() == saved[1]['workflow_sha256'], 'journal_stage')
            workflow = gate.strict_load(workflow_raw)
            _need(_yes(record.owners.authorization.check('commit', workflow, record.scope, record.authority)),
                  'journal_authority')
            proof = record.owners.validation_context.evidence_check('run_inventory',
                        copy.deepcopy(saved[2]), (copy.deepcopy(saved[1]),))
            _need(type(proof) is gate.GateResult and proof.rc == 0 and proof.category == 'PASS', 'journal_evidence')
            result = gate.validate_file(staged, self._bundle, context=self.context())
            _need(type(result) is gate.GateResult and result.rc == 0 and result.category == 'PASS', 'journal_validate')
            return AdmissionCheck(Truth.TRUE, None)
        except Exception:
            return AdmissionCheck(Truth.FALSE, Reason.STALE)

    # @anchor:wa.s4.mw_profiles._Journal.select
    def select(self, staged):
        try:
            with self._lock:
                saved = self._staged.get(staged)
                _need(saved is not None and _yes(self.validate(staged, saved[1])) and
                      self._record.active and time.monotonic_ns() < self._record.config.limits.deadline_ns,
                      'journal_selection')
                os.replace(staged, self._record.invocation.output)
                self._sync(self._root)
                del self._staged[staged]
                return AdmissionCheck(Truth.TRUE, None)
        except Exception:
            return AdmissionCheck(Truth.FALSE, Reason.RECONCILIATION)

    # @anchor:wa.s4.mw_profiles._Journal.close
    def close(self):
        with self._lock:
            if self._fd is not None:
                os.close(self._fd)
                self._fd, self._key = None, None


# @anchor:wa.s4.mw_profiles._Provisioning
@dataclass(repr=False)
class _Provisioning:
    profile: Profile
    invocation: LiveInvocation
    config: _HostConfiguration
    scope: object
    authority: object
    run_id: str
    actor_lock: object
    resources: object
    workspace: object = None
    owners: object = None
    journal: object = None
    ports: object = None
    active: bool = True

    # @anchor:wa.s4.mw_profiles._Provisioning._bound
    def _bound(self, profile, invocation):
        _need(self.active and profile is self.profile and invocation is self.invocation, 'provisioning_binding')

    # @anchor:wa.s4.mw_profiles._Provisioning._notice
    def _notice(self, invocation, *, scope, authority, now_ns, absolute_deadline_ns):
        try:
            self._bound(self.profile, invocation)
            _need(scope is self.scope and authority is self.authority and type(now_ns) is int and
                  absolute_deadline_ns == self.config.limits.deadline_ns and
                  now_ns <= time.monotonic_ns() < absolute_deadline_ns, 'notice_binding')
            _validate_token(self.profile, invocation)
            arguments = dict(invocation=invocation, scope=scope, authority=authority,
                             now_ns=now_ns, absolute_deadline_ns=absolute_deadline_ns)
            _need(_yes(self.config.notice(**arguments)), 'notice_evidence')
            _need(_yes(self.config.lease(**arguments)), 'notice_lease')
            return AdmissionCheck(Truth.TRUE, None)
        except Exception:
            return AdmissionCheck(Truth.FALSE, Reason.STALE)

    # @anchor:wa.s4.mw_profiles._Provisioning._surface
    def _surface(self, *, identity, container, window, identity_env, timeout, transport, trace):
        self._bound(self.profile, self.invocation)
        _need((identity, container, window, identity_env) == (self.invocation.identity,
              self.invocation.container, self.invocation.window, self.invocation.identity_env) and
              trace is None, 'surface_binding')
        _workspace_check(self.workspace)
        surface = SrfIdentitySurface(identity=identity, container=container, window=window,
            identity_env=identity_env, timeout=timeout, transport=transport, trace=None, ram_dir=str(self.workspace))
        if type(self.owners.observations) is _ProfileObservations:
            _need(self.owners.observations.surface is None, 'surface_once')
            self.owners.observations.surface = surface
        return surface

    # @anchor:wa.s4.mw_profiles._Provisioning._dependencies
    def _dependencies(self, profile, invocation):
        self._bound(profile, invocation)
        o, c = self.owners, self.config
        return dict(scope=self.scope, authority=self.authority, run_id=self.run_id, limits=c.limits,
            validation_context=o.validation_context, artifact=_Artifact(invocation, c.limits),
            authorization=o.authorization, capabilities=o.capabilities, observations=o.observations,
            resources=self.resources, journal=self.journal, ram=None, replay_policy=None, slots=None,
            private_paste=None, slot_length_bindings=(), actor_lock=self.actor_lock, actor_identity=self.run_id,
            announce_gate=self._notice, clock=time.monotonic_ns, sleep=self._sleep, native_policy=c.native_policy)

    # @anchor:wa.s4.mw_profiles._Provisioning._sleep
    def _sleep(self, duration):
        _need(self.active and type(duration) is int and 0 < duration <=
              self.config.limits.deadline_ns - time.monotonic_ns(), 'sleep_deadline')
        time.sleep(duration / 1_000_000_000)

    # @anchor:wa.s4.mw_profiles._Provisioning._locator_arguments
    def _locator_arguments(self, profile, invocation, ports):
        self._bound(profile, invocation)
        _need(ports.scope is self.scope and ports.authority is self.authority, 'ports_binding')
        self.ports = ports
        self.resources._joins.append(ports.native_read.drain)
        c = self.config
        describer = None
        if type(self.owners.observations) is _ProfileObservations:
            describer = LiveOCRDescriber(evidence_policy=c.evidence_policy,
                locator_policy=c.locator_policy, clock=ports.clock, ve_root=os.environ.get('VQA_VE_ROOT'))
            self.owners.observations.bind(ports, describer)
            self.resources._joins.extend((describer.drain, self.owners.observations.drain))
        # Only the verified digest enters invocation metadata.
        trust = LiveEvidenceTrust(c.capability_revision, c.supported_locales, c.destination,
            self._export, self._custody, self._support, (invocation.live_ack,))
        return dict(locator_policy=c.locator_policy, evidence_policy=c.evidence_policy, trust=trust,
            workspace=self.workspace, executable=c.executable, ve_root=os.environ.get('VQA_VE_ROOT'),
            surface_factory=self._surface,
            **({} if describer is None else {'describer': describer}),
            **({} if c.ai_locate else {'locate_transport': _DisabledLocateTransport()}))

    # @anchor:wa.s4.mw_profiles._Provisioning._evidence
    def _evidence(self, callback, **kwargs):
        try:
            _need(self.active and kwargs.get('scope') is self.scope and kwargs.get('authority') is self.authority and
                  type(kwargs.get('deadline_ns')) is int and
                  time.monotonic_ns() < kwargs['deadline_ns'] <= self.config.limits.deadline_ns, 'evidence_binding')
            binding = kwargs.get('binding', getattr(kwargs.get('observation'), 'value', None))
            _need(binding is not None and binding.scope is self.scope and binding.authority is self.authority,
                  'evidence_scope')
            if 'observation' in kwargs:
                observation = kwargs['observation']
                _need(observation.scope is self.scope and observation.authority is self.authority and
                      observation.complete is True and observation.missing_reason is None, 'evidence_observation')
            for key in ('frame_path', 'marks_path'):
                if key in kwargs:
                    _ram_path(kwargs[key], self.workspace, self.config.locator_policy.image_bytes)
            if 'destination' in kwargs:
                _need(kwargs['destination'] is self.config.destination, 'evidence_destination')
            answer = callback(**kwargs)
            return answer if _yes(answer) else AdmissionCheck(Truth.FALSE, Reason.PRIVACY)
        except Exception:
            return AdmissionCheck(Truth.FALSE, Reason.PRIVACY)

    # @anchor:wa.s4.mw_profiles._Provisioning._export
    def _export(self, **kwargs):
        if kwargs.get('operation') == 'downloads' and not any(
                key in kwargs for key in ('frame_path', 'marks_path', 'observation', 'description', 'binding')):
            return self._downloads_export(**kwargs)
        if type(getattr(self.config.export_check, '__self__', None)) is _DefaultHost:
            return AdmissionCheck(Truth.FALSE, Reason.PRIVACY)
        return self._evidence(self.config.export_check, **kwargs)

    # @anchor:wa.s4.mw_profiles._Provisioning._downloads_export
    def _downloads_export(self, *, invocation, scope, authority, deadline_ns, operation, destination):
        """Download custody admission is separate from image disclosure/completion."""
        try:
            self._bound(self.profile, invocation)
            _need(scope is self.scope and authority is self.authority and
                  type(deadline_ns) is int and time.monotonic_ns() < deadline_ns <=
                  self.config.limits.deadline_ns, 'downloads_binding')
            _validate_token(self.profile, invocation)
            _need(operation == 'downloads' and isinstance(destination, Path) and
                  destination == self.profile.export_dir, 'downloads_destination')
            caps = self.owners.capabilities
            _need(type(caps) is _ProfileCapabilities and _yes(caps.operation(operation)), 'downloads_probe')
            auth = self.owners.authorization
            _need(_yes(auth.check('send', auth.workflow, scope, authority)), 'downloads_authorization')
            with _guard(destination, directory=True):
                return AdmissionCheck(Truth.TRUE, None)
        except Exception:
            return AdmissionCheck(Truth.FALSE, Reason.PRIVACY)

    # @anchor:wa.s4.mw_profiles._Provisioning._custody
    def _custody(self, **kwargs):
        return self._evidence(self.config.custody_check, **kwargs)

    # @anchor:wa.s4.mw_profiles._Provisioning._support
    def _support(self, **kwargs):
        return self._evidence(self.config.support_check, **kwargs)


# @anchor:wa.s4.mw_profiles._workspace_check
def _workspace_check(path):
    root = _RAM_ROOT
    _need(path is not None and path != root and path.is_relative_to(root) and
          not any(p.is_symlink() for p in (path, *path.parents)), 'workspace_containment')
    _workspace_root_check(root)
    for p in (path,):
        meta = p.stat()
        _need(stat.S_ISDIR(meta.st_mode) and meta.st_uid == os.geteuid() and
              stat.S_IMODE(meta.st_mode) == 0o700, 'workspace_private')
    _ram_path(path, path)



# @anchor:wa.s4.mw_profiles._workspace_parent_check
def _workspace_parent_check(root):
    _need(root.is_absolute() and not any(p.is_symlink() for p in (root, *root.parents)),
          'workspace_containment')
    parent = root if root.exists() else root.parent
    _workspace_root_check(parent)


# @anchor:wa.s4.mw_profiles._workspace_root_check
def _workspace_root_check(root):
    meta = root.lstat()
    _need(not root.is_symlink() and root.resolve() == root and stat.S_ISDIR(meta.st_mode) and
          meta.st_uid in (0, os.geteuid()) and
          (not meta.st_mode & 0o002 or bool(meta.st_mode & stat.S_ISVTX)), 'workspace_root')
    mounts = []
    with open('/proc/self/mountinfo', encoding='utf-8') as stream:
        for line in stream:
            left, right = line.rstrip().split(' - ', 1)
            mount = left.split()[4]
            for code, char in ((r'\040', ' '), (r'\011', '\t'), (r'\012', '\n'), (r'\134', '\\')):
                mount = mount.replace(code, char)
            if root.is_relative_to(Path(mount)):
                mounts.append((len(Path(mount).parts), right.split()[0]))
    _need(bool(mounts) and max(mounts)[1] in ('tmpfs', 'ramfs'), 'workspace_mount')


# @anchor:wa.s4.mw_profiles._default_profile_services
@contextmanager
def _default_profile_services(profile, invocation, *, bundle):
    """Compose a fresh private run; unavailable capacity/owner bindings fail closed.

    _HOST_CONFIGURATION is an item-local trusted Python installation seam, not a
    source of production policy or evidence. Its owner callback must return fresh
    independently authorized backends. Composition fixtures cannot certify them.
    """
    provider = _HOST_CONFIGURATION.get()
    start_ns = time.monotonic_ns()
    if provider is None:
        config = _trusted_default_configuration(profile, invocation, bundle=bundle, start_ns=start_ns)
    else:
        _need(callable(provider), 'host_installed')
        config = provider(profile, invocation, bundle=bundle)
    _host_policy(config, start_ns)
    host = getattr(config.lease, '__self__', None)
    if type(host) is _DefaultHost:
        host.image_bytes = config.locator_policy.image_bytes
    record = _Provisioning(profile, invocation, config, object(), object(), uuid.uuid4().hex,
                           ActorLock(), _Resources(config))
    host = getattr(config.lease, '__self__', None)
    if type(host) is _DefaultHost:
        host.acquire(record.run_id)
        host.record = record
    root = _RAM_ROOT
    try:
        # Register lease cleanup before any workspace operation can fail.
        _workspace_parent_check(root)
        root.mkdir(mode=0o700, exist_ok=True)
        _workspace_root_check(root)
        workspace = root / record.run_id
        workspace.mkdir(mode=0o700)
        record.workspace = workspace
        _workspace_check(record.workspace)
        record.owners = config.owners(invocation=invocation, scope=record.scope, authority=record.authority,
            workspace=record.workspace, clock=time.monotonic_ns, bundle=bundle)
        _need(type(record.owners) is _HostOwners, 'host_owners')
        record.journal = _Journal(record, bundle)
        yield ProfileServices(record._dependencies, record._locator_arguments)
    finally:
        primary_failure = sys.exc_info()[0] is not None
        record.active = False
        cleanup_failure = None
        try:
            clean = _yes(record.resources.cancel(config.limits.cancel_ns))
            clean = _yes(record.resources.drain(config.limits.drain_ns)) and clean
            # Never recursively remove a workspace with unresolved producer custody.
            _need(clean and record.actor_lock.holder is None, 'provisioning_cleanup')
            if record.workspace is not None:
                _workspace_check(record.workspace)
                _need(not any(record.workspace.iterdir()), 'workspace_empty')
                record.workspace.rmdir()
            if type(host) is _DefaultHost:
                try:
                    if host.started:
                        host.append_notice('end')
                finally:
                    host.release()
        except Exception as error:
            cleanup_failure = error
        finally:
            try:
                if type(record.journal) is _Journal:
                    record.journal.close()
            except Exception as error:
                cleanup_failure = cleanup_failure or error
        if cleanup_failure is not None and not primary_failure:
            raise cleanup_failure


# @anchor:wa.s4.mw_profiles.run_profile
def run_profile(profile, invocation, *, bundle, services):
    try:
        _need(type(profile) is Profile and type(invocation) is LiveInvocation, 'invocation_type')
        _need(invocation.mode in profile.allowed_modes and invocation.mode in ('replay', 'shadow') and
              (invocation.identity, invocation.container, invocation.window, invocation.identity_env) ==
              (profile.capsule, profile.container, profile.window, profile.identity_env) and
              profile.announce_gate == 'trusted_notice_v1', 'binding')
        with ExitStack() as stack:
            _validate_token(profile, invocation)
            stack.enter_context(_guard(profile.export_dir, directory=True))
            if services is None:
                services = stack.enter_context(_default_profile_services(profile, invocation, bundle=bundle))
            _need(type(services) is ProfileServices and callable(services.dependencies) and
                  callable(services.locator_arguments), 'services')
            stack.enter_context(_guard(profile.identity_env))
            dependencies = _arguments(services.dependencies(profile, invocation), build_live_ports, {'invocation'})
            if 'authorization' in dependencies:
                dependencies['authorization'] = _PinnedAuthorization(profile, invocation, dependencies['authorization'])
            ports = build_live_ports(invocation, **dependencies)
            arguments = _arguments(services.locator_arguments(profile, invocation, ports), run_live_workflow,
                                   {'invocation', 'bundle', 'ports'})
            return run_live_workflow(invocation, bundle=bundle, ports=ports, **arguments).result
    except ProfileError:
        raise
    except Exception:
        raise ProfileError() from None
