# SPDX-License-Identifier: AGPL-3.0-only
"""Validate the records at the version installed in the component."""
from hashlib import sha256
import json
from pathlib import Path

from jsonschema import Draft202012Validator, ValidationError
from referencing import Registry, Resource

COMPONENT = Path(__file__).resolve().parents[1]
CONTRACTS = COMPONENT / "contracts"


def canonical(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                       allow_nan=False) + "\n").encode()


def digest(data):
    return sha256(data).hexdigest()


class Contracts:
    def __init__(self):
        names = ("frame", "video_source", "keyframe_set")
        self.schemas = {}
        resources = []
        pins = json.loads((CONTRACTS / "PINS.json").read_bytes())
        for name in names:
            path = CONTRACTS / f"rea.{name}_v1.schema.json"
            schema = json.loads(path.read_bytes())
            if digest(canonical(schema)) != pins["files"][path.name]:
                raise ValueError(f"contract pin mismatch: {name}")
            self.schemas[name] = schema
            resources.append((schema["$id"], Resource.from_contents(schema)))
        registry = Registry().with_resources(resources)
        self.validators = {name: Draft202012Validator(schema, registry=registry)
                           for name, schema in self.schemas.items()}

    def checked(self, name, record):
        try:
            self.validators[name].validate(record)
        except ValidationError as exc:
            path = ".".join(map(str, exc.absolute_path)) or "<record>"
            raise ValueError(f"rea.{name} contract rejected {path} ({exc.validator})") from exc
        return record
