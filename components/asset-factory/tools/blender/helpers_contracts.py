"""GLB-specific contract adapters around the frozen F0 kernel.

Factory records use af.* (INTERFACES_F0 C1). GLB floats stay inside GLB;
manifest transform values are exact binary64 hex strings, not quantized numbers.
Stage OK is structural completion, never asset appearance PASS.
"""
import hashlib
import ctypes
import errno
import json
import math
import os
import stat
from pathlib import Path

from asset_factory import core

STATUS, VERDICT = core.STATUS, core.VERDICT
MAX_BYTES = 64 * 2**20  # DESIGN_CHOICE resource bound, not a measured asset limit.


class GlbError(Exception):
    def __init__(self, code, rc=2):
        super().__init__(code)
        self.code, self.rc = code, rc


def record(rc, *reasons, **fields):
    return {'rc': rc, 'status': STATUS[rc], 'reason_codes': list(reasons), **fields}


def canonical(value):
    try:
        return core.canonical_json(value).encode('utf-8')
    except core.ContractError as exc:
        raise GlbError(exc.code) from exc


def digest(data):
    return hashlib.sha256(data).hexdigest()


def json_digest(obj):
    try:
        return core.sha256_json(obj)
    except core.ContractError as exc:
        raise GlbError(exc.code) from exc


def validate_record(value, schema):
    try:
        core.validate(value, schema)
    except core.ContractError as exc:
        raise GlbError(exc.code) from exc


def pairs(items):
    result = {}
    for k, v in items:
        if k in result:
            raise GlbError('duplicate_key')
        result[k] = v
    return result


def parse_json(data):
    try:
        return json.loads(data, object_pairs_hook=pairs,
                          parse_constant=lambda _: (_ for _ in ()).throw(GlbError('nonfinite')))
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise GlbError('malformed_json') from exc


def read_bytes(path):
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    with os.fdopen(fd, 'rb') as f:
        if not stat.S_ISREG(os.fstat(f.fileno()).st_mode):
            raise GlbError('non_regular_input')
        data = f.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise GlbError('input_budget', 6)
    return data


def closed(obj, fields):
    if not isinstance(obj, dict) or set(obj) != set(fields):
        raise GlbError('schema_mismatch')


def sha(value):
    if not isinstance(value, str) or len(value) != 64 or any(c not in '0123456789abcdef' for c in value):
        raise GlbError('invalid_sha256')
    return value


def integer(value, minimum=0):
    if type(value) is not int or value < minimum:
        raise GlbError('invalid_integer')
    return value


def number(value):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise GlbError('invalid_number')
    return value


def write_synced(path, data):
    with Path(path).open('xb') as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def publish_directory(stage, target):
    """Linux atomic fresh-directory commit; never replace even an empty target.

    Unsupported libc/kernel/filesystem is a refusal, never an os.replace fallback.
    File and staging-directory data are synced before this visibility boundary.
    Crash durability of the parent directory is outside this stage's claim.
    """
    libc = ctypes.CDLL(None, use_errno=True)
    try:
        rename = libc.renameat2
    except AttributeError as exc:
        raise GlbError('atomic_publish_unavailable', 3) from exc
    rename.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int,
                       ctypes.c_char_p, ctypes.c_uint)
    rename.restype = ctypes.c_int
    if rename(-100, os.fsencode(stage), -100, os.fsencode(target), 1) != 0:
        error = ctypes.get_errno()
        if error in (errno.ENOSYS, errno.EINVAL, errno.EOPNOTSUPP):
            raise GlbError('atomic_publish_unavailable', 3)
        if error in (errno.EEXIST, errno.ENOTEMPTY):
            raise GlbError('destination_exists')
        raise OSError(error, os.strerror(error))


def bound_path(root, relative):
    if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
        raise GlbError('path_escape')
    parts = Path(relative).parts
    if '..' in parts:
        raise GlbError('path_escape')
    path = Path(root) / relative
    # No symlinks, including in an otherwise in-root path.
    probe = Path(root)
    for part in parts:
        probe = probe / part
        if probe.is_symlink():
            raise GlbError('path_escape')
    if not path.resolve().is_relative_to(Path(root).resolve()):
        raise GlbError('path_escape')
    return path
