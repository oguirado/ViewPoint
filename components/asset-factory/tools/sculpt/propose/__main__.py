"""python3 -B -m tools.sculpt.propose --request FILE [--out FILE] [--gate]."""
import argparse
from pathlib import Path

from asset_factory import core
from .api import ProposalError, propose, verify_generator


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--gate", action="store_true")
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return 0 if exc.code == 0 else 2
    try:
        request = core.load_json(args.request, max_bytes=128*2**20)
        core.validate(request, core.load_json(Path(__file__).with_name("request.schema.json"), max_bytes=2**20))
        if request["schema"] != "af.sculpt-propose-request/v1":
            raise ProposalError(2, "schema_mismatch")
        operation = verify_generator if args.gate else propose
        result = operation(**{k: v for k, v in request.items() if k != "schema"})
        if result["rc"] == 0 and args.out is not None:
            # Successful publication only; every refusal preserves caller bytes.
            if args.out.resolve() == args.request.resolve():
                raise ProposalError(2, "input_output_alias")
            core.atomic_write_json(args.out, result)
        return core.emit(result)
    except core.ContractError as exc:
        return core.emit(exc.as_dict())
    except OSError:
        return core.emit({"rc": 2, "status": "ERROR", "reason_codes": ["io_error"]})


if __name__ == "__main__":
    raise SystemExit(main())
