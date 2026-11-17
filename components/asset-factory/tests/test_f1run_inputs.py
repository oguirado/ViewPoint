"""Measured-input gates and refusals for the authorized GB10 build."""
import copy
from pathlib import Path
import tempfile
import unittest
from providers.trellis2.build import build


class MeasuredInputTests(unittest.TestCase):
    def setUp(self):
        self.lock=build.read_json(build.HERE/'build-inputs.json')

    def test_measured_inputs_are_complete_without_executing_docker(self):
        build.check_inputs(self.lock)
        self.assertTrue(self.lock['wheels'])
        self.assertTrue(self.lock['debs']['builder'])
        self.assertTrue(self.lock['debs']['runtime'])

    def test_missing_compiler_and_packages_cannot_pass(self):
        for field in ('compiler_sha256','wheels'):
            with self.subTest(field=field):
                lock=copy.deepcopy(self.lock);lock[field]=None
                with self.assertRaises(build.Refusal) as caught:build.check_inputs(lock)
                self.assertEqual(caught.exception.rc,3)

    def test_floating_base_is_stale(self):
        lock=copy.deepcopy(self.lock);lock['bases']['builder']='nvcr.io/nvidia/cuda:latest'
        with self.assertRaises(build.Refusal) as caught:build.check_inputs(lock)
        self.assertEqual((caught.exception.rc,caught.exception.reason),(4,'base_unpinned'))

    def test_checksum_mismatch_stops_before_source_export_and_publication(self):
        lock=copy.deepcopy(self.lock);lock['wheels'][0]['sha256']='0'*64
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);output=root/'context';sentinel=root/'caller';sentinel.write_bytes(b'unchanged')
            with self.assertRaises(build.Refusal) as caught:
                build.prepare(lock,sources_root=Path('/home/admin/Documentos/The-Construct_v1/docker/trellis2/repos'),
                              packages_root=Path('/home/admin/Documentos/construct-rescate/build/f1run/packages'),output=output)
            self.assertEqual((caught.exception.rc,caught.exception.reason),(4,'dependency_stale'))
            self.assertFalse(output.exists())
            self.assertEqual(sentinel.read_bytes(),b'unchanged')


if __name__=='__main__':unittest.main()
