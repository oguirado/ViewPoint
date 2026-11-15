"""In-Blender read-only scene oracles (bo.*, bvqa.oracle-sd/v1), BSPEC_BORACLES section 2.

blender -b --factory-startup -noaudio --python-exit-code 9 <scene.blend> --python bpy_side/scene_checks.py -- \
  --manifest M --journal J --thresholds catalogue/thresholds.tsv --out scene.jsonl [--baseline clean.jsonl]

The mesh kernel (mesh_kernel, uv_*, relative_sigma_fails) is pure stdlib so the host tests can call it without bpy.
Never saves the blend. Output rows are integers only; thresholds come ONLY from the tsv
(DEFAULTS lists the required ids and their expected values; a missing row is rc 2 E_SCHEMA, never a fallback).
"""
import collections
import csv
import json
import math
import os
import sys
sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from drive_common import DriveError, Parser, load_json, run
from q import um as q_um

SCHEMA = "bvqa.oracle-sd/v1"
PPM_ONE = 1000000
Q_ONE = 65536
Q_DOMAIN = 1 << 30
ZF_WORK_MAX = 4000000
DEFAULTS = {
    "BO_WIND_MAX": 0, "BO_DEGEN_N2": 7378, "BO_DEGEN_MAX": 0, "BO_UV_TV_PPM": 150000, "BO_UV_DEG_PPM": 10000,
    "BO_UV_SIGMA_MULT_Q16": 98304, "BO_ANISO": 100000, "BO_SCALE_MIN_UM": 100, "BO_SCALE_MAX_UM": 1000000000,
    "BO_MEDIAN_RATIO": 1000, "BO_SUBJ_MIN_PPM": 5000, "BO_ZF_EPS_UM": 10, "BO_ZF_COS_PPM": 999900,
    "BO_ZF_PAIRS_MAX": 10000, "BO_CLIP_FAIL_CORNERS": 8, "BO_KERNEL_TRIS_MAX": 250000, "BUDGET_TRIS_OBJECT": 200000, "BUDGET_TRIS_SCENE": 1000000,
}


def q16(x):
    return int(math.floor(float(x) * 65536 + 0.5))


def load_thresholds(path):
    if not path:
        raise DriveError("E_SCHEMA", "thresholds_path_missing")
    th = {}
    with open(path) as f:
        for r in csv.DictReader(f, delimiter="\t"):
            if r["id"] in DEFAULTS:
                th[r["id"]] = int(r["value"])
    missing = sorted(set(DEFAULTS) - set(th))
    if missing:
        raise DriveError("E_SCHEMA", "threshold_rows_missing:" + ",".join(missing))
    return th


# ---------------------------------------------------------------- pure kernel (C1-C5)
def _cross(a, b):
    return (a[1]*b[2] - a[2]*b[1], a[2]*b[0] - a[0]*b[2], a[0]*b[1] - a[1]*b[0])


def _dot(a, b):
    return a[0]*b[0] + a[1]*b[1] + a[2]*b[2]


def _sub(a, b):
    return (a[0]-b[0], a[1]-b[1], a[2]-b[2])


# @anchor:bpy.scene_checks.mesh_kernel
def mesh_kernel(P, polys, tris, edges, degen_n2):
    """P: q16 int triples; polys: vertex-index tuples (loop order); tris: index triples; edges: (a,b) pairs."""
    parent = list(range(len(P)))

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    used, dirs = set(), {}
    incident = collections.defaultdict(list)
    for fi, poly in enumerate(polys):
        n = len(poly)
        for i, v in enumerate(poly):
            used.add(v)
            ra, rb = find(poly[0]), find(v)
            if ra != rb:
                parent[max(ra, rb)] = min(ra, rb)
            w = poly[(i + 1) % n]
            incident[v].append((fi, poly[i - 1], w))
            if v != w:
                dirs.setdefault((min(v, w), max(v, w)), []).append(1 if v < w else 0)
    nm = sum(1 for d in dirs.values() if len(d) > 2)
    boundary = sum(1 for d in dirs.values() if len(d) == 1)
    conflict = sum(1 for d in dirs.values() if len(d) == 2 and d[0] == d[1])
    open_roots = {find(a) for (a, b), d in dirs.items() if len(d) != 2}
    sigma = collections.defaultdict(int)
    degen = 0
    for a, b, c in tris:
        sigma[find(a)] += _dot(P[a], _cross(P[b], P[c]))
        n = _cross(_sub(P[b], P[a]), _sub(P[c], P[a]))
        if _dot(n, n) <= degen_n2:
            degen += 1
    roots = {find(v) for v in used}
    inv = sum(1 for r in roots if r not in open_roots and sigma[r] <= 0)
    pinched = 0
    for v, inc in incident.items():
        if len(inc) < 2:
            continue
        comp = {fi: fi for fi, _, _ in inc}

        def cf(x):
            while comp[x] != x:
                comp[x] = comp[comp[x]]
                x = comp[x]
            return x
        by_nb, worst = {}, 0
        for fi, p, w in inc:
            for u in (p, w):
                if u == v:
                    continue
                key = (min(u, v), max(u, v))
                worst = max(worst, len(dirs.get(key, ())))
                if u in by_nb:
                    ra, rb = cf(fi), cf(by_nb[u])
                    if ra != rb:
                        comp[max(ra, rb)] = min(ra, rb)
                else:
                    by_nb[u] = fi
        if len({cf(fi) for fi, _, _ in inc}) > 1 and worst <= 2:
            pinched += 1
    in_edges = {v for e in edges for v in e}
    loose_v = sum(1 for v in range(len(P)) if v not in used and v not in in_edges)
    loose_e = sum(1 for a, b in edges if (min(a, b), max(a, b)) not in dirs)
    groups = collections.Counter(tuple(sorted(p)) for p in polys)
    return {"shells": len(roots), "inv_shells": inv, "conflict_edges": conflict, "nm_edges": nm,
            "pinched_verts": pinched, "boundary_edges": boundary, "loose_verts": loose_v, "loose_edges": loose_e,
            "degen_tris": degen, "degen_ppm": degen * PPM_ONE // len(tris) if tris else 0,
            "dup_faces": sum(c - 1 for c in groups.values() if c > 1), "tris": len(tris),
            "sigmas": sorted(sigma[r] for r in roots)}


def tri_area2_isqrt(a, b, c):
    n = _cross(_sub(b, a), _sub(c, a))
    return math.isqrt(_dot(n, n))


# @anchor:bpy.scene_checks.uv_stats
def uv_stats(P, uv_q, polys, loops):
    """P q16 3D per vertex; uv_q q16 (u,v) per loop index; loops: per-poly loop index lists. Returns metrics dict."""
    a3, auv = [], []
    for poly, li in zip(polys, loops):
        s3 = su = 0
        for k in range(1, len(poly) - 1):
            s3 += tri_area2_isqrt(P[poly[0]], P[poly[k]], P[poly[k + 1]])
            u0, u1, u2 = uv_q[li[0]], uv_q[li[k]], uv_q[li[k + 1]]
            su += abs((u1[0]-u0[0]) * (u2[1]-u0[1]) - (u1[1]-u0[1]) * (u2[0]-u0[0]))
        a3.append(s3)
        auv.append(su)
    T3, TU = sum(a3), sum(auv)
    nf = len(polys)
    if nf == 0 or TU == 0 or T3 == 0:
        return {"faces": nf, "tv_ppm": PPM_ONE, "degenerate_ppm": PPM_ONE if nf else 0, "sigma_q16": 0, "empty": 1}
    parent = {}

    def find(a):
        parent.setdefault(a, a)
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a
    for poly in polys:
        for v in poly:
            ra, rb = find(poly[0]), find(v)
            if ra != rb:
                parent[max(ra, rb)] = min(ra, rb)
    # tv per mesh shell (worst shell): UV density legitimately differs between shells of different size.
    shells = collections.defaultdict(list)
    for i, poly in enumerate(polys):
        shells[find(poly[0])].append(i)
    tv = 0
    for idx in shells.values():
        su, s3 = sum(auv[i] for i in idx), sum(a3[i] for i in idx)
        if su == 0 or s3 == 0:
            tv = PPM_ONE
            continue
        tv = max(tv, sum(abs(auv[i] * PPM_ONE // su - a3[i] * PPM_ONE // s3) for i in idx) // 2)
    deg = sum(1 for u, t in zip(auv, a3) if u == 0 and t > 0) * PPM_ONE // nf
    sh = [u * 65536 // TU for u in auv]
    return {"faces": nf, "tv_ppm": tv, "degenerate_ppm": deg, "sigma_q16": sigma_q16(sh), "empty": 0}


def sigma_q16(shares):
    n = len(shares)
    if n == 0:
        return 0
    return math.isqrt(n * sum(s * s for s in shares) - sum(shares) ** 2) // n


def relative_sigma_fails(sigma, sigma_base, mult_q16, empty=False):
    """VG-0252 form: FAIL iff sigma*65536 > sigma_base*mult. An empty list FAILs."""
    return bool(empty) or sigma * Q_ONE > sigma_base * mult_q16


# ---------------------------------------------------------------- row helpers
class Rows:
    def __init__(self):
        self.rows = []

    def add(self, oid, obj, frame, metrics, thr, verdict, reason=""):
        self.rows.append({"schema": SCHEMA, "check_id": oid, "oracle_id": oid, "object": obj, "frame": frame,
                          "metrics": {k: int(v) for k, v in metrics.items()},
                          "thresholds": {k: int(v) for k, v in thr.items()}, "verdict": verdict, "reason": reason})

    def sd(self, oid, obj, frame, metrics, thr, bad, reason=""):
        self.add(oid, obj, frame, metrics, thr, "FAIL" if bad else "PASS", reason if bad else "")

    def cannot(self, oid, obj, reason, frame=None):
        self.add(oid, obj, frame, {}, {}, "CANNOT_SEE", reason)

    def final(self):
        rows = sorted(self.rows, key=lambda r: (r["oracle_id"], r["object"] or "", -1 if r["frame"] is None else r["frame"]))
        s = collections.Counter(r["verdict"] for r in rows)
        trailer = {"schema": SCHEMA, "summary": {"rows": len(rows), "fail": s["FAIL"], "cannot_see_count": s["CANNOT_SEE"],
                                                 "advisory": s["ADVISORY"]}}
        return rows + [trailer]


def obj_tags(ob):
    return set(t for t in str(ob.get("bvqa_tags", "")).split("|") if t)


# ---------------------------------------------------------------- Blender side
def eval_mesh(bpy, dg, ob):
    ev = ob.evaluated_get(dg)
    me = ev.to_mesh()
    me.calc_loop_triangles()
    return ev, me


def tree_nodes(bpy, tree, seen=None):
    """All nodes of a tree, recursing node groups (cycle guard by tree name)."""
    seen = set() if seen is None else seen
    if tree is None or tree.name in seen:
        return
    seen.add(tree.name)
    for n in tree.nodes:
        yield n
        if n.bl_idname == "ShaderNodeGroup":
            yield from tree_nodes(bpy, n.node_tree, seen)


def used_materials(ob):
    return [s.material for s in ob.material_slots if s.material is not None]


def image_nodes(bpy, ob):
    out = []
    for m in used_materials(ob):
        if m.use_nodes:
            out += [n for n in tree_nodes(bpy, m.node_tree) if n.bl_idname in ("ShaderNodeTexImage", "ShaderNodeTexEnvironment")]
    return out


def image_missing(bpy, img):
    if img is None or img.source != "FILE" or img.packed_file:
        return False
    return not os.path.exists(bpy.path.abspath(img.filepath, library=img.library))


def check_meshes(bpy, scene, dg, objs, th, rows, uv_base, baseline):
    cap = th["BO_KERNEL_TRIS_MAX"]
    for ob in objs:
        tags = obj_tags(ob)
        ev, me = eval_mesh(bpy, dg, ob)
        try:
            P = [(q16(v.co.x), q16(v.co.y), q16(v.co.z)) for v in me.vertices]
            polys = [tuple(p.vertices) for p in me.polygons]
            tris = [tuple(t.vertices) for t in me.loop_triangles]
            edges = [tuple(e.vertices) for e in me.edges]
            n_tris = len(tris)
            oob = any(abs(c) > Q_DOMAIN for p in P for c in p)
            ids = ("bo.mesh.inverted_normals", "bo.mesh.non_manifold", "bo.mesh.degenerate", "bo.mesh.duplicate_faces")
            if oob or n_tris > cap:
                for oid in ids:
                    rows.cannot(oid, ob.name, "q_domain" if oob else "kernel_cap")
            else:
                m = mesh_kernel(P, polys, tris, edges, th["BO_DEGEN_N2"])
                neg = int(ev.matrix_world.determinant() < 0)
                closed, opened = "closed" in tags, "open" in tags
                mi = {"shells": m["shells"], "inv_shells": m["inv_shells"], "conflict_edges": m["conflict_edges"], "neg_det": neg}
                ti = {"BO_WIND_MAX": th["BO_WIND_MAX"]}
                if m["conflict_edges"] > th["BO_WIND_MAX"] or (closed and m["inv_shells"] > 0):
                    rows.add(ids[0], ob.name, None, mi, ti, "FAIL", "inverted" if m["inv_shells"] else "winding_conflict")
                elif closed or opened:
                    rows.add(ids[0], ob.name, None, mi, ti, "PASS")
                else:
                    rows.cannot(ids[0], ob.name, "untagged")
                mn = {k: m[k] for k in ("nm_edges", "pinched_verts", "boundary_edges", "loose_verts", "loose_edges")}
                defect = m["nm_edges"] + m["pinched_verts"] + m["loose_verts"] + m["loose_edges"] > 0
                if defect or (closed and m["boundary_edges"] > 0):
                    rows.add(ids[1], ob.name, None, mn, {}, "FAIL", "non_manifold")
                elif closed or opened or m["boundary_edges"] == 0:
                    rows.add(ids[1], ob.name, None, mn, {}, "PASS")
                else:
                    rows.cannot(ids[1], ob.name, "untagged")
                rows.sd(ids[2], ob.name, None, {"degen_tris": m["degen_tris"], "degen_ppm": m["degen_ppm"]},
                        {"BO_DEGEN_N2": th["BO_DEGEN_N2"], "BO_DEGEN_MAX": th["BO_DEGEN_MAX"]},
                        m["degen_tris"] > th["BO_DEGEN_MAX"], "degenerate_tris")
                rows.sd(ids[3], ob.name, None, {"dup_faces": m["dup_faces"]}, {}, m["dup_faces"] > 0, "duplicate_faces")
            # UV
            if len(me.uv_layers) > 0 and not oob and ob.name in uv_base:
                uv = me.uv_layers[0].data
                uvq = [(q16(l.uv[0]), q16(l.uv[1])) for l in uv]
                loops = [list(range(p.loop_start, p.loop_start + p.loop_total)) for p in me.polygons]
                s = uv_stats(P, uvq, polys, loops)
                bad = s["tv_ppm"] > th["BO_UV_TV_PPM"] or s["degenerate_ppm"] > th["BO_UV_DEG_PPM"]
                reason = "uv_absolute" if bad else ""
                met = {"tv_ppm": s["tv_ppm"], "degenerate_ppm": s["degenerate_ppm"], "sigma_q16": s["sigma_q16"]}
                thr = {"BO_UV_TV_PPM": th["BO_UV_TV_PPM"], "BO_UV_DEG_PPM": th["BO_UV_DEG_PPM"]}
                if baseline is not None:
                    base = baseline.get(("bo.uv.stretch", ob.name))
                    if base is not None:
                        thr["BO_UV_SIGMA_MULT_Q16"] = th["BO_UV_SIGMA_MULT_Q16"]
                        met["sigma_base_q16"] = base["metrics"].get("sigma_q16", 0)
                        if relative_sigma_fails(s["sigma_q16"], met["sigma_base_q16"], th["BO_UV_SIGMA_MULT_Q16"], s["empty"]):
                            bad, reason = True, "uv_relative"
                rows.sd("bo.uv.stretch", ob.name, None, met, thr, bad, reason)
        finally:
            ev.to_mesh_clear()


def check_materials(bpy, objs, rows):
    for ob in objs:
        me = ob.data
        slots = ob.material_slots
        empty = sum(1 for p in me.polygons if p.material_index >= len(slots) or slots[p.material_index].material is None) \
            if len(slots) > 0 else 0
        rows.sd("bo.mat.missing", ob.name, None, {"empty_slots_used": empty, "no_slots": int(len(slots) == 0)}, {},
                empty > 0 or len(slots) == 0, "no_slots" if len(slots) == 0 else "empty_slot")
        nodes = image_nodes(bpy, ob)
        miss = sum(1 for n in nodes if image_missing(bpy, n.image))
        rows.sd("bo.tex.missing", ob.name, None, {"missing_images": miss, "image_nodes": len(nodes)}, {}, miss > 0, "missing_image")
        unlinked = sum(1 for n in nodes if "Vector" in n.inputs and not n.inputs["Vector"].is_linked)
        if nodes:
            nouv = unlinked if len(me.uv_layers) == 0 else 0
            rows.sd("bo.uv.missing", ob.name, None, {"img_nodes_without_uv": nouv}, {}, nouv > 0, "no_uv_layer")


def world_bbox_corners(ev):
    from mathutils import Vector
    return [ev.matrix_world @ Vector(c) for c in ev.bound_box]


def check_scale(bpy, dg, objs, th, rows):
    diag = {}
    for ob in objs:
        ev = ob.evaluated_get(dg)
        cs = world_bbox_corners(ev)
        lo = [min(c[i] for c in cs) for i in range(3)]
        hi = [max(c[i] for c in cs) for i in range(3)]
        diag[ob.name] = q_um(math.dist(lo, hi))
    ordered = sorted(d for d in diag.values() if d > 0)
    median = ordered[(len(ordered) - 1) // 2] if ordered else 0
    for ob in objs:
        ev = ob.evaluated_get(dg)
        sc = [q_um(abs(s)) for s in ev.matrix_world.to_scale()]
        smin, smax = min(sc), max(sc)
        aniso = (smax - smin) * PPM_ONE // smax if smax > 0 else 0
        d = diag[ob.name]
        r = th["BO_MEDIAN_RATIO"]
        out = median > 0 and d > 0 and (d >= median * r or d * r <= median)
        bad = smin < th["BO_SCALE_MIN_UM"] or smax >= th["BO_SCALE_MAX_UM"] or aniso > th["BO_ANISO"] or out
        rows.sd("bo.scale.anomaly", ob.name, None, {"aniso_ppm": aniso, "diag_um": d, "median_diag_um": median, "scale_min_um": smin,
                                                   "scale_max_um": smax},
                {"BO_ANISO": th["BO_ANISO"], "BO_SCALE_MIN_UM": th["BO_SCALE_MIN_UM"], "BO_SCALE_MAX_UM": th["BO_SCALE_MAX_UM"],
                 "BO_MEDIAN_RATIO": r}, bad, "scale_anomaly")


def check_budget(bpy, dg, objs, budgets, th, rows):
    per_obj = budgets.get("tris_object", th["BUDGET_TRIS_OBJECT"])
    per_scene = budgets.get("tris_scene", th["BUDGET_TRIS_SCENE"])
    total = 0
    for ob in objs:
        ev, me = eval_mesh(bpy, dg, ob)
        n = len(me.loop_triangles)
        ev.to_mesh_clear()
        total += n
        rows.sd("bo.poly.budget", ob.name, None, {"tris_obj": n}, {"tris_object": per_obj}, n > per_obj, "object_budget")
    rows.sd("bo.poly.budget", None, None, {"tris_scene": total}, {"tris_scene": per_scene}, total > per_scene, "scene_budget")


def lit_sources(bpy, scene, objs):
    n = sum(1 for o in scene.objects if o.type == "LIGHT" and o.data.energy > 0 and not o.hide_render)
    w = scene.world
    if w is not None:
        if w.use_nodes and w.node_tree:
            for node in tree_nodes(bpy, w.node_tree):
                if node.bl_idname == "ShaderNodeBackground":
                    st, col = node.inputs["Strength"], node.inputs["Color"]
                    if st.default_value > 0 and (col.is_linked or max(col.default_value[:3]) > 0):
                        n += 1
        elif max(w.color[:3]) > 0:
            n += 1
    seen = set()
    for ob in objs:
        for m in used_materials(ob):
            if m.name in seen or not (m.use_nodes and m.node_tree):
                continue
            seen.add(m.name)
            for node in tree_nodes(bpy, m.node_tree):
                if node.bl_idname == "ShaderNodeEmission" and node.inputs["Strength"].default_value > 0 \
                        and max(node.inputs["Color"].default_value[:3]) > 0:
                    n += 1
    return n


def excluded_collections(layer):
    out = set()

    def walk(lc, hidden):
        h = hidden or lc.exclude
        if h:
            out.add(lc.collection.name)
        for ch in lc.children:
            walk(ch, h)
    walk(layer.layer_collection, False)
    return out


def check_frames(bpy, scene, frames, subjects, anim_objs, th, rows):
    from bpy_extras.object_utils import world_to_camera_view
    cam = scene.camera
    seen_prev = {}
    moving = collections.Counter()
    if not subjects:
        rows.cannot("bo.cam.framing", None, "no_subject_tag")
        rows.cannot("bo.cam.clipping", None, "no_subject_tag")
    for fi, frame in enumerate(frames):
        scene.frame_set(frame)
        dg = bpy.context.evaluated_depsgraph_get()
        evc = cam.evaluated_get(dg)
        start, end = q_um(cam.data.clip_start), q_um(cam.data.clip_end)
        for ob in subjects:
            ev = ob.evaluated_get(dg)
            pts = [world_to_camera_view(scene, evc, c) for c in world_bbox_corners(ev)]
            xy = [(ppm_clip(p.x), ppm_clip(p.y)) for p in pts if p.z > 0]
            inside = sum(1 for p in pts if p.z > 0 and 0 <= p.x <= 1 and 0 <= p.y <= 1)
            if xy:
                area = (max(x for x, _ in xy) - min(x for x, _ in xy)) * (max(y for _, y in xy) - min(y for _, y in xy)) // PPM_ONE
            else:
                area = 0
            bad = inside == 0 or area < th["BO_SUBJ_MIN_PPM"]
            rows.sd("bo.cam.framing", ob.name, frame, {"in_view_corners": inside, "area_ppm": area},
                    {"BO_SUBJ_MIN_PPM": th["BO_SUBJ_MIN_PPM"]}, bad, "subject_out_of_frame")
            clipped = sum(1 for p in pts if q_um(p.z) < start or q_um(p.z) > end)
            met, thr = {"clipped_corners": clipped}, {"clip_start_um": start, "clip_end_um": end}
            if clipped >= th["BO_CLIP_FAIL_CORNERS"]:
                rows.add("bo.cam.clipping", ob.name, frame, met, thr, "FAIL", "subject_clipped")
            elif clipped > 0:
                rows.add("bo.cam.clipping", ob.name, frame, met, thr, "ADVISORY", "partially_clipped")
            else:
                rows.add("bo.cam.clipping", ob.name, frame, met, thr, "PASS")
        for ob in anim_objs:
            ev = ob.evaluated_get(dg)
            mat = tuple(q16(ev.matrix_world[r][c]) for r in range(4) for c in range(4))
            if ob.name in seen_prev and seen_prev[ob.name] != mat:
                moving[ob.name] += 1
            seen_prev[ob.name] = mat
    for ob in anim_objs:
        tags = obj_tags(ob)
        mv = moving[ob.name]
        bad = ("animated" in tags and mv == 0) or ("static" in tags and mv > 0)
        rows.sd("bo.anim.static", ob.name, None, {"moving_frames": mv, "frames": len(frames)}, {}, bad,
                "not_moving" if mv == 0 else "moving_static")


def ppm_clip(x):
    return max(0, min(PPM_ONE, int(math.floor(x * PPM_ONE + 0.5))))


def check_visibility(bpy, scene, layer, subjects, rows):
    excl = excluded_collections(layer)
    if not subjects:
        rows.cannot("bo.render.visibility", None, "no_subject_tag")
    for ob in subjects:
        cols = list(ob.users_collection)
        met = {"hide_render": int(ob.hide_render), "not_visible_camera": int(not ob.visible_camera),
               "excluded": int(any(c.name in excl for c in cols)), "collection_hide_render": int(any(c.hide_render for c in cols))}
        rows.sd("bo.render.visibility", ob.name, None, met, {}, sum(met.values()) > 0, "subject_hidden")


def tri_overlap_2d(A, B):
    """Interior overlap of two int triangles (SAT over edge normals; touching is separation)."""
    for T in (A, B):
        for i in range(3):
            p, q = T[i], T[(i + 1) % 3]
            ax, ay = q[1] - p[1], p[0] - q[0]
            pa = [ax * v[0] + ay * v[1] for v in A]
            pb = [ax * v[0] + ay * v[1] for v in B]
            if max(pa) <= min(pb) or max(pb) <= min(pa):
                return False
    return True


# @anchor:bpy.scene_checks.zfight
def check_zfight(bpy, dg, objs, th, rows):
    """Coplanar overlapping triangles of different objects. Own AABB broad phase (BVHTree.overlap ignores
    parallel faces a few um apart), then q16 cos, float plane distance quantised to um, exact int 2D overlap."""
    eps = th["BO_ZF_EPS_UM"] / PPM_ONE
    data = []
    for ob in objs:
        ev, me = eval_mesh(bpy, dg, ob)
        mw = ev.matrix_world
        vs = [mw @ v.co for v in me.vertices]
        tr = [tuple(t.vertices) for t in me.loop_triangles]
        ev.to_mesh_clear()
        if not tr:
            continue
        tris = []
        for t in tr:
            p = [vs[k] for k in t]
            q = [(q16(c.x), q16(c.y), q16(c.z)) for c in p]
            n = _cross(_sub(q[1], q[0]), _sub(q[2], q[0]))
            box = [(min(c[i] for c in p) - eps, max(c[i] for c in p) + eps) for i in range(3)]
            tris.append((p, q, n, box))
        lo = [min(v[i] for v in vs) for i in range(3)]
        hi = [max(v[i] for v in vs) for i in range(3)]
        data.append((ob, tris, lo, hi))
    pairs = work = 0
    for i in range(len(data)):
        for j in range(i + 1, len(data)):
            a, b = data[i], data[j]
            if any(a[2][k] > b[3][k] + eps or b[2][k] > a[3][k] + eps for k in range(3)):
                continue
            pairs += 1
            work += len(a[1]) * len(b[1])
            if pairs > th["BO_ZF_PAIRS_MAX"] or work > ZF_WORK_MAX:
                rows.cannot("bo.geom.zfight", None, "zfight_pairs_cap")
                return
    done = 0
    for i in range(len(data)):
        for j in range(i + 1, len(data)):
            a, b = data[i], data[j]
            if any(a[2][k] > b[3][k] + eps or b[2][k] > a[3][k] + eps for k in range(3)):
                continue
            cop = 0
            for pa, qa, na, ba in a[1]:
                if na == (0, 0, 0):
                    continue
                axis = max(range(3), key=lambda k: abs(na[k]))
                keep = [k for k in range(3) if k != axis]
                nrm = (pa[1] - pa[0]).cross(pa[2] - pa[0]).normalized()
                for pb, qb, nb, bb in b[1]:
                    if any(ba[k][0] > bb[k][1] or bb[k][0] > ba[k][1] for k in range(3)):
                        continue
                    den = math.isqrt(_dot(na, na) * _dot(nb, nb))
                    if den == 0 or abs(_dot(na, nb)) * PPM_ONE // den < th["BO_ZF_COS_PPM"]:
                        continue
                    if max(q_um(abs(nrm.dot(p - pa[0]))) for p in pb) >= th["BO_ZF_EPS_UM"]:
                        continue
                    if tri_overlap_2d([(c[keep[0]], c[keep[1]]) for c in qa], [(c[keep[0]], c[keep[1]]) for c in qb]):
                        cop += 1
            decal = "decal" in obj_tags(a[0]) and "decal" in obj_tags(b[0])
            done += 1
            rows.sd("bo.geom.zfight", a[0].name + "|" + b[0].name, None, {"coplanar_pairs": cop},
                    {"BO_ZF_EPS_UM": th["BO_ZF_EPS_UM"], "BO_ZF_COS_PPM": th["BO_ZF_COS_PPM"]}, cop > 0 and not decal, "coplanar_overlap")
    if done == 0:
        rows.sd("bo.geom.zfight", None, None, {"coplanar_pairs": 0}, {}, False)


def read_baseline(path):
    if not path:
        return None
    out = {}
    with open(path) as f:
        for line in f:
            r = json.loads(line)
            if "oracle_id" in r:
                out[(r["oracle_id"], r["object"])] = r
    return out


def entry():
    import bpy
    p = Parser()
    for k in ("--manifest", "--journal", "--out"):
        p.add_argument(k, required=True)
    p.add_argument("--thresholds", default="")
    p.add_argument("--baseline", default="")
    args = p.parse_args(sys.argv[sys.argv.index("--") + 1:])
    manifest, journal = load_json(args.manifest), load_json(args.journal)
    th = load_thresholds(args.thresholds)
    baseline = read_baseline(args.baseline)
    scene = bpy.data.scenes.get(journal["scene"])
    if scene is None or scene.camera is None:
        raise DriveError("E_SCENE", "no_camera")
    layer = scene.view_layers.get(journal["view_layer"]) or scene.view_layers[0]
    fl = journal.get("frames", {})
    frames = list(fl["list"]) if "list" in fl else list(range(scene.frame_start, scene.frame_end + 1))
    scene.frame_set(frames[0])
    dg = bpy.context.evaluated_depsgraph_get()
    meshes = sorted((o for o in scene.objects if o.type == "MESH"), key=lambda o: o.name)
    visible = [o for o in meshes if not o.hide_render]
    subjects = [o for o in meshes if "subject" in obj_tags(o)]
    anim_objs = [o for o in meshes if obj_tags(o) & {"animated", "static"}]
    rows = Rows()
    uv_names = {o.name for o in visible}
    check_meshes(bpy, scene, dg, meshes, th, rows, uv_names, baseline)
    check_materials(bpy, visible, rows)
    check_scale(bpy, dg, meshes, th, rows)
    check_budget(bpy, dg, visible, manifest.get("budgets", {}), th, rows)
    n_lit = lit_sources(bpy, scene, visible)
    rows.sd("bo.light.none", None, None, {"lit_sources": n_lit}, {}, n_lit == 0, "no_light")
    check_visibility(bpy, scene, layer, subjects, rows)
    check_zfight(bpy, dg, visible, th, rows)
    check_frames(bpy, scene, frames, subjects, anim_objs, th, rows)
    final = rows.final()
    tmp = args.out + ".tmp"
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(tmp, "w") as f:
        for r in final:
            f.write(json.dumps(r, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n")
    os.replace(tmp, args.out)
    return {"rows": len(final) - 1, "fail": final[-1]["summary"]["fail"]}


if __name__ == "__main__":
    run(entry)
