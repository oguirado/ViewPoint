"""python3 -m tools.pipeline.run_asset: receipt-bound, fail-closed asset runner.

BYO skips only the image-alpha gate. Geometry inventory/policy, image policy,
licence configuration, independent appearance review and profile evidence are
explicit inputs; absent evidence yields CANNOT_SEE. No weights are downloaded.
"""
import argparse
from pathlib import Path

from asset_factory import core
from .engine import Pipeline, fault, file_ref, load, require
from .stages import NativeStages


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise core.ContractError("usage")


def matte_receipt_path(value):
    path = Path(value)
    ref = file_ref(path)
    receipt = load(path, expected_sha256=ref["sha256"])
    require(type(receipt) is dict and receipt.get("schema") == "af.input-evidence/v1"
            and receipt.get("gate_id") == "input.matte", 2, "matte_receipt_invalid")
    return path


def parser():
    p = Parser(description=__doc__)
    source = p.add_mutually_exclusive_group(required=True)
    source.add_argument("--image", type=Path)
    source.add_argument("--glb", type=Path)
    p.add_argument("--provider", choices=("trellis2", "byo"), required=True)
    p.add_argument("--reference", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    intake = p.add_mutually_exclusive_group()
    intake.add_argument("--input-policy", type=Path)
    intake.add_argument("--matte-receipt", dest="input_policy", type=matte_receipt_path,
                        help="D18 matte receipt with digest-named witnesses alongside it")
    p.add_argument("--qualification", nargs=2, metavar=("PATH", "SHA256"),
                   help="digest-pinned provider admission record")
    for flag in ("mesh-policy", "factory-config", "territory-policy",
                 "provider-manifest", "trace", "appearance-request", "appearance-review"):
        p.add_argument("--" + flag, type=Path)
    p.add_argument("--profile")
    p.add_argument("--profile-sha256")
    p.add_argument("--required-check", action="append", dest="required_checks", default=[])
    p.add_argument("--method", choices=("identity", "decimate", "quadriflow"), default="identity")
    p.add_argument("--target-faces", type=int)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--pipeline-type", default="512")
    p.add_argument("--timeout-s", type=int, default=300)
    p.add_argument("--texture-resolution", type=int, default=64)
    p.add_argument("--max-stretch", type=int, default=4000)
    return p


def main(argv=None):
    try:
        args = parser().parse_args(argv)
        require((args.image is not None) == (args.provider == "trellis2"), 2, "source_provider_mismatch")
        require(1 <= args.timeout_s <= 300, 2, "timeout_invalid")
        backend = NativeStages(source=args.image or args.glb, reference=args.reference,
                               provider=args.provider, options=vars(args))
        result = Pipeline(args.out, backend).run()
    except Exception as exc:
        rc, reason = fault(exc)
        result = {"schema": "af.pipeline-error/v1", "rc": rc, "status": core.STATUS[rc],
                  "verdict": core.VERDICT[rc], "reason_codes": [reason]}
    return core.emit(result)


if __name__ == "__main__":
    raise SystemExit(main())
