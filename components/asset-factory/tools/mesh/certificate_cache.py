"""Process-local, content-addressed storage for immutable GEO certificates."""
from collections import OrderedDict
from importlib.metadata import PackageNotFoundError, version
import json
from pathlib import Path
import platform
import sys
from threading import Lock

from tools.input.evidence import InputIssue, bytes_digest, canonical_bytes, is_digest


_SCHEMA = "af.geometry-certificate-cache-entry/v1"


def measurement_key(*, subjects, policy, parameters, scope, registrations, checker, toolchain):
    """Hash a canonical request, preserving source/LOD order and field boundaries."""
    return bytes_digest(canonical_bytes({
        "schema": _SCHEMA,
        "subjects": subjects, "policy": policy, "parameters": parameters, "scope": scope,
        "registrations": registrations, "checker": checker, "toolchain": toolchain,
    }))


def runtime_pin(source_toolchain):
    distributions = {}
    for package in ("numpy", "numba", "llvmlite"):
        try:
            distributions[package] = version(package)
        except PackageNotFoundError:
            distributions[package] = None
    return bytes_digest(canonical_bytes({
        "sources": source_toolchain, "python": sys.version, "machine": platform.machine(),
        "distributions": distributions,
        "cache_source": bytes_digest(Path(__file__).read_bytes()),
    }))


def _matches(raw, digest):
    return type(raw) is bytes and is_digest(digest) and bytes_digest(raw) == digest


class CertificateCache:
    """Bounded cache owned by one trusted caller. Always reopen authority on hit."""

    def __init__(self, max_bytes=16 * 1024**2):
        if type(max_bytes) is not int or max_bytes < 1:
            raise ValueError("invalid geometry cache budget")
        self._limit = max_bytes
        self._size = 0
        self._entries = OrderedDict()
        self._lock = Lock()

    def _lookup(self, key, reader):
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            self._entries.move_to_end(key)
        digest, raw = entry
        if not _matches(raw, digest):
            return None
        envelope = json.loads(raw)
        if envelope["schema"] != _SCHEMA or envelope["key"] != key:
            return None
        try:
            for role, pin in envelope["reads"]:
                if not _matches(reader(role, pin), pin):
                    return None
        except InputIssue:
            return None
        return envelope["certificate"]

    def _remember(self, key, report, reads):
        if any(not _matches(raw, pin) for (_, pin), raw in reads.items()):
            return
        raw = canonical_bytes({"schema": _SCHEMA, "key": key, "certificate": report,
                               "reads": [[role, pin] for role, pin in reads]})
        if len(raw) > self._limit:
            return
        entry = bytes_digest(raw), raw
        with self._lock:
            if key in self._entries:
                return
            while self._size + len(raw) > self._limit:
                _, (_, evicted) = self._entries.popitem(last=False)
                self._size -= len(evicted)
            self._entries[key] = entry
            self._size += len(raw)
