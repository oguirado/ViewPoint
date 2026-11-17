"""Private subprocess protocol: one integer-only JSON request on stdin."""
import sys
from asset_factory import core
from gates.rank.evaluate import MAX_JSON, _json, evaluate
from gates.rank.metrics import RankError


def main():
    try:
        raw = sys.stdin.buffer.read(MAX_JSON + 1)
        if len(raw) > MAX_JSON:
            raise core.ContractError("request_budget_exceeded")
        result = evaluate(_json(raw))
    except core.ContractError as exc:
        result = exc.as_dict()
    except RankError as exc:
        result = {"rc": exc.rc, "reason_codes": [exc.reason]}
    except Exception:
        # A failed third-party import/forward is an ERROR, never private rc 1.
        result = {"rc": 2, "reason_codes": ["worker_exception"]}
    print(core.canonical_json(result), end="")
    return result["rc"]


if __name__ == "__main__":
    raise SystemExit(main())
