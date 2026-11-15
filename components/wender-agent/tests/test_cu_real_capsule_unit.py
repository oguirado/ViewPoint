"""Regression for the recorded Holdings capsule transport shape."""
import copy
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest

from tests.test_cu_loop_unit import Clock, Describer, proposal
from wender.learn.cu_loop import (
    SrfCapsule, ReplayDecider, _encode_png, run_loop,
)
from wender.learn.model_client import ActionValidationError, ModelClient, _prompt, validate_action


class TestRealCapsule(unittest.TestCase):
    def setUp(self):
        self.recording = json.loads((Path(__file__).parent / 'fixtures' /
            'real_capsule_frame_holdings_08d409a.json').read_text())
        self.tmp = tempfile.TemporaryDirectory(dir='/dev/shm')
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.frame = copy.deepcopy(self.recording['frame'])
        # The original pixels are ephemeral. Replace only the pixel evidence
        # with a freshly generated RAM image; retain the recorded lane verbatim.
        rgb = bytes(self.frame['width'] * self.frame['height'] * 3)
        image = root / 'frame.png'
        image.write_bytes(_encode_png(rgb, self.frame['width'], self.frame['height']))
        self.frame.update(path=str(image), sha256_rgb24=hashlib.sha256(rgb).hexdigest())
        self.response = root / 'response.json'
        # A real subprocess prints the capsule frame object, not the recording envelope.
        command = root / 'capsule'
        command.write_text(f'#!{sys.executable}\nfrom pathlib import Path\n'
                           f'print(Path({str(self.response)!r}).read_text())\n')
        command.chmod(0o700)
        self.capsule = SrfCapsule(self.frame['generation'], binary=str(command))
        self.write_response()

    def write_response(self):
        self.response.write_text(json.dumps(self.frame))

    def test_real_transport_normalizes_and_preserves_lane(self):
        frame = self.capsule.frame()
        self.assertEqual(frame['generation'], self.recording['frame']['generation'])
        self.assertIsInstance(frame['generation'], str)
        expected = copy.deepcopy(self.recording['frame']['state'])
        for element in expected['elements']:
            element.update(id=element['stable_id'], name=element['anchor'], bbox=None)
        self.assertEqual(frame['lane'], expected)
        self.assertNotIn('state', frame)
        self.assertEqual(self.frame['state'], self.recording['frame']['state'])

    def test_bad_state_digest_rejected(self):
        for digest in ['g' * 64, '0' * 63, None]:
            with self.subTest(digest=digest):
                self.frame['state']['state_sha256'] = digest
                self.write_response()
                with self.assertRaisesRegex(ValueError, 'SHA256'):
                    self.capsule.frame()

    def test_noninteger_tick_and_revision_rejected(self):
        for key in ('tick', 'revision'):
            for value in ('0', 0.5, True, None):
                with self.subTest(key=key, value=value):
                    self.frame['state'] = copy.deepcopy(self.recording['frame']['state'])
                    self.frame['state'][key] = value
                    self.write_response()
                    with self.assertRaisesRegex(ValueError, key):
                        self.capsule.frame()

    def test_geometry_and_ram_path_stay_strict(self):
        for key, value in (('width', self.frame['width'] + 1), ('height', True),
                           ('path', str(Path(__file__).resolve()))):
            with self.subTest(key=key):
                original = self.frame[key]
                self.frame[key] = value
                self.write_response()
                with self.assertRaises(ValueError):
                    self.capsule.frame()
                self.frame[key] = original

    # @anchor:wa.tests.test_cu_real_capsule_unit.TestRealCapsule.test_missing_bbox_is_observation_and_typed_grounding_failure
    def test_missing_bbox_is_observation_and_typed_grounding_failure(self):
        frame = self.capsule.frame()
        elements = frame['lane']['elements']
        click = proposal('click', element_id=elements[0]['id'])
        with self.assertRaisesRegex(ActionValidationError, 'no bbox'):
            validate_action(click, frame['width'], frame['height'], elements)

        class CaptureBackend:
            def generate(inner, request):
                inner.request = request
                return proposal('done', expectation={'kind': 'none'})

        backend = CaptureBackend()
        describe = {'schema': 've.cu_describe/v1', 'width': frame['width'],
                    'height': frame['height']}
        ModelClient(backend).act('observe', frame['path'], describe, elements=elements)
        self.assertEqual(backend.request['elements'], [
            {key: e[key] for key in ('id', 'name', 'role', 'actionable')} for e in elements])
        self.assertIn('not clickable element_id targets', _prompt(backend.request))
        # A fabricated model click is rejected through ModelClient as well.
        backend.generate = lambda request: click
        with self.assertRaisesRegex(ActionValidationError, 'no bbox'):
            ModelClient(backend).act('observe', frame['path'], describe, elements=elements)

        clock = Clock()
        summary = run_loop(self.capsule, Describer(), ReplayDecider([click]),
                           goal='observe', trace=io.StringIO(), clock=clock,
                           sleep=clock.sleep, max_steps=1)
        self.assertEqual(summary['failure']['stage'], 'validation')
        self.assertEqual(summary['failure']['error_class'], 'ActionValidationError')
        self.assertFalse(summary['failure']['input_effect_unknown'])
