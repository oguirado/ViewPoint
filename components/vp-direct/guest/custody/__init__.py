"""Owner-side accounting for the managed ARM64 guest, before H5 integration.

One registry is one capacity domain. The setup helper owns its session fence,
Core owns creation, and the ARM64 reconciler alone proves session closure.
This book reserves the pinned profile for a workload and consumes its matching
normal reconciliation archive. It never starts, stops or releases a session.
"""
from contextlib import closing, contextmanager
from dataclasses import dataclass
import fcntl
import importlib.util
import os
from pathlib import Path
import re
import sqlite3
import uuid

from schemas.validation import canonical_bytes, validate_contract

# Use the same protected inventory reader as the setup helper. The standalone
# reconciler is a repository tool, outside the vp-direct Python import root.
_path = Path(__file__).resolve().parents[4] / "tools/arm64_capsule_reconcile.py"
_spec = importlib.util.spec_from_file_location("arm64_capsule_reconcile", _path)
inventory = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(inventory)


class CustodyRefused(ValueError):
    def __init__(self, code, detail):
        self.code = code
        super().__init__(f"{code}: {detail}")


def _require(condition, code, detail):
    if not condition:
        raise CustodyRefused(code, detail)


@dataclass(frozen=True)
class Reservation:
    reservation_id: str
    workload_id: str
    surface_id: str
    cpu: int
    ram_bytes: int
    proof_sha256: str | None

    @property
    def held(self):
        return self.proof_sha256 is None


def _view(row):
    return Reservation(*(row[key] for key in Reservation.__dataclass_fields__))


def _source(root, reconciler):
    config, raw = inventory.load_config(root / "inventory-config.json")
    plan = inventory.decode(inventory.read_private(root / "PLAN.json"))
    _require(plan.get("schema") == "metaviewer-arm64-session/v1"
             and plan.get("run_id") == config["run_id"]
             and plan.get("unit") == f'metaviewer-arm64-{config["run_id"]}.scope',
             "OWNER", "session plan differs from inventory")
    reconciler.descriptor_context(root, plan, config)
    return config, inventory.digest(raw), plan


@contextmanager
def _allocation_lock(root):
    lock = root / "allocations.lock"
    fd = os.open(lock, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        inventory.private_file(os.fstat(fd))
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            raise CustodyRefused("CAPACITY", "allocation update in progress") from None
        _require(inventory.identity(lock.lstat()) == inventory.identity(os.fstat(fd)),
                 "CAPACITY", "allocation lock changed")
        yield
    finally:
        os.close(fd)


def _check_absence(state, proof):
    """Replay the native reconciler's exact process/unit absence predicates."""
    processes = [state["app"]["bound"]]
    units = [state["guardian"]]
    containers = []
    for item in state["resources"].values():
        stage = item["pending"]["stage"]
        bound = item["bound"]
        if stage in ("native_pending", "helper_pending"):
            containers.append(bound["cid"])
        if stage == "native_pending":
            processes.append({"pid": bound["native_pid"]["pid"],
                              "start_ticks": bound["native_pid"]["start"]})
        if stage == "service_pending":
            processes.append(bound)
            units.append(bound)
    _require(len(containers) == len(set(containers))
             and sorted(containers) == proof["absent_container_ids"],
             "OWNER", "container absence differs from bound inventory")
    records = proof["proofs"]
    _require(isinstance(records, list) and len(records) == len(processes) + len(units)
             and all(isinstance(record, dict) for record in records),
             "OWNER", "incomplete closure observations")
    records = list(records)
    for bound in processes:
        matches = [r for r in records if (r.get("pid"), r.get("start")) ==
                   (bound["pid"], bound["start_ticks"])]
        _require(matches, "OWNER", "missing process absence")
        record = matches[0]
        expected = inventory.prove_process(_ArchiveObservation(record),
                                           bound["pid"], bound["start_ticks"])
        _require(record == expected, "OWNER", "invalid process absence")
        records.remove(record)
    for bound in units:
        matches = [r for r in records if r.get("unit") == bound["unit"]]
        _require(matches, "OWNER", "missing unit absence")
        record = matches[0]
        expected = inventory.prove_unit(_ArchiveObservation(record), bound)
        _require(record == expected, "OWNER", "invalid unit absence")
        records.remove(record)


class _ArchiveObservation:
    """Feed a single reconciler observation back through its native predicate."""
    def __init__(self, record):
        self.record = record

    def pid_start(self, pid):
        _require("observed_start" in self.record, "OWNER", "missing process readback")
        return self.record["observed_start"]

    def unit(self, unit):
        _require("unit_state" in self.record, "OWNER", "missing unit readback")
        return self.record["unit_state"]

    def cgroup(self, cgroup):
        _require("cgroup_state" in self.record, "OWNER", "missing cgroup readback")
        return self.record["cgroup_state"]


class CustodyBook:
    """Trusted setup supplies the private registry, G1 catalogue and available budget.

    reserve() takes a trusted run ID and vp.guest/v1 before native allocation.
    status() can be used after visual/app exit. Only settle() changes the charge.
    """

    def __init__(self, registry, catalogue, *, cpu, ram_bytes, reconciler=None):
        self.registry = inventory.private_dir(registry)
        self.marker = self.registry / "held-session.json"
        self.db = self.registry / "guest-custody.sqlite3"
        # Installed setups pin their protected reconciler path inside setup.json.
        self.reconciler = inventory if reconciler is None else reconciler
        self.catalogue = catalogue
        for amount in (cpu, ram_bytes):
            _require(type(amount) is int and amount > 0, "CAPACITY", "unknown capacity")
        self.cpu = cpu
        self.ram_bytes = ram_bytes
        fd = os.open(self.db, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        try:
            inventory.private_file(os.fstat(fd))
        finally:
            os.close(fd)
        with self._transaction() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS reservations (
                reservation_id TEXT PRIMARY KEY, workload_id TEXT NOT NULL,
                surface_id TEXT NOT NULL, guest BLOB NOT NULL, run_id TEXT NOT NULL,
                root TEXT NOT NULL, held_digest TEXT NOT NULL, config_digest TEXT NOT NULL,
                prefix_count INTEGER NOT NULL, prefix_head TEXT NOT NULL, pack_digest TEXT NOT NULL,
                cpu INTEGER NOT NULL, ram_bytes INTEGER NOT NULL, proof_sha256 TEXT,
                UNIQUE(run_id, workload_id), UNIQUE(run_id, surface_id))""")
            # The one closed graphical profile has one native slot. H5 owns
            # any change to coexistence with the other Core providers.
            db.execute("""CREATE UNIQUE INDEX IF NOT EXISTS held_slot
                          ON reservations ((1)) WHERE proof_sha256 IS NULL""")

    @contextmanager
    def _transaction(self):
        inventory.private_file(self.db.lstat())
        with closing(sqlite3.connect(self.db, isolation_level=None)) as db:
            db.row_factory = sqlite3.Row
            db.execute("BEGIN IMMEDIATE")
            with db:
                yield db

    def reserve(self, guest, *, run_id, transport):
        validate_contract("vp.guest/v1", guest)
        _require(guest["kind"] == "managed_vm" and guest["profile_id"] == "notepad-vm"
                 and not guest["custody"]["snapshot"], "OWNER", "unsupported guest custody")
        _require(transport == "virtio-serial", "DISPLAY_UNSUPPORTED", "guest transport required")
        guest_bytes = canonical_bytes(guest)
        try:
            marker = inventory.read_private(self.marker)
        except FileNotFoundError:
            raise CustodyRefused("CAPACITY", "session fence missing") from None
        held = inventory.decode(marker)
        inventory.closed(held, ("root", "unit", "status"))
        _require(held["status"] == "closure-unproven" and
                 held["unit"] == f"metaviewer-arm64-{run_id}.scope",
                 "OWNER", "different session fence")
        root = inventory.private_dir(held["root"])
        config, config_digest, plan = _source(root, self.reconciler)
        _require(config["run_id"] == run_id, "OWNER", "different run")
        held_digest = inventory.digest(marker)
        packs = self.catalogue.pack_list()["packs"]
        _require(len(packs) == 1 and packs[0]["profile_id"] == guest["profile_id"],
                 "CAPACITY", "pinned profile unavailable")
        pack = packs[0]
        pack_digest = inventory.digest(canonical_bytes(pack))
        # Image and descriptor pin verification can be slow. Keep it outside
        # the native producer lock, then recheck the source before accounting.
        with _allocation_lock(root), self._transaction() as db:
            _require(inventory.read_private(self.marker) == marker
                     and inventory.digest(inventory.read_private(root / "inventory-config.json"))
                     == config_digest and inventory.decode(inventory.read_private(root / "PLAN.json"))
                     == plan, "CAPACITY", "setup changed during reservation")
            existing = db.execute("SELECT * FROM reservations WHERE run_id=? AND workload_id=?",
                                  (run_id, guest["workload_id"])).fetchone()
            if existing is not None:
                _require(existing["guest"] == guest_bytes
                         and existing["held_digest"] == held_digest
                         and existing["config_digest"] == config_digest
                         and existing["pack_digest"] == pack_digest and _view(existing).held,
                         "OWNER", "reservation belongs to a different lifetime")
                return _view(existing)
            active = db.execute("SELECT 1 FROM reservations WHERE proof_sha256 IS NULL").fetchone()
            _require(active is None, "CAPACITY", "unsettled workload")
            limits = pack["resolved"]
            _require(limits["cpu"] <= self.cpu and limits["ram_bytes"] <= self.ram_bytes,
                     "CAPACITY", "profile exceeds available budget")
            journal = inventory.read_journal(config["journal"])
            state = inventory.inventory_state(config, journal)
            events = inventory.event_inventory(root, plan)
            stopping = any(event["event"] in ("app_exited_closure_unproven", "app_stop_requested",
                                              "app_stop_unknown") for event in events)
            _require(not stopping and not os.path.lexists(root / "STOP_REQUESTED"),
                     "CAPACITY", "session is stopping")
            _require(state["app"] and "bound" in state["app"] and not state["sealed"]
                     and all(item.get("pending", {}).get("stage") == "helper_pending"
                             and "closed" in item for item in state["resources"].values()),
                     "CAPACITY", "source capacity is occupied or uncertain")
            _require(inventory.read_private(self.marker) == marker
                     and inventory.read_journal(config["journal"]) == journal,
                     "CAPACITY", "custody changed during reservation")
            rid = uuid.uuid4().hex
            db.execute("""INSERT INTO reservations
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)""",
                       (rid, guest["workload_id"], guest["surface_id"], guest_bytes, run_id,
                        str(root), held_digest, config_digest, len(state["rows"]),
                        state["rows"][-1]["sha256"], pack_digest,
                        limits["cpu"], limits["ram_bytes"]))
            return _view(self._row(db, rid, guest))

    def _row(self, db, reservation_id, guest):
        row = db.execute("SELECT * FROM reservations WHERE reservation_id=?",
                         (reservation_id,)).fetchone()
        _require(row is not None and row["guest"] == canonical_bytes(guest),
                 "OWNER", "workload, incarnation or authority differs")
        return row

    def status(self, reservation_id, guest):
        with self._transaction() as db:
            return _view(self._row(db, reservation_id, guest))

    def _closure_digest(self, row, archive):
        run_id = row["run_id"]
        path = Path(archive)
        _require(path.parent == self.registry and
                 re.fullmatch(rf"reconciled-{re.escape(run_id)}-[0-9a-f]{{16}}\.json", path.name),
                 "OWNER", "normal reconciler archive required")
        raw = inventory.read_private(path)
        proof = inventory.decode(raw)
        inventory.closed(proof, ("schema", "run_id", "held_sha256", "inventory_sha256", "head",
                                 "proofs", "absent_container_ids"))
        _require(proof["schema"] == "arm64-capsule-reconciliation-proof/v1"
                 and proof["run_id"] == run_id and proof["held_sha256"] == row["held_digest"],
                 "OWNER", "proof belongs to a different fence")
        config, digest, _ = _source(Path(row["root"]), self.reconciler)
        ledger = inventory.read_journal(config["journal"])
        state = inventory.inventory_state(config, ledger, complete=True)
        rows = state["rows"]
        _require(digest == row["config_digest"] and len(rows) > row["prefix_count"]
                 and rows[row["prefix_count"] - 1]["sha256"] == row["prefix_head"]
                 and proof["inventory_sha256"] == inventory.digest(ledger)
                 and proof["head"] == rows[-1]["sha256"], "OWNER", "inventory differs")
        resource = state["resources"].get("graphical:" + row["surface_id"])
        _require(resource and resource["pending"].get("native_kind") == "vm",
                 "OWNER", "workload has no bound VM allocation")
        _check_absence(state, proof)
        try:
            current = inventory.decode(inventory.read_private(self.marker))
        except FileNotFoundError:
            pass  # The independent reconciler has removed the marker.
        else:
            inventory.closed(current, ("root", "unit", "status"))
            _require(current["status"] == "closure-unproven" and current["root"] != row["root"]
                     and current.get("unit") != f"metaviewer-arm64-{run_id}.scope",
                     "CAPACITY", "original fence still held")
        return inventory.digest(raw)

    def settle(self, reservation_id, guest, archive):
        """Consume the reconciler's evidence without editing its marker or inventory."""
        with self._transaction() as db:
            row = self._row(db, reservation_id, guest)
            digest = self._closure_digest(row, archive)
            _require(row["proof_sha256"] in (None, digest), "OWNER", "closure proof changed")
            db.execute("UPDATE reservations SET proof_sha256=? WHERE reservation_id=?",
                       (digest, reservation_id))
            return _view(self._row(db, reservation_id, guest))
