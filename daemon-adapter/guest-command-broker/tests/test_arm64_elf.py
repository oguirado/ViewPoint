"""Build both AArch64 broker variants and exercise production ELF admission."""
from hashlib import sha256
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest

BROKER = Path(__file__).resolve().parents[1]
TARGET = 'aarch64-unknown-linux-gnu'
MAX_BROKER_BYTES = 12 << 20
sys.path.insert(0, str(BROKER.parent))
from guest_command_runtime.image import ImageError, static_arm64
from guest_command_runtime.native_checks import pinned_asset


def build_pie(target_dir):
    env = os.environ.copy()
    env.pop('CARGO_ENCODED_RUSTFLAGS', None)
    env['RUSTFLAGS'] = '-C target-feature=+crt-static -C relocation-model=pic'
    subprocess.run([
        'cargo', 'build', '--offline', '--locked', '--release', '--target', TARGET,
        '--target-dir', str(target_dir), '--bin', 'guest-command-broker',
    ], cwd=BROKER, env=env, check=True)
    return target_dir / TARGET / 'release' / 'guest-command-broker'


class BrokerELFTests(unittest.TestCase):
    def test_executable_rejects_each_forbidden_program_header(self):
        def executable(segment_type):
            header = struct.pack(
                '<16sHHIQQQIHHHHHH', b'\x7fELF\x02\x01\x01', 2, 183, 1,
                0x400000, 64, 0, 0, 64, 56, 2, 0, 0, 0)
            code = struct.pack('<IIQQQQQQ', 1, 5, 176, 0x400000, 0, 1, 1, 1)
            extra = struct.pack('<IIQQQQQQ', segment_type, 4, 177, 0, 0, 16, 16, 1)
            return header + code + extra + b'\0/lib/ld.so\0\0\0\0\0\0'

        with tempfile.TemporaryDirectory(prefix='mv-broker-headers-') as directory:
            asset = Path(directory) / 'broker'
            control = executable(0)  # PT_NULL
            asset.write_bytes(control)
            self.assertEqual(pinned_asset(asset, sha256(control).hexdigest(),
                                          static=True, max_bytes=MAX_BROKER_BYTES), control)
            static_arm64(control, MAX_BROKER_BYTES)
            for name, segment_type in (('PT_DYNAMIC', 2), ('PT_INTERP', 3)):
                data = executable(segment_type)
                asset.write_bytes(data)
                with self.subTest(header=name, gate='pinned_asset'):
                    with self.assertRaisesRegex(RuntimeError, '^init must have no PT_DYNAMIC or PT_INTERP$'):
                        pinned_asset(asset, sha256(data).hexdigest(), static=True, max_bytes=MAX_BROKER_BYTES)
                with self.subTest(header=name, gate='static_arm64'):
                    with self.assertRaisesRegex(ImageError, '^dynamic or malformed executable$'):
                        static_arm64(data, MAX_BROKER_BYTES)

    def test_static_pie_rejected_and_static_executable_admitted(self):
        output = os.environ.get('MV_BROKER_TEST_TARGET_DIR')
        if output is None:
            temporary = tempfile.TemporaryDirectory(prefix='mv-broker-elf-')
            self.addCleanup(temporary.cleanup)
            output = temporary.name
        output = Path(output).resolve()

        static_dir = output / 'static'
        subprocess.run([str(BROKER / 'build-arm64.sh'), str(static_dir)], cwd=BROKER, check=True)
        static = static_dir / TARGET / 'release' / 'guest-command-broker'
        info = static.stat()
        self.assertEqual(info.st_nlink, 1, 'the local registry rejects hard-linked artifacts')
        self.assertEqual(info.st_mode & 0o022, 0, 'the local registry rejects shared write access')
        static_data = static.read_bytes()
        self.assertEqual(pinned_asset(static, sha256(static_data).hexdigest(),
                                      static=True, max_bytes=MAX_BROKER_BYTES), static_data)
        static_arm64(static_data, MAX_BROKER_BYTES)
        self.assertEqual(struct.unpack_from('<H', static_data, 16)[0], 2)  # ET_EXEC

        pie = build_pie(output / 'pic')
        pie_data = pie.read_bytes()
        self.assertEqual(struct.unpack_from('<H', pie_data, 16)[0], 3)  # ET_DYN
        offset = struct.unpack_from('<Q', pie_data, 32)[0]
        entry_size, count = struct.unpack_from('<HH', pie_data, 54)
        segment_types = {struct.unpack_from('<I', pie_data, offset + i * entry_size)[0]
                         for i in range(count)}
        self.assertIn(2, segment_types)  # PT_DYNAMIC
        self.assertNotIn(3, segment_types)  # PT_INTERP
        with self.assertRaisesRegex(RuntimeError, '^init must have no PT_DYNAMIC or PT_INTERP$'):
            pinned_asset(pie, sha256(pie_data).hexdigest(), static=True, max_bytes=MAX_BROKER_BYTES)
        with self.assertRaisesRegex(ImageError, '^static ARM64 ET_EXEC required$'):
            static_arm64(pie_data, MAX_BROKER_BYTES)


if __name__ == '__main__':
    unittest.main()
