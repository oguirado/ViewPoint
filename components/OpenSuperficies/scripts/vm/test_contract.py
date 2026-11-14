#!/usr/bin/env python3
"""Offline negative and nominal image approval tests; never invokes Docker."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
import sys

sys.dont_write_bytecode = True

ROOT = Path(__file__).resolve().parents[2]
(ROOT / 'build/room_CAPSULE/tmp').mkdir(parents=True, exist_ok=True)

class Contract(unittest.TestCase):
    def test_sources(self):
        dockerfile = (ROOT / 'docker/vm/Dockerfile').read_text()
        self.assertRegex(dockerfile.splitlines()[1], r'^FROM ubuntu:24\.04@sha256:(OPERATOR_DIGEST|[0-9a-f]{64})$')
        self.assertIn('sha256sum -c SHA256SUMS', dockerfile)
        self.assertIn('USER 10001:10001', dockerfile)
        for shim in ('/usr/bin/xdotool', '/usr/bin/python3', '/usr/local/libexec/srf-fbread'):
            self.assertIn(shim, dockerfile)
        self.assertNotIn('apt-get', dockerfile)
        recipe = json.loads((ROOT / 'docker/vm/recipe.json').read_text())
        self.assertEqual(recipe['schema'], 'opensuperficies.vm-image-recipe/v1')
        self.assertEqual(recipe['image'], 'opensuperficies-vm-arm64:0.1.0')

    def test_printer_fail_closed(self):
        spec = importlib.util.spec_from_file_location('vm_print', ROOT / 'scripts/vm/print_build.py')
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        for digest in ('', 'AUSENTE', 'sha256:' + 'g' * 64):
            with self.assertRaises(m.Refused):
                m.base_digest(digest)
        with self.assertRaises(m.Refused):
            m.parse('{"files":{},"files":{}}')
        with self.assertRaises(m.Refused):
            m.approved({}, '0' * 64)
        with tempfile.TemporaryDirectory(dir=ROOT / 'build/room_CAPSULE/tmp') as d:
            root = Path(d)
            (root / 'docker/vm/vendor').mkdir(parents=True)
            with self.assertRaises(m.Refused):
                m.command(root, {'schema': 'opensuperficies.vm-image-recipe/v1'})

    def test_nominal_and_tamper(self):
        import hashlib
        import struct
        spec = importlib.util.spec_from_file_location('vm_print_nominal', ROOT / 'scripts/vm/print_build.py')
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        with tempfile.TemporaryDirectory(dir=ROOT / 'build/room_CAPSULE/tmp') as directory:
            root = Path(directory)
            def put(name, data):
                p = root / name
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(data)
            for name in m.SOURCES + m.ARTIFACTS:
                put(name, b'fake input')
            put('go.mod', b'module offline.test')
            put('go.sum', b'')
            for name in ('io.go', 'ax.go', 'downloads.go', 'record.go'):
                put('src/browserid/surface/' + name, b'package surface')
            base = 'sha256:' + 'a' * 64
            put('docker/vm/vendor/DIGEST', (base + '\n').encode())
            put('docker/vm/Dockerfile', ('# pinned\nFROM ubuntu:24.04@' + base + '\n').encode())
            payload = b'fake deb'
            put('docker/vm/vendor/qemu.deb', payload)
            put('docker/vm/vendor/SHA256SUMS', (hashlib.sha256(payload).hexdigest() + '  qemu.deb\n').encode())
            guest = b'fake guest'
            put('build/vm_guest/Image', guest)
            put('build/vm_guest/root.raw', guest)
            sha = hashlib.sha256(guest).hexdigest()
            put('build/vm_guest/MANIFEST.sha256', (sha + '  Image\n' + sha + '  root.raw\n').encode())
            elf = bytearray(120)
            elf[:6] = b'\x7fELF\x02\x01'
            struct.pack_into('<H', elf, 18, 183)
            struct.pack_into('<Q', elf, 32, 64)
            struct.pack_into('<HH', elf, 54, 56, 1)
            struct.pack_into('<I', elf, 64, 1)
            put(m.ARTIFACTS[0], elf)
            recipe = dict(schema=m.SCHEMA, image=m.IMAGE, platform='linux/arm64',
                          approval_path=m.APPROVAL, base_image='ubuntu:24.04@' + base,
                          files={n: m.digest(root, n) for n in m.inputs(root)})
            self.assertEqual(m.command(root, recipe)[0:3], ['docker', 'build', '--pull=false'])
            from types import SimpleNamespace
            m.verify_base(root, runner=lambda *a, **kw: SimpleNamespace(returncode=0, stdout='arm64 linux'))
            with self.assertRaises(m.Refused):
                m.verify_base(root, runner=lambda *a, **kw: SimpleNamespace(returncode=0, stdout='amd64 linux'))
            approval = dict(schema='os.cu.builder-approval/v1', mode='per_build', image=m.IMAGE,
                            arch='aarch64', recipe_sha256='0' * 64, approval_id='b2-1234', operator_note='offline fixture')
            m.approved(approval, '0' * 64)
            with self.assertRaises(m.Refused):
                m.approved(approval, '1' * 64)
            put('docker/vm/vendor/qemu.deb', b'tampered')
            with self.assertRaises(m.Refused):
                m.command(root, recipe)
            put('docker/vm/vendor/qemu.deb', payload)
            struct.pack_into('<I', elf, 64, 3)
            put(m.ARTIFACTS[0], elf)
            recipe['files'][m.ARTIFACTS[0]] = m.digest(root, m.ARTIFACTS[0])
            with self.assertRaises(m.Refused):
                m.command(root, recipe)

if __name__ == '__main__':
    result = unittest.main(exit=False)
    raise SystemExit(0 if result.result.wasSuccessful() else 2)
