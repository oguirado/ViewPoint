"""R1 canonical scene digest v2 (Track P, claude-fable-5-2). Self-contained: stdlib + the `bpy` passed in.

    digest_v2(bpy) -> {"col:<Collection>": hex, "mat:<Material>": hex, "render": hex, "ui:view3d": hex}

Superset of the F1 digest (ref_broker/bridge.py:26-40): the same `col:` keys, plus per object the
local transform, parent, visibility, mesh vertex coordinates and modifiers; plus materials (settings,
node values, links), render settings per scene, and the first 3D view (closes finding F4).
This file is embedded verbatim in every control script by ops.py, so it must not import anything
that is not available inside Blender and must not depend on its own module name.
Objects without `bl_rna` (the fake bpy of the unit tests) are read through vars().
"""
import hashlib

DIGEST_V = 2
# properties that are draw-time caches or editor cosmetics, not scene state
_RW_SKIP_NODE = frozenset((
    "rna_type", "dimensions", "select", "location", "width", "height", "width_hidden", "hide",
    "show_options", "show_preview", "show_texture", "is_active_output",
    "bl_width_default", "bl_width_min", "bl_width_max", "bl_height_default", "bl_height_min", "bl_height_max"))
_RW_SKIP_MOD = frozenset((
    "rna_type", "execution_time", "show_expanded", "is_active", "show_in_editmode", "show_on_cage",
    "is_override_data", "use_pin_to_last", "persistent_uid"))


def _rw_n(v):
    """Canonical, repr-stable form of a property value."""
    if v is None or isinstance(v, (bool, int, str)):
        return v
    if isinstance(v, float):
        return round(v, 5) + 0.0          # + 0.0 folds -0.0 into 0.0
    if isinstance(v, (set, frozenset)):
        return sorted(str(x) for x in v)
    if hasattr(v, "bl_rna"):              # a struct: identify it, do not walk it
        return "@" + str(getattr(v, "name", type(v).__name__))
    try:
        return [_rw_n(x) for x in v]      # Vector, Color, Matrix, bpy_prop_array, list, tuple
    except TypeError:
        return "@" + type(v).__name__


def _rw_props(o, skip):
    """Sorted (identifier, value) pairs of every non-collection property of an RNA struct."""
    out = []
    rna = getattr(o, "bl_rna", None)
    if rna is None:                       # fake bpy objects
        for k, v in vars(o).items():
            if not k.startswith("_") and k not in skip:
                out.append((k, _rw_n(v)))
        return sorted(out)
    for p in rna.properties:
        k = p.identifier
        if k in skip or p.type == "COLLECTION":
            continue
        try:
            v = getattr(o, k)
        except Exception:
            continue
        out.append((k, _rw_n(v)))
    return sorted(out)


def _rw_get(o, path, default=None):
    for part in path.split("."):
        o = getattr(o, part, None)
        if o is None:
            return default
    return _rw_n(o)


def _rw_hex(obj):
    return hashlib.sha256(repr(obj).encode()).hexdigest()[:16]


def _rw_verts(me):
    vs = getattr(me, "vertices", None)
    if vs is None:
        return ""
    n = len(vs)
    fg = getattr(vs, "foreach_get", None)
    if fg is None:
        return _rw_hex([_rw_n(v) for v in vs])
    buf = [0.0] * (3 * n)
    fg("co", buf)
    import array
    return hashlib.sha256(array.array("f", buf).tobytes()).hexdigest()[:16]


# Properties every ID datablock has that are bookkeeping of the running session (user
# counts, session ids, evaluation flags), not scene state: they differ after a file load.
_RW_SKIP_ID = frozenset((
    "rna_type", "users", "session_uid", "tag", "is_evaluated", "original", "is_library_indirect",
    "library", "library_weak_reference", "override_library", "preview", "is_embedded_data",
    "is_missing", "is_runtime_data", "name_full", "id_type", "asset_data", "use_fake_user",
    "use_extra_user", "animation_data", "is_editmode", "is_updated", "is_updated_data"))


def _rw_foreach(seq, attr, n, typecode):
    """sha of one per-element attribute read with foreach_get, or "" when unavailable (fake bpy)."""
    fg = getattr(seq, "foreach_get", None)
    if fg is None or n <= 0:
        return ""
    import array
    buf = array.array(typecode, [0]) * n
    try:
        fg(attr, buf)
    except Exception:
        return ""
    return hashlib.sha256(buf.tobytes()).hexdigest()[:16]


def _rw_topology(me):
    """Faces and edges of a mesh: which vertices each face uses and in which order (a deleted or
    flipped face keeps every vertex coordinate), smooth flags, material index per face."""
    polys = getattr(me, "polygons", None)
    loops = getattr(me, "loops", None)
    edges = getattr(me, "edges", None)
    if polys is None or loops is None:
        return ""
    np_, nl = len(polys), len(loops)
    return (np_, nl, len(edges) if edges is not None else -1,
            _rw_foreach(loops, "vertex_index", nl, "i"),
            _rw_foreach(polys, "loop_total", np_, "i"),
            _rw_foreach(polys, "use_smooth", np_, "b"),
            _rw_foreach(polys, "material_index", np_, "i"),
            _rw_foreach(edges, "vertices", 2 * len(edges), "i") if edges is not None else "")


def _rw_idprop(v):
    """Canonical form of a custom-property value (IDProperty group / array / scalar)."""
    td = getattr(v, "to_dict", None)
    if td is not None:
        return sorted((str(k), _rw_idprop(x)) for k, x in td().items())
    tl = getattr(v, "to_list", None)
    if tl is not None:
        return [_rw_n(x) for x in tl()]
    if isinstance(v, dict):
        return sorted((str(k), _rw_idprop(x)) for k, x in v.items())
    return _rw_n(v)


def _rw_custom(ob):
    """Sorted custom properties of an ID (ob["key"] = value)."""
    keys = getattr(ob, "keys", None)
    if keys is None or getattr(ob, "bl_rna", None) is None:
        return []
    # Group-valued keys are skipped: they are addon / versioning storage that Blender creates by
    # itself on file load (a camera made in the session gets a `cycles` group only after the
    # first save -> open [MEASURED 4.0.2]), so hashing them breaks restore equality. Scalars,
    # strings and arrays set by a script (ob["key"] = value) are covered.
    try:
        rna = ob.bl_rna.properties
        out = []
        for k in keys():
            v = ob[k]
            if k in rna or hasattr(v, "to_dict") or isinstance(v, dict):
                continue
            out.append((str(k), _rw_idprop(v)))
        return sorted(out)
    except Exception:
        return []


def _rw_hidden(ob):
    """Viewport eye icon (view-layer visibility); None when the object is in no view layer."""
    hg = getattr(ob, "hide_get", None)
    if hg is None:
        return None
    try:
        return bool(hg())
    except Exception:
        return None


def _rw_object(ob):
    mw = [round(v, 5) + 0.0 for row in ob.matrix_world for v in row]
    data = getattr(ob, "data", None)
    is_mesh = ob.type == "MESH" and data is not None
    nv = len(data.vertices) if is_mesh else -1
    mats = [s.material.name if s.material else "" for s in ob.material_slots]
    f1 = (ob.name, ob.type, mw, data.name if data else "", nv, mats)          # the F1 tuple, unchanged
    mods = [(m.name, m.type, _rw_props(m, _RW_SKIP_MOD)) for m in getattr(ob, "modifiers", ())]
    parent = getattr(ob, "parent", None)
    v2 = (_rw_get(ob, "location"), _rw_get(ob, "rotation_euler"), _rw_get(ob, "scale"),
          parent.name if parent else "", _rw_get(ob, "hide_viewport"), _rw_get(ob, "hide_render"),
          _rw_verts(data) if is_mesh else "", mods)
    # Coverage fix (test_digest_live.py): object data other than vertex coordinates.
    # Lights and cameras are state the look and camera agents edit; before this a light-only
    # turn had an empty delta (judge: NO_EFFECT) and was deduplicated by the store.
    if data is None:
        dprops = None
    elif is_mesh:
        dprops = _rw_topology(data)
    else:
        dprops = _rw_props(data, _RW_SKIP_ID)
    v3 = (dprops, _rw_custom(ob), _rw_custom(data) if data is not None else [],
          _rw_hidden(ob), _rw_get(ob, "hide_select"), _rw_get(ob, "display_type"))
    return (f1, v2, v3)


def _rw_live_collections(bpy):
    """Collections a .blend keeps: those reachable from a scene's master collection. A collection
    that is linked nowhere has no user, is not written by a save and is gone after a load, so
    hashing it would make the digest of a point differ from the digest after restoring that point
    (RESTORE_MISMATCH on a correct restore). Fake bpy (no bl_rna): every collection counts."""
    allc = list(bpy.data.collections)
    if not allc or getattr(allc[0], "bl_rna", None) is None:
        return allc
    seen, stack = set(), [sc.collection for sc in bpy.data.scenes]
    while stack:
        c = stack.pop()
        for k in c.children:
            if k.name not in seen:
                seen.add(k.name)
                stack.append(k)
    return [c for c in allc if c.name in seen]


def _rw_live_materials(bpy):
    """Materials a .blend keeps in a way the scene can show: used by a slot of an object in a
    live collection (or a scene master collection), or protected by a fake user. An unused
    material survives in the session but not in the file (same reason as above)."""
    mats = list(bpy.data.materials)
    if not mats or getattr(mats[0], "bl_rna", None) is None:
        return mats
    used = set()
    cols = _rw_live_collections(bpy) + [sc.collection for sc in bpy.data.scenes]
    for col in cols:
        for ob in col.objects:
            for sl in ob.material_slots:
                if sl.material is not None:
                    used.add(sl.material.name)
    return [m for m in mats if m.name in used or getattr(m, "use_fake_user", False)]


def _rw_collections(bpy):
    out = {}

    def _one(col):
        rows = [_rw_object(ob) for ob in sorted(col.objects, key=lambda o: o.name)]
        kids = sorted(c.name for c in getattr(col, "children", ()))
        return (rows, kids)

    for col in _rw_live_collections(bpy):
        out["col:" + col.name] = _rw_hex(_one(col))
    # Every scene's master collection has the same name ("Scene Collection"): with two scenes
    # the last one used to overwrite the first under one key. One scene keeps the old value
    # shape; several scenes are hashed together, ordered by scene name.
    masters = {}
    for sc in sorted(bpy.data.scenes, key=lambda s: s.name):
        masters.setdefault("col:" + sc.collection.name, []).append((sc.name, _one(sc.collection)))
    for key, rows in masters.items():
        out[key] = _rw_hex(rows[0][1] if len(rows) == 1 else rows)
    return out


def _rw_node_tree(nt):
    if nt is None:
        return None
    nodes = []
    for nd in sorted(nt.nodes, key=lambda x: x.name):
        ins = [(s.identifier, bool(getattr(s, "is_linked", False)), _rw_get(s, "default_value"))
               for s in getattr(nd, "inputs", ())]
        outs = [(s.identifier, _rw_get(s, "default_value")) for s in getattr(nd, "outputs", ())]
        nodes.append((nd.name, getattr(nd, "bl_idname", ""), _rw_props(nd, _RW_SKIP_NODE), ins, outs))
    links = sorted((l.from_node.name, l.from_socket.identifier, l.to_node.name, l.to_socket.identifier)
                   for l in nt.links)
    return (nodes, links)


_RW_MAT_FIELDS = ("diffuse_color", "metallic", "roughness", "specular_intensity", "blend_method",
                  "use_nodes", "use_backface_culling", "alpha_threshold", "pass_index")


def _rw_materials(bpy):
    out = {}
    for m in _rw_live_materials(bpy):
        fields = [(k, _rw_get(m, k)) for k in _RW_MAT_FIELDS]
        out["mat:" + m.name] = _rw_hex((fields, _rw_node_tree(getattr(m, "node_tree", None))))
    return out


_RW_RENDER_FIELDS = (
    "render.engine", "render.resolution_x", "render.resolution_y", "render.resolution_percentage",
    "render.fps", "render.filepath", "render.film_transparent", "render.use_border",
    "render.image_settings.file_format", "render.image_settings.color_mode",
    "frame_start", "frame_end", "frame_current", "camera.name", "world.name", "world.color",
    "view_settings.view_transform", "view_settings.look", "view_settings.exposure", "view_settings.gamma",
    "eevee.taa_render_samples", "cycles.samples", "unit_settings.system", "unit_settings.scale_length")


def _rw_render(bpy):
    rows = []
    for sc in sorted(bpy.data.scenes, key=lambda s: s.name):
        rows.append((sc.name, [(k, _rw_get(sc, k)) for k in _RW_RENDER_FIELDS]))
    return _rw_hex(rows)


# The view is hashed through its sources (location, rotation, distance, perspective); the raw
# view_matrix is a draw-time cache that lags behind them until the next redraw.
_RW_VIEW_FIELDS = ("view_perspective", "is_perspective", "view_location", "view_rotation", "view_distance",
                   "view_camera_zoom", "view_camera_offset")


def view3d_state(bpy):
    """Fields of the first 3D region (window order, then area order), or None without a GUI."""
    for wm in getattr(bpy.data, "window_managers", ()):
        for win in wm.windows:
            for area in win.screen.areas:
                if area.type != "VIEW_3D":
                    continue
                space = area.spaces.active
                r3d = getattr(space, "region_3d", None)
                if r3d is None:
                    continue
                return [(k, _rw_get(r3d, k)) for k in _RW_VIEW_FIELDS] + [("lens", _rw_get(space, "lens"))]
    return None


def digest_v2(bpy):
    out = _rw_collections(bpy)
    out.update(_rw_materials(bpy))
    out["render"] = _rw_render(bpy)
    out["ui:view3d"] = _rw_hex(view3d_state(bpy))
    return out
