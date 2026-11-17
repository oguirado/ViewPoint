"""Linear, immutable stage cache. A corrupt committed entry never recomputes.

The marker is the commit point; each cache read authenticates both the receipt
and every declared artifact/evidence file. Failed attempts live outside cache.
The run lock protects the complete output tree. Recovery is manual after crash.
"""
import fcntl
import os
from pathlib import Path
import shutil
import tempfile
import time

from asset_factory import core
from tools.blender.bake_support import read_bytes
from .contracts import RUN, validate_stage

STAGES = ("input", "provider", "mesh", "topology", "uv_bake_orm", "export", "appearance", "vqa3d")
MAX_JSON = 16 * 2**20  # [TARGET] ingestion budget, not a quality threshold.


class Refusal(Exception):
    def __init__(self, rc, reason):
        super().__init__(reason)
        self.rc, self.reason = (rc, reason) if type(rc) is int and rc in core.RC and rc != 0 else (2, "invalid_refusal_rc")


def require(condition, rc, reason):
    if not condition:
        raise Refusal(rc, reason)


def load(path):
    # read_bytes rejects symlinks, devices and unbounded input before parsing.
    read_bytes(path)
    return core.load_json(Path(path), max_bytes=MAX_JSON)


def file_ref(path):
    path = Path(path).absolute()
    require(path.resolve() == path, 2, "noncanonical_path")
    require(not (path.name in ("auth.json", "operator.token", "launch.token")
                 or path.name.startswith(".env") or path.suffix == ".key"
                 or ".gemini" in path.parts), 2, "sensitive_path_refused")
    data = read_bytes(path)
    import hashlib
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
    record = load(ref["path"])
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
    def __init__(self, out, backend):
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
        for name in STAGES:
            begin = time.monotonic_ns()
            inputs, params, pin, key = [], {}, "0" * 64, None
            record, ref, cache_hit, scratch = None, None, False, None
            try:
                require(blocked is None, blocked or 3, "upstream_blocked")
                # Revalidate the complete prefix before every invocation, including
                # cache hits; callbacks may have changed any earlier receipt.
                for prior in receipts:
                    validate_previous(prior, self.root)
                inputs, params, pin = self.backend.identity(name, subject)
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
                    require(self.backend.identity(name, subject) == (inputs, params, pin), 4, "stage_identity_drift")
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
                key = key or core.sha256_json({"stage": name, "previous": previous, "blocked": reason})
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
        try:
            for row in rows:
                authenticate(row["receipt"], inside=self.root)
                if row["rc"] == 0:
                    validate_previous(row["receipt"], self.root)
            self.backend.verify_sources()
        except Exception as exc:
            rc, reason = fault(exc)
            codes.append(rc)
            reasons.append(reason)
        rc = core.worst_rc(codes)
        result = {"schema": "af.pipeline-run/v1", "scope": self.backend.scope,
                  "rc": rc, "status": core.STATUS[rc], "verdict": core.VERDICT[rc],
                  "run_key": core.sha256_json([r["receipt"] for r in rows]), "stages": rows,
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
