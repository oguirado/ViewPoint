"""S1 session records, exclusive profile reservations and launch custody."""

from contextlib import contextmanager
from dataclasses import dataclass
from functools import lru_cache
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
from typing import Callable
from uuid import uuid4

from jsonschema import Draft202012Validator, ValidationError
from schemas.validation import SCHEMA_DIR, canonical_bytes, validate_contract


CHECKOUT = Path(__file__).resolve().parents[4]
_PROFILE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_XID = re.compile(r"(?:0[xX][0-9A-Fa-f]+|[0-9]+)")


class SessionRefused(RuntimeError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class LaunchNotRunning(SessionRefused):
    """The launcher positively established that no child remains."""


@dataclass(frozen=True)
class SiteProfile:
    """Trusted provisioning for one site and browser selector."""

    profile_id: str
    executable: Path
    display: str
    backend: str
    cdp_allowed: bool


@dataclass(frozen=True)
class OwnedInstance:
    """Witnessed binding and idempotent operations scoped to the owned child."""

    window: dict
    capsule_id: str
    incarnation: str
    stop: Callable[[], None]
    detach: Callable[[], None] | None = None
    cdp_port: int | None = None
    cdp_target_id: str | None = None


@lru_cache(maxsize=None)
def _args_validator(op):
    schema = json.loads((SCHEMA_DIR / "vp.control.req_v1.schema.json").read_bytes())
    for clause in schema["allOf"]:
        match = clause.get("if", {}).get("properties", {}).get("op", {})
        if match.get("const") == op:
            return Draft202012Validator(clause["then"]["properties"]["args"])
    raise SessionRefused("ARGS_SCHEMA_ABSENT", f"Frozen arguments missing for {op}")


def _check_args(op, args):
    try:
        _args_validator(op).validate(args)
    except ValidationError as error:
        raise SessionRefused("-32602", f"Invalid arguments for {op}") from error


@lru_cache(maxsize=1)
def _site_properties():
    schema = json.loads((SCHEMA_DIR / "vp.session_v1.schema.json").read_bytes())
    return schema["properties"]


def _validate_record(record):
    try:
        validate_contract("vp.session/v1", record)
    except ValidationError as error:
        raise SessionRefused("-32602", "Invalid vp.session/v1 record") from error


def _window_key(record):
    window = record.get("window", {})
    xid = window.get("xid", "")
    if not (len(xid) <= 12 and _XID.fullmatch(xid) and window.get("witness", "").strip()
            and record.get("incarnation", "").strip() and record.get("capsule_id", "").strip()):
        raise SessionRefused("identity_mismatch", "A complete native window binding is required")
    value = int(xid, 16 if xid.lower().startswith("0x") else 10)
    if not 0 < value <= 0xFFFFFFFF:
        raise SessionRefused("identity_mismatch", "XID is outside the X11 range")
    return record["display"], value


def _data_home(path):
    path = Path(path) if path is not None else Path(
        os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share"
    )
    if (not path.is_absolute() or path != path.resolve()
            or path.is_relative_to(CHECKOUT)):
        raise ValueError("Session data requires a canonical absolute path outside the checkout")
    return path


def _directory(path, *, private=True):
    path.mkdir(mode=0o700, exist_ok=True)
    info = path.lstat()
    mode = stat.S_IMODE(info.st_mode)
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
            or (mode != 0o700 if private else mode & 0o022)):
        raise ValueError("Session data directory has unsafe ownership or permissions")


def _dedicated_profile(profile_id):
    if (not _PROFILE_ID.fullmatch(profile_id)
            or profile_id.casefold() in {"default", "guest profile", "system profile"}
            or re.fullmatch(r"profile[._-]?[0-9]+", profile_id, re.IGNORECASE)):
        raise SessionRefused("DEFAULT_PROFILE_REFUSED", "A dedicated profile is required")


class SessionRegistry:
    """Cross-process store. Only the registry that launched a child can stop it.

    The broker remains the authority for active leases. If supplied, revoke
    must idempotently fence the identified broker lease before close proceeds.
    Attachment callers supply a record already admitted by the broker and A1.
    If the caller keeps a separate A1 association, detach_attached removes it
    idempotently using only the session ID.
    """

    def __init__(self, sites, launch, *, data_home=None, revoke=None, detach_attached=None):
        home = _data_home(data_home)
        home.mkdir(parents=True, exist_ok=True)
        root = home / "viewpoint"
        _directory(root, private=False)
        self.profile_root = root / "profiles"
        store = root / "session-registry"
        _directory(store)
        self.path = store / "sessions.sqlite3"
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600
                    or info.st_uid != os.geteuid() or info.st_nlink != 1):
                raise ValueError("Session store must be a private regular file")
        finally:
            os.close(fd)
        self._sites = dict(sites)
        self._launch = launch
        self._revoke = revoke
        self._detach_attached = detach_attached
        self._pid = os.getpid()
        self._owned = {}
        with self._db(write=True) as db:
            # Custody metadata is local SQL state. The wire column stores only vp.session/v1.
            db.execute("""CREATE TABLE IF NOT EXISTS sessions (
                session_id TEXT PRIMARY KEY, profile_id TEXT NOT NULL,
                wire BLOB, attached INTEGER NOT NULL, closing INTEGER DEFAULT 0,
                active INTEGER DEFAULT 1
            )""")
            db.execute("""CREATE UNIQUE INDEX IF NOT EXISTS live_profile
                          ON sessions (profile_id) WHERE active = 1""")

    @contextmanager
    def _db(self, *, write=False):
        db = sqlite3.connect(self.path, isolation_level=None)
        try:
            if write:
                db.execute("BEGIN IMMEDIATE")
            yield db
            if write:
                db.commit()
        finally:
            db.close()

    def _site(self, args):
        site = self._sites.get((args["site_group"], args["browser"]))
        if site is None:
            raise SessionRefused("-32602", "Site and browser are not provisioned")
        _dedicated_profile(site.profile_id)
        executable = str(site.executable)
        if executable.startswith(("/snap/", "/var/lib/snapd/")) and args["browser"] != "firefox":
            raise SessionRefused("SNAP_CDP_UNAVAILABLE", "Snap Chromium CDP is unavailable")
        if args["browser"] == "firefox" and site.backend == "cdp":
            raise SessionRefused("BIDI_DEFERRED", "Firefox CDP is deferred")
        for field in ("display", "backend", "cdp_allowed"):
            validator = Draft202012Validator(_site_properties()[field])
            if not validator.is_valid(getattr(site, field)):
                raise SessionRefused("-32602", "Provisioned site is incompatible with vp.session/v1")
        if site.backend == "cdp" and not site.cdp_allowed:
            raise SessionRefused("-32602", "Provisioned site forbids CDP")
        if site.display == ":0" and not site.cdp_allowed and site.backend != "atspi":
            raise SessionRefused("-32602", "A host session without CDP requires AT-SPI")
        return site

    def _reserve(self, db, record, *, attached=False):
        if db.execute("SELECT 1 FROM sessions WHERE profile_id = ? AND active = 1",
                      (record["profile_id"],)).fetchone():
            raise SessionRefused("PROFILE_IN_USE", "This profile already has a reservation")
        db.execute("INSERT INTO sessions(session_id, profile_id, wire, attached) VALUES (?, ?, ?, ?)",
                   (record["session_id"], record["profile_id"],
                    canonical_bytes(record) if attached else None, attached))

    def _check_instance(self, db, record, *, attached=False):
        window_key = _window_key(record)
        if record["state"] == "AGENT" and record["lease"]["holder"] is None:
            raise SessionRefused("NO_LEASE", "An agent session requires a broker lease")
        target = (record.get("cdp_port"), record.get("cdp_target_id"))
        rows = db.execute("SELECT wire, attached FROM sessions WHERE active = 1 AND wire IS NOT NULL")
        for wire, other_attached in rows:
            other = json.loads(wire)
            if (_window_key(other) == window_key or other["incarnation"] == record["incarnation"]
                    or (not attached and not other_attached
                        and other["window"]["pid"] == record["window"]["pid"])
                    or (target[0] is not None
                        and target == (other.get("cdp_port"), other.get("cdp_target_id")))):
                raise SessionRefused("identity_mismatch", "The window or target already has a session")

    def _cancel_reservation(self, session_id):
        with self._db(write=True) as db:
            db.execute("DELETE FROM sessions WHERE session_id = ? AND wire IS NULL", (session_id,))

    def vp_session_open(self, args):
        _check_args("vp_session_open", args)
        if self._pid != os.getpid():
            raise SessionRefused("OWNER", "An inherited registry cannot launch an owned child")
        site = self._site(args)
        session_id = f"s-{uuid4().hex}"
        record = {
            "schema": "vp.session/v1", "session_id": session_id, "kind": args["kind"],
            "site_group": args["site_group"], "profile_id": site.profile_id,
            "display": site.display, "backend": site.backend, "cdp_allowed": site.cdp_allowed,
            "state": "LOGIN_PENDING", "lease": {"holder": None, "authority_epoch": 0},
            "pace": dict(args["pace"]), "risk_ceiling": args["risk_ceiling"],
        }
        if record["kind"] == "browser":
            record["browser"] = args["browser"]
        with self._db(write=True) as db:
            self._reserve(db, record)
        try:
            _directory(self.profile_root)
            _directory(self.profile_root / site.profile_id)
        except (OSError, ValueError):
            self._cancel_reservation(session_id)
            raise
        try:
            instance = self._launch(site, self.profile_root / site.profile_id)
        except LaunchNotRunning:
            self._cancel_reservation(session_id)
            raise
        except Exception as error:
            raise SessionRefused("UNCERTAIN_OUTCOME", "Launch custody is unresolved") from error
        if not isinstance(instance, OwnedInstance) or not callable(instance.stop):
            raise SessionRefused("UNCERTAIN_OUTCOME", "Launcher returned no owned-child handle")
        try:
            record.update(window=dict(instance.window), capsule_id=instance.capsule_id,
                          incarnation=instance.incarnation)
            if site.backend == "cdp":
                record.update(cdp_port=instance.cdp_port, cdp_target_id=instance.cdp_target_id)
            _validate_record(record)
            with self._db(write=True) as db:
                self._check_instance(db, record)
                self._owned[session_id] = instance
                db.execute("UPDATE sessions SET wire = ? WHERE session_id = ?",
                           (canonical_bytes(record), session_id))
        except Exception:
            try:
                instance.stop()
            except Exception as error:
                raise SessionRefused("UNCERTAIN_OUTCOME", "Owned-child cleanup is unresolved") from error
            self._owned.pop(session_id, None)
            self._cancel_reservation(session_id)
            raise
        return record

    def register_attached(self, record):
        """Register a foreign instance supplied by the trusted attachment layer."""
        _validate_record(record)
        if record["state"] not in {"LOGIN_PENDING", "HUMAN", "AGENT"}:
            raise SessionRefused("-32602", "Only a live binding can be attached")
        _dedicated_profile(record["profile_id"])
        with self._db(write=True) as db:
            self._check_instance(db, record, attached=True)
            self._reserve(db, record, attached=True)

    def get_session(self, session_id):
        """Read a published binding for registry consumers such as S3."""
        with self._db() as db:
            row = db.execute("""SELECT wire FROM sessions
                                WHERE session_id = ? AND active = 1 AND closing = 0""",
                             (session_id,)).fetchone()
        if row and row[0] is not None:
            record = json.loads(row[0])
            _validate_record(record)
            return record
        return None

    def vp_session_list(self):
        with self._db() as db:
            rows = db.execute("""SELECT session_id, wire FROM sessions
                                 WHERE active = 1 AND closing = 0 ORDER BY session_id""").fetchall()
        return [{"session_id": session_id, "state": json.loads(wire)["state"] if wire else "LAUNCHING"}
                for session_id, wire in rows]

    def vp_session_close(self, session_id):
        _check_args("vp_session_close", {"session_id": session_id})
        reply = {"session_id": session_id, "state": "CLOSED"}
        with self._db(write=True) as db:
            row = db.execute("SELECT wire, attached, active FROM sessions WHERE session_id = ?",
                             (session_id,)).fetchone()
            if not row or row[0] is None:
                raise SessionRefused("NO_LEASE", "No published session to close")
            wire, attached, active = row
            if not active:
                return reply
            instance = self._owned.get(session_id) if self._pid == os.getpid() else None
            if not attached and instance is None:
                raise SessionRefused("OWNER", "The owned-child handle is unavailable")
            record = json.loads(wire)
            lease = record["lease"]
            if lease["holder"] is not None and self._revoke is None:
                raise SessionRefused("NO_LEASE", "An active broker lease requires a close fence")
            db.execute("UPDATE sessions SET closing = 1 WHERE session_id = ?", (session_id,))

        # Closing is already durable. A failed cleanup keeps the profile reserved.
        with self._db(write=True) as db:
            if not db.execute("SELECT active FROM sessions WHERE session_id = ?",
                              (session_id,)).fetchone()[0]:
                return reply
            try:
                if lease["holder"] is not None:
                    self._revoke(session_id, lease)
                if attached:
                    if self._detach_attached is not None:
                        self._detach_attached(session_id)
                else:
                    if instance.detach is not None:
                        instance.detach()
                    instance.stop()
            except Exception as error:
                raise SessionRefused("UNCERTAIN_OUTCOME", "Session close is unresolved") from error
            record["state"] = "CLOSED"
            record["lease"] = {"holder": None, "authority_epoch": lease["authority_epoch"] + 1}
            _validate_record(record)
            db.execute("UPDATE sessions SET wire = ?, active = 0 WHERE session_id = ?",
                       (canonical_bytes(record), session_id))
        self._owned.pop(session_id, None)
        return reply
