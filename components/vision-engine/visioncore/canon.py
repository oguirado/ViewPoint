# @module {"id":"vc.canon","layer":0,"stage":"L0","owner":"ARCH","deps":["vc.errors"],"book":"HB-VC-a0-arch"}
"""@module vc.canon L0 - canonical JSON, sha256 helpers, staged atomic writes (SPEC_INT R3/R4)."""
import hashlib
import json
import os
from pathlib import Path

from .errors import VqaError

SCHEMA_DIR = Path(__file__).resolve().parent / "schemas"


def _check_ints(o):
    if isinstance(o, float):
        raise VqaError("E_FLOAT", {"value": repr(o)})
    if isinstance(o, dict):
        for k, v in o.items():
            if not isinstance(k, str):
                raise VqaError("E_SCHEMA", {"key": repr(k)})
            _check_ints(v)
    elif isinstance(o, (list, tuple)):
        for v in o:
            _check_ints(v)


# @anchor:vc.canon.dumps
def dumps(o):
    _check_ints(o)
    return json.dumps(o, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"


def _no_float(s):
    raise VqaError("E_FLOAT", {"value": s})


def loads(text):
    try:
        return json.loads(text, parse_float=_no_float, parse_constant=_no_float)
    except ValueError as e:
        raise VqaError("E_SCHEMA", {"reason": str(e)[:120]})


def is_canonical(text):
    try:
        return dumps(loads(text)) == text
    except VqaError:
        return False


def sha256_bytes(b):
    return hashlib.sha256(b).hexdigest()


def sha256_obj(o):
    return sha256_bytes(dumps(o).encode("utf-8"))


def write_atomic(path, data):
    """Stage to <path>.tmp then os.replace; a failure leaves the target untouched (R4)."""
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    if isinstance(data, str):
        data = data.encode("utf-8")
    try:
        tmp.write_bytes(data)
        os.replace(tmp, path)
    except OSError as e:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise VqaError("E_IO", {"path": str(path), "reason": str(e)[:80]})


def load_schema(schema_id):
    """schema_id like 'vqa.frame/v1' -> parsed schema dict."""
    name, _, ver = schema_id.partition("/")
    p = SCHEMA_DIR / ("%s.%s.schema.json" % (name, ver))
    if not ver or not p.is_file():
        raise VqaError("E_SCHEMA", {"schema": schema_id})
    return loads(p.read_text(encoding="utf-8"))
