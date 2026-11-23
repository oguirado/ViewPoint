"""A runtime probe must work when installed source is read-only."""
import contextlib
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from providers.trellis2.build import runtime_probe as probe


class ProbeFilesystemTests(unittest.TestCase):
    def test_compiler_check_uses_writable_scratch_not_installed_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); installed=root/'installed';installed.mkdir();installed.chmod(0o555)
            binary=root/'ptxas';binary.write_text('#!/bin/sh\nprintf "ptxas release 12.9\\n"\n');binary.chmod(0o755)
            marker=root/'arch';marker.write_text('sm_121\n')
            called=[]; real=probe.run_owned
            def observed(*args,**kwargs):
                output=real(*args,**kwargs);called.append((Path(kwargs['cwd']),output));return output
            try:
                with patch.object(probe,'__file__',str(installed/'runtime_probe.py')), \
                     patch.object(probe,'read_json',return_value={'compiler_sha256':hashlib.sha256(binary.read_bytes()).hexdigest()}), \
                     patch.object(probe,'run_owned',side_effect=observed), \
                     patch.object(probe.importlib,'import_module',side_effect=ImportError('no host CUDA fixture')), \
                     contextlib.redirect_stdout(io.StringIO()) as output:
                    rc=probe.main(['--ptxas',str(binary),'--cache-arch',str(marker)])
                self.assertTrue(called,'compiler execution never reached writable scratch')
                self.assertNotEqual(called[0][0],installed)
                self.assertIn(b'ptxas release 12.9',called[0][1])
                self.assertEqual(rc,3)
                self.assertEqual(json.loads(output.getvalue())['reason_codes'],['runtime_dependency_unavailable'])
            finally:installed.chmod(0o755)


if __name__=='__main__':unittest.main()
