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
