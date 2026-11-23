# @module {"id":"wa.remember.sculpt_store","layer":1,"owner":"REMEMBER","deps":["wa.remember.skills","wa.remember.sculpt","wa.remember.program_map"],"book":"HB-WA-s3-remember"}
"""Atomic metadata-only sculpt revisions with re-admission on every read.

Separate from the legacy Store journal: revisions are immutable, publication
uses sibling staging and os.replace, and a nonblocking directory flock fences
concurrent writers. A checksum proves integrity, not producer authenticity.
No automatic migration, implicit execution, screenshots or mesh bytes.
"""
import fcntl
import json
import os
from pathlib import Path
import stat
import tempfile

from .program_map import canonical_json
from .skills import SkillRejected, admit
from .sculpt import _digest, record_sha256, validate_sculpt_skill

_MAX_BYTES = 8 * 2**20
_FILENAME = "sculpt_skills.json"


def _pairs(pairs):
    value = {}
    for key, child in pairs:
        if key in value:
            raise SkillRejected("duplicate sculpt store key")
        value[key] = child
    return value


def _number(value):
    raise SkillRejected("noninteger sculpt store number")


def _key(skill):
    return skill["id"], skill["sculpt"]["skill_sha256"]


# @anchor:wa.remember.sculpt_store.SculptPublicationUncertain
class SculptPublicationUncertain(OSError):
    """Replacement occurred but directory durability is unproven; read to reconcile."""

    def __init__(self, sha256):
        super().__init__("sculpt snapshot published; directory fsync failed")
        self.published_sha256 = sha256


# @anchor:wa.remember.sculpt_store.SculptSkillStore
class SculptSkillStore:
    """Publish immutable supported revisions; callers still admit every effect."""

    # @anchor:wa.remember.sculpt_store.SculptSkillStore.__init__
    def __init__(self, directory):
        self.directory = Path(directory).absolute()

    def _paths(self):
        if any(path.is_symlink() for path in (self.directory, *self.directory.parents)):
            raise SkillRejected("sculpt store symlink path")
        return self.directory / _FILENAME

    def _load(self):
        path = self._paths()
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            return []
        except OSError as exc:
            raise SkillRejected("sculpt store cannot be opened") from exc
        with os.fdopen(fd, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise SkillRejected("sculpt store must be a regular file")
            raw = stream.read(_MAX_BYTES + 1)
        if len(raw) > _MAX_BYTES:
            raise SkillRejected("sculpt store byte budget")
        try:
            record = json.loads(raw, object_pairs_hook=_pairs,
                                parse_float=_number, parse_constant=_number)
            if (type(record) is not dict or set(record) != {"schema", "skills", "sha256"} or
                    record["schema"] != "wender.sculpt-store/v1" or
                    type(record["skills"]) is not list):
                raise SkillRejected("sculpt store schema")
            unsigned = {key: value for key, value in record.items() if key != "sha256"}
            if record_sha256(unsigned) != record["sha256"]:
                raise SkillRejected("sculpt store checksum")
            if (canonical_json(record) + "\n").encode("utf-8") != raw:
                raise SkillRejected("noncanonical sculpt store")
            entries = record["skills"]
            for skill in entries:
                admit(skill)
                validate_sculpt_skill(skill)
            keys = [_key(skill) for skill in entries]
            if keys != sorted(set(keys)):
                raise SkillRejected("sculpt store ordering or duplicate revision")
            return entries
        except (ValueError, TypeError, KeyError, RecursionError) as exc:
            raise SkillRejected("invalid sculpt store: " + str(exc)) from exc

    # @anchor:wa.remember.sculpt_store.SculptSkillStore.load
    def load(self, *, expected_toolchain_sha256):
        """Re-admit history and require the caller's current toolchain identity."""
        _digest(expected_toolchain_sha256)
        entries = self._load()
        if any(skill["sculpt"]["toolchain_sha256"] != expected_toolchain_sha256 for skill in entries):
            raise SkillRejected("sculpt toolchain stale")
        return entries

    # @anchor:wa.remember.sculpt_store.SculptSkillStore.publish
    def publish(self, skill):
        """Validate before IO; atomically add a revision under an exclusive lock."""
        admitted = json.loads(admit(skill))
        validate_sculpt_skill(admitted)
        path = self._paths()
        self.directory.mkdir(parents=True, exist_ok=True)
        directory_fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        temporary = None
        try:
            fcntl.flock(directory_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            entries = self._load()
            for previous in entries:
                if _key(previous) == _key(admitted):
                    if previous != admitted:
                        raise SkillRejected("conflicting sculpt revision")
                    return
            entries.append(admitted)
            entries.sort(key=_key)
            record = {"schema": "wender.sculpt-store/v1", "skills": entries}
            record["sha256"] = record_sha256(record)
            raw = (canonical_json(record) + "\n").encode("utf-8")
            if len(raw) > _MAX_BYTES:
                raise SkillRejected("sculpt store byte budget")
            with tempfile.NamedTemporaryFile(dir=self.directory, delete=False) as stream:
                temporary = stream.name
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            temporary = None
            try:
                os.fsync(directory_fd)
            except OSError as exc:
                # This is a committed-but-uncertain publication, not a refusal
                # that promises unchanged caller bytes. Never blindly retry.
                import hashlib
                raise SculptPublicationUncertain(hashlib.sha256(raw).hexdigest()) from exc
        finally:
            if temporary is not None:
                os.unlink(temporary)
            os.close(directory_fd)
