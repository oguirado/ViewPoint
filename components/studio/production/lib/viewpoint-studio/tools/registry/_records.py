# SPDX-License-Identifier: AGPL-3.0-only
"""Canonical record identity and the local evidence closure."""

import json
import re

from asset_factory import math_acceptance

from ._disk import RegistryError, check_size

DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_FREE_TEXT = {"annotations", "labels", "absence_reasons"}
_RECORD_FIELDS = {
    "parent_records", "parent_asset_digest", "original_record_digest",
    "counterpart_record_digest", "record_digest",
}
_SET_KEYS = {
    "files": ("role", "logical_path", "digest"),
    "sockets": ("id",),
    "materials": ("id",),
    "maps": ("channel", "uv_set", "file_digest"),
    "lods": ("level",),
    "gates": ("gate_id", "receipt_digest"),
    "metrics": ("id",),
    "instances": ("instance_id",),
    "components": ("subject_digest", "component"),
}
_SET_STRINGS = {
    "allowed_peer_roles", "attributions", "dependencies", "parent_records",
    "required_check_ids", "semantic_labels", "territory_excluded",
}


def is_digest(value):
    return isinstance(value, str) and DIGEST.fullmatch(value) is not None


def _ordered(key, rows):
    if key in _SET_KEYS:
        values = [tuple(row[name] for name in _SET_KEYS[key]) for row in rows]
    elif key.endswith("_digests") or key in _SET_STRINGS:
        values = rows
    else:
        return
    if values != sorted(set(values)):
        raise RegistryError(2, "noncanonical_set:" + key)


def _fields(value, key=None):
    """Walk only fields of a BASE-validated record that can carry typed links."""
    if value is None or key in _FREE_TEXT:
        return
    if isinstance(value, dict):
        for name, child in value.items():
            if name != "dependencies":
                yield from _fields(child, name)
    elif isinstance(value, list):
        _ordered(key, value)
        for child in value:
            yield from _fields(child, key)
    elif key == "digest" or key.endswith(("_digest", "_digests")) or key == "parent_records":
        yield key, value


def _record_links(record):
    _ordered("dependencies", record["dependencies"])
    fields = list(_fields(record))
    related = {pin for key, pin in fields if key in _RECORD_FIELDS}
    assets = {pin for key, pin in fields if key == "parent_asset_digest"}
    required = {pin for _, pin in fields}
    required.update(value for key, value in record["representations"].items()
                    if key != "absence_reasons" and value is not None)
    return required, related, assets


def _verify(raw, digest, policy, trusted_read, get):
    checked = {}
    reader_refusal = None

    def read_checked(role, pin):
        nonlocal reader_refusal
        if reader_refusal is not None:
            return None
        try:
            approved = trusted_read(role, pin)
            if approved is None:
                return None
            _, stored = get(pin, load=True)
            if type(approved) is not bytes or approved != stored:
                raise RegistryError(4, "evidence_bytes_mismatch:" + role)
        except RegistryError as exc:
            # BASE normalizes reader exceptions. Preserve REG's storage verdict.
            reader_refusal = exc
            return None
        checked[role, pin] = stored
        return stored

    decision = math_acceptance.verify_record(
        raw, approved_scopes=policy(digest), read_checked=read_checked,
    )
    if reader_refusal is not None:
        raise reader_refusal
    if decision["rc"]:
        raise RegistryError(decision["rc"], "base:" + ",".join(decision["reasons"]))
    if decision["record_digest"] != digest:
        raise RegistryError(4, "base_record_digest_mismatch")
    required = {pin for _, pin in checked}
    for (role, _), stored in checked.items():
        if role == "profile":
            profile = json.loads(stored)
            required.update(profile["input_refs"])
            required.update(profile["reference_refs"])
            required.update((profile["domain_digest"], profile["toolchain_digest"]))
        elif role in ("certificate", "negative-control"):
            required.add(json.loads(stored)["witness_digest"])
    return json.loads(raw), required


def _files_check(record, sizes):
    paths = set()
    for file in record["files"]:
        path = file["logical_path"]
        if path in paths or any(piece in ("", ".", "..") for piece in path.split("/")):
            raise RegistryError(2, "invalid_logical_path")
        paths.add(path)
        if sizes[file["digest"]] != file["size_bytes"]:
            raise RegistryError(4, "file_size_mismatch:" + file["digest"])


def verify_closure(root, read, policy, trusted_read, max_objects=None, max_bytes=None):
    records, sizes, active, must_be_assets = {}, {}, set(), set()
    remaining = max_bytes

    def get(pin, *, load=False, record=False):
        nonlocal remaining
        known_size = sizes.get(pin)
        if known_size is not None:
            check_size(known_size, record=record, max_bytes=None)
            if not load:
                return known_size, None
            return read(pin, load=True, record=record, max_bytes=known_size)
        if max_objects is not None and len(sizes) >= max_objects:
            raise RegistryError(6, "object_budget")
        size, data = read(pin, load=load, record=record, max_bytes=remaining)
        sizes[pin] = size
        if remaining is not None:
            remaining -= size
        return size, data

    pending = [(root, False)]
    while pending:
        digest, expanded = pending.pop()
        if expanded:
            active.remove(digest)
            continue
        if digest in active:
            raise RegistryError(5, "record_cycle")
        if digest in records:
            continue
        _, raw = get(digest, load=True, record=True)
        record, checked = _verify(raw, digest, policy, trusted_read, get)
        required, related, assets = _record_links(record)
        declared = record["dependencies"]
        if not (required | checked).issubset(declared):
            raise RegistryError(3, "undeclared_dependency")
        if digest in declared:
            raise RegistryError(5, "record_cycle")
        for pin in declared:
            get(pin, record=pin in related)
        _files_check(record, sizes)
        records[digest] = record
        must_be_assets.update(assets)
        active.add(digest)
        pending.append((digest, True))
        pending.extend((pin, False) for pin in sorted(related, reverse=True))

    if any(records[pin]["record_kind"] != "asset" for pin in must_be_assets):
        raise RegistryError(2, "parent_asset_kind")
    return records.pop(root), records, dict(sorted(sizes.items()))
