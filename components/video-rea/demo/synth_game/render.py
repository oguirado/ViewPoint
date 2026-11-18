# SPDX-License-Identifier: AGPL-3.0-only
"""Pillow rasterizer. Actor coordinates are in level pixels, with y increasing down."""
from PIL import Image, ImageDraw, ImageFont

from .game import FLOOR, HEIGHT, ROWS, RULES, TILE, WIDTH

FONT = ImageFont.load_default(size=10)


def render(frame):
    image = Image.new("RGB", (WIDTH, HEIGHT), (17, 28, 49))
    draw = ImageDraw.Draw(image)
    camera = frame["camera_x_px"]
    for k in range(40):
        x = (k * 71 - camera // 4) % WIDTH
        y = 19 + k * 31 % 64
        draw.point((x, y), fill=(112, 147, 167))
    for col, tile in enumerate(ROWS[-1]):
        if tile == "#":
            x = col * TILE - camera
            draw.rectangle((x, FLOOR, x + TILE - 1, HEIGHT - 1), fill=(57, 69, 89))
            draw.line((x, FLOOR, x + TILE - 2, FLOOR), fill=(158, 197, 183))
            draw.line((x + 2, FLOOR + 9, x + 7, FLOOR + 9), fill=(83, 101, 115))
    goal = RULES["goal_x_px"] - camera
    draw.line((goal, FLOOR - 24, goal, FLOOR - 1), fill="white")
    draw.polygon([(goal, FLOOR - 24), (goal + 11, FLOOR - 20), (goal, FLOOR - 16)], fill="#ffa971")
    for kind, actor in frame["entities"].items():
        x, y = round(actor["x_px"] - camera), round(actor["y_px"])
        if kind == "player":
            draw.rectangle((x - 4, y - 12, x + 4, y - 1), fill=(82, 235, 207))
            draw.rectangle((x + 1, y - 10, x + 3, y - 8), fill="#102b3c")
        elif kind == "walker":
            draw.rounded_rectangle((x - 4, y - 4, x + 4, y + 4), radius=2, fill="#ed705a")
            eye = x + (2 if actor["state"] == "right" else -2)
            draw.point((eye, y - 2), fill="white")
        elif kind == "firefly":
            draw.ellipse((x - 4, y - 4, x + 4, y + 4), fill="#beaaff")
            wing_y = y - 2 if actor["state"] == "rise" else y + 2
            draw.line((x - 6, wing_y, x + 6, wing_y), fill="#eee1ff")
        elif actor["state"] == "available":
            draw.polygon([(x, y - 4), (x + 3, y), (x, y + 4), (x - 3, y)], fill="#ffcb69")
    draw.rectangle((0, 0, WIDTH - 1, 15), fill="#22364b")
    draw.text((4, 1), f"SCORE  {frame['score']:03d}", fill="#f4e9ce", font=FONT)
    draw.text((WIDTH - 55, 1), f"LIVES  {frame['lives']}", fill="#f4e9ce", font=FONT)
    panels = {"title": ("BEACON RUN", "Press start"), "game_over": ("GAME OVER", "Press start to retry"),
              "win": ("BEACON REACHED", "Course complete")}
    if frame["screen"] in panels:
        title, subtitle = panels[frame["screen"]]
        draw.rounded_rectangle((31, 34, 160, 78), radius=4, fill="#182f42", outline="#76b8ac")
        draw.text((WIDTH // 2, 47), title, anchor="mm", fill="#ffcb69", font=FONT)
        draw.text((WIDTH // 2, 65), subtitle, anchor="mm", fill="#e3ece9", font=FONT)
    return image
