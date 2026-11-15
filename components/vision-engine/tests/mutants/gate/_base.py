"""Clean minimal tree used by gate mutants: each mutant overlays one defect on it."""
import json

IDS = (
    "def field_id(frame, morton):\n"
    "    return 'f%d:t%04x' % (frame, morton)\n\n"
    "def region_id(frame, morton):\n"
    "    return 'r%d:%04x' % (frame, morton)\n"
)
GRID = (
    '# @module {"id":"vc.grid","layer":1,"deps":["vc.ids","vc.p"]}\n'
    "from visioncore import ids, params\n"
    "# @anchor:vc.s0.grid\n"
    "def grid(w, h):\n"
    "    return ids.field_id(0, 0), params.PARAMS\n"
)


def base_tree():
    return {
        "llms.txt": "vision-engine\nsee [agents](AGENTS.md)\n",
        "AGENTS.md": "# agents\nrun `tools/gates.py`\n",
        "tools/gates.py": "import json\n",
        "visioncore/__init__.py": "API_VERSION = '1.1.0'\n",
        "visioncore/ids.py": IDS,
        "visioncore/params.py": "PARAMS = {'T0_Q4': 32, 'H_DEAD': 2}\n",
        "visioncore/layout.py": "REC = 32\n",
        "visioncore/errors.py": "class VqaError(Exception):\n    pass\n",
        "visioncore/canon.py": "import json\n",
        "visioncore/grid.py": GRID,
        "visioncore/desc.py": "import visioncore.ids\n",
        "visioncore/change.py": "from visioncore import grid\nT = 3 // 2\n",
        "visioncore/gop.py": "from visioncore import change\n",
        "visioncore/regions.py": "from visioncore import change\n",
        "visioncore/schemas/vqa.tile.v1.schema.json": json.dumps({"$id": "vqa.tile/v1"}),
        "catalogue/thresholds.tsv": "threshold_id\tvalue\tunit\nT0_Q4\t32\tq4\nH_DEAD\t2\tbits\n",
        "catalogue/symbols.jsonl": json.dumps({"anchor": "vc.s0.grid", "path": "visioncore/grid.py", "line": 3, "symbol": "grid"}, sort_keys=True, separators=(",", ":")) + "\n",
        "catalogue/measurements.json": "{}\n",
    }
