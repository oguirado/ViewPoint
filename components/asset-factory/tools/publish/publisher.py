"""Plan-bound additive publishing with factory-stage return codes.

Asset versions and content-addressed publication events are immutable. Log and
catalogue caches are disposable; readers always reconcile the event set.
"""

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import stat
import tempfile
import time

from asset_factory import core
from tools.publish.local_store import (
    CredentialPathRefusal, LocalBudgetExceeded, LocalPathRefusal, LocalStore,
    bound_directory, check_names, child_directory, directory_identity, mount_id,
    remove_tree, rename_noreplace,
)

MAX_JSON = 16 * 1024 * 1024  # DESIGN_CHOICE: bounded plans and remote metadata.
MAX_FILES = 10000  # DESIGN_CHOICE: refuse larger inventories; never truncate.
MAX_LOCK_FILES = 1024
VERDICTS = ("PASS", "FAIL", "CANNOT_SEE", "ERROR")
METADATA_POLICY = {
    "schema": "af.publish-metadata-policy/v1",
    "immutable_assets": True,
    "mutable_paths": ["_estado/log.jsonl", "_index/catalog.json"],
    "history": "grow_only_content_addressed_event_set",
    "events": "_estado/events/<event_sha256>.json",
    "catalogue": "deterministic_event_set_union",
    "single_host_writer": False,
}
_SCHEMA = Path(__file__).with_name("contracts.schema.json")
_SLUG = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,95}\Z")
_PART = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}\Z")
_HEX = re.compile(r"[0-9a-f]{64}\Z")
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_ARTIFACT_SUFFIXES = {
    "meshes": {".glb"},
    "textures": {".png", ".exr"},
    "receipts": {".json"},
    "records": {".json"},
    "renders": {".png", ".jpg", ".webp", ".mp4", ".gif"},
    "physics": {".xml", ".mjcf", ".glb", ".obj", ".json"},
    "cad": {".step", ".stp"},
}
_SESSION_ROOTS = {}
GATE_SUBJECTS = {"af.mesh-regression/v1": "candidate_sha256",
                 "af.bridge-verdict/v1": "source_sha256"}


class PublishError(Exception):
    def __init__(self, reason, rc=2, verdict=None):
        if type(rc) is not int or rc not in core.RC or rc == 0:
            reason, rc, verdict = "invalid_return_code", 2, "ERROR"
        self.reason, self.rc, self.verdict = reason, rc, verdict
        super().__init__(reason)


def require(condition, reason, rc=2):
    if not condition:
        raise PublishError(reason, rc)


def contract(name, value):
    schema = core.load_json(_SCHEMA, max_bytes=MAX_JSON)
    try:
        core.validate(value, {"$defs": schema["$defs"], "$ref": "#/$defs/" + name})
    except core.ContractError as exc:
        raise PublishError("invalid_" + name) from exc


def decode(raw):
    require(len(raw) <= MAX_JSON, "metadata_budget_exceeded", 6)
    def pairs(items):
        result = {}
        for key, val in items:
            require(key not in result, "duplicate_json_key")
            result[key] = val
        return result
    def number(_):
        raise PublishError("non_integer_number")
    try:
        value = json.loads(raw, object_pairs_hook=pairs, parse_float=number,
                           parse_constant=number)
        core.canonical_json(value)
        return value
    except (ValueError, UnicodeError, core.ContractError, RecursionError) as exc:
        raise PublishError("invalid_json") from exc


def read_json(path):
    path = Path(path).absolute()
    try:
        check_names(path.parts, allow_config_directory=True)
        storage = LocalStore(path.parent, 120, MAX_FILES)
        with storage.open_file(path.name) as stream:
            return decode(stream.read(MAX_JSON + 1))
    except CredentialPathRefusal as exc:
        raise PublishError("credential_source_forbidden") from exc
    except LocalPathRefusal as exc:
        raise PublishError("request_path_unsafe", 5) from exc
    except LocalBudgetExceeded as exc:
        raise PublishError("metadata_budget_exceeded", 6) from exc


def encoded(value):
    return core.canonical_json(value).encode("utf-8")


def digests(raw):
    return {"size": len(raw), "sha256": hashlib.sha256(raw).hexdigest(),
            "md5": hashlib.md5(raw, usedforsecurity=False).hexdigest()}


def file_digests(path):
    with Path(path).open("rb") as stream:
        return stream_digests(stream)


def stream_digests(stream):
    sha, md5, size = hashlib.sha256(), hashlib.md5(usedforsecurity=False), 0
    for block in iter(lambda: stream.read(1024 * 1024), b""):
        sha.update(block)
        md5.update(block)
        size += len(block)
    return {"size": size, "sha256": sha.hexdigest(), "md5": md5.hexdigest()}


def enforce_hygiene(parts):
    try:
        check_names(parts)
    except CredentialPathRefusal as exc:
        raise PublishError("credential_source_forbidden") from exc


def slug(value):
    require(type(value) is str and _SLUG.fullmatch(value) is not None
            and value not in (".", ".."), "invalid_asset_identity")
    return value


def relative(value):
    require(type(value) is str and value and "\\" not in value
            and ":" not in value and not _CONTROL.search(value), "invalid_path")
    path = PurePosixPath(value)
    enforce_hygiene(path.parts)
    require(not path.is_absolute() and str(path) == value
            and all(_PART.fullmatch(p) and p not in (".", "..") for p in path.parts),
            "invalid_path")
    return value


def safe_source(value, source_root=None):
    path = Path(value)
    require(path.is_absolute(), "source_must_be_absolute")
    require(".." not in path.parts, "source_outside_asset_tree")
    enforce_hygiene(path.parts)
    require(all(not p.is_symlink() for p in (path, *path.parents)), "source_symlink")
    if source_root is not None:
        root = Path(source_root)
        require(root.is_absolute() and root != Path("/") and root.is_dir()
                and str(root) == str(root.resolve())
                and not any(p.is_symlink() for p in (root, *root.parents)), "invalid_source_root")
        require(path.is_relative_to(root), "source_outside_asset_tree")
    require(path.is_file(), "source_unavailable")
    return path


def source_snapshot(value, source_root, *, timeout_s, output=None, capture_limit=None):
    """Validate, hash and optionally copy the same no-follow regular-file handle."""
    path = safe_source(value, source_root)
    store = LocalStore(source_root, timeout_s, MAX_FILES)
    sha, md5, size = hashlib.sha256(), hashlib.md5(usedforsecurity=False), 0
    chunks = []
    try:
        with store.open_file(path.relative_to(source_root)) as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                store._tick()
                sha.update(block)
                md5.update(block)
                size += len(block)
                if output is not None:
                    output.write(block)
                if capture_limit is not None:
                    if size > capture_limit:
                        raise LocalBudgetExceeded()
                    chunks.append(block)
    except (OSError, LocalPathRefusal) as exc:
        raise PublishError("source_unsafe", 2) from exc
    except LocalBudgetExceeded as exc:
        raise PublishError("source_budget_exceeded", 6) from exc
    return path, {"size": size, "sha256": sha.hexdigest(), "md5": md5.hexdigest()}, b"".join(chunks)


def root_path(value):
    require(type(value) is str, "invalid_destination")
    value = value.rstrip("/")
    require(value.startswith(":local:"), "remote_disabled", 5)
    local_name = value[len(":local:"):]
    require(not _CONTROL.search(local_name), "invalid_local_destination")
    local = Path(local_name)
    enforce_hygiene(local.parts)
    require(local.anchor == "/" and local != Path("/") and ".." not in local.parts
            and not any(p.is_symlink() for p in (local, *local.parents))
            and str(local) == local_name, "invalid_local_destination")
    return value + "/"


class NamespaceAuthority:
    """Trusted host composition only; never construct from request/config data.

    The host authenticates principals before issuing sessions. HTTP/CLI data
    cannot mint these opaque objects. Arbitrary Python execution is trusted.
    """
    def __init__(self, grants):
        self._roots = {slug(principal): root_path(root) for principal, root in grants.items()}
        self._sessions = {}

    def session(self, authenticated_principal):
        require(authenticated_principal in self._roots, "namespace_not_authorised", 5)
        if authenticated_principal not in self._sessions:
            token = object()
            self._sessions[authenticated_principal] = token
            _SESSION_ROOTS[token] = self._roots[authenticated_principal]
        return self._sessions[authenticated_principal]


def namespace_access(config, session):
    require(type(session) is object and session in _SESSION_ROOTS,
            "namespace_session_required", 3)
    require(config["destination_root"] == _SESSION_ROOTS[session], "namespace_not_authorised", 5)


def configuration(payload=None):
    require(payload is not None, "publish_config_missing", 3)
    contract("config", payload)
    result = {"schema": "af.publish-config/v1", "standing_approval": False,
              "timeout_s": 120, "busy_attempts": 3}
    result.update(payload)
    require("remote" not in result and "root" not in result, "remote_disabled", 5)
    require("destination_root" in result, "publish_config_incomplete", 3)
    result["destination_root"] = root_path(result["destination_root"])
    require(1 <= result["timeout_s"] <= 300, "invalid_timeout")
    require(1 <= result["busy_attempts"] <= 5, "invalid_busy_attempts")
    return result


def config_path():
    return Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))) / "viewpoint/asset-factory/publish.json"


def load_configuration(path=None):
    target = Path(path) if path else config_path()
    if path or target.exists():
        return configuration(read_json(target))
    return configuration({"schema": "af.publish-config/v1"})


def asset_prefix(asset):
    for name in ("asset_class", "asset_id", "version"):
        slug(asset[name])
        enforce_hygiene((asset[name],))
    require(asset["final_verdict"] in VERDICTS, "invalid_verdict")
    prefix = "/".join(asset[k] for k in ("asset_class", "asset_id", "version")) + "/"
    return prefix if asset["final_verdict"] == "PASS" else "_review/" + prefix


def artifact_path(value):
    relative(value)
    path = PurePosixPath(value)
    if value in ("asset.glb", "asset.blend"):
        return
    require(len(path.parts) >= 2, "unsupported_artifact_path")
    role = path.parts[0]
    require(path.suffix in _ARTIFACT_SUFFIXES.get(role, ())
            and (role != "renders" or len(path.parts) == 2
                 and path.stem in ("front", "three_quarter", "turntable")),
            "unsupported_artifact_path")


def check_artifact_source(value, artifact):
    path = Path(value)
    enforce_hygiene(path.parts)
    require(path.suffix.lower() == PurePosixPath(artifact).suffix, "source_artifact_type_mismatch")


def validate_provenance(provenance, verdict, files, *, mesh_sha256=None):
    contract("provenance", provenance)
    for item in provenance["inputs"]:
        require(_HEX.fullmatch(item["sha256"]) is not None, "invalid_input_digest")
    names = set()
    for gate in provenance["gates"]:
        require(gate["name"] and gate["name"] not in names, "duplicate_gate")
        names.add(gate["name"])
        if gate["receipt"] is not None:
            require(gate["receipt"].startswith("receipts/") and
                    gate["receipt"] in files, "gate_receipt_missing")
            data = decode(files[gate["receipt"]])
            require(type(data) is dict, "invalid_gate_receipt")
            key = gate["verdict_key"]
            if key is not None:
                if key == "status" and data.get("schema") == "af.mesh-regression/v1":
                    require(type(data.get("rc")) is int and data["rc"] in core.RC
                            and data.get("status") == core.STATUS[data["rc"]]
                            and gate["verdict"] == core.VERDICT[data["rc"]], "gate_verdict_mismatch")
                else:
                    require(key in ("verdict", "appearance_verdict", "input_gate_verdict")
                            and data.get(key) == gate["verdict"], "gate_verdict_mismatch")
            elif verdict == "PASS":
                raise PublishError("unproven_gate")
            metrics = data.get("metrics", {})
            require(type(metrics) is dict, "invalid_gate_receipt")
            for name, number in gate["numbers"].items():
                require(name in data or name in metrics, "unbound_gate_number")
                require(data.get(name, metrics.get(name)) == number, "gate_number_mismatch")
            # A PASS gate must bind the exact uploaded GLB, not another asset.
            if verdict == "PASS":
                mesh_sha = mesh_sha256 or digests(files["asset.glb"])["sha256"]
                require(type(data) is dict and data.get("schema") in GATE_SUBJECTS,
                        "unsupported_gate_family")
                subject_key = GATE_SUBJECTS[data["schema"]]
                require(data.get(subject_key) == mesh_sha, "gate_subject_mismatch")
                require(type(data.get("rc")) is int and data["rc"] == 0
                        and (data.get("verdict") == "PASS" or
                             data["schema"] == "af.mesh-regression/v1" and data.get("status") == "OK"),
                        "invalid_gate_receipt")
                if data["schema"] == "af.mesh-regression/v1" and data.get("artefact_sha256") is not None:
                    require(data["artefact_sha256"] == mesh_sha, "gate_subject_mismatch")
                require(bool(gate["numbers"]) and all(v is not None for v in
                        gate["numbers"].values()), "gate_numbers_missing")
        else:
            require(verdict != "PASS" and bool(provenance["absence_reasons"]),
                    "gate_receipt_missing")
    if verdict == "PASS":
        model = provenance["model"]
        require(bool(model["id"]) and bool(model["revision"]) and
                model["revision"] not in ("main", "master", "latest", "HEAD") and
                not provenance["absence_reasons"] and
                all(g["verdict"] == "PASS" for g in provenance["gates"]),
                "unproven_pass")


def build_plan(source, config=None):
    """Build an offline inventory from the explicitly declared sources."""
    config = configuration(config)
    require(config.get("source_root"), "source_root_required", 3)
    contract("source", source)
    root, assets, seen = config["destination_root"], [], set()
    for asset in source["assets"]:
        prefix = asset_prefix(asset)
        require(prefix not in seen, "duplicate_asset_version")
        seen.add(prefix)
        files, raw_files = [], {}
        evidence_remaining = MAX_JSON
        for item in asset["files"]:
            name = item["path"]
            artifact_path(name)
            check_artifact_source(item["source"], name)
            require(name not in raw_files, "duplicate_file")
            # Keep receipt bytes and use the streamed digest for the GLB subject.
            path, info, raw = source_snapshot(
                item["source"], config["source_root"], timeout_s=config["timeout_s"],
                capture_limit=evidence_remaining if name.startswith("receipts/") else None)
            evidence_remaining -= len(raw)
            raw_files[name] = raw
            files.append({"path": name, "source": str(path), "content": None,
                          **info, "destination": root + prefix + name})
        require("asset.glb" in raw_files and any(n.startswith("receipts/") for n in raw_files),
                "asset_or_receipts_missing")
        mesh_sha = next(item["sha256"] for item in files if item["path"] == "asset.glb")
        validate_provenance(asset["provenance"], asset["final_verdict"], raw_files, mesh_sha256=mesh_sha)
        provenance = {"schema": "af.publish-provenance/v1",
                      **{k: asset[k] for k in ("asset_class", "asset_id", "version", "final_verdict")},
                      **asset["provenance"],
                      "files": [{k: f[k] for k in ("path", "size", "sha256", "md5")}
                                for f in sorted(files, key=lambda f: f["path"])]}
        generated = {"provenance.json": encoded(provenance)}
        if asset["final_verdict"] != "PASS":
            # The verdict appears in the filename, before VERDICT.txt.
            generated[asset["final_verdict"] + ".VERDICT.txt"] = (
                asset["final_verdict"] + "\nReview only; not an approved production asset.\n").encode()
        for name, raw in generated.items():
            files.append({"path": name, "source": None, "content": raw.decode(),
                          **digests(raw), "destination": root + prefix + name})
        assets.append({**{k: asset[k] for k in ("asset_class", "asset_id", "version", "final_verdict")},
                       "destination": root + prefix,
                       "files": sorted(files, key=lambda f: f["path"])})
    assets.sort(key=lambda a: a["destination"])
    count = sum(len(a["files"]) for a in assets)
    require(count <= MAX_FILES, "file_budget_exceeded", 6)
    plan = {"schema": "af.publish-plan/v1", "dry_run": True, "destination_root": root,
            "metadata_policy": METADATA_POLICY.copy(), "assets": assets,
            "file_count": count, "total_bytes": sum(f["size"] for a in assets for f in a["files"]),
            "errors": 0, "warnings": ["Remote state unobserved: this is a local inventory, not transfer success."]}
    contract("plan", plan)
    return plan


def validate_plan(plan, config):
    contract("plan", plan)
    require(plan["destination_root"] == config["destination_root"], "destination_changed")
    require(plan["metadata_policy"] == METADATA_POLICY, "metadata_policy_changed")
    count, total, seen = 0, 0, set()
    for asset in plan["assets"]:
        prefix = asset_prefix(asset)
        require(prefix not in seen, "duplicate_asset_version")
        seen.add(prefix)
        require(asset["destination"] == config["destination_root"] + prefix, "review_routing_violation")
        files = {}
        for item in asset["files"]:
            name = relative(item["path"])
            require(name not in files, "duplicate_file")
            require(item["destination"] == asset["destination"] + name, "destination_escape")
            require((item["source"] is None) != (item["content"] is None), "invalid_file_source")
            if item["source"] is None:
                require(name == "provenance.json" or
                        name == asset["final_verdict"] + ".VERDICT.txt", "unexpected_generated_file")
                raw = item["content"].encode()
                require(digests(raw) == {k: item[k] for k in ("size", "sha256", "md5")}, "plan_digest_mismatch")
            else:
                artifact_path(name)
                check_artifact_source(item["source"], name)
                require(_HEX.fullmatch(item["sha256"]) is not None and
                        re.fullmatch("[0-9a-f]{32}", item["md5"]) and item["size"] >= 0,
                        "invalid_file_digest")
            files[name] = item
            count += 1
            total += item["size"]
        require("provenance.json" in files and "asset.glb" in files, "missing_provenance_or_asset")
        prov = decode(files["provenance.json"]["content"])
        require(all(prov.get(k) == asset[k] for k in
                    ("asset_class", "asset_id", "version", "final_verdict")), "provenance_identity_mismatch")
        require(prov.get("schema") == "af.publish-provenance/v1" and prov.get("files") ==
                [{k: f[k] for k in ("path", "size", "sha256", "md5")}
                 for f in sorted(files.values(), key=lambda f: f["path"]) if f["source"] is not None],
                "provenance_manifest_mismatch")
        if asset["final_verdict"] != "PASS":
            require(asset["final_verdict"] + ".VERDICT.txt" in files, "review_verdict_missing")
    require(0 < count <= MAX_FILES and count == plan["file_count"] and total == plan["total_bytes"],
            "plan_totals_mismatch")


def authorise(plan, config, approval):
    require(approval is not None, "approval_required")
    contract("approval", approval)
    require(approval["plan_sha256"] == core.sha256_json(plan), "approval_plan_mismatch")
    require(approval["destination_root"] == config["destination_root"], "approval_destination_mismatch")


def lock_user_root():
    """Systemd's per-user runtime, independent of caller environment/scratch.

    The required test runner maps the host user to namespace root. Translate
    the effective UID through Linux's kernel-owned map before selecting the
    runtime directory, then verify ownership using namespace-visible IDs.
    Missing runtime storage is a refusal, never a fallback to another domain.
    """
    uid = os.geteuid()
    for row in Path("/proc/self/uid_map").read_text().splitlines():
        inside, outside, count = map(int, row.split())
        if inside <= uid < inside + count:
            return Path("/run/user") / str(outside + uid - inside)
    raise PublishError("lock_identity_unavailable", 3)


def acquire_lock(fd, attempts, busy_reason):
    for attempt in range(attempts):
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            if attempt + 1 == attempts:
                raise PublishError(busy_reason, 5)
            time.sleep(1)


@contextmanager
def lock_descriptor(root, attempts):
    """Pin protected lock storage; never unlink or replace a lock inode."""
    parent = directory = fd = None
    try:
        runtime = lock_user_root()
        parent = LocalStore(runtime, 5, MAX_FILES)._directory(runtime)
        info = os.fstat(parent)
        require(info.st_uid == os.geteuid() and stat.S_IMODE(info.st_mode) == 0o700,
                "lock_storage_unsafe", 5)
        directory = child_directory(parent, "af-publish-locks", create=True)
        info = os.fstat(directory)
        require(info.st_uid == os.geteuid() and stat.S_IMODE(info.st_mode) == 0o700,
                "lock_storage_unsafe", 5)
        # Export roots have one canonical spelling; imports use the already
        # validated absolute local-store identity in their separate domain.
        identity = root if root.startswith("import:") else root_path(root)
        name = hashlib.sha256(identity.encode()).hexdigest() + ".lock"
        flags = os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK
        try:
            fd = os.open(name, flags, dir_fd=directory)
        except FileNotFoundError:
            # Serialize admission of new names. Existing locks keep their old
            # inodes, including when the directory is at capacity.
            acquire_lock(directory, attempts, "lock_storage_busy")
            try:
                try:
                    fd = os.open(name, flags, dir_fd=directory)
                except FileNotFoundError:
                    with os.scandir(directory) as entries:
                        for count, _ in enumerate(entries, 1):
                            require(count < MAX_LOCK_FILES, "lock_capacity_exceeded", 6)
                    fd = os.open(name, flags | os.O_CREAT, 0o600, dir_fd=directory)
            finally:
                fcntl.flock(directory, fcntl.LOCK_UN)
        info = os.fstat(fd)
        require(stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid() and
                stat.S_IMODE(info.st_mode) == 0o600 and info.st_nlink == 1
                and mount_id(fd) == mount_id(directory) == mount_id(directory, name),
                "lock_storage_unsafe", 5)
        bound_directory(parent, "af-publish-locks", directory)
    except (OSError, LocalPathRefusal, LocalBudgetExceeded, ValueError) as exc:
        for opened in (fd, directory, parent):
            if opened is not None:
                os.close(opened)
        raise PublishError("lock_storage_unavailable", 3) from exc
    except BaseException:
        for opened in (fd, directory, parent):
            if opened is not None:
                os.close(opened)
        raise
    try:
        yield fd
    finally:
        for opened in (fd, directory, parent):
            os.close(opened)


@contextmanager
def destination_lock(root, attempts):
    with lock_descriptor(root, attempts) as fd:
        acquire_lock(fd, attempts, "transfer_in_progress")
        yield


@contextmanager
def local_io(reason, *, credential_reason="credential_path_forbidden", budget_reason="local_destination_budget_exceeded"):
    try:
        yield
    except CredentialPathRefusal as exc:
        raise PublishError(credential_reason, 5) from exc
    except (OSError, LocalPathRefusal) as exc:
        raise PublishError(reason, 5) from exc
    except (LocalBudgetExceeded, RecursionError) as exc:
        raise PublishError(budget_reason, 6) from exc
    except NotImplementedError as exc:
        raise PublishError("atomic_commit_unavailable", 3) from exc


class LocalPublication:
    """Authorized local destination. All byte operations use the same root fd."""

    def __init__(self, root, timeout_s):
        self.root = root_path(root)
        self.storage = LocalStore(self.root[len(":local:"):], timeout_s, MAX_FILES)
        self.fd = None
        self.commands = []

    @contextmanager
    def open(self, *, create=False):
        with local_io("local_destination_unsafe"):
            try:
                self.fd = self.storage._directory(self.storage.root, create=create)
            except FileNotFoundError:
                raise PublishError("local_destination_missing", 3) from None
            try:
                yield self
                self.storage.assert_root(self.fd)
            finally:
                os.close(self.fd)
                self.fd = None

    def inventory(self):
        self.storage.entries = 0
        result = {}
        for relative_name, parent, name in self.storage.walk(self.fd, ignore_write_temps=True):
            relative(relative_name)
            with self.storage._file(parent, name) as stream:
                info, _ = self.storage.digest(stream)
            result[relative_name] = {key: info[key] for key in ("size", "md5")}
        self.storage.assert_root(self.fd)
        return result

    def read(self, name, inventory):
        if name not in inventory:
            return None
        return self._observed(name, inventory, capture_limit=MAX_JSON)[1]

    def measure(self, name, inventory):
        return self._observed(name, inventory)[0]

    def _observed(self, name, inventory, *, capture_limit=None):
        require(name in inventory, "remote_witness_missing", 3)
        with self.storage.open_file(relative(name), root_fd=self.fd) as stream:
            info, raw = self.storage.digest(stream, capture_limit=capture_limit)
        require(all(info[k] == inventory[name][k] for k in ("size", "md5")), "remote_read_drift", 4)
        return info, raw

    def copy(self, source, destination=None, *, mutable=False):
        prefix = "" if destination is None else self.suffix(destination)
        sources = LocalStore(source, 120, MAX_FILES)
        fd = sources._directory(source)
        try:
            for name, parent, leaf in sources.walk(fd):
                relative(name)
                target = prefix + "/" + name if prefix else name
                if mutable:
                    require(target in METADATA_POLICY["mutable_paths"], "mutable_scope_violation")
                self.storage.assert_root(self.fd)
                with sources._file(parent, leaf) as stream:
                    self.storage.put(self.fd, target, stream, mutable=mutable)
        finally:
            os.close(fd)

    def check(self, source, destination):
        prefix = self.suffix(destination, allow_root=True)
        sources = LocalStore(source, 120, MAX_FILES)
        fd = sources._directory(source)
        count = 0
        try:
            for name, parent, leaf in sources.walk(fd):
                target = prefix + "/" + name if prefix else name
                with sources._file(parent, leaf) as expected, self.storage.open_file(
                        target, root_fd=self.fd) as actual:
                    require(sources.digest(expected)[0] == self.storage.digest(actual)[0],
                            "local_copy_mismatch", 5)
                count += 1
        finally:
            os.close(fd)
        require(count > 0, "check_coverage_incomplete", 3)

    def suffix(self, destination, *, allow_root=False):
        require(destination.startswith(self.root), "remote_scope_violation")
        suffix = destination[len(self.root):].rstrip("/")
        return "" if allow_root and not suffix else relative(suffix)

    def download(self, source, target_store, target_fd):
        self.storage.entries = 0
        with self.storage.directory(self.fd, self.suffix(source)) as source_fd:
            for name, parent, leaf in self.storage.walk(source_fd):
                with self.storage._file(parent, leaf) as stream:
                    target_store.put(target_fd, relative(name), stream)
        self.storage.assert_root(self.fd)


def stage(plan, target, source_root, *, timeout_s=120):
    """Snapshot and rehash before any remote access, including approval replays."""
    measured = {}
    for asset in plan["assets"]:
        prefix = asset_prefix(asset)
        evidence = {}
        evidence_remaining = MAX_JSON
        for item in asset["files"]:
            dest = target / prefix / item["path"]
            dest.parent.mkdir(parents=True, exist_ok=True)
            if item["source"] is None:
                dest.write_text(item["content"], encoding="utf-8")
            else:
                with dest.open("xb") as output:
                    source_snapshot(item["source"], source_root, timeout_s=timeout_s, output=output)
            info = file_digests(dest)
            require(info == {k: item[k] for k in ("size", "sha256", "md5")},
                    "source_changed_since_plan", 4)
            measured[prefix + item["path"]] = info
            if item["source"] is not None and item["path"].startswith("receipts/"):
                with dest.open("rb") as stream:
                    evidence[item["path"]] = stream.read(evidence_remaining + 1)
                evidence_remaining -= len(evidence[item["path"]])
                require(evidence_remaining >= 0, "metadata_budget_exceeded", 6)
        prov = decode((target / prefix / "provenance.json").read_bytes())
        keys = ("inputs", "model", "gates", "absence_reasons", "licence", "commercial_use", "jurisdiction")
        validate_provenance({key: prov[key] for key in keys if key in prov}, asset["final_verdict"], evidence,
                            mesh_sha256=measured[prefix + "asset.glb"]["sha256"])
    return measured


def preflight(plan, inventory):
    root = plan["destination_root"]
    for asset in plan["assets"]:
        prefix = asset["destination"][len(root):]
        wanted = {f["destination"][len(root):]: f for f in asset["files"]}
        require(not any(n.startswith(prefix) and n not in wanted for n in inventory), "version_has_unplanned_files", 5)
        for name, info in wanted.items():
            if name in inventory:
                require(all(info[k] == inventory[name][k] for k in ("size", "md5")), "immutable_conflict", 5)


def publication_events(client, inventory):
    """Read the grow-only set. A filename authenticates the exact canonical bytes."""
    events = []
    require(sum(info["size"] for name, info in inventory.items()
                if name.startswith("_estado/events/")) <= MAX_JSON, "event_budget_exceeded", 6)
    for name in sorted(inventory):
        if not name.startswith("_estado/events/"):
            continue
        digest = name[len("_estado/events/"):].removesuffix(".json")
        require(_HEX.fullmatch(digest) and name.endswith(".json"), "event_name_invalid", 5)
        raw = client.read(name, inventory)
        require(raw is not None, "event_witness_missing", 3)
        require(hashlib.sha256(raw).hexdigest() == digest, "event_digest_mismatch", 5)
        event = decode(raw)
        contract("event", event)
        require(encoded(event) == raw and _HEX.fullmatch(event["plan_sha256"]),
                "event_witness_invalid", 5)
        require(event["nuevos"] >= 0 and event["bytes"] >= 0, "event_witness_invalid", 5)
        require(root_path(event["destino"]) == event["destino"], "event_witness_invalid", 5)
        for entry in event["assets"]:
            prefix = asset_prefix(entry)
            require(entry["destination"] == event["destino"] + prefix and
                    entry["complete"] and entry["verdict"] == entry["final_verdict"],
                    "event_asset_invalid", 5)
            names = set()
            for item in entry["files"]:
                name = relative(item["path"])
                observed = item["remote_evidence"]
                require(name not in names and _HEX.fullmatch(item["local_sha256"]) and
                        re.fullmatch("[0-9a-f]{32}", item["local_md5"]) and item["local_size"] >= 0 and
                        observed == {"size": item["local_size"], "md5": item["local_md5"]},
                        "event_asset_invalid", 5)
                names.add(name)
            require("provenance.json" in names and entry["provenance_sha256"] == next(
                f["local_sha256"] for f in entry["files"] if f["path"] == "provenance.json"),
                "event_asset_invalid", 5)
        events.append((digest, event))
    return events


def publication_views(client, inventory):
    """Merge by immutable identities; never read a mutable view as authority.

    Repeated attempts of one logical plan may leave several immutable witnesses.
    Keep every witness, selecting a deterministic representative for the log.
    Asset identity conflicts refuse rather than choose a last writer.
    """
    logical, assets = {}, {}
    for digest, event in publication_events(client, inventory):
        key = event["plan_sha256"]
        rank = (event["fecha"], digest)
        if key not in logical or rank < logical[key][0]:
            logical[key] = (rank, event)
        for entry in event["assets"]:
            prefix = asset_prefix(entry)
            # Catalogue/import hashes are expected local-upload identities, not
            # remote SHA-256 observations. Restore independently measures bytes.
            files = [{"path": item["path"], "sha256": item["local_sha256"],
                      "size": item["local_size"], "md5": item["local_md5"]}
                     for item in entry["files"]]
            normalized = dict(entry, destination=client.root + prefix, files=files)
            if prefix in assets:
                require(assets[prefix] == normalized, "event_asset_conflict", 5)
            assets[prefix] = normalized
    rows = [event for _, event in sorted(logical.values(), key=lambda row: row[0])]
    log = b"".join(encoded(row) for row in rows)
    catalogue = {"schema": "af.publish-catalog/v1", "destination_root": client.root,
                 "assets": [assets[key] for key in sorted(assets)]}
    return log, catalogue


def remote_catalogue(client, inventory):
    return publication_views(client, inventory)[1]


def event_assets(plan, local, remote):
    """Keep measured upload identity and remote listing evidence disjoint."""
    entries = []
    for asset in plan["assets"]:
        files = []
        for item in sorted(asset["files"], key=lambda f: f["path"]):
            name = asset_prefix(asset) + item["path"]
            uploaded, observed = local[name], remote[name]
            require(uploaded == {k: item[k] for k in ("size", "sha256", "md5")},
                    "source_changed_since_plan", 4)
            require(set(observed) == {"size", "md5"} and
                    all(uploaded[k] == observed[k] for k in ("size", "md5")),
                    "uploaded_hash_mismatch", 5)
            files.append({"path": item["path"], "local_sha256": uploaded["sha256"],
                          "local_size": uploaded["size"], "local_md5": uploaded["md5"],
                          "remote_evidence": dict(observed)})
        entries.append({**{k: asset[k] for k in
                           ("asset_class", "asset_id", "version", "final_verdict", "destination")},
                        "verdict": asset["final_verdict"], "complete": True, "files": files,
                        "provenance_sha256": next(f["local_sha256"] for f in files
                                                  if f["path"] == "provenance.json")})
    return sorted(entries, key=lambda entry: entry["destination"])


def update_metadata(client, plan, scratch, before, local, remote):
    """Commit an immutable event; cache replacement cannot lose authoritative history."""
    # Preserve malformed legacy diagnostics for investigation; never overwrite them.
    old_log = client.read("_estado/log.jsonl", before) or b""
    require(not old_log or old_log.endswith(b"\n"), "remote_log_truncated", 5)
    plan_sha = core.sha256_json(plan)
    events = publication_events(client, client.inventory())
    existing = [event for _, event in events if event["plan_sha256"] == plan_sha]
    entries = event_assets(plan, local, remote)
    require(all(event["assets"] == entries and event["destino"] == client.root
                for event in existing), "event_log_conflict", 5)
    if not existing:
        event = {"schema": "af.publish-event/v1",
                 "fecha": datetime.now(timezone.utc).isoformat(timespec="microseconds"),
                 "evento": "asset-publish", "plan_sha256": plan_sha, "destino": client.root,
                 "nuevos": sum(f["destination"][len(client.root):] not in before
                               for a in plan["assets"] for f in a["files"]),
                 "modificados": 0, "borrados": 0, "bytes": plan["total_bytes"], "rc": 0,
                 "assets": entries}
        contract("event", event)
        raw = encoded(event)
        name = "_estado/events/" + hashlib.sha256(raw).hexdigest() + ".json"
        directory = scratch / "event"
        path = directory / name
        path.parent.mkdir(parents=True)
        path.write_bytes(raw)
        client.copy(directory)
        client.check(directory, client.root)
        committed = client.inventory()
        require(client.read(name, committed) == raw, "event_verification_failed", 5)
    inventory = client.inventory()
    log, catalogue = publication_views(client, inventory)
    require(any(event["plan_sha256"] == plan_sha for _, event in
                publication_events(client, inventory)), "event_witness_missing", 3)
    catalogue_raw = encoded(catalogue)
    snapshot = scratch / "catalog-snapshot" / "_index" / "snapshots"
    snapshot.mkdir(parents=True)
    (snapshot / (hashlib.sha256(catalogue_raw).hexdigest() + ".json")).write_bytes(catalogue_raw)
    client.copy(scratch / "catalog-snapshot")
    # Compatibility diagnostics only. Concurrent writers may replace these with
    # older views. Every reader above rebuilds from the event set, including
    # after interrupted writes.
    views = []
    for filename, raw, folder in (("log.jsonl", log, "_estado"),
                                  ("catalog.json", catalogue_raw, "_index")):
        directory = scratch / ("view-" + folder)
        directory.mkdir()
        (directory / filename).write_bytes(raw)
        destination = client.root + folder + "/"
        client.copy(directory, destination, mutable=True)
        views.append((directory, destination))
    client.check(scratch / "catalog-snapshot", client.root)
    for directory, destination in views:
        client.check(directory, destination)
    return hashlib.sha256(log).hexdigest(), hashlib.sha256(catalogue_raw).hexdigest()


def publish(plan, *, config=None, approval=None, session=None):
    config = configuration(config)
    started = time.monotonic_ns()
    client = LocalPublication(config["destination_root"], config["timeout_s"])
    receipt = {"schema": "af.publish-receipt/v1", "rc": 0, "transport_verdict": "PASS",
               "asset_verdict": None, "reason_codes": [],
               "plan_sha256": None, "destination_root": client.root, "files_verified": 0,
               "log_sha256": None, "catalog_sha256": None, "asset_verdicts": [], "commands": client.commands,
               "wall_ms": 0}
    try:
        validate_plan(plan, config)
        receipt["plan_sha256"] = core.sha256_json(plan)
        authorise(plan, config, approval)
        namespace_access(config, session)
        require(config.get("source_root"), "source_root_required", 3)
        with destination_lock(client.root, config["busy_attempts"]), tempfile.TemporaryDirectory(prefix="af-publish-") as tmp:
            scratch = Path(tmp)
            staging = scratch / "assets"
            local = stage(plan, staging, config["source_root"], timeout_s=config["timeout_s"])
            with client.open(create=True):
                before = client.inventory()
                preflight(plan, before)
                # Existing immutable objects are not upload evidence: even matching
                # size/MD5 can hide different bytes. Refuse before writing anything.
                for name, info in local.items():
                    if name in before:
                        require(client.measure(name, before) == info, "immutable_conflict", 5)
                client.copy(staging)
                client.check(staging, client.root)
                after = client.inventory()
                for asset in plan["assets"]:
                    for item in asset["files"]:
                        name = item["destination"][len(client.root):]
                        require(name in after and all(after[name][k] == item[k] for k in ("size", "md5")),
                                "uploaded_hash_mismatch", 5)
                        require(client.measure(name, after) == local[name], "uploaded_hash_mismatch", 5)
                receipt["files_verified"] = plan["file_count"]
                # Read current remote metadata after data verification, while retaining
                # the initial inventory for measured new-file counts.
                receipt["log_sha256"], receipt["catalog_sha256"] = update_metadata(
                    client, plan, scratch, before, local, after)
                receipt["asset_verdicts"] = [{"destination": a["destination"], "verdict": a["final_verdict"]}
                                             for a in plan["assets"]]
                receipt["asset_verdict"] = _combined_asset_verdict(
                    a["final_verdict"] for a in plan["assets"])
    except PublishError as exc:
        receipt.update(rc=exc.rc, transport_verdict=error_verdict(exc),
                       reason_codes=[exc.reason])
    except (OSError, ValueError, KeyError, TypeError, core.ContractError) as exc:
        receipt.update(rc=3, transport_verdict="CANNOT_SEE", reason_codes=["io_or_contract_error"])
    receipt["wall_ms"] = (time.monotonic_ns() - started) // 1000000
    contract("receipt", receipt)
    return receipt


def _combined_asset_verdict(verdicts):
    """Summarize verified assets using the factory's verdict precedence."""
    present = set(verdicts)
    if not present.issubset(VERDICTS):
        return None
    return next((verdict for verdict in ("ERROR", "FAIL", "CANNOT_SEE", "PASS")
                 if verdict in present), None)


def read_receipt(path):
    """Read either receipt generation without treating legacy transport as asset evidence."""
    value = read_json(path)
    require(type(value) is dict, "invalid_receipt")
    schema = value.get("schema")
    require(type(schema) is str, "invalid_receipt_schema")
    kind = {"af.publish-receipt/v1": "receipt", "af.import-receipt/v1": "import_receipt"}.get(schema)
    require(kind is not None, "invalid_receipt_schema")
    contract(kind, value)
    if "verdict" in value:
        require("transport_verdict" not in value and "asset_verdict" not in value,
                "ambiguous_receipt_verdict")
        value["transport_verdict"] = value.pop("verdict")
        value["asset_verdict"] = None
        if value["rc"] == 0:
            value["asset_verdict"] = _combined_asset_verdict(
                entry["verdict"] for entry in value.get("asset_verdicts", []))
    return value


def verdict_text(verdict):
    return (verdict + "\nReview only; not an approved production asset.\n").encode()


def error_verdict(exc):
    if exc.verdict is not None:
        return exc.verdict
    return core.VERDICT[exc.rc]


def doctor(config, *, session=None):
    config = configuration(config)
    namespace_access(config, session)
    client = LocalPublication(config["destination_root"], config["timeout_s"])
    with client.open():
        client.inventory()
    return {"rc": 0, "verdict": "PASS", "destination_root": client.root,
            "checks": ["local_root_verified"], "commands": client.commands}


def local_store(path):
    path = Path(path)
    enforce_hygiene(path.parts)
    require(path.anchor == "/" and path != Path("/") and ".." not in path.parts
            and not _CONTROL.search(str(path)), "invalid_local_store")
    require(not any(p.is_symlink() for p in (path, *path.parents)), "local_store_symlink")
    return path


def read_catalogue(client):
    inventory = client.inventory()
    _, cat = publication_views(client, inventory)
    require(bool(cat["assets"]), "catalogue_missing", 3)
    raw = encoded(cat)
    contract("catalogue", cat)
    require(cat["destination_root"] == client.root, "catalogue_destination_mismatch")
    return raw, cat, inventory


def plan_import(*, config, store, asset_paths=None, session=None):
    """Read-only remote plan. No download or local directory creation."""
    config = configuration(config)
    namespace_access(config, session)
    store = local_store(store)
    client = LocalPublication(config["destination_root"], config["timeout_s"])
    with client.open():
        raw, catalogue, _ = read_catalogue(client)
    return import_plan_from_catalogue(config, store, client, raw, catalogue, asset_paths)


def import_plan_from_catalogue(config, store, client, raw, catalogue, asset_paths):
    wanted = None if asset_paths is None else set(asset_paths)
    if wanted is not None:
        require(len(wanted) == len(asset_paths) and bool(wanted), "invalid_asset_selection")
        for path in wanted:
            relative(path.rstrip("/"))
    assets, seen = [], set()
    for entry in catalogue["assets"]:
        identity = {k: entry[k] for k in ("asset_class", "asset_id", "version")}
        identity["final_verdict"] = entry["final_verdict"]
        prefix = asset_prefix(identity)
        require(entry["destination"] == client.root + prefix and prefix not in seen,
                "catalogue_routing_invalid")
        seen.add(prefix)
        if wanted is not None and prefix.rstrip("/") not in {p.rstrip("/") for p in wanted}:
            continue
        require(entry["complete"], "catalogue_asset_incomplete", 3)
        assets.append({**identity, "source": client.root + prefix,
                       "destination": str(store / prefix), "files": entry["files"]})
    require(bool(assets) and (wanted is None or len(assets) == len(wanted)), "catalogue_asset_missing", 3)
    plan = {"schema": "af.import-plan/v1", "dry_run": True, "source_root": client.root,
            "catalogue_sha256": hashlib.sha256(raw).hexdigest(), "local_store": str(store),
            "assets": sorted(assets, key=lambda a: a["source"]),
            "file_count": sum(len(a["files"]) for a in assets),
            "total_bytes": sum(f["size"] for a in assets for f in a["files"])}
    validate_import_plan(plan, config)
    return plan


def validate_import_plan(plan, config):
    contract("import_plan", plan)
    require(plan["source_root"] == config["destination_root"], "import_source_changed")
    store = local_store(plan["local_store"])
    require(_HEX.fullmatch(plan["catalogue_sha256"]), "invalid_catalogue_digest")
    seen, count, total = set(), 0, 0
    for asset in plan["assets"]:
        prefix = asset_prefix(asset)
        require(prefix not in seen and asset["source"] == config["destination_root"] + prefix and
                asset["destination"] == str(store / prefix), "import_routing_invalid")
        seen.add(prefix)
        names = set()
        for item in asset["files"]:
            name = relative(item["path"])
            require(name not in names, "duplicate_import_file")
            names.add(name)
            if name not in ("provenance.json", asset["final_verdict"] + ".VERDICT.txt"):
                artifact_path(name)
            require(item["size"] >= 0 and _HEX.fullmatch(item["sha256"]) and
                    re.fullmatch("[0-9a-f]{32}", item["md5"]), "invalid_import_digest")
            count += 1
            total += item["size"]
        require({"asset.glb", "provenance.json"} <= names and any(n.startswith("receipts/") for n in names),
                "import_evidence_missing", 3)
        if asset["final_verdict"] != "PASS":
            require(asset["final_verdict"] + ".VERDICT.txt" in names, "review_verdict_missing")
    require(0 < count <= MAX_FILES and count == plan["file_count"] and total == plan["total_bytes"],
            "import_totals_mismatch")


def verify_tree(storage, directory_fd, asset):
    def capture(name):
        return name == "provenance.json" or name.startswith("receipts/") or name.endswith(".VERDICT.txt")

    expected = {item["path"] for item in asset["files"]}
    files = storage.read_tree(directory_fd, capture_limit=MAX_JSON, capture_name=capture, expected=expected)
    require(set(files) == expected, "import_file_coverage", 3)
    for item in asset["files"]:
        require(files[item["path"]][0] == {k: item[k] for k in ("size", "sha256", "md5")},
                "import_hash_mismatch", 5)
    prov = decode(files["provenance.json"][1])
    require(prov.get("schema") == "af.publish-provenance/v1" and all(
        prov.get(k) == asset[k] for k in ("asset_class", "asset_id", "version", "final_verdict")),
        "import_provenance_mismatch", 5)
    expected_manifest = [f for f in sorted(asset["files"], key=lambda f: f["path"])
                         if f["path"] not in ("provenance.json", asset["final_verdict"] + ".VERDICT.txt")]
    require(prov.get("files") == expected_manifest, "import_provenance_manifest_mismatch", 5)
    keys = ("inputs", "model", "gates", "absence_reasons", "licence", "commercial_use", "jurisdiction")
    evidence = {name: raw for name, (_, raw) in files.items()
                if name == "asset.glb" or name.startswith("receipts/")}
    validate_provenance({key: prov[key] for key in keys if key in prov}, asset["final_verdict"], evidence,
                        mesh_sha256=files["asset.glb"][0]["sha256"])
    if asset["final_verdict"] != "PASS":
        require(files[asset["final_verdict"] + ".VERDICT.txt"][1] ==
                verdict_text(asset["final_verdict"]), "import_verdict_mismatch", 5)
    return len(files)


@contextmanager
def import_stage(root_fd):
    name = ".af-import-" + secrets.token_hex(16)
    os.mkdir(name, mode=0o700, dir_fd=root_fd)
    fd = child_directory(root_fd, name)
    completed = False
    try:
        yield name, fd
        completed = True
    finally:
        try:
            bound_directory(root_fd, name, fd)
        except (OSError, LocalPathRefusal) as exc:
            # Preserve the detached staging tree and the unfamiliar replacement.
            if completed:
                raise PublishError("import_stage_changed", 5) from exc
        else:
            for child in os.listdir(fd):
                remove_tree(fd, child)
            bound_directory(root_fd, name, fd)
            os.rmdir(name, dir_fd=root_fd)
            os.fsync(root_fd)
        finally:
            os.close(fd)


def assert_stage(root_fd, stage_name, stage_fd):
    try:
        bound_directory(root_fd, stage_name, stage_fd)
    except (OSError, LocalPathRefusal) as exc:
        raise PublishError("import_stage_changed", 5) from exc


def commit_import(storage, root_fd, stage_name, stage_fd, name, prefix, asset, identity):
    target = Path(prefix)
    with storage.directory(stage_fd, name) as source:
        require(directory_identity(os.fstat(source)) == identity, "import_stage_changed", 5)
        verify_tree(storage, source, asset)
        storage.assert_root(root_fd)
        assert_stage(root_fd, stage_name, stage_fd)
        bound_directory(stage_fd, name, source)
        with storage.directory(root_fd, target.parent, create=True) as target_parent:
            assert_stage(root_fd, stage_name, stage_fd)
            try:
                rename_noreplace(stage_fd, name, target_parent, target.name)
            except FileExistsError as exc:
                raise PublishError("local_asset_conflict", 5) from exc
            except NotImplementedError as exc:
                raise PublishError("atomic_import_unavailable", 3) from exc
            try:
                with storage.directory(root_fd, target.parent) as current:
                    require(directory_identity(os.fstat(current)) == directory_identity(os.fstat(target_parent)),
                            "local_destination_unsafe", 5)
                    bound_directory(current, target.name, source)
                    count = verify_tree(storage, source, asset)
                    bound_directory(current, target.name, source)
                storage.assert_root(root_fd)
                assert_stage(root_fd, stage_name, stage_fd)
                os.fsync(target_parent)
                os.fsync(stage_fd)
            except (OSError, LocalPathRefusal, LocalBudgetExceeded, PublishError):
                # Only roll back the inode we staged. Preserve unknown entries
                # if the recovery identity can no longer be proven.
                try:
                    bound_directory(target_parent, target.name, source)
                    rename_noreplace(target_parent, target.name, stage_fd, name)
                    os.fsync(target_parent)
                    os.fsync(stage_fd)
                except (OSError, LocalPathRefusal) as exc:
                    raise PublishError("import_commit_unproven", 5) from exc
                raise
            return count

def restore_files(plan, client, store, receipt, timeout_s):
    storage = LocalStore(store, timeout_s, MAX_FILES)
    root_fd = storage._directory(store, create=True)
    try:
        with import_stage(root_fd) as (stage_name, stage_fd):
            staged, existing_count = [], 0
            for index, asset in enumerate(plan["assets"]):
                prefix = asset_prefix(asset).rstrip("/")
                opened_existing = False
                try:
                    with storage.directory(root_fd, prefix) as existing_fd:
                        opened_existing = True
                        count = verify_tree(storage, existing_fd, asset)
                        with storage.directory(root_fd, prefix) as current:
                            require(directory_identity(os.fstat(current)) ==
                                    directory_identity(os.fstat(existing_fd)), "local_asset_conflict", 5)
                except FileNotFoundError as exc:
                    if opened_existing:
                        raise PublishError("local_asset_conflict", 5) from exc
                except (LocalPathRefusal, PublishError) as exc:
                    raise PublishError("local_asset_conflict", 5) from exc
                else:
                    existing_count += count
                    continue
                name = str(index)
                with storage.directory(stage_fd, name, create=True) as directory_fd:
                    client.download(asset["source"], storage, directory_fd)
                    verify_tree(storage, directory_fd, asset)
                    identity = directory_identity(os.fstat(directory_fd))
                staged.append((name, prefix, asset, identity))
            # Refuse a changed staging parent before creating any final asset.
            assert_stage(root_fd, stage_name, stage_fd)
            count = existing_count
            for name, prefix, asset, identity in staged:
                count += commit_import(storage, root_fd, stage_name, stage_fd, name, prefix, asset, identity)
                receipt["assets_restored"].append(asset["destination"])
            storage.assert_root(root_fd)
        receipt["files_verified"] = count
    finally:
        os.close(root_fd)


def restore(plan, *, config, session=None):
    receipt = {"schema": "af.import-receipt/v1", "rc": 0, "transport_verdict": "PASS",
               "asset_verdict": None, "reason_codes": [],
               "plan_sha256": core.sha256_json(plan), "files_verified": 0, "assets_restored": [],
               "commands": []}
    try:
        config = configuration(config)
        namespace_access(config, session)
        validate_import_plan(plan, config)
        client = LocalPublication(config["destination_root"], config["timeout_s"])
        store = local_store(plan["local_store"])
        with destination_lock("import:" + str(store), config["busy_attempts"]), client.open():
            raw, catalogue, _ = read_catalogue(client)
            require(hashlib.sha256(raw).hexdigest() == plan["catalogue_sha256"], "import_catalogue_stale", 4)
            fresh = import_plan_from_catalogue(
                config, store, client, raw, catalogue, [asset_prefix(a).rstrip("/") for a in plan["assets"]])
            require(fresh == plan, "import_plan_drift", 4)
            with local_io("local_destination_unsafe", budget_reason="import_budget_exceeded"):
                restore_files(plan, client, store, receipt, config["timeout_s"])
        receipt["asset_verdict"] = _combined_asset_verdict(a["final_verdict"] for a in plan["assets"])
    except PublishError as exc:
        receipt.update(rc=exc.rc, transport_verdict=error_verdict(exc), reason_codes=[exc.reason], files_verified=0)
    except (OSError, ValueError, KeyError, TypeError, core.ContractError):
        receipt.update(rc=3, transport_verdict="CANNOT_SEE", reason_codes=["import_io_or_contract_error"], files_verified=0)
    contract("import_receipt", receipt)
    return receipt


class Parser(argparse.ArgumentParser):
    def error(self, _message):
        raise PublishError("usage_error")


def main(argv=None):
    parser = Parser(description=__doc__)
    parser.add_argument("command", nargs="?", choices=("connect", "doctor", "export", "import"))
    parser.add_argument("--config", type=Path)
    parser.add_argument("--remote")
    parser.add_argument("--root")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--where", action="store_true")
    group.add_argument("--plan", type=Path, nargs="?", const=True, metavar="SOURCE_JSON")
    group.add_argument("--upload", type=Path, metavar="PLAN_JSON")
    group.add_argument("--restore", type=Path, metavar="PLAN_JSON")
    parser.add_argument("--store", type=Path)
    parser.add_argument("--asset", action="append")
    parser.add_argument("--out", type=Path)
    parser.add_argument("--approval", type=Path)
    parser.add_argument("--receipt", type=Path)
    try:
        args = parser.parse_args(argv)
        require(args.command != "connect" and args.remote is None and args.root is None,
                "remote_disabled", 5)
        config = load_configuration(args.config)
        if args.command == "doctor":
            require(not any((args.where, args.plan, args.upload, args.restore, args.approval, args.receipt)), "usage_error")
            value = doctor(config)
        elif args.where:
            require(not any((args.out, args.approval, args.receipt)), "usage_error")
            value = {"destination_root": config["destination_root"], "verb": "local immutable copy",
                     "network": False, "standing_approval": config["standing_approval"],
                     "metadata_policy": METADATA_POLICY}
        elif args.command == "import":
            require(not args.upload and not args.approval, "usage_error")
            if args.plan is True:
                require(args.store is not None and args.out is not None and not args.receipt, "usage_error")
                plan = plan_import(config=config, store=args.store, asset_paths=args.asset)
                core.atomic_write_json(args.out, plan)
                value = {"rc": 0, "plan": str(args.out), "plan_sha256": core.sha256_json(plan), "read_only": True}
            else:
                require(args.restore is not None and args.receipt is not None and not any((args.out, args.store, args.asset)), "usage_error")
                value = restore(read_json(args.restore), config=config)
                if value["rc"] == 0:
                    core.atomic_write_json(args.receipt, value)
        elif args.plan is not None:
            require(isinstance(args.plan, Path), "source_json_required")
            require(args.out is not None and not any((args.approval, args.receipt)), "usage_error")
            value = build_plan(read_json(args.plan), config)
            core.atomic_write_json(args.out, value)
            value = {"rc": 0, "plan": str(args.out), "plan_sha256": core.sha256_json(value),
                     "destination_root": config["destination_root"], "network": False}
        else:
            require(args.upload is not None and args.receipt is not None and args.out is None
                    and not any((args.restore, args.store, args.asset)), "usage_error")
            value = publish(read_json(args.upload), config=config,
                            approval=read_json(args.approval) if args.approval else None)
            # Refusals go to stdout and preserve caller receipt bytes.
            if value["rc"] == 0:
                core.atomic_write_json(args.receipt, value)
        print(core.canonical_json(value), end="")
        return value.get("rc", 0)
    except (PublishError, core.ContractError, OSError) as exc:
        rc = exc.rc if isinstance(exc, PublishError) else 2
        value = {"rc": rc, "verdict": error_verdict(exc) if isinstance(exc, PublishError) else "ERROR",
                 "reason_codes": [exc.reason if isinstance(exc, PublishError) else "input_error"]}
        print(core.canonical_json(value), end="")
        return rc


if __name__ == "__main__":
    raise SystemExit(main())
