# SPDX-License-Identifier: AGPL-3.0-only
"""Replay the published input tape using fixture data alone."""
import math


def enemy_at(spawn, age_s):
    # Fold odd legs of the round trip back toward the spawn point.
    leg, distance = divmod(age_s * spawn["speed_px_s"], spawn["travel_px"])
    outbound = int(leg) % 2 == 0
    if not outbound:
        distance = spawn["travel_px"] - distance
    if spawn["axis"] == "x":
        return spawn["x_px"] + distance, spawn["y_px"], "right" if outbound else "left"
    return spawn["x_px"], spawn["y_px"] + distance, "fall" if outbound else "rise"


def check_screen_physics(world, scale, screen):
    for world_key, screen_key in (("gravity_m_s2", "gravity_px_s2"),
                                  ("run_speed_m_s", "run_speed_px_s"),
                                  ("jump_impulse_m_s", "jump_impulse_px_s"),
                                  ("friction_m_s2", "friction_px_s2")):
        if not math.isclose(world[world_key] * scale, screen[screen_key], rel_tol=0, abs_tol=1e-9):
            raise ValueError(f"trajectory error: screen physics differs from world calibration: {screen_key}")


def check_trajectory(truth, inputs, tolerance_px=0.5):
    """Return the maximum position error, or raise for any replay disagreement."""
    physics, rules, level = truth["physics"], truth["rules"], truth["level"]
    dt = physics["fixed_step_ms"] / 1000
    scale, world = physics["pixels_per_metre"], physics["world"]
    check_screen_physics(world, scale, physics["screen"])
    gravity_step = world["gravity_m_s2"] * scale * dt**2
    floor, start = rules["floor_y_px"], rules["start_x_px"]
    speed = world["run_speed_m_s"] * scale * dt
    drag = world["friction_m_s2"] * scale * dt**2
    jump_step = -world["jump_impulse_m_s"] * scale * dt
    width, height = truth["video"]["width"], truth["video"]["height"]
    floor_cells = level["rows"][floor // level["tile_px"]]
    max_camera = len(floor_cells) * level["tile_px"] - width
    spawns, pickups = truth["enemy_spawns"], truth["pickup_centres_px"]
    x, y, dx = start, floor, 0
    takeoff, impulse, air_ticks = floor, 0, 0
    screen, grounded, pending = "title", True, False
    age, lives, score = 0, rules["initial_lives"], 0
    collected = set()
    maximum = None
    half_width, body_height = rules["player_half_width_px"], rules["player_height_px"]

    def contact(cx, cy, radius):
        return abs(x - cx) <= half_width + radius and y - body_height - radius <= cy <= y + radius

    for frame, row in zip(truth["frames"], inputs, strict=True):
        for keys in row["steps"]:
            if screen != "play":
                if keys["start"]:
                    screen, lives, age, score = "play", rules["initial_lives"], 0, 0
                    x, y, dx, grounded = start, floor, 0, True
                    collected.clear()
                continue
            age += 1
            if pending:
                x, y, dx, grounded, pending = start, floor, 0, True, False
                continue
            dx = speed if keys["right"] else max(0, dx - drag)
            x += dx
            if grounded:
                air_ticks, takeoff = 0, y
                impulse = jump_step if keys["jump"] else 0
            air_ticks += 1
            previous_y = y
            # Closed-form displacement of semi-implicit Euler.
            y = takeoff + impulse * air_ticks + gravity_step * air_ticks * (air_ticks + 1) / 2
            col = math.floor(x / level["tile_px"])
            grounded = 0 <= col < len(floor_cells) and floor_cells[col] == "#" and previous_y <= floor <= y
            if grounded:
                y = floor

            for actor, (px, py) in pickups.items():
                if actor not in collected and contact(px, py, rules["pickup_radius_px"]):
                    collected.add(actor)
                    score += rules["pickup_points"]
            if y > height or any(contact(*enemy_at(spawn, age * dt)[:2], rules["enemy_radius_px"])
                                 for spawn in spawns.values()):
                lives -= rules["damage_lives"]
                pending = bool(lives)
                if not pending:
                    screen = "game_over"
            elif x >= rules["goal_x_px"]:
                screen = "win"

        index = frame["frame_index"]
        if frame["screen"] != screen:
            raise ValueError(f"trajectory error at frame {index}: expected screen {screen}")
        if screen != "title":
            player_state = "run" if dx else "idle"
            if not grounded:
                player_state = "rise" if impulse + gravity_step * air_ticks < 0 else "fall"
            if pending or screen == "game_over":
                player_state = "dead"
            predicted = {"player": (x, y, player_state)}
            predicted.update((actor, (*pos, "collected" if actor in collected else "available"))
                             for actor, pos in pickups.items())
            predicted.update((actor, enemy_at(spawn, age * dt)) for actor, spawn in spawns.items())
            for actor, (px, py, state) in predicted.items():
                observed = frame["entities"][actor]
                error = math.dist((px, py), (observed["x_px"], observed["y_px"]))
                if not math.isfinite(error) or error > tolerance_px:
                    raise ValueError(f"trajectory error for {actor} at frame {index}: {error:.6f} px")
                if observed["state"] != state:
                    raise ValueError(f"trajectory error for {actor} at frame {index}: expected state {state}")
                maximum = error if maximum is None else max(maximum, error)
        for field, expected in (("camera_x_px", min(max(round(x) - width // 2, 0), max_camera)),
                                ("camera_y_px", 0), ("score", score), ("lives", lives)):
            if frame[field] != expected:
                raise ValueError(f"trajectory error at frame {index}: expected {field}={expected}")
    if maximum is None:
        raise ValueError("trajectory error: empty comparison")
    return maximum
