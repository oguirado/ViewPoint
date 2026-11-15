# @module {"id":"wa.perceive.vision_bridge_vserve_tests","layer":2,"owner":"PERCEIVE","deps":["wa.perceive.vision_bridge"],"book":"HB-WA-s1-perceive"}
"""Opt-in bridge parity, failure reporting, and custody controls."""
import hashlib
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

from tests.test_vserve_client_unit import ServerFixture
from wender.perceive import vision_bridge as bridge


class BridgeVServeTests(ServerFixture):
    def engine(self):
        engine = bridge.select_vision_engine('persistent', ve_root=self.root, python=sys.executable)
        self.addCleanup(engine.close)
        return engine

    def test_explicit_backend_raw_and_contract(self):
        self.assertIsInstance(bridge.select_vision_engine(), bridge.FakeVisionEngine)
        engine = self.engine()
        result = engine.describe(self.frame)
        self.assertEqual(result.frame_sha256, hashlib.sha256(self.frame.data).hexdigest())
        self.assertEqual((result.width, result.height), (2, 2))
        self.assertEqual(engine.last_transport, 'vserve')
        self.assertIsNone(engine.last_fallback_reason)
        self.assertIn('frame_raw', self.requests()[-1])
        self.assertFalse(engine.describe(self.frame, self.frame).changed)
        self.assertEqual(engine.screen_identity(self.frame), result.frame_sha256[:16])

    def test_configuration_never_falls_back(self):
        (self.root / 'vadapt/vserve.py').unlink()
        with patch.object(bridge.subprocess, 'run') as run:
            with self.assertRaises(ValueError):
                self.engine()
            run.assert_not_called()

    def test_runtime_fallback_reported(self):
        self.mode('badhello')
        engine = self.engine()
        row = dict(schema='ve.cu_describe/v1', frame_sha256=hashlib.sha256(self.frame.data).hexdigest(),
                   width=2, height=2, changed=None, changed_regions=[], crops=[], timing_us=1)
        completed = bridge._subprocess.CompletedProcess([], 0, json.dumps(row).encode(), b'')
        with patch.object(bridge.subprocess, 'run', return_value=completed) as run:
            with self.assertWarnsRegex(RuntimeWarning, 'falling back'):
                engine.describe(self.frame)
        self.assertEqual(engine.last_transport, 'c2')
        self.assertIn('hello', engine.last_fallback_reason)
        self.assertIn('vadapt.cu_describe', run.call_args.args[0])
        self.assertIsNone(engine._client._proc)

    def test_real_c2_stub_fallback(self):
        self.mode('badhello')
        (self.root / 'vadapt/cu_describe.py').write_text(r'''import hashlib, json, pathlib, sys
path = sys.argv[sys.argv.index('--frame') + 1]
assert pathlib.Path(path).read_bytes().startswith(b'\x89PNG\r\n\x1a\n')
print(json.dumps(dict(schema='ve.cu_describe/v1', frame_sha256=hashlib.sha256(bytes(range(12))).hexdigest(),
                     width=2, height=2, changed=None, changed_regions=[], crops=[], timing_us=1)))
''')
        engine = self.engine()
        with self.assertWarnsRegex(RuntimeWarning, 'falling back'):
            result = engine.describe(self.frame)
        self.assertEqual(result.frame_sha256, hashlib.sha256(self.frame.data).hexdigest())
        self.assertEqual(engine.last_transport, 'c2')

    def test_deadline_exhaustion_never_starts_fallback(self):
        self.mode('timeout')
        engine = self.engine()
        engine.timeout = engine._client.timeout = 0.2
        with patch.object(bridge.subprocess, 'run') as run:
            with self.assertWarns(RuntimeWarning):
                with self.assertRaisesRegex(bridge.VisionBridgeError, 'deadline expired'):
                    engine.describe(self.frame)
            run.assert_not_called()
        self.assertIsNone(engine._client._proc)

    def test_parser_caps_score_order_and_custody(self):
        for mode in ('caps', 'badorder', 'baddigest'):
            with self.subTest(mode=mode):
                self.mode(mode)
                with self.assertRaises(bridge.VisionBridgeError):
                    self.engine().describe(self.frame)

    def test_input_validation_before_spawn(self):
        engine = self.engine()
        for kwargs in ({'masks': [(0, 0, 0, 1)]}, {'masks': [(0, 0, 1)]}, {'ocr_marks': True},
                       {'marks_out': str(self.root / 'marks.png')}):
            with self.assertRaises(ValueError):
                engine.describe(self.frame, **kwargs)
        self.assertIsNone(engine._client._proc)

    def test_scope_still_refuses_unrelated_path(self):
        from tests.test_vision_bridge_ocr_unit import OCRTransportTests
        scope = OCRTransportTests()._scope
        paths = ['wender/perceive/vserve_client.py', 'tests/test_vserve_client_unit.py',
                 'tests/test_vision_bridge_vserve_unit.py']
        self.assertTrue(scope(paths, '', [], True))
        self.assertFalse(scope(paths + ['wender/learn/cu_loop.py'], '', [], True))

    def test_fallback_retains_vserve_cause(self):
        """MUT-CAUSE-LOST: C2 failure must leave the original vserve error inspectable."""
        from wender.perceive.vserve_client import VServeError
        for failure in ('oserror', 'timeout', 'exit', 'parse'):
            with self.subTest(failure=failure):
                engine = self.engine()
                original = VServeError('original transport failure', unavailable=True)
                completed = bridge._subprocess.CompletedProcess([], int(failure == 'exit'), b'bad json', b'failed')
                error = {'oserror': OSError('C2 launch'),
                         'timeout': bridge.subprocess.TimeoutExpired('C2', 1)}.get(failure)
                with patch.object(engine._client, 'describe', side_effect=original):
                    with patch.object(bridge.subprocess, 'run', side_effect=error, return_value=completed):
                        with self.assertWarns(RuntimeWarning):
                            with self.assertRaises(bridge.VisionBridgeError):
                                engine.describe(self.frame)
                self.assertIs(engine.last_vserve_error, original)
                self.assertEqual(engine.last_fallback_reason, str(original))

    def test_shm_failure_reaches_fallback(self):
        engine = self.engine()
        row = dict(schema='ve.cu_describe/v1', frame_sha256=hashlib.sha256(self.frame.data).hexdigest(),
                   width=2, height=2, changed=None, changed_regions=[], crops=[], timing_us=1)
        completed = bridge._subprocess.CompletedProcess([], 0, json.dumps(row).encode(), b'')
        with patch('wender.perceive.vserve_client.tempfile.mkstemp', side_effect=OSError('full')):
            with patch.object(bridge.subprocess, 'run', return_value=completed):
                with self.assertWarns(RuntimeWarning):
                    engine.describe(self.frame)
        self.assertEqual(engine.last_transport, 'c2')
        self.assertTrue(engine.last_vserve_error.unavailable)

    def test_custody_mismatch_is_terminal(self):
        self.mode('baddigest')
        engine = self.engine()
        with self.assertRaisesRegex(bridge.VisionBridgeError, 'custody'):
            engine.describe(self.frame)
        self.mode('ok')
        with patch.object(bridge.subprocess, 'run') as run:
            with self.assertRaisesRegex(bridge.VisionBridgeError, 'closed'):
                engine.describe(self.frame)
            run.assert_not_called()
        self.assertTrue(engine._client._closed)

    def test_adapter_advertises_bridge_persistent_mode(self):
        from wender.perceive import vision_adapter
        self.assertEqual(vision_adapter.PERSISTENT_MODE, bridge.PERSISTENT_MODE)
        self.assertEqual(vision_adapter.PERSISTENT_MODE, 'vserve')
