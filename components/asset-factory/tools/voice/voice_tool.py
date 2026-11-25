#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Admit local narration under a caller-pinned job and intake manifest."""
import argparse
from array import array
from datetime import datetime, timezone
from fractions import Fraction
from hashlib import sha256
import io
import json
from math import ceil
import os
from pathlib import Path
import re
import stat
import struct
import sys
import wave

JOB_SCHEMA = "rp4.voice-ingest-job/v1"
MANIFEST_SCHEMA = "rp4.voice-intake-manifest/v1"
RESULT_SCHEMA = "rp4.voice-ingest-result/v1"
VOICE_DISPOSITION_SCHEMA = "rp3.voice-disposition/v1"
TIMING_SCHEMA = "rp3.audio-timing/v1"
CONSENT_SCHEMA = "rp3.voice-consent/v1"
SILENCE_SCHEMA = "rp3.silence-disposition/v1"
LATE_DECISION_SCHEMA = "rp3.voice-late-decision/v1"
MAX_JSON_BYTES = 4 * 2**20
MAX_AUDIO_BYTES = 64 * 2**20
# The package vendors this policy so its portable launcher can also enforce it.
from importlib.util import module_from_spec, spec_from_file_location

_policy_path = Path(__file__).resolve().parent.parent / "explainer_factory/package/voice_policy.py"
_policy_spec = spec_from_file_location("rp3_voice_policy", _policy_path)
policy = module_from_spec(_policy_spec)
_policy_spec.loader.exec_module(policy)


class VoiceRefusal(Exception):
    def __init__(self, code, detail=""):
        super().__init__(code)
        self.code = code
        self.detail = detail


def require(condition, code, detail=""):
    if not condition:
        raise VoiceRefusal(code, detail)


def sha(data):
    return sha256(data).hexdigest()


def canonical_json_bytes(obj):
    return (json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                       allow_nan=False) + "\n").encode("utf-8")


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, "duplicate_json_key", key)
        result[key] = value
    return result


def reject_number(value):
    raise VoiceRefusal("non_integer_json_number", value)


def parse_json(raw):
    try:
        obj = json.loads(raw, object_pairs_hook=unique_object, parse_float=reject_number,
                         parse_constant=reject_number)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise VoiceRefusal("invalid_json") from exc
    require(isinstance(obj, dict), "invalid_json")
    return obj


def parse_pin(value):
    require(isinstance(value, str) and re.fullmatch(r"(?:sha256:)?[a-f0-9]{64}", value),
            "invalid_digest_pin")
    return value.removeprefix("sha256:")


def pin(data):
    return "sha256:" + sha(data)


def private_directory(parent):
    require(parent.resolve() == parent.absolute(), "unrestricted_media_reference")
    fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    attrs = os.fstat(fd)
    if attrs.st_uid != os.getuid() or attrs.st_mode & 0o077:
        os.close(fd)
        raise VoiceRefusal("unrestricted_media_reference")
    return fd


def read_bytes(path, limit=MAX_JSON_BYTES, private=False):
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    if private:
        directory = private_directory(path.parent)
        try:
            fd = os.open(path.name, flags, dir_fd=directory)
        finally:
            os.close(directory)
    else:
        fd = os.open(path, flags)
    with os.fdopen(fd, "rb") as handle:
        attrs = os.fstat(handle.fileno())
        require(stat.S_ISREG(attrs.st_mode), "invalid_input_file")
        if private:
            require(attrs.st_uid == os.getuid() and not attrs.st_mode & 0o077,
                    "unrestricted_media_reference")
        require(attrs.st_size <= limit, "input_byte_budget_exceeded")
        raw = handle.read(limit + 1)
        require(len(raw) <= limit, "input_byte_budget_exceeded")
        return raw


def pinned_bytes(path, expected, code, **options):
    expected_sha = parse_pin(expected)
    raw = read_bytes(path, **options)
    require(sha(raw) == expected_sha, code)
    return raw


def timestamp(value):
    try:
        moment = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise VoiceRefusal("invalid_policy_clock") from exc
    require(moment.utcoffset() is not None, "invalid_policy_clock")
    return moment.astimezone(timezone.utc)


def load_job(raw, script):
    job = parse_json(raw)
    require(job.get("schema") == JOB_SCHEMA, "invalid_job")
    require(sha(read_bytes(script)) == parse_pin(job.get("script_sha256")), "script_digest_mismatch")
    fps = job.get("fps")
    require(isinstance(fps, list) and len(fps) == 2
            and all(type(i) is int and i > 0 for i in fps), "invalid_frame_clock")
    start, end, rate = (job.get(k) for k in ("start_frame", "end_frame", "sample_rate"))
    require(all(type(i) is int for i in (start, end, rate))
            and 0 <= start < end and 8000 <= rate <= 192000, "invalid_frame_clock")
    locale, domain = job.get("locale"), job.get("voice_domain")
    require(isinstance(locale, str) and re.fullmatch(r"[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*", locale)
            and policy.allowed_domain(domain),
            "invalid_job_scope")
    if "voice_purpose" in job:
        require(policy.valid_purpose(job["voice_purpose"]), "invalid_job_scope")
    min_active = job.get("min_active_samples")
    require(type(min_active) is int and min_active > 0, "invalid_activity_policy")
    cutoff = job.get("cutoff_policy")
    require(isinstance(cutoff, dict), "invalid_policy_clock")
    require(timestamp(cutoff.get("primary_at")) <= timestamp(cutoff.get("final_at")), "invalid_policy_clock")
    clock = Fraction(rate * fps[1], fps[0])
    total = ceil((end - start) * clock)
    require(total > 0 and total * 2 + 44 <= MAX_AUDIO_BYTES, "audio_byte_budget_exceeded")
    cues = job.get("cues")
    require(isinstance(cues, list) and cues, "invalid_cue_schedule")
    previous, ids = start, set()
    for cue in cues:
        require(isinstance(cue, dict) and cue.keys() == {"cue_id", "start_frame", "end_frame"},
                "invalid_cue_schedule")
        cue_id, left, right = (cue.get(k) for k in ("cue_id", "start_frame", "end_frame"))
        require(isinstance(cue_id, str) and re.fullmatch(r"(?:[A-Za-z0-9][A-Za-z0-9_.-]{0,79}|urn:story:[a-z0-9_-]+:cue:[a-z0-9_-]+)", cue_id)
                and cue_id not in ids and type(left) is int and type(right) is int
                and previous <= left < right <= end, "invalid_cue_schedule")
        require(ceil((right - start) * clock) - ceil((left - start) * clock) >= min_active,
                "invalid_cue_sample_window", cue_id)
        previous = right
        ids.add(cue_id)
    return job


def sample_at(job, frame):
    numerator, denominator = job["fps"]
    return ceil(Fraction((frame - job["start_frame"]) * job["sample_rate"] * denominator, numerator))


def cue_windows(job):
    return [(cue, sample_at(job, cue["start_frame"]), sample_at(job, cue["end_frame"]))
            for cue in job["cues"]]


def validate_riff(raw):
    require(len(raw) >= 12 and raw[:4] == b"RIFF" and raw[8:12] == b"WAVE"
            and struct.unpack_from("<I", raw, 4)[0] == len(raw) - 8, "invalid_wav_container")


def read_pcm(raw, expected_rate, expected_count):
    validate_riff(raw)
    try:
        with wave.open(io.BytesIO(raw), "rb") as wav:
            require(wav.getsampwidth() == 2 and wav.getcomptype() == "NONE"
                    and wav.getnchannels() in (1, 2), "invalid_pcm16_wav")
            channels, count = wav.getnchannels(), wav.getnframes()
            require(wav.getframerate() == expected_rate and count == expected_count,
                    "sample_clock_mismatch")
            pcm = wav.readframes(count + 1)
    except (wave.Error, EOFError, struct.error) as exc:
        raise VoiceRefusal("invalid_pcm16_wav") from exc
    require(len(pcm) == 2 * channels * count, "sample_clock_mismatch")
    return pcm, channels


def reject_known_fixture(wav, pcm=None):
    require(sha(wav) not in policy.KNOWN_FIXTURE_WAV_DIGESTS
            and (pcm is None or sha(pcm) not in policy.KNOWN_FIXTURE_PCM_DIGESTS),
            "substituted_voice_source", "known rehearsal or fixture digest")


def pcm_peaks(pcm, channels):
    samples = array("h")
    samples.frombytes(pcm)
    if sys.byteorder != "little":
        samples.byteswap()
    if channels == 1:
        return array("H", (abs(sample) for sample in samples))
    return array("H", (max(abs(samples[i]), abs(samples[i + 1])) for i in range(0, len(samples), 2)))


def time_and_hash_wav(raw, job):
    count = sample_at(job, job["end_frame"])
    pcm, channels = read_pcm(raw, job["sample_rate"], count)
    peaks = pcm_peaks(pcm, channels)
    width = 2 * channels
    clock = clock_fields(job)
    # Keep PCM verification exact and use AUDIO's established 48 kHz delivery
    # onset and envelope allowances, scaled to the intake sample rate.
    delivery_scale = Fraction(job["sample_rate"], 48000)
    threshold = 300
    cues, silences = [], []

    cursor = 0
    for cue, left, right in cue_windows(job):
        if cursor < left:
            require(not any(peaks[cursor:left]), "cue_window_spill", cue["cue_id"])
            silences.append([cursor, left])
        onset, last, active, longest_pause = None, 0, 0, 0
        for index in range(left, right):
            # Measure speech separately in each cue. Gaps outside the cues require exact zero.
            if peaks[index] > threshold:
                if onset is None:
                    onset = index
                else:
                    longest_pause = max(longest_pause, index - last - 1)
                last = index
                active += 1
        require(active >= job["min_active_samples"], "cue_missing_speech", cue["cue_id"])
        cues.append({**cue, "window_start_sample": left, "window_end_sample": right,
                     "expected_onset_sample": onset, "expected_end_sample": last + 1,
                     "active_sample_count": active, "max_intra_cue_gap_samples": longest_pause,
                     "pcm_slice_sha256": pin(pcm[onset * width:(last + 1) * width]),
                     "window_pcm_sha256": pin(pcm[left * width:right * width])})
        cursor = right
    if cursor < count:
        require(not any(peaks[cursor:]), "cue_window_spill", "tail")
        silences.append([cursor, count])
    return ({**clock, "channels": channels, "sample_width_bytes": 2, "silences": silences,
             "cues": cues, "wav_sha256": pin(raw), "pcm_sha256": pin(pcm),
             "silence_peak_pcm": threshold, "bridge_samples": max(c["max_intra_cue_gap_samples"] for c in cues),
             "activity_by_cue": True, "onset_tolerance_samples": 0, "tail_tolerance_samples": 0,
             "delivery_onset_tolerance_samples": ceil(64 * delivery_scale),
             "delivery_tail_tolerance_samples": ceil(480 * delivery_scale)}, pcm)


def clock_fields(job):
    start, end = job["start_frame"], job["end_frame"]
    duration = Fraction(end - start, 1) / Fraction(*job["fps"])
    return {key: job[key] for key in ("locale", "voice_domain", "fps",
                                      "sample_rate", "start_frame", "end_frame")} | {
        "schema": TIMING_SCHEMA,
        "script_sha256": "sha256:" + parse_pin(job["script_sha256"]),
        "total_frames": end - start,
        "duration_seconds_rational": [duration.numerator, duration.denominator],
        "sample_count": sample_at(job, end),
    }


def verify_authority(consent, job, timing, origin):
    fixture = origin == "generic_fixture"
    kind = "synthetic_fixture" if fixture else "genuine_operator_voice"
    authority = "fixture" if fixture else "operator"
    require(consent.get("source_kind") == kind and consent.get("subject_authority") == authority
            and consent.get("speaker_origin") == origin
            and consent.get("rehearsal_or_synthetic") is fixture, "substituted_voice_source")
    route = "fixture_playback" if fixture else "local_voiced_master"
    fields = {"schema", "assent", "source_kind", "subject_authority", "speaker_origin",
              "rehearsal_or_synthetic", "permitted_routes", "publication_authorized",
              "provider_egress_allowed", "locale", "voice_domain", "wav_sha256", "pcm_sha256",
              "script_sha256"}
    purpose = job.get("voice_purpose")
    if not fixture and purpose is None and job["voice_domain"] == policy.LEGACY_VOICE_DOMAIN:
        purpose = policy.LEGACY_VOICE_PURPOSE
    if purpose is not None:
        fields.add("purpose")
    require((fixture and purpose is None or policy.valid_purpose(purpose))
            and consent.keys() == fields and consent.get("purpose") == purpose
            and consent.get("schema") == CONSENT_SCHEMA
            and consent.get("assent") is True and consent.get("provider_egress_allowed") is False
            and consent.get("publication_authorized") is False
            and consent.get("permitted_routes") == [route], "inferred_consent_rejected")
    require(consent.get("voice_domain") == job["voice_domain"] and consent.get("locale") == job["locale"],
            "consent_scope_mismatch")
    for key in ("script_sha256", "wav_sha256", "pcm_sha256"):
        require(parse_pin(consent.get(key)) == parse_pin(timing[key]), "consent_digest_mismatch", key)
    return route


def evaluate(args, evaluated_at):
    manifest_raw = pinned_bytes(args.manifest, args.manifest_sha256, "manifest_digest_mismatch")
    manifest = parse_json(manifest_raw)
    require(manifest.get("schema") == MANIFEST_SCHEMA, "invalid_manifest")
    job_raw = pinned_bytes(args.job, manifest.get("job_sha256"), "job_digest_mismatch")
    job = load_job(job_raw, args.script)
    mode = manifest.get("mode")
    manifest_fields = {"schema", "job_sha256", "mode", "wav_sha256", "origin",
                       "consent_sha256", "late_decision_sha256", "silence_reason"}
    require(manifest.keys() <= manifest_fields, "invalid_manifest")
    after_primary = evaluated_at > timestamp(job["cutoff_policy"]["primary_at"])
    timing = clock_fields(job)
    result = {"schema": RESULT_SCHEMA, "manifest_sha256": pin(manifest_raw),
              "job_sha256": pin(job_raw), "evaluated_at": evaluated_at.isoformat(),
              "disposition": {"schema": VOICE_DISPOSITION_SCHEMA, "audio_mode": mode,
                              "wav_supplied": args.wav is not None}, "timing": timing}
    disposition = result["disposition"]

    if mode == "silent":
        require(all(manifest.get(k) is None for k in ("wav_sha256", "consent_sha256", "origin",
                                                     "late_decision_sha256"))
                and args.wav is None and args.consent is None and args.late_decision is None,
                "unmanifested_audio")
        reason = manifest.get("silence_reason")
        require(isinstance(reason, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", reason),
                "explicit_silence_required")
        disposition["disposition"] = "caption_only_cutoff" if after_primary else "caption_only_standby"
        result["silence_disposition"] = {
            "schema": SILENCE_SCHEMA, "reason": reason, "sample_count": timing["sample_count"],
            "disposition": disposition["disposition"], "publication_authorized": False,
            **{key: job[key] for key in ("voice_domain", "fps", "start_frame", "end_frame")},
        }
        timing.update({"timing_state": "explicit_silence", "wav_sha256": None, "pcm_sha256": None,
                       "cues": [{**cue, "window_start_sample": left, "window_end_sample": right}
                                for cue, left, right in cue_windows(job)],
                       "silences": [[0, timing["sample_count"]]]})
        return result

    require(mode == "voiced" and manifest.get("silence_reason") is None, "invalid_manifest")
    require(args.wav is not None and manifest.get("wav_sha256") is not None, "unmanifested_audio")
    origin = manifest.get("origin")
    require(origin == "operator_recording" or (origin == "generic_fixture" and args.allow_fixture),
            "substituted_voice_source")
    require(args.consent is not None and manifest.get("consent_sha256") is not None,
            "inferred_consent_rejected")
    wav = pinned_bytes(args.wav, manifest["wav_sha256"], "unmanifested_audio",
                       limit=MAX_AUDIO_BYTES, private=True)
    reject_known_fixture(wav)
    consent_raw = pinned_bytes(args.consent, manifest["consent_sha256"], "consent_digest_mismatch",
                               private=True)
    timing, pcm = time_and_hash_wav(wav, job)
    reject_known_fixture(wav, pcm)
    route = verify_authority(parse_json(consent_raw), job, timing, origin)
    require(evaluated_at <= timestamp(job["cutoff_policy"]["final_at"]),
            "late_take_after_final_mux_lock")
    if after_primary:
        expected = manifest.get("late_decision_sha256")
        require(args.late_decision is not None and expected is not None, "late_take_without_fresh_decision")
        late_raw = pinned_bytes(args.late_decision, expected, "late_take_without_fresh_decision", private=True)
        late = parse_json(late_raw)
        authority = "fixture" if origin == "generic_fixture" else "operator"
        require(late.get("schema") == LATE_DECISION_SCHEMA and late.get("authority") == authority
                and late.get("approved") is True
                and parse_pin(late.get("wav_sha256")) == sha(wav)
                and parse_pin(late.get("job_sha256")) == sha(job_raw), "late_take_without_fresh_decision")
        disposition["late_decision_sha256"] = pin(late_raw)
    else:
        require(args.late_decision is None and manifest.get("late_decision_sha256") is None,
                "unexpected_late_decision")

    fixture = origin == "generic_fixture"
    disposition.update({"disposition": "authorized_fixture_playback" if fixture else "authorized_voiced",
                        "route": route, "origin": origin, "is_late_take": after_primary,
                        "consent_sha256": pin(consent_raw), "publication_authorized": False})
    timing["timing_state"] = "bound_to_fixture_wav" if fixture else "bound_to_authorized_wav"
    result["timing"] = timing
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("decide", "verify"):
        command = sub.add_parser(name)
        for arg in ("job", "script", "manifest"):
            command.add_argument("--" + arg, required=True, type=Path)
        for arg in ("wav", "consent", "late-decision"):
            command.add_argument("--" + arg, type=Path)
        command.add_argument("--manifest-sha256", required=True, help="pin from the trusted local caller")
        command.add_argument("--allow-fixture", action="store_true")
        if name == "verify":
            command.add_argument("--receipt", type=Path, required=True)
            command.add_argument("--receipt-sha256", required=True, help="pin from the original decision")
    args = parser.parse_args(argv)
    try:
        if args.command == "decide":
            result = evaluate(args, datetime.now(timezone.utc))
        else:
            raw = pinned_bytes(args.receipt, args.receipt_sha256, "receipt_digest_mismatch")
            previous = parse_json(raw)
            result = evaluate(args, timestamp(previous.get("evaluated_at")))
            require(raw == canonical_json_bytes(result), "receipt_mismatch")
            result = {"status": "VERIFIED", "receipt_sha256": pin(raw)}
    except VoiceRefusal as exc:
        print(json.dumps({"status": "REFUSED", "code": exc.code, "detail": exc.detail}), file=sys.stderr)
        return 5
    except OSError as exc:
        print(json.dumps({"status": "ERROR", "code": "input_io_error", "errno": exc.errno}), file=sys.stderr)
        return 2
    sys.stdout.buffer.write(canonical_json_bytes(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
