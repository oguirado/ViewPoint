"""Private sidecar. A pending intent survives death between dispatch and readback."""
from contextlib import closing, contextmanager
from hashlib import sha256
import json
import sqlite3

from schemas.validation import canonical_bytes


class Catalog:
    def __init__(self, path, identity, *, fresh):
        self.path = path
        self.identity = identity
        if fresh:
            with self.transaction(create=True) as db:
                db.execute("CREATE TABLE catalog (id INTEGER PRIMARY KEY CHECK(id=1), "
                           "identity BLOB NOT NULL, pending TEXT, manifest BLOB, digest TEXT)")
                db.execute("INSERT INTO catalog VALUES(1, ?, NULL, NULL, NULL)",
                           (canonical_bytes(identity),))

    @contextmanager
    def transaction(self, *, create=False):
        mode = "rwc" if create else "rw"
        with closing(sqlite3.connect(self.path.as_uri() + "?mode=" + mode, uri=True)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            yield db

    def read(self):
        with self.transaction() as db:
            row = db.execute("SELECT identity,pending,manifest,digest FROM catalog WHERE id=1").fetchone()
        if row is None or row[0] != canonical_bytes(self.identity) or row[1] is not None:
            raise ValueError("unsettled or foreign catalog")
        raw, digest = row[2:]
        if raw is None or sha256(raw).hexdigest() != digest:
            raise ValueError("unverified manifest")
        return json.loads(raw), digest

    def begin(self, job, *, recovery=False):
        with self.transaction() as db:
            changed = db.execute("UPDATE catalog SET pending=? WHERE id=1 AND "
                                 "(pending IS NULL OR ?)", (job, recovery))
            if changed.rowcount != 1:
                raise ValueError("previous dispatch unsettled")

    def finish(self, job, raw, digest):
        with self.transaction() as db:
            changed = db.execute("UPDATE catalog SET pending=NULL, manifest=?, digest=? "
                                 "WHERE id=1 AND pending=?", (raw, digest, job))
            if changed.rowcount != 1:
                raise ValueError("dispatch identity changed")
