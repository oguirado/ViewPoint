"""Value types and on-disk bounds for the retention store."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from ..contracts.wire import MAX_RECORD_BYTES
from .files import digest

GIB = 1 << 30
MAX_METADATA = 1 << 20
MAX_INTENT = 4 * ((MAX_RECORD_BYTES + 2) // 3) + 4 * MAX_METADATA
ATTEMPTS = "attempts"
TOMBSTONES = "tombstones"
TENANTS = ("tenants",)


def reference(prefix):
    return prefix + "_" + uuid4().hex


def utc_now():
    return datetime.now(timezone.utc)


def timestamp(value):
    if value.tzinfo is None:
        raise ValueError("retention clock must include a time zone")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def when(value):
    return datetime.fromisoformat(value)


@dataclass(frozen=True)
class Actor:
    # Populated by the authenticated transport after peer authentication.
    tenant: str
    principal: str


@dataclass(frozen=True)
class Policy:
    revision: str
    object_bytes: int = GIB
    attempt_bytes: int = 4 * GIB
    tenant_bytes: int = 64 * GIB
    retention: timedelta = timedelta(days=30)
    purge_window: timedelta = timedelta(hours=24)
    snapshot_lifetime: timedelta = timedelta(minutes=15)
    snapshot_bytes: int = 32 << 20


@dataclass(frozen=True)
class Page:
    ref: str
    bytes: int
    sha256: str
    offset: int
    data: bytes

    @property
    def sha256_page(self):
        return digest(self.data)
