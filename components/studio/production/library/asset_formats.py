# SPDX-License-Identifier: AGPL-3.0-only
"""Passive format readers and CPU-only previews for the asset catalogue."""

import io
import json
import re
import struct
import subprocess
from pathlib import Path
from urllib.parse import unquote, urlsplit

import numpy as np
from PIL import Image, ImageOps

ABSENT = "AUSENTE"
IMAGES = set(".png .jpg .jpeg .webp .gif .exr .hdr .pfm .tif .tiff .tga .bmp .avif .svg".split())
MOVIES = set(".mp4 .mkv .webm .mov .avi .m4v .mpg .mpeg .flv .ogv".split())
AUDIO = set(".wav .flac .mp3 .ogg .m4a .aac .opus .aif .aiff".split())
GEOMETRY = set(".glb .gltf .blend .fbx .obj .ply .stl .usd .usda .usdc .usdz .dae .abc .3mf .off .pcd .splat .ksplat .vrm".split())
SETTING_JSON = {"set.json", "camera.json", "cameras.json", "shot-camera.json"}
MOTION_JSON = {"motion.json", "animation.json", "keyframes.json"}
JSON_LIMIT = 8 * 1024 * 1024

KIND_LABEL = {
    "trellis": "TRELLIS generation output",
    "studio_mesh": "studio mesh for character production",
    "mesh": "3D geometry or scene file",
    "rig": "skeletal rig",
    "motion": "animation or motion capture clip",
    "set": "scene and set layout",
    "camera": "camera definition",
    "render": "still image or rendered pass",
    "contact_sheet": "contact sheet or preview",
    "take": "video take or edited clip",
    "audio": "audio asset",
    "material": "material library",
}

def classify(relative):
    stem = relative.stem.lower()
    name = relative.name.lower()
    path = relative.as_posix().lower()
    suffix = relative.suffix.lower()
    if suffix in IMAGES:
        return "contact_sheet" if re.search(r"contact[-_ ]?sheet|(?:^|[-_])(?:grid|sheet)(?:[-_.]|$)", name) else "render"
    if suffix in MOVIES:
        return "take"
    if suffix in AUDIO:
        return "audio"
    if suffix in {".bvh", ".vmd", ".amc", ".asf"} or name in MOTION_JSON or name.endswith(("-motion.json", "_motion.json")):
        return "motion"
    if suffix == ".mtl":
        return "material"
    if name in SETTING_JSON or name in {"scene.xml", "world.xml"}:
        return "camera" if "camera" in name else "set"
    if suffix in {".npz", ".pt", ".bin", ".npy"}:
        if re.search(r"model[-_]native|^trellis", stem):
            return "trellis"
        if suffix in {".npy", ".npz"} and re.match(r"(?:motion|keyframe|mocap|retargeted)(?:[-_]|$)", stem):
            return "motion"
        return None
    if suffix not in GEOMETRY:
        return None
    if re.search(r"(?:template[-_]rig|^rig(?:[-_]|$)|rigged|skeleton|^skin$|unirig[-_]transfer|articulated|rigid[-_]skin)", stem):
        return "rig"
    if re.search(r"retarget|keyed|(?:^|[-_/])(?:motion|mocap)(?:[-_/]|$)", path):
        if suffix != ".blend" or "take" not in stem:
            return "motion"
    if re.search(r"^set(?:[-_]|$)|^take(?:[-_]|$)|^scene(?:[-_]|$)|^stage(?:[-_]|$)", stem):
        return "set"
    if re.search(r"^studio(?:[-_]|$)|remesh|retopo", stem) or re.search(r"(?:look[_-]lod|remesh-bake)/.*(?:selected|final|baked)", path):
        return "studio_mesh"
    if re.search(r"trellis|intake_(?:pbr|geo)/|a3h\.(?:pbr|geo)\.", path):
        return "trellis"
    return "mesh"

def small_json(path):
    if path.stat().st_size > JSON_LIMIT:
        raise ValueError("JSON metadata exceeds passive reader limit")
    with path.open(encoding="utf-8") as stream:
        return json.load(stream)

def gltf_document(path):
    if path.suffix.lower() == ".gltf":
        return small_json(path)
    with path.open("rb") as stream:
        header = stream.read(20)
        magic, version, length, chunk_bytes, chunk_type = struct.unpack("<4sIIII", header)
        if magic != b"glTF" or version != 2 or length != path.stat().st_size:
            raise ValueError("invalid GLB header")
        if chunk_type != 0x4E4F534A or not 0 < chunk_bytes <= min(JSON_LIMIT, length - 20):
            raise ValueError("invalid GLB JSON chunk")
        return json.loads(stream.read(chunk_bytes))

def gltf_metadata(document):
    nodes = document.get("nodes", [])
    meshes = document.get("meshes", [])
    accessors = document.get("accessors", [])
    triangles = 0
    count_known = True
    for mesh in meshes:
        for primitive in mesh.get("primitives", []):
            mode = primitive.get("mode", 4)
            if mode not in (4, 5, 6):
                continue
            index = primitive.get("indices", primitive.get("attributes", {}).get("POSITION"))
            if index is None:
                count_known = False
                continue
            count = accessors[index]["count"]
            if type(count) is not int or count < 0:
                raise ValueError("invalid GLTF accessor count")
            triangles += count // 3 if mode == 4 else max(0, count - 2)
    skins = [{
        "name": skin.get("name", f"skin_{idx}"),
        "joints": [nodes[node].get("name", f"node_{node}") for node in skin["joints"]],
    } for idx, skin in enumerate(document.get("skins", []))]
    materials = [{
        "name": material.get("name", f"material_{idx}"),
        "alpha_mode": material.get("alphaMode", "OPAQUE"),
        "pbr_channels": sorted(material.get("pbrMetallicRoughness", {})),
        "textures": sorted(key for key in material if key.endswith("Texture")),
    } for idx, material in enumerate(document.get("materials", []))]
    clips = [clip.get("name", f"animation_{idx}") for idx, clip in enumerate(document.get("animations", []))]
    cameras = [
        (f"/cameras/{idx}", camera.get("name", f"camera_{idx}"), camera)
        for idx, camera in enumerate(document.get("cameras", []))
    ]
    uris = sorted({item["uri"] for key in ("buffers", "images") for item in document.get(key, [])
                   if "uri" in item and not item["uri"].startswith("data:")})
    return {
        "triangles": triangles if count_known else ABSENT,
        "rig": {"skins": skins, "joint_roster": list(dict.fromkeys(joint for skin in skins for joint in skin["joints"]))},
        "materials": materials,
        "clips": clips,
        "format_note": "Triangle count across declared mesh primitives, counted once per mesh.",
    }, cameras, uris

def obj_metadata(path):
    materials, uris = set(), set()
    triangles = 0
    with path.open(encoding="utf-8", errors="replace") as stream:
        for line in stream:
            pieces = line.split("#", 1)[0].strip().split(None, 1)
            if len(pieces) != 2:
                continue
            key, value = pieces
            if key == "f":
                triangles += max(0, len(value.split()) - 2)
            elif key == "usemtl":
                materials.add(value)
            elif key == "mtllib":
                uris.update(value.split())
    return {"triangles": triangles, "materials": sorted(materials), "format_note": "OBJ face fan triangle count."}, [], sorted(uris)

def inspect(path):
    ext = path.suffix.lower()
    if ext in {".glb", ".gltf", ".vrm"}:
        return gltf_metadata(gltf_document(path))
    if ext == ".obj":
        return obj_metadata(path)
    if ext == ".mtl":
        uris = []
        materials = []
        with path.open(encoding="utf-8", errors="replace") as stream:
            for line in stream:
                fields = line.split("#", 1)[0].strip().split(None, 1)
                if len(fields) != 2:
                    continue
                key, value = fields
                if key == "newmtl":
                    materials.append(value)
                elif key.lower().startswith("map_") or key.lower() in {"bump", "disp", "decal", "refl", "norm"}:
                    uris.append(value.split()[-1] if value.startswith("-") else value)
        return {"materials": materials}, [], sorted(set(uris))
    if ext == ".stl":
        with path.open("rb") as stream:
            header = stream.read(84)
            if len(header) == 84:
                count = struct.unpack_from("<I", header, 80)[0]
                if path.stat().st_size == 84 + 50 * count:
                    return {"triangles": count}, [], []
            stream.seek(0)
            count = sum(line.lstrip().lower().startswith(b"facet normal ") for line in stream)
        return {"triangles": count if count or header.lstrip().startswith(b"solid") else ABSENT}, [], []
    if ext == ".bvh":
        joints = []
        frames = ABSENT
        frame_time = ABSENT
        with path.open(encoding="utf-8", errors="replace") as stream:
            for line in stream:
                tokens = line.strip().split()
                if len(tokens) >= 2 and tokens[0] in {"ROOT", "JOINT"}:
                    joints.append(tokens[1])
                elif tokens and tokens[0] == "Frames:":
                    frames = int(tokens[1])
                elif tokens[:2] == ["Frame", "Time:"]:
                    frame_time = float(tokens[2])
                    break
        if not joints:
            raise ValueError("BVH joint roster absent")
        return {"rig": {"joint_roster": joints}, "frames": frames, "frame_time": frame_time}, [], []
    if ext == ".json":
        data = small_json(path)
        if not isinstance(data, dict):
            raise ValueError("asset JSON must be an object")
        cameras = []
        if isinstance(data.get("camera"), dict):
            cameras.append(("/camera", data["camera"].get("name", "Camera"), data["camera"]))
        if isinstance(data.get("cameras"), list):
            cameras.extend((f"/cameras/{idx}", camera.get("name", f"Camera {idx}"), camera)
                           for idx, camera in enumerate(data["cameras"]) if isinstance(camera, dict))
        keys = ("fps", "start_frame", "end_frame", "duration_seconds", "markers", "units", "lights", "button", "screen")
        data_fields = {key: data[key] for key in keys if key in data}
        if path.name.lower() in MOTION_JSON or path.name.lower().endswith(("-motion.json", "_motion.json")):
            data_fields["motion_fields"] = sorted(data)
        if path.name.lower() in {"camera.json", "shot-camera.json"} and not cameras:
            data_fields["camera"] = data
        return data_fields, cameras, []
    return {}, [], []

def relative_uri(uri):
    if "\\" in uri:
        raise ValueError("unsupported path separator")
    parsed = urlsplit(uri)
    if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment:
        raise ValueError("external or ambiguous URI")
    path = Path(unquote(parsed.path))
    if path.is_absolute() or not path.parts or any(ord(char) < 32 for char in str(path)):
        raise ValueError("unsafe relative URI")
    return path

def scaled_image(path):
    with Image.open(path) as opened:
        if opened.format == "JPEG":
            opened.draft("RGB", (384, 384))
        image = ImageOps.exif_transpose(opened)
        image.thumbnail((256, 160), Image.Resampling.LANCZOS)
        return image.convert("RGB")

def video_preview(path):
    formats = "mov,mp4,m4a,3gp,3g2,mj2,matroska,webm,avi,mpeg,flv,ogg,mpegvideo"
    restrictions = ["-protocol_whitelist", "file,pipe", "-format_whitelist", formats]
    base = ["ffprobe", "-v", "error", *restrictions, "-threads", "1"]
    result = subprocess.run([*base, "-select_streams", "v:0", "-show_entries",
                             "format=duration:stream=width,height,avg_frame_rate,duration",
                             "-of", "json", str(path)], capture_output=True, timeout=12, check=True)
    report = json.loads(result.stdout)
    stream = report.get("streams", [{}])[0]
    try:
        duration = float(stream.get("duration") or report.get("format", {}).get("duration"))
        if not 0 < duration < float("inf"):
            duration = None
    except (ValueError, TypeError):
        duration = None
    timestamps = [duration * part for part in (0.10, 0.50, 0.90)] if duration else [0.0]
    frames = []
    sampled = []
    for timestamp in timestamps:
        command = ["ffmpeg", "-v", "error", "-nostdin", *restrictions,
                   "-hwaccel", "none", "-threads", "1", "-filter_threads", "1",
                   "-ss", f"{timestamp:.6f}", "-i", str(path), "-map", "0:v:0",
                   "-vf", "scale=256:160:force_original_aspect_ratio=decrease",
                   "-frames:v", "1", "-threads", "1", "-f", "image2pipe", "-vcodec", "mjpeg", "pipe:1"]
        result = subprocess.run(command, capture_output=True, timeout=18)
        if result.returncode == 0 and result.stdout:
            with Image.open(io.BytesIO(result.stdout)) as opened:
                frames.append(opened.convert("RGB"))
                sampled.append(round(timestamp, 6))
    return frames, {"duration_seconds": duration if duration is not None else ABSENT,
                    "width": stream.get("width", ABSENT), "height": stream.get("height", ABSENT),
                    "average_frame_rate": stream.get("avg_frame_rate", ABSENT),
                    "thumbnail_times_seconds": sampled}

def glb_preview(path):
    """Project a bounded sample of embedded positions at three neutral angles."""
    document = gltf_document(path)
    vertices = []
    with path.open("rb") as stream:
        stream.seek(12)
        json_length = struct.unpack("<I", stream.read(4))[0]
        stream.seek(20 + json_length)
        binary_length, chunk = struct.unpack("<II", stream.read(8))
        if chunk != 0x004E4942:
            return [], {}
        binary_offset = stream.tell()
    meshes = document.get("meshes", [])
    primitives = [primitive for mesh in meshes for primitive in mesh.get("primitives", [])]
    budget = max(12, 10000 // max(1, len(primitives)))
    for primitive in primitives:
        index = primitive.get("attributes", {}).get("POSITION")
        if index is None:
            continue
        access = document["accessors"][index]
        if access.get("componentType") != 5126 or access.get("type") != "VEC3" or "bufferView" not in access:
            continue
        view = document["bufferViews"][access["bufferView"]]
        offset = view.get("byteOffset", 0) + access.get("byteOffset", 0)
        stride, count = view.get("byteStride", 12), access["count"]
        if view.get("buffer", 0) or count <= 0 or offset < 0 or stride < 12 or offset + stride * (count - 1) + 12 > binary_length:
            continue
        extent = stride * (count - 1) + 12
        mapping = np.memmap(path, dtype="u1", mode="r", offset=binary_offset + offset, shape=(extent,))
        points = np.ndarray((count, 3), dtype="<f4", buffer=mapping, strides=(stride, 4))
        sample = points[np.linspace(0, count - 1, min(count, budget), dtype=int)]
        vertices.append(sample[np.isfinite(sample).all(axis=1)])
    if not vertices or not len(points := np.concatenate(vertices)):
        return [], {}
    points = points.astype(np.float64)
    images = []
    for angle in (0.0, np.pi / 4, np.pi / 2):
        yz = points[:, 2] * np.cos(angle) + points[:, 0] * np.sin(angle)
        xy = np.column_stack((points[:, 0] * np.cos(angle) - points[:, 2] * np.sin(angle), -points[:, 1]))
        lo, hi = xy.min(axis=0), xy.max(axis=0)
        scale = min(228 / max(1e-9, hi[0] - lo[0]), 136 / max(1e-9, hi[1] - lo[1]))
        pixels = np.rint((xy - (lo + hi) / 2) * scale + [128, 80]).astype(int)
        depth = np.rint((yz - yz.min()) / max(1e-9, float(np.ptp(yz))) * 100).astype("u1")
        layer = np.full((160, 256), 25, dtype="u1")
        for dy, dx in ((0, 0), (0, 1), (1, 0)):
            np.maximum.at(layer, (pixels[:, 1] + dy, pixels[:, 0] + dx), 112 + depth)
        images.append(Image.fromarray(layer).convert("RGB"))
    return images, {"thumbnail_basis": "neutral GLB vertex positions in mesh-local coordinates"}

def preview(path):
    try:
        if path.suffix.lower() in MOVIES:
            images, data = video_preview(path)
        elif path.suffix.lower() in {".glb", ".vrm"}:
            images, data = glb_preview(path)
        else:
            images, data = [scaled_image(path)], {}
        if not images:
            return None, data
        sheet = Image.new("RGB", (256 * len(images), 160), (25, 25, 25))
        for idx, frame in enumerate(images):
            sheet.paste(frame, (256 * idx + (256 - frame.width) // 2, (160 - frame.height) // 2))
        out = io.BytesIO()
        sheet.save(out, format="JPEG", quality=82, optimize=False, progressive=False)
        return out.getvalue(), data
    except (OSError, ValueError, IndexError, TypeError, KeyError, struct.error, subprocess.SubprocessError, Image.DecompressionBombError):
        return None, {}
