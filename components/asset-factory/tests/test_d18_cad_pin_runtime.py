"""Exercise portable native CAD pins without requiring a CAD installation."""

from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from asset_factory import core
from tools.cad import native_qualify
from tools.cad.contracts import Refusal


class RuntimeIdentityTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.runtime = self.base / 'runtime'
        self.python = self.runtime / 'factory-cad/bin/python'
        self.python.parent.mkdir(parents=True)
        self.binary = self.runtime / 'platform-python'
        self.binary.write_bytes(b'approved interpreter bytes')
        self.python.symlink_to(self.binary)
        self.freeze = self.runtime / 'factory-cad.freeze.txt'
        self.freeze.write_text('build123d==0.13.0\n')
        self.api = self.runtime / 'factory-cad/api.py'
        self.api.write_bytes(b'approved API source')
        self.manifest = {
            'schema': 'af.cad-runtime/v1',
            'python': '$AF_RUNTIME_ROOT/factory-cad/bin/python',
            'python_sha256': core.sha256_file(self.binary),
            'freeze': {'path': '$AF_RUNTIME_ROOT/factory-cad.freeze.txt',
                       'sha256': core.sha256_file(self.freeze)},
            'api_sources': [{'path': '$AF_RUNTIME_ROOT/factory-cad/api.py',
                             'sha256': core.sha256_file(self.api)}],
            'installation': 'operator_supplied_offline_environment',
            'build123d': '0.13.0',
            'ocp_distribution': 'cadquery-ocp-novtk==8.0.1.1.0',
            'resource_ceiling_bytes': 8 * 2**30,
            'installation_recipe': 'AUSENTE',
            'wheel_hashes': 'AUSENTE',
        }
        checkout = self.base / 'factory'
        self.manifest_path = checkout / 'providers/build123d/runtime.json'
        self.manifest_path.parent.mkdir(parents=True)
        self.write_manifest()
        schema = checkout / 'tools/cad/runtime.schema.json'
        schema.parent.mkdir(parents=True)
        schema.write_bytes((native_qualify.ROOT / 'tools/cad/runtime.schema.json').read_bytes())
        self.enterContext(patch.object(native_qualify, 'ROOT', checkout))
        self.enterContext(patch.object(native_qualify.sys, 'executable', str(self.python)))
        self.enterContext(patch.object(native_qualify.metadata, 'version', return_value='0.13.0'))
        self.enterContext(patch.dict(os.environ, {'AF_RUNTIME_ROOT': str(self.runtime)}))

    def write_manifest(self):
        self.manifest_path.write_text(json.dumps(self.manifest))

    def test_parent_traversal_refused_even_if_target_stays_inside(self):
        for source, field, path in (
                (self.manifest, 'python', 'factory-cad/bin/../bin/python'),
                (self.manifest['api_sources'][0], 'path', 'factory-cad/bin/../api.py'),
                (self.manifest['freeze'], 'path', 'factory-cad/../factory-cad.freeze.txt')):
            with self.subTest(path=path):
                original = source[field]
                source[field] = '$AF_RUNTIME_ROOT/' + path
                self.write_manifest()
                self.assert_refusal(2, 'runtime_pin_invalid')
                source[field] = original

    def test_interpreter_symlink_escape_refused(self):
        outside = self.base / 'platform-python'
        outside.write_bytes(self.binary.read_bytes())
        self.python.unlink()
        self.python.symlink_to(outside)
        self.assert_refusal(2, 'runtime_venv_cfg_invalid')

    def external_interpreter(self):
        home = self.base / 'base-python'
        home.mkdir()
        binary = home / 'python3'
        binary.write_bytes(self.binary.read_bytes())
        self.python.unlink()
        self.python.symlink_to(binary)
        config = self.python.parent.parent / 'pyvenv.cfg'
        config.write_text(f'home = {home}\ninclude-system-site-packages = false\n')
        return home, binary, config

    def test_invalid_external_venv_configuration(self):
        home, binary, config = self.external_interpreter()
        for payload, rc, reason in (
                (None, 2, 'runtime_venv_cfg_invalid'),
                ('home = relative\n', 2, 'runtime_venv_cfg_invalid'),
                (f'home = {binary}\n', 2, 'runtime_venv_cfg_invalid'),
                (f'home = {home}\nnot-an-assignment\n', 2, 'runtime_venv_cfg_invalid'),
                (f'home = {home}\nhome = {home}\n', 2, 'runtime_venv_cfg_invalid'),
                (f'home = {home}/../base-python\n', 2, 'runtime_pin_invalid')):
            with self.subTest(payload=payload):
                if payload is None:
                    config.unlink()
                else:
                    config.write_text(payload)
                self.assert_refusal(rc, reason)

    def test_external_venv_configuration_must_be_regular_before_read(self):
        _, _, config = self.external_interpreter()
        config.unlink()
        config.mkdir()
        read = Path.read_text

        def unexpected_config_read(path, *args, **kwargs):
            self.assertNotEqual(path, config)
            return read(path, *args, **kwargs)

        with patch.object(Path, 'read_text', unexpected_config_read):
            self.assert_refusal(2, 'runtime_venv_cfg_invalid')

    def test_external_venv_configuration_invalid_utf8_is_typed(self):
        _, _, config = self.external_interpreter()
        config.write_bytes(b'home = /usr/bin\xff\n')
        self.assert_refusal(2, 'runtime_venv_cfg_invalid')

    def test_external_venv_configuration_symlink_is_refused(self):
        _, _, config = self.external_interpreter()
        outside = self.base / 'external.cfg'
        config.rename(outside)
        config.symlink_to(outside)
        self.assert_refusal(4, 'runtime_pin_escape')

    def test_contained_binary_still_refuses_an_escaped_venv_configuration(self):
        outside = self.base / 'external.cfg'
        outside.write_text('home = /usr/bin\n')
        config = self.python.parent.parent / 'pyvenv.cfg'
        config.symlink_to(outside)
        self.assert_refusal(4, 'runtime_pin_escape')

    def test_venv_directory_is_contained_even_if_bin_points_back_inside(self):
        venv = self.python.parent.parent
        external = self.base / 'external-venv'
        venv.rename(external)
        contained_bin = self.runtime / 'bin'
        (external / 'bin').rename(contained_bin)
        (external / 'bin').symlink_to(contained_bin, target_is_directory=True)
        config = self.runtime / 'pyvenv.cfg'
        config.write_text('home = /usr/bin\n')
        (external / 'pyvenv.cfg').symlink_to(config)
        source = self.runtime / 'api.py'
        (external / 'api.py').rename(source)
        (external / 'api.py').symlink_to(source)
        venv.symlink_to(external, target_is_directory=True)
        with patch.object(native_qualify.sys, 'executable', str(contained_bin / 'python')):
            self.assert_refusal(4, 'runtime_pin_escape')

    def test_external_venv_launcher_parent_is_contained(self):
        directory = self.python.parent
        external = self.base / 'bin'
        directory.rename(external)
        directory.symlink_to(external, target_is_directory=True)
        self.assert_refusal(4, 'runtime_pin_escape')

    def test_external_interpreter_must_be_directly_in_canonical_home(self):
        home, binary, config = self.external_interpreter()
        alias = self.base / 'home-alias'
        alias.symlink_to(home, target_is_directory=True)
        config.write_text(f'home = {alias}\n')
        identity = native_qualify.runtime_identity(self.freeze)
        self.assertEqual(identity['python'], self.manifest['python'])
        nested = home / 'nested'
        nested.mkdir()
        for directory in (nested, self.base):
            with self.subTest(directory=directory):
                alternative = directory / 'python-copy'
                alternative.write_bytes(binary.read_bytes())
                self.python.unlink()
                self.python.symlink_to(alternative)
                self.assert_refusal(4, 'runtime_pin_escape')

    def test_external_interpreter_bytes_must_match_pin(self):
        _, binary, _ = self.external_interpreter()
        binary.write_bytes(b'changed base interpreter bytes')
        self.assert_refusal(4, 'runtime_interpreter_digest_stale')

    def test_other_external_venv_cannot_reuse_the_same_base_interpreter(self):
        _, binary, _ = self.external_interpreter()
        other = self.runtime / 'other-venv/bin/python'
        other.parent.mkdir(parents=True)
        other.symlink_to(binary)
        with patch.object(native_qualify.sys, 'executable', str(other)):
            self.assert_refusal(4, 'runtime_interpreter_stale')

    def test_non_python_pins_cannot_use_external_home(self):
        home, _, _ = self.external_interpreter()
        for local in (self.api, self.freeze):
            with self.subTest(local=local):
                outside = home / local.name
                local.rename(outside)
                local.symlink_to(outside)
                try:
                    self.assert_refusal(4, 'runtime_pin_escape')
                finally:
                    local.unlink()
                    outside.rename(local)

    def test_api_symlink_escape_refused(self):
        outside = self.base / 'api.py'
        self.api.rename(outside)
        self.api.symlink_to(outside)
        self.assert_refusal(4, 'runtime_pin_escape')

    def test_freeze_pin_symlink_escape_refused(self):
        outside = self.base / 'freeze.txt'
        self.freeze.rename(outside)
        self.freeze.symlink_to(outside)
        self.assert_refusal(4, 'runtime_pin_escape')

    def test_supplied_in_root_freeze_symlink_escape_refused(self):
        outside = self.base / 'freeze-copy.txt'
        outside.write_bytes(self.freeze.read_bytes())
        supplied = self.runtime / 'freeze-copy.txt'
        supplied.symlink_to(outside)
        with self.assertRaisesRegex(Refusal, '^runtime_pin_escape$'):
            native_qualify.runtime_identity(supplied)

    def assert_refusal(self, rc, code):
        with self.assertRaises(Refusal) as caught:
            native_qualify.runtime_identity(self.freeze)
        self.assertEqual((caught.exception.rc, caught.exception.code), (rc, code))

    def test_portable_runtime_preserves_venv_identity(self):
        identity = native_qualify.runtime_identity(self.freeze)
        self.assertEqual(identity['python'], self.manifest['python'])
        self.assertEqual(identity['api_sources'], self.manifest['api_sources'])

    def test_canonical_root_alias(self):
        alias = self.base / 'alias'
        alias.symlink_to(self.runtime, target_is_directory=True)
        os.environ['AF_RUNTIME_ROOT'] = str(alias)
        with patch.object(native_qualify.sys, 'executable',
                          str(alias / 'factory-cad/bin/python')):
            identity = native_qualify.runtime_identity(self.freeze)
        self.assertEqual(identity['python'], self.manifest['python'])

    def test_external_freeze_copy_is_refused_even_with_matching_bytes(self):
        copy = self.base / 'freeze-copy.txt'
        copy.write_bytes(self.freeze.read_bytes())
        with self.assertRaisesRegex(Refusal, '^runtime_pin_escape$') as caught:
            native_qualify.runtime_identity(copy)
        self.assertEqual(caught.exception.rc, 4)

    def test_receipt_freeze_label_is_independent_of_supplied_copy(self):
        copy = self.runtime / 'freeze-copy.txt'
        copy.write_bytes(self.freeze.read_bytes())
        identity = native_qualify.runtime_identity(copy)
        self.assertEqual(identity['freeze'], self.manifest['freeze'])
        self.assertEqual(identity, native_qualify.runtime_identity(self.freeze))
        self.assertNotIn(str(self.base), json.dumps(identity))

    def test_unset_root_refused(self):
        del os.environ['AF_RUNTIME_ROOT']
        self.assert_refusal(3, 'runtime_root_unavailable')

    def test_empty_root_refused(self):
        os.environ['AF_RUNTIME_ROOT'] = ''
        self.assert_refusal(3, 'runtime_root_unavailable')

    def test_relative_root_refused(self):
        os.environ['AF_RUNTIME_ROOT'] = os.path.relpath(self.runtime)
        self.assert_refusal(2, 'runtime_root_not_absolute')

    def test_non_directory_root_refused(self):
        os.environ['AF_RUNTIME_ROOT'] = str(self.freeze)
        self.assert_refusal(3, 'runtime_root_not_directory')

    def test_wrong_venv_refused_with_same_binary(self):
        alternative = self.python.with_name('other-python')
        alternative.symlink_to(self.binary)
        with patch.object(native_qualify.sys, 'executable', str(alternative)):
            self.assert_refusal(4, 'runtime_interpreter_stale')

    def test_missing_interpreter_digest_refused(self):
        self.manifest.pop('python_sha256', None)
        self.write_manifest()
        self.assert_refusal(2, 'runtime_interpreter_digest_invalid')

    def test_invalid_interpreter_digest_refused(self):
        self.manifest['python_sha256'] = 'invalid'
        self.write_manifest()
        self.assert_refusal(2, 'runtime_interpreter_digest_invalid')

    def test_runtime_schema_requires_interpreter_digest(self):
        self.manifest.pop('python_sha256', None)
        schema = core.load_json(native_qualify.ROOT / 'tools/cad/runtime.schema.json', max_bytes=1024*1024)
        with self.assertRaises(core.ContractError) as caught:
            core.validate(self.manifest, schema)
        self.assertEqual((caught.exception.code, caught.exception.path),
                         ('missing_required', '$/python_sha256'))

    def test_wrong_runtime_record_interpreter_digest_refused(self):
        self.manifest['python_sha256'] = '0' * 64
        self.write_manifest()
        self.assert_refusal(4, 'runtime_interpreter_digest_stale')

    def test_wrong_interpreter_digest_refused(self):
        self.binary.write_bytes(b'changed interpreter bytes at the same path')
        self.assert_refusal(4, 'runtime_interpreter_digest_stale')

    def test_wrong_api_digest_refused(self):
        self.api.write_bytes(b'changed API source')
        self.assert_refusal(4, 'runtime_api_stale')

    def test_wrong_freeze_digest_refused(self):
        self.freeze.write_text('build123d==0\n')
        self.assert_refusal(4, 'runtime_freeze_stale')


class QualificationReceiptTests(unittest.TestCase):
    def test_failed_export_publishes_typed_reason_without_process_tail(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / 'qualification'
            tail = f'Unexpected diagnostic for {root / "input"}'
            stdout = io.StringIO()
            with (patch.object(native_qualify, 'runtime_identity', return_value={}),
                  patch.object(native_qualify.shutil, 'which', return_value='blender'),
                  patch.object(native_qualify, 'run', return_value={'rc': 5, 'tail': tail}),
                  redirect_stdout(stdout)):
                rc = native_qualify.main(['--out', str(output), '--freeze', str(root / 'freeze')])
            self.assertEqual(rc, 5)
            self.assertEqual(json.loads(stdout.getvalue())['reason_codes'], ['native_export_failed'])
            self.assertFalse(output.exists())

    def test_process_receipt_excludes_free_text(self):
        process = {'rc': 5, 'tail': 'Could not read /tmp/unavailable-input', 'wall_ms': 4}
        self.assertEqual(native_qualify._process_receipt(process), {'rc': 5, 'wall_ms': 4})

    def test_negative_receipt_rejects_free_text_in_reason_codes(self):
        tail = json.dumps({'rc': 5, 'peak_rss_bytes': 8192,
                           'reason_codes': ['Could not read /tmp/unavailable-input']})
        with self.assertRaisesRegex(Refusal, '^negative_control_receipt_invalid$'):
            native_qualify._negative_refusal(tail)
