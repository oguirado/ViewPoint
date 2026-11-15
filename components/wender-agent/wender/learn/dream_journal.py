# @module {"id":"wa.learn.dream_journal","layer":2,"owner":"LEARN","deps":["wa.learn.script_ledger","wa.scripts.types"],"book":"HB-WA-s3-remember"}
"""RAM-only bounded Merkle journals and verified zero-inference replay support."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

JOURNAL_SCHEMA = 'cu.trace/v2'
DOMAIN_TAG = 'sw0222/step/v1'
GENESIS_HASH = '0' * 64
MAX_LINE_BYTES = 4096
MAX_JSON_DEPTH = MAX_JSON_MEMBERS = 32
MAX_STEPS = 64
SUPPORT_SLOTS = 256
PROMOTION_DELTA_Q16 = 9830
RC_OK, RC_OFF_SUPPORT, RC_SCHEMA_CONFLICT = 0, 64, 65
RC_TERMINAL, RC_CORRUPT, RC_INVALID = 70, 71, -1
_CHAIN_KEYS = ('step_hash', 'sha256', 'parent_hash', 'prev_sha256', 'step_index')


# @anchor:wa.learn.dream_journal.JournalVerificationError
class JournalVerificationError(ValueError):
    def __init__(self, rc, slot, reason):
        self.rc, self.slot, self.reason = rc, slot, reason
        super().__init__(f'journal verification rc={rc} slot={slot}: {reason}')


# @anchor:wa.learn.dream_journal.ns
def ns(value: str | bytes) -> bytes:
    data = value.encode('utf-8') if isinstance(value, str) else value
    return str(len(data)).encode('ascii') + b':' + data


def _hash_valid(value):
    return isinstance(value, str) and len(value) == 64 and all(c in '0123456789abcdef' for c in value)


# @anchor:wa.learn.dream_journal.compute_step_hash
def compute_step_hash(step_index: int, prev_sha256: str, payload_json: str) -> str:
    if type(step_index) is not int or step_index < 0 or not _hash_valid(prev_sha256):
        raise ValueError('invalid chain coordinate')
    if len(payload_json.encode('utf-8')) > MAX_LINE_BYTES:
        raise ValueError('payload too large')
    return hashlib.sha256(ns(DOMAIN_TAG) + ns(prev_sha256) + ns(str(step_index)) + ns(payload_json)).hexdigest()


# @anchor:wa.learn.dream_journal.canonical_payload
def canonical_payload(record: dict) -> str:
    return json.dumps({k: v for k, v in record.items() if k not in _CHAIN_KEYS},
                      sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)


def _shape(value, depth=1):
    if depth > MAX_JSON_DEPTH:
        raise ValueError('JSON depth exceeded')
    if isinstance(value, dict):
        if len(value) > MAX_JSON_MEMBERS or any(not isinstance(k, str) for k in value):
            raise ValueError('JSON members exceeded')
        for child in value.values():
            _shape(child, depth + 1)
    elif isinstance(value, list):
        for child in value:
            _shape(child, depth + 1)


def _pairs(pairs):
    result = {}
    for k, v in pairs:
        if k in result:
            raise ValueError('duplicate JSON member')
        result[k] = v
    return result


def _loads(raw):
    def invalid(_):
        raise ValueError('non-finite JSON number')
    return json.loads(raw, object_pairs_hook=_pairs, parse_constant=invalid)


def _ram_path(path):
    resolved = Path(path).resolve()
    if not resolved.is_relative_to('/dev/shm'):
        raise ValueError('dream journal must live in /dev/shm')
    return resolved


def _records(source):
    if isinstance(source, list):
        return source
    data = Path(source).read_bytes()
    if not data:
        return []
    lines = data.splitlines(keepends=True)
    result = []
    for i, line in enumerate(lines):
        try:
            if not line.endswith(b'\n') or b'\r' in line or len(line) > MAX_LINE_BYTES + 1:
                raise ValueError('invalid line framing')
            result.append(_loads(line.decode('utf-8')))
        except (ValueError, UnicodeError, RecursionError) as exc:
            raise JournalVerificationError(RC_SCHEMA_CONFLICT, i, 'invalid JSON framing') from exc
    return result


# @anchor:wa.learn.dream_journal.JournalWriter
class JournalWriter:
    def __init__(self, path: str | Path, *, anchor_path: str | Path | None = None):
        self.path = _ram_path(path)
        self.anchor_path = _ram_path(anchor_path or (str(path) + '.head'))
        if self.path == self.anchor_path:
            raise ValueError('head must be separate from journal')
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.anchor_path.parent.mkdir(parents=True, exist_ok=True)
        self.step_index, self.head_sha256 = 0, GENESIS_HASH
        if self.path.exists() and self.path.stat().st_size:
            _, self.step_index = verify_chain(self.path, self.anchor_path)
            self.head_sha256 = _records(self.path)[-1]['step_hash']
        elif self.anchor_path.exists():
            raise ValueError('orphan journal head')

    # @anchor:wa.learn.dream_journal.JournalWriter.append
    def append(self, record: dict) -> dict:
        if self.step_index >= MAX_STEPS:
            raise ValueError('episode step limit exceeded')
        _shape(record)
        payload = _loads(canonical_payload(record))
        digest = compute_step_hash(self.step_index, self.head_sha256, canonical_payload(payload))
        entry = dict(schema=JOURNAL_SCHEMA, step_index=self.step_index,
                     parent_hash=self.head_sha256, prev_sha256=self.head_sha256,
                     step_hash=digest, sha256=digest, payload=payload)
        _shape(entry)
        line = (json.dumps(entry, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False) + '\n').encode('utf-8')
        if len(line) > MAX_LINE_BYTES + 1:
            raise ValueError('journal line limit exceeded')
        # O_NOFOLLOW also rejects links replaced after path validation.
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
        try:
            if os.write(fd, line) != len(line):
                raise OSError('short journal write')
            os.fsync(fd)
        finally:
            os.close(fd)
        temp = self.anchor_path.with_name(self.anchor_path.name + '.tmp')
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            if os.write(fd, (digest + '\n').encode('ascii')) != 65:
                raise OSError('short head write')
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(temp, self.anchor_path)
        self.step_index += 1
        self.head_sha256 = digest
        return entry


# @anchor:wa.learn.dream_journal.verify_journal
def verify_journal(journal_or_path: str | Path | list[dict], anchor=None, *, raise_on_error=False) -> tuple[int, int]:
    slot = 0
    try:
        records = _records(journal_or_path)
        if not records:
            raise JournalVerificationError(RC_OFF_SUPPORT, 0, 'empty journal')
        if len(records) > MAX_STEPS:
            raise JournalVerificationError(RC_SCHEMA_CONFLICT, MAX_STEPS, 'episode limit exceeded')
        prev = GENESIS_HASH
        for slot, record in enumerate(records):
            try:
                _shape(record)
                if not isinstance(record, dict) or record.get('schema') != JOURNAL_SCHEMA or type(record.get('step_index')) is not int or record['step_index'] != slot or not isinstance(record.get('payload'), dict):
                    raise ValueError('invalid envelope')
                if len(json.dumps(record, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode('utf-8')) > MAX_LINE_BYTES:
                    raise ValueError('line too large')
                claimed_prev = record.get('prev_sha256', record.get('parent_hash'))
                claimed = record.get('step_hash', record.get('sha256'))
                if (claimed_prev != prev or record.get('parent_hash', prev) != prev):
                    raise JournalVerificationError(RC_CORRUPT, slot, 'prev_sha256 mismatch')
                expected = compute_step_hash(slot, prev, canonical_payload(record['payload']))
                if claimed != expected or record.get('sha256', expected) != expected:
                    raise JournalVerificationError(RC_CORRUPT, slot, 'step_hash mismatch')
                prev = expected
            except (ValueError, TypeError, RecursionError) as exc:
                if isinstance(exc, JournalVerificationError):
                    raise
                raise JournalVerificationError(RC_SCHEMA_CONFLICT, slot, 'invalid envelope') from exc
        if anchor is None and not isinstance(journal_or_path, list):
            anchor = Path(str(journal_or_path) + '.head')
        if anchor is not None:
            if isinstance(anchor, Path) or not _hash_valid(anchor):
                try:
                    head = Path(anchor).read_text().strip()
                except OSError as exc:
                    raise JournalVerificationError(RC_CORRUPT, len(records), 'head missing') from exc
            else:
                head = anchor
            if prev != head:
                raise JournalVerificationError(RC_CORRUPT, len(records), 'anchor head mismatch')
        return RC_OK, len(records)
    except JournalVerificationError as exc:
        if raise_on_error:
            raise
        return exc.rc, exc.slot
    except (OSError, ValueError, TypeError, RecursionError) as exc:
        error = JournalVerificationError(RC_SCHEMA_CONFLICT, slot, 'unreadable journal')
        if raise_on_error:
            raise error from exc
        return error.rc, error.slot


# @anchor:wa.learn.dream_journal.verify_chain
def verify_chain(records_or_path, anchor=None) -> tuple[int, int]:
    return verify_journal(records_or_path, anchor=anchor, raise_on_error=True)


# @anchor:wa.learn.dream_journal.build_support
def build_support(journal_or_path, anchor=None) -> dict:
    rc, slot = verify_journal(journal_or_path, anchor=anchor)
    if rc:
        return dict(rc=rc, error_slot=slot, table={})
    table, children, posts = {}, set(), set()
    for record in _records(journal_or_path):
        body = record['payload']
        pre, action, post = body.get('pre_hash'), body.get('action_sig'), body.get('post_hash')
        code, score = body.get('exit_code', 0), body.get('score_q16', 0)
        if not all(isinstance(v, str) and v for v in (pre, action, post)) or type(code) is not int or type(score) is not int:
            return dict(rc=RC_SCHEMA_CONFLICT, error_slot=record['step_index'], table={})
        key, value = (pre, action), (post, code, score)
        if key in table and table[key] != value:
            return dict(rc=RC_SCHEMA_CONFLICT, error_slot=record['step_index'], table={})
        table[key] = value
        children.add(pre)
        posts.add(post)
    for terminal in sorted(posts - children):
        table[(terminal, '0xFFFFFFFF')] = (terminal, RC_TERMINAL, 0)
    if len(table) > SUPPORT_SLOTS:
        return dict(rc=RC_OFF_SUPPORT, error_slot=slot, table={})
    return dict(rc=RC_OK, table=table, promotion_delta_q16=PROMOTION_DELTA_Q16)


# @anchor:wa.learn.dream_journal.validate_script_payload
def validate_script_payload(payload, event):
    """Closed projection matched to owner evidence, never permissive support.

    S5 handoff: the strict loader returns a tuple of retained immutable RunEvent
    objects, not a second VerifiedEpisode type. A typed ContractError supplies a
    fixed reason on unsupported/corrupt/incomplete input, with no partial result.
    """
    from wender.learn import script_ledger as ledger
    from wender.scripts import types as types
    ledger._need(type(event) is ledger.RunEvent and type(payload) is dict, types.Reason.TYPE)
    expected = ledger._wire(event)
    ledger._need(set(payload) == set(expected))
    # Python equality treats true as 1; compare strict canonical encodings too.
    ledger._need(ledger._canonical(payload) == ledger._canonical(expected), types.Reason.CONFLICT)
    if event.state is types.EventState.VERIFIED:
        ledger._need(type(payload['exit_code']) is int and type(payload['score_q16']) is int,
                     types.Reason.MISSING)
    return event


# @anchor:wa.learn.dream_journal.load_script_episode
def load_script_episode(fd, *, owner, seal, head, grant, custody_check, allocation_check):
    """Bounded host descriptor ingress. Files and equal seals grant no support.

    Host custody_check(fd) and allocation_check(grant, peak_bytes) return S0
    AdmissionCheck before any read. Their authority is supplied, never inferred
    from a path, descriptor, header or the grant's value equality.
    """
    from wender.learn import script_ledger as ledger
    from wender.scripts import types as types
    policy = owner._policy
    with owner._lock:
        ledger._need(type(fd) is int and type(policy) is ledger.LedgerPolicy, types.Reason.TYPE)
        owner.acknowledge_seal(seal, owner._artifact.scope)
        ledger._need(type(head) is str and head == seal.chain_head, types.Reason.MISSING)
        ledger._host_check(custody_check, fd)
        ledger._host_check(allocation_check, grant, policy.ingress_peak_bytes)
        ledger._need(owner._charged + policy.ingress_peak_bytes <= policy.ledger_bytes,
                     types.Reason.CAPACITY)
        owner._charged += policy.ingress_peak_bytes
        try:
            envelope = ledger._decode(ledger._read_fd(fd, policy), policy)
            ledger._need(set(envelope) == {'schema', 'head', 'events'})
            ledger._need(envelope['schema'] == 'wa.script.episode/v1'
                         and envelope['head'] == head, types.Reason.CONFLICT)
            records = envelope['events']
            ledger._need(type(records) is list and len(records) == seal.expected_count,
                         types.Reason.MISSING)
            ledger._need(len(owner._events) == len(records), types.Reason.CONFLICT)
            previous = '0' * 64
            validated = []
            for index, record in enumerate(records):
                ledger._need(type(record) is dict and set(record) == {'index', 'parent', 'head', 'event'})
                ledger._need(type(record['index']) is int and record['index'] == index
                             and record['parent'] == previous, types.Reason.CONFLICT)
                event = validate_script_payload(record['event'], owner._events[index])
                previous = hashlib.sha256(bytes.fromhex(previous) + ledger._canonical(event)).hexdigest()
                ledger._need(record['head'] == previous, types.Reason.CONFLICT)
                validated.append(event)
            ledger._need(previous == head, types.Reason.CONFLICT)
            owner.acknowledge_seal(seal, owner._artifact.scope)
            return tuple(validated)
        finally:
            owner._charged -= policy.ingress_peak_bytes
