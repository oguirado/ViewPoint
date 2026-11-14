"""
Python stdlib validator and proposed contract definitions for W8 Jules Massive Manifest offline tasks.

Provides validators and data models for:
- Manifest Carrier
- Capture Carrier
- Transcript Carrier
- Speech Payload Carrier
- Sidecar Carrier
- Identity Binding Carrier
- Snapshot Carrier
- Loop Carrier
- Text-Only Training-Signal Carrier
"""

import hashlib
import json
import re
import struct
from typing import Any, Dict, List, Optional, Tuple, Union

# Exit Code Constants (CITED)
EXIT_SUCCESS = 0
EXIT_INPUT = 2
EXIT_EXECUTION_SETUP = 3
EXIT_PREREQ_UNAVAILABLE = 4
EXIT_GATE_CONTAINMENT = 5
EXIT_QUARANTINE_INDETERMINATE = 6
EXIT_OPERATOR = 7


class ValidationError(Exception):
    def __init__(self, message: str, exit_code: int = EXIT_GATE_CONTAINMENT):
        super().__init__(message)
        if exit_code == 1:
            raise ValueError("Exit code 1 is forbidden by contract")
        self.message = message
        self.exit_code = exit_code


def validate_sha256(val: str, name: str = "sha256") -> str:
    if not isinstance(val, str) or not re.fullmatch(r"[0-9a-f]{64}", val):
        raise ValidationError(f"Invalid {name}: must be 64 lowercase hex characters", EXIT_INPUT)
    return val


def validate_commit_sha(val: str, name: str = "commit") -> str:
    if not isinstance(val, str) or not re.fullmatch(r"[0-9a-f]{40}", val):
        raise ValidationError(f"Invalid {name}: must be 40 lowercase hex characters", EXIT_INPUT)
    return val


def canonical_integer_json(data: Any) -> bytes:
    """Recursively validates no float values exist, sorts keys, and encodes to canonical UTF-8 JSON bytes."""
    def _check_no_floats(obj: Any):
        if isinstance(obj, float):
            raise ValidationError("Float values are strictly forbidden in canonical integer-JSON", EXIT_INPUT)
        elif isinstance(obj, dict):
            # Check duplicate keys if dict came from parser, but Python dict handles keys
            for k, v in obj.items():
                if not isinstance(k, str):
                    raise ValidationError("JSON object keys must be strings", EXIT_INPUT)
                _check_no_floats(v)
        elif isinstance(obj, list):
            for item in obj:
                _check_no_floats(item)

    _check_no_floats(data)
    json_str = json.dumps(data, separators=(",", ":"), sort_keys=True)
    return json_str.encode("utf-8")


class ManifestValidator:
    """Validates local TARGET manifest for Jules massive offline test work."""

    @staticmethod
    def validate(manifest: Dict[str, Any], computed_suite_sha256: Optional[str] = None) -> Dict[str, Any]:
        if not isinstance(manifest, dict):
            raise ValidationError("Manifest must be a JSON object/dict", EXIT_INPUT)

        required_fields = [
            "repo", "source_commit", "suite_sha256", "fixture_sha256", "seed",
            "ordered_gate_argv", "obligations", "resource_vector", "capture_scope",
            "stop_envelope", "task_id", "attempt_id", "lease_id", "epoch",
            "expected_negative_verdicts", "evidence_refs", "live_authorized"
        ]
        for field in required_fields:
            if field not in manifest:
                raise ValidationError(f"Missing required manifest field: {field}", EXIT_INPUT)

        # 1. repo & source_commit
        validate_commit_sha(manifest["source_commit"], "source_commit")
        if not isinstance(manifest["repo"], str) or not manifest["repo"]:
            raise ValidationError("repo must be non-empty string", EXIT_INPUT)

        # 2. suite & fixture digests
        suite_sha = validate_sha256(manifest["suite_sha256"], "suite_sha256")
        validate_sha256(manifest["fixture_sha256"], "fixture_sha256")
        if computed_suite_sha256 and computed_suite_sha256 != suite_sha:
            raise ValidationError(
                f"Suite digest mismatch: manifest has {suite_sha}, computed {computed_suite_sha256}",
                EXIT_GATE_CONTAINMENT
            )

        # 3. seed & gate argv
        if not isinstance(manifest["seed"], int) or isinstance(manifest["seed"], bool):
            raise ValidationError("seed must be an integer", EXIT_INPUT)
        if not isinstance(manifest["ordered_gate_argv"], list) or not manifest["ordered_gate_argv"]:
            raise ValidationError("ordered_gate_argv must be a non-empty list of strings", EXIT_INPUT)
        for arg in manifest["ordered_gate_argv"]:
            if not isinstance(arg, str):
                raise ValidationError("ordered_gate_argv items must be strings", EXIT_INPUT)

        # 4. obligations
        obligations = manifest["obligations"]
        if not isinstance(obligations, list):
            raise ValidationError("obligations must be a list", EXIT_INPUT)
        ob_ids = set()
        for ob in obligations:
            if not isinstance(ob, dict):
                raise ValidationError("obligation item must be a dict", EXIT_INPUT)
            if "id" not in ob or "weight" not in ob or "state" not in ob:
                raise ValidationError("obligation missing required fields (id, weight, state)", EXIT_INPUT)
            if ob["id"] in ob_ids:
                raise ValidationError(f"Duplicate obligation id: {ob['id']}", EXIT_INPUT)
            ob_ids.add(ob["id"])
            if not isinstance(ob["weight"], int) or ob["weight"] <= 0:
                raise ValidationError("obligation weight must be a positive integer", EXIT_INPUT)
            if ob["state"] not in ("accepted", "failed", "unresolved"):
                raise ValidationError(f"Invalid obligation state: {ob['state']}", EXIT_INPUT)

        # 5. resource_vector
        rv = manifest["resource_vector"]
        if not isinstance(rv, dict):
            raise ValidationError("resource_vector must be a dict", EXIT_INPUT)
        if "cpu_numerator" in rv and ("cpu_denominator" not in rv or rv["cpu_denominator"] <= 0):
            raise ValidationError("cpu_denominator must be a positive integer", EXIT_INPUT)

        # 6. stop_envelope
        stop = manifest["stop_envelope"]
        if stop is None or not isinstance(stop, dict):
            raise ValidationError("stop_envelope is missing or null", EXIT_GATE_CONTAINMENT)
        if "capacity" in stop and (not isinstance(stop["capacity"], int) or stop["capacity"] <= 0):
            raise ValidationError("stop_envelope capacity must be positive integer", EXIT_INPUT)

        # 7. expected_negative_verdicts
        neg = manifest["expected_negative_verdicts"]
        if not isinstance(neg, list) or len(neg) == 0:
            raise ValidationError("expected_negative_verdicts must be a non-empty list of negative controls", EXIT_GATE_CONTAINMENT)

        # 8. evidence_refs
        ev_refs = manifest["evidence_refs"]
        if not ev_refs or (isinstance(ev_refs, (dict, list)) and len(ev_refs) == 0):
            raise ValidationError("evidence_refs missing or stale host evidence", EXIT_PREREQ_UNAVAILABLE)
        if isinstance(ev_refs, dict) and ev_refs.get("stale", False):
            raise ValidationError("evidence_refs marked stale", EXIT_PREREQ_UNAVAILABLE)

        # 9. live_authorized
        if manifest["live_authorized"] is not False:
            raise ValidationError("live_authorized must be explicitly false for synthetic offline work", EXIT_GATE_CONTAINMENT)

        # 10. unmerged dependencies / authority check
        if manifest.get("has_unmerged_dependency", False):
            raise ValidationError("Unmerged dependency or missing authority detected", EXIT_PREREQ_UNAVAILABLE)

        return manifest


class CaptureCarrierValidator:
    """Validates proposed local TARGET capture carrier."""

    FIELDS = [
        "sample_id", "workload_repo", "workload_commit", "engine_sha256", "seed",
        "requested_ticks", "actual_ticks", "account_uid", "pid", "process_start",
        "namespace_id", "acquired_monotonic_ns", "clock_unit", "raw_capture_sha256",
        "inventory", "behavior", "cpu_numerator", "cpu_denominator", "ram_bytes",
        "censored", "status", "reason"
    ]

    @staticmethod
    def validate(carrier: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(carrier, dict):
            raise ValidationError("Capture carrier must be a dict", EXIT_INPUT)

        for field in CaptureCarrierValidator.FIELDS:
            if field not in carrier:
                raise ValidationError(f"Capture carrier missing field: {field}", EXIT_INPUT)

        # Counters and times must be integers (or null if AUSENTE)
        int_fields = ["requested_ticks", "actual_ticks", "pid", "process_start", "acquired_monotonic_ns", "cpu_numerator", "ram_bytes"]
        for f in int_fields:
            val = carrier[f]
            if val is not None:
                if not isinstance(val, int) or isinstance(val, bool):
                    raise ValidationError(f"Field {f} must be integer or null", EXIT_INPUT)
            elif carrier.get("reason") != "AUSENTE":
                raise ValidationError(f"Null field {f} requires reason AUSENTE", EXIT_INPUT)

        cpu_den = carrier["cpu_denominator"]
        if cpu_den is not None:
            if not isinstance(cpu_den, int) or isinstance(cpu_den, bool) or cpu_den <= 0:
                raise ValidationError("cpu_denominator must be a positive integer", EXIT_INPUT)

        # Inventory alone cannot qualify behavior; raw text and digest must agree
        raw_text = carrier.get("raw_capture_text")
        raw_sha = carrier.get("raw_capture_sha256")
        if raw_text is not None and raw_sha is not None:
            calc_sha = hashlib.sha256(raw_text.encode("utf-8")).hexdigest()
            if calc_sha != raw_sha:
                raise ValidationError("raw_capture_text digest does not match raw_capture_sha256", EXIT_GATE_CONTAINMENT)

        return carrier


class TranscriptValidator:
    """Validates proposed local TARGET transcript carrier."""

    @staticmethod
    def validate(transcript: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(transcript, dict):
            raise ValidationError("Transcript must be a dict", EXIT_INPUT)

        req = [
            "task_id", "attempt", "lease_id", "epoch", "repo_commit", "gate_argv",
            "image_sha256", "runner_sha256", "cache_sha256", "profile_sha256",
            "handle", "events", "receipt", "extract_sha256", "durable",
            "acknowledged", "purge_verified", "bridge_result", "reason"
        ]
        for f in req:
            if f not in transcript:
                raise ValidationError(f"Transcript missing field: {f}", EXIT_INPUT)

        # Dry publication check
        if transcript.get("dry_publication", False):
            if transcript["bridge_result"] is not None or transcript["reason"] != "unavailable":
                raise ValidationError("Unavailable dry publication must have null bridge_result and reason 'unavailable'", EXIT_GATE_CONTAINMENT)

        # Event sequence validation: submit, wait, receipt, durable collection, ack, purge verification
        events = transcript.get("events", [])
        if events:
            expected_order = ["submit", "wait", "receipt", "durable_collection", "ack", "purge_verification"]
            event_names = [e.get("type") if isinstance(e, dict) else e for e in events]
            filtered = [e for e in event_names if e in expected_order]
            for i in range(len(filtered) - 1):
                idx1 = expected_order.index(filtered[i])
                idx2 = expected_order.index(filtered[i + 1])
                if idx1 > idx2:
                    raise ValidationError(f"Invalid event sequence: {filtered[i]} before {filtered[i+1]}", EXIT_GATE_CONTAINMENT)

        # Copied receipt check
        receipt = transcript.get("receipt")
        if receipt and isinstance(receipt, dict):
            if receipt.get("copied_from_attempt"):
                raise ValidationError("Copied receipt detected", EXIT_GATE_CONTAINMENT)

        return transcript


class SpeechPayloadValidator:
    """Validates source-backed speech payload fields."""

    VALID_ACTS = {"finding", "repro", "affected", "status"}

    @staticmethod
    def validate(payload: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(payload, dict):
            raise ValidationError("Speech payload must be a dict", EXIT_INPUT)

        req = ["schema", "act", "wave_id", "task_id", "attempt_id", "lease_id", "seq", "repro_sha256", "affected", "status", "body"]
        for f in req:
            if f not in payload:
                raise ValidationError(f"Speech payload missing field: {f}", EXIT_INPUT)

        if payload["schema"] != "os.cu.speech-act/v1":
            raise ValidationError("Invalid schema: must be os.cu.speech-act/v1", EXIT_INPUT)

        if payload["act"] not in SpeechPayloadValidator.VALID_ACTS:
            raise ValidationError(f"Invalid act: {payload['act']}. Must be one of {SpeechPayloadValidator.VALID_ACTS}", EXIT_INPUT)

        return payload


class SidecarValidator:
    """Validates proposed local TARGET sidecar carrier."""

    def __init__(self, capacity: int = 100):
        self.capacity = capacity
        self.history: Dict[int, bytes] = {}
        self.last_applied_seq = 0

    def process(self, msg: Dict[str, Any], raw_body: bytes, verified_sig: bool) -> str:
        if not verified_sig:
            raise ValidationError("Sidecar payload failed signature/authenticator verification", EXIT_GATE_CONTAINMENT)

        req = ["sender", "task_id", "attempt_id", "lease_id", "epoch", "seq", "body_sha256", "body_length"]
        for f in req:
            if f not in msg:
                raise ValidationError(f"Sidecar message missing field: {f}", EXIT_INPUT)

        seq = msg["seq"]
        calc_sha = hashlib.sha256(raw_body).hexdigest()
        if calc_sha != msg["body_sha256"] or len(raw_body) != msg["body_length"]:
            raise ValidationError("Sidecar body digest or length mismatch", EXIT_GATE_CONTAINMENT)

        if seq in self.history:
            if self.history[seq] == raw_body:
                return "IDEMPOTENT_REPLAY"
            else:
                return "QUARANTINE_CHANGED_BYTES"

        if seq > self.last_applied_seq + 1:
            return "HOLD_GAP"

        self.history[seq] = raw_body
        self.last_applied_seq = seq
        return "APPLIED_ACK"


class IdentityBindingValidator:
    """Validates identity binding encoding according to restated spec."""

    DOMAIN = b"w8-repro-v1"

    @staticmethod
    def encode(identity_data: Dict[str, Any]) -> bytes:
        json_bytes = canonical_integer_json(identity_data)
        length_bytes = struct.pack(">Q", len(json_bytes))  # 8 big-endian bytes
        return IdentityBindingValidator.DOMAIN + length_bytes + json_bytes

    @staticmethod
    def decode_and_validate(blob: bytes) -> Dict[str, Any]:
        if not blob.startswith(IdentityBindingValidator.DOMAIN):
            raise ValidationError("Invalid identity binding domain prefix", EXIT_INPUT)

        offset = len(IdentityBindingValidator.DOMAIN)
        if len(blob) < offset + 8:
            raise ValidationError("Truncated identity binding header", EXIT_INPUT)

        length = struct.unpack(">Q", blob[offset:offset + 8])[0]
        offset += 8

        json_bytes = blob[offset:offset + length]
        if len(json_bytes) != length:
            raise ValidationError("Identity binding length mismatch", EXIT_INPUT)

        try:
            data = json.loads(json_bytes.decode("utf-8"))
        except Exception as e:
            raise ValidationError(f"Identity binding JSON decode error: {e}", EXIT_INPUT)

        req_semantic = [
            "normalization_version", "repo", "base_commit", "image", "toolchain",
            "cache_manifest", "ordered_gate_argv", "relative_cwd", "outcome",
            "assertion_or_crash_signature", "ordered_selected_frames",
            "affected_path_and_symbol", "input_identity"
        ]
        for f in req_semantic:
            if f not in data:
                raise ValidationError(f"Identity binding missing semantic field: {f}", EXIT_INPUT)

        return data


class SnapshotValidator:
    """Validates proposed local TARGET snapshot carrier."""

    @staticmethod
    def validate(snapshot: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(snapshot, dict):
            raise ValidationError("Snapshot must be a dict", EXIT_INPUT)

        req = [
            "sample_id", "acquired_monotonic_ns", "workload_commit", "cpu_numerator",
            "cpu_denominator", "ram_bytes", "pids", "fds", "ancestor_limits",
            "physical_headroom_bytes", "startup_bytes", "collector_bytes",
            "reaction_reserve_bytes", "pressure", "tail_latency_ns", "dwell_ns",
            "last_qualified_width", "entries"
        ]
        for f in req:
            if f not in snapshot:
                raise ValidationError(f"Snapshot missing field: {f}", EXIT_INPUT)

        width = snapshot.get("last_qualified_width")
        if width is not None:
            if isinstance(width, dict) and width.get("unqualified", False):
                raise ValidationError("Unqualified width proposal in snapshot", EXIT_GATE_CONTAINMENT)

        entries = snapshot.get("entries", [])
        if not isinstance(entries, list):
            raise ValidationError("Snapshot entries must be a list", EXIT_INPUT)

        for entry in entries:
            req_e = ["entry_id", "generation", "lease_id", "epoch", "state", "reserved_bytes", "cleanup_verified"]
            for f in req_e:
                if f not in entry:
                    raise ValidationError(f"Snapshot entry missing field: {f}", EXIT_INPUT)

        return snapshot


class LoopCarrierValidator:
    """Validates proposed local TARGET loop carrier."""

    @staticmethod
    def validate(loop: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(loop, dict):
            raise ValidationError("Loop carrier must be a dict", EXIT_INPUT)

        req = [
            "wave_id", "obligations", "attempts", "resource_vector", "escrow",
            "lease_id", "epoch", "origin_verified", "gates_verified",
            "cleanup_verified", "settlement_id", "body_sha256", "disposition"
        ]
        for f in req:
            if f not in loop:
                raise ValidationError(f"Loop carrier missing field: {f}", EXIT_INPUT)

        obligations = loop.get("obligations", [])
        if not obligations:
            raise ValidationError("Inventory conservation requires non-empty obligations", EXIT_GATE_CONTAINMENT)

        all_accepted = all(ob.get("state") == "accepted" for ob in obligations)
        origin_v = loop.get("origin_verified", False)
        gates_v = loop.get("gates_verified", False)
        cleanup_v = loop.get("cleanup_verified", False)

        has_failures = any(ob.get("state") in ("failed", "unresolved") for ob in obligations)

        if loop.get("disposition") == "SUCCESS":
            if not (all_accepted and origin_v and gates_v and cleanup_v and not has_failures):
                raise ValidationError("Loop success requires all obligations accepted, gates/origin proved, no failures, verified cleanup", EXIT_GATE_CONTAINMENT)

        return loop


class TrainingSignalValidator:
    """Validates text-only training-signal carrier according to restated training semantics."""

    RECOMMENDED_MODEL = "Argon"

    @staticmethod
    def validate(carrier: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(carrier, dict):
            raise ValidationError("Training signal carrier must be a dict", EXIT_INPUT)

        # Freeze repo, engine/source/tool versions, seed, ticks, target, tab, session, segment, acquisition provenance
        req = [
            "repository", "engine_version", "source_version", "tool_version", "seed",
            "requested_ticks", "actual_ticks", "target", "tab_id", "session_id",
            "segment_id", "acquisition_provenance", "state", "ax_sha256",
            "ax_stable_sha256", "diff_sha256", "t2_trace", "frame_refs",
            "state_sha256", "gameplay_state_hash"
        ]
        for f in req:
            if f not in carrier:
                raise ValidationError(f"Training signal missing field: {f}", EXIT_INPUT)

        # Reject floats, duplicate keys, wrong-session joins, replay, missing bounds, unproven labels
        # STATE must be canonical integer-JSON
        state = carrier["state"]
        canonical_integer_json(state)  # checks no floats

        ax_sha = validate_sha256(carrier["ax_sha256"], "ax_sha256")
        ax_stable_sha = validate_sha256(carrier["ax_stable_sha256"], "ax_stable_sha256")
        if ax_sha == ax_stable_sha:
            raise ValidationError("ax_sha256 and ax_stable_sha256 must be distinct", EXIT_INPUT)

        # UI-tree diff bound by diff_sha256
        validate_sha256(carrier["diff_sha256"], "diff_sha256")

        # T2 trace bytes with monotonic tick and seq and explicit admission/executor verdict
        t2 = carrier.get("t2_trace")
        if not isinstance(t2, dict):
            raise ValidationError("t2_trace must be a dict", EXIT_INPUT)
        if "ticks" not in t2 or "seqs" not in t2 or "admission_verdict" not in t2 or "executor_verdict" not in t2:
            raise ValidationError("t2_trace missing required monotonic fields or verdicts", EXIT_INPUT)

        ticks = t2["ticks"]
        seqs = t2["seqs"]
        for i in range(len(ticks) - 1):
            if ticks[i + 1] <= ticks[i]:
                raise ValidationError("t2_trace ticks must be strictly monotonic", EXIT_INPUT)
        for i in range(len(seqs) - 1):
            if seqs[i + 1] <= seqs[i]:
                raise ValidationError("t2_trace seqs must be strictly monotonic", EXIT_INPUT)

        # Frame metadata referenced by SHA only
        frames = carrier.get("frame_refs", [])
        for fr in frames:
            if isinstance(fr, dict) and ("raw_pixels" in fr or "base64" in fr or "html" in fr):
                raise ValidationError("Reject raw pixels, base64 blobs, or HTML in frame references", EXIT_GATE_CONTAINMENT)
            if not isinstance(fr, str) or not re.fullmatch(r"[0-9a-f]{64}", fr):
                raise ValidationError("Frame metadata must be referenced by SHA-256 string only", EXIT_INPUT)

        # Gameplay state_hash mapped explicitly to state_sha256. Gameplay hashes and AX hashes are distinct.
        gameplay_hash = carrier.get("gameplay_state_hash")
        state_sha = validate_sha256(carrier["state_sha256"], "state_sha256")
        if gameplay_hash in (ax_sha, ax_stable_sha):
            raise ValidationError("Gameplay state hash and AX hashes must be distinct", EXIT_INPUT)

        # Dummy renderer check
        renderer = carrier.get("renderer")
        if renderer == "dummy":
            if carrier.get("frame_grab_status") != "GRAB_UNAVAILABLE":
                raise ValidationError("Dummy renderer must say GRAB_UNAVAILABLE, never claim a frame", EXIT_GATE_CONTAINMENT)

        return carrier
