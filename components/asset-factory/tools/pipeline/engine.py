"""Linear, immutable stage cache. A corrupt committed entry never recomputes.

The marker is the commit point; each cache read authenticates both the receipt
and every declared artifact/evidence file. Failed attempts live outside cache.
The run lock protects the complete output tree. Recovery is manual after crash.
"""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import time

from asset_factory import core
from tools.blender.bake_support import read_bytes
from .contracts import RUN, validate_stage

STAGES = ("input", "provider", "mesh", "topology", "uv_bake_orm", "export", "appearance", "vqa3d")
CHARACTER_PLAN = (*STAGES[:6], "face", "skin", "act", "voice", "phys", *STAGES[6:])
STAGE_PLANS = {"prop": STAGES, "character": CHARACTER_PLAN}
CHARACTER_SCOPE = "character-stage-fixture-replay"
CHARACTER_CALIBRATION = "TARGET_UNCALIBRATED"
PRODUCTION_RC = {"PASS": 0, "CANNOT_SEE": 3, "FAIL": 5, "ERROR": 2}
MAX_JSON = 16 * 2**20  # [TARGET] ingestion budget, not a quality threshold.


class Refusal(Exception):
    def __init__(self, rc, reason):
        super().__init__(reason)
        self.rc, self.reason = (rc, reason) if type(rc) is int and rc in core.RC and rc != 0 else (2, "invalid_refusal_rc")


def require(condition, rc, reason):
    if not condition:
        raise Refusal(rc, reason)


def load(path, *, expected_sha256=None):
    data = read_bytes(path)
    require(len(data) <= MAX_JSON, 2, "json_byte_budget")
    if expected_sha256 is not None:
        require(hashlib.sha256(data).hexdigest() == expected_sha256, 4, "bytes_stale")
    value = json.loads(data.decode("utf-8"), object_pairs_hook=core._unique_object,
                       parse_constant=core._reject_number, parse_float=core._reject_number)
    core._check_json(value)
    return value


def file_ref(path):
    path = Path(path).absolute()
    require(path.resolve() == path, 2, "noncanonical_path")
    require(not (path.name in ("auth.json", "operator.token", "launch.token")
                 or path.name.startswith(".env") or path.suffix == ".key"
                 or ".gemini" in path.parts), 2, "sensitive_path_refused")
    data = read_bytes(path)
    return {"path": str(path), "sha256": hashlib.sha256(data).hexdigest()}


def authenticate(ref, *, inside=None):
    path = Path(ref["path"])
    require(path.is_absolute(), 2, "relative_reference")
    require(path.resolve() == path, 2, "noncanonical_reference")
    if inside is not None:
        require(path.is_relative_to(inside), 2, "cache_path_escape")
    require(file_ref(path) == ref, 4, "bytes_stale")


def owned_directory(path, root):
    """Reject every symlink before creating anything below a caller directory."""
    path = Path(path)
    require(path.is_relative_to(root), 2, "directory_escape")
    current = root
    for part in path.relative_to(root).parts:
        current = current / part
        require(not current.is_symlink(), 2, "directory_symlink")
        if not current.exists():
            current.mkdir()
        require(current.is_dir() and current.resolve() == current, 2, "directory_invalid")


def validate_previous(ref, root):
    authenticate(ref, inside=root)
    record = load(ref["path"], expected_sha256=ref["sha256"])
    validate_stage(record)
    require(record["rc"] == 0 and record["verdict"] == "PASS", record["rc"] or 2, "upstream_not_pass")
    for item in record["inputs"]:
        authenticate(item)
    for item in record["outputs"] + record["evidence"]:
        authenticate(item, inside=root)
    return record


def fault(exc):
    if isinstance(exc, Refusal):
        return exc.rc, exc.reason
    if isinstance(exc, FileNotFoundError):
        return 3, "evidence_missing"
    if isinstance(exc, TimeoutError):
        return 6, "deadline_exceeded"
    if isinstance(exc, core.ContractError):
        return 2, exc.code
    value = getattr(exc, "rc", None)
    if type(value) is int and value in core.RC and value != 0:
        return value, getattr(exc, "reason", getattr(exc, "code", "stage_refused"))
    return 2, "stage_contract_or_io"


class Pipeline:
    def __init__(self, out, backend, *, plan="prop"):
        require(type(plan) is str and plan in STAGE_PLANS, 2, "stage_plan_unknown")
        self.stages = STAGE_PLANS[plan]
        # The original preimages encode the default plan by convention.
        # Preserve their bytes for both implicit and explicit prop callers.
        self._plan = {"name": plan, "stages": list(self.stages)} if plan == "character" else None
        self.root = Path(out).absolute()
        self.backend = backend
        if hasattr(backend, "bind_output"):
            backend.bind_output(self.root)

    def run(self):
        require(not self.root.is_symlink() and self.root.resolve() == self.root, 2, "output_path_invalid")
        self.root.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.root / ".pipeline.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise Refusal(5, "pipeline_locked")
            return self._run()
        finally:
            os.close(fd)

    def _run(self):
        started = time.monotonic_ns()
        rows, receipts, counts, previous, subject, blocked = [], [], [], None, None, None
        for name in self.stages:
            begin = time.monotonic_ns()
            inputs, params, pin, key = [], {}, "0" * 64, None
            record, ref, cache_hit, scratch = None, None, False, None
            try:
                require(blocked is None, blocked or 3, "upstream_blocked")
                # Revalidate the complete prefix before every invocation, including
                # cache hits; callbacks may have changed any earlier receipt.
                for prior in receipts:
                    validate_previous(prior, self.root)
                if receipts:
                    self.backend.verify_sources()
                if self._plan and hasattr(self.backend, "bind_prefix"):
                    self.backend.bind_prefix(name, tuple(dict(ref) for ref in receipts))

                def stage_identity():
                    refs, parameters, toolchain = self.backend.identity(name, subject)
                    if self._plan:
                        parameters = {**parameters, "stage_plan": self._plan}
                    return refs, parameters, toolchain

                inputs, params, pin = stage_identity()
                for item in inputs:
                    authenticate(item)
                identity = {"stage": name, "inputs": inputs, "parameters": params,
                            "previous": previous, "toolchain_sha256": pin}
                key = core.sha256_json(identity)
                parent = self.root / "cache" / name
                owned_directory(parent, self.root)
                require(parent.resolve() == parent, 2, "cache_path_escape")
                dest = parent / key
                if dest.exists() or dest.is_symlink():
                    require(not dest.is_symlink(), 2, "cache_path_escape")
                    marker = load(dest / "entry.json")
                    require(type(marker) is dict and set(marker) == {"schema", "key", "receipt"}
                            and marker["schema"] == "af.pipeline-cache/v1" and marker["key"] == key,
                            4, "cache_identity_stale")
                    ref = marker["receipt"]
                    require(ref["path"] == str(dest / "receipt.json"), 2, "cache_receipt_escape")
                    record = validate_previous(ref, self.root)
                    require(record["key"] == key, 4, "cache_record_key_stale")
                    require({k: record[k] for k in identity} == identity, 4, "cache_identity_stale")
                    for item in record["outputs"] + record["evidence"]:
                        authenticate(item, inside=dest)
                    if hasattr(self.backend, "verify_cached"):
                        self.backend.verify_cached(name, dest, record)
                    cache_hit = True
                else:
                    scratch = Path(tempfile.mkdtemp(prefix=".stage-", dir=parent))
                    result = self.backend.execute(name, scratch, subject)
                    require(type(result) is dict and type(result.get("rc")) is int
                            and result["rc"] in core.RC, 2, "invalid_stage_rc")
                    if result["rc"]:
                        reasons = result.get("reason_codes") or ["stage_refused"]
                        raise Refusal(result["rc"], str(reasons[0]))
                    # Recheck the source and every prefix receipt after the writer.
                    for item in inputs:
                        authenticate(item)
                    for prior in receipts:
                        validate_previous(prior, self.root)
                    require(stage_identity() == (inputs, params, pin), 4, "stage_identity_drift")
                    files = sorted(scratch.rglob("*"))
                    require(all(not p.is_symlink() for p in files), 2, "stage_symlink_refused")
                    require(not any((scratch / p).exists() for p in ("receipt.json", "entry.json")),
                            2, "reserved_stage_path")
                    evidence_names = result.get("evidence", [])
                    output_names = result.get("outputs", [])
                    require(bool(evidence_names) and bool(output_names), 3, "stage_evidence_missing")
                    def refs(names):
                        found = []
                        for value in names:
                            path = scratch / value
                            require(path.is_relative_to(scratch) and ".." not in Path(value).parts
                                    and not Path(value).is_absolute(), 2, "stage_path_escape")
                            item = file_ref(path)
                            item["path"] = str(dest / path.relative_to(scratch))
                            found.append(item)
                        return found
                    outrefs, evidence = refs(output_names), refs(evidence_names)
                    subject_name = result.get("subject")
                    new_subject = str(dest / subject_name) if subject_name is not None else None
                    record = self._record(name, key, inputs, params, pin, previous, 0, [], begin,
                                          outputs=outrefs, evidence=evidence, subject=new_subject,
                                          checks=result.get("checks_evaluated", 0))
                    validate_stage(record)
                    core.atomic_write_json(scratch / "receipt.json", record)
                    ref = file_ref(scratch / "receipt.json")
                    ref["path"] = str(dest / "receipt.json")
                    core.atomic_write_json(scratch / "entry.json", {"schema": "af.pipeline-cache/v1",
                                           "key": key, "receipt": ref})
                    for path in scratch.rglob("*"):
                        if path.is_file():
                            with path.open("rb") as stream:
                                os.fsync(stream.fileno())
                    os.replace(scratch, dest)
                    parentfd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
                    try:
                        os.fsync(parentfd)
                    finally:
                        os.close(parentfd)
                    scratch = None
                    validate_previous(ref, self.root)
                subject = Path(record["subject"]) if record["subject"] else subject
                if name == "provider" and hasattr(self.backend, "bind_raw"):
                    require(subject is not None, 3, "retained_raw_absent")
                    self.backend.bind_raw(subject)
            except Exception as exc:
                rc, reason = fault(exc)
                blocked = rc
                if self._plan:
                    params = {**params, "stage_plan": self._plan}
                if key is None:
                    diagnostic = {"stage": name, "previous": previous, "blocked": reason}
                    if self._plan:
                        diagnostic["stage_plan"] = self._plan
                    key = core.sha256_json(diagnostic)
                record = self._record(name, key, inputs, params, pin, previous, rc, [reason], begin)
                # Retain rejected bytes for inspection. They are never cache
                # entries or published output artifacts, and never authorize work.
                if scratch is not None:
                    rejected = self.root / "rejected"
                    owned_directory(rejected, self.root)
                    retained = rejected / (name + "-" + scratch.name.lstrip("."))
                    os.replace(scratch, retained)
                    scratch = None
                    record["absence_reasons"]["rejected_artifacts"] = str(retained)
                if hasattr(exc, "evidence_record"):
                    record["evidence"] = [self._diagnostic(exc.evidence_record)]
                validate_stage(record)
                ref = self._diagnostic(record)
            finally:
                if scratch is not None:
                    shutil.rmtree(scratch)
            previous = ref
            receipts.append(ref)
            counts.append(record["checks_evaluated"])
            rows.append({"stage": name, "rc": record["rc"], "verdict": record["verdict"],
                         "receipt": ref, "cache_hit": cache_hit})
        # A last gate closes the mutation window following the terminal stage.
        codes = [r["rc"] for r in rows]
        reasons = []
        dispositions = []
        try:
            for row in rows:
                authenticate(row["receipt"], inside=self.root)
                if row["rc"] == 0:
                    record = validate_previous(row["receipt"], self.root)
                    params = record["parameters"]
                    if "production_verdict" in params:
                        verdict = params["production_verdict"]
                        require(type(verdict) is str and verdict in PRODUCTION_RC,
                                2, "production_verdict_invalid")
                        if PRODUCTION_RC[verdict]:
                            reason = params.get("calibration_state")
                            if verdict != "CANNOT_SEE" or type(reason) is not str or not reason:
                                reason = "stage_production_" + verdict.lower()
                            dispositions.append((PRODUCTION_RC[verdict], reason))
            self.backend.verify_sources()
        except Exception as exc:
            rc, reason = fault(exc)
            codes.append(rc)
            reasons.append(reason)
        scope = CHARACTER_SCOPE if self._plan else self.backend.scope
        if scope == CHARACTER_SCOPE:
            dispositions.append((3, CHARACTER_CALIBRATION))
        rc = core.worst_rc(codes + [code for code, _ in dispositions])
        reasons.extend(dict.fromkeys(reason for code, reason in dispositions if code == rc))
        if any(r["rc"] for r in rows) and "stage_not_pass" not in reasons:
            reasons.append("stage_not_pass")
        run_identity = [r["receipt"] for r in rows]
        if self._plan:
            run_identity = {"stage_plan": self._plan, "receipts": run_identity}
        result = {"schema": "af.pipeline-run/v1", "scope": scope,
                  "rc": rc, "status": core.STATUS[rc], "verdict": core.VERDICT[rc],
                  "run_key": core.sha256_json(run_identity), "stages": rows,
                  "checks_evaluated": sum(counts),
                  "wall_ms": (time.monotonic_ns() - started) // 1000000,
                  "artifact": file_ref(subject) if rc == 0 and subject else None,
                  "reason_codes": reasons or (["stage_not_pass"] if rc else [])}
        core.validate(result, RUN)
        target = self.root / "runs" / (core.sha256_json(result) + ".json")
        owned_directory(target.parent, self.root)
        require(target.parent.resolve() == target.parent, 2, "run_path_escape")
        core.atomic_write_json(target, result)
        return result

    def archive_failed_record(self, raw, *, approved_scopes, read_checked):
        """Retain verified failure-only BASE histories outside cache and registry."""
        from asset_factory import math_acceptance
        from tools.registry import _disk

        require(type(raw) is bytes and 0 < len(raw) <= _disk.RECORD_BYTE_LIMIT,
                2, "archive_record_invalid")
        witnesses = {}
        total = len(raw)
        over_budget = False

        def retain(role, pin):
            nonlocal total, over_budget
            if pin not in witnesses:
                data = read_checked(role, pin)
                if isinstance(data, bytes):
                    total += len(data)
                    if total > MAX_JSON:
                        over_budget = True
                        raise Refusal(6, "archive_witness_budget")
                    witnesses[pin] = data
                return data
            # Each lookup must still have authority for this role.
            data = read_checked(role, pin)
            require(data == witnesses[pin], 4, "archive_witness_drift")
            return data

        decision = math_acceptance.verify_record(raw, approved_scopes=approved_scopes, read_checked=retain)
        require(not over_budget, 6, "archive_witness_budget")
        require(decision["rc"] == 3 and decision["reasons"] == ["missing_required_gate"],
                decision["rc"] or 2, "archive_requires_verified_failure_history")
        record = json.loads(raw)
        require(record["gates"] and all(g["verdict"] != "PASS" for g in record["gates"]),
                2, "archive_requires_failed_attempts")

        def conflict(_existing, _incoming):
            raise Refusal(4, "archive_bytes_stale")

        require(self.root.resolve() == self.root, 2, "output_path_invalid")
        self.root.mkdir(parents=True, exist_ok=True)
        with _disk.root_directory(self.root) as root:
            _disk.ensure_directory(root, "attempts")
            with _disk.directory(root, "attempts") as attempts, _disk.writer(attempts):
                for name in ("records", "witnesses", "archive-staging"):
                    _disk.ensure_directory(attempts, name)
                with _disk.directory(attempts, "archive-staging") as staging:
                    _disk.recover_staging(staging)
                    with _disk.directory(attempts, "witnesses") as blobs:
                        for pin, data in witnesses.items():
                            _disk.install(staging, blobs, pin[7:], data, conflict)
                    digest = hashlib.sha256(raw).hexdigest()
                    with _disk.directory(attempts, "records") as records:
                        _disk.install(staging, records, digest, raw, conflict, record=True)
        return {"path": str(self.root / "attempts" / "records" / digest), "sha256": digest}

    def _diagnostic(self, record):
        directory = self.root / "attempts"
        owned_directory(directory, self.root)
        require(directory.resolve() == directory, 2, "attempt_path_escape")
        path = directory / (core.sha256_json(record) + ".json")
        core.atomic_write_json(path, record)
        return file_ref(path)

    @staticmethod
    def _record(name, key, inputs, params, pin, previous, rc, reasons, started,
                *, outputs=None, evidence=None, subject=None, checks=0):
        return {"schema": "af.pipeline-stage/v1", "stage": name, "key": key,
                "rc": rc, "status": core.STATUS[rc], "verdict": core.VERDICT[rc],
                "inputs": inputs, "parameters": params, "toolchain_sha256": pin,
                "previous": previous, "outputs": outputs or [], "evidence": evidence or [],
                "subject": subject, "checks_evaluated": checks,
                "wall_ms": (time.monotonic_ns() - started) // 1000000,
                "peak_rss_bytes": None, "absence_reasons": {"peak_rss_bytes": "not_independently_measured"},
                "reason_codes": reasons}
