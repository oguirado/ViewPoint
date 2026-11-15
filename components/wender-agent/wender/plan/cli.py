# @module {"id":"wa.plan.cli","layer":2,"owner":"LEARN","deps":["wa.plan.policy"],"book":"HB-WA-s4-learn"}
"""Pipe-only planner CLI reading one UTF-8 JSON request on stdin and writing one JSON response on stdout.

[TARGET, unit decision] Accepts either `{observation, element_table, goal}` (emitting
`{action, expectation, state, target_node}`) or a plain `observation/v0.2` (or wrapper
`{observation, proposal_id?, tick_window?, max_ms?, max_retries?}`, emitting
`action_proposal/v0.2`). Exits nonzero with `code: detail` on stderr on any refusal.
"""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
import json
import math
import os
import sys

from wender.plan.policy import (
    EXIT_CODES,
    PlannerError,
    canonical_json,
    plan,
    plan_holdings,
)

# @anchor:wa.plan.cli.MAX_STDIN_BYTES
MAX_STDIN_BYTES = 1024 * 1024
# @anchor:wa.plan.cli.MAX_ERROR_DETAIL_BYTES
MAX_ERROR_DETAIL_BYTES = 256
# @anchor:wa.plan.cli.MAX_PLANNER_ELEMENTS
MAX_PLANNER_ELEMENTS = 512
# @anchor:wa.plan.cli.MAX_TOTAL_PLANNER_NODES
MAX_TOTAL_PLANNER_NODES = 1024

_HOSTED_ENV_VAR = "MV_HOSTED"
_GENERIC_ERROR_DETAIL = "internal planner failure"

_REQUEST_KEYS = frozenset({"observation", "element_table", "goal"})
_OBSERVATION_WRAPPER_KEYS = frozenset({
    "observation", "proposal_id", "tick_window", "max_ms", "max_retries",
})


# @anchor:wa.plan.cli.enter_hosted_mode
def enter_hosted_mode() -> None:
    """Deterministically latch hosted mode for the planner CLI subprocess."""
    os.environ[_HOSTED_ENV_VAR] = "1"
    sys._wender_hosted_latched = True  # type: ignore[attr-defined]


# @anchor:wa.plan.cli.planner_cli_guard_scope
@contextmanager
def planner_cli_guard_scope() -> Iterator[None]:
    """Enter the planner-entry guard scope for the CLI lifetime."""
    depth = getattr(sys, "_wender_planner_entry_depth", 0)
    if not isinstance(depth, int) or isinstance(depth, bool) or depth < 0:
        depth = 0
    sys._wender_planner_entry_depth = depth + 1  # type: ignore[attr-defined]
    try:
        yield
    finally:
        cur = getattr(sys, "_wender_planner_entry_depth", 1)
        if not isinstance(cur, int) or isinstance(cur, bool):
            cur = 1
        sys._wender_planner_entry_depth = max(0, cur - 1)  # type: ignore[attr-defined]


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise PlannerError("bad_shape", "duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(token: str) -> None:
    raise PlannerError("bad_shape", f"non-finite JSON constant: {token}")


def _checked_float(token: str) -> float:
    value = float(token)
    if not math.isfinite(value):
        raise PlannerError("bad_shape", "non-finite float")
    return value


def _drain_stream_text(stream) -> str:
    """Drain stream to EOF while enforcing an exact MAX_STDIN_BYTES ceiling."""
    if hasattr(stream, "buffer"):
        raw_chunks: list[bytes] = []
        total_bytes = 0
        while True:
            step = max(1, (MAX_STDIN_BYTES + 1) - total_bytes)
            chunk = stream.buffer.read(step)
            if chunk == b"" or chunk is None:
                break
            if not isinstance(chunk, (bytes, bytearray, memoryview)):
                raise PlannerError("bad_shape", "planner input is not bytes")
            bchunk = bytes(chunk)
            total_bytes += len(bchunk)
            if total_bytes > MAX_STDIN_BYTES:
                raise PlannerError("bad_shape", "planner input exceeds maximum size")
            raw_chunks.append(bchunk)
        raw = b"".join(raw_chunks)
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise PlannerError("bad_shape", "planner input is not valid UTF-8") from exc

    str_chunks: list[str] = []
    total_bytes = 0
    while True:
        step = max(1, (MAX_STDIN_BYTES + 1) - total_bytes)
        chunk = stream.read(step)
        if chunk == "" or chunk is None:
            break
        if isinstance(chunk, (bytes, bytearray, memoryview)):
            bchunk = bytes(chunk)
            total_bytes += len(bchunk)
            if total_bytes > MAX_STDIN_BYTES:
                raise PlannerError("bad_shape", "planner input exceeds maximum size")
            try:
                str_chunks.append(bchunk.decode("utf-8"))
            except UnicodeDecodeError as exc:
                raise PlannerError("bad_shape", "planner input is not valid UTF-8") from exc
            continue
        if not isinstance(chunk, str):
            raise PlannerError("bad_shape", "planner input is not text")
        total_bytes += len(chunk.encode("utf-8", errors="surrogatepass"))
        if total_bytes > MAX_STDIN_BYTES:
            raise PlannerError("bad_shape", "planner input exceeds maximum size")
        str_chunks.append(chunk)
    return "".join(str_chunks)


def _validate_request_element_bounds(parsed: dict) -> None:
    total_nodes = 0
    obs = parsed.get("observation") if "observation" in parsed else parsed
    if isinstance(obs, dict):
        vision = obs.get("vision")
        if isinstance(vision, dict):
            for list_key in ("elements", "changed_regions"):
                items = vision.get(list_key)
                if isinstance(items, list):
                    if len(items) > MAX_PLANNER_ELEMENTS:
                        raise PlannerError("bad_shape", f"vision.{list_key} exceeds maximum count")
                    total_nodes += len(items)
    table = parsed.get("element_table")
    if isinstance(table, dict):
        table_elems = table.get("elements")
        if isinstance(table_elems, list):
            if len(table_elems) > MAX_PLANNER_ELEMENTS:
                raise PlannerError("bad_shape", "element_table.elements exceeds maximum count")
            total_nodes += len(table_elems)
    if total_nodes > MAX_TOTAL_PLANNER_NODES:
        raise PlannerError("bad_shape", "planner request exceeds maximum total element count")


def _format_bounded_error(code: str, detail: str) -> tuple[str, int]:
    eff_code = code if isinstance(code, str) and code in EXIT_CODES else "bad_shape"
    exit_code = EXIT_CODES[eff_code]
    raw_detail = detail if isinstance(detail, str) else _GENERIC_ERROR_DETAIL
    cleaned = " ".join(
        "".join(ch if ch >= " " and ch != "\x7f" else " " for ch in raw_detail).split()
    )
    if not cleaned:
        cleaned = _GENERIC_ERROR_DETAIL
    encoded = cleaned.encode("utf-8", errors="replace")
    if len(encoded) > MAX_ERROR_DETAIL_BYTES:
        cleaned = encoded[:MAX_ERROR_DETAIL_BYTES].decode("utf-8", errors="ignore").rstrip()
        if not cleaned:
            cleaned = _GENERIC_ERROR_DETAIL
    return f"{eff_code}: {cleaned}", exit_code


# @anchor:wa.plan.cli.read_stdin_request
def read_stdin_request(stream) -> tuple[str, dict]:
    """Read and classify exactly one bounded UTF-8 JSON object from stream."""
    text = _drain_stream_text(stream)
    stripped = text.strip()
    if not stripped:
        raise PlannerError("bad_shape", "planner input is empty")
    decoder = json.JSONDecoder(
        object_pairs_hook=_reject_duplicate_keys,
        parse_constant=_reject_constant,
        parse_float=_checked_float,
    )
    try:
        parsed, end_idx = decoder.raw_decode(stripped)
    except PlannerError:
        raise
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise PlannerError("bad_shape", "planner input is not valid JSON") from exc

    if stripped[end_idx:].strip():
        raise PlannerError("bad_shape", "planner input contains trailing data")

    if not isinstance(parsed, dict) or not parsed:
        raise PlannerError("bad_shape", "planner input must be a JSON object")

    _validate_request_element_bounds(parsed)

    keys = set(parsed)
    if keys == _REQUEST_KEYS:
        return "wender_envelope", parsed
    if "schema" in parsed and "frame_ref" in parsed:
        return "observation", parsed
    if "observation" in parsed and keys <= _OBSERVATION_WRAPPER_KEYS:
        return "observation_wrapper", parsed

    raise PlannerError(
        "bad_shape",
        "planner request must be an observation or {observation, element_table, goal}",
    )


# @anchor:wa.plan.cli.main
def main(
    argv: list[str] | tuple[str, ...] | None = None,
    *,
    stdin=None,
    stdout=None,
    stderr=None,
) -> int:
    """Pipe CLI entry point: read request on stdin, write canonical JSON on stdout."""
    if argv is None:
        enter_hosted_mode()
    with planner_cli_guard_scope():
        args = list(sys.argv[1:] if argv is None else argv)
        in_stream = sys.stdin if stdin is None else stdin
        out_stream = sys.stdout if stdout is None else stdout
        err_stream = sys.stderr if stderr is None else stderr

        if args:
            line, code = _format_bounded_error(
                "bad_shape", "planner CLI takes no positional arguments or flags"
            )
            print(line, file=err_stream)
            return code

        try:
            mode, request = read_stdin_request(in_stream)
            if mode == "wender_envelope":
                response = plan(
                    request["observation"],
                    request["element_table"],
                    request["goal"],
                )
            elif mode == "observation":
                response = plan_holdings(request)
            else:
                kwargs = {k: v for k, v in request.items() if k != "observation"}
                response = plan_holdings(request["observation"], **kwargs)
            output = canonical_json(response)
        except PlannerError as exc:
            line, code = _format_bounded_error(exc.code, exc.detail)
            print(line, file=err_stream)
            return code
        except Exception:
            line, code = _format_bounded_error("bad_shape", _GENERIC_ERROR_DETAIL)
            print(line, file=err_stream)
            return code

        print(output, file=out_stream)
        return 0


if __name__ == "__main__":
    enter_hosted_mode()
    raise SystemExit(main())
