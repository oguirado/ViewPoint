# SPDX-License-Identifier: AGPL-3.0-only
"""Open the reviewed fixture generation once and retain verified byte snapshots."""

import hashlib
import os
import stat
from pathlib import Path

from slot_host.projections.schema import digest
from video_rea.expose.wire import loads

DEFAULT_MANIFEST = Path(__file__).with_name('fixtures') / 'manifest.json'
MANIFEST_SHA256 = '3cf4019a74cb78f386238118e44fd03704ea7a4c1e9570de927f6280ef27817f'
FILES = {'inputs.json', 'truth.json', 'fixture_runner.py'}
FILE_LIMIT = 256 * 1024


class FixtureError(ValueError):
    """The fixture or its evidence could not be verified."""

    def __init__(self, message, *, sample=None):
        super().__init__(message)
        self.sample = sample


class FixtureReferenceError(FixtureError):
    """An unlisted or incorrectly bound fixture reference."""


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def read_regular(name, *, directory=None, limit=FILE_LIMIT):
    fd = os.open(name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=directory)
    with os.fdopen(fd, 'rb') as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise FixtureError('Expected a regular fixture file.')
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise FixtureError('Fixture exceeds its byte limit.')
    return raw


class Seal:
    def __init__(self, path):
        fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            raw = read_regular(path.name, directory=fd)
            if sha(raw) != MANIFEST_SHA256:
                raise FixtureError('Fixture manifest digest differs from the reviewed pin.')
            manifest = loads(raw)
            if manifest['schema'] != 'rea.medicion.fixture_manifest/v1' or set(manifest['files']) != FILES:
                raise FixtureError('Fixture manifest inventory differs from the reviewed contract.')
            documents = {}
            for name, expected in manifest['files'].items():
                raw = read_regular(name, directory=fd)
                if sha(raw) != expected:
                    raise FixtureError(f'Sealed fixture byte digest differs for {name}.')
                documents[name] = raw
        finally:
            os.close(fd)
        self.generation = manifest['generation']
        self.inputs = loads(documents['inputs.json'])
        self.truth = loads(documents['truth.json'])
        self.executable = documents['fixture_runner.py']
        slot = self.inputs['slot']
        provider = self.inputs['provider_record']
        adapter = self.inputs['adapter_record']
        if (self.inputs['generation'] != self.generation or self.truth['generation'] != self.generation
                or self.inputs['cases'].keys() != self.truth['assignments'].keys()
                or slot['provider_ref']['implementation_sha256'] != sha(self.executable)
                or slot['provider_ref']['provider_sha256'] != digest(provider)
                or slot['capabilities']['adapter_identity']['adapter_sha256'] != digest(adapter)):
            raise FixtureError('Fixture generation or identity disagrees with its sealed inventory.')
        for ref, row in self.inputs['assets'].items():
            if row['generation'] != self.generation or row['sha256'] != digest(row['data']):
                raise FixtureError(f'Sealed reference binding differs for {ref}.')
