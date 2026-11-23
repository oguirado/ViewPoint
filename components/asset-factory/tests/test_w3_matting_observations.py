"""Qualification integrity controls; synthetic alpha is not model-quality proof."""
import os
import unittest
from unittest import mock

from PIL import Image
from providers.birefnet import observations, offline, worker


class ObservationTests(unittest.TestCase):
    def test_cpu_visibility_refusal_precedes_torch_import(self):
        for env in ({}, {'CUDA_VISIBLE_DEVICES': '0'}):
            with mock.patch.dict(os.environ, env, clear=True):
                with self.assertRaises(offline.Issue) as caught:
                    worker.infer('birefnet', None, None, {})
                self.assertEqual((caught.exception.rc, caught.exception.code),
                                 (5, 'cuda_visibility_refused'))

    def test_opaque_negative_then_measured_alpha_stays_cannot_see(self):
        source = Image.new('RGBA', (3, 3), (12, 34, 56, 255))
        candidate = source.copy()
        with self.assertRaises(offline.Issue) as caught:
            observations.alpha_observations(source, candidate)
        self.assertEqual(caught.exception.code, 'constant_alpha')
        candidate.putalpha(Image.new('L', (3, 3), 0))
        candidate.putpixel((1, 1), (12, 34, 56, 255))
        result = observations.alpha_observations(source, candidate)
        self.assertEqual(result['metrics']['foreground_at_midpoint_ppm'], 111111)
        self.assertEqual(result['metrics']['border_at_midpoint_ppm'], 0)
        self.assertIsNone(result['metrics']['alpha_iou_ppm'])
        self.assertEqual(result['quality_verdict'], 'CANNOT_SEE')

    def test_border_counts_unique_pixels_and_detects_leak(self):
        source = Image.new('RGBA', (3, 3), (12, 34, 56, 255))
        candidate = source.copy()
        candidate.putalpha(Image.new('L', (3, 3), 0))
        candidate.putpixel((0, 0), (12, 34, 56, 128))
        self.assertEqual(observations.alpha_observations(source, candidate)
                         ['metrics']['border_at_midpoint_ppm'], 125000)

    def test_rgb_mutation_is_rejected(self):
        source = Image.new('RGBA', (3, 3), (12, 34, 56, 255))
        candidate = source.copy()
        candidate.putpixel((1, 1), (0, 0, 0, 0))
        with self.assertRaises(offline.Issue) as caught:
            observations.alpha_observations(source, candidate)
        self.assertEqual(caught.exception.code, 'source_rgb_changed')
