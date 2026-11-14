"""Proposed local text-only training-signal carrier validator package (w8evidence.training).

This package defines a proposed local offline contract under contracts/w8evidence/training/.
It is not a JSON Schema file, does not modify contracts/worker, and makes no claim about
any absent wender API, live capture, or model learning outcome.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "AUSENTE",
    "CARRIER_SCHEMA",
    "EXIT_GATE",
    "EXIT_INPUT",
    "EXIT_OK",
    "EXIT_OPERATOR",
    "EXIT_QUARANTINE",
    "EXIT_SETUP",
    "EXIT_UNAVAILABLE",
    "GRAB_UNAVAILABLE",
    "MAX_JSON_DEPTH",
    "TARGET_MAX_UNIX_SOCKET_BYTES",
    "DummyRendererPort",
    "FakeClock",
    "InMemorySupplyPort",
    "RejectError",
    "SupplyBundle",
    "ValidationResult",
    "canonical_encode",
    "exit_code_for",
    "main",
    "make_positive_synthetic_fixture",
    "strict_json_parse",
    "validate_carrier",
]


def __getattr__(name: str) -> Any:
    if name in __all__:
        from contracts.w8evidence.training import validator

        return getattr(validator, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
