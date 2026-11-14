# @module {"id":"wa.tools.layer_check","layer":0,"owner":"ARCH","deps":[],"book":"HB-WA-a0-arch"}
"""@module wa.tools.layer_check - enforce layer-rule import constraints across wender modules."""
import ast
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULE_RE = re.compile(r"^(?:#|//) @module (\{.*\})\s*$")
VISION_PACKAGES = {"visioncore", "vmem"}


# @anchor:wa.tools.layer_check.check
def check(root):
    root_path = Path(root).resolve()
    wender_dir = root_path / "wender"
    if not wender_dir.is_dir():
        return []

    py_files = [p for p in sorted(wender_dir.rglob("*.py")) if p.name != "__init__.py"]

    modules_map = {}
    mod_path_to_rel = {}

    for p in py_files:
        rel = str(p.relative_to(root_path))
        lines = p.read_text(encoding="utf-8").splitlines()
        m = MODULE_RE.match(lines[0]) if lines else None
        header = json.loads(m.group(1)) if m else None
        modules_map[rel] = header

        parts = p.relative_to(root_path).with_suffix("").parts
        mod_dotted = ".".join(parts)
        mod_path_to_rel[mod_dotted] = rel

    violations = []
    seen = set()

    for p in py_files:
        rel = str(p.relative_to(root_path))
        importer_header = modules_map.get(rel)
        importer_layer = importer_header.get("layer") if importer_header else None
        importer_id = importer_header.get("id") if importer_header else rel

        importer_parts = list(p.relative_to(root_path).with_suffix("").parts[:-1])

        try:
            tree = ast.parse(p.read_text(encoding="utf-8"), filename=str(p))
        except Exception:
            continue

        for node in ast.walk(tree):
            line = getattr(node, "lineno", 1)

            if isinstance(node, ast.Import):
                for alias in node.names:
                    name = alias.name
                    top_pkg = name.split(".")[0]
                    if top_pkg in VISION_PACKAGES:
                        key = (rel, line, "vision_engine_import", name)
                        if key not in seen:
                            seen.add(key)
                            violations.append({
                                "file": rel,
                                "line": line,
                                "type": "vision_engine_import",
                                "importer": importer_id,
                                "imported": name,
                                "message": f"prohibited v0 import of vision-engine package '{name}'",
                            })
                    elif name.startswith("wender.") or name == "wender":
                        _check_target(name, rel, line, importer_layer, importer_id, modules_map, mod_path_to_rel, violations, seen)

            elif isinstance(node, ast.ImportFrom):
                mod = node.module
                level = node.level

                if level == 0 and mod:
                    top_pkg = mod.split(".")[0]
                    if top_pkg in VISION_PACKAGES:
                        key = (rel, line, "vision_engine_import", mod)
                        if key not in seen:
                            seen.add(key)
                            violations.append({
                                "file": rel,
                                "line": line,
                                "type": "vision_engine_import",
                                "importer": importer_id,
                                "imported": mod,
                                "message": f"prohibited v0 import of vision-engine package '{mod}'",
                            })
                    elif mod.startswith("wender.") or mod == "wender":
                        _resolve_and_check_import_from(mod, node.names, rel, line, importer_layer, importer_id, modules_map, mod_path_to_rel, violations, seen)

                elif level > 0:
                    if level <= len(importer_parts):
                        base_parts = importer_parts[:len(importer_parts) - (level - 1)]
                        if mod:
                            target_mod_parts = base_parts + mod.split(".")
                        else:
                            target_mod_parts = base_parts
                        target_mod_dotted = ".".join(target_mod_parts)
                        _resolve_and_check_import_from(target_mod_dotted, node.names, rel, line, importer_layer, importer_id, modules_map, mod_path_to_rel, violations, seen)

    violations.sort(key=lambda v: (v["file"], v["line"], v["type"], v.get("imported", "")))
    return violations


def _check_target(target_dotted, rel, line, importer_layer, importer_id, modules_map, mod_path_to_rel, violations, seen):
    target_rel = mod_path_to_rel.get(target_dotted)
    if not target_rel:
        return

    imported_header = modules_map.get(target_rel)
    imported_id = imported_header.get("id") if imported_header else target_rel

    if imported_header is None:
        key = (rel, line, "missing_header", target_rel)
        if key not in seen:
            seen.add(key)
            violations.append({
                "file": rel,
                "line": line,
                "type": "missing_header",
                "importer": importer_id,
                "imported": target_rel,
                "message": f"imported module '{target_rel}' is missing @module header",
            })
    else:
        imported_layer = imported_header.get("layer")
        if importer_layer is not None and imported_layer is not None:
            if importer_layer < imported_layer:
                key = (rel, line, "layer_violation", imported_id)
                if key not in seen:
                    seen.add(key)
                    violations.append({
                        "file": rel,
                        "line": line,
                        "type": "layer_violation",
                        "importer": importer_id,
                        "imported": imported_id,
                        "message": f"layer {importer_layer} module imports layer {imported_layer} module '{imported_id}'",
                    })


def _resolve_and_check_import_from(mod_dotted, names, rel, line, importer_layer, importer_id, modules_map, mod_path_to_rel, violations, seen):
    if mod_dotted in mod_path_to_rel:
        _check_target(mod_dotted, rel, line, importer_layer, importer_id, modules_map, mod_path_to_rel, violations, seen)
    else:
        for alias in names:
            child_dotted = f"{mod_dotted}.{alias.name}" if mod_dotted else alias.name
            if child_dotted in mod_path_to_rel:
                _check_target(child_dotted, rel, line, importer_layer, importer_id, modules_map, mod_path_to_rel, violations, seen)


# @anchor:wa.tools.layer_check.main
def main(argv=None):
    if argv is None:
        argv = sys.argv[1:]
    violations = check(ROOT)
    for v in violations:
        print(f"{v['file']}:{v['line']}: [{v['type']}] {v['message']}")
    return 3 if violations else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
