"""Run fixed-limit synthetic calibration and prepare BASE qualification bytes."""
import json
from collections.abc import Mapping
from dataclasses import dataclass
from fractions import Fraction
from functools import lru_cache
from pathlib import Path

from jsonschema import Draft202012Validator

from asset_factory import core
from tools.lipsync import ENVELOPE, VoiceError, _audio_peaks, validate_track

from .annotations import CLOSURE, ONSET, labels, measure, metric, presentation, render_frames
from .common import CalibrationError, digest, encode, parse, require
from .ledger import ExposureLedger
from .pcm import MATCH_THRESHOLD_PPM, MAX_CORRELATION_TERMS, MAX_DECODED_SAMPLES, MIN_MATCH_FRAMES
from .pcm import compare_pcm, decode_pcm
from .split import SPLITS, verify_split


@lru_cache(maxsize=1)
def _validator():
    path = Path(__file__).resolve().parents[2] / "schemas" / "af.calibration-protocol.v1.schema.json"
    schema = json.loads(path.read_bytes())
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


@dataclass(frozen=True)
class Attempt:
    audio: bytes | None = None
    track: bytes | None = None
    annotations: bytes | None = None
    render: bytes | None = None
    alignment: bytes | None = None
    source_rc: int = 0


@dataclass(frozen=True)
class CalibrationResult:
    witness: bytes
    qualifications: tuple[bytes, ...]

    def read_checked(self, role, pin, *, frozen_protocol, approved_protocol_digest,
                     approved_fixture_witness_digest, retained_attempts=None):
        """Expose independently approved fixture bytes for the BASE reader."""
        witness_pin = digest(self.witness)
        if not self.qualifications or witness_pin != approved_fixture_witness_digest:
            return None
        try:
            verify_witness(self, frozen_protocol, approved_protocol_digest=approved_protocol_digest,
                           retained_attempts=retained_attempts)
        except CalibrationError:
            return None
        if role == "held-out-calibration" and pin == witness_pin:
            return self.witness
        if role == "calibration":
            return next((raw for raw in self.qualifications if digest(raw) == pin), None)
        return None


def _rc(codes):
    result = core.worst_rc(codes or [0])
    if result == 3:
        if 4 in codes:
            return 4
        if 6 in codes:
            return 6
    return result


def _evaluate_case(case, attempt, protocol, split_refusal):
    fallback = protocol["source"] == ENVELOPE
    limits = protocol["limits"]
    metrics = {key: metric(case["annotation_counts"][key]) for key in limits}
    artifact_digests = {}
    reasons = []
    try:
        require(attempt is not None, 3, "attempt_missing")
        require(type(attempt) is Attempt, 2, "invalid_attempt")
        for name in ("audio", "alignment", "track", "annotations", "render"):
            raw = getattr(attempt, name)
            require(raw is None or type(raw) is bytes, 2, "invalid_attempt_bytes")
            artifact_digests[name] = digest(raw) if raw is not None else None
        require(type(attempt.source_rc) is int and attempt.source_rc in core.RC, 2, "invalid_source_rc")
        require(attempt.source_rc == 0, attempt.source_rc, "source_refusal")
        require(attempt.audio is not None and attempt.track is not None, 3, "audio_or_track_missing")
        require(artifact_digests["audio"] == case["audio_digest"], 4, "audio_digest_changed")
        if split_refusal is not None:
            raise split_refusal
        if not fallback:
            require(attempt.alignment is not None, 3, "alignment_missing")
        require(artifact_digests["alignment"] == case["alignment_digest"], 4, "alignment_digest_changed")
        track = parse(attempt.track)
        validate_track(track)
        require(track["source"] == protocol["source"] and track["audio_sha256"] == case["audio_digest"][7:],
                4, "track_identity_changed")
        track_alignment = track["alignment_sha256"]
        require((f"sha256:{track_alignment}" if track_alignment else None) == case["alignment_digest"],
                4, "track_alignment_changed")
        require(Fraction(*track["fps"]) == Fraction(*case["track_fps"])
                and track["silence_peak_pcm"] == case["silence_peak_pcm"], 4, "track_policy_changed")
        rate, peaks = _audio_peaks(attempt.audio)
        require((rate, len(peaks)) == (case["sample_rate"], case["sample_count"])
                == (track["sample_rate"], track["sample_count"]), 4, "audio_timebase_changed")
        annotations = labels(attempt.annotations, case, fallback)
        require(all(max(peaks[start:end]) <= case["silence_peak_pcm"] for start, end in annotations[2]),
                5, "labelled_silence_contains_audio")
        observed = render_frames(attempt.render, case, artifact_digests["track"], fallback)
        metrics, unavailable = measure(observed, annotations, rate, fallback)
        for name, maximum in limits.items():
            value = metrics[name]["value"]
            if name in unavailable:
                reasons.append(unavailable[name])
            elif Fraction(*value) > maximum:
                reasons.append((5, name))
    except (CalibrationError, VoiceError) as exc:
        reasons.append((exc.rc, str(exc)))

    return {"case_id": case["id"], "split": case["split"], "rc": _rc([rc for rc, _ in reasons]),
            "source_rc": attempt.source_rc if type(attempt) is Attempt and type(attempt.source_rc) is int else None,
            "artifact_digests": artifact_digests, "metrics": metrics, "reason_codes": [name for _, name in reasons]}


def _pcm_refusals(cases, captured):
    signals = {split: {} for split in SPLITS}
    refusals = {}
    remaining_samples = MAX_DECODED_SAMPLES
    for case, attempt in zip(cases, captured, strict=True):
        if (type(attempt) is not Attempt or type(attempt.audio) is not bytes
                or type(attempt.source_rc) is not int or attempt.source_rc != 0
                or digest(attempt.audio) != case["audio_digest"]):
            continue
        try:
            channels, used = decode_pcm(attempt.audio, case, remaining_samples)
            remaining_samples -= used
            require(channels, 3, "pcm_split_overlap_unavailable")
        except CalibrationError as exc:
            refusals[case["id"]] = exc
        else:
            signals[case["split"]][case["id"]] = case["sample_rate"], channels

    remaining_terms = MAX_CORRELATION_TERMS
    for case_id, held_out in signals["held-out"].items():
        try:
            for calibration in signals["calibration"].values():
                matched, remaining_terms = compare_pcm(calibration, held_out, remaining_terms)
                require(not matched, 4, "cross_split_pcm_match")
        except CalibrationError as exc:
            refusals[case_id] = exc
            if exc.rc == 6:
                remaining_terms = 0
    return refusals


def _input_fingerprints(cases, read_audio):
    """Read only the approver's digest-addressed inputs, before attempt access."""
    fingerprints = {}
    remaining = MAX_DECODED_SAMPLES
    for case in cases:
        pin = case["audio_digest"]
        try:
            raw = read_audio(pin)
        except OSError as exc:
            raise CalibrationError(3, "calibration_input_audio_unavailable") from exc
        require(type(raw) is bytes, 3, "calibration_input_audio_unavailable")
        require(digest(raw) == pin, 4, "audio_digest_changed")
        channels, used = decode_pcm(raw, case, remaining)
        require(channels, 3, "pcm_split_overlap_unavailable")
        remaining -= used
        fingerprints[pin] = case["sample_rate"], channels
    return fingerprints


def _rebuild_result(raw, captured):
    protocol = parse(raw)
    cases = _ordered_cases(protocol)
    split_refusals = _pcm_refusals(cases, captured)
    rows = [_evaluate_case(case, attempt, protocol, split_refusals.get(case["id"]))
            for case, attempt in zip(cases, captured, strict=True)]
    return _build_result(protocol, digest(raw), rows)


def _ordered_cases(protocol):
    return sorted(protocol["cases"], key=lambda row: (row["split"], row["id"]))


def _build_result(protocol, protocol_digest, attempts):
    splits = {}
    for split in SPLITS:
        rows = [row for row in attempts if row["split"] == split]
        refused = sum(row["rc"] != 0 for row in rows)
        splits[split] = {"expected": len(rows), "passed": len(rows) - refused, "refused": refused}
    rc = _rc([row["rc"] for row in attempts])
    witness = encode({"schema": "af.synthetic-calibration-witness/v1", "protocol_digest": protocol_digest,
                      "decision_rule": protocol["decision_rule"], "material": protocol["material"],
                      "pcm_match_threshold_ppm": MATCH_THRESHOLD_PPM,
                      "pcm_min_match_frames": MIN_MATCH_FRAMES,
                      "source": protocol["source"], "splits": splits, "attempts": attempts,
                      "rc": rc, "fixture_verdict": core.VERDICT[rc], "production_calibration": "AUSENTE"})
    qualifications = ()
    if rc == 0:
        witness_pin = digest(witness)
        qualifications = tuple(encode({
            **protocol["scope"], "id": name, "unit": "ns" if name == ONSET else "ppm",
            "scale": 1, "operator": "le", "limit": limit, "basis": "CALIBRATED",
            "method_digest": protocol["method_digest"], "uncertainty_kind": "exact",
            "min_confidence_ppm": None, "witness_digest": witness_pin,
        }) for name, limit in sorted(protocol["limits"].items()))
    return CalibrationResult(witness, qualifications)


class _FrozenProtocol:
    def __init__(self, raw, ledger, lineage, held_out, fingerprints, held_out_audio):
        self._raw = raw
        self._ledger = ledger
        self._lineage = lineage
        self._held_out = held_out
        self._fingerprints = fingerprints
        self._held_out_audio = held_out_audio
        self._captured = None

    def evaluate(self, read_attempt):
        """Open predeclared cases once, in calibration then held-out order.

        The reviewer supplies the attempt reader. None records an unavailable
        attempt. An I/O failure consumes that case and is retained as a refusal.
        """
        require(callable(read_attempt), 2, "invalid_attempt_reader")
        self._ledger._check(digest(self._raw), self._lineage, load_fingerprints=lambda: self._fingerprints,
                            expose=self._held_out, held_out_audio=self._held_out_audio)
        captured = []
        for case in _ordered_cases(parse(self._raw)):
            try:
                attempt = read_attempt(case["id"])
            except OSError:
                attempt = Attempt(source_rc=3)
            captured.append(attempt)
        captured = tuple(captured)
        result = _rebuild_result(self._raw, captured)
        self._ledger._seal(digest(self._raw), digest(result.witness))
        self._captured = captured
        return result


def verify_witness(result, frozen_protocol, *, approved_protocol_digest, retained_attempts=None):
    """Rebuild from original captured bytes or independently retained attempts by case ID."""
    require(type(frozen_protocol) is _FrozenProtocol, 2, "invalid_frozen_protocol")
    require(type(result) is CalibrationResult, 2, "invalid_calibration_result")
    raw = frozen_protocol._raw
    reported = parse(result.witness)
    require(digest(raw) == approved_protocol_digest == reported.get("protocol_digest"),
            4, "witness_protocol_digest_changed")
    recorded_witness = frozen_protocol._ledger._witness_pin(approved_protocol_digest)
    captured = frozen_protocol._captured
    if retained_attempts is not None:
        case_ids = [case["id"] for case in _ordered_cases(parse(raw))]
        require(isinstance(retained_attempts, Mapping) and retained_attempts.keys() == set(case_ids),
                4, "retained_case_coverage_changed")
        captured = tuple(retained_attempts[case_id] for case_id in case_ids)
    require(captured is not None, 3, "attempt_evidence_unavailable")
    rebuilt = _rebuild_result(raw, captured)
    expected_rows = parse(rebuilt.witness)["attempts"]
    rows = reported.get("attempts")
    require(type(rows) is list and len(rows) == len(expected_rows), 4, "witness_case_coverage_changed")
    for row, expected in zip(rows, expected_rows, strict=True):
        require(type(row) is dict and (row.get("case_id"), row.get("split")) ==
                (expected["case_id"], expected["split"]), 4, "witness_case_split_changed")
        require(row.get("artifact_digests") == expected["artifact_digests"], 4, "witness_artifact_digest_changed")
    require(result == rebuilt, 4, "witness_rebuild_mismatch")
    require(digest(result.witness) == recorded_witness, 4, "recorded_witness_changed")


def freeze(raw, *, ledger, read_audio):
    """Check approved input audio and lineage before granting access to attempts."""
    protocol = parse(raw)
    require(_validator().is_valid(protocol), 2, "invalid_calibration_protocol")
    held_out = verify_split(protocol["lineage"], protocol["cases"])
    fallback = protocol["source"] == ENVELOPE
    for case in protocol["cases"]:
        require(case["annotator"] != case["render_observer"], 2, "annotation_independence_missing")
        require((case["annotation_counts"][CLOSURE] == 0) == fallback, 2, "closure_applicability_changed")
        presentation(case["presentation"], Fraction(case["sample_count"], case["sample_rate"]))
    require(type(ledger) is ExposureLedger, 2, "invalid_exposure_ledger")
    require(callable(read_audio), 2, "invalid_calibration_input_reader")
    lineage = frozenset(row["digest"] for row in protocol["lineage"])
    cases = _ordered_cases(protocol)
    fingerprints = ledger._check(digest(raw), lineage, load_fingerprints=lambda: _input_fingerprints(cases, read_audio))
    held_out_audio = frozenset(case["audio_digest"] for case in cases if case["split"] == "held-out")
    return _FrozenProtocol(raw, ledger, lineage, held_out, fingerprints, held_out_audio)
