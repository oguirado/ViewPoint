"""Durable request reservations and redacted channel receipts."""

from contextlib import closing, contextmanager
from hashlib import sha256
import json
import sqlite3

from schemas.validation import canonical_bytes


def value_evidence(request):
    for field in ("input_text", "url", "key"):
        if field in request:
            value = request[field]
            return {"value_len": len(value), "value_sha256": sha256(value.encode()).hexdigest()}
    return {}


class RequestStore:
    """All executors share the database in a provisioned, private runtime directory."""

    def __init__(self, path):
        self._path = path
        with self._connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS channel_requests (
                request_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, reply BLOB
            )""")

    @contextmanager
    def _connect(self):
        with closing(sqlite3.connect(self._path)) as db, db:
            db.execute("PRAGMA synchronous = FULL")
            yield db

    def reserve(self, agent_id, request):
        """Return (won reservation, reply). A null existing reply denotes uncertainty."""
        request_id = request["request_id"]
        fingerprint = sha256(canonical_bytes([agent_id, request])).hexdigest()
        with self._connect() as db:
            row = db.execute(
                "INSERT INTO channel_requests VALUES (?, ?, NULL) ON CONFLICT DO NOTHING",
                (request_id, fingerprint),
            )
            if row.rowcount == 1:
                return True, None
            existing, reply = db.execute(
                "SELECT fingerprint, reply FROM channel_requests WHERE request_id = ?", (request_id,)
            ).fetchone()
        if existing != fingerprint:
            return False, {"schema": "vp.channel.reply/v1", "ok": False, "code": "REQUEST_ID_CONFLICT"}
        return False, json.loads(reply) if reply is not None else None

    def complete(self, request_id, reply):
        with self._connect() as db:
            row = db.execute(
                "UPDATE channel_requests SET reply = ? WHERE request_id = ? AND reply IS NULL",
                (canonical_bytes(reply), request_id),
            )
            if row.rowcount != 1:
                raise sqlite3.IntegrityError("No pending channel reservation")
