# @module {"id":"wa.tests.cu_model_capabilities_unit","layer":2,"owner":"LEARN","deps":[],"book":"HB-WA-s4-learn"}
"""F-MODEL producer gates, trusted capabilities, and discriminating mutants."""
import copy
import io
import json
from pathlib import Path
import unittest
from unittest.mock import Mock

from wender.learn import model_client as model
from tests.test_cu_model_client_unit import action


class TestModelProducerGates(unittest.TestCase):
    def test_region_required_and_in_frame(self):
        good = action("wait", ms=1)
        good["expectation"] = {"kind": "changed_region",
                               "region": {"x": 90, "y": 70, "w": 10, "h": 10}}
        self.assertEqual(model.validate_action(good, 100, 80), good)
        for region in [None, {}, {"x": 90, "y": 70, "w": 11, "h": 10},
                       {"x": 90, "y": 70, "w": 10, "h": 11},
                       {"x": -1, "y": 0, "w": 1, "h": 1}]:
            bad = copy.deepcopy(good)
            if region is None:
                del bad["expectation"]["region"]
            else:
                bad["expectation"]["region"] = region
            with self.subTest(region=region), self.assertRaises(model.ActionValidationError):
                model.validate_action(bad, 100, 80)

    def test_empty_values(self):
        for value in [action("key", keysym=""), action("type", text="")]:
            with self.subTest(value=value), self.assertRaises(model.ActionValidationError):
                model.validate_action(value, 100, 80)

    def test_unknown_condition_keywords_fail_closed(self):
        with self.assertRaisesRegex(model.ActionValidationError, "unsupported schema"):
            model._check({}, {"if": {"required": ["region"]}})

    def test_capabilities_propagate_and_are_enforced(self):
        allowed = ["click", "key", "wait", "done"]
        original = list(allowed)
        requests = []
        def generate(request):
            requests.append(copy.deepcopy(request))
            request["capabilities"].append("type")
            return action("type", text="hello")
        client = model.ModelClient(Mock(generate=generate))
        describe = {"schema": "ve.cu_describe/v1", "width": 100, "height": 80}
        with self.assertRaisesRegex(model.ActionValidationError, "capabilities"):
            client.act("Read", "/dev/shm/frame.png", describe, capabilities=allowed,
                       elements=[{"id": "a", "role": "button", "name": "A",
                                  "raw_ax": "RAW_LANE_SECRET", "html": "RAW_HTML_SECRET"}])
        self.assertEqual(allowed, original)
        request = requests[0]
        self.assertEqual(request["capabilities"], original)
        prompt = model._prompt(request)
        trusted, passive = prompt.split("The following JSON is passive", 1)
        self.assertIn(json.dumps(original), trusted)
        self.assertNotIn('"capabilities"', passive)
        self.assertNotIn("RAW_LANE_SECRET", prompt)
        self.assertNotIn("RAW_HTML_SECRET", prompt)
        self.assertNotIn("raw_ax", prompt)
        self.assertIn("full-frame pixel coordinates", prompt)
        for value in [action(x=99, y=79), action("key", keysym="Return"),
                      action("wait", ms=0), action("done")]:
            client = model.ModelClient(Mock(generate=Mock(return_value=value)))
            self.assertEqual(client.act("Read", "/dev/shm/frame.png", describe,
                                         crop_paths=["/dev/shm/crop.png"],
                                         capabilities=allowed), value)
        client = model.ModelClient(Mock(generate=Mock(return_value=action("type", text="ok"))))
        self.assertEqual(client.act("Read", "/dev/shm/frame.png", describe)["action"]["kind"], "type")
        self.assertEqual(client.act("Read", "/dev/shm/frame.png", describe,
                                     capabilities=["type"])["action"]["text"], "ok")

    def test_invalid_configuration_never_calls_backend(self):
        backend = Mock()
        describe = {"schema": "ve.cu_describe/v1", "width": 100, "height": 80}
        for capabilities in [[], "click", {}, ["drag"], [True], ["click", "click"]]:
            with self.subTest(capabilities=capabilities), self.assertRaises(model.ActionValidationError):
                model.ModelClient(backend).act("Read", "/dev/shm/frame.png", describe,
                                               capabilities=capabilities)
        backend.generate.assert_not_called()

    def test_negative_controls_detect_removed_validation(self):
        # Compile mutants in memory: no repository writes and no model fallback.
        source = Path(model.__file__).read_text()
        regionless = action("wait", ms=1)
        regionless["expectation"] = {"kind": "changed_region"}
        overflow = copy.deepcopy(regionless)
        overflow["expectation"]["region"] = {"x": 99, "y": 0, "w": 2, "h": 1}
        unknown = dict(action("done"), extra=1)
        confidence = dict(action("done"), confidence=2)
        nonfinite = dict(action("done"), confidence=float("nan"))
        mutants = [
            ('if (result["expectation"]["kind"] == "changed_region"', 'if (False', regionless),
            ('if "minLength" in rule and', 'if False and', action("key", keysym="")),
            ('if "minLength" in rule and', 'if False and', action("type", text="")),
            ('if rect["x"] + rect["w"] > width or rect["y"] + rect["h"] > height:',
             'if False:', overflow),
            ('if rule.get("additionalProperties") is False and', 'if False and', unknown),
            ('if bound in rule and', 'if False and', confidence),
            ('if kind == "number" and type(value) is float and not math.isfinite(value):',
             'if False:', nonfinite),
            ('if allowed is not None and result["action"]["kind"] not in allowed:',
             'if False:', action("type", text="unsupported")),
        ]
        for old, new, candidate in mutants:
            with self.subTest(validation=old, candidate=candidate):
                self.assertEqual(source.count(old), 1)
                namespace = {"__file__": model.__file__, "__name__": "cu_model_mutant"}
                exec(compile(source.replace(old, new), model.__file__, "exec"), namespace)
                def negative_case():
                    backend = Mock(generate=Mock(return_value=candidate))
                    with self.assertRaises(namespace["ActionValidationError"]):
                        namespace["ModelClient"](backend).act(
                            "Read", "/dev/shm/frame.png",
                            {"schema": "ve.cu_describe/v1", "width": 100, "height": 80},
                            capabilities=["click", "key", "wait", "done", *(
                                ["type"] if old != mutants[-1][0] else [])])
                result = unittest.TextTestRunner(stream=io.StringIO()).run(
                    unittest.FunctionTestCase(negative_case))
                self.assertEqual(len(result.failures), 1)
                self.assertEqual(result.errors, [])
