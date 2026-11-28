# SPDX-License-Identifier: AGPL-3.0-only
"""Canonical JSON, file custody and refusals shared by the studio commands."""
import hashlib
import json
import os
from pathlib import Path
import re
from tempfile import NamedTemporaryFile

HASH = re.compile(r"[a-f0-9]{64}")


class Rejected(RuntimeError):
    def __init__(self, reason, retry_when="the inputs have been corrected", next_command=None):
        super().__init__(reason)
        self.retry_when = retry_when
        self.next_command = next_command

    def record(self, command=None):
        return {"reason": str(self), "retry_when": self.retry_when,
                "next_command": self.next_command or command}


def demand(condition, reason):
    if not condition:
        raise Rejected(reason)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def file_sha(path):
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def checked_json(data):
    def pairs(items):
        result = dict(items)
        demand(len(result) == len(items), "duplicate_json_key")
        return result

    def constant(_):
        raise Rejected("nonfinite_json_number")

    return json.loads(data, object_pairs_hook=pairs, parse_constant=constant)


def shape(value, keys, reason):
    demand(type(value) is dict and value.keys() == set(keys.split()), reason)


def encode(value):
    return (json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + "\n").encode()


def confined(root, name):
    demand(type(name) is str and name != "", "invalid_relative_path")
    relative = Path(name)
    demand(not relative.is_absolute() and relative.parts and ".." not in relative.parts,
           "invalid_relative_path")
    path = root / relative
    demand(path.resolve() == path, "symlink_refused")
    return path


def pin(root, path):
    demand(path.is_file() and path.resolve() == path, "pinned_file_missing_or_symlink")
    return {"path": path.relative_to(root).as_posix(), "sha256": file_sha(path)}


def read_pin(root, ref):
    shape(ref, "path sha256", "invalid_file_pin")
    path = confined(root, ref["path"])
    demand(type(ref["sha256"]) is str and HASH.fullmatch(ref["sha256"])
           and path.is_file() and file_sha(path) == ref["sha256"], "artifact_sha256_mismatch")
    return path


def atomic(path, data):
    with NamedTemporaryFile(dir=path.parent, prefix="." + path.name + ".", delete=False) as temp:
        pending = Path(temp.name)
        try:
            temp.write(data)
            temp.flush()
            os.fsync(temp.fileno())
            temp.close()
            pending.replace(path)
            directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            pending.unlink(missing_ok=True)
