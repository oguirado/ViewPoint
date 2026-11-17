"""Native permit verification and live, peer-bound lease authority (no models).

The trusted orchestrator writes APPROVAL_PATH. The approved host issuer owns
the lease and Docker container, and serves its in-memory permit digest over a
read-only bind-mounted UNIX socket. A caller-authored JSON flag is insufficient.
Host/Docker administrators are the trust boundary; this is not a signature or
protection from a hostile administrator who can replace the approved program.
"""
from __future__ import annotations

from copy import deepcopy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import secrets
import socket
import stat
import struct
import threading
import time

from asset_factory import core, proc
from .adapter import Refusal

PROVIDER_PATH = Path(__file__).with_name('provider.json')
APPROVAL_PATH = Path('/home/admin/Documentos/construct-rescate/build/night/gen/RUNTIME_APPROVAL.json')
AUTHORITY_DIR = Path('/run/af-native')
MAX_BYTES = 2**20
MAX_ADMISSION_AGE_NS = 30 * 10**9  # Freshness policy [TARGET], not a calibrated performance bound.
SCOPE = '512, low_vram, native inference and GLB export; not appearance acceptance'


def _holder_start_ticks(pid):
    """Lease identity only; this value is never a job or signal selector."""
    return Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()[19]


def need(condition, rc, code):
    if not condition:
        raise Refusal(rc, code)


def _read(path, *, authority=False):
    """One bounded, no-follow read supplies both parsed identity and raw digest."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            need(stat.S_ISREG(info.st_mode), 2, 'authority_file_unsafe')
            if authority:
                need(info.st_uid in (0, os.getuid()) and not info.st_mode & 0o022,
                     2, 'authority_file_unsafe')
            data = stream.read(MAX_BYTES + 1)
        need(len(data) <= MAX_BYTES, 6, 'admission_budget')
        def pairs(items):
            obj = {}
            for key, value in items:
                need(key not in obj, 2, 'admission_json_invalid')
                obj[key] = value
            return obj
        def invalid(_): raise Refusal(2, 'admission_json_invalid')
        obj = json.loads(data, object_pairs_hook=pairs, parse_float=invalid, parse_constant=invalid)
        need(type(obj) is dict, 2, 'admission_json_invalid')
        core.canonical_json(obj)
        return obj, hashlib.sha256(data).hexdigest()
    except FileNotFoundError as exc:
        raise Refusal(3, 'admission_evidence_missing') from exc
    except (ValueError, UnicodeError, core.ContractError) as exc:
        raise Refusal(2, 'admission_json_invalid') from exc


def calibration(provider, provider_path=PROVIDER_PATH):
    reference = provider['runtime']['ram_peak_measurement_ref']
    need(type(reference) is dict, 3, 'calibration_unavailable')
    relative = Path(reference['path'])
    root = Path(provider_path).resolve().parent
    need(not relative.is_absolute() and '..' not in relative.parts, 2, 'calibration_path_invalid')
    path = root / relative
    need(path.resolve().is_relative_to(root), 2, 'calibration_path_invalid')
    value, digest = _read(path)
    need(digest == reference['sha256'], 4, 'calibration_stale')
    need(value.get('scope') == SCOPE and value.get('kind') == 'GB10 native geometry calibration',
         3, 'calibration_unavailable')
    peak = value.get('ram_peak_gib_rounded_up')
    need(type(peak) is int and peak > 0 and peak == provider['runtime']['ram_peak_gb_measured'],
         4, 'calibration_stale')
    runs = value.get('runs')
    need(type(runs) is list and bool(runs), 3, 'calibration_unavailable')
    measured = []
    for run in runs:
        data = run['data']
        need(data.get('pipeline_type') == '512' and data.get('geometry_only') is True and
             data.get('preprocess_image') is False and type(data.get('rc')) is int and data['rc'] == 0,
             3, 'calibration_unavailable')
        number = data.get('cgroup_peak_bytes')
        need(type(number) is int and number > 0, 3, 'calibration_unavailable')
        measured.append(number)
    need((max(measured) + 2**30 - 1) // 2**30 == peak, 4, 'calibration_stale')
    return value


def calibrated_peak(provider, params, *, provider_path=PROVIDER_PATH):
    """No inferred defaults: only the measured workload has a known peak."""
    if (type(params) is not dict or set(params) != {'seed', 'pipeline_type'} or
        params['pipeline_type'] != '512' or type(params['seed']) is not int or
        not 0 <= params['seed'] <= 2**32-1):
        return None, None
    try:
        calibration(provider, provider_path)
    except (Refusal, KeyError, TypeError, OSError):
        return None, None
    return provider['runtime']['ram_peak_gb_measured'], deepcopy(provider['runtime']['ram_peak_measurement_ref'])


def bindings(provider_path=PROVIDER_PATH):
    provider, digest = _read(provider_path)
    value = calibration(provider, provider_path)
    here = Path(__file__).resolve().parent
    return {'provider_sha256': digest, 'weights_inventory_sha256': core.sha256_json(provider['weights']),
            'calibration_sha256': provider['runtime']['ram_peak_measurement_ref']['sha256'],
            'image_id': value['image_id'], 'runtime_source_sha256': core.sha256_file(here/'runtime.py'),
            'admission_source_sha256': core.sha256_file(here/'admission.py'),
            'runner_source_sha256': core.sha256_file(here/'admission_runner.py'),
            'process_guard_source_sha256': core.sha256_file(Path(proc.__file__)),
            'scheduler_source_sha256': core.sha256_file(here.parents[1]/'tools/scheduler/scheduler.py')}


def approved(approval, binding):
    need(approval.get('schema') == 'af.trellis-runtime-approval/v1' and
         approval.get('approved') is True, 3, 'runtime_approval_unavailable')
    fields = {'schema', 'approved', 'approval_id', 'approved_by', 'expires_unix_s',
              'pipeline_type', 'timeout_s', 'ceiling_bytes', 'min_free_bytes',
              'input_sha256', 'seeds', 'commercial_use', 'calibration_basis'}
    need(set(approval) == set(binding) | fields, 2, 'runtime_approval_invalid')
    need(all(approval.get(k) == v for k, v in binding.items()), 4, 'runtime_approval_stale')
    for key in ('approval_id', 'approved_by', 'calibration_basis'):
        need(type(approval.get(key)) is str and bool(approval[key].strip()), 2, 'runtime_approval_invalid')
    expiry = approval.get('expires_unix_s')
    need(type(expiry) is int, 2, 'runtime_approval_invalid')
    need(expiry > int(time.time()), 4, 'runtime_approval_expired')
    need(approval.get('pipeline_type') == '512' and type(approval.get('commercial_use')) is bool,
         2, 'runtime_approval_invalid')
    seeds = approval.get('seeds')
    need(type(seeds) is list and bool(seeds) and
         all(type(n) is int and 0 <= n <= 2**32-1 for n in seeds), 2, 'runtime_approval_invalid')
    digest = approval.get('input_sha256')
    need(type(digest) is str and len(digest) == 64 and all(c in '0123456789abcdef' for c in digest),
         2, 'runtime_approval_invalid')
    for key, low, high in (('timeout_s', 1, 1800), ('ceiling_bytes', 1, 48*2**30),
                           ('min_free_bytes', 20*2**30, 2**63-1)):
        need(type(approval.get(key)) is int and low <= approval[key] <= high, 2, 'runtime_approval_invalid')


def boot_id():
    return Path('/proc/sys/kernel/random/boot_id').read_text().strip()


def mem_available():
    rows = [line.split() for line in Path('/proc/meminfo').read_text().splitlines()
            if line.startswith('MemAvailable:')]
    need(len(rows) == 1 and len(rows[0]) == 3 and rows[0][2] == 'kB' and rows[0][1].isdigit(),
         3, 'memavailable_unknown')
    return int(rows[0][1]) * 1024


class Lease:
    """The original host-wide model lock, held for the entire container lifetime."""
    def __init__(self, path):
        self.path, self.fd = Path(path), None
        self.fence_path = Path(str(self.path)+'.native-fence.json')
        self.fence_identity = None

    def __enter__(self):
        self.fd = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(self.fd); self.fd = None
            raise Refusal(5, 'lease_held') from exc
        if self.fence_path.exists() or self.fence_path.is_symlink():
            self.__exit__(None, None, None)
            raise Refusal(3, 'lease_release_unproven')
        return self

    def arm(self):
        """Crash recovery is manual: allocation intent survives owner death."""
        data = core.canonical_json(self.evidence()).encode()
        fd = os.open(self.fence_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data); stream.flush(); os.fsync(stream.fileno())
            info = os.fstat(stream.fileno())
            self.fence_identity = (info.st_dev, info.st_ino)

    def release_verified(self):
        need(self.held() and self.fence_identity is not None, 3, 'lease_release_unproven')
        info = self.fence_path.stat()
        need((info.st_dev, info.st_ino) == self.fence_identity, 3, 'lease_release_unproven')
        self.fence_path.unlink(); self.fence_identity = None

    def evidence(self):
        need(self.held(), 5, 'lease_not_held')
        info = os.fstat(self.fd)
        return {'path': str(self.path.resolve()), 'device': info.st_dev, 'inode': info.st_ino,
                'holder_pid': os.getpid(), 'holder_start_ticks': _holder_start_ticks(os.getpid())}

    def held(self):
        if self.fd is None:
            return False
        info = os.fstat(self.fd)
        try:
            current = self.path.stat()
            if (info.st_dev, info.st_ino) != (current.st_dev, current.st_ino):
                return False
            fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW)
            try:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    return True
                return False
            finally:
                os.close(fd)
        except OSError:
            return False

    def __exit__(self, *args):
        if self.fd is not None:
            os.close(self.fd); self.fd = None


def make_permit(approval, approval_sha256, binding, *, seed, pipeline_type, input_sha256, container_id, receipt):
    approved(approval, binding)
    need(seed in approval['seeds'] and type(seed) is int and pipeline_type == approval['pipeline_type'] and
         input_sha256 == approval['input_sha256'], 5, 'runtime_operation_unapproved')
    need(receipt.get('rc') == 0 and receipt.get('schema') == 'af.trellis-native-admission/v1',
         3, 'scheduler_admission_unavailable')
    return dict(binding, schema='af.trellis-native-permit/v1', approval_sha256=approval_sha256,
                seed=seed, pipeline_type=pipeline_type, input_sha256=input_sha256, container_id=container_id,
                timeout_s=approval['timeout_s'], scheduler=deepcopy(receipt))


def _fresh(receipt):
    need(type(receipt.get('admitted_monotonic_ns')) is int and receipt['admitted_monotonic_ns'] > 0,
         2, 'scheduler_admission_invalid')
    elapsed = time.monotonic_ns() - receipt['admitted_monotonic_ns']
    need(receipt.get('boot_id') == boot_id() and 0 <= elapsed <= MAX_ADMISSION_AGE_NS, 4, 'admission_stale')


class Authority:
    """Live issuer attestation: no signature, secret, environment flag or disk key."""
    def __init__(self, directory, permit, lease, authorize_peer):
        self.path = Path(directory)/'authority.sock'
        self.permit, self.lease, self.authorize_peer = deepcopy(permit), lease, authorize_peer
        self.digest = core.sha256_json(self.permit)
        self.stop = threading.Event()
        self.used = False

    def __enter__(self):
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.socket.bind(str(self.path)); os.chmod(self.path, 0o600)
        self.socket.listen(4); self.socket.settimeout(0.2)
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()
        return self

    def _serve(self):
        while not self.stop.is_set():
            try:
                client, _ = self.socket.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            with client:
                client.settimeout(3)
                try:
                    data = client.recv(4097)
                    need(0 < len(data) <= 4096, 2, 'admission_budget')
                    request = json.loads(data)
                    pid, uid, _ = struct.unpack('3i', client.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
                    need(type(pid) is int and pid > 1 and uid == os.getuid() and self.authorize_peer(pid),
                         5, 'runtime_peer_unproven')
                    need(self.lease.held(), 5, 'lease_not_held')
                    need(self.lease.evidence() == self.permit['scheduler']['lease'], 4, 'lease_identity_changed')
                    need(request.get('permit_sha256') == self.digest, 4, 'permit_tampered')
                    _fresh(self.permit['scheduler'])
                    need(not self.used, 5, 'permit_replayed')
                    available = mem_available()
                    receipt = self.permit['scheduler']
                    need(available - receipt['measured_peak_bytes'] >= receipt['min_free_bytes'],
                         5, 'headroom_below_floor')
                    self.used = True
                    response = {'rc': 0, 'permit_sha256': self.digest, 'nonce': request['nonce'],
                                'host_mem_available_bytes': available}
                except Refusal as exc:
                    response = {'rc': exc.rc, 'reason': exc.code}
                except (OSError, ValueError, KeyError, TypeError):
                    response = {'rc': 3, 'reason': 'runtime_authority_unavailable'}
                try:
                    client.sendall(core.canonical_json(response).encode())
                except OSError:
                    pass

    def __exit__(self, *args):
        self.stop.set(); self.socket.close(); self.thread.join(timeout=40)
        need(not self.thread.is_alive(), 3, 'runtime_authority_release_unproven')
        self.path.unlink(missing_ok=True)


def _readonly(path):
    need(bool(os.statvfs(path).f_flag & os.ST_RDONLY), 5, 'authority_mount_writable')


def _container_id():
    return Path('/etc/hostname').read_text().strip()


def _attest(permit):
    nonce = secrets.token_hex(16)
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(5)
            client.connect(str(AUTHORITY_DIR/'authority.sock'))
            client.sendall(core.canonical_json({'nonce':nonce, 'permit_sha256':core.sha256_json(permit)}).encode())
            data = bytearray()
            while len(data) <= 4096:
                part = client.recv(4097-len(data))
                if not part: break
                data.extend(part)
            need(len(data) <= 4096, 6, 'admission_budget')
            response = json.loads(data)
    except (OSError, ValueError) as exc:
        raise Refusal(3, 'runtime_authority_unavailable') from exc
    need(type(response.get('rc')) is int, 2, 'runtime_authority_invalid')
    if response['rc'] != 0:
        raise Refusal(response['rc'], response.get('reason', 'runtime_authority_unavailable'))
    need(response.get('nonce') == nonce and response.get('permit_sha256') == core.sha256_json(permit),
         4, 'permit_tampered')


def verify(permit_path, *, image_path, seed, pipeline_type, provider_path=PROVIDER_PATH):
    need(permit_path is not None, 3, 'runtime_admission_unavailable')
    try:
        need(Path(permit_path).absolute() == AUTHORITY_DIR/'permit.json', 2, 'permit_path_invalid')
        _readonly(AUTHORITY_DIR); _readonly(APPROVAL_PATH)
        permit, _ = _read(permit_path, authority=True)
        approval, approval_sha = _read(APPROVAL_PATH, authority=True)
        binding = bindings(provider_path)
        approved(approval, binding)
        _attest(permit)
        need(permit.get('schema') == 'af.trellis-native-permit/v1', 2, 'permit_invalid')
        need(all(permit.get(k) == v for k, v in binding.items()) and
             permit.get('approval_sha256') == approval_sha, 4, 'permit_binding_stale')
        need(type(seed) is int and seed == permit.get('seed') and seed in approval['seeds'] and
             pipeline_type == permit.get('pipeline_type') == approval['pipeline_type'],
             5, 'runtime_operation_unapproved')
        need(permit.get('input_sha256') == approval['input_sha256'] == core.sha256_file(image_path),
             4, 'runtime_input_stale')
        cid = permit.get('container_id')
        need(type(cid) is str and len(cid) == 64 and all(c in '0123456789abcdef' for c in cid) and
             _container_id() in (cid, cid[:12]), 5, 'runtime_container_unproven')
        receipt = permit['scheduler']
        need(receipt.get('schema') == 'af.trellis-native-admission/v1' and type(receipt.get('rc')) is int and
             receipt['rc'] == 0 and receipt.get('reason_codes') == [], 3, 'scheduler_admission_unavailable')
        for key in ('admitted_monotonic_ns', 'host_mem_available_bytes', 'measured_peak_bytes',
                    'ceiling_bytes', 'min_free_bytes'):
            need(type(receipt.get(key)) is int and receipt[key] > 0, 2, 'scheduler_admission_invalid')
        need(type(permit.get('seed')) is int and type(permit.get('timeout_s')) is int,
             2, 'permit_invalid')
        _fresh(receipt)
        provider, _ = _read(provider_path)
        peak, _ = calibrated_peak(provider, {'seed':seed, 'pipeline_type':pipeline_type}, provider_path=provider_path)
        need(peak is not None and receipt['measured_peak_bytes'] == peak*2**30 and
             receipt['calibration_sha256'] == binding['calibration_sha256'] and
             receipt['image_id'] == binding['image_id'] and
             receipt['ceiling_bytes'] == approval['ceiling_bytes'] and
             receipt['min_free_bytes'] == approval['min_free_bytes'] and
             receipt['measured_peak_bytes'] <= receipt['ceiling_bytes'] and
             receipt['host_mem_available_bytes']-receipt['measured_peak_bytes'] >= receipt['min_free_bytes'] and
             permit['timeout_s'] == approval['timeout_s'], 5, 'scheduler_admission_invalid')
        return permit
    except (KeyError, TypeError, ValueError, core.ContractError) as exc:
        raise Refusal(2, 'permit_invalid') from exc
