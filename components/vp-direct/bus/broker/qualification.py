"""Load the supervisor-provisioned W-L0 projection for operation-specific checks."""
from copy import deepcopy
import json
from pathlib import Path


def load_wl0(source=None):
    """Return an independent vp.l0_receipt/v1 mapping from a path or parsed receipt."""
    if source is None:
        source = Path(__file__).resolve().parents[2] / "config" / "wl0.json"
    try:
        receipt = deepcopy(source) if isinstance(source, dict) else json.loads(Path(source).read_bytes())
        if (not isinstance(receipt, dict) or receipt.get("schema") != "vp.l0_receipt/v1"
                or not isinstance(receipt.get("steps"), dict)):
            raise ValueError("invalid W-L0 projection")
    except (OSError, TypeError, ValueError) as exc:
        raise ValueError("W-L0 qualification unavailable") from exc
    return receipt
