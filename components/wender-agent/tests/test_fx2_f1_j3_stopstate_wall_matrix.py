"""Matrix unit tests for bot wall and STOP state perception contracts.

Covers production detect API (wender.perceive.stopstate.detect) with:
(1) Malformed AX combined independently with Google sorry URLs, Cloudflare HTML challenge headers,
    and HTTP 429 alone.
(2) Genuine challenge frame URLs, scripts, invisible widgets, hidden-parent, absent and visible AX cues.
(3) Deceptive hosts, near-miss login and cookie consent controls.
(4) Documented precedence, evidence locality, URL sanitization, and unchanged input objects.
(5) Rejection of script/frame conflation and malformed-AX suppression mutants.

Owning fix: fx2-f1-perceive-wait-text-stopstate
"""

import copy
import json
import unittest


def make_malformed_ax_nodes():
    """Return malformed raw AX lists (dangling child, missing nodeId, cycle, duplicate nodeId)."""
    return [
        [{"role": {"value": "RootWebArea"}, "childIds": ["missing_child_1"]}],
        [{"role": "RootWebArea", "name": "No nodeId"}],
        [
            {"nodeId": "n1", "childIds": ["n2"]},
            {"nodeId": "n2", "childIds": ["n1"]},
        ],
        [
            {"nodeId": "n1", "role": "RootWebArea"},
            {"nodeId": "n1", "role": "button"},
        ],
    ]


def make_google_sorry_fixture():
    return {
        "url": "https://www.google.com/sorry/index?continue=https://www.google.com/search%3Fq%3Dtest",
        "frame_urls": [],
        "headers": {"content-type": "text/html; charset=utf-8"},
        "nodes": [],
    }


def make_cloudflare_challenge_headers():
    return {
        "cf-mitigated": "challenge",
        "content-type": "text/html; charset=UTF-8",
    }


def make_http_429_headers():
    return {
        "status": "429",
        "content-type": "application/json",
    }


def make_recaptcha_v2_fixture(host="www.google.com", hidden_parent=False, absent_cue=False, invisible=False):
    path = "/recaptcha/api2/anchor"
    query = "?size=invisible" if invisible else "?size=normal"
    frame_url = f"https://{host}{path}{query}"

    if absent_cue:
        nodes = []
    elif hidden_parent:
        nodes = [
            {
                "id": "parent_dialog",
                "role": "dialog",
                "name": "Hidden Container",
                "properties": [{"name": "hidden", "value": {"value": True}}],
            },
            {
                "id": "chk_robot",
                "role": "checkbox",
                "name": "I'm not a robot",
                "parent": "parent_dialog",
            },
        ]
    else:
        nodes = [
            {
                "id": "chk_robot",
                "role": "checkbox",
                "name": "I'm not a robot",
            }
        ]

    return {
        "url": "https://example.com/protected",
        "frame_urls": [frame_url],
        "headers": {"content-type": "text/html"},
        "nodes": nodes,
    }


def make_turnstile_fixture(hidden_parent=False, absent_cue=False, script_only=False):
    if script_only:
        return {
            "url": "https://example.com/app",
            "frame_urls": ["https://challenges.cloudflare.com/turnstile/v0/api.js"],
            "headers": {"content-type": "text/html"},
            "nodes": [{"id": "btn_1", "role": "button", "name": "Continue"}],
        }

    frame_url = "https://challenges.cloudflare.com/cdn-cgi/challenge-platform/h/g/orchestrate/chk_js/v1"

    if absent_cue:
        nodes = []
    elif hidden_parent:
        nodes = [
            {
                "id": "parent_hidden",
                "role": "group",
                "properties": [{"name": "hidden", "value": {"value": True}}],
            },
            {
                "id": "iframe_turnstile",
                "role": "Iframe",
                "name": "Cloudflare security challenge",
                "parent": "parent_hidden",
            },
        ]
    else:
        nodes = [
            {
                "id": "iframe_turnstile",
                "role": "Iframe",
                "name": "Cloudflare security challenge",
            }
        ]

    return {
        "url": "https://example.com/secure",
        "frame_urls": [frame_url],
        "headers": {"content-type": "text/html"},
        "nodes": nodes,
    }


def make_unusual_traffic_fixture():
    return {
        "url": "https://example.com/search",
        "frame_urls": [],
        "headers": {"content-type": "text/html"},
        "nodes": [
            {
                "id": "heading_1",
                "role": "heading",
                "name": "Our systems have detected unusual traffic from your computer network.",
            }
        ],
    }


def make_login_near_miss_fixture():
    return {
        "url": "https://example.com/login",
        "frame_urls": [],
        "headers": make_http_429_headers(),
        "nodes": [
            {"id": "f1", "role": "form", "name": "Log in"},
            {"id": "t1", "role": "textbox", "name": "Password"},
            {"id": "b1", "role": "button", "name": "Log in"},
        ],
    }


def make_cookie_consent_near_miss_fixture():
    return {
        "url": "https://example.com/welcome",
        "frame_urls": [],
        "headers": make_http_429_headers(),
        "nodes": [
            {"id": "d1", "role": "dialog", "name": "Cookie Preferences"},
            {"id": "b1", "role": "button", "name": "Rechazar todo"},
            {"id": "b2", "role": "button", "name": "Accept all cookies"},
        ],
    }


def make_generic_verify_near_miss_fixture():
    return {
        "url": "https://example.com/verify",
        "frame_urls": [],
        "headers": make_http_429_headers(),
        "nodes": [
            {"id": "b1", "role": "button", "name": "Verify"},
            {"id": "b2", "role": "button", "name": "Continue"},
        ],
    }


class TestHealthyControlMatrix(unittest.TestCase):
    """Healthy control tests verifying offline matrix fixtures and structure without expectedFailure."""

    def test_malformed_ax_fixture_variants(self):
        malformed_samples = make_malformed_ax_nodes()
        self.assertGreaterEqual(len(malformed_samples), 4)
        for raw in malformed_samples:
            self.assertIsInstance(raw, list)

    def test_google_sorry_fixture_structure(self):
        fix = make_google_sorry_fixture()
        self.assertIn("/sorry/index", fix["url"])
        self.assertEqual(fix["nodes"], [])

    def test_cloudflare_challenge_headers_structure(self):
        headers = make_cloudflare_challenge_headers()
        self.assertEqual(headers["cf-mitigated"], "challenge")
        self.assertIn("text/html", headers["content-type"])

    def test_recaptcha_v2_fixture_variants(self):
        visible_fix = make_recaptcha_v2_fixture(hidden_parent=False)
        self.assertTrue(any(n.get("name") == "I'm not a robot" for n in visible_fix["nodes"]))

        hidden_fix = make_recaptcha_v2_fixture(hidden_parent=True)
        parent = next(n for n in hidden_fix["nodes"] if n["id"] == "parent_dialog")
        self.assertEqual(parent["properties"][0]["value"]["value"], True)

        absent_fix = make_recaptcha_v2_fixture(absent_cue=True)
        self.assertEqual(absent_fix["nodes"], [])

        invisible_fix = make_recaptcha_v2_fixture(invisible=True)
        self.assertIn("size=invisible", invisible_fix["frame_urls"][0])

    def test_turnstile_fixture_variants(self):
        visible_fix = make_turnstile_fixture(hidden_parent=False)
        self.assertTrue(any("Cloudflare" in n.get("name", "") for n in visible_fix["nodes"]))

        script_only = make_turnstile_fixture(script_only=True)
        self.assertTrue(script_only["frame_urls"][0].endswith(".js"))

    def test_near_miss_controls_structure(self):
        login = make_login_near_miss_fixture()
        self.assertEqual(login["headers"]["status"], "429")
        self.assertTrue(any(n.get("name") == "Log in" for n in login["nodes"]))

        cookie = make_cookie_consent_near_miss_fixture()
        self.assertTrue(any(n.get("name") == "Rechazar todo" for n in cookie["nodes"]))

        generic = make_generic_verify_near_miss_fixture()
        self.assertTrue(any(n.get("name") == "Verify" for n in generic["nodes"]))


class TestStopStateWallMatrixProduction(unittest.TestCase):
    """Adversarial claim-pinning tests for wender.perceive.stopstate.detect."""

    def test_malformed_ax_with_google_sorry_url(self):
        # fx2-f1-perceive-wait-text-stopstate
        from wender.perceive.stopstate import detect

        for raw_malformed in make_malformed_ax_nodes():
            verdict = detect(
                raw_malformed,
                url="https://www.google.com/sorry/index?continue=https://google.com",
            )
            self.assertTrue(verdict.get("is_stop"))
            self.assertEqual(verdict.get("reason"), "google_sorry")
            self.assertEqual(verdict.get("confidence"), 100)
            self.assertTrue(verdict.get("evidence"))

    def test_malformed_ax_with_cloudflare_challenge_headers(self):
        # fx2-f1-perceive-wait-text-stopstate
        from wender.perceive.stopstate import detect

        headers = make_cloudflare_challenge_headers()
        for raw_malformed in make_malformed_ax_nodes():
            verdict = detect(raw_malformed, headers=headers)
            self.assertTrue(verdict.get("is_stop"))
            self.assertEqual(verdict.get("reason"), "cloudflare_challenge")
            self.assertEqual(verdict.get("confidence"), 100)

    def test_malformed_ax_with_http_429_alone(self):
        # fx2-f1-perceive-wait-text-stopstate
        from wender.perceive.stopstate import detect

        headers = make_http_429_headers()
        for raw_malformed in make_malformed_ax_nodes():
            verdict = detect(
                raw_malformed,
                url="https://api.example.com/data",
                headers=headers,
            )
            self.assertFalse(verdict.get("is_stop"))
            self.assertIsNone(verdict.get("reason"))

    def test_malformed_ax_alone_without_wall(self):
        # fx2-f1-perceive-wait-text-stopstate
        from wender.perceive.stopstate import detect

        for raw_malformed in make_malformed_ax_nodes():
            verdict = detect(raw_malformed, url="https://example.com/normal")
            self.assertFalse(verdict.get("is_stop"))
            self.assertIsNone(verdict.get("reason"))

    @unittest.expectedFailure
    def test_recaptcha_v2_visible_cue_with_frame_url(self):
        # fx2-f1-perceive-wait-text-stopstate
        from wender.perceive.stopstate import detect

        fix = make_recaptcha_v2_fixture(hidden_parent=False)
        verdict = detect(
            fix["nodes"],
            url=fix["url"],
            frame_urls=fix["frame_urls"],
            headers=fix["headers"],
        )
        self.assertTrue(verdict.get("is_stop"))
        self.assertEqual(verdict.get("reason"), "recaptcha_v2")
        self.assertEqual(verdict.get("confidence"), 95)

    def test_recaptcha_v2_hidden_parent_cue(self):
        # fx2-f1-perceive-wait-text-stopstate
        from wender.perceive.stopstate import detect

        fix = make_recaptcha_v2_fixture(hidden_parent=True)
        verdict = detect(
            fix["nodes"],
            url=fix["url"],
            frame_urls=fix["frame_urls"],
            headers=fix["headers"],
        )
        self.assertFalse(verdict.get("is_stop"))

    def test_recaptcha_v2_absent_cue(self):
        # fx2-f1-perceive-wait-text-stopstate
        from wender.perceive.stopstate import detect

        fix = make_recaptcha_v2_fixture(absent_cue=True)
        verdict = detect(
            fix["nodes"],
            url=fix["url"],
            frame_urls=fix["frame_urls"],
            headers=fix["headers"],
        )
        self.assertFalse(verdict.get("is_stop"))

    @unittest.expectedFailure
    def test_turnstile_visible_cue_with_frame_url(self):
        # fx2-f1-perceive-wait-text-stopstate
        from wender.perceive.stopstate import detect

        fix = make_turnstile_fixture(hidden_parent=False)
        verdict = detect(
            fix["nodes"],
            url=fix["url"],
            frame_urls=fix["frame_urls"],
            headers=fix["headers"],
        )
        self.assertTrue(verdict.get("is_stop"))
        self.assertEqual(verdict.get("reason"), "turnstile_challenge")
        self.assertEqual(verdict.get("confidence"), 95)

    def test_turnstile_hidden_parent_cue(self):
        # fx2-f1-perceive-wait-text-stopstate
        from wender.perceive.stopstate import detect

        fix = make_turnstile_fixture(hidden_parent=True)
        verdict = detect(
            fix["nodes"],
            url=fix["url"],
            frame_urls=fix["frame_urls"],
            headers=fix["headers"],
        )
        self.assertFalse(verdict.get("is_stop"))

    def test_invisible_widget_frame_url(self):
        # fx2-f1-perceive-wait-text-stopstate
        from wender.perceive.stopstate import detect

        fix = make_recaptcha_v2_fixture(invisible=True)
        verdict = detect(
            fix["nodes"],
            url=fix["url"],
            frame_urls=fix["frame_urls"],
            headers=fix["headers"],
        )
        self.assertFalse(verdict.get("is_stop"))

    def test_deceptive_hosts_and_userinfo_url(self):
        # fx2-f1-perceive-wait-text-stopstate
        from wender.perceive.stopstate import detect

        deceptive_urls = [
            "https://google.com.evil.test/sorry/index",
            "https://www.google.com@evil.test/sorry/index",
            "https://notgoogle.com/sorry/index",
            "https://challenges.cloudflare.com.evil.test/cdn-cgi/challenge-platform/h/g/chk",
        ]
        for url in deceptive_urls:
            verdict = detect([], url=url, frame_urls=[url])
            self.assertFalse(verdict.get("is_stop"), f"Failed for deceptive URL: {url}")

    def test_precedence_google_sorry_over_unusual_traffic(self):
        # fx2-f1-perceive-wait-text-stopstate
        from wender.perceive.stopstate import detect

        nodes = make_unusual_traffic_fixture()["nodes"]
        verdict = detect(
            nodes,
            url="https://www.google.com/sorry/index",
        )
        self.assertTrue(verdict.get("is_stop"))
        self.assertEqual(verdict.get("reason"), "google_sorry")
        self.assertEqual(verdict.get("confidence"), 100)

    def test_evidence_locality_and_url_sanitization(self):
        # fx2-f1-perceive-wait-text-stopstate
        from wender.perceive.stopstate import detect

        raw_url = "https://user:pass" "@www.google.com/sorry/index?q=secret#fragment"
        verdict = detect([], url=raw_url)
        self.assertTrue(verdict.get("is_stop"))
        self.assertEqual(verdict.get("reason"), "google_sorry")
        evidence = verdict.get("evidence", [])
        self.assertTrue(isinstance(evidence, list))
        self.assertEqual(len(evidence), 1)
        item = evidence[0]
        self.assertEqual(set(item.keys()), {"kind", "value"})
        self.assertEqual(item["kind"], "url")
        self.assertEqual(item["value"], "https://www.google.com/sorry/index")
        self.assertNotIn("secret", json.dumps(verdict))
        self.assertNotIn("user", json.dumps(verdict))

    def test_unchanged_input_objects(self):
        # fx2-f1-perceive-wait-text-stopstate
        from wender.perceive.stopstate import detect

        state = {
            "source": {"url": "https://www.google.com/sorry/index?q=1"},
            "nodes": [{"id": "n1", "parent": None, "role": "heading", "name": "Test", "states": {}}],
        }
        frame_urls = ["https://www.google.com/recaptcha/api2/anchor"]
        headers = {"cf-mitigated": "challenge", "content-type": "text/html"}

        state_copy = copy.deepcopy(state)
        frames_copy = copy.deepcopy(frame_urls)
        headers_copy = copy.deepcopy(headers)

        detect(state, frame_urls=frame_urls, headers=headers)

        self.assertEqual(state, state_copy)
        self.assertEqual(frame_urls, frames_copy)
        self.assertEqual(headers, headers_copy)

    def test_near_miss_login_controls(self):
        # fx2-f1-perceive-wait-text-stopstate
        from wender.perceive.stopstate import detect

        fix = make_login_near_miss_fixture()
        verdict = detect(
            fix["nodes"],
            url=fix["url"],
            headers=fix["headers"],
        )
        self.assertFalse(verdict.get("is_stop"))

    def test_near_miss_cookie_consent_controls(self):
        # fx2-f1-perceive-wait-text-stopstate
        from wender.perceive.stopstate import detect

        fix = make_cookie_consent_near_miss_fixture()
        verdict = detect(
            fix["nodes"],
            url=fix["url"],
            headers=fix["headers"],
        )
        self.assertFalse(verdict.get("is_stop"))

    def test_near_miss_generic_verify_controls(self):
        # fx2-f1-perceive-wait-text-stopstate
        from wender.perceive.stopstate import detect

        fix = make_generic_verify_near_miss_fixture()
        verdict = detect(
            fix["nodes"],
            url=fix["url"],
            headers=fix["headers"],
        )
        self.assertFalse(verdict.get("is_stop"))

    def test_reject_script_frame_conflation_mutants(self):
        # fx2-f1-perceive-wait-text-stopstate
        from wender.perceive.stopstate import detect

        fix = make_turnstile_fixture(script_only=True)
        verdict = detect(
            fix["nodes"],
            url=fix["url"],
            frame_urls=fix["frame_urls"],
            headers=fix["headers"],
        )
        self.assertFalse(verdict.get("is_stop"))

    def test_reject_malformed_ax_suppression_mutants(self):
        # fx2-f1-perceive-wait-text-stopstate
        from wender.perceive.stopstate import detect

        # Even with malformed AX tree, URL / header walls must be detected
        for raw_malformed in make_malformed_ax_nodes():
            sorry_verdict = detect(raw_malformed, url="https://www.google.com/sorry/index")
            self.assertTrue(sorry_verdict.get("is_stop"))
            self.assertEqual(sorry_verdict.get("reason"), "google_sorry")

            cf_verdict = detect(raw_malformed, headers=make_cloudflare_challenge_headers())
            self.assertTrue(cf_verdict.get("is_stop"))
            self.assertEqual(cf_verdict.get("reason"), "cloudflare_challenge")


if __name__ == "__main__":
    unittest.main()
