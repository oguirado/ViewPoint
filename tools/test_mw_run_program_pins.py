"""Offline integration with deps.lock and the helper in MV_VISION_POOL_HELPER."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mw_run_program as program
from metaviewer_agents.perception.base import Frame, PerceptionError
from metaviewer_agents.perception.deps_lock import resolve_locked_component
from metaviewer_agents.observe import ObservationError
from test_mw_run_program import frame


class IsolatedPoolTests(unittest.TestCase):
    def setUp(self):
        workspace = tempfile.TemporaryDirectory(dir='/dev/shm')
        self.addCleanup(workspace.cleanup)
        root = Path(workspace.name)
        shared = program.ProgramVisionPool(os.environ).helper
        self.helper = root / 'vision_pool.py'
        # Invoke the shared implementation and CLI with a private state directory.
        # The test never reads or changes the actual host pool queue.
        self.helper.write_text(f'''
import importlib.util
spec = importlib.util.spec_from_file_location("shared_pool", {str(shared)!r})
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
pool = module.Pool
module.Pool = lambda unused: pool({str(root / 'state')!r})
raise SystemExit(module.main())
''')
        self.helper.chmod(0o600)
        self.env = dict(MV_VISION_POOL_HELPER=str(self.helper))

    def invoke(self, *options):
        result = subprocess.run([sys.executable, '-I', '-B', str(self.helper), *options],
                                capture_output=True, timeout=12, check=False)
        return result.returncode, json.loads(result.stdout)

    def status(self):
        rc, receipt = self.invoke('status')
        self.assertEqual(rc, 0)
        return receipt

    def wait_until(self, predicate):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            status = self.status()
            if predicate(status):
                return status
            time.sleep(0.01)
        self.fail(f'unobserved pool state: {status}')

    @contextmanager
    def owner(self, front, ttl):
        process = subprocess.Popen([sys.executable, '-I', '-B', '-c',
                                    'import signal; signal.pause()'],
                                   stdin=subprocess.DEVNULL)
        args = ('--front', front, '--pid', str(process.pid))
        try:
            rc, lease = self.invoke('acquire', *args, '--ttl', str(ttl))
            self.assertEqual((rc, lease['status']), (0, 'granted'))
            try:
                yield args, lease
            finally:
                current = self.status()['lease']
                if current is not None and current['pid'] == process.pid:
                    self.assertEqual(self.invoke('release', *args, '--lease-id', lease['lease_id'])[0], 0)
        finally:
            process.terminate()
            process.wait(timeout=5)

    @contextmanager
    def locked_adapter(self, **options):
        pool = program.ProgramVisionPool(self.env)
        lease = pool.acquire(5, 30)
        adapter = None
        try:
            adapter = program.ProgramVisionAdapter(verify_lock=True, **options)
            yield adapter
        finally:
            if adapter is not None:
                adapter.close()
            pool.release(lease)


class SharedPoolTests(IsolatedPoolTests):
    def test_pool_queue_real_helper_admits_waiters_fifo_after_their_own_grant(self):
        started = threading.Event()
        finish = threading.Event()
        adapter = Mock()
        backend = SimpleNamespace(require_perception=Mock(), ownership=Mock(),
                                  run_deadline_ns=time.monotonic_ns() + 8_000_000_000)

        def run_main():
            with program.pooled_program_vision(backend, self.env, 1000):
                started.set()
                self.assertTrue(finish.wait(5))

        with ThreadPoolExecutor(max_workers=2) as executor, patch.object(
                program, 'ProgramVisionAdapter', return_value=adapter) as engine:
            with self.owner('3D', 30) as (holder_args, holder):
                main = executor.submit(run_main)
                self.wait_until(lambda status: [row['front'] for row in status['queue']] == ['MAIN'])
                next_args = ('--front', 'REPLAY', '--pid', str(os.getpid()))
                following = executor.submit(self.invoke, 'acquire', *next_args, '--timeout', '8')
                self.wait_until(lambda status: [row['front'] for row in status['queue']] == ['MAIN', 'REPLAY'])
                engine.assert_not_called()
                self.assertFalse(following.done())
                self.assertEqual(self.invoke('release', *holder_args, '--lease-id', holder['lease_id'])[0], 0)
                try:
                    self.assertTrue(started.wait(5))
                    current = self.status()
                    self.assertEqual(current['lease']['front'], 'MAIN')
                    self.assertEqual(current['lease']['pid'], os.getpid())
                    self.assertEqual([row['front'] for row in current['queue']], ['REPLAY'])
                    engine.assert_called_once()
                    self.assertFalse(following.done())
                finally:
                    finish.set()
                main.result(timeout=5)
                adapter.close.assert_called_once()
                rc, lease = following.result(timeout=5)
                self.assertEqual((rc, lease['front'], lease['status']), (0, 'REPLAY', 'granted'))
                self.assertEqual(self.invoke('release', *next_args, '--lease-id', lease['lease_id'])[0], 0)
            self.assertEqual(self.status(), dict(cap=1, lease=None, queue=[]))

    def test_pool_release_wrong_token_and_expired_live_owner_protect_the_slot(self):
        with self.owner('3D', 1) as (holder_args, holder):
            rc, receipt = self.invoke('release', *holder_args, '--lease-id', '0' * 32)
            self.assertEqual((rc, receipt['status']), (4, 'lease_mismatch'))
            rc, receipt = self.invoke('release', '--front', '3D', '--pid', str(os.getpid()),
                                      '--lease-id', holder['lease_id'])
            self.assertEqual((rc, receipt['status']), (4, 'lease_mismatch'))
            occupied = self.wait_until(lambda status: status['lease']['expired'])
            self.assertEqual(occupied['lease']['pid'], holder['pid'])
            backend = SimpleNamespace(require_perception=Mock(), ownership=Mock(),
                                      run_deadline_ns=time.monotonic_ns() + 1_900_000_000)
            with patch.object(program, 'ProgramVisionAdapter') as engine:
                with self.assertRaisesRegex(program.host.OwnerRefused, 'MW_PROGRAM_POOL_TIMEOUT'):
                    with program.pooled_program_vision(backend, self.env, 1000):
                        self.fail('a second job entered an occupied pool')
                engine.assert_not_called()
            self.assertEqual(self.status()['lease']['front'], '3D')
        self.assertEqual(self.status(), dict(cap=1, lease=None, queue=[]))


class PinnedProgramTests(IsolatedPoolTests):
    def test_actual_static_first_frame_needs_real_marks_for_localization(self):
        pixels = bytearray(b'\xff' * (100 * 80 * 3))
        for y in range(20, 40):
            for x in range(20, 52):
                if y in (20, 39) or x in (20, 51):
                    offset = (y * 100 + x) * 3
                    pixels[offset:offset + 3] = b'\0\0\0'
        image = Frame(100, 80, bytes(pixels))
        ref = dict(frame(), sha256=hashlib.sha256(image.data).hexdigest())
        with self.locked_adapter(timeout=10) as plain:
            no_marks = plain.perceive(ref, image)
            self.assertEqual(program.project_element_table(ref, no_marks)['elements'], [])
        with program.marks_workspace() as marks:
            with self.locked_adapter(timeout=10, marks_out=marks) as adapter:
                vision = adapter.perceive(ref, image)
                elements = program.project_element_table(ref, vision)['elements']
                self.assertTrue(elements)
                payload = adapter.last_c2_payload
                self.assertEqual(payload['frame_sha256'], ref['sha256'])
                payload['marks'].clear()
                self.assertTrue(adapter.last_c2_payload['marks'])
                self.assertTrue(marks.is_file())
                self.assertTrue(marks.is_relative_to('/dev/shm/vqa-cu'))
                with self.assertRaises(PerceptionError):
                    adapter.perceive(dict(ref, ramws_path='/dev/shm/nonexistent-program-test-frame'))
                self.assertIsNone(adapter.last_c2_payload)
        self.assertFalse(marks.exists())

    def test_actual_pinned_wender_cli_plans_goal_bound_click_without_url_or_ax(self):
        root = resolve_locked_component('wender-agent')
        planner = program.WenderSubprocessAdapter(wender_root=root, verify_lock=True,
            command=[sys.executable, '-m', 'wender.plan.cli'])
        observation = program.build_observation(frame(), surface_id='surface-alice', generation=1)
        table = dict(schema='vqa.element-table/v1', frame_sha256='a' * 64,
            width=100, height=80, elements=[
                dict(bbox=dict(x=10,y=20,w=20,h=20), role='button', name='Covered square', score_ppm=900000),
                dict(bbox=dict(x=60,y=20,w=20,h=20), role='button', name='Menu', score_ppm=900000)])
        action, expectation, state, target = planner.plan_step(observation, table, 'Covered square')
        self.assertEqual(action, dict(type='click', x=20, y=30, button='left'))
        self.assertEqual(target['name'], 'Covered square')
        self.assertEqual(expectation['type'], 'changed_region')
        self.assertIsNone(state['source']['url'])
        wrong = copy.deepcopy(table)
        wrong['frame_sha256'] = 'b' * 64
        with self.assertRaises(ObservationError):
            planner.plan_step(observation, wrong, 'Covered square')

    def test_real_vision_adapter_refuses_unavailable_frame(self):
        with self.locked_adapter() as adapter:
            missing = dict(frame(), ramws_path='/dev/shm/nonexistent-program-test-frame')
            with self.assertRaises(PerceptionError):
                adapter.perceive(missing)

    def test_actual_vision_and_wender_follow_a_control_moved_between_acquisitions(self):
        root=resolve_locked_component('wender-agent')
        planner=program.WenderSubprocessAdapter(wender_root=root,verify_lock=True,
            command=[sys.executable,'-m','wender.plan.cli'])
        clicks=[];digests=[]
        with program.marks_workspace() as marks:
            with self.locked_adapter(timeout=10, marks_out=marks) as adapter:
                for generation,(left,top) in enumerate(((20,20),(55,45)),1):
                    pixels=bytearray(b'\xff'*(100*80*3))
                    for y in range(top,top+20):
                        for x in range(left,left+32):
                            if y in (top,top+19) or x in (left,left+31):
                                offset=(y*100+x)*3;pixels[offset:offset+3]=b'\0\0\0'
                    image=Frame(100,80,bytes(pixels));digest=hashlib.sha256(image.data).hexdigest()
                    ref=dict(frame(generation),sha256=digest)
                    vision=adapter.perceive(ref,image)
                    table=program.project_element_table(ref,vision)
                    self.assertTrue(table['elements'])
                    goal=table['elements'][0]['name']
                    observation=program.build_observation(ref,surface_id=ref['surface_id'],generation=generation)
                    action,expectation,state,target=planner.plan_step(observation,table,goal)
                    program.require_grounded_goal(table,goal,target)
                    program.require_click_target(action,target)
                    self.assertEqual(adapter.last_c2_payload['frame_sha256'],digest)
                    self.assertTrue(left-2<=action['x']<=left+33 and top-2<=action['y']<=top+21)
                    self.assertEqual(expectation['type'],'changed_region')
                    clicks.append((action['x'],action['y']));digests.append(digest)
        self.assertNotEqual(digests[0],digests[1])
        self.assertGreater(clicks[1][0],clicks[0][0])
        self.assertGreater(clicks[1][1],clicks[0][1])


if __name__ == '__main__':
    unittest.main()
