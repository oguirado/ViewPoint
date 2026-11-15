#!/usr/bin/env python3
"""Publish one SYNTHETIC frame record into RT/frames (testing / live-check helper).

In the real capsule the broker is the only frame producer; this helper exists so the
agent side can be exercised without a broker or a vision job.

    make_frame.py --rt RT [--frame 0] [--png existing.png | --render]

--render renders the factory-startup Blender scene with `blender -b` (Cycles CPU,
small); run it inside the systemd-run memory scope. Without --png/--render a tiny
flat grey PNG is generated with the stdlib.
"""
import argparse
import hashlib
import json
import os
import shutil
import struct
import subprocess
import sys
import time
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import contract  # noqa: E402

RENDER_PY = (
    "import bpy\n"
    "s = bpy.data.scenes[0]\n"
    "s.render.engine = 'CYCLES'\n"
    "s.cycles.device = 'CPU'\n"
    "s.cycles.samples = 8\n"
    "s.cycles.use_denoising = False\n"
    "s.render.resolution_x = 640\n"
    "s.render.resolution_y = 360\n"
    "s.render.resolution_percentage = 100\n"
    "s.render.image_settings.file_format = 'PNG'\n"
    "for o in list(bpy.data.objects):\n"
    "    if o.type == 'MESH':\n"
    "        bpy.data.objects.remove(o)\n"
    "s.render.filepath = %r\n"
    "bpy.ops.render.render(write_still=True)\n"
)


def flat_png(path, w=64, h=36, rgb=(60, 60, 60)):
    def chunk(tag, data):
        c = struct.pack(">I", len(data)) + tag + data
        return c + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    raw = (b"\x00" + bytes(rgb) * w) * h
    png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))
    with open(path, "wb") as fh:
        fh.write(png)


def publish(rt, frame=0, png_src=None, render=False, by_event=None):
    d = os.path.join(rt, "frames")
    os.makedirs(d, exist_ok=True)
    png = os.path.join(d, "frame_%d.png" % frame)
    if render:
        r = subprocess.run(["blender", "-b", "--factory-startup", "--python-expr", RENDER_PY % png],
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=300)
        if r.returncode != 0 or not os.path.exists(png):
            raise RuntimeError("blender render failed rc=%s: %s" % (r.returncode, r.stdout[-400:]))
    elif png_src:
        shutil.copyfile(png_src, png)
    else:
        flat_png(png)
    with open(png, "rb") as fh:
        sha = hashlib.sha256(fh.read()).hexdigest()
    rec = {"v": 1, "frame": frame, "png": png, "sha256": sha, "ts_ms": int(time.time() * 1000),
           "changed_regions": [], "by_event": by_event}
    path = os.path.join(d, "frame_%d.json" % frame)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(rec, fh, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, path)
    return path


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--rt", default=None)
    ap.add_argument("--frame", type=int, default=0)
    ap.add_argument("--png", default=None)
    ap.add_argument("--render", action="store_true")
    a = ap.parse_args(argv)
    rt = os.path.realpath(contract.rt_root(a.rt))
    if not rt.startswith("/dev/shm/vqa-cu/ref"):
        print("RT must be under /dev/shm/vqa-cu/ref*", file=sys.stderr)
        return 2
    print(publish(rt, a.frame, a.png, a.render))
    return 0


if __name__ == "__main__":
    sys.exit(main())
