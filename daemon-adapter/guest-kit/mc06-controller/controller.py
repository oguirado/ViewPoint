"""Entry point for H1-ENTRY's isolated headless runner."""
from copy import deepcopy
import hashlib
import json
import os
import stat
import threading
import uuid

from arm_backend import ArmBackend
from contract_guard import (
    Contracts, Invalid, REQUEST_ID, match, pinned_module, validate_config,
)

MAX_RECORD = 65536
MAX_JOURNAL = 4 << 20
MAX_REQUESTS = 128

reference = pinned_module(
    "mc05_reference", "mc05-contract-candidate/reference_model.py",
    "5e1dac59a0d063de270e8ff203dd1f6c14e2a98340019faf9165dacdcd2dcab8",
)


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False).encode("ascii")


class StorageError(RuntimeError):
    pass


class Journal:
    """An exclusively created intent/evidence journal. Existing state bars replay."""

    def __init__(self, state, config):
        self.poisoned = False
        self.sequence = 0
        self.size = 0
        self.head = "0" * 64
        self.directory = os.open(state, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            self.fd = os.open("journal.jsonl", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                              0o600, dir_fd=self.directory)
        except BaseException:
            os.close(self.directory)
            raise
        try:
            st = os.fstat(self.fd)
            if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or stat.S_IMODE(st.st_mode) != 0o600:
                raise StorageError("journal custody")
            self.append("owner", {"config_sha256": hashlib.sha256(encoded(config)).hexdigest()})
        except BaseException:
            self.close()
            raise

    def append(self, kind, data):
        if self.poisoned:
            raise StorageError("journal poisoned")
        row = {"seq": self.sequence + 1, "prev": self.head, "kind": kind, "data": data}
        row["sha256"] = hashlib.sha256(encoded(row)).hexdigest()
        raw = encoded(row) + b"\n"
        try:
            if len(raw) > MAX_RECORD or self.size + len(raw) > MAX_JOURNAL:
                raise StorageError("journal bound")
            pending = memoryview(raw)
            while pending:
                count = os.write(self.fd, pending)
                if count <= 0:
                    raise StorageError("short journal write")
                pending = pending[count:]
            os.fsync(self.fd)
            if self.sequence == 0:
                os.fsync(self.directory)
        except BaseException:
            self.poisoned = True
            raise
        self.sequence += 1
        self.size += len(raw)
        self.head = row["sha256"]

    def close(self):
        os.close(self.fd)
        os.close(self.directory)


class Controller:
    def __init__(self, config, state_dir, backend_factory=ArmBackend):
        self.config = deepcopy(config)
        state = validate_config(self.config, state_dir)
        self.contracts = Contracts()
        self.model = reference.ReferenceModel(self.contracts.validate, self.config["owner_id"],
                                              "firecracker", "aarch64")
        self.lock = threading.Lock()
        self.closed = False
        self.session = None
        self.requests = set()
        self.journal = Journal(state, self.config)
        try:
            self.backend = backend_factory(self.config, self.journal.append)
        except BaseException:
            self.journal.close()
            raise

    def _event(self, request_id, event, data):
        message = {"schema": "headless_event/v0.2", "request_id": request_id,
                   "event_id": "event-" + uuid.uuid4().hex, "event": event, "data": deepcopy(data)}
        self.contracts.validate("headless_event", message)
        if event != "refused":
            self.model.observe(message)
        self.journal.append("event", message)
        return message

    def _refuse(self, request_id, code, disclose=True):
        session = self.session if disclose and self.session else {}
        return self._event(request_id, "refused", {
            "workload_id": session.get("workload_id"), "generation": session.get("generation"),
            "code": code, "effect": "not_dispatched",
        })

    def handle(self, request, actor_id):
        """Yield the durable accepted event before making a native call."""
        if not self.lock.acquire(blocking=False):
            raise Invalid("operation in progress")
        try:
            yield from self._handle(deepcopy(request), actor_id)
        finally:
            self.lock.release()

    def _handle(self, request, actor_id):
        if self.closed or self.journal.poisoned or self.model.pending is not None:
            raise StorageError("controller closed or transaction unresolved")
        if type(request) is not dict or not match(REQUEST_ID, request.get("request_id")):
            raise Invalid("request identity cannot be correlated")
        rid = request["request_id"]
        if type(actor_id) is not str or actor_id != self.config["owner_id"]:
            yield self._refuse(rid, "OWNER_MISMATCH", disclose=False)
            return
        if rid in self.requests:
            yield self._refuse(rid, "DUPLICATE_REQUEST")
            return
        if len(self.requests) >= MAX_REQUESTS:
            raise StorageError("request bound")
        self.requests.add(rid)
        if not self.config["runtime_admitted"]:
            yield self._refuse(rid, "UNAVAILABLE")
            return
        try:
            self.contracts.validate("headless_request", request)
            self.model.submit(request, actor_id)
        except Invalid:
            yield self._refuse(rid, "BAD_REQUEST")
            return
        except reference.Refused as error:
            yield self._refuse(rid, str(error))
            return

        op, params = request["op"], request["params"]
        if op == "create":
            self.session = {
                "schema": "headless_session/v0.2", "workload_id": "workload-" + uuid.uuid4().hex,
                "generation": self.config["generation"], "owner_id": actor_id,
                "profile_id": "headless-sumsq-v1", "backend": "firecracker", "isa": "aarch64",
                "phase": "starting", "ownership": "held", "job": None,
            }
        elif op == "run":
            self.session["phase"] = "running"
            self.session["job"] = {"job_id": params["job_id"], "operation": "sumsq", "n": params["n"],
                                   "status": "accepted", "value": None}
        else:
            self.session["phase"] = "stopping"
        self.journal.append("intent", {"request": request, "session": self.session})
        yield self._event(rid, "accepted", {"op": op, "session": self.session})
        try:
            if op == "create":
                self.backend.create()
                self.session["phase"] = "ready"
            elif op == "run":
                n = params["n"]
                result = self.backend.run(n)
                if type(result) is not int or result != n * (n + 1) * (2 * n + 1) // 6:
                    raise Invalid("unverified SUMSQ result")
                self.session["job"].update(status="completed", value=result)
                self.session["phase"] = "completed"
            else:
                proof = self.backend.stop()
                if type(proof) is not dict or proof.get("closed") is not True:
                    raise Invalid("unproved closure")
                self.journal.append("firecracker_stop", proof)
                self.session.update(phase="stopped", ownership="released")
        except Exception as error:
            if self.journal.poisoned:
                raise
            self.session.update(phase="uncertain", ownership="held")
            if op == "run":
                self.session["job"].update(status="unknown", value=None)
            self.journal.append("failure", {"op": op, "type": type(error).__name__})
            yield self._event(rid, "outcome_unknown", {"op": op, "code": "OUTCOME_UNKNOWN",
                                                     "session": self.session})
            return

        event = "completed" if op == "run" else "state_changed"
        yield self._event(rid, event, {"session": self.session})

    def close(self):
        if not self.lock.acquire(blocking=False):
            raise StorageError("active request during owner shutdown")
        try:
            if self.closed:
                return
            self.closed = True
            try:
                if self.session is not None and self.session["ownership"] == "held":
                    proof = self.backend.emergency_close()
                    self.journal.append("owner_shutdown", proof)
            finally:
                self.journal.close()
        finally:
            self.lock.release()
