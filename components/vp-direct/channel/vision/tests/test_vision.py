from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path
import sys
from types import ModuleType
import unittest
from unittest.mock import Mock


SURFACE = Path(__file__).resolve().parents[1]
VP_DIRECT = SURFACE.parents[1]
sys.path[:0] = [str(VP_DIRECT.parents[1] / "agents"), str(VP_DIRECT)]

from channel import vision
from jsonschema import ValidationError
from schemas.validation import validate_contract


EXAMPLES = VP_DIRECT / "schemas" / "tests" / "examples"
FRAME = {"width": 8, "height": 6, "sha256": sha256(bytes(range(8 * 6 * 3))).hexdigest()}
REPLY = {"schema": "vp.channel.reply/v1", "ok": True, "code": "OK"}
PRIMARY_CODE = "SNAP_CDP_UNAVAILABLE"


def example(name):
    return json.loads((EXAMPLES / (name + ".valid.json")).read_text(encoding="utf-8"))


def payload(**overrides):
    description = {
        "schema": "ve.cu_describe/v1",
        "width": FRAME["width"],
        "height": FRAME["height"],
        "frame_sha256": FRAME["sha256"],
        "changed": True,
        "changed_regions": [{"x": 1, "y": 2, "w": 4, "h": 3, "score": 1.0}],
        "crops": [{"x": 1, "y": 2, "w": 4, "h": 3, "reason": "changed"}],
        "timing_us": 42,
    }
    return json.dumps(dict(description, **overrides)).encode("utf-8")


def mutant(old, new):
    path = SURFACE / "__init__.py"
    source = path.read_text(encoding="utf-8")
    if source.count(old) != 1:
        raise AssertionError("Expected a unique mutation site")
    module = ModuleType("_u1d_mutant")
    sys.modules[module.__name__] = module
    try:
        exec(compile(source.replace(old, new), str(path), "exec"), vars(module))
    finally:
        del sys.modules[module.__name__]
    return module


class VisionTests(unittest.TestCase):
    def setUp(self):
        self.request = dict(example("vp.channel.req_v1"), op="act", action="click", dry_run=False)
        self.session = dict(example("vp.session_v1"), display="xpra:47")
        self.observation = vision.BoundDescription(
            self.request["session_id"], self.request["incarnation"], self.request["authority_epoch"],
            self.request["observation_id"], FRAME, payload(), True,
        )
        self.dispatch = Mock(return_value=REPLY)

    def run_fallback(self, module=vision, *, routes=(("cdp", PRIMARY_CODE),), **overrides):
        arguments = dict(session=self.session, observation=self.observation, point=(2, 3),
                         primary_code=PRIMARY_CODE, primary_sent=False)
        fallback = module.VisionFallback(routes=routes, dispatch=self.dispatch)
        return fallback.run(self.request, **(arguments | overrides))

    def assert_origin(self, module):
        reply = self.run_fallback(module)
        validate_contract("vp.channel.reply/v1", reply)
        self.assertEqual(reply, REPLY | {"fallback_from": "cdp"})
        self.assertNotIn("frame_sha256", reply)

    def assert_region_geometry(self, module):
        result = module.verify_description(payload(), frame_ref=FRAME, previous_available=True)
        self.assertEqual(result.regions, ((1, 2, 4, 3),))
        self.assertEqual(result.frame_sha256, FRAME["sha256"])
        self.assertIs(result.changed, True)

    def assert_host_refusal(self, module):
        self.session["display"] = ":0"
        self.assertEqual(self.run_fallback(module)["code"], "DISPLAY_UNSUPPORTED")
        self.dispatch.assert_not_called()

    def test_declared_fallback_sets_origin(self):
        self.assert_origin(vision)
        self.dispatch.assert_called_once()

    def test_private_display_dispatch_uses_bound_geometry(self):
        self.run_fallback()
        self.dispatch.assert_called_once()
        display, request, point, verified = self.dispatch.call_args.args
        self.assertEqual((display, request, point), ("xpra:47", self.request, (2, 3)))
        self.assertEqual((verified.width, verified.height), (8, 6))
        self.assertEqual(verified.frame_sha256, FRAME["sha256"])

    def test_atspi_origin_is_preserved_on_executor_refusal(self):
        self.request["backend"] = "atspi"
        self.dispatch.return_value = {"schema": "vp.channel.reply/v1", "ok": False, "code": "NO_LEASE"}
        reply = self.run_fallback(routes=(("atspi", PRIMARY_CODE),))
        self.assertEqual(reply, self.dispatch.return_value | {"fallback_from": "atspi"})

    def test_host_display_is_refused(self):
        self.assert_host_refusal(vision)

    def test_no_fallback_on_identity_mismatch(self):
        routes = (("cdp", "identity_mismatch"),)
        self.assertEqual(self.run_fallback(routes=routes, primary_code="identity_mismatch")["code"],
                         "identity_mismatch")
        self.request["target"] = {"cdp_target_id": "tab-other"}
        self.assertEqual(self.run_fallback()["code"], "identity_mismatch")
        self.dispatch.assert_not_called()

    def test_enviar_and_higher_are_refused(self):
        for risk in ("enviar", "publicar", "destructivo", "irreversible"):
            with self.subTest(risk=risk):
                self.request["risk_class"] = risk
                self.assertEqual(self.run_fallback()["code"], "FALLBACK_FORBIDDEN_IRREVERSIBLE")
        self.dispatch.assert_not_called()

    def test_region_score_is_never_confidence(self):
        self.assert_region_geometry(vision)

    def test_wrong_version_digest_and_geometry_are_refused(self):
        cases = (
            (payload(schema="ve.cu_describe/v2"), "unknown_version"),
            (payload(frame_sha256="a" * 64), "identity_mismatch"),
            (payload(width=FRAME["width"] + 1), "bad_shape"),
            (payload(changed_regions=[{"x": 8, "y": 0, "w": 1, "h": 1, "score": 1.0}]), "bad_shape"),
            (payload(changed=False), "bad_shape"),
        )
        for description, code in cases:
            with self.subTest(code=code):
                observation = replace(self.observation, payload=description)
                self.assertEqual(self.run_fallback(observation=observation)["code"], code)
        self.dispatch.assert_not_called()

    def test_missing_previous_frame_is_not_stability(self):
        first = payload(changed=None, changed_regions=[], crops=[])
        evidence = vision.verify_description(first, frame_ref=FRAME, previous_available=False)
        self.assertIsNone(evidence.changed)
        with self.assertRaises(vision.PerceptionError):
            vision.verify_description(first, frame_ref=FRAME, previous_available=True)

    def test_undeclared_route_preserves_primary_refusal(self):
        reply = self.run_fallback(routes=(("atspi", PRIMARY_CODE),))
        self.assertEqual(reply, {"schema": "vp.channel.reply/v1", "ok": False, "code": PRIMARY_CODE})
        self.dispatch.assert_not_called()

    def test_possibly_sent_primary_cannot_fall_back(self):
        for sent in (True, None):
            with self.subTest(sent=sent):
                reply = self.run_fallback(primary_sent=sent)
                self.assertEqual(reply["code"], "UNCERTAIN_OUTCOME")
                self.assertNotIn("fallback_from", reply)
        self.dispatch.assert_not_called()

    def test_stale_observation_or_human_lease_cannot_dispatch(self):
        observation = replace(self.observation, observation_id="o-old")
        self.assertEqual(self.run_fallback(observation=observation)["code"], "STALE_FRAME")
        self.session["state"] = "HUMAN"
        self.session["lease"]["holder"] = None
        self.assertEqual(self.run_fallback()["code"], "NO_LEASE")
        self.dispatch.assert_not_called()

    def test_timeout_carries_fallback_origin_without_retry(self):
        self.dispatch.side_effect = TimeoutError
        reply = self.run_fallback()
        self.assertEqual((reply["code"], reply["fallback_from"]), ("UNCERTAIN_OUTCOME", "cdp"))
        self.dispatch.assert_called_once()

    def test_malformed_executor_receipt_does_not_trigger_retry(self):
        self.dispatch.return_value = {}
        reply = self.run_fallback()
        self.assertEqual((reply["code"], reply["fallback_from"]), ("UNCERTAIN_OUTCOME", "cdp"))
        self.dispatch.assert_called_once()

    def test_dry_run_and_invalid_point_do_not_dispatch(self):
        self.request["dry_run"] = True
        self.assert_origin(vision)
        self.request["dry_run"] = False
        self.assertEqual(self.run_fallback(point=(FRAME["width"], 0))["code"], "bad_shape")
        self.dispatch.assert_not_called()

    def test_coordinate_claim_in_wire_request_is_rejected(self):
        self.request["point"] = [2, 3]
        with self.assertRaises(ValidationError):
            self.run_fallback()
        self.dispatch.assert_not_called()

    def test_mutant_silent_fallback_is_killed(self):
        variant = mutant("return dict(reply, fallback_from=backend)", "return dict(reply)")
        with self.assertRaises(AssertionError):
            self.assert_origin(variant)
        self.dispatch.assert_called_once()

    def test_mutant_score_as_confidence_is_killed(self):
        variant = mutant(
            'tuple((row["x"], row["y"], row["w"], row["h"]) for row in parsed["regions"])',
            'tuple(dict(row, confidence=row["score"]) for row in parsed["regions"])',
        )
        with self.assertRaises(AssertionError):
            self.assert_region_geometry(variant)

    def test_mutant_host_coordinates_are_killed(self):
        variant = mutant('if not re.fullmatch(r"xpra:[0-9]+", display):', 'if False:')
        with self.assertRaises(AssertionError):
            self.assert_host_refusal(variant)
        self.dispatch.assert_called_once()
        self.assertEqual(self.dispatch.call_args.args[0], ":0")
