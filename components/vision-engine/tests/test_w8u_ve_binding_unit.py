"""Offline public-path admission controls, no live transport/capsule/X server."""

from dataclasses import FrozenInstanceError, replace
import hashlib
import json
import unittest
from unittest.mock import patch

from vadapt.w8u_binding import (
    AdmittedBinding, BindingFields, BindingRefusal, CommitCoordinationProof,
    ContractProof, CurrentTarget, FrameChannelProof, GrantProof,
    TargetIdentity, admit_hello,
)


REV = "a" * 40
CONTRACT_SHA = "b" * 64
ROOT = "/dev/shm/vqa-ramws"
RUNROOT = ROOT + "/opensuperficies/os-cu-0/run-17"
TGT = TargetIdentity("x11_window", "cap:window-7", 4)
IMAGE, READER, ALLOWLIST, SEAT = ("c" * 64, "d" * 64, "e" * 64, "f" * 64)


def descriptor():
    return dict(schema="test-only.binding.schema/v1",
                run_id="run-17", lease_id="lease-17", seat_grant_sha256=SEAT,
                container_id="container-abc", target_handle=dict(kind="x11_window",
                  canonical_id="cap:window-7", generation=4), epoch=11,
                display=":91", xid=371, width=16, height=12,
                frames_root=ROOT, frame_run_root=RUNROOT,
                image_sha256=IMAGE, reader_sha256=READER,
                exec_allowlist_sha256=ALLOWLIST,
                max_record_bytes=4096, max_inflight_frames=2,
                tick_mode="unavailable")


def expected_fields():
    # Independent fixed, trusted source of grant claims, not a copy of HELLO.
    return BindingFields(
        "test-only.binding.schema/v1", "run-17", "lease-17",
        SEAT, "container-abc", TGT, 11, ":91", 371, 16, 12, ROOT, RUNROOT,
        IMAGE, READER, ALLOWLIST, 4096, 2, "unavailable")


def canonical(data):
    return json.dumps(data, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def positive_authorities(*, fields=None, contract=None, grant=None,
                         channel=None, current=None, commit=None, calls=None):
    f = fields if fields is not None else expected_fields()
    digest = hashlib.sha256(canonical(descriptor())).hexdigest()
    cp = contract if contract is not None else ContractProof(f.schema, True, REV, CONTRACT_SHA)
    gp = grant if grant is not None else GrantProof(
        True, True, REV, f, ROOT, ROOT, ROOT, RUNROOT, RUNROOT, RUNROOT, True)
    ch = channel if channel is not None else FrameChannelProof(
        True, True, REV, digest, TGT, 11, ":91", 371, 16, 12, ROOT, RUNROOT,
        IMAGE, READER, ALLOWLIST, 4096, 2, 8192, "unavailable", True)
    ct = current if current is not None else CurrentTarget(
        True, REV, TGT, 11, ":91", 371, 16, 12)
    mt = commit if commit is not None else CommitCoordinationProof(
        True, True, REV, 0, 0)
    trace = calls if calls is not None else []

    def authority(name, output):
        def cb(*args):
            trace.append(name)
            return output
        return cb

    return dict(contract_verifier=authority("contract", cp),
                grant_verifier=authority("grant", gp),
                channel_verifier=authority("channel", ch),
                target_snapshot=authority("current", ct),
                commit_verifier=authority("commit", mt))


class BindingAdmissionTests(unittest.TestCase):
    def reject(self, data=None, *, code="E_BINDING", authorities=None, mutate=None):
        incoming = descriptor() if data is None else data
        if mutate:
            mutate(incoming)
        calls = []
        kw = authorities if authorities is not None else positive_authorities(calls=calls)
        with self.assertRaises(BindingRefusal) as e:
            admit_hello(incoming, **kw)
        self.assertEqual(e.exception.code, code)
        return e.exception

    def test_01_authenticates_complete_immutable_hello(self):
        calls = []
        kw = positive_authorities(calls=calls)
        incoming = descriptor()
        result = admit_hello(incoming, **kw)
        self.assertIs(type(result), AdmittedBinding)
        self.assertEqual(result.fields, expected_fields())
        self.assertEqual(result.binding_sha256, hashlib.sha256(canonical(descriptor())).hexdigest())
        self.assertEqual(result.canonical_json, canonical(descriptor()))
        self.assertEqual(calls, ["contract", "grant", "channel", "current"])
        self.assertEqual(result.runtime_state, "HOLD")
        self.assertIs(result.worker_ui, False)
        incoming["target_handle"]["generation"] = 9999
        incoming["frames_root"] = "/dev/shm/w8/frames"
        self.assertEqual(result.fields.target_handle, TGT)
        self.assertEqual(result.fields.frames_root, ROOT)
        with self.assertRaises(FrozenInstanceError):
            result.fields.target_handle.generation = 78
        with self.assertRaises(FrozenInstanceError):
            result.fields.width = 40

    def test_02_exact_canonical_bytes_positive(self):
        raw = canonical(descriptor())
        self.assertEqual(admit_hello(raw, **positive_authorities()).canonical_json, raw)
        self.assertEqual(admit_hello(bytearray(raw), **positive_authorities()).canonical_json, raw)

    def test_03_unavailable_mode_does_not_claim_committed_tick(self):
        kw = positive_authorities()
        kw.pop("commit_verifier")
        result = admit_hello(canonical(descriptor()), **kw)
        self.assertEqual(result.fields.tick_mode, "unavailable")
        self.assertFalse(result.worker_ui)

    def test_04_missing_authentication_refuses_same_hash(self):
        gp = replace(positive_grant(), authenticated=False)
        self.reject(authorities=positive_authorities(grant=gp))

    def test_05_missing_grant_measurement_refuses(self):
        gp = replace(positive_grant(), measured=False)
        self.reject(authorities=positive_authorities(grant=gp))

    def test_06_missing_channel_authentication_refuses(self):
        ch = replace(positive_channel(), authenticated=False)
        self.reject(authorities=positive_authorities(channel=ch))

    def test_07_missing_channel_measurement_refuses(self):
        ch = replace(positive_channel(), measured=False)
        self.reject(authorities=positive_authorities(channel=ch))

    def test_08_missing_target_measurement_refuses(self):
        ct = replace(positive_current(), measured=False)
        self.reject(authorities=positive_authorities(current=ct))

    def test_09_contract_acceptance_required(self):
        cp = replace(positive_contract(), accepted=False)
        self.reject(authorities=positive_authorities(contract=cp))

    def test_10_literal_schema_must_match(self):
        self.reject(mutate=lambda d: d.__setitem__("schema", "different"))

    def test_11_extra_json_keys_rejected(self):
        self.reject(mutate=lambda d: d.__setitem__("overreach", 3))

    def test_12_missing_json_keys_rejected(self):
        self.reject(mutate=lambda d: d.pop("max_record_bytes"))

    def test_13_nested_extra_keys_rejected(self):
        self.reject(mutate=lambda d: d["target_handle"].__setitem__("mutable", "y"))

    def test_14_duplicate_json_key_rejected(self):
        raw = canonical(descriptor())
        raw = raw[:-1] + b',"width":16}'
        self.reject(raw)

    def test_15_unsorted_json_rejected(self):
        self.reject(json.dumps(descriptor()).encode())

    def test_16_non_utf8_rejected(self):
        self.reject(b"\xff")

    def test_17_float_integer_rejected(self):
        self.reject(mutate=lambda d: d.__setitem__("xid", 371.0))

    def test_18_bool_integer_rejected(self):
        self.reject(mutate=lambda d: d["target_handle"].__setitem__("generation", True))

    def test_19_unknown_tick_mode_rejected(self):
        self.reject(code="E_TICK", mutate=lambda d: d.__setitem__("tick_mode", "grab_equal"))

    def test_20_stale_generation_rejected(self):
        ct = replace(positive_current(), target_handle=TargetIdentity("x11_window", "cap:window-7", 3))
        self.reject(authorities=positive_authorities(current=ct))

    def test_21_stale_epoch_rejected(self):
        ct = replace(positive_current(), epoch=10)
        self.reject(authorities=positive_authorities(current=ct))

    def test_22_foreign_target_rejected(self):
        ct = replace(positive_current(), target_handle=TargetIdentity("x11_window", "foreign", 4))
        self.reject(authorities=positive_authorities(current=ct))

    def test_23_private_display_and_xid_required(self):
        ch = replace(positive_channel(), private_display=False)
        self.reject(authorities=positive_authorities(channel=ch))
        self.reject(authorities=positive_authorities(current=replace(positive_current(), xid=777)), code="E_GEOMETRY")

    def test_24_frame_digest_mismatch_rejected(self):
        ch = replace(positive_channel(), binding_sha256="0" * 64)
        self.reject(authorities=positive_authorities(channel=ch))

    def test_25_reader_image_allowlist_pins(self):
        for field in ("reader_sha256", "image_sha256", "exec_allowlist_sha256"):
            with self.subTest(field=field):
                ch = replace(positive_channel(), **{field: "0" * 64})
                self.reject(authorities=positive_authorities(channel=ch))

    def test_26_old_root_rejected_even_with_matching_signed_grant(self):
        data = descriptor()
        data["frames_root"] = "/dev/shm/w8/frames"
        bad = replace(expected_fields(), frames_root=data["frames_root"])
        gp = replace(positive_grant(), claims=bad, grant_root=data["frames_root"],
                     observation_root=data["frames_root"], ingest_root=data["frames_root"])
        ch = replace(positive_channel(), frames_root=data["frames_root"],
                     binding_sha256=hashlib.sha256(canonical(data)).hexdigest())
        self.reject(data, code="E_ROOT", authorities=positive_authorities(grant=gp, channel=ch))

    def test_27_ingest_root_disagreement_rejected(self):
        gp = replace(positive_grant(), ingest_root="/dev/shm/w8/frames")
        self.reject(code="E_ROOT", authorities=positive_authorities(grant=gp))

    def test_28_observation_run_root_disagreement_rejected(self):
        gp = replace(positive_grant(), observation_run_root="/dev/shm/w8/frames")
        self.reject(code="E_ROOT", authorities=positive_authorities(grant=gp))

    def test_29_run_root_alias_rejected(self):
        data = descriptor()
        data["frame_run_root"] += "/.."
        altered = replace(expected_fields(), frame_run_root=data["frame_run_root"])
        gp = replace(positive_grant(), claims=altered, grant_run_root=data["frame_run_root"],
                     observation_run_root=data["frame_run_root"], ingest_run_root=data["frame_run_root"])
        ch = replace(positive_channel(), frame_run_root=data["frame_run_root"],
                     binding_sha256=hashlib.sha256(canonical(data)).hexdigest())
        self.reject(data, code="E_ROOT", authorities=positive_authorities(grant=gp, channel=ch))

    def test_30_symlink_root_refused(self):
        with patch("vadapt.w8u_binding.os.path.islink", side_effect=lambda p: p == ROOT):
            self.reject(code="E_ROOT")

    def test_31_geometry_and_caps_rejected(self):
        self.reject(mutate=lambda d: d.__setitem__("width", 0), code="E_GEOMETRY")
        self.reject(mutate=lambda d: d.__setitem__("height", 2 ** 40), code="E_GEOMETRY")

    def test_32_max_record_exceeds_geometry_cap(self):
        data = descriptor()
        data["max_record_bytes"] = 100000000
        f = replace(expected_fields(), max_record_bytes=100000000)
        gp = replace(positive_grant(), claims=f)
        ch = replace(positive_channel(), max_record_bytes=100000000,
                     binding_sha256=hashlib.sha256(canonical(data)).hexdigest(),
                     reserved_bytes=200000000)
        self.reject(data, code="E_LENGTH", authorities=positive_authorities(grant=gp, channel=ch))

    def test_33_single_frame_capacity_valid(self):
        data = descriptor()
        data["max_inflight_frames"] = 1
        f = replace(expected_fields(), max_inflight_frames=1)
        gp = replace(positive_grant(), claims=f)
        ch = replace(positive_channel(), reserved_slots=1, reserved_bytes=4096,
                     binding_sha256=hashlib.sha256(canonical(data)).hexdigest())
        admitted = admit_hello(data, **positive_authorities(grant=gp, channel=ch))
        self.assertEqual(admitted.fields.max_inflight_frames, 1)
        self.assertFalse(admitted.worker_ui)

    def test_34_capacity_inflation_rejected(self):
        ch = replace(positive_channel(), reserved_slots=1)
        self.reject(code="E_CREDIT", authorities=positive_authorities(channel=ch))

    def test_35_reserved_ram_insufficient_rejected(self):
        ch = replace(positive_channel(), reserved_bytes=4096)
        self.reject(code="E_RAM_QUOTA", authorities=positive_authorities(channel=ch))

    def test_36_committed_requires_independent_evidence(self):
        data = descriptor()
        data["tick_mode"] = "committed"
        f = replace(expected_fields(), tick_mode="committed")
        gp = replace(positive_grant(), claims=f)
        ch = replace(positive_channel(), tick_mode="committed",
                     binding_sha256=hashlib.sha256(canonical(data)).hexdigest())
        kw = positive_authorities(grant=gp, channel=ch)
        kw["commit_verifier"] = None
        self.reject(data, code="E_TICK", authorities=kw)

    def test_37_committed_zero_valid_if_authenticated(self):
        data = descriptor()
        data["tick_mode"] = "committed"
        f = replace(expected_fields(), tick_mode="committed")
        gp = replace(positive_grant(), claims=f)
        ch = replace(positive_channel(), tick_mode="committed",
                     binding_sha256=hashlib.sha256(canonical(data)).hexdigest())
        a = admit_hello(data, **positive_authorities(grant=gp, channel=ch))
        self.assertEqual(a.fields.tick_mode, "committed")
        self.assertEqual(a.runtime_state, "HOLD")

    def test_38_committed_grab_equality_is_not_evidence(self):
        data = descriptor()
        data["tick_mode"] = "committed"
        f = replace(expected_fields(), tick_mode="committed")
        gp = replace(positive_grant(), claims=f)
        ch = replace(positive_channel(), tick_mode="committed",
                     binding_sha256=hashlib.sha256(canonical(data)).hexdigest())
        kw = positive_authorities(grant=gp, channel=ch, commit=CommitCoordinationProof(True, False, REV, 9, 9))
        self.reject(data, code="E_TICK", authorities=kw)

    def test_39_committed_revision_disagreement_rejected(self):
        data = descriptor()
        data["tick_mode"] = "committed"
        f = replace(expected_fields(), tick_mode="committed")
        gp = replace(positive_grant(), claims=f)
        ch = replace(positive_channel(), tick_mode="committed",
                     binding_sha256=hashlib.sha256(canonical(data)).hexdigest())
        kw = positive_authorities(grant=gp, channel=ch, commit=CommitCoordinationProof(True, True, REV, 5, 6))
        self.reject(data, code="E_TICK", authorities=kw)

    def test_40_mutable_nested_channel_identity_rejected(self):
        ch = replace(positive_channel(), target_handle={"kind": "x11_window", "canonical_id": "cap:window-7", "generation": 4})
        self.reject(authorities=positive_authorities(channel=ch))

    def test_41_length_limit_rejects_before_verifiers(self):
        self.reject(b" " * 16385, code="E_LENGTH")

    def test_42_authority_exception_refused(self):
        kw = positive_authorities()
        def bad(*args):
            raise RuntimeError("unreachable source")
        kw["grant_verifier"] = bad
        self.reject(authorities=kw)

    def test_43_missing_channel_verifier_refused(self):
        kw = positive_authorities()
        kw["channel_verifier"] = None
        self.reject(authorities=kw)

    def test_44_torn_nested_caller_mutation_not_retained(self):
        original = descriptor()
        kw = positive_authorities()
        result = admit_hello(original, **kw)
        original["target_handle"]["canonical_id"] = "spoof"
        original["target_handle"]["generation"] = 800
        original["image_sha256"] = "0" * 64
        self.assertEqual(result.fields, expected_fields())
        self.assertEqual(result.canonical_json, canonical(descriptor()))

    def test_45_bool_channel_integer_rejected(self):
        ch = replace(positive_channel(), xid=True)
        self.reject(authorities=positive_authorities(channel=ch))

    def test_46_float_current_integer_rejected(self):
        ct = replace(positive_current(), xid=371.0)
        self.reject(authorities=positive_authorities(current=ct))

    def test_47_bool_grant_claim_integer_rejected(self):
        # Boolean is not accepted as generation 1 even when Python equals it.
        data = descriptor()
        data["epoch"] = 1
        f = replace(expected_fields(), epoch=1)
        bad = replace(f, epoch=True)
        gp = replace(positive_grant(), claims=bad)
        ch = replace(positive_channel(), epoch=1,
                     binding_sha256=hashlib.sha256(canonical(data)).hexdigest())
        ct = replace(positive_current(), epoch=1)
        self.reject(data, authorities=positive_authorities(grant=gp, channel=ch, current=ct))

    def test_48_zero_credit_capacity_rejected(self):
        self.reject(code="E_CREDIT", mutate=lambda d: d.__setitem__("max_inflight_frames", 0))

    def test_49_too_small_record_cap_rejected(self):
        self.reject(code="E_LENGTH", mutate=lambda d: d.__setitem__("max_record_bytes", 0))

    def test_50_schema_not_accepted_if_contract_unavailable(self):
        kw = positive_authorities()
        kw["contract_verifier"] = None
        self.reject(authorities=kw)

    def test_51_invalid_contract_revision_rejected(self):
        cp = replace(positive_contract(), revision_sha="z" * 40)
        self.reject(authorities=positive_authorities(contract=cp))

    def test_52_channel_mismatched_geometry_rejected(self):
        ch = replace(positive_channel(), height=11)
        self.reject(authorities=positive_authorities(channel=ch))

    def test_53_channel_exposes_no_mutable_identity(self):
        ch = replace(positive_channel(), target_handle=TargetIdentity("x11_window", ["cap:window-7"], 4))
        self.reject(authorities=positive_authorities(channel=ch))

    def test_54_reject_syntax_before_any_external_effects(self):
        called = []
        kw = positive_authorities(calls=called)
        raw = descriptor()
        raw["target_handle"]["generation"] = True
        with self.assertRaises(BindingRefusal):
            admit_hello(raw, **kw)
        self.assertEqual(called, [])



def positive_contract():
    return ContractProof(expected_fields().schema, True, REV, CONTRACT_SHA)


def positive_grant():
    return GrantProof(True, True, REV, expected_fields(),
                      ROOT, ROOT, ROOT, RUNROOT, RUNROOT, RUNROOT, True)


def positive_channel():
    return FrameChannelProof(True, True, REV,
                             hashlib.sha256(canonical(descriptor())).hexdigest(),
                             TGT, 11, ":91", 371, 16, 12, ROOT, RUNROOT,
                             IMAGE, READER, ALLOWLIST, 4096, 2, 8192, "unavailable", True)


def positive_current():
    return CurrentTarget(True, REV, TGT, 11, ":91", 371, 16, 12)


MANDATORY_CASES = [
    "tests.test_w8u_ve_binding_unit.BindingAdmissionTests." + name
    for name in (
        "test_01_authenticates_complete_immutable_hello",
        "test_02_exact_canonical_bytes_positive",
        "test_03_unavailable_mode_does_not_claim_committed_tick",
        "test_04_missing_authentication_refuses_same_hash",
        "test_05_missing_grant_measurement_refuses",
        "test_06_missing_channel_authentication_refuses",
        "test_07_missing_channel_measurement_refuses",
        "test_08_missing_target_measurement_refuses",
        "test_09_contract_acceptance_required",
        "test_10_literal_schema_must_match",
        "test_11_extra_json_keys_rejected",
        "test_12_missing_json_keys_rejected",
        "test_13_nested_extra_keys_rejected",
        "test_14_duplicate_json_key_rejected",
        "test_15_unsorted_json_rejected",
        "test_16_non_utf8_rejected",
        "test_17_float_integer_rejected",
        "test_18_bool_integer_rejected",
        "test_19_unknown_tick_mode_rejected",
        "test_20_stale_generation_rejected",
        "test_21_stale_epoch_rejected",
        "test_22_foreign_target_rejected",
        "test_23_private_display_and_xid_required",
        "test_24_frame_digest_mismatch_rejected",
        "test_25_reader_image_allowlist_pins",
        "test_26_old_root_rejected_even_with_matching_signed_grant",
        "test_27_ingest_root_disagreement_rejected",
        "test_28_observation_run_root_disagreement_rejected",
        "test_29_run_root_alias_rejected",
        "test_30_symlink_root_refused",
        "test_31_geometry_and_caps_rejected",
        "test_32_max_record_exceeds_geometry_cap",
        "test_33_single_frame_capacity_valid",
        "test_34_capacity_inflation_rejected",
        "test_35_reserved_ram_insufficient_rejected",
        "test_36_committed_requires_independent_evidence",
        "test_37_committed_zero_valid_if_authenticated",
        "test_38_committed_grab_equality_is_not_evidence",
        "test_39_committed_revision_disagreement_rejected",
        "test_40_mutable_nested_channel_identity_rejected",
        "test_41_length_limit_rejects_before_verifiers",
        "test_42_authority_exception_refused",
        "test_43_missing_channel_verifier_refused",
        "test_44_torn_nested_caller_mutation_not_retained",
        "test_45_bool_channel_integer_rejected",
        "test_46_float_current_integer_rejected",
        "test_47_bool_grant_claim_integer_rejected",
        "test_48_zero_credit_capacity_rejected",
        "test_49_too_small_record_cap_rejected",
        "test_50_schema_not_accepted_if_contract_unavailable",
        "test_51_invalid_contract_revision_rejected",
        "test_52_channel_mismatched_geometry_rejected",
        "test_53_channel_exposes_no_mutable_identity",
        "test_54_reject_syntax_before_any_external_effects",
    )
]


if __name__ == "__main__":
    unittest.main()
