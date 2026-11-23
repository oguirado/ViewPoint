"""Synthetic analytic calibration parts only, never production asset generation.

A cell-boundary algorithm tessellates a box, optionally with a central
rectangular through-hole. The independent volume and tensor use closed-form
box subtraction, not the generated triangles. Dimensions are [TARGET] inputs.
"""
from pathlib import Path
import struct


def analytic_part(*, hole=False, offset_um=(0, 0, 0)):
    # Non-square section makes an inertia-axis swap observable.
    outer, bore = (100000, 50000, 20000), (20000, 10000, 20000)
    density = 1000
    volume = outer[0]*outer[1]*outer[2] - (bore[0]*bore[1]*bore[2] if hole else 0)
    def inertia(dim, axis):
        other = [dim[i] for i in range(3) if i != axis]
        # density * volume_um3 * length_um^2 * 1e-12 = kg m^2 * 1e18
        from fractions import Fraction
        return Fraction(density*dim[0]*dim[1]*dim[2]*(other[0]**2+other[1]**2), 12*10**12)
    diag = [round(inertia(outer, i)-(inertia(bore, i) if hole else 0)) for i in range(3)]
    policy = {'schema': 'af.cad-policy/v1',
              'expected_bbox_um': [[offset_um[i]-outer[i]//2 for i in range(3)],
                                   [offset_um[i]+outer[i]//2 for i in range(3)]],
              'expected_volume_um3': volume, 'expected_genus': int(hole),
              'expected_centre_um': list(offset_um),
              'expected_inertia_kg_m2_e18': [[diag[i] if i == j else 0 for j in range(3)] for i in range(3)],
              'dimension_tolerance_um': 10, 'volume_tolerance_ppm': 1,
              'moment_tolerance_ppm': 1}
    xs = [-50, -10, 10, 50] if hole else [-50, 50]
    ys = [-25, -5, 5, 25] if hole else [-25, 25]
    occupied = {(i, j) for i in range(len(xs)-1) for j in range(len(ys)-1)
                if not (hole and (i, j) == (1, 1))}
    triangles = []
    def quad(points):
        translated = [tuple(p[k]+offset_um[k]/1000 for k in range(3)) for p in points]
        triangles.extend([(translated[0], translated[1], translated[2]),
                          (translated[0], translated[2], translated[3])])
    for i, j in sorted(occupied):
        x, X, y, Y, z, Z = xs[i], xs[i+1], ys[j], ys[j+1], -10, 10
        quad([(x, y, Z), (X, y, Z), (X, Y, Z), (x, Y, Z)])
        quad([(x, Y, z), (X, Y, z), (X, y, z), (x, y, z)])
        if (i-1, j) not in occupied:
            quad([(x, y, z), (x, y, Z), (x, Y, Z), (x, Y, z)])
        if (i+1, j) not in occupied:
            quad([(X, Y, z), (X, Y, Z), (X, y, Z), (X, y, z)])
        if (i, j-1) not in occupied:
            quad([(X, y, z), (X, y, Z), (x, y, Z), (x, y, z)])
        if (i, j+1) not in occupied:
            quad([(x, Y, z), (x, Y, Z), (X, Y, Z), (X, Y, z)])
    return triangles, policy, density


def write_stl(path, triangles, *, scale=1):
    raw = bytearray(b'AF synthetic analytic fixture'.ljust(80, b'\0'))
    raw.extend(struct.pack('<I', len(triangles)))
    for triangle in triangles:
        coords = [v*scale for p in triangle for v in p]
        raw.extend(struct.pack('<12fH', 0., 0., 0., *coords, 0))
    Path(path).write_bytes(raw)
