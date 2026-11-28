# SPDX-License-Identifier: AGPL-3.0-only
"""Offline tests of the installed and source CLI contracts."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from tempfile import TemporaryDirectory
import unittest
import wave

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests/fixtures"
PYTHON = (sys.executable, "-B")
ENV = {key: value for key, value in os.environ.items()
       if key not in {"DISPLAY", "WAYLAND_DISPLAY", "XAUTHORITY", "PYTHONPATH", "PYTHONHOME"}}
ENV.update(LC_ALL="C.UTF-8", LANG="C.UTF-8", TZ="UTC", PYTHONDONTWRITEBYTECODE="1")


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write(path, value):
    path.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")
    return path


class Fixture(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory(prefix="studio-test-")
        self.addCleanup(temporary.cleanup)
        self.work = Path(temporary.name)

    def call(self, *args, rc=0, data=None, timeout=300):
        result = subprocess.run([str(part) for part in args], cwd=self.work, env=ENV,
                                input=data, capture_output=True, timeout=timeout)
        allowed = (rc,) if isinstance(rc, int) else rc
        self.assertIn(result.returncode, allowed, (result.args, result.stdout, result.stderr))
        return result

    def cli(self, path, *args, rc=0, data=None):
        command = [*PYTHON, ROOT / path] if isinstance(path, str) else [path]
        done = self.call(*command, *args, rc=rc, data=data)
        return json.loads(done.stdout or done.stderr)

    def refused(self, path, reason, *args, data=None):
        result = self.cli(path, *args, rc=1, data=data)
        self.assertEqual(result["reason"], reason, result)
        self.assertTrue(result["retry_when"])
        self.assertTrue(result["next_command"])
        return result

    def ffmpeg(self, *argv):
        self.call("ffmpeg", "-v", "error", "-nostdin", "-threads", "1", "-filter_threads", "1", *argv)

    def picture(self, path, color="red"):
        self.ffmpeg("-f", "lavfi", "-i", f"color={color}:size=64x48:rate=24",
                    "-frames:v", "8", "-c:v", "ffv1", "-threads", "1", path)
        return path

    def library(self):
        sources = self.work / "assets"
        sources.mkdir()
        scene = sources / "switch-proxy.obj"
        scene.write_text("v 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n", encoding="utf-8")
        catalogue = self.work / "catalogue"
        self.cli("library/asset_db.py", "--library", catalogue, "index", "--root", f"fixture={sources}", "--jobs", "1")
        snapshot = catalogue / "snapshots" / (catalogue / "CURRENT").read_text().strip()
        return scene, catalogue, snapshot

    def short(self):
        sources = [self.picture(self.work / f"{color}.mkv", color) for color in ("red", "blue")]
        mix = self.work / "mix.wav"
        with wave.open(str(mix), "wb") as audio:
            audio.setparams((1, 2, 48000, 0, "NONE", "not compressed"))
            audio.writeframes(bytes(32000))
        doc = {"schema": "viewpoint.short-edit/v1", "fps": 24, "pixels": [64, 48],
               "shots": [{"id": name, "source_cut": [2, 6], "media": {"path": path.name, "sha256": sha(path)}}
                         for name, path in zip(("first", "second"), sources, strict=True)],
               "mix": {"path": mix.name, "sha256": sha(mix)},
               "captions": [{"start": 4, "end": 8, "text": "[Signal]"}]}
        return write(self.work / "timeline.json", doc)

    def review(self):
        movie = self.work / "source.mp4"
        self.ffmpeg("-f", "lavfi", "-i", "color=red:size=64x48:rate=24",
                    "-frames:v", "8", "-c:v", "libx264", "-threads", "1", movie)
        contacts = write(self.work / "contacts.json", {"video_sha256": sha(movie),
                         "hand_button": {"first_index": 2, "last_index": 4},
                         "foot_plant": [{"first_index": 1, "last_index": 5}]})
        declared = self.work / "declared.json"
        self.cli("review/rp_review.py", "declared", "--movie-sha", sha(movie),
                 "--contacts", contacts, "--out", declared)
        return movie, declared

    def series(self):
        demo = self.work / "demo"
        self.call(*PYTHON, FIXTURES / "series/fixture.py", demo)
        policy = demo / "policy.json"
        flags = ("--fixture", "--destination", "fixture-local", "--policy", policy,
                 "--policy-sha256", sha(policy), "--evidence", demo / "evidence")
        return demo, demo / "source/project.json", flags


class ProductionTests(Fixture):
    def test_clean_install_discovery_dry_run_and_mcp(self):
        installed = self.work / "prefix"
        self.call(ROOT / "install.sh", installed)
        launcher = installed / "bin/viewpoint-studio"
        registry = self.cli(launcher, "--list-capabilities")
        self.assertEqual(len(registry["slots"]), 21)
        pin = registry["registry_sha256"]
        mcp = self.cli(launcher, "--mcp-manifest", "--expect-registry", pin)
        self.assertEqual(mcp["registry_sha256"], pin)
        self.assertEqual({tool["name"] for tool in mcp["tools"]},
                         {"vp_studio_capabilities", "vp_studio_dry_run", "vp_studio_run"})
        for slot, row in registry["slots"].items():
            inputs = [{"role": role, "text": "carry"} if role in {"text", "prompt"} else
                      {"role": role, "path": f"{role}.json", "sha256": "b" * 64, "media_type": "application/json"}
                      for role in row["input_roles"][0]]
            request = {"schema": row["contract"], "slot": slot, "kind": "request", "request_id": "fixture-" + slot,
                       "provider_id": "fixture", "provider_revision": "fixture-v1", "inputs": inputs,
                       "params": {}, "context": {"timeout_s": 10, "policy_sha256": "a" * 64, "toolchain_sha256": "c" * 64}}
            result = self.cli(launcher, "--dry-run", "--expect-registry", pin, data=json.dumps(request).encode())
            self.assertEqual((result["slot"], result["status"]), (slot, "VALIDATED"))
        data = b'{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}\n'
        self.assertEqual(self.cli(launcher, "--mcp", "--expect-registry", pin, data=data)["result"]["tools"], mcp["tools"])
        self.assertEqual({row["name"] for row in self.cli(launcher, "--episode", "--list-capabilities")},
                         {"af.episode", "af.episode.status"})

    def test_episode_background_render_archive_restore_and_resume(self):
        prefix = self.work / "prefix"
        self.call(ROOT / "install.sh", prefix)
        runtime = prefix / "lib/viewpoint-studio"
        launcher = prefix / "bin/viewpoint-studio"
        env = dict(ENV, PYTHONPATH=f"{runtime}:{FIXTURES / 'episode'}")
        for name in ("assets", "state", "archive", "restore", "setup"):
            (self.work / name).mkdir()
        provision = subprocess.run([*PYTHON, "-c", '''
from pathlib import Path
from make import make
from viewpoint.episode_local.common import write
import sys
root = Path(sys.argv[1])
write(root / "policy.json", make(root / "assets", root / "setup"))
''', str(self.work)], cwd=self.work, env=env, timeout=120, capture_output=True)
        self.assertEqual(provision.returncode, 0, (provision.stdout, provision.stderr,
            [(p.name, p.read_text(errors="replace")) for p in (self.work / "setup").glob("*.log")]))
        policy = self.work / "policy.json"
        flags = ("--episode", "--artifact-root", self.work / "assets", "--episode-state-root", self.work / "state",
                 "--episode-archive-root", self.work / "archive", "--episode-restore-root", self.work / "restore",
                 "--episode-policy", policy, "--episode-policy-sha256", sha(policy))
        request = {"schema": "viewpoint.episode-request/v1", "request_id": "offline-episode", "op": "af.episode",
                   "args": {"recipe": "recipe.json", "sha256": sha(self.work / "assets/recipe.json")}}
        encoded = json.dumps(request).encode()
        deadline = time.monotonic() + 150
        while True:
            outcome = self.call(launcher, *flags, data=encoded, rc=(0, 1))
            executed = json.loads(outcome.stdout)
            if outcome.returncode == 0:
                break
            self.assertIn(executed["reason"], {"ram_admission_hold", "ram_checkpoint_hold"}, executed)
            self.assertLess(time.monotonic(), deadline, executed)
            time.sleep(3)
        self.assertEqual(executed["status"], "RESTORED")
        self.assertEqual(executed, self.cli(launcher, *flags, data=encoded))
        request["op"] = "af.episode.status"
        self.assertEqual(executed, self.cli(launcher, *flags, data=json.dumps(request).encode()))
        self.assertTrue((self.work / "restore/restored/offline-episode/master.mp4").is_file())
        self.assertEqual(len(list((self.work / "archive/objects").glob("*.tar"))), 1)

    def test_editorial_boards_light_sound_and_conform(self):
        self.assertTrue(self.cli("editorial/editorial.py", "discover"))
        delivery = self.work / "delivery"
        result = self.cli("editorial/editorial.py", "execute", "--project", FIXTURES / "editorial",
                          "--request", FIXTURES / "editorial/request.json", "--out", delivery)
        self.assertEqual(result["status"], "DELIVERED")
        for name in ("boards.json", "blocking.json", "timeline.json", "master.mp4"):
            self.assertTrue((delivery / name).is_file(), name)
        revision = self.work / "revision"
        conform = self.cli("editorial/conform.py", "--timeline", delivery / "timeline.json", "--out", revision)
        self.assertEqual(conform["sha256"], result["conform"]["sha256"])

    def test_short_assembly_is_reproducible(self):
        timeline = self.short()
        first = self.cli("short/assemble.py", "--timeline", timeline, "--out", self.work / "a")
        second = self.cli("short/assemble.py", "--timeline", timeline, "--out", self.work / "b")
        self.assertEqual(first, second)
        self.assertEqual(first["frames"], 8)
        self.assertEqual(set(first["files"]), {"master.mp4", "picture-lossless.mkv", "captions.srt"})
        decoded = self.call("ffmpeg", "-v", "error", "-i", self.work / "a/picture-lossless.mkv",
                            "-vf", "scale=1:1", "-pix_fmt", "rgb24", "-threads", "1", "-f", "rawvideo", "-").stdout
        self.assertEqual(len(decoded), 24)
        self.assertGreater(decoded[0], decoded[2])
        self.assertGreater(decoded[-1], decoded[-3])

    def test_catalogue_fts_curation_and_generic_rclone(self):
        _, catalogue, snapshot = self.library()
        script, args = "library/asset_db.py", ("--library", catalogue)
        hit, = self.cli(script, *args, "find", "switch")["results"]
        show = self.cli(script, *args, "show", hit["asset_id"])
        self.assertEqual(show["kind"], "mesh")
        verify = self.cli(script, *args, "verify")
        self.assertEqual(verify["assets_verified"], 1)
        plan = self.cli(script, *args, "sync", "--remote", "archive-fixture:", "--root", "studio/assets", "--dry-run")
        self.assertEqual(plan["destination"], "archive-fixture:studio/assets")
        self.assertEqual(plan["network_calls"], 0)
        staged = self.cli(script, *args, "stage", "--plan", plan["plan_file"], "--index", plan["local_index_file"],
                          "--destination", self.work / "local-stage")
        self.assertEqual(staged["network_calls"], 0)
        opts = ("--snapshot", snapshot, "--catalog-sha256", sha(snapshot / "catalog.sqlite3"),
                "--index", self.work / "curation.sqlite3")
        self.cli("library/library.py", *opts, "build")
        self.assertTrue(self.cli("library/library.py", *opts, "find", "switch")["results"])
        self.assertFalse(self.cli("library/library.py", *opts, "find", "switch", "--rights", "private")["results"])

    def test_series_continuity_and_zip_round_trip(self):
        demo, project, flags = self.series()
        baseline = self.cli("series/series.py", "check", "--project", project)
        (demo / "one").mkdir()
        (demo / "two").mkdir()
        first = self.cli("series/series.py", "export", "--project", project, "--output", demo / "one", *flags)
        second = self.cli("series/series.py", "export", "--project", project, "--output", demo / "two", *flags)
        self.assertEqual(first["snapshot_sha256"], second["snapshot_sha256"])
        bundle = demo / "one" / (first["snapshot_sha256"] + ".zip")
        self.cli("series/series.py", "import", "--archive", bundle, "--snapshot-sha256", first["snapshot_sha256"],
                 "--into", demo / "restored", *flags)
        self.assertEqual(baseline, self.cli("series/series.py", "check", "--project", demo / "restored/project.json"))

    def test_blinded_review_pack_replays_and_restores(self):
        movie, declared = self.review()
        pack = self.work / "pack"
        review = "review/rp_review.py"
        built = self.cli(review, "build", "--movie", movie, "--sha256", sha(movie), "--declared", declared, "--pack", pack)
        verified = self.cli(review, "verify", "--pack", pack, "--movie", movie, "--sha256", sha(movie))
        self.assertEqual(built["manifest_sha256"], verified["manifest_sha256"])
        self.assertGreaterEqual(built["sample_count"], 5)
        self.assertEqual(self.cli(review, "replay", "--pack", pack)["manifest_sha256"], built["manifest_sha256"])
        token = json.loads((pack / "manifest.json").read_text())["samples"][0]
        frame = pack / token["file"]
        frame.unlink()
        self.cli(review, "restore", "--pack", pack)
        self.assertEqual(sha(frame), token["png_sha256"])

    def test_review_panel_blinds_context_and_holds_without_cards(self):
        movie, declared = self.review()
        pack = self.work / "pack"
        review = "review/rp_review.py"
        self.cli(review, "build", "--movie", movie, "--sha256", sha(movie), "--declared", declared, "--pack", pack)
        config = self.work / "protocol"
        shutil.copytree(ROOT / "review/protocol", config)
        protocol_path = config / "PROTOCOL.json"
        protocol = json.loads(protocol_path.read_text())
        seats = self.work / "seats"
        seats.mkdir()
        for slot in ("viewer-1", "viewer-2", "reserve", "judge"):
            key = {"seat": slot, "model": "fixture-" + slot, "family": "fixture", "adapter": "offline"}
            protocol["seats"][slot] = key
            write(seats / (slot + ".json"), {"schema": "swarm.seat.v1", "name": slot, "model": key["model"],
                                            "family": key["family"], "launcher": key["adapter"],
                                            "expected_model_substr": key["model"]})
        write(protocol_path, protocol)
        workspace = self.work / "workspace"
        workspace.mkdir()
        flags = ("--protocol", protocol_path, "--seat-registry", seats)
        panel = ("--root", workspace, "--pack", pack)
        self.assertTrue(self.cli(review, *flags, "register", *panel)["independent"])
        phrase = "A sealed fictional objective appears only in the brief."
        note = self.work / "brief.txt"
        note.write_text(phrase, encoding="utf-8")
        self.cli(review, *flags, "brief", *panel, "--text", note, "--source", "synthetic")
        result = self.cli(review, *flags, "blind-ctx", *panel, "--slot", "viewer-1", "--segment", 0)
        self.assertEqual(result["movie_sha256"], sha(movie))
        context, = workspace.rglob("ctx.md")
        self.assertNotIn(phrase, context.read_text())
        hold = self.refused(review, "panel_incomplete", *flags, "score", *panel)
        self.assertEqual(hold["verdict"], "HOLD")
        self.assertIn("missing_card:viewer-1", hold["reasons"])
        protocol["thresholds"]["min_axis"] = 1
        write(protocol_path, protocol)
        self.refused(review, "registered_protocol_changed", *flags, "score", *panel)

    def test_reuse_frame_comparison_measures_exact_pixels(self):
        first = self.work / "first.png"
        second = self.work / "second.png"
        from PIL import Image
        Image.new("RGB", (4, 3), (10, 21, 32)).save(first)
        Image.new("RGB", (4, 3), (10, 22, 32)).save(second)
        result = self.cli("library/reuse_proof.py", "compare", "--first", first, "--first-sha256", sha(first),
                          "--second", second, "--second-sha256", sha(second))
        self.assertEqual((result["byte_identical"], result["max_abs_diff"], result["differing_channels"]), (False, 1, 12))

    def test_registered_adapter_conforms_pinned_frames_and_captions(self):
        from PIL import Image

        render = self.work / "renders"
        shots = [{"id": "first", "source": {"in": 2, "out": 3}},
                 {"id": "second", "source": {"in": 1, "out": 2}}]
        bound = write(self.work / "bound.json", {
            "schema": "viewpoint.episode-bound/v1", "shots": shots, "pixels": [64, 48],
            "entry_point": {"shot_script": {"sha256": sha(ROOT / "adapter/episode_shot.py")}},
            "timebase": {"edit_rate_fps": 24}, "total_edit_frames": 4,
            "cues": [{"id": "signal", "edit_frame": 2, "caption": "[Signal]"}],
        })
        for shot, color in zip(shots, ("red", "blue"), strict=True):
            folder = render / shot["id"] / "frames"
            folder.mkdir(parents=True)
            pins = []
            for number in range(1, 5):
                frame = folder / f"{number:03d}.png"
                Image.new("RGB", (64, 48), color).save(frame)
                pins.append({"frame": number, "sha256": sha(frame)})
            write(folder.parent / "shot-receipt.json",
                  {"status": "PASS", "bound_sha256": sha(bound), "shot": shot["id"], "frames": pins})
        cut = self.work / "cut"
        data = self.cli("adapter/episode.py", "conform", "--bound", bound, "--sha256", sha(bound),
                        "--renders", render, "--out", cut)
        self.assertEqual(data["frames"], 4)
        self.assertEqual(data["shots"], [{"end": 2, "id": "first", "start": 0}, {"end": 4, "id": "second", "start": 2}])
        self.assertEqual(sha(cut / "frames/0000.png"), sha(render / "first/frames/002.png"))
        self.assertEqual(sha(cut / "frames/0002.png"), sha(render / "second/frames/001.png"))
        self.assertIn("00:00:00,083 --> 00:00:00,167", (cut / "captions.srt").read_text())

    def test_registered_adapter_refuses_dropped_shot(self):
        _, catalogue, snapshot = self.library()
        data = {"schema": "viewpoint.assemble-resolved/v1", "episode_id": "fixture",
                "snapshot": snapshot.name, "fps": 24, "resolution": [64, 48], "shots": [{"id": "first"}]}
        intent = {"schema": "viewpoint.edit-intent/v1", "episode_id": "fixture", "shots": [{"id": "second"}]}
        self.refused("adapter/episode.py", "edit_shot_dropped", "bind", "--library", catalogue,
                     "--resolved", write(self.work / "resolve.json", data),
                     "--intent", write(self.work / "intent.json", intent), "--material-name", "Fixture PBR",
                     "--out", self.work / "bound.json")
        self.assertFalse((self.work / "bound.json").exists())


class NegativeTests(Fixture):
    def test_registry_refuses_mismatched_release(self):
        self.refused(ROOT / "bin/viewpoint-studio", "registry_stale", "--list-capabilities", "--expect-registry", "0" * 64)

    def test_episode_requires_provisioned_roots(self):
        self.refused(ROOT / "bin/viewpoint-studio", "local_profile_unconfigured", "--episode", data=b"{}")

    def test_editorial_refuses_stale_request(self):
        project = self.work / "project"
        shutil.copytree(FIXTURES / "editorial", project)
        source = project / "lighting.json"
        source.write_bytes(source.read_bytes() + b"\n")
        self.refused("editorial/editorial.py", "request_identity_conflict", "execute", "--project", project,
                     "--request", project / "request.json", "--out", self.work / "out")

    def test_short_refuses_stale_picture(self):
        timeline = self.short()
        timeline.parent.joinpath("blue.mkv").write_bytes(b"changed")
        self.refused("short/assemble.py", "stale_picture", "--timeline", timeline, "--out", self.work / "out")
        self.assertFalse((self.work / "out").exists())

    def test_series_refuses_substituted_archive(self):
        demo, project, flags = self.series()
        out = demo / "bundles"
        out.mkdir()
        receipt = self.cli("series/series.py", "export", "--project", project, "--output", out, *flags)
        self.refused("series/series.py", "snapshot_digest_mismatch", "import", "--archive", receipt["archive"],
                     "--snapshot-sha256", "0" * 64, "--into", demo / "restore", *flags)

    def test_catalogue_refuses_unplanned_transfer(self):
        _, catalogue, _ = self.library()
        self.refused("library/asset_db.py", "dry_run_required", "--library", catalogue, "sync",
                     "--remote", "fixture:", "--root", "studio/assets")

    def test_review_refuses_wrong_movie(self):
        movie, declared = self.review()
        self.refused("review/rp_review.py", "movie_hash_mismatch", "build", "--movie", movie,
                     "--sha256", "0" * 64, "--declared", declared, "--pack", self.work / "pack")

    def test_reuse_refuses_pixel_drift(self):
        from PIL import Image
        first, second = self.work / "a.png", self.work / "b.png"
        Image.new("RGB", (2, 2), (0, 0, 0)).save(first)
        Image.new("RGB", (2, 2), (0, 0, 10)).save(second)
        self.refused("library/reuse_proof.py", "replay_not_reproducible", "compare",
                     "--first", first, "--first-sha256", sha(first),
                     "--second", second, "--second-sha256", sha(second))
