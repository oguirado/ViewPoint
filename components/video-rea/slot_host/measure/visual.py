# SPDX-License-Identifier: AGPL-3.0-only
"""CPU-only decoder and lease for the sealed RGB8 recordings."""

import io
import os
import struct
import time
from contextlib import contextmanager
from uuid import uuid4

from pixel_compare import RGB8Frame
from slot_host.compare._visual import DecodedFrame, RecordingCompletion
from slot_host.runtime.process_lifecycle import proc_starttime

from .seal import FixtureError, sha

PPM = b'P6\n2 1\n255\n'
DECODER = 'fixture.rgb8.decode'
HEADER = struct.Struct('>II')
FRAME = struct.Struct('>I6s')


def rgb8_header(stream, decoder_ref):
    if decoder_ref != DECODER:
        raise FixtureError('Unlisted CPU fixture decoder.')
    return HEADER.unpack(stream.read(HEADER.size))


def rgb8_frame(stream):
    return FRAME.unpack(stream.read(FRAME.size))


class VisualFixture:
    def __init__(self, recordings):
        self.recordings = recordings
        self.lease = None
        self.grants = self.releases = self.streams = self.buffers = 0

    def acquire(self, front, pid, timeout, ttl):
        starttime = proc_starttime(pid)
        if self.lease is not None or pid != os.getpid() or type(starttime) is not int:
            return None
        token = 'fixture-' + uuid4().hex
        self.lease = (front, pid, token)
        self.grants += 1
        return {'front': front, 'pid': pid, 'starttime': starttime, 'lease_id': token,
                'expires_at_monotonic': time.monotonic() + ttl}

    def release(self, front, pid, lease_id):
        if self.lease != (front, pid, lease_id) or self.streams or self.buffers:
            return False
        self.lease = None
        self.releases += 1
        return True

    @contextmanager
    def open_recording(self, ref):
        if self.lease is None:
            raise FixtureError('CPU fixture decode requires a disposable lease.')
        self.streams += 1
        try:
            with io.BytesIO(self.recordings[ref]) as stream:
                yield stream
        finally:
            self.streams -= 1

    def decode(self, stream, decoder_ref, *, recording_ref, deadline, budget):
        count, den = rgb8_header(stream, decoder_ref)
        raw_digest = sha(self.recordings[recording_ref])
        for index in range(count):
            if time.monotonic() >= deadline:
                raise FixtureError('CPU fixture decode exceeded its deadline.')
            with budget.reserve(len(PPM) + 16) as reservation:
                self.buffers += 1
                try:
                    tick, pixels = rgb8_frame(stream)
                    yield DecodedFrame(index=index, pts_ticks=tick, time_base=(1, den),
                                       raster=RGB8Frame(2, 1, pixels), recording_sha256=raw_digest,
                                       reservation=reservation, retained_file_bytes=PPM + pixels)
                finally:
                    self.buffers -= 1
        if stream.read(1):
            raise FixtureError('CPU fixture recording has an unexpected tail.')
        return RecordingCompletion(recording_sha256=raw_digest, frame_count=count)

    def settled(self):
        return self.lease is None and not self.streams and not self.buffers and self.grants == self.releases
