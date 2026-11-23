"""Rebuild router index inputs from the registry pinned by both exposure manifests."""

import argparse
from hashlib import sha256
import json
from pathlib import Path
import sys

from jsonschema import ValidationError


if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from control.core.control import CapabilityRegistry, canonical_bytes, digest
from expose.cli.generate import MANIFEST as CLI_MANIFEST
from expose.cli.verbs import registration_manifest as cli_manifest
from expose.mcp.generate import MANIFEST as MCP_MANIFEST
from expose.mcp.tools import registration_manifest as mcp_manifest
from router import router


_ROOT = Path(__file__).resolve().parents[3]
_GOLDEN = _ROOT / "components/vp-direct/fixtures/golden/router_golden.json"


def rebuild_inputs(registry):
    """Return the canonical pre-calibration projection and its manifest pins."""
    pins = {}
    for path, generate in ((MCP_MANIFEST, mcp_manifest), (CLI_MANIFEST, cli_manifest)):
        raw = path.read_bytes()
        if raw != canonical_bytes(generate(registry)):
            raise ValueError(f"Exposure manifest differs from C1: {path.relative_to(_ROOT)}")
        pins[path.relative_to(_ROOT).as_posix()] = sha256(raw).hexdigest()
    return router._index_inputs(registry.entries()), pins


def rebuild_receipt(inputs, pins):
    return {
        "calibration": "open",
        "registry_sha256": inputs["registry_sha256"],
        "exposure_manifest_sha256": pins,
        "index_inputs_sha256": digest(inputs),
        "model_facts_sha256": inputs["model_facts_sha256"],
        "tool_count": len(inputs["entries"]),
        "selector_count": sum(len(entry["ops"]) for entry in inputs["entries"]),
        "supported_selector_count": sum(row["supported"] for entry in inputs["entries"]
                                        for row in entry.get("op_table", [])),
    }


def reviewed_judgments(path, expected_sha256):
    """Require the operator-reviewed digest and screen out known development prompts."""
    path = path.resolve(strict=True)
    if not path.is_relative_to(_ROOT):
        raise ValueError("Held-out judgments must be inside the source root for the frozen receipt pin")
    if path == _GOLDEN.resolve(strict=True):
        raise ValueError("The development golden set cannot calibrate the publication")
    raw = path.read_bytes()
    if sha256(raw).hexdigest() != expected_sha256:
        raise ValueError("Calibration judgments differ from their reviewed digest")
    development = {router._tokens(case["request"]) for case in json.loads(_GOLDEN.read_bytes())["entries"]}
    judgments = json.loads(raw)
    prompts = [router._tokens(case["request"]) for case in judgments["entries"]]
    if not prompts or any(not prompt for prompt in prompts) or len(set(prompts)) != len(prompts):
        raise ValueError("Held-out judgments require distinct, nonempty prompts")
    if development.intersection(prompts):
        raise ValueError("Held-out judgments overlap the development golden set")
    return path


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--catalog-only", action="store_true", help="Use C1's frozen adapter catalog")
    source.add_argument("--registry", type=Path, help="Provisioned C1 capability entries as a JSON array")
    parser.add_argument("--held-out", type=Path, help="Independently judged prompt set for calibration")
    parser.add_argument("--held-out-sha256", help="Digest from independent review of the held-out set")
    parser.add_argument("--manifest-generation", type=int)
    parser.add_argument("--score-min", type=int)
    args = parser.parse_args(argv)
    calibration = (args.held_out, args.held_out_sha256, args.manifest_generation, args.score_min)
    if any(value is not None for value in calibration) and any(value is None for value in calibration):
        parser.error("calibration requires held-out judgments, their reviewed digest, generation and score minimum")
    try:
        entries = [] if args.catalog_only else json.loads(args.registry.read_bytes())
        if not isinstance(entries, list):
            raise ValueError("The registry must be a JSON array")
        registry = CapabilityRegistry(entries)
        inputs, pins = rebuild_inputs(registry)
        if args.held_out is None:
            result = rebuild_receipt(inputs, pins)
        else:
            path = reviewed_judgments(args.held_out, args.held_out_sha256)
            if args.manifest_generation < 0:
                raise ValueError("Manifest generation must be nonnegative")
            if any(entry["supported"] and (model := router._model(entry))
                   and router._model_denial(model) is None for entry in inputs["entries"]):
                raise ValueError("Model-bearing publication needs independently verified M2 evidence")
            index, receipt = router.build_index(
                registry.entries(), manifest_generation=args.manifest_generation,
                score_min=args.score_min, golden_path=path, source_root=_ROOT,
                expected_golden_sha256=args.held_out_sha256,
            )
            result = {"index": index, "calibration_receipt": receipt}
    except (OSError, ValueError, KeyError, TypeError, ValidationError) as error:
        print(error, file=sys.stderr)
        return 1
    sys.stdout.buffer.write(canonical_bytes(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
