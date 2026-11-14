# @module {"id":"wa.remember.store","layer":1,"owner":"REMEMBER","deps":["wa.remember.program_map","wa.remember.skills","wa.state.schema"],"book":"HB-WA-s3-remember"}
"""Canonical, integrity-checked program maps and admitted skills on disk.

Each append_map is an observation batch: seen values are additive deltas.
A changed skill needs a strictly newer integer evidence.version. Single writer
per directory; callers must serialize access. Frames remain RAM-only.
"""
import hashlib
import json
import os
from pathlib import Path
import tempfile

from .program_map import ProgramMap, canonicalize_state_key
from .skills import admit
from ..state.schema import dumps

_SCHEMA = "wender.store/v1"
_FORBIDDEN = frozenset(("frame", "frames", "pixels", "image", "screenshot", "png"))


# @anchor:wa.remember.store.StoreCorrupt
class StoreCorrupt(ValueError):
    """A persisted record failed integrity or replay validation."""


# @anchor:wa.remember.store.StoreRefused
class StoreRefused(ValueError):
    """An append would violate storage or merge policy."""


def _guard(value, active=None):
    if isinstance(value, (bytes, bytearray, memoryview)):
        raise StoreRefused("binary frame-like value")
    if isinstance(value, (dict, list, tuple)):
        active = set() if active is None else active
        if id(value) in active:
            raise StoreRefused("cyclic body")
        active.add(id(value))
        try:
            if isinstance(value, dict):
                for key, child in value.items():
                    if key in _FORBIDDEN:
                        raise StoreRefused("forbidden body key: %s" % key)
                    _guard(child, active)
            else:
                for child in value:
                    _guard(child, active)
        finally:
            active.remove(id(value))


def _integer(value):
    return type(value) is int


def _version(skill):
    value = skill["evidence"].get("version")
    return value if _integer(value) else None


def _newer(candidate, previous):
    new, old = _version(candidate), _version(previous)
    return new is not None and old is not None and new > old


def _record(kind, body, seq):
    record = {"schema": _SCHEMA, "kind": kind, "seq": seq, "body": body}
    record["line_sha256"] = hashlib.sha256(dumps(record).encode("utf-8")).hexdigest()
    return dumps(record) + "\n"


# @anchor:wa.remember.store.Store
class Store:
    """Append/fsync logs and atomically compact them within a directory."""

    def __init__(self, directory):
        self.directory = Path(directory)

    def _read(self, filename):
        path = self.directory / filename
        if not path.exists():
            return []
        records = []
        previous = 0
        # Binary iteration keeps malformed UTF-8 attributable to its line.
        with path.open("rb") as stream:
            for number, line in enumerate(stream, 1):
                try:
                    record = json.loads(line)
                    if not line.endswith(b"\n"):
                        raise ValueError("incomplete record")
                    if not isinstance(record, dict) or set(record) != {
                            "schema", "kind", "seq", "body", "line_sha256"}:
                        raise ValueError("record keys")
                    if record["schema"] != _SCHEMA:
                        raise ValueError("schema")
                    seq = record["seq"]
                    if not _integer(seq) or seq <= previous or (not records and seq != 1):
                        raise ValueError("seq order")
                    allowed = ("skill",) if filename == "skills.jsonl" else ("map_node", "map_edge")
                    if record["kind"] not in allowed or not isinstance(record["body"], dict):
                        raise ValueError("kind/body")
                    unsigned = {k: v for k, v in record.items() if k != "line_sha256"}
                    digest = hashlib.sha256(dumps(unsigned).encode("utf-8")).hexdigest()
                    if record["line_sha256"] != digest:
                        raise ValueError("line_sha256 mismatch")
                    _guard(record["body"])
                    previous = seq
                    records.append(record)
                except (ValueError, TypeError, KeyError, RecursionError) as exc:
                    raise StoreCorrupt("%s line %d: %s" % (filename, number, exc)) from exc
        return records

    def _maps(self, records):
        maps = {}
        # Resolve only observed journal spellings; never create alias nodes.
        spellings = {}
        for number, record in enumerate(records, 1):
            try:
                body = record["body"]
                program = body["program"]
                if not isinstance(program, str):
                    raise ValueError("program must be a string")
                result = maps.setdefault(program, ProgramMap(program))
                aliases = spellings.setdefault(program, {})
                if record["kind"] == "map_node":
                    node, stored_key = body["node"], body["key"]
                    if not isinstance(node, dict) or set(node) != {"screen_fp", "ax_stable_sha", "seen"}:
                        raise ValueError("node fields")
                    if not isinstance(stored_key, str):
                        raise ValueError("node key")
                    key = canonicalize_state_key(stored_key, node["screen_fp"], node["ax_stable_sha"])
                    if not _integer(node["seen"]) or node["seen"] < 0:
                        raise ValueError("seen must be a non-negative integer")
                    # A spelling cannot name two typed identities, including a
                    # legacy spelling that would shadow another canonical key.
                    for spelling in (stored_key, key):
                        if spelling in aliases and aliases[spelling] != key:
                            raise ValueError("node identity conflict")
                    if key in result.nodes:
                        old = result.nodes[key]
                        if any(old[k] != node[k] for k in ("screen_fp", "ax_stable_sha")):
                            raise ValueError("node identity conflict")
                        old["seen"] += node["seen"]
                    else:
                        result.nodes[key] = dict(node)
                    aliases[stored_key] = key
                    aliases[key] = key
                else:
                    edge = body["edge"]
                    if not isinstance(edge, dict) or set(edge) != {"pre", "post", "action", "changed"}:
                        raise ValueError("edge fields")
                    endpoints = []
                    for endpoint in (edge["pre"], edge["post"]):
                        key = aliases.get(endpoint, endpoint)
                        if key not in result.nodes:
                            raise KeyError("unknown state %s" % endpoint)
                        endpoints.append(key)
                    result.add_edge(endpoints[0], edge["action"], endpoints[1], edge["changed"])
            except (ValueError, TypeError, KeyError) as exc:
                raise StoreCorrupt("program_map.jsonl line %d: %s" % (number, exc)) from exc
        return maps

    def _skills(self, records):
        skills = {}
        for number, record in enumerate(records, 1):
            try:
                body = record["body"]
                canonical = admit(body)
                key = body["id"]
                if not isinstance(key, str):
                    raise ValueError("skill id must be a string")
                previous = skills.get(key)
                if previous is not None and canonical != dumps(previous) and not _newer(body, previous):
                    raise ValueError("skill evidence is not newer")
                skills[key] = body
            except (ValueError, TypeError, KeyError, AttributeError) as exc:
                raise StoreCorrupt("skills.jsonl line %d: %s" % (number, exc)) from exc
        return skills

    def _append(self, filename, records, pending):
        if not pending:
            return
        seq = records[-1]["seq"] if records else 0
        # Fully validate and encode the entire batch before creating any file.
        encoded = "".join(_record(kind, body, seq + i)
                          for i, (kind, body) in enumerate(pending, 1)).encode("utf-8")
        self.directory.mkdir(parents=True, exist_ok=True)
        with (self.directory / filename).open("ab") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())

    # @anchor:wa.remember.store.Store.append_map
    def append_map(self, program_map):
        """Merge an observation batch; append node deltas and unseen edges."""
        _guard({"program": program_map.program, "nodes": program_map.nodes, "edges": program_map.edges})
        records = self._read("program_map.jsonl")
        maps = self._maps(records)
        existing = maps.get(program_map.program, ProgramMap(program_map.program))
        pending = []
        for key, node in program_map.nodes.items():
            if key not in existing.nodes or node["seen"]:
                pending.append(("map_node", {"program": program_map.program, "key": key, "node": node}))
        for edge in program_map.edges:
            if edge not in existing.edges:
                pending.append(("map_edge", {"program": program_map.program, "edge": edge}))
        # Replay the candidate batch before append, including edge references.
        candidates = [{"kind": kind, "body": body} for kind, body in pending]
        self._maps(records + candidates)
        self._append("program_map.jsonl", records, pending)

    # @anchor:wa.remember.store.Store.load_map
    def load_map(self, program):
        """Replay into a fresh ProgramMap for the requested program."""
        return self._maps(self._read("program_map.jsonl")).get(program, ProgramMap(program))

    # @anchor:wa.remember.store.Store.append_skill
    def append_skill(self, skill):
        """Admit a skill; conflicting bodies require newer evidence.version."""
        _guard(skill)
        canonical = admit(skill)
        if not isinstance(skill["id"], str):
            raise StoreRefused("skill id must be a string")
        records = self._read("skills.jsonl")
        previous = self._skills(records).get(skill["id"])
        if previous is not None:
            if dumps(previous) == canonical:
                return
            if not _newer(skill, previous):
                raise StoreRefused("conflicting skill requires newer integer evidence.version")
        self._append("skills.jsonl", records, [("skill", skill)])

    # @anchor:wa.remember.store.Store.load_skills
    def load_skills(self):
        """Return independent admitted records, sorted by skill id."""
        skills = self._skills(self._read("skills.jsonl"))
        return [skills[key] for key in sorted(skills)]

    # @anchor:wa.remember.store.Store.compact
    def compact(self):
        """Fold deltas and superseded skills; replace logs via same-dir temps."""
        map_records = self._read("program_map.jsonl")
        skill_records = self._read("skills.jsonl")
        maps, skills = self._maps(map_records), self._skills(skill_records)
        pending = []
        for program, result in maps.items():
            pending.extend(("map_node", {"program": program, "key": key, "node": node})
                           for key, node in result.nodes.items())
            pending.extend(("map_edge", {"program": program, "edge": edge}) for edge in result.edges)
        groups = (("program_map.jsonl", pending),
                  ("skills.jsonl", [("skill", skills[key]) for key in sorted(skills)]))
        for filename, entries in groups:
            path = self.directory / filename
            if not path.exists():
                continue
            encoded = "".join(_record(kind, body, i) for i, (kind, body) in enumerate(entries, 1))
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n",
                                                 dir=self.directory, delete=False) as stream:
                    temporary = stream.name
                    stream.write(encoded)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, path)
            finally:
                if temporary is not None and os.path.exists(temporary):
                    os.unlink(temporary)
