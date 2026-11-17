"""S2-U1: integer, surface-anchored candidates, never sculpt acceptance.

Public API: propose(mesh, *, regions, seed, reference=None, policy=None).
Mesh coordinates are explicit local micrometres. Reference residuals require a
source-bound vertex-index correspondence with identical triangle topology and
coordinate convention. No screen-space residual or inferred registration exists.

Curvature is a uniform one-ring Laplacian proxy, not an exact curvature estimate.
Boundary candidates relax an observed rim towards its edge midpoint; they neither
fill holes nor claim topology repair. All methods and default sizes are design
choices, uncalibrated for appearance. The executor must freshly admit every stroke.
"""
import copy
import hashlib
from math import isqrt
from pathlib import Path
import time

from asset_factory import core

PPM = 1000000
# TARGET, bounded engineering defaults; no aesthetic or error-bound claim.
DEFAULT_POLICY = {"k": 8, "max_vertices": 1000000, "max_faces": 1000000,
                  "max_regions": 64, "max_region_faces": 8192, "timeout_ms": 30000,
                  "max_displacement_ppm": 10000, "radius_ppm": 20000,
                  "strength_ppm": 100000}


class ProposalError(core.ContractError):
    def __init__(self, rc, code, path="$"):
        super().__init__(code, path)
        self.rc = rc

    def as_dict(self):
        return {"rc": self.rc, "status": core.STATUS[self.rc],
                "reason_codes": [self.code], "path": self.path}


def _fail(code, path="$", rc=2):
    raise ProposalError(rc, code, path)


def _keys(obj, keys, path):
    if type(obj) is not dict or set(obj) != set(keys):
        _fail("schema_mismatch", path)


def _integer(value, lo, hi, path):
    if type(value) is not int or not lo <= value <= hi:
        _fail("integer_out_of_range", path)


def _tick(deadline):
    if time.monotonic_ns() >= deadline:
        _fail("deadline_exceeded", rc=6)


def _policy(policy):
    if policy is not None and (type(policy) is not dict or set(policy) - set(DEFAULT_POLICY)):
        _fail("policy_invalid")
    if type(policy) is dict and type(policy.get("k")) is int and policy["k"] > 8:
        _fail("candidate_budget_exceeded", rc=6)
    result = {**DEFAULT_POLICY, **({} if policy is None else policy)}
    for key, value in result.items():
        _integer(value, 1, DEFAULT_POLICY[key], "$/policy/"+key)
    return result


def _sub(a, b):
    return [x-y for x, y in zip(a, b)]


def _cross(a, b):
    return [a[1]*b[2]-a[2]*b[1], a[2]*b[0]-a[0]*b[2], a[0]*b[1]-a[1]*b[0]]


def _norm2(a):
    return sum(x*x for x in a)


def _div(a, b):
    """Nearest integer, ties away from zero, entirely in integer arithmetic."""
    return (1 if a >= 0 else -1) * ((abs(a)+b//2)//b)


def _mesh(mesh, policy, deadline):
    _keys(mesh, ("schema", "units", "frame", "vertices_um", "triangles"), "$/mesh")
    if (mesh["schema"], mesh["units"], mesh["frame"]) != ("af.sculpt-mesh/v1", "um", "mesh_local"):
        _fail("unsupported_mesh_convention")
    vertices, faces = mesh["vertices_um"], mesh["triangles"]
    for items, cap, name in ((vertices, policy["max_vertices"], "vertices"),
                             (faces, policy["max_faces"], "faces")):
        if type(items) is not list:
            _fail("schema_mismatch", "$/mesh/"+name)
        if not items:
            _fail("empty_mesh", rc=3)
        if len(items) > cap:
            _fail("mesh_budget_exceeded", rc=6)
    for i, vertex in enumerate(vertices):
        if i % 512 == 0:
            _tick(deadline)
        if type(vertex) is not list or len(vertex) != 3:
            _fail("schema_mismatch", "$/vertices_um")
        for value in vertex:
            _integer(value, -10**12, 10**12, "$/vertices_um")
    for i, face in enumerate(faces):
        if i % 512 == 0:
            _tick(deadline)
        if type(face) is not list or len(face) != 3:
            _fail("non_triangular_mesh")
        for index in face:
            _integer(index, 0, len(vertices)-1, "$/triangles")
        a, b, c = (vertices[index] for index in face)
        if not _norm2(_cross(_sub(b, a), _sub(c, a))):
            _fail("degenerate_triangle", "$/triangles/"+str(i))
    diagonal = isqrt(sum((max(v[axis] for v in vertices)-min(v[axis] for v in vertices))**2
                         for axis in range(3)))
    if not diagonal:
        _fail("zero_scale", rc=3)
    _tick(deadline)
    return diagonal


def mesh_digest(mesh):
    """Identity of the explicit ordered mesh, with core's canonical newline."""
    return core.sha256_json(mesh)


def _regions(regions, nfaces, policy):
    if type(regions) is not list:
        _fail("regions_invalid")
    if not regions:
        _fail("regions_unavailable", rc=3)
    if len(regions) > policy["max_regions"]:
        _fail("region_budget_exceeded", rc=6)
    result, seen = [], set()
    for region in regions:
        _keys(region, ("id", "faces"), "$/regions")
        name, faces = region["id"], region["faces"]
        if type(name) is not str or not name or len(name) > 128 or name in seen:
            _fail("region_id_invalid")
        if type(faces) is not list or not faces:
            _fail("empty_region", rc=3)
        if len(faces) > policy["max_region_faces"]:
            _fail("region_budget_exceeded", rc=6)
        for face in faces:
            _integer(face, 0, nfaces-1, "$/regions/faces")
        if len(set(faces)) != len(faces):
            _fail("duplicate_region_face")
        seen.add(name)
        result.append({"id": name, "faces": sorted(faces)})
    return sorted(result, key=lambda r: r["id"])


def _edges(face):
    a, b, c = face
    return [tuple(sorted((a, b))), tuple(sorted((b, c))), tuple(sorted((c, a)))]


def _topology(mesh, regions, deadline):
    """Global incidence for selected edges; region cuts cannot become boundaries."""
    faces, vertices = mesh["triangles"], mesh["vertices_um"]
    selected = {index for region in regions for index in region["faces"]}
    active = {v for i in selected for v in faces[i]}
    incidence = {edge: 0 for i in selected for edge in _edges(faces[i])}
    neighbors = {v: set() for v in active}
    for i, face in enumerate(faces):
        if i % 512 == 0:
            _tick(deadline)
        for a, b in _edges(face):
            edge = (a, b)
            if edge in incidence:
                incidence[edge] += 1
                if incidence[edge] > 2:
                    _fail("nonmanifold_region")
            if a in neighbors:
                neighbors[a].add(b)
            if b in neighbors:
                neighbors[b].add(a)
    laplacian = {}
    for v in sorted(neighbors):
        ring = sorted(neighbors[v])
        laplacian[v] = [_div(sum(vertices[j][axis] for j in ring), len(ring))-vertices[v][axis]
                        for axis in range(3)]
    return incidence, laplacian


def _draw(seed, region, serial, label):
    payload = core.canonical_json(["af.sculpt-propose-prng/v1", seed, region, serial, label])
    return int.from_bytes(hashlib.sha256(payload.encode("utf-8")).digest(), "big")


def _weights(seed, region, serial, edge=None, face=None):
    if edge is None:
        # Interior cut points leave every coordinate positive.
        a = 1 + _draw(seed, region, serial, "u") % (PPM-2)
        b = a + 1 + _draw(seed, region, serial, "v") % (PPM-a-1)
        return [a, b-a, PPM-b]
    t = 1 + _draw(seed, region, serial, "edge") % (PPM-1)
    return [t if v == edge[0] else PPM-t if v == edge[1] else 0 for v in face]


def _point(vectors, face, weights):
    return [_div(sum(vectors[v][axis]*w for v, w in zip(face, weights)), PPM)
            for axis in range(3)]


def _clamp(vector, limit):
    length2 = _norm2(vector)
    if length2 <= limit*limit:
        return vector
    length = isqrt(length2) + (isqrt(length2)**2 != length2)
    # Truncation toward zero guarantees the Euclidean displacement cap.
    return [(1 if v >= 0 else -1)*(abs(v)*limit//length) for v in vector]


def _effect(candidate):
    """Only executable effect: seed/id/hash/diagnostics cannot satisfy the gate."""
    return {key: candidate[key] for key in ("generator", "brush", "path", "delta_um",
                                            "radius_um", "strength_ppm", "pressure_ppm", "symmetry")}


def _toolchain():
    root = Path(__file__).parent
    files = {name: core.sha256_file(root/name) for name in
             ("__init__.py", "api.py", "__main__.py", "request.schema.json", "proposal.schema.json")}
    files["core.py"] = core.sha256_file(Path(core.__file__))
    return core.sha256_json(files)


def propose(mesh, *, regions, seed, reference=None, policy=None):
    """Return eight candidates per region, or explicit incomplete evidence.

    Malformed/stale/budget input raises ProposalError (rc 2/3/4/6). A missing
    reference returns rc 3 with measured curvature/boundary candidates if possible.
    No asset file is read or written, and no caller object is mutated.
    """
    p = _policy(policy)
    if p["k"] != 8:
        _fail("k_must_be_eight")
    _integer(seed, 0, 2**64-1, "$/seed")
    deadline = time.monotonic_ns() + p["timeout_ms"]*1000000
    diagonal = _mesh(mesh, p, deadline)
    rs = _regions(regions, len(mesh["triangles"]), p)
    source_hash = mesh_digest(mesh)
    ref_hash, ref_vectors = None, None
    reasons = []
    if reference is None:
        reasons.append("reference_unavailable")
    else:
        _keys(reference, ("schema", "source_mesh_sha256", "mapping", "mesh"), "$/reference")
        if reference["schema"] != "af.sculpt-reference/v1" or reference["mapping"] != "vertex_index":
            _fail("reference_mapping_unavailable", rc=3)
        if reference["source_mesh_sha256"] != source_hash:
            _fail("reference_stale", rc=4)
        _mesh(reference["mesh"], p, deadline)
        if (reference["mesh"]["triangles"] != mesh["triangles"] or
                len(reference["mesh"]["vertices_um"]) != len(mesh["vertices_um"])):
            _fail("reference_topology_mismatch", rc=3)
        ref_vectors = [_sub(a, b) for a, b in zip(reference["mesh"]["vertices_um"], mesh["vertices_um"])]
        ref_hash = mesh_digest(reference["mesh"])
    incidence, laplacian = _topology(mesh, rs, deadline)
    step = diagonal*p["max_displacement_ppm"]//PPM
    radius = diagonal*p["radius_ppm"]//PPM
    if not step or not radius:
        _fail("quantization_scale_unavailable", rc=3)
    rows = []
    for region in rs:
        _tick(deadline)
        faces = mesh["triangles"]
        pool = {"residual": [], "curvature": [], "boundary": []}
        for face_id in region["faces"]:
            face = faces[face_id]
            if ref_vectors is not None and any(_norm2(ref_vectors[v]) for v in face):
                pool["residual"].append((face_id, None))
            if any(_norm2(laplacian[v]) for v in face):
                pool["curvature"].append((face_id, None))
            for edge in _edges(face):
                if incidence[edge] == 1:
                    pool["boundary"].append((face_id, edge))
        available = [name for name in ("residual", "curvature", "boundary") if pool[name]]
        candidates, effects = [], set()
        # Bounded rejection for rounded no-ops or duplicate effects.
        for serial in range(8*64):
            _tick(deadline)
            if len(candidates) == 8 or not available:
                break
            kind = available[serial % len(available)]
            pool_index = _draw(seed, region["id"], serial, "face") % len(pool[kind])
            face_id, edge = pool[kind][pool_index]
            face = faces[face_id]
            weights = _weights(seed, region["id"], serial, edge, face)
            previous = _weights(seed, region["id"], serial+1024, edge, face)
            if kind == "boundary":
                midpoint = [_div(mesh["vertices_um"][edge[0]][axis]+mesh["vertices_um"][edge[1]][axis], 2)
                            for axis in range(3)]
                vector = _sub(midpoint, _point(mesh["vertices_um"], face, weights))
            else:
                vector = _point(ref_vectors if kind == "residual" else laplacian, face, weights)
            delta = _clamp(vector, step)
            if not _norm2(delta):
                continue
            candidate = {"schema": "af.sculpt-candidate/v1", "generator": kind,
                         "brush": {"residual": "displace", "curvature": "smooth", "boundary": "rim_relax"}[kind],
                         "mesh_sha256": source_hash, "reference_sha256": ref_hash if kind == "residual" else None,
                         "region_id": region["id"], "seed": seed, "symmetry": "none",
                         "path": [{"face": face_id, "barycentric_ppm": previous},
                                  {"face": face_id, "barycentric_ppm": weights}],
                         "delta_um": delta, "radius_um": radius,
                         "strength_ppm": p["strength_ppm"], "pressure_ppm": [PPM, PPM]}
            effect_hash = core.sha256_json(_effect(candidate))
            if effect_hash in effects:
                continue
            effects.add(effect_hash)
            candidate["id"] = core.sha256_json(candidate)
            candidates.append(candidate)
        local_reasons = list(reasons)
        if len(candidates) != 8:
            local_reasons.append("insufficient_nonzero_candidates")
        rows.append({"id": region["id"], "rc": 3 if local_reasons else 0,
                     "reason_codes": local_reasons,
                     "signals": {"residual_faces": None if reference is None else len(pool["residual"]),
                                 "curvature_faces": len(pool["curvature"]),
                                 "boundary_edges": len(pool["boundary"])}, "candidates": candidates})
    rc = core.worst_rc(row["rc"] for row in rows)
    effects = [{"id": row["id"], "effects": [_effect(c) for c in row["candidates"]]} for row in rows]
    result = {"schema": "af.sculpt-proposals/v1", "rc": rc, "status": core.STATUS[rc],
              "reason_codes": sorted({r for row in rows for r in row["reason_codes"]}),
              "mesh_sha256": source_hash, "reference_sha256": ref_hash, "seed": seed, "k": 8,
              "toolchain_sha256": _toolchain(), "policy_sha256": core.sha256_json(p),
              "max_step_um": step, "regions": rows, "candidates_sha256": core.sha256_json(effects)}
    _tick(deadline)
    return result


def _validate_result(result, mesh, regions, policy, diagonal, incidence, reference):
    """Independent witness checks; never trust a reported digest or check count."""
    if type(result) is not dict or result.get("rc") != 0 or result.get("status") != "OK":
        _fail("proposal_evidence_unavailable", rc=3)
    try:
        schema = core.load_json(Path(__file__).with_name("proposal.schema.json"), max_bytes=2**20)
        core.validate(result, schema)
    except core.ContractError:
        _fail("proposal_schema_invalid", rc=5)
    rows = result.get("regions")
    if type(rows) is not list or len(rows) != len(regions):
        _fail("candidate_coverage_invalid", rc=5)
    source_hash = mesh_digest(mesh)
    expected_step = diagonal*policy["max_displacement_ppm"]//PPM
    expected_radius = diagonal*policy["radius_ppm"]//PPM
    reference_hash = None if reference is None else mesh_digest(reference["mesh"])
    if (result["max_step_um"] != expected_step or result["mesh_sha256"] != source_hash
            or result["policy_sha256"] != core.sha256_json(policy)
            or result["toolchain_sha256"] != _toolchain()
            or result["reference_sha256"] != reference_hash):
        _fail("proposal_binding_invalid", rc=5)
    effects, ids = [], set()
    for row, region in zip(rows, regions):
        if row.get("id") != region["id"] or row.get("rc") != 0:
            _fail("region_binding_invalid", rc=5)
        candidates = row.get("candidates")
        if type(candidates) is not list or len(candidates) != 8:
            _fail("candidate_coverage_invalid", rc=5)
        witnesses = []
        for c in candidates:
            if (c.get("schema") != "af.sculpt-candidate/v1" or c.get("mesh_sha256") != source_hash
                    or c.get("region_id") != region["id"]):
                _fail("candidate_binding_invalid", rc=5)
            body = {k: v for k, v in c.items() if k != "id"}
            if c.get("id") != core.sha256_json(body) or c["id"] in ids:
                _fail("candidate_identity_invalid", rc=5)
            ids.add(c["id"])
            if c.get("generator") not in ("residual", "curvature", "boundary"):
                _fail("candidate_generator_invalid", rc=5)
            if (c["brush"] != {"residual": "displace", "curvature": "smooth", "boundary": "rim_relax"}[c["generator"]]
                    or c["reference_sha256"] != (reference_hash if c["generator"] == "residual" else None)
                    or c["radius_um"] != expected_radius or c["strength_ppm"] != policy["strength_ppm"]
                    or c["pressure_ppm"] != [PPM, PPM]):
                _fail("candidate_parameters_invalid", rc=5)
            if type(c.get("path")) is not list or len(c["path"]) != 2:
                _fail("candidate_path_invalid", rc=5)
            for anchor in c["path"]:
                face, weights = anchor.get("face"), anchor.get("barycentric_ppm")
                if type(face) is not int or face not in region["faces"]:
                    _fail("candidate_off_surface", rc=5)
                if (type(weights) is not list or len(weights) != 3 or
                        any(type(w) is not int or not 0 <= w <= PPM for w in weights) or sum(weights) != PPM):
                    _fail("candidate_off_surface", rc=5)
                if c["generator"] == "boundary":
                    support = [v for v, w in zip(mesh["triangles"][face], weights) if w]
                    if len(support) != 2 or incidence[tuple(sorted(support))] != 1:
                        _fail("candidate_not_on_boundary", rc=5)
            delta = c.get("delta_um")
            if (type(delta) is not list or len(delta) != 3 or any(type(v) is not int for v in delta)
                    or not 0 < _norm2(delta) <= expected_step**2):
                _fail("candidate_displacement_invalid", rc=5)
            witnesses.append(_effect(c))
        if len({core.sha256_json(e) for e in witnesses}) != 8:
            _fail("duplicate_candidate_effect", rc=5)
        effects.append({"id": row["id"], "effects": witnesses})
    digest = core.sha256_json(effects)
    if digest != result.get("candidates_sha256"):
        _fail("effect_digest_invalid", rc=5)
    return digest


def verify_generator(mesh, *, regions, reference=None, seed=17, other_seed=None, policy=None, generator=propose):
    """Metamorphic gate; the injected callable is solely a testing seam.

    Requires complete evidence; an ignored seed is rc 5 even if all reported
    IDs/digests/seed fields change. Fresh deep copies expose input mutation.
    """
    gate = {"schema": "af.sculpt-propose-gate/v1", "rc": 0, "status": "OK",
            "reason_codes": [], "checks_evaluated": 0, "effect_digests": []}
    try:
        p = _policy(policy)
        _integer(seed, 0, 2**64-1, "$/seed")
        if other_seed is None:
            other_seed = (seed+1) % 2**64
        _integer(other_seed, 0, 2**64-1, "$/other_seed")
        if seed == other_seed:
            _fail("distinct_gate_seeds_required")
        deadline = time.monotonic_ns()+p["timeout_ms"]*1000000
        diagonal = _mesh(mesh, p, deadline)
        normalized = _regions(regions, len(mesh["triangles"]), p)
        incidence, _ = _topology(mesh, normalized, deadline)
        outputs = []
        for selected_seed in (seed, seed, other_seed):
            args = copy.deepcopy({"mesh": mesh, "regions": regions, "reference": reference,
                                  "policy": policy, "seed": selected_seed})
            before = core.sha256_json(args)
            output = generator(**args)
            if before != core.sha256_json(args):
                _fail("caller_mutated", rc=5)
            gate["checks_evaluated"] += 1
            digest = _validate_result(output, mesh, normalized, p, diagonal, incidence, reference)
            gate["effect_digests"].append(digest)
            outputs.append(core.canonical_json(output))
            gate["checks_evaluated"] += 1
        if outputs[0] != outputs[1]:
            _fail("nondeterministic_candidates", rc=5)
        gate["checks_evaluated"] += 1
        if gate["effect_digests"][0] == gate["effect_digests"][2]:
            _fail("seed_ignored", rc=5)
        gate["checks_evaluated"] += 1
        _tick(deadline)
    except core.ContractError as exc:
        gate.update(rc=exc.rc, status=core.STATUS[exc.rc], reason_codes=[exc.code])
    except (KeyError, IndexError, TypeError, ValueError, AttributeError):
        gate.update(rc=5, status="FAIL", reason_codes=["malformed_generator_witness"])
    return gate
