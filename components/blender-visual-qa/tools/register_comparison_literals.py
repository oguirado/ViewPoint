#!/usr/bin/env python3
"""Inventory integer comparison bounds; TARGET rows are contracts, not calibration evidence."""
import ast
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent


def collect(root):
    found = {}
    for folder in ('blender_visual_qa', 'bpy_side', 'tools'):
        for path in sorted((root / folder).rglob('*.py')):
            if '__pycache__' in path.parts: continue
            for node in ast.walk(ast.parse(path.read_text())):
                if not isinstance(node, ast.Compare): continue
                for part in ast.walk(node):
                    if isinstance(part, ast.Constant) and type(part.value) is int:
                        found.setdefault(part.value, []).append({'path': path.relative_to(root).as_posix(), 'line': part.lineno})
    return {str(v): sorted({(r['path'],r['line']) for r in refs}) for v,refs in sorted(found.items())}


def main():
    try:
        refs = collect(ROOT)
        target = ROOT / 'catalogue/comparison_literals.json'
        target.write_text(json.dumps(refs,sort_keys=True,separators=(',', ':'))+'\n')
        source = ROOT / 'catalogue/thresholds.src'
        lines = [line for line in source.read_text().splitlines() if not line.startswith('CMP_INT_')]
        values = {int(line.split('\t')[1]) for line in lines[1:]}
        for value in refs:
            integer = int(value)
            if integer not in values:
                name = 'CMP_INT_' + value.replace('-', 'NEG_')
                lines.append('\t'.join((name,value,'contract-bound','BINT','TARGET','catalogue/comparison_literals.json#'+value)))
        source.write_text('\n'.join(lines)+'\n')
        print(json.dumps({'code':'OK','rc':0,'values':len(refs)},sort_keys=True,separators=(',', ':')))
        return 0
    except (OSError,ValueError,SyntaxError) as exc:
        print(json.dumps({'code':'E_SCHEMA','rc':2,'reason':str(exc)},sort_keys=True,separators=(',', ':')))
        return 2


if __name__ == '__main__':
    sys.exit(main())
