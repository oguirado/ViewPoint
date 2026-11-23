"""Exact UV predicates on active static GLB triangles and explicit atlas bytes.

A protected reader authorizes layout and sampler facts. Native results describe
the decoded geometry and declared sampling domain. BASE qualifies their use.
"""
import math
from fractions import Fraction

from asset_factory import core, domains
from tools.input.evidence import InputIssue, bytes_digest, checked_record, closed, is_digest, point, require
from tools.mesh.contracts import MeshError
from tools.mesh.raw_admission import _accessor, _active_meshes
from tools.mesh.worker import _document
from .bake_support import upstream


# DESIGN_CHOICE_UNCALIBRATED (CALIB): alignment with the projected UV
# derivative allows vertex averaging. The separate normal dot limit allows
# rounding of the exported normal and tangent components.
TANGENT_COS_FLOOR = Fraction(19, 20)
TANGENT_NORMAL_DOT_MAX = Fraction(1, 10000)


def native(gate, raw, pin):
    subject = bytes_digest(raw) if type(raw) is bytes else None
    return {"schema": "af.uv-evidence/v1", "gate_id": gate, "subject_digest": subject,
            "domain_digest": pin, "source_rc": 0, "reason_codes": [], "metrics": {},
            "input_refs": [subject] if subject else [], "reference_refs": [pin]}


def failure(receipt, error):
    if isinstance(error, (InputIssue, MeshError)):
        rc, reason = error.rc, error.code
    elif isinstance(error, MemoryError):
        rc, reason = 6, "uv_memory_budget"
    elif isinstance(error, OSError):
        rc, reason = 3, "uv_evidence_io_unavailable"
    else:
        rc, reason = 2, "uv_malformed_input"
    receipt.update(source_rc=rc, reason_codes=[reason])
    return receipt


def checked_bytes(reader, role, pin, length):
    require(is_digest(pin), 2, "invalid_" + role + "_pin")
    raw = reader(role, pin)
    require(raw is not None, 3, "missing_" + role)
    require(type(raw) is bytes and len(raw) == length, 2, "invalid_" + role + "_bytes")
    require(bytes_digest(raw) == pin, 4, "stale_" + role)
    return raw


def raster_size(value):
    w, h = domains.integers(value, 2)
    require(min(w, h) > 0, 2, "uv_raster_invalid")
    require(max(w, h) <= 4096, 6, "uv_raster_budget")
    return w, h


def load_triangles(raw, layout, *, mirrored=None):
    require(type(raw) is bytes, 2, "uv_glb_bytes_invalid")
    require(layout["mesh_digest"] == bytes_digest(raw) and layout["frame"] == domains.FRAME,
            4, "uv_mesh_or_frame_drift")
    for key in ("max_mesh_bytes", "max_faces", "max_work"):
        require(type(layout[key]) is int and layout[key] > 0, 2, "uv_invalid_work_budget")
    require(len(raw) <= layout["max_mesh_bytes"], 6, "uv_mesh_byte_budget")
    doc = _document(raw)
    size = raster_size(layout["size"])
    width, height = size
    _, binary = upstream("glb_core")._split(raw)
    faces, tangent_validity = [], {}
    for mesh_id in _active_meshes(doc):
        for prim in doc["meshes"][mesh_id]["primitives"]:
            attrs = prim["attributes"]
            uv_id = attrs.get("TEXCOORD_0")
            require(uv_id is not None, 3, "uv_missing")
            acc = doc["accessors"]
            require(acc[uv_id]["componentType"] == 5126 and not acc[uv_id].get("normalized"),
                    3, "uv_accessor_unsupported")
            count = acc[attrs["POSITION"]]["count"]
            index_id = prim.get("indices")
            corners = count if index_id is None else acc[index_id]["count"]
            require(corners > 0 and corners % 3 == 0, 2, "uv_triangle_count_invalid")
            require(len(faces) + corners // 3 <= layout["max_faces"]
                    and count <= 3 * layout["max_faces"], 6, "uv_face_or_vertex_budget")
            if index_id is None:
                indices = iter(range(count))
            else:
                code = {5121: "B", 5123: "H", 5125: "I"}[acc[index_id]["componentType"]]
                indices = (row[0] for row in _accessor(doc, binary, index_id, code))
            p = [(Fraction(x) * domains.UM_PER_METER, -Fraction(z) * domains.UM_PER_METER,
                  Fraction(y) * domains.UM_PER_METER)
                 for x, y, z in _accessor(doc, binary, attrs["POSITION"], "fff")]
            u = [(Fraction(x) * width, Fraction(y) * height)
                 for x, y in _accessor(doc, binary, uv_id, "ff")]
            tangents = normals = None
            tangent_id = attrs.get("TANGENT")
            if mirrored is not None and tangent_id is not None:
                accessor = acc[tangent_id]
                require(accessor["componentType"] == 5126 and accessor["type"] == "VEC4"
                        and accessor["count"] == count and not accessor.get("normalized"),
                        2, "uv_tangent_accessor_invalid")
                tangents = [
                    ((Fraction(x), -Fraction(z), Fraction(y)), w)
                    if all(math.isfinite(value) for value in (x, y, z, w)) else None
                    for x, y, z, w in _accessor(doc, binary, tangent_id, "ffff")
                ]
                normal_id = attrs.get("NORMAL")
                if normal_id is not None:
                    accessor = acc[normal_id]
                    require(accessor["componentType"] == 5126 and accessor["type"] == "VEC3"
                            and accessor["count"] == count and not accessor.get("normalized"),
                            2, "uv_tangent_normal_accessor_invalid")
                    normals = [
                        (Fraction(x), -Fraction(z), Fraction(y))
                        if all(math.isfinite(value) for value in (x, y, z)) else None
                        for x, y, z in _accessor(doc, binary, normal_id, "fff")
                    ]
            for tri in zip(indices, indices, indices):
                require(max(tri) < count, 2, "uv_index_invalid")
                xyz, uv = tuple(p[i] for i in tri), tuple(u[i] for i in tri)
                if mirrored is not None:
                    face_id = len(faces)
                    if tangents is not None:
                        corner_normals = tuple(normals[i] for i in tri) if normals is not None else None
                        tangent_validity[face_id] = _valid_face_tangents(
                            xyz, uv, (tangents[i] for i in tri), corner_normals)
                    elif face_id in mirrored:
                        tangent_validity[face_id] = None
                faces.append((xyz, uv))
    require(faces, 3, "uv_faces_missing")
    for xyz, uv in faces:
        require(all(0 <= x <= width and 0 <= y <= height for x, y in uv), 5, "uv_out_of_bounds")
        require(area2(uv) != 0, 5, "uv_collapsed")
        a, b = sub(xyz[1], xyz[0]), sub(xyz[2], xyz[0])
        require(any(cross(a, b)), 5, "uv_surface_degenerate")
    return size, faces, tangent_validity


def _valid_face_tangents(xyz, uv, corners, corner_normals):
    edge1, edge2 = sub(xyz[1], xyz[0]), sub(xyz[2], xyz[0])
    face_normal = cross(edge1, edge2)
    determinant = area2(uv)
    u1, v1 = sub(uv[1], uv[0])
    u2, v2 = sub(uv[2], uv[0])
    if determinant == 0 or not any(face_normal):
        return False
    # Keep only the signed derivative numerators. Blender stores UV V down
    # in glTF and retains its MikkTSpace sign for the increasing-V-up axis.
    sign = 1 if determinant > 0 else -1
    dpdu = tuple(sign * (a * v2 - b * v1) for a, b in zip(edge1, edge2))
    dpdv = tuple(sign * (a * u2 - b * u1) for a, b in zip(edge1, edge2))
    face_w = None
    for i, corner in enumerate(corners):
        normal = face_normal if corner_normals is None else corner_normals[i]
        if corner is None or normal is None:
            return False
        direction, w = corner
        normal_norm2, tangent_norm2 = dot(normal, normal), dot(direction, direction)
        if dot(direction, normal) ** 2 > TANGENT_NORMAL_DOT_MAX ** 2 * tangent_norm2 * normal_norm2:
            return False
        normal_part = dot(dpdu, normal)
        reference = tuple(normal_norm2 * d - normal_part * n for d, n in zip(dpdu, normal))
        alignment = dot(direction, reference)
        if alignment <= 0 or alignment ** 2 < TANGENT_COS_FLOOR ** 2 * tangent_norm2 * dot(reference, reference):
            return False
        handedness = dot(cross(normal, direction), dpdv)
        if handedness == 0 or w != (1 if handedness > 0 else -1) or (face_w is not None and w != face_w):
            return False
        face_w = w
    return True


def dot(a, b):
    return sum(x * y for x, y in zip(a, b))


def sub(a, b):
    return tuple(x - y for x, y in zip(a, b))


def cross(a, b):
    return tuple(a[i - 2] * b[i - 1] - a[i - 1] * b[i - 2] for i in range(3))


def cross2(a, b):
    return a[0] * b[1] - a[1] * b[0]


def area2(uv):
    return cross2(sub(uv[1], uv[0]), sub(uv[2], uv[0]))


def weights(uv, position):
    a, b = sub(uv[1], uv[0]), sub(uv[2], uv[0])
    det = cross2(a, b)
    p = sub(position, uv[0])
    v, w = cross2(p, b) / det, cross2(a, p) / det
    return 1 - v - w, v, w


def corners(points):
    x, y = zip(*points)
    return min(x), min(y), max(x), max(y)


def work_budget(limit):
    def charge(count):
        nonlocal limit
        limit -= count
        require(limit >= 0, 6, "uv_work_budget")
    return charge


def texel_faces(size, faces, charge):
    """Return atlas-center ownership. Coincident seam samples stay ambiguous."""
    width, height = size
    owners = {}
    half = Fraction(1, 2)
    for face, (_, uv) in enumerate(faces):
        l, t, r, b = corners(uv)
        left, top = max(0, math.ceil(l - half)), max(0, math.ceil(t - half))
        right, bottom = min(width - 1, math.floor(r - half)), min(height - 1, math.floor(b - half))
        charge(max(0, right - left + 1) * max(0, bottom - top + 1))
        for y in range(top, bottom + 1):
            for x in range(left, right + 1):
                bary = weights(uv, (x + half, y + half))
                if min(bary) >= 0:
                    owners.setdefault(y * width + x, []).append((face, bary))
    return owners


def overlaps(a, b):
    """Strict SAT intersection. Boundaries shared by UV triangles are allowed."""
    for poly in (a, b):
        for p, q in zip(poly, (*poly[1:], poly[0])):
            x, y = sub(q, p)
            axis = (-y, x)
            pa = [dot(point, axis) for point in a]
            pb = [dot(point, axis) for point in b]
            if max(pa) <= min(pb) or max(pb) <= min(pa):
                return False
    return True


def check_nonoverlap(faces, charge):
    ordered = sorted((corners(uv), i, uv) for i, (_, uv) in enumerate(faces))
    active = []
    for (left, top, right, bottom), _, uv in ordered:
        charge(len(active) + 1)
        active = [(bounds, tri) for bounds, tri in active if bounds[2] > left]
        for bounds, other in active:
            if top < bounds[3] and bounds[1] < bottom:
                require(not overlaps(uv, other), 5, "uv_overlap")
        active.append(((left, top, right, bottom), uv))


def sqrt_bounds(value, scale=domains.PPM):
    exact = Fraction(value) * scale ** 2
    low = math.isqrt(exact.numerator // exact.denominator)
    high = low if low ** 2 == exact else low + 1
    return Fraction(low, scale), Fraction(high, scale)


def interval(low, high):
    return [domains.pair(low), domains.pair(high)]


def geometric_metrics(faces, charts, orientation, stretch, density, density_factor, charge):
    k = stretch ** 2
    targets = (density ** 2 / density_factor ** 2, density ** 2 * density_factor ** 2)
    stretch_bounds, conformal_bounds, density_ratios, edges = [], [], [], {}
    for (xyz, texel), chart, sign in zip(faces, charts, orientation):
        area = area2(texel)
        require((area > 0) == (sign > 0), 5, "uv_flip")
        a, b = sub(xyz[1], xyz[0]), sub(xyz[2], xyz[0])
        c, e = sub(texel[1], texel[0]), sub(texel[2], texel[0])
        p, q, r = dot(a, a), dot(a, b), dot(b, b)
        det = p * r - q ** 2
        trace = (r * dot(c, c) + p * dot(e, e) - 2 * q * dot(c, e)) / det
        product = area ** 2 / det
        delta = trace ** 2 - 4 * product
        require(delta * (1 + k) ** 2 <= trace ** 2 * (k - 1) ** 2, 5, "uv_stretch_exceeded")
        require(targets[0] ** 2 <= product <= targets[1] ** 2, 5, "uv_density_exceeded")
        lo, hi = sqrt_bounds(delta / trace ** 2)
        stretch_low = max(1, (trace * (1 + lo)) ** 2 / (4 * product))
        stretch_high = (trace * (1 + hi)) ** 2 / (4 * product)
        stretch_bounds.append((stretch_low, stretch_high))
        k_lo, _ = sqrt_bounds(stretch_low)
        _, k_hi = sqrt_bounds(stretch_high)
        conformal_bounds.append((k_lo + 1 / k_lo - 2, k_hi + 1 / k_hi - 2))
        density_ratios.append(sqrt_bounds(product / density ** 4))
        for i, j in ((0, 1), (1, 2), (2, 0)):
            order = (i, j) if xyz[i] < xyz[j] else (j, i)
            edge = (xyz[order[0]], xyz[order[1]])
            edges.setdefault(edge, []).append((chart, texel[order[0]], texel[order[1]]))
    check_nonoverlap(faces, charge)
    seam_low = seam_high = 0
    for (a, b), uses in edges.items():
        if len(set(uses)) > 1:
            lo, hi = sqrt_bounds(dot(sub(a, b), sub(a, b)))
            seam_low += lo
            seam_high += hi
    return {
        "uv_k2_upper": interval(max(q[0] for q in stretch_bounds), max(q[1] for q in stretch_bounds)),
        "uv_conformal_error_ppm": interval(max(q[0] for q in conformal_bounds),
                                          max(q[1] for q in conformal_bounds)),
        "density_ratio_lower_ppm": interval(min(q[0] for q in density_ratios), min(q[1] for q in density_ratios)),
        "density_ratio_upper_ppm": interval(max(q[0] for q in density_ratios), max(q[1] for q in density_ratios)),
        "seam_length_upper_um": interval(seam_low, seam_high),
        "uv_flips": point(0), "uv_overlaps": point(0), "mirrored_faces": point(orientation.count(-1)),
    }


def _rgba(raw, index):
    return raw[4 * index:4 * index + 4]


def _check_footprint(faces, charts, width, height, factor, owners, charge):
    # A positive bilinear tap at x is supported by the open interval
    # (x - 1/2, x + 3/2) in pixel-edge coordinates, clamped at the image edge.
    half = Fraction(1, 2)
    for (_, uv), chart in zip(faces, charts):
        tri = tuple((u / factor, v / factor) for u, v in uv)
        l, t, r, b = corners(tri)
        x0, y0 = max(0, math.floor(l - half)), max(0, math.floor(t - half))
        x1, y1 = min(width, math.ceil(r + half)), min(height, math.ceil(b + half))
        charge((x1 - x0) * (y1 - y0))
        for y in range(y0, y1):
            for x in range(x0, x1):
                if owners.get(y * width + x) == chart:
                    continue
                a, c, b, d = x - half, y - half, x + 1 + half, y + 1 + half
                if overlaps(tri, ((a, c), (b, c), (b, d), (a, d))):
                    raise InputIssue(5, "uv_footprint_escape")


def _check_mips(size, faces, charts, layout, sampler, mips, coverage, charge):
    require(sampler == layout["sampler"], 5, "uv_sampler_drift")
    require(type(sampler) is dict and sampler.keys() == {"filter", "wrap", "mip", "level"}
            and sampler["filter"] == "bilinear" and sampler["wrap"] == "clamp-to-edge"
            and sampler["mip"] == "linear-box", 3, "uv_sampler_unsupported")
    width, height = size
    level = sampler["level"]
    require(type(level) is int and 0 <= level <= min(width, height).bit_length() - 1,
            2, "uv_mip_level_invalid")
    require(width % (1 << level) == height % (1 << level) == 0, 3, "uv_mip_dimensions_unsupported")
    require(type(mips) in (list, tuple) and len(mips) == level + 1, 3, "uv_mip_chain_missing")
    for n, mip in enumerate(mips):
        require(type(mip) is bytes and len(mip) == 4 * (width >> n) * (height >> n),
                5, "uv_mip_byte_length")
    owners = {}
    for i, hits in coverage.items():
        ids = {charts[face] for face, _ in hits}
        require(len(ids) == 1, 5, "uv_chart_texel_ambiguous")
        require(mips[0][4*i+3] > 0, 5, "uv_chart_unpopulated")
        owners[i] = next(iter(ids))
    gutters = layout["gutters"]
    require(type(gutters) is list, 2, "uv_gutters_invalid")
    charge(len(gutters))
    for row in gutters:
        i, source = domains.integers(row, 2)
        require(0 <= i < width * height and i not in owners and source in coverage,
                5, "uv_gutter_support_invalid")
        require(_rgba(mips[0], i) == _rgba(mips[0], source), 5, "uv_gutter_pixels_changed")
        owners[i] = owners[source]
    for n, mip in enumerate(mips):
        if n:
            previous_width = width
            width, height = width // 2, height // 2
            charge(width * height)
            parent = {}
            for y in range(height):
                for x in range(width):
                    start = 2 * (y * previous_width + x)
                    kids = (start, start + 1, start + previous_width, start + previous_width + 1)
                    chart = owners.get(start)
                    if chart is None or any(owners.get(child) != chart for child in kids):
                        continue
                    index = y * width + x
                    parent[index] = chart
                    expected = bytes((sum(mips[n - 1][4*j+c] for j in kids) + 2) // 4 for c in range(4))
                    require(_rgba(mip, index) == expected, 5, "uv_mip_pixels_changed")
            owners = parent
        _check_footprint(faces, charts, width, height, 1 << n, owners, charge)


def encoded_mip_byte_count(size, mips, *, block_width, block_height, block_bytes):
    """Check exact lengths for an explicitly declared block-compressed mip chain.

    The caller pins the codec and the subresource layout. Content/decoder
    identity and filtering evidence are separate from encoded length.
    """
    width, height = raster_size(size)
    require(all(type(n) is int and n > 0 for n in (block_width, block_height, block_bytes)),
            2, "uv_block_format_invalid")
    require(type(mips) in (list, tuple) and 0 < len(mips) <= max(width, height).bit_length(),
            2, "uv_encoded_mip_chain_invalid")
    total = 0
    for data in mips:
        x_blocks = (width + block_width - 1) // block_width
        y_blocks = (height + block_height - 1) // block_height
        expected = x_blocks * y_blocks * block_bytes
        require(type(data) is bytes and len(data) == expected, 5, "uv_encoded_mip_byte_length")
        total += expected
        width, height = max(1, width // 2), max(1, height // 2)
    return total


def inspect_atlas(raw, *, witness_digest, read_checked, sampler, mips):
    """Audit explicit linear RGBA8 mips, with chart-owned box sources and bilinear taps."""
    receipt = native("uv.atlas", raw, witness_digest)
    try:
        layout = checked_record(read_checked, "uv-layout", witness_digest)
        require("color_space" in layout, 3, "uv_color_space_missing")
        closed(layout, "af.uv-layout/v1", "mesh_digest", "frame", "size", "charts", "orientation",
               "color_space", "gutters", "sampler", "stretch", "density", "density_factor", "min_packing_ppm",
               "max_faces", "max_work", "max_mesh_bytes")
        color_space = layout["color_space"]
        require(color_space in domains.COLOR_SPACES, 2, "uv_color_space_invalid")
        receipt["color_space"] = color_space
        require(color_space == "linear", 3, "uv_color_space_unsupported")
        orientation = layout["orientation"]
        require(type(orientation) is list and all(type(sign) is int and sign in (-1, 1)
                                                 for sign in orientation), 2, "uv_chart_policy_invalid")
        mirrored = {i for i, sign in enumerate(orientation) if sign == -1}
        size, faces, tangent_validity = load_triangles(raw, layout, mirrored=mirrored)
        charts = domains.integers(layout["charts"], len(faces))
        orientation = domains.integers(layout["orientation"], len(faces))
        require(min(charts) > 0, 2, "uv_chart_policy_invalid")
        stretch, density, factor = (domains.rational(layout[k]) for k in ("stretch", "density", "density_factor"))
        packing = layout["min_packing_ppm"]
        require(stretch >= 1 and density > 0 and factor >= 1 and type(packing) is int
                and 0 <= packing <= domains.PPM, 2, "uv_quality_policy_invalid")
        charge = work_budget(layout["max_work"])
        receipt["metrics"].update(geometric_metrics(faces, charts, orientation, stretch, density, factor, charge))
        require(all(valid is not None for valid in tangent_validity.values()), 3, "uv_mirrored_tangent_missing")
        for face_id, valid in tangent_validity.items():
            reason = "uv_mirrored_tangent_invalid" if face_id in mirrored else "uv_tangent_invalid"
            require(valid, 5, reason)
        coverage = texel_faces(size, faces, charge)
        # Nonempty coverage for each required triangle prevents very thin, wholly
        # unsampled charts from disappearing under a whole-atlas packing average.
        require({face for hits in coverage.values() for face, _ in hits} == set(range(len(faces))),
                3, "uv_face_texel_support_missing")
        occupied = Fraction(len(coverage), size[0] * size[1])
        receipt["metrics"]["packing_lower_ppm"] = point(occupied)
        require(occupied * domains.PPM >= packing, 5, "uv_packing_below_limit")
        _check_mips(size, faces, charts, layout, sampler, mips, coverage, charge)
        receipt["metrics"]["footprint_escape_count"] = point(0)
        mip_digests = [bytes_digest(mip) for mip in mips]
        receipt["input_refs"].extend(mip_digests)
        receipt.update(mip_digests=mip_digests, texture_bytes=sum(map(len, mips)),
                       scope="explicit_linear_rgba8_mips")
        return receipt
    except (InputIssue, MeshError, core.ContractError, ValueError, TypeError, KeyError, IndexError,
            MemoryError, OSError, OverflowError, RecursionError) as error:
        return failure(receipt, error)
