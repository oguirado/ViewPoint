"""Host-owned per-capsule stream boundary contract vectors and validator.

Offline stdlib-only contract validator and test vectors for the host-owned
per-capsule stream boundary (task os-f-w8-3-10-switchboard-vectors).
The live switchboard implementation (src/w8wave/switchboard) is unmerged and
remains AUSENTE.
"""

from typing import Any


def __getattr__(name: str) -> Any:
    from contracts.w8evidence.busvectors import validator

    if hasattr(validator, name):
        return getattr(validator, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
