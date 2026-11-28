# SPDX-License-Identifier: AGPL-3.0-only
"""Pinned review runner: bind a movie by SHA-256, sample it by fixed rule, verify, replay.

Nothing here selects a movie by location or modification time. A review starts
from an explicit digest, and every later step is checked against that digest.
"""
import contextlib
import json
import os
import re
import shutil
import subprocess
import sys
from fractions import Fraction
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import rpr_common as C  # noqa: E402

MOVIE_NAME = "movie.mp4"
PACK_FILES = (MOVIE_NAME, "catalog.json", "declared.json", "manifest.json", "frame_index.json")
SAMPLING_CURRENT = "rp4.sampling.v2-full-contact"
SAMPLING_LEGACY = "rp4.sampling.v1-landing"
CURRENT_RULE = SAMPLING_CURRENT
CAMERA_KEYS = ("name", "lens_mm", "sensor_width_mm", "location_m", "rotation_euler_rad", "target_m")


def sampling_rule(sampling):
    """Name of the rule a manifest was built under. A manifest from before rules were named is legacy."""
    return (sampling or {}).get("rule") or SAMPLING_LEGACY


def pack_id_for(movie_sha, rule=SAMPLING_CURRENT):
    """Opaque id of a movie under one sampling rule. Two rules never share a pack."""
    seed = f"{C.VERSION}|pack|{movie_sha}" + ("" if rule == SAMPLING_LEGACY else f"|{rule}")
    return C.sha_text(seed)[:16]


def sampling_block(rule, protocol=None):
    """The exact sampling block a pack built under `rule` must carry."""
    protocol = protocol or C.load_protocol()
    return protocol["sampling"] if rule == protocol["sampling"]["rule"] else protocol["sampling_history"][rule]


def next_command(verb, *args):
    return ["python3", "-I", str(C.ROOT / "rp_review.py"), verb, *map(str, args)]


# ------------------------------------------------------------- declared plan
def declared_from_sources(set_path, motion_path, movie_sha):
    """Derive the contact plan from two independent pinned records of the take."""
    set_doc = C.read_json(set_path)
    motion = C.read_json(motion_path)
    try:
        markers = set_doc["markers"]
        if (type(set_doc["fps"]) is not int or set_doc["fps"] <= 0 or set_doc["start_frame"] != 1
                or type(set_doc["end_frame"]) is not int or set_doc["end_frame"] < 1):
            raise ValueError("invalid one-based set timeline")
        one_based = {name: markers[name] for name in (
            "APPROACH", "PLANT_AND_JAB", "HAND_CONTACT", "BUTTON_BOTTOMED", "BUTTON_RELEASED", "RECOVERY_AND_SCREEN")}
    except (KeyError, TypeError, ValueError) as exc:
        raise C.Harness(f"set registration does not declare the expected timeline: {exc}") from exc
    zero = {name: frame - 1 for name, frame in one_based.items()}
    press = sorted(int(key) for key in motion.get("press_window_error_m_by_frame", {}))
    if not press:
        raise C.Harness("motion report declares no press window")
    if (press[0], press[-1]) != (zero["HAND_CONTACT"], zero["BUTTON_BOTTOMED"]):
        raise C.Refused("declared_sources_disagree",
                        "supply a motion report and set registration that declare the same press window",
                        next_command("declared", "--set", set_path, "--motion-report", motion_path))
    plants = []
    for side in ("left", "right"):
        for number, stance in enumerate(motion.get("stance", {}).get(side, [])):
            if len(stance["frames"]) != 2:
                raise C.Harness(f"motion_report.stance.{side}[{number}].frames is not a [first, end) pair")
            first, end = (int(value) for value in stance["frames"])
            # Stance ranges of the motion report are zero-based and half-open: frames a <= i < b
            # (W4-CONTACT/contact.py, plan.plant_ranges_end_exclusive). last_index is inclusive.
            if not 0 <= first < end:
                raise C.Harness(f"motion_report.stance.{side}[{number}].frames is empty or reversed")
            plants.append({"first_index": first, "last_index": end - 1, "source_frames": [first, end],
                           "source": f"motion_report.stance.{side}[{number}].frames"})
    plants.sort(key=lambda item: (item["first_index"], item["last_index"]))
    camera = set_doc.get("camera")
    declared = {
        "protocol": C.VERSION,
        "video_sha256": movie_sha,
        "index_origin": 0,
        "index_note": "Set markers are one-based scene frames; the runner uses zero-based decode index = marker - 1.",
        "foot_plant_note": "motion_report.stance frames are zero-based half-open [first, end); last_index is inclusive "
                           "[CITED: W4-CONTACT/contact.py, plan.plant_ranges_end_exclusive]",
        "sources": [
            {"name": "set_registration", "sha256": C.sha_file(set_path)},
            {"name": "motion_report", "sha256": C.sha_file(motion_path)},
        ],
        "phases": {
            "approach": {"first_index": zero["APPROACH"], "last_index": zero["PLANT_AND_JAB"] - 1,
                         "source": "SET.markers APPROACH..PLANT_AND_JAB"},
            "release": {"first_index": zero["BUTTON_BOTTOMED"], "last_index": zero["BUTTON_RELEASED"],
                        "source": "SET.markers BUTTON_BOTTOMED..BUTTON_RELEASED"},
            "recovery": {"first_index": zero["BUTTON_RELEASED"], "last_index": zero["RECOVERY_AND_SCREEN"],
                         "source": "SET.markers BUTTON_RELEASED..RECOVERY_AND_SCREEN"},
        },
        "foot_plant": plants,
        "hand_button": {"first_index": press[0], "last_index": press[-1],
                        "source": "motion_report.press_window_error_m_by_frame; equals SET HAND_CONTACT..BUTTON_BOTTOMED"},
    }
    if isinstance(camera, dict) and all(key in camera for key in CAMERA_KEYS):
        declared["camera"] = {key: camera[key] for key in CAMERA_KEYS}
        declared["camera"].update(source="SET.camera", source_sha256=C.sha_file(set_path))
    return declared


def declared_from_contacts(contacts_path, movie_sha):
    """Accept an rp4.e2e-vision.v1 contact plan bound to the same movie."""
    contacts = C.read_json(contacts_path)
    if contacts.get("video_sha256") != movie_sha:
        raise C.Refused("declared_movie_mismatch", "supply a contact plan bound to the pinned movie",
                        next_command("build"))
    interval = {}
    for name in ("foot_plant", "hand_button"):
        value = contacts.get(name)
        if isinstance(value, list):
            interval[name] = [{"first_index": int(item["first_index"]), "last_index": int(item["last_index"]),
                               "source": f"contacts.{name}"} for item in value]
        elif isinstance(value, dict):
            interval[name] = [{"first_index": int(value["first_index"]), "last_index": int(value["last_index"]),
                               "source": f"contacts.{name}"}]
    return {
        "protocol": C.VERSION, "video_sha256": movie_sha, "index_origin": 0,
        "sources": [{"name": "contacts", "sha256": C.sha_file(contacts_path)}],
        "phases": {}, "foot_plant": interval.get("foot_plant", []),
        "hand_button": interval.get("hand_button", [{}])[0],
    }


def validate_declared(declared, movie_sha):
    if not isinstance(declared, dict) or declared.get("protocol") != C.VERSION:
        raise C.Harness("declared plan is not an rp4.review-protocol.v1 document")
    if declared.get("video_sha256") != movie_sha:
        raise C.Refused("declared_movie_mismatch", "supply a declared plan bound to the pinned movie",
                        next_command("build"))
    hand = declared.get("hand_button")
    if not isinstance(hand, dict) or "first_index" not in hand or "last_index" not in hand:
        raise C.Harness("declared plan has no hand_button interval")
    for interval in [declared.get("hand_button"), *declared.get("foot_plant", []), *declared.get("phases", {}).values()]:
        if not (isinstance(interval, dict) and isinstance(interval.get("first_index"), int)
                and isinstance(interval.get("last_index"), int) and isinstance(interval["first_index"], bool) is False
                and isinstance(interval["last_index"], bool) is False
                and 0 <= interval["first_index"] <= interval["last_index"]):
            raise C.Refused("declared_range", "supply inclusive zero-based intervals inside the movie",
                            next_command("build"))
    camera = declared.get("camera")
    if camera is not None and (not isinstance(camera, dict) or not all(key in camera for key in CAMERA_KEYS)
                               or not camera.get("source_sha256")):
        raise C.Refused("declared_camera", "supply the camera as SET.json declares it, with the digest of its source",
                        next_command("build"))


# ------------------------------------------------------------------ sampling
def grid_indices(catalog, cadence):
    """First frame on or after each clock tick, plus the final frame (rp4.e2e-vision.v1)."""
    frames = catalog["frames"]
    clock = Fraction(catalog["time_base"])
    origin = frames[0]["pts"]
    scheduled = 0
    indices = {len(frames) - 1}
    for frame in frames:
        ticks = (frame["pts"] - origin) * clock * cadence
        if ticks >= scheduled:
            indices.add(frame["index"])
            scheduled = int(ticks) + 1
    return indices


def plan_windows(declared, catalog, sampling):
    last_frame = catalog["frame_count"] - 1
    pad = sampling["contact_pad_frames"]
    windows = []

    def add(name, first, last, source, padded=True):
        low = max(0, first - pad) if padded else first
        high = min(last_frame, last + pad) if padded else last
        if not 0 <= first <= last <= last_frame or low > high:
            raise C.Refused("declared_range", f"{name}: supply an interval inside the movie", next_command("build"))
        windows.append({"name": name, "declared": [first, last], "first_index": low, "last_index": high, "source": source})

    phases = declared.get("phases", {})
    if "approach" in phases:
        add("approach", phases["approach"]["first_index"], phases["approach"]["last_index"],
            phases["approach"].get("source", "approach"))
    if sampling_rule(sampling) == SAMPLING_LEGACY:
        # Superseded rule, replayed only for packs built under it: a plant was cut to its landing,
        # the first seven frames of a stance as the pipeline's judge pack takes them.
        landing = sampling["plant_landing_frames"]
        for number, interval in enumerate(declared.get("foot_plant", [])):
            first = interval["first_index"]
            last = min(first + landing, interval["last_index"])
            add(f"foot_plant_{number}", first, last, interval.get("source", "foot_plant"))
    else:
        # Every native frame of each declared interval stays inside the consecutive window, so
        # a foot that slips late in a held stance is reviewed frame by frame.
        for number, interval in enumerate(declared.get("foot_plant", [])):
            add(f"foot_plant_{number}", interval["first_index"], interval["last_index"],
                interval.get("source", "foot_plant"))
    hand = declared["hand_button"]
    add("hand_button", hand["first_index"], hand["last_index"], hand.get("source", "hand_button"))
    first = max(0, hand["last_index"] + 1 - pad)
    last = hand["last_index"] + sampling["release_recovery_frames"]
    if "recovery" in phases:
        last = max(last, phases["recovery"]["last_index"] + pad)
    for name in ("release", "recovery"):
        if name in phases:
            phase = phases[name]
            if not (first <= phase["first_index"] and phase["last_index"] <= last):
                raise C.Refused("declared_release_outside_window",
                                f"{name} is declared beyond the release and recovery window", next_command("build"))
    add("release_and_recovery", first, min(last, last_frame),
        f"fixed {sampling['release_recovery_frames']} frames after the hand-button interval"
        + (", extended to the declared recovery" if "recovery" in phases else ""), padded=False)
    return windows


def merge_ranges(ranges):
    merged = []
    for low, high in sorted(ranges):
        if merged and low <= merged[-1][1] + 1:
            merged[-1][1] = max(merged[-1][1], high)
        else:
            merged.append([low, high])
    return merged


def plan_samples(catalog, declared, sampling):
    """Pure function of the catalog, the declared plan and the protocol constants."""
    windows = plan_windows(declared, catalog, sampling)
    grid = grid_indices(catalog, sampling["grid_cadence_fps"])
    covered = set()
    for window in windows:
        covered.update(range(window["first_index"], window["last_index"] + 1))
    indices = sorted(grid | covered)
    samples = []
    previous = None
    for index in indices:
        record = catalog["frames"][index]
        groups = [window["name"] for window in windows
                  if window["first_index"] <= index <= window["last_index"]] or ["grid"]
        samples.append({
            "index": index, "pts": record["pts"], "duration_ticks": record["duration_ticks"],
            "time_s": C.time_seconds(record["pts"], catalog["time_base"]),
            "step": 1 if previous is None else index - previous,
            "groups": groups, "rgb24_sha256": record["rgb24_sha256"],
        })
        previous = index
    return windows, merge_ranges([(w["first_index"], w["last_index"]) for w in windows]), samples


# ------------------------------------------------------------------- build
def extract_samples(movie, pack, catalog, indices, raw_options, movie_sha):
    import numpy as np

    width, height = catalog["width"], catalog["height"]
    frame_dir = pack / "frames"
    frame_dir.mkdir()
    command = C.ffmpeg_base(movie) + raw_options + [
        "-vf", f"select='{C.select_expression(indices)}'", "-frames:v", str(len(indices)),
        "-f", "rawvideo", "pipe:1"]
    size = width * height * 3
    samples = []
    child = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=C.ENV)
    try:
        for index in indices:
            raw = child.stdout.read(size)
            record = catalog["frames"][index]
            if len(raw) != size or C.sha_bytes(raw) != record["rgb24_sha256"]:
                raise C.Refused("sample_pixels_mismatch",
                                f"decoded sample {index} is not the catalogued frame; supply the pinned movie",
                                next_command("build"))
            target = pack / f"frames/frame-{index:06d}.png"
            target.write_bytes(C.png_encode(np.frombuffer(raw, dtype=np.uint8).reshape(height, width, 3)))
            samples.append({"file": f"frames/frame-{index:06d}.png", "png_sha256": C.sha_file(target)})
        rest = child.stdout.read()
        child.stderr.read()
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()
    if rest or child.returncode != 0:
        raise C.Refused("sample_extraction_incomplete", "supply a movie that decodes every sampled frame",
                        next_command("build"))
    return samples


def encoder_identity():
    import numpy as np

    return {"png": "rpr-png-v1", "filter": "per-row minimum sum of absolute values among none, sub, up, average",
            "zlib_level": 6, "zlib": C.zlib.ZLIB_RUNTIME_VERSION, "numpy": np.__version__}


def build(movie, expected_sha, declared, pack, sampling=None):
    """Stage a movie by digest and write its sample pack. Deterministic for the same bytes.

    A new build always uses the rule in force. Only a replay or a restore names the rule of the
    pack it rebuilds.
    """
    movie, pack = Path(movie), Path(pack)
    if not C.SHA_RE.match(expected_sha or ""):
        raise C.Refused("movie_hash_missing", "supply the explicit movie SHA-256; no location or time is accepted",
                        next_command("build"))
    if not movie.is_file():
        raise C.Refused("movie_missing", "supply the pinned movie file", next_command("build"))
    if sha_or_none(movie) != expected_sha:
        raise C.Refused("movie_hash_mismatch", "supply the movie whose SHA-256 is the pinned digest",
                        next_command("build"))
    validate_declared(declared, expected_sha)
    protocol = C.load_protocol()
    sampling = sampling or protocol["sampling"]
    if sampling != sampling_block(sampling_rule(sampling), protocol):
        raise C.Harness("the sampling block is not one this protocol defines")
    if pack.exists():
        raise C.Harness(f"pack destination already exists: {pack}")
    pack.mkdir(parents=True)
    try:
        staged = pack / MOVIE_NAME
        shutil.copyfile(movie, staged)
        if sha_or_none(staged) != expected_sha or sha_or_none(movie) != expected_sha:
            raise C.Refused("movie_changed_while_staging", "supply a movie that is not being rewritten",
                            next_command("build"))
        catalog, raw_options = C.decoded_catalog(staged)
        windows, ranges, samples = plan_samples(catalog, declared, sampling)
        extracted = extract_samples(staged, pack, catalog, [s["index"] for s in samples], raw_options, expected_sha)
        for sample, record in zip(samples, extracted):
            sample.update(record)
            sample["frame_token"] = C.frame_token(expected_sha, sample["index"], sample["pts"], sample["png_sha256"])
        catalog_sha = C.sha_bytes(C.json_bytes(catalog))
        manifest = {
            "protocol": C.VERSION,
            "movie": {"file": MOVIE_NAME, "sha256": expected_sha, "bytes": staged.stat().st_size},
            "catalog_sha256": catalog_sha,
            "time_base": catalog["time_base"], "avg_frame_rate": catalog["avg_frame_rate"],
            "frame_count": catalog["frame_count"], "width": catalog["width"], "height": catalog["height"],
            "declared_sha256": C.sha_bytes(C.json_bytes(declared)),
            "declared": declared,
            "sampling": sampling,
            "windows": windows,
            "consecutive_ranges": ranges,
            "sample_count": len(samples),
            "samples": samples,
            "encoder": encoder_identity(),
        }
        C.write_json(pack / "catalog.json", catalog)
        C.write_json(pack / "declared.json", declared)
        C.write_json(pack / "manifest.json", manifest)
        manifest_sha = C.sha_file(pack / "manifest.json")
        C.write_json(pack / "frame_index.json", {
            "protocol": C.VERSION, "movie_sha256": expected_sha, "manifest_sha256": manifest_sha,
            "catalog_sha256": catalog_sha, "samples": samples})
        # A pack is a sealed record: an accidental append to any of its files must fail.
        for path in (staged, *(pack / name for name in ("catalog.json", "declared.json", "manifest.json", "frame_index.json")),
                     *(pack / sample["file"] for sample in samples)):
            os.chmod(path, 0o444)
        return {"status": "BUILT", "pack": str(pack), "movie_sha256": expected_sha,
                "catalog_sha256": catalog_sha, "manifest_sha256": manifest_sha,
                "sample_count": len(samples), "consecutive_ranges": ranges}
    except BaseException:
        shutil.rmtree(pack, ignore_errors=True)
        raise


def sha_or_none(path):
    try:
        return C.sha_file(path)
    except OSError:
        return None


def replace_entry(target, data):
    """Put `data` at `target` by swapping the directory entry.

    The file that is there now is never opened, written or re-permissioned: its inode is only
    unlinked from this directory. A hard link to it from outside keeps the bytes it had, so a
    repair inside the pack cannot change a file that is linked from somewhere else.
    """
    target = Path(target)
    pending = target.with_name(f".{target.name}.restore-{os.getpid()}")
    try:
        with open(pending, "xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(pending, 0o444)
        os.replace(pending, target)
    except BaseException:
        with contextlib.suppress(OSError):
            pending.unlink()
        raise
    directory = os.open(target.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def pack_names(manifest):
    return [*PACK_FILES, *(sample["file"] for sample in manifest["samples"])]


def pack_boundary(pack, manifest):
    """Refuse a pack whose entries are not regular files inside its own directory."""
    pack = Path(pack)
    if pack.is_symlink() or not pack.is_dir() or (pack / "frames").is_symlink() or not (pack / "frames").is_dir():
        raise C.Refused("pack_boundary", "supply a pack that is a real directory holding its own frames",
                        next_command("build"))
    for name in pack_names(manifest):
        path = pack / name
        if os.path.lexists(path) and (path.is_symlink() or not path.is_file()):
            raise C.Refused("pack_boundary", f"{name} is not a regular file inside the pack", next_command("build"))


def restore(pack):
    """Rebuild from the pack's own pinned movie and put back any file that drifted from its manifest.

    The rebuilt manifest must equal the sealed one byte for byte; otherwise nothing is touched.
    A drifted file is replaced by swapping its directory entry, never by writing into its inode.
    """
    pack = Path(pack)
    manifest, manifest_sha = load_manifest(pack)
    pack_boundary(pack, manifest)
    if sha_or_none(pack / MOVIE_NAME) != manifest["movie"]["sha256"]:
        raise C.Refused("movie_hash_mismatch", "the staged movie is not the pinned bytes; build a new pack",
                        next_command("build"))
    names = pack_names(manifest)
    before = {name: (os.stat(pack / name).st_nlink if os.path.lexists(pack / name) else 0) for name in names}
    scratch = pack.parent / f".restore-{pack.name}-{os.getpid()}"
    try:
        build(pack / MOVIE_NAME, manifest["movie"]["sha256"], manifest["declared"], scratch, manifest["sampling"])
        if C.sha_file(scratch / "manifest.json") != manifest_sha:
            raise C.Refused("replay_diverged", "the pinned movie no longer rebuilds the sealed manifest; build a new pack",
                            next_command("build"))
        drifted = []
        for name in names:
            if sha_or_none(pack / name) != C.sha_file(scratch / name) or before[name] != 1:
                replace_entry(pack / name, (scratch / name).read_bytes())
                drifted.append({"file": name, "had_links": before[name]})
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    verified = verify(pack)
    return {**verified, "status": "RESTORED", "drifted": [item["file"] for item in drifted],
            "drifted_detail": drifted, "drifted_count": len(drifted),
            "links_after": sorted({os.stat(pack / name).st_nlink for name in names})}


# ------------------------------------------------------------------ verify
def load_manifest(pack):
    manifest_path = Path(pack) / "manifest.json"
    if not manifest_path.is_file():
        raise C.Refused("pack_missing", "supply a pack built by the pinned runner", next_command("build"))
    manifest = C.read_json(manifest_path)
    if manifest.get("protocol") != C.VERSION or not isinstance(manifest.get("samples"), list):
        raise C.Harness("pack manifest is not an rp4.review-protocol.v1 document")
    return manifest, C.sha_file(manifest_path)


def verify(pack, movie=None, expected_sha=None, pixels=True):
    """Refuse any pack whose movie, timing catalog, rule or pixels disagree with its manifest."""
    pack = Path(pack)
    manifest, manifest_sha = load_manifest(pack)
    staged = pack / MOVIE_NAME
    if not staged.is_file():
        raise C.Refused("pack_missing", "supply a pack that contains its movie", next_command("verify"))
    staged_sha = sha_or_none(staged)
    if staged_sha != manifest["movie"]["sha256"]:
        raise C.Refused("movie_hash_mismatch", "the staged movie is not the pinned bytes; rebuild the pack",
                        next_command("build"))
    if expected_sha is not None:
        external = Path(movie) if movie else staged
        if sha_or_none(external) != expected_sha or expected_sha != manifest["movie"]["sha256"]:
            raise C.Refused("movie_hash_mismatch",
                            "supply the movie whose SHA-256 is the pinned digest; a newer file is never substituted",
                            next_command("verify", "--pack", pack, "--sha256", expected_sha))
    pack_boundary(pack, manifest)
    stored_catalog = pack / "catalog.json"
    if not stored_catalog.is_file() or C.sha_file(stored_catalog) != manifest["catalog_sha256"]:
        raise C.Refused("stale_pts", "rebuild the pack from the pinned movie so the timing catalog matches",
                        next_command("build"))
    catalog = C.parse_json(stored_catalog.read_bytes())
    protocol = C.load_protocol()
    rule = sampling_rule(manifest["sampling"])
    # The rule in force for every new review, or one the protocol retired and still defines.
    if rule != protocol["sampling"]["rule"] and rule not in protocol["sampling_history"]:
        raise C.Refused("manifest_rule_diverged", "the pack was built under a sampling rule this protocol does not define",
                        next_command("build"))
    if manifest["sampling"] != sampling_block(rule, protocol):
        raise C.Refused("manifest_rule_diverged", "the pack's sampling block is not the rule it names",
                        next_command("build"))
    windows, ranges, expected_samples = plan_samples(catalog, manifest["declared"], manifest["sampling"])
    listed = [(s["index"], s["pts"], s["rgb24_sha256"]) for s in manifest["samples"]]
    planned = [(s["index"], s["pts"], s["rgb24_sha256"]) for s in expected_samples]
    if (listed != planned or manifest["sample_count"] != len(expected_samples) or manifest["windows"] != windows
            or manifest["consecutive_ranges"] != ranges):
        raise C.Refused("manifest_rule_diverged", "rebuild the pack with the declared sampling rule",
                        next_command("build"))
    # The declared plan travels inside the manifest under its own digest.
    if (C.sha_bytes(C.json_bytes(manifest["declared"])) != manifest["declared_sha256"]
            or not (pack / "declared.json").is_file()
            or (pack / "declared.json").read_bytes() != C.json_bytes(manifest["declared"])):
        raise C.Refused("manifest_rule_diverged", "the declared plan inside the pack was edited after the build",
                        next_command("build"))
    linked = [name for name in pack_names(manifest)
              if os.path.lexists(pack / name) and os.stat(pack / name).st_nlink != 1]
    if linked:
        raise C.Refused("pack_file_linked", f"{linked[0]} has more than one link; a pack file must not be reachable "
                        "from outside the pack", next_command("restore", "--pack", pack))
    if pixels:
        fresh, _ = C.decoded_catalog(staged)
        if C.sha_bytes(C.json_bytes(fresh)) != manifest["catalog_sha256"]:
            raise C.Refused("stale_pts", "the movie's presentation timestamps differ from the sealed catalog",
                            next_command("build"))
        for sample in manifest["samples"]:
            frame = fresh["frames"][sample["index"]]
            if (frame["pts"], frame["rgb24_sha256"]) != (sample["pts"], sample["rgb24_sha256"]):
                raise C.Refused("stale_pts", f"sample {sample['index']} is bound to another presentation time",
                                next_command("build"))
    # The printed seconds are parsed back and compared with ticks times clock, so a wrong
    # time column cannot pass on the strength of the tick comparison alone.
    clock = Fraction(manifest["time_base"])
    if any(abs(Fraction(sample["time_s"]) - sample["pts"] * clock) > Fraction(1, 10 ** 6)
           for sample in manifest["samples"]):
        raise C.Refused("stale_pts", "a sample's printed time disagrees with its presentation timestamp",
                        next_command("build"))
    tokens_ok = all(C.frame_token(manifest["movie"]["sha256"], s["index"], s["pts"], s["png_sha256"]) == s["frame_token"]
                    for s in manifest["samples"])
    if not tokens_ok or len({s["frame_token"] for s in manifest["samples"]}) != len(manifest["samples"]):
        raise C.Refused("manifest_rule_diverged", "frame tokens do not follow from the manifest", next_command("build"))
    for sample in manifest["samples"]:
        target = pack / sample["file"]
        if target.is_symlink() or not target.is_file() or target.resolve().parent != (pack / "frames").resolve():
            raise C.Refused("sample_missing", f"sample {sample['index']} is not a pack frame", next_command("build"))
        if C.sha_file(target) != sample["png_sha256"]:
            raise C.Refused("sample_digest_mismatch", f"sample {sample['index']} differs from its sealed PNG",
                            next_command("build"))
        if pixels:
            width, height, rgb_sha = C.png_rgb_sha(target)
            if (width, height) != (manifest["width"], manifest["height"]) or rgb_sha != sample["rgb24_sha256"]:
                raise C.Refused("sample_pixels_mismatch", f"sample {sample['index']} does not decode to the catalogued pixels",
                                next_command("build"))
    return {"status": "VERIFIED", "pack": str(pack), "movie_sha256": manifest["movie"]["sha256"],
            "catalog_sha256": manifest["catalog_sha256"], "manifest_sha256": manifest_sha,
            "sample_count": manifest["sample_count"], "pixels_checked": pixels, "sampling_rule": rule,
            "contact_window": "full_declared_intervals" if rule == CURRENT_RULE else "landing_only",
            "consecutive_ranges": manifest["consecutive_ranges"]}


def replay(pack, record=None):
    """Rebuild the manifest from the pack's own movie and require byte-identical output."""
    pack = Path(pack)
    manifest, manifest_sha = load_manifest(pack)
    scratch = pack.parent / f".replay-{pack.name}-{os.getpid()}"
    try:
        build(pack / MOVIE_NAME, manifest["movie"]["sha256"], manifest["declared"], scratch, manifest["sampling"])
        replayed = C.sha_file(scratch / "manifest.json")
        same = (C.sha_file(scratch / "catalog.json") == C.sha_file(pack / "catalog.json")
                and C.sha_file(scratch / "frame_index.json") == C.sha_file(pack / "frame_index.json"))
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    if replayed != manifest_sha or not same:
        raise C.Refused("replay_diverged", "the same movie must generate the same sample manifest; rebuild and re-review",
                        next_command("replay", "--pack", pack))
    result = {"status": "REPLAY_IDENTICAL", "pack": str(pack), "movie_sha256": manifest["movie"]["sha256"],
              "manifest_sha256": manifest_sha, "replayed_manifest_sha256": replayed,
              "catalog_sha256": manifest["catalog_sha256"], "sample_count": manifest["sample_count"]}
    if record:
        C.write_json(record, result)
    return result
