#!/usr/bin/env python3
"""Offline guest recipe checks. Never invokes Docker, QEMU, a GUI or the network."""
import ast
import hashlib
import json
from pathlib import Path
import re
import subprocess
import struct
import uuid
import zlib
import sys
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[2]
SCRATCH = REPO / 'build/room_GUEST_BOOT'


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for data in iter(lambda: f.read(1024 * 1024), b''):
            h.update(data)
    return h.hexdigest()


ROOT_PARTUUID = 'c0ffee00-5a1e-4d0e-9e00-0e5f0a70c001'


def check_root_disk(path):
    """Validate GPT copies and partition bounds without loading the disk image."""
    with Path(path).open('rb') as disk:
        total = disk.seek(0, 2)
        if total % 512 or total < (2048 + 34) * 512:
            raise ValueError('invalid GPT disk size')
        disk.seek(0)
        mbr = disk.read(512)
        if mbr[510:512] != b'\x55\xaa' or mbr[450] != 0xee:
            raise ValueError('missing protective MBR')
        tables = []
        for lba, other, table_lba in ((1, total//512-1, 2),
                                    (total//512-1, 1, total//512-33)):
            disk.seek(lba * 512)
            header = bytearray(disk.read(512))
            if (header[:8] != b'EFI PART'
                    or struct.unpack_from('<II', header, 8) != (0x10000, 92)
                    or struct.unpack_from('<QQ', header, 24) != (lba, other)
                    or struct.unpack_from('<QII', header, 72) != (table_lba, 128, 128)):
                raise ValueError('invalid GPT header')
            checksum = struct.unpack_from('<I', header, 16)[0]
            header[16:20] = bytes(4)
            if zlib.crc32(header[:92]) != checksum:
                raise ValueError('GPT header checksum mismatch')
            disk.seek(table_lba * 512)
            table = disk.read(128 * 128)
            if zlib.crc32(table) != struct.unpack_from('<I', header, 88)[0]:
                raise ValueError('GPT entries checksum mismatch')
            tables.append(table)
        entries = tables[0]
        if (tables[1] != entries or entries[128:] != bytes(len(entries)-128)
                or uuid.UUID(bytes_le=entries[:16]) != uuid.UUID('0fc63daf-8483-4772-8e79-3d69d8477de4')
                or uuid.UUID(bytes_le=entries[16:32]) != uuid.UUID(ROOT_PARTUUID)):
            raise ValueError('invalid root partition identity')
        first, last = struct.unpack_from('<QQ', entries, 32)
        if first != 2048 or last < first or last != total//512-34:
            raise ValueError('invalid root partition bounds')
        return (last-first+1)*512


def check_artifacts(directory):
    directory = Path(directory)
    manifest = (directory / 'MANIFEST.sha256').read_text().splitlines()
    names = set()
    for line in manifest:
        match = re.fullmatch(r'([0-9a-f]{64})  (Image|root.raw)', line)
        if not match or match[2] in names:
            raise ValueError('invalid manifest entry')
        names.add(match[2])
        path = directory / match[2]
        if path.is_symlink() or digest(path) != match[1]:
            raise ValueError('artifact hash mismatch')
    if names != {'Image', 'root.raw'}:
        raise ValueError('incomplete manifest')
    provenance = json.loads((directory / 'PROVENANCE.json').read_text())
    if (provenance['schema'] != 'opensuperficies.vm-guest-provenance/v1'
            or provenance['network'] != 'none' or provenance['cgo_enabled'] is not False
            or provenance['arch'] != 'arm64' or provenance['root_readonly'] is not True
            or provenance['kernel_sha256'] != digest(directory / 'Image')
            or provenance['root_partuuid'] != ROOT_PARTUUID
            or provenance['root_fs_bytes'] != 3 * 1024**3
            or check_root_disk(directory / 'root.raw') != provenance['root_fs_bytes']
            or (directory / 'root.raw').stat().st_size != provenance['root_bytes']
            or (directory / 'root.raw').stat().st_mode & 0o222):
        raise ValueError('guest provenance or permissions mismatch')
    return provenance


class GuestRecipe(unittest.TestCase):
    def test_root_gpt_same_assembly_function(self):
        with tempfile.TemporaryDirectory(dir=SCRATCH) as tmp:
            fs, disk = Path(tmp) / 'fs', Path(tmp) / 'root.raw'
            blob = bytes(range(256)) * 16
            fs.write_bytes(blob)
            result = subprocess.run(['sh', '-c',
                '. "$1"; create_root_disk "$2" "$3"', 'gpt-test',
                str(REPO / 'docker/vm/guest/root_disk.sh'), str(fs), str(disk)],
                capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(check_root_disk(disk), len(blob))
            data = disk.read_bytes()
            self.assertEqual(data[510:512], b'\x55\xaa')
            self.assertEqual(data[450], 0xee)
            header = data[512:1024]
            self.assertEqual(header[:8], b'EFI PART')
            size, checksum = struct.unpack_from('<II', header, 12)
            zeroed = bytearray(header[:size])
            zeroed[16:20] = bytes(4)
            self.assertEqual(zlib.crc32(zeroed), checksum)
            backup = struct.unpack_from('<Q', header, 32)[0]
            self.assertEqual(data[backup*512:backup*512+8], b'EFI PART')
            table, count, stride, crc = struct.unpack_from('<QIII', header, 72)
            entries = data[table*512:table*512+count*stride]
            self.assertEqual(zlib.crc32(entries), crc)
            self.assertEqual(uuid.UUID(bytes_le=entries[:16]),
                             uuid.UUID('0fc63daf-8483-4772-8e79-3d69d8477de4'))
            self.assertEqual(uuid.UUID(bytes_le=entries[16:32]),
                             uuid.UUID('c0ffee00-5a1e-4d0e-9e00-0e5f0a70c001'))
            first, last = struct.unpack_from('<QQ', entries, 32)
            self.assertEqual(first, 2048)
            self.assertEqual((last-first+1)*512, len(blob))
            self.assertEqual(entries[stride:], bytes(len(entries)-stride))
            self.assertEqual(data[first*512:(last+1)*512], blob)
            for offset in (512, 512+16, table*512+16, backup*512, 510):
                corrupted = bytearray(data)
                corrupted[offset] ^= 1
                disk.write_bytes(corrupted)
                with self.assertRaises(ValueError):
                    check_root_disk(disk)
            fs.write_bytes(b'bad sector size')
            bad = subprocess.run(['sh', '-c', '. "$1"; create_root_disk "$2" "$3"',
                'gpt-test', str(REPO / 'docker/vm/guest/root_disk.sh'), str(fs),
                str(Path(tmp) / 'bad.raw')], capture_output=True, timeout=10)
            self.assertNotEqual(bad.returncode, 0)

    def test_shell_syntax_and_offline_contract(self):
        for relative in ('scripts/vm/build_guest_rootfs.sh', 'docker/vm/guest/assemble.sh',
                         'docker/vm/guest/root_disk.sh'):
            subprocess.run(['sh', '-n', str(REPO / relative)], check=True, timeout=5)
        script = (REPO / 'scripts/vm/build_guest_rootfs.sh').read_text()
        self.assertIn('CGO_ENABLED=0 GOOS=linux GOARCH=arm64', script)
        self.assertIn('GOPROXY=off GOSUMDB=off GOTOOLCHAIN=local', script)
        self.assertIn('docker create --network none "$base_id"', script)
        self.assertIn('docker run --rm --network none --cpuset-cpus 10-14', script)
        self.assertNotRegex(script, r'(?m)^\s*(?:docker pull|apt-get|curl|wget)\b')
        self.assertIn('mv -T "$work/stage-out" "$out"', script)
        assembly = (REPO / 'docker/vm/guest/assemble.sh').read_text()
        self.assertIn('truncate -s 3G', assembly)
        self.assertIn('-d /stage', assembly)
        self.assertIn('/stage/init', assembly)
        self.assertIn('/stage/usr/local/bin/srf-guest-agent', assembly)
        self.assertIn('e2fsck', assembly)

    def test_adapted_entrypoint_retains_program_mapping(self):
        script = (REPO / 'scripts/vm/build_guest_rootfs.sh').read_text()
        adaptation = script.split("<<'ADAPT'\n", 1)[1].split('\nADAPT', 1)[0]
        # Execute only the isolated host adaptation against disposable files.
        with tempfile.TemporaryDirectory(dir=SCRATCH) as tmp:
            result = subprocess.run([sys.executable, '-I', '-', str(REPO), tmp],
                                    input=adaptation, text=True, timeout=5,
                                    capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            entry = (Path(tmp) / 'identity-entrypoint.sh').read_text()
            self.assertIn("PROGRAMS['notepad-vm'] = PROGRAMS['notepad']", entry)
            python = entry.split("<<'PYTHON'\n", 1)[1].rsplit('\nPYTHON', 1)[0]
            ast.parse(python)
            # The taken-XID rule is identical in the guest copy.
            original = (REPO / 'docker/program/entrypoint.sh').read_text()
            for line in original.splitlines():
                if 'taken =' in line or 'if xid not in taken' in line:
                    self.assertIn(line, entry)
            self.assertNotIn("'/srfview'", entry)
            xpra = (Path(tmp) / 'identity-xpra-layer.sh').read_text()
            self.assertIn('XPRA_SOCKET=/run/srfview/xpra.sock', xpra)
            self.assertNotIn('/srfview', xpra.replace('/run/srfview', ''))

    def test_incomplete_manifest_refused(self):
        with tempfile.TemporaryDirectory(dir=SCRATCH) as tmp:
            root = Path(tmp)
            (root / 'MANIFEST.sha256').write_text('')
            with self.assertRaises(ValueError):
                check_artifacts(root)

    def test_corrupt_and_traversal_manifest_refused(self):
        for line in ('0' * 64 + '  Image\n', '0' * 64 + '  ../Image\n'):
            with tempfile.TemporaryDirectory(dir=SCRATCH) as tmp:
                root = Path(tmp)
                (root / 'Image').write_bytes(b'kernel')
                (root / 'MANIFEST.sha256').write_text(line)
                with self.assertRaises(ValueError):
                    check_artifacts(root)


if __name__ == '__main__':
    SCRATCH.mkdir(parents=True, exist_ok=True)
    if len(sys.argv) == 3 and sys.argv[1] == '--artifacts':
        try:
            check_artifacts(sys.argv[2])
        except (OSError, ValueError, KeyError) as exc:
            print('AUSENTE — invalid guest artifacts: ' + str(exc), file=sys.stderr)
            sys.exit(3)
        print('guest artifacts verified')
    elif len(sys.argv) == 1:
        result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(GuestRecipe))
        sys.exit(0 if result.wasSuccessful() else 3)
    else:
        print('AUSENTE — invalid guest test arguments', file=sys.stderr)
        sys.exit(2)
