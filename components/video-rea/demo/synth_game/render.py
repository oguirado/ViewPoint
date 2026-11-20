# SPDX-License-Identifier: AGPL-3.0-only
"""Pillow rasterizer. Actor coordinates are in level pixels, with y increasing down."""
from PIL import Image, ImageDraw, ImageFont

from .game import DEV_COURSE, FLOOR, HEIGHT, TILE, WIDTH
from .holdout import HUD_WIDTH

FONT = ImageFont.load_default(size=10)
DEV_PALETTE = {
    "sky": "#111c31", "distant": "#7093a7", "floor": "#394559",
    "edge": "#9ec5b7", "floor_detail": "#536573", "hud": "#22364b",
    "panel": "#182f42", "player": "#52ebcf", "walker": "#ed705a",
    "firefly": "#beaaff", "pickup": "#ffcb69",
}
PANELS = {"title": ("BEACON RUN", "Press start"), "game_over": ("GAME OVER", "Press start to retry"),
          "win": ("BEACON REACHED", "Course complete")}
OVERLAY_RADIUS = 4


def visual_label(kind, box):
    """Label a projected half-open rectangle and its viewport intersection."""
    if box is None:
        return {"kind": kind, "bbox_px": None, "viewport_bbox_px": None}
    x1, y1, x2, y2 = box
    clipped = [max(0, x1), max(0, y1), min(WIDTH, x2), min(HEIGHT, y2)]
    if clipped[0] >= clipped[2] or clipped[1] >= clipped[3]:
        clipped = None
    return {"kind": kind, "bbox_px": list(box), "viewport_bbox_px": clipped}


def render(frame, holdout=None):
    course = DEV_COURSE if holdout is None else holdout.course
    palette = DEV_PALETTE if holdout is None else holdout.palette
    image = Image.new("RGB", (WIDTH, HEIGHT), palette["sky"])
    draw = ImageDraw.Draw(image)
    camera = frame["camera_x_px"]
    for k in range(40):
        x = (k * 71 - camera // 4) % WIDTH
        y = 19 + k * 31 % 64
        draw.point((x, y), fill=palette["distant"])
    for col, tile in enumerate(course.floor_row):
        if tile == "#":
            x = col * TILE - camera
            draw.rectangle((x, FLOOR, x + TILE - 1, HEIGHT - 1), fill=palette["floor"])
            draw.line((x, FLOOR, x + TILE - 2, FLOOR), fill=palette["edge"])
            draw.line((x + 2, FLOOR + 9, x + 7, FLOOR + 9), fill=palette["floor_detail"])

    goal = course.goal_x_px - camera
    draw.line((goal, FLOOR - 24, goal, FLOOR - 1), fill="white")
    draw.polygon([(goal, FLOOR - 24), (goal + 11, FLOOR - 20), (goal, FLOOR - 16)], fill="#ffa971")
    actors = {}
    if holdout is not None:
        actors["beacon"] = visual_label("goal", (goal, FLOOR - 24, goal + 12, FLOOR))
    for kind, actor in frame["entities"].items():
        x, y = round(actor["x_px"] - camera), round(actor["y_px"])
        if kind == "player":
            box = (x - 4, y - 12, x + 5, y)
            draw.rectangle((x - 4, y - 12, x + 4, y - 1), fill=palette["player"])
            draw.rectangle((x + 1, y - 10, x + 3, y - 8), fill="#102b3c")
        elif kind == "walker":
            box = (x - 4, y - 4, x + 5, y + 5)
            draw.rounded_rectangle((x - 4, y - 4, x + 4, y + 4), radius=2, fill=palette["walker"])
            eye = x + (2 if actor["state"] == "right" else -2)
            draw.point((eye, y - 2), fill="white")
        elif kind == "firefly":
            box = (x - 6, y - 4, x + 7, y + 5)
            draw.ellipse((x - 4, y - 4, x + 4, y + 4), fill=palette["firefly"])
            wing_y = y - 2 if actor["state"] == "rise" else y + 2
            draw.line((x - 6, wing_y, x + 6, wing_y), fill="#eee1ff")
        elif actor["state"] == "available":
            box = (x - 3, y - 4, x + 4, y + 5)
            draw.polygon([(x, y - 4), (x + 3, y), (x, y + 4), (x - 3, y)], fill=palette["pickup"])
        else:
            box = None
        if holdout is not None:
            actors[kind] = visual_label("pickup" if kind.startswith("star_") else kind, box)

    widgets = {}

    def text(name, xy, value, color, anchor=None):
        draw.text(xy, value, fill=color, font=FONT, anchor=anchor)
        if holdout is not None:
            box = draw.textbbox(xy, value, font=FONT, anchor=anchor)
            widgets[name] = {**visual_label("text", box), "text": value}

    x, y, hud_width = (0, 0, WIDTH) if holdout is None else (*holdout.hud_origin, HUD_WIDTH)
    draw.rectangle((x, y, x + hud_width - 1, y + 15), fill=palette["hud"])
    if holdout is not None:
        widgets["hud"] = visual_label("panel", (x, y, x + hud_width, y + 16))
    text("score", (x + 4, y + 1), f"SCORE  {frame['score']:03d}", "#f4e9ce")
    text("lives", (x + hud_width - 55, y + 1), f"LIVES  {frame['lives']}", "#f4e9ce")

    if frame["screen"] in PANELS:
        title, subtitle = PANELS[frame["screen"]]
        draw.rounded_rectangle((31, 34, 160, 78), radius=OVERLAY_RADIUS, fill=palette["panel"], outline="#76b8ac")
        if holdout is not None:
            widgets["overlay"] = visual_label("panel", (31, 34, 161, 79))
        text("overlay_title", (WIDTH // 2, 47), title, palette["pickup"], anchor="mm")
        text("overlay_subtitle", (WIDTH // 2, 65), subtitle, "#e3ece9", anchor="mm")
    if holdout is not None:
        frame["actor_boxes"] = actors
        frame["widgets"] = widgets
    return image


def holdout_cells(level, frames):
    """Give a tile its label when a frame exposes any pixel outside the HUD and overlay."""
    tile = level["tile_px"]
    rows = level["rows"]
    coverage = Image.new("1", (len(rows[0]) * tile, len(rows) * tile))
    for frame in frames:
        visible = Image.new("1", (WIDTH, HEIGHT), 1)
        mask = ImageDraw.Draw(visible)
        widgets = frame["widgets"]
        left, top, right, bottom = widgets["hud"]["bbox_px"]
        mask.rectangle((left, top, right - 1, bottom - 1), fill=0)
        if "overlay" in widgets:
            left, top, right, bottom = widgets["overlay"]["bbox_px"]
            mask.rounded_rectangle((left, top, right - 1, bottom - 1), radius=OVERLAY_RADIUS, fill=0)
        x, y = frame["camera_x_px"], frame["camera_y_px"]
        coverage.paste(1, (x, y, x + WIDTH, y + HEIGHT), visible)

    return {
        f"{x},{y}": (level["legend"][symbol]
                     if coverage.crop((x * tile, y * tile, (x + 1) * tile, (y + 1) * tile)).getbbox()
                     else None)
        for y, row in enumerate(rows)
        for x, symbol in enumerate(row)
    }
