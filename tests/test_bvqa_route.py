"""blender-visual-qa component router: the committed index must be fresh and the frozen bench must hold.

The component suite owns the detailed router tests; this root test keeps hosted CI honest
about staleness, abstention on absent 3D environments and the measured bench floors.
"""
import json
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / 'components' / 'blender-visual-qa'


def route(*argv):
    result = subprocess.run([sys.executable, '-B', str(COMPONENT / 'route' / 'query.py'), *argv],
                            capture_output=True, text=True, timeout=300, cwd=COMPONENT)
    return result.returncode, result.stdout


class BvqaRouteTests(unittest.TestCase):
    def test_index_is_fresh(self):
        rc, out = route('check')
        self.assertEqual((rc, out.strip()), (0, 'ROUTE_OK'),
                         'run: python3 -B components/blender-visual-qa/route/build.py')

    def test_absent_environment_abstains(self):
        rc, out = route('any', 'where do we step the MuJoCo simulation and read contact forces')
        self.assertEqual(rc, 3, out)
        self.assertIn('mujoco', json.loads(out)['absent'])

    def test_frozen_bench_floors_hold(self):
        rc, out = route('bench')
        self.assertEqual(rc, 0, out)
        bench = json.loads(out)['bench']
        self.assertEqual(sorted(bench), ['DEV', 'FIT', 'HOLD', 'TEST'])
        for split in ('TEST', 'HOLD'):
            self.assertEqual(bench[split]['neg_answered'], [])


if __name__ == '__main__':
    unittest.main()
