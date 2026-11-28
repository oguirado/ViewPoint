# SPDX-License-Identifier: AGPL-3.0-only
"""File contracts shared by the local episode provider and its Blender worker."""
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path, PurePosixPath

IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
DIGEST = re.compile(r"[a-f0-9]{64}\Z")


class Refusal(Exception):
    def __init__(self, reason, detail=""):
        super().__init__(detail or reason)
        self.reason = reason


def require(condition, reason, detail=""):
    if not condition:
        raise Refusal(reason, detail)


def unique_object(pairs):
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError("duplicate JSON key")
    return result


def decode(data):
    return json.loads(data, object_pairs_hook=unique_object, parse_constant=invalid_constant)


def invalid_constant(value):
    raise ValueError(f"nonfinite JSON constant {value}")


def encoded(value):
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode()


def digest(data):
    return hashlib.sha256(data).hexdigest()


def sha_file(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def load(path):
    return decode(Path(path).read_bytes())


def sync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic(path, data, *, immutable=False):
    fd, name = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if immutable:
            try:
                os.link(name, path)
            except FileExistsError:
                require(path.is_file() and path.read_bytes() == data, "immutable_artifact_conflict", path.name)
        else:
            os.replace(name, path)
        sync_dir(path.parent)
    finally:
        Path(name).unlink(missing_ok=True)


def write(path, value, *, immutable=False):
    atomic(path, encoded(value), immutable=immutable)


def relative(name):
    require(isinstance(name, str) and name and "\\" not in name, "invalid_artifact_path")
    path = PurePosixPath(name)
    require(not path.is_absolute() and ".." not in path.parts and str(path) == name and name != ".",
            "invalid_artifact_path")
    return path


def artifact(root, name):
    path = root / relative(name)
    require(path.resolve() == path and path.is_file(), "artifact_unavailable", name)
    return path


def pin(root, ref, reason="artifact_digest_mismatch"):
    path = artifact(root, ref["path"])
    require(sha_file(path) == ref["sha256"], reason, ref["path"])
    return path


def immutable_commit(pending, target):
    for directory, _, files in os.walk(pending, topdown=False):
        for name in files:
            with (Path(directory) / name).open("rb") as stream:
                os.fsync(stream.fileno())
        sync_dir(directory)
    pending.rename(target)
    sync_dir(target.parent)
