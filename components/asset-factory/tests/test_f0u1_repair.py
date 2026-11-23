"""Regression controls for Unicode and invalid-path refusal boundaries."""

import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from asset_factory import core, contracts
from test_f0u1_contracts import provider, request


ROOT = Path(__file__).resolve().parents[1]


class RepairTests(unittest.TestCase):
    def assert_refusal(self, record):
        self.assertEqual(record['rc'], 2)
        self.assertEqual(record['status'], 'ERROR')
        self.assertEqual(record['reason_codes'], ['schema_mismatch'])
        self.assertIsInstance(record['path'], str)
        record['path'].encode('utf-8')
        encoded = core.canonical_json(record)
        self.assertEqual(json.loads(encoded), record)
        return encoded

    def assert_child_refusal(self, completed):
        self.assertEqual(completed.returncode, 2, completed.stderr)
        self.assertEqual(completed.stderr, b'')
        record = json.loads(completed.stdout)
        self.assert_refusal(record)
        expected = json.dumps(record, sort_keys=True, separators=(',', ':'),
                              ensure_ascii=False, allow_nan=False).encode('utf-8') + b'\n'
        self.assertEqual(completed.stdout, expected)

    def test_surrogate_strings_and_keys_rejected_by_load_and_validate(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'input.json'
            for surrogate in ('\ud800', '\udfff'):
                for payload in ({'label': surrogate}, {surrogate: 'label'},
                                {'nested': [surrogate]}, {'nested': {surrogate: 0}}):
                    with self.subTest(payload=repr(payload)):
                        raw = json.dumps(payload, ensure_ascii=True).encode('ascii')
                        source.write_bytes(raw)
                        with self.assertRaises(core.ContractError) as caught:
                            core.load_json(source, max_bytes=4096)
                        self.assert_refusal(caught.exception.as_dict())
                        with self.assertRaises(core.ContractError) as caught:
                            core.validate(payload, {'type': 'object'})
                        self.assert_refusal(caught.exception.as_dict())
                        self.assertEqual(source.read_bytes(), raw)

    def test_surrogates_rejected_in_slot_and_provider_metadata(self):
        for surrogate in ('\ud800', '\udfff'):
            for field in ('value', 'key'):
                payload = request()
                payload['params'] = {'label': surrogate} if field == 'value' else {surrogate: 0}
                with self.subTest(surface='slot', field=field, surrogate=repr(surrogate)):
                    with self.assertRaises(core.ContractError) as caught:
                        contracts.validate_slot(payload, 'generate', 'request')
                    self.assert_refusal(caught.exception.as_dict())
            for field in ('id', 'known_issues', 'unknown_key'):
                payload = provider()
                if field == 'unknown_key':
                    payload[surrogate] = True
                else:
                    payload[field] = [surrogate] if field == 'known_issues' else surrogate
                with self.subTest(surface='provider', field=field, surrogate=repr(surrogate)):
                    with self.assertRaises(core.ContractError) as caught:
                        contracts.validate_provider(payload)
                    self.assert_refusal(caught.exception.as_dict())

    def test_duplicate_surrogate_keys_have_serializable_refusal(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'input.json'
            for escape in (b'\\ud800', b'\\udfff'):
                with self.subTest(escape=escape):
                    raw = b'{"' + escape + b'":0,"' + escape + b'":1}'
                    source.write_bytes(raw)
                    with self.assertRaises(core.ContractError) as caught:
                        core.load_json(source, max_bytes=4096)
                    self.assert_refusal(caught.exception.as_dict())
                    self.assertEqual(source.read_bytes(), raw)

    def test_surrogate_diagnostic_paths_emit_canonical_refusal(self):
        for surrogate in ('\ud800', '\udfff'):
            with self.subTest(surrogate=repr(surrogate)):
                error = core.ContractError('schema_mismatch', '$/' + surrogate)
                record = error.as_dict()
                expected = self.assert_refusal(record)
                with mock.patch('sys.stdout', new_callable=io.StringIO) as output:
                    self.assertEqual(core.emit(record), 2)
                self.assertEqual(output.getvalue(), expected)

    def test_cli_rejects_surrogate_values_keys_and_duplicates(self):
        cases = []
        for surrogate in ('\ud800', '\udfff'):
            for params in ({'label': surrogate}, {surrogate: 0}):
                payload = request()
                payload['params'] = params
                cases.append((json.dumps(payload, ensure_ascii=True).encode('ascii'),
                              ['--slot', 'generate', '--kind', 'request', '--in']))
            for field in ('known_issues', 'unknown_key'):
                payload = provider()
                if field == 'unknown_key':
                    payload[surrogate] = True
                else:
                    payload[field] = [surrogate]
                cases.append((json.dumps(payload, ensure_ascii=True).encode('ascii'), ['--provider']))
            key = json.dumps(surrogate).encode('ascii')
            cases.append((b'{' + key + b':0,' + key + b':1}', ['--provider']))
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'input.json'
            for raw, arguments in cases:
                with self.subTest(raw=raw):
                    source.write_bytes(raw)
                    completed = subprocess.run(
                        [sys.executable, '-B', '-m', 'asset_factory.contracts', *arguments, str(source)],
                        cwd=ROOT, capture_output=True, timeout=10)
                    self.assert_child_refusal(completed)
                    self.assertEqual(source.read_bytes(), raw)

    def test_valid_unicode_roundtrip_validates_and_hashes(self):
        payload = request()
        payload['params'] = {'label': 'caf\u00e9 \U0001f642', '\u03bb': True}
        raw = json.dumps(payload, ensure_ascii=True).encode('ascii')
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'input.json'
            source.write_bytes(raw)
            loaded = core.load_json(source, max_bytes=4096)
            contracts.validate_slot(loaded, 'generate', 'request')
            self.assertEqual(loaded, payload)
            expected = json.dumps(payload, sort_keys=True, separators=(',', ':'),
                                  ensure_ascii=False, allow_nan=False).encode('utf-8') + b'\n'
            self.assertEqual(core.canonical_json(loaded).encode('utf-8'), expected)
            self.assertEqual(core.sha256_json(loaded), hashlib.sha256(expected).hexdigest())
            completed = subprocess.run(
                [sys.executable, '-B', '-m', 'asset_factory.contracts', '--slot', 'generate',
                 '--kind', 'request', '--in', str(source)], cwd=ROOT,
                capture_output=True, timeout=10)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(completed.stdout, b'{"rc":0,"status":"OK"}\n')
            self.assertEqual(completed.stderr, b'')
            self.assertEqual(source.read_bytes(), raw)

    def test_sha256_file_invalid_paths_emit_typed_refusal(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'input.json'
            source.write_bytes(b'caller bytes')
            for suffix in ('\x00', '\ud800'):
                with self.subTest(suffix=repr(suffix)):
                    with self.assertRaises(core.ContractError) as caught:
                        core.sha256_file(Path(str(source) + suffix))
                    self.assert_refusal(caught.exception.as_dict())
                    self.assertEqual(source.read_bytes(), b'caller bytes')
                    self.assertEqual(list(Path(directory).iterdir()), [source])

    def test_atomic_write_invalid_paths_preserve_bytes_and_clean_staging(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / 'output.json'
            target.write_bytes(b'caller bytes')
            for invalid in (Path(str(target) + '\x00'), root / 'invalid\x00' / 'output.json',
                            Path(str(target) + '\ud800')):
                with self.subTest(path=repr(str(invalid))):
                    with self.assertRaises(core.ContractError) as caught:
                        core.atomic_write_json(invalid, {'rc': 0, 'status': 'OK'})
                    self.assert_refusal(caught.exception.as_dict())
                    self.assertEqual(target.read_bytes(), b'caller bytes')
                    self.assertEqual(list(root.iterdir()), [target])

    def test_publication_value_error_preserves_bytes_and_cleans_staging(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / 'output.json'
            target.write_bytes(b'caller bytes')
            with mock.patch.object(core.os, 'replace', side_effect=ValueError('embedded null byte')):
                with self.assertRaises(core.ContractError) as caught:
                    core.atomic_write_json(target, {'rc': 0, 'status': 'OK'})
            self.assert_refusal(caught.exception.as_dict())
            self.assertEqual(target.read_bytes(), b'caller bytes')
            self.assertEqual(list(root.iterdir()), [target])

    def test_invalid_path_consumer_emits_rc2(self):
        program = '''
import sys
from pathlib import Path
from asset_factory import core
try:
    path = Path(sys.argv[2] + "\\0")
    if sys.argv[1] == "sha256_file":
        core.sha256_file(path)
    else:
        core.atomic_write_json(path, {"rc": 0, "status": "OK"})
except core.ContractError as error:
    raise SystemExit(core.emit(error.as_dict()))
raise SystemExit(core.emit({"rc": 0, "status": "OK"}))
'''
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'output.json'
            target.write_bytes(b'caller bytes')
            for helper in ('sha256_file', 'atomic_write_json'):
                with self.subTest(helper=helper):
                    completed = subprocess.run(
                        [sys.executable, '-B', '-c', program, helper, str(target)],
                        cwd=ROOT, capture_output=True, timeout=10)
                    self.assert_child_refusal(completed)
                    self.assertEqual(target.read_bytes(), b'caller bytes')
                    self.assertEqual(list(Path(directory).iterdir()), [target])


if __name__ == '__main__':
    unittest.main()
