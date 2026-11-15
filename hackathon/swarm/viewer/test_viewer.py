"""Unit tests, V1..V5 controls, and H5_r1 repair gates (R8, R4, R2, R6, R7, /healthz)."""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import unittest
import zlib
from pathlib import Path
from typing import Any
from unittest import mock

VIEWER_DIR = Path(__file__).resolve().parent
REPO_ROOT = VIEWER_DIR.parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from gateway.replay_store import ReplayError, ReplayStore  # noqa: E402
from swarm.viewer.export_replay import (  # noqa: E402
    CANONICAL_AGENT_PALETTE,
    VERIFIED_VERDICTS,
    ExportError,
    _verdict_to_replay_status,
    export_replay,
    extract_embedded_swarm_sha256,
    load_pins_manifest,
    validate_swarm_bundle,
)
from swarm.viewer.server import (  # noqa: E402
    CSP_POLICY,
    ViewerState,
    create_server,
    sanitize_gateway_url,
)


def _make_solid_png(rgb: tuple[int, int, int] = (24, 48, 96), width: int = 16, height: int = 9) -> bytes:
    """Construct a valid minimal RGB PNG using Python stdlib (`zlib` + `struct`)."""

    def chunk(tag: bytes, payload: bytes) -> bytes:
        crc = zlib.crc32(tag + payload) & 0xFFFFFFFF
        return struct.pack(">I", len(payload)) + tag + payload + struct.pack(">I", crc)

    raw_rows = b"".join(b"\x00" + bytes(rgb) * width for _ in range(height))
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    idat = zlib.compress(raw_rows, level=6)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", idat)
        + chunk(b"IEND", b"")
    )


def _write_frame_pair(
    frames_dir: Path,
    frame_num: int,
    ts_ms: int,
    *,
    rgb: tuple[int, int, int] = (32, 64, 128),
    scene_epoch: int = 0,
    view_epoch: int = 0,
) -> str:
    frames_dir.mkdir(parents=True, exist_ok=True)
    png_bytes = _make_solid_png(rgb=rgb)
    digest = hashlib.sha256(png_bytes).hexdigest()
    (frames_dir / f"frame_{frame_num}.png").write_bytes(png_bytes)
    meta = {
        "v": 1,
        "frame": frame_num,
        "sha256": digest,
        "width": 16,
        "height": 9,
        "view_epoch": view_epoch,
        "scene_epoch": scene_epoch,
        "changed_regions": [[0, 0, 8, 8]],
        "ts_ms": ts_ms,
    }
    (frames_dir / f"frame_{frame_num}.json").write_text(
        json.dumps(meta) + "\n",
        encoding="utf-8",
    )
    return digest


def _sample_valid_records(ts_base: int = 5000, event_id: str = "ev-1", frame_base: int = 0) -> list[dict[str, Any]]:
    return [
        {
            "v": 1,
            "rec": "claim",
            "agent_id": "modeler-agent",
            "lease_id": f"lease-{event_id}",
            "lease_epoch": 1,
            "ttl_ms": 300000,
            "issued_ts_ms": ts_base,
            "expires_ts_ms": ts_base + 300000,
            "allow_gui": False,
            "write_prefixes": ["col:CHAR_", "obj:CHAR_"],
        },
        {
            "v": 1,
            "rec": "enqueue",
            "event_id": event_id,
            "agent_id": "modeler-agent",
            "lane": "script",
            "action_class": "state_job",
            "kind": "job",
            "based_on_frame": frame_base,
            "view_epoch": 0,
            "scene_epoch": 0,
            "lease_epoch": 1,
            "write_set": ["obj:CHAR_Torso"],
            "target_region": None,
            "footprint": None,
            "expect": f"Build torso for {event_id}",
            "script_sha256": "a" * 64,
            "max_acts": 1,
            "ts_ms": ts_base + 50,
        },
        {
            "v": 1,
            "rec": "turn",
            "turn": 1,
            "turn_epoch": 1,
            "agent_id": "modeler-agent",
            "head_frame": frame_base,
            "hold_ms": 120000,
            "ts_ms": ts_base + 100,
        },
        {
            "v": 1,
            "rec": "close",
            "tick": 1,
            "turn": 1,
            "event_id": event_id,
            "agent_id": "modeler-agent",
            "lane": "script",
            "verdict": "STATE_VERIFIED",
            "rebased": False,
            "dispatch_capture_seq": frame_base,
            "verify_frame_ref": frame_base + 1,
            "own_regions": [],
            "spill_regions": [],
            "drops": 0,
            "reason": "",
            "ts_ms": ts_base + 200,
        },
        {
            "v": 1,
            "rec": "cursor",
            "agent_id": "modeler-agent",
            "event_id": event_id,
            "phase": "result",
            "x": 480,
            "y": 420,
            "frame": frame_base + 1,
            "label": f"modeler-agent: done {event_id}",
            "color": "#ff6b6b",
            "ts_ms": ts_base + 205,
        },
    ]


def _raw_http_request(
    host: str,
    port: int,
    method: str,
    path: str,
) -> tuple[int, dict[str, str], bytes]:
    conn = http.client.HTTPConnection(host, port, timeout=5.0)
    try:
        conn.putrequest(method, path, skip_accept_encoding=True)
        conn.endheaders()
        resp = conn.getresponse()
        body = resp.read()
        headers = {k.lower(): v for k, v in resp.getheaders()}
        return resp.status, headers, body
    finally:
        conn.close()


class ViewerExporterAndControlsTest(unittest.TestCase):
    """Comprehensive suite covering pre-seeded bundles, R8/R4/R2/R6/R7, V1..V5, and HTTP server."""

    def test_preseeded_three_replays_default_order_and_size_budgets(self) -> None:
        manifest_path = VIEWER_DIR / "PINS.manifest"
        self.assertTrue(manifest_path.is_file(), "root PINS.manifest missing")
        self.assertFalse((VIEWER_DIR / "replays" / "PINS.txt").exists(), "legacy replays/PINS.txt must not exist")
        self.assertFalse((VIEWER_DIR / "static" / "data" / "PINS.txt").exists(), "legacy static/data/PINS.txt must not exist")

        replay_pins, swarm_pins = load_pins_manifest(manifest_path)
        expected_ids = {"take2-w2-4agents", "take3-w2-guards", "take1-w1-2agents"}
        self.assertEqual(set(replay_pins), expected_ids)
        self.assertEqual(set(swarm_pins), expected_ids)

        store = ReplayStore(
            VIEWER_DIR / "replays",
            VIEWER_DIR / "static",
            pins=replay_pins,
            require_pin=True,
        )

        for rid in expected_ids:
            replay_path = VIEWER_DIR / "replays" / f"{rid}.json"
            swarm_path = VIEWER_DIR / "static" / "data" / f"{rid}.swarm.json"
            self.assertTrue(replay_path.is_file())
            self.assertTrue(swarm_path.is_file())

            replay = store.get(rid)
            self.assertEqual(replay.attestation, "pinned")
            self.assertEqual(replay.data["mode"], "REPLAY")
            self.assertIs(replay.data["labels"]["live"], False)
            self.assertIs(replay.data["sample"], False)

            for frame in replay.data["frames"]:
                src = frame["src"]
                self.assertTrue(
                    src.startswith(f"/static/frames/{rid}_f") and src.endswith(".png"),
                    f"expected collision-free flat frame filename for {rid}, got {src}",
                )
                self.assertEqual(src.count("/"), 3)

            swarm_doc = validate_swarm_bundle(
                swarm_path,
                static_dir=VIEWER_DIR / "static",
                replay_dir=VIEWER_DIR / "replays",
                pins=replay_pins,
                swarm_pins=swarm_pins,
                require_pin=True,
            )
            self.assertEqual(swarm_doc["schema"], "viewpoint.swarm_timeline/v1")
            self.assertEqual(swarm_doc["mode"], "REPLAY")
            self.assertIs(swarm_doc["live"], False)
            self.assertEqual(swarm_doc["agent_palette"], CANONICAL_AGENT_PALETTE)

        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("REPLAY_PINS", None)
            os.environ.pop("DEFAULT_REPLAY_ID", None)
            vstate = ViewerState(VIEWER_DIR)
            listing = vstate.list_replays_payload()
            self.assertEqual(listing["default_replay_id"], "take2-w2-4agents")
            self.assertEqual(
                [r["replay_id"] for r in listing["replays"]],
                ["take2-w2-4agents", "take3-w2-guards", "take1-w1-2agents"],
            )

        # Enforce zero .mp4 files, every single file < 2 MB, and total swarm/viewer/ <= 8 MiB
        total_bytes = 0
        for p in VIEWER_DIR.rglob("*"):
            if p.is_file():
                self.assertNotEqual(p.suffix.lower(), ".mp4", f"unexpected .mp4 binary in viewer: {p}")
                sz = p.stat().st_size
                self.assertLess(sz, 2_000_000, f"file exceeds 2 MB cap ({sz} bytes): {p}")
                total_bytes += sz
        self.assertLessEqual(
            total_bytes,
            8 * 1024 * 1024,
            f"swarm/viewer/ exceeded 8 MiB budget: {total_bytes} bytes",
        )

    def test_r8_verified_with_spill_never_verified_or_green(self) -> None:
        # Check pure Python constants and mapping function first
        self.assertNotIn("VERIFIED_WITH_SPILL", VERIFIED_VERDICTS)
        self.assertEqual(_verdict_to_replay_status("VERIFIED_WITH_SPILL"), "observed")
        self.assertNotEqual(_verdict_to_replay_status("VERIFIED_WITH_SPILL"), "verified")
        self.assertEqual(_verdict_to_replay_status("STATE_VERIFIED"), "verified")
        self.assertEqual(_verdict_to_replay_status("VERIFIED"), "verified")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            frames_dir = root / "frames"
            _write_frame_pair(frames_dir, 0, 5000, rgb=(20, 30, 40))
            _write_frame_pair(frames_dir, 1, 5200, rgb=(40, 80, 120), scene_epoch=1)
            _write_frame_pair(frames_dir, 2, 5400, rgb=(80, 120, 160), scene_epoch=1, view_epoch=1)

            recs = _sample_valid_records(ts_base=5000, event_id="clean-script-1", frame_base=0)
            recs.extend(
                [
                    {
                        "v": 1,
                        "rec": "claim",
                        "agent_id": "camera-agent",
                        "lease_id": "lease-camera-1",
                        "lease_epoch": 1,
                        "ttl_ms": 300000,
                        "issued_ts_ms": 5210,
                        "expires_ts_ms": 305210,
                        "allow_gui": True,
                        "write_prefixes": ["cam:", "render:"],
                    },
                    {
                        "v": 1,
                        "rec": "enqueue",
                        "event_id": "cam-spill-ev-2",
                        "agent_id": "camera-agent",
                        "lane": "gui",
                        "action_class": "view_change",
                        "kind": "key",
                        "based_on_frame": 1,
                        "view_epoch": 0,
                        "scene_epoch": 1,
                        "lease_epoch": 1,
                        "write_set": [],
                        "target_region": None,
                        "footprint": [0, 80, 1580, 900],
                        "expect": "Orbit camera KP_3",
                        "script_sha256": None,
                        "max_acts": 1,
                        "ts_ms": 5250,
                    },
                    {
                        "v": 1,
                        "rec": "turn",
                        "turn": 2,
                        "turn_epoch": 2,
                        "agent_id": "camera-agent",
                        "head_frame": 1,
                        "hold_ms": 60000,
                        "ts_ms": 5300,
                    },
                    {
                        "v": 1,
                        "rec": "close",
                        "tick": 2,
                        "turn": 2,
                        "event_id": "cam-spill-ev-2",
                        "agent_id": "camera-agent",
                        "lane": "gui",
                        "verdict": "VERIFIED_WITH_SPILL",
                        "rebased": False,
                        "dispatch_capture_seq": 1,
                        "verify_frame_ref": 2,
                        "own_regions": [[100, 100, 50, 50]],
                        "spill_regions": [[10, 10, 20, 20], [1700, 50, 30, 30]],
                        "drops": 0,
                        "reason": "",
                        "ts_ms": 5400,
                    },
                ]
            )

            events_log = root / "r8_events.jsonl"
            events_log.write_text("".join(json.dumps(r) + "\n" for r in recs), encoding="utf-8")
            out_dir = root / "r8_out"
            res = export_replay(
                events_path=events_log,
                frames_dir=frames_dir,
                out_dir=out_dir,
                replay_id="r8-spill-check",
            )

            replay_doc = json.loads(res.replay_path.read_text(encoding="utf-8"))
            swarm_doc = json.loads(res.swarm_path.read_text(encoding="utf-8"))

            # (a) viewpoint.replay/v1 step status != "verified" (status == "observed") + names VERIFIED_WITH_SPILL + spill count
            spill_steps = [
                s for s in replay_doc["steps"]
                if "cam-spill-ev-2" in s["target"] and s["actor"] == "engine"
            ]
            self.assertEqual(len(spill_steps), 1)
            spill_step = spill_steps[0]
            self.assertNotEqual(spill_step["status"], "verified")
            self.assertEqual(spill_step["status"], "observed")
            self.assertIn("VERIFIED_WITH_SPILL", spill_step["action"])
            self.assertIn("spill_regions=2", spill_step["action"])
            self.assertIn("VERIFIED_WITH_SPILL", spill_step["outcome"])
            self.assertIn("spill_regions=2", spill_step["outcome"])

            clean_steps = [
                s for s in replay_doc["steps"]
                if "clean-script-1" in s["target"] and s["actor"] == "engine"
            ]
            self.assertEqual(len(clean_steps), 1)
            self.assertEqual(clean_steps[0]["status"], "verified")

            # (b) swarm timeline entry carries verdict "VERIFIED_WITH_SPILL" with non-verified status
            spill_turns = [t for t in swarm_doc["turns"] if t["event_id"] == "cam-spill-ev-2"]
            self.assertEqual(len(spill_turns), 1)
            self.assertEqual(spill_turns[0]["verdict"], "VERIFIED_WITH_SPILL")
            self.assertEqual(spill_turns[0]["status"], "observed")
            self.assertNotEqual(spill_turns[0]["status"], "verified")
            self.assertEqual(spill_turns[0]["spill_region_count"], 2)

            # Mutating VERIFIED_WITH_SPILL turn status to "verified" must fail closed
            bad_swarm = json.loads(json.dumps(swarm_doc))
            for t in bad_swarm["turns"]:
                if t["verdict"] == "VERIFIED_WITH_SPILL":
                    t["status"] = "verified"
            with self.assertRaises(ExportError):
                validate_swarm_bundle(
                    bad_swarm,
                    pins={"r8-spill-check": res.replay_sha256},
                    require_pin=True,
                )

        # Verify all pre-seeded live bundles satisfy R8 as well
        for rid in ("take2-w2-4agents", "take3-w2-guards", "take1-w1-2agents"):
            rdoc = json.loads((VIEWER_DIR / "replays" / f"{rid}.json").read_text(encoding="utf-8"))
            sdoc = json.loads((VIEWER_DIR / "static" / "data" / f"{rid}.swarm.json").read_text(encoding="utf-8"))
            for st in rdoc["steps"]:
                if "VERIFIED_WITH_SPILL" in st["outcome"] or "VERIFIED_WITH_SPILL" in st["action"]:
                    self.assertEqual(st["status"], "observed")
            for tr in sdoc["turns"]:
                if tr["verdict"] == "VERIFIED_WITH_SPILL":
                    self.assertEqual(tr["status"], "observed")

        # (c) Static check + JS VM execution check on static/app.js and static/style.css
        app_js = (VIEWER_DIR / "static" / "app.js").read_text(encoding="utf-8")
        style_css = (VIEWER_DIR / "static" / "style.css").read_text(encoding="utf-8")
        self.assertIn(".verdict-spill", style_css)
        self.assertIn(".verdict-amber", style_css)
        self.assertIn('SPILL_VERDICTS = new Set(["VERIFIED_WITH_SPILL"])', app_js)
        self.assertIn("Clean Verified: ", app_js)
        self.assertIn("Spill Warning: ", app_js)

        green_match = re.search(r"GREEN_VERDICTS\s*=\s*new Set\(\[([^\]]*)\]\)", app_js)
        cyan_match = re.search(r"CYAN_VERDICTS\s*=\s*new Set\(\[([^\]]*)\]\)", app_js)
        self.assertIsNotNone(green_match)
        self.assertIsNotNone(cyan_match)
        assert green_match is not None and cyan_match is not None
        self.assertNotIn("VERIFIED_WITH_SPILL", green_match.group(1))
        self.assertNotIn("VERIFIED_WITH_SPILL", cyan_match.group(1))

        node_bin = shutil.which("node")
        if node_bin is not None:
            # Extract pure helper functions from static/app.js and evaluate in Node
            js_probe = (
                "const fs = require('fs');\n"
                "const src = fs.readFileSync(" + json.dumps(str(VIEWER_DIR / "static" / "app.js")) + ", 'utf8');\n"
                "const body = src.replace(/^\\(function \\(\\) \\{/, '').replace(/if \\(document\\.readyState[\\s\\S]*$/, '');\n"
                "const fn = new Function('document', 'window', body + '\\nreturn { verdictClass, stepHasSpill, statusToVerdictClass };');\n"
                "const api = fn({ getElementById: () => null, createElement: () => ({}) }, {});\n"
                "const vcSpill = api.verdictClass('VERIFIED_WITH_SPILL');\n"
                "const vcClean = api.verdictClass('STATE_VERIFIED');\n"
                "const hasSpill = api.stepHasSpill({ status: 'observed', outcome: 'VERIFIED_WITH_SPILL (spill_regions=2) on ev', action: '' });\n"
                "const stSpill = api.statusToVerdictClass('observed', hasSpill);\n"
                "if (vcSpill.includes('verdict-green') || vcSpill.includes('verdict-cyan')) process.exit(3);\n"
                "if (!vcSpill.includes('verdict-spill') || !vcSpill.includes('verdict-amber')) process.exit(4);\n"
                "if (vcClean !== 'verdict-green') process.exit(5);\n"
                "if (!hasSpill || stSpill.includes('verdict-green') || !stSpill.includes('verdict-spill')) process.exit(6);\n"
            )
            proc = subprocess.run([node_bin, "-e", js_probe], capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0, msg=proc.stderr)

    def test_r4_external_env_pins_refuse_cotampered_replay_and_local_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            frames_dir = root / "frames"
            _write_frame_pair(frames_dir, 0, 5000)
            _write_frame_pair(frames_dir, 1, 5200, rgb=(70, 110, 150))

            events_log = root / "events.jsonl"
            events_log.write_text(
                "".join(json.dumps(r) + "\n" for r in _sample_valid_records(5000, "r4-ev-1")),
                encoding="utf-8",
            )

            vdir = root / "viewer_r4"
            (vdir / "static").mkdir(parents=True, exist_ok=True)
            for asset in ("index.html", "app.js", "style.css"):
                shutil.copy2(VIEWER_DIR / "static" / asset, vdir / "static" / asset)

            res = export_replay(
                events_path=events_log,
                frames_dir=frames_dir,
                out_dir=vdir,
                replay_id="r4-cotamper-id",
            )
            orig_replay_sha = res.replay_sha256

            # Confirm untampered bundle passes with external REPLAY_PINS + REPLAY_REQUIRE_PIN=1
            with mock.patch.dict(
                os.environ,
                {
                    "REPLAY_PINS": f"r4-cotamper-id={orig_replay_sha}",
                    "REPLAY_REQUIRE_PIN": "1",
                },
                clear=False,
            ):
                ok_state = ViewerState(vdir)
                self.assertEqual(ok_state.get_replay_payload("r4-cotamper-id")["attestation"], "pinned")

            # Co-tamper BOTH replays/r4-cotamper-id.json AND static/data/r4-cotamper-id.swarm.json
            # AND PINS.manifest AND plant co-tampered replays/PINS.txt + static/data/PINS.txt!
            r_path = vdir / "replays" / "r4-cotamper-id.json"
            s_path = vdir / "static" / "data" / "r4-cotamper-id.swarm.json"
            manifest_path = vdir / "PINS.manifest"

            s_doc = json.loads(s_path.read_text(encoding="utf-8"))
            s_doc["title"] = "Co-Tampered Title Forged By Local Editor"
            s_tampered_bytes = (json.dumps(s_doc, indent=2) + "\n").encode("utf-8")
            s_path.write_bytes(s_tampered_bytes)
            tampered_swarm_sha = hashlib.sha256(s_tampered_bytes).hexdigest()

            r_doc = json.loads(r_path.read_text(encoding="utf-8"))
            r_doc["title"] = "Co-Tampered Title Forged By Local Editor"
            r_doc["provenance"]["note"] = f"Co-tampered bundle; swarm_sha256={tampered_swarm_sha}"
            r_tampered_bytes = (json.dumps(r_doc, indent=2) + "\n").encode("utf-8")
            r_path.write_bytes(r_tampered_bytes)
            tampered_replay_sha = hashlib.sha256(r_tampered_bytes).hexdigest()
            self.assertNotEqual(tampered_replay_sha, orig_replay_sha)

            manifest_path.write_text(
                f"replay:r4-cotamper-id={tampered_replay_sha}\n"
                f"swarm:r4-cotamper-id={tampered_swarm_sha}\n",
                encoding="utf-8",
            )
            (vdir / "replays" / "PINS.txt").write_text(
                f"r4-cotamper-id={tampered_replay_sha}\n", encoding="utf-8"
            )
            (vdir / "static" / "data" / "PINS.txt").write_text(
                f"r4-cotamper-id={tampered_swarm_sha}\n", encoding="utf-8"
            )

            # With external REPLAY_PINS="<id>=<original_sha256>" and REPLAY_REQUIRE_PIN="1",
            # startup MUST REFUSE fail-closed (even when --pins-file PINS.manifest is passed)!
            with mock.patch.dict(
                os.environ,
                {
                    "REPLAY_PINS": f"r4-cotamper-id={orig_replay_sha}",
                    "REPLAY_REQUIRE_PIN": "1",
                },
                clear=False,
            ):
                with self.assertRaises((ReplayError, ExportError)):
                    ViewerState(vdir)
                with self.assertRaises((ReplayError, ExportError)):
                    ViewerState(vdir, pins_file=manifest_path)
                with self.assertRaises((ReplayError, ExportError)):
                    create_server("127.0.0.1", 0, viewer_dir=vdir, pins_file=manifest_path)

            # Also verify CLI subprocess `server.py` exits non-zero (rc == 2) fail-closed!
            cli_env = dict(
                os.environ,
                PYTHONDONTWRITEBYTECODE="1",
                REPLAY_PINS=f"r4-cotamper-id={orig_replay_sha}",
                REPLAY_REQUIRE_PIN="1",
            )
            proc = subprocess.run(
                [
                    sys.executable,
                    "-B",
                    "-m",
                    "swarm.viewer.server",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    "0",
                    "--viewer-dir",
                    str(vdir),
                    "--pins-file",
                    str(manifest_path),
                ],
                cwd=str(REPO_ROOT),
                env=cli_env,
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(proc.returncode, 2, msg=proc.stderr)
            self.assertIn("ViewerStartupError", proc.stderr)

            # Also verify --pins-file inside mutable replays/ or static/ is always refused
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("REPLAY_PINS", None)
                with self.assertRaises(ExportError):
                    ViewerState(vdir, pins_file=vdir / "replays" / "PINS.txt")
                with self.assertRaises(ExportError):
                    ViewerState(vdir, pins_file=vdir / "static" / "data" / "PINS.txt")

    def test_r6_vendored_replay_store_byte_identical_to_gateway(self) -> None:
        vendored_path = VIEWER_DIR / "replay_store.py"
        gateway_path = REPO_ROOT / "gateway" / "replay_store.py"
        self.assertTrue(vendored_path.is_file(), "swarm/viewer/replay_store.py missing")
        if gateway_path.is_file():
            vendored_bytes = vendored_path.read_bytes()
            gateway_bytes = gateway_path.read_bytes()
            self.assertEqual(
                hashlib.sha256(vendored_bytes).hexdigest(),
                hashlib.sha256(gateway_bytes).hexdigest(),
                "swarm/viewer/replay_store.py drifted from gateway/replay_store.py",
            )
            self.assertEqual(vendored_bytes, gateway_bytes)

    def test_r7_budget_disclosure_and_events_jsonl_sha256(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            frames_dir = root / "frames"
            _write_frame_pair(frames_dir, 0, 5000, rgb=(10, 20, 30))
            _write_frame_pair(frames_dir, 1, 5100, rgb=(30, 40, 50), scene_epoch=1)
            _write_frame_pair(frames_dir, 2, 5200, rgb=(50, 60, 70), scene_epoch=1)

            recs = _sample_valid_records(ts_base=5000, event_id="r7-ev-1", frame_base=0)
            # Add two idle PASS closes so <P> == 2
            for pass_turn, pass_ts in ((2, 5210), (3, 5220)):
                recs.append(
                    {
                        "v": 1,
                        "rec": "turn",
                        "turn": pass_turn,
                        "turn_epoch": pass_turn,
                        "agent_id": "props-agent",
                        "head_frame": 1,
                        "hold_ms": 120000,
                        "ts_ms": pass_ts,
                    }
                )
                recs.append(
                    {
                        "v": 1,
                        "rec": "close",
                        "tick": 1,
                        "turn": pass_turn,
                        "event_id": None,
                        "agent_id": "props-agent",
                        "lane": None,
                        "verdict": "PASS",
                        "rebased": False,
                        "dispatch_capture_seq": None,
                        "verify_frame_ref": 2,
                        "own_regions": [],
                        "spill_regions": [],
                        "drops": 0,
                        "reason": "",
                        "ts_ms": pass_ts + 5,
                    }
                )

            events_log = root / "r7_events.jsonl"
            raw_events = "".join(json.dumps(r) + "\n" for r in recs).encode("utf-8")
            events_log.write_bytes(raw_events)
            expected_events_sha = hashlib.sha256(raw_events).hexdigest()

            out_dir = root / "r7_out"
            res = export_replay(
                events_path=events_log,
                frames_dir=frames_dir,
                out_dir=out_dir,
                replay_id="r7-budget-test",
                max_frames=2,
            )
            self.assertEqual(res.source_events_sha256, expected_events_sha)

            replay_doc = json.loads(res.replay_path.read_text(encoding="utf-8"))
            swarm_doc = json.loads(res.swarm_path.read_text(encoding="utf-8"))
            self.assertEqual(swarm_doc["source_events_sha256"], expected_events_sha)

            expected_disclosure = (
                f"Exported 3 steps (from 9 source records in window; 2 idle PASS closes compacted) "
                f"and 2 frames (from 3 source frames); source events.jsonl sha256={expected_events_sha}."
            )
            self.assertIn(expected_disclosure, replay_doc["limitations"])
            self.assertIn(expected_disclosure, swarm_doc["limitations"])

            # Removing the sha256 disclosure from limitations must fail closed in validate_swarm_bundle
            tampered_swarm = json.loads(json.dumps(swarm_doc))
            tampered_swarm["limitations"] = [
                item for item in tampered_swarm["limitations"] if "source events.jsonl sha256=" not in item
            ]
            with self.assertRaises(ExportError):
                validate_swarm_bundle(
                    tampered_swarm,
                    pins={"r7-budget-test": res.replay_sha256},
                    require_pin=True,
                )

        # Verify UI receipt panel displays source events.jsonl SHA-256 prominently
        index_html = (VIEWER_DIR / "static" / "index.html").read_text(encoding="utf-8")
        app_js = (VIEWER_DIR / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn('id="receipt-events-sha"', index_html)
        self.assertIn("Source events.jsonl SHA-256", index_html)
        self.assertIn('byId("receipt-events-sha")', app_js)

        # Verify all 3 pre-seeded bundles carry matching raw events.jsonl SHA-256
        live_sources = {
            "take2-w2-4agents": REPO_ROOT / "swarm" / "run" / "take2_w2_4agents" / "evidence" / "events.jsonl",
            "take3-w2-guards": REPO_ROOT / "swarm" / "run" / "take3_w2_guards" / "evidence" / "events.jsonl",
            "take1-w1-2agents": REPO_ROOT / "swarm" / "run" / "take1_w1_2agents" / "evidence" / "events.jsonl",
        }
        for rid, src_log in live_sources.items():
            if src_log.is_file():
                raw_sha = hashlib.sha256(src_log.read_bytes()).hexdigest()
                rdoc = json.loads((VIEWER_DIR / "replays" / f"{rid}.json").read_text(encoding="utf-8"))
                sdoc = json.loads((VIEWER_DIR / "static" / "data" / f"{rid}.swarm.json").read_text(encoding="utf-8"))
                self.assertEqual(sdoc["source_events_sha256"], raw_sha)
                self.assertTrue(
                    any(f"source events.jsonl sha256={raw_sha}." in lim for lim in rdoc["limitations"]),
                    f"{rid} missing raw events.jsonl sha256 in replay limitations",
                )

    def test_v1_torn_and_garbage_line_fail_closed_exact_byte_offset(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            frames_dir = root / "frames"
            _write_frame_pair(frames_dir, 0, 5000)
            _write_frame_pair(frames_dir, 1, 5200, rgb=(64, 96, 32))

            recs = _sample_valid_records(ts_base=5000)
            line1 = (json.dumps(recs[0]) + "\n").encode("utf-8")
            line2_torn = json.dumps(recs[1]).encode("utf-8")

            torn_log = root / "torn_events.jsonl"
            torn_log.write_bytes(line1 + line2_torn)
            out_torn = root / "out_torn"

            with self.assertRaises(ExportError) as ctx_torn:
                export_replay(
                    events_path=torn_log,
                    frames_dir=frames_dir,
                    out_dir=out_torn,
                    replay_id="v1-torn-test",
                )
            self.assertEqual(ctx_torn.exception.byte_offset, len(line1))
            self.assertIn(f"byte_offset={len(line1)}", str(ctx_torn.exception))
            self.assertFalse(out_torn.exists(), "torn log must write zero output files")

            env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
            proc_torn = subprocess.run(
                [
                    sys.executable,
                    "-B",
                    "-m",
                    "swarm.viewer.export_replay",
                    "--events",
                    str(torn_log),
                    "--frames",
                    str(frames_dir),
                    "--out-dir",
                    str(out_torn),
                    "--replay-id",
                    "v1-torn-cli",
                ],
                cwd=str(REPO_ROOT),
                env=env,
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(proc_torn.returncode, 0)
            self.assertIn(f"byte_offset={len(line1)}", proc_torn.stderr)
            self.assertFalse(out_torn.exists())

            line2_garbage = b'{"v":1,"rec":"enqueue",GARBAGE_TOKEN}\n'
            line3_valid = (json.dumps(recs[2]) + "\n").encode("utf-8")
            mid_log = root / "garbage_mid.jsonl"
            mid_log.write_bytes(line1 + line2_garbage + line3_valid)
            out_mid = root / "out_mid"

            with self.assertRaises(ExportError) as ctx_mid:
                export_replay(
                    events_path=mid_log,
                    frames_dir=frames_dir,
                    out_dir=out_mid,
                    replay_id="v1-garbage-mid",
                )
            self.assertEqual(ctx_mid.exception.byte_offset, len(line1))
            self.assertIn(f"byte_offset={len(line1)}", str(ctx_mid.exception))
            self.assertFalse(out_mid.exists())

            for label, bad_line in (
                ("utf8", b'{"v":1,"rec":"control","reason":"\xff\xfe"}\n'),
                ("nondict", b"[1, 2, 3]\n"),
                ("badver", b'{"v":2,"rec":"control","op":"stop","by":"broker","reason":"x","ts_ms":5100}\n'),
            ):
                bad_log = root / f"bad_{label}.jsonl"
                bad_log.write_bytes(line1 + bad_line + line3_valid)
                out_bad = root / f"out_{label}"
                with self.assertRaises(ExportError) as ctx_bad:
                    export_replay(
                        events_path=bad_log,
                        frames_dir=frames_dir,
                        out_dir=out_bad,
                        replay_id=f"v1-{label}",
                    )
                self.assertEqual(ctx_bad.exception.byte_offset, len(line1))
                self.assertFalse(out_bad.exists())

    def test_v2_two_run_shared_log_sidecar_cutoff(self) -> None:
        """V2 + R2 mutant kill: ts_ms == start_ts_ms - 1 excluded, == start_ts_ms KEPT, == start_ts_ms + 1 kept."""
        start_ts_ms = 5000
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            frames_dir = root / "frames"
            # Frame 0 (old run) + Frame 1 at exact start_ts_ms - 1 (4999 -> MUST BE EXCLUDED)
            _write_frame_pair(frames_dir, 0, 1000, rgb=(10, 10, 10))
            _write_frame_pair(frames_dir, 1, start_ts_ms - 1, rgb=(20, 20, 20))
            # Frame 2 at exact boundary start_ts_ms (5000 -> MUST BE KEPT!)
            _write_frame_pair(frames_dir, 2, start_ts_ms, rgb=(40, 80, 160), scene_epoch=1)
            # Frame 3 at start_ts_ms + 1 (5001 -> MUST BE KEPT!)
            _write_frame_pair(frames_dir, 3, start_ts_ms + 1, rgb=(80, 160, 220), scene_epoch=2)

            records: list[dict[str, Any]] = [
                # Record at ts_ms == start_ts_ms - 1 (4999) -> MUST BE EXCLUDED
                {
                    "v": 1,
                    "rec": "control",
                    "op": "checkpoint",
                    "by": "broker",
                    "reason": "boundary-minus-1-4999-excluded",
                    "ts_ms": start_ts_ms - 1,
                },
                {
                    "v": 1,
                    "rec": "enqueue",
                    "event_id": "ev-minus-1-4999",
                    "agent_id": "modeler-agent",
                    "lane": "script",
                    "action_class": "state_job",
                    "kind": "job",
                    "based_on_frame": 1,
                    "view_epoch": 0,
                    "scene_epoch": 0,
                    "lease_epoch": 1,
                    "write_set": ["obj:CHAR_Old"],
                    "target_region": None,
                    "footprint": None,
                    "expect": "Prior run excluded at 4999",
                    "script_sha256": "b" * 64,
                    "max_acts": 1,
                    "ts_ms": start_ts_ms - 1,
                },
                # Unique record ONLY at exact boundary ts_ms == start_ts_ms (5000) -> MUST BE KEPT!
                # Mutating `>=` to `>` in export_replay.py drops this record and turns test RED!
                {
                    "v": 1,
                    "rec": "claim",
                    "agent_id": "modeler-agent",
                    "lease_id": "lease-exact-boundary-5000",
                    "lease_epoch": 1,
                    "ttl_ms": 300000,
                    "issued_ts_ms": start_ts_ms,
                    "expires_ts_ms": start_ts_ms + 300000,
                    "allow_gui": False,
                    "write_prefixes": ["col:CHAR_", "obj:CHAR_"],
                },
                {
                    "v": 1,
                    "rec": "control",
                    "op": "resume",
                    "by": "director-agent",
                    "reason": "boundary-exact-5000-must-keep",
                    "ts_ms": start_ts_ms,
                },
                # Unique records at ts_ms == start_ts_ms + 1 (5001) -> MUST BE KEPT!
                {
                    "v": 1,
                    "rec": "enqueue",
                    "event_id": "ev-plus-1-5001",
                    "agent_id": "modeler-agent",
                    "lane": "script",
                    "action_class": "state_job",
                    "kind": "job",
                    "based_on_frame": 2,
                    "view_epoch": 0,
                    "scene_epoch": 1,
                    "lease_epoch": 1,
                    "write_set": ["obj:CHAR_Hull"],
                    "target_region": None,
                    "footprint": None,
                    "expect": "Build hull at 5001",
                    "script_sha256": "c" * 64,
                    "max_acts": 1,
                    "ts_ms": start_ts_ms + 1,
                },
                {
                    "v": 1,
                    "rec": "turn",
                    "turn": 1,
                    "turn_epoch": 1,
                    "agent_id": "modeler-agent",
                    "head_frame": 2,
                    "hold_ms": 120000,
                    "ts_ms": start_ts_ms + 1,
                },
                {
                    "v": 1,
                    "rec": "close",
                    "tick": 1,
                    "turn": 1,
                    "event_id": "ev-plus-1-5001",
                    "agent_id": "modeler-agent",
                    "lane": "script",
                    "verdict": "STATE_VERIFIED",
                    "rebased": False,
                    "dispatch_capture_seq": 2,
                    "verify_frame_ref": 3,
                    "own_regions": [],
                    "spill_regions": [],
                    "drops": 0,
                    "reason": "",
                    "ts_ms": start_ts_ms + 1,
                },
            ]

            shared_log = root / "shared_events.jsonl"
            shared_log.write_text(
                "".join(json.dumps(r) + "\n" for r in records),
                encoding="utf-8",
            )

            sidecar = root / "take.mp4.meta.json"
            sidecar.write_text(
                json.dumps({"v": 1, "start_ts_ms": start_ts_ms, "fps": 15}),
                encoding="utf-8",
            )

            out_dir = root / "viewer_out"
            res = export_replay(
                events_path=shared_log,
                frames_dir=frames_dir,
                out_dir=out_dir,
                replay_id="v2-cutoff-run",
                sidecar_path=sidecar,
            )

            replay_text = res.replay_path.read_text(encoding="utf-8")
            swarm_text = res.swarm_path.read_text(encoding="utf-8")

            # ts_ms == start_ts_ms - 1 (4999) MUST be excluded
            self.assertNotIn("boundary-minus-1-4999-excluded", replay_text)
            self.assertNotIn("boundary-minus-1-4999-excluded", swarm_text)
            self.assertNotIn("ev-minus-1-4999", replay_text)
            self.assertNotIn("ev-minus-1-4999", swarm_text)

            # ts_ms == start_ts_ms (5000) MUST be kept!
            self.assertIn("lease-exact-boundary-5000", replay_text)
            self.assertIn("lease-exact-boundary-5000", swarm_text)
            self.assertIn("boundary-exact-5000-must-keep", replay_text)
            self.assertIn("boundary-exact-5000-must-keep", swarm_text)

            # ts_ms == start_ts_ms + 1 (5001) MUST be kept!
            self.assertIn("ev-plus-1-5001", replay_text)
            self.assertIn("ev-plus-1-5001", swarm_text)

            swarm_doc = json.loads(swarm_text)
            self.assertEqual(swarm_doc["window_start_ts_ms"], start_ts_ms)
            self.assertEqual([c["lease_id"] for c in swarm_doc["claims"]], ["lease-exact-boundary-5000"])
            self.assertEqual([c["reason"] for c in swarm_doc["controls"]], ["boundary-exact-5000-must-keep"])
            exported_source_frames = [f["source_frame"] for f in swarm_doc["frames"]]
            self.assertEqual(exported_source_frames, [2, 3])

            for bad_val in (True, False, -1, 5000.5, "5000"):
                bad_sidecar = root / "bad.meta.json"
                bad_sidecar.write_text(json.dumps({"v": 1, "start_ts_ms": bad_val}), encoding="utf-8")
                with self.assertRaises(ExportError):
                    export_replay(
                        events_path=shared_log,
                        frames_dir=frames_dir,
                        out_dir=root / "bad_out",
                        replay_id="v2-bad-sidecar",
                        sidecar_path=bad_sidecar,
                    )

    def test_v3_xss_label_textcontent_csp_and_safe_json_api(self) -> None:
        app_js = (VIEWER_DIR / "static" / "app.js").read_text(encoding="utf-8")
        index_html = (VIEWER_DIR / "static" / "index.html").read_text(encoding="utf-8")
        forbidden_dom_sinks = (
            "innerHTML",
            "outerHTML",
            "insertAdjacentHTML",
            "document.write",
            "eval(",
        )
        for sink in forbidden_dom_sinks:
            self.assertNotIn(sink, app_js, f"forbidden DOM sink {sink!r} found in static/app.js")
            self.assertNotIn(sink, index_html, f"forbidden sink {sink!r} found in static/index.html")
        self.assertIn("textContent", app_js)
        self.assertNotIn("style=", index_html.lower())

        xss_payload = "<img src=x onerror=alert(1)>"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            frames_dir = root / "frames"
            _write_frame_pair(frames_dir, 0, 5000)
            _write_frame_pair(frames_dir, 1, 5200, rgb=(90, 30, 30))

            recs = _sample_valid_records(ts_base=5000, event_id="xss-ev-1", frame_base=0)
            recs[1]["expect"] = f"Expect {xss_payload}"
            recs[3]["verdict"] = "JOB_ERROR"
            recs[3]["reason"] = f"Reason {xss_payload}"
            recs[4]["label"] = f"Label {xss_payload}"
            recs.append(
                {
                    "v": 1,
                    "rec": "control",
                    "op": "stop",
                    "by": "director-agent",
                    "reason": f"Control {xss_payload}",
                    "ts_ms": 5300,
                }
            )

            events_log = root / "xss_events.jsonl"
            events_log.write_text("".join(json.dumps(r) + "\n" for r in recs), encoding="utf-8")

            viewer_copy = root / "viewer_xss"
            shutil.copytree(VIEWER_DIR / "static", viewer_copy / "static")
            shutil.rmtree(viewer_copy / "static" / "data")
            shutil.rmtree(viewer_copy / "static" / "frames")

            export_replay(
                events_path=events_log,
                frames_dir=frames_dir,
                out_dir=viewer_copy,
                replay_id="xss-check-v1",
                title=f"Title {xss_payload}",
            )

            with mock.patch.dict(os.environ, {"VIEWER_QUIET": "1"}, clear=False):
                os.environ.pop("REPLAY_PINS", None)
                httpd = create_server("127.0.0.1", 0, viewer_dir=viewer_copy)
            port = httpd.server_address[1]
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            try:
                status, headers, raw_body = _raw_http_request(
                    "127.0.0.1", port, "GET", "/api/replays/xss-check-v1"
                )
                self.assertEqual(status, 200)
                csp = headers.get("content-security-policy", "")
                self.assertEqual(csp, CSP_POLICY)
                self.assertIn("script-src 'self'", csp)
                self.assertIn("object-src 'none'", csp)
                self.assertIn("style-src 'self'", csp)
                self.assertEqual(headers.get("x-content-type-options"), "nosniff")

                self.assertNotIn(b"<img", raw_body)
                self.assertIn(b"\\u003cimg src=x onerror=alert(1)\\u003e", raw_body)

                decoded = json.loads(raw_body.decode("utf-8"))
                self.assertIn(xss_payload, decoded["replay"]["title"])
                self.assertIn(xss_payload, decoded["swarm"]["turns"][0]["expect"])
                self.assertIn(xss_payload, decoded["swarm"]["turns"][0]["reason"])
                self.assertIn(xss_payload, decoded["swarm"]["cursors"][0]["label"])
            finally:
                httpd.shutdown()
                httpd.server_close()

    def test_v4_extra_key_pin_mismatch_and_symlink_refusal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            frames_dir = root / "frames"
            _write_frame_pair(frames_dir, 0, 5000)
            _write_frame_pair(frames_dir, 1, 5200, rgb=(100, 120, 40))

            events_log = root / "events.jsonl"
            events_log.write_text(
                "".join(json.dumps(r) + "\n" for r in _sample_valid_records(5000)),
                encoding="utf-8",
            )

            def make_clean_viewer(sub: str) -> Path:
                vdir = root / sub
                (vdir / "static").mkdir(parents=True, exist_ok=True)
                for asset in ("index.html", "app.js", "style.css"):
                    shutil.copy2(VIEWER_DIR / "static" / asset, vdir / "static" / asset)
                export_replay(
                    events_path=events_log,
                    frames_dir=frames_dir,
                    out_dir=vdir,
                    replay_id="v4-bundle",
                )
                return vdir

            # V4a: Extra top-level key in replays/v4-bundle.json (even when re-pinned in PINS.manifest!)
            v_extra = make_clean_viewer("v4_extra_replay")
            r_path = v_extra / "replays" / "v4-bundle.json"
            s_path = v_extra / "static" / "data" / "v4-bundle.swarm.json"
            doc = json.loads(r_path.read_text(encoding="utf-8"))
            doc["unexpected_extra_key"] = True
            mut_bytes = (json.dumps(doc, indent=2) + "\n").encode("utf-8")
            r_path.write_bytes(mut_bytes)
            mut_sha = hashlib.sha256(mut_bytes).hexdigest()
            orig_sw_sha = hashlib.sha256(s_path.read_bytes()).hexdigest()
            (v_extra / "PINS.manifest").write_text(
                f"replay:v4-bundle={mut_sha}\nswarm:v4-bundle={orig_sw_sha}\n",
                encoding="utf-8",
            )

            with self.assertRaises(ReplayError):
                ReplayStore(
                    v_extra / "replays",
                    v_extra / "static",
                    pins={"v4-bundle": mut_sha},
                    require_pin=True,
                )
            with self.assertRaises(ExportError):
                validate_swarm_bundle(
                    s_path,
                    static_dir=v_extra / "static",
                    replay_dir=v_extra / "replays",
                    pins={"v4-bundle": mut_sha},
                    require_pin=True,
                )

            # V4b: Extra top-level key in static/data/v4-bundle.swarm.json (even when PINS.manifest updated)
            v_extra_sw = make_clean_viewer("v4_extra_swarm")
            sw_path = v_extra_sw / "static" / "data" / "v4-bundle.swarm.json"
            orig_r_sha = hashlib.sha256((v_extra_sw / "replays" / "v4-bundle.json").read_bytes()).hexdigest()
            sw_doc = json.loads(sw_path.read_text(encoding="utf-8"))
            sw_doc["rogue_field"] = 123
            sw_bytes = (json.dumps(sw_doc, indent=2) + "\n").encode("utf-8")
            sw_path.write_bytes(sw_bytes)
            sw_sha = hashlib.sha256(sw_bytes).hexdigest()
            (v_extra_sw / "PINS.manifest").write_text(
                f"replay:v4-bundle={orig_r_sha}\nswarm:v4-bundle={sw_sha}\n",
                encoding="utf-8",
            )
            with self.assertRaises(ExportError):
                validate_swarm_bundle(sw_path, require_pin=True)
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("REPLAY_PINS", None)
                with self.assertRaises(ExportError):
                    ViewerState(v_extra_sw)

            # V4c: 1-byte mutation in replays/v4-bundle.json so SHA-256 mismatches PINS.manifest
            v_pin_mis = make_clean_viewer("v4_pin_mismatch")
            r_mis_path = v_pin_mis / "replays" / "v4-bundle.json"
            s_mis_path = v_pin_mis / "static" / "data" / "v4-bundle.swarm.json"
            orig_bytes = r_mis_path.read_bytes()
            tampered_bytes = orig_bytes.replace(b'"1.0.0"', b'"1.0.1"', 1)
            r_mis_path.write_bytes(tampered_bytes)
            pins_mis, sw_pins_mis = load_pins_manifest(v_pin_mis / "PINS.manifest")

            with self.assertRaises(ReplayError):
                ReplayStore(
                    v_pin_mis / "replays",
                    v_pin_mis / "static",
                    pins=pins_mis,
                    require_pin=True,
                )
            with self.assertRaises(ExportError):
                validate_swarm_bundle(
                    s_mis_path,
                    static_dir=v_pin_mis / "static",
                    replay_dir=v_pin_mis / "replays",
                    pins=pins_mis,
                    swarm_pins=sw_pins_mis,
                    require_pin=True,
                )
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("REPLAY_PINS", None)
                with self.assertRaises((ReplayError, ExportError)):
                    ViewerState(v_pin_mis)

            # V4d: Symlink frame in source frames_dir must be rejected fail-closed
            sym_frames = root / "sym_frames"
            _write_frame_pair(sym_frames, 0, 5000)
            _write_frame_pair(sym_frames, 1, 5200)
            real_png = root / "outside.png"
            real_png.write_bytes((sym_frames / "frame_1.png").read_bytes())
            (sym_frames / "frame_1.png").unlink()
            (sym_frames / "frame_1.png").symlink_to(real_png)

            with self.assertRaises(ExportError):
                export_replay(
                    events_path=events_log,
                    frames_dir=sym_frames,
                    out_dir=root / "sym_out",
                    replay_id="v4-symlink",
                )

    def test_v5_live_mode_refusal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            frames_dir = root / "frames"
            _write_frame_pair(frames_dir, 0, 5000)
            _write_frame_pair(frames_dir, 1, 5200, rgb=(50, 120, 90))

            events_log = root / "events.jsonl"
            events_log.write_text(
                "".join(json.dumps(r) + "\n" for r in _sample_valid_records(5000)),
                encoding="utf-8",
            )

            def make_viewer_for_v5(sub: str) -> Path:
                vdir = root / sub
                (vdir / "static").mkdir(parents=True, exist_ok=True)
                for asset in ("index.html", "app.js", "style.css"):
                    shutil.copy2(VIEWER_DIR / "static" / asset, vdir / "static" / asset)
                export_replay(
                    events_path=events_log,
                    frames_dir=frames_dir,
                    out_dir=vdir,
                    replay_id="v5-bundle",
                )
                return vdir

            v_live_label = make_viewer_for_v5("v5_replay_live_true")
            rp = v_live_label / "replays" / "v5-bundle.json"
            sp = v_live_label / "static" / "data" / "v5-bundle.swarm.json"
            ssha = hashlib.sha256(sp.read_bytes()).hexdigest()
            rdoc = json.loads(rp.read_text(encoding="utf-8"))
            rdoc["labels"]["live"] = True
            rdoc["provenance"]["note"] = f"Live test; swarm_sha256={ssha}"
            rbytes = (json.dumps(rdoc, indent=2) + "\n").encode("utf-8")
            rp.write_bytes(rbytes)
            rsha = hashlib.sha256(rbytes).hexdigest()
            (v_live_label / "PINS.manifest").write_text(
                f"replay:v5-bundle={rsha}\nswarm:v5-bundle={ssha}\n",
                encoding="utf-8",
            )

            with self.assertRaises(ReplayError):
                ReplayStore(
                    v_live_label / "replays",
                    v_live_label / "static",
                    pins={"v5-bundle": rsha},
                    require_pin=True,
                )
            with self.assertRaises(ExportError):
                validate_swarm_bundle(sp, require_pin=True)
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("REPLAY_PINS", None)
                with self.assertRaises((ReplayError, ExportError)):
                    create_server("127.0.0.1", 0, viewer_dir=v_live_label)

            v_mode_live = make_viewer_for_v5("v5_mode_live")
            rp2 = v_mode_live / "replays" / "v5-bundle.json"
            sp2 = v_mode_live / "static" / "data" / "v5-bundle.swarm.json"

            sdoc2 = json.loads(sp2.read_text(encoding="utf-8"))
            sdoc2["mode"] = "LIVE"
            sdoc2["live"] = True
            sbytes2 = (json.dumps(sdoc2, indent=2) + "\n").encode("utf-8")
            sp2.write_bytes(sbytes2)
            ssha2 = hashlib.sha256(sbytes2).hexdigest()

            rdoc2 = json.loads(rp2.read_text(encoding="utf-8"))
            rdoc2["mode"] = "LIVE"
            rdoc2["provenance"]["note"] = f"Mode live test; swarm_sha256={ssha2}"
            rbytes2 = (json.dumps(rdoc2, indent=2) + "\n").encode("utf-8")
            rp2.write_bytes(rbytes2)
            rsha2 = hashlib.sha256(rbytes2).hexdigest()
            (v_mode_live / "PINS.manifest").write_text(
                f"replay:v5-bundle={rsha2}\nswarm:v5-bundle={ssha2}\n",
                encoding="utf-8",
            )

            with self.assertRaises(ReplayError):
                ReplayStore(
                    v_mode_live / "replays",
                    v_mode_live / "static",
                    pins={"v5-bundle": rsha2},
                    require_pin=True,
                )
            with self.assertRaises(ExportError):
                validate_swarm_bundle(sp2, require_pin=True)
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("REPLAY_PINS", None)
                with self.assertRaises((ReplayError, ExportError)):
                    create_server("127.0.0.1", 0, viewer_dir=v_mode_live)

    def test_http_integration_healthz_gateway_url_and_standalone_layout(self) -> None:
        # Unit test sanitize_gateway_url rules
        self.assertEqual(
            sanitize_gateway_url("https://viewpoint-gateway.example.run.app"),
            "https://viewpoint-gateway.example.run.app",
        )
        self.assertEqual(sanitize_gateway_url("http://localhost:8081"), "http://localhost:8081")
        self.assertIsNone(sanitize_gateway_url("http://remote-insecure.example.com"))
        self.assertIsNone(sanitize_gateway_url("javascript:alert(1)"))
        self.assertIsNone(sanitize_gateway_url(""))
        self.assertIsNone(sanitize_gateway_url(None))

        with mock.patch.dict(
            os.environ,
            {
                "VIEWER_QUIET": "1",
                "GATEWAY_URL": "https://viewpoint-gateway.example.run.app",
            },
            clear=False,
        ):
            os.environ.pop("REPLAY_PINS", None)
            os.environ.pop("DEFAULT_REPLAY_ID", None)
            httpd = create_server("127.0.0.1", 0, viewer_dir=VIEWER_DIR)

        port = httpd.server_address[1]
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()

        try:
            # GET /healthz & HEAD /healthz -> 200 application/json {"ok": true} + security headers
            st_hz, hdr_hz, body_hz = _raw_http_request("127.0.0.1", port, "GET", "/healthz")
            self.assertEqual(st_hz, 200)
            self.assertIn("application/json", hdr_hz.get("content-type", ""))
            self.assertEqual(hdr_hz.get("content-security-policy"), CSP_POLICY)
            self.assertEqual(hdr_hz.get("x-content-type-options"), "nosniff")
            self.assertEqual(hdr_hz.get("x-frame-options"), "DENY")
            self.assertEqual(json.loads(body_hz.decode("utf-8")), {"ok": True})

            st_hz_head, hdr_hz_head, body_hz_head = _raw_http_request(
                "127.0.0.1", port, "HEAD", "/healthz"
            )
            self.assertEqual(st_hz_head, 200)
            self.assertEqual(body_hz_head, b"")
            self.assertEqual(hdr_hz_head.get("content-security-policy"), CSP_POLICY)

            # GET /
            st_root, hdr_root, body_root = _raw_http_request("127.0.0.1", port, "GET", "/")
            self.assertEqual(st_root, 200)
            self.assertEqual(hdr_root.get("content-security-policy"), CSP_POLICY)
            self.assertIn(b"REPLAY \xc2\xb7 NOT LIVE", body_root)

            # HEAD /
            st_head, hdr_head, body_head = _raw_http_request("127.0.0.1", port, "HEAD", "/")
            self.assertEqual(st_head, 200)
            self.assertEqual(body_head, b"")
            self.assertEqual(hdr_head.get("content-security-policy"), CSP_POLICY)

            # GET /api/replays -> default_replay_id == "take2-w2-4agents", all 3 replays + gateway_url
            st_list, _, body_list = _raw_http_request("127.0.0.1", port, "GET", "/api/replays")
            self.assertEqual(st_list, 200)
            catalog = json.loads(body_list.decode("utf-8"))
            self.assertEqual(catalog["mode"], "REPLAY")
            self.assertIs(catalog["live"], False)
            self.assertEqual(catalog["default_replay_id"], "take2-w2-4agents")
            self.assertEqual(catalog["gateway_url"], "https://viewpoint-gateway.example.run.app")
            self.assertEqual(
                [r["replay_id"] for r in catalog["replays"]],
                ["take2-w2-4agents", "take3-w2-guards", "take1-w1-2agents"],
            )

            # GET /api/replays/take2-w2-4agents
            st_item, _, body_item = _raw_http_request(
                "127.0.0.1", port, "GET", "/api/replays/take2-w2-4agents"
            )
            self.assertEqual(st_item, 200)
            item = json.loads(body_item.decode("utf-8"))
            self.assertEqual(item["replay_id"], "take2-w2-4agents")
            self.assertEqual(item["mode"], "REPLAY")
            self.assertIs(item["live"], False)
            self.assertEqual(item["attestation"], "pinned")
            self.assertEqual(item["swarm"]["schema"], "viewpoint.swarm_timeline/v1")

            first_frame_src = item["replay"]["frames"][0]["src"]
            self.assertEqual(first_frame_src, "/static/frames/take2-w2-4agents_f00.png")

            st_js, hdr_js, body_js = _raw_http_request("127.0.0.1", port, "GET", "/static/app.js")
            self.assertEqual(st_js, 200)
            self.assertIn("javascript", hdr_js.get("content-type", ""))
            self.assertIn(b"textContent", body_js)

            st_png, hdr_png, body_png = _raw_http_request(
                "127.0.0.1", port, "GET", first_frame_src
            )
            self.assertEqual(st_png, 200)
            self.assertEqual(hdr_png.get("content-type"), "image/png")
            self.assertTrue(body_png.startswith(b"\x89PNG\r\n\x1a\n"))

            for verb in ("POST", "PUT", "DELETE"):
                st_mut, hdr_mut, _ = _raw_http_request("127.0.0.1", port, verb, "/")
                self.assertEqual(st_mut, 405, f"expected 405 for {verb}")
                self.assertEqual(hdr_mut.get("allow"), "GET, HEAD")
                self.assertEqual(hdr_mut.get("content-security-policy"), CSP_POLICY)

            for escape_path in (
                "/static/../server.py",
                "/static/%2e%2e/server.py",
                "/static/frames/../../server.py",
                "/../server.py",
            ):
                st_esc, _, body_esc = _raw_http_request("127.0.0.1", port, "GET", escape_path)
                self.assertIn(st_esc, (403, 404), f"unexpected status {st_esc} on {escape_path}")
                self.assertNotIn(b"ThreadingHTTPServer", body_esc)
        finally:
            httpd.shutdown()
            httpd.server_close()

        # Verify Cloud Run standalone `/app` allowlist layout (without `gateway/` parent)
        with tempfile.TemporaryDirectory() as tmp:
            app_dir = Path(tmp) / "isolated_app"
            app_dir.mkdir(parents=True)
            for fname in ("__init__.py", "replay_store.py", "export_replay.py", "server.py", "PINS.manifest"):
                shutil.copy2(VIEWER_DIR / fname, app_dir / fname)
            shutil.copytree(VIEWER_DIR / "static", app_dir / "static")
            shutil.copytree(VIEWER_DIR / "replays", app_dir / "replays")

            outside_secret = Path(tmp) / "outside.txt"
            outside_secret.write_text("do-not-serve", encoding="utf-8")
            (app_dir / "static" / "leak.txt").symlink_to(outside_secret)

            code = (
                "import json, sys; sys.path.insert(0, '.')\n"
                "import http.client, threading, server\n"
                "httpd = server.create_server('127.0.0.1', 0, pins_file='PINS.manifest')\n"
                "port = httpd.server_address[1]\n"
                "t = threading.Thread(target=httpd.serve_forever, daemon=True)\n"
                "t.start()\n"
                "c = http.client.HTTPConnection('127.0.0.1', port, timeout=5)\n"
                "c.request('GET', '/static/leak.txt')\n"
                "r = c.getresponse(); body = r.read(); st_sym = r.status\n"
                "c.request('GET', '/healthz')\n"
                "r2 = c.getresponse(); body2 = r2.read(); st_ok = r2.status\n"
                "httpd.shutdown(); httpd.server_close()\n"
                "assert st_sym == 403, f'expected 403 on symlink, got {st_sym}'\n"
                "assert b'do-not-serve' not in body\n"
                "assert st_ok == 200 and json.loads(body2.decode()) == {'ok': True}\n"
            )
            proc = subprocess.run(
                [sys.executable, "-B", "-I", "-c", code],
                cwd=str(app_dir),
                capture_output=True,
                text=True,
            )
            self.assertEqual(proc.returncode, 0, msg=proc.stderr)

    def test_over_40_frames_sampling_and_optional_video_url(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            frames_dir = root / "frames"
            records: list[dict[str, Any]] = [
                {
                    "v": 1,
                    "rec": "claim",
                    "agent_id": "camera-agent",
                    "lease_id": "lease-cam-1",
                    "lease_epoch": 1,
                    "ttl_ms": 600000,
                    "issued_ts_ms": 1000,
                    "expires_ts_ms": 601000,
                    "allow_gui": True,
                    "write_prefixes": ["cam:", "render:"],
                }
            ]
            for i in range(48):
                _write_frame_pair(
                    frames_dir,
                    i,
                    1000 + i * 100,
                    rgb=((i * 5) % 255, (i * 11) % 255, (i * 17) % 255),
                    view_epoch=i,
                )
            for i in range(47):
                ev_id = f"cam-step-{i:02d}"
                ts = 1000 + i * 100
                records.append(
                    {
                        "v": 1,
                        "rec": "enqueue",
                        "event_id": ev_id,
                        "agent_id": "camera-agent",
                        "lane": "gui",
                        "action_class": "view_change",
                        "kind": "key",
                        "based_on_frame": i,
                        "view_epoch": i,
                        "scene_epoch": 0,
                        "lease_epoch": 1,
                        "write_set": [],
                        "target_region": None,
                        "footprint": [0, 80, 1580, 900],
                        "expect": f"Orbit {i}",
                        "script_sha256": None,
                        "max_acts": 1,
                        "ts_ms": ts + 10,
                    }
                )
                records.append(
                    {
                        "v": 1,
                        "rec": "turn",
                        "turn": i + 1,
                        "turn_epoch": i + 1,
                        "agent_id": "camera-agent",
                        "head_frame": i,
                        "hold_ms": 60000,
                        "ts_ms": ts + 20,
                    }
                )
                records.append(
                    {
                        "v": 1,
                        "rec": "close",
                        "tick": i + 1,
                        "turn": i + 1,
                        "event_id": ev_id,
                        "agent_id": "camera-agent",
                        "lane": "gui",
                        "verdict": "VERIFIED",
                        "rebased": False,
                        "dispatch_capture_seq": i,
                        "verify_frame_ref": i + 1,
                        "own_regions": [[0, 0, 4, 4]],
                        "spill_regions": [],
                        "drops": 0,
                        "reason": "",
                        "ts_ms": ts + 50,
                    }
                )

            events_log = root / "many.jsonl"
            events_log.write_text(
                "".join(json.dumps(r) + "\n" for r in records),
                encoding="utf-8",
            )
            out_dir = root / "out_many"
            res = export_replay(
                events_path=events_log,
                frames_dir=frames_dir,
                out_dir=out_dir,
                replay_id="many-frames-v1",
                video="swarm_composed_hud.mp4",
            )
            self.assertEqual(res.frame_count, 40)
            self.assertLessEqual(res.step_count, 60)

            swarm_doc = json.loads(res.swarm_path.read_text(encoding="utf-8"))
            self.assertEqual(swarm_doc["video_url"], "/static/videos/swarm_composed_hud.mp4")

            with self.assertRaises(ExportError):
                export_replay(
                    events_path=events_log,
                    frames_dir=frames_dir,
                    out_dir=root / "out_bad_vid",
                    replay_id="bad-video-v1",
                    video="../secret.mp4",
                )

    def test_h5r2_1_swarm_json_cryptographically_bound_to_replay_pins(self) -> None:
        """Sol 6.1 #1: Modifying ONLY .swarm.json + updating/deleting PINS.manifest while REPLAY_PINS is set MUST fail closed."""
        # Verify all 3 pre-seeded replays carry valid embedded swarm_sha256 tokens matching static/data/<id>.swarm.json
        for rid in ("take2-w2-4agents", "take3-w2-guards", "take1-w1-2agents"):
            rdoc = json.loads((VIEWER_DIR / "replays" / f"{rid}.json").read_text(encoding="utf-8"))
            embedded_sha = extract_embedded_swarm_sha256(rdoc, f"{rid}.json")
            actual_sw_sha = hashlib.sha256(
                (VIEWER_DIR / "static" / "data" / f"{rid}.swarm.json").read_bytes()
            ).hexdigest()
            self.assertEqual(embedded_sha, actual_sw_sha)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            frames_dir = root / "frames"
            _write_frame_pair(frames_dir, 0, 5000)
            _write_frame_pair(frames_dir, 1, 5200, rgb=(48, 96, 144))

            events_log = root / "events.jsonl"
            events_log.write_text(
                "".join(json.dumps(r) + "\n" for r in _sample_valid_records(5000, "bind-ev-1")),
                encoding="utf-8",
            )

            vdir = root / "viewer_bind"
            (vdir / "static").mkdir(parents=True, exist_ok=True)
            for asset in ("index.html", "app.js", "style.css"):
                shutil.copy2(VIEWER_DIR / "static" / asset, vdir / "static" / asset)

            res = export_replay(
                events_path=events_log,
                frames_dir=frames_dir,
                out_dir=vdir,
                replay_id="h5r2-bind-id",
            )
            valid_replay_sha = res.replay_sha256
            orig_swarm_sha = res.swarm_sha256

            # Untampered bundle loads cleanly with external REPLAY_PINS even when PINS.manifest is deleted
            manifest_path = vdir / "PINS.manifest"
            manifest_backup = manifest_path.read_bytes()
            manifest_path.unlink()
            with mock.patch.dict(
                os.environ,
                {
                    "REPLAY_REQUIRE_PIN": "1",
                    "REPLAY_PINS": f"h5r2-bind-id={valid_replay_sha}",
                },
                clear=False,
            ):
                os.environ.pop("SWARM_PINS", None)
                ok_state = ViewerState(vdir)
                self.assertEqual(ok_state.get_replay_payload("h5r2-bind-id")["swarm_sha256"], orig_swarm_sha)
            manifest_path.write_bytes(manifest_backup)

            # Attack: Modify ONLY static/data/h5r2-bind-id.swarm.json with a schema-valid edit,
            # keeping replays/h5r2-bind-id.json completely untouched (matching REPLAY_PINS)!
            s_path = vdir / "static" / "data" / "h5r2-bind-id.swarm.json"
            s_doc = json.loads(s_path.read_text(encoding="utf-8"))
            s_doc["title"] = "Schema-Valid Tampered Swarm Timeline Title"
            s_tampered_bytes = (json.dumps(s_doc, indent=2) + "\n").encode("utf-8")
            s_path.write_bytes(s_tampered_bytes)
            tampered_swarm_sha = hashlib.sha256(s_tampered_bytes).hexdigest()
            self.assertNotEqual(tampered_swarm_sha, orig_swarm_sha)
            self.assertEqual(
                hashlib.sha256((vdir / "replays" / "h5r2-bind-id.json").read_bytes()).hexdigest(),
                valid_replay_sha,
            )

            # Case A: Attacker updates PINS.manifest to match the modified .swarm.json
            manifest_path.write_text(
                f"replay:h5r2-bind-id={valid_replay_sha}\n"
                f"swarm:h5r2-bind-id={tampered_swarm_sha}\n",
                encoding="utf-8",
            )
            with mock.patch.dict(
                os.environ,
                {
                    "REPLAY_REQUIRE_PIN": "1",
                    "REPLAY_PINS": f"h5r2-bind-id={valid_replay_sha}",
                },
                clear=False,
            ):
                os.environ.pop("SWARM_PINS", None)
                with self.assertRaises(ReplayError):
                    ViewerState(vdir)
                with self.assertRaises(ReplayError):
                    create_server("127.0.0.1", 0, viewer_dir=vdir)
                with self.assertRaises(ReplayError):
                    validate_swarm_bundle(
                        s_path,
                        static_dir=vdir / "static",
                        replay_dir=vdir / "replays",
                        pins={"h5r2-bind-id": valid_replay_sha},
                        swarm_pins=None,
                        require_pin=True,
                    )
                with self.assertRaises(ReplayError):
                    validate_swarm_bundle(
                        s_path,
                        static_dir=vdir / "static",
                        replay_dir=vdir / "replays",
                        require_pin=False,
                    )

            # Case B: Attacker deletes PINS.manifest completely while .swarm.json is tampered
            manifest_path.unlink()
            with mock.patch.dict(
                os.environ,
                {
                    "REPLAY_REQUIRE_PIN": "1",
                    "REPLAY_PINS": f"h5r2-bind-id={valid_replay_sha}",
                },
                clear=False,
            ):
                os.environ.pop("SWARM_PINS", None)
                with self.assertRaises(ReplayError):
                    ViewerState(vdir)
                with self.assertRaises(ReplayError):
                    create_server("127.0.0.1", 0, viewer_dir=vdir)

            # Case C: Replay missing `swarm_sha256=<64-hex>` token in provenance.note MUST fail closed even if re-pinned
            vdir_notoken = root / "viewer_notoken"
            (vdir_notoken / "static").mkdir(parents=True, exist_ok=True)
            for asset in ("index.html", "app.js", "style.css"):
                shutil.copy2(VIEWER_DIR / "static" / asset, vdir_notoken / "static" / asset)
            res2 = export_replay(
                events_path=events_log,
                frames_dir=frames_dir,
                out_dir=vdir_notoken,
                replay_id="h5r2-notoken",
            )
            r2_doc = json.loads(res2.replay_path.read_text(encoding="utf-8"))
            r2_doc["provenance"]["note"] = "Exported without embedded swarm digest token."
            r2_bytes = (json.dumps(r2_doc, indent=2) + "\n").encode("utf-8")
            res2.replay_path.write_bytes(r2_bytes)
            r2_sha = hashlib.sha256(r2_bytes).hexdigest()
            (vdir_notoken / "PINS.manifest").write_text(
                f"replay:h5r2-notoken={r2_sha}\nswarm:h5r2-notoken={res2.swarm_sha256}\n",
                encoding="utf-8",
            )
            with mock.patch.dict(
                os.environ,
                {
                    "REPLAY_REQUIRE_PIN": "1",
                    "REPLAY_PINS": f"h5r2-notoken={r2_sha}",
                },
                clear=False,
            ):
                with self.assertRaises(ReplayError):
                    ViewerState(vdir_notoken)

    def test_h5r2_2_sidecar_cutoff_single_and_all_precutoff_frames_fail_closed(self) -> None:
        """Sol 6.1 #2: Lone pre-cutoff PNG and all-pre-cutoff PNG sets with --sidecar MUST raise ExportError."""
        start_ts_ms = 10_000
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sidecar = root / "cutoff.meta.json"
            sidecar.write_text(json.dumps({"v": 1, "start_ts_ms": start_ts_ms}), encoding="utf-8")

            # Events log with valid in-window records (>= start_ts_ms)
            recs_ref = _sample_valid_records(ts_base=start_ts_ms, event_id="cutoff-lone-1", frame_base=0)
            # Adjust close to reference frame 0 as well so lone frame_0 can be loaded directly
            recs_ref[3]["verify_frame_ref"] = 0
            recs_ref[4]["frame"] = 0
            log_ref = root / "events_ref0.jsonl"
            log_ref.write_text("".join(json.dumps(r) + "\n" for r in recs_ref), encoding="utf-8")

            log_unreferenced = root / "events_unref.jsonl"
            log_unreferenced.write_text(
                json.dumps(
                    {
                        "v": 1,
                        "rec": "control",
                        "op": "checkpoint",
                        "by": "broker",
                        "reason": "in-window-control",
                        "ts_ms": start_ts_ms + 50,
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            # (a) Lone pre-cutoff PNG (ts_ms = start_ts_ms - 1000, mtime = (start_ts_ms - 1000)/1000)
            lone_frames = root / "frames_lone_pre"
            pre_ts = start_ts_ms - 1000
            _write_frame_pair(lone_frames, 0, pre_ts, rgb=(20, 30, 40))
            os.utime(lone_frames / "frame_0.png", (pre_ts / 1000.0, pre_ts / 1000.0))
            os.utime(lone_frames / "frame_0.json", (pre_ts / 1000.0, pre_ts / 1000.0))

            for idx, ev_path in enumerate((log_ref, log_unreferenced)):
                out_lone = root / f"out_lone_{idx}"
                with self.assertRaises(ExportError):
                    export_replay(
                        events_path=ev_path,
                        frames_dir=lone_frames,
                        out_dir=out_lone,
                        replay_id=f"lone-pre-{idx}",
                        sidecar_path=sidecar,
                    )
                self.assertFalse(out_lone.exists(), "failed pre-cutoff export must not create out_dir")

            # (b) Multiple pre-cutoff PNGs (all ts_ms < start_ts_ms, up to start_ts_ms - 1)
            multi_frames = root / "frames_multi_pre"
            for fnum, fts in ((0, start_ts_ms - 3000), (1, start_ts_ms - 1000), (2, start_ts_ms - 1)):
                _write_frame_pair(multi_frames, fnum, fts, rgb=(30 + fnum * 20, 40, 60))
                os.utime(multi_frames / f"frame_{fnum}.png", (fts / 1000.0, fts / 1000.0))
                os.utime(multi_frames / f"frame_{fnum}.json", (fts / 1000.0, fts / 1000.0))

            recs_multi = _sample_valid_records(ts_base=start_ts_ms, event_id="cutoff-multi-1", frame_base=0)
            log_multi = root / "events_multi.jsonl"
            log_multi.write_text("".join(json.dumps(r) + "\n" for r in recs_multi), encoding="utf-8")

            for idx, ev_path in enumerate((log_multi, log_unreferenced)):
                out_multi = root / f"out_multi_{idx}"
                with self.assertRaises(ExportError):
                    export_replay(
                        events_path=ev_path,
                        frames_dir=multi_frames,
                        out_dir=out_multi,
                        replay_id=f"multi-pre-{idx}",
                        sidecar_path=sidecar,
                    )
                self.assertFalse(out_multi.exists())

            # (c) Positive contrast: lone in-window PNG at exact boundary ts_ms == start_ts_ms succeeds
            lone_ok = root / "frames_lone_ok"
            _write_frame_pair(lone_ok, 0, start_ts_ms, rgb=(90, 140, 190))
            res_ok = export_replay(
                events_path=log_ref,
                frames_dir=lone_ok,
                out_dir=root / "out_lone_ok",
                replay_id="lone-ok-id",
                sidecar_path=sidecar,
            )
            self.assertEqual(res_ok.frame_count, 1)

    def test_h5r2_3_output_ancestor_symlink_refused_before_any_mutation(self) -> None:
        """Sol 6.1 #3: Symlinked out_dir or output subdirectories MUST fail closed BEFORE any external mutation."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            frames_dir = root / "frames"
            _write_frame_pair(frames_dir, 0, 5000)
            _write_frame_pair(frames_dir, 1, 5200, rgb=(60, 100, 140))

            events_log = root / "events.jsonl"
            events_log.write_text(
                "".join(json.dumps(r) + "\n" for r in _sample_valid_records(5000, "sym-out-1")),
                encoding="utf-8",
            )

            rid = "sym-guard-id"
            external_dir = root / "external_target"
            external_dir.mkdir(parents=True)
            sentinels = {
                "sentinel.txt": b"untouched-sentinel-payload\n",
                "PINS.txt": b"untouched-external-pins\n",
                f"{rid}_f00.png": b"untouched-frame-00\n",
                f"{rid}_f99.png": b"untouched-frame-99-do-not-prune\n",
                f"{rid}.json": b'{"untouched": true}\n',
                f"{rid}.swarm.json": b'{"untouched_swarm": true}\n',
            }
            for name, content in sentinels.items():
                (external_dir / name).write_bytes(content)

            def snapshot_external() -> dict[str, bytes]:
                return {p.name: p.read_bytes() for p in sorted(external_dir.iterdir())}

            baseline = snapshot_external()

            symlink_cases = [
                ("root_symlink", ()),
                ("replays_symlink", ("replays",)),
                ("static_symlink", ("static",)),
                ("static_data_symlink", ("static", "data")),
                ("static_frames_symlink", ("static", "frames")),
                ("static_frames_rid_symlink", ("static", "frames", rid)),
            ]

            for label, rel_parts in symlink_cases:
                case_out = root / label
                if not rel_parts:
                    case_out.symlink_to(external_dir)
                else:
                    parent_dir = case_out.joinpath(*rel_parts[:-1])
                    parent_dir.mkdir(parents=True, exist_ok=True)
                    parent_dir.joinpath(rel_parts[-1]).symlink_to(external_dir)

                with self.assertRaises(ExportError, msg=f"expected ExportError for {label}"):
                    export_replay(
                        events_path=events_log,
                        frames_dir=frames_dir,
                        out_dir=case_out,
                        replay_id=rid,
                    )
                self.assertEqual(
                    snapshot_external(),
                    baseline,
                    f"external directory was mutated during {label}",
                )

    def test_h5r2_4_gateway_url_fragment_preserved_and_ui_link_wired(self) -> None:
        """Sol 6.1 #4 & DEPLOY_PLAN §7.2 Check 4: GATEWAY_URL fragment preserved & #gateway-link wired safely."""
        frag_url = "https://viewpoint-gateway-xyz.a.run.app/#demo_token=deadbeefcafe1234"
        self.assertEqual(sanitize_gateway_url(frag_url), frag_url)
        self.assertEqual(
            sanitize_gateway_url("http://127.0.0.1:8081/#demo_token=local123"),
            "http://127.0.0.1:8081/#demo_token=local123",
        )
        self.assertIsNone(sanitize_gateway_url("javascript:alert(1)"))
        self.assertIsNone(sanitize_gateway_url("data:text/html,<script>alert(1)</script>"))
        self.assertIsNone(sanitize_gateway_url("https://user:<redacted-email>/"))

        index_html = (VIEWER_DIR / "static" / "index.html").read_text(encoding="utf-8")
        app_js = (VIEWER_DIR / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn(
            '<a id="gateway-link" class="gateway-link" hidden rel="noopener noreferrer" target="_blank">Live Gemini plan (separate service)</a>',
            index_html,
        )
        self.assertGreaterEqual(index_html.count("REPLAY"), 1)
        self.assertIn('byId("gateway-link")', app_js)
        self.assertIn("applyGatewayLink(", app_js)

        # Verify HTTP /api/replays preserves exact fragment URL
        with mock.patch.dict(
            os.environ,
            {"VIEWER_QUIET": "1", "GATEWAY_URL": frag_url},
            clear=False,
        ):
            os.environ.pop("REPLAY_PINS", None)
            httpd_ok = create_server("127.0.0.1", 0, viewer_dir=VIEWER_DIR)
        port_ok = httpd_ok.server_address[1]
        t_ok = threading.Thread(target=httpd_ok.serve_forever, daemon=True)
        t_ok.start()
        try:
            st, _, body = _raw_http_request("127.0.0.1", port_ok, "GET", "/api/replays")
            self.assertEqual(st, 200)
            self.assertEqual(json.loads(body.decode("utf-8"))["gateway_url"], frag_url)

            st_html, _, body_html = _raw_http_request("127.0.0.1", port_ok, "GET", "/")
            self.assertEqual(st_html, 200)
            self.assertIn(b'id="gateway-link"', body_html)
            self.assertGreaterEqual(body_html.count(b"REPLAY"), 1)
        finally:
            httpd_ok.shutdown()
            httpd_ok.server_close()

        # Verify HTTP /api/replays rejects javascript:alert(1)
        with mock.patch.dict(
            os.environ,
            {"VIEWER_QUIET": "1", "GATEWAY_URL": "javascript:alert(1)"},
            clear=False,
        ):
            os.environ.pop("REPLAY_PINS", None)
            httpd_bad = create_server("127.0.0.1", 0, viewer_dir=VIEWER_DIR)
        port_bad = httpd_bad.server_address[1]
        t_bad = threading.Thread(target=httpd_bad.serve_forever, daemon=True)
        t_bad.start()
        try:
            st_b, _, body_b = _raw_http_request("127.0.0.1", port_bad, "GET", "/api/replays")
            self.assertEqual(st_b, 200)
            self.assertIsNone(json.loads(body_b.decode("utf-8"))["gateway_url"])
        finally:
            httpd_bad.shutdown()
            httpd_bad.server_close()

        node_bin = shutil.which("node")
        if node_bin is not None:
            js_probe = (
                "const fs = require('fs');\n"
                "const src = fs.readFileSync(" + json.dumps(str(VIEWER_DIR / "static" / "app.js")) + ", 'utf8');\n"
                "const body = src.replace(/^\\(function \\(\\) \\{/, '').replace(/if \\(document\\.readyState[\\s\\S]*$/, '');\n"
                "const linkEl = { hidden: true, attrs: {}, setAttribute(k, v) { this.attrs[k] = v; }, removeAttribute(k) { delete this.attrs[k]; } };\n"
                "const fn = new Function('document', 'window', body + '\\nreturn { isSafeGatewayUrl, applyGatewayLink };');\n"
                "const api = fn({ getElementById: (id) => id === 'gateway-link' ? linkEl : null, createElement: () => ({}) }, {});\n"
                "const good = " + json.dumps(frag_url) + ";\n"
                "api.applyGatewayLink(good);\n"
                "if (linkEl.hidden !== false || linkEl.attrs.href !== good) process.exit(3);\n"
                "for (const bad of ['javascript:alert(1)', 'data:text/html,x', 'http://evil.example.com', null, '']) {\n"
                "  api.applyGatewayLink(bad);\n"
                "  if (linkEl.hidden !== true || 'href' in linkEl.attrs) process.exit(4);\n"
                "}\n"
                "api.applyGatewayLink('http://localhost:8081/#demo_token=xyz');\n"
                "if (linkEl.hidden !== false || linkEl.attrs.href !== 'http://localhost:8081/#demo_token=xyz') process.exit(5);\n"
            )
            proc = subprocess.run([node_bin, "-e", js_probe], capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0, msg=proc.stderr)

    def test_h5r2_5_cloud_run_preflight_gate_and_home_redaction(self) -> None:
        """DEPLOY_PLAN §6 Step 6: Redact host paths while preserving raw events_sha256; verify CLEAN viewer tree & R6 cmp."""
        home_marker = "/" + "home" + "/"
        aiza_marker = "AI" + "za"
        pk_head = "BEGIN "
        pk_tail = "PRIVATE KEY"
        pk_line_re = re.compile((pk_head + r".*" + pk_tail).encode("ascii"))

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            frames_dir = root / "frames"
            _write_frame_pair(frames_dir, 0, 5000)
            _write_frame_pair(frames_dir, 1, 5200, rgb=(110, 50, 70))

            recs = _sample_valid_records(ts_base=5000, event_id="redact-ev-1", frame_base=0)
            recs[1]["expect"] = f"Inspect {home_marker}operator/scene/main.blend"
            recs[3]["verdict"] = "JOB_ERROR"
            recs[3]["reason"] = (
                f'Traceback (most recent call last):\n  File "{home_marker}operator/repo/swarm/broker/bridge.py", line 179'
            )
            recs[4]["label"] = f"err at {home_marker}operator/repo/script.py"

            raw_jsonl = "".join(json.dumps(r) + "\n" for r in recs).encode("utf-8")
            expected_raw_sha = hashlib.sha256(raw_jsonl).hexdigest()
            events_log = root / "events_with_home.jsonl"
            events_log.write_bytes(raw_jsonl)

            res = export_replay(
                events_path=events_log,
                frames_dir=frames_dir,
                out_dir=root / "out_redacted",
                replay_id="redact-home-v1",
            )
            self.assertEqual(res.source_events_sha256, expected_raw_sha)
            r_txt = res.replay_path.read_text(encoding="utf-8")
            s_txt = res.swarm_path.read_text(encoding="utf-8")
            self.assertNotIn(home_marker, r_txt)
            self.assertNotIn(home_marker, s_txt)
            self.assertIn("<home>/repo/swarm/broker/bridge.py", r_txt)
            self.assertIn("<home>/repo/swarm/broker/bridge.py", s_txt)
            self.assertIn(f"source events.jsonl sha256={expected_raw_sha}.", r_txt)

        # Verify zero files under swarm/viewer/ (excluding __pycache__ / .pyc) match Cloud Run preflight gate patterns
        home_bytes = home_marker.encode("ascii")
        aiza_bytes = aiza_marker.encode("ascii")
        for path in sorted(VIEWER_DIR.rglob("*")):
            if "__pycache__" in path.parts or path.suffix == ".pyc" or not path.is_file():
                continue
            raw = path.read_bytes()
            if b"\x00" in raw:
                # Skip binary PNG images (matching `grep -I` behavior)
                continue
            self.assertNotIn(home_bytes, raw, f"forbidden host path pattern found in {path}")
            self.assertNotIn(aiza_bytes, raw, f"forbidden API key prefix found in {path}")
            for line in raw.splitlines():
                self.assertIsNone(pk_line_re.search(line), f"forbidden key header found in {path}")

        gateway_rs = REPO_ROOT / "gateway" / "replay_store.py"
        viewer_rs = VIEWER_DIR / "replay_store.py"
        if gateway_rs.is_file():
            cmp_proc = subprocess.run(["cmp", "-s", str(gateway_rs), str(viewer_rs)], check=False)
            self.assertEqual(cmp_proc.returncode, 0, "cmp -s gateway/replay_store.py swarm/viewer/replay_store.py failed")


if __name__ == "__main__":
    unittest.main()
