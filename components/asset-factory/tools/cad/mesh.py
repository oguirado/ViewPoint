"""Independent binary-STL topology and surface-integral mass measurements.

No vertex welding, repair, axis normalisation or unit guessing is performed.
Internal floating-point integration is quantized to integer SI-scaled records.
These are properties of the tessellated surface, never exact CAD properties.
"""
from collections import defaultdict
import math
from pathlib import Path
import struct

from tools.cad.contracts import MAX_BYTES, MAX_TRIANGLES, Refusal, integer, require


def read_stl(path):
    with Path(path).open('rb') as stream:
        raw = stream.read(MAX_BYTES + 1)
    require(len(raw) <= MAX_BYTES, 6, 'mesh_budget_exceeded')
    require(len(raw) >= 84, 2, 'stl_invalid')
    count = struct.unpack_from('<I', raw, 80)[0]
    require(0 < count <= MAX_TRIANGLES, 6 if count > MAX_TRIANGLES else 3, 'triangle_budget_or_empty')
    require(len(raw) == 84 + count * 50, 2, 'stl_invalid')
    vertices, indices, faces = [], {}, []
    for offset in range(84, len(raw), 50):
        values = struct.unpack_from('<12fH', raw, offset)
        require(all(math.isfinite(v) for v in values[:12]), 2, 'stl_nonfinite')
        face = []
        for i in (3, 6, 9):
            point = tuple(values[i:i+3])
            if point not in indices:
                indices[point] = len(vertices)
                vertices.append(point)
            face.append(indices[point])
        require(len(set(face)) == 3, 5, 'degenerate_triangle')
        a, b, c = [vertices[i] for i in face]
        ab, ac = [b[i]-a[i] for i in range(3)], [c[i]-a[i] for i in range(3)]
        require(any(v != 0 for v in _cross(ab, ac)), 5, 'degenerate_triangle')
        faces.append(tuple(face))
    return vertices, faces


def _cross(a, b):
    return (a[1]*b[2]-a[2]*b[1], a[2]*b[0]-a[0]*b[2], a[0]*b[1]-a[1]*b[0])


def topology(vertices, faces):
    edges, links = defaultdict(list), defaultdict(list)
    unique = set()
    adjacency = defaultdict(set)
    for face in faces:
        key = tuple(sorted(face))
        require(key not in unique, 5, 'duplicate_triangle')
        unique.add(key)
        for a, b, c in (face, (face[1], face[2], face[0]), (face[2], face[0], face[1])):
            edges[min(a, b), max(a, b)].append(a < b)
            links[a].append((b, c))
            adjacency[a].add(b)
            adjacency[b].add(a)
    boundary = sum(len(v) == 1 for v in edges.values())
    nonmanifold = sum(len(v) != 2 for v in edges.values())
    require(boundary == 0 and nonmanifold == 0, 5, 'mesh_not_closed')
    require(all(v[0] != v[1] for v in edges.values()), 5, 'orientation_inconsistent')
    # Every vertex link must be a single cycle (edge counts alone miss pinches).
    for incident in links.values():
        graph = defaultdict(set)
        for a, b in incident:
            graph[a].add(b)
            graph[b].add(a)
        require(all(len(v) == 2 for v in graph.values()), 5, 'vertex_nonmanifold')
        seen, todo = set(), [next(iter(graph))]
        while todo:
            node = todo.pop()
            if node not in seen:
                seen.add(node)
                todo.extend(graph[node] - seen)
        require(len(seen) == len(graph), 5, 'vertex_nonmanifold')
    seen, components = set(), 0
    for vertex in range(len(vertices)):
        if vertex in seen:
            continue
        components += 1
        todo = [vertex]
        while todo:
            node = todo.pop()
            if node not in seen:
                seen.add(node)
                todo.extend(adjacency[node] - seen)
    require(components == 1, 5, 'multiple_solids_unsupported')
    chi = len(vertices) - len(edges) + len(faces)
    require(chi <= 2 and (2-chi) % 2 == 0, 5, 'topology_invalid')
    return {'vertices': len(vertices), 'triangles': len(faces), 'boundary_edges': boundary,
            'non_manifold_edges': nonmanifold, 'components': components, 'genus': (2-chi)//2}


def measure(path, *, density_kg_m3, units):
    require(units == 'mm', 2, 'units_unsupported')
    require(integer(density_kg_m3, 1), 2, 'density_invalid')
    vertices, faces = read_stl(path)
    inventory = topology(vertices, faces)
    bounds = [[min(p[i] for p in vertices) for i in range(3)],
              [max(p[i] for p in vertices) for i in range(3)]]
    origin = [(bounds[0][i]+bounds[1][i])/2 for i in range(3)]
    # SI metres, shifted near the shape to reduce cancellation far from origin.
    points = [tuple((p[i]-origin[i])*0.001 for i in range(3)) for p in vertices]
    terms = [[] for _ in range(10)]
    for face in faces:
        a, b, c = [points[i] for i in face]
        bc = _cross(b, c)
        volume = math.fsum(a[i]*bc[i] for i in range(3))/6
        terms[0].append(volume)
        for i in range(3):
            terms[1+i].append(volume*(a[i]+b[i]+c[i])/4)
            terms[4+i].append(volume*(a[i]**2+b[i]**2+c[i]**2
                                      +a[i]*b[i]+a[i]*c[i]+b[i]*c[i])/10)
        for k, (i, j) in enumerate(((0, 1), (0, 2), (1, 2))):
            terms[7+k].append(volume*((a[i]+b[i]+c[i])*(a[j]+b[j]+c[j])
                                      +a[i]*a[j]+b[i]*b[j]+c[i]*c[j])/20)
    values = [math.fsum(v) for v in terms]
    require(all(math.isfinite(v) for v in values), 2, 'moments_nonfinite')
    volume = values[0]
    require(volume > 0, 5, 'nonpositive_volume')
    centre = [v/volume for v in values[1:4]]
    second = [[0.0]*3 for _ in range(3)]
    for i in range(3):
        second[i][i] = values[4+i] - volume*centre[i]**2
    for k, (i, j) in enumerate(((0, 1), (0, 2), (1, 2))):
        second[i][j] = second[j][i] = values[7+k] - volume*centre[i]*centre[j]
    trace = sum(second[i][i] for i in range(3))
    inertia = [[density_kg_m3*((trace if i == j else 0)-second[i][j])
                for j in range(3)] for i in range(3)]
    result = {'schema': 'af.cad-mass/v1', 'units': 'SI',
              'method': 'oriented_stl_surface_integral', 'exact_brep_properties': False,
              'density_kg_m3': density_kg_m3, 'volume_um3': round(volume*10**18),
              'mass_ng': round(volume*density_kg_m3*10**12),
              'centre_um': [round((centre[i]+origin[i]*0.001)*10**6) for i in range(3)],
              'inertia_kg_m2_e18': [[round(v*10**18) for v in row] for row in inertia],
              'bbox_um': [[round(v*1000) for v in row] for row in bounds],
              'topology': inventory,
              'absence_reasons': {'brep_centre': 'exact_brep_api_unqualified',
                                  'brep_inertia': 'exact_brep_api_unqualified',
                                  'global_chord_error': 'no_independent_brep_distance_evaluator'}}
    physical_validate(result)
    return result


def physical_validate(mass):
    require(integer(mass['volume_um3'], 1) and integer(mass['mass_ng'], 1), 5, 'mass_nonpositive')
    tensor = mass['inertia_kg_m2_e18']
    require(all(type(row) is list and len(row) == 3
                and all(type(x) is int for x in row) for row in tensor)
            and len(tensor) == 3, 2, 'tensor_invalid')
    require(all(tensor[i][j] == tensor[j][i] for i in range(3) for j in range(3)), 5, 'tensor_asymmetric')
    a, b, c = tensor[0]
    _, d, e = tensor[1]
    _, _, f = tensor[2]
    require(a > 0 and a*d-b*b > 0 and a*d*f+2*b*c*e-a*e*e-d*c*c-f*b*b > 0,
            5, 'tensor_not_positive')
    # Physical triangle inequality in every direction: trace(I)/2 * 1 - I >= 0.
    # Integer-scaled PSD principal minors avoid a numerical eigenvalue tolerance.
    trace = a+d+f
    q = [[(trace if i == j else 0)-2*tensor[i][j] for j in range(3)] for i in range(3)]
    require(all(q[i][i] >= 0 for i in range(3)) and all(
        q[i][i]*q[j][j]-q[i][j]**2 >= 0 for i, j in ((0, 1), (0, 2), (1, 2)))
        and q[0][0]*q[1][1]*q[2][2]+2*q[0][1]*q[0][2]*q[1][2]
        -q[0][0]*q[1][2]**2-q[1][1]*q[0][2]**2-q[2][2]*q[0][1]**2 >= 0,
        5, 'tensor_triangle_inequality')
