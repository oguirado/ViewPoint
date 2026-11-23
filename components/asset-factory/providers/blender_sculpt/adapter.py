"""Read-only refine admission boundary; live sculpt execution is unqualified.

The provider revision pins this adapter's bytes, not Blender or a sculpt loop.
No executor API, brush parameters, geometry evaluator or licence grant is
inferred. This adapter intentionally has no successful execution path until
those dependencies qualify. A schema-valid request is not an asset approval.

Library: refine(request, input_root=Path(...)). CLI: -m
providers.blender_sculpt.adapter --request FILE --input-root DIR [--result FILE].
Refusals go to stdout and never publish to --result or mutate input files.
"""

import argparse
import hashlib
import os
from pathlib import Path
import stat
import time

from asset_factory import core, contracts


PROVIDER_ID = "blender_sculpt"
# [TARGET, DESIGN_CHOICE] Admission work bounds, not calibrated sculpt budgets.
MAX_METADATA_BYTES = 2**20
MAX_INPUT_BYTES = 512 * 2**20
MAX_ADMISSION_SECONDS = 30


class _Refusal(Exception):
    def __init__(self, rc, reason):
        self.rc = rc
        self.reason = reason


def load_provider():
    """Validate the committed provider; mutable pins retain the kernel error."""
    provider = core.load_json(Path(__file__).with_name("provider.json"),
                              max_bytes=MAX_METADATA_BYTES)
    contracts.validate_provider(provider)
    return provider


def _budget(deadline_ns):
    if time.monotonic_ns() >= deadline_ns:
        raise _Refusal(6, "deadline_exceeded")


def _input_digest(root, item, deadline_ns, remaining_bytes):
    _budget(deadline_ns)
    # Resolve even missing leaves so an escaping dangling link is a path error.
    path = (root / item["path"]).resolve()
    if not path.is_relative_to(root):
        raise _Refusal(2, "path_escape")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    except FileNotFoundError as exc:
        raise _Refusal(3, "input_missing") from exc
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise _Refusal(2, "input_not_regular")
    with os.fdopen(fd, "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise _Refusal(2, "input_not_regular")
        if before.st_size > remaining_bytes:
            raise _Refusal(6, "input_budget_exceeded")
        digest = hashlib.sha256()
        read_bytes = 0
        while True:
            _budget(deadline_ns)
            chunk = stream.read(min(2**20, remaining_bytes - read_bytes + 1))
            if not chunk:
                break
            read_bytes += len(chunk)
            if read_bytes > remaining_bytes:
                raise _Refusal(6, "input_budget_exceeded")
            digest.update(chunk)
        after = os.fstat(stream.fileno())
        if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise _Refusal(4, "input_stale")
        if digest.hexdigest() != item["sha256"]:
            raise _Refusal(4, "input_stale")
        return read_bytes


def _result(request, rc, reason, started_ns):
    """Use the existing closed slot result; no invented receipt schema."""
    result = {key: request[key] for key in (
        "schema", "slot", "request_id", "provider_id", "provider_revision")}
    result.update({
        "kind": "result", "rc": rc, "status": core.STATUS[rc],
        "outputs": [], "receipts": [],
        "metrics": {"wall_ms": max(0, (time.monotonic_ns() - started_ns) // 1_000_000),
                    "peak_rss_bytes": None},
        "absence_reasons": {"execution": reason,
                            "peak_rss_bytes": "live_sculpt_peak_unmeasured"},
    })
    contracts.validate_slot(result, "refine", "result")
    return result


def refine(request, *, input_root):
    """Authenticate request/pin/input bytes and refuse unqualified execution.

    The input root is explicit. Hash checking authenticates bytes only; neither
    GLB geometry nor reference pixels are evaluated. No models, subprocesses,
    downloads, Blender imports or writes occur here, including on refusals.
    """
    started_ns = time.monotonic_ns()
    # If a result identity cannot be established, use the shared error record.
    try:
        contracts.validate_slot(request, "refine", "request")
    except core.ContractError as exc:
        return exc.as_dict()
    try:
        provider = load_provider()
        if (provider["id"] != PROVIDER_ID or provider["slot"] != "refine"
                or provider["kind"] != "tool" or provider["weights"] != []
                or request["provider_id"] != PROVIDER_ID):
            raise _Refusal(2, "provider_mismatch")
        if provider["revision"] != "sha256:" + core.sha256_file(Path(__file__)):
            raise _Refusal(4, "adapter_stale")
        if request["provider_revision"] != provider["revision"]:
            raise _Refusal(4, "provider_stale")
        deadline_ns = started_ns + min(request["context"]["timeout_s"],
                                       MAX_ADMISSION_SECONDS) * 1_000_000_000
        _budget(deadline_ns)
        root = Path(input_root).resolve(strict=True)
        if not root.is_dir():
            raise _Refusal(2, "input_root_invalid")
        remaining_bytes = MAX_INPUT_BYTES
        for item in request["inputs"]:
            remaining_bytes -= _input_digest(root, item, deadline_ns, remaining_bytes)
        _budget(deadline_ns)
        # A changed runnable flag cannot manufacture an executor or RAM proof.
        raise _Refusal(3, "sculpt_execution_unqualified")
    except _Refusal as exc:
        return _result(request, exc.rc, exc.reason, started_ns)
    except core.ContractError as exc:
        return exc.as_dict()
    except (OSError, ValueError, RuntimeError, TypeError):
        return _result(request, 2, "input_io", started_ns)


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise core.ContractError("schema_mismatch", "$argv")


def main(argv=None):
    parser = _Parser(add_help=False, allow_abbrev=False)
    parser.add_argument("--request", required=True)
    parser.add_argument("--input-root", required=True)
    parser.add_argument("--result")
    try:
        args = parser.parse_args(argv)
        request = core.load_json(Path(args.request), max_bytes=MAX_METADATA_BYTES)
        result = refine(request, input_root=Path(args.input_root))
        # No publication branch exists while execution is unqualified. Retain
        # --result as an explicit caller destination, never as evidence.
        return core.emit(result)
    except core.ContractError as exc:
        return core.emit(exc.as_dict())


if __name__ == "__main__":
    raise SystemExit(main())
