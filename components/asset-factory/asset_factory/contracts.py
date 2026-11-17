"""Additive slot/provider metadata validators; never authenticate evidence or licences.

SLOTS is the expanded contract registry. The F0 kernel remains unchanged; consumers
that enumerate core.SLOTS need an explicit integration migration before dispatch.
Validation returns no asset verdict and never qualifies a provider runtime.
"""

import argparse
from pathlib import Path
import re
from types import MappingProxyType

from asset_factory import core

_SCHEMAS = Path(__file__).resolve().parents[1] / "slots"
# DESIGN_CHOICE: bounded metadata input, independent of the caller's asset size.
_MAX_JSON_BYTES = 16 * 2**20
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_MUTABLE_REVISIONS = frozenset(("main", "master", "latest", "HEAD"))
NEW_SLOTS = ("cad_code", "collision", "physics", "procedural", "scene_compose", "denoise", "texture")
SLOTS = core.SLOTS + NEW_SLOTS
_INPUTS = {"generate": ({"image"}, {"prompt"}),
           "refine": ({"coarse_glb", "reference_image"},),
           "topology": ({"raw_glb"},), "uv_texture": ({"glb", "reference_image"},),
           "parts": ({"glb"}, {"image"}), "rig": ({"glb"},),
           "motion": ({"rig_glb", "video"}, {"rig_glb", "text"}),
           "render": ({"blend_scene"},),
           "cad_code": ({"cad_source"},), "collision": ({"visual_glb"},),
           "physics": ({"physics_scene", "physics_protocol"},),
           "procedural": ({"recipe"},),
           "scene_compose": ({"asset_set", "layout_recipe"},),
           "denoise": ({"frame_set"},),
           # DESIGN_CHOICE: texture is image-conditioned GLB/PBR texturing,
           # with the same artifact roles as uv_texture and a distinct slot id.
           "texture": ({"glb", "reference_image"},)}
_OUTPUTS = {"generate": ({"mesh_glb"}, {"texture"}),
            "refine": ({"refined_glb"}, set()),
            "topology": ({"topology_glb"}, {"normal_map"}),
            "uv_texture": ({"textured_glb", "albedo", "normal", "orm"}, set()),
            "parts": ({"parts_glb", "part_map"}, set()),
            "rig": ({"rigged_glb", "skeleton"}, set()),
            "render": ({"frame_set", "render_receipt"}, set()),
            "cad_code": ({"cad_brep", "cad_mesh"}, set()),
            "collision": ({"collision_glb", "collision_manifest"}, set()),
            "physics": ({"physics_trace", "physics_receipt"}, set()),
            "procedural": ({"scene_blend", "scene_glb"}, set()),
            "scene_compose": ({"scene_blend", "scene_usd"}, set()),
            "denoise": ({"denoised_frame_set", "denoise_receipt"}, set()),
            "texture": ({"textured_glb", "albedo", "normal", "orm"}, set())}
# Seal dispatch metadata, including nested role sets, after module initialization.
_INPUTS = MappingProxyType({slot: tuple(frozenset(roles) for roles in alternatives)
                           for slot, alternatives in _INPUTS.items()})
_OUTPUTS = MappingProxyType({slot: tuple(frozenset(roles) for roles in pair)
                            for slot, pair in _OUTPUTS.items()})


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


def validate_slot(payload: dict, slot: str, kind: str) -> None:
    # Keep the legacy refusal guard intact, including its mutation-test anchor.
    if type(slot) is not str or slot not in NEW_SLOTS:
        if type(slot) is not str or slot not in core.SLOTS:
            raise core.ContractError("unknown_slot", "$/slot")
    if type(kind) is not str or kind not in ("request", "result"):
        raise core.ContractError("unknown_kind", "$/kind")
    # Run this before minItems so empty exclusive unions retain their named error.
    if kind == "request" and type(payload) is dict and "inputs" in payload:
        roles = _roles(payload["inputs"])
        if slot in ("generate", "parts", "motion"):
            if len(roles) != len(set(roles)) or set(roles) not in _INPUTS[slot]:
                raise core.ContractError("input_exclusivity", "$/inputs")
    schema = core.load_json(_SCHEMAS / (slot + ".schema.json"), max_bytes=_MAX_JSON_BYTES)
    core.validate(payload, schema)
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
        if slot == "motion":
            complete = set(roles) in ({"action_bvh"}, {"action_glb"})
        else:
            required, optional = _OUTPUTS[slot]
            complete = required <= set(roles) <= required | optional
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
    schema = core.load_json(_SCHEMAS / "provider.schema.json", max_bytes=_MAX_JSON_BYTES)
    core.validate(manifest, schema)
    _text(manifest["id"], "$/id")
    if manifest["revision"] is not None or manifest["runnable"]:
        _revision(manifest["revision"], "$/revision")
    if not manifest["runnable"]:
        _text(manifest["blocked_reason"], "$/blocked_reason")
    for key in ("code", "weights"):
        _text(manifest["licence"][key], "$/licence/" + key)
    for index, dep in enumerate(manifest["licence"]["deps"]):
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
