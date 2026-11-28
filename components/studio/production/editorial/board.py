# SPDX-License-Identifier: AGPL-3.0-only
"""Draw the clay boards and render editable per-shot media on the CPU."""
import math
from pathlib import Path
import subprocess

from PIL import Image, ImageDraw

from blocking import frame_state, project_point, resolve
from common import ENV, Parser, destination, digest, dump, project, run, slot

AA = 2
RGB = "rgb24"


def mix(a, b, u):
    return tuple(round(x + (y - x) * u) for x, y in zip(a, b))


class Stage:
    def __init__(self, block, shot):
        self.block = block
        self.shot = shot
        self.pixels = tuple(block["pixels"])
        self.gain = shot["lighting"]["key"]
        self.background = self.room()

    def point(self, p):
        return tuple(round(v * AA) for v in project_point(p, self.shot["camera_binding"], self.pixels))

    def tint(self, paint):
        return tuple(max(0, min(255, round(v * g))) for v, g in zip(paint, self.gain))

    def poly(self, draw, points, color):
        draw.polygon([self.point(point) for point in points], fill=tuple(color))

    def line(self, draw, points, color, width=1):
        draw.line([self.point(point) for point in points], fill=tuple(color), width=AA * width, joint="curve")

    def disc(self, draw, point, size, color):
        x, y = self.point(point)
        rx, ry = (max(1, round(AA * v)) for v in size)
        draw.ellipse((x-rx, y-ry, x+rx, y+ry), fill=tuple(color))

    def box(self, draw, center, size, color):
        x, y, z = center
        w, d, h = (a / 2 for a in size)
        a, b = (x-w, y-d, z-h), (x+w, y-d, z-h)
        c, e = (x+w, y-d, z+h), (x-w, y-d, z+h)
        f, g, j = (x+w, y+d, z-h), (x+w, y+d, z+h), (x-w, y+d, z+h)
        self.poly(draw, (b, f, g, c), mix(color, (0, 0, 0), .25))
        self.poly(draw, (a, b, c, e), color)
        self.poly(draw, (e, c, g, j), mix(color, (255, 255, 255), .19))

    def room(self):
        image = Image.new("RGB", tuple(v * AA for v in self.pixels))
        draw = ImageDraw.Draw(image)
        light = self.shot["lighting"]
        width, height = image.size
        for row in range(height):
            draw.line((0, row, width, row), fill=mix(light["wall_top"], light["wall_bottom"], row / height))
        for x in (-5.0, -3.3, -1.6, .1, 1.8, 3.5):
            self.line(draw, [(x, 2.8, .1), (x, 2.8, 5)], mix(light["wall_top"], light["wall_bottom"], .33), 2)
            self.line(draw, [(x+.02, 2.8, .1), (x+.02, 2.8, 5)], mix(light["wall_top"], light["wall_bottom"], .61))
        self.poly(draw, [(-12, 2.6, 0), (12, 2.6, 0), (12, -12, 0), (-12, -12, 0)], light["floor"])
        self.box(draw, (0, 2.58, .07), (23, .14, .14), mix(light["floor"], light["wall_bottom"], .45))
        for x in range(-8, 9):
            self.line(draw, [(x, -8, 0), (x, 2.53, 0)], mix(light["floor"], light["shadow"], .55))
        for y in (-3, -1.4, -.45, .85, 1.72):
            self.line(draw, [(-12, y, 0), (12, y, 0)], mix(light["floor"], light["shadow"], .55))
        self.box(draw, (-.03, .51, .34), (.94, .55, .64), self.tint((67, 82, 94)))
        self.box(draw, (-.03, .45, .56), (1.19, .83, .12), self.tint((164, 140, 115)))
        for z in (.24, .31, .38):
            self.line(draw, [(-.35, .225, z), (.29, .225, z)], self.tint((53, 64, 73)), 2)
        self.box(draw, (.89, .42, .435), (.25, .30, .82), self.tint((83, 95, 108)))
        return image

    def pin(self, draw, actor, state, asset):
        x, y = actor["root"]
        height = asset["height_m"]
        sway = 0 if state["loaded"] else math.sin(state["frame"] * .55)
        right = 1 if state["actors"][actor["gaze"]]["root"][0] > x else -1
        sole = asset["sole_m"]
        self.disc(draw, (x+.03, y+.06, 0), (25, 6), self.shot["lighting"]["shadow"])
        for side in (1, -1):
            step = sway * side
            foot = (x + side * .11 + step*.1, y + side*.11, .062 + .05 * max(0, step))
            self.line(draw, [(x, y+side*.075, .43), foot], self.tint((69, 89, 105)), 7)
            self.box(draw, foot, (sole[0]*1.22, sole[1], .10), self.tint((152, 172, 177)))
        body = self.tint((99, 156, 145))
        self.box(draw, (x, y, .62), (.35, .30, .47), body)
        self.box(draw, (x, y, height*.81), (.33, .25, .27), self.tint((198, 220, 200)))
        self.box(draw, (x-.025, y, height*.96), (.10, .15, .075), self.tint((159, 182, 171)))
        for dx in (.01, .095):
            self.disc(draw, (x+right*dx, y-.133, height*.82), (3, 5), (22, 58, 65))
        self.box(draw, (x-.02, y-.153, .62), (.23, .023, .09), self.tint((62, 104, 104)))
        shoulder = (x+.175, y-.17, .75)
        idle = (x+.25, y-.17, .44)
        sx, sy, _ = self.block["set"]["switch_m"]
        target = (sx, sy-.012, state["switch_z"]+.059)
        hand = tuple(a + (b-a)*state["reach"] for a, b in zip(idle, target))
        elbow = ((shoulder[0]+hand[0])/2, y-.20, max(shoulder[2], hand[2])-.045)
        self.line(draw, [shoulder, elbow], self.tint((73, 128, 126)), 9)
        self.line(draw, [elbow, hand], self.tint((145, 200, 181)), 6)
        self.disc(draw, elbow, (4, 4), self.tint((66, 108, 118)))
        self.disc(draw, hand, (5, 7), self.tint((199, 230, 203)))

    def cart(self, draw, actor, state, asset):
        x, y = actor["root"]
        direction = 1 if state["actors"][actor["gaze"]]["root"][0] > x else -1
        lean = state["reply"] * direction
        self.disc(draw, (x, y+.03, 0), (35, 9), self.shot["lighting"]["shadow"])
        for side in (1, -1):
            offset, lower = asset["wheel_offset_m"]
            self.box(draw, (x, y+side*offset, asset["body_z_m"]+lower), asset["wheel_m"], self.tint((47, 57, 65)))
        for dx in (-.18, .18):
            self.disc(draw, (x+dx, y-.326, .13), (8, 8), self.tint((87, 95, 102)))
            self.disc(draw, (x+dx, y-.33, .13), (3, 3), self.tint((152, 155, 157)))
        enamel = tuple(round(255*c**(1/2.2)) for c in asset["body_rgb_linear"])
        self.box(draw, (x, y, asset["body_z_m"]), asset["body_m"], self.tint(enamel))
        self.box(draw, (x+.09*lean, y-.25, .41+.04*state["reply"]), (.50, .08, .19),
                 self.tint((38, 51, 55)))
        for dx in (-.12, .09):
            self.disc(draw, (x+dx+.13*lean, y-.303, .41+.04*state["reply"]), (4, 6),
                      (166, 254, 202) if state["signal"] else (77, 115, 111))
        self.box(draw, (x+.18, y, .61), (.085, .09, .15), self.tint((82, 92, 95)))
        self.disc(draw, (x+.18, y, .72), (5, 5),
                  (159, 253, 214) if state["signal"] else self.tint((91, 111, 115)))

    def frame(self, edit_frame):
        state = frame_state(self.block, edit_frame)
        image = self.background.copy()
        draw = ImageDraw.Draw(image)
        x, y, z = self.block["set"]["signal_m"]
        self.box(draw, (x, y, z-.06), (.43, .18, .40), self.tint((116, 138, 150)))
        shade = (64, 223, 204) if state["signal"] else (33, 65, 73)
        self.box(draw, (x, y-.115, z-.027), (.32, .06, .22), shade)
        self.disc(draw, (x, y-.15, z), (9, 6), (213, 255, 221) if state["signal"] else (49, 82, 85))
        sx, sy, sz = self.block["set"]["switch_m"]
        self.box(draw, (sx, sy, sz-.037), (.29, .27, .10), self.tint((62, 74, 83)))
        self.box(draw, (sx, sy, state["switch_z"]+.015), (.19, .17, .07), self.tint((232, 135, 58)))
        for form in ("cart", "swivel"):
            actor_id = self.block["cast_by_form"][form]
            drawing = self.cart if form == "cart" else self.pin
            drawing(draw, state["actors"][actor_id], state, self.block["assets"][self.block["cast"][actor_id]["asset"]])
        return image.resize(self.pixels, Image.Resampling.LANCZOS)


def write_boards(block, root):
    boards = root / "boards"
    boards.mkdir()
    rows = []
    for shot in block["shots"]:
        frame = shot["board_frame"]
        image = boards / f'{shot["id"]}.png'
        Stage(block, shot).frame(frame).save(image)
        rows.append({"path": str(image.relative_to(root)), "sha256": digest(image),
                     "edit_frame": frame, "shot": shot["id"], "beat": shot["beat"],
                     "camera": shot["camera"], "light": shot["light"]})
    dump(root / "boards.json", {"schema": "rp4.editorial-boards/v1", "title": block["title"],
                                "pixels": block["pixels"], "frames": rows})
    return rows


def write_sources(block, root):
    paths = root / "shots"
    paths.mkdir()
    rows = []
    w, h = block["pixels"]
    for shot in block["shots"]:
        stage = Stage(block, shot)
        target = paths / f'{shot["id"]}.mkv'
        argv = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-f", "rawvideo",
                "-pix_fmt", RGB, "-s", f"{w}x{h}", "-r", str(block["fps"]), "-i", "pipe:0",
                "-c:v", "ffv1", "-threads", "1", "-map_metadata", "-1", "-fflags", "+bitexact",
                "-flags:v", "+bitexact", str(target)]
        clip_start = shot["edit"][0] - shot["handles"][0]
        with subprocess.Popen(argv, stdin=subprocess.PIPE, stderr=subprocess.PIPE, env=ENV) as encoder:
            try:
                for source_frame in range(shot["source"][0], shot["source"][1]+1):
                    frame = clip_start + source_frame - 1
                    encoder.stdin.write(stage.frame(frame).tobytes())
            except BrokenPipeError:
                pass
            finally:
                encoder.stdin.close()
            error = encoder.stderr.read()
            if encoder.wait():
                raise RuntimeError(error.decode(errors="replace"))
        rows.append({"id": shot["id"], "path": str(target.relative_to(root)), "sha256": digest(target)})
    return rows


def main():
    args = Parser(description=__doc__)
    slot(args)
    args = args.parse_args()
    block = resolve(project(args.project))
    with destination(args.out) as stage:
        rows = write_boards(block, stage)
    return {"status": "BOARDS_READY", "boards": rows}


if __name__ == "__main__":
    raise SystemExit(run(main))
