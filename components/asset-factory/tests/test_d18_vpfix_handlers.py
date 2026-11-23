import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from contextvars import ContextVar
from unittest.mock import patch

from viewpoint import handlers


OPERATIONS = set("""
    af.select_frames af.matte af.generate af.repair af.lod af.project_views
    af.uv_bake af.face_fit af.assemble af.rig af.motion_from_video af.lipsync
    af.render af.mux af.registry.resolve af.registry.put af.publish
    vqa3d.score vqa3d.gate
""".split())
MUTATING_EVENTS = {
    "os.remove", "os.rename", "os.mkdir", "os.rmdir", "os.chmod", "os.link",
    "os.symlink", "os.utime", "os.truncate", "subprocess.Popen", "socket.__new__",
    "os.system", "os.exec", "os.posix_spawn", "os.posix_spawnp", "os.fork", "os.forkpty",
    ".".join(("os", "kill")), ".".join(("os", "killpg")),
}
_AUDIT_DRY_RUN = ContextVar("audit_vpfix_dry_run", default=False)


class DryRunSideEffect(AssertionError):
    pass


def _audit(event, values):
    if _AUDIT_DRY_RUN.get() and (event in MUTATING_EVENTS or event.startswith("os.exec") or (
        event == "open" and values[2] & (
            os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
        )
    )):
        raise DryRunSideEffect(event)


def setUpModule():
    sys.addaudithook(_audit)


@contextmanager
def observe_side_effects():
    token = _AUDIT_DRY_RUN.set(True)
    try:
        yield
    finally:
        _AUDIT_DRY_RUN.reset(token)


class LocalDryRunTests(unittest.TestCase):
    def setUp(self):
        self.catalog = {entry["name"]: entry for entry in handlers.local_capabilities()}
        self.lane = handlers.LocalDryRun()

    def request(self, name="vqa3d.gate", request_id="test-request"):
        entry = self.catalog[name]
        args = dict.fromkeys(entry["args_schema"]["required"], "a" * 64)
        args["goal_id"] = "test-goal"
        if "locator" in args:
            args["locator"] = "af://test/asset@sha256:" + "a" * 64
        return {"request_id": request_id, "op": name, "args": args,
                "risk_class": entry["risk_class"], "operator_only": entry["operator_only"]}

    def test_inventory_and_all_dry_run_checkpoints(self):
        self.assertEqual(set(self.catalog), OPERATIONS)
        self.assertEqual(len(handlers.local_capabilities()), len(OPERATIONS))
        self.assertEqual(self.catalog["af.publish"]["risk_class"], "enviar")
        self.assertEqual({name for name, row in self.catalog.items()
                          if row["operator_only"]}, {"af.publish"})
        for name, entry in self.catalog.items():
            with self.subTest(op=name):
                self.assertEqual(entry["handler"], "viewpoint.handlers:LocalDryRun.handle")
                self.assertFalse(entry["args_schema"]["additionalProperties"])
                request = self.request(name, name)
                with observe_side_effects():
                    receipt = self.lane.handle(request, dry_run=True)
                self.assertEqual(receipt["op"], name)
                self.assertEqual(receipt["request_id"], name)
                self.assertTrue(receipt["dry_run"])
                self.assertFalse(receipt["executed"])
                self.assertEqual(receipt["checkpoint"], entry["checkpoint"])
                self.assertEqual(receipt["checkpoint"]["backend"], "blender_rw1")
                self.assertTrue(receipt["checkpoint"]["name"])
                self.assertFalse(receipt["checkpoint_taken"])
                self.assertRegex(receipt["request_sha256"], "^[0-9a-f]{64}$")

    def test_typed_refusal_for_pending_unit(self):
        for op, unit in {"af.select_frames": "INPUT", "af.project_views": "LOOK-BAKE",
                         "af.face_fit": "FACE", "af.lipsync": "VOICE",
                         "af.registry.put": "REG", "af.publish": "PUB"}.items():
            with self.subTest(op=op):
                result = self.lane.handle(self.request(op, op), dry_run=True)
                self.assertFalse(result["ok"])
                self.assertEqual(result["code"], "NOT_IMPLEMENTED")
                self.assertEqual(result["pending_unit"], unit)
                self.assertFalse(result["executed"])

    def test_added_operations_and_irreversible_boundary_checkpoint_labels(self):
        expected = {
            "af.select_frames": ("selected_frames", "after"),
            "af.project_views": ("projected_views", "after"),
            "af.face_fit": ("face_controls", "after"),
            "af.mux": ("muxed_film", "after"),
            "af.render": ("render_settings", "before"),
            "af.publish": ("accepted_local_release", "before"),
        }
        for op, (name, phase) in expected.items():
            with self.subTest(op=op):
                result = self.lane.handle(self.request(op, op), dry_run=True)
                self.assertEqual(result["checkpoint"], {
                    "backend": "blender_rw1", "name": name, "phase": phase,
                })
                self.assertFalse(result["checkpoint_taken"])

    def test_wrong_operation_refuses(self):
        request = self.request()
        request["op"] = "af.generate_typo"
        self.assertEqual(self.lane.handle(request, dry_run=True)["code"], "UNKNOWN_OP")
        request["op"] = []
        self.assertEqual(self.lane.handle(request, dry_run=True)["code"], "INVALID_REQUEST")

    def test_publish_risk_and_authority_downgrades_refuse(self):
        request = self.request("af.publish")
        for update in ({"risk_class": "leer"}, {"risk_class": "publicar"},
                       {"operator_only": False}, {"operator_only": 1}):
            with self.subTest(update=update):
                result = self.lane.handle(request | update, dry_run=True)
                self.assertFalse(result["ok"])
                self.assertEqual(result["code"], "POLICY_MISMATCH")
        self.assertEqual(self.lane.handle(request, dry_run=True)["code"], "NOT_IMPLEMENTED")

    def test_replay_and_conflicting_reuse(self):
        request = self.request()
        original = self.lane.handle(request, dry_run=True)
        self.assertTrue(original["ok"])
        self.assertEqual(original["code"], "DRY_RUN")
        reordered = dict(reversed(list(request.items())))
        reordered["args"] = dict(reversed(list(request["args"].items())))
        self.assertEqual(self.lane.handle(reordered, dry_run=True), original)
        changed = request | {"args": request["args"] | {"goal_id": "other-goal"}}
        self.assertEqual(self.lane.handle(changed, dry_run=True)["code"],
                         "REQUEST_ID_CONFLICT")
        self.assertEqual(self.lane.handle(request, dry_run=True), original)

    def test_conflicting_race_admits_only_one_request(self):
        first = self.request()
        second = self.request("vqa3d.score")
        with ThreadPoolExecutor(max_workers=2) as workers:
            result1 = workers.submit(self.lane.handle, first, dry_run=True)
            result2 = workers.submit(self.lane.handle, second, dry_run=True)
        self.assertCountEqual([result1.result()["code"], result2.result()["code"]],
                              ["DRY_RUN", "REQUEST_ID_CONFLICT"])

    def test_argument_contract_and_non_dry_run_refuse(self):
        request = self.request("af.registry.resolve")
        for args in ({}, request["args"] | {"unexpected": True},
                     request["args"] | {"locator": "af://test/asset:candidate"},
                     request["args"] | {"goal_id": "test-goal\n"}):
            with self.subTest(args=args):
                refused = self.lane.handle(request | {"args": args}, dry_run=True)
                self.assertEqual(refused["code"], "INVALID_ARGS")
        refused = self.lane.handle(request, dry_run=False)
        self.assertEqual(refused["code"], "DRY_RUN_REQUIRED")
        self.assertFalse(refused["executed"])
        result = self.lane.handle(request, dry_run=True)
        self.assertEqual(result["code"], "NOT_IMPLEMENTED")

    def test_cli_dry_run_and_duplicate_keys(self):
        payload = json.dumps(self.request())
        for raw, code, rc in ((payload, "DRY_RUN", 0),
                              ('{"op":"first",' + payload[1:], "INVALID_REQUEST", 2)):
            with self.subTest(code=code), observe_side_effects():
                output = io.StringIO()
                self.assertEqual(handlers.main(["--dry-run"], io.StringIO(raw), output), rc)
                self.assertEqual(json.loads(output.getvalue())["code"], code)

    def test_cli_lists_the_local_inventory(self):
        output = io.StringIO()
        with observe_side_effects():
            rc = handlers.main(["--list-capabilities"], io.StringIO(), output)
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(output.getvalue()), handlers.local_capabilities())

    def test_dry_run_oracle_rejects_file_write_mutant(self):
        request = self.request()
        with tempfile.TemporaryDirectory(dir=os.getenv("VPFIX_TEST_TEMP")) as folder:
            sentinel = Path(folder) / "sentinel"
            sentinel.write_text("original", encoding="utf-8")

            def impure_digest(_value):
                sentinel.write_text("changed", encoding="utf-8")

            with patch.object(handlers, "sha256_json", impure_digest):
                with self.assertRaisesRegex(DryRunSideEffect, "^open$"), observe_side_effects():
                    self.lane.handle(request, dry_run=True)
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "original")

    def test_dry_run_oracle_rejects_os_system_mutant(self):
        request = self.request()

        def impure_digest(_value):
            os.system(":")

        with patch.object(handlers, "sha256_json", impure_digest):
            with self.assertRaisesRegex(DryRunSideEffect, "^os.system$"), observe_side_effects():
                self.lane.handle(request, dry_run=True)
