'Adversarial claim-pinning tests for wender.perceive.stopstate.detect (WP1-05).'
import unittest
def make_google_sorry_fixture():
 return {'url': 'https://www.google.com/sorry/index?continue=https://www.google.com/search%3Fq%3Dtest', 'status_code': 200, 'headers': {'content-type': 'text/html; charset=utf-8'}, 'nodes': [], 'text': ''}
def make_recaptcha_v2_fixture(host='www.google.com'):
 return {'url': 'https://example.com/login', 'status_code': 200, 'headers': {'content-type': 'text/html'}, 'nodes': [{'id': 'node_frame_1', 'role': 'Iframe', 'name': 'reCAPTCHA', 'src': f'https://{host}/recaptcha/api2/anchor?ar=1&k=6Ld_sample'}, {'id': 'node_chk_1', 'role': 'checkbox', 'name': "I'm not a robot", 'ancestors': ('node_frame_1',)}], 'text': "Please verify you are human. I'm not a robot"}
def make_turnstile_fixture(interactive=True):
 src = 'https://challenges.cloudflare.com/cdn-cgi/challenge-platform/h/g/turnstile/if/ov2/av0/fixture' if interactive else 'https://challenges.cloudflare.com/turnstile/v0/api.js'
 nodes = [{'id': 'node_turnstile_frame', 'role': 'Iframe', 'name': 'Cloudflare Turnstile Challenge', 'src': src}]
 if interactive:
  nodes.append({'id': 'node_turnstile_checkbox', 'role': 'checkbox', 'name': 'Verify you are human', 'ancestors': ('node_turnstile_frame',)})
 return {'url': 'https://example.com/protected', 'status_code': 200, 'headers': {'content-type': 'text/html'}, 'nodes': nodes, 'text': 'Verify you are human' if interactive else ''}
def make_unusual_traffic_fixture():
 return {'url': 'https://example.com/search?q=query', 'status_code': 200, 'headers': {'content-type': 'text/html'}, 'nodes': [{'id': 'heading_1', 'role': 'heading', 'name': 'Our systems have detected unusual traffic from your computer network'}, {'id': 'static_text_1', 'role': 'StaticText', 'name': 'Please try your request again later.'}], 'text': 'Our systems have detected unusual traffic from your computer network. Please try your request again later.'}
def make_clean_fixture():
 return {'url': 'https://example.com/login', 'status_code': 200, 'headers': {'content-type': 'text/html'}, 'nodes': [{'id': 'form_login', 'role': 'form', 'name': 'Login'}, {'id': 'btn_signin', 'role': 'button', 'name': 'Sign in', 'ancestors': ('form_login',)}, {'id': 'dialog_cookie', 'role': 'dialog', 'name': 'Cookie Preferences'}, {'id': 'btn_reject_all', 'role': 'button', 'name': 'Rechazar todo', 'ancestors': ('dialog_cookie',)}], 'text': 'Welcome. Login Sign in Rechazar todo'}
def make_recaptcha_v3_script_only_fixture():
 return {'url': 'https://example.com/checkout', 'status_code': 200, 'headers': {'content-type': 'text/html'}, 'scripts': ['https://www.google.com/recaptcha/api.js?render=explicit', 'https://recaptcha.google.com/recaptcha/enterprise.js'], 'nodes': [{'id': 'btn_pay', 'role': 'button', 'name': 'Pay Now'}], 'text': 'Checkout Pay Now'}
def make_turnstile_script_only_fixture():
 return {'url': 'https://example.com/app', 'status_code': 200, 'headers': {'content-type': 'text/html'}, 'scripts': ['https://challenges.cloudflare.com/turnstile/v0/api.js'], 'nodes': [{'id': 'btn_dashboard', 'role': 'button', 'name': 'Go to Dashboard'}], 'text': 'Dashboard'}
def make_cloudflare_fullpage_challenge_fixture():
 return {'url': 'https://example.com/secure', 'status_code': 403, 'headers': {'content-type': 'text/html', 'cf-mitigated': 'challenge'}, 'nodes': [{'id': 'heading_cf', 'role': 'heading', 'name': 'Just a moment...'}, {'id': 'frame_cf', 'role': 'Iframe', 'src': 'https://challenges.cloudflare.com/cdn-cgi/challenge-platform/h/g/orchestrate/chk_js/v1'}], 'text': 'Just a moment... Enable JavaScript and cookies to continue'}
def make_http_429_fixture():
 return {'url': 'https://api.example.com/data', 'status_code': 429, 'headers': {'content-type': 'application/json'}, 'nodes': [], 'text': '{"error": "Too Many Requests"}'}
def make_generic_controls_fixture():
 return {'url': 'https://example.com/verify-email', 'status_code': 200, 'headers': {'content-type': 'text/html'}, 'nodes': [{'id': 'chk_remember', 'role': 'checkbox', 'name': 'Remember this browser'}, {'id': 'btn_continue', 'role': 'button', 'name': 'Continue'}, {'id': 'btn_verify', 'role': 'button', 'name': 'Verify'}], 'text': 'Verify email to continue'}
class TestHealthyControlFixtures(unittest.TestCase):
 def test_fixture_a_google_sorry_structure(self):
  fix = make_google_sorry_fixture(); self.assertIn('/sorry/index', fix['url']); self.assertEqual(fix['nodes'], [])
 def test_fixture_b_recaptcha_v2_hosts(self):
  for host in ('www.google.com', 'recaptcha.google.com', 'www.recaptcha.net'):
   fix = make_recaptcha_v2_fixture(host)
   self.assertTrue(any((host in n.get('src', '') for n in fix['nodes'])) and any((n.get('name') == "I'm not a robot" for n in fix['nodes'])))
 def test_fixture_c_turnstile_distinction(self):
  self.assertTrue(any((n['role'] == 'checkbox' for n in make_turnstile_fixture(True)['nodes']))); self.assertFalse(any((n['role'] == 'checkbox' for n in make_turnstile_script_only_fixture()['nodes'])))
 def test_fixture_d_unusual_traffic_structure(self):
  self.assertIn('unusual traffic', make_unusual_traffic_fixture()['text'].lower())
 def test_fixture_e_clean_structure(self):
  fix = make_clean_fixture(); self.assertTrue(any((n['name'] == 'Sign in' for n in fix['nodes'])) and any((n['name'] == 'Rechazar todo' for n in fix['nodes'])))
 def test_negative_controls_structure(self):
  self.assertIn('recaptcha', make_recaptcha_v3_script_only_fixture()['scripts'][0]); self.assertEqual(make_http_429_fixture()['status_code'], 429)
  self.assertTrue(any((n['name'] == 'Continue' for n in make_generic_controls_fixture()['nodes'])))
class TestJq1A6StopSignalsClaims(unittest.TestCase):
 def _check_stop(self, v, reason, conf, *ev_items):
  self.assertEqual(set(v), {'is_stop', 'reason', 'confidence', 'evidence'}); self.assertEqual((v['is_stop'], v['reason'], v['confidence']), (True, reason, conf))
  self.assertIsInstance(v['evidence'], list); self.assertTrue(v['evidence'])
  for item in v['evidence']:
   self.assertEqual(set(item), {'kind', 'value'})
  for expected in ev_items:
   self.assertIn(expected, v['evidence'])
 def _check_clear(self, v):
  self.assertEqual(v, {'is_stop': False, 'reason': None, 'confidence': 0, 'evidence': []})
 def test_claim_google_sorry_url_returns_stop(self):
  from wender.perceive.stopstate import detect; self._check_stop(detect(make_google_sorry_fixture()), 'google_sorry', 100, {'kind': 'url', 'value': 'https://www.google.com/sorry/index'})
 def test_claim_recaptcha_v2_www_google_returns_stop(self):
  from wender.perceive.stopstate import detect; self._check_stop(detect(make_recaptcha_v2_fixture('www.google.com')), 'recaptcha_v2', 95, {'kind': 'frame_url', 'value': 'https://www.google.com/recaptcha/api2/anchor'}, {'kind': 'ax_role_name', 'value': "checkbox: I'm not a robot"})
 def test_claim_recaptcha_v2_recaptcha_google_returns_stop(self):
  from wender.perceive.stopstate import detect; self._check_stop(detect(make_recaptcha_v2_fixture('recaptcha.google.com')), 'recaptcha_v2', 95, {'kind': 'frame_url', 'value': 'https://recaptcha.google.com/recaptcha/api2/anchor'}, {'kind': 'ax_role_name', 'value': "checkbox: I'm not a robot"})
 def test_claim_recaptcha_v2_recaptcha_net_returns_stop(self):
  from wender.perceive.stopstate import detect; self._check_stop(detect(make_recaptcha_v2_fixture('www.recaptcha.net')), 'recaptcha_v2', 95, {'kind': 'frame_url', 'value': 'https://www.recaptcha.net/recaptcha/api2/anchor'}, {'kind': 'ax_role_name', 'value': "checkbox: I'm not a robot"})
 def test_claim_turnstile_interactive_returns_stop(self):
  from wender.perceive.stopstate import detect; self._check_stop(detect(make_turnstile_fixture(True)), 'turnstile_challenge', 95, {'kind': 'frame_url', 'value': 'https://challenges.cloudflare.com/cdn-cgi/challenge-platform/h/g/turnstile/if/ov2/av0/fixture'}, {'kind': 'ax_role_name', 'value': 'checkbox: Verify you are human'})
 def test_claim_cloudflare_fullpage_challenge_returns_stop(self):
  from wender.perceive.stopstate import detect; self._check_stop(detect(make_cloudflare_fullpage_challenge_fixture()), 'cloudflare_challenge', 100, {'kind': 'header', 'value': 'cf-mitigated: challenge'}, {'kind': 'header', 'value': 'content-type: text/html'})
 def test_claim_unusual_traffic_text_returns_stop(self):
  from wender.perceive.stopstate import detect; self._check_stop(detect(make_unusual_traffic_fixture()), 'unusual_traffic', 90, {'kind': 'text', 'value': 'Our systems have detected unusual traffic from your computer network.'})
 def test_claim_clean_fixture_returns_not_stop(self):
  from wender.perceive.stopstate import detect; self._check_clear(detect(make_clean_fixture()))
 def test_claim_recaptcha_v3_script_only_returns_not_stop(self):
  from wender.perceive.stopstate import detect; self._check_clear(detect(make_recaptcha_v3_script_only_fixture()))
 def test_claim_turnstile_script_only_returns_not_stop(self):
  from wender.perceive.stopstate import detect; self._check_clear(detect(make_turnstile_script_only_fixture()))
 def test_claim_http_429_alone_returns_not_stop(self):
  from wender.perceive.stopstate import detect; self._check_clear(detect(make_http_429_fixture()))
 def test_claim_generic_controls_returns_not_stop(self):
  from wender.perceive.stopstate import detect; self._check_clear(detect(make_generic_controls_fixture()))
 def test_claim_non_frame_src_and_passive_turnstile_stay_negative(self):
  from wender.perceive.stopstate import detect; self._check_clear(detect(make_turnstile_fixture(False))); self._check_clear(detect({'url': 'https://example.com/login', 'nodes': [{'id': 's1', 'role': 'generic', 'src': 'https://www.google.com/recaptcha/api2/anchor'}, {'id': 'c1', 'role': 'checkbox', 'name': "I'm not a robot"}]}))
if __name__ == '__main__':
 unittest.main()
