"""Optional block-runtime selection for the ViewPoint data adapter."""

from concurrent.futures import Future
from dataclasses import dataclass
import importlib.util
from pathlib import Path
from threading import Lock, Thread
from typing import Callable, Literal


_SCHEMAS = Path(__file__).resolve().parents[2] / "schemas"
_SPEC = importlib.util.spec_from_file_location("vp_data_abi_validation", _SCHEMAS / "validation.py")
_VALIDATION = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_VALIDATION)

_COPY_RESULTS = frozenset({"put", "resolve", "status"})


@dataclass(frozen=True)
class DataResult:
    """Local outcome. Only ``result`` is the frozen per-operation wire payload."""

    result: dict
    copy: Literal["file", "none"]


@dataclass(frozen=True)
class RuntimeBinding:
    """A Python binding supplied by a qualified consumer of the KO header."""

    abi_version: str
    dispatch: Callable[..., DataResult]


class BlockRuntimeAdapter:
    """Route closed data operations to a file handler until a probe qualifies a runtime.

    The file handler takes adapter args and a ``dry_run`` keyword, and returns
    the per-operation result. The owner supplies any native binding and its
    expected version. Construction and dispatch work during the optional probe.
    Once dispatch uses files, this instance stays on the file handler so a late
    probe cannot move existing artifact or lease state to another backend.
    """

    def __init__(self, file_dispatch):
        self._file_dispatch = file_dispatch
        self._lock = Lock()
        self._runtime = None
        self._file_selected = False
        self._probe_future = None

    def start_probe(self, probe, *, expected_version: str) -> Future[bool]:
        """Start one asynchronous probe. The future reports native selection."""
        if not isinstance(expected_version, str) or not expected_version.strip():
            raise ValueError("an explicit ABI version is required")
        with self._lock:
            if self._probe_future is not None:
                return self._probe_future
            future = Future()
            future.set_running_or_notify_cancel()
            self._probe_future = future
            worker = Thread(target=self._probe, args=(probe, expected_version, future), daemon=True)
            try:
                worker.start()
            except RuntimeError:
                future.set_result(False)
        return future

    def _probe(self, probe, expected_version, future):
        try:
            candidate = probe()
        except (ImportError, OSError):
            future.set_result(False)
            return
        except Exception as error:
            future.set_exception(error)
            return

        compatible = (
            isinstance(candidate, RuntimeBinding)
            and candidate.abi_version == expected_version
            and callable(candidate.dispatch)
        )
        with self._lock:
            selected = compatible and not self._file_selected
            if selected:
                self._runtime = candidate
        future.set_result(selected)

    def dispatch(self, args, *, dry_run=False) -> DataResult:
        """Run the selected backend once and retain its copy mode with the result."""
        _VALIDATION.validate_contract("vp_data_call.args/v1", args)
        op = args["op"]
        with self._lock:
            runtime = self._runtime
            if runtime is None:
                self._file_selected = True
        if runtime is None:
            result = dict(self._file_dispatch(args, dry_run=dry_run))
            if op in _COPY_RESULTS:
                result["copy"] = "file"
            outcome = DataResult(result, "file")
        else:
            outcome = runtime.dispatch(args, dry_run=dry_run)

        _VALIDATION.validate_contract(f"vp_data_call.{op}.result/v1", outcome.result)
        if outcome.copy not in ("file", "none"):
            raise ValueError("data result has an unsupported copy mode")
        if op in _COPY_RESULTS and outcome.result["copy"] != outcome.copy:
            raise ValueError("data result copy mode disagrees with the wire payload")
        return outcome
