"""Persist owner-scoped deployment defaults for the local-open profile."""

from dataclasses import dataclass
import os
from pathlib import Path
import stat

from ..contracts.validation import ContractError, WIRE_PATH, strict_json
from ..contracts.wire import record_bytes
from ..store import Policy
from ..store.files import DIR_FLAGS, Files
from ..transport.access import ACL, ComparisonGrant, Grant, Scope


FROZEN_DIR = WIRE_PATH.parent / "frozen"
APP_DIR = "viewpoint-retention"
WRITE_VERBS = ("admit", "start", "claim", "upload", "finish")
DEFAULTS_FILE = ("defaults.json",)


def _check_directory(path, *, private):
    entry = path.lstat()
    forbidden = 0o077 if private else 0o022
    if (not stat.S_ISDIR(entry.st_mode) or entry.st_uid != os.geteuid()
            or entry.st_mode & forbidden or path.resolve() != path):
        raise ContractError("local retention requires an owner-controlled canonical directory")


def _sync_parent(path):
    fd = os.open(path.parent, DIR_FLAGS)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _user_directory(path, *, private, parents=False):
    if not path.is_absolute() or path.resolve() != path:
        raise ContractError("local retention directories must be canonical and absolute")
    for parent in reversed(path.parents):
        try:
            entry = parent.lstat()
        except FileNotFoundError:
            if not parents:
                raise
            parent.mkdir(mode=0o700, exist_ok=True)
            _sync_parent(parent)
            entry = parent.lstat()
        if (not stat.S_ISDIR(entry.st_mode) or entry.st_uid not in {0, os.geteuid()}
                or (entry.st_mode & 0o022 and not entry.st_mode & stat.S_ISVTX)):
            raise ContractError("local retention directory has an unprotected ancestor")
    path.mkdir(mode=0o700, exist_ok=True)
    _check_directory(path, private=private)
    _sync_parent(path)
    return path


def _state_directory():
    configured = os.environ.get("XDG_STATE_HOME")
    base = Path(configured) if configured else Path.home() / ".local" / "state"
    _user_directory(base, private=False, parents=True)
    return _user_directory(base / APP_DIR, private=True)


def _runtime_directory(state):
    configured = os.environ.get("XDG_RUNTIME_DIR")
    if configured:
        base = Path(configured)
        _check_directory(base, private=True)
        return _user_directory(base / APP_DIR, private=True)
    return _user_directory(state / "run", private=True)


def _defaults():
    uid = os.geteuid()
    base = f"local/uid/{uid}"
    policy = Policy("local-policy/v1")
    return {
        "profile": "local-open",
        "uid": uid,
        "gid": os.getegid(),
        "socket_mode": 0o600,
        "store_directory": "store",
        "endpoints": {
            "writer": {"socket": "retention.sock", "principal": f"{base}/writer", "tenant": base},
            "inspector": {"socket": "inspect.sock", "principal": f"{base}/inspector", "tenant": base},
            "denial": {"socket": "denial.sock", "principal": f"{base}/denial", "tenant": f"{base}/control"},
        },
        "acl_revision": "local-acl/v1",
        "policy_revision": policy.revision,
        "policy": {
            "object_bytes": policy.object_bytes,
            "attempt_bytes": policy.attempt_bytes,
            "tenant_bytes": policy.tenant_bytes,
            "snapshot_bytes": policy.snapshot_bytes,
            "retention_seconds": int(policy.retention.total_seconds()),
            "purge_window_seconds": int(policy.purge_window.total_seconds()),
            "snapshot_lifetime_seconds": int(policy.snapshot_lifetime.total_seconds()),
        },
    }


@dataclass(frozen=True)
class LocalDeployment:
    store: Path
    runtime: Path
    defaults: dict

    def socket_path(self, role):
        return self.runtime / self.defaults["endpoints"][role]["socket"]

    def acl(self):
        writer, inspector, denial = (self.defaults["endpoints"][role] for role in ("writer", "inspector", "denial"))
        principal, tenant = writer["principal"], writer["tenant"]
        grants = (
            *(Grant(principal, tenant, verb) for verb in (*WRITE_VERBS, "hold", "recover")),
            *(Grant(principal, denial["tenant"], verb) for verb in WRITE_VERBS),
            Grant(inspector["principal"], tenant, "inspect"),
            Grant(denial["principal"], denial["tenant"], "inspect"),
        )
        comparison = ComparisonGrant(principal, inspector["principal"], tenant, Scope(), Scope(), Scope())
        return ACL(self.defaults["acl_revision"], (), grants, (comparison,))


def load_local():
    state = _state_directory()
    expected = record_bytes(_defaults())
    disk = Files(state, lambda *_: None, wait_for_lock=True)
    try:
        if not disk.exists(DEFAULTS_FILE):
            disk.publish(DEFAULTS_FILE, expected, "local-defaults")
        raw = disk.read(DEFAULTS_FILE, len(expected))
        if raw != expected:
            raise ContractError("persisted local retention defaults failed verification")
        # Sync again before allowing a racing first-run process to use the defaults.
        disk.sync(())
        defaults = strict_json(raw)
        disk.mkdir((defaults["store_directory"],))
    finally:
        disk.close()
    return LocalDeployment(state / defaults["store_directory"], _runtime_directory(state), defaults)
