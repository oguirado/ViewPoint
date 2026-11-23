"""Independent source author and end-to-end FACE tests, including saved scene mutants."""
from contextlib import nullcontext
from fractions import Fraction
from io import BytesIO
import json
import math
import os
import shutil
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
import wave

if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from asset_factory import core, domains, proc
from tools.face.blender_fixture import make_scene, install_camera, render
from tools.face.fixture import build, verify_open_scene
from tools.face.fit import read_inputs
from tools.face.evaluation import heldout
from tools.input.evidence import bytes_digest, canonical_bytes, checked_record, InputIssue
from tools.lipsync import build_track


class Catalog:
    def __init__(self, directory):
        self.root = Path(directory)
        self.root.mkdir(exist_ok=True, parents=True)

    def put(self, role, filename, raw):
        path = self.root / role / filename
        path.parent.mkdir(exist_ok=True)
        with path.open("xb") as stream:
            stream.write(raw)
        return bytes_digest(raw)

    def record(self, role, filename, obj):
        return self.put(role, filename, canonical_bytes(obj))

    def read(self, role, pin):
        # Only explicitly owned role directories are used. No filename comes from a request.
        for path in (self.root / role).glob("*"):
            raw = path.read_bytes()
            if bytes_digest(raw) == pin:
                return raw
        return None

    def json(self, filename):
        return core.load_json(self.root / filename, max_bytes=1_048_576)

    def save(self, filename, value):
        with (self.root / filename).open("xb") as stream:
            stream.write(canonical_bytes(value))


def head():
    vertices, parts, faces, materials = [], [], [], []
    def ellipsoid(name, colour, centre, radii):
        base = len(vertices)
        def add(direction):
            vertices.append([round((c + r*d)/100)*100 for c,r,d in zip(centre,radii,direction)])
            parts.append(name)
        def tri(a,b,c):
            faces.append([base+a,base+b,base+c])
            materials.append(colour)
        add((0,0,-1))
        columns, bands = 24, 12
        for row in range(1,bands):
            lat = math.pi * (row/bands - .5)
            for j in range(columns):
                lon = 2*math.pi*j/columns
                add((math.cos(lon)*math.cos(lat),math.sin(lon)*math.cos(lat),math.sin(lat)))
        add((0,0,1))
        for j in range(columns):
            k = (j+1)%columns
            tri(0,1+k,1+j)
            for row in range(bands-2):
                a = 1+row*columns
                tri(a+j,a+k,a+columns+k)
                tri(a+j,a+columns+k,a+columns+j)
            a = 1+(bands-2)*columns
            tri(a+j,a+k,a+columns)
    for params in (
        ("skull",0,(0,4000,6000),(73000,64000,96000)),
        ("chin",0,(0,-25000,-51000),(44000,37000,35000)),
        ("nose",3,(0,-66500,3000),(14500,24000,19000)),
        ("right_eye",2,(-27500,-49300,27000),(15000,8200,8200)),
        ("left_eye",2,(27500,-49300,27000),(15000,8200,8200)),
        ("upper_lip",1,(0,-62000,-32000),(24500,7600,4300)),
        ("lower_lip",1,(0,-61700,-41800),(23500,7800,4500))):
        ellipsoid(*params)
    return dict(schema="face.fixture-mesh/v1", vertices_um=vertices, parts=parts,
                faces=faces, materials=materials)


def camera(name, distance, focal, size=(96, 96), *, heldout=False):
    cosine, sine = (Fraction(60, 61), Fraction(11, 61)) if heldout else (1, 0)
    if name == "front":
        rows = [(cosine, sine, 0), (0, 0, -1), (-sine, cosine, 0)]
    else:
        rows = [(sine, cosine, 0), (0, 0, -1), (-cosine, sine, 0)]
    return {"rotation":[[domains.pair(v) for v in row] for row in rows],
            "translation_um":[[0,1],[0,1],[distance,1]], "intrinsics":[[focal,1],[focal,1],[size[0],2],[size[1],2]]}


def observations(scene, rig, cam, record, parts, allowed, excluded, heldout):
    import bpy
    from bpy_extras.object_utils import world_to_camera_view
    origin = cam.matrix_world.translation
    graph = bpy.context.evaluated_depsgraph_get()
    rows = []
    for part in allowed:
        picked = []
        for v in rig.data.vertices:
            if parts[v.index] != part or v.index in excluded:
                continue
            direction = (v.co-origin).normalized()
            hit, pos, *_ = scene.ray_cast(graph, origin, direction)
            if hit and (pos-v.co).length < 0.000015:
                picked.append(v)
        if not picked:
            raise ValueError("invisible reference landmark group: " + part)
        v = picked[len(picked)//2]
        if heldout:
            projected = world_to_camera_view(scene, cam, v.co)
            xy = [round(projected.x*scene.render.resolution_x*1000),
                  round((1-projected.y)*scene.render.resolution_y*1000)]
        else:
            xyz = [round(c*1_000_000) for c in v.co]
            xy = domains.pinhole(record)(xyz)
        rows.append({"vertex":v.index, "xy_millipx":list(map(domains.pair, xy))})
    return rows


def author(repo):
    import bpy
    template = head()
    mesh_pin = repo.record("face-template", "head.json", template)
    truth = [[round(Fraction(a*k,10)) for a,k in zip(p,(12,9,11))]
             for p in template["vertices_um"]]
    scene, rig = make_scene(template, truth)
    size = [104, 96]
    scene.render.resolution_x, scene.render.resolution_y = size
    fit_views, held_views, excluded = {}, {}, set()
    for split, views, distance, focal in (("fit",fit_views,480000,180),("held",held_views,430000,165)):
        for name in ("front","profile"):
            record = camera(name, distance, focal, size, heldout=split == "held")
            cam = install_camera(scene, split + "-" + name, record)
            if split == "fit":
                parts = ("skull","nose","chin")
            else:
                parts = ("left_eye","nose","upper_lip","lower_lip","chin")
            marks = observations(scene, rig, cam, record, template["parts"], parts,
                                 excluded if split == "held" else set(), split == "held")
            views[name] = dict(camera=record, landmarks=marks)
            if split == "fit":
                excluded.update(row["vertex"] for row in marks)
                directory = repo.root / "face-designated-render"
                directory.mkdir(exist_ok=True)
                path = directory / (name + ".png")
                render(scene, cam, path)
                views[name].update(size=size, observation_image="sha256:"+core.sha256_file(path))
            else:
                views[name].update(reference_images=[], size=size, tile_size=4,
                                   protected_rect=[6,7,90,90])
    scene.camera = scene.objects["held-front"]
    source_scene = repo.root / "face-reference-scene" / "source.blend"
    source_scene.parent.mkdir()
    rig.shape_key_add(name="Basis")

    # A synthetic labelled closed/open utterance, with silence at either end.
    buffer = BytesIO()
    with wave.open(buffer,"wb") as wav:
        wav.setparams((1,2,8000,0,"NONE",""))
        wav.writeframes(b"\0\0"*1000 + b"\x20\x20"*2000 + b"\0\0"*1000)
    wav_bytes = buffer.getvalue()
    timing = b'{"characters":["b","a"],"character_start_times_seconds":[0.125,0.25],"character_end_times_seconds":[0.25,0.375]}'
    repo.put("voice-source","fixture.wav",wav_bytes)
    repo.put("voice-source","timing.json",timing)
    track = build_track(wav_bytes,timing,fps=[8,1],silence_peak_pcm=500,max_frames=4)
    track_pin = repo.record("face-viseme-track","track.json",track)
    # Independent authoring table, direct edits in µm. No candidate keys or fitted coordinates.
    poses = [("rest",0,0,0),("closed",-2000,2000,0),("open",4000,-12200,-10200),("rest",0,0,0)]
    for frame,(pose,upper,lower,chin) in enumerate(poses):
        if track["frames"][frame]["mouth_ppm"][pose] != 1_000_000:
            raise ValueError("authored cue mismatch")
        block = rig.shape_key_add(name=f"authored-{frame}", from_mix=False)
        for v, p, part in zip(block.data,truth,template["parts"], strict=True):
            dz = {"upper_lip":upper,"lower_lip":lower,"chin":chin}.get(part,0)
            v.co.z = (p[2]+dz)/1_000_000
        block.value = 1
        bpy.context.view_layer.update()
        evaluated = rig.evaluated_get(bpy.context.evaluated_depsgraph_get())
        from bpy_extras.object_utils import world_to_camera_view
        for name,view in held_views.items():
            cam = scene.objects["held-"+name]
            marks = []
            for mark in view["landmarks"]:
                idx = mark["vertex"]
                pos = evaluated.matrix_world @ evaluated.data.vertices[idx].co
                xy = world_to_camera_view(scene, cam, pos)
                values = (round(xy.x * size[0] * 1000), round((1-xy.y) * size[1] * 1000))
                marks.append({"vertex": idx, "xy_millipx": list(map(domains.pair, values))})
            view.setdefault("frame_landmarks", []).append(marks)
            directory = repo.root / "face-reference-render"
            directory.mkdir(exist_ok=True)
            output = directory / f"{name}-{frame}.png"
            render(scene,scene.objects["held-"+name],output)
            view["reference_images"].append("sha256:"+core.sha256_file(output))
        block.value = 0
    bpy.context.view_layer.update()
    bpy.ops.wm.save_as_mainfile(filepath=str(source_scene))
    source = core.sha256_file(source_scene)
    fit_pin = repo.record("face-fit-observations","designated.json",
                          dict(schema="face.fit-observations/v1",source_sha256=source,views=fit_views))
    projection = dict(schema="face.source-projection/v1", source_sha256=source, track_pin=track_pin,
                      views={name: view["frame_landmarks"] for name, view in held_views.items()})
    projection_pin = repo.record("face-source-projection", "projection.json", projection)
    held_pin = repo.record("face-independent-observations","heldout.json",
        dict(schema="face.heldout-observations/v1",origin="pre_fit_authored_scene",source_sha256=source,
             views=held_views, source_projection_pin=projection_pin))
    views_pin = repo.record("face-views","views.json",dict(schema="face.fixture-views/v1",
        mesh_sha256=mesh_pin[7:],fit_pin=fit_pin,heldout_pin=held_pin,source_sha256=source,track_pin=track_pin))
    repo.save("args.json",dict(goal_id="face-fixture",mesh_sha256=mesh_pin[7:],face_views_sha256=views_pin[7:]))




def exercise(mode, source, attempt):
    import bpy
    ref, out = Catalog(source), Catalog(attempt)
    args = ref.json("args.json")

    if mode == "build":
        scales = build(args,ref.read,out.root/"head.blend")
        out.save("scales.json",scales)
    elif mode in ("shift","mute-jaw","mute-mouth","mute-closed","blink","resize"):
        mesh, _, _ = read_inputs(args,ref.read)
        rig = bpy.context.scene.objects["FixtureHead"]
        blocks = rig.data.shape_keys.key_blocks
        if mode == "resize":
            bpy.context.scene.render.resolution_x += 8
        elif mode == "blink":
            rig.shape_key_add(name="blink", from_mix=False).value = 1.0
        elif mode == "shift":
            for block in blocks:
                for p,part in zip(block.data,mesh["parts"]):
                    if part in ("upper_lip","lower_lip"):
                        p.co.z += .012
        else:
            # Keep the animated control values. Remove the actual geometry response.
            key = {"mute-jaw":"jaw", "mute-mouth":"open", "mute-closed":"closed"}[mode]
            for p,base in zip(blocks[key].data,blocks["Basis"].data):
                p.co = base.co
        bpy.ops.wm.save_as_mainfile(filepath=str(out.root/"head.blend"))
    elif mode == "verify":
        try:
            result = verify_open_scene(args,ref.read,out.root/"renders")
        except InputIssue as issue:
            out.save("refusal.json", {"rc": issue.rc, "reason": issue.code})
            return issue.rc
        out.save("receipt.json",result)
        return result["rc"]
    else:
        raise ValueError(mode)


def run_blender(mode, source, attempt, scene=None, expect=0):
    flags = [str(scene)] if scene else ["--factory-startup"]
    argv = ["blender", "--background", "--disable-autoexec", *flags, "--threads", "2",
            "--python-exit-code", "2", "--python", str(Path(__file__).resolve()),
            "--", mode, str(source), str(attempt)]
    child = proc.popen(argv, start_new_session=True, runtime_s=90, stderr=subprocess.STDOUT)
    try:
        stdout, _ = proc.communicate(child, timeout=90)
        if child.returncode != expect:
            raise AssertionError((child.returncode, expect, stdout[-2200:].decode(errors="replace")))
    finally:
        if proc.cleanup(child):
            raise AssertionError("Blender cleanup refused")


class ReopenedFixtureTests(unittest.TestCase):
    def test_independent_source_and_reopened_controls(self):
        from jsonschema import Draft202012Validator
        from tools.face.base import assess_images

        self.assertEqual(os.environ.get("AF_PROC_BACKEND"), "pidns")
        retain = os.environ.get("FACE_FIXTURE_RETAIN_DIR")
        if retain:
            Path(retain).mkdir(exist_ok=False)
        with nullcontext(retain) if retain else TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "reference"
            run_blender("author", source, source)
            ref = Catalog(source)
            args = ref.json("args.json")
            # Pin the protected inputs before launching the fitting process.
            before = {p:core.sha256_file(p) for p in source.rglob("*") if p.is_file()}
            good = root / "nominal"
            run_blender("build", source, good)
            run_blender("verify", source, good, good/"head.blend")
            schema = core.load_json(Path(__file__).resolve().parents[1] / "schemas" /
                                    "af.face-fit.v1.schema.json", max_bytes=32768)
            Draft202012Validator.check_schema(schema)
            validator = Draft202012Validator(schema)
            receipt = Catalog(good).json("receipt.json")
            validator.validate(receipt)
            self.assertFalse(validator.is_valid({**receipt, "character_verdict":"PASS"}))
            self.assertFalse(validator.is_valid({**receipt, "fixture_verdict":"FAIL"}))
            self.assertEqual(receipt["scales"], [[6,5], [9,10], [11,10]])
            self.assertEqual(receipt["args"], args)
            self.assertEqual(receipt["source_projection_sha256"],
                             core.sha256_file(source / "face-source-projection/projection.json"))
            self.assertEqual(receipt["camera_separation_policy"],
                             {"basis": "TARGET_UNCALIBRATED", "max_axis_cosine": [984807, 1000000]})
            legacy_policy = {"min_centre_um": 10000, "max_axis_cosine": [999, 1000]}
            self.assertFalse(validator.is_valid({**receipt, "camera_separation_policy": legacy_policy}))
            designated = ref.json("face-fit-observations/designated.json")["views"]
            holdout = ref.json("face-independent-observations/heldout.json")["views"]
            for view in holdout.values():
                axis = tuple(map(domains.rational, view["camera"]["rotation"][2]))
                cosines = [
                    sum(a * domains.rational(b) for a, b in zip(axis, fit["camera"]["rotation"][2]))
                    for fit in designated.values()
                ]
                self.assertEqual(max(cosines), Fraction(60, 61))
            for frame in receipt["frames"]:
                self.assertLessEqual(max(frame["landmark_error_millipx"].values()), 1)
            self.assertEqual(receipt["fixture_verdict"], "PASS")
            self.assertEqual(receipt["character_verdict"], "CANNOT_SEE")
            self.assertEqual(receipt["scene_sha256"], core.sha256_file(good/"head.blend"))
            self.assertLessEqual(max(receipt["landmark_error_millipx"].values()), 1)
            self.assertEqual(len(receipt["frames"]), 4)
            with self.subTest(gate="reference and decision receipts"):
                self.assertIn("source_projection_sha256", receipt)
                self.assertEqual(receipt["i02_decision"], "local_comparison_of_uncalibrated_TARGET_limits")
            with self.subTest(gate="exact reopened key inventory"):
                case = root / "blink"
                run_blender("blink", source, case, good/"head.blend")
                run_blender("verify", source, case, case/"head.blend", expect=5)
                self.assertEqual(Catalog(case).json("refusal.json")["reason"], "reopened_shape_key_set")
            with self.subTest(gate="reopened raster"):
                case = root / "resize"
                run_blender("resize", source, case, good/"head.blend")
                run_blender("verify", source, case, case/"head.blend", expect=4)
                self.assertEqual(Catalog(case).json("refusal.json")["reason"], "reopened_raster_drift")
            for mode in ("shift", "mute-jaw", "mute-mouth", "mute-closed"):
                case = root / mode
                run_blender(mode, source, case, good/"head.blend")
                run_blender("verify", source, case, case/"head.blend", expect=5)
                failing = Catalog(case).json("receipt.json")
                validator.validate(failing)
                self.assertFalse(validator.is_valid({**failing, "rc":0, "fixture_verdict":"PASS", "reasons":[]}))
                self.assertEqual(failing["rc"], 5)
                self.assertEqual(failing["fixture_verdict"], "FAIL")
                for view in ("front", "profile"):
                    self.assertIn("protected_tile_limit." + view, failing["reasons"])
                if mode == "shift":
                    self.assertIn("held_out_landmark_limit", failing["reasons"])
                else:
                    with self.subTest(gate="posed landmarks", mutant=mode):
                        self.assertIn("held_out_landmark_limit", failing["reasons"])
                        affected = 1 if mode == "mute-closed" else 2
                        for view in ("front", "profile"):
                            self.assertLessEqual(failing["frames"][0]["landmark_error_millipx"][view], 1)
                            self.assertGreater(failing["frames"][affected]["landmark_error_millipx"][view], 1)
                    for view in ("front", "profile"):
                        self.assertFalse(failing["protected_metrics"][view][affected]["within_targets"])
                        self.assertTrue(failing["protected_metrics"][view][0]["within_targets"])

            negative_raw = (root / "shift" / "receipt.json").read_bytes()
            negative_pin = bytes_digest(negative_raw)
            for case_name in ("nominal", "mute-mouth"):
                case = Catalog(root / case_name)
                native = case.json("receipt.json")
                def evidence_reader(role, pin):
                    if role == "face-negative-receipt" and pin == negative_pin:
                        return negative_raw
                    if role == "face-candidate-scene":
                        return (case.root / "head.blend").read_bytes()
                    if role == "face-reopened-render":
                        return case.read("renders", pin)
                    return ref.read(role, pin)
                def archive(role, pin, raw):
                    path = case.root / "base-inputs" / role / pin[7:]
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(raw)
                assessed = assess_images(native, read_checked=evidence_reader, negative_pin=negative_pin,
                                         archive=archive)
                self.assertEqual(assessed["base"]["rc"], 3)
                self.assertEqual(assessed["fixture_i02_verdict"], native["fixture_verdict"])
                self.assertEqual(assessed["i02_decision"], "local_comparison_of_uncalibrated_TARGET_limits")
                case.save("base.json", assessed)
                native["protected_metrics"]["front"][0]["worst_tile_error_upper_ppm"] += 1
                with self.assertRaisesRegex(InputIssue, "base_face_metric_drift"):
                    assess_images(native, read_checked=evidence_reader, negative_pin=negative_pin)

            # Use rehashed mutated records so a generic digest mismatch cannot kill the control.
            registration = ref.json("face-views/views.json")
            controls = {}
            designated = ref.json("face-fit-observations/designated.json")
            front_pin = designated["views"]["front"]["observation_image"]
            def without_front(role, key):
                return None if (role,key) == ("face-designated-render",front_pin) else ref.read(role,key)
            with self.assertRaisesRegex(InputIssue, "render_missing"):
                read_inputs(args, without_front)
            with self.assertRaisesRegex(InputIssue, "face_capability_args"):
                read_inputs({**args, "candidate_landmarks": []}, ref.read)
            with self.assertRaisesRegex(InputIssue, "face_reference_scene_missing"):
                heldout(args, lambda role, pin: None if role == "face-reference-scene" else ref.read(role, pin),
                        receipt["scene_sha256"])
            for case, expected in (("no-profile", "missing_required_profile"),
                                   ("from-fit", "face_reference_origin"),
                                   ("relabelled-fit", "face_reference_origin"),
                                   ("reused-camera", "held_out_camera_reused"),
                                   ("near-camera", "held_out_camera_reused"),
                                   ("same-axis-moved-back", "held_out_camera_reused"),
                                   ("quaternion-camera", "held_out_camera_reused"),
                                   ("intrinsics-only", "held_out_camera_reused"),
                                   ("reprojected-fit", "source_projection_drift"),
                                   ("mixed-size", "fixture_common_raster_required"),
                                   ("missing-posed-frame", "source_pose_coverage"),
                                   ("missing-posed-chin", "source_pose_landmark_coverage"),
                                   ("eroded-region", "unprotected_landmark"),
                                   ("unapproved-origin", "missing_face-independent-observations")):
                witness = ref.json("face-independent-observations/heldout.json")
                if case == "no-profile":
                    del witness["views"]["profile"]
                elif case == "from-fit":
                    witness["origin"] = "fit_result"
                elif case in ("relabelled-fit", "unapproved-origin", "reprojected-fit"):
                    from tools.face.fit import fit_request
                    fitted, _ = fit_request(args, ref.read)
                    for view in witness["views"].values():
                        project = domains.pinhole(view["camera"])
                        for mark in view["landmarks"]:
                            mark["xy_millipx"] = list(map(domains.pair, project(fitted[mark["vertex"]])))
                    if case == "relabelled-fit":
                        witness["source_sha256"] = receipt["scene_sha256"]
                elif case in ("reused-camera", "near-camera", "same-axis-moved-back",
                              "quaternion-camera", "intrinsics-only"):
                    designated = ref.json("face-fit-observations/designated.json")
                    cam = designated["views"]["profile"]["camera"]
                    if case == "near-camera":
                        cam["translation_um"][2][0] += 1
                    elif case == "same-axis-moved-back":
                        cam["translation_um"][2][0] += 50000
                    elif case == "quaternion-camera":
                        del cam["rotation"]
                        cam["rotation_quaternion_xyzw"] = [[1,1],[1,1],[-1,1],[1,1]]
                        self.assertEqual(domains.camera_center(cam),
                                         domains.camera_center(camera("profile", 480000, 180)))
                    elif case == "intrinsics-only":
                        cam["intrinsics"][0][0] += 1
                    witness["views"]["profile"]["camera"] = cam
                elif case == "mixed-size":
                    witness["views"]["profile"]["size"][0] += 8
                elif case == "missing-posed-frame":
                    witness["views"]["front"]["frame_landmarks"].pop(1)
                elif case == "missing-posed-chin":
                    witness["views"]["profile"]["frame_landmarks"][2].pop()
                else:
                    witness["views"]["front"]["protected_rect"] = [0,0,4,4]
                raw = canonical_bytes(witness)
                witness_pin = bytes_digest(raw)
                data = canonical_bytes({**registration, "heldout_pin":witness_pin})
                pin = bytes_digest(data)
                altered = {**args,"face_views_sha256":pin[7:]}
                def reader(role, key):
                    if (role,key) == ("face-views",pin):
                        return data
                    if case != "unapproved-origin" and (role,key) == ("face-independent-observations",witness_pin):
                        return raw
                    return ref.read(role,key)
                with self.subTest(witness=case):
                    with self.assertRaisesRegex(InputIssue, expected) as caught:
                        heldout(altered, reader, receipt["scene_sha256"])
                    if case == "same-axis-moved-back":
                        self.assertEqual(caught.exception.rc, 5)
                    controls[case] = {"rc": caught.exception.rc, "reason": caught.exception.code,
                                      "request":altered, "witness_digest":witness_pin}
            # Even a reader approving consistent forged pins cannot substitute a pose
            # table for the Blender projection of the protected source scene.
            forged = Catalog(shutil.copytree(source, root / "forged-reference"))
            witness = forged.json("face-independent-observations/heldout.json")
            projection = forged.json("face-source-projection/projection.json")
            lip = next(i for i, m in enumerate(witness["views"]["front"]["landmarks"])
                       if ref.json("face-template/head.json")["parts"][m["vertex"]] == "lower_lip")
            for table in (witness["views"]["front"]["frame_landmarks"], projection["views"]["front"]):
                table[2][lip]["xy_millipx"][0][0] += 2
            witness["source_projection_pin"] = forged.record("face-source-projection", "forged.json", projection)
            registration["heldout_pin"] = forged.record("face-independent-observations", "forged.json", witness)
            forged_args = {**args, "face_views_sha256": forged.record("face-views", "forged.json", registration)[7:]}
            (forged.root / "args.json").write_bytes(canonical_bytes(forged_args))
            case = root / "forged-replay"
            run_blender("build", forged.root, case)
            run_blender("verify", forged.root, case, case/"head.blend", expect=4)
            controls["forged-replay"] = Catalog(case).json("refusal.json")
            self.assertEqual(controls["forged-replay"]["reason"], "source_replay_projection_drift")
            Catalog(root).save("refusals.json", controls)
            self.assertEqual(before, {p:core.sha256_file(p) for p in source.rglob("*") if p.is_file()})


if __name__ == "__main__":
    mode, source, attempt = sys.argv[sys.argv.index("--") + 1:]
    if mode == "author":
        author(Catalog(source))
    else:
        result = exercise(mode, source, attempt)
        if result:
            sys.exit(result)
