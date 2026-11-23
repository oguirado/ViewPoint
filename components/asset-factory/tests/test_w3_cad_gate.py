import copy
from pathlib import Path
import struct
import tempfile
import unittest

from asset_factory import core
from tools.cad.contracts import Refusal
from tools.cad.fixtures import analytic_part, write_stl
from tools.cad.gate import evaluate
from tools.cad.mesh import physical_validate


class CadGateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)/'part.stl'
        self.triangles, self.policy, self.density = analytic_part()
        write_stl(self.path, self.triangles)

    def evaluate(self, policy=None, density=None, units='mm'):
        return evaluate(self.path, self.policy if policy is None else policy,
                        density_kg_m3=self.density if density is None else density, units=units)

    def test_analytic_box_mass_centre_inertia(self):
        result = self.evaluate()
        self.assertEqual(result['rc'], 0, result)
        mass = result['measurements']
        self.assertEqual(mass['volume_um3'], 100000000000000)
        self.assertEqual(mass['mass_ng'], 100000000000)
        self.assertEqual(mass['centre_um'], [0, 0, 0])
        self.assertEqual(mass['inertia_kg_m2_e18'], self.policy['expected_inertia_kg_m2_e18'])
        self.assertFalse(mass['exact_brep_properties'])
        self.assertIn('exact_brep_inertia', result['unavailable'])
        self.assertEqual(core.canonical_json(result), core.canonical_json(self.evaluate()))

    def test_off_origin_box(self):
        triangles, policy, _ = analytic_part(offset_um=(123000, -456000, 789000))
        write_stl(self.path, triangles)
        result = self.evaluate(policy)
        self.assertEqual(result['rc'], 0, result)
        self.assertEqual(result['measurements']['centre_um'], [123000, -456000, 789000])

    def test_hole_and_missing_hole_red_then_green(self):
        triangles, policy, _ = analytic_part(hole=True)
        bad = self.evaluate(policy)
        self.assertEqual(bad['rc'], 5)
        self.assertIn('hole_topology_mismatch', bad['reason_codes'])
        self.assertIn('volume_mismatch', bad['reason_codes'])
        write_stl(self.path, triangles)
        good = self.evaluate(policy)
        self.assertEqual(good['rc'], 0, good)
        self.assertEqual(good['measurements']['topology']['genus'], 1)

    def test_unit_swap_red_then_green(self):
        write_stl(self.path, self.triangles, scale=1000)
        self.assertEqual(self.evaluate()['rc'], 5)
        write_stl(self.path, self.triangles)
        self.assertEqual(self.evaluate()['rc'], 0)
        self.assertEqual(self.evaluate(units='m')['rc'], 2)

    def test_open_solid_red_then_green(self):
        write_stl(self.path, self.triangles[:-1])
        self.assertEqual(self.evaluate()['rc'], 5)
        write_stl(self.path, self.triangles)
        self.assertEqual(self.evaluate()['rc'], 0)

    def test_density_swap_and_unknown_density(self):
        self.assertEqual(self.evaluate(density=1000000)['rc'], 5)
        self.assertEqual(self.evaluate(density=None)['rc'], 0)
        self.assertEqual(evaluate(self.path, self.policy, density_kg_m3=None, units='mm')['rc'], 2)
        self.assertEqual(self.evaluate(density=True)['rc'], 2)

    def test_corrupt_nonfinite_and_empty_stl(self):
        raw = self.path.read_bytes()
        self.path.write_bytes(raw[:-1])
        self.assertEqual(self.evaluate()['rc'], 2)
        broken = bytearray(raw)
        struct.pack_into('<f', broken, 96, float('nan'))
        self.path.write_bytes(broken)
        self.assertEqual(self.evaluate()['rc'], 2)
        write_stl(self.path, [])
        self.assertEqual(self.evaluate()['rc'], 3)

    def test_duplicate_and_inverted_triangles(self):
        write_stl(self.path, self.triangles+[self.triangles[0]])
        self.assertEqual(self.evaluate()['rc'], 5)
        write_stl(self.path, [tuple(reversed(t)) for t in self.triangles])
        self.assertEqual(self.evaluate()['rc'], 5)

    def test_inertia_axis_swap(self):
        policy = copy.deepcopy(self.policy)
        tensor = policy['expected_inertia_kg_m2_e18']
        tensor[0][0], tensor[2][2] = tensor[2][2], tensor[0][0]
        self.assertEqual(self.evaluate(policy)['rc'], 5)

    def test_inertia_physical_invariants(self):
        good = self.evaluate()['measurements']
        for mutation in ('asymmetric', 'negative', 'triangle'):
            bad = copy.deepcopy(good)
            matrix = bad['inertia_kg_m2_e18']
            if mutation == 'asymmetric':
                matrix[0][1] = 1
            elif mutation == 'negative':
                matrix[0][0] = -1
            else:
                matrix[2][2] = 10*(matrix[0][0]+matrix[1][1])
            with self.assertRaises(Refusal) as caught:
                physical_validate(bad)
            self.assertEqual(caught.exception.rc, 5)

    def test_missing_loose_or_boolean_policy_refused(self):
        for mutate in (lambda p: p.pop('expected_genus'),
                       lambda p: p.update(dimension_tolerance_um=11),
                       lambda p: p.update(expected_genus=True),
                       lambda p: p.update(volume_tolerance_ppm=1000000)):
            policy = copy.deepcopy(self.policy)
            mutate(policy)
            self.assertEqual(self.evaluate(policy)['rc'], 2)

    def test_bbox_check_is_independently_observable(self):
        policy = copy.deepcopy(self.policy)
        policy['expected_bbox_um'][1][0] += 100
        result = self.evaluate(policy)
        self.assertEqual(result['rc'], 5)
        self.assertEqual(result['reason_codes'], ['dimensions_mismatch'])

    def test_record_schemas_are_closed(self):
        from tools.cad.contracts import ROOT
        schema = core.load_json(ROOT/'tools/cad/records.schema.json', max_bytes=100000)
        result = self.evaluate()
        for record in (result, self.policy, result['measurements']):
            core.validate(record, schema)
            bad = copy.deepcopy(record)
            bad['invented_field'] = 'no'
            with self.assertRaises(core.ContractError):
                core.validate(bad, schema)


if __name__ == '__main__':
    unittest.main()
