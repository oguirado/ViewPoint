"""Reference-mask gate. CLI: python3 -B -m gates.input_gate.

No preparation or model execution occurs here. The optional Pillow decoder is
admitted before decompression. Thresholds have no defaults: calibration remains
the policy owner's responsibility. This gate proves mask quality, not semantics.
Publication reserves a new directory exclusively; receipt.json commits it last.
"""
from __future__ import annotations
from asset_factory import proc as af_proc

import argparse
import base64
import hashlib
import importlib.metadata
import io
import json
import os
from pathlib import Path
import signal
import shutil
import subprocess
import sys
import tempfile
import time
import warnings

# Isolated script execution admits only this component's known package root.
if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from asset_factory import core

# Inline closed contracts, owned by this gate (INTERFACES_F0 C1 and section 4).
POLICY_KEYS = (
    "schema", "min_side_px", "max_encoded_bytes", "max_decoded_pixels",
    "alpha_background_max", "alpha_foreground_min", "min_foreground_ppm",
    "max_foreground_ppm", "max_border_foreground_ppm", "max_small_component_ppm",
    "max_soft_alpha_ppm", "small_component_max_pixels", "max_components",
    "connectivity", "ppm_rounding", "calibration_ref",
)
RESULT_KEYS = ("schema", "rc", "status", "verdict", "source_sha256", "policy_sha256",
               "toolchain_sha256", "metrics", "reason_codes", "preparation")
RECEIPT_KEYS = ("schema", "source", "result", "policy", "calibration", "toolchain_sha256")
METRIC_KEYS = ("width", "height", "pixels", "foreground_pixels", "foreground_ppm",
               "border_foreground_ppm", "small_component_ppm", "soft_alpha_ppm",
               "components", "background_pixels", "bbox", "format", "mode", "bit_depth")
MAX_JSON_BYTES = 1024 * 1024  # Resource budget, not a quality threshold.
PPM = 1000000


class GateIssue(Exception):
    def __init__(self, rc, code):
        super().__init__(code)
        self.rc, self.code = rc, code


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _digest(value):
    return isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def toolchain_identity():
    try:
        decoder_version = importlib.metadata.version("Pillow")
    except importlib.metadata.PackageNotFoundError:
        decoder_version = None
    return core.sha256_json({"gate_sha256": _sha(Path(__file__).read_bytes()),
                             "decoder": "Pillow", "decoder_version": decoder_version,
                             "kernel_sha256": _sha(Path(core.__file__).read_bytes())})


def _closed(value, keys, code):
    if not isinstance(value, dict) or set(value) != set(keys):
        raise GateIssue(2, code)


def _json_bytes(data):
    """Strict bounded integer JSON, also used at the worker boundary."""
    def pairs(items):
        value = {}
        for key, item in items:
            if key in value:
                raise ValueError("duplicate_key")
            value[key] = item
        return value
    def reject(value):
        raise ValueError("non_integer_number")
    if len(data) > MAX_JSON_BYTES:
        raise GateIssue(6, "json_byte_budget")
    return json.loads(data, object_pairs_hook=pairs, parse_float=reject, parse_constant=reject)


def _calibration(policy, data):
    if data is None:
        raise GateIssue(3, "calibration_missing")
    if not isinstance(data, bytes):
        raise GateIssue(2, "calibration_invalid")
    if len(data) > MAX_JSON_BYTES:
        raise GateIssue(6, "calibration_byte_budget")
    if _sha(data) != policy["calibration_ref"]["sha256"]:
        raise GateIssue(4, "calibration_stale")
    try:
        record = _json_bytes(data)
    except (ValueError, TypeError):
        raise GateIssue(2, "calibration_invalid")
    _closed(record, ("schema", "policy_sha256", "rc", "verdict"), "calibration_invalid")
    if (record["schema"] != "af.input-calibration/v1" or type(record["rc"]) is not int
            or record["rc"] not in core.RC or not _digest(record["policy_sha256"])
            or record["verdict"] != core.VERDICT[record["rc"]]):
        raise GateIssue(2, "calibration_invalid")
    # Excluding the reference avoids a circular hash, while binding every criterion.
    if record["policy_sha256"] != core.sha256_json({k: v for k, v in policy.items() if k != "calibration_ref"}):
        raise GateIssue(4, "calibration_policy_stale")
    if record["rc"] != 0:
        raise GateIssue(3, "policy_uncalibrated")


def _policy(policy):
    _closed(policy, POLICY_KEYS, "policy_invalid")
    if policy["schema"] != "af.input-policy/v1":
        raise GateIssue(2, "policy_invalid")
    numeric = set(POLICY_KEYS) - {"schema", "ppm_rounding", "calibration_ref"}
    if any(type(policy[k]) is not int or policy[k] < 0 for k in numeric):
        raise GateIssue(2, "policy_invalid")
    if (any(policy[k] < 1 for k in ("min_side_px", "max_encoded_bytes", "max_decoded_pixels", "max_components"))
            or not 0 <= policy["alpha_background_max"] < policy["alpha_foreground_min"] <= 255
            or policy["connectivity"] not in (4, 8) or policy["ppm_rounding"] != "floor"
            or policy["min_foreground_ppm"] > policy["max_foreground_ppm"]
            or any(policy[k] > PPM for k in numeric if k.endswith("_ppm"))):
        raise GateIssue(2, "policy_invalid")
    if policy["calibration_ref"] is None:
        raise GateIssue(3, "policy_uncalibrated")
    ref = policy["calibration_ref"]
    _closed(ref, ("path", "sha256"), "calibration_invalid")
    if not isinstance(ref["path"], str) or not ref["path"] or not _digest(ref["sha256"]):
        raise GateIssue(2, "calibration_invalid")


class PillowDecoder:
    def decode(self, data, *, max_pixels):
        try:
            from PIL import Image
        except ImportError as exc:
            raise GateIssue(3, "decoder_unavailable") from exc
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(io.BytesIO(data), formats=("PNG",)) as image:
                    width, height = image.size
                    if width < 1 or height < 1:
                        raise GateIssue(2, "image_invalid")
                    if width * height > max_pixels:
                        raise GateIssue(6, "decoded_pixel_budget")
                    if getattr(image, "n_frames", 1) != 1:
                        raise GateIssue(2, "animated_image_unsupported")
                    image.verify()
                with Image.open(io.BytesIO(data), formats=("PNG",)) as image:
                    image.load()
                    alpha = image.convert("RGBA").getchannel("A").tobytes()
                    # PNG IHDR stores source depth at this fixed byte offset.
                    # Pillow may otherwise silently downconvert 16-bit RGBA.
                    return width, height, alpha, image.format, image.mode, data[24]
        except GateIssue:
            raise
        except MemoryError:
            raise
        except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
            raise GateIssue(6, "decoded_pixel_budget") from exc
        except Exception as exc:
            raise GateIssue(2, "image_invalid") from exc


def _metrics(width, height, alpha, policy):
    count = width * height
    if type(width) is not int or type(height) is not int or width < 1 or height < 1 or len(alpha) != count:
        raise GateIssue(2, "decoder_contract")
    foreground = bytearray(a >= policy["alpha_foreground_min"] for a in alpha)
    foreground_count = sum(foreground)
    soft = sum(policy["alpha_background_max"] < a < policy["alpha_foreground_min"] for a in alpha)
    border = sum(foreground[i] for i in range(count)
                 if i < width or i >= count - width or i % width in (0, width - 1))
    border_count = count if width == 1 or height == 1 else 2 * width + 2 * height - 4
    components, small = 0, 0
    bbox = None
    offsets = ((-1, 0), (1, 0), (0, -1), (0, 1))
    if policy["connectivity"] == 8:
        offsets += ((-1, -1), (1, -1), (-1, 1), (1, 1))
    for index in range(count):
        if not foreground[index]:
            continue
        components += 1
        foreground[index] = 0
        stack, size = [index], 0
        while stack:
            current = stack.pop()
            x, y = current % width, current // width
            bbox = [x, y, x, y] if bbox is None else [min(bbox[0], x), min(bbox[1], y),
                                                     max(bbox[2], x), max(bbox[3], y)]
            size += 1
            for dx, dy in offsets:
                nx, ny = x + dx, y + dy
                if 0 <= nx < width and 0 <= ny < height:
                    neighbor = ny * width + nx
                    if foreground[neighbor]:
                        foreground[neighbor] = 0
                        stack.append(neighbor)
        if size <= policy["small_component_max_pixels"]:
            small += size
    return {"width": width, "height": height, "pixels": count,
            "background_pixels": sum(a <= policy["alpha_background_max"] for a in alpha),
            "foreground_pixels": foreground_count, "foreground_ppm": foreground_count * PPM // count,
            "border_foreground_ppm": border * PPM // border_count,
            "small_component_ppm": None if not foreground_count else small * PPM // foreground_count,
            "soft_alpha_ppm": soft * PPM // count, "components": components, "bbox": bbox}


def _quality_findings(metrics, policy):
    checks = (
        ("opaque_mask", metrics["background_pixels"] > 0),
        ("resolution", min(metrics["width"], metrics["height"]) >= policy["min_side_px"]),
        ("foreground_occupancy", policy["min_foreground_ppm"] <= metrics["foreground_ppm"] <= policy["max_foreground_ppm"] < PPM),
        ("border_leakage", metrics["border_foreground_ppm"] <= policy["max_border_foreground_ppm"] < PPM),
        ("small_components", metrics["small_component_ppm"] is not None and metrics["small_component_ppm"] <= policy["max_small_component_ppm"]),
        ("soft_alpha", metrics["soft_alpha_ppm"] <= policy["max_soft_alpha_ppm"]),
        ("component_count", 0 < metrics["components"] <= policy["max_components"]),
    )
    return [name for name, passed in checks if not passed]


def _validate_result(result):
    _closed(result, RESULT_KEYS, "result_invalid")
    rc = result["rc"]
    if (result["schema"] != "af.input-gate/v1" or type(rc) is not int or rc not in core.RC
            or result["status"] != core.STATUS[rc] or result["verdict"] != core.VERDICT[rc]
            or not isinstance(result["reason_codes"], list)
            or any(not isinstance(v, str) for v in result["reason_codes"])):
        raise GateIssue(2, "result_invalid")
    if rc == 0:
        _closed(result["metrics"], METRIC_KEYS, "result_invalid")
        if (any(type(result["metrics"][k]) is not int for k in METRIC_KEYS if k not in ("bbox", "format", "mode"))
                or result["metrics"]["format"] != "PNG" or result["metrics"]["mode"] != "RGBA"
                or result["metrics"]["bit_depth"] != 8
                or result["reason_codes"] or any(not _digest(result[k]) for k in
                    ("source_sha256", "policy_sha256", "toolchain_sha256"))):
            raise GateIssue(2, "result_invalid")


def _record(rc, source, policy_hash, toolchain_hash, metrics=None, reason_codes=(), preparation=None):
    return {"schema": "af.input-gate/v1", "rc": rc, "status": core.STATUS[rc], "verdict": core.VERDICT[rc],
            "source_sha256": source, "policy_sha256": policy_hash, "toolchain_sha256": toolchain_hash,
            "metrics": metrics, "reason_codes": list(reason_codes),
            "preparation": {"kind": "unverified_provenance"} if preparation is None else preparation}


def inspect_reference(image_bytes: bytes, *, policy: dict, policy_sha256: str,
                      toolchain_sha256: str, calibration_bytes: bytes | None = None) -> dict:
    """Inspect supplied alpha without rewriting bytes. Null metrics remain unknown."""
    source = _sha(image_bytes) if isinstance(image_bytes, bytes) else None
    try:
        if source is None:
            raise GateIssue(2, "image_invalid")
        _policy(policy)
        if not _digest(policy_sha256) or not _digest(toolchain_sha256):
            raise GateIssue(2, "identity_invalid")
        if core.sha256_json(policy) != policy_sha256:
            raise GateIssue(4, "policy_stale")
        if toolchain_identity() != toolchain_sha256:
            raise GateIssue(4, "toolchain_stale")
        _calibration(policy, calibration_bytes)
        if len(image_bytes) > policy["max_encoded_bytes"]:
            raise GateIssue(6, "encoded_byte_budget")
        decoded = PillowDecoder().decode(image_bytes, max_pixels=policy["max_decoded_pixels"])
        if not isinstance(decoded, tuple) or len(decoded) != 6:
            raise GateIssue(2, "decoder_contract")
        width, height, alpha, image_format, mode, bit_depth = decoded
        if width * height > policy["max_decoded_pixels"]:
            raise GateIssue(6, "decoded_pixel_budget")
        metrics = _metrics(width, height, alpha, policy)
        metrics.update(format=image_format, mode=mode, bit_depth=bit_depth)
        # Opaque RGB is still a measured failure. Transparent alternative encodings
        # cannot authorize an adapter whose preprocessing equivalence is unproven.
        if bit_depth != 8:
            raise GateIssue(3, "encoding_unproven")
        if image_format != "PNG" or mode != "RGBA":
            if metrics["background_pixels"] > 0:
                raise GateIssue(3, "encoding_unproven")
        findings = _quality_findings(metrics, policy)
        return _record(5 if findings else 0, source, policy_sha256, toolchain_sha256, metrics, findings)
    except GateIssue as exc:
        return _record(exc.rc, source, policy_sha256, toolchain_sha256, reason_codes=[exc.code])
    except MemoryError:
        return _record(6, source, policy_sha256, toolchain_sha256, reason_codes=["memory_budget"])
    except (core.ContractError, TypeError, ValueError, OSError):
        return _record(2, source, policy_sha256, toolchain_sha256, reason_codes=["input_invalid"])


def inspect_prepared_reference(original_bytes, candidate_bytes, *, preparation_receipt,
                               decision, policy, policy_sha256, toolchain_sha256,
                               calibration_bytes=None, provider=None,
                               configuration_sha256=None, licence_policy_sha256=None,
                               jurisdiction=None, commercial_use=None, code_sha256=None):
    """Bind preparation to caller's current manifest and independently pinned code.

    weights_sha256 identifies the canonical provider weights manifest. Evidence
    binding does not establish that the model executed; no backend is called.
    """
    source = _sha(candidate_bytes) if isinstance(candidate_bytes, bytes) else None
    try:
        if source is None or not isinstance(original_bytes, bytes):
            raise GateIssue(2, "preparation_invalid")
        if preparation_receipt is None or decision is None:
            raise GateIssue(3, "preparation_unproven")
        receipt = preparation_receipt
        _closed(receipt, ("schema", "rc", "status", "provider_id", "revision", "code_sha256",
                          "weights_sha256", "original_sha256", "candidate_sha256", "decision_sha256"), "preparation_invalid")
        if (receipt["schema"] != "af.reference-preparation/v1" or type(receipt["rc"]) is not int
                or receipt["rc"] not in core.RC
                or receipt["provider_id"] != "birefnet"):
            raise GateIssue(2, "preparation_invalid")
        _closed(decision, ("schema", "rc", "verdict", "allowed", "provider_id", "operation",
                           "jurisdiction", "commercial_use", "monetisable", "reason_codes",
                           "evaluated_dependencies", "configuration_sha256", "provider_sha256",
                           "policy_sha256"), "preparation_invalid")
        if receipt["rc"] != 0 or receipt["status"] != "OK":
            raise GateIssue(3, "preparation_unproven")
        revision = receipt["revision"]
        if not isinstance(revision, str) or not revision or revision.lower() in ("main", "master", "latest", "head"):
            raise GateIssue(2, "preparation_unpinned")
        if any(not _digest(receipt[k]) for k in ("code_sha256", "weights_sha256", "original_sha256", "candidate_sha256", "decision_sha256")):
            raise GateIssue(2, "preparation_invalid")
        if (receipt["original_sha256"] != _sha(original_bytes) or receipt["candidate_sha256"] != source
                or receipt["decision_sha256"] != core.sha256_json(decision)):
            raise GateIssue(4, "preparation_stale")
        if (decision.get("schema") != "af.licence-decision/v1" or type(decision.get("rc")) is not int
                or decision["rc"] != 0 or decision.get("allowed") is not True
                or decision.get("provider_id") != "birefnet" or decision["verdict"] != "PASS"
                or decision["operation"] != "generate"):
            raise GateIssue(5, "preparation_licence_refused")
        if provider is None:
            raise GateIssue(3, "preparation_provider_missing")
        if any(value is None for value in (configuration_sha256, licence_policy_sha256,
                                          jurisdiction, commercial_use, code_sha256)):
            raise GateIssue(3, "preparation_current_identity_missing")
        _closed(provider, ("schema", "id", "slot", "kind", "runnable", "revision", "licence",
                           "territory_excluded", "commercial", "gated", "weights", "runtime",
                           "known_issues"), "preparation_provider_invalid")
        if (provider["schema"] != "af.provider/v1" or provider["id"] != "birefnet"
                or provider["slot"] != "generate" or provider["kind"] != "model"
                or provider["runnable"] is not True or type(provider["commercial"]) is not bool
                or type(provider["gated"]) is not bool or not isinstance(provider["licence"], dict)
                or not provider["weights"] or not isinstance(provider["weights"], list)):
            raise GateIssue(2, "preparation_provider_invalid")
        for weight in provider["weights"]:
            _closed(weight, ("repo", "revision", "files"), "preparation_provider_invalid")
            if (not isinstance(weight["repo"], str) or not weight["repo"]
                    or not isinstance(weight["revision"], str) or not weight["revision"]
                    or weight["revision"].lower() in ("main", "master", "latest", "head")
                    or not isinstance(weight["files"], list) or not weight["files"]):
                raise GateIssue(2, "preparation_provider_invalid")
            for item in weight["files"]:
                _closed(item, ("path", "sha256"), "preparation_provider_invalid")
                if not isinstance(item["path"], str) or not item["path"] or not _digest(item["sha256"]):
                    raise GateIssue(2, "preparation_provider_invalid")
        if (any(not _digest(v) for v in (configuration_sha256, licence_policy_sha256, code_sha256))
                or not isinstance(jurisdiction, str) or len(jurisdiction) != 2
                or type(commercial_use) is not bool
                or type(decision["commercial_use"]) is not bool
                or type(decision["monetisable"]) is not bool
                or not isinstance(decision["reason_codes"], list) or decision["reason_codes"]
                or not isinstance(decision["evaluated_dependencies"], list)):
            raise GateIssue(2, "preparation_invalid")
        if (decision["configuration_sha256"] != configuration_sha256
                or decision["provider_sha256"] != core.sha256_json(provider)
                or decision["policy_sha256"] != licence_policy_sha256
                or decision["jurisdiction"] != jurisdiction
                or decision["commercial_use"] != commercial_use):
            raise GateIssue(4, "preparation_decision_stale")
        if (receipt["revision"] != provider["revision"] or receipt["code_sha256"] != code_sha256
                or receipt["weights_sha256"] != core.sha256_json(provider["weights"])):
            raise GateIssue(4, "preparation_provider_stale")
        result = inspect_reference(candidate_bytes, policy=policy, policy_sha256=policy_sha256,
                                   toolchain_sha256=toolchain_sha256, calibration_bytes=calibration_bytes)
        result["preparation"] = {"kind": "birefnet", "original_sha256": _sha(original_bytes),
                                  "receipt_sha256": core.sha256_json(receipt),
                                  "execution": "unverified"}
        return result
    except GateIssue as exc:
        return _record(exc.rc, source, policy_sha256, toolchain_sha256,
                       reason_codes=[exc.code], preparation={"kind": "birefnet"})
    except (core.ContractError, TypeError, ValueError):
        return _record(2, source, policy_sha256, toolchain_sha256,
                       reason_codes=["preparation_invalid"], preparation={"kind": "birefnet"})


def _safe_file(root, name):
    path = root / name
    if path.is_symlink() or path.resolve().parent != root.resolve():
        raise GateIssue(2, "evidence_path_invalid")
    if not path.exists():
        raise GateIssue(3, "evidence_missing")
    if not path.is_file():
        raise GateIssue(2, "evidence_path_invalid")
    return path


def verify_receipt(output_dir: Path, *, expected_source_sha256: str,
                   expected_policy_sha256: str, expected_toolchain_sha256: str,
                   timeout_seconds: int = 10) -> dict:
    """Rehash and independently remeasure; a stored PASS alone grants nothing."""
    try:
        if type(timeout_seconds) is not int or timeout_seconds <= 0:
            raise GateIssue(2, "timeout_invalid")
        deadline = time.monotonic() + timeout_seconds
        root = Path(output_dir)
        if root.is_symlink():
            raise GateIssue(2, "evidence_path_invalid")
        if not (root / "receipt.json").exists():
            raise GateIssue(3, "receipt_missing")
        if {p.name for p in root.iterdir()} != {"receipt.json", "reference.image", "input_gate.json",
                                               "policy.json", "calibration.evidence"}:
            raise GateIssue(2, "evidence_files_invalid")
        receipt = core.load_json(_safe_file(root, "receipt.json"), max_bytes=MAX_JSON_BYTES)
        _closed(receipt, RECEIPT_KEYS, "receipt_invalid")
        if receipt["schema"] != "af.input-gate-receipt/v1":
            raise GateIssue(2, "receipt_invalid")
        expected = (expected_source_sha256, expected_policy_sha256, expected_toolchain_sha256)
        if any(not _digest(v) for v in expected):
            raise GateIssue(2, "identity_invalid")
        blobs = {}
        for key, name in (("source", "reference.image"), ("result", "input_gate.json"), ("policy", "policy.json")):
            ref = receipt[key]
            _closed(ref, ("path", "sha256"), "receipt_invalid")
            if ref["path"] != name or not _digest(ref["sha256"]):
                raise GateIssue(2, "receipt_invalid")
            path = _safe_file(root, name)
            if key != "source":
                blobs[key] = core.load_json(path, max_bytes=MAX_JSON_BYTES)
            else:
                policy_path = _safe_file(root, "policy.json")
                policy = core.load_json(policy_path, max_bytes=MAX_JSON_BYTES)
                _policy(policy)
                with path.open("rb") as stream:
                    blobs[key] = stream.read(policy["max_encoded_bytes"] + 1)
                if len(blobs[key]) > policy["max_encoded_bytes"]:
                    raise GateIssue(6, "encoded_byte_budget")
            raw_hash = _sha(blobs[key]) if key == "source" else core.sha256_json(blobs[key])
            if ref["sha256"] != raw_hash:
                raise GateIssue(4, "evidence_stale")
        calibration_ref = receipt["calibration"]
        _closed(calibration_ref, ("path", "sha256"), "receipt_invalid")
        if calibration_ref["path"] != "calibration.evidence":
            raise GateIssue(2, "receipt_invalid")
        with _safe_file(root, "calibration.evidence").open("rb") as stream:
            calibration = stream.read(MAX_JSON_BYTES + 1)
        if len(calibration) > MAX_JSON_BYTES:
            raise GateIssue(6, "calibration_byte_budget")
        if not calibration_ref["sha256"] == _sha(calibration) == blobs["policy"]["calibration_ref"]["sha256"]:
            raise GateIssue(4, "calibration_stale")
        _calibration(blobs["policy"], calibration)
        if (receipt["source"]["sha256"], receipt["policy"]["sha256"], receipt["toolchain_sha256"]) != expected:
            raise GateIssue(4, "binding_stale")
        result = blobs["result"]
        _validate_result(result)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise GateIssue(6, "deadline_exceeded")
        fresh = _inspect_worker(blobs["source"], blobs["policy"], expected_policy_sha256,
                                expected_toolchain_sha256, remaining, calibration)
        if time.monotonic() >= deadline:
            raise GateIssue(6, "deadline_exceeded")
        if result != fresh:
            raise GateIssue(4, "result_stale")
        return fresh
    except GateIssue as exc:
        return _record(exc.rc, expected_source_sha256, expected_policy_sha256,
                       expected_toolchain_sha256, reason_codes=[exc.code])
    except MemoryError:
        return _record(6, expected_source_sha256, expected_policy_sha256,
                       expected_toolchain_sha256, reason_codes=["memory_budget"])
    except (core.ContractError, OSError, ValueError, TypeError):
        return _record(2, expected_source_sha256, expected_policy_sha256,
                       expected_toolchain_sha256, reason_codes=["evidence_invalid"])


def _run_owned_worker(payload, timeout_seconds):
    child = af_proc.popen([sys.executable, "-B", "-m", "gates.input_gate", "--worker"],
                             input=core.canonical_json(payload), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             text=True, start_new_session=True, cwd=Path(__file__).resolve().parents[1])
    try:
        stdout, stderr = af_proc.communicate(child, timeout=timeout_seconds)
        return subprocess.CompletedProcess([sys.executable, "-m", "gates.input_gate"], child.returncode, stdout, stderr)
    except BaseException:
        try:
            af_proc.signal_group(child, signal.SIGKILL)
        except ProcessLookupError:
            pass
        af_proc.communicate(child)
        raise


def _inspect_worker(data, policy, policy_hash, toolchain_hash, timeout_seconds, calibration=None):
    payload = {"image": base64.b64encode(data).decode("ascii"), "policy": policy,
               "policy_sha256": policy_hash, "toolchain_sha256": toolchain_hash,
               "calibration": None if calibration is None else base64.b64encode(calibration).decode("ascii")}
    try:
        child = _run_owned_worker(payload, timeout_seconds)
    except af_proc.SignalRefusal as exc:
        raise GateIssue(exc.rc, exc.code) from exc
    except subprocess.TimeoutExpired as exc:
        raise GateIssue(6, "deadline_exceeded") from exc
    if child.returncode < 0:
        raise GateIssue(6, "worker_budget_terminated")
    if child.returncode not in core.RC:
        raise GateIssue(2, "worker_contract")
    try:
        result = _json_bytes(child.stdout)
        _validate_result(result)
        if type(result["rc"]) is not int or result["rc"] != child.returncode:
            raise GateIssue(2, "worker_contract")
        return result
    except (ValueError, TypeError) as exc:
        raise GateIssue(2, "worker_contract") from exc


def _publish(output_dir, data, policy, result, calibration, deadline):
    stage = Path(tempfile.mkdtemp(prefix=".input-gate-", dir=output_dir.parent))
    reserved, moved, directory_fd = False, [], None
    try:
        with (stage / "reference.image").open("wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        with (stage / "calibration.evidence").open("wb") as stream:
            stream.write(calibration)
            stream.flush()
            os.fsync(stream.fileno())
        core.atomic_write_json(stage / "policy.json", policy)
        core.atomic_write_json(stage / "input_gate.json", result)
        receipt = {"schema": "af.input-gate-receipt/v1", "source": {"path": "reference.image", "sha256": _sha(data)},
                   "result": {"path": "input_gate.json", "sha256": core.sha256_json(result)},
                   "policy": {"path": "policy.json", "sha256": core.sha256_json(policy)},
                   "calibration": {"path": "calibration.evidence", "sha256": _sha(calibration)},
                   "toolchain_sha256": result["toolchain_sha256"]}
        core.atomic_write_json(stage / "receipt.json", receipt)
        if time.monotonic() >= deadline:
            raise GateIssue(6, "deadline_exceeded")
        # Exclusive mkdir closes the check/replace race even for empty directories.
        # The complete receipt is the final commit marker; verification refuses a
        # directory without it. No existing directory is ever renamed over.
        try:
            output_dir.mkdir(mode=0o700)
        except FileExistsError as exc:
            raise GateIssue(2, "output_exists") from exc
        reserved = True
        directory_fd = os.open(output_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        for name in ("reference.image", "calibration.evidence", "policy.json", "input_gate.json", "receipt.json"):
            if time.monotonic() >= deadline:
                raise GateIssue(6, "deadline_exceeded")
            os.replace(stage / name, name, dst_dir_fd=directory_fd)
            moved.append(name)
        os.fsync(directory_fd)
        parent_fd = os.open(output_dir.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
        reserved = False
    finally:
        if reserved and directory_fd is not None:
            for name in moved:
                os.unlink(name, dir_fd=directory_fd)
            try:
                if output_dir.stat().st_ino == os.fstat(directory_fd).st_ino:
                    output_dir.rmdir()
            except OSError:
                pass
        if directory_fd is not None:
            os.close(directory_fd)
        if stage.exists():
            shutil.rmtree(stage)


def run(input_path: Path, output_dir: Path, *, policy_path: Path, timeout_seconds: int) -> int:
    """Bound decode/inspection in an owned child; publish only a successful bundle."""
    source, policy_hash, toolchain_hash = None, None, None
    try:
        if type(timeout_seconds) is not int or timeout_seconds <= 0:
            raise GateIssue(2, "timeout_invalid")
        deadline = time.monotonic() + timeout_seconds
        if not Path(policy_path).exists() or not Path(input_path).exists():
            raise GateIssue(3, "input_missing")
        if not Path(policy_path).is_file() or not Path(input_path).is_file():
            raise GateIssue(2, "input_not_regular_file")
        policy = core.load_json(Path(policy_path), max_bytes=MAX_JSON_BYTES)
        _policy(policy)
        policy_hash, toolchain_hash = core.sha256_json(policy), toolchain_identity()
        ref = policy["calibration_ref"]
        calibration_path = (Path(policy_path).parent / ref["path"]).resolve()
        if not calibration_path.is_relative_to(Path(policy_path).parent.resolve()):
            raise GateIssue(2, "calibration_path_escape")
        if not calibration_path.is_file():
            raise GateIssue(3, "calibration_missing")
        with calibration_path.open("rb") as stream:
            calibration = stream.read(MAX_JSON_BYTES + 1)
        if len(calibration) > MAX_JSON_BYTES:
            raise GateIssue(6, "calibration_byte_budget")
        if _sha(calibration) != ref["sha256"]:
            raise GateIssue(4, "calibration_stale")
        _calibration(policy, calibration)
        with Path(input_path).open("rb") as stream:
            data = stream.read(policy["max_encoded_bytes"] + 1)
        if len(data) > policy["max_encoded_bytes"]:
            raise GateIssue(6, "encoded_byte_budget")
        source = _sha(data)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise GateIssue(6, "deadline_exceeded")
        result = _inspect_worker(data, policy, policy_hash, toolchain_hash, remaining, calibration)
        if result["rc"] == 0:
            # Check the child's complete record before crossing the publication boundary.
            if (result["source_sha256"], result["policy_sha256"], result["toolchain_sha256"]) != (source, policy_hash, toolchain_hash):
                raise GateIssue(4, "worker_binding_stale")
            if result["status"] != "OK" or result["verdict"] != "PASS" or result["reason_codes"]:
                raise GateIssue(2, "worker_contract")
            _publish(Path(output_dir), data, policy, result, calibration, deadline)
    except GateIssue as exc:
        result = _record(exc.rc, source, policy_hash, toolchain_hash, reason_codes=[exc.code])
    except MemoryError:
        result = _record(6, source, policy_hash, toolchain_hash, reason_codes=["memory_budget"])
    except (core.ContractError, OSError, ValueError, TypeError):
        result = _record(2, source, policy_hash, toolchain_hash, reason_codes=["input_or_publication_invalid"])
    return core.emit(result)


def main(argv=None):
    if argv is None and sys.argv[1:] == ["--worker"]:
        try:
            payload = _json_bytes(sys.stdin.read(MAX_JSON_BYTES + 1))
            _closed(payload, ("image", "policy", "policy_sha256", "toolchain_sha256", "calibration"), "worker_input_invalid")
            result = inspect_reference(base64.b64decode(payload["image"], validate=True), policy=payload["policy"],
                                       policy_sha256=payload["policy_sha256"], toolchain_sha256=payload["toolchain_sha256"],
                                       calibration_bytes=None if payload["calibration"] is None else base64.b64decode(payload["calibration"], validate=True))
            return core.emit(result)
        except GateIssue as exc:
            return core.emit(_record(exc.rc, None, None, None, reason_codes=[exc.code]))
        except (ValueError, KeyError, TypeError):
            return core.emit(_record(2, None, None, None, reason_codes=["worker_input_invalid"]))
    class Parser(argparse.ArgumentParser):
        def error(self, message):
            raise GateIssue(2, "usage_invalid")

    parser = Parser(description=__doc__, add_help=False)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--policy", required=True, type=Path)
    parser.add_argument("--timeout-seconds", required=True, type=int)
    try:
        args = parser.parse_args(argv)
    except GateIssue as exc:
        return core.emit(_record(exc.rc, None, None, None, reason_codes=[exc.code]))
    return run(args.input, args.output_dir, policy_path=args.policy, timeout_seconds=args.timeout_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
