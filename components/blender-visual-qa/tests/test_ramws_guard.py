"""STORAGE LAW L2: require_ram_out guards must verify the resolved VQA_RAMWS_ROOT is tmpfs/ramfs."""
import ast
import contextlib
import importlib
import io
import os
from pathlib import Path
import runpy
import sys
import tempfile
import types
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
WORKERS = ("bake_worker.py", "orbit_worker.py", "sheet_worker.py")


def load_guard(worker_name):
    """Load the worker's require_ram_out guard and its canon imports without importing bpy or running main()."""
    bpy_dir = str(ROOT / "bpy_side")
    if bpy_dir not in sys.path:
        sys.path.insert(0, bpy_dir)
    import canon
    importlib.reload(canon)
    src = (ROOT / "bpy_side" / "assets" / worker_name).read_text(encoding="utf-8")
    tree = ast.parse(src, filename=worker_name)
    keep = [
        node for node in tree.body
        if (isinstance(node, ast.ImportFrom) and node.module == "canon")
        or (isinstance(node, ast.FunctionDef) and node.name == "require_ram_out")
    ]
    if len([n for n in keep if isinstance(n, ast.FunctionDef)]) != 1:
        raise AssertionError(f"expected one require_ram_out in {worker_name}")
    ns = {"os": os, "sys": sys}
    exec(compile(ast.Module(keep, []), worker_name, "exec"), ns)
    return canon, ns["require_ram_out"]


def _make_fake_blender():
    """Minimal pure-Python bpy and mathutils stubs recording every write path."""
    written = []

    class _Vec(tuple):
        def __new__(cls, vals):
            return super().__new__(cls, (float(v) for v in vals))

        def __add__(self, o):
            return _Vec(a + b for a, b in zip(self, o))

        def __sub__(self, o):
            return _Vec(a - b for a, b in zip(self, o))

        def __mul__(self, s):
            return _Vec(a * s for a in self)

        def __neg__(self):
            return _Vec(-a for a in self)

        @property
        def length(self):
            return sum(a * a for a in self) ** 0.5

        def to_track_quat(self, _track, _up):
            return (1.0, 0.0, 0.0, 0.0)

    class _Mat:
        def __matmul__(self, v):
            return _Vec(v)

    class _Sock:
        def __init__(self, default=None):
            self.default_value = default
            self.is_linked = False
            self.links = []

    class _Node:
        def __init__(self, ntype="UNKNOWN"):
            self.type = ntype
            self.is_active_output = True
            self.inputs = types.SimpleNamespace()
            self._items = {
                "Surface": _Sock(), "Color": _Sock(), "Emission": _Sock(),
                "Base Color": _Sock((0.8, 0.8, 0.8, 1.0)), "Metallic": _Sock(0.0),
                "Roughness": _Sock(0.5), "Normal": _Sock(),
                0: _Sock((0.5, 0.5, 0.5, 1.0)), 1: _Sock(1.0), 2: _Sock((0.5, 0.5, 0.5)),
            }
            self.outputs = {"Emission": _Sock(), "Normal": _Sock(), 0: _Sock(), 1: _Sock(), 2: _Sock()}
            self.inputs = self

        def __getitem__(self, k):
            return self._items.setdefault(k, _Sock())

    class _NodeList(list):
        def __init__(self, names=()):
            super().__init__()
            self._named = {}
            self.active = None
            for k, ntype in names:
                n = _Node(ntype)
                self.append(n)
                self._named[k] = n

        def new(self, kind):
            ntype = "OUTPUT_MATERIAL" if "OutputMaterial" in kind else (
                "BSDF_PRINCIPLED" if "Principled" in kind else kind
            )
            n = _Node(ntype)
            self.append(n)
            return n

        def remove(self, n):
            if n in self:
                super().remove(n)

        def __getitem__(self, k):
            if isinstance(k, str):
                return self._named.setdefault(k, _Node(k))
            return super().__getitem__(k)

    class _MatBlock:
        def __init__(self, name):
            self.name = name
            self.use_nodes = True
            self.node_tree = types.SimpleNamespace(
                nodes=_NodeList([("Background", "BACKGROUND"), ("Principled BSDF", "BSDF_PRINCIPLED")]),
                links=types.SimpleNamespace(new=lambda *_a: None),
            )

    class _MeshData:
        def __init__(self):
            self.uv_layers = [types.SimpleNamespace(name="UVMap")]
            self.materials = []
            self.polygons = [types.SimpleNamespace(vertices=[0, 1, 2])]
            self.loop_triangles = [0]

        def calc_loop_triangles(self):
            return None

    class _Obj:
        def __init__(self, name="mesh", otype="MESH"):
            self.name = name
            self.type = otype
            self.hide_render = False
            self.parent = None
            self.scale = types.SimpleNamespace(x=1.0)
            self.location = _Vec((0.0, 0.0, 0.0))
            self.rotation_quaternion = (1.0, 0.0, 0.0, 0.0)
            self.rotation_euler = (0.0, 0.0, 0.0)
            self.bound_box = [(-1.0, -1.0, -1.0)] * 4 + [(1.0, 1.0, 1.0)] * 4
            self.matrix_world = _Mat()
            self.data = _MeshData()

        def select_set(self, _v):
            return None

        def keyframe_insert(self, *_a, **_kw):
            return None

    class _Img:
        def __init__(self):
            self.generated_color = None
            self.colorspace_settings = types.SimpleNamespace(name="sRGB")
            self.filepath_raw = ""
            self.file_format = "PNG"

        def save(self):
            written.append(self.filepath_raw)

    class _IterableList(list):
        pass

    mesh_obj = _Obj()
    scene_objects = _IterableList([mesh_obj])
    scene = types.SimpleNamespace(
        objects=scene_objects,
        collection=types.SimpleNamespace(objects=types.SimpleNamespace(link=lambda _o: None)),
        camera=None,
        world=None,
        render=types.SimpleNamespace(
            engine="CYCLES", resolution_x=64, resolution_y=64, resolution_percentage=100,
            film_transparent=False, threads_mode="FIXED", threads=1, use_persistent_data=False,
            use_stamp=False, filepath="",
            image_settings=types.SimpleNamespace(file_format="PNG", color_mode="RGB", color_depth="8", compression=15),
        ),
        view_settings=types.SimpleNamespace(view_transform="Standard"),
        cycles=types.SimpleNamespace(
            device="CPU", samples=8, seed=0, use_animated_seed=False,
            use_denoising=False, use_adaptive_sampling=False,
        ),
        frame_start=1, frame_end=1, frame_set=lambda _n: None,
    )

    class _ViewLayer:
        def __init__(self):
            self.objects = scene_objects
            self.material_override = None

        def update(self):
            return None

    scene_objects.active = mesh_obj
    view_layer = _ViewLayer()

    class _DataObjects(_IterableList):
        def new(self, name, _d):
            return _Obj(name, "OTHER")

    class _DataMaterials(_IterableList):
        def new(self, name):
            return _MatBlock(name)

    data_objects = _DataObjects(scene_objects)
    data = types.SimpleNamespace(
        objects=data_objects,
        materials=_DataMaterials(),
        images=types.SimpleNamespace(new=lambda *_a, **_kw: _Img()),
        cameras=types.SimpleNamespace(new=lambda _n: types.SimpleNamespace(angle=0.5, clip_start=0.1, clip_end=10.0, type="PERSP", ortho_scale=1.0)),
        lights=types.SimpleNamespace(new=lambda _n, _t: types.SimpleNamespace(energy=1.0)),
        worlds=types.SimpleNamespace(new=lambda name: _MatBlock(name)),
    )

    def _import_gltf(**_kw):
        obj = _Obj("imported")
        data_objects.append(obj)
        scene_objects.append(obj)

    ops = types.SimpleNamespace(
        wm=types.SimpleNamespace(
            read_factory_settings=lambda **_kw: None,
            save_as_mainfile=lambda **kw: written.append(kw["filepath"]),
        ),
        import_scene=types.SimpleNamespace(gltf=_import_gltf),
        export_scene=types.SimpleNamespace(gltf=lambda **kw: written.append(kw["filepath"])),
        object=types.SimpleNamespace(join=lambda: None, mode_set=lambda **_kw: None, bake=lambda **_kw: None),
        mesh=types.SimpleNamespace(select_all=lambda **_kw: None),
        uv=types.SimpleNamespace(smart_project=lambda **_kw: None, pack_islands=lambda **_kw: None),
        render=types.SimpleNamespace(render=lambda **_kw: written.append(scene.render.filepath)),
    )
    bpy_mod = types.SimpleNamespace(
        app=types.SimpleNamespace(version_string="4.0.2"),
        context=types.SimpleNamespace(scene=scene, view_layer=view_layer),
        data=data,
        ops=ops,
    )
    mathutils_mod = types.SimpleNamespace(Vector=_Vec)
    return bpy_mod, mathutils_mod, written


class RamwsGuardTests(unittest.TestCase):
    def test_disk_vqa_ramws_root_refused(self):
        with tempfile.TemporaryDirectory(prefix=".tmp_ramws_guard_", dir=ROOT) as disk_root:
            out_dir = os.path.join(disk_root, "out")
            with mock.patch.dict(os.environ, {"VQA_RAMWS_ROOT": disk_root}):
                for worker in WORKERS:
                    with self.subTest(worker=worker):
                        _, guard = load_guard(worker)
                        err = io.StringIO()
                        with contextlib.redirect_stderr(err):
                            with self.assertRaises(SystemExit) as cm:
                                guard(out_dir)
                        self.assertEqual(cm.exception.code, 5)
                        self.assertIn("E_RAM_NOT_TMPFS", err.getvalue())

    def test_default_tmpfs_root_accepted(self):
        env_patch = {k: v for k, v in os.environ.items() if k != "VQA_RAMWS_ROOT"}
        with mock.patch.dict(os.environ, env_patch, clear=True):
            for worker in WORKERS:
                with self.subTest(worker=worker):
                    canon, guard = load_guard(worker)
                    guard(os.path.join(canon.RAMWS_ROOT, "bvqa-asset", "asset", "run", "render"))
                    err = io.StringIO()
                    with contextlib.redirect_stderr(err):
                        with self.assertRaises(SystemExit) as cm:
                            guard(os.path.join(canon.RAMWS_ROOT, "..", "escape"))
                    self.assertEqual(cm.exception.code, 5)

    def test_nested_disk_mount_under_tmpfs_refused(self):
        with tempfile.TemporaryDirectory(prefix=".tmp_ramws_nested_", dir=ROOT) as tmp:
            ram_root = os.path.join(tmp, "ram_root")
            nested_mnt = os.path.join(ram_root, "nested_disk")
            os.makedirs(nested_mnt)
            real_ram = os.path.realpath(ram_root).replace(" ", "\\040")
            real_nested = os.path.realpath(nested_mnt).replace(" ", "\\040")
            mountinfo = os.path.join(tmp, "mountinfo")
            Path(mountinfo).write_text(
                f"10 1 0:1 / / rw,relatime - ext4 /dev/root rw\n"
                f"11 10 0:2 / {real_ram} rw,nosuid,nodev - tmpfs tmpfs rw\n"
                f"12 11 8:1 / {real_nested} rw,relatime - ext4 /dev/sda1 rw\n",
                encoding="utf-8",
            )
            ram_out = os.path.join(ram_root, "ok", "render")
            nested_out = os.path.join(nested_mnt, "sub", "render")
            with mock.patch.dict(os.environ, {"VQA_RAMWS_ROOT": ram_root}):
                for worker in WORKERS:
                    with self.subTest(worker=worker):
                        canon, guard = load_guard(worker)
                        orig = canon.require_ram_out
                        with mock.patch.dict(
                            guard.__globals__,
                            {"_require_ram_out": lambda p, r=None, _o=orig, _mi=mountinfo: _o(p, r, mountinfo=_mi)},
                        ):
                            guard(ram_out)
                            for target in (nested_mnt, nested_out):
                                err = io.StringIO()
                                with contextlib.redirect_stderr(err):
                                    with self.assertRaises(SystemExit) as cm:
                                        guard(target)
                                self.assertEqual(cm.exception.code, 5)
                                self.assertIn("E_RAM_NOT_TMPFS", err.getvalue())

    def test_sol_bounce_symlink_no_disk_mkdir(self):
        with tempfile.TemporaryDirectory(prefix="bvqa_ramws_sol_", dir="/dev/shm") as ram_tmp, \
                tempfile.TemporaryDirectory(prefix=".tmp_ramws_sol_", dir=ROOT) as disk_tmp:
            ram_root = os.path.join(ram_tmp, "ram_root")
            disk_dir = os.path.join(disk_tmp, "disk")
            os.makedirs(ram_root)
            os.makedirs(disk_dir)
            dummy_glb = os.path.join(disk_tmp, "dummy.glb")
            Path(dummy_glb).write_bytes(b"glTF")
            to_disk = os.path.join(disk_tmp, "to_disk")
            os.symlink(disk_dir, to_disk)
            back_to_ram = os.path.join(disk_dir, "back_to_ram")
            os.symlink(ram_root, back_to_ram)

            worker_specs = (
                ("bake_worker.py", lambda out, _blend: [
                    "--high", dummy_glb, "--low", dummy_glb, "--out-dir", out,
                    "--res", "32", "--margin-px", "2", "--cage-extrusion-um", "1000", "--samples", "1",
                ]),
                ("orbit_worker.py", lambda out, blend: [
                    "--glb", dummy_glb, "--out", out, "--first", "1", "--last", "1", "--frames", "1",
                    "--step-mdeg", "5000", "--res", "32", "--spp", "1", "--pitch-mdeg", "15000",
                    "--fov-mdeg", "40000", "--save-blend", blend,
                ]),
                ("sheet_worker.py", lambda out, _blend: [
                    "--glb", dummy_glb, "--out", out, "--views", "P0:persp:0:15000", "--passes", "beauty",
                    "--res", "32", "--spp", "1", "--fov-mdeg", "40000",
                ]),
            )

            bpy_dir = str(ROOT / "bpy_side")
            if bpy_dir not in sys.path:
                sys.path.insert(0, bpy_dir)
            for worker, make_argv in worker_specs:
                stem = worker.split("_")[0]
                disk_mid = os.path.join(disk_dir, f"missing_{stem}")
                disk_blend_mid = os.path.join(disk_dir, f"missing_{stem}_blend")
                orig_out = os.path.join(to_disk, f"missing_{stem}", "..", "back_to_ram", f"out_{stem}")
                orig_blend = os.path.join(
                    to_disk, f"missing_{stem}_blend", "..", "back_to_ram", f"out_{stem}", "scene.blend",
                )
                expected_ram_out = os.path.realpath(os.path.join(ram_root, f"out_{stem}"))
                bpy_mod, mathutils_mod, written = _make_fake_blender()
                with self.subTest(worker=worker):
                    with mock.patch.dict(os.environ, {"VQA_RAMWS_ROOT": ram_root}), \
                            mock.patch.dict(sys.modules, {"bpy": bpy_mod, "mathutils": mathutils_mod}), \
                            mock.patch.object(sys, "argv", [worker, "--"] + make_argv(orig_out, orig_blend)), \
                            contextlib.redirect_stdout(io.StringIO()):
                        runpy.run_path(str(ROOT / "bpy_side" / "assets" / worker), run_name="__main__")
                    self.assertFalse(os.path.exists(disk_mid), f"{worker} created dir on disk: {disk_mid}")
                    self.assertFalse(os.path.exists(disk_blend_mid), f"{worker} created dir on disk: {disk_blend_mid}")
                    self.assertEqual(sorted(os.listdir(disk_dir)), ["back_to_ram"])
                    self.assertTrue(os.path.isdir(expected_ram_out))
                    self.assertTrue(written, f"{worker} recorded no writes")
                    for wpath in written:
                        self.assertTrue(
                            wpath.startswith(expected_ram_out + os.sep),
                            f"{worker} wrote non-canonical path {wpath!r} (expected prefix {expected_ram_out + os.sep!r})",
                        )

    def test_property_ramws_guard_combinations(self):
        def _esc(p):
            return p.replace("\\", "\\134").replace(" ", "\\040").replace("\t", "\\011")

        def _walk_dirs(base):
            found = set()
            for dirpath, dirnames, _ in os.walk(base, followlinks=False):
                for d in dirnames:
                    found.add(os.path.join(dirpath, d))
            return found

        with tempfile.TemporaryDirectory(prefix="bvqa_ramws_prop_", dir="/dev/shm") as ram_tmp, \
                tempfile.TemporaryDirectory(prefix=".tmp_ramws_prop_", dir=ROOT) as disk_tmp:
            disk_dir = os.path.join(disk_tmp, "disk_area")
            sym_target_disk = os.path.join(disk_dir, "sym_target_disk")
            os.makedirs(sym_target_disk)
            for octal_mp in (False, True):
                ram_mp = os.path.join(ram_tmp, "ram root\t1\\s" if octal_mp else "ram_root")
                nested_mnt = os.path.join(ram_mp, "nested disk\t2" if octal_mp else "nested_disk")
                sym_target_ram = os.path.join(ram_mp, "sym_target_ram")
                os.makedirs(nested_mnt, exist_ok=True)
                os.makedirs(sym_target_ram, exist_ok=True)
                real_ram_mp = os.path.realpath(ram_mp)
                real_nested_mnt = os.path.realpath(nested_mnt)

                mi_path = os.path.join(disk_tmp, f"mountinfo_{int(octal_mp)}")
                Path(mi_path).write_text(
                    f"10 1 0:1 / / rw,relatime - ext4 /dev/root rw\n"
                    f"11 10 0:2 / {_esc(real_ram_mp)} rw,nosuid,nodev - tmpfs tmpfs rw\n"
                    f"12 11 8:1 / {_esc(real_nested_mnt)} rw,relatime - ext4 /dev/sda1 rw\n",
                    encoding="utf-8",
                )

                sym_root_ram = os.path.join(disk_dir, f"sym_root_ram_{int(octal_mp)}")
                sym_root_disk = os.path.join(ram_mp, f"sym_root_disk_{int(octal_mp)}")
                sym_dest_ram = os.path.join(ram_mp, f"sym_dest_ram_{int(octal_mp)}")
                sym_dest_disk = os.path.join(ram_mp, f"sym_dest_disk_{int(octal_mp)}")
                back_to_ram = os.path.join(sym_target_disk, f"back_to_ram_{int(octal_mp)}")
                for link, target in (
                    (sym_root_ram, ram_mp),
                    (sym_root_disk, disk_dir),
                    (sym_dest_ram, sym_target_ram),
                    (sym_dest_disk, sym_target_disk),
                    (back_to_ram, ram_mp),
                ):
                    if not os.path.lexists(link):
                        os.symlink(target, link)

                roots = (("direct_ram", ram_mp), ("sym_ram", sym_root_ram), ("sym_disk", sym_root_disk))
                dests = ("direct_ram", "sym_to_ram", "sym_to_disk", "nested_disk", "dotdot_escape", "bounce_disk_to_ram")
                idx = 0
                for worker in WORKERS:
                    canon, guard = load_guard(worker)
                    orig_req = canon.require_ram_out
                    with mock.patch.dict(
                        guard.__globals__,
                        {"_require_ram_out": lambda p, r=None, _o=orig_req, _mi=mi_path: _o(p, r, mountinfo=_mi)},
                    ):
                        for root_kind, chosen_root in roots:
                            for dest_kind in dests:
                                for missing_mid in (False, True):
                                    for use_dotdot in (False, True):
                                        idx += 1
                                        leaf = f"case_{idx}"
                                        if dest_kind == "direct_ram":
                                            parts = [chosen_root]
                                        elif dest_kind == "sym_to_ram":
                                            parts = [sym_dest_ram]
                                        elif dest_kind == "sym_to_disk":
                                            parts = [sym_dest_disk]
                                        elif dest_kind == "nested_disk":
                                            parts = [nested_mnt]
                                        elif dest_kind == "dotdot_escape":
                                            parts = [chosen_root, "..", f"escape_{idx}"]
                                        else:
                                            parts = [sym_dest_disk]
                                            if missing_mid:
                                                parts.append(f"mid_{idx}")
                                            if use_dotdot:
                                                if not missing_mid:
                                                    parts.append(f"mid_{idx}")
                                                parts.append("..")
                                            parts.append(f"back_to_ram_{int(octal_mp)}")
                                        if dest_kind != "bounce_disk_to_ram":
                                            if missing_mid:
                                                parts.append(f"mid_{idx}")
                                            if use_dotdot:
                                                parts.extend([f"sub_{idx}", ".."])
                                        parts.append(leaf)
                                        target = os.path.join(*parts)

                                        disk_before = _walk_dirs(disk_tmp)
                                        nested_before = _walk_dirs(nested_mnt)
                                        with mock.patch.dict(os.environ, {"VQA_RAMWS_ROOT": chosen_root}):
                                            err = io.StringIO()
                                            try:
                                                with contextlib.redirect_stderr(err):
                                                    resolved = guard(target)
                                            except SystemExit as exc:
                                                self.assertEqual(exc.code, 5)
                                                self.assertIn("E_RAM_NOT_TMPFS", err.getvalue())
                                                self.assertEqual(_walk_dirs(disk_tmp), disk_before)
                                                self.assertEqual(_walk_dirs(nested_mnt), nested_before)
                                                continue
                                            self.assertIsInstance(resolved, str)
                                            os.makedirs(resolved, exist_ok=True)
                                            self.assertEqual(_walk_dirs(disk_tmp), disk_before)
                                            self.assertEqual(_walk_dirs(nested_mnt), nested_before)
                                            real_resolved = os.path.realpath(resolved)
                                            self.assertTrue(
                                                (real_resolved + os.sep).startswith(real_ram_mp + os.sep),
                                                f"created outside tmpfs root: {real_resolved}",
                                            )
                                            self.assertFalse(
                                                (real_resolved + os.sep).startswith(real_nested_mnt + os.sep),
                                                f"created inside nested disk mount: {real_resolved}",
                                            )


class RamwsChildGuardTests(unittest.TestCase):
    def test_ram_child_rejects_unsafe_components(self):
        bpy_dir = str(ROOT / "bpy_side")
        if bpy_dir not in sys.path:
            sys.path.insert(0, bpy_dir)
        import canon
        importlib.reload(canon)

        with tempfile.TemporaryDirectory(prefix="bvqa_ram_child_", dir="/dev/shm") as ram_tmp, \
                tempfile.TemporaryDirectory(prefix=".tmp_ram_child_", dir=ROOT) as disk_tmp:
            base = os.path.realpath(ram_tmp)
            abs_escape = os.path.join(disk_tmp, "abs_escape")
            sym_escape = os.path.join(base, "sym_escape")
            os.symlink(disk_tmp, sym_escape)
            sym_self = os.path.join(base, "sym_self")
            os.symlink(base, sym_self)

            for bad in (abs_escape, "../x", "a/b", "..", ".", "", "a\x00b", "sym_escape", "sym_self"):
                with self.subTest(bad=bad):
                    err = io.StringIO()
                    with contextlib.redirect_stderr(err):
                        with self.assertRaises(SystemExit) as cm:
                            canon.ram_child(base, bad)
                    self.assertEqual(cm.exception.code, 5)
                    self.assertIn("E_RAM_NOT_TMPFS", err.getvalue())

            with contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as cm:
                    canon.ram_child(base, "sub", "../x")
                self.assertEqual(cm.exception.code, 5)

            self.assertEqual(canon.ram_child(base, "P0_beauty.png"), os.path.join(base, "P0_beauty.png"))
            self.assertEqual(canon.ram_child(base, "sub", "P0_beauty.png"), os.path.join(base, "sub", "P0_beauty.png"))

    def test_sheet_views_rejects_unsafe_ids_at_parse_time(self):
        with tempfile.TemporaryDirectory(prefix="bvqa_sheet_vid_", dir="/dev/shm") as ram_tmp, \
                tempfile.TemporaryDirectory(prefix=".tmp_sheet_vid_", dir=ROOT) as disk_tmp:
            ram_root = os.path.realpath(ram_tmp)
            out_dir = os.path.join(ram_root, "sheet_out")
            dummy_glb = os.path.join(disk_tmp, "dummy.glb")
            Path(dummy_glb).write_bytes(b"glTF")
            abs_escape = os.path.join(disk_tmp, "abs_view")

            bpy_dir = str(ROOT / "bpy_side")
            if bpy_dir not in sys.path:
                sys.path.insert(0, bpy_dir)

            for bad_id in (abs_escape, "../x", "a/b", "..", ".", "", "bad\x00id"):
                with self.subTest(bad_id=bad_id):
                    bpy_mod, mathutils_mod, written = _make_fake_blender()
                    out_buf = io.StringIO()
                    err_buf = io.StringIO()
                    argv = [
                        "sheet_worker.py", "--",
                        "--glb", dummy_glb, "--out", out_dir,
                        "--views", f"{bad_id}:persp:0:15000", "--passes", "beauty",
                        "--res", "32", "--spp", "1", "--fov-mdeg", "40000",
                    ]
                    with mock.patch.dict(os.environ, {"VQA_RAMWS_ROOT": ram_root}), \
                            mock.patch.dict(sys.modules, {"bpy": bpy_mod, "mathutils": mathutils_mod}), \
                            mock.patch.object(sys, "argv", argv), \
                            contextlib.redirect_stdout(out_buf), \
                            contextlib.redirect_stderr(err_buf):
                        with self.assertRaises(SystemExit) as cm:
                            runpy.run_path(str(ROOT / "bpy_side" / "assets" / "sheet_worker.py"), run_name="__main__")
                    self.assertEqual(cm.exception.code, 2, out_buf.getvalue() + err_buf.getvalue())
                    self.assertIn("BVQA_SHEET_ERROR", out_buf.getvalue())
                    self.assertEqual(written, [])
                    self.assertFalse(os.path.exists(abs_escape + "_beauty.png"))

    def test_workers_ram_child_blocks_composed_names(self):
        bpy_dir = str(ROOT / "bpy_side")
        assets_dir = str(ROOT / "bpy_side" / "assets")
        for d in (bpy_dir, assets_dir):
            if d not in sys.path:
                sys.path.insert(0, d)
        import canon
        importlib.reload(canon)

        for worker in WORKERS:
            src = (ROOT / "bpy_side" / "assets" / worker).read_text(encoding="utf-8")
            tree = ast.parse(src, filename=worker)
            join_calls = [
                n for n in ast.walk(tree)
                if isinstance(n, ast.Call)
                and isinstance(n.func, ast.Attribute)
                and n.func.attr == "join"
                and isinstance(n.func.value, ast.Attribute)
                and n.func.value.attr == "path"
            ]
            self.assertEqual(join_calls, [], f"{worker} still uses os.path.join for output paths")
            self.assertIn("ram_child", src, f"{worker} must use ram_child")

        import bake_worker
        import sheet_worker
        for mod in (bake_worker, sheet_worker):
            importlib.reload(mod)

        with tempfile.TemporaryDirectory(prefix="bvqa_child_w_", dir="/dev/shm") as ram_tmp, \
                tempfile.TemporaryDirectory(prefix=".tmp_child_w_", dir=ROOT) as disk_tmp:
            ram_root = os.path.realpath(ram_tmp)
            dummy_glb = os.path.join(disk_tmp, "dummy.glb")
            Path(dummy_glb).write_bytes(b"glTF")
            abs_escape = os.path.join(disk_tmp, "abs_map")

            for bad_name in (abs_escape, "../x", "a/b", "bad\x00name"):
                with self.subTest(worker="sheet_worker", bad_name=bad_name):
                    bpy_mod, mathutils_mod, written = _make_fake_blender()
                    out_dir = os.path.join(ram_root, "sheet_bypass")
                    argv = [
                        "sheet_worker.py", "--",
                        "--glb", dummy_glb, "--out", out_dir,
                        "--views", "P0:persp:0:15000", "--passes", "beauty",
                        "--res", "32", "--spp", "1", "--fov-mdeg", "40000",
                    ]
                    bypass = [{"id": bad_name, "kind": "persp", "az_mdeg": 0, "el_mdeg": 15000}]
                    err_buf = io.StringIO()
                    with mock.patch.dict(os.environ, {"VQA_RAMWS_ROOT": ram_root}), \
                            mock.patch.dict(sys.modules, {"bpy": bpy_mod, "mathutils": mathutils_mod}), \
                            mock.patch.object(sys, "argv", argv), \
                            mock.patch.object(sheet_worker, "parse_views", return_value=bypass), \
                            contextlib.redirect_stdout(io.StringIO()), \
                            contextlib.redirect_stderr(err_buf):
                        with self.assertRaises(SystemExit) as cm:
                            sheet_worker.main()
                    self.assertEqual(cm.exception.code, 5)
                    self.assertIn("E_RAM_NOT_TMPFS", err_buf.getvalue())
                    self.assertEqual(written, [])

                with self.subTest(worker="bake_worker", bad_name=bad_name):
                    bpy_mod, mathutils_mod, written = _make_fake_blender()
                    out_dir = os.path.join(ram_root, "bake_bypass")
                    os.makedirs(out_dir, exist_ok=True)
                    args = bake_worker.parse_args([
                        "--", "--high", dummy_glb, "--low", dummy_glb, "--out-dir", out_dir,
                        "--res", "32", "--margin-px", "2", "--cage-extrusion-um", "1000", "--samples", "1",
                    ])
                    err_buf = io.StringIO()
                    with mock.patch.object(bake_worker, "MAPS", ((bad_name, "NORMAL", "Non-Color"),)), \
                            contextlib.redirect_stderr(err_buf):
                        with self.assertRaises(SystemExit) as cm:
                            bake_worker.bake(bpy_mod, args)
                    self.assertEqual(cm.exception.code, 5)
                    self.assertIn("E_RAM_NOT_TMPFS", err_buf.getvalue())
                    self.assertEqual(written, [])

            # Nominal orbit_worker with ram_child composes canonical frame paths inside out dir,
            # and rejects a symlink child escaping out dir.
            bpy_mod, mathutils_mod, written = _make_fake_blender()
            orbit_out = os.path.join(ram_root, "orbit_ok")
            argv = [
                "orbit_worker.py", "--",
                "--glb", dummy_glb, "--out", orbit_out, "--first", "1", "--last", "2", "--frames", "2",
                "--step-mdeg", "5000", "--res", "32", "--spp", "1", "--pitch-mdeg", "15000", "--fov-mdeg", "40000",
            ]
            with mock.patch.dict(os.environ, {"VQA_RAMWS_ROOT": ram_root}), \
                    mock.patch.dict(sys.modules, {"bpy": bpy_mod, "mathutils": mathutils_mod}), \
                    mock.patch.object(sys, "argv", argv), \
                    contextlib.redirect_stdout(io.StringIO()):
                runpy.run_path(str(ROOT / "bpy_side" / "assets" / "orbit_worker.py"), run_name="__main__")
            self.assertEqual(written, [os.path.join(orbit_out, "000001.png"), os.path.join(orbit_out, "000002.png")])

            bpy_mod, mathutils_mod, written = _make_fake_blender()
            orbit_sym = os.path.join(ram_root, "orbit_sym")
            os.makedirs(orbit_sym, exist_ok=True)
            os.symlink(disk_tmp, os.path.join(orbit_sym, "000001.png"))
            argv_sym = [
                "orbit_worker.py", "--",
                "--glb", dummy_glb, "--out", orbit_sym, "--first", "1", "--last", "1", "--frames", "1",
                "--step-mdeg", "5000", "--res", "32", "--spp", "1", "--pitch-mdeg", "15000", "--fov-mdeg", "40000",
            ]
            err_buf = io.StringIO()
            with mock.patch.dict(os.environ, {"VQA_RAMWS_ROOT": ram_root}), \
                    mock.patch.dict(sys.modules, {"bpy": bpy_mod, "mathutils": mathutils_mod}), \
                    mock.patch.object(sys, "argv", argv_sym), \
                    contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(err_buf):
                with self.assertRaises(SystemExit) as cm:
                    runpy.run_path(str(ROOT / "bpy_side" / "assets" / "orbit_worker.py"), run_name="__main__")
            self.assertEqual(cm.exception.code, 5)
            self.assertIn("E_RAM_NOT_TMPFS", err_buf.getvalue())
            self.assertEqual(written, [])


class RamwsDeviceGuardTests(unittest.TestCase):
    """STORAGE LAW L2 device invariant: pre-write exact-path st_dev/non-symlink and post-write verify_written walk."""

    @staticmethod
    def _patch_st_dev(target_prefix, fake_dev):
        orig_stat = os.stat
        orig_lstat = os.lstat
        real_prefix = os.path.realpath(target_prefix)
        pfx = real_prefix + os.sep

        def _fake(fn, path, *args, **kwargs):
            st = fn(path, *args, **kwargs)
            p_str = os.fspath(path)
            if p_str == target_prefix or p_str.startswith(target_prefix + os.sep) \
                    or p_str == real_prefix or p_str.startswith(pfx):
                vals = list(st)
                vals[2] = fake_dev
                return os.stat_result(vals)
            return st

        return mock.patch.multiple(
            os,
            stat=lambda p, *a, **kw: _fake(orig_stat, p, *a, **kw),
            lstat=lambda p, *a, **kw: _fake(orig_lstat, p, *a, **kw),
        )

    def test_ram_child_and_require_ram_out_reject_nested_mount_by_st_dev(self):
        bpy_dir = str(ROOT / "bpy_side")
        if bpy_dir not in sys.path:
            sys.path.insert(0, bpy_dir)
        import canon
        importlib.reload(canon)

        with tempfile.TemporaryDirectory(prefix="bvqa_dev_nested_", dir="/dev/shm") as ram_tmp:
            ram_root = os.path.realpath(ram_tmp)
            out_dir = os.path.join(ram_root, "out")
            nested = os.path.join(out_dir, "nested_disk")
            os.makedirs(nested, exist_ok=True)
            foreign_dev = os.stat(ram_root).st_dev + 12345

            with mock.patch.dict(os.environ, {"VQA_RAMWS_ROOT": ram_root}), \
                    self._patch_st_dev(nested, foreign_dev):
                self.assertEqual(canon.ram_child(out_dir, "ok.png"), os.path.join(out_dir, "ok.png"))
                for bad_args in (("nested_disk",), ("nested_disk", "sheet.png")):
                    with self.subTest(bad_args=bad_args):
                        err = io.StringIO()
                        with contextlib.redirect_stderr(err):
                            with self.assertRaises(SystemExit) as cm:
                                canon.ram_child(out_dir, *bad_args)
                        self.assertEqual(cm.exception.code, 5)
                        self.assertIn("E_RAM_NOT_TMPFS", err.getvalue())

                err = io.StringIO()
                with contextlib.redirect_stderr(err):
                    with self.assertRaises(SystemExit) as cm:
                        canon.require_ram_out(os.path.join(nested, "sub"), ramws_root=ram_root)
                self.assertEqual(cm.exception.code, 5)
                self.assertIn("E_RAM_NOT_TMPFS", err.getvalue())

    def test_orbit_refuses_preexisting_symlink_at_final_png_and_passes_png_paths(self):
        with tempfile.TemporaryDirectory(prefix="bvqa_orbit_png_", dir="/dev/shm") as ram_tmp, \
                tempfile.TemporaryDirectory(prefix=".tmp_orbit_png_", dir=ROOT) as disk_tmp:
            ram_root = os.path.realpath(ram_tmp)
            dummy_glb = os.path.join(disk_tmp, "dummy.glb")
            Path(dummy_glb).write_bytes(b"glTF")
            ram_victim = os.path.join(ram_root, "ram_victim.png")
            Path(ram_victim).write_bytes(b"keep")
            disk_victim = os.path.join(disk_tmp, "disk_victim.png")
            Path(disk_victim).write_bytes(b"keep")

            bpy_dir = str(ROOT / "bpy_side")
            if bpy_dir not in sys.path:
                sys.path.insert(0, bpy_dir)

            # Nominal orbit_worker passes exact .png paths to Blender.
            nom_out = os.path.join(ram_root, "orbit_nom")
            bpy_mod, mathutils_mod, written = _make_fake_blender()
            argv_nom = [
                "orbit_worker.py", "--",
                "--glb", dummy_glb, "--out", nom_out, "--first", "1", "--last", "2", "--frames", "2",
                "--step-mdeg", "5000", "--res", "32", "--spp", "1", "--pitch-mdeg", "15000", "--fov-mdeg", "40000",
            ]
            with mock.patch.dict(os.environ, {"VQA_RAMWS_ROOT": ram_root}), \
                    mock.patch.dict(sys.modules, {"bpy": bpy_mod, "mathutils": mathutils_mod}), \
                    mock.patch.object(sys, "argv", argv_nom), \
                    contextlib.redirect_stdout(io.StringIO()):
                runpy.run_path(str(ROOT / "bpy_side" / "assets" / "orbit_worker.py"), run_name="__main__")
            self.assertEqual(written, [os.path.join(nom_out, "000001.png"), os.path.join(nom_out, "000002.png")])

            # Sol case 2: pre-existing symlink at final .png (on frame 1 or frame 2, pointing to disk, RAM, or dangling).
            for label, fname, target in (
                ("frame1_disk", "000001.png", disk_victim),
                ("frame2_disk", "000002.png", disk_victim),
                ("frame1_ram", "000001.png", ram_victim),
                ("frame1_dangling", "000001.png", os.path.join(disk_tmp, "missing.png")),
            ):
                with self.subTest(case=label):
                    out_dir = os.path.join(ram_root, f"orbit_{label}")
                    os.makedirs(out_dir, exist_ok=True)
                    os.symlink(target, os.path.join(out_dir, fname))
                    bpy_mod, mathutils_mod, written = _make_fake_blender()
                    argv = [
                        "orbit_worker.py", "--",
                        "--glb", dummy_glb, "--out", out_dir, "--first", "1", "--last", "2", "--frames", "2",
                        "--step-mdeg", "5000", "--res", "32", "--spp", "1", "--pitch-mdeg", "15000", "--fov-mdeg", "40000",
                    ]
                    out_buf, err_buf = io.StringIO(), io.StringIO()
                    with mock.patch.dict(os.environ, {"VQA_RAMWS_ROOT": ram_root}), \
                            mock.patch.dict(sys.modules, {"bpy": bpy_mod, "mathutils": mathutils_mod}), \
                            mock.patch.object(sys, "argv", argv), \
                            contextlib.redirect_stdout(out_buf), \
                            contextlib.redirect_stderr(err_buf):
                        with self.assertRaises(SystemExit) as cm:
                            runpy.run_path(str(ROOT / "bpy_side" / "assets" / "orbit_worker.py"), run_name="__main__")
                    self.assertEqual(cm.exception.code, 5)
                    self.assertIn("E_RAM_NOT_TMPFS", err_buf.getvalue())
                    self.assertNotIn("BVQA_ORBIT ", out_buf.getvalue())
                    self.assertEqual(written, [])

            # Pre-existing symlink at --save-blend destination is also refused before any render.
            blend_dir = os.path.join(ram_root, "orbit_blend_sym")
            os.makedirs(blend_dir, exist_ok=True)
            blend_sym = os.path.join(blend_dir, "scene.blend")
            os.symlink(os.path.join(disk_tmp, "victim.blend"), blend_sym)
            bpy_mod, mathutils_mod, written = _make_fake_blender()
            argv_blend = [
                "orbit_worker.py", "--",
                "--glb", dummy_glb, "--out", blend_dir, "--first", "1", "--last", "1", "--frames", "1",
                "--step-mdeg", "5000", "--res", "32", "--spp", "1", "--pitch-mdeg", "15000",
                "--fov-mdeg", "40000", "--save-blend", blend_sym,
            ]
            err_buf = io.StringIO()
            with mock.patch.dict(os.environ, {"VQA_RAMWS_ROOT": ram_root}), \
                    mock.patch.dict(sys.modules, {"bpy": bpy_mod, "mathutils": mathutils_mod}), \
                    mock.patch.object(sys, "argv", argv_blend), \
                    contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(err_buf):
                with self.assertRaises(SystemExit) as cm:
                    runpy.run_path(str(ROOT / "bpy_side" / "assets" / "orbit_worker.py"), run_name="__main__")
            self.assertEqual(cm.exception.code, 5)
            self.assertIn("E_RAM_NOT_TMPFS", err_buf.getvalue())
            self.assertEqual(written, [])

    def test_bake_and_sheet_refuse_preexisting_output_symlinks_and_nested_mount(self):
        with tempfile.TemporaryDirectory(prefix="bvqa_bs_sym_", dir="/dev/shm") as ram_tmp, \
                tempfile.TemporaryDirectory(prefix=".tmp_bs_sym_", dir=ROOT) as disk_tmp:
            ram_root = os.path.realpath(ram_tmp)
            dummy_glb = os.path.join(disk_tmp, "dummy.glb")
            Path(dummy_glb).write_bytes(b"glTF")
            ram_victim = os.path.join(ram_root, "ram_victim")
            Path(ram_victim).write_bytes(b"keep")
            disk_victim = os.path.join(disk_tmp, "disk_victim")
            Path(disk_victim).write_bytes(b"keep")

            bpy_dir = str(ROOT / "bpy_side")
            if bpy_dir not in sys.path:
                sys.path.insert(0, bpy_dir)

            for fname in ("albedo.png", "low_uv.glb"):
                for kind, target in (("ram", ram_victim), ("disk", disk_victim)):
                    with self.subTest(worker="bake", fname=fname, kind=kind):
                        out_dir = os.path.join(ram_root, f"bake_{fname}_{kind}")
                        os.makedirs(out_dir, exist_ok=True)
                        os.symlink(target, os.path.join(out_dir, fname))
                        bpy_mod, mathutils_mod, written = _make_fake_blender()
                        argv = [
                            "bake_worker.py", "--",
                            "--high", dummy_glb, "--low", dummy_glb, "--out-dir", out_dir,
                            "--res", "32", "--margin-px", "2", "--cage-extrusion-um", "1000", "--samples", "1",
                        ]
                        out_buf, err_buf = io.StringIO(), io.StringIO()
                        with mock.patch.dict(os.environ, {"VQA_RAMWS_ROOT": ram_root}), \
                                mock.patch.dict(sys.modules, {"bpy": bpy_mod, "mathutils": mathutils_mod}), \
                                mock.patch.object(sys, "argv", argv), \
                                contextlib.redirect_stdout(out_buf), \
                                contextlib.redirect_stderr(err_buf):
                            with self.assertRaises(SystemExit) as cm:
                                runpy.run_path(str(ROOT / "bpy_side" / "assets" / "bake_worker.py"), run_name="__main__")
                        self.assertEqual(cm.exception.code, 5)
                        self.assertIn("E_RAM_NOT_TMPFS", err_buf.getvalue())
                        self.assertNotIn("BVQA_BAKE ", out_buf.getvalue())
                        self.assertEqual(written, [])

            for kind, target in (("ram", ram_victim), ("disk", disk_victim)):
                with self.subTest(worker="sheet", kind=kind):
                    out_dir = os.path.join(ram_root, f"sheet_{kind}")
                    os.makedirs(out_dir, exist_ok=True)
                    os.symlink(target, os.path.join(out_dir, "P0_beauty.png"))
                    bpy_mod, mathutils_mod, written = _make_fake_blender()
                    argv = [
                        "sheet_worker.py", "--",
                        "--glb", dummy_glb, "--out", out_dir, "--views", "P0:persp:0:15000", "--passes", "beauty",
                        "--res", "32", "--spp", "1", "--fov-mdeg", "40000",
                    ]
                    out_buf, err_buf = io.StringIO(), io.StringIO()
                    with mock.patch.dict(os.environ, {"VQA_RAMWS_ROOT": ram_root}), \
                            mock.patch.dict(sys.modules, {"bpy": bpy_mod, "mathutils": mathutils_mod}), \
                            mock.patch.object(sys, "argv", argv), \
                            contextlib.redirect_stdout(out_buf), \
                            contextlib.redirect_stderr(err_buf):
                        with self.assertRaises(SystemExit) as cm:
                            runpy.run_path(str(ROOT / "bpy_side" / "assets" / "sheet_worker.py"), run_name="__main__")
                    self.assertEqual(cm.exception.code, 5)
                    self.assertIn("E_RAM_NOT_TMPFS", err_buf.getvalue())
                    self.assertNotIn("BVQA_SHEET ", out_buf.getvalue())
                    self.assertEqual(written, [])

            # Nested disk st_dev on an output file inside out_dir is also rejected pre-render.
            sheet_dev_dir = os.path.join(ram_root, "sheet_st_dev")
            os.makedirs(sheet_dev_dir, exist_ok=True)
            bad_png = os.path.join(sheet_dev_dir, "P0_beauty.png")
            Path(bad_png).write_bytes(b"pre")
            foreign_dev = os.stat(ram_root).st_dev + 12345
            bpy_mod, mathutils_mod, written = _make_fake_blender()
            argv = [
                "sheet_worker.py", "--",
                "--glb", dummy_glb, "--out", sheet_dev_dir, "--views", "P0:persp:0:15000", "--passes", "beauty",
                "--res", "32", "--spp", "1", "--fov-mdeg", "40000",
            ]
            err_buf = io.StringIO()
            with mock.patch.dict(os.environ, {"VQA_RAMWS_ROOT": ram_root}), \
                    mock.patch.dict(sys.modules, {"bpy": bpy_mod, "mathutils": mathutils_mod}), \
                    mock.patch.object(sys, "argv", argv), \
                    self._patch_st_dev(bad_png, foreign_dev), \
                    contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(err_buf):
                with self.assertRaises(SystemExit) as cm:
                    runpy.run_path(str(ROOT / "bpy_side" / "assets" / "sheet_worker.py"), run_name="__main__")
            self.assertEqual(cm.exception.code, 5)
            self.assertIn("E_RAM_NOT_TMPFS", err_buf.getvalue())
            self.assertEqual(written, [])

    def test_verify_written_catches_post_render_symlink_and_foreign_dev_without_removing(self):
        bpy_dir = str(ROOT / "bpy_side")
        if bpy_dir not in sys.path:
            sys.path.insert(0, bpy_dir)
        import canon
        importlib.reload(canon)

        with tempfile.TemporaryDirectory(prefix="bvqa_verify_w_", dir="/dev/shm") as ram_tmp, \
                tempfile.TemporaryDirectory(prefix=".tmp_verify_w_", dir=ROOT) as disk_tmp:
            ram_root = os.path.realpath(ram_tmp)
            dummy_glb = os.path.join(disk_tmp, "dummy.glb")
            Path(dummy_glb).write_bytes(b"glTF")

            # Direct verify_written unit checks: valid dir passes; planted symlink or foreign st_dev exits 5 and removes nothing.
            ok_dir = os.path.join(ram_root, "ok_dir")
            os.makedirs(ok_dir, exist_ok=True)
            ok_file = os.path.join(ok_dir, "frame.png")
            Path(ok_file).write_bytes(b"png")
            with mock.patch.dict(os.environ, {"VQA_RAMWS_ROOT": ram_root}):
                canon.verify_written(ok_dir)

                foreign_dev = os.stat(ram_root).st_dev + 999
                with self._patch_st_dev(ok_file, foreign_dev):
                    err_buf = io.StringIO()
                    with contextlib.redirect_stderr(err_buf):
                        with self.assertRaises(SystemExit) as cm:
                            canon.verify_written(ok_dir)
                    self.assertEqual(cm.exception.code, 5)
                    self.assertIn("E_RAM_NOT_TMPFS", err_buf.getvalue())
                self.assertTrue(os.path.isfile(ok_file))

                planted = os.path.join(ok_dir, "planted.png")
                os.symlink(disk_tmp, planted)
                err_buf = io.StringIO()
                with contextlib.redirect_stderr(err_buf):
                    with self.assertRaises(SystemExit) as cm:
                        canon.verify_written(ok_dir)
                self.assertEqual(cm.exception.code, 5)
                self.assertIn("E_RAM_NOT_TMPFS", err_buf.getvalue())
                self.assertTrue(os.path.islink(planted))

            # End-to-end across all three workers: symlink planted during render is caught by verify_written before success output.
            worker_specs = (
                ("bake_worker.py", "BVQA_BAKE ", lambda out: [
                    "--high", dummy_glb, "--low", dummy_glb, "--out-dir", out,
                    "--res", "32", "--margin-px", "2", "--cage-extrusion-um", "1000", "--samples", "1",
                ]),
                ("orbit_worker.py", "BVQA_ORBIT ", lambda out: [
                    "--glb", dummy_glb, "--out", out, "--first", "1", "--last", "1", "--frames", "1",
                    "--step-mdeg", "5000", "--res", "32", "--spp", "1", "--pitch-mdeg", "15000", "--fov-mdeg", "40000",
                ]),
                ("sheet_worker.py", "BVQA_SHEET ", lambda out: [
                    "--glb", dummy_glb, "--out", out, "--views", "P0:persp:0:15000", "--passes", "beauty",
                    "--res", "32", "--spp", "1", "--fov-mdeg", "40000",
                ]),
            )
            for worker, tag, make_argv in worker_specs:
                with self.subTest(worker=worker):
                    out_dir = os.path.join(ram_root, "post_" + worker.split("_")[0])
                    planted = os.path.join(out_dir, "post_render_link.png")
                    bpy_mod, mathutils_mod, _ = _make_fake_blender()
                    if worker == "bake_worker.py":
                        orig_export = bpy_mod.ops.export_scene.gltf

                        def _plant_export(**kw):
                            orig_export(**kw)
                            if not os.path.lexists(planted):
                                os.symlink(disk_tmp, planted)

                        bpy_mod.ops.export_scene.gltf = _plant_export
                    else:
                        orig_render = bpy_mod.ops.render.render

                        def _plant_render(**kw):
                            orig_render(**kw)
                            if not os.path.lexists(planted):
                                os.symlink(disk_tmp, planted)

                        bpy_mod.ops.render.render = _plant_render

                    out_buf, err_buf = io.StringIO(), io.StringIO()
                    with mock.patch.dict(os.environ, {"VQA_RAMWS_ROOT": ram_root}), \
                            mock.patch.dict(sys.modules, {"bpy": bpy_mod, "mathutils": mathutils_mod}), \
                            mock.patch.object(sys, "argv", [worker, "--"] + make_argv(out_dir)), \
                            contextlib.redirect_stdout(out_buf), \
                            contextlib.redirect_stderr(err_buf):
                        with self.assertRaises(SystemExit) as cm:
                            runpy.run_path(str(ROOT / "bpy_side" / "assets" / worker), run_name="__main__")
                    self.assertEqual(cm.exception.code, 5)
                    self.assertIn("E_RAM_NOT_TMPFS", err_buf.getvalue())
                    self.assertNotIn(tag, out_buf.getvalue())
                    self.assertTrue(os.path.islink(planted))


if __name__ == "__main__":
    unittest.main()
