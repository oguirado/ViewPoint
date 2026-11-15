"""Unit tests for the small real pieces of PERCEIVE (atspi, ladder), ACT, REMEMBER and LEARN."""
import unittest

from wender.act import actor
from wender.learn import demonstrations
from wender.perceive import atspi, ladder, vision_bridge
from wender.perceive.vision_adapter import Frame
from wender.remember import program_map, skills


def fake_call(address, dest, path, method, *args, timeout=10):
    if method.endswith("GetChildren"):
        return "([(':1.5', objectpath '/org/a11y/atspi/accessible/root'), (':1.9', '/org/a11y/atspi/accessible/root')],)"
    if method.endswith("GetConnectionUnixProcessID"):
        return {":1.5": "(uint32 100,)", ":1.9": "(uint32 200,)"}[args[0]]
    if args[-1] == "ChildCount":
        return "(<3>,)" if dest == ":1.5" else "(<0>,)"
    if args[-1] == "Name":
        return "(<'gedit'>,)"
    return "(<'GTK'>,)"


class TestAtspi(unittest.TestCase):
    def test_index_and_summary(self):
        idx = atspi.Index(address="unix:path=/fake", call=fake_call)
        self.assertEqual(sorted(idx.by_pid), [100, 200])
        s = atspi.window_summary(100, idx)
        self.assertEqual((s["tree"], s["rung"], s["toolkit"]), (True, 2, "GTK"))
        self.assertFalse(atspi.window_summary(200, idx)["tree"])
        self.assertEqual(atspi.window_summary(7, idx, family_pids=[100])["link"], "by_family_pid")
        self.assertEqual(atspi.window_summary(None, idx)["link"], "no_pid")

    def test_no_bus(self):
        idx = atspi.Index(address="unix:path=/fake", call=lambda *a, **k: None)
        self.assertFalse(idx)
        self.assertIsNone(atspi.window_summary(5, idx)["tree"])


class TestLadderAct(unittest.TestCase):
    def test_choose_rung(self):
        self.assertEqual(ladder.choose_rung({"pixels", "accessibility"}), 2)
        self.assertEqual(ladder.choose_rung({"vision_model", "pointer"}), 4)
        self.assertIsNone(ladder.choose_rung(set()))

    def test_choose_path(self):
        self.assertEqual(actor.choose_path({"coordinates", "ax"}), "ax")
        self.assertEqual(actor.choose_path({"api", "ax"}), "api")

    def test_single_actor(self):
        lock = actor.ActorLock()
        with lock.acquire("a"):
            with self.assertRaises(actor.ActorBusy):
                lock.acquire("b")
        lock.acquire("b").release()

    def test_stubs_raise(self):
        with self.assertRaises(NotImplementedError):
            actor.execute("ax", {})
        with self.assertRaises(ValueError):
            vision_bridge.screen_identity(None)
        frame = Frame(8, 8, bytes(8 * 8 * 3))
        self.assertRegex(vision_bridge.screen_identity(frame), r"^[0-9a-f]{16}$")
        self.assertEqual(vision_bridge.changed_regions(frame, frame), [])


class TestRemember(unittest.TestCase):
    def test_program_map(self):
        m = program_map.ProgramMap("blender")
        a = m.observe("fp1", "s1")
        b = m.observe("fp2", "s2")
        m.add_edge(a, {"path": "ax", "op": "press", "target": "x"}, b, {"tiles": 3})
        m.add_edge(a, {"path": "ax", "op": "press", "target": "x"}, b, {"tiles": 3})
        self.assertEqual(len(m.successors(a)), 1)
        self.assertEqual(m.to_json(), m.to_json())
        with self.assertRaises(KeyError):
            m.add_edge(a, {}, "nope")

    def test_skill_admission(self):
        sk = {"id": "add-cube", "intent": "add a cube", "params": {"size": 2},
              "steps": [{"path": "api", "call": "bpy.ops.mesh.primitive_cube_add"}],
              "checkpoints": [{"after": 0, "expect": "object count +1"}],
              "provenance": {"source": "demo:video-1#seg3"},
              "evidence": {"reproduced": True, "verified_by": "scene diff"}}
        self.assertTrue(skills.admit(sk).startswith("{"))
        for bad in ({"evidence": {"reproduced": False, "verified_by": "x"}}, {"checkpoints": []},
                    {"steps": [{"path": "teleport"}]}):
            with self.assertRaises(skills.SkillRejected):
                skills.admit(dict(sk, **bad))


class TestLearn(unittest.TestCase):
    def test_reproduced_share_counts_only_verified(self):
        segs = [{"reproduced": True, "verified": True}, {"reproduced": True, "verified": False},
                {"reproduced": False}, {"reproduced": True, "verified": True}]
        self.assertEqual(demonstrations.reproduced_share_ppm(segs), 500000)
        self.assertEqual(demonstrations.reproduced_share_ppm([]), 0)


if __name__ == "__main__":
    unittest.main()
