# SPDX-License-Identifier: AGPL-3.0-only
"""Episode binding, per-shot render commits, editorial conform and durable delivery."""
import fcntl
import math
import os
import shutil
import sys
import tarfile
import tempfile
import wave
from array import array
from pathlib import Path

from . import common as C
from . import host

ROOT = Path(__file__).resolve().parent
BLENDER = ("/usr/bin/blender", "-b", "--factory-startup", "--disable-autoexec",
           "-noaudio", "--python-exit-code", "2", "--python", str(ROOT / "episode_shot.py"), "--")
FFMPEG = ("/usr/bin/ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-xerror")
FFPROBE = ("/usr/bin/ffprobe", "-v", "error")
ROLES = ("scene", "clip", "camera")
EFFECTS = ["render", "mux", "archive", "restore"]


def replay_identity(request, policy_sha, roots):
    files = [*sorted(ROOT.glob("*.py")), ROOT.parent / "handlers.py",
             Path(BLENDER[0]), Path(FFMPEG[0]), Path(FFPROBE[0]), Path(sys.executable)]
    revisions = {file.name: C.sha_file(file) for file in files}
    return {"request_sha256": C.digest(C.encoded(request)), "policy_sha256": policy_sha,
            "runtime_sha256": C.digest(C.encoded(revisions)), "revisions": revisions,
            "memory_max_bytes": host.cap_bytes(),
            "roots_sha256": C.digest(C.encoded([str(root) for root in roots]))}


def keys(obj, expected, reason):
    C.require(isinstance(obj, dict) and obj.keys() == set(expected.split()), reason)


def integer(value, low, high):
    return type(value) is int and low <= value <= high


def position(value):
    return isinstance(value, list) and len(value) == 3 and all(
        type(item) in (int, float) and -100000 <= item <= 100000 for item in value)


def reference(ref):
    keys(ref, "path sha256", "invalid_artifact_reference")
    C.relative(ref["path"])
    C.require(isinstance(ref["sha256"], str) and C.DIGEST.fullmatch(ref["sha256"]), "invalid_artifact_reference")


def validate_shot(assets, shot, approved):
    keys(shot, "id scene clip camera source cut", "invalid_shot")
    C.require(approved is not None, "shot_rights_denied")
    keys(approved, "scene clip camera", "invalid_policy")
    for role in ROLES:
        ref = shot[role]
        reference(ref)
        C.require(ref == approved[role], f"{role}_substitution", shot["id"])
        C.pin(assets, ref, f"{role}_substitution")
    for field in ("source", "cut"):
        rng = shot[field]
        C.require(isinstance(rng, list) and len(rng) == 2 and all(integer(n, 1, 100000) for n in rng),
                  "invalid_frame_range")
    start, end = shot["source"]
    first, last = shot["cut"]
    C.require(start <= first <= last <= end, "invalid_frame_range")

    camera = C.load(C.pin(assets, shot["camera"], "camera_substitution"))
    keys(camera, "name location_m target_m lens_mm sensor_width_mm", "invalid_camera")
    C.require(isinstance(camera["name"], str) and camera["name"]
              and position(camera["location_m"]) and position(camera["target_m"])
              and camera["location_m"] != camera["target_m"]
              and integer(camera["lens_mm"], 1, 250) and integer(camera["sensor_width_mm"], 1, 100),
              "invalid_camera")
    clip = C.load(C.pin(assets, shot["clip"], "clip_substitution"))
    keys(clip, "name actor signal keys", "invalid_clip")
    C.require(all(isinstance(clip[name], str) and clip[name] for name in ("name", "actor", "signal"))
              and clip["actor"] != clip["signal"] and isinstance(clip["keys"], list) and len(clip["keys"]) >= 2,
              "invalid_clip")
    past = start - 1
    for key in clip["keys"]:
        keys(key, "frame location signal_on", "invalid_clip")
        C.require(integer(key["frame"], past + 1, end) and position(key["location"])
                  and type(key["signal_on"]) is bool, "invalid_clip")
        past = key["frame"]
    C.require(clip["keys"][0]["frame"] == start and past == end, "invalid_clip")


def bind(assets, request, policy, identity):
    keys(policy, "schema profile rights bindings ceiling destination", "invalid_policy")
    C.require(policy["schema"] == "viewpoint.episode-policy/v1" and policy["profile"] == "local-private-archive"
              and policy["destination"] == "private-local" and isinstance(policy["bindings"], dict), "destination_denied")
    rights = policy["rights"]
    keys(rights, "source license effects", "invalid_rights")
    C.require(all(isinstance(rights[name], str) and rights[name].strip() for name in ("source", "license"))
              and rights["effects"] == EFFECTS, "rights_denied")
    cap = policy["ceiling"]
    keys(cap, "shots render_frames pixels_per_frame", "invalid_ceiling")
    C.require(integer(cap["shots"], 1, 100) and integer(cap["render_frames"], 1, 10000)
              and integer(cap["pixels_per_frame"], 1, 4096 * 4096), "invalid_ceiling")

    source = C.pin(assets, {"path": request["args"]["recipe"], "sha256": request["args"]["sha256"]},
                   "recipe_digest_mismatch")
    recipe = C.load(source)
    keys(recipe, "schema episode_id fps pixels shots cues", "invalid_recipe")
    C.require(recipe["schema"] == "viewpoint.episode-recipe/v1"
              and isinstance(recipe["episode_id"], str) and C.IDENTIFIER.fullmatch(recipe["episode_id"])
              and integer(recipe["fps"], 1, 120) and 48000 % recipe["fps"] == 0
              and isinstance(recipe["pixels"], list) and len(recipe["pixels"]) == 2
              and all(integer(n, 16, 4096) and n % 2 == 0 for n in recipe["pixels"])
              and isinstance(recipe["shots"], list) and 1 <= len(recipe["shots"]) <= cap["shots"],
              "invalid_recipe")
    C.require(math.prod(recipe["pixels"]) <= cap["pixels_per_frame"], "pixel_ceiling_exceeded")

    seen, frames, cut_frames = set(), 0, 0
    for shot in recipe["shots"]:
        C.require(isinstance(shot, dict) and isinstance(shot.get("id"), str)
                  and C.IDENTIFIER.fullmatch(shot["id"]) and shot["id"] not in seen, "invalid_shot")
        seen.add(shot["id"])
        validate_shot(assets, shot, policy["bindings"].get(shot["id"]))
        frames += shot["source"][1] - shot["source"][0] + 1
        cut_frames += shot["cut"][1] - shot["cut"][0] + 1
    C.require(frames <= cap["render_frames"], "render_ceiling_exceeded")

    C.require(isinstance(recipe["cues"], list) and 0 < len(recipe["cues"]) <= cut_frames, "invalid_cues")
    past = 0
    for cue in recipe["cues"]:
        keys(cue, "frame duration_frames hz caption", "invalid_cue")
        C.require(integer(cue["frame"], past, cut_frames - 1)
                  and integer(cue["duration_frames"], 1, cut_frames - cue["frame"])
                  and integer(cue["hz"], 40, 4000) and isinstance(cue["caption"], str)
                  and 0 < len(cue["caption"]) <= 120 and cue["caption"].isprintable(), "invalid_cue")
        past = cue["frame"] + cue["duration_frames"]

    return {"schema": "viewpoint.episode-bound/v1", "identity": identity,
            "recipe": recipe, "rights": rights, "render_frames": frames, "edit_frames": cut_frames}


def attempt(job, label):
    directory = job / "attempts"
    directory.mkdir(exist_ok=True)
    number = len(list(directory.glob(label + "-*"))) + 1
    work = directory / f"{label}-{number:03d}"
    work.mkdir()
    return work


def event(job, stage, **fields):
    path = job / "events.json"
    rows = C.load(path) if path.exists() else []
    C.write(path, [*rows, {"stage": stage, **fields}])


def inventory(directory, names):
    return {str(name): C.sha_file(directory / name) for name in sorted(names)}


def check_files(directory, pins):
    for name, sha in pins.items():
        C.pin(directory, {"path": name, "sha256": sha}, "committed_artifact_changed")


def command(name, argv, timeout=180):
    return {"name": name, "argv": [str(part) for part in argv], "timeout": timeout}


def media(work, reads, commands):
    receipt = host.scoped(work, reads, commands)
    for entry in receipt["commands"]:
        if entry["rc"]:
            raise RuntimeError(f"{entry['name']} exited {entry['rc']}; see {work / (entry['name'] + '.log')}")
    return receipt


def shot_binding(shot, recipe):
    return C.digest(C.encoded({"shot": shot, "pixels": recipe["pixels"], "fps": recipe["fps"]}))


def settings_for(work, assets, shot, recipe, mode, interrupt=False):
    return {"work": str(work), "artifact_root": str(assets), "shot": shot, "source": shot["source"],
            "fps": recipe["fps"], "pixels": recipe["pixels"], "binding_sha256": shot_binding(shot, recipe),
            "mode": mode, "interrupt": interrupt}


def validate_shot_output(directory, shot, recipe):
    receipt = C.load(directory / "shot-receipt.json")
    expected = range(shot["source"][0], shot["source"][1] + 1)
    C.require(receipt["status"] == "PASS" and receipt["binding_sha256"] == shot_binding(shot, recipe)
              and receipt["shot"] == shot["id"] and [row["frame"] for row in receipt["frames"]] == list(expected),
              "shot_receipt_stale")
    C.pin(directory, {"path": "shot.blend", "sha256": receipt["blend_sha256"]}, "saved_scene_changed")
    for row in receipt["frames"]:
        C.require(row["file"] == f"{row['frame']:04d}.png", "shot_receipt_stale")
        C.pin(directory, {"path": "frames/" + row["file"], "sha256": row["sha256"]}, "shot_frame_changed")
    return receipt


def render_shot(job, assets, recipe, shot, inject):
    shot_id = shot["id"]
    shots = job / "shots"
    shots.mkdir(exist_ok=True)
    target = shots / shot_id
    if target.exists():
        return validate_shot_output(target, shot, recipe)
    work = attempt(job, "render-" + shot_id)
    retried = any(row["stage"] == "render_interrupted" and row["shot"] == shot_id
                  for row in C.load(job / "events.json"))
    settings = work / "render-settings.json"
    inject = inject and not retried
    C.write(settings, settings_for(work, assets, shot, recipe, "render", interrupt=inject))
    scope = host.scoped(work, [ROOT.parents[1], assets], [command("blender-render", [*BLENDER, settings], 1800)])
    rc = scope["commands"][-1]["rc"]
    if inject and rc in (-9, 137):
        witness = C.load(work / "interrupted.json")
        first = work / "frames" / witness["file"]
        C.require(C.sha_file(first) == witness["sha256"] and not (work / "shot-receipt.json").exists(),
                  "interrupt_control_invalid")
        event(job, "render_interrupted", shot=shot_id, scope=str(work.relative_to(job) / "scope.json"),
              rc=rc, witness=witness)
        raise C.Refusal("render_interrupted")
    if rc in (-15, -9, 124, 130, 137, 143):
        event(job, "render_interrupted", shot=shot_id, scope=str(work.relative_to(job) / "scope.json"), rc=rc)
        raise C.Refusal("render_interrupted")
    if rc:
        raise RuntimeError(f"Blender exited {rc}; see {work / 'blender-render.log'}")
    receipt = C.load(work / "shot-receipt.json")
    if receipt["status"] == "REFUSED":
        raise C.Refusal(receipt["reason"], receipt.get("detail", ""))
    receipt = validate_shot_output(work, shot, recipe)
    C.immutable_commit(work, target)
    event(job, "shot_committed", shot=shot_id, sha256=receipt["blend_sha256"],
          scope=str(target.relative_to(job) / "scope.json"))
    return receipt


def timestamp(frame, fps):
    millis = round(frame * 1000 / fps)
    return f"{millis // 3600000:02d}:{millis // 60000 % 60:02d}:{millis // 1000 % 60:02d},{millis % 1000:03d}"


def sound_and_captions(work, recipe, total):
    rate = 48000
    per_frame = rate // recipe["fps"]
    samples = array("h", [0]) * (total * per_frame)
    captions = []
    for idx, cue in enumerate(recipe["cues"], 1):
        start = cue["frame"] * per_frame
        count = cue["duration_frames"] * per_frame
        for i in range(count):
            decay = (1 - i / count) ** 2
            samples[start + i] = round(11000 * decay * math.cos(math.tau * cue["hz"] * i / rate))
        first = timestamp(cue["frame"], recipe["fps"])
        last = timestamp(cue["frame"] + cue["duration_frames"], recipe["fps"])
        captions.append(f"{idx}\n{first} --> {last}\n{cue['caption']}\n")
    if sys.byteorder != "little":
        samples.byteswap()
    with wave.open(str(work / "effects.wav"), "wb") as pcm:
        pcm.setparams((1, 2, rate, len(samples), "NONE", "not compressed"))
        pcm.writeframes(samples.tobytes())
    (work / "captions.en.srt").write_text("\n".join(captions), encoding="utf-8")


def conform(job, recipe, renders, bound):
    target = job / "conform"
    if target.exists():
        check_files(target, C.load(target / "outputs.json"))
        return target
    work = attempt(job, "conform")
    frames = work / "edit-frames"
    frames.mkdir()
    index, cuts = 0, []
    for shot, rendered in zip(recipe["shots"], renders, strict=True):
        by_frame = {row["frame"]: row for row in rendered["frames"]}
        cut = {"id": shot["id"], "camera": rendered["camera"]["name"], "clip": rendered["clip"]["name"],
               "source_range": shot["source"], "edit_start": index, "frames": []}
        for frame in range(shot["cut"][0], shot["cut"][1] + 1):
            row = by_frame[frame]
            shutil.copyfile(job / "shots" / shot["id"] / "frames" / row["file"], frames / f"{index:06d}.png")
            cut["frames"].append({"edit_frame": index, "source_frame": frame, "sha256": row["sha256"]})
            index += 1
        cut["edit_end"] = index
        cuts.append(cut)
    C.require(index == bound["edit_frames"], "edit_length_mismatch")
    sound_and_captions(work, recipe, index)
    fps = recipe["fps"]
    picture, master = work / "picture.mp4", work / "master.mp4"
    media(work, [], [
        command("conform", [*FFMPEG, "-framerate", fps, "-start_number", 0, "-i", frames / "%06d.png",
            "-frames:v", index, "-an", "-map_metadata", "-1", "-c:v", "libx264", "-preset", "medium",
            "-crf", 16, "-pix_fmt", "yuv420p", "-threads", 2, "-video_track_timescale", fps * 1000, picture]),
        command("mux", [*FFMPEG, "-i", picture, "-i", work / "effects.wav", "-i", work / "captions.en.srt",
            "-map", "0:v", "-map", "1:a", "-map", "2:s", "-map_metadata", "-1", "-c:v", "copy",
            "-c:a", "aac", "-b:a", "128k", "-c:s", "mov_text", "-metadata:s:s:0", "language=eng",
            "-movflags", "+faststart", master]),
    ])
    C.write(work / "timeline.json", {"schema": "viewpoint.episode-timeline/v1",
        "episode_id": recipe["episode_id"], "fps": fps, "pixels": recipe["pixels"],
        "total_edit_frames": index, "handles_not_cut": bound["render_frames"] - index,
        "picture_sha256": C.sha_file(picture), "movie_sha256": C.sha_file(master),
        "shots": cuts, "cues": recipe["cues"]})
    shutil.rmtree(frames)
    names = ("timeline.json", "picture.mp4", "master.mp4", "effects.wav", "captions.en.srt")
    C.write(work / "outputs.json", inventory(work, names))
    C.immutable_commit(work, target)
    event(job, "conformed_and_muxed", scope="conform/scope.json", sha256=C.sha_file(target / "master.mp4"))
    return target


def sha_set(root):
    return inventory(root, [path.relative_to(root) for path in root.rglob("*") if path.is_file()])


def archive(job, state, request, assets, policy, bound, conform_dir, inject):
    request_id = request["request_id"]
    objects, indices = state / "objects", state / "index"
    for directory in (objects, indices):
        directory.mkdir(exist_ok=True)
        C.sync_dir(directory.parent)
    bundle = job / "archive-plan.json"
    if not bundle.exists():
        work = attempt(job, "pack")
        stage = work / "package"
        stage.mkdir()
        for name in C.load(conform_dir / "outputs.json"):
            shutil.copyfile(conform_dir / name, stage / name)
        inputs = stage / "inputs"
        inputs.mkdir()
        for shot in bound["recipe"]["shots"]:
            for role in ROLES:
                path = C.pin(assets, shot[role], f"{role}_substitution")
                dest = inputs / shot[role]["path"]
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, dest)
                C.pin(inputs, shot[role], f"{role}_substitution")
            source = job / "shots" / shot["id"]
            dest = stage / "shots" / shot["id"]
            (dest / "frames").mkdir(parents=True)
            for name in ("shot.blend", "shot-receipt.json"):
                shutil.copyfile(source / name, dest / name)
            for frame in sorted((source / "frames").glob("*.png")):
                shutil.copyfile(frame, dest / "frames" / frame.name)
        for name, payload in (("bound.json", bound), ("recipe.json", bound["recipe"]), ("rights-policy.json", policy)):
            C.write(stage / name, payload)
        C.write(stage / "files.json", sha_set(stage))
        tar = work / "package.tar"
        with tarfile.open(tar, "w", format=tarfile.PAX_FORMAT) as out:
            for name in sha_set(stage):
                source = stage / name
                meta = tarfile.TarInfo(name)
                meta.size = source.stat().st_size
                meta.mtime, meta.mode = 0, 0o644
                with source.open("rb") as stream:
                    out.addfile(meta, stream)
        with tar.open("rb") as stream:
            os.fsync(stream.fileno())
        archive_sha = C.sha_file(tar)
        target = objects / (archive_sha + ".tar")
        if not target.exists():
            fd, name = tempfile.mkstemp(prefix=".pending-", dir=objects)
            try:
                with os.fdopen(fd, "wb") as out, tar.open("rb") as source:
                    shutil.copyfileobj(source, out)
                    out.flush()
                    os.fsync(out.fileno())
                try:
                    os.link(name, target)
                except FileExistsError:
                    pass
            finally:
                Path(name).unlink(missing_ok=True)
        C.require(C.sha_file(target) == archive_sha, "archive_object_changed")
        C.sync_dir(objects)
        C.write(bundle, {"archive_sha256": archive_sha, "archive_bytes": target.stat().st_size,
                         "files_sha256": C.sha_file(stage / "files.json")})
        event(job, "archive_object_durable", sha256=archive_sha)
    plan = C.load(bundle)
    tar = C.pin(objects, {"path": plan["archive_sha256"] + ".tar", "sha256": plan["archive_sha256"]},
                "archive_object_changed")
    index = indices / (request_id + ".json")
    index_doc = {"schema": "viewpoint.episode-archive/v1", "request_id": request_id, **plan}
    if not index.exists():
        events = C.load(job / "events.json")
        if inject and not any(row["stage"] == "archive_ack_lost" for row in events):
            event(job, "archive_ack_lost", sha256=plan["archive_sha256"])
            raise C.Refusal("archive_ack_lost")
        C.write(index, index_doc, immutable=True)
        event(job, "archive_index_durable", sha256=plan["archive_sha256"])
    C.require(C.load(index) == index_doc, "archive_index_changed")
    return tar, plan


def unpack(tar, work, plan):
    stage = work / "restored"
    stage.mkdir()
    seen = set()
    with tarfile.open(tar, "r:") as source:
        for entry in source:
            name = str(C.relative(entry.name))
            C.require(entry.isfile() and name not in seen and not entry.issparse(), "archive_member_rejected")
            seen.add(name)
            target = stage / name
            target.parent.mkdir(parents=True, exist_ok=True)
            with source.extractfile(entry) as inp, target.open("xb") as out:
                shutil.copyfileobj(inp, out)
    files = C.pin(stage, {"path": "files.json", "sha256": plan["files_sha256"]}, "archive_manifest_changed")
    pins = C.load(files)
    C.require(seen == {"files.json", *pins}, "archive_inventory_mismatch")
    check_files(stage, pins)
    return stage


def verify_master(stage, work, recipe, bound):
    picture, movie = stage / "picture.mp4", stage / "master.mp4"
    hash_video = ["-map", "0:v:0", "-c", "copy", "-f", "hash", "-hash", "sha256", "-"]
    media(work, [stage], [
        command("probe", [*FFPROBE, "-count_frames", "-show_streams", "-show_format", "-of", "json", movie]),
        command("decode", [*FFMPEG, "-i", movie, "-map", "0:v:0", "-map", "0:a:0", "-f", "null", "-"]),
        command("picture-hash", [*FFMPEG, "-i", picture, *hash_video]),
        command("master-picture-hash", [*FFMPEG, "-i", movie, *hash_video]),
        command("captions", [*FFMPEG, "-i", movie, "-map", "0:s:0", "-f", "srt", "-"]),
    ])
    streams = C.load(work / "probe.log")["streams"]
    C.require([stream["codec_type"] for stream in streams] == ["video", "audio", "subtitle"], "master_streams_changed")
    video, audio, subs = streams
    C.require(video["nb_read_frames"] == str(bound["edit_frames"])
              and [video["width"], video["height"]] == recipe["pixels"]
              and video["avg_frame_rate"] == f"{recipe['fps']}/1" and audio["sample_rate"] == "48000"
              and subs.get("tags", {}).get("language") == "eng", "master_readback_mismatch")
    C.require((work / "picture-hash.log").read_bytes() == (work / "master-picture-hash.log").read_bytes()
              and (work / "captions.log").read_text().strip() == (stage / "captions.en.srt").read_text().strip(),
              "mux_binding_mismatch")
    return {"video_frames": int(video["nb_read_frames"]), "streams": [s["codec_name"] for s in streams],
            "duration_s": float(video["duration"]), "pixels": recipe["pixels"], "fps": recipe["fps"]}


def restore(job, restore_root, request_id, tar, plan, bound):
    parent = restore_root / "restored"
    parent.mkdir(exist_ok=True)
    target = parent / request_id
    verify = job / "restore.json"
    if verify.exists():
        receipt = C.load(verify)
        check_files(target, C.load(C.pin(target, {"path": "files.json", "sha256": plan["files_sha256"]},
                                         "archive_manifest_changed")))
        C.require(receipt["archive_sha256"] == plan["archive_sha256"], "restore_receipt_stale")
        return receipt
    work = attempt(job, "restore")
    with tempfile.TemporaryDirectory(prefix=f".pending-{request_id}-", dir=parent) as pending:
        stage = unpack(tar, Path(pending), plan)
        recipe = bound["recipe"]
        master = verify_master(stage, work, recipe, bound)
        witnesses, scopes = [], [str(work.relative_to(job) / "scope.json")]
        for shot in recipe["shots"]:
            for role in ROLES:
                C.pin(stage / "inputs", shot[role], f"{role}_substitution")
            source = stage / "shots" / shot["id"]
            validate_shot_output(source, shot, recipe)
            witness = work / ("reopen-" + shot["id"])
            witness.mkdir()
            for name in ("shot.blend", "shot-receipt.json"):
                shutil.copyfile(source / name, witness / name)
            settings = witness / "settings.json"
            C.write(settings, settings_for(witness, stage / "inputs", shot, recipe, "reopen"))
            media(witness, [ROOT.parents[1], stage / "inputs"], [command("blender-reopen", [*BLENDER, settings])])
            proof = C.load(witness / "reopen-receipt.json")
            C.require(proof["status"] == "PASS", proof.get("reason", "native_reopen_failed"), proof.get("detail", ""))
            witnesses.append(proof)
            scopes.append(str(witness.relative_to(job) / "scope.json"))
        if target.exists():
            C.pin(target, {"path": "files.json", "sha256": plan["files_sha256"]}, "restore_target_changed")
            check_files(target, C.load(target / "files.json"))
        else:
            C.immutable_commit(stage, target)
    receipt = {"schema": "viewpoint.episode-restore/v1", **plan,
               "master_sha256": C.sha_file(target / "master.mp4"), "master": master, "native_reopen": witnesses,
               "files": len(C.load(target / "files.json")) + 1, "scopes": scopes}
    C.write(verify, receipt)
    event(job, "restored", sha256=receipt["master_sha256"])
    return receipt


def execute(state, archive_root, restore_root, assets, request, policy, policy_sha, inject):
    request_id = request["request_id"]
    jobs = state / "jobs"
    jobs.mkdir(exist_ok=True)
    C.sync_dir(state)
    job = jobs / request_id
    job.mkdir(exist_ok=True)
    C.sync_dir(jobs)
    identity_file = job / "identity.json"
    final = job / "receipt.json"
    is_status = request["op"] == "af.episode.status"
    identity = replay_identity(request | {"op": "af.episode"}, policy_sha, (assets, archive_root, restore_root))
    with (job / "request.lock").open("ab") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        if identity_file.exists():
            C.require(C.load(identity_file) == identity, "request_identity_conflict")
        elif is_status:
            raise C.Refusal("request_unknown")
        else:
            C.write(identity_file, identity)
        if is_status:
            if final.exists():
                return C.load(final)
            events = job / "events.json"
            return {"status": "READY_TO_RESUME", "request_id": request_id, "identity": identity,
                    "events": C.load(events) if events.exists() else []}

        bound = bind(assets, request, policy, identity)
        if final.exists():
            stored = C.load(final)
            plan = stored["restore"]
            C.require(C.load(archive_root / "index" / (request_id + ".json")) ==
                      {"schema": "viewpoint.episode-archive/v1", "request_id": request_id,
                       **{key: plan[key] for key in ("archive_sha256", "archive_bytes", "files_sha256")}},
                      "archive_index_changed")
            C.pin(archive_root / "objects", {"path": plan["archive_sha256"] + ".tar", "sha256": plan["archive_sha256"]},
                  "archive_object_changed")
            restored = restore_root / "restored" / request_id
            files = C.pin(restored, {"path": "files.json", "sha256": plan["files_sha256"]},
                          "archive_manifest_changed")
            check_files(restored, C.load(files))
            return stored
        bound_path = job / "bound.json"
        if bound_path.exists():
            C.require(C.load(bound_path) == bound, "request_identity_conflict")
        else:
            C.write(bound_path, bound)
        if not (job / "events.json").exists():
            event(job, "accepted", recipe_sha256=request["args"]["sha256"])
        recipe = bound["recipe"]
        renders = [render_shot(job, assets, recipe, shot, inject and index == 1)
                   for index, shot in enumerate(recipe["shots"])]
        edition = conform(job, recipe, renders, bound)
        tar, plan = archive(job, archive_root, request, assets, policy, bound, edition, inject)
        restored = restore(job, restore_root, request_id, tar, plan, bound)
        receipt = {"schema": "viewpoint.episode-receipt/v1", "status": "RESTORED", "request_id": request_id,
                   "identity": identity, "restore": restored, "events": C.load(job / "events.json"),
                   "interventions": 0, "scope": "local-private-archive"}
        C.write(final, receipt)
        return receipt
