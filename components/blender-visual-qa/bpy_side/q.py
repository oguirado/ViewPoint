"""Integer quantizers (R-B8): the only place floats become integers before leaving Blender."""
import math


def _finite(x):
    x = float(x)
    if not math.isfinite(x):
        raise ValueError("non-finite")
    return x


def _round(x, scale):
    return int(math.floor(_finite(x) * scale + 0.5))


# @anchor:bpy.q.mm
def mm(metres):
    return _round(metres, 1000)


# @anchor:bpy.q.um
def um(metres):
    return _round(metres, 1000000)


# @anchor:bpy.q.milli_deg
def milli_deg(radians):
    return _round(math.degrees(_finite(radians)), 1000)


# @anchor:bpy.q.ppm
def ppm(ratio):
    return _round(ratio, 1000000)


# @anchor:bpy.q.q4
def q4(pixels):
    return _round(pixels, 16)


# @anchor:bpy.q.i8_normal
def i8_normal(component):
    return max(-127, min(127, _round(component, 127)))
