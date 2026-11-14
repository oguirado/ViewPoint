"""Vision memory limits and named negative controls (stdlib, RAM frames only).

Mutants: remove_axis_cap, remove_pixel_cap, remove_parse_cap,
remove_pipe_cap, materialize_png. Each corresponding test must fail.
"""
import hashlib
import io
import json
import os
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch
import zlib

from wender.perceive import vision_adapter as adapter
from wender.perceive import vision_bridge as bridge


class VisionBoundsTests(unittest.TestCase):
    def test_axis_cap(self):
        for width, height in ((16385, 1), (1, 16385)):
            with self.subTest(width=width, height=height):
                with self.assertRaisesRegex(ValueError, 'dimension exceeds'):
                    adapter.Frame(width, height, b'')
        for width, height in ((16384, 1), (1, 16384)):
            adapter.Frame(width, height, bytes(width * height * 3))

    def test_pixel_cap(self):
        with self.assertRaisesRegex(ValueError, 'pixel count exceeds'):
            adapter.Frame(7680, 4321, b'')
        frame = adapter.Frame(7680, 4320, bytes(7680 * 4320 * 3))
        self.assertEqual(len(frame.data), 99_532_800)

    def test_revalidate_before_launch(self):
        frame = adapter.Frame(1, 1, bytes(3))
        object.__setattr__(frame, 'width', 16385)
        with tempfile.TemporaryDirectory() as root:
            with patch.object(bridge.subprocess, 'run') as run:
                with self.assertRaisesRegex(ValueError, 'dimension exceeds'):
                    bridge.SubprocessVisionEngine(root).describe(frame)
                run.assert_not_called()

    def test_parse_cap(self):
        with patch.object(bridge.json, 'loads') as loads:
            with self.assertRaisesRegex(bridge.VisionBridgeError, 'stdout exceeds'):
                bridge._parse_description(b' ' * (bridge.MAX_C2_OUTPUT_BYTES + 1))
            loads.assert_not_called()
        row = dict(schema='ve.cu_describe/v1', frame_sha256='0' * 64,
                   width=1, height=1, changed=None, changed_regions=[], crops=[], timing_us=0)
        raw = json.dumps(row).encode()
        padded = raw + b' ' * (bridge.MAX_C2_OUTPUT_BYTES - len(raw))
        self.assertEqual(bridge._parse_description(padded).width, 1)

    def _child(self, body):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        package = Path(temporary.name) / 'vadapt'
        package.mkdir()
        (package / '__init__.py').write_text('')
        (package / 'cu_describe.py').write_text(body)
        return bridge.SubprocessVisionEngine(temporary.name, python=sys.executable, timeout=2)

    def test_pipe_cap(self):
        # Both streams are drained concurrently and independently bounded.
        for fd, name in ((1, 'stdout'), (2, 'stderr')):
            engine = self._child('import os\nfor _ in range(145):\n os.write(%d, b"x" * 65536)\n' % fd)
            with self.subTest(stream=name):
                with self.assertRaisesRegex(bridge.VisionBridgeError, name + ' exceeds byte limit'):
                    engine.describe(adapter.Frame(1, 1, bytes(3)))

    def test_pipe_exact_limit(self):
        row = dict(schema='ve.cu_describe/v1', frame_sha256=hashlib.sha256(bytes(3)).hexdigest(),
                   width=1, height=1, changed=None, changed_regions=[], crops=[], timing_us=0)
        body = ('import os\nraw = %r\nos.write(2, b"diagnostic")\n'
                'os.write(1, raw + b" " * (%d - len(raw)))\n') % (
                    json.dumps(row).encode(), bridge.MAX_C2_OUTPUT_BYTES)
        self.assertEqual(self._child(body).describe(adapter.Frame(1, 1, bytes(3))).width, 1)

    def test_timeout_after_pipe_eof(self):
        engine = self._child('import os, time\nos.close(1)\nos.close(2)\ntime.sleep(5)\n')
        engine.timeout = 0.1
        with self.assertRaisesRegex(bridge.VisionBridgeError, 'timed out'):
            engine.describe(adapter.Frame(1, 1, bytes(3)))

    def test_streaming_png(self):
        # Incompressible rows force multiple IDAT chunks; no frame-sized write.
        frame = adapter.Frame(1024, 128, os.urandom(1024 * 128 * 3))
        class Sink(io.BytesIO):
            def write(self, data):
                if len(data) > 131072:
                    raise AssertionError('full-frame PNG materialized')
                return super().write(data)
        sink = Sink()
        bridge._write_png(sink, frame)
        png = sink.getvalue()
        self.assertEqual(png[:8], b'\x89PNG\r\n\x1a\n')
        offset, compressed = 8, []
        while offset < len(png):
            size, kind = struct.unpack_from('>I4s', png, offset)
            data = png[offset + 8:offset + 8 + size]
            crc = struct.unpack_from('>I', png, offset + 8 + size)[0]
            self.assertEqual(crc, zlib.crc32(data, zlib.crc32(kind)))
            if kind == b'IDAT':
                compressed.append(data)
            offset += size + 12
        self.assertGreater(len(compressed), 1)
        stride = frame.width * 3
        expected = b''.join(b'\x00' + frame.data[i:i + stride]
                            for i in range(0, len(frame.data), stride))
        self.assertEqual(zlib.decompress(b''.join(compressed)), expected)


if __name__ == '__main__':
    unittest.main()
