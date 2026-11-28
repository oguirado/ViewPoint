#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Regenerate the release inventory from the contracts in this distribution."""

import copy
import hashlib
import json
from pathlib import Path
import re
import sys

HERE = Path(__file__).resolve().parent
ROOT = HERE / "lib/viewpoint-studio"
sys.path.insert(0, str(ROOT))

from asset_factory.studio_slots import EXTENSIONS
from registry import canonical, inventory, mcp_manifest, sha256


def extension_schemas():
    template = json.loads((ROOT / "slots/generate.schema.json").read_bytes())
    for name, (schema_id, inputs, outputs) in EXTENSIONS.items():
        schema = copy.deepcopy(template)
        schema["$id"] = "urn:vp:studio:" + name + ":v1"
        request = schema["$defs"]["request"]["properties"]
        result = schema["$defs"]["result"]["properties"]
        file_input, text_input = request["inputs"]["items"]["oneOf"]
        file_input["properties"]["role"]["enum"] = [role for role in inputs if role != "text"]
        text_input["properties"]["role"]["const"] = "text"
        request["inputs"]["items"] = {"oneOf": [file_input, text_input]} if "text" in inputs else file_input
        for properties in (request, result):
            properties["schema"]["const"] = schema_id
            properties["slot"]["const"] = name
        result["outputs"]["items"]["properties"]["role"]["enum"] = list(outputs)
        (ROOT / f"slots/{name}.schema.json").write_bytes(canonical(schema))


def main():
    extension_schemas()
    registry = inventory()
    registry_file = ROOT / "registry.json"
    registry_file.write_bytes(canonical(registry))
    (ROOT / "mcp-manifest.json").write_bytes(canonical(mcp_manifest(
        sha256(registry_file), registry["slots"])))
    sealed = {"asset_factory", "slots", "tools", "viewpoint"}
    files = ["episode_cli.py", "registry.py", "providers.py", "mcp_stdio.py", "registry.json", "mcp-manifest.json"]
    files.extend(str(path.relative_to(ROOT)) for path in ROOT.rglob("*")
                 if path.is_file() and path.relative_to(ROOT).parts[0] in sealed
                 and path.suffix in {".py", ".json"})
    seal = {"schema": "vp.studio.release-seal/v1",
            "files": {name: sha256(ROOT / name) for name in sorted(files)},
            "launcher_sha256": sha256(HERE / "bin/viewpoint-studio")}
    seal_bytes = canonical(seal)
    (ROOT / "release-seal.json").write_bytes(seal_bytes)
    entry = ROOT / "studio.py"
    content, matches = re.subn(r'^RELEASE_SEAL_SHA256 = "[^"\n]+"$',
                               f'RELEASE_SEAL_SHA256 = "{hashlib.sha256(seal_bytes).hexdigest()}"',
                               entry.read_text(encoding="utf-8"), flags=re.M)
    if matches != 1:
        raise RuntimeError("missing release-seal pin location")
    entry.write_text(content, encoding="utf-8")


if __name__ == "__main__":
    main()
