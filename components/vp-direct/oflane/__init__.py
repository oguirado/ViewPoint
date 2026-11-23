"""File mailbox for the OpenFresnel companion.

Provision one mailbox for a C1 lane identity. The client and worker share a
trusted filesystem; mailbox producers must publish complete, immutable files.
The independent verifier and trusted completion readers share an attestation
key that must be inaccessible to the worker. No key is loaded from disk here.
"""

from contextlib import ExitStack, contextmanager
import fcntl
import hashlib
import hmac
import importlib.util
import json
import os
from pathlib import Path
import re
import stat
from tempfile import NamedTemporaryFile

from jsonschema import ValidationError


def _load_validation():
    path = Path(__file__).resolve().parents[1] / "schemas" / "validation.py"
    spec = importlib.util.spec_from_file_location("vp_oflane_validation", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_validation = _load_validation()
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")
_METACHAR = re.compile(r"""[;&|<>`$\\'"*?\[\]{}()~\x00-\x1f\x7f]""")


class MailboxError(ValueError):
    """Local transport refusal for an invalid or unverifiable exchange."""


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _read(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise MailboxError("MAILBOX_FILE_REQUIRED")
        return handle.read()


def _unique_object(pairs):
    result = {}
    for name, value in pairs:
        if name in result:
            raise MailboxError("DUPLICATE_JSON_KEY")
        result[name] = value
    return result


def _read_json(path):
    def reject_constant(_value):
        raise MailboxError("NONFINITE_JSON_NUMBER")

    return json.loads(_read(path), object_pairs_hook=_unique_object, parse_constant=reject_constant)


def _publish(path, data):
    """Publish once, with atomic visibility and a durable directory entry."""
    with NamedTemporaryFile(dir=path.parent, prefix=".pending-") as pending:
        pending.write(data)
        pending.flush()
        os.fsync(pending.fileno())
        os.link(pending.name, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)


def _literal(value):
    if isinstance(value, dict):
        return all(_literal(k) and _literal(v) for k, v in value.items())
    if isinstance(value, list):
        return all(map(_literal, value))
    return not isinstance(value, str) or _METACHAR.search(value) is None


class Mailbox:
    def __init__(self, root, identity):
        self.root = Path(root).resolve(strict=True)
        self.identity = identity
        self._max_id = os.pathconf(self.root, "PC_NAME_MAX") - len(".reply.json")
        self._state = self.root / ".oflane-state"
        self._state.mkdir(mode=0o700, exist_ok=True)
        if self._state.is_symlink() or not self._state.is_dir():
            raise MailboxError("MAILBOX_STATE_DIRECTORY_REQUIRED")

    def _path(self, request_id, suffix):
        if (not isinstance(request_id, str) or len(request_id) > self._max_id
                or _ID.fullmatch(request_id) is None):
            raise MailboxError("INVALID_REQUEST_ID")
        return self.root / (request_id + suffix)

    @contextmanager
    def _lock(self, scope, key):
        lock_name = scope + "-" + _sha(key.encode("utf-8")) + ".lock"
        fd = os.open(self._state / lock_name, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise MailboxError("MAILBOX_FILE_REQUIRED")
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

    @staticmethod
    def _check_request(request):
        _validation.validate_contract("vp.oflane.req/v1", request)
        context, args = request["context"], request["args"]
        if not _literal(args):
            raise MailboxError("METACHARACTERS_REFUSED")
        for name in ("session_id", "capsule_id"):
            if name in context and name in args and context[name] != args[name]:
                raise MailboxError("TARGET_MISMATCH")

    def _request(self, request_id):
        request = _read_json(self._path(request_id, ".req.json"))
        self._check_request(request)
        if request["request_id"] != request_id:
            raise MailboxError("REQUEST_ID_MISMATCH")
        return request

    def _effective(self, request):
        context = dict(request["context"], caller=self.identity.caller,
                       agent_instance=self.identity.agent_instance)
        return dict(request, context=context)

    def _check_reply(self, request, reply):
        effective = self._effective(request)
        _validation.validate_contract("vp.oflane.reply/v1", reply)
        if (reply["request_id"] != effective["request_id"] or reply["op"] != effective["op"]
                or reply["effective_request_sha256"] != _sha(_validation.canonical_bytes(effective))):
            raise MailboxError("REPLY_REQUEST_MISMATCH")
        result = reply["result"]
        status = result.get("status") if isinstance(result, dict) else None
        if any(isinstance(status, str) and status.strip().casefold() == "accepted"
               for status in (reply["code"], status)):
            raise MailboxError("NON_TERMINAL_REPLY")
        if effective["op"] in _validation._adapter_rows():
            _validation.validate_adapter_exchange(effective, reply)

    def submit(self, request):
        """Publish the unchanged control envelope. An exact retry shares its order."""
        self._check_request(request)
        request_id = request["request_id"]
        path = self._path(request_id, ".req.json")
        payload = _validation.canonical_bytes(request)
        with self._lock("order", request_id):
            try:
                _publish(path, payload)
            except FileExistsError:
                if _validation.canonical_bytes(self._request(request_id)) != payload:
                    raise MailboxError("REQUEST_ID_CONFLICT") from None

    def pending(self):
        """List request files awaiting a worker reply, in request ID order."""
        request_ids = (path.name.removesuffix(".req.json") for path in self.root.glob("*.req.json"))
        return tuple(sorted(request_id for request_id in request_ids
                            if len(request_id) <= self._max_id and _ID.fullmatch(request_id)
                            and not self._path(request_id, ".reply.json").exists()))

    def abort(self, request_id):
        """Request cooperative cancellation. Return False once a reply is visible."""
        self._request(request_id)
        with self._lock("order", request_id):
            if self._path(request_id, ".reply.json").exists():
                return False
            try:
                _publish(self._path(request_id, ".abort"), b"")
            except FileExistsError:
                _read(self._path(request_id, ".abort"))
            return True

    def _aborted(self, request_id):
        try:
            _read(self._path(request_id, ".abort"))
        except FileNotFoundError:
            return False
        return True

    def _refusal(self, request, code, reason):
        return {
            "schema": "vp.control.reply/v1", "request_id": request["request_id"],
            "op": request["op"], "ok": False, "code": code, "result": {},
            "effective_request_sha256": _sha(_validation.canonical_bytes(self._effective(request))),
            "evidence": [], "not_verified": [reason],
        }

    def _finish(self, request, reply):
        self._check_reply(request, reply)
        _publish(self._path(request["request_id"], ".reply.json"), _validation.canonical_bytes(reply))
        return reply

    def run_one(self, request_id, dispatcher):
        """Consume one order via the provisioned C1 dispatcher and its policy gates."""
        request = self._request(request_id)
        reply_path = self._path(request_id, ".reply.json")
        started = self._state / (_sha(request_id.encode("ascii")) + ".started")
        with self._lock("executor", request_id), ExitStack() as resources:
            for name in ("capsule_id", "session_id"):
                key = request["context"].get(name, request["args"].get(name))
                if key is not None:
                    resources.enter_context(self._lock(name, key))
            with self._lock("order", request_id):
                try:
                    reply = _read_json(reply_path)
                except FileNotFoundError:
                    pass
                else:
                    self._check_reply(request, reply)
                    return reply
                if started.exists():
                    return self._finish(
                        request, self._refusal(request, "UNCERTAIN_OUTCOME", "dispatch_interrupted"))
                if self._aborted(request_id):
                    return self._finish(
                        request, self._refusal(request, "HOLD", "aborted_before_dispatch"))
                _publish(started, b"")

            # A concurrent abort can be published while the backend is running.
            reply = dispatcher.dispatch_mailbox(request, self.identity)

            with self._lock("order", request_id):
                if self._aborted(request_id):
                    reply = self._refusal(request, "UNCERTAIN_OUTCOME", "aborted_after_dispatch_claim")
                return self._finish(request, reply)

    def _exchange(self, request_id):
        request = self._request(request_id)
        reply = _read_json(self._path(request_id, ".reply.json"))
        self._check_reply(request, reply)
        return request, reply

    def _sentinel(self, request, reply, key):
        marker = f'<!-- {request["request_id"]}_DONE -->\n'.encode("ascii")
        message = _validation.canonical_bytes([str(self.root), request, reply])
        proof = hmac.digest(key, b"vp.oflane.done/v1\0" + message, "sha256").hex()
        return marker + f"<!-- {proof} -->\n".encode("ascii")

    def completion(self, request_id, key):
        """Return only a terminal reply authenticated by the independent verifier."""
        path = self._path(request_id, ".done")
        _check_key(key)
        try:
            supplied = _read(path)
        except FileNotFoundError:
            return None
        request, reply = self._exchange(request_id)
        if not hmac.compare_digest(supplied, self._sentinel(request, reply, key)):
            raise MailboxError("UNVERIFIED_COMPLETION")
        return reply


def _check_key(key):
    if not isinstance(key, bytes) or len(key) < hashlib.sha256().digest_size:
        raise ValueError("Verifier attestation key must contain at least 32 bytes")


class Verifier:
    """Independent terminal evidence callback and exclusive sentinel publisher."""

    def __init__(self, mailbox, key, verify):
        _check_key(key)
        self.mailbox = mailbox
        self.key = key
        self.verify = verify

    def seal(self, request_id):
        request, reply = self.mailbox._exchange(request_id)
        attestation = self.mailbox._sentinel(request, reply, self.key)
        # Pass copies so verification code cannot change the attested exchange.
        if self.verify(json.loads(_validation.canonical_bytes(request)),
                       json.loads(_validation.canonical_bytes(reply))) is not True:
            raise MailboxError("TERMINAL_EVIDENCE_REQUIRED")
        path = self.mailbox._path(request_id, ".done")
        try:
            _publish(path, attestation)
        except FileExistsError:
            if not hmac.compare_digest(_read(path), attestation):
                raise MailboxError("UNVERIFIED_COMPLETION") from None
