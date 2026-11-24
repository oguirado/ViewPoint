"""Shared SQLite request-store durability and timing tests."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from threading import Barrier
import unittest
from unittest.mock import patch

from test_control import c


class RequestStoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(self.enterContext(TemporaryDirectory()))
        self.path = self.directory / "requests.sqlite"
        self.trace = self.directory / "trace.jsonl"
        self.enterContext(patch.dict(os.environ, {"VP_REQUEST_STORE_TRACE": ""}))

    def journal_mode(self):
        with closing(sqlite3.connect(self.path)) as db:
            return db.execute("PRAGMA journal_mode").fetchone()[0]

    def test_legacy_store_converts_in_place_with_claims_and_permissions(self):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("PRAGMA journal_mode=DELETE")
            db.execute("CREATE TABLE requests (id TEXT PRIMARY KEY, digest TEXT NOT NULL, reply BLOB)")
            db.executemany("INSERT INTO requests VALUES (?, ?, ?)", (
                ("pending", "first", None), ("done", "second", b'{"ok":true}'),
            ))
        self.path.chmod(0o600)

        store = c.RequestStore(self.path)
        self.assertEqual(self.journal_mode(), "wal")
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(store.claim("pending", "wrong"), "REQUEST_ID_CONFLICT")
        self.assertEqual(store.claim("pending", "first"), "UNCERTAIN_OUTCOME")
        self.assertEqual(store.claim("done", "second"), {"ok": True})
        with self.assertRaisesRegex(RuntimeError, "could not be completed"):
            store.complete("pending", "wrong", {"ok": False})
        store.complete("pending", "first", {"ok": True})
        with self.assertRaisesRegex(RuntimeError, "could not be completed"):
            store.complete("pending", "first", {"ok": False})
        self.assertEqual(c.RequestStore(self.path).claim("pending", "first"), {"ok": True})

    def test_writer_connections_use_explicit_timeout_and_full_sync(self):
        connect = sqlite3.connect
        with patch.object(c.sqlite3, "connect", wraps=connect) as observed:
            store = c.RequestStore(self.path)
            self.assertIsNone(store.claim("request", "digest"))
            store.complete("request", "digest", {"ok": True})

        self.assertGreater(observed.call_count, 0)
        self.assertTrue(all(call.kwargs.get("timeout") == 5.0 for call in observed.call_args_list))
        with store._transaction("probe") as (db, _):
            self.assertEqual(db.execute("PRAGMA busy_timeout").fetchone(), (5000,))
            self.assertEqual(db.execute("PRAGMA synchronous").fetchone(), (2,))

    def test_concurrent_claim_and_completion_survive_reopen(self):
        callers = 16
        start, claimed = Barrier(callers, timeout=15), Barrier(callers, timeout=15)

        def claim(index):
            start.wait()
            store = c.RequestStore(self.path)
            shared = store.claim("shared", "digest")
            self.assertIsNone(store.claim(f"own-{index}", "digest"))
            return store, shared

        def complete(index):
            store, shared = results[index]
            claimed.wait()
            store.complete(f"own-{index}", "digest", {"index": index})
            if shared is None:
                store.complete("shared", "digest", {"winner": index})

        with ThreadPoolExecutor(max_workers=callers) as pool:
            results = list(pool.map(claim, range(callers)))
            winners = [index for index, (_, reply) in enumerate(results) if reply is None]
            self.assertEqual(len(winners), 1, "shared request must have exactly one winner")
            self.assertTrue(all(reply == "UNCERTAIN_OUTCOME" for _, reply in results if reply is not None))
            list(pool.map(complete, range(callers)))

        reader = c.RequestStore(self.path)
        self.assertEqual(reader.claim("shared", "digest"), {"winner": winners[0]})
        for index in range(callers):
            self.assertEqual(reader.claim(f"own-{index}", "digest"), {"index": index})
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("SELECT count(*), count(reply) FROM requests").fetchone(),
                             (callers + 1, callers + 1))
        self.assertEqual(self.journal_mode(), "wal")

    def test_stamps_are_opt_in_and_cover_sql_and_transaction_exit(self):
        store = c.RequestStore(self.path)
        store.claim("silent", "digest")
        self.assertFalse(self.trace.exists())

        with patch.dict(os.environ, {"VP_REQUEST_STORE_TRACE": str(self.trace)}):
            store = c.RequestStore(self.path)
            store.claim("request", "digest")
            store.claim("request", "digest")
            store.complete("request", "digest", {"ok": True})
            with self.assertRaises(RuntimeError):
                store.complete("request", "digest", {"ok": False})

        self.assertTrue(self.trace.exists(), "enabled request-store trace is missing")
        rows = [json.loads(line) for line in self.trace.read_text().splitlines()]
        self.assertEqual([row["operation"] for row in rows],
                         ["initialize", "claim", "claim", "complete", "complete"])
        self.assertEqual([row["request_id"] for row in rows], [None, *["request"] * 4])
        for row in rows:
            self.assertEqual(row["pid"], os.getpid())
            self.assertTrue(row["boot_id"])
            for clock in ("monotonic_ns", "thread_cpu_ns"):
                values = [stamp[clock] for stamp in row["stages"].values()]
                self.assertEqual(values, sorted(values))
        for row, statement in zip(rows, ("create", "insert", "select", "update", "update")):
            stages = row["stages"]
            self.assertIn(statement + "_enter", stages)
            self.assertIn(statement + "_return", stages)
            if row["ok"]:
                self.assertLessEqual(stages[statement + "_return"]["monotonic_ns"],
                                     stages["commit_enter"]["monotonic_ns"])
                self.assertIn("commit_return", stages)
        self.assertEqual([row["ok"] for row in rows], [True, True, True, True, False])
        self.assertNotIn("commit_return", rows[-1]["stages"])
