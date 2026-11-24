# SPDX-License-Identifier: AGPL-3.0-only
"""Request-scoped fixture retention and an independent result consumer."""

import os
import stat
from functools import partial
from time import perf_counter_ns

from slot_host.projections.schema import jcs_bytes
from video_rea.expose.wire import loads

from .seal import FILE_LIMIT, FixtureError, read_regular, sha

PAGE_BYTES = 512


class Store:
    def __init__(self, root, request_id, attempt_id):
        self.root = root
        self.root.mkdir(mode=0o700)
        self.request_id = request_id
        self.attempt_id = attempt_id
        self.index = {}

    def put(self, key, raw, *, ref=None):
        if key in self.index or type(raw) is not bytes or len(raw) > FILE_LIMIT:
            raise FixtureError('Invalid fixture retention object.')
        checksum = sha(raw)
        if ref and ref.rsplit(':', 1)[-1] != checksum:
            raise FixtureError('Advertised reference differs from retained bytes.')
        path = self.root / checksum
        try:
            with path.open('xb') as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
        except FileExistsError:
            if read_regular(path, limit=len(raw)) != raw:
                raise FixtureError('Content-addressed evidence changed during retention.') from None
        self._sync_directory()
        self.index[key] = {'sha256': checksum, 'bytes': len(raw), 'ref': ref}

    def put_json(self, key, document):
        self.put(key, jcs_bytes(document))

    def _authorized_object(self, key, request_id, attempt_id):
        if (request_id, attempt_id) != (self.request_id, self.attempt_id):
            raise PermissionError('Fixture evidence does not belong to this request and attempt.')
        return self.index[key]

    def read(self, key, *, request_id, attempt_id):
        raw, _ = self.read_verified(key, request_id=request_id, attempt_id=attempt_id)
        return raw

    def read_verified(self, key, *, request_id, attempt_id):
        row = self._authorized_object(key, request_id, attempt_id)
        raw = read_regular(self.root / row['sha256'], limit=row['bytes'])
        if len(raw) != row['bytes']:
            raise FixtureError('Retained fixture readback differs from its index.')
        checksum = sha(raw)
        if checksum != row['sha256']:
            raise FixtureError('Retained fixture readback differs from its index.')
        return raw, checksum

    def read_page(self, key, offset, *, request_id, attempt_id):
        row = self._authorized_object(key, request_id, attempt_id)
        if type(offset) is not int or not 0 <= offset <= row['bytes']:
            raise FixtureError('Invalid fixture readback cursor.')
        return _read_page(self.root / row['sha256'], row['bytes'], offset)

    def read_pages(self, key, size, *, request_id, attempt_id):
        row = self._authorized_object(key, request_id, attempt_id)
        # Keep opening the current name for each page so replacements and truncation
        # still face the per-page file checks and the consumer's final digest check.
        return collect_pages(size, partial(_read_page, self.root / row['sha256'], row['bytes']))

    def _sync_directory(self):
        fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def commit(self):
        path = self.root / 'index'
        with path.open('xb') as stream:
            stream.write(jcs_bytes({'request_id': self.request_id, 'attempt_id': self.attempt_id,
                                    'objects': self.index}))
            stream.flush()
            os.fsync(stream.fileno())
        self._sync_directory()
        return path


def _read_page(path, size, offset):
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size != size:
            raise FixtureError('Retained fixture readback differs from its index.')
        chunk = os.pread(fd, PAGE_BYTES, offset)
    finally:
        os.close(fd)
    end = offset + len(chunk)
    return chunk, end if end < size else None


def collect_pages(size, page_reader):
    offset, chunks = 0, []
    while offset is not None:
        chunk, continuation = page_reader(offset)
        end = offset + len(chunk)
        expected = min(offset + PAGE_BYTES, size)
        if end != expected or continuation != (end if end < size else None):
            raise FixtureError('Fixture evidence paging is incomplete or out of order.')
        chunks.append(chunk)
        offset = continuation
    return b''.join(chunks), len(chunks)


def readback(store, keys, binding):
    index_path = store.commit()
    start = perf_counter_ns()
    index_bytes = read_regular(index_path)
    index = loads(index_bytes)
    owner = {key: binding[key] for key in ('request_id', 'attempt_id')}
    if (any(index[key] != value for key, value in owner.items())
            or set(index['objects']) != set(keys)):
        raise FixtureError('Retained fixture index has the wrong parent or inventory.')
    values, pages, size = {}, 0, 0
    for key, row in index['objects'].items():
        raw, count = store.read_pages(key, row['bytes'], **owner)
        if sha(raw) != row['sha256'] or len(raw) != row['bytes']:
            raise FixtureError('Consumer could not reconstruct the indexed bytes.')
        values[key] = loads(raw)
        pages += count
        size += len(raw)
    return values, {
        'readback_ns': perf_counter_ns() - start, 'pages': pages, 'bytes': size,
        'index_sha256': sha(index_bytes), 'objects': index['objects'],
    }


def occurrences(expected, actual):
    counts = dict.fromkeys(('assigned', 'observed', 'missing', 'duplicate', 'wrong', 'late', 'refused', 'unknown'), 0)
    counts.update(assigned=len(expected), observed=len(actual))
    remaining = {row['occurrence']: row for row in expected}
    if len(remaining) != len(expected):
        raise FixtureError('Frozen occurrence identifiers must be unique.')
    seen = set()
    for row in actual:
        if not isinstance(row, dict) or type(row.get('occurrence')) is not str:
            counts['unknown'] += 1
            continue
        key = row['occurrence']
        if key in seen:
            counts['duplicate'] += 1
        elif key not in remaining:
            counts['unknown'] += 1
        else:
            wanted = remaining.pop(key)
            if row.get('outcome') == 'refused':
                counts['refused'] += 1
            elif any(row.get(field) != value for field, value in wanted.items() if field not in ('ticks', 'late_tolerance_ticks')):
                counts['wrong'] += 1
            elif 'ticks' in wanted:
                observed_ticks = row.get('ticks')
                if type(observed_ticks) is not int or observed_ticks < wanted['ticks']:
                    counts['wrong'] += 1
                elif observed_ticks - wanted['ticks'] > wanted.get('late_tolerance_ticks', 0):
                    counts['late'] += 1
        seen.add(key)
    counts['missing'] = len(remaining)
    return counts


def check_truth(expectations, result, artifacts):
    evidence = {'result': result, 'artifact': artifacts}
    for path, expected in expectations:
        value = evidence
        for part in path.split('/'):
            value = value[int(part)] if type(value) is list else value[part]
        if jcs_bytes(value) != jcs_bytes(expected):
            raise FixtureError(f'Frozen result witness differs at {path}.')
