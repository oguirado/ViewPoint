# SPDX-License-Identifier: AGPL-3.0-only
"""Canonical slot contracts and structural validation for slot and provider records."""

import argparse
from pathlib import Path
import re
from types import MappingProxyType
from typing import NamedTuple

from asset_factory import core
from asset_factory.studio_slots import EXTENSIONS

_SCHEMAS = Path(__file__).resolve().parents[1] / "slots"
# DESIGN_CHOICE: bounded metadata input, independent of the caller's asset size.
_MAX_JSON_BYTES = 16 * 2**20
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_MUTABLE_REVISIONS = frozenset(("main", "master", "latest", "HEAD"))
NEW_SLOTS = core.NEW_SLOTS
SLOTS = core.SLOTS + tuple(EXTENSIONS)


class SlotContract(NamedTuple):
    schema_id: str
    input_roles: tuple[frozenset[str], ...]
    output_roles: tuple[frozenset[str], ...]
    optional_output_roles: frozenset[str]


def _slot(schema_id, inputs, outputs, *, optional=()):
    return SlotContract(schema_id, tuple(frozenset(roles) for roles in inputs),
                        tuple(frozenset(roles) for roles in outputs), frozenset(optional))


_SLOT_CONTRACTS = MappingProxyType({
    "generate": _slot("af.generate/v1", ({"image"}, {"prompt"}), ({"mesh_glb"},), optional={"texture"}),
    "refine": _slot("af.refine/v1", ({"coarse_glb", "reference_image"},), ({"refined_glb"},)),
    "topology": _slot("af.topology/v1", ({"raw_glb"},), ({"topology_glb"},), optional={"normal_map"}),
    "uv_texture": _slot("af.uv_texture/v1", ({"glb", "reference_image"},),
                        ({"textured_glb", "albedo", "normal", "orm"},)),
    "parts": _slot("af.parts/v1", ({"glb"}, {"image"}), ({"parts_glb", "part_map"},)),
    "rig": _slot("af.rig/v1", ({"glb"},), ({"rigged_glb", "skeleton"},)),
    "motion": _slot("af.motion/v1", ({"rig_glb", "video"}, {"rig_glb", "text"}),
                    ({"action_bvh"}, {"action_glb"})),
    "render": _slot("af.render/v1", ({"blend_scene"},), ({"frame_set", "render_receipt"},)),
    "cad_code": _slot("af.cad-code/v1", ({"cad_source"},), ({"cad_brep", "cad_mesh"},)),
    "collision": _slot("af.collision/v1", ({"visual_glb"},), ({"collision_glb", "collision_manifest"},)),
    "physics": _slot("af.physics/v1", ({"physics_scene", "physics_protocol"},),
                     ({"physics_trace", "physics_receipt"},)),
    "procedural": _slot("af.procedural/v1", ({"recipe"},), ({"scene_blend", "scene_glb"},)),
    "scene_compose": _slot("af.scene-compose/v1", ({"asset_set", "layout_recipe"},),
                           ({"scene_blend", "scene_usd"},)),
    "denoise": _slot("af.denoise/v1", ({"frame_set"},), ({"denoised_frame_set", "denoise_receipt"},)),
    "texture": _slot("af.texture/v1", ({"glb", "reference_image"},),
                     ({"textured_glb", "albedo", "normal", "orm"},)),
    **{name: _slot(schema_id, (inputs,), (outputs,))
       for name, (schema_id, inputs, outputs) in EXTENSIONS.items()},
})
_INPUTS = MappingProxyType({slot: contract.input_roles for slot, contract in _SLOT_CONTRACTS.items()})
_OUTPUTS = MappingProxyType({
    slot: (contract.output_roles[0], contract.optional_output_roles)
    for slot, contract in _SLOT_CONTRACTS.items() if len(contract.output_roles) == 1
})


def slot_contract(slot: str) -> SlotContract:
    """Resolve a slot selector to its wire schema ID and required artifact roles."""
    if type(slot) is not str or slot not in SLOTS:
        raise core.ContractError("unknown_slot", "$/slot")
    return _SLOT_CONTRACTS[slot]


def _text(value, path):
    if type(value) is not str or not value.strip():
        raise core.ContractError("schema_mismatch", path)


def _revision(value, path):
    if type(value) is not str or not value.strip() or value in _MUTABLE_REVISIONS:
        raise core.ContractError("unpinned_revision", path)


def _digest(value, path):
    if type(value) is not str or _DIGEST.fullmatch(value) is None:
        raise core.ContractError("schema_mismatch", path)


def _file(item, path, *, receipt=False):
    for key in (("schema", "path") if receipt else ("role", "path", "media_type")):
        _text(item[key], path + "/" + key)
    _digest(item["sha256"], path + "/sha256")


def _roles(items, path="$/inputs"):
    if type(items) is not list or any(type(i) is not dict or type(i.get("role")) is not str for i in items):
        raise core.ContractError("schema_mismatch", path)
    return [i["role"] for i in items]


def load_slot_schema(slot: str) -> dict:
    slot_contract(slot)
    return core.load_json(_SCHEMAS / (slot + ".schema.json"), max_bytes=_MAX_JSON_BYTES)


def validate_slot(payload: dict, slot: str, kind: str) -> None:
    """Validate a slot record using its packaged schema."""
    validate_slot_record(payload, slot, kind)


def validate_slot_record(payload: dict, slot: str, kind: str, *, schema: dict | None = None) -> None:
    """Validate a slot record, optionally against a trusted packaged schema snapshot."""
    contract = slot_contract(slot)
    if type(kind) is not str or kind not in ("request", "result"):
        raise core.ContractError("unknown_kind", "$/kind")
    # Run this before minItems so empty exclusive unions retain their named error.
    if kind == "request" and type(payload) is dict and "inputs" in payload:
        roles = _roles(payload["inputs"])
        if len(contract.input_roles) > 1:
            if len(roles) != len(set(roles)) or set(roles) not in _INPUTS[slot]:
                raise core.ContractError("input_exclusivity", "$/inputs")
    if schema is None:
        schema = load_slot_schema(slot)
    core.validate(payload, schema)
    if payload["schema"] != contract.schema_id:
        raise core.ContractError("schema_mismatch", "$/schema")
    if payload["kind"] != kind:
        raise core.ContractError("schema_mismatch", "$/kind")
    for key in ("request_id", "provider_id"):
        _text(payload[key], "$/" + key)
    _revision(payload["provider_revision"], "$/provider_revision")
    if kind == "request":
        roles = _roles(payload["inputs"])
        if len(roles) != len(set(roles)) or set(roles) not in _INPUTS[slot]:
            raise core.ContractError("input_exclusivity", "$/inputs")
        context = payload["context"]
        if "jurisdiction" in context:
            _text(context["jurisdiction"], "$/context/jurisdiction")
        for key in ("policy_sha256", "toolchain_sha256"):
            _digest(context[key], "$/context/" + key)
        if context["timeout_s"] <= 0:
            raise core.ContractError("schema_mismatch", "$/context/timeout_s")
        for index, item in enumerate(payload["inputs"]):
            path = "$/inputs/" + str(index)
            if item["role"] in ("prompt", "text"):
                _text(item["text"], path + "/text")
            else:
                _file(item, path)
        return
    rc = payload["rc"]
    if type(rc) is not int or rc not in core.RC or payload["status"] != core.STATUS[rc]:
        raise core.ContractError("rc_status_mismatch", "$/status")
    if rc == 0 and not payload["receipts"]:
        raise core.ContractError("ok_without_receipt", "$/receipts")
    roles = _roles(payload["outputs"], "$/outputs")
    if len(roles) != len(set(roles)):
        raise core.ContractError("schema_mismatch", "$/outputs")
    if rc == 0:
        present = set(roles)
        complete = any(required <= present <= required | contract.optional_output_roles
                       for required in contract.output_roles)
        if not complete:
            raise core.ContractError("schema_mismatch", "$/outputs")
    for index, item in enumerate(payload["outputs"]):
        _file(item, "$/outputs/" + str(index))
    for index, item in enumerate(payload["receipts"]):
        _file(item, "$/receipts/" + str(index), receipt=True)
    for key, value in payload["metrics"].items():
        if value is None:
            reason = payload["absence_reasons"].get(key)
            if type(reason) is not str or not reason.strip():
                raise core.ContractError("null_metric_without_reason", "$/metrics/" + key)
        elif value < 0:
            raise core.ContractError("schema_mismatch", "$/metrics/" + key)
    for key, value in payload["absence_reasons"].items():
        _text(value, "$/absence_reasons/" + key)


def validate_provider(manifest: dict) -> None:
    """Validate a provider declaration before backend-specific admission."""
    schema = core.load_json(_SCHEMAS / "provider.schema.json", max_bytes=_MAX_JSON_BYTES)
    core.validate(manifest, schema)
    _text(manifest["id"], "$/id")
    if manifest["revision"] is not None or manifest["runnable"]:
        _revision(manifest["revision"], "$/revision")
    if not manifest["runnable"]:
        _text(manifest["blocked_reason"], "$/blocked_reason")
    licence = manifest.get("licence")
    if licence is not None:
        _text(licence["code"], "$/licence/code")
        if licence.get("weights") is not None:
            _text(licence["weights"], "$/licence/weights")
    for index, dep in enumerate(licence["deps"] if licence is not None else ()):
        for key in ("name", "licence"):
            _text(dep[key], "$/licence/deps/" + str(index) + "/" + key)
        for territory in dep["territory_excluded"]:
            _text(territory, "$/licence/deps/territory_excluded")
    for territory in manifest["territory_excluded"]:
        _text(territory, "$/territory_excluded")
    for index, weight in enumerate(manifest["weights"]):
        path = "$/weights/" + str(index)
        _text(weight["repo"], path + "/repo")
        _revision(weight["revision"], path + "/revision")
        for file in weight["files"]:
            _text(file["path"], path + "/files/path")
            _digest(file["sha256"], path + "/files/sha256")
    runtime = manifest["runtime"]
    # A local adapter can declare no container and prove execution separately.
    if runtime["container"] is not None:
        _text(runtime["container"], "$/runtime/container")
    peak = runtime["ram_peak_gb_measured"]
    reference = runtime["ram_peak_measurement_ref"]
    if peak is not None and (peak < 0 or reference is None):
        raise core.ContractError("schema_mismatch", "$/runtime/ram_peak_gb_measured")
    if reference is not None:
        _text(reference["path"], "$/runtime/ram_peak_measurement_ref/path")
        _digest(reference["sha256"], "$/runtime/ram_peak_measurement_ref/sha256")


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise core.ContractError("schema_mismatch", "$argv")


def main(argv: list[str] | None = None) -> int:
    parser = _Parser(add_help=False, allow_abbrev=False)
    parser.add_argument("--slot")
    parser.add_argument("--kind")
    parser.add_argument("--in", dest="input_path")
    parser.add_argument("--provider")
    try:
        args = parser.parse_args(argv)
        if args.provider is not None:
            if any(v is not None for v in (args.slot, args.kind, args.input_path)):
                raise core.ContractError("schema_mismatch", "$argv")
            validate_provider(core.load_json(Path(args.provider), max_bytes=_MAX_JSON_BYTES))
        else:
            if any(v is None for v in (args.slot, args.kind, args.input_path)):
                raise core.ContractError("missing_required", "$argv")
            validate_slot(core.load_json(Path(args.input_path), max_bytes=_MAX_JSON_BYTES), args.slot, args.kind)
        return core.emit({"rc": 0, "status": "OK"})
    except core.ContractError as exc:
        return core.emit(exc.as_dict())


if __name__ == "__main__":
    raise SystemExit(main())
