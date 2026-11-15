# @module {"id":"wa.tests.scripts.test_ast_gate","layer":2,"owner":"LEARN","deps":["wa.scripts.ast_gate","wa.scripts.types","wa.tests.scripts.support"],"book":"HB-WA-s4-learn"}
"""Paired negative/positive controls for syntax and injected admission ports."""
import ast
import hashlib
import io
import ctypes.util
import os
import select
import json
from pathlib import Path
import pickle
import subprocess
import sys
import threading
import time
from contextlib import contextmanager, redirect_stdout, redirect_stderr
from dataclasses import asdict, replace
import unittest
from unittest.mock import patch
from wender.scripts import ast_gate as gate
from wender.scripts.types import ContractError, Reason, SourcePolicy, ParserPolicy

_POLICY = ParserPolicy(1024, 8, 32, 1000, 134217728, 'retained')
_SOURCE = SourcePolicy(16384, 65536, 64)
_MINIMAL = 'def run(act, params):\n    return None\n'
_PREFIX = '# [SCRIPT_TAG] {"logical_id":"example","metadata_references":[],"predecessor":{"reason":"missing_evidence","value":null},"revision":1,"scope":{"attestation":null,"attestation_reason":"missing_evidence","browser_profile_incarnation":"profile","frame_ancestry":["https://example.test"],"principal":"p","scope_revision":1,"target_frame_origin":"https://example.test","tenant":"t","top_level_origin":"https://example.test"}}\n# [LANE_CONFIG] {"abis":{"act":"wender-act/v1","compiler":"wender-compiler/v1","dsl":"wender-dsl/v1","predicate":"wender-predicate/v1","schema":"wender-schema/v1"},"limits":{"absolute_deadline_ns":1000,"canonical_bytes":1024,"episode_input_bytes":128,"episode_result_bytes":128,"max_acquisitions":4,"max_nodes":4}}\n# [SURGERY_DECLARATION] {"entry":{"children":[],"operands":[{"kind":"TEXT","operand_id":"goal","query_id":"goal_query","value":"goal"}],"operator":"task_goal","scope":{"attestation":null,"attestation_reason":"missing_evidence","browser_profile_incarnation":"profile","frame_ancestry":["https://example.test"],"principal":"p","scope_revision":1,"target_frame_origin":"https://example.test","tenant":"t","top_level_origin":"https://example.test"},"source_requirements":["HOST"],"transition_reference":{"reason":"missing_evidence","value":null}},"terminal_goal":"goal"}\n# [CAPA_TRILOGIA] {"capabilities":[]}\n# [IO_CONTRACT] {"goal_contract":"goal","signature_contract":"signature","site_contract":"site"}\n# [MUTANT_PROOF] {"mutant":{"reason":"missing_evidence","value":null},"nominal":{"reason":"missing_evidence","value":null}}\n# [MERKLE_PIN] {"pins_reason":"missing_evidence","supplied_pins":null}\n# [META:args] []\n'


def _tree(body):
    return ast.parse('def run(act, params):\n' + '\n'.join('    ' + line for line in body.splitlines()) + '\n')


class _Backend:
    def __init__(self, events, failure=None):
        self.events, self.failure = events, failure

    # @anchor:wa.tests.scripts.test_ast_gate.Backend.parse
    def parse(self, body, policy, output_bytes):
        self.events.append(('parse', body))
        if self.failure is not None:
            raise self.failure
        return ast.parse(body)

    # @anchor:wa.tests.scripts.test_ast_gate.Backend.close
    def close(self):
        self.events.append('close')
        return True


# Fixed trusted adversarial worker; it never executes candidate code. All
# runtime modules load before filtering; source enters only after probe receipt.
_CONTAINED_WORKER = r"""
import ast, ctypes, errno, hashlib, json, mmap, os, pickle, resource, socket, sys, time
library, loading_library, unrelated, home, workspace, memory, mode = sys.argv[1:]
limit = int(memory)
sec = ctypes.CDLL(library, use_errno=True)
libc = ctypes.CDLL(None, use_errno=True)
sec.seccomp_init.argtypes = [ctypes.c_uint32]
sec.seccomp_init.restype = ctypes.c_void_p
sec.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
sec.seccomp_syscall_resolve_name.restype = ctypes.c_int
sec.seccomp_rule_add.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int, ctypes.c_uint]
sec.seccomp_load.argtypes = [ctypes.c_void_p]
sec.seccomp_release.argtypes = [ctypes.c_void_p]
class _Comparison(ctypes.Structure):
    _fields_ = [('arg', ctypes.c_uint), ('op', ctypes.c_uint),
                ('a', ctypes.c_uint64), ('b', ctypes.c_uint64)]
sec.seccomp_rule_add_array.argtypes = [ctypes.c_void_p, ctypes.c_uint32,
                                      ctypes.c_int, ctypes.c_uint,
                                      ctypes.POINTER(_Comparison)]
# Address-space increment is stricter than incremental resident-memory usage.
safe_cwd = os.getcwd() == '/'
with open('/proc/self/maps') as mappings:
    assert loading_library not in mappings.read()
with open('/proc/self/statm') as state:
    baseline = int(state.read().split()[0]) * os.sysconf('SC_PAGE_SIZE')
resource.setrlimit(resource.RLIMIT_AS, (baseline + limit, baseline + limit))
resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
context = sec.seccomp_init(0x00050000 | errno.EPERM)
assert context
for name in ('read', 'write', 'close', 'fstat', 'newfstatat', 'lseek',
             'brk', 'munmap', 'mremap', 'madvise', 'futex', 'getrandom',
             'rt_sigaction', 'rt_sigprocmask', 'rt_sigreturn', 'sigaltstack',
             'clock_gettime', 'clock_nanosleep', 'nanosleep', 'getpid',
             'gettid', 'exit', 'exit_group'):
    number = sec.seccomp_syscall_resolve_name(name.encode())
    if number >= 0:
        assert sec.seccomp_rule_add(context, 0x7fff0000, number, 0) == 0
# mmap may allocate data, but cannot introduce executable pages.
number = sec.seccomp_syscall_resolve_name(b'mmap')
comparison = _Comparison(2, 7, mmap.PROT_EXEC, 0)  # SCMP_CMP_MASKED_EQ
assert number >= 0
assert sec.seccomp_rule_add_array(context, 0x7fff0000, number, 1,
                                  ctypes.byref(comparison)) == 0
assert sec.seccomp_load(context) == 0
sec.seccomp_release(context)
probes = {'environment': (os.getenv('S1B_SYNTHETIC_SECRET') is None
                          and set(os.environ) <= {'LC_CTYPE'}),
          'cwd': safe_cwd}
# getcwd is deliberately unavailable too; parent independently pins cwd='/'.
def _denied(name, operation, expected=errno.EPERM):
    try:
        result = operation()
    except OSError as error:
        probes[name] = error.errno == expected
    else:
        probes[name] = False
        if name == 'children' and result == 0:
            os._exit(97)
        if hasattr(result, 'close'):
            result.close()
_denied('descriptor', lambda: os.read(int(unrelated), 1), errno.EBADF)
_denied('home', lambda: open(home, 'rb'))
_denied('workspace', lambda: open(workspace, 'rb'))
_denied('write', lambda: open(workspace, 'ab'))
_denied('socket', lambda: socket.socket())
# Direct connect syscall is denied even for an invalid descriptor.
ctypes.set_errno(0)
number = sec.seccomp_syscall_resolve_name(b'connect')
probes['network'] = libc.syscall(number, -1, 0, 0) == -1 and ctypes.get_errno() == errno.EPERM
_denied('children', os.fork)
_denied('signal', lambda: os.kill(os.getpid(), 0))
ctypes.set_errno(0)
probes['trace'] = libc.ptrace(0, 0, 0, 0) == -1 and ctypes.get_errno() == errno.EPERM
_denied('exec', lambda: os.execve(sys.executable, [sys.executable], {}))
_denied('executable_mapping', lambda: mmap.mmap(-1, 4096, prot=mmap.PROT_READ | mmap.PROT_EXEC))
ctypes.set_errno(0)
probes['executable_protection'] = libc.mprotect(0, 4096, mmap.PROT_EXEC) == -1 and ctypes.get_errno() == errno.EPERM
try:
    ctypes.CDLL(loading_library)
except OSError as error:
    # A real loadable library, proven absent from the startup mappings, must
    # fail specifically because opening it is prohibited.
    probes['loading'] = os.strerror(errno.EPERM) in str(error)
else:
    probes['loading'] = False
print(json.dumps(probes), flush=True)
body = sys.stdin.buffer.read()
if mode == 'stall':
    while True:
        time.sleep(1)
if mode == 'memory':
    try:
        bytearray(limit * 2)
    except MemoryError:
        sys.stdout.buffer.write(b'MEMORY_DENIED')
        sys.stdout.buffer.flush()
        sys.exit(0)
    sys.exit(98)
if mode in ('flood', 'stderr_flood'):
    stream = sys.stdout.buffer if mode == 'flood' else sys.stderr.buffer
    # A valid prefix forces the size gate, rather than the malformed-frame gate.
    if mode == 'flood':
        stream.write(b'S1BAST01' + (0).to_bytes(8, 'big') + bytes(32))
    while True:
        stream.write(b'CANARY_PRIVATE_TRANSPORT' * 4096)
        stream.flush()
if mode == 'malformed':
    sys.stdout.buffer.write(b'CANARY_PRIVATE_TRANSPORT' * 3)
    sys.stdout.buffer.flush()
    while True:
        time.sleep(1)
payload = pickle.dumps(ast.parse(body))
checksum = hashlib.sha256(payload).digest()
if mode == 'corrupt':
    payload = b'CANARY_PRIVATE_TRANSPORT'
    checksum = bytes(32)
sys.stdout.buffer.write(b'S1BAST01' + len(payload).to_bytes(8, 'big') + checksum + payload)
sys.stdout.buffer.flush()
"""


class _ContainedBackend:
    """Linux test supervisor, deliberately separate from production registration."""
    def __init__(self, policy, mode='healthy'):
        self.policy, self.mode = policy, mode
        self.child = None
        self.probes = {}
        self.closed = False
        self.events = []
        self.buffered_peak = 0
        self.read_peak = 0
        self.decoded = 0
        self.transport_size = None
        library = ctypes.util.find_library('seccomp')
        loading_library = ctypes.util.find_library('uuid')
        if library is None or loading_library is None:
            raise ContractError(Reason.MISSING)
        read_fd, write_fd = os.pipe()
        try:
            os.write(write_fd, b'SYNTHETIC_UNRELATED_FD')
            # Empty environment and close_fds strip only synthetic test material;
            # no real secret or credential is inspected by this test.
            self.child = subprocess.Popen(
                [sys.executable, '-I', '-B', '-c', _CONTAINED_WORKER,
                 library, loading_library, str(read_fd), str(Path.home()),
                 str(Path(gate.__file__).resolve()),
                 str(policy.incremental_memory_bytes), mode],
                env={}, cwd='/', close_fds=True,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                bufsize=0)
            if not select.select([self.child.stdout], [], [], 5)[0]:
                raise ContractError(Reason.MISSING)
            receipt = self.child.stdout.readline(4096)
            self.probes = json.loads(receipt)
        except BaseException:
            self.close()
            raise
        finally:
            os.close(read_fd)
            os.close(write_fd)

    # @anchor:wa.tests.scripts.test_ast_gate.ContainedBackend.verify
    def verify(self, reference, candidate, policy):
        required = {'environment', 'cwd', 'descriptor', 'home', 'workspace', 'write',
                    'socket', 'network', 'children', 'trace', 'signal', 'exec',
                    'executable_mapping', 'executable_protection', 'loading'}
        return (reference == 'retained' and candidate is self and policy == self.policy
                and not self.closed and required <= self.probes.keys()
                and all(self.probes[key] is True for key in required))

    # @anchor:wa.tests.scripts.test_ast_gate.ContainedBackend.parse
    def parse(self, body, policy, output_bytes):
        if not self.verify(policy.supervisor_reference, self, policy):
            raise ContractError(Reason.MISSING)
        deadline = time.monotonic() + policy.timeout_ms / 1000
        pending = memoryview(body)
        while pending:
            written = self.child.stdin.write(pending)
            pending = pending[written:]
        self.child.stdin.close()
        output = bytearray()
        diagnostic_bytes = 0
        streams = [self.child.stdout, self.child.stderr]
        while streams:
            remaining_time = deadline - time.monotonic()
            if remaining_time <= 0:
                raise ContractError(Reason.DEADLINE)
            readable, _, _ = select.select(streams, [], [], remaining_time)
            if not readable:
                raise ContractError(Reason.DEADLINE)
            for stream in readable:
                # The sentinel byte detects overflow; never retain it. Stdout
                # and stderr share the same aggregate transport ceiling.
                chunk = os.read(stream.fileno(), min(4096, output_bytes - len(output) - diagnostic_bytes + 1))
                self.read_peak = max(self.read_peak, len(output) + diagnostic_bytes + len(chunk))
                if not chunk:
                    streams.remove(stream)
                    continue
                if len(output) + diagnostic_bytes + len(chunk) > output_bytes:
                    raise ContractError(Reason.BOUNDS)
                if stream is self.child.stderr:
                    # Diagnostics are never decoded or surfaced.
                    diagnostic_bytes += len(chunk)
                    continue
                output.extend(chunk)
                self.buffered_peak = max(self.buffered_peak, len(output))
                if output == b'MEMORY_DENIED':
                    raise ContractError(Reason.CAPACITY)
                if len(output) >= 48:
                    if output[:8] != b'S1BAST01':
                        raise ContractError(Reason.SCHEMA)
                    if int.from_bytes(output[8:16], 'big') + 48 > output_bytes:
                        raise ContractError(Reason.BOUNDS)
        self.child.wait(timeout=max(0.001, deadline - time.monotonic()))
        if diagnostic_bytes or self.child.returncode != 0 or len(output) < 48:
            raise ContractError(Reason.SCHEMA)
        size = int.from_bytes(output[8:16], 'big')
        if (output[:8] != b'S1BAST01' or size != len(output) - 48
                or hashlib.sha256(output[48:]).digest() != output[16:48]):
            raise ContractError(Reason.SCHEMA)
        self.transport_size = len(output)
        # Fixed trusted child only. Framing, size and integrity are checked
        # before the test-only pickle decoder; this is no production ABI.
        self.decoded += 1
        self.events.append('decode')
        return pickle.loads(output[48:])

    # @anchor:wa.tests.scripts.test_ast_gate.ContainedBackend.close
    def close(self):
        if self.closed:
            return True
        if self.child is not None:
            if self.child.poll() is None:
                self.child.kill()
                self.events.append('terminate')
            self.child.wait(timeout=5)
            self.events.append('join')
            # Once joined, finite kernel pipe contents are discarded in bounded
            # chunks; communicate() must never buffer a flooding child's output.
            for stream in (self.child.stdout, self.child.stderr):
                while os.read(stream.fileno(), 4096):
                    pass
                stream.close()
            if not self.child.stdin.closed:
                self.child.stdin.close()
            self.events.append('drain')
        self.closed = True
        self.events.append('close')
        return True



class _RecordResolver:
    """Trusted test owner uses S0 records and retained identity, never literals as grants."""
    def __init__(self):
        from wender.scripts import types as t
        from .support import IdentityRegistry, make_fixture
        self.t = t
        fixture = make_fixture()
        self.scope, self.episode = fixture['scope'], 'episode'
        self.registry = IdentityRegistry(fixture['handle'], 1)
        self.query = self.registry.register(t.QueryDeclaration(
            'known', fixture['selector'], self.scope, (t.PredicateOperator.EXISTS_UNIQUE,)))
        self.predicate = self.registry.register(fixture['entry'])
        self.input_grant = self.registry.mint('input', 'input-test-grant')
        self.output_grant = self.registry.mint('harvest', 'output-test-grant')
        self.input = t.SlotDescriptor('input', t.ValueKind.TEXT, t.Purpose.INPUT,
            self.input_grant.handle_identity,
            self.episode, self.scope, 'destination', 'input-contract', None)
        self.output = replace(self.input, slot_name='output', purpose=t.Purpose.HARVEST,
            capability=self.output_grant.handle_identity,
            capability_contract='output-contract')
        # Retain the exact capability objects independently of their equal fields.
        self.registry.register(self.input.capability)
        self.registry.register(self.output.capability)
        self.vocabulary = self.registry.register(t.HostKeyVocabulary(
            'host-keys', ('ENTER', '1'), fixture['handle']))
        self.registry.register(self.vocabulary.attestation)
        self.slot_policy = t.SlotPolicy(2, 7, 19, 7, 19)
        self.input_value = b'fixture'
        self.wait_limit = 17
        self.seen = []

    def _retained(self, value):
        return self.registry.check(value, self.registry.owner, self.registry.generation)

    def __call__(self, role, value, policy):
        t = self.t
        self.seen.append(role)
        def _need(condition, reason=Reason.FOREIGN):
            if not condition:
                raise ContractError(reason)
        if role == 'query':
            _need(type(value) is str and value == self.query.name, Reason.SCHEMA)
            self._retained(self.query)
            _need(self.query.scope == self.scope)
            self._retained(self.query.selector.label_export_receipt)
        elif role == 'predicate':
            _need(value == 'goal', Reason.MISSING)
            self._retained(self.predicate)
            pending, count = [self.predicate], 0
            while pending:
                predicate = pending.pop()
                _need(type(predicate) is t.PredicateSpec, Reason.TYPE)
                _need(predicate.scope == self.scope)
                count += 1
                _need(count <= policy.predicate_nodes, Reason.BOUNDS)
                pending.extend(predicate.children)
        elif role in ('slot', 'slot_out'):
            descriptor = self.input if role == 'slot' else self.output
            _need(value == descriptor.slot_name, Reason.MISSING)
            _need(descriptor.scope == self.scope and descriptor.episode_id == self.episode)
            _need(descriptor.purpose is (t.Purpose.INPUT if role == 'slot' else t.Purpose.HARVEST))
            grant = self.input_grant if role == 'slot' else self.output_grant
            self._retained(grant)
            _need(grant.kind == ('input' if role == 'slot' else 'harvest'))
            _need(descriptor.capability is grant.handle_identity)
            self._retained(descriptor.capability)
            _need(2 <= self.slot_policy.slots, Reason.BOUNDS)
            if role == 'slot':
                _need(len(self.input_value) <= self.slot_policy.input_slot_bytes, Reason.BOUNDS)
        elif role == 'keysym':
            self._retained(self.vocabulary)
            self._retained(self.vocabulary.attestation)
            _need(value in self.vocabulary.symbols, Reason.SCHEMA)
        elif role == 'timeout_ms':
            _need(value <= self.wait_limit, Reason.BOUNDS)
        elif role == 'max_bytes':
            _need(value <= self.slot_policy.harvest_result_bytes, Reason.BOUNDS)
        elif role == 'code':
            _need(value in tuple(reason.value for reason in Reason), Reason.SCHEMA)
        else:
            raise ContractError(Reason.SCHEMA)
        return True


class _GateTests(unittest.TestCase):
    def _reject(self, tree, reason=Reason.SCHEMA, policy=_POLICY):
        with self.assertRaises(ContractError) as caught:
            gate.validate_restricted_ast(tree, policy)
        self.assertIs(caught.exception.reason, reason)
        self.assertIsNone(caught.exception.__context__)
        self.assertIsNone(caught.exception.__cause__)
        return caught.exception

    def _admission_reject(self, source, ports, reason):
        result = object()
        sentinel = result
        output = io.StringIO()
        with redirect_stdout(output), redirect_stderr(output):
            with self.assertRaises(ContractError) as caught:
                result = gate.admit_source(source.encode() if type(source) is str else source, _SOURCE, _POLICY, **ports)
        self.assertIs(result, sentinel)
        self.assertIs(caught.exception.reason, reason)
        self.assertIsNone(caught.exception.__context__)
        self.assertIsNone(caught.exception.__cause__)
        self.assertNotIn('CANARY', str(caught.exception) + repr(caught.exception))
        self.assertEqual(output.getvalue(), '')

    # @anchor:wa.tests.scripts.test_ast_gate.test_wrapper_and_poisoned_suffix
    def test_wrapper_and_poisoned_suffix(self):
        self.assertIs(gate.validate_restricted_ast(ast.parse(_MINIMAL), _POLICY), True)
        for body in ('"doc"', 'return', 'return 1', 'return None\nx = act.find("q")',
                     'import os', 'for x in []:\n    act.click(x, expect="p")',
                     'if True:\n    act.find("q")', '[x for x in []]', 'lambda: None',
                     'try:\n    return None\nexcept:\n    return None', 'with x:\n    return None',
                     'def nested():\n    return None', 'assert True'):
            with self.subTest(body=body):
                self._reject(_tree('x = act.find("q")\n' + body))
        for source in ('x=1\n' + _MINIMAL, '@foo\n' + _MINIMAL,
                       _MINIMAL.replace('params)', 'params=1)'),
                       _MINIMAL.replace('params)', 'params: int)'),
                       _MINIMAL.replace('params)', 'params, extra)'),
                       'async ' + _MINIMAL, _MINIMAL.replace('run(act, params)', 'run(act, /, params)'),
                       _MINIMAL.replace('run(act, params)', 'run(act, *params)')):
            self._reject(ast.parse(source))
        malformed = ast.parse(_MINIMAL)
        malformed.body[0].body = []
        self._reject(malformed)

    # @anchor:wa.tests.scripts.test_ast_gate.test_names_and_every_verb
    def test_names_and_every_verb(self):
        good = '''One = act.find({"text": "é", "nested": [True, None, (1,)]})
Two = act.find("q")
act.click(One, expect="p")
act.type(Two, expect="p", slot="text")
act.key(One, keysym="ENTER", expect="p")
act.wait_until("p", timeout_ms=30000)
act.assert_tag("p", code="missing_evidence")
act.harvest(One, slot_out="result", max_bytes=16384)'''
        self.assertTrue(gate.validate_restricted_ast(_tree(good), _POLICY))
        for body in ('x = act.find("q")\nx = act.find("q")', 'X = act.find("q")\nx = act.find("q")',
                     'act = act.find("q")', 'params = act.find("q")', 'ACT = act.find("q")',
                     '_x = act.find("q")', 'é = act.find("q")', 'x = y = act.find("q")',
                     'x = act.find("q")\ny = x',
                     'x, y = act.find("q")', 'x = act', 'x = params', 'x = act.find(y)',
                     'act.click(x, expect="p")', 'x = act.click(x, expect="p")'):
            self._reject(_tree(body))

    # @anchor:wa.tests.scripts.test_ast_gate.test_call_signature_pairs
    def test_call_signature_pairs(self):
        prefix = 'x = act.find("q")\n'
        for call in ('getattr(act,"click")(x)', 'act.foo.click(x)', 'print(x)', 'act.unknown(x)',
                     'act.click(act.find("q"), expect="p")', 'act.click(*x, expect="p")',
                     'act.click(x, **{})', 'act.click(x, expect="p", expect="p")',
                     'act.click(x)', 'act.click(x, expect="p", extra=1)', 'act.click(x,x,expect="p")',
                     'act.find("q")', 'act.click(x[0], expect="p")'):
            self._reject(_tree(prefix + call))
        self.assertTrue(gate.validate_restricted_ast(_tree(prefix + 'act.click(x, expect="p")'), _POLICY))

    # @anchor:wa.tests.scripts.test_ast_gate.test_literals_and_numeric_boundaries
    def test_literals_and_numeric_boundaries(self):
        for expression, reason in (('1.0', Reason.SCHEMA), ('b"x"', Reason.SCHEMA),
                ('f"x"', Reason.SCHEMA), ('1+1', Reason.SCHEMA), ('-1', Reason.SCHEMA),
                ('{"a":1,"a":2}', Reason.SCHEMA), ('{**{}}', Reason.SCHEMA),
                ('{1:"x"}', Reason.SCHEMA), ('"\\ud800"', Reason.SCHEMA), ('"\\x00"', Reason.SCHEMA)):
            self._reject(_tree('x = act.find(' + expression + ')'), reason)
        for verb, kw, ceiling in (('wait_until', 'timeout_ms', 30000), ('harvest', 'max_bytes', 16384)):
            target = '"p"' if verb == 'wait_until' else 'x'
            other = '' if verb == 'wait_until' else ', slot_out="out"'
            prefix = 'x = act.find("q")\n'
            for value, reason in (('True', Reason.TYPE), ('0', Reason.BOUNDS), (str(ceiling+1), Reason.BOUNDS)):
                self._reject(_tree(prefix + f'act.{verb}({target}, {kw}={value}{other})'), reason)
            self.assertTrue(gate.validate_restricted_ast(_tree(prefix + f'act.{verb}({target}, {kw}={ceiling}{other})'), _POLICY))
        self._reject(_tree('act.assert_tag("p", code="invented")'))
        self._reject(_tree('x = act.find("q")\nact.type(x, slot="private payload", expect="p")'))

    # @anchor:wa.tests.scripts.test_ast_gate.test_dictionary_lengths_through_both_apis
    def test_dictionary_lengths_through_both_apis(self):
        # Predicate: dictionary keys and values pair exactly; neither API may
        # silently truncate malformed parser output through zip().
        source = b'def run(act, params):\n    x = act.find({"a": 1, "b": 2})\n'
        expected = (('x', 'find', 'query', ('dict', (('a', 1), ('b', 2))), ()),)
        valid = ast.parse(source)
        original = ast.dump(valid, include_attributes=True)
        for api in ('validate', 'admit'):
            for shortened in ('keys', 'values'):
                with self.subTest(api=api, shortened=shortened):
                    malformed = ast.parse(source)
                    dictionary = malformed.body[0].body[0].value.args[0]
                    getattr(dictionary, shortened).pop()
                    self.assertNotEqual(len(dictionary.keys), len(dictionary.values))
                    before = ast.dump(malformed, include_attributes=True)
                    if api == 'validate':
                        self._reject(malformed, Reason.SCHEMA)
                    else:
                        events, ports = self._ports()
                        with patch.object(ports['supervisor'], 'parse', return_value=malformed) as parse:
                            self._admission_reject(source, ports, Reason.SCHEMA)
                        parse.assert_called_once()
                        self.assertEqual(events[-2:], ['close', 'release'])
                    self.assertEqual(ast.dump(malformed, include_attributes=True), before)
        # Positive controls preserve all dictionary entries and the empty tag.
        for body, checked in ((source, expected),
                              (source.replace(b'{"a": 1, "b": 2}', b'{}'),
                               (('x', 'find', 'query', ('dict', ()), ()),))):
            self.assertIs(gate.validate_restricted_ast(ast.parse(body), _POLICY), True)
            events, ports = self._ports()
            self.assertEqual(gate.admit_source(body, _SOURCE, _POLICY, **ports), checked)
            self.assertEqual(events[-2:], ['close', 'release'])
        self.assertEqual(ast.dump(valid, include_attributes=True), original)

    # @anchor:wa.tests.scripts.test_ast_gate.test_ast_occurrences_depth_and_malformed
    def test_ast_occurrences_depth_and_malformed(self):
        tree = ast.parse(_MINIMAL)
        # Trusted test-only traversal measures each occurrence, including contexts.
        pending, count, depth = [(tree, 0)], 0, 0
        while pending:
            node, edge = pending.pop()
            count += 1
            depth = max(depth, edge)
            pending.extend((child, edge+1) for child in ast.iter_child_nodes(node))
        self.assertTrue(gate.validate_restricted_ast(tree, replace(_POLICY, ast_nodes=count, ast_depth=depth)))
        self._reject(tree, Reason.BOUNDS, replace(_POLICY, ast_nodes=count-1))
        self._reject(tree, Reason.BOUNDS, replace(_POLICY, ast_depth=depth-1))
        cycle = ast.parse(_MINIMAL)
        cycle.body[0].body = [cycle]
        self._reject(cycle)
        for field, value in (('body', 'bad'), ('type_ignores', None)):
            bad = ast.parse(_MINIMAL)
            setattr(bad, field, value)
            self._reject(bad)
        bad = ast.parse(_MINIMAL)
        bad.extra = 'CANARY'
        self._reject(bad)
        bad = ast.parse(_MINIMAL)
        bad.body[0].lineno = 'CANARY'
        self._reject(bad)
        class _Forged(ast.Module):
            pass
        self._reject(_Forged(body=[], type_ignores=[]))
        shared = _tree('x = act.find("q")\nact.click(x, expect="p")')
        nodes = sum(1 for _ in ast.walk(shared))
        self.assertTrue(gate.validate_restricted_ast(shared, replace(_POLICY, ast_nodes=nodes)))
        self._reject(shared, Reason.BOUNDS, replace(_POLICY, ast_nodes=nodes-1))

    # @anchor:wa.tests.scripts.test_ast_gate.test_required_fields_and_load_store_contexts
    def test_required_fields_and_load_store_contexts(self):
        # Predicate: validation rejects incomplete or mis-contextualized trees
        # without repairing them; the original parser output remains valid.
        body = 'x = act.find("q")\nact.click(x, expect="p")'
        valid = _tree(body)
        original = ast.dump(valid, include_attributes=True)
        self.assertIs(gate.validate_restricted_ast(valid, _POLICY), True)
        for fixture in ('missing_body', 'missing_call_args', 'missing_name_id',
                        'assignment_load', 'handle_store'):
            with self.subTest(fixture=fixture):
                malformed = _tree(body)
                assignment, click = malformed.body[0].body
                if fixture == 'missing_body':
                    del malformed.body
                elif fixture == 'missing_call_args':
                    del assignment.value.args
                elif fixture == 'missing_name_id':
                    del assignment.targets[0].id
                elif fixture == 'assignment_load':
                    assignment.targets[0].ctx = ast.Load()
                else:
                    click.value.args[0].ctx = ast.Store()
                before = ast.dump(malformed, include_attributes=True)
                self._reject(malformed, Reason.SCHEMA)
                self.assertEqual(ast.dump(malformed, include_attributes=True), before)
        self.assertIs(gate.validate_restricted_ast(valid, _POLICY), True)
        self.assertEqual(ast.dump(valid, include_attributes=True), original)

    # @anchor:wa.tests.scripts.test_ast_gate.test_explicit_instance_fields_through_both_apis
    def test_explicit_instance_fields_through_both_apis(self):
        # Predicate: every declared field must exist on the actual node,
        # including optional fields whose class defaults hide deletion.
        body = 'x = act.find("q")\nact.click(x, expect="p")'
        source = 'def run(act, params):\n    x = act.find("q")\n    act.click(x, expect="p")\n'
        expected = (('x', 'find', 'query', 'q', ()),
                    (None, 'click', 'handle', ('handle', 'x'), (('expect', 'p'),)))
        valid = _tree(body)
        original = ast.dump(valid, include_attributes=True)
        self.assertIs(gate.validate_restricted_ast(valid, _POLICY), True)
        _, ports = self._ports()
        self.assertEqual(gate.admit_source(source.encode(), _SOURCE, _POLICY, **ports), expected)
        for index, node in enumerate(ast.walk(valid)):
            for field in node._fields:
                with self.subTest(node=type(node).__name__, field=field, occurrence=index):
                    malformed = _tree(body)
                    target = list(ast.walk(malformed))[index]
                    self.assertIn(field, vars(target))
                    delattr(target, field)
                    self.assertNotIn(field, vars(target))
                    self._reject(malformed, Reason.SCHEMA)
                    events, ports = self._ports()
                    with patch.object(ports['supervisor'], 'parse', return_value=malformed) as parse:
                        self._admission_reject(source, ports, Reason.SCHEMA)
                    parse.assert_called_once()
                    self.assertLess(events.index('close'), events.index('release'))
                    self.assertNotIn(field, vars(target))
        self.assertIs(gate.validate_restricted_ast(valid, _POLICY), True)
        self.assertEqual(ast.dump(valid, include_attributes=True), original)
        _, ports = self._ports()
        self.assertEqual(gate.admit_source(source.encode(), _SOURCE, _POLICY, **ports), expected)

    def _ports(self, failure=None, resolve=None):
        events = []
        backend = _Backend(events, failure)
        @contextmanager
        def _reserve():
            events.append('reserve')
            try:
                yield True
            finally:
                events.append('release')
        def _header(raw, policy):
            events.append('header')
            # Closed synthetic freeze-vector codec, not a production header ABI.
            # Headerless bodies are structural fixtures only.
            normalized = raw.replace(b'\r\n', b'\n').replace(b'\r', b'\n')
            lines = normalized.splitlines(keepends=True)
            declarations = []
            while lines and lines[0].startswith(b'# ['):
                declarations.append(lines.pop(0))
            if len(declarations) > policy.header_lines:
                raise ContractError(Reason.BOUNDS)
            if declarations and b''.join(declarations) != _PREFIX.encode():
                raise ContractError(Reason.SCHEMA)
            if any(line.lstrip().startswith(b'# [') for line in lines):
                raise ContractError(Reason.SCHEMA)
            return b''.join(lines)
        return events, dict(header_codec=_header, reservation=_reserve(), supervisor=backend,
            verify_supervisor=lambda reference, candidate, policy: reference == 'retained' and candidate is backend,
            # Default resolver proves structure only; semantic tests inject a closed resolver.
            resolve=resolve or (lambda role, value, policy: True), representation_bytes=65536)

    # @anchor:wa.tests.scripts.test_ast_gate.test_frozen_crlf_and_no_execution
    def test_frozen_crlf_and_no_execution(self):
        prefix = _PREFIX
        raw = (prefix + _MINIMAL).replace('\n', '\r\n').encode()
        events, ports = self._ports()
        with patch('builtins.eval', side_effect=AssertionError('execution')), patch('builtins.exec', side_effect=AssertionError('execution')):
            self.assertEqual(gate.admit_source(raw, _SOURCE, _POLICY, **ports), ())
        self.assertIn(('parse', _MINIMAL.encode()), events)
        self.assertEqual(events[-2:], ['close', 'release'])

    # @anchor:wa.tests.scripts.test_ast_gate.test_missing_evidence_and_forged_identity
    def test_missing_evidence_and_forged_identity(self):
        for policy in (None, _POLICY):
            with self.assertRaises(ContractError) as caught:
                gate.admit_source(_MINIMAL.encode(), _SOURCE, policy)
            self.assertIs(caught.exception.reason, Reason.MISSING)
        events, ports = self._ports()
        ports['verify_supervisor'] = lambda *args: False
        with self.assertRaises(ContractError) as caught:
            gate.admit_source(_MINIMAL.encode(), _SOURCE, _POLICY, **ports)
        self.assertIs(caught.exception.reason, Reason.MISSING)
        self.assertFalse(any(type(e) is tuple for e in events))
        events, ports = self._ports()
        retained = ports['supervisor']
        foreign = _Backend(events)
        self.assertEqual(vars(foreign), vars(retained))
        ports['supervisor'] = foreign
        self._admission_reject(_MINIMAL, ports, Reason.MISSING)
        self.assertFalse(any(type(e) is tuple for e in events))
        events, ports = self._ports()
        self.assertEqual(gate.admit_source(_MINIMAL.encode(), _SOURCE, _POLICY, **ports), ())

    # @anchor:wa.tests.scripts.test_ast_gate.test_source_preflight_never_dispatches
    def test_source_preflight_never_dispatches(self):
        for raw, reason in ((123, Reason.TYPE), (b'\xff', Reason.SCHEMA), (('\ufeff'+_MINIMAL).encode(), Reason.SCHEMA),
                            (('# coding: latin-1\n'+_MINIMAL).encode(), Reason.SCHEMA), (b'x'*65537, Reason.BOUNDS), (_MINIMAL, Reason.TYPE)):
            events, ports = self._ports()
            with self.assertRaises(ContractError) as caught:
                gate.admit_source(raw, _SOURCE, _POLICY, **ports)
            self.assertIs(caught.exception.reason, reason)
            self.assertEqual(events, ['close'] if type(raw) is not bytes or len(raw) > _SOURCE.utf8_bytes else ['reserve', 'close', 'release'])
        events, ports = self._ports()
        self.assertEqual(gate.admit_source(_MINIMAL.encode(), _SOURCE, _POLICY, **ports), ())

    # @anchor:wa.tests.scripts.test_ast_gate.test_cleanup_privacy_schema_and_refusal_mutant
    def test_cleanup_privacy_schema_and_refusal_mutant(self):
        for failure, reason in ((RuntimeError('CANARY'), Reason.SCHEMA),
                                (ContractError(Reason.DEADLINE), Reason.DEADLINE),
                                (ContractError(Reason.CANCELLED), Reason.CANCELLED)):
            events, ports = self._ports(failure)
            self._admission_reject(_MINIMAL, ports, reason)
            self.assertEqual(events[-2:], ['close', 'release'])
        for failure in (RuntimeError('CANARY'), ContractError(Reason.FOREIGN)):
            events, ports = self._ports()
            def _private_resolver(*args):
                raise failure
            ports['resolve'] = _private_resolver
            self._admission_reject(b'def run(act, params):\n    x = act.find("CANARY")\n',
                                   ports, failure.reason if isinstance(failure, ContractError) else Reason.SCHEMA)
            self.assertEqual(events[-2:], ['close', 'release'])
        source = 'def run(act, params):\n    x = act.find({"unknown": "CANARY"})\n'
        events, ports = self._ports(resolve=lambda *args: False)
        self._admission_reject(source, ports, Reason.SCHEMA)
        self.assertEqual(events[-2:], ['close', 'release'])
        events, ports = self._ports(resolve=lambda role, value, policy: value == ('dict', (('known', 'q'),)))
        checked = gate.admit_source(source.replace('unknown', 'known').replace('CANARY', 'q').encode(), _SOURCE, _POLICY, **ports)
        self.assertEqual(checked, (('x', 'find', 'query', ('dict', (('known', 'q'),)), ()),))
        events, ports = self._ports()
        with patch.object(gate, '_validate', side_effect=ContractError(Reason.SCHEMA)):
            self._admission_reject(_MINIMAL, ports, Reason.SCHEMA)
        self.assertEqual(events[-2:], ['close', 'release'])
        for returned in (None, 'CANARY', ast.parse('import os')):
            events, ports = self._ports()
            ports['supervisor'].parse = lambda *args: returned
            self._admission_reject(_MINIMAL, ports, Reason.SCHEMA)
            self.assertEqual(events[-2:], ['close', 'release'])

    # @anchor:wa.tests.scripts.test_ast_gate.test_capacity_and_representation_budget
    def test_capacity_and_representation_budget(self):
        events, ports = self._ports()
        @contextmanager
        def _denied():
            raise ContractError(Reason.CAPACITY)
            yield
        for source in (_MINIMAL.encode(), _MINIMAL.encode()):
            events.clear()
            ports['reservation'] = _denied()
            with patch.object(gate, '_source_bytes', side_effect=AssertionError('transformed')) as transform:
                self._admission_reject(source, ports, Reason.CAPACITY)
            transform.assert_not_called()
            self.assertEqual(events, ['close'])
        events, ports = self._ports()
        ports['representation_bytes'] = len(_MINIMAL.encode())
        with self.assertRaises(ContractError) as caught:
            gate.admit_source(_MINIMAL.encode(), _SOURCE, _POLICY, **ports)
        self.assertIs(caught.exception.reason, Reason.BOUNDS)
        self.assertEqual(events[-2:], ['close', 'release'])
        events, ports = self._ports()
        self.assertEqual(gate.admit_source(_MINIMAL.encode(), _SOURCE, _POLICY, **ports), ())

    # @anchor:wa.tests.scripts.test_ast_gate.test_original_identifier_spellings
    def test_original_identifier_spellings(self):
        for spelling in ('K', 'Ｋ', 'é', 'a\u0301'):
            events, ports = self._ports()
            self._admission_reject('def run(act, params):\n    ' + spelling + ' = act.find("q")\n', ports, Reason.SCHEMA)
            self.assertEqual(events, ['reserve', 'close', 'release'])
        events, ports = self._ports()
        source = 'def run(act, params):\n    K = act.find({"K": "é"}) # K\n'
        checked = gate.admit_source(source.encode(), _SOURCE, _POLICY, **ports)
        self.assertEqual(checked, (('K', 'find', 'query', ('dict', (('K', 'é'),)), ()),))
        self.assertEqual(events[-2:], ['close', 'release'])

    # @anchor:wa.tests.scripts.test_ast_gate.test_scalar_field_shapes_and_dictionary_keys
    def test_scalar_field_shapes_and_dictionary_keys(self):
        for location in ('kind', 'value', 'id', 'name', 'arg', 'attr', 'type_comment'):
            tree = _tree('x = act.find({"known": "q"})')
            fn = tree.body[0]
            assignment = fn.body[0]
            targets = dict(kind=assignment.value.args[0].keys[0],
                           value=assignment.value.args[0].keys[0],
                           id=assignment.targets[0], name=fn, arg=fn.args.args[0],
                           attr=assignment.value.func, type_comment=assignment)
            setattr(targets[location], location, ast.Name(id='params', ctx=ast.Load()))
            with self.subTest(field=location):
                self._reject(tree)
                events, ports = self._ports()
                ports['supervisor'].parse = lambda *args: tree
                self._admission_reject(_MINIMAL, ports, Reason.SCHEMA)
                self.assertEqual(events[-2:], ['close', 'release'])
        good = _tree('x = act.find({"known": "q"})')
        self.assertTrue(gate.validate_restricted_ast(good, _POLICY))
        events, ports = self._ports()
        ports['supervisor'].parse = lambda *args: good
        self.assertEqual(gate.admit_source(_MINIMAL.encode(), _SOURCE, _POLICY, **ports),
                         (('x', 'find', 'query', ('dict', (('known', 'q'),)), ()),))

    # @anchor:wa.tests.scripts.test_ast_gate.test_oversized_string_precedes_encoding
    def test_oversized_string_precedes_encoding(self):
        oversized = 'CANARY' * 12000
        tree = _tree('x = act.find("q")')
        tree.body[0].body[0].value.args[0].value = oversized
        events, ports = self._ports()
        ports['supervisor'].parse = lambda *args: tree
        original = gate._text
        def _observe(value):
            self.assertIsNot(value, oversized, 'oversized string encoded before rejection')
            return original(value)
        with patch.object(gate, '_text', side_effect=_observe):
            self._admission_reject(_MINIMAL, ports, Reason.BOUNDS)
        self.assertEqual(events[-2:], ['close', 'release'])
        tree.body[0].body[0].value.args[0].value = 'q'
        events, ports = self._ports()
        ports['supervisor'].parse = lambda *args: tree
        self.assertEqual(gate.admit_source(_MINIMAL.encode(), _SOURCE, _POLICY, **ports),
                         (('x', 'find', 'query', 'q', ()),))

    # @anchor:wa.tests.scripts.test_ast_gate.test_policy_revalidation
    def test_policy_revalidation(self):
        forged = replace(_POLICY)
        object.__setattr__(forged, 'ast_nodes', True)
        self._reject(ast.parse(_MINIMAL), Reason.TYPE, forged)
        forged = replace(_POLICY)
        object.__setattr__(forged, 'ast_depth', 0)
        self._reject(ast.parse(_MINIMAL), Reason.BOUNDS, forged)
        self.assertTrue(gate.validate_restricted_ast(ast.parse(_MINIMAL), _POLICY))

    # @anchor:wa.tests.scripts.test_ast_gate.test_comment_preflight
    def test_comment_preflight(self):
        # Predicate: comment directives reject before dispatch; literal text is passive.
        for source in (
                '# coding: latin-1\n' + _MINIMAL,
                ('#!/usr/bin/python\n# coding=utf-8\n' + _MINIMAL).replace('\n', '\r'),
                '#!/usr/bin/python\n# coding=utf-8\n' + _MINIMAL,
                '\n  # coding: utf-8\n' + _MINIMAL,
                'def run(act, params):\n    x = act.find("q") # type: int\n',
                _MINIMAL.rstrip() + ' # type: ignore\n',
                '# type: ignore\n' + _MINIMAL):
            events, ports = self._ports()
            self._admission_reject(source, ports, Reason.SCHEMA)
            self.assertEqual(events, ['reserve', 'close', 'release'])
        for literal in ('coding: safe label', 'coding=utf-8', '# type: int', '# type: ignore'):
            events, ports = self._ports()
            source = 'def run(act, params):\n    x = act.find(' + repr(literal) + ')\n'
            checked = gate.admit_source(('# ordinary\r' + source.replace('\n', '\r')).encode(), _SOURCE, _POLICY, **ports)
            self.assertEqual(checked, (('x', 'find', 'query', literal, ()),))
            self.assertEqual(events[-2:], ['close', 'release'])
        # A coding-like comment outside the declaration positions is ordinary data.
        events, ports = self._ports()
        source = 'def run(act, params): # coding: safe label\n    return None\n'
        self.assertEqual(gate.admit_source(source.encode(), _SOURCE, _POLICY, **ports), ())

    # @anchor:wa.tests.scripts.test_ast_gate.test_pre_dispatch_ownership_and_evidence
    def test_pre_dispatch_ownership_and_evidence(self):
        # Predicate: retained identity alone never supplies enforcement evidence.
        for identity_ok, enforcement_ok, accepted in (
                (False, True, False), (True, False, False), (True, True, True)):
            events, ports = self._ports()
            retained = ports['supervisor']
            observed = []
            def _verify(reference, candidate, policy):
                identity = reference == 'retained' and candidate is retained and identity_ok
                observed.append((identity, enforcement_ok))
                return identity and enforcement_ok
            ports['verify_supervisor'] = _verify
            if accepted:
                self.assertEqual(gate.admit_source(_MINIMAL.encode(), _SOURCE, _POLICY, **ports), ())
            else:
                self._admission_reject(_MINIMAL, ports, Reason.MISSING)
                self.assertFalse(any(type(event) is tuple for event in events))
            self.assertEqual(observed, [(identity_ok, enforcement_ok)] * (2 if accepted else 1))
            self.assertEqual(events[-2:], ['close', 'release'])
        for source in (b'\xffCANARY', '\ufeff' + _MINIMAL,
                       'def run(act, params):\n    K = act.find("CANARY")\n'):
            events, ports = self._ports()
            self._admission_reject(source, ports, Reason.SCHEMA)
            self.assertEqual(events, ['reserve', 'close', 'release'])
        events, ports = self._ports()
        ports['header_codec'] = lambda *args: (_ for _ in ()).throw(RuntimeError('CANARY'))
        self._admission_reject(_MINIMAL, ports, Reason.SCHEMA)
        self.assertEqual(events, ['reserve', 'close', 'release'])

    # @anchor:wa.tests.scripts.test_ast_gate.test_closed_resolver_branches
    def test_closed_resolver_branches(self):
        # Synthetic agreed schema, not a new production query/predicate ABI.
        # Predicate: unresolved references, undeclared slots and resolved budgets
        # fail admission without returning the valid structural prefix.
        resolved = {'goal': tuple(() for _ in range(_POLICY.predicate_nodes - 1)),
                    'oversized': tuple(() for _ in range(_POLICY.predicate_nodes))}
        def _resolve(role, value, policy):
            if role == 'query':
                if value != 'known':
                    raise ContractError(Reason.MISSING)
            elif role in ('slot', 'slot_out'):
                if value not in ('input', 'output'):
                    raise ContractError(Reason.FOREIGN)
            elif role == 'predicate':
                if value not in resolved:
                    raise ContractError(Reason.MISSING)
                pending, count = [resolved[value]], 0
                while pending:
                    node = pending.pop()
                    count += 1
                    if count > policy.predicate_nodes:
                        raise ContractError(Reason.BOUNDS)
                    pending.extend(node)
            return True
        base = 'def run(act, params):\n    x = act.find("known")\n'
        for bad, good, reason in (
                (base.replace('known', 'CANARY'), base, Reason.MISSING),
                (base + '    act.type(x, slot="CANARY", expect="goal")\n',
                 base + '    act.type(x, slot="input", expect="goal")\n', Reason.FOREIGN),
                (base + '    act.harvest(x, slot_out="CANARY", max_bytes=1)\n',
                 base + '    act.harvest(x, slot_out="output", max_bytes=1)\n', Reason.FOREIGN),
                (base + '    act.wait_until("CANARY", timeout_ms=1)\n',
                 base + '    act.wait_until("goal", timeout_ms=1)\n', Reason.MISSING),
                (base + '    act.wait_until("oversized", timeout_ms=1)\n',
                 base + '    act.wait_until("goal", timeout_ms=1)\n', Reason.BOUNDS)):
            events, ports = self._ports(resolve=_resolve)
            self._admission_reject(bad, ports, reason)
            self.assertEqual(events[-2:], ['close', 'release'])
            events, ports = self._ports(resolve=_resolve)
            self.assertTrue(gate.admit_source(good.encode(), _SOURCE, _POLICY, **ports))
            self.assertEqual(events[-2:], ['close', 'release'])

    # @anchor:wa.tests.scripts.test_ast_gate.test_observable_supervisor_lifecycle
    def test_observable_supervisor_lifecycle(self):
        # Real child parsing/lifecycle through a synthetic host port; this does
        # not certify production containment, latency or memory enforcement.
        for mode, reason in (('stall', Reason.DEADLINE),
                             ('cancel', Reason.CANCELLED), ('healthy', None)):
            with self.subTest(mode=mode):
                events, ports = self._ports()
                ready, cancelled = threading.Event(), threading.Event()
                processes, drained = [], []
                def _cancel():
                    if ready.wait(5):
                        cancelled.set()
                canceller = threading.Thread(target=_cancel)
                if mode == 'cancel':
                    canceller.start()
                def _parse(body, policy, output_bytes):
                    program = ('import ast,pickle,sys; '
                               'pickle.dump(ast.parse(sys.stdin.buffer.read()),sys.stdout.buffer)'
                               if mode == 'healthy' else
                               'import sys; sys.stdout.write("CANARY"); sys.stdout.flush(); sys.stdin.read()')
                    child = subprocess.Popen(
                        [sys.executable, '-c', program],
                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                    processes.append(child)
                    ready.set()
                    if mode == 'cancel':
                        self.assertTrue(cancelled.wait(5))
                        raise ContractError(Reason.CANCELLED)
                    if mode == 'stall':
                        # Keep stdin open: the worker cannot complete naturally.
                        expired = False
                        try:
                            child.wait(timeout=0.05)
                        except subprocess.TimeoutExpired:
                            expired = True
                        self.assertTrue(expired)
                        raise ContractError(Reason.DEADLINE)
                    drained.append(child.communicate(input=body, timeout=5))
                    self.assertEqual(child.returncode, 0)
                    self.assertEqual(drained[-1][1], b'')
                    # Trusted fixture child only; production transport must be
                    # lossless and bounded and must never unpickle candidates.
                    return pickle.loads(drained[-1][0])
                def _close():
                    for child in processes:
                        if child.poll() is None:
                            child.terminate()
                            events.append('terminate')
                            drained.append(child.communicate(timeout=5))
                        child.wait(timeout=5)
                        events.extend(('join', 'drain'))
                    events.append('close')
                    return True
                ports['supervisor'].parse = _parse
                ports['supervisor'].close = _close
                try:
                    if reason is None:
                        self.assertEqual(gate.admit_source(_MINIMAL.encode(), _SOURCE, _POLICY, **ports), ())
                    else:
                        self._admission_reject(_MINIMAL, ports, reason)
                        self.assertIn('terminate', events)
                    self.assertEqual(events[-4:], ['join', 'drain', 'close', 'release'])
                    self.assertIn('release', events)
                    self.assertEqual(len(processes), 1)
                    self.assertEqual(len(drained), 1)
                    for child in processes:
                        self.assertIsNotNone(child.poll())
                        self.assertTrue(child.stdin.closed and child.stdout.closed and child.stderr.closed)
                finally:
                    ready.set()
                    if mode == 'cancel':
                        canceller.join(timeout=5)
                        self.assertFalse(canceller.is_alive())
                    for child in processes:
                        if child.poll() is None:
                            child.kill()
                            child.communicate(timeout=5)

    # @anchor:wa.tests.scripts.test_ast_gate.test_parser_caps_actual_denials
    def test_parser_caps_actual_denials(self):
        # Predicate: complete observed syscall denials plus the exact retained
        # supervisor are required before source dispatch. No Boolean stand-in.
        os.putenv('S1B_SYNTHETIC_SECRET', 'SYNTHETIC_NOT_A_CREDENTIAL')
        try:
            required = ('environment', 'cwd', 'descriptor', 'home', 'workspace',
                        'write', 'socket', 'network', 'children', 'trace', 'signal',
                        'exec', 'executable_mapping', 'executable_protection', 'loading')
            cases = [(None, None)] + [(probe, evidence) for probe in required
                                     for evidence in ('missing', False)]
            for missing, evidence in cases:
                with self.subTest(missing=missing, evidence=evidence):
                    backend = _ContainedBackend(_POLICY)
                    events, ports = self._ports()
                    ports.update(supervisor=backend, verify_supervisor=backend.verify)
                    try:
                        self.assertTrue(backend.verify('retained', backend, _POLICY), backend.probes)
                        self.assertFalse(backend.verify('retained', object(), _POLICY))
                        if missing is not None:
                            if evidence == 'missing':
                                del backend.probes[missing]
                            else:
                                backend.probes[missing] = evidence
                            with patch.object(backend, 'parse', side_effect=AssertionError('dispatch')):
                                self._admission_reject(_PREFIX + _MINIMAL, ports, Reason.MISSING)
                        else:
                            self.assertEqual(gate.admit_source(
                                (_PREFIX + _MINIMAL).encode(), _SOURCE, _POLICY, **ports), ())
                        self.assertTrue(backend.closed)
                        self.assertIsNotNone(backend.child.poll())
                        self.assertTrue(all(stream.closed for stream in (
                            backend.child.stdin, backend.child.stdout, backend.child.stderr)))
                        self.assertEqual(events[-1], 'release')
                    finally:
                        backend.close()
        finally:
            os.unsetenv('S1B_SYNTHETIC_SECRET')

    # @anchor:wa.tests.scripts.test_ast_gate.test_parser_policy_resource_enforcement
    def test_parser_policy_resource_enforcement(self):
        # Predicate: the same filtered backend enforces the supplied parse-time
        # deadline and incremental address-space bound, and joins before release.
        policy = replace(_POLICY, timeout_ms=200, incremental_memory_bytes=8 * 1024 * 1024)
        for mode, reason in (('stall', Reason.DEADLINE),
                             ('memory', Reason.CAPACITY), ('healthy', None)):
            with self.subTest(mode=mode):
                backend = _ContainedBackend(policy, mode)
                events, ports = self._ports()
                ports.update(supervisor=backend, verify_supervisor=backend.verify)
                try:
                    self.assertTrue(backend.verify('retained', backend, policy), backend.probes)
                    if reason is None:
                        self.assertEqual(gate.admit_source(
                            (_PREFIX + _MINIMAL).encode(), _SOURCE, policy, **ports), ())
                    else:
                        with self.assertRaises(ContractError) as caught:
                            gate.admit_source((_PREFIX + _MINIMAL).encode(), _SOURCE, policy, **ports)
                        self.assertIs(caught.exception.reason, reason)
                        self.assertIsNone(caught.exception.__context__)
                    self.assertTrue(backend.closed)
                    self.assertIsNotNone(backend.child.poll())
                    self.assertTrue(all(stream.closed for stream in (
                        backend.child.stdin, backend.child.stdout, backend.child.stderr)))
                    self.assertEqual(events[-1], 'release')
                finally:
                    backend.close()

    # @anchor:wa.tests.scripts.test_ast_gate.test_bounded_contained_transport
    def test_bounded_contained_transport(self):
        # Predicate: bounded streaming rejects floods and malformed envelopes
        # before decoding, and cleanup completes before capacity is released.
        body = 'def run(act, params):\n    x = act.find("known")\n'
        expected = (('x', 'find', 'query', 'known', ()),)
        measured_size = None
        for mode, reason in (('healthy', None), ('flood', Reason.BOUNDS),
                             ('stderr_flood', Reason.BOUNDS),
                             ('malformed', Reason.SCHEMA), ('corrupt', Reason.SCHEMA),
                             ('boundary', None), ('undersized', Reason.BOUNDS)):
            with self.subTest(mode=mode):
                backend = _ContainedBackend(_POLICY,
                    'healthy' if mode in ('boundary', 'undersized') else mode)
                events, ports = self._ports(resolve=_RecordResolver())
                backend.events = events
                if mode in ('boundary', 'undersized'):
                    self.assertIsNotNone(measured_size)
                    ceiling = measured_size - (mode == 'undersized')
                else:
                    ceiling = 4096
                # Keep AST representation accounting independent of transport.
                def _parse(raw, policy, output_bytes):
                    return _ContainedBackend.parse(backend, raw, policy, ceiling)
                backend.parse = _parse
                ports.update(supervisor=backend, verify_supervisor=backend.verify)
                try:
                    with patch.object(pickle, 'loads', wraps=pickle.loads) as decoder:
                        if reason is None:
                            self.assertEqual(gate.admit_source(
                                body.encode(), _SOURCE, _POLICY, **ports), expected)
                            self.assertEqual(decoder.call_count, 1)
                            if mode == 'healthy':
                                measured_size = backend.transport_size
                            else:
                                self.assertEqual(backend.transport_size, ceiling)
                        else:
                            self._admission_reject(body, ports, reason)
                            decoder.assert_not_called()
                            self.assertEqual(backend.decoded, 0)
                    self.assertLessEqual(backend.buffered_peak, ceiling)
                    self.assertLessEqual(backend.read_peak, ceiling + 1)
                    if mode in ('flood', 'stderr_flood'):
                        self.assertEqual(backend.read_peak, ceiling + 1)
                    self.assertEqual(events[-4:], ['join', 'drain', 'close', 'release'])
                    if mode in ('flood', 'stderr_flood', 'malformed'):
                        self.assertIn('terminate', events)
                        self.assertLess(events.index('terminate'), events.index('join'))
                    self.assertIsNotNone(backend.child.poll())
                    self.assertTrue(backend.closed)
                    self.assertTrue(all(stream.closed for stream in (
                        backend.child.stdin, backend.child.stdout, backend.child.stderr)))
                finally:
                    backend.close()

    # @anchor:wa.tests.scripts.test_ast_gate.test_retained_digit_key_membership
    def test_retained_digit_key_membership(self):
        # Predicate: exact host vocabulary membership, independent of the local
        # handle grammar. Unknown strings and nonstrings must fail.
        for key, reason in (('1', None), ('ENTER', None),
                            ('UNREGISTERED', Reason.SCHEMA), (1, Reason.SCHEMA)):
            with self.subTest(key=key):
                resolver = _RecordResolver()
                events, ports = self._ports(resolve=resolver)
                body = ('def run(act, params):\n    x = act.find("known")\n'
                        f'    act.key(x, keysym={key!r}, expect="goal")\n')
                if reason is None:
                    checked = gate.admit_source(body.encode(), _SOURCE, _POLICY, **ports)
                    self.assertEqual(checked, (
                        ('x', 'find', 'query', 'known', ()),
                        (None, 'key', 'handle', ('handle', 'x'),
                         (('keysym', key), ('expect', 'goal')))))
                    self.assertIn('keysym', resolver.seen)
                else:
                    self._admission_reject(body, ports, reason)
                    if type(key) is str:
                        self.assertIn('keysym', resolver.seen)
                self.assertEqual(events[-1], 'release')
        # Isolate the grammar's exact-string check from vocabulary membership.
        # A trusted resolver accepting everything cannot admit integer keys.
        events, ports = self._ports(resolve=lambda *args: True)
        self._admission_reject(body, ports, Reason.SCHEMA)
        self.assertEqual(events[-2:], ['close', 'release'])

    # @anchor:wa.tests.scripts.test_ast_gate.test_resolver_requires_exact_true
    def test_resolver_requires_exact_true(self):
        # Predicate: each query/keyword resolution needs exact True; false and
        # truthy stand-ins cannot authorize even a structurally valid source.
        source = (b'def run(act, params):\n    x = act.find("known")\n'
                  b'    act.type(x, slot="input", expect="goal")\n')
        expected = (('x', 'find', 'query', 'known', ()),
                    (None, 'type', 'handle', ('handle', 'x'),
                     (('slot', 'input'), ('expect', 'goal'))))
        for role in ('query', 'slot', 'predicate'):
            for accepted in (False, None, 1, 'yes', True):
                with self.subTest(role=role, accepted=accepted):
                    def _resolve(candidate, value, policy):
                        return accepted if candidate == role else True
                    events, ports = self._ports(resolve=_resolve)
                    if accepted is True:
                        self.assertEqual(gate.admit_source(source, _SOURCE, _POLICY, **ports), expected)
                    else:
                        self._admission_reject(source, ports, Reason.SCHEMA)
                    self.assertEqual(events[-2:], ['close', 'release'])

    # @anchor:wa.tests.scripts.test_ast_gate.test_s0_records_and_retained_authority
    def test_s0_records_and_retained_authority(self):
        # Predicate: S0 construction/equality cannot substitute for current
        # owner-retained grants, exact scope/episode/purpose or host limits.
        from wender.scripts import types as t
        body = ('def run(act, params):\n'
                '    x = act.find("known")\n'
                '    act.type(x, slot="input", expect="goal")\n'
                '    act.key(x, keysym="ENTER", expect="goal")\n'
                '    act.wait_until("goal", timeout_ms=17)\n'
                '    act.harvest(x, slot_out="output", max_bytes=19)\n')
        def _bound_ports(resolver):
            events, ports = self._ports(resolve=resolver)
            # Complete closed header fixture, using the actual retained S0
            # scope instead of unrelated shorthand authority. No production
            # header codec or attestation is asserted by this test owner.
            scope = asdict(resolver.scope)
            declarations = []
            for line in _PREFIX.splitlines():
                tag, payload = line.split('] ', 1)
                record = json.loads(payload)
                if tag == '# [SCRIPT_TAG':
                    record['scope'] = scope
                elif tag == '# [SURGERY_DECLARATION':
                    record['entry']['scope'] = scope
                declarations.append(tag + '] ' + json.dumps(record, default=lambda value: value.value))
            prefix = ('\n'.join(declarations) + '\n').encode()
            def _header(raw, policy):
                if not raw.startswith(prefix):
                    raise ContractError(Reason.FOREIGN)
                return raw[len(prefix):]
            ports['header_codec'] = _header
            return events, ports, prefix

        cases = ('healthy', 'foreign_query', 'foreign_predicate', 'foreign_input',
                 'foreign_output', 'wrong_principal', 'wrong_episode', 'wrong_input_purpose',
                 'wrong_output_purpose', 'absent_input_grant', 'absent_output_grant',
                 'cross_purpose_grant', 'copied_grant',
                 'revoked_input_grant', 'revoked_output_grant',
                 'copied_query', 'copied_vocabulary', 'absent_vocabulary_attestation',
                 'unsupported_key', 'unknown_query', 'undeclared_slot', 'missing_predicate', 'expanded_predicate',
                 'slot_count', 'input_bytes', 'output_bytes', 'wait_limit')
        for case in cases:
            with self.subTest(case=case):
                resolver = _RecordResolver()
                foreign = replace(resolver.scope, principal='other-principal')
                policy, source = _POLICY, body
                reason = Reason.FOREIGN
                if case in ('foreign_query', 'copied_query'):
                    resolver.query = replace(resolver.query,
                        scope=foreign if case == 'foreign_query' else resolver.scope)
                    if case == 'foreign_query':
                        resolver.registry.register(resolver.query)
                elif case == 'foreign_predicate':
                    resolver.predicate = resolver.registry.register(replace(resolver.predicate, scope=foreign))
                elif case in ('foreign_input', 'foreign_output'):
                    attribute = 'input' if case == 'foreign_input' else 'output'
                    setattr(resolver, attribute, replace(getattr(resolver, attribute), scope=foreign))
                elif case == 'wrong_principal':
                    resolver.scope = foreign
                elif case == 'wrong_episode':
                    resolver.input = replace(resolver.input, episode_id='other-episode')
                elif case in ('wrong_input_purpose', 'wrong_output_purpose'):
                    attribute = 'input' if case == 'wrong_input_purpose' else 'output'
                    purpose = t.Purpose.HARVEST if attribute == 'input' else t.Purpose.INPUT
                    setattr(resolver, attribute, replace(getattr(resolver, attribute), purpose=purpose))
                elif case in ('absent_input_grant', 'absent_output_grant'):
                    attribute = 'input' if case == 'absent_input_grant' else 'output'
                    slot = getattr(resolver, attribute)
                    # Equal S0 capability, absent from the retained owner registry.
                    setattr(resolver, attribute, replace(slot, capability=replace(slot.capability)))
                elif case == 'cross_purpose_grant':
                    resolver.input = replace(resolver.input, capability=resolver.output.capability)
                elif case == 'copied_grant':
                    resolver.input_grant = replace(resolver.input_grant)
                elif case == 'copied_vocabulary':
                    resolver.vocabulary = replace(resolver.vocabulary)
                elif case == 'absent_vocabulary_attestation':
                    resolver.vocabulary = resolver.registry.register(replace(
                        resolver.vocabulary, attestation=replace(resolver.vocabulary.attestation)))
                elif case == 'unsupported_key':
                    source, reason = body.replace('ENTER', 'UNSUPPORTED'), Reason.SCHEMA
                elif case == 'unknown_query':
                    source, reason = body.replace('"known"', '{"unknown":"known"}'), Reason.SCHEMA
                elif case == 'undeclared_slot':
                    source, reason = body.replace('slot="input"', 'slot="undeclared"'), Reason.MISSING
                elif case == 'missing_predicate':
                    source, reason = body.replace('expect="goal"', 'expect="absent"'), Reason.MISSING
                elif case == 'expanded_predicate':
                    resolver.predicate = resolver.registry.register(t.PredicateSpec(
                        t.PredicateOperator.ALL, (), (resolver.predicate, resolver.predicate),
                        resolver.scope, (t.EvidenceSource.AX,), t.MissingRef(None, Reason.MISSING)))
                    policy, reason = replace(policy, predicate_nodes=2), Reason.BOUNDS
                elif case == 'slot_count':
                    resolver.slot_policy = replace(resolver.slot_policy, slots=1)
                    reason = Reason.BOUNDS
                elif case == 'input_bytes':
                    resolver.slot_policy = replace(resolver.slot_policy, input_slot_bytes=6)
                    reason = Reason.BOUNDS
                elif case == 'output_bytes':
                    resolver.slot_policy = replace(resolver.slot_policy, harvest_result_bytes=18)
                    reason = Reason.BOUNDS
                elif case == 'wait_limit':
                    resolver.wait_limit, reason = 16, Reason.BOUNDS
                events, ports, prefix = _bound_ports(resolver)
                if case in ('revoked_input_grant', 'revoked_output_grant'):
                    # First prove these exact retained grants admit this source.
                    # Revoke only the selected grant, preserving its descriptor,
                    # handle identity, source, scope and all other retained data.
                    admitted = gate.admit_source(prefix + source.encode(), _SOURCE, policy, **ports)
                    self.assertEqual(len(admitted), 5)
                    self.assertEqual(events[-2:], ['close', 'release'])
                    grant = (resolver.input_grant if case == 'revoked_input_grant'
                             else resolver.output_grant)
                    self.assertTrue(resolver._retained(grant))
                    resolver.registry._retained[:] = [
                        row for row in resolver.registry._retained if row[0] is not grant]
                    self.assertFalse(any(item is grant for item in resolver.registry.outstanding()))
                    events, ports, same_prefix = _bound_ports(resolver)
                    self.assertEqual(same_prefix, prefix)
                if case == 'healthy':
                    result = gate.admit_source(prefix + source.encode(), _SOURCE, policy, **ports)
                    self.assertEqual(len(result), 5)
                    self.assertEqual(set(resolver.seen),
                        {'query', 'slot', 'predicate', 'keysym', 'timeout_ms', 'slot_out', 'max_bytes'})
                else:
                    sentinel = object()
                    result = sentinel
                    with self.assertRaises(ContractError) as caught:
                        result = gate.admit_source(prefix + source.encode(), _SOURCE, policy, **ports)
                    self.assertIs(result, sentinel)
                    self.assertEqual(events[-2:], ['close', 'release'])
                    self.assertIs(caught.exception.reason, reason)
                    self.assertIsNone(caught.exception.__context__)
                    # Each failed authority fixture has a fresh same-source
                    # positive control with genuine records and retained grants.
                    healthy = _RecordResolver()
                    if case == 'expanded_predicate':
                        healthy.predicate = healthy.registry.register(t.PredicateSpec(
                            t.PredicateOperator.ALL, (), (healthy.predicate, healthy.predicate),
                            healthy.scope, (t.EvidenceSource.AX,), t.MissingRef(None, Reason.MISSING)))
                    _, good_ports, good_prefix = _bound_ports(healthy)
                    self.assertTrue(gate.admit_source(good_prefix + body.encode(), _SOURCE,
                        replace(_POLICY, predicate_nodes=3) if case == 'expanded_predicate' else _POLICY,
                        **good_ports))
                self.assertEqual(events[-2:], ['close', 'release'])

    # @anchor:wa.tests.scripts.test_ast_gate.test_exact_ingress_and_budget_types
    def test_exact_ingress_and_budget_types(self):
        # Predicate: ingress and configuration are exact types, never coercions.
        class _Bytes(bytes):
            pass
        for source in (_MINIMAL, compile(_MINIMAL, '<fixture>', 'exec'),
                       _Bytes(_MINIMAL.encode()), memoryview(_MINIMAL.encode())):
            events, ports = self._ports()
            with self.assertRaises(ContractError) as caught:
                gate.admit_source(source, _SOURCE, _POLICY, **ports)
            self.assertIs(caught.exception.reason, Reason.TYPE)
            self.assertEqual(events, ['close'])
        for budget, reason in ((None, Reason.MISSING), (True, Reason.TYPE),
                               (0, Reason.BOUNDS), (-1, Reason.BOUNDS)):
            events, ports = self._ports()
            ports['representation_bytes'] = budget
            self._admission_reject(_MINIMAL, ports, reason)
            self.assertEqual(events, ['close'])
        for policy, reason in ((None, Reason.MISSING), (object(), Reason.TYPE)):
            self._reject(ast.parse(_MINIMAL), reason, policy)
        events, ports = self._ports()
        self.assertEqual(gate.admit_source(_MINIMAL.encode(), _SOURCE, _POLICY, **ports), ())

    # @anchor:wa.tests.scripts.test_ast_gate.test_frozen_header_boundary_pairs
    def test_frozen_header_boundary_pairs(self):
        # Predicate: a complete supported prefix is removed exactly; all body
        # bytes (including ordinary comments, Unicode and EOF) survive.
        body = 'def run(act, params):\n    # ordinary é\n    return None'
        expected = body.encode()
        for separator in ('\n', '\r\n', '\r'):
            events, ports = self._ports()
            raw = (_PREFIX + body).replace('\n', separator).encode()
            self.assertEqual(gate.admit_source(raw, _SOURCE, _POLICY, **ports), ())
            self.assertIn(('parse', expected), events)
        first = _PREFIX.splitlines(keepends=True)[0]
        for prefix, suffix, reason in (
                (first + _PREFIX, '', Reason.SCHEMA),
                (_PREFIX.replace('[IO_CONTRACT]', '[UNKNOWN]'), '', Reason.SCHEMA),
                (_PREFIX.replace('wender-dsl/v1', 'wender-dsl/v999'), '', Reason.SCHEMA),
                (_PREFIX, '\n# [IO_CONTRACT] {}', Reason.SCHEMA),
                (first * (_SOURCE.header_lines + 1), '', Reason.BOUNDS)):
            events, ports = self._ports()
            self._admission_reject((prefix + body + suffix).encode(), ports, reason)
            self.assertFalse(any(type(event) is tuple for event in events))
            self.assertEqual(events[-2:], ['close', 'release'])
        for raw in (b'\xef\xbb\xbf' + _MINIMAL.encode(),
                    _MINIMAL.encode() + b'\x00', b'\xffCANARY'):
            events, ports = self._ports()
            self._admission_reject(raw, ports, Reason.SCHEMA)
            self.assertNotIn('header', events)
        events, ports = self._ports()
        ports['header_codec'] = lambda *args: _MINIMAL.encode() + b'\x00'
        self._admission_reject(_MINIMAL, ports, Reason.SCHEMA)
        self.assertFalse(any(type(event) is tuple for event in events))

    # @anchor:wa.tests.scripts.test_ast_gate.test_cleanup_failure_cannot_admit
    def test_cleanup_failure_cannot_admit(self):
        # Predicate: cleanup failure cannot return checked output, even if a
        # capacity manager attempts to suppress the exception.
        for suppress in (False, True):
            events, ports = self._ports()
            class _Reservation:
                def __enter__(self):
                    events.append('reserve')
                    return True

                def __exit__(self, *exc):
                    events.append('release')
                    return suppress
            def _close():
                events.append('close')
                raise RuntimeError('CANARY')
            ports['reservation'] = _Reservation()
            ports['supervisor'].close = _close
            self._admission_reject(_MINIMAL, ports, Reason.SCHEMA)
            self.assertEqual(events[-1], 'close')
            self.assertNotIn('release', events)
            self.assertEqual(events.count('close'), 1)
        events, ports = self._ports()
        self.assertEqual(gate.admit_source(_MINIMAL.encode(), _SOURCE, _POLICY, **ports), ())
        self.assertEqual(events[-2:], ['close', 'release'])

    # @anchor:wa.tests.scripts.test_ast_gate.test_complete_checked_semantics_and_mutant
    def test_complete_checked_semantics_and_mutant(self):
        # Closed synthetic role schema and grants, deliberately no production ABI.
        # Predicate: every role resolves and output preserves container kinds,
        # keyword order and exact Unicode. Unknown keys/keysym and tighter
        # trusted numeric caps reject the whole draft.
        query = ('dict', (('text', 'e\u0301'), ('options', ('list', (True, None, ('tuple', (1,)))))))
        expected_roles = {
            'query': (query,), 'predicate': ('goal',), 'slot': ('input',),
            'slot_out': ('output',), 'keysym': ('ENTER',),
            'code': ('missing_evidence',), 'timeout_ms': (17,), 'max_bytes': (19,),
        }
        seen = []
        def _resolve(role, value, policy):
            seen.append((role, value))
            return role in expected_roles and value in expected_roles[role]
        body = ('One = act.find({"text": "e\u0301", "options": [True, None, (1,)]})\n'
                'Two = act.find({"text": "e\u0301", "options": [True, None, (1,)]})\n'
                'act.click(One, expect="goal")\n'
                'act.type(Two, expect="goal", slot="input")\n'
                'act.key(One, expect="goal", keysym="ENTER")\n'
                'act.wait_until("goal", timeout_ms=17)\n'
                'act.assert_tag("goal", code="missing_evidence")\n'
                'act.harvest(One, max_bytes=19, slot_out="output")\n'
                'return None')
        source = 'def run(act, params):\n' + '\n'.join('    ' + line for line in body.splitlines()) + '\n'
        events, ports = self._ports(resolve=_resolve)
        checked = gate.admit_source(source.encode(), _SOURCE, _POLICY, **ports)
        self.assertEqual(checked, (
            ('One', 'find', 'query', query, ()), ('Two', 'find', 'query', query, ()),
            (None, 'click', 'handle', ('handle', 'One'), (('expect', 'goal'),)),
            (None, 'type', 'handle', ('handle', 'Two'), (('slot', 'input'), ('expect', 'goal'))),
            (None, 'key', 'handle', ('handle', 'One'), (('keysym', 'ENTER'), ('expect', 'goal'))),
            (None, 'wait_until', 'predicate', 'goal', (('timeout_ms', 17),)),
            (None, 'assert_tag', 'predicate', 'goal', (('code', 'missing_evidence'),)),
            (None, 'harvest', 'handle', ('handle', 'One'), (('slot_out', 'output'), ('max_bytes', 19))),
        ))
        self.assertEqual(set(role for role, _ in seen), set(expected_roles))
        for old, new in (('"text"', '"unknown"'), ('ENTER', 'CANARY'),
                         ('timeout_ms=17', 'timeout_ms=18'), ('max_bytes=19', 'max_bytes=20')):
            events, ports = self._ports(resolve=_resolve)
            self._admission_reject(source.replace(old, new), ports, Reason.SCHEMA)
        events, ports = self._ports(resolve=lambda *args: False)
        self._admission_reject(source, ports, Reason.SCHEMA)
        with patch.object(gate, '_validate', side_effect=ContractError(Reason.SCHEMA)):
            events, ports = self._ports(resolve=_resolve)
            self._admission_reject(source, ports, Reason.SCHEMA)

    # @anchor:wa.tests.scripts.test_ast_gate.test_no_diagnostic_repr_or_candidate_execution
    def test_no_diagnostic_repr_or_candidate_execution(self):
        # Predicate: admission does not allocate a result repr or execute code;
        # an executable suffix poisons the entire source.
        source = 'def run(act, params):\n    x = act.find("q")\n'
        for suffix in ('', '    import os\n    os.system("CANARY")\n'):
            events, ports = self._ports()
            with patch.object(gate, 'repr', side_effect=AssertionError('repr'), create=True), \
                    patch('builtins.eval', side_effect=AssertionError('eval')), \
                    patch('builtins.exec', side_effect=AssertionError('exec')), \
                    patch('os.system', side_effect=AssertionError('shell')), \
                    patch('subprocess.Popen', side_effect=AssertionError('process')):
                if suffix:
                    self._admission_reject(source + suffix, ports, Reason.SCHEMA)
                else:
                    self.assertEqual(gate.admit_source(source.encode(), _SOURCE, _POLICY, **ports),
                                     (('x', 'find', 'query', 'q', ()),))

    # @anchor:wa.tests.scripts.test_ast_gate.test_assigned_routing_scope
    def test_assigned_routing_scope(self):
        # Predicate: S1B has complete anchors in its own exact ownership manifest.
        # The S0 language control retains its separate, narrower write set.
        from pathlib import Path
        from .support import verify_routing_manifest
        root = Path(__file__).resolve().parents[2]
        manifest = {'wender/scripts/ast_gate.py': 'wa.scripts.ast_gate',
                    'tests/scripts/test_ast_gate.py': 'wa.tests.scripts.test_ast_gate'}
        # The positive fixture is this assignment's manifest; foreign paths
        # below remain rejecting controls independently of the integration diff.
        changed = sorted(manifest)
        self.assertTrue(verify_routing_manifest(root, manifest, changed))
        for forbidden in ('wender/scripts/types.py', 'tests/scripts/support.py'):
            with self.assertRaises(ContractError) as caught:
                verify_routing_manifest(root, manifest, changed + [forbidden])
            self.assertIs(caught.exception.reason, Reason.FOREIGN)
        alias = 'wender/scripts/../scripts/ast_gate.py'
        aliased_manifest = {alias: manifest['wender/scripts/ast_gate.py']}
        with self.assertRaises(ContractError) as caught:
            verify_routing_manifest(root, aliased_manifest, [alias])
        self.assertIs(caught.exception.reason, Reason.FOREIGN)

    # @anchor:wa.tests.scripts.test_ast_gate.test_current_evidence_before_success
    def test_current_evidence_before_success(self):
        # Predicate: evidence revoked during parsing or resolution denies output.
        for revoked in (True, False):
            events, ports = self._ports()
            live = [True]
            observed = []
            def _verify(*args):
                observed.append(live[0])
                return live[0]
            def _resolve(*args):
                live[0] = not revoked
                return True
            ports.update(verify_supervisor=_verify, resolve=_resolve)
            source = b'def run(act, params):\n    x = act.find("q")\n'
            if revoked:
                self._admission_reject(source, ports, Reason.MISSING)
            else:
                self.assertEqual(gate.admit_source(source, _SOURCE, _POLICY, **ports),
                                 (('x', 'find', 'query', 'q', ()),))
            self.assertEqual(observed, [True, not revoked])
            self.assertEqual(events[-2:], ['close', 'release'])

    # @anchor:wa.tests.scripts.test_ast_gate.test_explicit_peak_and_cleanup_evidence
    def test_explicit_peak_and_cleanup_evidence(self):
        # Predicate: absent aggregate reservation evidence cannot decode/parse;
        # absent cleanup completion retains the reservation and denies output.
        for peak, cleanup, accepted in ((None, True, False), (True, None, False),
                                        (True, False, False), (True, True, True)):
            events, ports = self._ports()
            class _Reservation:
                def __enter__(self):
                    events.append('reserve')
                    return peak

                def __exit__(self, *exc):
                    events.append('release')
                    return False
            ports['reservation'] = _Reservation()
            def _close():
                events.append('close')
                return cleanup
            ports['supervisor'].close = _close
            if accepted:
                self.assertEqual(gate.admit_source(_MINIMAL.encode(), _SOURCE, _POLICY, **ports), ())
                self.assertEqual(events[-2:], ['close', 'release'])
            else:
                self._admission_reject(_MINIMAL, ports, Reason.MISSING)
                self.assertNotIn('release', events)
                if peak is not True:
                    self.assertEqual(events, ['reserve', 'close'])
