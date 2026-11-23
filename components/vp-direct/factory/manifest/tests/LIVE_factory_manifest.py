#!/usr/bin/env python3
"""LIVE: check the operator-provided production manifest without factory effects."""

import argparse
from importlib.util import module_from_spec, spec_from_file_location
import json
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path, help="Production manifest below components/asset-factory")
    options = parser.parse_args()

    spec = spec_from_file_location("factory_manifest", Path(__file__).resolve().parents[1] / "manifest.py")
    m1 = module_from_spec(spec)
    spec.loader.exec_module(m1)
    manifest = m1.load_manifest(options.manifest)
    ops = manifest.registry_entry()["ops"]
    tool, = manifest.tools_list()
    if not (
        tool["name"] == m1.TOOL
        and tool["inputSchema"]["properties"]["op"]["enum"] == ops
        and manifest.cli_ops() == {m1.CLI: ops}
        and manifest.json_lane_ops() == {m1.TOOL: ops}
    ):
        raise SystemExit("Factory operation projections disagree")

    for op in ops:
        receipt = manifest.preview({
            "schema": m1.REQUEST,
            "request_id": f"live-m1-{op}",
            "op": m1.TOOL,
            "context": {"caller": "agent", "agent_instance": "m1-live-check"},
            "args": {"op": op, "goal_id": "m1-live-check", "inputs": []},
            "dry_run": True,
        })
        print(json.dumps(receipt, sort_keys=True, allow_nan=False))


if __name__ == "__main__":
    main()
