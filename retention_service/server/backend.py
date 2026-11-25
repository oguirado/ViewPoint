"""Connect authenticated transport operations to the durable store."""

from contextlib import contextmanager
from contextvars import ContextVar
from hashlib import sha256

from .. import store
from ..contracts.validation import ContractError
from ..store.model import ATTEMPTS, TOMBSTONES
from ..transport.access import Denied, Grant, Scope
from ..transport.routes import Conflict, Integrity, Outcome, Page, Unsatisfiable, Unavailable
from ..transport.tls import TLSError
from .local import WRITE_VERBS


@contextmanager
def wire_errors():
    try:
        yield
    except store.Unavailable:
        raise Denied from None
    except store.Conflict:
        raise Conflict from None
    except store.RangeError:
        raise Unsatisfiable from None
    except store.IntegrityError:
        raise Integrity from None
    except store.StorageFailure:
        raise Unavailable from None


def record_scope(admission):
    return Scope(admission["request_id"], admission["attempt_id"])


class InterruptedUpload(Exception):
    def __init__(self, error):
        self.error = error


class UploadStream:
    """Let Store.upload verify and publish directly from the framed request."""

    def __init__(self, upload, recheck):
        upload.started = True
        self.chunks = iter(upload.reader.chunks(upload.length))
        self.recheck = recheck
        self.remaining = b""
        self.interrupted = None

    def read(self, size):
        try:
            if not self.remaining:
                self.remaining = next(self.chunks, b"")
            if not self.remaining:
                self.recheck()
                return b""
        except (TLSError, Denied, Unavailable) as exc:
            self.interrupted = exc
            # Store.upload removes an unpublished staging directory on ValueError.
            raise ContractError("upload interrupted") from exc
        block, self.remaining = self.remaining[:size], self.remaining[size:]
        return block


class StoreBackend:
    def __init__(self, root, validator, policy, *, witnesses, fault=None):
        self.request_access = ContextVar("retention_request_access")
        self.witnesses = witnesses
        def reject(*_):
            return False

        self.store = store.Store(
            root, validator, policy, authorize=self._authorize,
            authorize_source=self._authorize_source,
            verify_witness=witnesses.verify_witness if witnesses is not None else reject,
            validate_profile=witnesses.validate_profile if witnesses is not None else reject,
            fault=fault,
        )

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.store.close()

    @contextmanager
    def _request(self, access, verb):
        token = self.request_access.set((access, verb))
        try:
            with wire_errors():
                yield store.Actor(access.tenant, access.principal)
        finally:
            self.request_access.reset(token)

    def ready(self, access, verb):
        self.require_witness_source(verb)
        with self._request(access, verb) as actor:
            return self.store.ready(actor, verb)

    def require_witness_source(self, verb):
        if self.witnesses is None and verb in WRITE_VERBS:
            raise Unavailable("witness source is unavailable")

    @contextmanager
    def transaction(self, access, command):
        try:
            # Keep lookup, authorization and execution serialized across the request.
            with self._request(access, command.route.verb) as actor, self.store.lock:
                yield StoreTransaction(self, command, access, actor)
        except InterruptedUpload as exc:
            raise exc.error from None

    def _access(self, actor):
        access, verb = self.request_access.get((None, None))
        if access is None or (access.tenant, access.principal) != (actor.tenant, actor.principal):
            raise Denied
        return access, verb

    def _authorize(self, actor, verb, attempt, admission):
        access, _ = self._access(actor)
        scope = Scope(attempt_id=attempt) if admission is None else record_scope(admission)
        return access.current().permits(
            actor.principal, actor.tenant, verb, scope, partial=admission is None,
        )

    def _authorize_source(self, actor, destination, source_id, side, source_admission):
        access, verb = self._access(actor)
        acl = access.current()
        writer = destination["executor_id"]
        target = record_scope(destination)
        source = Scope(attempt_id=source_id) if source_admission is None else record_scope(source_admission)
        partial = source_admission is None
        if (not acl.permits(actor.principal, actor.tenant, verb, target)
                or not acl.permits(writer, actor.tenant, "finish", target)):
            return False
        return any(
            rule.tenant == actor.tenant and rule.writer == writer
            and Grant(writer, actor.tenant, "finish", rule.destination).permits(target)
            and Grant(writer, actor.tenant, "inspect", getattr(rule, side)).permits(source, partial=partial)
            and acl.permits(rule.inspector, actor.tenant, "inspect", source, partial=partial)
            for rule in acl.comparisons
        )


class StoreTransaction:
    def __init__(self, backend, command, access, actor):
        self.backend = backend
        self.store = backend.store
        self.command = command
        self.access = access
        self.actor = actor
        self.admission = None

    def _metadata(self, attempt, *, missing_ok=False):
        with self.store.transaction():
            try:
                tenant = self.store._tenant(self.actor.tenant)
            except store.Unavailable:
                if missing_ok:
                    return None
                raise
            path = tenant + (ATTEMPTS, attempt)
            if self.store.disk.exists(tenant + (TOMBSTONES, attempt)):
                raise Denied
            if not self.store.disk.exists(path + ("admission.json",)):
                if missing_ok:
                    return None
                raise Denied
            return self.store._admission(path)

    def scope(self):
        command = self.command
        operation = command.route.operation
        attempt = command.attempt
        if operation in {"recovery", "recovery_item"}:
            return Scope()
        acl = self.access.current()
        if operation == "receipt":
            attempt = self.store.receipts.get((self.actor.tenant, command.reference))
            if attempt is None:
                raise Denied
        partial = Scope(attempt_id=attempt)
        principal, tenant, verb = self.actor.principal, self.actor.tenant, command.route.verb
        if not acl.permits(principal, tenant, verb, partial, partial=True):
            raise Denied
        try:
            self.admission = self._metadata(attempt, missing_ok=operation == "admission")
        except store.IntegrityError:
            if not acl.permits(principal, tenant, verb, partial):
                raise Denied from None
            raise
        return partial if self.admission is None else record_scope(self.admission)

    def sources(self, admission):
        scopes = []
        for side in ("reference", "candidate"):
            attempt = admission[f"{side}_attempt_id"]
            if not self.backend._authorize_source(self.actor, admission, attempt, side, None):
                raise Denied
            scopes.append(record_scope(self._metadata(attempt)))
        return tuple(scopes)

    def compare(self):
        if self.admission["operation"] == "compare":
            return self.sources(self.admission)
        return None

    def _witness(self, operation, value):
        if self.actor.principal != self.admission["executor_id"]:
            raise Denied
        try:
            return self.backend.witnesses.witness(operation, self.actor, self.admission, value)
        except ValueError:
            raise ContractError("witness evidence is invalid") from None
        except OSError:
            raise Unavailable("witness source is unavailable") from None

    def execute(self, value, access):
        command = self.command
        operation = command.route.operation
        self.backend.require_witness_source(command.route.verb)
        attempt, ref, actor = command.attempt, command.reference, self.actor
        if operation == "admission":
            result, created = self.store.admit(actor, value)
        elif operation == "start":
            result, created = self.store.start(
                actor, attempt, value, content_length=len(value),
                sha256=sha256(value).hexdigest(), evidence=self._witness(operation, value),
            )
        elif operation == "claim":
            result = self.store.claim(actor, attempt, value, evidence=self._witness(operation, value))
            created = True
        elif operation == "upload":
            stream = UploadStream(value, lambda: access.require("upload", record_scope(self.admission)))
            try:
                result, created = self.store.upload(
                    actor, attempt, ref, stream, content_length=value.length, sha256=value.digest,
                )
            except ContractError:
                if stream.interrupted is not None:
                    raise InterruptedUpload(stream.interrupted) from None
                raise
            value.verified = True
        elif operation == "finish":
            result, created = self.store.finish(actor, attempt, *value, evidence=self._witness(operation, value))
        elif operation in {"hold", "release"}:
            self.store.hold(actor, attempt, ref, enable=operation == "hold")
            return Outcome(204)
        else:
            return Outcome(200, self._read(operation, value))
        return Outcome(201 if created else 200, result)

    def _read(self, operation, value):
        command = self.command
        attempt, ref, actor = command.attempt, command.reference, self.actor
        if operation == "inspect":
            return self.store.inspect(actor, attempt)
        if operation == "receipt":
            return self.store.receipt(actor, ref)
        if operation == "artifact":
            return self.store.artifact(actor, attempt, ref)
        if operation == "recovery":
            return self.store.recovery_page(actor, **value)
        offset, limit = value
        if operation == "object":
            page = self.store.object(actor, attempt, ref, offset=offset, limit=limit)
        elif operation == "recovery_item":
            page = self.store.recovery_item(actor, ref, offset=offset, limit=limit)
        else:
            raise Integrity
        return Page(page.ref, page.bytes, page.sha256, page.offset, page.data)
