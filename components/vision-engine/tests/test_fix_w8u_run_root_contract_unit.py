"""Golden HELLO body admission and fail-closed direct-child run-root regression.

The fixed fields and SHA-256 below are from OpenSuperficies
testdata/w8u/seatstream_vectors.json at
fbf329c14e3e1d9584b52aa716ec9dcbc236288e. Authorities here are injected test
fixtures, not claims of production contract or grant authentication.
"""

from dataclasses import asdict, replace
import hashlib
import json
import unittest
from unittest.mock import Mock, patch

from vadapt import w8u_binding as wb, w8u_credits as wc
from tests.test_w8u_ve_credits_unit import make_accepted_contract


ROOT = "/dev/shm/vqa-ramws"
CAMPAIGN_ROOT = ROOT + "/opensuperficies/os-cu-0"
RUN_ID = "w8u-vec-0001"
RUN_ROOT = CAMPAIGN_ROOT + "/" + RUN_ID
GOLDEN_SHA256 = "5379903b32258ef3fec684578622b9b2c6d2da22b1ed8565d07905f850f7ef08"
TEST_REVISION = "a" * 40


def golden_fields():
    """Fixed trusted test facts, independent of callbacks' incoming arguments."""
    return wb.BindingFields(
        schema="w8u-binding/1",
        run_id=RUN_ID,
        lease_id="lease-w8u-vec-0001",
        seat_grant_sha256="5e011f3184c052cf05798d69f99d227b23dafcaf036d5ae55061acf629ef2e54",
        container_id="2aa071a664f39ec95d37f089cc810470fc10c16a70636eb9ded9ba6f5f50e2b2",
        target_handle=wb.TargetIdentity("capsule", "os-cu-0/app", 1),
        epoch=3,
        display=":100",
        xid=2097153,
        width=4,
        height=3,
        frames_root=ROOT,
        frame_run_root=RUN_ROOT,
        image_sha256="0ba33fe0368356430a7667a4d1363f8e3ff7b8f68b1b39d171c6af04a99bfc2f",
        reader_sha256="0216f7cc34e15f0cec4f694c1c73261032c4a84225975678272748bf6f2df8f1",
        exec_allowlist_sha256="af3603acc097bf500c504a52d7aece2009afaa55e33ea920186b6e4115303677",
        max_record_bytes=2097392,
        max_inflight_frames=2,
        tick_mode="unavailable",
    )


def canonical_hello(fields):
    return json.dumps(asdict(fields), ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def binding_authorities(fields):
    """Keep explicit authorities consistent so malformed paths reach root checks."""
    f = fields
    contract = wb.ContractProof(f.schema, True, TEST_REVISION, "b" * 64)
    grant = wb.GrantProof(
        True, True, TEST_REVISION, f,
        f.frames_root, f.frames_root, f.frames_root,
        f.frame_run_root, f.frame_run_root, f.frame_run_root, True,
    )
    channel = wb.FrameChannelProof(
        True, True, TEST_REVISION, hashlib.sha256(canonical_hello(f)).hexdigest(),
        f.target_handle, f.epoch, f.display, f.xid, f.width, f.height,
        f.frames_root, f.frame_run_root, f.image_sha256, f.reader_sha256,
        f.exec_allowlist_sha256, f.max_record_bytes, f.max_inflight_frames,
        f.max_record_bytes * f.max_inflight_frames, f.tick_mode, True,
    )
    current = wb.CurrentTarget(
        True, TEST_REVISION, f.target_handle, f.epoch, f.display,
        f.xid, f.width, f.height,
    )
    return dict(
        contract_verifier=Mock(return_value=contract),
        grant_verifier=Mock(return_value=grant),
        channel_verifier=Mock(return_value=channel),
        target_snapshot=Mock(return_value=current),
    )


def credit_controller(fields):
    trusted_claims = asdict(fields)
    authenticator = Mock(side_effect=lambda candidate: candidate == trusted_claims)
    contract = replace(make_accepted_contract(), binding_schema_id=fields.schema)
    controller = wc.W8UCreditController(
        clock=wc.InjectedClock(), contract=contract,
        grant_authenticator=authenticator,
    )
    return controller, authenticator


class RunRootContractTests(unittest.TestCase):
    def assert_both_refuse(self, fields, *, binding_code="E_ROOT", credit_code="E_ROOT"):
        raw = canonical_hello(fields)
        with self.assertRaises(wb.BindingRefusal) as binding_error:
            wb.admit_hello(raw, **binding_authorities(fields))
        self.assertEqual(binding_error.exception.code, binding_code)

        controller, authenticator = credit_controller(fields)
        with self.assertRaises(wc.W8UCreditError) as credit_error:
            controller.receive_hello(raw)
        self.assertEqual(credit_error.exception.code, credit_code)
        authenticator.assert_not_called()
        self.assertFalse(controller.hello_received)
        self.assertFalse(controller.hello_confirmed)
        self.assertIsNone(controller.confirmed_binding)
        self.assertFalse(controller.capacity_reserved)
        self.assertIsNone(controller.bank)
        self.assertEqual(controller.cumulative_credit_total, 0)
        self.assertEqual(controller.spent_total, 0)
        self.assertEqual(controller.status, "idle")

    def test_same_golden_hello_body_passes_binding_and_credit_admission(self):
        fields = golden_fields()
        raw = canonical_hello(fields)
        self.assertEqual(len(raw), 835)
        self.assertEqual(hashlib.sha256(raw).hexdigest(), GOLDEN_SHA256)

        admitted = wb.admit_hello(raw, **binding_authorities(fields))
        controller, authenticator = credit_controller(fields)
        confirmed = controller.receive_hello(
            raw, expected_target=fields.target_handle, expected_epoch=fields.epoch,
            expected_geometry=(fields.width, fields.height),
        )
        self.assertEqual(admitted.canonical_json, raw)
        self.assertEqual(confirmed.canonical_bytes, raw)
        self.assertEqual(admitted.fields, fields)
        self.assertEqual(confirmed.run_id, RUN_ID)
        self.assertEqual(confirmed.frames_root, ROOT)
        self.assertEqual(confirmed.frame_run_root, RUN_ROOT)
        authenticator.assert_called_once_with(asdict(fields))
        self.assertTrue(controller.hello_confirmed)
        self.assertEqual(controller.available_credits, 0)
        self.assertEqual(controller.spent_total, 0)
        self.assertFalse(controller.capacity_reserved)
        self.assertIsNone(controller.bank)
        self.assertEqual(admitted.runtime_state, "HOLD")
        self.assertFalse(admitted.worker_ui)

    def test_old_extra_run_level_must_fail(self):
        self.assert_both_refuse(replace(golden_fields(), frame_run_root=CAMPAIGN_ROOT + "/run/" + RUN_ID))

    def test_nested_child_must_fail(self):
        self.assert_both_refuse(replace(golden_fields(), frame_run_root=RUN_ROOT + "/nested"))

    def test_slash_in_run_id_must_fail_even_with_matching_path(self):
        self.assert_both_refuse(
            replace(golden_fields(), run_id="nested/" + RUN_ID,
                    frame_run_root=CAMPAIGN_ROOT + "/nested/" + RUN_ID),
            binding_code="E_BINDING",
        )

    def test_sibling_campaign_must_fail(self):
        self.assert_both_refuse(replace(
            golden_fields(), frame_run_root=ROOT + "/opensuperficies/os-cu-1/" + RUN_ID,
        ))

    def test_symlinked_root_campaign_or_run_must_fail(self):
        for linked_path in (ROOT, ROOT + "/opensuperficies", CAMPAIGN_ROOT, RUN_ROOT):
            with self.subTest(linked_path=linked_path), patch(
                "os.path.islink", side_effect=lambda path: path == linked_path,
            ):
                self.assert_both_refuse(golden_fields())

    def test_mismatched_direct_child_must_fail(self):
        self.assert_both_refuse(replace(golden_fields(), frame_run_root=CAMPAIGN_ROOT + "/other-run"))

    def test_empty_dot_dotdot_and_nul_children_must_fail(self):
        for child in ("", ".", "..", "bad\x00child"):
            with self.subTest(child=child):
                self.assert_both_refuse(
                    replace(golden_fields(), frame_run_root=CAMPAIGN_ROOT + "/" + child),
                    binding_code="E_BINDING" if "\x00" in child else "E_ROOT",
                    credit_code="E_BINDING" if "\x00" in child else "E_ROOT",
                )

    def test_literal_run_is_valid_when_it_is_the_run_id(self):
        fields = replace(golden_fields(), run_id="run", frame_run_root=CAMPAIGN_ROOT + "/run")
        raw = canonical_hello(fields)
        self.assertEqual(wb.admit_hello(raw, **binding_authorities(fields)).fields.run_id, "run")
        controller, _ = credit_controller(fields)
        self.assertEqual(controller.receive_hello(raw).run_id, "run")
        self.assertEqual(controller.available_credits, 0)


if __name__ == "__main__":
    unittest.main()
