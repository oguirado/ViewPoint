"""Real native checks: run with the approved factory-cad interpreter.

Host-only environments explicitly skip this group; they cannot qualify it.
The external native qualification receipt supplies native non-skip evidence.
"""
import copy
import importlib.util
import io
from pathlib import Path
import tempfile
import unittest
from contextlib import redirect_stdout

from asset_factory import core
from providers.build123d import worker
from tools.cad.brep import compare, properties
from tools.cad.contracts import ROOT, Refusal
from tools.cad.fixtures import analytic_part
from tools.cad.native_qualify import fixture_params, runtime_identity


@unittest.skipUnless(importlib.util.find_spec('build123d'), 'native CAD runtime unavailable: CANNOT_SEE')
class NativeCadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import build123d
        cls.cad = build123d

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.policy = analytic_part(hole=True)[1]
        self.part = self.cad.Box(100, 50, 20) - self.cad.Box(20, 10, 40)

    def test_brep_mass_matches_independent_box_subtraction(self):
        mass = properties(self.part, density_kg_m3=1000)
        gate = compare(mass, self.policy)
        self.assertEqual(gate['rc'], 0, gate)
        self.assertEqual(mass['volume_um3'], 96000000000000)
        self.assertEqual(mass['mass_ng'], 96000000000)
        self.assertEqual(mass['inertia_kg_m2_e18'], self.policy['expected_inertia_kg_m2_e18'])
        self.assertEqual(mass['calculation'], 'floating_point_brep_not_mesh')

    def test_translated_centre_has_same_centre_frame_tensor(self):
        offset = (123000, -456000, 789000)
        moved = self.cad.Pos(*(v/1000 for v in offset))*self.part
        a = properties(self.part, density_kg_m3=1000)
        b = properties(moved, density_kg_m3=1000)
        self.assertEqual(b['centre_um'], list(offset))
        self.assertEqual(a['inertia_kg_m2_e18'], b['inertia_kg_m2_e18'])
        self.assertEqual(compare(b, analytic_part(hole=True, offset_um=offset)[1])['rc'], 0)

    def test_unit_swap_red_then_green_native(self):
        scaled = self.cad.Box(100000, 50000, 20000) - self.cad.Box(20000, 10000, 40000)
        red = compare(properties(scaled, density_kg_m3=1000), self.policy)
        self.assertEqual(red['rc'], 5)
        self.assertIn('dimensions_mismatch', red['reason_codes'])
        self.assertEqual(compare(properties(self.part, density_kg_m3=1000), self.policy)['rc'], 0)

    def test_missing_hole_red_then_green_native(self):
        red = compare(properties(self.cad.Box(100, 50, 20), density_kg_m3=1000), self.policy)
        self.assertEqual(red['rc'], 5)
        self.assertIn('volume_mismatch', red['reason_codes'])
        self.assertEqual(compare(properties(self.part, density_kg_m3=1000), self.policy)['rc'], 0)

    def test_open_brep_red_then_green_native(self):
        opened = self.cad.Shell(list(self.part.faces())[:-1])
        with self.assertRaises(Refusal) as caught:
            properties(opened, density_kg_m3=1000)
        self.assertEqual(caught.exception.rc, 5)
        self.assertEqual(compare(properties(self.part, density_kg_m3=1000), self.policy)['rc'], 0)

    def test_native_density_and_forged_mass_are_detected(self):
        wrong = properties(self.part, density_kg_m3=1000000)
        self.assertEqual(compare(wrong, self.policy)['rc'], 5)
        right = properties(self.part, density_kg_m3=1000)
        right['mass_ng'] *= 1000
        self.assertIn('mass_density_mismatch', compare(right, self.policy)['reason_codes'])
        with self.assertRaises(Refusal) as caught:
            properties(self.part, density_kg_m3=True)
        self.assertEqual(caught.exception.rc, 2)

    def test_native_mass_closed_schema_and_shape(self):
        mass = properties(self.part, density_kg_m3=1000)
        schema = core.load_json(ROOT/'tools/cad/native.schema.json', max_bytes=100000)
        gate = compare(mass, self.policy)
        for value in (mass, gate):
            core.validate(value, schema)
            bad = copy.deepcopy(value)
            bad['invented'] = 1
            with self.assertRaises(core.ContractError):
                core.validate(bad, schema)
        mass['centre_um'] = []
        with self.assertRaises(Refusal) as caught:
            compare(mass, self.policy)
        self.assertEqual(caught.exception.rc, 2)

    def test_worker_real_export_roundtrip_and_refusal_preservation(self):
        source = ROOT/'providers/build123d/fixtures/analytic.py'
        params, policy = self.root/'params.json', self.root/'policy.json'
        core.atomic_write_json(policy, self.policy)
        core.atomic_write_json(params, fixture_params(hole=False))
        target = self.root/'refused'
        argv = ['--source', str(source), '--params', str(params), '--policy', str(policy), '--out', str(target)]
        with redirect_stdout(io.StringIO()):
            red = worker.main(argv)
        self.assertEqual(red, 5)
        self.assertFalse(target.exists())
        core.atomic_write_json(params, fixture_params(hole=True))
        with redirect_stdout(io.StringIO()):
            green = worker.main(argv)
        self.assertEqual(green, 0)
        first_hashes = {name: core.sha256_file(target/name) for name in
                        ('part.step', 'part.stl', 'mass.json', 'roundtrip.mass.json')}
        self.assertEqual(first_hashes['mass.json'], first_hashes['roundtrip.mass.json'])
        with redirect_stdout(io.StringIO()):
            refused = worker.main(argv)
        self.assertEqual(refused, 5)
        self.assertEqual(first_hashes, {name: core.sha256_file(target/name) for name in first_hashes})
        self.assertEqual(list(self.root.glob('.cad-*')), [])
        witness = core.load_json(target/'export.json', max_bytes=100000)
        core.validate(witness, core.load_json(ROOT/'tools/cad/records.schema.json', max_bytes=100000))
        self.assertEqual(witness['mass_sha256'], first_hashes['mass.json'])

    def test_operator_freeze_drift_is_stale(self):
        altered = self.root/'altered.freeze.txt'
        altered.write_text('build123d==0\n')
        with self.assertRaises(Refusal) as caught:
            runtime_identity(altered)
        self.assertEqual(caught.exception.rc, 4)


if __name__ == '__main__':
    unittest.main()
