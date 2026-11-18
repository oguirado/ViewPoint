"""D18 INPUT source identity, matte and raw intent regression controls."""
import io
import json
import struct
import unittest

from PIL import Image

from asset_factory.domains import REQUIRED_VIEWS
from gates.input_d18 import inspect_matte, qualify_input
from tools.input.frames import DecodedVideo, Frame, select_frames
from tools.input.evidence import bytes_digest, canonical_bytes
from tools.mesh.raw_admission import inspect_raw


def png(mode, size, data):
    stream = io.BytesIO()
    Image.frombytes(mode, size, bytes(data)).save(stream, format="PNG")
    return stream.getvalue()


class ProtectedStore(dict):
    def add(self, role, value):
        data = canonical_bytes(value)
        pin = bytes_digest(data)
        self[role, pin] = data
        return pin

    def read(self, role, pin):
        return self.get((role, pin))


def view_camera(view):
    across = {"left": (0, 1, 0), "right": (0, -1, 0), "back": (-1, 0, 0)}.get(view, (1, 0, 0))
    right, up, depth = across, (0, 0, -1), (-across[1], across[0], 0)
    return {"rotation": [[[v, 1] for v in row] for row in (right, up, depth)],
            "translation_um": [[0, 1], [0, 1], [4, 1]], "intrinsics": [[2, 1], [2, 1], [4, 1], [4, 1]]}


class Views(unittest.TestCase):
    def setUp(self):
        self.source = bytes_digest(b"synthetic-video")
        self.frames = tuple(Frame(i, (i, 24), bytes([i + 1]) * 8 * 8 * 3)
                            for i in range(len(REQUIRED_VIEWS)))
        self.video = DecodedVideo(self.source, 0, 8, 8, self.frames)
        self.store = ProtectedStore()
        self.rights = self.store.add("input-rights", {
            "schema": "af.input-rights/v1", "source_digest": self.source,
            "allowed_uses": ["held_out"],
        })
        self.split = self.store.add("input-split", {
            "schema": "af.input-split/v1", "calibration": [bytes_digest(b"another-video")],
            "held_out": [self.source],
        })
        self.rows = [{
            "id": view, "view": view, "index": f.index, "pts": list(f.pts),
            "frame_digest": f.digest, "camera": view_camera(view),
            "crop": {"rect": [0, 0, 8, 8], "size": [8, 8]},
            "pose_digest": bytes_digest(b"pose"), "regions": {name: [2, 2, 4, 4] for name in {"face": ("face",), "hands": ("left_hand", "right_hand")}.get(view, ("subject",))},
            "landmarks": [{"id": "witness", "world_um": [0, 0, 0],
                           "pixel_millipx": [4000, 4000], "uncertainty_millipx": 100}],
            "light_samples": [{"signal": [4, 5], "dark": [1, 10], "reflectance": [1, 2]},
                              {"signal": [4, 5], "dark": [1, 10], "reflectance": [1, 2]}],
        } for view, f in zip(REQUIRED_VIEWS, self.frames)]
        for view, sign in (("left", 1), ("right", -1)):
            self.row(view)["landmarks"] += [
                {"id": f"{view}_ear", "world_um": [sign, 0, 0],
                 "pixel_millipx": [4000, 4000], "uncertainty_millipx": 100},
                {"id": "nose_tip", "world_um": [0, -1, 0],
                 "pixel_millipx": [4000 - sign * 500, 4000], "uncertainty_millipx": 100},
            ]
        self.plan = {"schema": "af.input-view-plan/v1", "source_digest": self.source,
                     "stream": 0, "raster": [8, 8], "phase": "held_out",
                     "rights_digest": self.rights, "split_digest": self.split,
                     "invented_details": [], "observations": self.rows}

    def run_gate(self, **kwargs):
        pin = self.store.add("input-plan", self.plan)
        return select_frames(self.video, plan_digest=pin, read_checked=self.store.read, **kwargs)

    def row(self, view):
        return next(row for row in self.rows if row["view"] == view)

    def test_registered_views_remain_diagnostic_without_base(self):
        result = self.run_gate()
        self.assertEqual(result["source_rc"], 0, result)
        self.assertEqual(set(result["selection"]["selected"]), set(REQUIRED_VIEWS))
        self.assertEqual(result["metrics"]["coverage"], [[1, 1], [1, 1]])
        self.assertEqual(qualify_input(result)["disposition"], "DIAGNOSTIC")

    def test_front_labelled_back(self):
        requests = {view: view for view in REQUIRED_VIEWS}
        requests["back"] = "front"
        self.assertEqual(self.run_gate(choices=requests)["source_rc"], 5)

    def test_replayed_rear_different_pts_same_pixels(self):
        rear = self.row("back")
        frames = list(self.video.frames)
        old = frames[rear["index"]]
        frames[rear["index"]] = Frame(old.index, old.pts, frames[0].rgb)
        self.video = DecodedVideo(self.source, 0, 8, 8, tuple(frames))
        rear["frame_digest"] = frames[0].digest
        self.assertEqual(self.run_gate()["source_rc"], 5)

    def test_same_timestamp_cannot_cover_two_views(self):
        row = self.row("back")
        frames = list(self.video.frames)
        back = frames[row["index"]]
        row["pts"] = list(frames[0].pts)
        frames[back.index] = Frame(back.index, frames[0].pts, back.rgb)
        self.video = DecodedVideo(self.source, 0, 8, 8, tuple(frames))
        self.assertEqual(self.run_gate()["source_rc"], 5)

    def test_identical_metrics_do_not_make_frame_selections_interchangeable(self):
        frame = Frame(len(self.frames), (1, 1), bytes(8 * 8 * 3))
        self.video = DecodedVideo(self.source, 0, 8, 8, (*self.frames, frame))
        self.rows.append({**self.row("front"), "id": "other_front", "index": frame.index,
                          "pts": list(frame.pts), "frame_digest": frame.digest})
        first = self.run_gate()
        second = self.run_gate(choices={view: "other_front" if view == "front" else view
                                        for view in REQUIRED_VIEWS})
        self.assertEqual(first["source_rc"], 0, first)
        self.assertEqual(second["source_rc"], 0, second)
        self.assertEqual(first["metrics"], second["metrics"])
        self.assertNotEqual(first["subject_digest"], second["subject_digest"])

    def test_back_camera_relabelled_even_with_different_pixels(self):
        self.row("back")["camera"] = view_camera("front")
        self.assertIn("camera_view_direction_mismatch", self.run_gate()["reason_codes"])

    def test_cropped_hand(self):
        self.row("hands")["crop"]["rect"] = [0, 0, 3, 8]
        self.assertEqual(self.run_gate()["source_rc"], 5)

    def test_face_without_registration(self):
        self.row("face")["camera"] = None
        self.assertEqual(self.run_gate()["source_rc"], 3)

    def test_calibration_capture_cannot_release_or_be_rebranded_as_production(self):
        self.plan["phase"] = "calibration"
        self.plan["rights_digest"] = self.store.add("input-rights", {
            "schema": "af.input-rights/v1", "source_digest": self.source,
            "allowed_uses": ["calibration", "production"],
        })
        self.plan["split_digest"] = self.store.add("input-split", {
            "schema": "af.input-split/v1", "calibration": [self.source],
            "held_out": [bytes_digest(b"separate validation capture")],
        })
        receipt = self.run_gate()
        self.assertEqual(receipt["source_rc"], 0, receipt)
        self.assertEqual(qualify_input(receipt)["reasons"], ["calibration_capture_only"])
        self.plan["phase"] = "production"
        self.assertIn("production_reuses_calibration_or_held_out_capture",
                      self.run_gate()["reason_codes"])

    def test_reflected_camera_refused(self):
        self.row("face")["camera"]["rotation"][0][0] = [-1, 1]
        self.assertIn("non_rigid_camera", self.run_gate()["reason_codes"])

    def test_face_landmark_outside_uncertainty(self):
        self.row("face")["landmarks"][0]["pixel_millipx"][0] = 4200
        self.assertEqual(self.run_gate()["source_rc"], 5)

    def test_missing_view_rights_and_stale_timestamp(self):
        back = self.row("back")
        self.rows.remove(back)
        self.assertEqual(self.run_gate()["source_rc"], 3)
        self.rows.append(back)
        back["pts"] = [100, 24]
        self.assertEqual(self.run_gate()["source_rc"], 4)
        del self.store["input-rights", self.rights]
        self.assertEqual(self.run_gate()["source_rc"], 3)


class Mattes(unittest.TestCase):
    matte_source = "fixture"  # The raster and alpha mask are constructed in setUp.

    def setUp(self):
        self.size = (8, 8)
        self.rgb = [200, 160, 120] * 64
        self.alpha = [0] * 64
        for y in range(2, 6):
            for x in range(2, 6):
                self.alpha[y * 8 + x] = 255
        self.alpha[3 * 8 + 3] = 0
        self.alpha[2 * 8 + 3] = 128
        self.expected = bytes(255 if a else 0 for a in self.alpha)
        self.original = png("RGB", self.size, self.rgb)
        self.reference = png("L", self.size, self.expected)
        self.intent = {
            "schema": "af.input-matte-intent/v1", "original_digest": bytes_digest(self.original),
            "matte_source": self.matte_source,
            "mask_digest": bytes_digest(self.reference), "support_min": 1,
            "regions": {"border": [[0, 0, 8, 1]], "holes": [[3, 3, 4, 4]],
                        "extremities": [[3, 2, 4, 3]], "fine_structure": []},
            "boundary_iou_min": [1, 1], "max_encoded_bytes": 10000, "max_pixels": 100,
            "invented_details": [],
        }
        views = Views()
        views.setUp()
        raster = 10
        frame_rgb = bytes([200, 160, 120]) * raster * raster
        frames = tuple(Frame(i, f.pts, frame_rgb if i == 0 else bytes([i]) * raster * raster * 3)
                       for i, f in enumerate(views.frames))
        views.video = DecodedVideo(views.source, 0, raster, raster, frames)
        views.plan["raster"] = [raster, raster]
        for row, frame in zip(views.rows, frames):
            row["frame_digest"] = frame.digest
        views.row("front")["crop"]["rect"] = [1, 1, 9, 9]
        selection = views.run_gate()
        self.assertEqual(selection["source_rc"], 0, selection)
        self.store = views.store
        self.store["selected-frame-rgb", frames[0].digest] = frame_rgb
        self.intent.update(view="front", frame_to_original_transform=[[1, 1], [1, 1], [-1, 1], [-1, 1]],
                           selection_digest=self.store.add("input-selection", selection["selection"]))

    def payload(self, passes=1):
        # The unresized upstream hard-alpha box here is [2,2,4,4).
        pixels = bytearray()
        for y in range(2, 4):
            for x in range(2, 4):
                colors = [200, 160, 120]
                for _ in range(passes):
                    colors = [color * self.alpha[y * 8 + x] // 255 for color in colors]
                pixels.extend(colors)
        return png("RGB", (2, 2), pixels)

    def admit(self, submitted):
        pixels = [v for i, alpha in enumerate(self.alpha)
                  for v in (*self.rgb[i*3:i*3+3], alpha)]
        master = png("RGBA", self.size, pixels)
        pin = self.store.add("matte-intent", self.intent)
        return inspect_matte(self.original, master, submitted, self.reference,
                             intent_digest=pin, read_checked=self.store.read,
                             matte_source=self.matte_source)

    def test_exact_crop_is_diagnostic(self):
        result = self.admit(self.payload())
        self.assertEqual(result["source_rc"], 0, result)
        self.assertEqual(result["metrics"]["alpha_iou"], [[1, 1], [1, 1]])
        self.assertEqual(qualify_input(result)["verdict"], "CANNOT_SEE")

    def test_prepare_submits_the_verified_single_transform(self):
        from gates.input_d18 import prepare_matte
        pixels = [v for i, alpha in enumerate(self.alpha)
                  for v in (*self.rgb[i*3:i*3+3], alpha)]
        master = png("RGBA", self.size, pixels)
        pin = self.store.add("matte-intent", self.intent)
        submitted, receipt = prepare_matte(self.original, master, self.reference,
                                           intent_digest=pin, read_checked=self.store.read,
                                           matte_source=self.matte_source)
        self.assertEqual(receipt["source_rc"], 0, receipt)
        self.assertEqual(receipt, self.admit(submitted))
        self.assertEqual(receipt["subject_digest"], bytes_digest(submitted))
        self.assertEqual(receipt["generator"]["required_preprocess_image"], False)

    def test_intent_must_declare_its_matte_source(self):
        del self.intent["matte_source"]
        result = self.admit(self.payload())
        self.assertEqual((result["source_rc"], result["reason_codes"]), (3, ["matte_intent_source_missing"]))

    def test_intent_candidate_is_required_only_for_model_source(self):
        original = self.intent
        for fields, rc, reason in (
            ({"matte_source": "unknown"}, 2, "invalid_matte_source"),
            ({"matte_source": "birefnet"}, 3, "matte_intent_candidate_missing"),
            ({"matte_source": "birefnet", "birefnet_candidate_sha256": "bad"}, 2, "invalid_matte_intent_candidate"),
            ({"matte_source": "fixture", "birefnet_candidate_sha256": "b" * 64}, 2, "invalid_af.input-matte-intent/v1"),
        ):
            with self.subTest(fields=fields):
                self.intent = original | fields
                result = self.admit(self.payload())
                self.assertEqual((result["source_rc"], result["reason_codes"]), (rc, [reason]))

    def test_opaque_alpha(self):
        self.alpha[:] = [255] * len(self.alpha)
        self.assertEqual(self.admit(self.payload())["reason_codes"], ["alpha_opaque_or_empty"])

    def test_lost_finger(self):
        self.alpha[2 * 8 + 3] = 0
        self.assertIn("protected_extremity_missing", self.admit(self.payload())["reason_codes"])

    def test_filled_hole(self):
        self.alpha[3 * 8 + 3] = 255
        self.assertIn("protected_hole_filled", self.admit(self.payload())["reason_codes"])

    def test_master_finger_that_upstream_crop_removes_is_refused(self):
        self.intent["regions"]["extremities"] = [[5, 4, 6, 5]]
        self.assertIn("protected_extremity_lost_in_generator_crop",
                      self.admit(self.payload())["reason_codes"])

    def test_protected_border_pixel_cannot_hide_in_global_iou(self):
        self.alpha[1] = 128
        self.assertIn("protected_border_filled", self.admit(self.payload())["reason_codes"])

    def test_double_premultiplication(self):
        self.assertEqual(self.admit(self.payload(2))["reason_codes"], ["submitted_derivative_mismatch"])


def glb(vertices, indices):
    data = b"".join(struct.pack("<fff", *xyz) for xyz in vertices)
    first = len(data)
    data += bytes(indices)
    doc = {"asset": {"version": "2.0"}, "scene": 0, "scenes": [{"nodes": [0]}],
           "nodes": [{"mesh": 0}],
           "meshes": [{"primitives": [{"attributes": {"POSITION": 0}, "indices": 1}]}],
           "buffers": [{"byteLength": len(data)}],
           "bufferViews": [{"buffer": 0, "byteLength": first},
                           {"buffer": 0, "byteOffset": first, "byteLength": len(indices)}],
           "accessors": [{"bufferView": 0, "componentType": 5126, "count": len(vertices), "type": "VEC3"},
                         {"bufferView": 1, "componentType": 5121, "count": len(indices), "type": "SCALAR"}]}
    js = json.dumps(doc).encode()
    js += b" " * (-len(js) % 4)
    data += b"\x00" * (-len(data) % 4)
    return (b"glTF" + struct.pack("<IIII", 2, 28 + len(js) + len(data), len(js), 0x4e4f534a)
            + js + struct.pack("<II", len(data), 0x004e4942) + data)


class RawIntent(unittest.TestCase):
    def setUp(self):
        self.store = ProtectedStore()
        self.intent = {
            "schema": "af.raw-intent/v1", "forbidden_boxes_um": [
                [[-900000, -500000, -100000], [-400000, 500000, 100000]]],
            "component_counts": {"0:0": {"role": "body-and-thin-feature", "count": 1}},
            "extent_ranges_um": [[500000, 3000000], [0, 3000000], [500000, 3000000]],
            "max_glb_bytes": 20000, "max_faces": 30, "max_box_checks": 30, "max_degenerate_faces": 0,
        }

    def gate(self, vertices, indices):
        raw = glb(vertices, indices)
        pin = self.store.add("raw-intent", self.intent)
        registration = self.store.add("raw-registration", {
            "schema": "af.raw-registration/v1", "subject_digest": bytes_digest(raw),
            "intent_digest": pin, "frame": "right-handed-z-up-front-negative-y",
            "roles": {"0:0": "body-and-thin-feature"},
        })
        return inspect_raw(raw, intent_digest=pin, registration_digest=registration, read_checked=self.store.read)

    def test_thin_legitimate_part_and_attached_slab(self):
        v = [(0, 0, 0), (1, 0, 0), (0, 1, 0), (0.015625, 1, 0.015625)]
        result = self.gate(v, [0, 1, 2, 0, 2, 3])
        self.assertEqual(result["source_rc"], 0, result)
        self.assertEqual(result["metrics"]["component_count"], [[1, 1], [1, 1]])
        v += [(-1, 0, 0.25), (-1, 0, -0.25)]
        result = self.gate(v, [0, 1, 2, 0, 2, 3, 0, 4, 5])
        self.assertEqual(result["reason_codes"], ["forbidden_support_intersection"])

    def test_detached_slab_and_unresolved_solid_containment(self):
        detached = [(0, 0, 0), (1, 0, 0), (0, 1, 0),
                    (-1, 0, 0.25), (-0.5, 0, -0.25), (-0.5, 0.125, 0)]
        self.assertEqual(self.gate(detached, [0, 1, 2, 3, 4, 5])["source_rc"], 5)
        tetrahedron = [(0, 0, 0), (1, 0, 0), (0, 1, 0), (0, 0, -1)]
        self.intent["forbidden_boxes_um"] = [[[100000, 100000, 100000], [200000, 200000, 200000]]]
        result = self.gate(tetrahedron, [0, 1, 2, 0, 3, 1, 0, 2, 3, 1, 3, 2])
        self.assertEqual(result["source_rc"], 3, result)
        self.assertIn("forbidden_volume_containment_unresolved", result["reason_codes"])

    def test_work_budget_stops_raw_decode(self):
        self.intent["max_faces"] = 1
        v = [(0, 0, 0), (1, 0, 0), (0, 1, 0), (0.015625, 1, 0.015625)]
        self.assertEqual(self.gate(v, [0, 1, 2, 0, 2, 3])["source_rc"], 6)

    def test_wrong_scale(self):
        v = [(0, 0, 0), (0.125, 0, 0), (0, 0.125, 0)]
        self.assertEqual(self.gate(v, [0, 1, 2])["reason_codes"], ["extent_outside_intent"])


if __name__ == "__main__":
    unittest.main()
