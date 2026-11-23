"""Deterministic orthographic rig shared by the compiler and the Blender worker."""
from math import hypot


def world_matrix(direction, center, radius):
    length = hypot(*direction)
    x, y = (v / length for v in direction)
    return ((-y, 0.0, x, center[0] + radius*x),
            (x, 0.0, y, center[1] + radius*y),
            (0.0, 1.0, 0.0, center[2]),
            (0.0, 0.0, 0.0, 1.0))
