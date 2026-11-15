# @module {"id":"wa.tests.scripts.test_header","layer":2,"owner":"LEARN","deps":["wa.scripts.header","wa.scripts.types","wa.tests.scripts.support","wa.tools.build_code_index","wa.tools.route_code"],"book":"HB-WA-s4-learn"}
"""Frozen-byte oracles, retained-owner controls and a local seccomp parser.

Deterministic fakes prove orchestration; executable probes certify only the
fixed local test boundary, not an absent production host adapter.
"""
import ast as _ast
import copy as _copy
import dataclasses as _dc
import enum as _enum
import hashlib as _hashlib
import gc as _gc
import weakref as _weakref
import json as _json
import ctypes.util as _ctypes_util
import os as _os
import sys as _sys
import time as _time
import tracemalloc as _tracemalloc
from pathlib import Path as _Path
import subprocess as _subprocess
import tempfile as _tempfile
import unittest as _unittest
from unittest import mock as _mock
from wender.scripts import header as _h, types as _t
from tests.scripts.support import make_fixture as _fixture, verify_routing_manifest as _routing
from tools import build_code_index as _index, route_code as _router

_PREFIX = '# [SCRIPT_TAG] {"logical_id":"example","metadata_references":[],"predecessor":{"reason":"missing_evidence","value":null},"revision":1,"scope":{"attestation":null,"attestation_reason":"missing_evidence","browser_profile_incarnation":"profile","frame_ancestry":["https://example.test"],"principal":"p","scope_revision":1,"target_frame_origin":"https://example.test","tenant":"t","top_level_origin":"https://example.test"}}\n# [LANE_CONFIG] {"abis":{"act":"wender-act/v1","compiler":"wender-compiler/v1","dsl":"wender-dsl/v1","predicate":"wender-predicate/v1","schema":"wender-schema/v1"},"limits":{"absolute_deadline_ns":1000,"canonical_bytes":1024,"episode_input_bytes":128,"episode_result_bytes":128,"max_acquisitions":4,"max_nodes":4}}\n# [SURGERY_DECLARATION] {"entry":{"children":[],"operands":[{"kind":"TEXT","operand_id":"goal","query_id":"goal_query","value":"goal"}],"operator":"task_goal","scope":{"attestation":null,"attestation_reason":"missing_evidence","browser_profile_incarnation":"profile","frame_ancestry":["https://example.test"],"principal":"p","scope_revision":1,"target_frame_origin":"https://example.test","tenant":"t","top_level_origin":"https://example.test"},"source_requirements":["HOST"],"transition_reference":{"reason":"missing_evidence","value":null}},"terminal_goal":"goal"}\n# [CAPA_TRILOGIA] {"capabilities":[]}\n# [IO_CONTRACT] {"goal_contract":"goal","signature_contract":"signature","site_contract":"site"}\n# [MUTANT_PROOF] {"mutant":{"reason":"missing_evidence","value":null},"nominal":{"reason":"missing_evidence","value":null}}\n# [MERKLE_PIN] {"pins_reason":"missing_evidence","supplied_pins":null}\n# [META:args] []\n'
_BODY = b'def run(act, params):\n    return None\n'
_TRUE = _t.AdmissionCheck(_t.Truth.TRUE, None)
_PROBES = frozenset(('environment', 'descriptors', 'home-read', 'workspace-read',
    'write', 'network', 'socket', 'child', 'trace', 'signal', 'exec-load',
    'memory', 'deadline', 'terminate', 'join', 'drain', 'safe-round-trip'))


def _wire_record(value):
    if isinstance(value, _enum.Enum):
        return value.value
    if _dc.is_dataclass(value):
        return {f.name: _wire_record(getattr(value, f.name)) for f in _dc.fields(value)}
    if type(value) is tuple:
        return [_wire_record(v) for v in value]
    return value


def _declarations(prefix=_PREFIX):
    return [(line.split('] ', 1)[0][3:], _json.loads(line.split('] ', 1)[1]))
            for line in prefix.splitlines()]


def _source(declarations=None, body=_BODY):
    if declarations is None:
        return _PREFIX.encode() + body
    return ''.join('# [' + tag + '] ' + _json.dumps(value, ensure_ascii=False,
        separators=(',', ':')) + '\n' for tag, value in declarations).encode() + body


def _reap(child):
    if child.poll() is None:
        child.kill()
    child.communicate(timeout=2)


def _ref(name):
    return _t.RegistryRef(name, 'incarnation', 1, _t.PrivateRef('host', 'incarnation', name))


class _Host:
    """Identity-sensitive synthetic owners, no production containment claim."""
    def __init__(self):
        self.now = 0
        self.owner = _t.PrivateRef('host', 'incarnation', 'owner')
        self.scope = _h._decode(_declarations()[0][1]['scope'], _t.ScopeIdentity)
        self.source = _t.SourcePolicy(16384, 65536, 64)
        self.parser = _t.ParserPolicy(1024, 8, 32, 1000, 1000000, 'registered')
        self.slots = _t.SlotPolicy(32, 4096, 16384, 16384, 65536)
        self.cancel = _t.CancellationPolicy(10, 'escalate', 'join', 'boundary', 20, 30, 4, 5000000)
        self.policy = _t.ResourcePolicy(10000000, 5000000, 100000, 100, 1000000,
                                      1000, 1000, self.cancel, 1)
        self.events, self.grants, self.proofs, self.releases = [], [], {}, {}
        self.backend = self
        self.current = self
        self.evidence = _ref('probe-receipt')
        self.probes = _PROBES
        self.attempt_count, self.dispatches, self.inputs = 0, 0, 0
        self.wire = b'["Module",[["Pass",[]]]]'
        self.failure = None
        self.drain = True
        self.peak = 2000000
        self.used = None
        self.refuse_growth = False
        self.fault_after_parse = None
        self.retained_plan = None
        self.compiler = None
        self.outstanding_children = []
        self.holders = {}
        self.current_proofs = {}
        self.holder_failures = 0
        self.growth_requests = []
        self.allow_growth = False
        self.grant_deadline = None
        self.parser_deadlines = []

    # @anchor:wa.tests.scripts.test_header._Host.now_ns
    def now_ns(self):
        return self.now

    # @anchor:wa.tests.scripts.test_header._Host.charge
    def charge(self, scope, owner, operation, policy, now_ns):
        self.events.append('charge')
        self.attempt_count += 1
        if self.attempt_count > policy.cross_episode_operations:
            return _t.AdmissionCheck(_t.Truth.FALSE, _t.Reason.CAPACITY)
        return _TRUE

    # @anchor:wa.tests.scripts.test_header._Host.reserve
    def reserve(self, request, policy):
        self.events.append('reserve')
        grant = _t.ReservationGrant(request, _ref('grant-' + str(len(self.grants))),
            request.peak_bytes, request.accounting_revision, self.now,
            min(request.absolute_deadline_ns, self.grant_deadline or request.absolute_deadline_ns))
        self.grants.append(grant)
        return _t.ReservationOutcome(grant, None)

    # @anchor:wa.tests.scripts.test_header._Host.grow
    def grow(self, grant, request, policy):
        self.growth_requests.append((grant, request))
        if not self.allow_growth:
            return _t.ReservationOutcome(None, _t.Reason.CAPACITY)
        grown = _dc.replace(grant, request=request, charged_bytes=request.peak_bytes)
        self.grants[self.grants.index(grant)] = grown
        return _t.ReservationOutcome(grown, None)

    # @anchor:wa.tests.scripts.test_header._Host.request
    def request(self, holder):
        self.events.append('drain')
        if not self.drain:
            return _t.CancellationResult(True, False, False, False, False, (), None, _t.MissingReason.ABSENT)
        for child in self.outstanding_children:
            child.terminate()
            child.communicate(timeout=2)
        self.outstanding_children.clear()
        grant = self.holders.get(id(holder))
        if grant is None:
            grant = self.holders[id(holder)]
        proof = _t.CancellationResult(True, True, False, False, True,
                                      (grant.reservation,), _ref('drained'), None)
        self.proofs[id(proof)] = (proof, holder, grant)
        self.current_proofs[id(holder)] = proof
        return proof

    # @anchor:wa.tests.scripts.test_header._Host.verify
    def verify(self, *args):
        if len(args) == 2:
            holder, proof = args
            retained = self.proofs.get(id(proof))
            if retained is None or retained[0] is not proof or retained[1] is not holder:
                return _t.AdmissionCheck(_t.Truth.FALSE, _t.Reason.FOREIGN)
            if self.current_proofs.get(id(holder)) is not proof:
                return _t.AdmissionCheck(_t.Truth.FALSE, _t.Reason.STALE)
            return _TRUE
        backend, reference, policy, deadline = args
        if backend is not self.current or self.probes != _PROBES:
            raise _t.ContractError(_t.Reason.MISSING)
        return self.evidence

    # @anchor:wa.tests.scripts.test_header._Host.check
    def check(self, *args):
        if len(args) == 4:
            evidence, backend, reference, policy = args
            return _TRUE if evidence is self.evidence and backend is self.current else _t.AdmissionCheck(_t.Truth.FALSE, _t.Reason.STALE)
        plan, compiler, identity, scope, abis = args
        return _TRUE if plan is self.retained_plan and compiler is self.compiler else _t.AdmissionCheck(_t.Truth.FALSE, _t.Reason.FOREIGN)

    # @anchor:wa.tests.scripts.test_header._Host.release_unused
    def release_unused(self, grant, amount, proof):
        retained = self.proofs.get(id(proof))
        if retained is None or retained[0] is not proof or retained[2] is not grant:
            return _t.AdmissionCheck(_t.Truth.FALSE, _t.Reason.FOREIGN)
        prior = self.releases.get(id(grant))
        if prior is not None:
            return _TRUE if prior[1] == amount else _t.AdmissionCheck(_t.Truth.FALSE, _t.Reason.CONFLICT)
        if not any(g is grant for g in self.grants):
            return _t.AdmissionCheck(_t.Truth.FALSE, _t.Reason.FOREIGN)
        self.grants.remove(grant)
        self.releases[id(grant)] = (grant, amount)
        return _TRUE

    # @anchor:wa.tests.scripts.test_header._Host.resolve
    def resolve(self, reference):
        return self.backend

    # @anchor:wa.tests.scripts.test_header._Host.parse
    def parse(self, body, policy, deadline, grant, holder):
        self.events.append('parse')
        self.holders[id(holder)] = grant
        self.dispatches += 1
        self.parser_deadlines.append(deadline)
        if self.failure:
            raise self.failure
        if self.fault_after_parse:
            self.fault_after_parse()
        return self.wire

    # @anchor:wa.tests.scripts.test_header._Host.telemetry
    def telemetry(self):
        return sum(g.charged_bytes for g in self.grants) if self.used is None else self.used

    # @anchor:wa.tests.scripts.test_header._Host.peak_bound
    def peak_bound(self, *args):
        return self.peak

    # @anchor:wa.tests.scripts.test_header._Host.decode
    def decode(self, wire, policy):
        self.events.append('decode-result')
        def freeze(v):
            return tuple(freeze(i) for i in v) if type(v) is list else v
        return freeze(_json.loads(wire))

    # @anchor:wa.tests.scripts.test_header._Host.holder_factory
    def holder_factory(self, grant, scope, policy, endpoint, boundary):
        if self.holder_failures:
            self.holder_failures -= 1
            raise RuntimeError('interrupted holder acquisition')
        for holder_id, retained in self.holders.items():
            if retained is grant:
                return self.retained_holders[holder_id]
        holder = _t.HolderContract('source-admission', _t.Purpose.INPUT, scope,
            grant.charged_bytes, grant.absolute_deadline_ns, endpoint, boundary, policy)
        if not hasattr(self, 'retained_holders'):
            self.retained_holders = {}
        self.retained_holders[id(holder)] = holder
        self.holders[id(holder)] = grant
        return holder

    # @anchor:wa.tests.scripts.test_header._Host.admission
    def admission(self, **changes):
        args = dict(scope=self.scope, owner=self.owner, source_policy=self.source,
            parser_policy=self.parser, slot_policy=self.slots, resource_policy=self.policy,
            clock=self, attempts=self, reservations=self, cancellation=self,
            resolver=self, verifier=self, peak_bound=self.peak_bound, telemetry=self.telemetry,
            result_decoder=self.decode, endpoint=_ref('endpoint'), boundary=_ref('boundary'),
            holder_factory=self.holder_factory, accounting_revision=1, absolute_deadline_ns=10000)
        args.update(changes)
        return _h.SourceAdmission(**args)


# Fixed, test-owned executable bootstrap. No candidate code is executed.
# Imports/runtime loading precede a kernel default-deny seccomp filter; only
# pipe I/O, nonexecutable anonymous memory and Python runtime syscalls survive.
_BOUNDARY_CHILD = r'''
import ast, ctypes, errno, json, os, resource, socket, sys
mode, library, runtime, owner_file, unrelated_fd, allow_socket = sys.argv[1:]
body = sys.stdin.buffer.read(65537)
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
resource.setrlimit(resource.RLIMIT_AS, (64 * 1024**2, 64 * 1024**2))
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
if allow_socket == 'yes':  # Must-FAIL boundary mutation, never candidate-controlled.
    assert lib.seccomp_rule_add(ctx, 0x7fff0000,
        lib.seccomp_syscall_resolve_name(b'socket'), 0) == 0
assert lib.seccomp_load(ctx) == 0
lib.seccomp_release(ctx)
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
    tree = ast.parse(body.decode('utf-8'))
    def freeze_tree(node):
        return [type(node).__name__, [freeze_tree(child) for child in ast.iter_child_nodes(node)]]
    os.write(1, json.dumps(freeze_tree(tree)).encode())
'''


class _ProcessHost(_Host):
    """Local executable fixture, not an installed production host adapter."""
    def __init__(self):
        super().__init__()
        self.library = _ctypes_util.find_library('seccomp')
        if self.library is None:
            raise _t.ContractError(_t.Reason.MISSING)
        self.parser = _dc.replace(self.parser, incremental_memory_bytes=64 * 1024**2,
                                  timeout_ms=1000)
        self.cancel = _dc.replace(self.cancel, consumer_byte_ceiling=128 * 1024**2,
                                  join_deadline_ns=100000000)
        self.policy = _dc.replace(self.policy, host_bytes=256 * 1024**2,
            process_bytes=128 * 1024**2, cancellation=self.cancel)
        self.peak = 96 * 1024**2
        self.mode, self.allow_socket = 'parse', False
        self.receipt = None
        self.process_evidence = []
        self.probe_path = str(_Path(__file__).resolve())
        self.foreign_fd = 99
        self.holder_processes = {}
        self.cleanup_evidence = {}

    def _configuration(self):
        return (self.library, _sys.executable, self.allow_socket,
                _hashlib.sha256(_BOUNDARY_CHILD.encode()).digest())

    # @anchor:wa.tests.scripts.test_header._ProcessHost.now_ns
    def now_ns(self):
        return _time.monotonic_ns()

    def _run(self, mode, body, deadline):
        child = _subprocess.Popen([_sys.executable, '-I', '-S', '-c', _BOUNDARY_CHILD,
            mode, self.library, _sys.executable, self.probe_path, str(self.foreign_fd),
            'yes' if self.allow_socket else 'no'], cwd='/', env={'LC_ALL': 'C.UTF-8'},
            close_fds=True, stdin=_subprocess.PIPE, stdout=_subprocess.PIPE,
            stderr=_subprocess.PIPE)
        self.outstanding_children.append(child)
        reason = None
        try:
            output, diagnostic = child.communicate(body, timeout=max(
                0.001, (deadline - self.now_ns()) / 1000000000))
        except _subprocess.TimeoutExpired:
            child.terminate()
            try:
                output, diagnostic = child.communicate(timeout=1)
            except _subprocess.TimeoutExpired:
                child.kill()
                output, diagnostic = child.communicate(timeout=1)
            reason = _t.Reason.DEADLINE
        self.outstanding_children.remove(child)
        self.process_evidence.append((mode, child.pid, child.returncode,
            child.stdin.closed, child.stdout.closed, child.stderr.closed))
        if reason is not None:
            raise _t.ContractError(reason)
        if child.returncode == 42 and output == b'ENFORCED_MEMORY_LIMIT':
            raise _t.ContractError(_t.Reason.CAPACITY)
        if child.returncode != 0:
            raise _t.ContractError(_t.Reason.MISSING)
        return output

    # @anchor:wa.tests.scripts.test_header._ProcessHost.verify
    def verify(self, *args):
        if len(args) == 2:
            return super().verify(*args)
        backend, reference, policy, deadline = args
        if backend is not self or policy is not self.parser:
            raise _t.ContractError(_t.Reason.FOREIGN)
        result = _json.loads(self._run('probes', _BODY, deadline))
        expected = _PROBES - {'memory', 'deadline', 'terminate', 'join', 'drain', 'safe-round-trip'}
        if (set(result['denied']) != expected | {'executable-memory'}
                or 'FunctionDef' not in result['parsed'] or not result['unrelated_closed']):
            raise _t.ContractError(_t.Reason.MISSING)
        for mode, reason in (('exhaust', _t.Reason.CAPACITY), ('stall', _t.Reason.DEADLINE)):
            observed = None
            try:
                self._run(mode, _BODY, min(deadline, self.now_ns() + 200000000))
            except _t.ContractError as error:
                observed = error.reason
            if observed is not reason:
                raise _t.ContractError(_t.Reason.MISSING)
        parsed = _json.loads(self._run('parse', _BODY, deadline))
        if parsed[0] != 'Module' or parsed[1][0][0] != 'FunctionDef':
            raise _t.ContractError(_t.Reason.MISSING)
        self.receipt = (backend, reference, policy, self._configuration())
        return self.evidence

    # @anchor:wa.tests.scripts.test_header._ProcessHost.check
    def check(self, *args):
        if len(args) == 4:
            evidence, backend, reference, policy = args
            if (self.receipt is None or self.receipt[0] is not backend
                    or self.receipt[1] != reference or self.receipt[2] is not policy
                    or self.receipt[3] != self._configuration()):
                return _t.AdmissionCheck(_t.Truth.FALSE, _t.Reason.STALE)
        return super().check(*args)

    # @anchor:wa.tests.scripts.test_header._ProcessHost.parse
    def parse(self, body, policy, deadline, grant, holder):
        self.dispatches += 1
        self.holder_processes[id(holder)] = len(self.process_evidence)
        return self._run(self.mode, body, deadline)

    # @anchor:wa.tests.scripts.test_header._ProcessHost.request
    def request(self, holder):
        if self.outstanding_children:
            raise _t.ContractError(_t.Reason.MISSING)
        start = self.holder_processes.get(id(holder))
        rows = () if start is None else tuple(self.process_evidence[start:])
        if start is not None and (not rows or not all(
                row[2] is not None and all(row[3:]) for row in rows)):
            raise _t.ContractError(_t.Reason.MISSING)
        grant = self.holders[id(holder)]
        proof = _t.CancellationResult(True, not rows, bool(rows), bool(rows), True,
            (grant.reservation,), _ref('joined-drained'), None)
        self.proofs[id(proof)] = (proof, holder, grant)
        self.current_proofs[id(holder)] = proof
        self.cleanup_evidence[id(proof)] = (holder, grant.reservation, rows)
        return proof

    # @anchor:wa.tests.scripts.test_header._ProcessHost.admission
    def admission(self, **changes):
        changes.setdefault('absolute_deadline_ns', self.now_ns() + 5000000000)
        return super().admission(**changes)

    def _source(self):
        declarations = _declarations()
        declarations[1][1]['limits']['absolute_deadline_ns'] = self.now_ns() + 5000000000
        return _source(declarations)


# @anchor:wa.tests.scripts.test_header.HeaderControls
class HeaderControls(_unittest.TestCase):
    def _admit(self, source=None):
        host = _Host()
        admission = host.admission()
        session = admission.admit(_source() if source is None else source)
        self.addCleanup(admission.finish, session)
        return host, admission, session

    def _reject(self, source, reason, dispatches=0, host=None):
        host = host or _Host()
        admission = host.admission()
        with self.assertRaises(_t.ContractError) as error:
            admission.admit(source)
        self.assertIs(error.exception.reason, reason)
        self.assertIsNone(error.exception.__context__)
        self.assertIsNone(error.exception.__cause__)
        self.assertEqual((host.inputs, host.dispatches, host.grants, admission.outstanding()),
                         (0, dispatches, [], ()))
        return host

    # @anchor:wa.tests.scripts.test_header.HeaderControls.test_g_source_wire
    def test_g_source_wire(self):
        original = _declarations()
        mutations = [original[1:], original[:1] + original,
                     [original[1], original[0]] + original[2:],
                     original + [('META:exec', 'passive')],
                     original + [('META:uso', 'x'), ('META:fts', ['x'])],
                     original + [('META:uso', 'x'), ('META:uso', 'x')]]
        for draft in mutations:
            with self.subTest(draft=draft[0][0]):
                self._reject(_source(draft), _t.Reason.SCHEMA)
        for text in (_PREFIX.replace('# [LANE_CONFIG]', '\n# [LANE_CONFIG]'),
                     '\n' + _PREFIX, '#!/usr/bin/python3\n' + _PREFIX,
                     '# ordinary comment\n' + _PREFIX,
                     _PREFIX.rstrip('\n'),
                     _PREFIX.replace('"revision":1', '"revision":1,"revision":1'),
                     _PREFIX.replace('"reason":"missing_evidence"', '"unknown":0,"reason":"missing_evidence"', 1),
                     _PREFIX.replace('"metadata_references":[],', '', 1),
                     _PREFIX.replace('"capabilities":[]', '"capabilities":\n[]')):
            self._reject(text.encode() + _BODY, _t.Reason.SCHEMA)
        reordered = [(tag, dict(reversed(tuple(value.items()))) if type(value) is dict else value)
                     for tag, value in original]
        host, admission, session = self._admit(_source(reordered))
        self.assertEqual(session._body, _BODY)
        self.assertEqual((host.dispatches, host.inputs, len(host.grants)), (1, 0, 1))

    # @anchor:wa.tests.scripts.test_header.HeaderControls.test_g_source_encoding
    def test_g_source_encoding(self):
        for source, reason in ((str(_source()), _t.Reason.TYPE),
            (compile('pass', '<test>', 'exec'), _t.Reason.TYPE), (b'\xff', _t.Reason.TYPE),
            (b'\xef\xbb\xbf' + _source(), _t.Reason.SCHEMA),
            (_source(body=b'# coding: latin-1\npass\n'), _t.Reason.SCHEMA),
            (_source(body=b'\x00'), _t.Reason.SCHEMA),
            (b'x' * 65537, _t.Reason.BOUNDS), (_source(body=('é' * 16385).encode()), _t.Reason.BOUNDS)):
            self._reject(source, reason)
        class _Bytes(bytes):
            pass
        self._reject(_Bytes(_source()), _t.Reason.TYPE)
        for newline in (b'\r\n', b'\r'):
            host, admission, session = self._admit(_source().replace(b'\n', newline))
            self.assertEqual(session._body, _BODY)
        body = '# café\n\t# e\u0301\npass'.encode()
        self.assertEqual(self._admit(_source(body=body))[2]._body, body)

    # @anchor:wa.tests.scripts.test_header.HeaderControls.test_encoding_metadata_is_passive
    def test_encoding_metadata_is_passive(self):
        # Positive control: every optional display/retrieval string survives
        # source admission and canonical encoding without becoming a cookie.
        for tag in ('META:fts', 'META:uso', 'META:cli'):
            for text in ('Character encoding: UTF-8', 'coding=latin-1',
                         '# coding: ascii', 'encoding: UTF-8\n# coding: latin-1'):
                with self.subTest(tag=tag, text=text):
                    value = [text] if tag == 'META:fts' else text
                    host, admission, session = self._admit(
                        _source(_declarations() + [(tag, value)]))
                    metadata = _h.canonical_metadata(session._header, session._auxiliary,
                                                     session=session, admission=admission)
                    self.assertEqual(_json.loads(metadata)[tag], value)
                    self.assertEqual(session._body, _BODY)
                    self.assertEqual((host.dispatches, host.inputs), (1, 0))
                    admission.finish(session)
                    self.assertEqual((host.grants, admission.outstanding()), ([], ()))

    # @anchor:wa.tests.scripts.test_header.HeaderControls.test_actual_encoding_declarations_reject
    def test_actual_encoding_declarations_reject(self):
        # Must-FAIL: actual comments refuse before dispatch, across normalized
        # newlines, even when harmless encoding text is also present in META.
        declarations = _declarations() + [('META:uso', 'Character encoding: UTF-8')]
        for cookie in (b'# coding: latin-1', b'# coding=utf-8',
                       b'  # -*- coding: ascii -*-', b'\t# encoding: UTF-8'):
            for newline in (b'\n', b'\r\n', b'\r'):
                with self.subTest(cookie=cookie, newline=newline):
                    raw = _source(declarations, cookie + b'\n' + _BODY)
                    self._reject(raw.replace(b'\n', newline), _t.Reason.SCHEMA)
            self._reject(cookie + b'\n' + _source(), _t.Reason.SCHEMA)
        self.assertEqual(self._admit(_source(declarations))[0].dispatches, 1)

    # @anchor:wa.tests.scripts.test_header.HeaderControls.test_g_source_abi
    def test_g_source_abi(self):
        for name in _declarations()[1][1]['abis']:
            draft = _declarations()
            draft[1][1]['abis'][name] += '-test'
            self._reject(_source(draft), _t.Reason.SCHEMA)
        self.assertEqual(self._admit()[2]._header.abis.dsl, 'wender-dsl/v1')

    # @anchor:wa.tests.scripts.test_header.HeaderControls.test_g_source_json_types
    def test_g_source_json_types(self):
        for replacement, reason in (('true', _t.Reason.TYPE), ('1.0', _t.Reason.TYPE),
                                    ('NaN', _t.Reason.TYPE), ('Infinity', _t.Reason.TYPE),
                                    ('-Infinity', _t.Reason.TYPE), ('"\\ud800"', _t.Reason.TYPE)):
            source = _PREFIX.replace('"revision":1', '"revision":' + replacement).encode() + _BODY
            self._reject(source, reason)
        # Unicode failures are tested in a string position, not merely an int mismatch.
        self._reject(_PREFIX.replace('"example"', '"\\ud800"').encode() + _BODY, _t.Reason.TYPE)
        deeply_nested = _PREFIX.replace('# [META:args] []', '# [META:args] ' + '[' * 65 + ']' * 65)
        self._reject(deeply_nested.encode() + _BODY, _t.Reason.BOUNDS)
        self._reject(_PREFIX.replace('"HOST"', '"UNKNOWN_SOURCE"').encode() + _BODY,
                     _t.Reason.SCHEMA)
        host, admission, session = self._admit(_PREFIX.replace('"revision":1', '"revision":-0').encode() + _BODY)
        self.assertEqual(session._header.revision, 0)

    # @anchor:wa.tests.scripts.test_header.HeaderControls.test_g_canonical_vectors
    def test_g_canonical_vectors(self):
        self.assertEqual(_hashlib.sha256(_PREFIX.encode()).hexdigest(),
                         '3d40e4aafc71492c9dc0cffae9634eda065efb8e17ea8739002bb4e153c7007c')
        host, admission, session = self._admit(_source().replace(b'\n', b'\r\n'))
        actual = _h.canonical_metadata(session._header, session._auxiliary, session=session, admission=admission)
        self.assertEqual(_hashlib.sha256(b'wender-metadata/v1\0' + actual).hexdigest(),
                         '7bbaccf1e4c154fe704ff45e95f213411fbf30463f632591e301ac1f2eb12c4b')
        self.assertEqual(_hashlib.sha256(b'wender-source/v1\0' + session._body).hexdigest(),
                         'c4b7510822545cbace60682cdfd0705435ccf401d481b376fe2403c0d72dfa68')
        obj = {'z': ['\u2028', '\x00', 'é'], 'a': -0}
        canonical = _h._canonical(obj, admission, session)
        self.assertEqual(canonical, '{"a":0,"z":["\u2028","\\u0000","é"]}'.encode())
        self.assertNotEqual(canonical, b'{"z":[],"a":0}')
        self.assertEqual(_json.loads(actual)['SCRIPT_TAG']['scope'], dict(
            browser_profile_incarnation='profile', frame_ancestry=['https://example.test'],
            principal='p', scope_revision=1, target_frame_origin='https://example.test',
            tenant='t', top_level_origin='https://example.test'))

    # @anchor:wa.tests.scripts.test_header.HeaderControls.test_g_resource_admission
    def test_g_resource_admission(self):
        host = _Host()
        for name in ('source_policy', 'parser_policy', 'slot_policy', 'resource_policy',
                     'attempts', 'clock', 'reservations', 'cancellation', 'peak_bound',
                     'telemetry', 'resolver', 'verifier', 'result_decoder', 'holder_factory'):
            with self.subTest(missing=name), self.assertRaises(_t.ContractError) as error:
                host.admission(**{name: None})
            self.assertIs(error.exception.reason, _t.Reason.MISSING)
        for name in ('host_bytes', 'emergency_reserve_bytes'):
            policy = _copy.copy(host.policy)
            object.__setattr__(policy, name, None)
            admission = host.admission(resource_policy=policy)
            with self.assertRaises(_t.ContractError) as error:
                admission.admit(_source())
            self.assertIs(error.exception.reason, _t.Reason.UNKNOWN)
            self.assertEqual((host.grants, host.dispatches, admission.outstanding()), ([], 0, ()))
        host.peak = None
        self._reject(_source(), _t.Reason.MISSING, host=host)
        host = _Host()
        host.used = host.policy.host_bytes
        self._reject(_source(), _t.Reason.CAPACITY, host=host)
        host, admission, session = self._admit()
        self.assertLess(host.events.index('charge'), host.events.index('reserve'))
        self.assertLess(host.events.index('reserve'), host.events.index('parse'))
        grant = host.grants[0]
        self.assertIsNone(host.grow(grant, grant.request, host.policy).grant)
        self.assertIs(host.grants[0], grant)
        self.assertTrue(admission.outstanding())

    # @anchor:wa.tests.scripts.test_header.HeaderControls.test_g_resource_attempts
    def test_g_resource_attempts(self):
        host = _Host()
        host.policy = _dc.replace(host.policy, cross_episode_operations=2)
        for _ in range(2):
            self._reject(b'invalid', _t.Reason.SCHEMA, host=host)
        # A new coordinator/episode does not reset host accounting.
        self._reject(_source(), _t.Reason.CAPACITY, host=host)
        self.assertEqual((host.attempt_count, host.dispatches), (3, 0))
        fresh, admission, session = self._admit()
        admission.finish(session)
        self.assertEqual((fresh.attempt_count, fresh.dispatches), (1, 1))

    # @anchor:wa.tests.scripts.test_header.HeaderControls.test_g_parser_supervisor_identity
    def test_g_parser_supervisor_identity(self):
        host = _Host()
        host.backend = None
        self._reject(_source(), _t.Reason.MISSING, host=host)
        host = _Host()
        host.backend = _copy.copy(host)
        self._reject(_source(), _t.Reason.MISSING, host=host)
        host = _Host()
        host.probes -= {'safe-round-trip'}
        self._reject(_source(), _t.Reason.MISSING, host=host)
        host, admission, session = self._admit()
        self.assertEqual(session._result, ('Module', (('Pass', ()),)))
        with self.assertRaises(_t.ContractError) as error:
            _h.parse_header(host.grants[0].request, session=_dc.replace(session), admission=admission)
        self.assertIs(error.exception.reason, _t.Reason.FOREIGN)

    # @anchor:wa.tests.scripts.test_header.HeaderControls.test_g_parser_caps
    def test_g_parser_caps(self):
        # Real denied syscalls, real safe AST parse, same fixed kernel boundary.
        _os.putenv('S1A_TEST_NONSECRET_CANARY', 'fixture-only')
        self.addCleanup(_os.unsetenv, 'S1A_TEST_NONSECRET_CANARY')
        fd_read, fd_write = _os.pipe()
        _os.write(fd_write, b'unrelated-fixture-canary')
        self.addCleanup(_os.close, fd_read)
        self.addCleanup(_os.close, fd_write)
        for allow_socket in (True, False):
            host = _ProcessHost()
            host.allow_socket, host.foreign_fd = allow_socket, fd_read
            admission = host.admission()
            if allow_socket:
                with self.assertRaises(_t.ContractError) as error:
                    admission.admit(host._source())
                self.assertIs(error.exception.reason, _t.Reason.MISSING)
                self.assertEqual((host.inputs, host.dispatches, host.grants,
                                  admission.outstanding()), (0, 0, [], ()))
            else:
                session = admission.admit(host._source())
                self.assertEqual(session._result[1][0][0], 'FunctionDef')
                admission.finish(session)
                self.assertEqual((host.inputs, host.dispatches, host.grants,
                                  admission.outstanding()), (0, 1, [], ()))
            self.assertEqual(host.outstanding_children, [])
            self.assertTrue(all(row[2] is not None and all(row[3:])
                                for row in host.process_evidence))

    # @anchor:wa.tests.scripts.test_header.HeaderControls.test_g_resource_process_cleanup
    def test_g_resource_process_cleanup(self):
        for mode, reason in (('stall', _t.Reason.DEADLINE), ('exhaust', _t.Reason.CAPACITY)):
            host = _ProcessHost()
            host.mode = mode
            admission = host.admission()
            with self.assertRaises(_t.ContractError) as error:
                admission.admit(host._source())
            self.assertIs(error.exception.reason, reason)
            self.assertEqual((host.inputs, host.dispatches, host.grants,
                              admission.outstanding()), (0, 1, [], ()))
            self.assertEqual(host.outstanding_children, [])
            self.assertTrue(all(row[2] is not None and all(row[3:])
                                for row in host.process_evidence))
            actual = host.process_evidence[-1]
            self.assertEqual(actual[0], mode)
            self.assertEqual(actual[2], -15 if mode == 'stall' else 42)
            host.mode = 'parse'
            admission = host.admission()
            session = admission.admit(host._source())
            self.assertEqual(session._result[1][0][0], 'FunctionDef')
            admission.finish(session)
            self.assertEqual((host.inputs, host.dispatches, host.grants,
                              admission.outstanding()), (0, 2, [], ()))
        for wire, reason in ((b'x', _t.Reason.SCHEMA), (b'x' * 1025, _t.Reason.BOUNDS),
                             (b'{}', _t.Reason.TYPE)):
            host = _Host()
            host.wire = wire
            self._reject(_source(), reason, 1, host)

    # @anchor:wa.tests.scripts.test_header.HeaderControls.test_g_resource_release_identity
    def test_g_resource_release_identity(self):
        host, admission, session = self._admit()
        state = admission._sessions[id(session)][1]
        proof = host.request(state.holder)
        with self.assertRaises(_t.ContractError) as error:
            admission.finish(session, _dc.replace(proof))
        self.assertIs(error.exception.reason, _t.Reason.FOREIGN)
        self.assertEqual(len(host.grants), 1)
        host.now = 100000
        self.assertEqual(len(admission.outstanding()), 1)  # Deadline cannot release.
        admission.finish(session, proof)
        admission.finish(session, proof)
        self.assertEqual((len(host.grants), len(host.releases)), (0, 1))
        self.assertIs(host.release_unused(_dc.replace(state.grant), state.grant.charged_bytes, proof).reason,
                      _t.Reason.FOREIGN)
        self.assertIs(host.release_unused(state.grant, 1, proof).reason, _t.Reason.CONFLICT)

    # @anchor:wa.tests.scripts.test_header.HeaderControls.test_holder_factory_recovery
    def test_holder_factory_recovery(self):
        for failures in (1, 2):
            host = _Host()
            host.holder_failures = failures
            admission = host.admission()
            with self.assertRaises(_t.ContractError) as error:
                admission.admit(_source())
            self.assertIs(error.exception.reason, _t.Reason.UNKNOWN)
            self.assertEqual((host.inputs, host.dispatches), (0, 0))
            if failures == 2:
                self.assertEqual((len(host.grants), len(admission.outstanding())), (1, 1))
                exact = host.grants[0]
                admission.finish(admission.outstanding()[0])
                released, amount = host.releases[id(exact)]
                self.assertIs(released, exact)
                proof = next(p for p, holder, grant in host.proofs.values() if grant is exact)
                self.assertIs(proof.released_tokens[0], exact.reservation)
            self.assertEqual((host.grants, admission.outstanding(), len(host.releases)), ([], (), 1))
            session = admission.admit(_source())
            admission.finish(session)
            self.assertEqual((host.inputs, host.dispatches, host.grants,
                              admission.outstanding(), len(host.releases)), (0, 1, [], (), 2))
        host = _Host()
        factory = host.holder_factory
        interrupted = []
        def interrupted_factory(*args):
            retained = factory(*args)
            if not interrupted:
                interrupted.append(retained)
                raise RuntimeError('interrupted after mint')
            self.assertIs(retained, interrupted[0])
            return retained
        admission = host.admission(holder_factory=interrupted_factory)
        with self.assertRaises(_t.ContractError) as error:
            admission.admit(_source())
        self.assertIs(error.exception.reason, _t.Reason.UNKNOWN)
        self.assertEqual((host.inputs, host.dispatches, host.grants,
                          admission.outstanding(), len(host.retained_holders)), (0, 0, [], (), 1))
        recovery = host.admission()
        session = recovery.admit(_source())
        recovery.finish(session)
        self.assertEqual((host.inputs, host.dispatches, host.grants), (0, 1, []))

    # @anchor:wa.tests.scripts.test_header.HeaderControls.test_deadline_cleanup_allowance
    def test_deadline_cleanup_allowance(self):
        for tighter in ('whole-run', 'request', 'grant', 'header', 'parser'):
            host = _Host()
            host.now = 100
            declarations = _declarations()
            declarations[1][1]['limits']['absolute_deadline_ns'] = 10000
            absolute = 10000
            if tighter == 'whole-run':
                absolute = 500
            elif tighter == 'grant':
                host.grant_deadline = 500
            elif tighter == 'header':
                declarations[1][1]['limits']['absolute_deadline_ns'] = 500
            elif tighter == 'parser':
                # Explicit larger authority lets the parser allocation win.
                absolute = 2000000000
                declarations[1][1]['limits']['absolute_deadline_ns'] = absolute
            admission = host.admission(absolute_deadline_ns=absolute)
            if tighter == 'request':
                reserve = host.reserve
                def shortened(request, policy):
                    object.__setattr__(request, 'absolute_deadline_ns', 500)
                    return reserve(request, policy)
                host.reserve = shortened
            session = admission.admit(_source(declarations))
            expected = 1000000100 if tighter == 'parser' else 470
            self.assertEqual(host.parser_deadlines, [expected])
            admission.finish(session)
            self.assertEqual((host.inputs, host.dispatches, host.grants), (0, 1, []))
        for tighter in ('whole-run', 'request', 'grant', 'header'):
            host = _Host()
            host.now = 100
            declarations = _declarations()
            declarations[1][1]['limits']['absolute_deadline_ns'] = 10000
            absolute = 120 if tighter == 'whole-run' else 10000
            if tighter == 'grant':
                host.grant_deadline = 120
            if tighter == 'header':
                declarations[1][1]['limits']['absolute_deadline_ns'] = 120
            if tighter == 'request':
                reserve = host.reserve
                def shortened(request, policy):
                    object.__setattr__(request, 'absolute_deadline_ns', 120)
                    return reserve(request, policy)
                host.reserve = shortened
            admission = host.admission(absolute_deadline_ns=absolute)
            with self.assertRaises(_t.ContractError) as error:
                admission.admit(_source(declarations))
            self.assertIs(error.exception.reason, _t.Reason.DEADLINE)
            self.assertEqual((host.inputs, host.dispatches, host.grants,
                              admission.outstanding()), (0, 0, [], ()))
            host.now = 0
            host.grant_deadline = None
            host.reserve = _Host.reserve.__get__(host)
            recovery = host.admission()
            session = recovery.admit(_source())
            recovery.finish(session)
            self.assertEqual((host.inputs, host.dispatches, host.grants,
                              recovery.outstanding()), (0, 1, [], ()))

    # @anchor:wa.tests.scripts.test_header.HeaderControls.test_cross_holder_and_superseded_finish
    def test_cross_holder_and_superseded_finish(self):
        host = _Host()
        admission = host.admission()
        first, second = admission.admit(_source()), admission.admit(_source())
        a, b = (admission._sessions[id(s)][1] for s in (first, second))
        first_proof = host.request(a.holder)
        old_second = host.request(b.holder)
        current_second = host.request(b.holder)
        for proof, reason in ((first_proof, _t.Reason.FOREIGN), (old_second, _t.Reason.STALE)):
            with self.assertRaises(_t.ContractError) as error:
                admission.finish(second, proof)
            self.assertIs(error.exception.reason, reason)
            self.assertEqual((host.inputs, host.dispatches, len(host.grants),
                              len(admission.outstanding()), len(host.releases)), (0, 2, 2, 2, 0))
        admission.finish(second, current_second)
        self.assertEqual((len(host.grants), len(admission.outstanding())), (1, 1))
        admission.finish(first, first_proof)
        self.assertEqual((host.grants, admission.outstanding(), len(host.releases)), ([], (), 2))

    # @anchor:wa.tests.scripts.test_header.HeaderControls.test_g_pin_domains
    def test_g_pin_domains(self):
        vectors = {'source': '56c1654f5b4d54abd45483b3c7d57beb838912272acce068fd0f3ae24ce6ea46',
            'plan': 'a415e8aa5d4d0bee3d3d0ae9a20a800f922f1af4c5f2c8d21b1734047d98bbc1',
            'metadata': 'c92adf694faf13d2f134ebc66f7eecf0bb3d1179b828b7dc9aa97f4648eda9ca',
            'artifact': '3667dcb2d851aeeb4ab592da7afe90e6c49ee3406d9c9587454ae2698356e858',
            'screen': '8656ce4530398047dc2fbe3c5587fbb97c178cf23820a3679362fb06966ec9b3'}
        for domain, expected in vectors.items():
            self.assertEqual(_h._hash(_t.PlanDigest, 'wender-' + domain + '/v1', b'{}').full_digest, expected)
            self.assertNotEqual(_hashlib.sha256(('wender-' + domain + '/v1').encode() + b'{}').hexdigest(), expected)
        for value in ('a' * 63, 'A' * 64, 'placeholder'):
            with self.assertRaises(_t.ContractError):
                _t.PlanDigest('sha256', value)

    def _definition(self):
        fixture = _fixture()
        old = fixture['plan']
        # Paired complete S0 plan with production ABI and explicit draft pins.
        h = _dc.replace(old.definition.header, abis=_h._ABIS, supplied_pins=None,
                        pins_reason=_t.Reason.MISSING, limits=_dc.replace(old.limits, canonical_bytes=262144))
        identity = _dc.replace(old.identity, abis=h.abis)
        definition = _dc.replace(old.definition, header=h, identity=identity, limits=h.limits)
        declarations = _declarations()
        for index, (tag, value) in enumerate(declarations):
            if tag not in ('MUTANT_PROOF', 'META:args'):
                declarations[index] = (tag, {name: _wire_record(getattr(h, name)) for name in _h._SCHEMA[tag]})
        host = _Host()
        host.scope = h.scope
        admission = host.admission()
        session = admission.admit(_source(declarations))
        self.addCleanup(admission.finish, session)
        return old, definition, host, admission, session

    # @anchor:wa.tests.scripts.test_header.HeaderControls.test_g_pin_mutations
    def test_g_pin_mutations(self):
        old, definition, host, admission, session = self._definition()
        canonical, identity = _h.recompute_identity(definition, session=session, admission=admission, screen_check=lambda d: _TRUE)
        # Independent P and hash oracle: Q removes only the frozen authority fields.
        def project(v):
            if _dc.is_dataclass(v):
                omit = {_t.ScopeIdentity: {'attestation', 'attestation_reason'},
                    _t.TargetSelector: {'label_export_receipt'}, _t.TerminalGoalRef: {'host_handle'}}.get(type(v), set())
                return {f.name: project(getattr(v, f.name)) for f in _dc.fields(v) if f.name not in omit}
            if isinstance(v, _enum.Enum):
                return v.value
            if type(v) is tuple:
                return [project(i) for i in v]
            return v
        expected = _json.dumps({n: project(getattr(definition, n)) for n in (
            'nodes','selectors','symbols','slots','entry','terminal_goal','limits','queries')},
            sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()
        self.assertEqual(canonical, expected)
        self.assertEqual(identity.plan.full_digest, _hashlib.sha256(b'wender-plan/v1\0' + expected).hexdigest())
        for cls_name in ('_source', '_parser', '_slots'):
            original = getattr(admission, cls_name)
            for field in _dc.fields(original):
                if field.name == 'supervisor_reference':
                    continue
                value = getattr(original, field.name)
                # Tighten rather than violate inherited ceilings.
                setattr(admission, cls_name, _dc.replace(original, **{field.name: value - 1}))
                changed = _h.recompute_identity(definition, session=session, admission=admission, screen_check=lambda d: _TRUE)[1]
                self.assertNotEqual(changed.artifact, identity.artifact)
            setattr(admission, cls_name, original)
        with self.assertRaises(_t.ContractError) as error:
            _h.recompute_identity(definition, session=session, admission=admission, screen_check=None)
        self.assertIs(error.exception.reason, _t.Reason.MISSING)

    # @anchor:wa.tests.scripts.test_header.HeaderControls.test_g_pin_registry
    def test_g_pin_registry(self):
        old, definition, host, admission, session = self._definition()
        canonical, identity = _h.recompute_identity(definition, session=session, admission=admission, screen_check=lambda d: _TRUE)
        definition = _dc.replace(definition, identity=identity, source_digest=identity.source_digest)
        plan = _dc.replace(old, definition=definition, identity=identity, limits=definition.limits,
                           abis=identity.abis, canonical_bytes=canonical)
        host.retained_plan, host.compiler = plan, plan.compiler_handle
        args = dict(compiler=host.compiler, registry=host, session=session,
                    admission=admission, screen_check=lambda d: _TRUE)
        self.assertIs(_h.verify_registered_plan(plan, **args), plan)
        with self.assertRaises(_t.ContractError) as error:
            _h.verify_registered_plan(_dc.replace(plan), **args)
        self.assertIs(error.exception.reason, _t.Reason.FOREIGN)
        with self.assertRaises(_t.ContractError) as error:
            _h.verify_registered_plan(plan, **dict(args, compiler=_dc.replace(host.compiler)))
        self.assertIs(error.exception.reason, _t.Reason.FOREIGN)
        with self.assertRaises(_t.ContractError) as error:
            _h.verify_registered_plan(_dc.replace(plan, canonical_bytes=b'{}'), **args)
        self.assertIs(error.exception.reason, _t.Reason.STALE)

    # @anchor:wa.tests.scripts.test_header.HeaderControls.test_retained_pin_mutations
    def test_retained_pin_mutations(self):
        old, definition, host, admission, session = self._definition()
        canonical, identity = _h.recompute_identity(definition, session=session,
            admission=admission, screen_check=lambda d: _TRUE)
        pinned = _dc.replace(session._header, supplied_pins=identity, pins_reason=None)
        object.__setattr__(session, '_header', pinned)
        definition = _dc.replace(definition, header=pinned, identity=identity,
                                 source_digest=identity.source_digest)
        plan = _dc.replace(old, definition=definition, identity=identity, limits=definition.limits,
                           abis=identity.abis, canonical_bytes=canonical)
        host.retained_plan, host.compiler = plan, plan.compiler_handle
        args = dict(compiler=host.compiler, registry=host, session=session,
                    admission=admission, screen_check=lambda d: _TRUE)
        self.assertIs(_h.verify_registered_plan(plan, **args), plan)
        slot = _t.ArgSpec('query', _t.ValueKind.TEXT, True, 0, 16, _t.Sensitivity.PRIVATE,
                         _t.Purpose.INPUT, 'input', 'composer', 'wender-args/verbatim-v1')
        # Corrupt retained records deliberately, bypassing constructor checks;
        # old exact pins remain installed for every independent mutation.
        cases = [
            (definition, 'selectors', (_dc.replace(definition.selectors[0],
                canonical_label_reference='changed-selector'),)),
            (definition, 'slots', (slot,)),
            (definition, 'limits', _dc.replace(definition.limits, max_acquisitions=3)),
            (pinned, 'abis', _dc.replace(pinned.abis, compiler='changed-compiler')),
            (session, '_body', session._body + b'\n'),
            (session, '_auxiliary', session._auxiliary + (('META:uso', 'changed metadata'),)),
            (pinned, 'metadata_references', ('changed-reference',)),
        ]
        for target, name, changed in cases:
            original = getattr(target, name)
            with self.subTest(field=name):
                object.__setattr__(target, name, changed)
                try:
                    with self.assertRaises(_t.ContractError) as error:
                        _h.verify_registered_plan(plan, **args)
                    self.assertIs(error.exception.reason, _t.Reason.STALE)
                    self.assertIs(pinned.supplied_pins, identity)
                    self.assertEqual((host.inputs, host.dispatches, len(host.grants),
                                      len(admission.outstanding())), (0, 1, 1, 1))
                finally:
                    object.__setattr__(target, name, original)
                self.assertIs(_h.verify_registered_plan(plan, **args), plan)
        # Each complete ceiling independently invalidates the old retained pins.
        for policy_name in ('_source', '_parser', '_slots'):
            original = getattr(admission, policy_name)
            for field in _dc.fields(original):
                if field.name == 'supervisor_reference':
                    continue
                with self.subTest(policy=policy_name, field=field.name):
                    setattr(admission, policy_name, _dc.replace(original,
                        **{field.name: getattr(original, field.name) - 1}))
                    try:
                        with self.assertRaises(_t.ContractError) as error:
                            _h.verify_registered_plan(plan, **args)
                        self.assertIs(error.exception.reason, _t.Reason.STALE)
                        self.assertEqual((host.inputs, host.dispatches, len(host.grants)), (0, 1, 1))
                    finally:
                        setattr(admission, policy_name, original)
                    self.assertIs(_h.verify_registered_plan(plan, **args), plan)

    # @anchor:wa.tests.scripts.test_header.HeaderControls.test_supplied_pin_fields
    def test_supplied_pin_fields(self):
        _, definition, host, admission, session = self._definition()
        args = dict(session=session, admission=admission, screen_check=lambda d: _TRUE)
        canonical, identity = _h.recompute_identity(definition, **args)
        pinned = _dc.replace(session._header, supplied_pins=identity, pins_reason=None)
        object.__setattr__(session, '_header', pinned)
        final = _dc.replace(definition, header=pinned, identity=identity,
                            source_digest=identity.source_digest)
        self.assertEqual(_h.verify_pins(final, **args), (identity, canonical))
        for field in _dc.fields(identity):
            value = getattr(identity, field.name)
            if type(value) is str:
                changed = value + '-stale'
            elif type(value) is int:
                changed = value + 1
            elif isinstance(value, (_t.PlanDigest, _t.MetadataDigest, _t.ArtifactDigest)):
                changed = _dc.replace(value, full_digest='0' * 64)
            elif type(value) is _t.ABISet:
                changed = _dc.replace(value, compiler='changed-compiler')
            else:
                self.assertIs(type(value), _t.ScopeIdentity)
                changed = _dc.replace(value, principal='changed-principal')
            stale = _copy.copy(identity)
            object.__setattr__(stale, field.name, changed)
            with self.subTest(field=field.name):
                object.__setattr__(pinned, 'supplied_pins', stale)
                try:
                    with self.assertRaises(_t.ContractError) as error:
                        _h.verify_pins(final, **args)
                    self.assertIs(error.exception.reason, _t.Reason.STALE)
                    self.assertIsNone(host.retained_plan)
                    self.assertEqual((host.inputs, host.dispatches), (0, 1))
                finally:
                    object.__setattr__(pinned, 'supplied_pins', identity)
                self.assertEqual(_h.verify_pins(final, **args), (identity, canonical))

    # @anchor:wa.tests.scripts.test_header.HeaderControls.test_recompute_bounded_encoding
    def test_recompute_bounded_encoding(self):
        old, definition, host, admission, session = self._definition()
        # A large selector is passive retained input; no oversized source decode
        # or constructor rejection can substitute for the public identity seam.
        selector = _copy.copy(definition.selectors[0])
        candidate = _copy.copy(definition)
        object.__setattr__(candidate, 'selectors', (selector,))
        original_grant = host.grants[0]
        for count, limit, reason in ((2100000, 262144, _t.Reason.BOUNDS),
                                     (180000, 262144, _t.Reason.CAPACITY)):
            object.__setattr__(selector, 'canonical_label_reference', 'x' * count)
            object.__setattr__(candidate, 'limits', _dc.replace(definition.limits, canonical_bytes=limit))
            with _mock.patch.object(_h, 'bytearray', create=True,
                                    side_effect=AssertionError('complete output allocated')):
                with self.assertRaises(_t.ContractError) as error:
                    _h.recompute_identity(candidate, session=session, admission=admission,
                                           screen_check=lambda d: _TRUE)
            self.assertIs(error.exception.reason, reason)
            self.assertIs(host.grants[0], original_grant)
            self.assertIs(admission._sessions[id(session)][1].grant, original_grant)
            self.assertEqual((host.inputs, host.dispatches, len(host.grants),
                              len(admission.outstanding())), (0, 1, 1, 1))
        self.assertTrue(host.growth_requests)
        self.assertIs(host.growth_requests[-1][0], original_grant)
        # Same data succeeds only after the retained owner grants the extra peak.
        host.allow_growth = True
        canonical, identity = _h.recompute_identity(candidate, session=session,
            admission=admission, screen_check=lambda d: _TRUE)
        self.assertGreater(len(canonical), 180000)
        self.assertIsNot(host.grants[0], original_grant)
        self.assertIs(host.grants[0].reservation, original_grant.reservation)
        self.assertGreater(host.grants[0].charged_bytes, original_grant.charged_bytes)
        admission.finish(session)
        self.assertEqual((host.grants, admission.outstanding(), len(host.releases)), ([], (), 1))

    # @anchor:wa.tests.scripts.test_header.HeaderControls.test_ast_object_boundaries
    def test_ast_object_boundaries(self):
        # Independent AST-object oracle: root-zero edges, including operators.
        bodies = (b'pass\n', b'x = ' + b'-' * 6 + b'1\n',
                  b'x = ' + b'-' * 10 + b'1\n')
        for body in bodies:
            tree = _ast.parse(body)
            def freeze(node):
                return (type(node).__name__, tuple(freeze(c) for c in _ast.iter_child_nodes(node)))
            pending, count, depth = [(tree, 0)], 0, 0
            while pending:
                node, edge = pending.pop()
                count += 1
                depth = max(depth, edge)
                pending.extend((c, edge + 1) for c in _ast.iter_child_nodes(node))
            cases = [(count, min(depth, 8), depth <= 8),
                     (count - 1, min(depth, 8), False)]
            if depth > 1:
                cases.append((count, min(depth - 1, 8), False))
            for nodes, edges, accepted in cases:
                host = _Host()
                host.parser = _dc.replace(host.parser, ast_nodes=nodes, ast_depth=edges)
                host.wire = _json.dumps(freeze(tree)).encode()
                admission = host.admission()
                if accepted:
                    session = admission.admit(_source(body=body))
                    self.assertEqual(session._result, freeze(tree))
                    admission.finish(session)
                else:
                    with self.assertRaises(_t.ContractError) as error:
                        admission.admit(_source(body=body))
                    self.assertIs(error.exception.reason, _t.Reason.BOUNDS)
                self.assertEqual((host.inputs, host.dispatches, host.grants,
                                  admission.outstanding()), (0, 1, [], ()))
                self.assertEqual(len(host.releases), 1)
                # Exact-token release is authenticated by the retained owner.
                released = next(iter(host.releases.values()))[0]
                self.assertTrue(any(row[2] is released for row in host.proofs.values()))
                host.parser = _dc.replace(host.parser, ast_nodes=2, ast_depth=1)
                host.wire = b'["Module",[["Pass",[]]]]'
                recovered = admission = host.admission()
                session = recovered.admit(_source(body=b'pass\n'))
                recovered.finish(session)
                self.assertEqual((host.inputs, host.dispatches, host.grants,
                                  recovered.outstanding()), (0, 2, [], ()))

    # @anchor:wa.tests.scripts.test_header.HeaderControls.test_argument_bounded_allocation
    def test_argument_bounded_allocation(self):
        host = _Host()
        spec = _t.ArgSpec('query', _t.ValueKind.TEXT, True, 0, 4096,
            _t.Sensitivity.PRIVATE, _t.Purpose.INPUT, 'input', 'composer',
            'wender-args/verbatim-v1')
        slot = _t.SlotDescriptor('query', spec.kind, spec.purpose, host.owner,
                               'episode', host.scope, 'composer', 'input', None)
        args = dict(scope=host.scope, episode_id='episode', slots=(slot,),
                    slot_check=lambda *a: _TRUE, vocabulary_check=None, slot_policy=host.slots)
        for text in ('x' * 1050000, 'é' * 4096):
            values = {'query': text}
            _tracemalloc.start()
            try:
                with self.assertRaises(_t.ContractError) as error:
                    _h.validate_arguments((spec,), values, **args)
                peak = _tracemalloc.get_traced_memory()[1]
            finally:
                _tracemalloc.stop()
            self.assertIs(error.exception.reason, _t.Reason.BOUNDS)
            self.assertLess(peak, 65536)
        text = 'é' * 2048
        self.assertEqual(_h.validate_arguments((spec,), {'query': text}, **args),
                         (('query', text),))
        self.assertEqual((host.inputs, host.dispatches, host.grants), (0, 0, []))

    # @anchor:wa.tests.scripts.test_header.HeaderControls.test_g_args_verbatim
    def test_g_args_verbatim(self):
        host = _Host()
        spec = _t.ArgSpec('query', _t.ValueKind.TEXT, True, 0, 16, _t.Sensitivity.PRIVATE,
                         _t.Purpose.INPUT, 'input', 'composer', 'wender-args/verbatim-v1')
        slot = _t.SlotDescriptor('query', spec.kind, spec.purpose, host.owner,
                                 'episode', host.scope, 'composer', 'input', None)
        args = dict(scope=host.scope, episode_id='episode', slots=(slot,),
                    slot_check=lambda *a: _TRUE, vocabulary_check=None, slot_policy=host.slots)
        for text in (' e\u0301 ', ''):
            self.assertEqual(_h.validate_arguments((spec,), {'query': text}, **args), (('query', text),))
        for values, expected in (({'query': None}, _t.Reason.TYPE), ({}, _t.Reason.MISSING),
                                  ({'other': 'x'}, _t.Reason.SCHEMA)):
            with self.assertRaises(_t.ContractError) as error:
                _h.validate_arguments((spec,), values, **args)
            self.assertIs(error.exception.reason, expected)
        with self.assertRaises(_t.ContractError) as error:
            _h.validate_arguments((spec,), {'query':'x'}, **dict(args, slots=()))
        self.assertIs(error.exception.reason, _t.Reason.MISSING)
        integer = _dc.replace(spec, kind=_t.ValueKind.INTEGER, minimum=-10)
        args['slots'] = (_dc.replace(slot, kind=_t.ValueKind.INTEGER),)
        self.assertEqual(_h.validate_arguments((integer,), {'query':7}, **args), (('query',7),))
        with self.assertRaises(_t.ContractError) as error:
            _h.validate_arguments((integer,), {'query':True}, **args)
        self.assertIs(error.exception.reason, _t.Reason.TYPE)
        optional = _dc.replace(spec, required=False)
        with self.assertRaises(_t.ContractError) as error:
            _h.validate_arguments((optional, _dc.replace(optional, name='QUERY')), {},
                                  **dict(args, slots=()))
        self.assertIs(error.exception.reason, _t.Reason.SCHEMA)
        self.assertEqual(_h.validate_arguments((optional,), {}, **dict(args, slots=())), ())
        self.assertEqual(host.inputs, 0)

    # @anchor:wa.tests.scripts.test_header.HeaderControls.test_g_privacy_passive_metadata
    def test_g_privacy_passive_metadata(self):
        canary = 'PRIVATE_CANARY_<retain-as-data>'
        host, admission, session = self._admit(_source(_declarations() + [('META:uso', canary)], body=('# ' + canary).encode()))
        self.assertNotIn(canary, repr(session))
        metadata = _h.canonical_metadata(session._header, session._auxiliary, session=session, admission=admission)
        self.assertIn(canary.encode(), metadata)
        host = _Host()
        host.failure = ValueError(canary)
        self._reject(_source(), _t.Reason.UNKNOWN, 1, host)
        host = _Host()
        host.wire = b'"' + canary.encode() + b'"'
        self._reject(_source(), _t.Reason.TYPE, 1, host)
        self._reject(_source().replace(b'"example"', ('"' + canary).encode()), _t.Reason.SCHEMA)

    # @anchor:wa.tests.scripts.test_header.HeaderControls.test_g_anchor_scope
    def test_g_anchor_scope(self):
        root = _Path(__file__).resolve().parents[2]
        owned = ('wender/scripts/header.py', 'tests/scripts/test_header.py')
        missing = [r for r in _index.check_missing_anchors(root) if r['file'] in owned]
        self.assertEqual(missing, [])
        with _mock.patch.object(_index, 'SRC_GLOBS', _index.SRC_GLOBS + ('tests/scripts/test_header.py',)):
            index = _index.build_memory_index(root)
        rows = [r for r in index['anchors'] if r['file'] in owned]
        self.assertEqual({r['file'] for r in rows}, set(owned))
        self.assertTrue(rows)
        self.assertEqual(len({r['id'] for r in rows}), len(rows))
        self.assertTrue(all(r['stage'] == 'HB-WA-s4-learn' for r in rows))
        for name in owned:
            path = root / name
            self.assertFalse(path.is_symlink())
            self.assertEqual(path.resolve(), root / name)
            source = path.read_text()
            _ast.parse(source)
            module = _json.loads(source.splitlines()[0].split('@module ', 1)[1])
            self.assertEqual((module['owner'], module['book']), ('LEARN', 'HB-WA-s4-learn'))
        # Calibration: an unanchored public symbol really is rejected.
        source = (root / owned[0]).read_text() + '\ndef unanchored(): pass\n'
        self.assertTrue(_index._missing_in_source(owned[0], source))
        with _tempfile.TemporaryDirectory() as temp:
            out = _Path(temp) / 'index'
            out.mkdir()
            for name, raw in index['files'].items():
                (out / name).write_bytes(raw)
            with _mock.patch.object(_index, 'SRC_GLOBS', _index.SRC_GLOBS + ('tests/scripts/test_header.py',)):
                loaded = _router.load_index(root, out)
            for row in rows:
                self.assertIn(row['file'], str(_router.lookup_anchor(loaded, row['id'])))
        manifest = {name: _json.loads((root / name).read_text().splitlines()[0].split('@module ',1)[1])['id']
                    for name in owned}
        self.assertTrue(_routing(root, manifest, owned))
        with self.assertRaises(_t.ContractError) as error:
            _routing(root, manifest, (*owned, 'not-owned.py'))
        self.assertIs(error.exception.reason, _t.Reason.FOREIGN)
        with _tempfile.TemporaryDirectory() as temp:
            base = _Path(temp)
            path = base / 'wender/scripts/header.py'
            path.parent.mkdir(parents=True)
            original = (root / owned[0]).read_text()
            one = {owned[0]: 'wa.scripts.header'}
            for mutation, reason in ((original + '\ndef unanchored(): pass\n', _t.Reason.MISSING),
                (original.replace('"owner":"LEARN"','"owner":"ACT"'), _t.Reason.SCHEMA),
                (original.replace('"book":"HB-WA-s4-learn"','"book":"HB-WA-s1-perceive"'), _t.Reason.SCHEMA),
                (original + '\n# @anchor:wa.scripts.header.parse_header\n', _t.Reason.SCHEMA)):
                path.write_text(mutation)
                with self.assertRaises(_t.ContractError) as error:
                    _routing(base, one, (owned[0],))
                self.assertIs(error.exception.reason, reason)
            path.unlink()
            path.symlink_to(root / owned[0])
            with self.assertRaises(_t.ContractError) as error:
                _routing(base, one, (owned[0],))
            self.assertIs(error.exception.reason, _t.Reason.FOREIGN)

    # @anchor:wa.tests.scripts.test_header.HeaderControls.test_source_boundaries_and_arguments_abi
    def test_source_boundaries_and_arguments_abi(self):
        host = _Host()
        host.source = _dc.replace(host.source, header_lines=7)
        self._reject(_source(), _t.Reason.BOUNDS, host=host)
        for body in (b'# [SCRIPT_TAG] {}\n', b'pass\n  # [META:uso] "x"\n'):
            self._reject(_source(body=body), _t.Reason.SCHEMA)
        host = _Host()
        # Exact raw-byte boundary and exact Unicode-scalar boundary both pass.
        raw = _source(body='é'.encode())
        host.source = _t.SourcePolicy(len(raw.decode()), len(raw) - 1, 8)
        self._reject(raw, _t.Reason.BOUNDS, host=host)
        self.assertEqual(host.events, ['charge'])
        host = _Host()
        host.source = _t.SourcePolicy(len(raw.decode()), len(raw), 8)
        admission = host.admission()
        session = admission.admit(raw)
        admission.finish(session)
        spec = _t.ArgSpec('query', _t.ValueKind.TEXT, True, 1, 16, _t.Sensitivity.PRIVATE,
                         _t.Purpose.INPUT, 'input', 'composer', 'wender-args/verbatim-v1')
        draft = _declarations()
        draft[-1] = ('META:args', [_wire_record(spec)])
        host, admission, session = self._admit(_source(draft))
        expected = b'{"capability_contract":"input","destination_contract":"composer","kind":"TEXT","maximum":16,"minimum":1,"name":"query","normalization_abi":"wender-args/verbatim-v1","purpose":"INPUT","required":true,"sensitivity":"PRIVATE"}'
        self.assertEqual(_h._canonical(session._header.arguments[0], admission, session), expected)
        draft[-1][1][0]['normalization_abi'] = 'unsupported'
        self._reject(_source(draft), _t.Reason.SCHEMA)
        draft[-1][1][0]['normalization_abi'] = spec.normalization_abi
        del draft[-1][1][0]['normalization_abi']
        self._reject(_source(draft), _t.Reason.SCHEMA)

    # @anchor:wa.tests.scripts.test_header.HeaderControls.test_reservation_order_overlap_and_cleanup_seams
    def test_reservation_order_overlap_and_cleanup_seams(self):
        host = _Host()
        admission = host.admission(telemetry=lambda: None)
        with self.assertRaises(_t.ContractError) as error:
            admission.admit(_source())
        self.assertIs(error.exception.reason, _t.Reason.MISSING)
        self.assertEqual((host.grants, host.dispatches, host.inputs), ([], 0, 0))
        host = _Host()
        admission = host.admission()
        decode = _h._json_load
        def record(*args):
            host.events.append('decode-header')
            return decode(*args)
        with _mock.patch.object(_h, '_json_load', record):
            session = admission.admit(_source())
        self.addCleanup(admission.finish, session)
        self.assertLess(host.events.index('reserve'), host.events.index('decode-header'))
        host.used = host.policy.host_bytes - host.peak
        second = host.admission()
        with self.assertRaises(_t.ContractError) as error:
            second.admit(_source())
        self.assertIs(error.exception.reason, _t.Reason.CAPACITY)
        self.assertEqual((len(host.grants), host.dispatches, host.inputs), (1, 1, 0))
        self.assertEqual(second.outstanding(), ())
        self.assertEqual(admission.outstanding(), (session,))
        admission.finish(session)
        host.used = None
        next_session = second.admit(_source())
        second.finish(next_session)
        self.assertEqual((host.grants, second.outstanding()), ([], ()))
        # Authenticated incomplete cleanup retains actual outstanding owners.
        host = _Host()
        host.drain = False
        host.failure = _t.ContractError(_t.Reason.CANCELLED)
        admission = host.admission()
        with self.assertRaises(_t.ContractError) as error:
            admission.admit(_source())
        self.assertIs(error.exception.reason, _t.Reason.FOREIGN)
        self.assertEqual((len(host.grants), len(admission.outstanding()), host.dispatches, host.inputs), (1,1,1,0))
        host.drain = True
        admission.finish(admission.outstanding()[0])
        self.assertEqual(host.grants, [])
        host.failure = None
        host.fault_after_parse = lambda: setattr(host, 'now', 1001)
        self._reject(_source(), _t.Reason.DEADLINE, 2, host)

    # @anchor:wa.tests.scripts.test_header.HeaderControls.test_key_boolean_and_retained_slot_custody
    def test_key_boolean_and_retained_slot_custody(self):
        host = _Host()
        spec = _t.ArgSpec('key', _t.ValueKind.KEY, True, 1, 16, _t.Sensitivity.PRIVATE,
                         _t.Purpose.INPUT, 'keyboard', 'composer', 'wender-args/verbatim-v1')
        vocabulary = _t.HostKeyVocabulary('host-v1', ('ENTER', 'TAB'), host.owner)
        slot = _t.SlotDescriptor('key', spec.kind, spec.purpose, host.owner,
                                 'episode', host.scope, 'composer', 'keyboard', vocabulary)
        def slot_check(s, *args):
            return _TRUE if s is slot else _t.AdmissionCheck(_t.Truth.FALSE, _t.Reason.FOREIGN)
        def vocab_check(v):
            return _TRUE if v is vocabulary else _t.AdmissionCheck(_t.Truth.FALSE, _t.Reason.FOREIGN)
        args = dict(scope=host.scope, episode_id='episode', slots=(slot,), slot_check=slot_check,
                    vocabulary_check=vocab_check, slot_policy=host.slots)
        self.assertEqual(_h.validate_arguments((spec,), {'key':'ENTER'}, **args), (('key','ENTER'),))
        for value, changes, reason in (('ESC', {}, _t.Reason.FOREIGN),
            ('ENTER', {'vocabulary_check':None}, _t.Reason.MISSING),
            ('ENTER', {'slots':(_dc.replace(slot),)}, _t.Reason.FOREIGN),
            ('ENTER', {'episode_id':'foreign'}, _t.Reason.FOREIGN),
            ('ENTER', {'slots':(_dc.replace(slot, key_vocabulary=_dc.replace(vocabulary)),)}, _t.Reason.FOREIGN),
            ('é', {}, _t.Reason.TYPE), ('', {}, _t.Reason.TYPE)):
            with self.assertRaises(_t.ContractError) as error:
                _h.validate_arguments((spec,), {'key':value}, **dict(args, **changes))
            self.assertIs(error.exception.reason, reason)
        boolean = _dc.replace(spec, name='flag', kind=_t.ValueKind.BOOLEAN, minimum=0, maximum=1)
        flag = _dc.replace(slot, slot_name='flag', kind=_t.ValueKind.BOOLEAN, key_vocabulary=None)
        args = dict(args, slots=(flag,), slot_check=lambda *a: _TRUE)
        self.assertEqual(_h.validate_arguments((boolean,), {'flag':False}, **args), (('flag',False),))
        with self.assertRaises(_t.ContractError) as error:
            _h.validate_arguments((boolean,), {'flag':0}, **args)
        self.assertIs(error.exception.reason, _t.Reason.TYPE)
        optional = _dc.replace(boolean, required=False)
        self.assertEqual(_h.validate_arguments((optional,), {}, **dict(args, slots=())), ())
        for name in ('act', 'params', 'Ｆlag', 'flag-arg'):
            with self.assertRaises(_t.ContractError) as error:
                _h.validate_arguments((_dc.replace(boolean, name=name),), {}, **dict(args, slots=()))
            self.assertIs(error.exception.reason, _t.Reason.SCHEMA)
        self.assertEqual((host.inputs, host.dispatches, host.grants), (0,0,[]))


    # @anchor:wa.tests.scripts.test_header.HeaderControls.test_completed_sessions_do_not_retain_output
    def test_completed_sessions_do_not_retain_output(self):
        host = _Host()
        admission = host.admission()
        session = admission.admit(_source())
        reference = _weakref.ref(session)
        self.assertTrue(admission.outstanding())
        admission.finish(session)
        admission.finish(session)
        self.assertEqual((host.grants, admission.outstanding(), len(admission._sessions)), ([], (), 0))
        with self.assertRaises(_t.ContractError) as error:
            _h.canonical_metadata(session._header, session._auxiliary, session=session, admission=admission)
        self.assertIs(error.exception.reason, _t.Reason.STALE)
        del session
        _gc.collect()
        self.assertIsNone(reference())
        self.assertEqual(len(admission._finished), 0)

    def _local(self):
        host = _ProcessHost()
        # Resolve the actual host library; never guess an architecture path.
        cache = _subprocess.run(['ldconfig', '-p'], check=True,
                                capture_output=True, text=True).stdout
        library = next(line.split(' => ', 1)[1].strip() for line in cache.splitlines()
                       if line.strip().startswith(host.library + ' '))
        backend = _h.LocalParserSupervisor(runtime=_os.path.realpath(_sys.executable),
            seccomp_library=library, probe_path=str(_Path(__file__).resolve()),
            reference=host.parser.supervisor_reference,
            registration=_ref('local-parser'), cleanup_timeout_ns=host.cancel.join_deadline_ns)
        owner_request = host.request
        def _request(holder):
            if backend.outstanding():
                raise _t.ContractError(_t.Reason.MISSING)
            return owner_request(holder)
        host.request = _request
        admission = host.admission(resolver=backend, verifier=backend,
                                   result_decoder=backend.decode)
        return host, backend, admission

    # @anchor:wa.tests.scripts.test_header.HeaderControls.test_local_supervisor_admission
    def test_local_supervisor_admission(self):
        host, backend, admission = self._local()
        # A mere registration is insufficient before verification.
        with self.assertRaises(_t.ContractError) as error:
            backend.check(backend._registration, backend,
                          host.parser.supervisor_reference, host.parser)
        self.assertIs(error.exception.reason, _t.Reason.STALE)
        session = _h.admit_source(host._source(), admission=admission)
        self.assertEqual(session._result[1][0][0], 'FunctionDef')
        self.assertEqual(backend.outstanding(), ())
        self.assertEqual(host.inputs, 0)
        self.assertIs(backend.resolve(host.parser.supervisor_reference), backend)
        self.assertIsNone(backend.resolve('foreign'))
        with self.assertRaises(_t.ContractError) as error:
            backend.check(_dc.replace(backend._registration), backend,
                          host.parser.supervisor_reference, host.parser)
        self.assertIs(error.exception.reason, _t.Reason.FOREIGN)
        self.assertIsNone(error.exception.__context__)
        # An outstanding backend holder prevents the owner from authenticating
        # drain. A failed finish must preserve the original reservation.
        backend._children['borrow'] = object()
        try:
            with self.assertRaises(_t.ContractError) as error:
                admission.finish(session)
            self.assertIs(error.exception.reason, _t.Reason.MISSING)
            self.assertEqual((len(host.grants), len(admission.outstanding())), (1, 1))
        finally:
            del backend._children['borrow']
        admission.finish(session)
        self.assertEqual((host.grants, admission.outstanding(), backend.outstanding()), ([], (), ()))

    # @anchor:wa.tests.scripts.test_header.HeaderControls.test_local_ast_semantics
    def test_local_ast_semantics(self):
        # Predicate: each semantic mutation must survive actual supervisor IPC.
        host, backend, admission = self._local()
        def parse(body):
            admission = host.admission(resolver=backend, verifier=backend,
                                       result_decoder=backend.decode)
            declarations = _declarations()
            declarations[1][1]['limits']['absolute_deadline_ns'] = host.now_ns() + 5000000000
            declarations[1][1]['limits']['canonical_bytes'] = 262144
            session = _h.admit_source(_source(declarations, body=body), admission=admission)
            result = session._result
            admission.finish(session)
            self.assertEqual((host.grants, backend.outstanding(), admission.outstanding()),
                             ([], (), ()))
            return result
        def field(node, name):
            return dict(node[2])[name]
        baseline = b'def run(act, params): return None\n'
        original = parse(baseline)
        self.assertEqual(original, parse(baseline))  # Positive control.
        wrapper = original[1][0]
        self.assertEqual(field(wrapper, 'name'), ('str', 'run'))
        arguments = wrapper[1][0]
        self.assertEqual([field(child, 'arg') for child in arguments[1]],
                         [('str', 'act'), ('str', 'params')])
        self.assertEqual(field(wrapper[1][1][1][0], 'value'), ('none', None))
        for body in (b'def wrong(act, params): return None\n',
                     b'def run(other, names): return None\n',
                     b'def run(act, params): return "private"\n',
                     b'def wrong(other, names): return "private"\n'):
            with self.subTest(body=body):
                self.assertNotEqual(original, parse(body))
        call = b'def run(act, params): act.find(label="safe")\n'
        parsed_call = parse(call)
        expression = parsed_call[1][0][1][1][1][0]
        self.assertEqual(field(expression[1][0], 'attr'), ('str', 'find'))
        self.assertEqual(field(expression[1][1], 'arg'), ('str', 'label'))
        self.assertEqual(field(expression[1][1][1][0], 'value'), ('str', 'safe'))
        for body in (call.replace(b'act.find', b'act.type'),
                     call.replace(b'act.find', b'other.find'),
                     call.replace(b'label=', b'other='),
                     call.replace(b'"safe"', b'"private"')):
            with self.subTest(body=body):
                self.assertNotEqual(parsed_call, parse(body))
        for literal, expected in ((b'False', ('bool', False)), (b'7', ('int', 7)),
                                  (b'b"x"', ('bytes', '78')),
                                  (b'1.5', ('float', '0x1.8000000000000p+0')),
                                  (b'2j', ('complex', ('0x0.0p+0', '0x1.0000000000000p+1'))),
                                  (b'...', ('ellipsis', None))):
            tree = parse(b'def run(act, params): return ' + literal + b'\n')
            self.assertEqual(field(tree[1][0][1][1][1][0], 'value'), expected)

    # @anchor:wa.tests.scripts.test_header.HeaderControls.test_local_transport_independent_of_nodes
    def test_local_transport_independent_of_nodes(self):
        # Predicate: scalar byte length cannot consume the AST object budget.
        host, backend, _ = self._local()
        short = b'def run(act, params): act.find(label="safe")\n'
        long = short.replace(b'safe', b'a' * 3900)
        results = []
        for nodes, body, ceiling, reason in (
                (32, short, 262144, None),
                (32, long, 262144, None),
                (1024, long, 262144, None),
                (8, short, 262144, _t.Reason.BOUNDS),
                (32, long, 1024, _t.Reason.BOUNDS)):
            with self.subTest(nodes=nodes, length=len(body), ceiling=ceiling):
                host.parser = _dc.replace(host.parser, ast_nodes=nodes)
                admission = host.admission(resolver=backend, verifier=backend,
                                           result_decoder=backend.decode)
                declarations = _declarations()
                declarations[1][1]['limits']['absolute_deadline_ns'] = host.now_ns() + 5000000000
                declarations[1][1]['limits']['canonical_bytes'] = ceiling
                raw = _source(declarations, body)
                if reason is None:
                    session = _h.admit_source(raw, admission=admission)
                    results.append(session._result)
                    admission.finish(session)
                else:
                    with self.assertRaises(_t.ContractError) as error:
                        _h.admit_source(raw, admission=admission)
                    self.assertIs(error.exception.reason, reason)
                self.assertEqual((host.grants, backend.outstanding(), admission.outstanding()),
                                 ([], (), ()))
        self.assertNotEqual(results[0], results[1])
        self.assertEqual(results[1], results[2])
        expression = results[1][1][0][1][1][1][0]
        self.assertEqual(dict(expression[1][1][1][0][2])['value'], ('str', 'a' * 3900))

    # @anchor:wa.tests.scripts.test_header.HeaderControls.test_local_ast_closed_fields
    def test_local_ast_closed_fields(self):
        host, backend, admission = self._local()
        valid = ['Module', [['Pass', [], []]],
                 [['body', ['list', [['node', 0]]]], ['type_ignores', ['list', []]]]]
        accepted = backend.decode(_json.dumps(valid).encode(), host.parser)
        self.assertEqual(accepted[1], (('Pass', (), ()),))
        mutants = []
        for kind in ('unknown', 'missing', 'duplicate', 'reorder', 'index', 'bool-index',
                     'unreferenced', 'scalar', 'nested-list', 'old-lossy'):
            value = _copy.deepcopy(valid)
            if kind == 'unknown':
                value[0] = 'UnknownNode'
            elif kind == 'missing':
                value[2].pop()
            elif kind == 'duplicate':
                value[2][1][0] = 'body'
            elif kind == 'reorder':
                value[2].reverse()
            elif kind in ('index', 'bool-index'):
                value[2][0][1][1][0][1] = 1 if kind == 'index' else False
            elif kind == 'unreferenced':
                value[2][0][1][1] = []
            elif kind == 'scalar':
                value[2][0][1][1][0] = ['object', 'private']
            elif kind == 'nested-list':
                value[2][0][1][1][0] = ['list', []]
            else:
                value = ['Module', [['Pass', []]]]
            mutants.append((kind, value))
        for kind, value in mutants:
            with self.subTest(kind=kind), self.assertRaises(_t.ContractError):
                backend.decode(_json.dumps(value).encode(), host.parser)
        self.assertEqual(backend.decode(_json.dumps(valid).encode(), host.parser), accepted)

    # @anchor:wa.tests.scripts.test_header.HeaderControls.test_local_supervisor_denial_mutant
    def test_local_supervisor_denial_mutant(self):
        host, backend, admission = self._local()
        # This mutation allows socket creation; actual syscall probes must fail
        # verification before any candidate parse, with all custody released.
        allow = "assert lib.seccomp_rule_add(ctx, 0x7fff0000, lib.seccomp_syscall_resolve_name(b'socket'), 0) == 0\n"
        mutant = _h._LOCAL_CHILD.replace('assert lib.seccomp_load(ctx) == 0',
                                        allow + 'assert lib.seccomp_load(ctx) == 0')
        with _mock.patch.object(_h, '_LOCAL_CHILD', mutant):
            with self.assertRaises(_t.ContractError) as error:
                admission.admit(host._source())
        self.assertIs(error.exception.reason, _t.Reason.MISSING)
        self.assertEqual((host.grants, admission.outstanding(), backend.outstanding()), ([], (), ()))
        session = admission.admit(host._source())
        self.assertEqual(session._result[0], 'Module')
        admission.finish(session)

    # @anchor:wa.tests.scripts.test_header.HeaderControls.test_local_supervisor_failures_and_cleanup
    def test_local_supervisor_failures_and_cleanup(self):
        host, backend, admission = self._local()
        for body, reason in ((b'def broken(', _t.Reason.SCHEMA),
                             (b'x = ' + b'(' * 20 + b'1' + b',)' * 20, _t.Reason.BOUNDS)):
            declarations = _declarations()
            declarations[1][1]['limits']['absolute_deadline_ns'] = host.now_ns() + 5000000000
            with self.assertRaises(_t.ContractError) as error:
                admission.admit(_source(declarations, body))
            self.assertIs(error.exception.reason, reason)
            self.assertIsNone(error.exception.__context__)
            self.assertEqual((host.grants, admission.outstanding(), backend.outstanding()), ([], (), ()))
        # Probe memory/deadline failures in the shipped backend itself, under
        # the same boundary used for accepting safe source.
        for mode, reason in (('stall', _t.Reason.DEADLINE), ('exhaust', _t.Reason.CAPACITY)):
            with self.assertRaises(_t.ContractError) as error:
                _h._private_call(backend._run, mode, b'', host.parser, host.now_ns() + 200000000)
            self.assertIs(error.exception.reason, reason)
            self.assertEqual(backend.outstanding(), ())
        session = admission.admit(host._source())
        admission.finish(session)
        self.assertEqual((host.grants, admission.outstanding(), backend.outstanding()), ([], (), ()))

    # @anchor:wa.tests.scripts.test_header.HeaderControls.test_local_supervisor_bounded_wire
    def test_local_supervisor_bounded_wire(self):
        host, backend, admission = self._local()
        for wire, reason in ((b'[' * 40, _t.Reason.BOUNDS),
                             (b'x' * (262144 + 1), _t.Reason.BOUNDS),
                             (b'["Module",{}]', _t.Reason.TYPE)):
            with self.assertRaises(_t.ContractError) as error:
                backend.decode(wire, host.parser)
            self.assertIs(error.exception.reason, reason)
            self.assertIsNone(error.exception.__context__)
        valid = (b'["Module",[["Pass",[],[]]],[["body",["list",[["node",0]]]],'
                 b'["type_ignores",["list",[]]]]]')
        with self.assertRaises(_t.ContractError) as error:
            backend.decode(valid, host.parser, result_byte_limit=len(valid) - 1)
        self.assertIs(error.exception.reason, _t.Reason.BOUNDS)
        self.assertEqual(backend.decode(valid, host.parser, result_byte_limit=len(valid)),
            ('Module', (('Pass', (), ()),),
             (('body', ('list', (('node', 0),))), ('type_ignores', ('list', ())))))
        session = admission.admit(host._source())
        with _mock.patch.object(backend, '_fingerprint', return_value=()):
            with self.assertRaises(_t.ContractError) as error:
                backend.check(backend._registration, backend,
                              host.parser.supervisor_reference, host.parser)
            self.assertIs(error.exception.reason, _t.Reason.STALE)
        admission.finish(session)

    # @anchor:wa.tests.scripts.test_header.HeaderControls.test_identity_before_plan_and_pin_entrypoint
    def test_identity_before_plan_and_pin_entrypoint(self):
        old, definition, host, admission, session = self._definition()
        components = {name: getattr(definition, name) for name in (
            'nodes', 'selectors', 'symbols', 'slots', 'entry', 'terminal_goal',
            'limits', 'queries', 'structural_screen_digest')}
        args = dict(session=session, admission=admission, screen_check=lambda d: _TRUE)
        expected_bytes, expected_identity = _h.recompute_identity(definition, **args)
        # The new path must never construct a temporary identity-bearing plan.
        with _mock.patch.object(_t, 'PlanDefinition', side_effect=AssertionError('placeholder plan')):
            identity, canonical = _h.canonical_identity(**components, **args)
        self.assertEqual((identity, canonical), (expected_identity, expected_bytes))
        self.assertIsNone(host.retained_plan)
        self.assertIsNone(session._header.supplied_pins)
        with self.assertRaises(_t.ContractError) as error:
            _h.canonical_identity(**dict(components, nodes=list(components['nodes'])), **args)
        self.assertIs(error.exception.reason, _t.Reason.TYPE)
        with self.assertRaises(_t.ContractError) as error:
            _h.canonical_identity(**components, **dict(args, screen_check=None))
        self.assertIs(error.exception.reason, _t.Reason.MISSING)
        with self.assertRaises(_t.ContractError) as error:
            _h.verify_pins(definition, **args)
        self.assertIs(error.exception.reason, _t.Reason.STALE)
        final = _dc.replace(definition, identity=identity, source_digest=identity.source_digest)
        self.assertEqual(_h.verify_pins(final, **args), (identity, canonical))
        plan = _dc.replace(old, definition=final, identity=identity, limits=final.limits,
                           abis=identity.abis, canonical_bytes=canonical)
        host.retained_plan, host.compiler = plan, plan.compiler_handle
        registry_args = dict(args, plan=plan, compiler=host.compiler, registry=host)
        self.assertEqual(_h.verify_pins(final, **registry_args), (identity, canonical))
        with self.assertRaises(_t.ContractError) as error:
            _h.verify_pins(final, **dict(registry_args, plan=_dc.replace(plan)))
        self.assertIs(error.exception.reason, _t.Reason.FOREIGN)

    # @anchor:wa.tests.scripts.test_header.HeaderControls.test_supervisor_replaced_during_dispatch
    def test_supervisor_replaced_during_dispatch(self):
        host = _Host()
        host.fault_after_parse = lambda: setattr(host, 'backend', _copy.copy(host))
        self._reject(_source(), _t.Reason.STALE, dispatches=1, host=host)
        control, admission, session = self._admit()
        self.assertEqual((session._result[0], control.dispatches), ('Module', 1))
