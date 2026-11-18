# SPDX-License-Identifier: AGPL-3.0-only
"""Authored level, input tape and fixed-step simulation for Beacon Run."""
import math
import random
from dataclasses import dataclass, field


WIDTH, HEIGHT = 192, 112
TILE, FLOOR = 16, 96
ROWS = ["." * 28 for _ in range(6)] + ["#" * 14 + ".." + "#" * 12]
STEP_MS = 20
SCALE = 16
WORLD = {
    "gravity_m_s2": 31.25,
    "run_speed_m_s": 6.25,
    "jump_impulse_m_s": 13.125,
    "friction_m_s2": 31.25,
}
RULES = {
    "initial_lives": 2, "damage_lives": 1, "pickup_points": 10,
    "player_half_width_px": 4, "player_height_px": 12, "pickup_radius_px": 3, "enemy_radius_px": 4,
    "start_x_px": 16, "floor_y_px": FLOOR, "goal_x_px": 424,
    "damage": "Touch an enemy or fall below the viewport. Lose a life and respawn on the next tick if lives remain.",
    "pickup": "Contact collects the star once per episode. Respawn preserves score and collected stars.",
    "win": "Reach the beacon on foot or in flight.", "lose": "Lives reach zero.",
    "audible_events": ["jump", "pickup", "damage", "death", "win", "lose"],
}
PICKUPS = {"star_west": 52, "star_east": 372}
DT = STEP_MS / 1000


def controls(tick):
    # First traverse without jumping. Retry with a coast interval and two jumps.
    retry_tick = tick - 135
    return {"tick": tick, "start": tick in (8, 135),
            "right": (8 < tick < 135) or (retry_tick > 0 and not 7 <= retry_tick <= 13),
            "jump": retry_tick in (35, 104)}


def spawns(seed, randomized):
    rng = random.Random(seed)
    offset = rng.randint(-5, 5) if randomized else 0
    height = rng.randint(-4, 4) if randomized else 0
    return {
        "walker": {"x_px": 108 + offset, "y_px": FLOOR - 5, "axis": "x",
                   "travel_px": 16, "speed_px_s": 25},
        "firefly": {"x_px": 329, "y_px": 38 + height, "axis": "y",
                    "travel_px": 18, "speed_px_s": 50},
    }


def pose(x, y, state):
    return {"x_px": x, "y_px": y, "state": state}


def moving_enemy(kind, spawn, age):
    span = spawn["travel_px"]
    phase = age * DT * spawn["speed_px_s"] % (2 * span)
    distance = min(phase, 2 * span - phase)
    if kind == "walker":
        return pose(spawn["x_px"] + distance, spawn["y_px"], "right" if phase < span else "left")
    return pose(spawn["x_px"], spawn["y_px"] + distance, "fall" if phase < span else "rise")


@dataclass
class Game:
    enemies: dict
    screen: str = "title"
    episode: int = 0
    age: int = 0
    x: float = RULES["start_x_px"]
    y: float = FLOOR
    vx: float = 0
    vy: float = 0
    grounded: bool = True
    respawn_pending: bool = False
    lives: int = RULES["initial_lives"]
    score: int = 0
    collected: set = field(default_factory=set)

    def respawn(self):
        self.x, self.y = RULES["start_x_px"], FLOOR
        self.vx, self.vy, self.grounded = 0, 0, True

    def entities(self):
        if self.screen == "title":
            return {}
        state = "run" if self.vx else "idle"
        if not self.grounded:
            state = "rise" if self.vy < 0 else "fall"
        if self.respawn_pending or self.screen == "game_over":
            state = "dead"
        entities = {"player": pose(self.x, self.y, state)}
        for kind, spawn in self.enemies.items():
            entities[kind] = moving_enemy(kind, spawn, self.age)
        for name, x in PICKUPS.items():
            entities[name] = pose(x, FLOOR - 7, "collected" if name in self.collected else "available")
        return entities

    def step(self, keys):
        if self.screen != "play":
            if not keys["start"]:
                return []
            self.screen = "play"
            self.episode += 1
            self.lives, self.score, self.age = RULES["initial_lives"], 0, 0
            self.collected.clear()
            self.respawn()
            return [("start", "player")]

        self.age += 1
        if self.respawn_pending:
            self.respawn_pending = False
            self.respawn()
            return [("respawn", "player")]

        events = []
        if keys["right"]:
            self.vx = WORLD["run_speed_m_s"] * SCALE
        else:
            self.vx = max(0, self.vx - WORLD["friction_m_s2"] * SCALE * DT)
        if keys["jump"] and self.grounded:
            self.vy = -WORLD["jump_impulse_m_s"] * SCALE
            events.append(("jump", "player"))
        previous_y = self.y
        self.vy += WORLD["gravity_m_s2"] * SCALE * DT
        self.x += self.vx * DT
        self.y += self.vy * DT
        tile = math.floor(self.x / TILE)
        solid = 0 <= tile < len(ROWS[-1]) and ROWS[-1][tile] == "#"
        self.grounded = solid and previous_y <= FLOOR <= self.y
        if self.grounded:
            self.y, self.vy = FLOOR, 0

        for name, x in PICKUPS.items():
            if name not in self.collected and self.touches(x, FLOOR - 7, RULES["pickup_radius_px"]):
                self.collected.add(name)
                self.score += RULES["pickup_points"]
                events.append(("pickup", name))
        hazard = None
        for kind, spawn in self.enemies.items():
            enemy = moving_enemy(kind, spawn, self.age)
            if self.touches(enemy["x_px"], enemy["y_px"], RULES["enemy_radius_px"]):
                hazard = kind
                break
        if self.y > HEIGHT:
            hazard = "pit"
        if hazard:
            self.lives -= RULES["damage_lives"]
            events.extend([("damage", hazard), ("death", "player")])
            if self.lives:
                self.respawn_pending = True
            else:
                self.screen = "game_over"
                events.append(("lose", "player"))
        elif self.x >= RULES["goal_x_px"]:
            self.screen = "win"
            events.append(("win", "beacon"))
        return events

    def touches(self, x, y, radius):
        return (abs(self.x - x) <= RULES["player_half_width_px"] + radius
                and self.y - RULES["player_height_px"] <= y + radius and y - radius <= self.y)

    def snapshot(self):
        level_width = len(ROWS[-1]) * TILE
        camera = min(max(round(self.x) - WIDTH // 2, 0), level_width - WIDTH)
        return {"screen": self.screen, "episode": self.episode, "episode_tick": self.age,
                "camera_x_px": camera, "camera_y_px": 0, "lives": self.lives,
                "score": self.score, "entities": self.entities()}
