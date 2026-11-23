"""Shared host boundary for F2-U3; no Blender or geometry implementation.

The sibling component supplies algorithms; the F0 kernel owns factory JSON.
Library results carry stage status, never an asset PASS.
"""
import hashlib
import importlib
import json
import os
from pathlib import Path
import stat
import sys

from asset_factory import core

RC, STATUS = core.RC, core.STATUS
MAX_BYTES = 64 * 2**20
SIBLING = Path(__file__).resolve().parents[3] / "blender-visual-qa"


class Refusal(Exception):
    def __init__(self, rc, reason):
        super().__init__(reason)
        self.rc, self.reason = rc, reason


def result(rc, *reasons, **fields):
    if type(rc) is not int or rc not in RC:
        rc, reasons = 2, ("invalid_upstream_rc",)
    return dict(rc=rc, status=STATUS[rc], reason_codes=list(reasons), **fields)


def upstream(name):
    """Resolve only the checked-in sibling, including under python -I."""
    if not SIBLING.is_dir():
        raise Refusal(3, "vqa3d_unavailable")
    # Reject already-loaded foreign origins before executing any requested code.
    for key, loaded in tuple(sys.modules.items()):
        if key == "blender_visual_qa" or key.startswith("blender_visual_qa."):
            origin = getattr(loaded, "__file__", None)
            if origin is None or not Path(origin).resolve().is_relative_to(SIBLING.resolve()):
                raise Refusal(4, "vqa3d_origin_drift")
    if not name.isidentifier() or not (SIBLING / "blender_visual_qa" / "assets" / (name + ".py")).is_file():
        raise Refusal(3, "vqa3d_unavailable")
    before = list(sys.path)
    sys.path.insert(0, str(SIBLING.resolve()))
    try:
        module = importlib.import_module("blender_visual_qa.assets." + name)
    except ImportError as exc:
        raise Refusal(3, "vqa3d_unavailable") from exc
    finally:
        sys.path[:] = before
    if not Path(module.__file__).resolve().is_relative_to(SIBLING.resolve()):
        raise Refusal(4, "vqa3d_origin_drift")
    return module


def read_bytes(path):
    path = Path(os.path.abspath(path))
    fd = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        leaf = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        with os.fdopen(leaf, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise Refusal(2, "regular_file_required")
            raw = stream.read(MAX_BYTES + 1)
    except FileNotFoundError as exc:
        raise Refusal(3, "missing_file") from exc
    except OSError as exc:
        raise Refusal(2, "symlink_or_io_refused") from exc
    finally:
        os.close(fd)
    if len(raw) > MAX_BYTES:
        raise Refusal(6, "file_budget_exceeded")
    return raw


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def canonical(obj):
    try:
        return core.canonical_json(obj)
    except core.ContractError as exc:
        raise Refusal(2, exc.code) from exc


def write_record(path, record):
    canonical(record)
    core.atomic_write_json(Path(path), record)


def load_record(raw):
    def pairs(items):
        out = {}
        for key, value in items:
            if key in out:
                raise Refusal(2, "duplicate_json_key")
            out[key] = value
        return out
    def reject(_):
        raise Refusal(2, "non_integer_number")
    return json.loads(raw, object_pairs_hook=pairs, parse_float=reject, parse_constant=reject)


def require_receipt(rc, receipt, schema, *, link=None, previous=""):
    if type(rc) is not int or rc not in RC:
        raise Refusal(2, "invalid_upstream_rc")
    if rc:
        raise Refusal(rc, "upstream_refused")
    if not isinstance(receipt, dict) or not receipt:
        raise Refusal(3, "receipt_missing")
    if receipt.get("schema") != schema or type(receipt.get("rc")) is not int or receipt["rc"] != rc:
        raise Refusal(2, "receipt_invalid")
    if receipt.get("status", receipt.get("verdict")) != "PASS":
        raise Refusal(3, "receipt_not_pass")
    if receipt.get("failed") or receipt.get("violations"):
        raise Refusal(5, "receipt_failed_checks")
    if link and receipt.get(link) != previous:
        raise Refusal(4, "receipt_chain_drift")


def safe_glb(raw):
    """Blender may only consume self-contained GLB inputs, never external URIs."""
    g = upstream("glb_core")
    try:
        doc, _ = g._split(raw)
        g.parse_glb(raw)
    except (g.GlbError, ValueError, TypeError, KeyError) as exc:
        raise Refusal(2, "glb_invalid") from exc
    for entry in doc.get("images", []) + doc.get("buffers", []):
        if "uri" in entry:
            raise Refusal(2, "external_uri_refused")
    return doc
