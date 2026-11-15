import email.message
import hashlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

GATEWAY = Path(__file__).resolve().parents[1]
if str(GATEWAY) not in sys.path:
    sys.path.insert(0, str(GATEWAY))

import app as app_mod  # noqa: E402
import replay_store as replay_store_mod  # noqa: E402
from plan_schema import validate_plan  # noqa: E402
from replay_store import ReplayError, ReplayStore  # noqa: E402

_STRIP = {"REPLAY_PINS", "REPLAY_REQUIRE_PIN", "DEMO_TOKEN", "TRUST_PROXY",
          "PLAN_QUOTA_FILE", "KEEPALIVE_IDLE", "MAX_WORKERS", "SOCKET_TIMEOUT",
          "PLAN_DAILY_CAP", "PLAN_RATE_CAPACITY", "PLAN_RATE_REFILL",
          "PLAN_MAX_INFLIGHT"}
_ENV = {k: v for k, v in os.environ.items() if k not in _STRIP}


class _Client:
    configured = True
    model = "stub-model"

    def __init__(self, refs):
        self._refs = refs

    def generate_plan(self, replay):
        return {"summary": "s", "reasoning": "r",
                "steps": [{"title": "t", "detail": "d",
                           "engine_step_refs": list(self._refs)}]}


class _TreeCase(unittest.TestCase):
    """Builds an isolated replay/ + static/frames/ tree in a temp directory."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.replay_dir = self.root / "replay"
        self.static_dir = self.root / "static"
        (self.static_dir / "frames").mkdir(parents=True)
        self.replay_dir.mkdir()

    def write_static(self, rel, payload=b"<svg xmlns='http://www.w3.org/2000/svg'/>"):
        path = self.static_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        return hashlib.sha256(payload).hexdigest()

    def document(self, replay_id="neg-one"):
        digest_a = self.write_static("frames/a.svg", b"<svg id='a'/>")
        digest_b = self.write_static("frames/b.svg", b"<svg id='b'/>")
        return {
            "schema": "viewpoint.replay/v1",
            "replay_id": replay_id,
            "version": "1.0.0",
            "mode": "REPLAY",
            "sample": True,
            "sample_notice": "SAMPLE negative-control document.",
            "title": "Negative control replay",
            "task": "Exercise replay store integrity checks.",
            "recorded_at": "2026-01-01T00:00:00Z",
            "provenance": {"source": "unit test", "capture": "synthetic",
                           "note": "not a real session"},
            "labels": {"mode": "REPLAY", "live": False, "sample": True},
            "frames": [
                {"frame_id": "f01", "t_ms": 0, "src": "/static/frames/a.svg",
                 "sha256": digest_a, "caption": "first", "url": "https://example.invalid/a"},
                {"frame_id": "f02", "t_ms": 100, "src": "/static/frames/b.svg",
                 "sha256": digest_b, "caption": "second", "url": "https://example.invalid/b"},
            ],
            "steps": [
                {"step_id": "s01", "t_ms": 0, "actor": "operator", "action": "open",
                 "target": "page", "outcome": "opened", "status": "observed",
                 "frame_id": "f01"},
                {"step_id": "s02", "t_ms": 100, "actor": "engine", "action": "click",
                 "target": "button", "outcome": "clicked", "status": "verified",
                 "frame_id": "f02"},
            ],
            "terminal_outcome": "Synthetic recording finished.",
            "limitations": ["Synthetic data only."],
        }

    def write_doc(self, doc, filename=None, pad_to=None):
        raw = json.dumps(doc).encode("utf-8")
        if pad_to is not None:
            raw += b" " * (pad_to - len(raw))
        path = self.replay_dir / (filename or (doc["replay_id"] + ".json"))
        path.write_bytes(raw)
        return path

    def load(self, **kw):
        return ReplayStore(self.replay_dir, self.static_dir, **kw)

    def symlink_or_skip(self, target, link):
        try:
            os.symlink(target, link)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable in this sandbox")


class FrameIntegrityNegativeControls(_TreeCase):
    def test_tampered_frame_bytes_rejected(self):
        # Protects replay_store.py _verify_frame: `if actual != digest:`
        doc = self.document()
        self.write_doc(doc)
        self.write_static("frames/a.svg", b"<svg id='TAMPERED'/>")
        with self.assertRaisesRegex(ReplayError, "does not match"):
            self.load()

    def test_missing_frame_file_rejected(self):
        # Protects replay_store.py _verify_frame: `if not candidate.is_file():`
        doc = self.document()
        self.write_doc(doc)
        (self.static_dir / "frames" / "b.svg").unlink()
        with self.assertRaisesRegex(ReplayError, "does not exist"):
            self.load()

    def test_symlinked_frame_file_rejected_even_when_hash_matches(self):
        # Protects replay_store.py _verify_frame: `if probe.is_symlink(): raise ReplayError(... must not be a symlink ...)`
        doc = self.document()
        digest = self.write_static("frames/real.svg", b"<svg id='real'/>")
        self.symlink_or_skip(self.static_dir / "frames" / "real.svg",
                             self.static_dir / "frames" / "link.svg")
        doc["frames"][0]["src"] = "/static/frames/link.svg"
        doc["frames"][0]["sha256"] = digest
        self.write_doc(doc)
        with self.assertRaisesRegex(ReplayError, "symlink"):
            self.load()

    def test_symlinked_directory_component_rejected_inside_static(self):
        # Protects replay_store.py _verify_frame: the parent-walk loop (`probe = parent`)
        # that catches symlinked directories. The target stays inside static/, so the
        # "escapes the static directory" check alone would NOT reject it.
        doc = self.document()
        digest = self.write_static("realdir/c.svg", b"<svg id='c'/>")
        self.symlink_or_skip(self.static_dir / "realdir",
                             self.static_dir / "frames" / "linkdir")
        doc["frames"][0]["src"] = "/static/frames/linkdir/c.svg"
        doc["frames"][0]["sha256"] = digest
        self.write_doc(doc)
        with self.assertRaisesRegex(ReplayError, "symlink"):
            self.load()

    def test_dotdot_in_src_rejected_even_when_target_is_valid(self):
        # Protects replay_store.py _validate: `or ".." in src` in the /static/frames/ prefix check.
        # static/secret.svg exists with a matching hash and resolves inside static/, so
        # without the ".." guard this document would be admitted.
        doc = self.document()
        digest = self.write_static("secret.svg", b"<svg id='secret'/>")
        doc["frames"][0]["src"] = "/static/frames/../secret.svg"
        doc["frames"][0]["sha256"] = digest
        self.write_doc(doc)
        with self.assertRaisesRegex(ReplayError, "must stay under /static/frames/"):
            self.load()


class TimelineAndSchemaNegativeControls(_TreeCase):
    def test_frames_out_of_order_rejected(self):
        # Protects replay_store.py _validate: `if timestamp < last_frame_t:`
        doc = self.document()
        doc["frames"][0]["t_ms"] = 500
        doc["frames"][1]["t_ms"] = 100
        self.write_doc(doc)
        with self.assertRaisesRegex(ReplayError, "frames must be ordered by t_ms"):
            self.load()

    def test_steps_out_of_order_rejected(self):
        # Protects replay_store.py _validate: `if timestamp < last_step_t:`
        doc = self.document()
        doc["steps"][0]["t_ms"] = 900
        doc["steps"][1]["t_ms"] = 100
        self.write_doc(doc)
        with self.assertRaisesRegex(ReplayError, "steps must be ordered by t_ms"):
            self.load()

    def test_equal_timestamps_are_accepted_boundary(self):
        # Boundary for replay_store.py `timestamp < last_*_t` (strict less-than):
        # changing it to `<=` would reject simultaneous frames/steps and fail this test.
        doc = self.document()
        for frame in doc["frames"]:
            frame["t_ms"] = 0
        for step in doc["steps"]:
            step["t_ms"] = 0
        self.write_doc(doc)
        store = self.load()
        self.assertEqual([r.replay_id for r in store.all()], ["neg-one"])

    def test_unknown_schema_version_rejected(self):
        # Protects replay_store.py _validate: `if data["schema"] != SCHEMA:`
        doc = self.document()
        doc["schema"] = "viewpoint.replay/v2"
        self.write_doc(doc)
        with self.assertRaisesRegex(ReplayError, "unsupported schema"):
            self.load()

    def test_step_referencing_unknown_frame_rejected(self):
        # Protects replay_store.py _validate: `if frame_id not in frame_ids:` for steps
        doc = self.document()
        doc["steps"][0]["frame_id"] = "f99"
        self.write_doc(doc)
        with self.assertRaisesRegex(ReplayError, "frame_id does not exist"):
            self.load()

    def test_replay_one_byte_over_limit_rejected(self):
        # Protects replay_store.py _load: `if len(raw) > MAX_REPLAY_BYTES:`
        doc = self.document()
        self.write_doc(doc, pad_to=replay_store_mod.MAX_REPLAY_BYTES + 1)
        with self.assertRaisesRegex(ReplayError, "exceeds"):
            self.load()

    def test_replay_exactly_at_limit_accepted_boundary(self):
        # Boundary for replay_store.py `len(raw) > MAX_REPLAY_BYTES` (strict greater-than):
        # changing it to `>=` would reject this valid document.
        doc = self.document()
        self.write_doc(doc, pad_to=replay_store_mod.MAX_REPLAY_BYTES)
        store = self.load()
        self.assertEqual(len(store.all()), 1)


class AttestationAndMembershipNegativeControls(_TreeCase):
    def test_pin_applies_per_replay_not_globally(self):
        # Protects replay_store.py _load: the `pinned is not None` / `else: attestation = "self-attested"`
        # branches. Only neg-one is pinned; neg-two must stay self-attested.
        path_one = self.write_doc(self.document("neg-one"), filename="a-one.json")
        self.write_doc(self.document("neg-two"), filename="b-two.json")
        digest = hashlib.sha256(path_one.read_bytes()).hexdigest()
        store = self.load(pins={"neg-one": digest})
        self.assertEqual(store.get("neg-one").attestation, "pinned")
        self.assertEqual(store.get("neg-two").attestation, "self-attested")
        summaries = {s["replay_id"]: s["attestation"]
                     for s in (ReplayStore.public_summary(r) for r in store.all())}
        self.assertEqual(summaries, {"neg-one": "pinned", "neg-two": "self-attested"})

    def test_pin_for_unloaded_replay_id_fails_closed(self):
        # Protects replay_store.py __init__: `if missing: raise ReplayError("pinned replay ids not loaded ...")`
        self.write_doc(self.document("neg-one"))
        with self.assertRaisesRegex(ReplayError, "pinned replay ids not loaded"):
            self.load(pins={"neg-ghost": "0" * 64})

    def test_plan_response_labels_partial_membership_and_attestation(self):
        # Protects app.py Application.plan: validate_plan(raw_plan, replay.data["steps"]) and the
        # `replay_attestation` field; protects plan_schema.py dedup (`if ref not in refs`) and strip().
        path = self.write_doc(self.document("neg-one"))
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        refs = ["s01", " s01 ", "zzz"]  # duplicate after strip + one id absent from the recording

        def run(store):
            with mock.patch.dict(os.environ, _ENV, clear=True):
                application = app_mod.Application(
                    store, None, _Client(refs), "",
                    log_stream=io.StringIO(), quota_file="")
                return application.plan("neg-one", client_ip="203.0.113.9")

        unpinned = run(self.load())
        self.assertEqual(unpinned["replay_attestation"], "self-attested")
        self.assertEqual(unpinned["replay_sha256"], digest)
        step = unpinned["plan"]["steps"][0]
        self.assertEqual(step["match"], "partial")
        self.assertEqual(step["ref_matches"],
                         [{"id": "s01", "exists": True}, {"id": "zzz", "exists": False}])

        pinned = run(self.load(pins={"neg-one": digest}))
        self.assertEqual(pinned["replay_attestation"], "pinned")
        self.assertEqual(pinned["plan"]["steps"][0]["match"], "partial")


if __name__ == "__main__":
    unittest.main()
