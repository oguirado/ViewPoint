"""Tests for path selection shape handling and F-ACT-06 rejection contract."""
import unittest
from unittest.mock import patch

from wender.act import actor


class TestChoosePathShape(unittest.TestCase):
    """Test choose_path input shape, controls, and proposed F-ACT-06 rejection contract."""

    def test_bare_string_api_raises(self):
        # F-ACT-06: Bare string 'api' should raise TypeError or ValueError instead of returning None
        with self.assertRaises((TypeError, ValueError)):
            actor.choose_path("api")

    def test_bare_string_ax_raises(self):
        # F-ACT-06: Bare string 'ax' should raise TypeError or ValueError instead of returning None
        with self.assertRaises((TypeError, ValueError)):
            actor.choose_path("ax")

    def test_nominal_controls(self):
        # One-element label list
        self.assertEqual(actor.choose_path(["ax"]), "ax")

        # Empty generator
        def empty_gen():
            return
            yield

        self.assertIsNone(actor.choose_path(empty_gen()))

        # Generator yielding duplicate coordinates, then ax, then api
        def full_gen():
            yield "coordinates"
            yield "coordinates"
            yield "ax"
            yield "api"

        self.assertEqual(actor.choose_path(full_gen()), "api")

    def test_invalid_shape_controls_raise_typeerror(self):
        # None input control
        with self.assertRaises(TypeError):
            actor.choose_path(None)

        # Unhashable nested list control
        with self.assertRaises(TypeError):
            actor.choose_path([[1]])

    def test_preference_independent_of_iterable_order(self):
        # Order in iterable should not affect preference: 'api' > 'ax' > 'coordinates'
        self.assertEqual(actor.choose_path(["coordinates", "ax", "api"]), "api")
        self.assertEqual(actor.choose_path(["coordinates", "api"]), "api")
        self.assertEqual(actor.choose_path(("coordinates", "ax")), "ax")
        self.assertEqual(actor.choose_path(["ax", "coordinates"]), "ax")
        self.assertEqual(actor.choose_path(["api", "coordinates", "ax"]), "api")

    def test_selection_does_not_invoke_executor(self):
        with patch("wender.act.actor.execute") as mock_exec:
            res = actor.choose_path(["api", "ax"])
            self.assertEqual(res, "api")
            mock_exec.assert_not_called()


if __name__ == "__main__":
    unittest.main()
