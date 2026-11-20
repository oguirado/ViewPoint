# SPDX-License-Identifier: AGPL-3.0-only
"""Authored, versioned camera and terrain fixtures for wide scene reconstruction."""

import hashlib
import json
import random
from dataclasses import dataclass

from PIL import Image, ImageDraw

from .game import HEIGHT, STEP_MS, TILE, WIDTH


PROFILE = "wide-v1"
AIR = "."
TERRAIN = {
    AIR: "air",
    "#": "ground",
    "=": "elevated_platform",
    "|": "cliff",
    "/": "rising_slope",
    "\\": "falling_slope",
}
SHOTS = 3
POS_PER_SHOT = 9


def seeded(seed, domain):
    """Keep the parameters of each independently sampled feature on its own stream."""
    key = f"{PROFILE}:{domain}:{seed}".encode("ascii")
    return random.Random(hashlib.sha256(key).digest())


def terrain(seed):
    extent = seeded(seed, "extent")
    height = extent.randint(12, 15)
    width = extent.randint(29, 35)
    shaping = seeded(seed, "terrain")
    rows = [[AIR] * width for _ in range(height)]
    floor = shaping.randint(5, 7)
    slope_x = shaping.randint(5, 7)
    for x in range(width):
        for y in range(floor + 1, height):
            rows[y][x] = "#"
        rows[floor][x] = shaping.choice(("#", "#", AIR)) if x > 16 else "#"
    for x, code in enumerate(("/", "|", "\\"), slope_x):
        rows[floor - 1][x] = code
    cliff_x = shaping.randint(10, 12)
    for y in range(floor - 3, floor):
        rows[y][cliff_x] = "|"
    # The south and east edges of this raised platform supply clipping witnesses.
    rows[3][3:5] = ["=", "="]
    y = shaping.choice((1, 2))
    x = shaping.randint(7, 9)
    rows[y][x:x + 2] = ["=", "="]
    return tuple("".join(row) for row in rows)


def tile_scanlines(code):
    """Half-open horizontal support at each local y, including triangular tiles."""
    if code == AIR:
        return
    for y in range(TILE):
        if code == "/":
            yield y, TILE - y - 1, TILE
        elif code == "\\":
            yield y, 0, y + 1
        else:
            yield y, 0, TILE


def atlas(seed):
    rng = seeded(seed, "sheet")
    terrain_sheet = Image.new("RGBA", ((len(TERRAIN) - 1) * TILE, TILE))
    actor_sheet = Image.new("RGBA", (4 * TILE, TILE))
    colors = {}
    for i, code in enumerate(code for code in TERRAIN if code != AIR):
        base = (rng.randint(35, 105), rng.randint(50, 135), rng.randint(70, 200))
        colors[code] = base
        draw = ImageDraw.Draw(terrain_sheet)
        for y, left, right in tile_scanlines(code):
            draw.line((i * TILE + left, y, i * TILE + right - 1, y), fill=base)
        # The opaque support stays unchanged as the sheet texture changes.
        for _ in range(10):
            x, y = rng.randrange(TILE), rng.randrange(TILE)
            px = i * TILE + x
            if terrain_sheet.getpixel((px, y))[3]:
                draw.point((px, y), fill=tuple(min(255, c + 31) for c in base))
    body = (rng.randint(160, 235), rng.randint(140, 215), rng.randint(35, 90))
    draw = ImageDraw.Draw(actor_sheet)
    for i in range(4):
        x = i * TILE
        draw.ellipse((x + 3, 2, x + 12, 11), fill=body)
        draw.rectangle((x + 7 + (i % 2), 4, x + 9 + (i % 2), 5), fill="#273144")
        draw.line((x + 4, 10, x + 1 + i, 14), fill=body, width=2)
        draw.line((x + 10, 10, x + 14 - i, 14), fill=body, width=2)
    tiles = {code: terrain_sheet.crop((i * TILE, 0, (i + 1) * TILE, TILE))
             for i, code in enumerate(code for code in TERRAIN if code != AIR)}
    poses = tuple(actor_sheet.crop((i * TILE, 0, (i + 1) * TILE, TILE)) for i in range(4))
    return tiles, poses, colors


@dataclass(frozen=True)
class Frame:
    index: int
    tick: int
    shot: int
    camera: tuple[int, int]
    hud: tuple[int, int, int, int] | None
    actor: tuple[int, int]
    pose: int

    @property
    def pts_ms(self):
        return self.tick * STEP_MS

    @property
    def path(self):
        return f"frames/{self.index:05d}.png"


def timeline(seed):
    pan = seeded(seed, "camera")
    clock = seeded(seed, "clock")
    hud = seeded(seed, "hud")
    actor = seeded(seed, "actor")
    tick = 0
    for shot in range(SHOTS):
        hud_x, hud_y = hud.randint(36, 58), hud.randint(4, 18)
        actor_x, actor_y = actor.randint(101, 123), actor.randint(80, 97)
        actor_step = actor.choice((-3, -2, 2, 3))
        path = (
            (-pan.randint(2, 11), -pan.randint(2, 9)),
            (pan.randint(7, 22), pan.randint(0, 12)),
            (3 * TILE + TILE - 1, 0),
            (pan.randint(0, 10), 3 * TILE + TILE - 1),
            (pan.randint(84, 103), pan.randint(21, 39)),
            (pan.randint(107, 151), pan.choice((65, 81, 97))),
            (pan.randint(74, 94), pan.randint(12, 28)),
            (pan.randint(26, 40), pan.randint(-9, 3)),
            (pan.randint(43, 57), pan.randint(8, 22)),
        )
        for i, camera in enumerate(path):
            index = shot * POS_PER_SHOT + i
            box = None if 2 <= i <= 3 else (hud_x + 4 * i, hud_y + 2 * i, 64, 13)
            x = actor_x + actor_step * i
            y = actor_y - i * (POS_PER_SHOT - 1 - i)
            yield Frame(index, tick, shot, camera, box, (x, y), (index + shot) % 4)
            tick += clock.choice((1, 2, 3))


@dataclass(frozen=True)
class Background:
    upper: tuple[int, int, int]
    lower: tuple[int, int, int]
    layers: tuple[tuple[int, int, int, tuple[int, int, int]], ...]
    phase: int


def background(seed, platform_color):
    rng = seeded(seed, "sky")
    upper = (rng.randint(105, 185), rng.randint(28, 85), rng.randint(125, 220))
    lower = (rng.randint(50, 135), rng.randint(115, 185), rng.randint(95, 180))
    # Swap colour while retaining the platform's rounded luma. This broad
    # band supplies pixels that a luma-only classifier cannot distinguish.
    r, g, b = platform_color
    equal_luma = (r + 24, g - 10, b - 11)
    layers = tuple((factor, rng.randint(31, 57), rng.randint(0, 113), color)
                   for factor, color in zip((rng.randint(1, 3), rng.randint(4, 6)),
                                            (equal_luma, lower), strict=True))
    return Background(upper, lower, layers, rng.randint(0, 113))


def paint_sky(image, frame, style):
    draw = ImageDraw.Draw(image)
    for y in range(HEIGHT):
        shift = (y * 3 + frame.shot * 37 + style.phase) % (2 * HEIGHT)
        t = min(shift, 2 * HEIGHT - shift)
        color = tuple((a * (HEIGHT - t) + b * t) // HEIGHT
                      for a, b in zip(style.upper, style.lower, strict=True))
        draw.line((0, y, WIDTH - 1, y), fill=color)
    x, y = frame.camera
    for i, (rate, stride, phase, color) in enumerate(style.layers):
        base = 22 + i * 48 - y * rate // 8
        drift = x * rate // 8 + phase
        repeat = 2 * stride
        for left in range(-drift % repeat - repeat, WIDTH, repeat):
            draw.polygon(((left, base + 12), (left + stride // 2, base - 8),
                          (left + stride, base + 1), (left + repeat, base + 12)),
                         fill=color)


def render(frame, rows, tiles, poses, style):
    image = Image.new("RGB", (WIDTH, HEIGHT))
    paint_sky(image, frame, style)
    x, y = frame.camera
    for row in range(max(0, y // TILE), min(len(rows), (y + HEIGHT + TILE - 1) // TILE)):
        for col in range(max(0, x // TILE), min(len(rows[row]), (x + WIDTH + TILE - 1) // TILE)):
            code = rows[row][col]
            if code != AIR:
                tile = tiles[code]
                image.paste(tile, (col * TILE - x, row * TILE - y), tile)
    draw = ImageDraw.Draw(image)
    # A world-fixed asymmetric prop gives a recoverable coordinate origin.
    draw.line((8 - x, 8 - y, 8 - x, 22 - y, 19 - x, 22 - y), fill="#f2c983", width=2)
    draw.rectangle((12 - x, 11 - y, 15 - x, 14 - y), fill="#3b283b")
    sprite = poses[frame.pose]
    image.paste(sprite, (frame.actor[0] - x, frame.actor[1] - y), sprite)
    if frame.hud is not None:
        left, top, w, h = frame.hud
        draw.rectangle((left, top, left + w - 1, top + h - 1), fill="#221927")
        # The displayed scene number changes on cuts. HUD motion within a
        # scene is independent and its disappearance does not mark a cut.
        for i in range(frame.shot + 1):
            mark = left + 5 + 4 * i
            draw.rectangle((mark, top + 4, mark + 1, top + h - 5), fill="#f4d19e")
    return image


def tape(frame, previous):
    # Director actions contain no world coordinates, seed or expected output.
    return {"frame_index": frame.index, "pts_ms": frame.pts_ms,
            "steps": [{"tick": tick, "sample": tick == frame.tick}
                      for tick in range(previous + 1, frame.tick + 1)]}


def write_observations(stage, seed, encode_movie, probe_timing, validate_timing):
    rows = terrain(seed)
    tiles, poses, colors = atlas(seed)
    style = background(seed, colors["="])
    records = []
    previous = -1
    (stage / "frames").mkdir()
    with (stage / "inputs.jsonl").open("w", encoding="utf-8") as inputs:
        for frame in timeline(seed):
            render(frame, rows, tiles, poses, style).save(stage / frame.path)
            records.append({"path": frame.path, "pts_ms": frame.pts_ms})
            inputs.write(json.dumps(tape(frame, previous), separators=(",", ":")) + "\n")
            previous = frame.tick
    duration = records[-1]["pts_ms"] + STEP_MS
    encode_movie(stage, records, duration)
    validate_timing({"frames": records, "video": {"duration_ms": duration}},
                    probe_timing(stage / "recording.mp4"), tolerance_ms=0)
