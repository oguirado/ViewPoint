"""Bounded calibration precise part, not a generated production asset.

Closed-form box subtraction in tools.cad.fixtures is the independent oracle.
Units are explicit mm. Variants exist solely to qualify the rejection gates.
"""
from build123d import Box, Pos, Shell

scale = CAD_PARAMS['fixture_scale']
part = Box(100*scale, 50*scale, 20*scale)
if CAD_PARAMS['fixture_hole']:
    part = part - Box(20*scale, 10*scale, 40*scale)
if CAD_PARAMS['fixture_open']:
    part = Shell(list(part.faces())[:-1])
part = Pos(CAD_PARAMS['offset_x_um']/1000,
           CAD_PARAMS['offset_y_um']/1000,
           CAD_PARAMS['offset_z_um']/1000) * part
