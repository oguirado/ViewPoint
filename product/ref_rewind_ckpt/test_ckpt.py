"""R1 tests: digest v2, ops scripts through the real F1 bridge core + F1 broker, checkpoint store.
No Blender: `FakeBpy` models the bpy surface the scripts use (its .blend is a pickle of the data).

    python3 -m unittest -v test_ckpt          (from this directory)

Negative controls are the `test_nc_*` tests: each runs the same check body against a mutant
(check removed) and asserts that the check goes red.
"""
import copy
import json
import os
import pickle
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "ref_rewind"))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "ref_broker"))
import bridge  # noqa: E402
import broker as B  # noqa: E402
import fakes  # noqa: E402
import schema as RW  # noqa: E402
import digest as D  # noqa: E402
import ops  # noqa: E402
from store import Store  # noqa: E402

NONCE = "n0ncer1"
I4 = [[1.0, 0.0, 0.0, 0.0], [0.0, 1.0, 0.0, 0.0], [0.0, 0.0, 1.0, 0.0], [0.0, 0.0, 0.0, 1.0]]


# ---------------------------------------------------------------- fake bpy
class O(object):
    def __init__(self, **kw):
        self.__dict__.update(kw)


class _ViewLayer(O):
    def update(self):
        pass


def _socket(ident, value, linked=False):
    return O(identifier=ident, default_value=value, is_linked=linked)


def _material(name):
    bsdf = O(name="Principled BSDF", bl_idname="ShaderNodeBsdfPrincipled", mute=False,
             inputs=[_socket("Base Color", [0.8, 0.8, 0.8, 1.0]), _socket("Roughness", 0.5)],
             outputs=[_socket("BSDF", None)])
    out = O(name="Material Output", bl_idname="ShaderNodeOutputMaterial", mute=False,
            inputs=[_socket("Surface", None, True)], outputs=[])
    link = O(from_node=bsdf, from_socket=bsdf.outputs[0], to_node=out, to_socket=out.inputs[0])
    return O(name=name, diffuse_color=[0.8, 0.8, 0.8, 1.0], metallic=0.0, roughness=0.5, use_nodes=True,
             blend_method="OPAQUE", node_tree=O(nodes=[bsdf, out], links=[link]))


def _object(name, mat=None, z=0.0):
    mw = copy.deepcopy(I4)
    mw[2][3] = z
    me = O(name=name, vertices=[(0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0)])
    return O(name=name, type="MESH", matrix_world=mw, data=me, material_slots=[O(material=mat)] if mat else [],
             modifiers=[], parent=None, location=[0.0, 0.0, z], rotation_euler=[0.0, 0.0, 0.0],
             scale=[1.0, 1.0, 1.0], hide_viewport=False, hide_render=False)


def _data():
    mat = _material("Material")
    cube = _object("Cube", mat)
    col = O(name="Collection", objects=[cube], children=[])
    root = O(name="Scene Collection", objects=[], children=[col])
    scene = O(name="Scene", collection=root, frame_start=1, frame_end=250, frame_current=1,
              render=O(engine="BLENDER_EEVEE", resolution_x=1920, resolution_y=1080, resolution_percentage=100,
                       fps=24, filepath="/tmp/", film_transparent=False))
    r3d = O(view_perspective="PERSP", is_perspective=True, view_location=[0.0, 0.0, 0.0],
            view_rotation=[0.716, 0.439, 0.291, 0.459], view_distance=17.9)
    area = O(type="VIEW_3D", spaces=O(active=O(region_3d=r3d, lens=50.0)))
    win = O(screen=O(areas=[O(type="OUTLINER", spaces=O(active=O())), area]), view_layer=_ViewLayer())
    return O(collections=[col], scenes=[scene], materials=[mat], window_managers=[O(windows=[win])])


class FakeBpy(object):
    """bpy surface used by digest.py and the ops scripts. A file load drops non-persistent timers,
    as Blender does."""

    def __init__(self):
        self.data = _data()
        self.timers = []                 # [(fn, persistent)]
        self.loads = 0
        fb = self

        class _wm(object):
            @staticmethod
            def save_as_mainfile(filepath, copy=False, check_existing=True, compress=False):
                assert copy, "a checkpoint must not retarget bpy.data.filepath"
                with open(filepath, "wb") as f:
                    pickle.dump(fb.data, f)
                return {"FINISHED"}

            @staticmethod
            def open_mainfile(filepath, load_ui=True, use_scripts=False):
                with open(filepath, "rb") as f:
                    try:
                        new = pickle.load(f)
                    except Exception as e:
                        raise RuntimeError("not a blend file: %s" % e)
                if not load_ui:
                    new.window_managers = fb.data.window_managers
                fb.data = new
                fb.loads += 1
                fb.timers = [t for t in fb.timers if t[1]]
                return {"FINISHED"}

        class _timers(object):
            @staticmethod
            def register(fn, first_interval=0.0, persistent=False):
                fb.timers.append((fn, persistent))

        self.ops = O(wm=_wm)
        self.app = O(timers=_timers)
        self.context = O(view_layer=_ViewLayer())

    def view(self):
        return self.data.window_managers[0].windows[0].screen.areas[1].spaces.active.region_3d

    def tick(self):
        for fn, _ in list(self.timers):
            fn()


class Rig(object):
    """Temp RT + F1 broker + F1 bridge core registered as a FakeBpy timer (persistent or not)."""

    def __init__(self, persistent=True, cap=3.0):
        self.rt = tempfile.mkdtemp(prefix="vprw1_test_")
        self.bpy = FakeBpy()
        shots = os.path.join(self.rt, "_shots")
        os.makedirs(shots)
        self.broker = B.Broker(self.rt, "fakejob", NONCE, vision=fakes.FakeVision(shots, {}), job_cap_s=cap,
                               poll_s=0.01, differ=fakes.fake_differ)
        self.bpy.app.timers.register(self._poll, first_interval=1.0, persistent=persistent)   # bridge._register
        self._stop = threading.Event()
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()
        self.store = Store(self.rt)
        self.n = 0

    def _poll(self):
        bridge.poll_once(self.rt, NONCE, {"bpy": self.bpy}, digest=lambda: bridge.scene_digest(self.bpy),
                         log=lambda m: None)

    def _loop(self):
        while not self._stop.is_set():
            self.bpy.tick()
            time.sleep(0.005)

    def ctl(self, kind, script, *parts):
        self.n += 1
        return RW.dispatch_control(self.broker, RW.control_job_id(kind, *(parts or (self.n,))), script)

    def mark(self, pid, pinned=()):
        r = self.ctl("mark", ops.mark_script(self.store.path_for(pid)), pid)
        assert r["ok"], r
        return self.store.admit(pid, 0, RW.SCENE_EPOCH_0, r["result"]["digest"], 0, pinned=pinned)

    def close(self):
        self._stop.set()
        self.thread.join(2)
        self.broker.close()
        shutil.rmtree(self.rt, ignore_errors=True)


# ---------------------------------------------------------------- digest v2
class DigestV2(unittest.TestCase):
    def setUp(self):
        self.bpy = FakeBpy()
        self.d0 = D.digest_v2(self.bpy)

    def delta(self, digest_fn=D.digest_v2):
        return RW.digest_delta(self.d0, digest_fn(self.bpy))

    def test_namespaces_and_schema(self):
        RW.check_digest(self.d0)
        self.assertEqual(sorted(self.d0), ["col:Collection", "col:Scene Collection", "mat:Material", "render", "ui:view3d"])
        self.assertEqual(D.DIGEST_V, RW.DIGEST_VERSION)

    def test_deterministic_and_negative_zero(self):
        self.bpy.data.collections[0].objects[0].location[0] = -0.0
        self.assertEqual(self.delta(), [])

    def test_superset_of_f1(self):
        f1 = bridge.scene_digest(self.bpy)
        self.assertTrue(set(f1) <= set(self.d0))
        ob = self.bpy.data.collections[0].objects[0]
        ob.matrix_world[0][3] = 2.0                  # what F1 sees, v2 sees
        self.assertNotEqual(bridge.scene_digest(self.bpy), f1)
        self.assertEqual(self.delta(), ["col:Collection"])

    def _f4(self, mutate, key):
        """State outside the F1 digest (finding F4) must move exactly one v2 key."""
        f1 = bridge.scene_digest(self.bpy)
        mutate(self.bpy)
        self.assertEqual(bridge.scene_digest(self.bpy), f1, "F1 digest is expected to be blind here")
        self.assertEqual(self.delta(), [key])

    def test_material_node_value(self):
        self._f4(lambda b: b.data.materials[0].node_tree.nodes[0].inputs[0].default_value.__setitem__(0, 0.1), "mat:Material")

    def test_material_link(self):
        self._f4(lambda b: b.data.materials[0].node_tree.links.pop(), "mat:Material")

    def test_material_setting(self):
        self._f4(lambda b: setattr(b.data.materials[0], "roughness", 0.9), "mat:Material")

    def test_modifier_added_and_param(self):
        ob = self.bpy.data.collections[0].objects[0]
        self._f4(lambda b: ob.modifiers.append(O(name="Bevel", type="BEVEL", width=0.1)), "col:Collection")
        self.d0 = D.digest_v2(self.bpy)
        self._f4(lambda b: setattr(ob.modifiers[0], "width", 0.3), "col:Collection")
        self.d0 = D.digest_v2(self.bpy)
        ob.modifiers[0].show_expanded = False            # editor cosmetics stay outside
        self.assertEqual(self.delta(), [])

    def test_mesh_vertex_moved(self):
        self._f4(lambda b: b.data.collections[0].objects[0].data.vertices.__setitem__(0, (0.0, 0.0, 9.0)), "col:Collection")

    def test_render_setting(self):
        self._f4(lambda b: setattr(b.data.scenes[0].render, "resolution_x", 1234), "render")

    def _view_check(self, digest_fn):
        self.bpy.view().view_rotation = [1.0, 0.0, 0.0, 0.0]
        self.assertEqual(self.delta(digest_fn), ["ui:view3d"])
        self.d0 = digest_fn(self.bpy)
        self.bpy.view().view_perspective = "ORTHO"
        self.assertEqual(self.delta(digest_fn), ["ui:view3d"])

    def test_view3d(self):
        self._view_check(D.digest_v2)

    def test_nc_view3d_removed_goes_red(self):
        def mutant(bpy):                              # digest without the view (the F1 gap)
            d = D.digest_v2(bpy)
            d["ui:view3d"] = "0"
            return d
        with self.assertRaises(AssertionError):
            self._view_check(mutant)

    def test_no_gui_is_a_value(self):
        self.bpy.data.window_managers = []
        d = D.digest_v2(self.bpy)
        RW.check_digest(d)
        self.assertNotEqual(d["ui:view3d"], self.d0["ui:view3d"])


# ---------------------------------------------------------------- ops scripts through bridge + broker
class OpsScripts(unittest.TestCase):
    def setUp(self):
        self.rig = Rig()
        self.addCleanup(self.rig.close)

    def test_scripts_are_data(self):
        for s in (ops.mark_script("/x/it's \"p1\".blend"), ops.restore_script("/x/p1.blend", False), ops.digest_script()):
            compile(s, "s.py", "exec")
            self.assertNotIn("import bpy", s)          # bpy comes from the bridge namespace
            self.assertNotIn("import schema", s)        # nothing of this repository is imported in Blender
        self.assertEqual(ops.RESULT_PREFIX, RW.RESULT_PREFIX)

    def test_digest_job(self):
        r = self.rig.ctl("digest", ops.digest_script())
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["result"]["op"], "digest")
        self.assertEqual(r["result"]["digest"], D.digest_v2(self.rig.bpy))
        self.assertEqual(r["changed"], [])               # read-only for the F1 digest too

    def test_mark_writes_point(self):
        p = self.rig.store.path_for("p1")
        r = self.rig.ctl("mark", ops.mark_script(p), "p1")
        self.assertTrue(r["ok"], r)
        res = r["result"]
        self.assertEqual((res["op"], res["path"], res["bytes"]), ("mark", p, os.path.getsize(p)))
        self.assertEqual(res["digest"], D.digest_v2(self.rig.bpy))
        self.assertEqual(sorted(os.listdir(os.path.dirname(p))), ["p1.blend"])     # no tmp left behind

    def _edit(self):
        b = self.rig.bpy
        b.data.collections[0].objects.append(_object("Wrong", z=5.0))
        b.data.materials[0].roughness = 0.1
        b.data.scenes[0].render.resolution_x = 640
        b.view().view_distance = 3.0

    def _rollback_check(self, restore_script):
        rec = self.rig.mark("p1")
        self._edit()
        self.assertEqual(len(RW.digest_delta(rec["digest"], D.digest_v2(self.rig.bpy))), 4)
        r = self.rig.ctl("restore", restore_script(self.rig.store.verify("p1"), True), "p1", "e2")
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["result"]["op"], "restore")
        self.assertTrue(RW.digest_match(r["result"]["digest"], rec["digest"]), "digest after restore != point")
        self.assertEqual(D.digest_v2(self.rig.bpy), rec["digest"])

    def test_restore_equals_point(self):
        self._rollback_check(ops.restore_script)

    def test_nc_noop_restore_goes_red(self):
        def noop(path, load_ui=True):                   # answers like a restore, loads nothing
            return ops._PRELUDE + "print(_rw_result({'op':'restore','path':%r,'load_ui':True,'digest':digest_v2(bpy)}))\n" % path
        with self.assertRaises(AssertionError):
            self._rollback_check(noop)

    def test_load_ui_false_keeps_view(self):
        rec = self.rig.mark("p1")
        self.rig.bpy.view().view_distance = 3.0
        r = self.rig.ctl("restore", ops.restore_script(self.rig.store.verify("p1"), False), "p1", "e2")
        self.assertEqual(RW.digest_delta(rec["digest"], r["result"]["digest"]), ["ui:view3d"])
        r = self.rig.ctl("restore", ops.restore_script(self.rig.store.verify("p1"), True), "p1", "e3")
        self.assertTrue(RW.digest_match(rec["digest"], r["result"]["digest"]))

    def _blender_side_sha(self, sha_of):
        rec = self.rig.mark("p1")
        self._edit()
        before = D.digest_v2(self.rig.bpy)
        path = self.rig.store.path_for("p1")
        other = _data()
        other.scenes[0].render.resolution_x = 77
        good = pickle.dumps(other)                      # a loadable file with other content
        with open(path, "wb") as f:
            f.write(good)
        r = self.rig.ctl("restore", ops.restore_script(path, True, sha256=sha_of(rec)), "p1", "e2")
        self.assertFalse(r["ok"])
        self.assertEqual(r["code"], "JOB_ERROR")
        self.assertEqual(self.rig.bpy.loads, 0)
        self.assertEqual(D.digest_v2(self.rig.bpy), before)

    def test_restore_rechecks_sha_inside_blender(self):
        self._blender_side_sha(lambda rec: rec["blend_sha256"])

    def test_nc_blender_side_sha_removed_goes_red(self):
        with self.assertRaises(AssertionError):
            self._blender_side_sha(lambda rec: None)     # no sha passed -> swapped file is loaded

    def test_restore_missing_file(self):
        r = self.rig.ctl("restore", ops.restore_script(os.path.join(self.rig.rt, "ckpt", "p9.blend")), "p9", "e2")
        self.assertEqual((r["ok"], r["code"]), (False, "JOB_ERROR"))


class TimerSurvival(unittest.TestCase):
    """rw-build section 7 / L16: the restore answer chain needs the bridge timer registered persistent."""

    def _chain(self, persistent):
        rig = Rig(persistent=persistent, cap=0.6)
        try:
            rec = rig.mark("p1")
            r = rig.ctl("restore", ops.restore_script(rig.store.verify("p1")), "p1", "e2")
            self.assertTrue(r["ok"], r)                  # the job that loads still answers
            r = rig.ctl("digest", ops.digest_script())  # the next one needs a live timer
            self.assertTrue(r["ok"], "no answer after the load: %s" % r["code"])
            self.assertTrue(RW.digest_match(r["result"]["digest"], rec["digest"]))
        finally:
            rig.close()

    def test_persistent_bridge_answers_after_load(self):
        self._chain(True)

    def test_nc_non_persistent_bridge_goes_red(self):
        with self.assertRaises(AssertionError):
            self._chain(False)


# ---------------------------------------------------------------- store
class StoreTests(unittest.TestCase):
    def setUp(self):
        self.rt = tempfile.mkdtemp(prefix="vprw1_store_")
        self.addCleanup(shutil.rmtree, self.rt, True)

    def put(self, st, pid, i=None, size=100, pinned=(), write=True):
        i = int(pid[1:]) if i is None else i
        if write:
            with open(st.path_for(pid), "wb") as f:
                f.write(bytes([i % 251]) * size)
        return st.admit(pid, i, RW.SCENE_EPOCH_0, {"col:A": "%04x" % i, "render": "aa", "ui:view3d": "bb"}, i, pinned=pinned)

    def exists(self, st, pid):
        return os.path.isfile(st.path_for(pid))

    def test_admit_get_verify_and_record(self):
        st = Store(self.rt)
        rec = self.put(st, "p0")
        RW.validate_record(rec)
        self.assertEqual(rec["blend_sha256"], RW.sha256_file(st.path_for("p0")))
        self.assertEqual((rec["bytes"], rec["digest_version"]), (100, 2))
        self.assertEqual(st.verify("p0"), st.path_for("p0"))
        self.assertEqual(st.get("p0"), rec)
        self.assertEqual(st.points(), ["p0"])
        self.assertEqual(RW.replay([rec]).violations, [])

    def test_missing_and_bad_ids(self):
        st = Store(self.rt)
        with self.assertRaises(RW.CkptError) as c:
            self.put(st, "p0", write=False)
        self.assertEqual(c.exception.code, RW.CKPT_MISSING)
        with self.assertRaises(RW.CkptError) as c:
            st.verify("p7")
        self.assertEqual(c.exception.code, RW.CKPT_MISSING)
        with self.assertRaises(RW.SchemaError):
            st.path_for("../p0")
        self.put(st, "p0")
        with self.assertRaises(RW.SchemaError):
            self.put(st, "p0")                           # a point id is never reused

    def _tamper_check(self, **kw):
        st = Store(self.rt, **kw)
        self.put(st, "p0")
        with open(st.path_for("p0"), "r+b") as f:        # flip one bit of one byte
            f.seek(50)
            b = f.read(1)
            f.seek(50)
            f.write(bytes([b[0] ^ 1]))
        with self.assertRaises(RW.CkptError) as c:
            st.verify("p0")
        self.assertEqual(c.exception.code, RW.CKPT_CORRUPT)

    def test_tampered_blend_refused(self):
        self._tamper_check()

    def test_nc_sha_check_removed_goes_red(self):
        with self.assertRaises(AssertionError):
            self._tamper_check(check_sha=False)

    def test_dedup(self):
        st = Store(self.rt)
        r0 = self.put(st, "p0")
        r1 = self.put(st, "p1", i=0)                     # same digest, file written by a mark job
        self.assertEqual(r1["dedup_of"], "p0")
        self.assertEqual((r1["blend_sha256"], r1["bytes"]), (r0["blend_sha256"], r0["bytes"]))
        self.assertFalse(self.exists(st, "p1"))
        r2 = self.put(st, "p2", i=0, write=False)        # same digest, mark job skipped
        self.assertEqual(r2["dedup_of"], "p0")
        self.assertEqual(st.dedup_candidate(r0["digest"]), "p2")
        self.assertIsNone(st.dedup_candidate({"col:A": "ffff"}))
        self.assertEqual(st.verify("p2"), st.path_for("p0"))
        self.assertEqual((st.files(), st.bytes_used()), (["p0"], 100))
        RW.validate_record(r2)
        r3 = self.put(st, "p3")                          # different digest -> own file
        self.assertNotIn("dedup_of", r3)

    def test_dedup_only_against_newest(self):
        st = Store(self.rt)
        self.put(st, "p0")
        self.put(st, "p1")
        r = self.put(st, "p2", i=0)                      # equals p0, not the previous point
        self.assertNotIn("dedup_of", r)

    def _ring_check(self, pin_base):
        """last_good and the base of an open transaction survive any number of admits."""
        st = Store(self.rt, slots=3)
        self.put(st, "p0")
        self.put(st, "p1", pinned=("p0",))               # p1 = base of a transaction that stays open
        last_good = "p1"
        for n in range(2, 9):
            pid = "p%d" % n
            self.put(st, pid, pinned=(last_good, "p1") if pin_base else (last_good,))
            last_good = pid                              # committed
            self.assertLessEqual(len(st.files()), 3)
            self.assertEqual(st.verify(last_good), st.path_for(last_good))
            self.assertTrue(self.exists(st, "p1"), "open-transaction base evicted at %s" % pid)
            st.verify("p1")
        self.assertEqual(st.points(), ["p1", "p7", "p8"])
        self.assertFalse(self.exists(st, "p0"))
        with self.assertRaises(RW.CkptError) as c:
            st.verify("p0")
        self.assertEqual(c.exception.code, RW.CKPT_MISSING)

    def test_ring_never_drops_last_good_or_open_base(self):
        self._ring_check(True)

    def test_nc_unpinned_base_goes_red(self):
        with self.assertRaises(AssertionError):
            self._ring_check(False)

    def _stall_check(self, **kw):
        """Unconfirmed points are never overwritten: the ring stalls with CKPT_BUDGET."""
        st = Store(self.rt, slots=3, **kw)
        self.put(st, "p0")
        self.put(st, "p1", pinned=("p0",))               # S-1 = 2 unconfirmed points next to last_good p0
        self.put(st, "p2", pinned=("p0",))
        with self.assertRaises(RW.CkptError) as c:
            self.put(st, "p3", pinned=("p0",))
        self.assertEqual(c.exception.code, RW.CKPT_BUDGET)
        self.assertEqual(st.points(), ["p0", "p1", "p2"])
        self.assertTrue(all(self.exists(st, p) for p in ("p0", "p1", "p2")))
        self.assertFalse(self.exists(st, "p3"))          # the refused file is discarded
        self.put(st, "p4", pinned=("p2",))               # p2 committed -> p0, p1 become evictable
        self.assertEqual(st.points(), ["p1", "p2", "p4"])

    def test_stall_guard_refuses_overwrite(self):
        self._stall_check()

    def test_nc_stall_guard_removed_goes_red(self):
        with self.assertRaises(AssertionError):
            self._stall_check(stall_guard=False)

    def test_byte_budget(self):
        st = Store(self.rt, budget_bytes=250)
        self.put(st, "p0")
        self.put(st, "p1", pinned=("p0",))
        with self.assertRaises(RW.CkptError) as c:       # 300 > 250 and nothing is evictable
            self.put(st, "p2", pinned=("p0",))
        self.assertEqual(c.exception.code, RW.CKPT_BUDGET)
        self.assertEqual(st.bytes_used(), 200)
        self.put(st, "p3", pinned=("p1",))               # p0 confirmed-old -> evicted for bytes
        self.assertEqual((st.points(), st.bytes_used()), (["p1", "p3"], 200))
        with self.assertRaises(RW.CkptError):            # one file alone over the budget
            self.put(st, "p4", size=999, pinned=("p3",))

    def test_implicit_last_good_without_pins(self):
        st = Store(self.rt, slots=3)
        for n in range(6):                               # FakeStore-style caller: pinned=()
            self.put(st, "p%d" % n)
        self.assertEqual(st.points(), ["p3", "p4", "p5"])

    def test_manifest_survives_restart(self):
        st = Store(self.rt, slots=3)
        for n in range(5):
            self.put(st, "p%d" % n)
        self.put(st, "p5", i=4)                          # dedup row
        with open(st.p_manifest, "a") as f:
            f.write('{"op":"admit","rec":{"point_')       # torn tail of a crashed writer
        st2 = Store(self.rt, slots=3)
        self.assertEqual(st2.points(), st.points())
        self.assertEqual([st2.get(p) for p in st2.points()], [st.get(p) for p in st.points()])
        self.assertEqual(st2.verify("p5"), st.path_for("p4"))
        self.assertEqual(sorted(n for n in os.listdir(st.dir) if n.endswith(".blend")),
                         ["p2.blend", "p3.blend", "p4.blend"])

    def _torn_then_admit(self, **kw):
        """xr-core R1-1: a writer crashed mid-row; the next process admits a point; a third one loads."""
        st = Store(self.rt, slots=4)
        self.put(st, "p0")
        with open(st.p_manifest, "a") as f:
            f.write('{"op":"admit","rec":{"point_')       # torn tail, no newline
        st2 = Store(self.rt, slots=4, **kw)
        self.assertEqual(st2.points(), ["p0"])
        self.put(st2, "p1")                               # first append after the torn tail
        st3 = Store(self.rt, slots=4, **kw)
        self.assertEqual(st3.points(), ["p0", "p1"], "the admit after a torn tail must survive a reload")
        self.assertEqual(st3.verify("p1"), st3.path_for("p1"))
        with open(st.p_manifest, "rb") as f:
            for line in f.read().splitlines():
                json.loads(line)                          # no glued or torn row is left behind

    def test_admit_after_torn_tail_survives_reload(self):
        self._torn_then_admit()

    def test_nc_no_tail_heal_goes_red(self):
        with self.assertRaises(AssertionError):
            self._torn_then_admit(heal_tail=False)

    def test_unterminated_complete_row_is_not_half_applied(self):
        st = Store(self.rt, slots=4)
        self.put(st, "p0")
        self.put(st, "p1")
        with open(st.p_manifest, "rb") as f:
            data = f.read()
        with open(st.p_manifest, "wb") as f:
            f.write(data[:-1])                            # last row complete but its newline is missing
        st2 = Store(self.rt, slots=4)
        self.assertEqual(st2.points(), ["p0"])            # memory and disk agree: the row is cut, not kept
        self.assertEqual(Store(self.rt, slots=4).points(), ["p0"])

    def test_same_interface_as_fake_store(self):
        for name in ("path_for", "admit", "verify", "get", "points"):
            self.assertTrue(callable(getattr(Store, name)) and callable(getattr(fakes.FakeStore, name)))


class BaseUntouched(unittest.TestCase):
    def test_f1_and_r0_not_edited(self):
        top = os.path.dirname(os.path.dirname(HERE))
        for ref, path in (("b255057", "product/ref_broker"), ("4d33836", "product/ref_rewind")):
            p = subprocess.run(["git", "diff", "--stat", ref, "--", path], cwd=top, capture_output=True, text=True)
            if p.returncode != 0:
                self.skipTest("git unavailable: %s" % p.stderr[:80])
            self.assertEqual(p.stdout.strip(), "", "%s was edited" % path)


if __name__ == "__main__":
    unittest.main()
