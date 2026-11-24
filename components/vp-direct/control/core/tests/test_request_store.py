"""Shared SQLite request-store durability and timing tests."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Barrier, Event
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

    def test_writer_connections_use_bounded_timeout_normal_sync_and_immediate_transactions(self):
        connect = sqlite3.connect
        statements = []

        def trace_connect(*args, **kwargs):
            db = connect(*args, **kwargs)
            db.set_trace_callback(statements.append)
            return db

        with patch.object(c.sqlite3, "connect", side_effect=trace_connect) as observed:
            store = c.RequestStore(self.path)
            self.assertIsNone(store.claim("request", "digest"))
            store.complete("request", "digest", {"ok": True})

        self.assertGreater(observed.call_count, 0)
        self.assertTrue(all(call.kwargs.get("timeout") == 5.0 for call in observed.call_args_list))
        self.assertEqual([sql for sql in statements if sql.startswith("BEGIN")],
                         ["BEGIN IMMEDIATE", "BEGIN IMMEDIATE"])
        with store._transaction("probe") as (db, _):
            self.assertEqual(db.execute("PRAGMA busy_timeout").fetchone(), (5000,))
            self.assertEqual(db.execute("PRAGMA synchronous").fetchone(), (1,))
            self.assertTrue(db.getconfig(sqlite3.SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE))
            self.assertGreater(db.execute("PRAGMA wal_autocheckpoint").fetchone()[0], 0)
        self.assertGreater(Path(str(self.path) + "-wal").stat().st_size, 0)
        self.assertEqual(c.RequestStore(self.path).claim("request", "digest"), {"ok": True})

    def test_same_path_writers_queue_before_connecting(self):
        first = c.RequestStore(self.path)
        alias = self.directory / "alias.sqlite"
        alias.symlink_to(self.path)
        second = c.RequestStore(alias)
        independent = c.RequestStore(self.directory / "independent.sqlite")
        start, connected = Event(), Event()
        connect = sqlite3.connect

        def observe(path, *args, **kwargs):
            if path == self.path:
                connected.set()
            return connect(path, *args, **kwargs)

        def compete():
            start.set()
            return second.claim("queued", "digest")

        with ThreadPoolExecutor(max_workers=2) as pool:
            with first._transaction("claim"):
                with patch.object(c.sqlite3, "connect", side_effect=observe):
                    queued = pool.submit(compete)
                    self.assertTrue(start.wait(2))
                    self.assertIsNone(pool.submit(independent.claim, "other", "digest").result(2))
                    self.assertFalse(connected.wait(0.1), "second writer reached SQLite before release")
            self.assertIsNone(queued.result(2))

    def test_reopening_a_provisioned_store_skips_schema_ddl(self):
        c.RequestStore(self.path)
        connect = sqlite3.connect
        statements = []

        def trace_connect(*args, **kwargs):
            db = connect(*args, **kwargs)
            db.set_trace_callback(statements.append)
            return db

        with patch.object(c.sqlite3, "connect", side_effect=trace_connect):
            c.RequestStore(self.path)
        self.assertFalse(any(sql.startswith("CREATE TABLE") for sql in statements))

    def test_process_crash_preserves_committed_lineage_and_rolls_back_open_write(self):
        child = """
import os
from pathlib import Path
import sys
sys.path.insert(0, sys.argv[1])
from control.core.control import RequestStore

store = RequestStore(Path(sys.argv[2]))
store.claim("pending", "first")
store.claim("done", "second")
store.complete("done", "second", {"ok": True})
with store._transaction("claim", "interrupted") as (db, _):
    assert db.in_transaction, "claim must hold the writer before its SQL"
    db.execute("INSERT INTO requests (id, digest) VALUES ('interrupted', 'third')")
    os._exit(99)
"""
        result = subprocess.run(
            [sys.executable, "-B", "-c", child, str(Path(c.__file__).resolve().parents[2]), str(self.path)],
            capture_output=True, text=True, timeout=10, check=False,
        )
        self.assertEqual(result.returncode, 99, result.stderr)
        for suffix in ("-wal", "-shm"):
            self.assertEqual(Path(str(self.path) + suffix).stat().st_mode & 0o777, 0o600)
        restored = c.RequestStore(self.path)
        self.assertEqual(restored.claim("pending", "wrong"), "REQUEST_ID_CONFLICT")
        self.assertEqual(restored.claim("pending", "first"), "UNCERTAIN_OUTCOME")
        self.assertEqual(restored.claim("done", "second"), {"ok": True})
        self.assertIsNone(restored.claim("interrupted", "third"))

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
            self.assertIn("writer_return", stages)
            if row["operation"] != "initialize":
                self.assertLessEqual(stages["begin_return"]["monotonic_ns"],
                                     stages[statement + "_enter"]["monotonic_ns"])
            if row["ok"]:
                self.assertLessEqual(stages[statement + "_return"]["monotonic_ns"],
                                     stages["commit_enter"]["monotonic_ns"])
                self.assertIn("commit_return", stages)
        self.assertEqual([row["ok"] for row in rows], [True, True, True, True, False])
        self.assertNotIn("commit_return", rows[-1]["stages"])

        length = self.trace.stat().st_size
        os.environ.pop("VP_REQUEST_STORE_TRACE", None)
        silent = c.RequestStore(self.directory / "silent.sqlite")
        silent.claim("silent", "digest")
        silent.complete("silent", "digest", {"ok": True})
        self.assertEqual(self.trace.stat().st_size, length)
