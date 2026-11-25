# SPDX-License-Identifier: AGPL-3.0-only
"""Durable request identity and execution claims within a host exchange root."""

from contextlib import closing, contextmanager
import sqlite3


class ClaimRefused(ValueError):
    pass


@contextmanager
def transaction(path):
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("PRAGMA synchronous = FULL")
        db.execute("BEGIN IMMEDIATE")
        db.execute("""CREATE TABLE IF NOT EXISTS external_requests (
            id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, state TEXT NOT NULL
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS external_attempts (
            request_id TEXT NOT NULL, attempt_id TEXT NOT NULL,
            PRIMARY KEY (request_id, attempt_id)
        )""")
        yield db


def claim(path, request_id, attempt_id, fingerprint):
    with transaction(path) as db:
        previous = db.execute(
            "SELECT fingerprint, state FROM external_requests WHERE id = ?", (request_id,)
        ).fetchone()
        if previous is None:
            db.execute("INSERT INTO external_requests VALUES (?, ?, 'available')",
                       (request_id, fingerprint))
            state = "available"
        else:
            previous_fingerprint, state = previous
            if previous_fingerprint != fingerprint:
                raise ClaimRefused("REQUEST_ID_CONFLICT")
        if db.execute("SELECT 1 FROM external_attempts WHERE request_id = ? AND attempt_id = ?",
                      (request_id, attempt_id)).fetchone():
            raise ClaimRefused("DUPLICATE_ATTEMPT")
        if state != "available":
            raise ClaimRefused({
                "committed": "REQUEST_ALREADY_COMMITTED",
                "running": "UNCERTAIN_OUTCOME",
            }[state])
        db.execute("INSERT INTO external_attempts VALUES (?, ?)", (request_id, attempt_id))
        db.execute("UPDATE external_requests SET state = 'running' WHERE id = ?", (request_id,))


def finish(path, request_id, *, committed):
    with transaction(path) as db:
        updated = db.execute(
            "UPDATE external_requests SET state = ? WHERE id = ? AND state = 'running'",
            ("committed" if committed else "available", request_id),
        )
        if updated.rowcount != 1:
            raise RuntimeError("External attempt claim is missing")
