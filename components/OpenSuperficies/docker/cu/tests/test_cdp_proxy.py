"""Lane P (W6a): CDP allow-list proxy, oracle shim, no-listener scan. Fakes only: no Chromium, no docker, no X.
Run: taskset -c 10-14 timeout 300 python3 -B -m unittest docker.cu.tests.test_cdp_proxy"""
import importlib.util, json, os, select, shutil, stat, subprocess, sys, tempfile, time, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ORA = os.path.join(HERE, "..", "oracle")
FX = os.path.join(HERE, "fixtures", "cdp")
URL = "file:///opt/cu/scenes/v3/wl1s_small.html"
PY = sys.executable


def _load(name):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ORA, name + ".py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


P = _load("cdp_proxy")
AL = os.path.join(ORA, "allowlist.json")


def req(method, id=1, params=None, sid="S1"):
    o = {"id": id, "method": method}
    if params is not None:
        o["params"] = params
    if sid:
        o["sessionId"] = sid
    return json.dumps(o).encode()


def bound_policy():
    pol = P.Policy(P.load_allowlist(AL), URL)
    ev = {"method": "Target.attachedToTarget", "params": {"sessionId": "S1", "waitingForDebugger": False,
          "targetInfo": {"targetId": "T1", "type": "page", "url": URL}}}
    act, _ = pol.inbound(json.dumps(ev).encode())
    assert act == "forward", act
    return pol


class Framing(unittest.TestCase):
    def test_status_cases(self):
        self.assertEqual(P.frame_status(b'{"a":1}\0rest', 100), (1, 7))
        self.assertEqual(P.frame_status(b'abc', 8), (0, 0))
        self.assertEqual(P.frame_status(b'a' * 8, 8), (0, 0))          # M bytes, no NUL yet: need more
        self.assertEqual(P.frame_status(b'a' * 8 + b'\0', 8), (1, 8))   # payload == M accepted
        self.assertEqual(P.frame_status(b'a' * 9, 8)[0], -1)            # M+1 bytes without NUL: oversize
        self.assertEqual(P.frame_status(b'a' * 9 + b'\0', 8)[0], -1)    # payload M+1: oversize (lim = M+1)

    def test_default_bound_is_100_mib(self):
        self.assertEqual(P.DEFAULT_MAX, 100 * 1024 * 1024)

    def test_split_at_every_offset_equals_whole(self):
        stream = b'{"id":1}\0{"id":2,"method":"x"}\0{"id":3}\0'
        whole = P.Framer(64).feed(stream)
        self.assertEqual(whole, [b'{"id":1}', b'{"id":2,"method":"x"}', b'{"id":3}'])
        for cut in range(len(stream) + 1):
            f = P.Framer(64)
            got = f.feed(stream[:cut]) + f.feed(stream[cut:])
            self.assertEqual(got, whole, cut)
        f = P.Framer(64)
        got = []
        for i in range(len(stream)):
            got += f.feed(stream[i:i + 1])
        self.assertEqual(got, whole)

    def test_oversize_raises(self):
        with self.assertRaises(P.Oversize):
            P.Framer(8).feed(b"a" * 9)
        f = P.Framer(8)
        self.assertEqual(f.feed(b"a" * 8 + b"\0"), [b"a" * 8])


ALLOWED = [
    ("Target.getTargets", {}, None),
    ("Target.setAutoAttach", {"autoAttach": True, "waitForDebuggerOnStart": False, "flatten": True}, ""),
    ("Accessibility.enable", None, "S1"),
    ("Accessibility.getFullAXTree", None, "S1"),
    ("DOMSnapshot.enable", None, "S1"),
    ("DOMSnapshot.captureSnapshot", {"computedStyles": ["visibility", "opacity", "display"]}, "S1"),
    ("PerformanceTimeline.enable", {"eventTypes": ["layout-shift"]}, "S1"),
    ("Page.enable", None, "S1"),
    ("Page.getFrameTree", None, "S1"),
    ("Page.setLifecycleEventsEnabled", {"enabled": True}, "S1"),
    ("Page.navigate", {"url": URL}, "S1"),
]


class Policy(unittest.TestCase):
    def test_allowed_pass(self):
        pol = bound_policy()
        for i, (m, params, sid) in enumerate(ALLOWED, 10):
            ok, why = pol.check_request(req(m, i, params, sid))
            self.assertTrue(ok, (m, why))

    def test_refused(self):
        pol = bound_policy()
        bad = [
            req("Input.dispatchMouseEvent", 20, {"type": "mousePressed"}),
            req("Input.insertText", 21, {"text": "x"}),
            req("Runtime.evaluate", 22, {"expression": "1"}),
            req("Page.navigate", 23, {"url": "file:///etc/passwd"}),
            req("Page.navigate", 24, {"url": URL + "x"}),
            req("Page.navigate", 25, {"url": URL, "referrer": "x"}),
            req("Accessibility.enableX", 26),
            req("accessibility.enable", 27),
            req("Accessibility.enable ", 28),
            req("Accessibility.enable\u0000x", 29),
            req("Page.reload", 30), req("Page.captureScreenshot", 31), req("Page.startScreencast", 32),
            req("Emulation.setDeviceMetricsOverride", 33, {}), req("Network.enable", 34), req("Fetch.enable", 35),
            req("Debugger.enable", 36), req("DOM.setAttributeValue", 37, {}), req("Browser.close", 38, None, ""),
            req("Target.attachToTarget", 39, {"targetId": "T1", "flatten": True}, ""),
            req("Target.sendMessageToTarget", 40, {"message": "{}"}, ""),
            req("Target.createTarget", 41, {"url": "about:blank"}, ""),
            req("Target.closeTarget", 42, {"targetId": "T1"}, ""),
            req("Target.exposeDevToolsProtocol", 43, {"targetId": "T1"}, ""),
            req("Target.setAutoAttach", 44, {"autoAttach": True, "waitForDebuggerOnStart": True, "flatten": True}, ""),
            req("Target.setAutoAttach", 45, {"autoAttach": True, "waitForDebuggerOnStart": False, "flatten": False}, ""),
            req("Target.setAutoAttach", 46, {"autoAttach": 1, "waitForDebuggerOnStart": 0, "flatten": 1}, ""),
            req("Target.setAutoAttach", 47, {"autoAttach": True, "waitForDebuggerOnStart": False, "flatten": True, "filter": []}, ""),
            req("PerformanceTimeline.enable", 48, {"eventTypes": ["layout-shift", "largest-contentful-paint"]}),
            req("PerformanceTimeline.enable", 49, {"eventTypes": ["largest-contentful-paint"]}),
            req("PerformanceTimeline.enable", 50),
            req("DOMSnapshot.captureSnapshot", 51, {"computedStyles": ["background-image"]}),
            req("Accessibility.enable", 52, None, "NOSESSION"),
            req("Accessibility.enable", 53, None, ""),
            req("Target.getTargets", 54, {}, "S1"),
            req("Target.getTargetsX", 60, {}, ""), req("target.gettargets", 61, {}, ""), req("Target.getTarget", 62, {}, ""),
            b'{"id":55,"method":"Accessibility.enable","sessionId":"S1","extra":1}',
            b'{"id":56,"method":"Accessibility.enable","method":"Runtime.evaluate","sessionId":"S1"}',
            b'{"id":57,"method":"Runtime.evaluate","method":"Accessibility.enable","sessionId":"S1"}',
            b'["Accessibility.enable"]', b'not json', b'', b'{"id":58}', b'{"id":"x","method":"Page.enable","sessionId":"S1"}',
            b'{"id":true,"method":"Page.enable","sessionId":"S1"}',
            b'{"id":59,"method":"Page.enable","sessionId":"S1","params":NaN}',
            b'\xff\xfe',
        ]
        for f in bad:
            ok, why = pol.check_request(f)
            self.assertFalse(ok, f)
            self.assertTrue(why, f)

    def test_duplicate_id_refused(self):
        pol = bound_policy()
        self.assertTrue(pol.check_request(req("Page.enable", 7))[0])
        self.assertFalse(pol.check_request(req("Page.enable", 7))[0])

    def test_nul_in_wire_stream_splits_into_refused_frames(self):
        pol = bound_policy()
        frames = P.Framer(1 << 20).feed(req("Accessibility.enable", 5) [:-1] + b'\0x"}\0')
        self.assertTrue(frames)
        self.assertTrue(any(not pol.check_request(f)[0] for f in frames))

    def test_events(self):
        pol = bound_policy()
        ev = lambda m, sid="S1", **k: json.dumps(dict({"method": m, "params": {}, "sessionId": sid}, **k)).encode()
        self.assertEqual(pol.inbound(ev("Page.loadEventFired"))[0], "forward")
        self.assertEqual(pol.inbound(ev("PerformanceTimeline.timelineEventAdded"))[0], "forward")
        for m in ("Runtime.consoleAPICalled", "Network.requestWillBeSent", "Target.targetCreated", "Input.x", "page.loadEventFired", "Page.loadEventFired\u0000x"):
            self.assertEqual(pol.inbound(ev(m))[0], "drop", m)
        self.assertEqual(pol.inbound(ev("Page.loadEventFired", "OTHER"))[0], "drop")
        self.assertEqual(pol.inbound(b"not json")[0], "violation")

    def test_attach_binding(self):
        pol = P.Policy(P.load_allowlist(AL), URL)
        mk = lambda t, u, s: json.dumps({"method": "Target.attachedToTarget", "params": {"sessionId": s, "waitingForDebugger": False, "targetInfo": {"targetId": "T", "type": t, "url": u}}}).encode()
        self.assertEqual(pol.inbound(mk("iframe", URL, "S2"))[0], "drop")
        self.assertEqual(pol.inbound(mk("page", "about:blank", "S3"))[0], "drop")
        self.assertEqual(pol.inbound(mk("page", URL, "S4"))[0], "forward")
        self.assertFalse(pol.check_request(req("Page.enable", 1, None, "S2"))[0])
        self.assertFalse(pol.check_request(req("Page.enable", 2, None, "S3"))[0])
        self.assertTrue(pol.check_request(req("Page.enable", 3, None, "S4"))[0])

    def test_attach_blank_then_info_changed_binds_scene_page(self):
        """ADC-W6A-04: a page announced at about:blank is tracked until Target.targetInfoChanged reaches the scene url."""
        pol = P.Policy(P.load_allowlist(AL), URL)
        att = lambda t, u, s, tid="T1": json.dumps({"method": "Target.attachedToTarget", "params": {"sessionId": s, "waitingForDebugger": False, "targetInfo": {"targetId": tid, "type": t, "url": u}}}).encode()
        chg = lambda t, u, tid="T1": json.dumps({"method": "Target.targetInfoChanged", "params": {"targetInfo": {"targetId": tid, "type": t, "url": u}}}).encode()
        self.assertEqual(pol.inbound(att("page", "about:blank", "S1"))[0], "drop")
        self.assertEqual(pol.inbound(chg("page", "about:blank"))[0], "drop")         # still blank: nothing bound
        self.assertFalse(pol.check_request(req("Page.enable", 1, None, "S1"))[0])
        self.assertEqual(pol.inbound(chg("page", URL))[0], "forward")
        self.assertTrue(pol.check_request(req("Page.enable", 2, None, "S1"))[0])
        self.assertEqual(len(pol.pre), 1)                                              # synthesised attachedToTarget for the client
        syn = json.loads(pol.pre[0])
        self.assertEqual(syn["method"], "Target.attachedToTarget")
        self.assertEqual((syn["params"]["sessionId"], syn["params"]["targetInfo"]["url"], syn["params"]["targetInfo"]["type"]), ("S1", URL, "page"))
        self.assertEqual(pol.last_session, ("S1", "T1"))

    def test_info_changed_never_binds_other_urls_unknown_targets_or_iframes(self):
        pol = P.Policy(P.load_allowlist(AL), URL)
        att = lambda t, u, s, tid: json.dumps({"method": "Target.attachedToTarget", "params": {"sessionId": s, "waitingForDebugger": False, "targetInfo": {"targetId": tid, "type": t, "url": u}}}).encode()
        chg = lambda t, u, tid: json.dumps({"method": "Target.targetInfoChanged", "params": {"targetInfo": {"targetId": tid, "type": t, "url": u}}}).encode()
        pol.inbound(att("page", "about:blank", "S1", "T1"))
        pol.inbound(att("iframe", "about:blank", "S2", "T2"))
        self.assertEqual(pol.inbound(chg("page", "https://evil.example/", "T1"))[0], "drop")
        self.assertEqual(pol.inbound(chg("iframe", URL, "T2"))[0], "drop")
        self.assertEqual(pol.inbound(chg("page", URL, "T9"))[0], "drop")             # never announced
        self.assertEqual(pol.inbound(chg("service_worker", URL, "T1"))[0], "drop")   # type must be page
        for sid in ("S1", "S2"):
            self.assertFalse(pol.check_request(req("Page.enable", 5, None, sid))[0], sid)
        self.assertEqual(pol.pre, [])
        self.assertEqual(pol.inbound(b'{"method":"Target.targetInfoChanged","params":{}}')[0], "drop")

    def test_bound_page_navigating_away_is_unbound(self):
        pol = bound_policy()
        chg = lambda u: json.dumps({"method": "Target.targetInfoChanged", "params": {"targetInfo": {"targetId": "T1", "type": "page", "url": u}}}).encode()
        self.assertEqual(pol.inbound(chg(URL))[0], "forward")                          # same url: read-only event passes
        self.assertEqual(pol.inbound(chg("https://evil.example/"))[0], "drop")
        self.assertFalse(pol.check_request(req("Page.enable", 7))[0])                  # session no longer bound

    def test_candidate_table_is_bounded(self):
        pol = P.Policy(P.load_allowlist(AL), URL)
        for i in range(200):
            pol.inbound(json.dumps({"method": "Target.attachedToTarget", "params": {"sessionId": "S%d" % i, "targetInfo": {"targetId": "T%d" % i, "type": "page", "url": "about:blank"}}}).encode())
        self.assertLessEqual(len(pol.candidates), 16)

    def test_response_only_for_forwarded_ids(self):
        pol = bound_policy()
        self.assertTrue(pol.check_request(req("Page.enable", 8))[0])
        self.assertEqual(pol.inbound(b'{"id":8,"result":{}}')[0], "forward")
        self.assertEqual(pol.inbound(b'{"id":99,"result":{}}')[0], "drop")

    def test_allowlist_file_closed(self):
        al = P.load_allowlist(AL)
        self.assertEqual(al["schema"], "os.cu.oracle-allowlist/v1")
        self.assertEqual(al["max_frame_bytes"], 100 * 1024 * 1024)
        names = set(al["browser_methods"]) | set(al["session_methods"])
        self.assertEqual(names, {m for m, _, _ in ALLOWED})
        self.assertEqual(sorted(al["event_methods"]), ["Target.attachedToTarget", "Target.targetInfoChanged"])
        for forbidden in ("Runtime.", "Input.", "Emulation.", "Network.", "Fetch.", "Debugger.", "Browser."):
            self.assertFalse([n for n in names if n.startswith(forbidden)], forbidden)


class Serve:
    """proxy `serve` with the fake chromium behind it; client side talks over the two FIFOs."""
    def __init__(self, tc, mode="echo", extra=(), env=None):
        self.tc = tc
        self.d = tempfile.mkdtemp(prefix="oscu-px.", dir="/tmp/os-cu-0-w6p" if os.path.isdir("/tmp/os-cu-0-w6p") else None)
        self.log = os.path.join(self.d, "fake.log")
        e = dict(os.environ, FAKE_MODE=mode, FAKE_LOG=self.log, FAKE_URL=URL, **(env or {}))
        self.p = subprocess.Popen([PY, "-B", os.path.join(ORA, "cdp_proxy.py"), "serve", "--allowlist", AL, "--scene-url", URL,
                                   "--fifo-dir", self.d, "--ttl", "20", *extra, "--", PY, "-B", os.path.join(FX, "fake_chromium.py")],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=e)
        self.fi = os.path.join(self.d, "oracle.in")
        self.fo = os.path.join(self.d, "oracle.out")
        t = time.time() + 10
        while not (os.path.exists(self.fi) and os.path.exists(self.fo)):
            if time.time() > t or self.p.poll() is not None:
                break
            time.sleep(0.02)
        self.w = self.r = None
        if os.path.exists(self.fo):
            self.r = os.open(self.fo, os.O_RDONLY | os.O_NONBLOCK)
            self.w = os.open(self.fi, os.O_WRONLY)
        self.buf = b""

    def send(self, b):
        os.write(self.w, b + b"\0")

    def recv(self, timeout=5):
        end = time.time() + timeout
        while b"\0" not in self.buf:
            left = end - time.time()
            if left <= 0:
                return None
            if select.select([self.r], [], [], left)[0]:
                c = os.read(self.r, 65536)
                if not c:
                    return None
                self.buf += c
        f, self.buf = self.buf.split(b"\0", 1)
        return json.loads(f)

    def finish(self, timeout=15):
        try:
            out, err = self.p.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.p.kill()
            out, err = self.p.communicate()
            self.tc.fail("proxy did not exit")
        for fd in (self.w, self.r):
            if fd is not None:
                os.close(fd)
        rows = [json.loads(l) for l in out.decode().splitlines() if l.strip()]
        return self.p.returncode, rows, err

    def seen(self):
        if not os.path.exists(self.log):
            return b""
        with open(self.log, "rb") as f:
            return f.read()


class EndToEnd(unittest.TestCase):
    def test_allowed_flow_reaches_chromium_and_denied_events_are_dropped(self):
        s = Serve(self)
        self.assertIsNotNone(s.w, "fifos not created")
        for n in ("oracle.in", "oracle.out"):
            st = os.stat(os.path.join(s.d, n))
            self.assertTrue(stat.S_ISFIFO(st.st_mode))
            self.assertEqual(stat.S_IMODE(st.st_mode), 0o600)
            self.assertEqual(st.st_uid, os.getuid())
        s.send(req("Target.setAutoAttach", 1, {"autoAttach": True, "waitForDebuggerOnStart": False, "flatten": True}, ""))
        got = [s.recv(), s.recv()]
        self.assertTrue(all(g is not None for g in got), got)
        self.assertEqual([g.get("method") for g in got if "method" in g], ["Target.attachedToTarget"])  # consoleAPICalled dropped
        self.assertIn({"id": 1, "result": {}}, got)
        s.send(req("Accessibility.enable", 2))
        r = s.recv()
        self.assertEqual(r, {"id": 2, "result": {}})
        os.close(s.w); s.w = None
        rc, rows, err = s.finish()
        self.assertEqual(rc, 0, err)
        self.assertEqual(rows[-1]["kind"], "summary")
        self.assertGreaterEqual(rows[-1]["events_dropped"], 2)
        self.assertEqual(rows[-1]["refused"], 0)
        self.assertEqual(s.seen().count(b"\n"), 2)

    def test_attach_blank_stream_binds_after_navigation(self):
        s = Serve(self, mode="attach_blank")
        s.send(req("Target.setAutoAttach", 1, {"autoAttach": True, "waitForDebuggerOnStart": False, "flatten": True}, ""))
        got = [s.recv(), s.recv()]
        self.assertTrue(all(g is not None for g in got), got)
        att = [g for g in got if g.get("method") == "Target.attachedToTarget"]
        self.assertEqual(len(att), 1, got)
        self.assertEqual((att[0]["params"]["sessionId"], att[0]["params"]["targetInfo"]["url"]), ("S1", URL))
        s.send(req("Accessibility.enable", 2))
        self.assertEqual(s.recv(), {"id": 2, "result": {}})
        os.close(s.w); s.w = None
        rc, rows, err = s.finish()
        self.assertEqual(rc, 0, err)
        self.assertEqual([r["sessionId"] for r in rows if r["kind"] == "session"], ["S1"])

    def test_attach_blank_navigating_elsewhere_never_binds(self):
        s = Serve(self, mode="attach_other")
        s.send(req("Target.setAutoAttach", 1, {"autoAttach": True, "waitForDebuggerOnStart": False, "flatten": True}, ""))
        got = [s.recv(), s.recv(timeout=1)]
        self.assertEqual([g for g in got if g and "method" in g], [])                  # no attach event leaks to the client
        s.send(req("Accessibility.enable", 2))                                         # session never bound: refused
        rc, rows, err = s.finish()
        self.assertEqual(rc, 5)
        self.assertEqual(sum(1 for r in rows if r["kind"] == "refusal"), 1)
        self.assertNotIn(b"Accessibility.enable", s.seen())

    def test_refusal_is_rc5_sticky_and_never_reaches_chromium(self):
        s = Serve(self)
        s.send(req("Input.dispatchMouseEvent", 9, {"type": "mousePressed", "x": 1, "y": 1}, ""))
        e = s.recv()
        self.assertIsNotNone(e)
        self.assertEqual(e["id"], 9)
        self.assertIn("error", e)
        rc, rows, err = s.finish()
        self.assertEqual(rc, 5, err)
        ref = [r for r in rows if r["kind"] == "refusal"]
        self.assertEqual(len(ref), 1)
        self.assertEqual(ref[0]["method"], "Input.dispatchMouseEvent")
        self.assertEqual(ref[0]["schema"], "os.cu.oracle-proxy/v1")
        self.assertEqual(rows[-1]["refused"], 1)
        self.assertNotIn(b"Input.", s.seen())
        self.assertFalse(os.path.exists(s.fi))   # fifos removed at exit

    def test_oversize_client_frame_rc5(self):
        s = Serve(self, extra=("--max-frame", "256"))
        os.write(s.w, b"a" * 300)
        rc, rows, err = s.finish()
        self.assertEqual(rc, 5, err)
        self.assertTrue([r for r in rows if r["kind"] == "refusal" and r["reason"].startswith("oversize")])

    def test_oversize_chromium_frame_rc5(self):
        s = Serve(self, mode="oversize", extra=("--max-frame", "256"))
        rc, rows, err = s.finish()
        self.assertEqual(rc, 5, err)

    def test_malformed_chromium_frame_rc5(self):
        s = Serve(self, mode="badjson")
        rc, rows, err = s.finish()
        self.assertEqual(rc, 5, err)

    def test_nul_split_method_refused(self):
        s = Serve(self)
        os.write(s.w, req("Accessibility.enable", 3)[:-1].replace(b'enable', b'enable\\u0000x') + b"\0")
        rc, rows, err = s.finish()
        self.assertEqual(rc, 5, err)

    def test_no_unix_listener_created_by_proxy(self):
        s = Serve(self)
        s.send(req("Target.getTargets", 1, {}, ""))
        self.assertIsNotNone(s.recv())
        with open("/proc/net/unix") as f:
            txt = f.read().splitlines()[1:]
        mine = [l for l in txt if s.d in l]
        self.assertEqual(mine, [])
        os.close(s.w); s.w = None
        self.assertEqual(s.finish()[0], 0)

    def test_usage_rc2_never_1(self):
        for a in ([], ["serve"], ["bogus"], ["serve", "--allowlist", "/nonexistent", "--scene-url", URL, "--fifo-dir", "/tmp", "--", "true"]):
            rc = subprocess.run([PY, "-B", os.path.join(ORA, "cdp_proxy.py"), *a], capture_output=True, timeout=20).returncode
            self.assertEqual(rc, 2, a)


def run_shim(args, mid=None, env_extra=None, hook=None):
    out = tempfile.mktemp(prefix="oscu-shim.", dir="/tmp/os-cu-0-w6p" if os.path.isdir("/tmp/os-cu-0-w6p") else None)
    r, w = os.pipe()
    r2, w2 = os.pipe()
    def pre():
        os.dup2(r, 3); os.dup2(w2, 4); os.set_inheritable(3, True); os.set_inheritable(4, True)
        if hook:
            hook()
    env = dict(os.environ, OS_ORACLE_REAL_CHROMIUM=os.path.join(FX, "fake_real_chromium.py"), FAKE_OUT=out, **(env_extra or {}))
    leak = ["sh", "-c", 'exec %d</dev/null; exec "$0" "$@"' % mid] if mid is not None else []   # a leaked inheritable fd
    p = subprocess.run([*leak, PY, "-B", os.path.join(ORA, "bin", "chromium"), *args], env=env, preexec_fn=pre, close_fds=False, capture_output=True, timeout=30)
    for fd in (r, w, r2, w2):
        os.close(fd)
    rec = json.load(open(out)) if os.path.exists(out) else None
    return p.returncode, rec


class Shim(unittest.TestCase):
    def test_exactly_one_pipe_flag_appended_and_fds_exact(self):
        rc, rec = run_shim(["--kiosk", URL], mid=7)
        self.assertEqual(rc, 0)
        self.assertIsNotNone(rec)
        self.assertEqual(rec["argv"].count("--remote-debugging-pipe"), 1)
        self.assertEqual(rec["argv"][:2], ["--kiosk", URL])
        self.assertFalse([a for a in rec["argv"] if "remote-debugging-port" in a or "remote-debugging-address" in a])
        self.assertEqual(rec["fds"], [0, 1, 2, 3, 4])      # leaked fd 7 closed before exec

    def test_port_address_or_second_pipe_refused(self):
        for bad in ("--remote-debugging-port=9222", "--remote-debugging-address=0.0.0.0", "--remote-debugging-pipe", "--remote-debugging-port"):
            rc, rec = run_shim([bad, URL])
            self.assertEqual(rc, 2, bad)
            self.assertIsNone(rec, bad)

    def test_fd3_not_fifo_is_rc5(self):
        def hook():
            os.dup2(os.open("/dev/null", os.O_RDONLY), 3)   # char device, not a FIFO
        rc, rec = run_shim([URL], hook=hook)
        self.assertEqual(rc, 5)
        self.assertIsNone(rec)


class ArgvTable(unittest.TestCase):
    """ADC-W6A-07: table-driven argv refusal (docker/cu/oracle/argv_refusal.json), shim and argv_policy agree."""
    T = json.load(open(os.path.join(ORA, "argv_refusal.json")))
    AP = _load("argv_policy")

    def test_shim_refuses_every_spelling_rc2(self):
        for args in self.T["input_refuse"]:
            rc, rec = run_shim([*args, URL])
            self.assertEqual(rc, 2, args)
            self.assertIsNone(rec, args)

    def test_shim_accepts_clean_argv_and_appends_one_pipe(self):
        for args in self.T["input_accept"]:
            rc, rec = run_shim(args)
            self.assertEqual(rc, 0, args)
            self.assertEqual(rec["argv"], [*args, "--remote-debugging-pipe"])

    def test_policy_matches_table(self):
        for args in self.T["input_refuse"]:
            self.assertTrue(self.AP.refuse_input(args), args)
        for args in self.T["input_accept"]:
            self.assertFalse(self.AP.refuse_input(args), args)
        for args in self.T["observed_ok"]:
            self.assertTrue(self.AP.observed_ok(args), args)
        for args in self.T["observed_bad"]:
            self.assertFalse(self.AP.observed_ok(args), args)


class ShimFds(unittest.TestCase):
    def test_shim_refuses_missing_pipe_fds(self):
        """JW6A-04: fd 3/4 closed -> rc 5 (never 1) with an fd-table message."""
        env = dict(os.environ, OS_ORACLE_REAL_CHROMIUM="/bin/true")
        p = subprocess.run([PY, "-B", os.path.join(ORA, "bin", "chromium"), "--x"], env=env, close_fds=True, capture_output=True, timeout=30)
        self.assertEqual(p.returncode, 5)
        self.assertIn(b"fd table", p.stderr)


class NoListener(unittest.TestCase):
    def setUp(self):
        # OD-2 owner/mode assertion: a scratch fs root holding every baseline socket path, owned by us (uid = our uid), dirs 0700
        self.fs = tempfile.mkdtemp(prefix="nolisten_fs_")
        self.addCleanup(shutil.rmtree, self.fs, True)
        for p in ("/run/newton-tenant/bus", "/tmp/chromium-123.AbC/SingletonSocket", "/tmp/org.chromium.Chromium.3PRvjo/SingletonSocket"):
            d = os.path.join(self.fs, os.path.dirname(p).lstrip("/"))
            os.makedirs(d, exist_ok=True)
            os.chmod(d, 0o700)
            open(os.path.join(self.fs, p.lstrip("/")), "w").close()

    def scan(self, proc, tmp="tmp_clean", baseline=False, uid=None):
        a = [PY, "-B", os.path.join(ORA, "no_listener.py"), "--proc-root", os.path.join(FX, proc), "--tmp-root", os.path.join(FX, tmp),
             "--fs-root", self.fs, "--baseline-uid", str(os.getuid() if uid is None else uid)]
        if baseline:
            a.append("--allow-capsule-baseline")
        p = subprocess.run(a, capture_output=True, timeout=20)
        return p.returncode, (json.loads(p.stdout) if p.stdout.strip() else None)

    def test_clean_is_rc0(self):
        rc, j = self.scan("proc_clean")
        self.assertEqual(rc, 0)
        self.assertEqual(j["tcp_listen"], [])
        self.assertEqual(j["unix_listen"], [])

    def test_tcp_0A_rc5(self):
        rc, j = self.scan("proc_tcp_listen")
        self.assertEqual(rc, 5)
        self.assertEqual(len(j["tcp_listen"]), 1)

    def test_tcp6_0A_rc5(self):
        self.assertEqual(self.scan("proc_tcp6_listen")[0], 5)

    def test_unix_acceptcon_rc5(self):
        rc, j = self.scan("proc_unix_listen")
        self.assertEqual(rc, 5)
        self.assertEqual(j["unix_listen"], ["/tmp/evil/listen.sock"])
        self.assertEqual(self.scan("proc_unix_listen", baseline=True)[0], 5)

    def test_baseline_only_with_flag(self):
        self.assertEqual(self.scan("proc_unix_baseline")[0], 5)
        rc, j = self.scan("proc_unix_baseline", baseline=True)
        self.assertEqual(rc, 0)
        self.assertEqual(len(j["baseline_unix"]), 2)

    def test_abstract_and_devtools_never_baseline(self):
        """OD-2: only the exact Xvfb abstract @/tmp/.X11-unix/X100 is tolerated (with the flag); any other abstract name is rc 5."""
        self.assertEqual(self.scan("proc_abstract")[0], 5)
        rc, j = self.scan("proc_abstract", baseline=True)
        self.assertEqual(rc, 0)
        self.assertEqual(j["baseline_unix"], ["@/tmp/.X11-unix/X100"])
        rc, j = self.scan("proc_abstract_other", baseline=True)
        self.assertEqual(rc, 5)
        self.assertEqual(j["unix_listen"], ["@/tmp/.X11-unix/X99", "@/tmp/.X11-unix/X1000"])
        self.assertEqual(self.scan("proc_devtools", baseline=True)[0], 5)

    def test_od2_capsule_baseline_tolerated_when_owner_and_mode_hold(self):
        """OD-2 option A: the measured capsule listeners (SingletonSocket uid 10001 in a 0700 dir, abstract X100) pass."""
        self.assertEqual(self.scan("proc_unix_od2")[0], 5)
        rc, j = self.scan("proc_unix_od2", baseline=True)
        self.assertEqual(rc, 0, j)
        self.assertEqual(j["baseline_unix"], ["/tmp/org.chromium.Chromium.3PRvjo/SingletonSocket", "@/tmp/.X11-unix/X100"])
        self.assertEqual(j["baseline_drift"], [])

    def test_od2_owner_or_mode_drift_rc5(self):
        """OD-2 permanent assertion: wrong uid, a dir mode other than 0700, or a missing socket file stops rc 5."""
        rc, j = self.scan("proc_unix_od2", baseline=True, uid=os.getuid() + 1)
        self.assertEqual(rc, 5)
        self.assertIn("baseline owner/mode drift", j["violations"])
        d = os.path.join(self.fs, "tmp/org.chromium.Chromium.3PRvjo")
        os.chmod(d, 0o750)
        rc, j = self.scan("proc_unix_od2", baseline=True)
        self.assertEqual(rc, 5)
        self.assertEqual(j["baseline_drift"], ["/tmp/org.chromium.Chromium.3PRvjo/SingletonSocket"])
        os.chmod(d, 0o700)
        os.unlink(os.path.join(d, "SingletonSocket"))
        self.assertEqual(self.scan("proc_unix_od2", baseline=True)[0], 5)

    def test_devtools_active_port_file_rc5(self):
        rc, j = self.scan("proc_clean", tmp="tmp_dap", baseline=True)
        self.assertEqual(rc, 5)
        self.assertEqual(len(j["devtools_port_files"]), 1)

    def test_debug_named_baseline_shaped_socket_rc5(self):
        """JW6A-05: a /tmp/chromium-*/SingletonSocket-shaped path with 'debug' in the name is never baseline."""
        rc, j = self.scan("proc_unix_debugname", baseline=True)
        self.assertEqual(rc, 5)
        self.assertEqual(j["unix_listen"], ["/tmp/chromium-debugX/SingletonSocket"])
        self.assertEqual(j["baseline_unix"], [])

    def test_orgchromium_temp_socket_needs_od2_rc5(self):
        """JW6A-03 / OD-2: a dot-prefixed /tmp/.org.chromium.Chromium.* dir is NOT the approved shape (only the measured
        /tmp/org.chromium.Chromium.XXXXXX/SingletonSocket is, see test_od2_*). Fixture proc_unix_orgchromium stays rc 5."""
        self.assertEqual(self.scan("proc_unix_orgchromium")[0], 5)
        rc, j = self.scan("proc_unix_orgchromium", baseline=True)
        self.assertEqual(rc, 5)
        self.assertEqual(j["unix_listen"], ["/tmp/.org.chromium.Chromium.AbC123/SingletonSocket"])

    def test_missing_proc_fails_closed_rc5(self):
        self.assertEqual(self.scan("does_not_exist")[0], 5)


if __name__ == "__main__":
    unittest.main()
