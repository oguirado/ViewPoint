"""LIVE D4 check for operator-provisioned P6 accounting and a qualified runtime."""

import argparse
from importlib.util import module_from_spec, spec_from_file_location
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from data.residency import ResidencyClient, ResidencyError


def _load_client(path):
    # The operator supplies a trusted local module exporting create_client().
    spec = spec_from_file_location("operator_d4_wiring", path)
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    client = module.create_client()
    if not isinstance(client, ResidencyClient):
        raise TypeError("D4 wiring must return a configured ResidencyClient")
    return client


def _lease(client, op, digest, holder):
    args = {"op": op, "model_sha256": digest, "holder": holder, "device": "gpu"}
    return client.handle(args, holder=holder)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wiring", type=Path, required=True,
                        help="trusted owner module supplying real P6 and runtime receipt verification")
    parser.add_argument("--model-sha256", required=True)
    parser.add_argument("--holder", required=True)
    parser.add_argument("--competing-holder", required=True)
    parser.add_argument("--competing-model-sha256", help="separately qualified GPU model digest")
    args = parser.parse_args()
    if args.holder == args.competing_holder:
        parser.error("use distinct provisioned holders")
    digest = args.model_sha256
    competing_digest = args.competing_model_sha256 or digest
    client = _load_client(args.wiring)
    if client.mode != "cold_process" or client.handle({"op": "status"}, holder=args.holder)["leases"]:
        raise SystemExit("the check requires an empty dedicated lease owner")

    acquired = _lease(client, "lease_acquire", digest, args.holder)
    try:
        if acquired["state"] != "held" or client.mode != "resident":
            raise AssertionError("resident state lacks verified acquisition")
        try:
            _lease(client, "lease_acquire", competing_digest, args.competing_holder)
        except ResidencyError as error:
            if error.code != "BUDGET_EXCEEDED":
                raise
            refusal = error.code
        else:
            # Both are test-owned leases if the exclusivity oracle fails.
            _lease(client, "lease_release", competing_digest, args.competing_holder)
            client.evict(competing_digest, holder=args.competing_holder)
            raise AssertionError("a competing GPU lease was granted")
    finally:
        released = _lease(client, "lease_release", digest, args.holder)
        retained_mode = client.mode
        evicted = client.evict(digest, holder=args.holder)

    if released["state"] != "released" or retained_mode != "resident":
        raise SystemExit("release did not retain the verified model")
    if evicted["state"] != "evicted" or evicted["model_sha256"] != digest or client.mode != "cold_process":
        raise SystemExit("verified eviction did not settle the model by digest")
    print(json.dumps({"acquired": acquired, "competing_model_sha256": competing_digest,
                      "refusal": refusal, "released": released, "evicted": evicted,
                      "mode": client.mode}, sort_keys=True))


if __name__ == "__main__":
    main()
