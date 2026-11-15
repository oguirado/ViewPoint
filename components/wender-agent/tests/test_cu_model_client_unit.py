"""C3 validation and real subprocess boundaries, without network or model auth."""
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from wender.learn.model_client import (
    ActionValidationError, BackendError, CommandBackend, ModelClient,
    ModelTimeoutError, MuseBackend, ReplayBackend, SCHEMA_PATH,
    parse_muse_output, validate_action,
)


def action(kind="click", **fields):
    return {"schema": "cu.action/v1", "observation": "The control is visible.",
            "action": {"kind": kind, **fields},
            "expectation": {"kind": "none"}, "confidence": 0.9}


def record(kind, **payload):
    return {"schema_version": 1, "id": "test-record", "stream": {"kind": "run", "id": "test-run"},
            "sequence": 1, "recorded_at": 0, "record_type": "event", "durability": "durable",
            "causation_id": "test-command", "payload_type": kind,
            "payload_schema_version": 1, "payload": payload}


def jsonl(value):
    text = json.dumps(value)
    return "\n".join(json.dumps(event) for event in [
        record("run.lifecycle.started"),
        record("run.output.delta", text=text[:17]),
        record("run.progress", text="ignored"),
        record("run.output.delta", text=text[17:]),
        record("run.terminal.completed"),
    ]) + "\n"


class TestValidation(unittest.TestCase):
    def test_schema_is_shipped_and_strict(self):
        schema = json.loads(SCHEMA_PATH.read_text())
        self.assertEqual(schema["properties"]["schema"]["const"], "cu.action/v1")
        self.assertEqual(set(schema["properties"]["action"]["properties"]["kind"]["enum"]),
                         {"click", "key", "type", "wait", "done"})

        def check(node):
            if isinstance(node, dict):
                if "properties" in node or "required" in node:
                    self.assertIs(node.get("additionalProperties"), False)
                    self.assertEqual(set(node["required"]), set(node["properties"]))
                for child in node.values():
                    check(child)
            elif isinstance(node, list):
                for child in node:
                    check(child)
        check(schema)

    def test_nullable_wire_fields_normalize_before_c3(self):
        for value in [action(x=2, y=3), action("key", keysym="Return"),
                      action("type", text="hello"), action("wait", ms=0), action("done")]:
            wire = copy.deepcopy(value)
            for field in ("x", "y", "keysym", "text", "ms", "element_id"):
                wire["action"].setdefault(field, None)
            wire["expectation"]["region"] = None
            self.assertEqual(validate_action(parse_muse_output(jsonl(wire)), 100, 80), value)
            self.assertIn("region", wire["expectation"])
        for bad in [action("click", x=None, y=None), action("key", keysym=None),
                    action("type", text=""), action("key", keysym=""), action("wait", ms=None)]:
            with self.assertRaises(ActionValidationError):
                validate_action(bad, 100, 80)

    def test_unused_neutral_fields_for_each_kind(self):
        cases = [(action(x=2, y=3), {"text": "", "keysym": "", "ms": 0}),
                 (action("key", keysym="Return"), {"text": "", "ms": 0}),
                 (action("type", text="hello"), {"keysym": "", "ms": 0}),
                 (action("wait", ms=0), {"text": "", "keysym": ""}),
                 (action("done"), {"text": "", "keysym": "", "ms": 0})]
        for expected, unused in cases:
            wire = copy.deepcopy(expected)
            wire["action"].update(unused)
            for field in ("x", "y", "keysym", "text", "ms", "element_id"):
                wire["action"].setdefault(field, None)
            original = copy.deepcopy(wire)
            with self.subTest(kind=expected["action"]["kind"]):
                self.assertEqual(validate_action(wire, 100, 80), expected)
                self.assertEqual(wire, original)

    def test_real_muse_click_with_zero_ms(self):
        wire = {"schema": "cu.action/v1", "observation": "The control is visible.",
                "action": {"element_id": None, "keysym": None, "kind": "click",
                           "ms": 0, "text": None, "x": 273, "y": 73},
                "confidence": 0.92,
                "expectation": {"kind": "state_change", "region": None}}
        result = validate_action(parse_muse_output(jsonl(wire)), 1024, 768)
        self.assertEqual(result["action"], {"kind": "click", "x": 273, "y": 73})
        self.assertEqual(result["expectation"], {"kind": "state_change"})
        self.assertEqual(result["confidence"], 0.92)

    def test_unused_meaningful_fields_and_invalid_neutrals_rejected(self):
        for value in [action(x=2, y=3, text="abc"), action(x=2, y=3, ms=500),
                      action(x=2, y=3, keysym="Return"),
                      action("key", keysym="Return", x=0),
                      action("type", text="hello", y=0),
                      action("wait", ms=0, text="abc"),
                      action("done", element_id="save"), action("done", ms=False),
                      action("done", ms=0.0), action("done", extra=None)]:
            with self.subTest(value=value), self.assertRaises(ActionValidationError):
                validate_action(value, 100, 80)

    def test_all_kinds(self):
        for value in [action(x=0, y=0), action(x=99, y=79),
                      action("key", keysym="Return"), action("type", text="hello"),
                      action("wait", ms=0), action("done")]:
            with self.subTest(value=value):
                self.assertEqual(validate_action(value, 100, 80), value)

    def test_invalid_fields_and_types(self):
        good = action(x=12, y=23)
        invalid = [action("drag"), action(), action(x=1), action("key"),
                   action("type"), action("wait"), action("wait", ms=-1),
                   action(x=True, y=0), action(x=1.0, y=0),
                   action("done", x=1), action("key", keysym=3)]
        for key in good:
            missing = copy.deepcopy(good)
            del missing[key]
            invalid.append(missing)
        for key, value in [("confidence", True), ("confidence", float("nan")),
                           ("confidence", float("inf")), ("confidence", 1.1),
                           ("confidence", 10 ** 400),
                           ("confidence", -0.1), ("observation", 7),
                           ("schema", "cu.action/v2"), ("extra", 1),
                           ("expectation", {"kind": "unknown"}),
                           ("expectation", {"kind": "none", "extra": 1})]:
            candidate = copy.deepcopy(good)
            candidate[key] = value
            invalid.append(candidate)
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ActionValidationError):
                validate_action(value, 100, 80)

    def test_out_of_frame(self):
        for x, y in [(-1, 0), (100, 0), (0, -1), (0, 80)]:
            with self.subTest(x=x, y=y), self.assertRaises(ActionValidationError):
                validate_action(action(x=x, y=y), 100, 80)
        for rect in [{"x": 90, "y": 0, "w": 11, "h": 1},
                     {"x": 0, "y": 0, "w": 1, "h": 81},
                     {"x": 0, "y": 0, "w": 0, "h": 1}]:
            value = action("done")
            value["expectation"] = {"kind": "changed_region", "region": rect}
            with self.subTest(rect=rect), self.assertRaises(ActionValidationError):
                validate_action(value, 100, 80)

    def test_element_center_and_no_mutation(self):
        value = action(element_id="save")
        elements = [{"id": "save", "role": "button", "name": "Save",
                     "bbox": {"x": 10, "y": 20, "w": 11, "h": 9}}]
        result = validate_action(value, 100, 80, elements)
        self.assertEqual(result["action"], {"kind": "click", "element_id": "save",
                                             "x": 15, "y": 24})
        self.assertNotIn("x", value["action"])
        self.assertEqual(elements[0]["bbox"]["x"], 10)

    def test_element_resolution_errors(self):
        for elements in [None, [], [{"id": "other", "bbox": None}],
                         [{"id": "save", "bbox": None}],
                         [{"id": "save", "bbox": {"x": 99, "y": 0, "w": 2, "h": 1}}],
                         [{"id": "save", "bbox": {}}, {"id": "save", "bbox": {}}]]:
            with self.subTest(elements=elements), self.assertRaises(ActionValidationError):
                validate_action(action(element_id="save"), 100, 80, elements)

    def test_bad_frame_dimensions(self):
        for width, height in [(0, 80), (-1, 80), (100, None), (True, 80), (10.0, 80)]:
            with self.subTest(width=width, height=height):
                with self.assertRaises(ActionValidationError):
                    validate_action(action("done"), width, height)


class TestDeltaParser(unittest.TestCase):
    def test_split_text_deltas(self):
        value = action("type", text="caf\u00e9")
        parsed = parse_muse_output(jsonl(value))
        self.assertEqual(parsed, value)
        self.assertEqual(parsed.extra_objects, 0)

    def test_real_success_capture(self):
        capture = Path(__file__).with_name("fixtures") / "real_muse_output_ok.jsonl"
        value = parse_muse_output(capture.read_text())
        self.assertEqual(value["action"], {"kind": "click", "x": 885, "y": 45})
        self.assertIn("attention-mask", value["observation"])

    def test_real_drafted_ahead_capture(self):
        capture = Path(__file__).with_name("fixtures") / "real_muse_output_multi.jsonl"
        value = parse_muse_output(capture.read_text())
        self.assertEqual({k: value["action"][k] for k in ("kind", "x", "y")},
                         {"kind": "click", "x": 213, "y": 73})
        self.assertEqual(value["expectation"]["kind"], "state_change")
        self.assertEqual(value.extra_objects, 1)
        validated = validate_action(value, 1024, 768)
        self.assertEqual(validated.extra_objects, 1)
        self.assertEqual(validated["action"], {"kind": "click", "x": 213, "y": 73})
        client = ModelClient(MuseBackend("."))
        with patch.object(client.backend, "generate", return_value=value):
            result = client.act("test", "frame.png", {
                "schema": "ve.cu_describe/v1", "width": 1024, "height": 768})
        self.assertEqual(result.extra_objects, 1)

    def test_multiple_objects_and_leading_whitespace(self):
        first = action("done")
        text = " \n" + json.dumps(first) + "\n" + json.dumps(action("wait", ms=800)) + "{}"
        value = parse_muse_output(json.dumps(record("run.output.delta", text=text)))
        self.assertEqual(value, first)
        self.assertEqual(value.extra_objects, 2)

    def test_real_failure_capture(self):
        capture = Path(__file__).with_name("fixtures") / "real_muse_output_schema400.jsonl"
        with self.assertRaisesRegex(BackendError, "API error 400.*region.*invalid_request_error"):
            parse_muse_output(capture.read_text())

    def test_failure_wins_over_output_and_redacts(self):
        failure = record("run.terminal.failed", reason="API error token=private Bearer private https://user:secret@example.test " + "x" * 400)
        with self.assertRaises(BackendError) as caught:
            parse_muse_output(jsonl(action("done")) + json.dumps(failure))
        message = str(caught.exception)
        self.assertNotIn("private", message)
        self.assertNotIn("secret", message)
        self.assertLessEqual(len(message), len("Muse run failed: ") + 300)

    def test_bad_jsonl_and_payload(self):
        invalid = ["", "not JSON", "[]", json.dumps(record("run.terminal.completed"))]
        invalid += [json.dumps(record("run.output.delta", text=value))
                    for value in (1, "", "   ", "garbage", "[]", "null", "{}garbage", "{} {", "{", "NaN", '{"x":1,"x":2}')]
        invalid += [json.dumps(record("run.output.delta", delta="{}")),
                    json.dumps({"payload_type": "run.output.delta", "payload": None})]
        for stdout in invalid:
            with self.subTest(stdout=stdout), self.assertRaises(BackendError):
                parse_muse_output(stdout)


class TestBackends(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="cu-model-test-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.describe = {"schema": "ve.cu_describe/v1", "width": 100, "height": 80,
                         "changed": None, "changed_regions": [], "crops": []}
        # CLI stubs only inspect image arguments; no images are written to disk.
        self.frame = self.root / "frame.png"
        self.crop = self.root / "crop.png"
        self.log = self.root / "argv.json"

    def stub(self, body):
        executable = self.root / "muse"
        executable.write_text("#!" + sys.executable + "\n" + body)
        executable.chmod(0o700)
        return patch.dict(os.environ, {"PATH": str(self.root) + os.pathsep + os.environ["PATH"]})

    def test_muse_cli_and_context(self):
        value = action(element_id="save")
        body = ("import json, pathlib, sys\n"
                "args = sys.argv[1:]\n"
                "prompt = pathlib.Path(args[args.index('--prompt-file')+1]).read_text()\n"
                f"pathlib.Path({str(self.log)!r}).write_text(json.dumps([args, prompt]))\n"
                f"sys.stdout.write({jsonl(value)!r})\n")
        elements = [{"id": "save", "role": "button", "name": "Save",
                     "bbox": {"x": 10, "y": 20, "w": 20, "h": 10},
                     "raw_ax": "do not include"}]
        with self.stub(body):
            result = ModelClient(MuseBackend(self.root)).act(
                "Save", self.frame, self.describe, crop_paths=[self.crop],
                elements=elements, trace_steps=[{"step": 7}])
        self.assertEqual((result["action"]["x"], result["action"]["y"]), (20, 25))
        args, prompt = json.loads(self.log.read_text())
        prompt_index = args.index("--prompt-file") + 1
        self.assertFalse(Path(args[prompt_index]).exists())
        expected = ["exec", "--json", "--prompt-file", args[prompt_index],
                    "--image", str(self.frame), "--image", str(self.crop),
                    "--output-schema", str(SCHEMA_PATH), "--model", "muse-spark-1.3",
                    "--reasoning-effort", "medium", "--workspace", str(self.root),
                    "--trust-workspace", "--sandbox-network", "restricted",
                    "--approval-mode", "never", "--contributor-mode", "on",
                    "--disable-write", "--disable-web-tools", "--disable-reminders",
                    "--max-model-steps", "1", "--session-id", args[-1]]
        __import__("uuid").UUID(args[-1])
        self.assertEqual(args, expected)
        self.assertIn("exactly ONE action for the current frame only", prompt)
        self.assertIn("Never plan or emit later steps", prompt)
        self.assertIn("Fields not used by the chosen action kind must be null", prompt)
        self.assertIn('"goal": "Save"', prompt)
        self.assertIn('"step": 7', prompt)
        self.assertNotIn("raw_ax", prompt)
        self.assertNotIn("do not include", prompt)

    def test_muse_default_timeout_reaches_subprocess(self):
        with patch("wender.learn.model_client.subprocess.run") as run:
            run.return_value = type("Result", (), {
                "returncode": 0, "stdout": jsonl(action("done")), "stderr": ""})()
            ModelClient(MuseBackend(self.root)).act("Save", self.frame, self.describe)
        self.assertEqual(run.call_args.kwargs["timeout"], 240)

    def test_muse_timeout_and_prompt_cleanup(self):
        body = ("import json, pathlib, sys, time\n"
                f"pathlib.Path({str(self.log)!r}).write_text(json.dumps(sys.argv[1:]))\n"
                "time.sleep(30)\n")
        with self.stub(body), self.assertRaises(ModelTimeoutError):
            ModelClient(MuseBackend(self.root, timeout=0.3)).act(
                "Save", self.frame, self.describe)
        args = json.loads(self.log.read_text())
        self.assertFalse(Path(args[args.index("--prompt-file") + 1]).exists())

    def test_muse_nonzero_and_missing_executable(self):
        with self.stub("import sys\nsys.stderr.write('private diagnostic')\nsys.exit(7)\n"):
            with self.assertRaises(BackendError) as caught:
                ModelClient(MuseBackend(self.root)).act("Save", self.frame, self.describe)
        self.assertNotIn("private diagnostic", str(caught.exception))
        with self.assertRaises(BackendError):
            ModelClient(MuseBackend(self.root, executable=self.root / "absent")).act(
                "Save", self.frame, self.describe)

    def test_muse_nonzero_preserves_terminal_failure(self):
        failure = json.dumps(record("run.terminal.failed", reason="API error 400: invalid schema"))
        with self.stub("import sys\nsys.stdout.write(%r)\nsys.exit(1)\n" % failure):
            with self.assertRaisesRegex(BackendError, "API error 400"):
                ModelClient(MuseBackend(self.root)).act("Save", self.frame, self.describe)

    def test_muse_invalid_c3_is_rejected(self):
        with self.stub("import sys\nsys.stdout.write(%r)\n" % jsonl(action(x=100, y=0))):
            with self.assertRaises(ActionValidationError):
                ModelClient(MuseBackend(self.root)).act("Save", self.frame, self.describe)

    def test_replay_sequence_and_exhaustion(self):
        path = self.root / "actions.json"
        values = [action("key", keysym="Tab"), action("done")]
        path.write_text(json.dumps(values))
        client = ModelClient(ReplayBackend(path))
        for expected in values:
            self.assertEqual(client.act("Save", self.frame, self.describe), expected)
        with self.assertRaisesRegex(BackendError, "exhausted"):
            client.act("Save", self.frame, self.describe)

    def test_replay_validates_and_returns_detached_actions(self):
        path = self.root / "actions.json"
        path.write_text(json.dumps([action(x=0, y=0), action(x=-1, y=0)]))
        backend = ReplayBackend(path)
        client = ModelClient(backend)
        result = client.act("Save", self.frame, self.describe)
        result["action"]["x"] = 99
        self.assertEqual(backend.actions[0]["action"]["x"], 0)
        with self.assertRaises(ActionValidationError):
            client.act("Save", self.frame, self.describe)

    def test_invalid_replay_files(self):
        path = self.root / "actions.json"
        for text in ["{", "{}", "NaN"]:
            path.write_text(text)
            with self.subTest(text=text), self.assertRaises(BackendError):
                ReplayBackend(path)
        with self.assertRaises(BackendError):
            ReplayBackend(self.root / "absent")

    def test_command_hook_and_timeout(self):
        script = self.root / "fallback.py"
        value = action("done")
        script.write_text("import sys\nassert sys.argv[1] == '--image'\nprint(%r)\n"
                          % json.dumps(value))
        backend = CommandBackend([sys.executable, str(script), "{images}",
                                  "{prompt_file}", "{schema}", "{workspace}"], self.root)
        self.assertEqual(ModelClient(backend).act("Save", self.frame, self.describe), value)
        backend = CommandBackend([sys.executable, "-c", "import time; time.sleep(30)"],
                                 self.root, timeout=0.1)
        with self.assertRaises(ModelTimeoutError):
            ModelClient(backend).act("Save", self.frame, self.describe)

    def test_bad_input_never_invokes_backend(self):
        backend = unittest.mock.Mock()
        client = ModelClient(backend)
        for describe in [{}, dict(self.describe, width=0)]:
            with self.assertRaises(ValueError):
                client.act("Save", self.frame, describe)
        backend.generate.assert_not_called()


if __name__ == "__main__":
    unittest.main()
