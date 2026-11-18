"""Restartable P2 owner for lifecycle wake delivery, on broker-private storage.

P2 still owns admission, ordering, claims, credit and drain. Persist its state
under its publication lock before a notified reader can take the work. A
replacement issues fresh registration handles only after lease revalidation.
Claimed deliveries are never enqueued on recovery.
"""
from collections import Counter, deque
from contextlib import closing, contextmanager
from dataclasses import asdict
from hashlib import sha256
import fcntl
import os
from pathlib import Path
import sqlite3
import uuid
import json

from perception.demand import DemandCoordinator, DemandError, Work
from perception.demand.coordinator import _Subscription
from schemas.validation import canonical_bytes, validate_contract


class RecoverableDemandCoordinator(DemandCoordinator):
    def __init__(self, path, binding, *, interests, authorize, drain):
        validate_contract("vp.surface_binding/v1", binding)
        super().__init__(binding["surface_id"], interests=interests, authorize=authorize, drain=drain)
        self._path = Path(path).absolute()
        self._owner_file = os.fdopen(os.open(self._path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600), "r+b")
        self._wake_claims = {}
        self._uri = self._path.as_uri() + "?mode=rw"
        self._resumable = set()
        self._registrations = {}
        self._drain_due = False
        self.lifecycle_bound = False
        try:
            fcntl.flock(self._owner_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with closing(sqlite3.connect(self._uri, uri=True)) as db, db:
                db.execute("CREATE TABLE IF NOT EXISTS demand_owner "
                           "(id INTEGER PRIMARY KEY CHECK(id=1), binding BLOB, source TEXT, state BLOB)")
                db.execute("CREATE TABLE IF NOT EXISTS lifecycle_publication_ack "
                           "(id INTEGER PRIMARY KEY CHECK(id=1), boundary TEXT NOT NULL)")
                row = db.execute("SELECT binding,source,state FROM demand_owner WHERE id=1").fetchone()
                identity = canonical_bytes([binding, sorted(self._interests)])
                if row is None:
                    self.source_id = uuid.uuid4().hex
                    db.execute("INSERT INTO demand_owner VALUES(1,?,?,?)",
                               (identity, self.source_id, self._image()))
                else:
                    if row[0] != identity:
                        raise DemandError("identity_mismatch")
                    self.source_id = row[1]
                    self._restore(json.loads(row[2]))
        except BaseException:
            self._owner_file.close()
            raise

    def _image(self):
        subscriptions = [dict(registration=self._registrations[s.record["demand_id"]],
                              record=s.record, active=s.active, sequence=s.sequence,
                              outstanding=s.outstanding) for s in self._subscriptions.values()]
        return canonical_bytes(dict(state=self._state, subscriptions=subscriptions,
                                    queues={k: [asdict(w) for w in q] for k, q in self._queues.items()},
                                    claimed=[asdict(w) for w in self._claimed], wake_claims=self._wake_claims,
                                    borrows=[asdict(w) for w in self._borrows]))

    def _restore(self, saved):
        # Borrowed memory and an unacknowledged producer drain need external closure.
        if saved["borrows"] or saved["state"] in ("draining", "drain_failed"):
            raise DemandError("HOLD")
        for row in saved["subscriptions"]:
            record = row["record"]
            validate_contract("vp.demand/v1", record)
            if self._authorize(record.copy()) is not True:
                raise DemandError("NO_LEASE")
            sub = _Subscription(record, object(), row["active"], row["sequence"], row["outstanding"])
            self._subscriptions[record["demand_id"]] = sub
            self._registrations[record["demand_id"]] = row["registration"]
            if sub.active:
                self._by_handle[sub.handle] = sub
                self._resumable.add(record["demand_id"])
        self._queues = {k: deque(Work(**w) for w in q) for k, q in saved["queues"].items()}
        self._claimed = {Work(**w) for w in saved["claimed"]}
        self._wake_claims = saved["wake_claims"]
        work = [w for queue in self._queues.values() for w in queue] + list(self._claimed)
        counts = Counter(w.demand_id for w in work)
        if (set(self._queues) != self._interests or saved["state"] not in ("idle", "in_use")
                or bool(self._subscriptions) != (saved["state"] == "in_use")
                or set(counts) - self._subscriptions.keys()):
            raise DemandError("HOLD")
        for key, sub in self._subscriptions.items():
            if not counts[key] == sub.outstanding <= sub.record["credits"]:
                raise DemandError("HOLD")
        self._state = saved["state"]

    def check_owner(self):
        if self._owner_file.closed:
            raise DemandError("NO_LEASE")

    def _save(self):
        try:
            with closing(sqlite3.connect(self._uri, uri=True)) as db, db:
                updated = db.execute("UPDATE demand_owner SET state=? WHERE id=1 AND source=?",
                                     (self._image(), self.source_id))
                if updated.rowcount != 1:
                    raise DemandError("HOLD")
        except BaseException:
            # No callback or claim can observe unpublished work after a storage failure.
            self._owner_file.close()
            raise

    def publication_acknowledged(self, boundary):
        with self._lock, closing(sqlite3.connect(self._uri, uri=True)) as db:
            self.check_owner()
            return db.execute("SELECT 1 FROM lifecycle_publication_ack WHERE boundary=?",
                              (boundary,)).fetchone() is not None

    def acknowledge_publication(self, boundary):
        with self._lock, closing(sqlite3.connect(self._uri, uri=True)) as db, db:
            self.check_owner()
            db.execute("INSERT OR REPLACE INTO lifecycle_publication_ack VALUES(1, ?)", (boundary,))

    def retire_owner(self):
        """Release sole-writer custody. Process exit also releases this kernel lock."""
        with self._lock:
            self._owner_file.close()
            for ready in self._ready.values():
                ready.notify_all()

    def snapshot(self):
        with self._lock:
            self.check_owner()
            return super().snapshot()

    def _retire_if_settled(self, sub):
        drain = super()._retire_if_settled(sub)
        key = sub.record["demand_id"]
        if key not in self._subscriptions:
            self._registrations.pop(key, None)
            self._resumable.discard(key)
        return drain

    def _finish_drain(self):
        # P2 calls here after its mutation. Persist that fence before calling out.
        self._drain_due = True

    def _change(self, operation, *args, **kwargs):
        with self._lock:
            self.check_owner()
            result = operation(*args, **kwargs)
            self.check_owner()  # a waiting claim can outlive the old owner
            self._save()
            draining, self._drain_due = self._drain_due, False
        if draining:
            try:
                super()._finish_drain()
            finally:
                with self._lock:
                    self.check_owner()
                    self._save()
        return result

    @contextmanager
    def register(self, demand):
        subscription = super().register(demand)
        def enter():
            handle = subscription.__enter__()
            self._registrations[demand["demand_id"]] = uuid.uuid4().hex
            return handle

        handle = self._change(enter)
        try:
            yield handle
        finally:
            with self._lock:
                retired = self._owner_file.closed
            if not retired:
                self._change(subscription.__exit__, None, None, None)

    @contextmanager
    def resume_subscription(self, demand):
        """Reconnect one surviving publisher using its exact reauthorized lease."""
        with self._lock:
            self.check_owner()
            key = demand["demand_id"]
            sub = self._subscriptions.get(key)
            if (key not in self._resumable or sub is None or sub.record != demand
                    or self._authorize(demand.copy()) is not True):
                raise DemandError("NO_LEASE")
            self._resumable.remove(key)
        try:
            yield sub.handle
        finally:
            if not self._owner_file.closed:
                self._change(self._cancel_resumed, sub)

    def _cancel_resumed(self, sub):
        sub.active = False
        del self._by_handle[sub.handle]
        if self._retire_if_settled(sub):
            self._finish_drain()

    def recovery_claims(self):
        """Outstanding handles for trusted reconciliation only. Never hand them to a worker."""
        with self._lock:
            self.check_owner()
            return {self.work_token(work): work for work in self._claimed}

    def work_token(self, work):
        with self._lock:
            self.check_owner()
            if work not in self._claimed:
                raise DemandError("NO_LEASE")
            registration = self._registrations[work.demand_id]
            return sha256(canonical_bytes([self.source_id, registration, asdict(work)])).hexdigest()

    def claim_for_wake(self, goal_id):
        def claim():
            work = super(RecoverableDemandCoordinator, self).claim("VERIFIED", budget=1, timeout=0)
            if work:
                self._wake_claims[self.work_token(work[0])] = goal_id
            return work
        return self._change(claim)

    def recover_unbound_claim(self, ledger):
        """Restore a claim lost before U4a admitted it. It could not have left P4."""
        def recover():
            claims = self.recovery_claims()
            missing = [token for token, goal in self._wake_claims.items()
                       if goal == ledger["goal_id"] and token not in ledger["work"]]
            if len(missing) > 1:
                raise DemandError("HOLD")
            for token in missing:
                work = claims[token]
                self._claimed.remove(work)
                self._queues["VERIFIED"].appendleft(work)
                del self._wake_claims[token]
                self._ready["VERIFIED"].notify_all()
        self._change(recover)

    def publish(self, handle, sequence, reference):
        return self._change(super().publish, handle, sequence, reference)

    def claim(self, interest, *, budget, timeout):
        return self._change(super().claim, interest, budget=budget, timeout=timeout)

    @contextmanager
    def borrow(self, work, size_bytes):
        span = super().borrow(work, size_bytes)
        self._change(span.__enter__)
        try:
            yield
        finally:
            if not self._owner_file.closed:
                self._change(span.__exit__, None, None, None)

    def complete(self, work):
        def complete():
            token = self.work_token(work)
            super(RecoverableDemandCoordinator, self).complete(work)
            self._wake_claims.pop(token, None)
        return self._change(complete)

    def release(self):
        return self._change(super().release)
