"""Adversarial regression controls; no model inference or network access.

Execute only under the assigned resource scope and isolated PID namespace.
Texture positives use actual CPU Blender. Matting workers and embeddings are
explicit synthetic seams, never model or appearance qualification.
"""
import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from asset_factory import core
from tools.blender import texture_project as texture
from tools.blender.bake_support import Refusal
from tools.mesh import contracts
from tools.mesh import remesh
from gates.rank import evaluate as ranking
from gates.rank.metrics import RankError
from providers.birefnet import adapter, offline, resources
import test_w3_texture_projection as texture_fixtures
import test_w3_matting_adapters as matting_fixtures
import test_w3_rank_gate as rank_fixtures


class RemeshScopeRepair(unittest.TestCase):
    def test_inherited_bounded_scope_avoids_nested_user_bus(self):
        observed = {'memory_max_bytes': 8*2**30, 'memory_high_bytes': 6*2**30,
                    'swap_max_bytes': 0, 'headroom_bytes': 7*2**30}
        def bounded_runner(argv, timeout):
            self.assertNotIn('systemd-run', argv)
            self.assertIn('taskset', argv)
            return 0, 'AF_REMESH_RC 0\n'
        with patch.object(resources, 'cgroup_observation', return_value=observed), \
             patch.object(remesh, 'default_runner', side_effect=bounded_runner):
            self.assertEqual(remesh._worker([sys.executable, '-c', 'pass'], time.monotonic()+5), 0)

    def test_scope_without_observed_bounds_must_establish_new_limits(self):
        expected = remesh.SCOPE[:-1] + [str(15 - os.getpriority(os.PRIO_PROCESS, 0))]
        expected[3] = "--quiet"
        with patch.object(resources, 'cgroup_observation',
                          side_effect=resources.ObservationUnavailable('cgroup_unknown')):
            self.assertEqual(resources.bounded_scope_prefix(remesh.SCOPE), expected)
        observed = {'memory_max_bytes': None, 'memory_high_bytes': None, 'swap_max_bytes': None}
        with patch.object(resources, 'cgroup_observation', return_value=observed):
            self.assertEqual(resources.bounded_scope_prefix(remesh.SCOPE), expected)


class TextureSupportRepair(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix='texture-support-repair-')
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.root = Path(cls.temporary.name)
        cls.mesh, cls.reference, cls.rights = texture_fixtures.fixture(cls.root)
        cls.options = dict(rights=cls.rights, front='-Y', resolution=128,
                           margin_px=4, timeout_s=120, max_front_mae_ppm=50000)
        cls.baseline = cls.root / 'baseline'
        # The entire test already inherits the mandatory scope/PID namespace.
        # Remove only the redundant inner systemd scope (the user bus refuses
        # PID-namespace credentials). Actual spawning/signalling stays guarded.
        with patch.object(texture, 'MEMORY_SCOPE', []):
            result = texture.project(cls.mesh, cls.reference, cls.baseline, **cls.options)
        if result['rc'] != 0:
            raise AssertionError(result)

    def test_f1_tiny_overlap_and_partial_alpha_cannot_publish(self):
        source_before = self.mesh.read_bytes()
        width, height, _, expected = texture_fixtures.P.decode_png(
            (self.baseline / 'front_projected.png').read_bytes())
        for mutation in ('one_pixel', 'partial_alpha'):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory(dir=self.root) as d:
                stage = Path(d)
                for name in texture.FILES:
                    shutil.copyfile(self.baseline / name, stage / name)
                actual = bytearray(expected)
                support = [i for i in range(0, len(expected), 4) if expected[i+3] == 255]
                self.assertGreater(len(support), 1)
                for i in support[1:]:
                    actual[i+3] = 0 if mutation == 'one_pixel' else 254
                (stage / 'front_baked.png').write_bytes(
                    texture_fixtures.P.encode_png(width, height, actual))
                destination = stage / 'published'
                def replay(argv, timeout, log):
                    for name in texture.FILES:
                        shutil.copyfile(stage / name, Path(log).parent / name)
                    Path(log).write_text('Synthetic corrupted render replay\n')
                    return 0
                result = texture.project(self.mesh, self.reference, destination,
                                         runner=replay, **self.options)
                self.assertEqual(result['rc'], 5, result)
                self.assertIn('front_render_support_changed', result['reason_codes'])
                self.assertFalse(destination.exists())
        self.assertEqual(self.mesh.read_bytes(), source_before)

    def test_f1_valid_support_still_measures_colour_error(self):
        metrics = texture.measure_bundle(self.baseline, self.mesh.read_bytes(), 128, 50000)
        self.assertGreater(metrics['front_render_pixels'], 1)
        self.assertLessEqual(metrics['front_reconstruction_mae_ppm'], 50000)


class TextureCleanupRepair(unittest.TestCase):
    def test_f3_timeout_and_success_retire_term_ignoring_descendant(self):
        for outcome, escaped in (('timeout', False), ('success', False),
                                 ('timeout', True), ('success', True)):
            with self.subTest(outcome=outcome, escaped=escaped), tempfile.TemporaryDirectory() as d:
                root = Path(d)
                pidfile = root / 'descendant.pid'
                script = root / 'fork.py'
                script.write_text(
                    'import os,signal,time\nfrom pathlib import Path\n'
                    'child=os.fork()\n'
                    'if child == 0:\n'
                    + (' os.setsid()\n' if escaped else '') +
                    ' signal.signal(signal.SIGTERM,signal.SIG_IGN)\n'
                    ' Path(' + repr(str(pidfile)) + ').write_text(str(os.getpid()))\n'
                    ' while True: signal.pause()\n'
                    'else:\n'
                    ' end=time.monotonic()+3\n'
                    ' while not Path(' + repr(str(pidfile)) + ').exists() and time.monotonic()<end: time.sleep(.01)\n'
                    + (' time.sleep(60)\n' if outcome == 'timeout' else ' pass\n'))
                # Real processes, owned helper; no Popen mocks or raw signals.
                with patch.object(texture, 'MEMORY_SCOPE', []):
                    if outcome == 'timeout':
                        with self.assertRaises(subprocess.TimeoutExpired):
                            texture.scoped_run([sys.executable, '-B', str(script)], 1, root / 'log')
                    else:
                        self.assertEqual(texture.scoped_run(
                            [sys.executable, '-B', str(script)], 5, root / 'log'), 0)
                self.assertTrue(pidfile.exists(), 'descendant must actually start')
                pid = int(pidfile.read_text())
                # Namespace-local PID can be reused. A lingering live member
                # fails; a zombie has stopped and is reaped by namespace init.
                state = 'absent'
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline:
                    try:
                        state = (Path('/proc') / str(pid) / 'stat').read_text().rsplit(')', 1)[1].split()[0]
                    except FileNotFoundError:
                        state = 'absent'
                    if state in ('absent', 'Z'):
                        break
                    time.sleep(.01)
                self.assertIn(state, ('absent', 'Z'), 'owned descendant still executing')


class RankReceiptRepair(unittest.TestCase):
    def test_f2_incomplete_pass_cannot_make_a_seed_eligible(self):
        f = rank_fixtures.Fixture()
        f.setUp()
        try:
            item = f.request['candidates'][0]
            malformed = {'schema': 'af.mesh-regression/v1', 'rc': 0, 'status': 'OK',
                         'input_sha256': item['artifact']['sha256'],
                         'artefact_sha256': item['artifact']['sha256'], 'failed': [],
                         'metrics': {'component_inventories': [
                             {'tris': 12, 'boundary_edges': 0, 'failed': []}]}}
            with self.assertRaises(contracts.MeshError):
                contracts.validate_receipt(malformed)
            item['gate'] = f.write_json('incomplete.json', malformed)
            result = f.evaluate()
            self.assertEqual(result['rc'], 2, result)
            self.assertIsNone(result['winner_id'])
        finally:
            f.doCleanups()

    def test_f2_complete_intake_and_comparison_use_distinct_evidence(self):
        subject = 'c'*64
        expected = {'operation': 'metrics', 'policy_sha256': 'a'*64, 'toolchain_sha256': 'b'*64}
        receipt = rank_fixtures.mesh_receipt(subject)
        def check(value, expectation=expected):
            return ranking._gate(core.canonical_json(value).encode(), subject, expectation)
        self.assertEqual(check(receipt), 0)  # Intake legitimately lacks distances.
        for key in ('policy_sha256', 'toolchain_sha256', 'unavailable', 'prev_sha256'):
            changed = copy.deepcopy(receipt)
            del changed[key]
            with self.subTest(missing=key), self.assertRaises(RankError) as caught:
                check(changed)
            self.assertEqual(caught.exception.rc, 2)
        for key in ('policy_sha256', 'toolchain_sha256'):
            changed = copy.deepcopy(receipt)
            changed[key] = 'd'*64
            with self.subTest(drift=key), self.assertRaises(RankError) as caught:
                check(changed)
            self.assertEqual(caught.exception.rc, 4)
        with self.assertRaises(RankError) as caught:
            check(receipt, None)
        self.assertEqual(caught.exception.rc, 3)
        comparison = copy.deepcopy(receipt)
        comparison.update(input_sha256='d'*64, candidate_sha256=subject)
        comparison['metrics'].update(hausdorff_forward_um=1, hausdorff_backward_um=1,
            raw_diag_um=1000000, hausdorff_relative_ppm=1, silhouette_iou_ppm=1000000,
            distance_is_exact=False,
            silhouette_per_view_ppm={view: 1000000 for view in ('front', 'back', 'left', 'right')})
        comparison['unavailable'] = {}
        expected_comparison = {**expected, 'operation': 'compare'}
        self.assertEqual(check(comparison, expected_comparison), 0)
        with self.assertRaises(RankError) as caught:
            check(comparison, expected)
        self.assertEqual(caught.exception.rc, 2)
        for key, value in (('hausdorff_backward_um', None), ('hausdorff_backward_um', 30000),
                           ('silhouette_per_view_ppm', {'front': 1000000})):
            changed = copy.deepcopy(comparison)
            changed['metrics'][key] = value
            if value is None:
                changed['unavailable'][key] = 'not_measured'
            with self.subTest(regression=key, value=value), self.assertRaises(RankError) as caught:
                check(changed, expected_comparison)
            self.assertEqual(caught.exception.rc, 2)


class MattingEnvelopeRepair(unittest.TestCase):
    def test_f4_actual_scope_headroom_cannot_fund_host_only_floor(self):
        factory = {'memory': {'max_resident_models': 1, 'ceiling_gb_default': 8,
                              'min_free_gb_before_start': 20}}
        with self.assertRaises(offline.Issue) as caught:
            offline.admit_memory(factory, 2**30, {'memory_max_bytes': 8*2**30})
        self.assertEqual(caught.exception.rc, 5)
        self.assertEqual(caught.exception.code, 'headroom_below_floor')

    def test_f4_worker_exceeding_admitted_peak_cannot_publish(self):
        f = matting_fixtures.AdapterTests()
        f.setUp()
        try:
            root, args = f.fixture()
            def drift(*arguments):
                candidate, metadata = f.fake_worker(*arguments)
                metadata['peak_rss_bytes'] = 2*2**30
                return candidate, metadata
            with patch.object(offline, 'package_observation', side_effect=f.observed_packages), \
                 patch.object(offline, 'run_worker', side_effect=drift):
                result = adapter.prepare(f.source, root, f.root / 'output', **args)
            self.assertEqual(result['rc'], 6, result)
            self.assertIn('admitted_peak_exceeded', result['reason_codes'])
            self.assertFalse((f.root / 'output').exists())
            self.assertFalse(args['lease_path'].exists())
        finally:
            f.doCleanups()

    def test_f4_ancestor_usage_limits_headroom_and_missing_observations_refuse(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            mount = root / 'cgroup'
            parent, child = mount / 'parent', mount / 'parent/child'
            child.mkdir(parents=True)
            membership, mountinfo = root / 'membership', root / 'mountinfo'
            membership.write_text('0::/parent/child\n')
            mountinfo.write_text('30 20 0:1 / ' + str(mount) + ' rw - cgroup2 cgroup rw\n')
            for group, current, maximum in ((parent, 14*2**30, 16*2**30), (child, 2**30, 8*2**30)):
                (group / 'memory.current').write_text(str(current))
                (group / 'memory.max').write_text(str(maximum))
                (group / 'memory.high').write_text(str(6*2**30))
                (group / 'memory.swap.max').write_text('0')
            observed = resources.cgroup_observation(membership=membership, mountinfo=mountinfo)
            self.assertEqual(observed['headroom_bytes'], 2*2**30)
            self.assertEqual(observed['memory_max_bytes'], 8*2**30)
            factory = {'memory': {'max_resident_models': 1, 'ceiling_gb_default': 8,
                                  'min_free_gb_before_start': 0}}
            with patch.object(resources, 'host_available', return_value=32*2**30), \
                 patch.object(resources, 'cgroup_observation', return_value=observed):
                with self.assertRaises(offline.Issue) as caught:
                    offline.admit_memory(factory, 3*2**30, {'memory_max_bytes': 8*2**30})
                self.assertEqual(caught.exception.code, 'headroom_below_floor')
                offline.admit_memory(factory, 2**30, {'memory_max_bytes': 8*2**30})
            (parent / 'memory.max').unlink()
            with self.assertRaises(resources.ObservationUnavailable):
                resources.cgroup_observation(membership=membership, mountinfo=mountinfo)
            with patch.object(resources, 'cgroup_observation',
                              side_effect=resources.ObservationUnavailable('cgroup_unknown')):
                with self.assertRaises(offline.Issue) as caught:
                    offline.admit_memory(factory, 2**30, {'memory_max_bytes': 8*2**30})
                self.assertEqual((caught.exception.rc, caught.exception.code), (3, 'cgroup_unknown'))
