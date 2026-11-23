#!/usr/bin/env python3
"""Blender calibration readiness: future B5 execution never masquerades as a clean run."""
import json
from pathlib import Path
import sys


def required_tool_paths():
    return ['blender_visual_qa/cli.py', 'blender_visual_qa/contracts.py', 'blender_visual_qa/report.py', 'blender_visual_qa/judge/__init__.py', 'tools/gates.py']


def main(argv=None):
    # Actual scene-oracle mutant qualification is owned by the later waves.
    result = {'code': 'E_INCOMPLETE', 'rc': 3, 'reason': 'B5_calibration_not_built', 'qualified': False}
    print(json.dumps(result, sort_keys=True, separators=(',', ':')))
    return 3


if __name__ == '__main__':
    sys.exit(main())
