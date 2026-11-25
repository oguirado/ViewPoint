"""Verify explicitly approved local profile pins with the D1 contract validator."""

from jsonschema import Draft202012Validator
from referencing import Registry
from referencing.jsonschema import DRAFT202012

from ..contracts.formats import format_checker
from ..contracts.validation import ContractError, ContractValidator, _check_schema, strict_json
from .files import digest


class PinnedProfiles:
    """Build from an operator-approved {(name, SHA-256): schema_bytes} mapping.

    No network retrieval is available. A binding verifier must check the
    profile's request, attempt and operation fields for its approved version.
    """

    def __init__(self, schemas, verify_binding):
        if not callable(verify_binding):
            raise TypeError("a version-aware profile binding verifier is required")
        self.verify_binding = verify_binding
        self.validators = {}
        parsed = {}
        registry = Registry()
        checker = format_checker()
        for (name, sha), raw in schemas.items():
            if digest(raw) != sha:
                raise ContractError(f"profile schema digest mismatch: {name}")
            document = strict_json(raw)
            if not isinstance(document, dict):
                raise ContractError(f"invalid profile schema: {name}")
            resource = DRAFT202012.create_resource(document)
            uri = document.get("$id")
            if uri is not None:
                if uri in registry:
                    raise ContractError(f"ambiguous profile schema URI: {uri}")
                registry = registry.with_resource(uri, resource)
            parsed[name, sha] = document, resource
        for pin, (document, resource) in parsed.items():
            _check_schema(document, resource, registry, checker)
            self.validators[pin] = Draft202012Validator(document, registry=registry,
                                                        format_checker=checker)

    def __call__(self, admission, profile, terminal):
        pin = admission["profile_schema"], admission["profile_schema_sha256"]
        if pin not in self.validators:
            raise ContractError("unapproved profile schema pin")
        ContractValidator._validate(self.validators[pin], profile)
        return self.verify_binding(admission, profile, terminal) is True
