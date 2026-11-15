"""Component prerequisite order and caller fixture contracts stay explicit."""
from pathlib import Path
import tempfile
import unittest
from tools.vp_gate import ROOT, SHARED_RESOURCES, component_cases, component_environment, component_lanes, component_workdirs, guarded


class ComponentPlanTests(unittest.TestCase):
    def test_physical_workspace_keeps_repository_socket_within_platform_limit(self):
        with tempfile.TemporaryDirectory(dir='/tmp') as directory:
            ctl = Path(directory)
            with self.assertRaisesRegex(ValueError, 'socket path budget'):
                component_workdirs(ctl / ('long-path-' * 10))
            work = component_workdirs(ctl)
            self.assertEqual(work['OpenSuperficies'].name, 'o')
            self.assertFalse(work['OpenSuperficies'].parent.is_symlink())
            relative = work['OpenSuperficies'].relative_to(ctl)
            socket = ROOT.parent / 'ctl' / relative / 'build/room_FB3/tmp/test-1451585838/peer.sock'
            self.assertLess(len(str(socket).encode()), 108)

    def test_native_artifacts_precede_consumer_suites(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            work = {name: base / name for name in
                    ('wender-agent', 'vision-engine', 'OpenSuperficies')}
            crate = work['vision-engine'] / 'rust/native'
            crate.mkdir(parents=True)
            (crate / 'Cargo.toml').write_text('[package]\nname = "fixture"\n')
            cases = component_cases(work)
            names = [case['name'] for case in cases]
            self.assertEqual(len(names), len(set(names)))
            for build in ('vision-engine/release-native', 'vision-engine/native',
                          'vision-engine/wheel', 'vision-engine/peer-wheel'):
                for consumer in ('vision-engine/python', 'vision-engine/ci-health',
                                 'vision-engine/gates', 'OpenSuperficies/cu-gates'):
                    self.assertLess(names.index(build), names.index(consumer))
            by_name = {case['name']: case for case in cases}
            for owner in ('wender-agent', 'vision-engine'):
                health_output = Path(by_name[owner + '/ci-health']['command'][-1])
                self.assertTrue(health_output.is_absolute())
                self.assertFalse(health_output.is_relative_to(work[owner]))
            self.assertGreater(by_name['OpenSuperficies/cu-gates']['seconds'], 1500)
            self.assertEqual(by_name['OpenSuperficies/go-race']['command'][:2],
                             ['xvfb-run', '--auto-servernum'])
            self.assertEqual(by_name['wender-agent/state-budget']['command'],
                             ['python3', '-B', '-m', 'tools.state_budget'])
            self.assertEqual(by_name['wender-agent/memoryweb']['command'][-1],
                             'entry-check')
            self.assertEqual(by_name['vision-engine/typescript-w2']['cwd'], work['vision-engine'])
            self.assertEqual(by_name['vision-engine/typescript-w2']['command'],
                             ['node', '--test', 'langx/x2/ts_w2/x2.test.ts'])
            self.assertEqual(by_name['vision-engine/release-native']['command'],
                             ['cargo', 'build', '--release', '--offline', '-j', '2'])
            self.assertEqual(by_name['wender-agent/python']['command'],
                             ['python3', '-B', '-m', 'unittest', 'discover',
                              '-s', 'tests', '-t', '.'])
            self.assertEqual(by_name['OpenSuperficies/frontend-tests']['command'],
                             ['npm', 'exec', '--offline', '--', 'vitest', 'run',
                              '--exclude', 'tests/terminal.test.ts'])

    def test_explicit_browser_fixture_is_preserved(self):
        env = {'CHROME_BIN': '/fixture/chromium', 'MV_WENDER_ROOT': '/fixture/root',
               'CARGO_TARGET_DIR': '/fixture/root-target'}
        child = component_environment(env, Path('/fixture/control'))
        self.assertEqual(child['CHROME_BIN'], env['CHROME_BIN'])
        self.assertNotIn('MV_WENDER_ROOT', child)
        self.assertNotIn('CARGO_TARGET_DIR', child)
        self.assertEqual(env['CARGO_TARGET_DIR'], '/fixture/root-target')
        self.assertEqual(env['MV_WENDER_ROOT'], '/fixture/root')


class ParallelLaneTests(unittest.TestCase):
    def test_lanes_keep_builders_first_and_owners_disjoint(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            work = {name: base / name for name in
                    ('wender-agent', 'vision-engine', 'OpenSuperficies')}
            crate = work['vision-engine'] / 'rust/native'
            crate.mkdir(parents=True)
            (crate / 'Cargo.toml').write_text('[package]\nname = "fixture"\n')
            cases = component_cases(work)
            builders, lanes = component_lanes(cases)
            built = {case['name'] for case in builders}
            self.assertIn('vision-engine/peer-wheel', built)
            self.assertIn('vision-engine/release-native', built)
            # Every planned case runs exactly once, in builders or one lane.
            flat = [case['name'] for case in builders] + [c['name'] for lane in lanes for c in lane]
            self.assertEqual(sorted(flat), sorted(case['name'] for case in cases))
            self.assertEqual(len(flat), len(set(flat)))
            # One owner per lane, and each lane keeps the sequential order.
            order = [case['name'] for case in cases]
            for lane in lanes:
                self.assertEqual(len({case['owner'] for case in lane}), 1)
                names = [case['name'] for case in lane]
                self.assertEqual(names, sorted(names, key=order.index))
            self.assertEqual({lane[0]['owner'] for lane in lanes},
                             {'wender-agent', 'vision-engine', 'OpenSuperficies'})
            by_name = {case['name']: case for case in cases}
            self.assertGreater(by_name['vision-engine/python']['seconds'], 1200)


class ComponentTagTests(unittest.TestCase):
    def test_component_checkout_gets_only_its_namespaced_tags(self):
        import subprocess
        from tools.vp_gate import local_checkout
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source = base / 'mono'
            def git(*args, cwd=source):
                return subprocess.run(['git', '-C', str(cwd), '-c', 'user.name=t', '-c', 'user.email=t@t',
                                       *args], check=True, capture_output=True, text=True).stdout.strip()
            subprocess.run(['git', 'init', '-q', str(source)], check=True)
            (source / 'f').write_text('x\n')
            git('add', 'f')
            git('commit', '-q', '-m', 'init')
            pin = git('rev-parse', 'HEAD')
            git('tag', '-a', '-m', 'root', 'v0.1.0')
            git('tag', '-a', '-m', 'component', 'component-v0.1.0')
            oid = git('rev-parse', 'refs/tags/component-v0.1.0')
            git('update-ref', 'refs/component-tags/vision-engine/v0.1.0', oid)
            git('update-ref', 'refs/component-tags/wender-agent/w-only', oid)
            target = local_checkout(source, base / 'v', pin, 'vision-engine')
            self.assertEqual(git('tag', cwd=target).split(), ['v0.1.0'])
            self.assertEqual(git('rev-parse', 'refs/tags/v0.1.0', cwd=target), oid)


class ParallelSafetyTests(unittest.TestCase):
    def test_crashed_step_records_a_failure(self):
        rows = []
        def boom():
            raise ValueError('local pin cannot be resolved')
        guarded('gate/components', boom, rows.append)()
        self.assertEqual([(r['name'], r['status']) for r in rows], [('gate/components', 'FAIL')])
        guarded('gate/root', lambda: None, rows.append)()
        self.assertEqual(len(rows), 1)

    def test_suites_sharing_the_vision_ram_workspace_share_one_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            work = {name: base / name for name in
                    ('wender-agent', 'vision-engine', 'OpenSuperficies')}
            by_name = {case['name']: case for case in component_cases(work)}
            for name in SHARED_RESOURCES['vision-ramws']:
                self.assertEqual(by_name[name]['resources'], ['vision-ramws'])
            # The two owners that collided on /dev/shm/vqa-ramws are both covered.
            self.assertIn('vision-engine/python', SHARED_RESOURCES['vision-ramws'])
            self.assertIn('OpenSuperficies/cu-suite', SHARED_RESOURCES['vision-ramws'])
            self.assertEqual(by_name['wender-agent/python']['resources'], [])
