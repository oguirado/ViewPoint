"""Regressions for real offline CUDA package preparation (no network)."""
import io
import unittest
from unittest.mock import patch
import zipfile

from providers.trellis2.build import build


def wheel_bytes(*, payload_bytes=8192, restricted=False):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('largecuda-1.dist-info/METADATA', 'Name: largecuda\nVersion: 1\n')
        archive.writestr('largecuda/kernels.so', b'0' * payload_bytes)
        if restricted:
            archive.writestr('nvdiffrast/__init__.py', '')
    return stream.getvalue()


class PackageBudgetTests(unittest.TestCase):
    item = {'path': 'largecuda-1-py3-none-any.whl', 'requirement': 'largecuda==1'}

    def test_cuda_package_budget_is_independent_of_source_archive_budget(self):
        # A compressed native package can legitimately exceed a source archive.
        # Keep the source budget small to exercise this distinction cheaply.
        with patch.object(build, 'MAX_BYTES', 4096):
            build.inspect_package(wheel_bytes(), self.item, 'wheels')

    def test_expanded_package_still_has_a_finite_budget(self):
        with patch.object(build, 'MAX_PACKAGE_BYTES', 4096, create=True):
            with self.assertRaises(build.Refusal) as caught:
                build.inspect_package(wheel_bytes(), self.item, 'wheels')
        self.assertEqual(caught.exception.rc, 6)
        self.assertEqual(caught.exception.reason, 'package_budget')

    def test_large_package_does_not_bypass_restricted_payload_check(self):
        with self.assertRaises(build.Refusal) as caught:
            build.inspect_package(wheel_bytes(restricted=True), self.item, 'wheels')
        self.assertEqual((caught.exception.rc, caught.exception.reason), (5, 'restricted_dependency'))


class DebianZstdTests(unittest.TestCase):
    @staticmethod
    def compressed(data):
        import ctypes
        import ctypes.util
        lib = ctypes.CDLL(ctypes.util.find_library('zstd'))
        lib.ZSTD_compressBound.argtypes = [ctypes.c_size_t]
        lib.ZSTD_compressBound.restype = ctypes.c_size_t
        output = ctypes.create_string_buffer(lib.ZSTD_compressBound(len(data)))
        lib.ZSTD_compress.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int]
        lib.ZSTD_compress.restype = ctypes.c_size_t
        size = lib.ZSTD_compress(output, len(output), data, len(data), 1)
        return output.raw[:size]

    @classmethod
    def deb(cls, restricted=False):
        import tarfile
        def tar(name, data):
            output = io.BytesIO()
            with tarfile.open(fileobj=output, mode='w') as archive:
                info = tarfile.TarInfo(name)
                info.size = len(data)
                archive.addfile(info, io.BytesIO(data))
            return cls.compressed(output.getvalue())
        members = [('debian-binary', b'2.0\n'),
                   ('control.tar.zst', tar('./control', b'Package: safe\nVersion: 1\nArchitecture: arm64\n')),
                   ('data.tar.zst', tar('./usr/lib/nvdiffrast/hidden.so' if restricted else './usr/lib/safe.so', b'payload'))]
        result = b'!<arch>\n'
        for name, payload in members:
            header = f'{name+"/":<16}{0:<12}{0:<6}{0:<6}{"100644":<8}{len(payload):<10}`\n'.encode()
            result += header + payload + (b'\n' if len(payload)%2 else b'')
        return result

    def test_official_ubuntu_zstd_package_is_inspected(self):
        build.inspect_package(self.deb(), {'path': 'safe.deb'}, 'debs/runtime')

    def test_ubuntu_zstd_restricted_payload_is_refused(self):
        with self.assertRaises(build.Refusal) as caught:
            build.inspect_package(self.deb(True), {'path': 'safe.deb'}, 'debs/runtime')
        self.assertEqual((caught.exception.rc, caught.exception.reason), (5, 'restricted_dependency'))

    def test_ubuntu_zstd_expansion_budget_is_enforced(self):
        with patch.object(build, 'MAX_PACKAGE_BYTES', 4096):
            with self.assertRaises(build.Refusal) as caught:
                build.inspect_package(self.deb(), {'path': 'safe.deb'}, 'debs/runtime')
        self.assertEqual((caught.exception.rc, caught.exception.reason), (6, 'package_budget'))


if __name__ == '__main__':
    unittest.main()
