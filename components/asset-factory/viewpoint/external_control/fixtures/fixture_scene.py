# SPDX-License-Identifier: AGPL-3.0-only
"""Redistributable triangle used by the structured contract fixtures."""

from base64 import b64encode
from pathlib import Path
from struct import pack
import subprocess
import sys


def blender_run(executable, action, node, artifact):
    if not executable:
        raise ValueError("Blender executable pin required")
    script = Path(__file__).with_name("blender_scene.py")
    result = subprocess.run(
        [executable, "--background", "--factory-startup", "--threads", "1",
         "--python-exit-code", "3", "--python", str(script), "--", action, node, artifact],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    if result.returncode:
        print(result.stdout, file=sys.stderr)
        result.check_returncode()


def triangle(node):
    points = (0, 0, 0, 1, 0, 0, 0, 1, 0)
    buffer = pack("<9f3H", *points, 0, 1, 2)
    return {
        "asset": {"version": "2.0"},
        "scene": 0,
        "scenes": [{"nodes": [0]}],
        "nodes": [{"name": node, "mesh": 0}],
        "meshes": [{"primitives": [{"attributes": {"POSITION": 0}, "indices": 1}]}],
        "buffers": [{"byteLength": len(buffer),
                     "uri": "data:application/octet-stream;base64," + b64encode(buffer).decode()}],
        "bufferViews": [{"buffer": 0, "byteLength": 36},
                        {"buffer": 0, "byteOffset": 36, "byteLength": 6}],
        "accessors": [
            {"bufferView": 0, "componentType": 5126, "type": "VEC3", "count": 3,
             "min": [0, 0, 0], "max": [1, 1, 0]},
            {"bufferView": 1, "componentType": 5123, "type": "SCALAR", "count": 3},
        ],
    }
