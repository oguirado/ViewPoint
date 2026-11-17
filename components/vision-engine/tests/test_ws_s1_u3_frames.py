"""RAM-only adapter checks. Pixel algorithms remain in the sealed engine."""
import hashlib
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from vadapt.sculpt_frames import pair_source, residual_source
from visioncore.errors import VqaError


class FrameTests(unittest.TestCase):
    def test_equal_bytes_equal_manifests_and_readonly_arrays(self):
        pixels = b"\x80" * (32 * 32 * 3)
        sha = hashlib.sha256(pixels).hexdigest()
        args = dict(width=32, height=32, reference_sha256=sha, candidate_sha256=sha)
        source, a = pair_source(pixels, pixels, **args)
        _, b = pair_source(pixels, pixels, **args)
        self.assertEqual(a, b)
        self.assertEqual(len(source), 2)
        self.assertTrue(all(not frame.rgb.flags.writeable for frame in source))

    def test_wrong_decoded_hash_refused(self):
        pixels = b"\x80" * (32 * 32 * 3)
        sha = hashlib.sha256(pixels).hexdigest()
        with self.assertRaises(VqaError):
            pair_source(pixels, pixels, width=32, height=32,
                        reference_sha256=sha, candidate_sha256="0" * 64)

    def test_mutable_bytes_invalid_size_and_boolean_dimensions_refused(self):
        pixels = b"\x80" * (32 * 32 * 3)
        sha = hashlib.sha256(pixels).hexdigest()
        for reference, width in ((bytearray(pixels), 32), (pixels[:-1], 32), (pixels, True)):
            with self.assertRaises(VqaError):
                pair_source(reference, pixels, width=width, height=32,
                            reference_sha256=sha, candidate_sha256=sha)

    def test_subthreshold_single_channel_change_preserved_in_binary_witness(self):
        a = b"\x80" * (32 * 32 * 3)
        b = b"\x81" + a[1:]
        source, manifest, original = residual_source(
            a, b, width=32, height=32, reference_sha256=hashlib.sha256(a).hexdigest(),
            candidate_sha256=hashlib.sha256(b).hexdigest())
        before, after = list(source)
        self.assertEqual(before.rgb.tobytes(), bytes(len(a)))
        self.assertEqual(after.rgb.tobytes(), b"\xff\xff\xff" + bytes(len(a) - 3))
        self.assertNotEqual(manifest["frames"][1]["sha256"], original["frames"][1]["sha256"])


if __name__ == "__main__":
    unittest.main()
