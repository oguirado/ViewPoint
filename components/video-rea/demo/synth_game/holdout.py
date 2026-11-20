# SPDX-License-Identifier: AGPL-3.0-only
"""Seeded holdout courses, palettes and traversal tape."""
import random
from dataclasses import dataclass

from .game import Course, FLOOR, RULES, SCALE, STEP_MS, TILE, WIDTH, WORLD

HUD_WIDTH = 144
HOLDOUT_TICKS = 530
RETRY_TICK = 165

PALETTE_VARIANTS = (
    # Sky, distant detail, ground, ground edge, ground detail, HUD, panel, player, walker, firefly, pickup.
    ("#30203c", "#ada0d9", "#513b56", "#fa91b5", "#816182", "#4d2b52", "#39253e", "#9bf0e1", "#ff8a7b", "#b9a2ff", "#ffd77e"),
    ("#20332d", "#97b98d", "#405d3e", "#b8d881", "#6b8763", "#304a38", "#243e32", "#f7d888", "#f99473", "#8fe0dd", "#ffe597"),
    ("#18394a", "#92b9ca", "#385b68", "#9ddde8", "#578797", "#28475c", "#20394e", "#b6f496", "#ff9a8e", "#ffd490", "#f8e09d"),
    ("#503324", "#bd9586", "#725244", "#f7bf8a", "#a07e69", "#563e35", "#48332f", "#a9ecea", "#f1a3ce", "#cdf193", "#ffda71"),
    ("#292d46", "#8fa5cf", "#454e79", "#b4baf2", "#6e7fb2", "#3e365c", "#2c304e", "#f7aee7", "#f8a67c", "#91e6d6", "#fbea8a"),
    ("#3f2630", "#c88f96", "#63424b", "#f6ad99", "#956671", "#5b3540", "#492e41", "#e1ee9b", "#ff9d79", "#a9d8f9", "#f9db94"),
)
PALETTE_ROLES = ("sky", "distant", "floor", "edge", "floor_detail", "hud", "panel",
                 "player", "walker", "firefly", "pickup")


@dataclass(frozen=True)
class Holdout:
    course: Course
    enemies: dict
    palette: dict[str, str]
    hud_origin: tuple[int, int]
    jump_ticks: tuple[int, int]

    def controls(self, tick):
        return {"tick": tick, "start": tick in (8, RETRY_TICK), "right": tick > 8,
                "jump": tick in self.jump_ticks}


def seeded_holdout(seed):
    rng = random.Random(seed)
    columns = rng.randint(29, 44)
    gap_col, gap_tiles = rng.randint(13, 19), rng.randint(1, 3)
    gap_left, gap_end = gap_col * TILE, (gap_col + gap_tiles) * TILE
    goal = columns * TILE - 24
    floor = "#" * gap_col + "." * gap_tiles + "#" * (columns - gap_col - gap_tiles)
    # The horizontal camera never exposes the row below the playfield.
    rows = ["." * columns] * (FLOOR // TILE) + [floor, "#" * columns]

    walker_x = rng.randint(84, 114)
    enemies = {
        "walker": {"x_px": walker_x, "y_px": FLOOR - 5, "axis": "x",
                   "travel_px": rng.randint(10, 16), "speed_px_s": rng.randint(22, 27)},
        "firefly": {"x_px": rng.randint(gap_end + 12, goal - 20), "y_px": rng.randint(20, 25),
                    "axis": "y", "travel_px": rng.randint(8, 11), "speed_px_s": rng.randint(38, 58)},
    }
    course = Course(rows, {"star_west": rng.randint(36, 56),
                           "star_east": rng.randint(gap_end + 66, goal - 12)},
                    goal, rng.randint(56, 132))
    palette = dict(zip(PALETTE_ROLES, rng.choice(PALETTE_VARIANTS), strict=True))
    hud_origin = (rng.randint(0, WIDTH - HUD_WIDTH), rng.choice((rng.randint(0, 10), FLOOR)))

    # The first traversal establishes damage and retry. The second jumps before the walker and
    # the gap; those clearances leave room for the walker's patrol and the widest gap.
    step_px = WORLD["run_speed_m_s"] * SCALE * STEP_MS / 1000
    jump_ticks = tuple(RETRY_TICK + 1 + int((x - RULES["start_x_px"]) // step_px)
                       for x in (walker_x - 20, gap_left - 12))
    return Holdout(course, enemies, palette, hud_origin, jump_ticks)
