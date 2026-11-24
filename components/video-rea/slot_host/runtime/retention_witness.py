# SPDX-License-Identifier: AGPL-3.0-only
"""Bridge authenticated Tools witness calls to the host evidence source."""

import fcntl
import json
import os
import re
from functools import cache
from hashlib import sha256
from pathlib import Path
import socket
import stat
import struct
from tempfile import NamedTemporaryFile
from types import SimpleNamespace

from . import evidence_projection as projection

_MAX_EVIDENCE_BYTES = 1 << 20
_DIGEST = re.compile(r"[0-9a-f]{64}")


def _document(raw):
    value = json.loads(raw)
    if type(value) is not dict or raw != projection.record_bytes(value):
        raise ValueError("offline evidence requires canonical record bytes")
    return value


def _binding(kind, actor, admission, value):
    if actor.principal != admission.get("executor_id") or not actor.tenant:
        raise ValueError("witness actor does not match admission")
    if kind == "claim":
        if (admission["operation"] == "compare" or type(value) is not dict
                or set(value) != {"attempt_id", "request_sha256", "start_sha256"}
                or any(value[key] != admission[key] for key in ("attempt_id", "request_sha256"))):
            raise ValueError("invalid one-shot claim witness binding")
        digest = value["start_sha256"]
    elif kind in ("start", "finish"):
        if kind == "finish":
            if type(value) is not tuple or len(value) != 2 or any(type(raw) is not bytes for raw in value):
                raise ValueError("finish requires the original terminal and artifact octets")
            raw, artifact = value
        else:
            raw = value
        if type(raw) is not bytes:
            raise ValueError("witness requires canonical record octets")
        record = _document(raw)
        if record["attempt_id"] != admission["attempt_id"]:
            raise ValueError("witness record belongs to another attempt")
        if kind == "start":
            request = record.get("external_process", record)
            if (request.get("request_id"), request.get("request_sha256"), record.get("executor_id")) != (
                admission["request_id"], admission["request_sha256"], actor.principal,
            ):
                raise ValueError("start witness does not bind the admitted request and executor")
        if kind == "finish" and (
            len(artifact), sha256(artifact).hexdigest(),
        ) != (record["artifact"]["bytes"], record["artifact"]["sha256"]):
            raise ValueError("finish witness does not bind these artifact bytes")
        digest = sha256(raw).hexdigest()
    else:
        raise ValueError("unknown offline witness event")
    if type(digest) is not str or not _DIGEST.fullmatch(digest):
        raise ValueError("invalid record digest")
    return {
        "kind": kind, "tenant": actor.tenant, "principal": actor.principal,
        "admission_sha256": admission["admission_sha256"], "record_sha256": digest,
    }


def _historical_binding(kind, actor, admission, record):
    if record["attempt_id"] != admission["attempt_id"]:
        raise ValueError("historical record belongs to another attempt")
    raw = projection.record_bytes(record)
    if kind == "claim":
        value = {key: admission[key] for key in ("attempt_id", "request_sha256")}
        value["start_sha256"] = sha256(raw).hexdigest()
        return _binding(kind, actor, admission, value)
    if kind == "start":
        return _binding(kind, actor, admission, raw)
    if kind != "finish" or actor.principal != admission["executor_id"] or not actor.tenant:
        raise ValueError("historical witness actor or phase differs")
    return {
        "kind": kind, "tenant": actor.tenant, "principal": actor.principal,
        "admission_sha256": admission["admission_sha256"], "record_sha256": sha256(raw).hexdigest(),
    }


class OfflineWitness:
    """Resolve a pre-existing proof from the offline evidence source."""

    def __init__(self, directory, profiles):
        self.directory = Path(directory)
        if not self.directory.is_absolute():
            raise ValueError("offline witness directory must be explicit and absolute")
        self.profiles = profiles

    def _proof(self, binding):
        filename = projection.record_digest(binding) + ".json"
        with (self.directory / filename).open("rb") as source:
            raw = source.read(_MAX_EVIDENCE_BYTES + 1)
        if len(raw) > _MAX_EVIDENCE_BYTES:
            raise ValueError("offline proof exceeds the evidence bound")
        proof = _document(raw)
        if set(proof) != {"binding", "evidence"} or proof["binding"] != binding or not proof["evidence"]:
            raise ValueError("offline proof does not match the authenticated record")
        if binding["kind"] == "start" and len(projection.record_bytes({
            "start_sha256": binding["record_sha256"], "evidence": proof,
        })) > _MAX_EVIDENCE_BYTES:
            raise ValueError("offline start proof exceeds the wrapped evidence bound")
        return proof

    def witness(self, kind, actor, admission, value):
        try:
            binding = _binding(kind, actor, admission, value)
        except (AttributeError, KeyError, TypeError) as exc:
            raise ValueError("invalid offline witness inputs") from exc
        return self._proof(binding)

    def verify_witness(self, kind, actor, admission, record, evidence):
        try:
            binding = _historical_binding(kind, actor, admission, record)
            return projection.record_bytes(self._proof(binding)) == projection.record_bytes(evidence)
        except (KeyError, OSError, TypeError, ValueError):
            return False

    def validate_profile(self, admission, profile, terminal):
        return self.profiles(admission, profile, terminal) is True


def verify_profile_binding(admission, profile, terminal):
    """Verify the REA profile fields that bind the frozen terminal."""
    if admission["profile_schema"] != projection.PROFILE_SCHEMA_V1:
        return False
    attempt = profile["attempt"]
    if any(attempt[key] != admission[key] for key in ("request_id", "attempt_id", "operation")):
        return False
    if terminal["attempt_id"] != attempt["attempt_id"]:
        return False
    if profile["shared_receipt_adapter"] != {
        "profile": projection.PROFILE_SCHEMA_V1, "binding_state": "bound",
        "shared_schema_id": projection.V03_ID, "shared_schema_sha256": projection.V03_SHA,
        "live_use_permitted": False,
    }:
        return False
    if profile["episode"].get("episode_id") != terminal.get("episode_id"):
        return False
    if profile["episode"].get("parent_episode_id") != terminal.get("parent_episode_id"):
        return False
    if attempt["operation"] == "compare":
        if terminal.get("operation") != "compare" or projection.project_verdict(profile) != terminal["verdict"]:
            return False
    elif "operation" in terminal or any(profile["slot"].get(key) != admission[admitted] for key, admitted in (
        ("tool_id", "slot_id"), ("provider_id", "provider_id"), ("provider_revision", "provider_revision"),
        ("manifest_sha256", "manifest_sha256"), ("implementation_sha256", "implementation_sha256"),
        ("source_url", "source_url"),
    )):
        return False
    if terminal["state"] == "aborted_before_spawn":
        return (attempt.get("control_outcome") == terminal["abort_reason"]
                and profile["observation"]["producer"]["process_bound"] is False
                and "slot_result" not in attempt)
    process = terminal.get("execution", {}).get("process", {})
    if projection.project_observation(
        profile, pid=process.get("pid"), starttime_ticks=process.get("starttime_ticks"),
    ) != terminal["observation"]:
        return False
    if profile.get("media", {}) != terminal.get("media", {}):
        return False
    profile_timeline, terminal_timeline = profile.get("timeline", {}), terminal.get("timeline", {})
    return profile_timeline.keys() == terminal_timeline.keys() and all(
        {key: item for key, item in profile_timeline[side].items() if key != "events"} ==
        {key: item for key, item in terminal_timeline[side].items() if key not in ("events", "events_object_ref")}
        and ("events" not in terminal_timeline[side]
             or profile_timeline[side]["events"] == terminal_timeline[side]["events"])
        for side in profile_timeline
    )


def pinned_profiles(schemas):
    """Construct the approved validator from the explicitly supplied Tools export."""
    from .retention_deployment import import_tools

    module = import_tools("retention_service.store")
    return module.PinnedProfiles(schemas, verify_profile_binding)


@cache
def _offline():
    directory = os.environ.get("REA_RETENTION_OFFLINE_WITNESS_DIR")
    schema = os.environ.get("REA_RETENTION_OFFLINE_PROFILE_SCHEMA")
    pin = os.environ.get("REA_RETENTION_OFFLINE_PROFILE_SHA256")
    if not directory or not schema or not pin:
        raise OSError("owner-pending: T03 (offline witness inputs are absent)")
    with Path(schema).open("rb") as source:
        raw = source.read(_MAX_EVIDENCE_BYTES + 1)
    if len(raw) > _MAX_EVIDENCE_BYTES or sha256(raw).hexdigest() != pin:
        raise ValueError("offline profile schema does not match its configured pin")
    return OfflineWitness(directory, pinned_profiles({(projection.PROFILE_SCHEMA_V1, pin): raw}))


class CredentialCapture:
    """Attribute each stdout fragment to the supervised child using kernel credentials."""

    _credentials = struct.Struct("3i")

    def __init__(self):
        self.reader, self.writer = socket.socketpair()
        try:
            self.reader.setsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED, 1)
        except OSError:
            self.close()
            raise
        self.pid = None
        self.digest = sha256()
        self.complete = False

    def fileno(self):
        return self.reader.fileno()

    def read_chunk(self, count):
        raw, ancillary, flags, _ = self.reader.recvmsg(count, socket.CMSG_SPACE(self._credentials.size))
        if raw:
            expected = self._credentials.pack(self.pid, os.geteuid(), os.getegid())
            if flags & socket.MSG_CTRUNC or ancillary != [(socket.SOL_SOCKET, socket.SCM_CREDENTIALS, expected)]:
                raise OSError("stdout credentials differ from the supervised child")
            self.digest.update(raw)
        else:
            self.complete = True
        return raw

    def close(self):
        self.reader.close()
        self.writer.close()


class LocalWitness:
    """Persist host observations beside the private Tools deployment for historical checks."""

    def __init__(self, directory, *, create=False):
        self.directory = Path(directory)
        if create:
            self.directory.mkdir(mode=0o700, exist_ok=True)
        try:
            info = self.directory.lstat()
        except FileNotFoundError as exc:
            raise OSError("local-open live witness source is absent from this deployment") from exc
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise OSError("local-open witness directory must be owner-only")

    @classmethod
    def current(cls, *, create=False):
        configured = os.environ.get("XDG_STATE_HOME")
        base = Path(configured) if configured else Path.home() / ".local" / "state"
        state = base / "viewpoint-retention"
        try:
            info = state.lstat()
        except FileNotFoundError as exc:
            raise OSError("local-open live witness source is absent from this deployment") from exc
        if (not state.is_absolute() or not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o700):
            raise OSError("local-open live witness source is absent from an owner-only deployment")
        return cls(state / "rea-witness", create=create)

    def _path(self, admission, kind):
        identity = admission["admission_sha256"]
        if not _DIGEST.fullmatch(identity) or kind not in ("start", "claim", "process", "capture", "finish"):
            raise ValueError("invalid local-open admission digest or witness phase")
        return self.directory / f"{identity}.{kind}.json"

    def _load(self, admission, kind):
        fd = os.open(self._path(admission, kind), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                    or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > _MAX_EVIDENCE_BYTES):
                raise OSError("invalid owner-only local-open witness file")
            raw = stream.read(_MAX_EVIDENCE_BYTES + 1)
            if len(raw) > _MAX_EVIDENCE_BYTES:
                raise ValueError("local-open witness exceeds the byte limit")
            return _document(raw)

    def _save(self, admission, kind, data):
        raw = projection.record_bytes(data)
        if len(raw) > _MAX_EVIDENCE_BYTES:
            raise ValueError("local-open witness exceeds the byte limit")
        target = self._path(admission, kind)
        with NamedTemporaryFile(dir=self.directory, prefix=".witness-") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
            try:
                os.link(stream.name, target)
            except FileExistsError:
                if projection.record_bytes(self._load(admission, kind)) != raw:
                    raise ValueError("local-open witness differs from the original observation") from None
            fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)

    @staticmethod
    def _actor(attempt):
        return SimpleNamespace(tenant=attempt.tenant, principal=attempt.admission["executor_id"])

    def _record(self, attempt, kind, value, facts):
        binding = _binding(kind, self._actor(attempt), attempt.admission, value)
        proof = {"binding": binding, "evidence": facts}
        if kind == "start" and len(projection.record_bytes({
            "start_sha256": binding["record_sha256"], "evidence": proof,
        })) > _MAX_EVIDENCE_BYTES:
            raise ValueError("wrapped start witness exceeds the byte limit")
        self._save(attempt.admission, kind, proof)

    def stage(self, attempt, started, fd, argv, cwd, env, stage):
        from .executable_custody import _open_source, _sha256_fd, executable_identity

        command = started["command"]
        executable = _sha256_fd(fd)
        identity = executable_identity(fd)
        path = Path(argv[0])
        staged_fd, _ = _open_source(path)
        try:
            staged_identity = executable_identity(staged_fd)
            staged_sha = _sha256_fd(staged_fd)
        finally:
            os.close(staged_fd)
        seals = fcntl.fcntl(fd, fcntl.F_GET_SEALS)
        required = fcntl.F_SEAL_WRITE | fcntl.F_SEAL_GROW | fcntl.F_SEAL_SHRINK | fcntl.F_SEAL_SEAL
        if (path.parent != stage or seals & required != required
                or executable != staged_sha or executable != attempt.admission["implementation_sha256"]
                or (command["arguments"], command["cwd"], command["env_sha256"])
                != (argv, str(cwd), projection.env_sha256(env)) or not cwd.is_dir()):
            raise ValueError("staged fd, argv, cwd or environment differs from the admitted start")
        self._record(attempt, "start", projection.record_bytes(started), {
            "executable_sha256": executable, "executable_identity": list(identity),
            "staged_identity": list(staged_identity), "fd_seals": seals,
            "command": command, "stage": str(stage),
        })

    def compare_start(self, attempt, started):
        self._record(attempt, "start", projection.record_bytes(started), {
            key: started[key] for key in ("operation", "reference_attempt_id", "candidate_attempt_id")
        })

    def claim(self, attempt, ack):
        start = self._load(attempt.admission, "start")
        if ack["start_sha256"] != start["binding"]["record_sha256"]:
            raise ValueError("claim does not bind the witnessed start")
        self._record(attempt, "claim", ack, {"start_ack": ack})

    def launched(self, attempt, pid, starttime, capture):
        if type(starttime) is not int:
            raise OSError("the launched process has no observed starttime")
        capture.pid = pid
        self._save(attempt.admission, "process", {"pid": pid, "starttime_ticks": starttime})

    def retired(self, attempt, proc, capture):
        from .process_lifecycle import proc_starttime

        start = self._load(attempt.admission, "start")["evidence"]
        process = self._load(attempt.admission, "process")
        scratch, stage = start["command"]["cwd"], start["stage"]
        if (not capture.complete or proc.pid != process["pid"] or proc.returncode is None
                or proc_starttime(proc.pid) == process["starttime_ticks"]
                or os.path.lexists(scratch) or os.path.lexists(stage)):
            raise OSError("stdout custody or scratch and stage retirement is unresolved")
        self._save(attempt.admission, "capture", {
            "process": process, "stdout_sha256": capture.digest.hexdigest(),
            "scratch_retired": scratch, "stage_retired": stage, "returncode": proc.returncode,
        })

    def report(self, attempt, raw):
        self._save(attempt.admission, "capture", {"report_sha256": sha256(raw).hexdigest()})

    def observe_terminal(self, admission, started, facts):
        from .retention import TerminalEvidence

        start = self._load(admission, "start")
        start_sha = projection.record_digest(started)
        if start["binding"]["record_sha256"] != start_sha:
            raise ValueError("terminal observation differs from the witnessed start")
        capture = self._load(admission, "capture")
        attempt = admission["attempt_id"]
        common = {
            "start_sha256": start_sha,
            "artifact_observer_id": "rea/local-open/capture", "artifact_observation_id": f"capture/{attempt}",
            "cleanup_observer_id": "rea/local-open/retirement", "cleanup_observation_id": f"retirement/{attempt}",
            "cleanup_retirement_sha256": projection.record_digest(capture),
        }
        if admission["operation"] == "compare":
            if facts.get("comparison_sha256") != capture["report_sha256"]:
                raise ValueError("host report differs from its independent capture")
            return TerminalEvidence(**common, captured_sha256=capture["report_sha256"])
        process = capture["process"]
        pid, ticks = process["pid"], process["starttime_ticks"]
        if (facts.get("process_identity") != (pid, ticks) or facts.get("cleanup_observed") is not True
                or facts.get("captured_sha256") != capture["stdout_sha256"]):
            raise ValueError("process, stdout or retirement differs from the independent capture")
        return TerminalEvidence(
            **common, captured_sha256=capture["stdout_sha256"],
            process={**process, "start_id": f"rea/start/{ticks}", "job_id": f"rea/job/{pid}"},
            launch_observation={
                "observer_id": "rea/local-open/launch", "observation_id": f"launch/{attempt}",
                "receipt_sha256": projection.record_digest({"start": start, "process": process}),
                "executable_sha256": start["evidence"]["executable_sha256"],
                "executed_path": start["evidence"]["command"]["arguments"][0],
            },
            attribution_receipt_sha256=projection.record_digest(capture),
        )

    def finish(self, attempt, terminal, artifact, index):
        capture = self._load(attempt.admission, "capture")
        comparison = attempt.admission["operation"] == "compare"
        role, key = ("comparison", "report_sha256") if comparison else ("observation", "stdout_sha256")
        capture_sha = projection.record_digest(capture)
        if (not any(entry["role"] == role and entry["sha256"] == capture[key] for entry in index["objects"])
                or terminal["cleanup"].get("retirement_receipt_sha256") != capture_sha):
            raise ValueError("terminal index or cleanup differs from independent capture")
        if comparison:
            if terminal.get("operation") != "compare" or "execution" in terminal:
                raise ValueError("report capture belongs to an in-host comparison")
        else:
            process = terminal["execution"]["process"]
            if (any(process.get(name) != value for name, value in capture["process"].items())
                    or terminal["artifact"].get("attribution_receipt_sha256") != capture_sha):
                raise ValueError("terminal process or artifact differs from independent capture")
        self._record(attempt, "finish", (projection.record_bytes(terminal), artifact),
                     {"capture": capture, "artifact_sha256": sha256(artifact).hexdigest()})

    def witness(self, kind, actor, admission, value):
        binding = _binding(kind, actor, admission, value)
        try:
            proof = self._load(admission, kind)
        except FileNotFoundError as exc:
            raise OSError("local-open live witness source is absent for this attempt") from exc
        self._check_binding(kind, binding, proof)
        return proof

    @staticmethod
    def _check_binding(kind, binding, proof):
        original = proof["binding"]
        # The store compares conflicting finish bytes with its durable original before
        # validating evidence. A different digest cannot pass historical verification.
        if kind == "finish":
            binding = dict(binding, record_sha256=original["record_sha256"])
        if original != binding:
            raise ValueError("local-open witness does not bind this request")

    def verify_witness(self, kind, actor, admission, record, evidence):
        try:
            binding = _historical_binding(kind, actor, admission, record)
            proof = self._load(admission, kind)
            return (proof["binding"] == binding
                    and projection.record_bytes(proof) == projection.record_bytes(evidence))
        except (KeyError, OSError, TypeError, ValueError):
            return False


def _source(principal):
    return LocalWitness.current() if principal.startswith("local/uid/") else _offline()


def witness(kind, actor, admission, value):
    if actor.principal.startswith("local/uid/"):
        _binding(kind, actor, admission, value)
    return _source(actor.principal).witness(kind, actor, admission, value)


def verify_witness(kind, actor, admission, record, evidence):
    try:
        return _source(admission["executor_id"]).verify_witness(kind, actor, admission, record, evidence) is True
    except (KeyError, OSError, TypeError, ValueError):
        return False


@cache
def _local_profiles():
    from .retention_deployment import local_open_profile

    pins, raw = local_open_profile()
    profile = pins["profile"]
    return pinned_profiles({(profile["id"], profile["sha256"]): raw})


def validate_profile(admission, profile, terminal):
    try:
        if admission["executor_id"].startswith("local/uid/"):
            return _local_profiles()(admission, profile, terminal) is True
        return _offline().validate_profile(admission, profile, terminal) is True
    except (KeyError, OSError, TypeError, ValueError):
        return False
