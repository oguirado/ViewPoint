"""Exercise shared reservation and private session custody without a guest."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import shutil
import socket
import stat
import tempfile
import threading
import unittest
from unittest import mock

from tools import arm64_capsule_session as session
from tools import mv_capsule_ramp as ramp
from test_arm64_capsules import Fixture


def shm_owner(uid):
    """Supply mount ownership independently of the test process's UID map."""
    mount = Path('/dev/shm')
    original_lstat = Path.lstat

    def fixture_lstat(path):
        metadata = original_lstat(path)
        if path != mount:
            return metadata
        fields = list(metadata)
        fields[stat.ST_UID] = uid
        return os.stat_result(fields)

    return mock.patch.object(Path, 'lstat', fixture_lstat)


class MultiLaunchTests(unittest.TestCase):
    def test_concurrent_slots_hold_distinct_locks_and_leave_registry_lock_free(self):
        with tempfile.TemporaryDirectory() as folder:
            registry = Path(folder)
            ready = threading.Barrier(2, timeout=3)
            run_ids = ['a' * 24, 'b' * 24]

            def launch(run_id):
                slot = session.reserve_multi(registry, run_id)
                with session.session_lock(slot):
                    session.publish_held_marker(slot, {'root': run_id, 'unit': run_id,
                                                       'status': 'closure-unproven'})
                    ready.wait()
                    if run_id == run_ids[0]:
                        with session.session_lock(registry):
                            self.assertFalse((registry / 'held-session.json').exists())
                    with self.assertRaises(BlockingIOError), session.session_lock(slot):
                        pass
                    return (slot / 'session.lock').stat().st_ino

            available = ramp.MEMORY_FLOOR_KIB + 2 * ramp.SESSION_RESERVATION_KIB
            with mock.patch.object(session, 'available_kib', return_value=available):
                with ThreadPoolExecutor(max_workers=2) as pool:
                    self.assertEqual(len(set(pool.map(launch, run_ids))), 2)
                with self.assertRaisesRegex(ValueError, 'memory floor'):
                    session.reserve_multi(registry, 'c' * 24)
            with session.session_lock(registry):
                self.assertFalse((registry / 'held-session.json').exists())
            for run_id in run_ids:
                slot = registry / 'multi-launch' / run_id
                marker = session.setup.decode(session.setup.read(slot / 'held-session.json', private=True))
                self.assertEqual(marker['root'], run_id)

    def test_full_request_refuses_one_kib_below_pinned_floor(self):
        with tempfile.TemporaryDirectory() as folder:
            registry = Path(folder)
            minimum = ramp.MEMORY_FLOOR_KIB + 2 * ramp.SESSION_RESERVATION_KIB
            with mock.patch.object(session, 'available_kib', return_value=minimum - 1):
                with self.assertRaisesRegex(ValueError, 'memory floor'):
                    session.preflight_multi(registry, 2)
            self.assertFalse((registry / 'multi-launch').exists())
            with mock.patch.object(session, 'available_kib', return_value=minimum):
                self.assertEqual(session.preflight_multi(registry, 2), 2 * ramp.SESSION_RESERVATION_KIB)

    def test_legacy_marker_and_unreconciled_slots_still_fence_admission(self):
        with tempfile.TemporaryDirectory() as folder:
            registry = Path(folder)
            legacy = registry / 'held-session.json'
            session.setup.write_new(legacy, {})
            with self.assertRaisesRegex(ValueError, 'requires reconciliation'):
                session.reserve_multi(registry, 'a' * 24)
            legacy.unlink()
            with mock.patch.object(session, 'available_kib', return_value=session.M7_MEMORY_FLOOR_KIB):
                slot = session.reserve_multi(registry, 'a' * 24)
                self.assertEqual(session.outstanding_slots(registry), 1)
                with self.assertRaisesRegex(ValueError, 'memory floor'):
                    session.reserve_multi(registry, 'b' * 24)
                self.assertFalse((slot / 'held-session.json').exists())

    def test_untrusted_registry_parent_refuses_before_registry_read(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            directory.chmod(0o755)
            with (mock.patch.dict(os.environ, {'DISPLAY': ':90'}),
                  mock.patch.object(session.setup, 'registry_path', return_value=directory / 'setup.json'),
                  mock.patch.object(session.setup, 'read') as read,
                  mock.patch.object(session.setup, 'interpreter_check') as probe):
                with self.assertRaisesRegex(ValueError, 'private directory'):
                    session.launch(None, None, None, None, None, None,
                                   multi_grant='/fixture/grant', multi_run_id='a' * 24, multi_display=':90')
            read.assert_not_called()
            probe.assert_not_called()

    def test_external_grant_binds_parent_start_group_and_registry(self):
        with tempfile.TemporaryDirectory() as folder:
            parent = session.process_identity(os.getppid())
            registry_sha = 'a' * 64
            grant = {'schema': 'metaviewer-arm64-launch-grant/v1', 'launcher_pid': parent['pid'],
                     'launcher_start': parent['start_ticks'], 'launcher_cgroup': parent['cgroup'],
                     'registry_sha256': registry_sha}
            for key, bad in (('launcher_pid', os.getpid()), ('launcher_start', parent['start_ticks'] + 1),
                             ('launcher_cgroup', '/foreign'), ('registry_sha256', 'b' * 64)):
                path = Path(folder) / key
                session.setup.write_new(path, dict(grant, **{key: bad}))
                with self.assertRaises(ValueError):
                    session.bind_multi_grant(path, registry_sha)
            path = Path(folder) / 'operator.json'
            session.setup.write_new(path, grant)
            self.assertEqual(session.bind_multi_grant(path, registry_sha)['pid'], parent['pid'])

    def test_live_profile_root_must_be_private_on_shm(self):
        with shm_owner(1), self.assertRaisesRegex(ValueError, 'root-owned sticky'):
            session.shm_base()
        with (shm_owner(0), tempfile.TemporaryDirectory() as unrelated,
              tempfile.TemporaryDirectory(dir='/dev/shm') as ram):
            self.assertEqual(session.private_ram_root(ram), Path(ram))
            with self.assertRaisesRegex(ValueError, 'on /dev/shm'):
                session.private_ram_root(unrelated)
            with self.assertRaisesRegex(ValueError, 'owner-private'):
                session.private_ram_root('/dev/shm')


class MultiLifecycleTests(Fixture):
    def setUp(self):
        super().setUp()
        environment = mock.patch.dict(os.environ)
        environment.start()
        self.addCleanup(environment.stop)
        os.environ.pop('DISPLAY', None)

    def test_concurrent_m7_lifecycle_binds_each_real_ledger_and_reconciliation(self):
        owner = session.process_identity(os.getpid())
        self.value['producer_pins']['setup'] = self.file(
            'source/tools/arm64_capsule_session.py', Path(session.__file__).read_bytes())
        self.file('source/tools/arm64_capsules.py', Path(session.setup.__file__).read_bytes())
        self.value['reconciler'] = self.file('source/tools/arm64_capsule_reconcile.py',
            Path(session.__file__).with_name('arm64_capsule_reconcile.py').read_bytes())
        registry = self.root / 'setup.json'
        session.setup.write_new(registry, self.value)
        transport = self.root / 'ui.json'
        endpoint = self.root / 'bus'
        bus = socket.socket(socket.AF_UNIX)
        self.addCleanup(bus.close)
        bus.bind(str(endpoint))
        session.setup.write_new(transport, {'schema': 'metaviewer-arm64-ui-transport/v1',
            'mode': 'trusted-user-invocation', 'display': ':90', 'xauthority': None,
            'dbus_session_bus_address': 'unix:path=' + str(endpoint)})
        observer = mock.Mock()
        observer.docker_ids.return_value = set()
        observer.pid_start.return_value = None
        observer.unit.return_value = {'LoadState': 'not-found', 'ActiveState': 'inactive',
                                      'ControlGroup': '', 'InvocationID': ''}
        observer.cgroup.return_value = None
        inventory = session.inventory_module(self.value)
        reconcile = inventory.reconcile_held
        inventory.reconcile_held = lambda path, *, release: reconcile(path, release=release, observer=observer)
        barrier = threading.Barrier(2, timeout=5)
        journals = {}
        allocate = session.allocate_session_root

        def allocate_root(run_id, rewind):
            root = allocate(run_id, rewind)
            self.addCleanup(shutil.rmtree, root)
            return root

        def fixture_scope(start, root, plan):
            run_id = plan['run_id']
            group = '/user.slice/user-%d.slice/user@%d.service/newton-ia.slice/' % (os.getuid(), os.getuid()) + plan['unit']
            session.initialize_inventory(self.value, root, plan, inventory)
            session.inventory_append(root, inventory, 'guardian_bound', id='guardian',
                unit=plan['unit'], invocation='a' * 32, cgroup=group, device=1, inode=2)
            session.inventory_append(root, inventory, 'app_pending', id='app')
            session.inventory_append(root, inventory, 'app_bound', id='app',
                pid=int(run_id[:4], 16), start_ticks=7, cgroup=group)
            config = root / 'inventory-config.json'
            for producer, pin in self.value['producer_pins'].items():
                inventory.seal_inventory(config, producer, Path(pin['path']))
            slot = session.multi_slot(self.root, run_id)
            with self.assertRaises(BlockingIOError), session.session_lock(slot):
                pass
            journals[run_id] = root / 'allocations.jsonl'
            barrier.wait()
            return 0

        def launch(run_id):
            return session.launch(self.value['package']['app']['path'], self.value['source_root'],
                str(transport), self.value['source_commit'], None, None, multi_grant='/fixture/grant',
                multi_run_id=run_id, multi_display=':90')

        run_ids = ['a' * 24, 'b' * 24]
        # The synthetic scope does not start the desktop bus proxy. The real
        # shared-memory mount and filesystem custody checks still run.
        with (shm_owner(0),
              mock.patch.object(session, '__file__', self.value['producer_pins']['setup']['path']),
              mock.patch.object(session, 'ui_bus_executable'),
              mock.patch.object(session, 'bind_multi_grant', return_value=owner),
              mock.patch.object(session, 'available_kib', return_value=36 * ramp.GIB_KIB),
              mock.patch.object(session, 'allocate_session_root', side_effect=allocate_root),
              mock.patch.object(session, 'monitor_scope', side_effect=fixture_scope),
              mock.patch.object(session, 'inventory_module', return_value=inventory),
              mock.patch.object(session.setup, 'registry_path', return_value=registry),
              mock.patch.object(session.setup, 'validate', return_value=self.value),
              mock.patch.object(session.setup, 'interpreter_check'),
              mock.patch.object(session.setup, 'host_check')):
            with ThreadPoolExecutor(max_workers=2) as pool:
                self.assertEqual(list(pool.map(launch, run_ids)), [0, 0])
        self.assertEqual(session.outstanding_slots(self.root), 0)
        heads = set()
        for run_id, journal in journals.items():
            rows = [json.loads(line) for line in journal.read_text().splitlines()]
            parts = [json.loads(row['payload']) for row in rows]
            self.assertEqual(parts[0]['run_id'], run_id)
            self.assertEqual(rows[0]['prev'], '0' * 64)
            self.assertEqual([row['prev'] for row in rows[1:]], [row['sha256'] for row in rows[:-1]])
            heads.add(rows[-1]['sha256'])
            self.assertIsNotNone(session.multi_proof(session.multi_slot(self.root, run_id)))
        self.assertEqual(len(heads), 2)
        source_slot = session.multi_slot(self.root, run_ids[0])
        foreign = session.multi_slot(self.root, 'c' * 24)
        foreign.mkdir(mode=0o700)
        raw = session.setup.read(source_slot / 'reservation.json', private=True)
        for name in ('reservation.json', 'held-session.json'):
            session.setup.write_bytes_new(foreign / name, raw)
        with session.session_lock(foreign):
            pass
        with self.assertRaisesRegex(ValueError, 'multi-session marker mismatch'):
            reconcile(foreign / 'held-session.json', release=True, observer=observer)
        self.assertTrue((foreign / 'held-session.json').exists())
