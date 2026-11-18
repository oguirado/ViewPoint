from contextlib import nullcontext
from dataclasses import replace
import inspect
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch


TESTS = Path(__file__).resolve().parent
VP_DIRECT = TESTS.parents[1]
sys.path.insert(0, str(VP_DIRECT))
from bus.broker import Broker
import privacy
from privacy import (HostPrivacyEvidence, PrivacyBinding, PrivacyBlocked, PrivacyMetadata,
                     PrivacySentinel, PrivacyState, classify_host_privacy, host_privacy_for)
from recorder.timeline import TimelinePosition, TimelineRecorder
from schemas.validation import canonical_bytes


def variant(before, after):
    source = inspect.getsource(PrivacySentinel)
    if source.count(before) != 1:
        raise AssertionError("mutant must change exactly one guard")
    namespace = dict(vars(privacy))
    exec(compile(source.replace(before, after), privacy.__file__, "exec"), namespace)
    return namespace["PrivacySentinel"]


def example(contract):
    path = VP_DIRECT / "schemas" / "tests" / "examples" / f"{contract}_v1.valid.json"
    return json.loads(path.read_bytes())


class PrivacyBindingTests(unittest.TestCase):
    def test_binding_requires_an_integer_authority_epoch(self):
        self.assertEqual(PrivacyBinding("surface", "inc", 1, "cdp", "window").authority_epoch, 1)
        with self.assertRaisesRegex(ValueError, "invalid privacy binding"):
            PrivacyBinding("surface", "inc", True, "cdp", "window")

    def test_binding_accepts_zero_and_rejects_negative_authority_epoch(self):
        self.assertEqual(PrivacyBinding("surface", "inc", 0, "cdp", "window").authority_epoch, 0)
        with self.assertRaisesRegex(ValueError, "invalid privacy binding"):
            PrivacyBinding("surface", "inc", -1, "cdp", "window")


class PrivacyTests(unittest.TestCase):
    def setUp(self):
        self.binding = PrivacyBinding("surface", "inc", 1, "cdp", "fixture-window")
        self.clear = PrivacyMetadata(self.binding, 1, PrivacyState.CLEAR)
        self.login = PrivacyMetadata(self.binding, 2, PrivacyState.LOGIN)
        self.reauth = PrivacyMetadata(self.binding, 2, PrivacyState.REAUTHENTICATION)
        self.host_clear = HostPrivacyEvidence(
            self.binding, 1, login_active=False, reauthenticating=False, window_active=True,
            input_drained=True, covers_capture=True, atspi_roles=("window", "push button"),
        )

    def test_host_classifier_requires_nonblank_roles(self):
        empty_role = replace(self.host_clear, atspi_roles=("window", " "))
        self.assertIs(classify_host_privacy(self.binding, empty_role).state, PrivacyState.UNKNOWN)
        self.assertEqual(classify_host_privacy(self.binding, self.host_clear), self.clear)

    def test_host_classifier_accepts_zero_and_rejects_negative_revision(self):
        initial = replace(self.host_clear, revision=0)
        self.assertEqual(classify_host_privacy(self.binding, initial), replace(self.clear, revision=0))
        self.assertIsNone(classify_host_privacy(self.binding, replace(initial, revision=-1)))

    def test_sentinel_accepts_zero_and_refuses_negative_revision(self):
        initial = replace(self.clear, revision=0)
        sentinel = self.sentinel([initial, initial])
        self.assertEqual(sentinel.observe(self.acquire, self.publish), "published")
        self.acquire.assert_called_once_with(initial)
        self.assert_preflight_blocked(self.sentinel([replace(initial, revision=-1)]), "HOLD")

    def test_host_classifier_requires_bound_content_free_coverage(self):
        self.assertEqual(classify_host_privacy(self.binding, self.host_clear), self.clear)
        for overrides in (dict(login_active=None), dict(reauthenticating=None), dict(window_active=False),
                          dict(input_drained=False), dict(covers_capture=False), dict(atspi_roles=None),
                          dict(atspi_roles=()), dict(atspi_roles=("window", "password text"))):
            with self.subTest(overrides=overrides):
                metadata = classify_host_privacy(self.binding, replace(self.host_clear, **overrides))
                self.assertEqual(metadata.state, PrivacyState.UNKNOWN)
        self.assertIsNone(classify_host_privacy(replace(self.binding, witness="foreign"), self.host_clear))

    def test_host_login_and_reauth_veto_even_without_capture_coverage(self):
        for flags in (dict(login_active=True), dict(reauthenticating=True)):
            with self.subTest(flags=flags):
                evidence = replace(self.host_clear, atspi_roles=None, covers_capture=None, **flags)
                self.acquire, self.publish = Mock(), Mock()
                gap, human = Mock(), Mock()
                selector = host_privacy_for(self.binding, lambda _: evidence, record_gap=gap, request_human=human)
                state = Mock()
                state.privacy_binding.return_value = replace(self.binding, authority_epoch=2)
                self.assertIsNone(selector(state))
                state.privacy_binding.return_value = self.binding
                self.assertIs(selector(state), selector(state))
                self.assert_preflight_blocked(selector(state), "HUMAN_NEEDED")
                gap.assert_called_once()
                human.assert_called_once_with("login")

    def test_mutant_erases_raw_password_role_before_classification(self):
        evidence = replace(self.host_clear, atspi_roles=("window", "password text"))
        state = Mock()
        state.privacy_binding.return_value = self.binding

        def check():
            self.acquire, self.publish = Mock(), Mock()
            selector = host_privacy_for(self.binding, lambda _: evidence, record_gap=Mock(), request_human=Mock())
            self.assert_preflight_blocked(selector(state), "HOLD")

        check()
        with patch.object(privacy, "classify_host_privacy", side_effect=lambda binding, row:
                          classify_host_privacy(binding, replace(row, atspi_roles=("window",)))):
            with self.assertRaisesRegex(AssertionError, "capture callback ran"):
                check()

    def sentinel(self, reads, *, binding=None, implementation=PrivacySentinel,
                 record_gap=None, request_human=None):
        if binding is None:
            binding = self.binding
        self.read = Mock(side_effect=reads)
        self.gap = record_gap if record_gap is not None else Mock()
        self.human = request_human if request_human is not None else Mock()
        self.acquire = Mock(return_value=b"staged fixture")
        self.publish = Mock(return_value="published")
        return implementation(binding, self.read, record_gap=self.gap, request_human=self.human,
                              clock=iter(range(100, 200)).__next__)

    def assert_preflight_blocked(self, sentinel, code):
        actual = None
        try:
            sentinel.observe(self.acquire, self.publish)
        except PrivacyBlocked as exc:
            actual = exc.code
        self.assertEqual(self.acquire.call_count, 0, "capture callback ran on a blocked source")
        self.publish.assert_not_called()
        self.assertEqual(actual, code)

    def test_authorized_metadata_first_then_revalidation_before_handoff(self):
        sentinel = self.sentinel([self.clear, self.clear])

        def acquire(grant):
            self.read.assert_called_once_with(grant.binding)
            return b"private"

        self.acquire.side_effect = acquire

        def publish(payload):
            self.assertEqual(self.read.call_count, 2)
            self.acquire.assert_called_once_with(self.clear)
            self.assertEqual(payload, b"private")
            return "published"

        self.assertEqual(sentinel.observe(self.acquire, publish), "published")
        self.gap.assert_not_called()
        self.human.assert_not_called()

    def test_equal_binding_from_fresh_metadata_requires_both_reads(self):
        alias = replace(self.binding)
        self.assertIsNot(alias, self.binding)
        metadata = replace(self.clear, binding=alias)

        def check():
            sentinel = self.sentinel([metadata, metadata])
            self.assertEqual(sentinel.observe(self.acquire, self.publish), "published")
            self.assertEqual(self.read.call_count, 2)
            self.publish.assert_called_once()

        check()
        # Mutate comparisons to identity-only, dropping equality for a fresh binding.
        with patch.object(PrivacyBinding, "__eq__", object.__eq__):
            with self.assertRaisesRegex(PrivacyBlocked, "^HOLD$"):
                check()

    def test_login_and_reauthentication_block_before_capture_and_request_human(self):
        for evidence in (self.login, self.reauth):
            with self.subTest(state=evidence.state):
                sentinel = self.sentinel([evidence, evidence])
                self.assert_preflight_blocked(sentinel, "HUMAN_NEEDED")
                self.assert_preflight_blocked(sentinel, "HUMAN_NEEDED")
                self.gap.assert_called_once_with(True, 100)
                self.human.assert_called_once_with("login")

    def assert_unknown_xwd_blocked(self, implementation):
        binding = replace(self.binding, source="xwd")
        unknown = PrivacyMetadata(binding, 1, PrivacyState.UNKNOWN)
        sentinel = self.sentinel([unknown, unknown], binding=binding, implementation=implementation)
        self.assert_preflight_blocked(sentinel, "HOLD")
        self.gap.assert_called_once_with(True, 100)
        self.human.assert_not_called()

    def test_unknown_full_xwd_is_not_source_exclusion(self):
        self.assert_unknown_xwd_blocked(PrivacySentinel)

    def test_missing_untrusted_and_mismatched_metadata_fail_closed(self):
        for evidence in (None, {"state": "clear", "source_excluded": True},
                         OSError("unavailable fixture metadata"), replace(self.clear, revision=True),
                         replace(self.clear, state="clear"),
                         replace(self.clear, binding=replace(self.binding, authority_epoch=2)),
                         replace(self.clear, binding=replace(self.binding, witness="retired-window")),
                         replace(self.clear, binding=replace(self.binding, incarnation="retired-incarnation")),
                         replace(self.clear, binding=replace(self.binding, source="xwd"))):
            with self.subTest(evidence=evidence):
                sentinel = self.sentinel([evidence])
                self.assert_preflight_blocked(sentinel, "HOLD")
                self.human.assert_not_called()

    def test_revalidation_rejects_retired_evidence_before_publication(self):
        for after in (None, replace(self.clear, revision=0), replace(self.clear, revision=2),
                      replace(self.clear, binding=replace(self.binding, witness="replacement-window")),
                      replace(self.clear, state=PrivacyState.UNKNOWN)):
            with self.subTest(after=after):
                sentinel = self.sentinel([self.clear, after])
                with self.assertRaisesRegex(PrivacyBlocked, "^STALE_FRAME$"):
                    sentinel.observe(self.acquire, self.publish)
                self.acquire.assert_called_once_with(self.clear)
                self.publish.assert_not_called()
                self.gap.assert_called_once_with(True, 100)

    def test_login_gap_recovers_on_new_clear_revision_and_rechecks_after_gap_sink(self):
        clear = replace(self.clear, revision=3)
        sentinel = self.sentinel([self.login, self.clear, clear, clear, clear])
        self.assert_preflight_blocked(sentinel, "HUMAN_NEEDED")
        self.assert_preflight_blocked(sentinel, "HOLD")
        self.assertEqual(sentinel.observe(self.acquire, self.publish), "published")
        self.assertEqual([call.args for call in self.gap.call_args_list], [(True, 100), (False, 102)])
        self.acquire.assert_called_once_with(clear)
        self.assertEqual(self.read.call_count, 5)

        relogin = replace(self.login, revision=4)
        sentinel = self.sentinel([self.login, clear, relogin])
        self.assert_preflight_blocked(sentinel, "HUMAN_NEEDED")
        self.assert_preflight_blocked(sentinel, "HUMAN_NEEDED")
        self.assertEqual([call.args for call in self.gap.call_args_list],
                         [(True, 100), (False, 101), (True, 102)])

    def test_failed_boundary_sinks_retry_without_permitting_capture(self):
        gap = Mock(side_effect=[OSError("gap failed"), None])
        human = Mock(side_effect=[OSError("notice failed"), None])
        sentinel = self.sentinel([self.login] * 4, record_gap=gap, request_human=human)
        self.assert_preflight_blocked(sentinel, "HOLD")
        human.assert_not_called()
        self.assert_preflight_blocked(sentinel, "HOLD")
        self.assert_preflight_blocked(sentinel, "HUMAN_NEEDED")
        self.assertEqual(gap.call_count, 2)
        self.assertEqual(human.call_count, 2)

    def test_failed_gap_is_recovered_before_next_clear_capture(self):
        clear = replace(self.clear, revision=3)
        gap = Mock(side_effect=[OSError("gap failed"), None, None])
        sentinel = self.sentinel([self.login, clear, clear, clear, clear], record_gap=gap)
        self.assert_preflight_blocked(sentinel, "HOLD")

        def acquire(grant):
            self.assertEqual([call.args for call in gap.call_args_list],
                             [(True, 100), (True, 100), (False, 101)])
            self.assertEqual(grant, clear)
            return b"public"

        self.assertEqual(sentinel.observe(acquire, self.publish), "published")

    def test_capture_error_does_not_surface_source_content(self):
        sentinel = self.sentinel([self.clear, self.reauth])
        self.acquire.side_effect = RuntimeError("synthetic backend observation text")
        with self.assertRaisesRegex(PrivacyBlocked, "^HUMAN_NEEDED$") as blocked:
            sentinel.observe(self.acquire, self.publish)
        self.assertIsNone(blocked.exception.__context__)
        self.publish.assert_not_called()

    def test_fixture_secrets_never_reach_agent_ocr_models_timeline_or_journal(self):
        fixtures = VP_DIRECT / "fixtures"
        self.assertIn('type="password"', (fixtures / "site" / "login.html").read_text(encoding="utf-8"))
        fake_secrets = json.loads((fixtures / "privacy" / "synthetic_secrets.json").read_bytes())
        fake_canaries = json.loads((fixtures / "privacy" / "canaries.json").read_bytes())
        markers = [item["value"] for item in fake_secrets["secrets"]]
        markers += [item["token"] for item in fake_canaries["canaries"]]
        private = "\n".join(markers)
        downstream = {key: [] for key in ("agent", "OCR", "models", "recording", "journal")}
        recorder = TimelineRecorder(surface_id="surface", capsule_id="fixture-capsule", incarnation="inc",
                                    register=lambda _: nullcontext(), verify_ram_frame=lambda *_: True)
        with TemporaryDirectory(dir=TESTS) as runtime, Broker(runtime) as broker:
            broker.bind("fixture-session", example("vp.surface_binding"), lease_state="AGENT")
            with recorder.recording(example("vp.demand")):
                sentinel = self.sentinel(
                    [self.login, replace(self.clear, revision=3), replace(self.clear, revision=3), replace(self.reauth, revision=4)],
                    record_gap=lambda active, when: recorder.set_gap(
                        TimelinePosition(when, "fixture-capsule", "inc"), "privacy", active=active),
                    request_human=lambda reason: broker.human_needed("owner", "fixture-session", "goal", reason),
                )

                def publish(content):
                    for received in downstream.values():
                        received.append(content)
                    recorder.add_cdp(TimelinePosition(102, "fixture-capsule", "inc"), text=content)

                self.acquire.return_value = private
                with self.assertRaisesRegex(PrivacyBlocked, "^HUMAN_NEEDED$"):
                    sentinel.observe(self.acquire, publish)
                self.acquire.assert_not_called()
                with self.assertRaisesRegex(PrivacyBlocked, "^HUMAN_NEEDED$"):
                    sentinel.observe(self.acquire, publish)
                self.acquire.assert_called_once()
                self.assertTrue(all(not received for received in downstream.values()))
                timeline = recorder.snapshot()
            journal = (Path(runtime) / "events.jsonl").read_bytes()
        self.assertEqual([row["op"] for row in timeline["events"]],
                         ["privacy_gap_start", "privacy_gap_end", "privacy_gap_start"])
        for row in timeline["events"]:
            self.assertTrue(row["gap"])
            self.assertEqual(set(row), {"mono_ns", "capsule_id", "incarnation", "source", "gap", "op"})
        notices = [row for row in map(json.loads, journal.splitlines()) if row["rec"] == "HUMAN_NEEDED"]
        self.assertEqual(len(notices), 2)
        for row in notices:
            self.assertEqual((row["by"], row["reason"], row["session_id"]),
                             ("broker", "login", "fixture-session"))
        for marker in markers:
            self.assertNotIn(marker.encode(), canonical_bytes(timeline) + journal)

    def test_mutant_capture_then_filter_is_rejected(self):
        mutant = variant(
            '            if before is None or before.state is not PrivacyState.CLEAR:\n'
            '                self._block(before, self._clock(), "HOLD")\n',
            '            if before is None or before.state is not PrivacyState.CLEAR:\n'
            '                acquire(before)\n'
            '                self._block(before, self._clock(), "HOLD")\n',
        )
        sentinel = self.sentinel([self.login], implementation=mutant)
        with self.assertRaisesRegex(AssertionError, "capture callback ran on a blocked source"):
            self.assert_preflight_blocked(sentinel, "HUMAN_NEEDED")
        self.acquire.assert_called_once_with(self.login)

    def test_mutant_full_xwd_as_exclusion_is_rejected(self):
        mutant = variant(
            '            if before is None or before.state is not PrivacyState.CLEAR:\n'
            '                self._block(before, self._clock(), "HOLD")\n\n',
            '            if ((before is None or before.state is not PrivacyState.CLEAR)\n'
            '                    and self._binding.source != "xwd"):\n'
            '                self._block(before, self._clock(), "HOLD")\n\n',
        )
        with self.assertRaisesRegex(AssertionError, "capture callback ran on a blocked source"):
            self.assert_unknown_xwd_blocked(mutant)
        self.acquire.assert_called_once()

    def test_mutant_omitting_revalidation_reaches_publication(self):
        mutant = variant("            if after != before:\n", "            if False:\n")
        sentinel = self.sentinel([self.clear, self.reauth], implementation=mutant)

        def observe():
            try:
                sentinel.observe(self.acquire, self.publish)
            except PrivacyBlocked:
                pass
            self.assertEqual(self.publish.call_count, 0, "stale observation reached publication")

        with self.assertRaisesRegex(AssertionError, "stale observation reached publication"):
            observe()
        self.acquire.assert_called_once_with(self.clear)
        self.publish.assert_called_once_with(b"staged fixture")
