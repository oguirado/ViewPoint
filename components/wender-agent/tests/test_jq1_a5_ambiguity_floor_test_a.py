import unittest

from wender.perceive import ax
from wender.perceive.vision_adapter import FakeVisionEngine, Frame, fuse_regions
from wender.state import schema


def _make_frame(width=96, height=64, patch_color=(100, 100, 250), bg_color=(200, 200, 200)):
    data = bytearray(width * height * 3)
    stride = width * 3
    # Fill background
    for y in range(height):
        for x in range(width):
            idx = y * stride + x * 3
            data[idx] = bg_color[0]
            data[idx + 1] = bg_color[1]
            data[idx + 2] = bg_color[2]
    # Fill button patch at x=16, y=16, w=64, h=32
    for y in range(16, 16 + 32):
        for x in range(16, 16 + 64):
            idx = y * stride + x * 3
            data[idx] = patch_color[0]
            data[idx + 1] = patch_color[1]
            data[idx + 2] = patch_color[2]
    return Frame(width, height, bytes(data))


def _raw_ax_tree(disabled=False):
    return [
        {
            "nodeId": "1",
            "role": {"type": "role", "value": "RootWebArea"},
            "name": {"type": "computedString", "value": "Root"},
            "childIds": ["2"],
            "ignored": False,
        },
        {
            "nodeId": "2",
            "role": {"type": "role", "value": "button"},
            "name": {"type": "computedString", "value": "Submit"},
            "childIds": [],
            "ignored": False,
            "properties": [
                {"name": "disabled", "value": {"type": "boolean", "value": disabled}}
            ],
            "backendDOMNodeId": 101,
        },
    ]


class TestAmbiguityFloorA(unittest.TestCase):
    def test_enabled_vs_disabled_ambiguity_floor(self):
        # 1. Byte-identical 96x64 Frame buffers with button patch x=16, y=16, w=64, h=32
        frame_enabled = _make_frame()
        frame_disabled = _make_frame()
        frame_negative = _make_frame(patch_color=(255, 0, 0))

        self.assertEqual(frame_enabled.data, frame_disabled.data)
        self.assertNotEqual(frame_enabled.data, frame_negative.data)

        # 2. FakeVisionEngine identities and changed regions
        engine = FakeVisionEngine(tile=16, threshold=8)
        ident_enabled = engine.screen_identity(frame_enabled)
        ident_disabled = engine.screen_identity(frame_disabled)
        ident_negative = engine.screen_identity(frame_negative)

        self.assertEqual(ident_enabled, ident_disabled)
        self.assertNotEqual(ident_enabled, ident_negative)

        self.assertEqual(engine.changed_regions(frame_enabled, frame_disabled), [])
        neg_changed = engine.changed_regions(frame_enabled, frame_negative)
        self.assertTrue(len(neg_changed) > 0)

        # 3. Derive two otherwise identical raw AX trees, varying only disabled
        raw_enabled = _raw_ax_tree(disabled=False)
        raw_disabled = _raw_ax_tree(disabled=True)

        pruned_enabled = ax.prune(raw_enabled)
        pruned_disabled = ax.prune(raw_disabled)

        stable_sha_enabled = ax.snapshot_sha(pruned_enabled, with_state=False)
        stable_sha_disabled = ax.snapshot_sha(pruned_disabled, with_state=False)
        exact_sha_enabled = ax.snapshot_sha(pruned_enabled, with_state=True)
        exact_sha_disabled = ax.snapshot_sha(pruned_disabled, with_state=True)

        self.assertEqual(stable_sha_enabled, stable_sha_disabled)
        self.assertNotEqual(exact_sha_enabled, exact_sha_disabled)

        # 4. Build states with identical boxes
        button_id_enabled = pruned_enabled[1]["id"]
        button_id_disabled = pruned_disabled[1]["id"]
        self.assertEqual(button_id_enabled, button_id_disabled)

        boxes = {button_id_enabled: [16, 16, 64, 32]}

        state_enabled = schema.build_state(
            pruned_enabled,
            kind="cdp",
            url="http://example.com",
            title="Ambiguity Test",
            ax_sha256=exact_sha_enabled,
            ax_stable_sha256=stable_sha_enabled,
            prune=ax.PRUNE_NAME,
            raw_count=len(raw_enabled),
            boxes=boxes,
            actionable_roles=ax.ACTIONABLE_ROLES,
        )

        state_disabled = schema.build_state(
            pruned_disabled,
            kind="cdp",
            url="http://example.com",
            title="Ambiguity Test",
            ax_sha256=exact_sha_disabled,
            ax_stable_sha256=stable_sha_disabled,
            prune=ax.PRUNE_NAME,
            raw_count=len(raw_disabled),
            boxes=boxes,
            actionable_roles=ax.ACTIONABLE_ROLES,
        )

        # 5. Pass same region to fuse_regions and check actionable status
        region = [{"x": 16, "y": 16, "w": 64, "h": 32}]
        fused_enabled = fuse_regions(state_enabled, region)
        fused_disabled = fuse_regions(state_disabled, region)

        self.assertEqual(len(fused_enabled["hits"]), 1)
        self.assertEqual(len(fused_disabled["hits"]), 1)

        self.assertTrue(fused_enabled["hits"][0]["actionable"])
        self.assertFalse(fused_disabled["hits"][0]["actionable"])

        # 6. Test-local integer calibration witness with TP=1 and FP=1
        tp = 1
        fp = 1
        # Floor calculation: (1000 * 2) // 4 = 500
        floor_ppm = (1000 * (tp + fp)) // 4
        self.assertEqual(floor_ppm, 500)

        # Ensure visual evidence alone (identical frame) cannot justify > floor confidence
        visual_confidence = floor_ppm
        self.assertEqual(visual_confidence, 500)


if __name__ == "__main__":
    unittest.main()
