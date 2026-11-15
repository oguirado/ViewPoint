#!/usr/bin/env python3
"""stdlib-only JSON/JSONL record schema validator for visioncore schemas."""

import argparse
import json
import os
import re
import sys
from pathlib import Path

ALLOWED_SCHEMA_KEYWORDS = {
    "$id",
    "$schema",
    "title",
    "description",
    "type",
    "required",
    "properties",
    "additionalProperties",
    "const",
    "enum",
    "pattern",
    "minimum",
    "maximum",
    "items",
    "minItems",
    "maxItems",
    "uniqueItems",
    "maxLength",
}


def validate_schema_keywords(schema_obj, path="$"):
    """Check that schema dict and all nested sub-schemas use only ALLOWED_SCHEMA_KEYWORDS."""
    if not isinstance(schema_obj, dict):
        return
    for key, value in schema_obj.items():
        if key not in ALLOWED_SCHEMA_KEYWORDS:
            raise ValueError(f"Unsupported schema keyword {key!r} at {path}")
        if key == "properties" and isinstance(value, dict):
            for prop_name, prop_schema in value.items():
                validate_schema_keywords(prop_schema, f"{path}.properties.{prop_name}")
        elif key == "items" and isinstance(value, dict):
            validate_schema_keywords(value, f"{path}.items")


def load_all_schemas(schema_dir=None):
    """Load and validate all *.schema.json files under visioncore/schemas."""
    if schema_dir is None:
        schema_dir = Path(__file__).resolve().parent.parent / "visioncore" / "schemas"
    else:
        schema_dir = Path(schema_dir)

    if not schema_dir.is_dir():
        raise RuntimeError(f"Schema directory not found: {schema_dir}")

    schemas = {}
    for p in sorted(schema_dir.glob("*.schema.json")):
        filename = p.name
        try:
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            raise RuntimeError(f"Failed to parse schema {filename}: {e}")

        if not isinstance(data, dict):
            raise RuntimeError(f"Schema {filename} must be a JSON object")

        sid = data.get("$id")
        if not sid or not isinstance(sid, str):
            raise RuntimeError(f"Schema {filename} missing string $id")

        expected_filename = sid.replace("/", ".") + ".schema.json"
        if filename != expected_filename:
            raise RuntimeError(
                f"Schema $id mismatch in {filename}: $id {sid!r} expects filename {expected_filename!r}"
            )

        validate_schema_keywords(data)
        schemas[sid] = data

    return schemas


def match_type(val, schema_type):
    """
    Check if val matches schema_type (which can be a str or list of str).
    JSON schema types: null, boolean, integer, number, string, array, object.
    In Python / JSON canon rules:
    - boolean is bool (note bool is a subclass of int in python!)
    - integer is int and NOT bool
    - number is (int, float) and NOT bool
    - string is str
    - array is list
    - object is dict
    - null is None
    """
    if isinstance(schema_type, list):
        return any(match_type(val, t) for t in schema_type)

    if schema_type == "null":
        return val is None
    elif schema_type == "boolean":
        return isinstance(val, bool)
    elif schema_type == "integer":
        return isinstance(val, int) and not isinstance(val, bool)
    elif schema_type == "number":
        return (isinstance(val, (int, float))) and not isinstance(val, bool)
    elif schema_type == "string":
        return isinstance(val, str)
    elif schema_type == "array":
        return isinstance(val, list)
    elif schema_type == "object":
        return isinstance(val, dict)
    else:
        return False


def validate_value(val, schema, path="$"):
    """
    Recursively validate a JSON value against a sub-schema dict.
    Returns a list of error strings with paths (e.g. "$.prop: error message").
    """
    errors = []
    if not isinstance(schema, dict):
        return errors

    # type
    if "type" in schema:
        expected_type = schema["type"]
        if not match_type(val, expected_type):
            errors.append(f"{path}: expected type {expected_type!r}, got {type(val).__name__}")
            return errors  # type mismatch stops further deep checking on this node

    # const
    if "const" in schema:
        expected_const = schema["const"]
        if val != expected_const or type(val) != type(expected_const):
            errors.append(f"{path}: expected const {expected_const!r}, got {val!r}")

    # enum
    if "enum" in schema:
        allowed_enum = schema["enum"]
        if not any(val == item and type(val) == type(item) for item in allowed_enum):
            errors.append(f"{path}: value {val!r} not in enum {allowed_enum!r}")

    # string checks
    if isinstance(val, str):
        if "pattern" in schema:
            pattern = schema["pattern"]
            if re.search(pattern, val) is None:
                errors.append(f"{path}: value {val!r} does not match pattern {pattern!r}")
        if "maxLength" in schema:
            max_len = schema["maxLength"]
            if len(val) > max_len:
                errors.append(f"{path}: string length {len(val)} exceeds maxLength {max_len}")

    # numeric checks (integer / number)
    if isinstance(val, (int, float)) and not isinstance(val, bool):
        if "minimum" in schema:
            min_val = schema["minimum"]
            if val < min_val:
                errors.append(f"{path}: value {val} is less than minimum {min_val}")
        if "maximum" in schema:
            max_val = schema["maximum"]
            if val > max_val:
                errors.append(f"{path}: value {val} is greater than maximum {max_val}")

    # array checks
    if isinstance(val, list):
        if "minItems" in schema:
            min_items = schema["minItems"]
            if len(val) < min_items:
                errors.append(f"{path}: array length {len(val)} is less than minItems {min_items}")
        if "maxItems" in schema:
            max_items = schema["maxItems"]
            if len(val) > max_items:
                errors.append(f"{path}: array length {len(val)} exceeds maxItems {max_items}")
        if schema.get("uniqueItems") is True:
            seen = []
            for item in val:
                if any(item == prev and type(item) == type(prev) for prev in seen):
                    errors.append(f"{path}: array items are not unique")
                    break
                seen.append(item)
        if "items" in schema:
            item_schema = schema["items"]
            for idx, item in enumerate(val):
                errors.extend(validate_value(item, item_schema, f"{path}[{idx}]"))

    # object checks
    if isinstance(val, dict):
        if "required" in schema:
            for req_key in schema["required"]:
                if req_key not in val:
                    errors.append(f"{path}: missing required property {req_key!r}")

        properties = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            for k in val.keys():
                if k not in properties:
                    errors.append(f"{path}: additional property {k!r} is not allowed")

        for prop_name, prop_schema in properties.items():
            if prop_name in val:
                errors.extend(validate_value(val[prop_name], prop_schema, f"{path}.{prop_name}"))

    return errors


def process_file(filepath, schemas):
    """
    Process a single JSON or JSONL file.
    Returns (stats_dict, list_of_error_strings).
    stats_dict: {"path": str, "total": int, "valid": int, "invalid": int}
    """
    path_obj = Path(filepath)
    if not path_obj.is_file():
        err_msg = f"{filepath}:1:$: file not found"
        return {"path": str(filepath), "total": 0, "valid": 0, "invalid": 1}, [err_msg]

    content = path_obj.read_text(encoding="utf-8")

    # Determine if JSON or JSONL by file extension or content structure
    rows = []
    is_json = filepath.endswith(".json") and not filepath.endswith(".jsonl")

    if is_json:
        try:
            parsed = json.loads(content)
            if isinstance(parsed, list):
                # Array of records in .json
                for idx, item in enumerate(parsed, start=1):
                    rows.append((idx, item, None))
            elif isinstance(parsed, dict):
                # Single record in .json
                rows.append((1, parsed, None))
            else:
                rows.append((1, None, "JSON root must be object or array"))
        except Exception as e:
            rows.append((1, None, f"JSON parse error: {e}"))
    else:
        # JSONL format (line by line)
        lines = content.splitlines()
        for line_num, line in enumerate(lines, start=1):
            line_str = line.strip()
            if not line_str:
                continue
            try:
                parsed = json.loads(line_str)
                rows.append((line_num, parsed, None))
            except Exception as e:
                rows.append((line_num, None, f"JSON parse error: {e}"))

    total = len(rows)
    valid = 0
    invalid = 0
    errors = []

    for line_num, item, parse_err in rows:
        if parse_err:
            invalid += 1
            errors.append(f"{filepath}:{line_num}:$: {parse_err}")
            continue

        if not isinstance(item, dict):
            invalid += 1
            errors.append(f"{filepath}:{line_num}:$: row is not a JSON object")
            continue

        schema_id = item.get("schema")
        if not schema_id:
            invalid += 1
            errors.append(f"{filepath}:{line_num}:$: missing 'schema' field in row")
            continue

        if schema_id not in schemas:
            invalid += 1
            errors.append(f"{filepath}:{line_num}:$.schema: unknown schema id {schema_id!r}")
            continue

        schema = schemas[schema_id]
        row_errors = validate_value(item, schema, "$")
        if row_errors:
            invalid += 1
            for err in row_errors:
                # err is formatted like "$.path: message" or "$: message"
                errors.append(f"{filepath}:{line_num}:{err}")
        else:
            valid += 1

    return {"path": str(filepath), "total": total, "valid": valid, "invalid": invalid}, errors


def main():
    parser = argparse.ArgumentParser(
        description="Validate JSON and JSONL records against visioncore schemas."
    )
    parser.add_argument("files", nargs="*", help="JSON or JSONL file paths to validate")
    parser.add_argument(
        "--schema-dir",
        default=None,
        help="Path to schemas directory (default: visioncore/schemas)",
    )
    args = parser.parse_args()

    try:
        schemas = load_all_schemas(args.schema_dir)
    except Exception as e:
        summary = {
            "files": [],
            "total_records": 0,
            "valid_records": 0,
            "invalid_records": 0,
            "errors": [f"Schema loading error: {e}"],
            "ok": False,
        }
        print(json.dumps(summary, indent=2, sort_keys=True))
        sys.exit(2)

    all_file_stats = []
    all_errors = []
    total_records = 0
    valid_records = 0
    invalid_records = 0

    for filepath in args.files:
        stats, errs = process_file(filepath, schemas)
        all_file_stats.append(stats)
        all_errors.extend(errs)
        total_records += stats["total"]
        valid_records += stats["valid"]
        invalid_records += stats["invalid"]

    ok = (invalid_records == 0) and (len(all_errors) == 0)
    # Print canonical JSON summary with up to first 20 errors
    summary = {
        "files": all_file_stats,
        "total_records": total_records,
        "valid_records": valid_records,
        "invalid_records": invalid_records,
        "errors": all_errors[:20],
        "ok": ok,
    }

    print(json.dumps(summary, indent=2, sort_keys=True))
    sys.exit(0 if ok else 2)


if __name__ == "__main__":
    main()
