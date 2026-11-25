# SPDX-License-Identifier: AGPL-3.0-only
"""Outside-capsule control, durable attempts, and custody of exchange results."""

from contextlib import ExitStack, closing, contextmanager
from hashlib import sha256
import os
from pathlib import Path
import re
import sqlite3
import stat
import subprocess
import tempfile

from .compiler import (
    ARGUMENTS, OPERATION_ID, REQUEST_ID, REQUEST, REPLY, SELECTOR, SLOT_ID, Catalog, Refusal,
    canonical, closed, control_overlay, defaults, digest, parse, pinned, validate,
)

EXCHANGE_REQUEST = "vp3d.host_exchange.req/v1"
EXCHANGE_REPLY = "vp3d.host_exchange.reply/v1"
ATTEMPT_RECEIPT = "vp3d.host_attempt_receipt/v1"
DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
INPUT_LIMIT = 16 * 1024 * 1024
CHILD_ENV = {"LC_ALL": "C.UTF-8", "LANG": "C.UTF-8", "TZ": "UTC",
             "PYTHONDONTWRITEBYTECODE": "1", "PYTHONHASHSEED": "0"}


class Attempts:
    """Keep the admitted route snapshot with its claim even after deregistration."""

    def __init__(self, path):
        self.path = path
        with self.transaction() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS host_attempts (
                id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, route BLOB,
                reply BLOB, pending_effect TEXT UNIQUE, policy TEXT NOT NULL
            )""")

    @contextmanager
    def transaction(self):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("PRAGMA synchronous = FULL")
            db.execute("BEGIN IMMEDIATE")
            yield db

    def claim(self, request_id, route, normalize):
        with self.transaction() as db:
            previous = db.execute(
                "SELECT fingerprint, route, reply, policy FROM host_attempts WHERE id = ?", (request_id,)
            ).fetchone()
            if previous:
                fingerprint, snapshot, reply, previous_policy = previous
                try:
                    candidate = normalize(parse(snapshot), previous_policy)
                except Refusal as exc:
                    raise Refusal("REQUEST_ID_CONFLICT") from exc
                if digest(candidate) != fingerprint:
                    raise Refusal("REQUEST_ID_CONFLICT")
                if reply is None:
                    raise Refusal("UNCERTAIN_OUTCOME", fingerprint)
                return candidate, parse(reply)
            effective = normalize(route)
            db.execute("INSERT INTO host_attempts (id, fingerprint, route, policy) VALUES (?, ?, ?, ?)",
                       (request_id, digest(effective), canonical(route), effective["policy_sha256"]))
            return effective, None

    def start_effect(self, request_id, effect):
        with self.transaction() as db:
            existing = db.execute("SELECT id FROM host_attempts WHERE pending_effect = ?",
                                  (effect,)).fetchone()
            if existing:
                return False
            changed = db.execute(
                "UPDATE host_attempts SET pending_effect = ? WHERE id = ? AND reply IS NULL",
                (effect, request_id),
            )
            if changed.rowcount != 1:
                raise RuntimeError("Cannot run a missing or closed attempt")
            return True

    def complete(self, reply):
        with self.transaction() as db:
            changed = db.execute(
                "UPDATE host_attempts SET reply = ?, pending_effect = NULL "
                "WHERE id = ? AND reply IS NULL", (canonical(reply), reply["request_id"]),
            )
            if changed.rowcount != 1:
                raise RuntimeError("Cannot complete a missing or closed attempt")

    def historical_route(self, request_id, face, alias):
        if not isinstance(request_id, str):
            return None
        with self.transaction() as db:
            saved = db.execute("SELECT route FROM host_attempts WHERE id = ?", (request_id,)).fetchone()
        row = parse(saved[0]) if saved else None
        if row and face == "mcp" and row["mcp"]["name"] == alias:
            return row["id"]
        if row and face == "cli" and tuple(row["cli"]["argv"]) == alias:
            return row["id"]
        return None


def relative_parts(value):
    parts = value.split("/")
    if not all(part and part not in {".", ".."} and "\x00" not in part for part in parts):
        raise Refusal("EXCHANGE_PATH_OUTSIDE_ROOT")
    return parts


def open_directories(root, parts, stack):
    fd = root
    for part in parts:
        try:
            fd = os.open(part, DIRECTORY, dir_fd=fd)
        except OSError as exc:
            raise Refusal("EXCHANGE_PATH_OUTSIDE_ROOT") from exc
        stack.callback(os.close, fd)
    return fd


def read_file(root, relative, limit):
    parts = relative_parts(relative)
    with ExitStack() as stack:
        parent = open_directories(root, parts[:-1], stack)
        try:
            fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        except FileNotFoundError as exc:
            raise Refusal("ABSENT") from exc
        except OSError as exc:
            raise Refusal("EXCHANGE_PATH_OUTSIDE_ROOT") from exc
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise Refusal("EXCHANGE_PATH_OUTSIDE_ROOT")
            data = stream.read(limit + 1)
    if len(data) > limit:
        raise Refusal("EXPECTATION_FAILED")
    return data


def write_new(root, name, data):
    fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=root)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    os.fsync(root)


def retain(root, payload):
    data = canonical(payload)
    checksum = sha256(data).hexdigest()
    name = f"{checksum}.json"
    if (root / name).exists():
        if (root / name).read_bytes() != data:
            raise RuntimeError("Retained evidence digest mismatch")
    else:
        # Each publisher has a different temporary path. Hard link publishes only full bytes.
        with tempfile.NamedTemporaryFile(dir=root) as temporary:
            temporary.write(data)
            temporary.flush()
            os.fsync(temporary.fileno())
            try:
                os.link(temporary.name, root / name)
            except FileExistsError:
                if (root / name).read_bytes() != data:
                    raise RuntimeError("Retained evidence digest mismatch")
        fd = os.open(root, DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    return {"path": f"evidence/{name}", "sha256": checksum}


def external_alias(face, alias):
    if face == "cli":
        _, _, slot, operation = alias
    else:
        if not alias.startswith("rea_ext_"):
            return None
        encoded_slot, separator, operation = alias.removeprefix("rea_ext_").partition("__")
        if not separator:
            return None
        slot = encoded_slot.replace("_", "-")
    if re.fullmatch(SLOT_ID, slot) and re.fullmatch(OPERATION_ID, operation):
        return f"ext.{slot}.{operation}"
    return None


class Host:
    """One handler for every face. The service supplies its own authenticated context."""

    def __init__(self, config):
        self.config = config
        self.bindings = config["bindings"]
        self.context = config["context"]
        if any(not isinstance(self.context.get(key), str) or not self.context[key]
               for key in ("capsule_id", "incarnation")):
            raise ValueError("Host context must bind the capsule and its incarnation")
        self.state = Path(config["state"]).resolve(strict=True)
        if not self.state.is_dir() or stat.S_IMODE(self.state.stat().st_mode) & 0o077:
            raise ValueError("Host state must be a private directory")
        self.evidence = self.state / "evidence"
        self.evidence.mkdir(mode=0o700, exist_ok=True)
        if self.evidence.is_symlink():
            raise ValueError("Host evidence cannot be a symlink")
        attempt_db = self.state / "attempts.sqlite"
        if attempt_db.is_symlink():
            raise ValueError("Host attempts database cannot be a symlink")
        self.attempts = Attempts(attempt_db)
        schema = config["pins"]
        documents = [parse(pinned(pin)) for pin in config["manifests"]]
        self.catalog = Catalog(documents, parse(pinned(schema["base"])),
                               parse(pinned(schema["domain"])), self.bindings)
        self.request_schema, self.reply_schema = control_overlay(
            parse(pinned(schema["request"])), parse(pinned(schema["reply"])),
        )
        self.policy_digest = digest({"context": self.context, "pins": schema,
                                     "granted_root": config["granted_root"],
                                     "bindings": self.bindings, "profile": "offline_fixture"})

    def reply(self, request_id, code, effective_sha=None, result=None, evidence=()):
        reply = {"schema": REPLY, "op": SELECTOR, "request_id": request_id, "ok": code == "OK",
                 "code": code, "result": {} if result is None else result,
                 "effective_request_sha256": effective_sha or digest({"request_id": request_id}),
                 "evidence": list(evidence), "not_verified": ["live_capsule", "shared_evidence_binding"]}
        validate(self.reply_schema, reply)
        return reply

    def invalid(self, request_id=None):
        usable = isinstance(request_id, str) and re.fullmatch(REQUEST_ID, request_id)
        return self.reply(request_id if usable else "invalid-request", "SCHEMA_INVALID")

    def face(self, face, selector, wrapper):
        if not isinstance(wrapper, dict):
            return self.invalid()
        request_id = wrapper.get("request_id")
        if face == "router":
            route_id = selector
        else:
            aliases = self.catalog.cli if face == "cli" else self.catalog.mcp
            route_id = (aliases.get(selector)
                        or self.attempts.historical_route(request_id, face, selector)
                        or external_alias(face, selector))
        if route_id is None:
            if not isinstance(request_id, str) or not re.fullmatch(REQUEST_ID, request_id):
                return self.invalid(request_id)
            return self.reply(request_id, "UNKNOWN_TOOL_ID")
        return self.handle({"schema": REQUEST, "op": SELECTOR, "request_id": request_id,
                            "context": self.context, "dry_run": wrapper.get("dry_run", False),
                            "args": {"route_id": route_id, "arguments": {
                                name: value for name, value in wrapper.items()
                                if name not in {"request_id", "dry_run"}
                            }}})

    def handle(self, request):
        request_id = request.get("request_id") if isinstance(request, dict) else None
        if not isinstance(request_id, str) or not re.fullmatch(REQUEST_ID, request_id):
            return self.invalid(request_id)
        try:
            if request.get("op") != SELECTOR or request.get("context") != self.context:
                raise Refusal("SCHEMA_INVALID")
            request = {"dry_run": False} | request
            validate(self.request_schema, request)
        except Refusal:
            return self.invalid(request_id)

        route_id = request["args"]["route_id"]
        route = self.catalog.routes.get(route_id)

        def normalize(snapshot, policy=None):
            if snapshot is not None and snapshot["id"] != route_id:
                raise Refusal("REQUEST_ID_CONFLICT")
            schema = snapshot["json"]["argument_schema"] if snapshot else closed(ARGUMENTS)
            arguments = request["args"]["arguments"]
            validate(schema, arguments)
            return {"request_id": request_id, "route_id": route_id, "context": request["context"],
                    "arguments": defaults(schema) | arguments, "dry_run": request["dry_run"],
                    "manifest_sha256": snapshot["manifest_sha256"] if snapshot else None,
                    "policy_sha256": policy or self.policy_digest}

        try:
            effective, saved = self.attempts.claim(request_id, route, normalize)
        except Refusal as exc:
            # Preserve the effective hash on valid competing or still-pending requests.
            try:
                fingerprint = digest(normalize(route))
            except Refusal:
                fingerprint = digest(request)
            return self.reply(request_id, exc.code, exc.fingerprint or fingerprint)
        if saved is not None:
            return saved

        fingerprint = digest(effective)
        try:
            if route is None:
                raise Refusal("UNKNOWN_TOOL_ID")
            if not route["callable"]:
                code = ("AVAILABILITY_NOT_INSTALLED" if route["availability"] == "not_installed"
                        else "PROFILE_NOT_ADMITTED")
                raise Refusal(code)
            reply = self.invoke(route, effective, fingerprint)
        except Refusal as exc:
            reply = self.reply(request_id, exc.code, fingerprint)
        if reply["code"] != "UNCERTAIN_OUTCOME" or reply["evidence"]:
            self.attempts.complete(reply)
        return reply

    def invoke(self, route, effective, fingerprint):
        adapter = self.bindings[route["id"]]
        root = Path(self.config["granted_root"])
        request_id = effective["request_id"]
        with ExitStack() as stack:
            try:
                if not root.is_absolute() or root.resolve(strict=True) != root:
                    raise Refusal("EXCHANGE_PATH_OUTSIDE_ROOT")
                root_fd = os.open(root, DIRECTORY)
            except OSError as exc:
                raise Refusal("EXCHANGE_PATH_OUTSIDE_ROOT") from exc
            stack.callback(os.close, root_fd)
            exchange = open_directories(root_fd, relative_parts(adapter["exchange"]), stack)
            arguments = effective["arguments"]
            if arguments["perception_level"] != "none":
                raise Refusal("PERCEPTION_DENIED")
            materialized = []
            for item in arguments["inputs"]:
                data = read_file(root_fd, item["ref"], INPUT_LIMIT)
                if sha256(data).hexdigest() != item["sha256"]:
                    raise Refusal("INPUT_PIN_MISMATCH")
                materialized.append((item, data))
            if effective["dry_run"]:
                return self.reply(request_id, "OK", fingerprint, {"preview": effective})
            # The same unresolved edit is held across new IDs for this capsule and route.
            effect = digest({
                "capsule_id": self.context["capsule_id"],
                "incarnation": self.context["incarnation"],
                "route_id": route["id"],
                "base_revision_sha256": arguments["params"].get("base_revision_sha256"),
            })
            if not self.attempts.start_effect(request_id, effect):
                reply = self.reply(request_id, "UNCERTAIN_OUTCOME", fingerprint)
                self.attempts.complete(reply)
                return reply

            try:
                os.mkdir(fingerprint, mode=0o700, dir_fd=exchange)
                attempt = open_directories(exchange, [fingerprint], stack)
                inputs = []
                for index, (item, data) in enumerate(materialized):
                    filename = f"input-{index}"
                    write_new(attempt, filename, data)
                    inputs.append({"role": item["role"], "file": filename, "sha256": item["sha256"]})
                send = {"schema": EXCHANGE_REQUEST, "request_sha256": fingerprint,
                        "operation": route["operation"], "inputs": inputs, "params": arguments["params"]}
                write_new(attempt, "request.json", canonical(send))
                argv = [*adapter["command"], route["operation"]]
                child = subprocess.run(argv, cwd=f"/proc/self/fd/{attempt}", pass_fds=(attempt,),
                               env=CHILD_ENV, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, check=False,
                               timeout=min(adapter["timeout_s"], route["limits"]["timeout_s"]))
                limit = min(adapter["max_output_bytes"], route["limits"]["max_output_bytes"])
                capsule_reply = parse(read_file(attempt, "reply.json", limit))
                self.validate_exchange(capsule_reply, fingerprint, adapter["result"])
                if child.returncode and capsule_reply["code"] == "OK":
                    raise ValueError("Successful reply from a failed adapter")
                result_ref = retain(self.evidence, capsule_reply)
                receipt = {"schema": ATTEMPT_RECEIPT, "mode": adapter["mode"], "request_id": request_id,
                           "request_sha256": fingerprint, "route_id": route["id"],
                           "manifest_sha256": route["manifest_sha256"], "policy_sha256": self.policy_digest,
                           "exchange_request_sha256": digest(send), "result_ref": result_ref,
                           "code": capsule_reply["code"], "live_use_permitted": False}
                if child.returncode:
                    receipt["adapter_rc"] = child.returncode
                receipt_ref = retain(self.evidence, receipt)
                result = ({"output": capsule_reply["result"], "receipt": receipt_ref}
                          if capsule_reply["code"] == "OK" else None)
                return self.reply(request_id, capsule_reply["code"], fingerprint, result,
                                  (receipt_ref, result_ref))
            except (OSError, ValueError, subprocess.SubprocessError, Refusal, RuntimeError):
                # Once reserved for dispatch, an absent or invalid reply cannot prove no effect.
                return self.reply(request_id, "UNCERTAIN_OUTCOME", fingerprint)

    @staticmethod
    def validate_exchange(reply, fingerprint, result_schema):
        keys = {"schema", "request_sha256", "code", "result"}
        if (not isinstance(reply, dict) or reply.keys() != keys
                or reply["schema"] != EXCHANGE_REPLY or reply["request_sha256"] != fingerprint
                or not isinstance(reply["code"], str)
                or reply["code"] not in {"OK", "EXPECTATION_FAILED", "ABSENT"}):
            raise ValueError("Unbound or invalid capsule reply")
        if reply["code"] == "OK":
            validate(result_schema, reply["result"])
        elif reply["result"] != {}:
            raise ValueError("A capsule refusal must have an empty result object")
