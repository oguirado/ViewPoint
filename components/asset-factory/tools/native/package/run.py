#!/usr/bin/env python3
"""Build a local native plate job through the staged factory runtime."""
import argparse
from fractions import Fraction
import hashlib
import json
from math import ceil
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time

PACKAGE = Path(__file__).resolve().parent
FACTORY = PACKAGE.parents[2]
SLOTS = {
    "STORY": "tools/storyboard/compile.py",
    "TEXT": "tools/text/slot.py",
    "PLANE": "tools/ui_plane/slot.py",
    "EDIT": "tools/edit/slot.py",
    "AUDIO": "tools/audio/slot.py",
}
PYTHON = [sys.executable, "-I", "-B"]
FFMPEG = ["/usr/bin/ffmpeg", "-nostdin", "-v", "error", "-xerror"]
PLATE_BACKENDS = {
    "backend": "plate", "renderer": "ffmpeg", "text": "blender-cairo-cpu",
    "plane": "pillow-cpu", "edit": "ffmpeg-signed", "audio": "f5-presentation-mux",
}
ENVIRONMENT = {"LC_ALL": "C.UTF-8", "LANG": "C.UTF-8", "TZ": "UTC"}
DOMAIN = "film_explainer_voiceover"
F5_GUARDS = {
    "CPU": 0,
    "CUDA": {"rc": 3, "reason": "cuda_admission_unavailable"},
    "sequencer_active": {"rc": 3, "reason": "scene_sequencer_render_unqualified"},
}


class Refusal(Exception):
    """A rejected package, profile or pilot input."""


def require(condition, reason):
    if not condition:
        raise Refusal(reason)


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def write(path, obj):
    path.write_text(json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")


def read(path):
    return json.loads(path.read_text())


def local(root, relative, code="input_path"):
    require(isinstance(relative, str), code)
    path = root / relative
    parts = Path(relative).parts
    require(parts and not Path(relative).is_absolute() and ".." not in parts
            and Path(relative).as_posix() == relative, code)
    for part in parts:
        root = root / part
        require(not root.is_symlink(), code)
    require(path.is_file(), code)
    return path


def admit(job):
    admission = read(PACKAGE / "admission.json")
    require(isinstance(job, dict) and set(job) ==
            {"schema", "locale", "profile", "board", "planes", "edit", "audio"}, "invalid_job_fields")
    require(job["schema"] == "rp4.native-job/v2", "job_schema_unqualified")
    require(isinstance(job["locale"], str)
            and re.fullmatch(r"[a-z]{2,3}(?:-[A-Z]{2})?", job["locale"]), "invalid_content_locale")
    profile = job["profile"]
    require(isinstance(profile, dict) and
            set(profile) == set(PLATE_BACKENDS) | {"device", "sequencer_active", "width", "height", "fps"}
            and all(profile[key] == choice for key, choice in PLATE_BACKENDS.items()),
            "renderer_device_unqualified")
    require(profile["device"] in ("CPU", "CUDA") and type(profile["sequencer_active"]) is bool,
            "renderer_device_unqualified")
    require(all(type(profile[key]) is int and 4 <= profile[key] <= 65536 and profile[key] % 2 == 0
                for key in ("width", "height")), "invalid_canvas")
    fps = profile["fps"]
    require(isinstance(fps, list) and len(fps) == 2 and all(type(n) is int for n in fps)
            and 1 <= fps[0] <= 240 and fps[1] == 1, "text_frame_rate_unqualified")
    require(admission["schema"] == "rp4.native-admission/v1", "package_schema_unqualified")
    for unit, relative in SLOTS.items():
        source = local(FACTORY, relative, f"slot_not_packaged:{unit}")
        require(admission["files"].get(relative) == digest(source), f"slot_digest_mismatch:{unit}")
    for relative, pin in admission["files"].items():
        source = local(FACTORY, relative, f"package_member_unavailable:{relative}")
        require(digest(source) == pin, f"package_digest_mismatch:{relative}")
    return admission


class Build:
    def __init__(self, root, profile):
        self.root = root
        self.profile = profile
        self.width, self.height = profile["width"], profile["height"]
        self.fps = Fraction(*profile["fps"])
        self.commands = []
        self.tmp = root / "tmp"
        self.tmp.mkdir()

    def run(self, argv, *, cwd=None, payload=None, inherit_path=False):
        args = [str(arg) for arg in argv]
        env = {**os.environ, "TMPDIR": str(self.tmp), "PYTHONDONTWRITEBYTECODE": "1"}
        for name in ("ASSET_FACTORY_ROOT", "PYTHONPATH", "PYTHONHOME"):
            env.pop(name, None)
        if not inherit_path:
            env["PATH"] = "/usr/bin:/bin"
        env.update(ENVIRONMENT)
        start = time.monotonic()
        result = subprocess.run(args, cwd=cwd or self.root, env=env, input=payload,
                                capture_output=True, timeout=300)
        self.commands.append({"argv": [arg.replace(str(self.root), ".").replace(str(PACKAGE), "<package>").replace(str(FACTORY), "<factory>")
                                       for arg in args], "rc": result.returncode,
                              "wall_s": round(time.monotonic() - start, 3)})
        if result.returncode:
            out = result.stdout.decode(errors="replace")[-2500:]
            err = result.stderr.decode(errors="replace")[-2500:]
            raise RuntimeError(f"{' '.join(args[:3])} rc={result.returncode}\n{out}\n{err}")
        return result.stdout

    def slot(self, unit, *args):
        return self.run([*PYTHON, FACTORY / SLOTS[unit], *args])

    def render_text(self, shot, cues, font, directory):
        start, end = shot["start_frame"], shot["end_frame"]
        text_job = {
            "schema": "text-slot/job-v1", "font": {"path": "caption.ttf", "sha256": digest(font)},
            "fps": self.fps.numerator, "frame_start": start, "frame_end": end - 1,
            "safe_area": [0.05, 0.05, 0.95, 0.95],
            "profiles": [{"id": "delivery", "width": self.width, "height": self.height}],
            "cues": [
                {"id": cue["id"].rsplit(":", 1)[1], "kind": "caption", "text": cue["text"],
                 "start": max(cue["start_frame"], start), "end": min(cue["end_frame"], end) - 1,
                 "size": 0.047, "box": [0.08, 0.74, 0.92, 0.95],
                 "align": "center", "valign": "bottom", "color": [1, 1, 1, 1],
                 "background": [0.05, 0.13, 0.22, 0.94], "padding": 0.016}
                for cue in cues if cue["id"] in shot["cue_ids"] and cue["kind"] == "caption"
            ],
        }
        if not text_job["cues"]:
            return None
        shutil.copyfile(font, directory / "caption.ttf")
        job = directory / "text-job.json"
        output = directory / "text"
        write(job, text_job)
        # The registered offline CPU caller supplies an inherited-scope adapter on PATH.
        self.run(["systemd-run", "--user", "--scope", "--quiet", "--slice=newton-ia.slice",
                  "-p", "MemoryHigh=6G", "-p", "MemoryMax=8G", "-p", "MemorySwapMax=0",
                  "nice", "-n", "15", "taskset", "-c", "0-15", "blender", "-b", "--factory-startup",
                  "--disable-autoexec", "--python-exit-code", "2", "--python", FACTORY / SLOTS["TEXT"],
                  "--", "build", "--job", job, "--output", output], inherit_path=True)
        return output / "delivery"

    def render_plane(self, shot, source_root, plane, scene, directory):
        start, end = shot["start_frame"], shot["end_frame"]
        surfaces = {surface["id"]: surface for surface in shot["surfaces"]}
        frame_ids = plane.get("frames")
        if frame_ids is None:
            require(len(surfaces) == 1, "still_surface_required")
            frame_ids = list(surfaces)
            kind = "still"
        else:
            require(isinstance(frame_ids, list) and len(frame_ids) == scene["end_frame"] - scene["start_frame"]
                    and all(isinstance(key, str) and key in surfaces for key in frame_ids), "invalid_plane_frames")
            frame_ids = frame_ids[start - scene["start_frame"]:end - scene["start_frame"]]
            kind = "clip"
        media = directory / "media-input"
        media.mkdir()
        frames = []
        for key in frame_ids:
            surface = surfaces[key]
            source = local(source_root, surface["path"])
            pin = surface["sha256"]
            require(digest(source) == pin, "surface_digest_mismatch")
            relative = f"media-input/{pin}.png"
            if not (directory / relative).exists():
                shutil.copyfile(source, directory / relative)
            frames.append({"path": relative, "sha256": pin})
        crop = plane["crop"]
        draft = {
            "schema": "rp3.ui-plane/v1", "backend": self.profile["plane"],
            "canvas": [self.width, self.height], "fps": self.profile["fps"],
            "interval": [start, end], "crop": crop, "viewport": plane["viewport"],
            "view": [{"frame": start, "center": [crop[0] + crop[2] / 2, crop[1] + crop[3] / 2], "zoom": 1}],
            "cursor": [{"frame": frame, "position": xy}
                       for frame, xy in zip(sorted({start, end - 1}), plane.get("cursor", []))],
            "protected": [{"id": "protected-region", "rect": plane["protected"]}],
            "min_ui_scale": 1,
            "source": {"kind": kind, "orientation": 1, "pixel_aspect": [1, 1], "frames": frames},
        }
        request, packed, output = (directory / name for name in ("plane-draft.json", "plane-package", "plane"))
        write(request, draft)
        self.slot("PLANE", "pack", request, packed)
        self.slot("PLANE", "verify", packed)
        self.slot("PLANE", "render", packed, output)
        return output

    def picture_clip(self, source, output, *, first, alpha):
        self.run([*FFMPEG, "-framerate", str(self.fps), "-start_number", first, "-i", source / "%06d.png",
                  "-an", "-c:v", "png", "-pix_fmt", "rgba" if alpha else "rgb24",
                  "-threads:v", "2", "-video_track_timescale", self.fps.numerator, output])
        return {"path": output.relative_to(self.root).as_posix(), "kind": "clip", "sha256": digest(output)}

    def edit(self, spec, shots, assets):
        definitions = {}
        for shot in shots:
            definitions.setdefault(shot["scene_id"], []).append(shot)
        require(len(spec) == len(definitions) and {row["scene"] for row in spec} == set(definitions),
                "edit_must_cover_each_scene_once")
        edited = []
        for row in spec:
            parts = definitions[row["scene"]]
            first = parts[0]["start_frame"]
            start, end = row["in"], row["out"]
            require(type(start) is int and type(end) is int
                    and 0 <= start < end <= parts[-1]["end_frame"] - first, "invalid_edit_trim")
            for shot in parts:
                offset = shot["start_frame"] - first
                low = max(start - offset, 0)
                high = min(end - offset, shot["end_frame"] - shot["start_frame"])
                if low >= high:
                    continue
                ident = shot["id"]
                layer = f"{ident}-text"
                layers = ([{"asset": layer, "start_frame": low, "at_frame": 0,
                            "frames": high - low, "x": 0, "y": 0}] if layer in assets else [])
                edited.append({"id": ident, "asset": f"{ident}-ui", "start_frame": low,
                               "end_frame": high, "layers": layers})
        job = self.root / "edit.json"
        write(job, {"schema": "rp3.edit/v1", "fps": f"{self.fps.numerator}/{self.fps.denominator}", "width": self.width,
                    "height": self.height, "assets": assets, "shots": edited})
        self.sign_edit(job)
        dest = self.root / "edit"
        args = ["--job", job, "--signature", self.root / "edit.sig",
                "--public-key", self.root / "edit.pub", "--asset-root", self.root, "--output", dest]
        self.slot("EDIT", "render", *args)
        self.slot("EDIT", "verify", *args)
        return dest

    def sign_edit(self, job):
        key = self.run(["openssl", "genpkey", "-algorithm", "ED25519"])
        # The local fixture signing key is ephemeral. Persist only the public key and signature.
        (self.root / "edit.pub").write_bytes(self.run(["openssl", "pkey", "-pubout"], payload=key))
        self.run(["openssl", "pkeyutl", "-sign", "-rawin", "-inkey", "/dev/stdin", "-in", job,
                  "-out", self.root / "edit.sig"], payload=key)

    def timeline(self, shots, edit_map):
        definitions = {shot["id"]: shot for shot in shots}
        cuts, source_frames = [], []
        for row in edit_map["frames"]:
            shot = definitions[row["shot"]]
            source_frame = shot["start_frame"] + row["source_frame"]
            source_frames.append(source_frame)
            camera = next(cut["camera"] for cut in reversed(shot["camera_cuts"]) if cut["frame"] <= source_frame)
            if not cuts or camera != cuts[-1]["camera"]:
                at = row["frame"] / self.fps
                cuts.append({"frame": row["frame"], "at": [at.numerator, at.denominator], "camera": camera})
        return cuts, source_frames

    def audio_input(self, spec, project, board, source_frames):
        required = {"mode", "scope", "route", "narration", "silence_disposition"}
        require(isinstance(spec, dict) and required <= spec.keys() <= required | {"voice_domain", "voice_purpose"},
                "invalid_audio_fields")
        require(spec["mode"] in ("narrated", "silent"), "invalid_audio_mode")

        def stage_ref(relative, pin):
            source = local(project, relative, "audio_input_unavailable")
            require(pin == "sha256:" + digest(source), "audio_input_digest_mismatch")
            staged = self.root / "audio-input" / relative
            staged.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, staged)
            return staged.relative_to(self.root).as_posix()

        if spec["mode"] == "silent":
            ref = spec["silence_disposition"]
            require(spec["narration"] is None and isinstance(ref, dict) and set(ref) == {"path", "sha256"},
                    "invalid_silence_disposition")
            staged = stage_ref(ref["path"], ref["sha256"])
            return {**spec, "silence_disposition": {**ref, "path": staged}}

        narration = spec["narration"]
        roles = ("wav", "timing", "consent", "publication_grant")
        fields = {"source_kind", "script_sha256"} | {f"{role}_{suffix}" for role in roles
                                                     for suffix in ("path", "sha256")}
        require(spec["silence_disposition"] is None and isinstance(narration, dict)
                and set(narration) == fields, "invalid_narration_fields")
        narration = narration.copy()
        for role in roles:
            key = f"{role}_path"
            pin = narration[f"{role}_sha256"]
            if role in ("wav", "timing") or narration[key] is not None or pin is not None:
                narration[key] = stage_ref(narration[key], pin)
        timing = read(self.root / narration["timing_path"])
        expected = {}
        voice_text = {}
        for cue in board["cues"]:
            if cue["kind"] != "vo":
                continue
            frames = [index for index, frame in enumerate(source_frames)
                      if cue["start_frame"] <= frame < cue["end_frame"]]
            require(frames and frames == list(range(frames[0], frames[-1] + 1)),
                    "narration_cue_trimmed_or_discontiguous")
            expected[cue["id"]] = (frames[0], frames[-1] + 1)
            voice_text[cue["id"]] = cue["text"]
        cue_order = sorted(expected, key=expected.__getitem__)
        script = "".join(voice_text[key] + "\n" for key in cue_order).encode("utf-8")
        require(narration["script_sha256"] == "sha256:" + hashlib.sha256(script).hexdigest(),
                "narration_script_does_not_match_story")
        cues = timing["cues"]
        require(cue_order and [row["cue_id"] for row in cues] == cue_order and
                {row["cue_id"]: (row["start_frame"], row["end_frame"]) for row in cues} == expected,
                "narration_clock_does_not_match_edit")
        samples_per_frame = Fraction(timing["sample_rate"]) / self.fps
        require(all(ceil(row["start_frame"] * samples_per_frame) <= row["expected_onset_sample"]
                    < row["expected_end_sample"] <= ceil(row["end_frame"] * samples_per_frame)
                    for row in cues), "narration_samples_outside_cue")
        require((1000 * len(source_frames) / self.fps).denominator == 1, "delivery_audio_clock_unqualified")
        return {**spec, "narration": narration}

    def mux(self, board, shots, edit_dir, audio, project):
        edit_map = read(edit_dir / "frame-map.json")
        frame_count = edit_map["frame_count"]
        cuts, source_frames = self.timeline(shots, edit_map)
        inputs = self.audio_input(audio, project, board, source_frames)
        audio_codec = "none" if inputs["mode"] == "silent" else "aac"
        request = self.root / "audio.json"
        write(request, {
            "schema": "af.audio-mux/v1", "kind": "request", "slot": "audio",
            "request_id": board["id"], "voice_domain": DOMAIN, **inputs,
            "picture": {
                "kind": "lossless_mov", "path": "edit/master.mov",
                "sha256": "sha256:" + digest(edit_dir / "master.mov"),
                "width": self.width, "height": self.height, "fps": self.profile["fps"],
                "start_frame": 0, "end_frame": frame_count, "camera_cuts": cuts,
                "frame_hashes": [{"frame": row["frame"], "sha256": "sha256:" + row["rgb24_sha256"]}
                                 for row in edit_map["frames"]],
            },
            "delivery": {"enabled": True, "video_codec": "libx264", "pix_fmt": "yuv420p",
                         "crf": 18, "audio_codec": audio_codec, "audio_bitrate_kbps": 192,
                         "min_pcm_correlation_ppm": 950000},
            "toolchain": {"factory_root": None, "ffmpeg": "/usr/bin/ffmpeg",
                          "ffprobe": "/usr/bin/ffprobe", "timeout_s": 180},
        })
        args = ["--request", request, "--output", self.root / "film",
                "--factory-root", FACTORY, "--require-factory-mux"]
        result = json.loads(self.slot("AUDIO", "run", *args))
        self.slot("AUDIO", "verify", *args)
        require(result["mux_provider"] == "asset_factory.tools.render.presentation_mux",
                "factory_mux_not_selected")
        for media in ("master.mov", "delivery.mp4"):
            self.run([*FFMPEG, "-i", self.root / "film" / media, "-map", "0:v:0", "-map", "0:a?", "-f", "null", "-"])
        return result["metrics"], cuts


def execute(builder, job, job_path, admission):
    root = builder.root
    command = [*PYTHON, PACKAGE / "probe_f5.py", "--device", builder.profile["device"]]
    if builder.profile["sequencer_active"]:
        command.append("--sequencer-active")
    probe = json.loads(builder.run(command))
    requested = probe.pop("requested")
    if probe != F5_GUARDS:
        raise RuntimeError("f5_guard_contract_changed")
    if requested != 0:
        raise Refusal(f"F5 rc={requested['rc']} {requested['reason']}")
    write(root / "f5-guards.json", probe)
    board_path = local(job_path.parent, job["board"])
    font = Path(admission["caption_font"]["path"])
    require(font.is_file() and digest(font) == admission["caption_font"]["sha256"],
            "caption_font_digest_mismatch")
    shots_path = root / "shots.json"
    builder.slot("STORY", "compile", board_path, "--project-root", job_path.parent, "--output", shots_path)
    builder.slot("STORY", "validate", shots_path, "--project-root", job_path.parent,
                 "--expect-sha256", digest(shots_path))
    compiled = read(shots_path)
    board = compiled["storyboard"]
    shots = compiled["shots"]
    require(board["fps"] == job["profile"]["fps"]
            and all(shot["backend"] == job["profile"]["backend"] for shot in shots), "shot_profile_unqualified")
    scenes = {scene["id"]: scene for scene in board["scenes"]}
    require(set(job["planes"]) == set(scenes), "plane_selection_mismatch")
    receipts = {"STORY": [shots_path], "TEXT": [], "PLANE": [], "EDIT": [], "AUDIO": [], "BENCH": []}
    assets = {}
    for shot in shots:
        ident = shot["id"]
        directory = root / ident
        directory.mkdir()
        text = builder.render_text(shot, board["cues"], font, directory)
        scene = scenes[shot["scene_id"]]
        plane = builder.render_plane(shot, job_path.parent, job["planes"][scene["id"]], scene, directory)
        assets[f"{ident}-ui"] = builder.picture_clip(plane / "frames", directory / "ui.mov", first=0, alpha=False)
        if text is not None:
            assets[f"{ident}-text"] = builder.picture_clip(
                text / "plates", directory / "text.mov", first=shot["start_frame"], alpha=True)
            receipts["TEXT"].append(text / "receipt.json")
        receipts["PLANE"].append(plane / "receipt.json")
    edit = builder.edit(job["edit"], shots, assets)
    metrics, cuts = builder.mux(board, shots, edit, job["audio"], job_path.parent)
    audio_receipt = read(root / "film/receipt.json")
    require(audio_receipt["mux_module_sha256"] ==
            "sha256:" + admission["files"]["tools/render/presentation_mux.py"],
            "factory_mux_digest_mismatch")
    receipts["EDIT"].append(edit / "frame-map.json")
    receipts["AUDIO"].extend(root / "film" / filename for filename in ("receipt.json", "result.json"))
    write(root / "bench.json", {
        "schema": "rp4.native-plate-evidence/v1", "profile": job["profile"],
        "frame_count": read(edit / "frame-map.json")["frame_count"],
        "frame_map_sha256": digest(edit / "frame-map.json"),
        "delivery_sha256": digest(root / "film/delivery.mp4"),
    })
    receipts["BENCH"].append(root / "bench.json")
    write(root / "receipt.json", {
        "schema": "rp4.native-build/v2", "unit": "RP3-NATIVE", "status": "PASS",
        "profile": job["profile"], "locale": job["locale"], "job_sha256": digest(job_path),
        "board_sha256": digest(board_path), "source_manifest_sha256": admission["source_manifest_sha256"],
        "voice_kind": (job["audio"]["narration"] or {}).get("source_kind", "silent"),
        "camera_cuts": cuts, "metrics": metrics, "f5_guards": probe,
        "slot_receipts": {unit: [{"path": path.relative_to(root).as_posix(), "sha256": digest(path)}
                                 for path in paths] for unit, paths in receipts.items()},
        "commands": builder.commands,
    })


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--job", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        started = time.monotonic()
        job_path = args.job.resolve(strict=True)
        job = read(job_path)
        admission = admit(job)
        output = args.output.absolute()
        output.parent.resolve(strict=True)
        require(not output.exists() and not output.is_symlink(), "output_exists")
        stage = Path(tempfile.mkdtemp(prefix=".native-", dir=output.parent))
        try:
            builder = Build(stage, job["profile"])
            execute(builder, job, job_path, admission)
            builder.tmp.rmdir()
            stage.rename(output)
        finally:
            if stage.exists():
                shutil.rmtree(stage)
        print(json.dumps({"status": "PASS", "output": str(output),
                          "wall_s": round(time.monotonic() - started, 3)}, sort_keys=True))
        return 0
    except Refusal as exc:
        print(f"NATIVE_REFUSED {exc}", file=sys.stderr)
        return 1
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"NATIVE_ERROR {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
