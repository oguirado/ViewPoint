"""Authenticated seed ranking, supervised by gates.rank.rank for hard timeout.

    Required bindings use relative paths under evidence_root. Checkpoints use
    a separate explicit read-only root. All measurements bind original bytes;
    reference pixels or missing DINO observations never become zero scores.
"""
import hashlib
import io
import os
from pathlib import Path
import stat
import time

from asset_factory import core
from tools.mesh import contracts as mesh_contracts
from gates.rank.dino import LocalDino, PREPROCESS, FAMILIES, validate_model_document
from gates.rank.metrics import RankError, require, silhouette_iou_ppm, cosine_ppm, order

MAX_JSON = 2 * 1024 * 1024
MAX_IMAGE = 16 * 1024 * 1024
MAX_ARTIFACT = 256 * 1024 * 1024
MAX_CANDIDATES = 32


def _hash(value):
    require(type(value) is str and len(value) == 64 and all(c in "0123456789abcdef" for c in value),
            2, "digest_invalid")


def _read(root, relative, limit):
    """Descriptor-relative no-follow reads, including the root and parents."""
    require(type(relative) is str and bool(relative), 2, "path_invalid")
    path = Path(relative)
    require(not path.is_absolute() and all(p not in ("..", ".") for p in path.parts), 2, "path_escape")
    require(path.name not in ("auth.json", ".env") and path.suffix not in (".key", ".token"), 2, "path_forbidden")
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in path.parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        leaf = os.open(path.parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        with os.fdopen(leaf, "rb") as stream:
            require(stat.S_ISREG(os.fstat(stream.fileno()).st_mode), 2, "not_regular_file")
            require(os.fstat(stream.fileno()).st_size <= limit, 6, "evidence_budget_exceeded")
            raw = stream.read(limit + 1)
            require(len(raw) <= limit, 6, "evidence_budget_exceeded")
            return raw
    finally:
        os.close(fd)


class Evidence:
    def __init__(self, root):
        require(Path(root).is_absolute(), 2, "root_not_absolute")
        self.root, self.witnesses = root, []

    def read(self, binding, limit):
        _hash(binding["sha256"])
        try:
            raw = _read(self.root, binding["path"], limit)
        except FileNotFoundError as exc:
            raise RankError(3, "evidence_missing") from exc
        except OSError as exc:
            raise RankError(2, "evidence_io_or_symlink") from exc
        require(hashlib.sha256(raw).hexdigest() == binding["sha256"], 4, "evidence_stale")
        self.witnesses.append((dict(binding), limit))
        return raw

    def recheck(self):
        for binding, limit in list(self.witnesses):
            self.read(binding, limit)


def _json(raw):
    # Reuse the duplicate-key and integer-only F0 parser without a disk copy.
    import json
    try:
        return json.loads(raw, object_pairs_hook=core._unique_object,
                          parse_float=core._reject_number, parse_constant=core._reject_number)
    except (ValueError, UnicodeError) as exc:
        raise RankError(2, "evidence_json_invalid") from exc


def validate_request(request):
    schema = core.load_json(Path(__file__).with_name("request.schema.json"), max_bytes=MAX_JSON)
    core.validate(request, schema)
    p = request["policy"]
    require(all(type(v) is int for v in p.values()), 2, "policy_integer_required")
    require(1 <= p["timeout_s"] <= 300 and
            1 <= p["iou_weight"] <= 1000 and 1 <= p["dino_weight"] <= 1000 and
            0 <= p["min_iou_ppm"] <= 1_000_000 and 0 <= p["min_dino_ppm"] <= 1_000_000,
            2, "policy_invalid")
    capture = request["capture"]
    require(1 <= capture["width"] <= 2048 and 1 <= capture["height"] <= 2048, 2, "image_dimensions_invalid")
    _hash(capture["camera_sha256"])
    model = request["model"]
    for k in ("config_sha256", "weights_sha256"):
        _hash(model[k])
    require(model["revision"].lower() not in ("main", "master", "latest", "head", ""), 2, "unpinned_revision")
    require(Path(request["evidence_root"]).is_absolute() and Path(model["root"]).is_absolute(), 2, "root_not_absolute")
    candidates = request["candidates"]
    require(1 <= len(candidates) <= MAX_CANDIDATES, 6, "candidate_budget_exceeded")
    require(len({c["id"] for c in candidates}) == len(candidates) and all(c["id"] for c in candidates), 2, "duplicate_candidate")
    require(len({c["render"]["sha256"] for c in candidates}) == len(candidates), 5, "replayed_render")
    view_pin = core.sha256_json(capture)
    for c in candidates:
        require(c["reference_sha256"] == request["reference"]["image"]["sha256"], 4, "reference_mismatch")
        require(c["capture_sha256"] == view_pin, 4, "capture_mismatch")
        require(c["seed"] is None or type(c["seed"]) is int and 0 <= c["seed"] < 2**63, 2, "seed_invalid")
        expectation = c.get("gate_expectation")
        if expectation is not None:
            for key in ("policy_sha256", "toolchain_sha256"):
                _hash(expectation[key])
    if request["mode"] == "seeds":
        require(len(candidates) >= 2, 3, "best_of_n_requires_multiple_seeds")
        require(all(c["seed"] is not None and all(c[k] is not None for k in ("artifact", "generation", "gate", "capture_receipt")) for c in candidates),
                3, "seed_evidence_missing")
        require(all(c.get("gate_expectation") is not None for c in candidates), 3, "gate_expectation_missing")
        require(len({c["seed"] for c in candidates}) == len(candidates), 2, "duplicate_seed")


def _image(raw, size, *, mask=False):
    try:
        from PIL import Image
    except ImportError as exc:
        raise RankError(3, "image_runtime_unavailable") from exc
    try:
        image = Image.open(io.BytesIO(raw))
        require(image.format == "PNG" and image.size == size, 2, "image_dimensions_or_format")
        require(image.mode == "L" if mask else image.mode in ("RGB", "RGBA"), 2, "image_mode_invalid")
        image.load()
        if mask:
            return image.tobytes()
        if image.mode == "RGBA":
            background = Image.new("RGBA", size, "white")
            return Image.alpha_composite(background, image).convert("RGB")
        return image
    except (OSError, ValueError) as exc:
        raise RankError(2, "image_invalid") from exc


def _gate(raw, artifact_sha256, expectation=None):
    receipt = _json(raw)
    require(type(receipt) is dict and (receipt.get("candidate_sha256") or receipt.get("input_sha256")) == artifact_sha256,
            4, "gate_subject_stale")
    rc = receipt.get("rc")
    require(type(rc) is int and rc in core.RC, 2, "gate_rc_invalid")
    try:
        mesh_contracts.validate_receipt(receipt)
    except mesh_contracts.MeshError as exc:
        raise RankError(2, "gate_contract_" + exc.code) from exc
    require(type(expectation) is dict, 3, "gate_expectation_missing")
    require(set(expectation) == {"operation", "policy_sha256", "toolchain_sha256"} and
            expectation["operation"] in ("metrics", "compare", "repair"), 2, "gate_expectation_invalid")
    for key in ("policy_sha256", "toolchain_sha256"):
        _hash(expectation[key])
        require(receipt[key] == expectation[key], 4, "gate_" + key + "_stale")
    operation = expectation["operation"]
    require((receipt["candidate_sha256"] is None) == (operation == "metrics"),
            2, "gate_operation_mismatch")
    if rc == 0:
        inventories = receipt.get("metrics", {}).get("component_inventories")
        require(type(inventories) is list and bool(inventories), 3, "gate_has_no_measurements")
        require(receipt.get("failed") == [] and receipt.get("artefact_sha256") == artifact_sha256,
                4, "gate_pass_unbound")
        require(all(type(item) is dict and type(item.get("tris")) is int and item["tris"] > 0
                    and type(item.get("boundary_edges")) is int and item["boundary_edges"] == 0
                    and item.get("failed") == [] for item in inventories), 5, "mesh_gate_not_closed")
        try:
            mesh_contracts.validate_success(receipt, operation)
        except mesh_contracts.MeshError as exc:
            raise RankError(2, "gate_contract_" + exc.code) from exc
    return rc


def evaluate(request, *, _loader=LocalDino):
    """Run inside an owned bounded subprocess. Private injection is for tests."""
    start = time.monotonic_ns()
    record = {"schema": "af.seed-ranking/v1", "scope": "ordinal-ranking-only",
              "request_sha256": None, "rc": 3, "verdict": "CANNOT_SEE",
              "asset_verdict": "CANNOT_SEE", "preferred_id": None, "winner_id": None,
              "reason_codes": [], "rankings": [], "model": None,
              "policy_status": "DESIGN_CHOICE_uncalibrated", "preprocess": PREPROCESS,
              "wall_ms": None, "peak_rss_kib": None,
              "source_files": {name: core.sha256_file(Path(__file__).with_name(name))
                               for name in ("request.schema.json", "result.schema.json", "metrics.py", "dino.py", "evaluate.py", "worker.py", "rank.py", "capture.py", "finalize.py")}}
    record["source_files"]["asset_factory/core.py"] = core.sha256_file(Path(core.__file__))
    record["source_files"]["tools/mesh/contracts.py"] = core.sha256_file(Path(mesh_contracts.__file__))
    rows = []
    try:
        record["request_sha256"] = core.sha256_json(request)
        validate_request(request)
        record["reference_sha256"] = request["reference"]["image"]["sha256"]
        record["capture_sha256"] = core.sha256_json(request["capture"])
        evidence = Evidence(request["evidence_root"])
        size = (request["capture"]["width"], request["capture"]["height"])
        reference = _image(evidence.read(request["reference"]["image"], MAX_IMAGE), size)
        reference_mask = _image(evidence.read(request["reference"]["mask"], MAX_IMAGE), size, mask=True)
        images = []
        for c in request["candidates"]:
            image = _image(evidence.read(c["render"], MAX_IMAGE), size)
            mask = _image(evidence.read(c["mask"], MAX_IMAGE), size, mask=True)
            iou = silhouette_iou_ppm(reference_mask, mask)
            gate_rc = 3
            if c["artifact"] is not None:
                evidence.read(c["artifact"], MAX_ARTIFACT)
            if c["generation"] is not None:
                generation = _json(evidence.read(c["generation"], MAX_JSON))
                require(c["artifact"] is not None and generation.get("seed") == c["seed"] and
                        type(generation.get("seed")) is int and type(generation.get("rc")) is int and generation.get("rc") == 0 and
                        generation.get("input_sha256") == c["reference_sha256"] and
                        generation.get("output_sha256") == c["artifact"]["sha256"], 4, "generation_stale")
            if c["gate"] is not None:
                require(c["artifact"] is not None, 3, "gate_artifact_missing")
                gate_rc = _gate(evidence.read(c["gate"], MAX_JSON), c["artifact"]["sha256"],
                                c.get("gate_expectation"))
            if c["capture_receipt"] is not None:
                capture = _json(evidence.read(c["capture_receipt"], MAX_JSON))
                require(c["artifact"] is not None and capture.get("schema") == "af.rank-capture/v1" and
                        capture.get("input_sha256") == c["artifact"]["sha256"] and
                        capture.get("capture_sha256") == c["capture_sha256"] and
                        capture.get("render_sha256") == c["render"]["sha256"] and
                        capture.get("mask_sha256") == c["mask"]["sha256"] and
                        capture.get("renderer_sha256") == record["source_files"]["capture.py"] and
                        capture.get("mask_policy") == "alpha_ge_128" and
                        type(capture.get("rc")) is int and capture["rc"] == 0,
                        4, "capture_receipt_stale")
            rows.append({**c, "iou_ppm": iou, "dino_ppm": None,
                         "gate_rc": gate_rc, "absence_reasons": {"dino_ppm": "not_measured"}})
            images.append(image)
        model = request["model"]
        record["preprocess"] = FAMILIES[model["id"]][2]
        weights = Evidence(model["root"])
        for path, key, limit in (("config.json", "config_sha256", MAX_JSON),
                                 ("model.safetensors", "weights_sha256", 2 * 1024**3)):
            # Stream weights; avoid an additional checkpoint-sized Python bytes.
            binding = {"path": path, "sha256": model[key]}
            if path == "config.json":
                import json
                document = json.loads(weights.read(binding, limit), object_pairs_hook=core._unique_object)
                validate_model_document(document, model["id"])
            else:
                _stream_weight(model["root"], binding, limit)
        record["model"] = {**model, "device": "cpu", "preprocess": record["preprocess"], "inference_executed": False}
        runner = _loader(model["root"])
        ref_vector = runner.embed(reference)
        for row, image in zip(rows, images):
            row["dino_ppm"] = cosine_ppm(ref_vector, runner.embed(image))
            row["absence_reasons"] = {}
        evidence.recheck()
        weights.recheck()
        _stream_weight(model["root"], {"path": "model.safetensors", "sha256": model["weights_sha256"]}, 2 * 1024**3)
        record["model"].update(runner.identity)
        record["model"]["inference_executed"] = True
        record["rankings"] = order(rows, request["policy"])
        record["preferred_id"] = record["rankings"][0]["id"]
        if request["mode"] == "diagnostic":
            raise RankError(3, "diagnostic_not_asset_admission")
        if not record["rankings"][0]["eligible"]:
            rc = core.worst_rc([r["gate_rc"] for r in rows])
            raise RankError(rc if rc else 5, "no_candidate_clears_gates_and_floors")
        record.update(rc=0, verdict="PASS", winner_id=record["preferred_id"])
    except RankError as exc:
        record.update(rc=exc.rc, verdict=core.VERDICT[exc.rc], reason_codes=[exc.reason], winner_id=None)
    except core.ContractError as exc:
        record.update(rc=2, verdict="ERROR", reason_codes=[exc.code], winner_id=None)
    except (OSError, ValueError, TypeError, KeyError, OverflowError) as exc:
        record.update(rc=2, verdict="ERROR", reason_codes=["ranking_input_invalid"], winner_id=None)
    if not record["rankings"] and rows:
        # Preserve measured IoU without pretending an unobserved embedding is 0.
        record["rankings"] = order(rows, request["policy"])
        for row in record["rankings"]:
            if row["dino_ppm"] is None:
                row["absence_reasons"]["dino_ppm"] = record["reason_codes"][0]
    import resource
    record.update(wall_ms=(time.monotonic_ns() - start) // 1_000_000,
                  peak_rss_kib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    return record


def _stream_weight(root, binding, limit):
    directory = None
    try:
        directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        fd = os.open(binding["path"], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        with os.fdopen(fd, "rb") as stream:
            require(stat.S_ISREG(os.fstat(stream.fileno()).st_mode), 2, "not_regular_file")
            require(os.fstat(stream.fileno()).st_size <= limit, 6, "weight_budget_exceeded")
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
    except FileNotFoundError as exc:
        raise RankError(3, "dino_weights_missing") from exc
    except OSError as exc:
        raise RankError(2, "weight_io_or_symlink") from exc
    finally:
        if directory is not None:
            os.close(directory)
    require(digest == binding["sha256"], 4, "dino_weights_stale")
