"""Lifetime exclusion and durable, ordered writes to the reference broker's RT."""

import fcntl
import json
import os
from pathlib import Path

from schemas.validation import canonical_bytes


class BrokerError(RuntimeError):
    """A refusal from the broker. The message is the verdict code."""


class Journal:
    def __init__(self, rt):
        rt = Path(rt)
        rt.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.pid = os.getpid()
        self.seq = 0
        self.lock = (rt / "broker.lock").open("ab")
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.file = (rt / "events.jsonl").open("a+b", buffering=0)
            try:
                directory = os.open(rt, os.O_DIRECTORY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
            except BaseException:
                self.file.close()
                raise
        except BaseException:
            self.lock.close()
            raise

    def replay(self):
        self.file.seek(0)
        for line in self.file:
            if not line.endswith(b"\n"):
                raise BrokerError("JOURNAL_TORN")
            record = json.loads(line)
            if (record["seq"] != self.seq + 1 or record["v"] != 1
                    or record["by"] != "broker"):
                raise BrokerError("invalid broker record")
            self.seq += 1
            yield record

    def append(self, record):
        if os.getpid() != self.pid or self.file.closed:
            raise BrokerError("NO_LEASE")
        row = dict(record, by="broker", v=1, seq=self.seq + 1)
        line = canonical_bytes(row)
        try:
            if self.file.write(line) != len(line):
                raise OSError("short broker write")
            os.fsync(self.file.fileno())
        except BaseException:
            self.file.close()  # Never acknowledge or append behind an uncertain tail.
            raise
        self.seq += 1
        return row

    def close(self):
        self.file.close()
        self.lock.close()
