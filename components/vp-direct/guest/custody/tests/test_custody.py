"""Offline owner, inventory and absence evidence. No real guest is created."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import fcntl
import hashlib
import importlib.util
import inspect
import json
from pathlib import Path
import sys
import tempfile
import textwrap
import threading
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
DIRECT = HERE.parents[2]
REPO = DIRECT.parents[1]
sys.path.insert(0, str(DIRECT))
from guest import custody, imagepack

# Reuse the reconciler's actual source-bound inventory fixture and its observation
# double, including the synthetic payload ownership which normally requires root.
spec = importlib.util.spec_from_file_location("arm64_fixture", REPO / "tests/test_arm64_capsule_reconcile.py")
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)


def mutate_reserve(guard, replacement):
    source = textwrap.dedent(inspect.getsource(custody.CustodyBook.reserve))
    if source.count(guard) != 1:
        raise AssertionError("mutation must replace exactly one production guard")
    namespace = dict(vars(custody))
    exec(compile(source.replace(guard, replacement), "<reserve mutant>", "exec"), namespace)
    return patch.object(custody.CustodyBook, "reserve", namespace["reserve"])


class CustodyTests(unittest.TestCase):
    def setUp(self):
        def local_tmp(*args, **kwargs):
            return temporary_directory(*args, **dict(kwargs, dir=HERE))

        temporary_directory = tempfile.TemporaryDirectory
        protected_dir = Path(self.enterContext(temporary_directory(dir=HERE)))
        reconciler = protected_dir / "arm64_capsule_reconcile.py"
        reconciler.write_bytes(Path(fixture.r.__file__).read_bytes())
        reconciler.chmod(0o500)
        spec = importlib.util.spec_from_file_location("fixture_reconciler", reconciler)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.enterContext(patch.object(fixture, "r", module))

        self.source = fixture.InventoryTests()
        self.addCleanup(self.source.doCleanups)
        with patch.object(tempfile, "TemporaryDirectory", local_tmp):
            self.source.setUp()
        self.s = self.source
        self.pack = json.loads((DIRECT / "schemas/tests/examples/vp.image_pack_v1.valid.json").read_text())
        config = self.pack["resolved"]
        for name, data in {"kernel": bytes(56) + b"ARM\x64" + bytes(4),
                           "root": b"offline root", "initrd": b"offline initrd"}.items():
            path = self.s.directory / name
            path.write_bytes(data)
            config[name] = {"path": str(path), "sha256": hashlib.sha256(data).hexdigest()}
        self.pack["requested"] = deepcopy(config)
        self.catalogue = imagepack.ImagePackCatalogue(self.pack)
        self.enterContext(patch.object(imagepack.platform, "machine", return_value="aarch64"))
        self.book = self.reopen()
        self.guest = {"schema": "vp.guest/v1", "kind": "managed_vm", "surface_id": "surface-one",
                      "workload_id": "work-one", "incarnation": "inc-one", "authority_epoch": 1,
                      "profile_id": "notepad-vm", "custody": {"vmm": True, "disk": True, "snapshot": False}}

    def reopen(self, **budget):
        return custody.CustodyBook(self.s.registry, self.catalogue, reconciler=fixture.r,
                                   **({"cpu": 1, "ram_bytes": 512 << 20} | budget))

    def reserve(self, guest=None, book=None, **kw):
        return (book or self.book).reserve(guest or self.guest,
                                          **({"run_id": self.s.run, "transport": "virtio-serial"} | kw))

    def archive(self):
        self.s.native_resource()
        self.s.seal()
        return self.reconcile(self.s.Absent())

    def reconcile(self, observer):
        with self.s.payload_metadata():
            result = fixture.r.reconcile_held(self.s.marker, release=True, observer=observer)
        self.assertTrue(result["closure_proven"])
        return Path(result["proof"])

    def assert_held(self, reservation):
        self.assertEqual(self.reopen().status(reservation.reservation_id, self.guest), reservation)
        self.assertTrue(reservation.held)

    def test_charge_and_exact_normal_closure_survive_restart(self):
        res = self.reserve()
        self.assertEqual((res.cpu, res.ram_bytes), (1, 512 << 20))
        self.assertEqual(self.reserve(book=self.reopen()), res)
        proof = self.archive()
        settled = self.reopen().settle(res.reservation_id, self.guest, proof)
        self.assertFalse(settled.held)
        self.assertEqual(settled.proof_sha256, hashlib.sha256(proof.read_bytes()).hexdigest())
        self.assertEqual(self.book.settle(res.reservation_id, self.guest, proof), settled)

    def test_each_workload_needs_independent_capacity_identity_and_proof(self):
        for budget in ({"cpu": 0}, {"ram_bytes": (512 << 20) - 1}):
            with self.subTest(**budget), self.assertRaisesRegex(custody.CustodyRefused, "CAPACITY"):
                self.reserve(book=self.reopen(**budget))
        first = self.reserve()
        second = dict(self.guest, workload_id="work-two", surface_id="surface-two")
        with self.assertRaisesRegex(custody.CustodyRefused, "CAPACITY"):
            self.reserve(second)
        proof = self.archive()
        for guest in (second, dict(self.guest, authority_epoch=2), dict(self.guest, incarnation="next")):
            with self.subTest(guest=guest), self.assertRaisesRegex(custody.CustodyRefused, "OWNER"):
                self.book.settle(first.reservation_id, guest, proof)
        self.assert_held(first)

    def test_new_session_gets_new_reservation_and_cannot_reuse_first_archive(self):
        first = self.reserve()
        old_archive = self.archive()
        self.book.settle(first.reservation_id, self.guest, old_archive)
        registry = self.s.registry
        base_setup = fixture.ReconcileTests.setUp

        def new_session(test):
            base_setup(test)
            old_run = test.run
            test.run = "b" * 24
            new_root = test.root.with_name(test.root.name.replace(old_run, test.run))
            test.root.rename(new_root)
            test.root = new_root
            test.unit = test.unit.replace(old_run, test.run)
            test.plan.update(run_id=test.run, unit=test.unit)
            test.write(test.root / "PLAN.json", test.plan)
            test.registry = registry
            test.marker = registry / "held-session.json"
            test.write(test.marker, dict(test.held, root=str(test.root), unit=test.unit))

        second_source = fixture.InventoryTests()
        self.addCleanup(second_source.doCleanups)
        with patch.object(fixture.ReconcileTests, "setUp", new_session):
            temporary_directory = tempfile.TemporaryDirectory
            with patch.object(tempfile, "TemporaryDirectory",
                              side_effect=lambda **kw: temporary_directory(**(kw | {"dir": HERE}))):
                second_source.setUp()
        self.s = second_source
        second_guest = dict(self.guest, workload_id="work-two", incarnation="inc-two")
        second = self.reserve(second_guest)
        self.assertNotEqual(first.reservation_id, second.reservation_id)
        proof = self.archive()
        with self.assertRaisesRegex(custody.CustodyRefused, "OWNER"):
            self.book.settle(second.reservation_id, second_guest, old_archive)
        self.assertTrue(self.book.status(second.reservation_id, second_guest).held)
        self.assertFalse(self.book.settle(second.reservation_id, second_guest, proof).held)

    def test_two_callers_racing_for_one_slot(self):
        barrier = threading.Barrier(2)
        def request(i):
            book = self.reopen()
            barrier.wait(timeout=5)
            try:
                return self.reserve(dict(self.guest, workload_id=f"work-{i}"), book=book).held
            except custody.CustodyRefused as error:
                return error.code
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertCountEqual(pool.map(request, range(2)), [True, "CAPACITY"])

    def test_app_exit_preserves_held_marker_bytes_and_charge(self):
        res = self.reserve()
        before = self.s.marker.read_bytes()
        self.s.event(0, "app_exited_closure_unproven", returncode=0)
        audit = fixture.r.reconcile_held(self.s.marker)
        self.assertFalse(audit["closure_proven"])
        with self.assertRaises((ValueError, OSError)):
            self.book.settle(res.reservation_id, self.guest, self.s.root / "event-0000.json")
        self.assertEqual(self.s.marker.read_bytes(), before)
        self.assert_held(res)

    def test_deleted_marker_alone_neither_admits_nor_releases(self):
        res = self.reserve()
        self.s.marker.unlink()
        with self.assertRaisesRegex(custody.CustodyRefused, "CAPACITY"):
            self.reserve(dict(self.guest, workload_id="next"))
        with self.assertRaises(FileNotFoundError):
            self.book.settle(res.reservation_id, self.guest,
                             self.s.registry / f"reconciled-{self.s.run}-{'a'*16}.json")
        self.assert_held(res)

    def test_uncertain_unsealed_and_still_live_refuse(self):
        res = self.reserve()
        self.s.native_resource()
        marker = self.s.marker.read_bytes()
        with self.assertRaisesRegex(fixture.r.Refused, "unsealed"):
            self.reconcile(self.s.Absent())
        self.s.seal()
        cid = self.s.cid
        class Present(self.s.Absent):
            def docker_ids(self):
                return {cid}
        with self.assertRaisesRegex(fixture.r.Refused, "container remains"):
            self.reconcile(Present())
        self.assertEqual(self.s.marker.read_bytes(), marker)
        self.assert_held(res)

    def test_pack_verification_leaves_native_writer_free_and_rechecks_capacity(self):
        read_pack = self.catalogue.pack_list

        def verify_while_producer_writes():
            packs = read_pack()
            self.s.append("graphical", {"producer": "graphical", "stage": "unknown",
                                       "id": "raced", "reason": "unsettled creation"})
            return packs

        with patch.object(self.catalogue, "pack_list", side_effect=verify_while_producer_writes):
            with self.assertRaisesRegex(custody.CustodyRefused, "CAPACITY"):
                self.reserve()

    def test_busy_producer_blocks_reservation(self):
        with (self.s.root / "allocations.lock").open("rb") as writer:
            fcntl.flock(writer, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(custody.CustodyRefused, "CAPACITY"):
                self.reserve()
        self.assertTrue(self.reserve().held)

    def test_unknown_allocation_blocks_reservation(self):
        self.s.append("graphical", {"producer": "graphical", "id": "unsettled",
                                    "stage": "unknown", "reason": "unknown outcome"})
        with self.assertRaisesRegex(custody.CustodyRefused, "CAPACITY"):
            self.reserve()

    def test_host_transport_pack_viewer_and_foreign_run_refused(self):
        for transport in (":0", "x11", "host-xwd", "ax_only"):
            with self.subTest(transport=transport), self.assertRaisesRegex(
                    custody.CustodyRefused, "DISPLAY_UNSUPPORTED"):
                self.reserve(transport=transport)
        viewer = json.loads((DIRECT / "schemas/tests/examples/vp.guest_v1.valid.json").read_text())
        with self.assertRaisesRegex(custody.CustodyRefused, "OWNER"):
            self.reserve(viewer)
        with self.assertRaisesRegex(custody.CustodyRefused, "OWNER"):
            self.reserve(run_id="b" * 24)
        Path(self.pack["resolved"]["kernel"]["path"]).write_bytes(b"unpinned")
        with self.assertRaises(imagepack.ImagePackError):
            self.reserve()

    def test_archived_proof_cannot_close_different_native_surface(self):
        guest = dict(self.guest, surface_id="missing-surface")
        res = self.reserve(guest)
        with self.assertRaisesRegex(custody.CustodyRefused, "no bound VM"):
            self.book.settle(res.reservation_id, guest, self.archive())
        self.assertTrue(self.reopen().status(res.reservation_id, guest).held)

    def test_replaced_evidence_or_remaining_marker_keeps_charge(self):
        res = self.reserve()
        marker = self.s.marker.read_bytes()
        path = self.archive()
        proof = json.loads(path.read_bytes())
        for field, value in (("held_sha256", "0"*64), ("inventory_sha256", "0"*64),
                             ("run_id", "b"*24), ("absent_container_ids", []),
                             ("proofs", proof["proofs"][1:])):
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.s.write(path, dict(proof, **{field: value}))
                self.book.settle(res.reservation_id, self.guest, path)
        self.s.write(path, proof)
        self.s.marker.write_bytes(marker)
        self.s.marker.chmod(0o600)
        with self.assertRaisesRegex(custody.CustodyRefused, "CAPACITY"):
            self.book.settle(res.reservation_id, self.guest, path)
        self.assert_held(res)

    def test_mutant_remove_closure_fence(self):
        res = self.reserve()
        # A broken settle accepts an exit event as the proof. The independent
        # durable-charge oracle sees capacity released while the source still holds it.
        self.s.event(0, "app_exited_closure_unproven", returncode=0)
        with patch.object(self.book, "_closure_digest", return_value="0"*64) as removed:
            self.book.settle(res.reservation_id, self.guest, self.s.root / "event-0000.json")
            removed.assert_called_once()
        with self.assertRaises(AssertionError):
            self.assert_held(res)
        self.assertTrue(self.s.marker.exists())

    def test_mutant_share_reservation(self):
        res = self.reserve()
        guest = dict(self.guest, workload_id="work-two", surface_id="surface-two")
        guard = '_require(active is None, "CAPACITY", "unsettled workload")'
        shared = ('if active is not None: return _view(db.execute('
                  '"SELECT * FROM reservations WHERE proof_sha256 IS NULL").fetchone())')
        with mutate_reserve(guard, shared):
            returned = self.reserve(guest)
        self.assertEqual(returned.reservation_id, res.reservation_id)
        with self.assertRaises(AssertionError):
            self.assertEqual((returned.workload_id, returned.surface_id),
                             (guest["workload_id"], guest["surface_id"]))

    def test_mutant_substitute_host_transport(self):
        with mutate_reserve('transport == "virtio-serial"', 'transport in ("virtio-serial", "host-xwd")'):
            with self.assertRaises(AssertionError):
                with self.assertRaisesRegex(custody.CustodyRefused, "DISPLAY_UNSUPPORTED"):
                    self.reserve(transport="host-xwd")
        # The mutant reached native-slot accounting with the substituted transport.
        with self.assertRaisesRegex(custody.CustodyRefused, "CAPACITY"):
            self.reserve(dict(self.guest, workload_id="other"))
