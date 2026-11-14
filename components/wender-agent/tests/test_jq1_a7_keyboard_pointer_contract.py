"""LOCAL PROPOSED REFERENCE MODEL for router C5; no production executor repair.

Only ax.prune, ax.snapshot_sha and AX name/identity helpers below are repository
code. All planners, verification and state-machine rules are local PROPOSAL.
Mock records command order and simulated acknowledgments only: these tests do
not validate a CdpPipe, browser, Radix, focus, event handling or application
submission. Inputs are local data; no sleeps, network, screenshots or frames.

One LiteralSubmission instance represents one insertion_id. A production caller
would need a session ledger preserving that identity across retries/restarts;
constructing a new object does not implement persistence or idempotent CDP.
"""

import unittest
from unittest.mock import Mock, call

from wender.perceive import ax


class PolicyError(ValueError):
    """The local proposal refuses an invalid or unsafe transition."""


KEYS = {
    "Enter": ("Enter", 13), "Escape": ("Escape", 27),
    "Tab": ("Tab", 9), "Backspace": ("Backspace", 8),
    "ArrowLeft": ("ArrowLeft", 37), "ArrowUp": ("ArrowUp", 38),
    "ArrowRight": ("ArrowRight", 39), "ArrowDown": ("ArrowDown", 40),
}
MODIFIERS = {"Alt": 1, "Ctrl": 2, "Meta": 4, "Shift": 8}


def integer(value, minimum=None, maximum=None):
    if (type(value) is not int or (minimum is not None and value < minimum)
            or (maximum is not None and value > maximum)):
        raise PolicyError("integer outside the proposed bounds")
    return value


def modifier_mask(names):
    if not isinstance(names, (list, tuple)):
        raise PolicyError("modifier names must be a list or tuple")
    mask = 0
    for name in names:
        if not isinstance(name, str) or name not in MODIFIERS:
            raise PolicyError("unknown modifier")
        if mask & MODIFIERS[name]:
            raise PolicyError("duplicate modifier")
        mask |= MODIFIERS[name]
    return mask


def press_events(key, modifiers=0, profile="nontext"):
    if not isinstance(key, str) or key not in KEYS:
        raise PolicyError("unknown canonical key")
    integer(modifiers, 0, 15)
    if not isinstance(profile, str) or profile not in ("nontext", "enter_text"):
        raise PolicyError("unknown key profile")
    if profile == "enter_text" and key != "Enter":
        raise PolicyError("enter_text is restricted to Enter")
    code, virtual_key = KEYS[key]
    common = {"key": key, "code": code, "windowsVirtualKeyCode": virtual_key,
              "modifiers": modifiers}
    down = dict(common, type="rawKeyDown")
    if profile == "enter_text":
        down.update(type="keyDown", text="\r")
    return [("Input.dispatchKeyEvent", down),
            ("Input.dispatchKeyEvent", dict(common, type="keyUp"))]


def literal_events(text):
    if not isinstance(text, str):
        raise PolicyError("literal text must be a string")
    return [("Input.insertText", {"text": text})]


def vector(value, size):
    if not isinstance(value, (list, tuple)) or len(value) != size:
        raise PolicyError("invalid geometry shape")
    return tuple(integer(item) for item in value)


def pointer_events(op, box, viewport, origin=None, *, box_generation,
                   current_generation, irreversible=False):
    """Integer viewport CSS pixels; fresh box after scroll/layout observation.

    Signed box origins are allowed if their floor-rounded centre is visible.
    A generation is caller-supplied local freshness evidence, not a CDP field.
    """
    if not isinstance(op, str) or op not in ("hover", "lpath_click"):
        raise PolicyError("unknown pointer operation")
    if type(irreversible) is not bool or irreversible:
        raise PolicyError("coordinate policy requires reversible action")
    if integer(box_generation, 1) != integer(current_generation, 1):
        raise PolicyError("stale box; refresh after scroll or layout change")
    x, y, w, h = vector(box, 4)
    vw, vh = vector(viewport, 2)
    integer(w, 1), integer(h, 1), integer(vw, 1), integer(vh, 1)
    centre = (x + w // 2, y + h // 2)

    def visible(point):
        if not (0 <= point[0] < vw and 0 <= point[1] < vh):
            raise PolicyError("point outside viewport CSS pixels")

    visible(centre)
    points = [centre]
    if op == "lpath_click":
        start = vector(origin, 2)
        visible(start)
        points = [start, (centre[0], start[1]), centre]
    elif origin is not None:
        raise PolicyError("hover does not consume an origin")
    moves = []
    for point in points:
        if not moves or point != moves[-1]:
            moves.append(point)
    events = [("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": px, "y": py})
              for px, py in moves]
    if op == "lpath_click":
        for kind in ("mousePressed", "mouseReleased"):
            events.append(("Input.dispatchMouseEvent", {
                "type": kind, "x": centre[0], "y": centre[1],
                "button": "left", "clickCount": 1}))
    return events


def verify_click(initial, changed, target_id, expect=None):
    """Proposed verification, consuming the REAL repository hash unmocked.

    None means changed-snapshot fallback; explicit semantics never fall through.
    Empty/malformed expectations fail closed. This does not dispatch a click.
    """
    pre_sha = ax.snapshot_sha(initial, True)
    post_sha = ax.snapshot_sha(changed, True)

    def result(verified, reason):
        return {"verified": int(verified), "reason": reason,
                "pre_sha": pre_sha, "post_sha": post_sha}

    original = next((n for n in initial if n["id"] == target_id), None)
    if original is None:
        return result(False, "initial_target_missing")
    target = next((n for n in changed if n["id"] == target_id), None)
    if expect is None:
        if pre_sha == post_sha:
            return result(False, "unchanged_snapshot")
        return result(True, "changed_navigation" if target is None else "changed_snapshot")
    if not isinstance(expect, dict) or not expect or set(expect) - {"target_name", "target_states"}:
        return result(False, "invalid_expectation")
    states = expect.get("target_states", {})
    name = expect.get("target_name")
    if (not isinstance(states, dict)
            or ("target_states" in expect and not states)
            or any(k not in ax.STATE_ATTRS or not isinstance(v, str) for k, v in states.items())
            or ("target_name" in expect and not isinstance(name, str))
            or (not states and name is None)):
        return result(False, "invalid_expectation")
    if target is None and name is not None:
        renamed = dict(original, name=ax.normalize_name(name))
        renamed_id = ax.stable_id(renamed)
        target = next((n for n in changed if n["id"] == renamed_id), None)
    if target is None:
        return result(False, "expected_target_missing")
    actual = dict(target["attrs"])
    matched = all(actual.get(k) == v for k, v in states.items())
    matched = matched and (name is None or target["name"] == ax.normalize_name(name))
    return result(matched, "semantic_match" if matched else "semantic_mismatch")


class OneShotDispatch:
    """A model-owned command sequence can be attempted once, including failure."""

    def __init__(self, send):
        self.send = send
        self.started = False
        self.attempted = self.acknowledged = 0
        self.outcome = "NEW"

    def run(self, events):
        if self.started:
            raise PolicyError("dispatch replay refused")
        self.started = True  # reserve before the first possibly ambiguous send
        for method, params in events:
            self.attempted += 1
            try:
                self.send(method, params)
            except Exception:
                self.outcome = "UNCERTAIN"
                return False
            self.acknowledged += 1
        self.outcome = "ACKNOWLEDGED"
        return True


class LiteralSubmission:
    """One insertion identity, bounded local reads, one submission ATTEMPT.

    Readiness and acceptance dictionaries are injected evidence, not protocol
    replies. A nonempty evidence string identifies the site-specific acceptance
    witness. An acknowledgment cannot manufacture it or guarantee submission.
    """

    def __init__(self, send, target, text, max_prepare_reads=2, max_acceptance_reads=3):
        if not isinstance(target, str) or not target:
            raise PolicyError("target must be a nonempty string")
        literal_events(text)
        self.max_prepare_reads = integer(max_prepare_reads, 1, 3)
        self.max_acceptance_reads = integer(max_acceptance_reads, 1, 3)
        self.target, self.text = target, text
        self.state = "READY"
        self.prepare_reads = self.acceptance_reads = self.submission_count = 0
        self.last_generation = self.accepted_generation = 0
        self.insertion = OneShotDispatch(send)
        self.submission = OneShotDispatch(send)

    def insert(self, readiness_observations):
        if self.state != "READY":
            raise PolicyError("duplicate insertion refused")
        self.state = "PREPARING"  # reserve the identity and its finite read budget
        try:
            observations = iter(readiness_observations)
        except Exception:
            self.state = "PREPARE_EXHAUSTED"
            return False
        for _ in range(self.max_prepare_reads):
            self.prepare_reads += 1  # failed reads also consume an attempt
            try:
                evidence = next(observations)
            except StopIteration:
                break
            except Exception:
                continue
            if not isinstance(evidence, dict):
                continue
            generation = evidence.get("generation")
            fresh = type(generation) is int and generation > self.last_generation
            if fresh:
                self.last_generation = generation
            if (not fresh or evidence.get("target") != self.target
                    or evidence.get("focused") is not True or evidence.get("editable") is not True):
                continue
            self.state = "INSERT_PENDING"
            ok = self.insertion.run(literal_events(self.text))
            self.state = "INSERT_ACKED" if ok else "UNCERTAIN"
            return ok
        self.state = "PREPARE_EXHAUSTED"
        return False

    def observe_acceptance(self, evidence):
        if self.state != "INSERT_ACKED":
            raise PolicyError("acceptance observation outside its bounded phase")
        self.acceptance_reads += 1
        accepted = False
        if isinstance(evidence, dict):
            generation = evidence.get("generation")
            fresh = type(generation) is int and generation > self.last_generation
            if fresh:
                self.last_generation = generation
            accepted = (fresh and evidence.get("target") == self.target
                        and evidence.get("text") == self.text
                        and evidence.get("accepted") is True and evidence.get("focused") is True
                        and isinstance(evidence.get("evidence"), str)
                        and bool(evidence["evidence"].strip()))
        if accepted:
            self.accepted_generation = self.last_generation
            self.state = "ACCEPTED"
        elif self.acceptance_reads == self.max_acceptance_reads:
            self.state = "ACCEPTANCE_EXHAUSTED"
        return bool(accepted)

    def submit(self, current_generation):
        if self.state != "ACCEPTED":
            raise PolicyError("Enter requires accepted exact text")
        if type(current_generation) is not int or current_generation != self.accepted_generation:
            self.state = "EVIDENCE_STALE"
            raise PolicyError("acceptance evidence no longer current")
        self.submission_count += 1  # number of attempts, never an application-success claim
        self.state = "SUBMITTING"
        ok = self.submission.run(press_events("Enter", profile="enter_text"))
        self.state = "SUBMISSION_ACKED" if ok else "UNCERTAIN"
        return ok

    def timeout(self):
        if self.state in ("INSERT_ACKED", "ACCEPTED"):
            self.state = "ACCEPTANCE_EXHAUSTED"


def local_nodes(pressed="false", page="Page", target_name="Options", editor_value=""):
    """Local raw nodes passed through the real prune function, with no CDP."""
    return ax.prune([
        {"nodeId": "root", "role": {"value": "RootWebArea"}, "name": {"value": page},
         "childIds": ["target", "editor"]},
        {"nodeId": "target", "role": {"value": "button"}, "name": {"value": target_name},
         "backendDOMNodeId": 2, "properties": [{"name": "pressed", "value": {"value": pressed}}]},
        {"nodeId": "editor", "role": {"value": "textbox"}, "name": {"value": "Editor"},
         "backendDOMNodeId": 3, "value": {"value": editor_value}},
    ])


def ready(generation=1, **overrides):
    return dict({"target": "editor", "focused": True, "editable": True,
                 "generation": generation}, **overrides)


def acceptance(generation=2, **overrides):
    return dict({"target": "editor", "text": "literal", "accepted": True, "focused": True,
                 "generation": generation, "evidence": "local app model accepted exact value"}, **overrides)


class ScriptedReadiness:
    """Local iterator whose failures consume entries; it performs no I/O."""

    def __init__(self, values):
        self.values = iter(values)
        self.reads = 0

    def __iter__(self):
        return self

    def __next__(self):
        self.reads += 1
        value = next(self.values)
        if isinstance(value, Exception):
            raise value
        return value


class TestProposedKeyAndLiteralPolicy(unittest.TestCase):
    def test_canonical_key_table_and_nontext_event_pairs(self):
        expected = {"Enter": 13, "Escape": 27, "Tab": 9, "Backspace": 8,
                    "ArrowLeft": 37, "ArrowUp": 38, "ArrowRight": 39, "ArrowDown": 40}
        for key, virtual_key in expected.items():
            with self.subTest(key=key):
                events = press_events(key)
                self.assertEqual([p["type"] for _, p in events], ["rawKeyDown", "keyUp"])
                for method, params in events:
                    self.assertEqual(method, "Input.dispatchKeyEvent")
                    self.assertEqual((params["key"], params["code"], params["windowsVirtualKeyCode"]),
                                     (key, key, virtual_key))
                    self.assertNotIn("text", params)

    def test_enter_text_profile_is_two_events_with_carriage_return_only_down(self):
        down, up = press_events("Enter", profile="enter_text")
        self.assertEqual(down, ("Input.dispatchKeyEvent", {
            "type": "keyDown", "key": "Enter", "code": "Enter", "windowsVirtualKeyCode": 13,
            "modifiers": 0, "text": "\r"}))
        self.assertEqual(up, ("Input.dispatchKeyEvent", {
            "type": "keyUp", "key": "Enter", "code": "Enter", "windowsVirtualKeyCode": 13,
            "modifiers": 0}))

    def test_modifier_bits_are_composed_without_inventing_modifier_events(self):
        self.assertEqual([modifier_mask([name]) for name in ("Alt", "Ctrl", "Meta", "Shift")],
                         [1, 2, 4, 8])
        self.assertEqual(modifier_mask(["Ctrl", "Shift"]), 10)
        self.assertEqual(modifier_mask(list(MODIFIERS)), 15)
        self.assertEqual(modifier_mask([]), 0)
        for mask in range(16):
            events = press_events("Tab", modifiers=mask)
            self.assertEqual(len(events), 2)
            self.assertTrue(all(p["modifiers"] == mask for _, p in events))

    def test_invalid_keys_profiles_modifiers_are_policy_errors(self):
        for key in ("", "Return", "enter", "A", "Enter\n", None, 13, [], {}):
            with self.subTest(key=key), self.assertRaises(PolicyError):
                press_events(key)
        for profile in ("text", None, [], {}):
            with self.subTest(profile=profile), self.assertRaises(PolicyError):
                press_events("Enter", profile=profile)
        for mask in (-1, 16, True, False, 1.0, "2", None, []):
            with self.subTest(mask=mask), self.assertRaises(PolicyError):
                press_events("Enter", modifiers=mask)
        for names in (["Control"], ["Ctrl", "Ctrl"], [[]], "Alt", None):
            with self.subTest(names=names), self.assertRaises(PolicyError):
                modifier_mask(names)
        with self.assertRaises(PolicyError):
            press_events("Tab", profile="enter_text")

    def test_literal_insert_is_one_exact_command_and_no_per_key_events(self):
        for text in ("", "a\nb\r\t", "quotes ' \" and \\", "Español 🧪 e\u0301"):
            with self.subTest(text=text):
                send = Mock(return_value={})
                self.assertTrue(OneShotDispatch(send).run(literal_events(text)))
                send.assert_called_once_with("Input.insertText", {"text": text})
        for text in (None, 2, False, [], {}):
            with self.subTest(text=text), self.assertRaises(PolicyError):
                literal_events(text)


class TestProposedPointerPolicy(unittest.TestCase):
    def plan(self, op="lpath_click", box=(90, 70, 20, 20), origin=(20, 20), **overrides):
        args = {"box_generation": 3, "current_generation": 3}
        args.update(overrides)
        return pointer_events(op, box, (200, 120), origin, **args)

    def test_right_menu_is_orthogonal_and_final_move_press_release_share_hit(self):
        events = self.plan()
        self.assertEqual([(p["x"], p["y"]) for _, p in events],
                         [(20, 20), (100, 20), (100, 80), (100, 80), (100, 80)])
        self.assertEqual([p["type"] for _, p in events],
                         ["mouseMoved", "mouseMoved", "mouseMoved", "mousePressed", "mouseReleased"])
        for _, params in events[-2:]:
            self.assertEqual((params["button"], params["clickCount"]), ("left", 1))

    def test_left_menu_mirrors_the_elbow_without_a_diagonal(self):
        events = self.plan(box=(10, 70, 20, 20), origin=(100, 20))
        points = [(p["x"], p["y"]) for _, p in events[:3]]
        self.assertEqual(points, [(100, 20), (20, 20), (20, 80)])
        self.assertTrue(all(a[0] == b[0] or a[1] == b[1] for a, b in zip(points, points[1:])))

    def test_hover_is_move_only_and_centre_uses_integer_floor_in_css_pixels(self):
        self.assertEqual(self.plan("hover", box=(10, 20, 3, 5), origin=None),
                         [("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": 11, "y": 22})])
        self.assertEqual(self.plan("hover", box=(-2, 0, 5, 5), origin=None)[0][1]["x"], 0)
        self.assertEqual(len(self.plan(origin=(100, 80))), 3)

    def test_stale_box_requires_refresh_before_planning_new_coordinates(self):
        with self.assertRaises(PolicyError):
            self.plan(box_generation=2, current_generation=3)
        events = self.plan(box=(140, 80, 20, 20), box_generation=4, current_generation=4)
        self.assertEqual((events[-1][1]["x"], events[-1][1]["y"]), (150, 90))

    def test_geometry_shape_type_extent_viewport_and_irreversible_rejections(self):
        for box in (None, [], (0, 0, 1), (0, 0, 1, 1, 1), (True, 0, 2, 2),
                    (0, 0, 2.0, 2), (0, 0, 0, 2), (0, 0, -1, 2),
                    (200, 0, 2, 2), (-10, 0, 2, 2)):
            with self.subTest(box=box), self.assertRaises(PolicyError):
                self.plan(box=box)
        for origin in (None, [1], [1.0, 2], [False, 2], [-1, 2], [200, 2]):
            with self.subTest(origin=origin), self.assertRaises(PolicyError):
                self.plan(origin=origin)
        for viewport in ((0, 120), (200, False), (200, 120.0), (200,)):
            with self.subTest(viewport=viewport), self.assertRaises(PolicyError):
                pointer_events("hover", (0, 0, 2, 2), viewport,
                               box_generation=1, current_generation=1)
        for generation in (0, True, 1.0, "1"):
            with self.subTest(generation=generation), self.assertRaises(PolicyError):
                self.plan(box_generation=generation)
        for irreversible in (True, 1, None):
            with self.subTest(irreversible=irreversible), self.assertRaises(PolicyError):
                self.plan(irreversible=irreversible)

    def test_partial_pointer_sequence_is_uncertain_and_never_replayed(self):
        send = Mock(side_effect=[{}, {}, {}, {}, TimeoutError("release acknowledgment lost")])
        dispatch = OneShotDispatch(send)
        self.assertFalse(dispatch.run(self.plan()))
        self.assertEqual((dispatch.attempted, dispatch.acknowledged, dispatch.outcome), (5, 4, "UNCERTAIN"))
        with self.assertRaises(PolicyError):
            dispatch.run(self.plan())
        self.assertEqual(send.call_count, 5)


class TestProposedVerificationWithRealAxHash(unittest.TestCase):
    def setUp(self):
        self.initial = local_nodes()
        self.target = self.initial[1]["id"]

    def test_unchanged_click_with_none_expectation_fails(self):
        result = verify_click(self.initial, local_nodes(), self.target, expect=None)
        self.assertEqual((result["verified"], result["reason"]), (0, "unchanged_snapshot"))
        self.assertEqual(result["pre_sha"], result["post_sha"])
        self.assertEqual(result["pre_sha"], ax.snapshot_sha(self.initial, True))
        self.assertEqual(len(result["pre_sha"]), 64)

    def test_real_hashed_state_change_verifies_with_no_expectation(self):
        changed = local_nodes(pressed="true")
        result = verify_click(self.initial, changed, self.target)
        self.assertEqual((result["verified"], result["reason"]), (1, "changed_snapshot"))
        self.assertNotEqual(result["pre_sha"], result["post_sha"])
        self.assertEqual(result["post_sha"], ax.snapshot_sha(changed, True))

    def test_navigation_missing_target_requires_changed_hash_and_none_expectation(self):
        navigation = ax.prune([{"nodeId": "new", "role": {"value": "RootWebArea"},
                                "name": {"value": "Next page"}}])
        result = verify_click(self.initial, navigation, self.target)
        self.assertEqual((result["verified"], result["reason"]), (1, "changed_navigation"))
        self.assertNotEqual(result["pre_sha"], result["post_sha"])
        for expect in ({"target_states": {"pressed": "true"}}, {"target_name": "Options"}, {}):
            with self.subTest(expect=expect):
                self.assertEqual(verify_click(self.initial, navigation, self.target, expect)["verified"], 0)

    def test_explicit_semantics_override_hash_change_and_can_match_unchanged_state(self):
        wrong = verify_click(self.initial, local_nodes(page="Different"), self.target,
                             {"target_states": {"pressed": "true"}})
        self.assertNotEqual(wrong["pre_sha"], wrong["post_sha"])
        self.assertEqual(wrong["verified"], 0)
        for changed in (self.initial, local_nodes(pressed="true")):
            result = verify_click(self.initial, changed, self.target, {"target_name": "Options"})
            self.assertEqual((result["verified"], result["reason"]), (1, "semantic_match"))
        right = verify_click(self.initial, local_nodes(pressed="true"), self.target,
                             {"target_states": {"pressed": "true"}})
        self.assertEqual(right["verified"], 1)

    def test_explicit_rename_uses_real_normalization_and_stable_identity(self):
        renamed = local_nodes(target_name=" New   options ")
        self.assertNotEqual(renamed[1]["id"], self.target)
        self.assertEqual(verify_click(self.initial, renamed, self.target,
                                     {"target_name": "New options"})["verified"], 1)

    def test_empty_unknown_or_malformed_expectations_fail_closed(self):
        for expect in ({}, [], {"target_states": {}}, {"target_states": None},
                       {"target_states": {}, "target_name": "Options"},
                       {"target_name": None}, {"target_name": []}, {"unknown": "yes"},
                       {"target_states": {"pressed": True}}, {"target_states": {"focused": "true"}}):
            with self.subTest(expect=expect):
                result = verify_click(self.initial, local_nodes(pressed="true"), self.target, expect)
                self.assertEqual((result["verified"], result["reason"]), (0, "invalid_expectation"))

    def test_ax_hash_does_not_establish_literal_editable_text_acceptance(self):
        before, after = local_nodes(editor_value="before"), local_nodes(editor_value="literal")
        self.assertEqual(ax.snapshot_sha(before, True), ax.snapshot_sha(after, True))


class TestProposedInsertionAcceptanceSubmission(unittest.TestCase):
    def inserted(self, **kwargs):
        send = Mock(return_value={})
        model = LiteralSubmission(send, "editor", "literal", **kwargs)
        self.assertTrue(model.insert([ready()]))
        return model, send

    def test_insertion_ack_is_not_acceptance_and_cannot_authorize_enter(self):
        model, send = self.inserted()
        self.assertEqual(model.state, "INSERT_ACKED")
        with self.assertRaises(PolicyError):
            model.submit(1)
        self.assertEqual(model.submission_count, 0)
        send.assert_called_once_with("Input.insertText", {"text": "literal"})

    def test_fresh_exact_accepted_text_authorizes_exactly_one_enter_attempt(self):
        model, send = self.inserted()
        self.assertTrue(model.observe_acceptance(acceptance()))
        self.assertTrue(model.submit(2))
        self.assertEqual((model.state, model.submission_count), ("SUBMISSION_ACKED", 1))
        self.assertEqual(send.call_args_list, [call("Input.insertText", {"text": "literal"})]
                         + [call(method, params) for method, params in press_events("Enter", profile="enter_text")])
        with self.assertRaises(PolicyError):
            model.submit(2)
        with self.assertRaises(PolicyError):
            model.insert([ready(3)])
        self.assertEqual(send.call_count, 3)

    def test_wrong_text_target_focus_unaccepted_or_stale_evidence_blocks_enter(self):
        for evidence in (acceptance(text="literal\n"), acceptance(target="other"),
                         acceptance(focused=False), acceptance(accepted=False),
                         acceptance(1), acceptance(True), acceptance(2.0),
                         acceptance(evidence=""), acceptance(evidence="   "), None):
            with self.subTest(evidence=evidence):
                model, send = self.inserted()
                self.assertFalse(model.observe_acceptance(evidence))
                with self.assertRaises(PolicyError):
                    model.submit(2)
                self.assertEqual(send.call_count, 1)

    def test_acceptance_reads_are_bounded_and_never_repeat_insertion(self):
        model, send = self.inserted()
        for generation in (2, 3, 4):
            self.assertFalse(model.observe_acceptance(acceptance(generation, accepted=False)))
        self.assertEqual((model.state, model.acceptance_reads), ("ACCEPTANCE_EXHAUSTED", 3))
        with self.assertRaises(PolicyError):
            model.observe_acceptance(acceptance(5))
        with self.assertRaises(PolicyError):
            model.insert([ready(5)])
        self.assertEqual(send.call_count, 1)

    def test_acceptance_on_final_read_succeeds_but_reused_generation_does_not(self):
        model, _ = self.inserted()
        self.assertFalse(model.observe_acceptance(acceptance(2, accepted=False)))
        self.assertFalse(model.observe_acceptance(acceptance(2)))
        self.assertTrue(model.observe_acceptance(acceptance(3)))
        self.assertEqual(model.acceptance_reads, 3)
        self.assertTrue(model.submit(3))

    def test_pre_send_read_retries_are_bounded_and_do_not_emit_input(self):
        send = Mock(return_value={})
        model = LiteralSubmission(send, "editor", "literal")
        evidence = iter([None, ready(focused=False), ready(3)])
        self.assertFalse(model.insert(evidence))
        self.assertEqual((model.state, model.prepare_reads), ("PREPARE_EXHAUSTED", 2))
        self.assertEqual(next(evidence), ready(3))  # the third read was never consumed
        send.assert_not_called()
        model = LiteralSubmission(send, "editor", "literal")
        self.assertTrue(model.insert([ready(editable=False), ready(2)]))
        self.assertEqual(model.prepare_reads, 2)
        self.assertEqual(send.call_count, 1)

    def test_readiness_generations_cannot_go_backwards_or_repeat(self):
        for unready in (ready(3, focused=False), ready(3, editable=False), ready(3, target="other")):
            for generation in (2, 3):
                with self.subTest(unready=unready, generation=generation):
                    send = Mock(return_value={})
                    model = LiteralSubmission(send, "editor", "literal")
                    self.assertFalse(model.insert([unready, ready(generation)]))
                    self.assertEqual((model.state, model.last_generation, model.prepare_reads),
                                     ("PREPARE_EXHAUSTED", 3, 2))
                    with self.assertRaises(PolicyError):
                        model.insert([ready(4)])
                    self.assertEqual(model.prepare_reads, 2)
                    send.assert_not_called()

    def test_read_exception_can_recover_only_within_the_remaining_budget(self):
        send = Mock(return_value={})
        model = LiteralSubmission(send, "editor", "literal")
        observations = ScriptedReadiness([RuntimeError("local read failure"), ready(2), ready(3)])
        self.assertTrue(model.insert(observations))
        self.assertEqual((model.state, model.prepare_reads, observations.reads), ("INSERT_ACKED", 2, 2))
        with self.assertRaises(PolicyError):
            model.insert(observations)
        self.assertEqual(observations.reads, 2)
        send.assert_called_once_with("Input.insertText", {"text": "literal"})

    def test_read_exception_exhaustion_seals_the_budget_without_reset(self):
        send = Mock(return_value={})
        model = LiteralSubmission(send, "editor", "literal")
        observations = ScriptedReadiness([RuntimeError("first"), RuntimeError("second"), ready(3)])
        self.assertFalse(model.insert(observations))
        self.assertEqual((model.state, model.prepare_reads, observations.reads), ("PREPARE_EXHAUSTED", 2, 2))
        with self.assertRaises(PolicyError):
            model.insert(observations)
        self.assertEqual((model.prepare_reads, observations.reads), (2, 2))
        send.assert_not_called()

    def test_invalid_iterable_is_terminal_before_any_read_can_reset_the_budget(self):
        class BrokenIterable:
            def __iter__(self):
                raise RuntimeError("local iterable construction failed")

        for observations in (None, BrokenIterable()):
            with self.subTest(observations=type(observations).__name__):
                send = Mock(return_value={})
                model = LiteralSubmission(send, "editor", "literal")
                self.assertFalse(model.insert(observations))
                self.assertEqual((model.state, model.prepare_reads), ("PREPARE_EXHAUSTED", 0))
                with self.assertRaises(PolicyError):
                    model.insert([ready()])
                send.assert_not_called()

    def test_ack_timeout_reserves_insertion_before_send_and_refuses_duplicate(self):
        send = Mock(side_effect=TimeoutError("insert acknowledgment unknown"))
        model = LiteralSubmission(send, "editor", "literal")
        self.assertFalse(model.insert([ready()]))
        self.assertEqual((model.state, model.insertion.attempted, model.insertion.acknowledged),
                         ("UNCERTAIN", 1, 0))
        model.timeout()
        with self.assertRaises(PolicyError):
            model.insert([ready(2)])
        self.assertEqual(send.call_count, 1)

    def test_acceptance_timeout_does_not_reset_the_insertion_identity(self):
        model, send = self.inserted()
        model.timeout()
        with self.assertRaises(PolicyError):
            model.insert([ready(2)])
        with self.assertRaises(PolicyError):
            model.submit(2)
        self.assertEqual(send.call_count, 1)

    def test_partial_enter_pair_is_uncertain_without_replay(self):
        send = Mock(side_effect=[{}, {}, TimeoutError("keyUp acknowledgment unknown")])
        model = LiteralSubmission(send, "editor", "literal")
        self.assertTrue(model.insert([ready()]))
        self.assertTrue(model.observe_acceptance(acceptance()))
        self.assertFalse(model.submit(2))
        self.assertEqual((model.state, model.submission_count), ("UNCERTAIN", 1))
        self.assertEqual((model.submission.attempted, model.submission.acknowledged), (2, 1))
        with self.assertRaises(PolicyError):
            model.submit(2)
        self.assertEqual(send.call_count, 3)

    def test_new_observation_invalidates_accepted_evidence_before_enter(self):
        model, send = self.inserted()
        self.assertTrue(model.observe_acceptance(acceptance()))
        with self.assertRaises(PolicyError):
            model.submit(3)
        self.assertEqual(model.state, "EVIDENCE_STALE")
        with self.assertRaises(PolicyError):
            model.submit(2)
        self.assertEqual(send.call_count, 1)

    def test_read_budgets_reject_booleans_floats_zero_and_unbounded_values(self):
        for budget in (False, True, 1.0, 0, -1, 4, "3", None):
            for name in ("max_prepare_reads", "max_acceptance_reads"):
                with self.subTest(name=name, budget=budget), self.assertRaises(PolicyError):
                    LiteralSubmission(Mock(), "editor", "literal", **{name: budget})


if __name__ == "__main__":
    unittest.main()
