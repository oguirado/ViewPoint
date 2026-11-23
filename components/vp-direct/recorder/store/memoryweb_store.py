"""Offline storage for immutable MemoryWeb workflow revisions and public receipts.

The caller provisions a trusted store root. Revisions live under each site's
``revisions/<workflow_sha256>/``. The public journal uses MemoryWeb's
``sites/<site>/runs/receipts.jsonl`` layout and domain-separated receipt hashes.
Receipt pins select committed records by that hash. Schema and digest checks
establish integrity. The caller owns review, authorization, and execution.
"""

from contextlib import contextmanager
import errno
import fcntl
import hashlib
import os
from pathlib import Path
import re
import runpy
import stat
from uuid import uuid4


_REPO = Path(__file__).resolve().parents[4]
_SCHEMA = _REPO / "memoryweb-builtin/standards/schemas/mw.v1.schema.json"
_GATE = runpy.run_path(str(_REPO / "components/wender-agent/tools/memoryweb_gate.py"))
_canon = _GATE["canon"]
_strict_load = _GATE["strict_load"]
_conforms = _GATE["conforms"]
_TRUST = "signature_unverified"
_RECEIPT_DOMAIN = b"mw.publication_receipt.v1\0"
_JOURNAL_LIMIT = 65536
_WORKFLOW_LIMIT = 65536
_SCRIPT_LIMIT = 32768
_DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_SITE_OR_WORKFLOW = re.compile(r"[a-z0-9][a-z0-9-]{0,62}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_SCRIPT_REF = re.compile(r"scripts/[A-Za-z0-9_-]+\.py")
_CAPS = b"# MW-CAPS: "


class StoreError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def canonical_bytes(value):
    return (_canon(value) + "\n").encode("utf-8")


def _load_canonical(raw):
    try:
        value = _strict_load(raw)
    except (ValueError, RecursionError) as exc:
        raise StoreError(getattr(exc, "category", "JSON_INVALID")) from None
    if canonical_bytes(value) != raw:
        raise StoreError("JSON_NONCANONICAL")
    return value


def _sha256(raw):
    return hashlib.sha256(raw).hexdigest()


def _require_digest(raw, expected):
    if _sha256(raw) != expected:
        raise StoreError("DIGEST_MISMATCH")


def _bounded_bytes(raw, limit, oversized):
    if not isinstance(raw, bytes):
        raise StoreError("BYTES_REQUIRED")
    if len(raw) > limit:
        raise StoreError(oversized)


def _identifier(value, pattern):
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise StoreError("INVALID_IDENTIFIER")
    return value


@contextmanager
def _directory(parent, parts, *, create=False):
    fd = os.dup(parent)
    try:
        for part in parts:
            if create:
                try:
                    os.mkdir(part, mode=0o700, dir_fd=fd)
                except FileExistsError:
                    pass
                else:
                    os.fsync(fd)
            child = os.open(part, _DIRECTORY, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR):
            raise StoreError("CONTAINMENT") from None
        raise
    finally:
        os.close(fd)


def _read(fd, name, limit, oversized="ARTIFACT_OVERSIZE"):
    try:
        source = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise StoreError("CONTAINMENT") from None
        raise
    with os.fdopen(source, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise StoreError("FILE_TYPE")
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise StoreError(oversized)
    return raw


def _publish(fd, name, raw, *, replace=False):
    temp = ".pending-" + uuid4().hex
    new = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=fd)
    try:
        with os.fdopen(new, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        if replace:
            os.replace(temp, name, src_dir_fd=fd, dst_dir_fd=fd)
        else:
            try:
                os.link(temp, name, src_dir_fd=fd, dst_dir_fd=fd)
            except FileExistsError:
                if _read(fd, name, len(raw)) != raw:
                    raise StoreError("REVISION_CONFLICT") from None
    finally:
        try:
            os.unlink(temp, dir_fd=fd)
        except FileNotFoundError:
            pass
    os.fsync(fd)


class MemoryWebStore:
    def __init__(self, root):
        self._root = Path(root).resolve(strict=True)
        bundle = _SCHEMA.read_bytes()
        pins = _load_canonical(_SCHEMA.with_name("PINS.json").read_bytes())
        if _sha256(bundle) != pins["bundle_sha256"]:
            raise StoreError("SCHEMA_PIN_MISMATCH")
        self._defs = _GATE["compile_bundle"](_strict_load(bundle))

    @contextmanager
    def _site(self, site_id, *, write=False):
        _identifier(site_id, _SITE_OR_WORKFLOW)
        root = os.open(self._root, _DIRECTORY)
        try:
            with _directory(root, ("sites", site_id), create=write) as site:
                if write:
                    fcntl.flock(site, fcntl.LOCK_EX)
                yield site
        except FileNotFoundError:
            raise StoreError("NOT_FOUND") from None
        finally:
            os.close(root)

    def _validate(self, value, schema, code):
        if not _conforms(value, self._defs[schema], self._defs):
            raise StoreError(code)

    def _script_name(self, workflow):
        ref, pin = workflow["script_ref"], workflow["script_sha256"]
        if (ref is None) != (pin is None):
            raise StoreError("SCRIPT_PAIR")
        if ref is None:
            return None
        if not _SCRIPT_REF.fullmatch(ref):
            raise StoreError("SCRIPT_PATH")
        return ref.split("/")[1]

    def _check_script(self, workflow, raw):
        if not isinstance(raw, bytes):
            raise StoreError("SCRIPT_REQUIRED")
        if len(raw) > _SCRIPT_LIMIT:
            raise StoreError("SCRIPT_TOO_LARGE")
        _require_digest(raw, workflow["script_sha256"])
        try:
            raw.decode("utf-8")
        except UnicodeDecodeError:
            raise StoreError("SCRIPT_UTF8") from None
        if b"\r" in raw or b"\0" in raw:
            raise StoreError("SCRIPT_TEXT")
        lines = raw.split(b"\n")
        header = 1 if lines[0].startswith(b"#!") else 0
        if header >= len(lines) or not lines[header].startswith(_CAPS):
            raise StoreError("SCRIPT_CAPS_MISSING")
        if header == len(lines) - 1:
            raise StoreError("SCRIPT_CAPS_NO_LF")
        if sum(bool(re.match(rb"\s*#\s*MW-CAPS:", line)) for line in lines) != 1:
            raise StoreError("SCRIPT_CAPS_DUPLICATE")
        caps = _load_canonical(lines[header][len(_CAPS):] + b"\n")
        self._validate(caps, "caps", "SCRIPT_CAPS_SCHEMA")
        if not set(caps["engines"]) <= {entry["engine"] for entry in workflow["engine_compat"]}:
            raise StoreError("SCRIPT_ENGINE_UNDECLARED")
        if (caps["surface_access"] == "act_bounded" and
                not workflow["execution_policy"]["requires_operator_go_ahead_on_write"]):
            raise StoreError("SCRIPT_ACT_UNGATED")

    def save_workflow(self, raw, *, script=None):
        """Store canonical workflow bytes and, when declared, the pinned script bytes."""
        _bounded_bytes(raw, _WORKFLOW_LIMIT, "ARTIFACT_OVERSIZE")
        workflow = _load_canonical(raw)
        self._validate(workflow, "workflow", "WORKFLOW_SCHEMA")
        workflow_id = _identifier(workflow["id"], _SITE_OR_WORKFLOW)
        script_name = self._script_name(workflow)
        if script_name:
            self._check_script(workflow, script)
        elif script is not None:
            raise StoreError("SCRIPT_UNDECLARED")
        digest = _sha256(raw)
        with self._site(workflow["site_id"], write=True) as site:
            with _directory(site, ("revisions", digest), create=True) as revision:
                if script_name:
                    with _directory(revision, ("scripts",), create=True) as scripts:
                        _publish(scripts, script_name, script)
                _publish(revision, workflow_id + ".json", raw)
        return {"workflow_sha256": digest, "trust": _TRUST}

    def _load_workflow(self, site, site_id, workflow_id, digest):
        _identifier(workflow_id, _SITE_OR_WORKFLOW)
        _identifier(digest, _SHA256)
        with _directory(site, ("revisions", digest)) as revision:
            raw = _read(revision, workflow_id + ".json", _WORKFLOW_LIMIT)
            workflow = _load_canonical(raw)
            _require_digest(raw, digest)
            self._validate(workflow, "workflow", "WORKFLOW_SCHEMA")
            if (workflow["site_id"], workflow["id"]) != (site_id, workflow_id):
                raise StoreError("WORKFLOW_IDENTITY")
            script = None
            script_name = self._script_name(workflow)
            if script_name:
                with _directory(revision, ("scripts",)) as scripts:
                    script = _read(scripts, script_name, _SCRIPT_LIMIT, "SCRIPT_TOO_LARGE")
                self._check_script(workflow, script)
        return {"workflow": workflow, "script": script, "workflow_sha256": digest, "trust": _TRUST}

    def load_workflow(self, site_id, workflow_id, workflow_sha256):
        with self._site(site_id) as site:
            return self._load_workflow(site, site_id, workflow_id, workflow_sha256)

    def _receipt_link(self, site_id, raw, seq, previous):
        record = _load_canonical(raw)
        self._validate(record, "publication_receipt", "JOURNAL_SCHEMA")
        if record["site_id"] != site_id:
            raise StoreError("JOURNAL_OWNER")
        if record["seq"] != seq:
            raise StoreError("JOURNAL_SEQ")
        if record["prev_receipt_sha256"] != previous:
            raise StoreError("JOURNAL_CHAIN")
        return _sha256(_RECEIPT_DOMAIN + raw), record

    def _journal(self, runs, site_id):
        try:
            raw = _read(runs, "receipts.jsonl", _JOURNAL_LIMIT, "JOURNAL_OVERSIZE")
        except FileNotFoundError:
            return b"", {}
        if raw and not raw.endswith(b"\n"):
            raise StoreError("JOURNAL_TORN")
        previous, records = None, {}
        for seq, body in enumerate(raw.split(b"\n")[:-1], 1):
            previous, record = self._receipt_link(site_id, body + b"\n", seq, previous)
            records[previous] = record
        return raw, records

    def append_publication_receipt(self, site_id, raw):
        """Append supplied canonical receipt bytes. Exact retries are idempotent."""
        _bounded_bytes(raw, _JOURNAL_LIMIT, "JOURNAL_FULL")
        with self._site(site_id, write=True) as site:
            with _directory(site, ("runs",), create=True) as runs:
                journal, records = self._journal(runs, site_id)
                digest = _sha256(_RECEIPT_DOMAIN + raw)
                existing = records.get(digest)
                if existing is None:
                    digest, record = self._receipt_link(site_id, raw, len(records) + 1, next(reversed(records), None))
                else:
                    record = existing
                self._load_workflow(site, site_id, record["workflow_id"], record["workflow_sha256"])
                if existing is None:
                    if len(journal) + len(raw) > _JOURNAL_LIMIT:
                        raise StoreError("JOURNAL_FULL")
                    _publish(runs, "receipts.jsonl", journal + raw, replace=True)
        return {"publication_receipt_sha256": digest, "trust": _TRUST}

    def load_pinned_workflow(self, site_id, publication_receipt_sha256):
        """Select a workflow through a verified public receipt in the site's journal."""
        _identifier(publication_receipt_sha256, _SHA256)
        with self._site(site_id) as site:
            with _directory(site, ("runs",)) as runs:
                _, records = self._journal(runs, site_id)
            record = records.get(publication_receipt_sha256)
            if record is None:
                raise StoreError("RECEIPT_NOT_FOUND")
            answer = self._load_workflow(site, site_id, record["workflow_id"], record["workflow_sha256"])
        return dict(answer, publication_receipt=record, publication_receipt_sha256=publication_receipt_sha256)
