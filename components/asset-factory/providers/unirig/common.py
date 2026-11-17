"""Shared bounded records for the offline UniRig boundary (stdlib only)."""
from pathlib import Path
import re

from asset_factory import core

ROOT = Path(__file__).resolve().parent
DIGEST = re.compile(r"[0-9a-f]{64}\Z")
PIN = re.compile(r"[0-9a-f]{40}\Z")
MAX_JSON_BYTES = 16 * 2**20  # [TARGET: bounded metadata, DESIGN_CHOICE]


class Refusal(Exception):
    def __init__(self, rc, code):
        self.rc, self.code = rc, code
        super().__init__(code)


def require(condition, rc, code):
    if not condition:
        raise Refusal(rc, code)


def integer(value, lo, hi):
    return type(value) is int and lo <= value <= hi


def digest(value):
    return type(value) is str and DIGEST.fullmatch(value) is not None


def load(path):
    return core.load_json(Path(path), max_bytes=MAX_JSON_BYTES)


def validate(record, name):
    schema = load(ROOT / "records.schema.json")
    core.validate(record, {"$defs": schema["$defs"], "$ref": "#/$defs/" + name})


def outcome(schema, rc, reasons, **extra):
    return {"schema": schema, "rc": rc, "status": core.STATUS[rc],
            "verdict": core.VERDICT[rc], "reason_codes": sorted(set(reasons)), **extra}
