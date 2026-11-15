#!/usr/bin/env python3
"""Default-deny CDP allow-list proxy between the oracle sampler and Chromium (W6a lane P). Python 3 stdlib only.

serve: creates 0600 FIFOs <fifo-dir>/oracle.in (sampler -> proxy) and oracle.out (proxy -> sampler) (no unix listener),
spawns the launcher command with pipe ends dup2'd to fd 3 (chromium reads) / fd 4 (chromium writes), and relays
NUL-framed CDP JSON after policy checks. Any refusal / oversize / protocol violation is sticky: CDP error to the client,
a refusal row on stdout, the child's process group is terminated and the proxy exits rc 5. Exit codes: 0, 2 usage, 5 failure
(never 1; the child's rc 1 is mapped to 5). Stdout rows: schema os.cu.oracle-proxy/v1, kind refusal|session|summary."""
import argparse, fcntl, json, os, re, signal, stat, sys, threading, time

DEFAULT_MAX = 100 * 1024 * 1024  # M, OS_DISTIL L14 (http_connection.cc 100 MiB)
ROW_SCHEMA = "os.cu.oracle-proxy/v1"
AL_SCHEMA = "os.cu.oracle-allowlist/v1"
_AL_KEYS = {"schema", "max_frame_bytes", "frame_keys", "browser_methods", "session_methods", "event_domains", "event_methods", "refused_explicit"}
MAX_CANDIDATES = 16
_EVENT_RE = re.compile(r"^[A-Za-z]+\.[A-Za-z0-9]+$")


class Oversize(Exception):
    pass


class Violation(Exception):
    pass


def frame_status(buf, max_payload=DEFAULT_MAX):
    """(1, n): a frame of n payload bytes; (0, 0): need more; (-1, 0): oversize. lim = min(L, M+1) (SW-0217 s4.1)."""
    lim = min(len(buf), max_payload + 1)
    i = buf.find(b"\0", 0, lim)
    if i >= 0:
        return 1, i
    return (-1, 0) if len(buf) > max_payload else (0, 0)


class Framer:
    """Incremental NUL framing; the result does not depend on how the stream is cut into chunks."""
    def __init__(self, max_payload=DEFAULT_MAX):
        self.max = max_payload
        self.buf = bytearray()
        self.scanned = 0

    def feed(self, data):
        self.buf += data
        out = []
        while True:
            lim = min(len(self.buf), self.max + 1)
            i = self.buf.find(b"\0", self.scanned, lim)
            if i >= 0:
                out.append(bytes(self.buf[:i]))
                del self.buf[:i + 1]
                self.scanned = 0
                continue
            if len(self.buf) > self.max:
                raise Oversize("oversize: more than %d payload bytes without NUL" % self.max)
            self.scanned = len(self.buf)
            return out


def load_allowlist(path):
    with open(path, "rb") as f:
        al = json.loads(f.read().decode("utf-8"))
    if not isinstance(al, dict) or set(al) != _AL_KEYS or al["schema"] != AL_SCHEMA:
        raise ValueError("allowlist: schema/keys")
    if not isinstance(al["max_frame_bytes"], int) or al["max_frame_bytes"] < 1:
        raise ValueError("allowlist: max_frame_bytes")
    return al


def _no_dups(pairs):
    d = {}
    for k, v in pairs:
        if k in d:
            raise ValueError("duplicate key")
        d[k] = v
    return d


def _bad_const(c):
    raise ValueError("non-finite constant")


def _parse(frame):
    try:
        return json.loads(frame.decode("utf-8"), object_pairs_hook=_no_dups, parse_constant=_bad_const)
    except Exception as e:  # UnicodeDecodeError, ValueError, RecursionError
        raise Violation("bad json frame: %s" % type(e).__name__)


def _canon(o):
    return json.dumps(o, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


class Policy:
    def __init__(self, allowlist, scene_url):
        self.al = allowlist
        self.url = scene_url
        self.sessions = set()
        self.pending = {}
        self.seen_ids = set()
        self.stats = {"requests_passed": 0, "refused": 0, "events_forwarded": 0, "events_dropped": 0, "responses": 0}
        self.last_session = None  # sessionId of the last row-worthy binding (for the caller's row)
        self.candidates = {}      # targetId -> (sessionId, targetInfo): page attaches announced at another url (ADC-W6A-04), bounded
        self.bound = {}           # targetId -> sessionId of the bound scene page
        self.pre = []             # frames the proxy synthesises for the client before the current forward (attachedToTarget)

    # client -> chromium
    def check_request(self, frame):
        """(ok, reason). Exact-length, exact-byte method match (dict key equality), params shape per method, session binding."""
        try:
            o = _parse(frame)
        except Violation as e:
            return False, str(e)
        if not isinstance(o, dict):
            return False, "frame is not an object"
        if not set(o) <= set(self.al["frame_keys"]):
            return False, "unknown frame key"
        i = o.get("id")
        if type(i) is not int or i < 0:
            return False, "id must be a non-negative integer"
        m = o.get("method")
        if not isinstance(m, str):
            return False, "method must be a string"
        if i in self.seen_ids:
            return False, "duplicate id"
        params = o.get("params")
        if "params" in o and not isinstance(params, dict):
            return False, "params must be an object"
        if "sessionId" in o:
            sid = o["sessionId"]
            if not isinstance(sid, str) or sid not in self.sessions:
                return False, "sessionId not bound to the scene page"
            spec = self.al["session_methods"].get(m)
        else:
            spec = self.al["browser_methods"].get(m)
        if spec is None:
            return False, "method not on the allow-list"
        why = self._params_ok(spec["params"], params)
        if why:
            return False, why
        self.seen_ids.add(i)
        self.pending[i] = m
        self.stats["requests_passed"] += 1
        return True, ""

    def _params_ok(self, rule, params):
        mode = rule["mode"]
        if mode == "empty":
            return "" if not params else "params must be empty"
        if mode == "exact":
            return "" if params is not None and _canon(params) == _canon(rule["value"]) else "params differ from the sealed value"
        if mode == "scene_url":
            return "" if params is not None and set(params) == {"url"} and params["url"] == self.url else "navigate url is not the launched scene url"
        if mode == "keys":
            params = params or {}
            allowed = rule["allowed"]
            if not set(params) <= set(allowed) or not set(rule["required"]) <= set(params):
                return "params keys"
            for k, v in params.items():
                t = allowed[k]
                if t == "int" and not (type(v) is int):
                    return "param type"
                if t == "bool" and not (type(v) is bool):
                    return "param type"
                if isinstance(t, list) and not (isinstance(v, list) and all(type(x) is str and x in t for x in v)):
                    return "param value not allowed"
            return ""
        return "unknown rule"

    # chromium -> client
    def inbound(self, frame):
        """('forward'|'drop'|'violation', reason). Events pass only for allowed domains on a bound page session + Target.attachedToTarget."""
        try:
            o = _parse(frame)
        except Violation as e:
            return "violation", str(e)
        if not isinstance(o, dict):
            return "violation", "frame is not an object"
        if "method" not in o:
            if type(o.get("id")) is int and ("result" in o or "error" in o):
                if o["id"] in self.pending:
                    del self.pending[o["id"]]
                    self.stats["responses"] += 1
                    return "forward", ""
                return self._drop("response to an id the proxy never forwarded")
            return "violation", "neither response nor event"
        m = o["method"]
        if not isinstance(m, str) or not _EVENT_RE.match(m):
            return self._drop("event method syntax")
        if m in self.al["event_methods"]:
            p = o.get("params")
            ti = p.get("targetInfo") if isinstance(p, dict) else None
            if not isinstance(ti, dict):
                return self._drop("target event without targetInfo")
            if m == "Target.attachedToTarget":
                sid = p.get("sessionId")
                if isinstance(sid, str) and ti.get("type") == "page" and ti.get("url") == self.url:
                    self._bind(sid, ti)
                    self.stats["events_forwarded"] += 1
                    return "forward", ""
                if isinstance(sid, str) and ti.get("type") == "page" and isinstance(ti.get("targetId"), str) and (len(self.candidates) < MAX_CANDIDATES or ti["targetId"] in self.candidates):
                    self.candidates[ti["targetId"]] = (sid, {"targetId": ti["targetId"], "type": "page", "url": ti.get("url") if isinstance(ti.get("url"), str) else ""})
                return self._drop("attach not a page on the scene url")
            if m == "Target.targetInfoChanged":   # read-only; never forwards a session of another url
                tid = ti.get("targetId")
                at_scene = ti.get("type") == "page" and ti.get("url") == self.url
                if tid in self.bound:
                    if not at_scene:
                        self.sessions.discard(self.bound.pop(tid))
                        return self._drop("bound page left the scene url")
                    self.stats["events_forwarded"] += 1
                    return "forward", ""
                if at_scene and tid in self.candidates:
                    sid, _ = self.candidates.pop(tid)
                    self._bind(sid, dict(ti))
                    self.pre = [json.dumps({"method": "Target.attachedToTarget", "params": {"sessionId": sid, "waitingForDebugger": False, "targetInfo": ti}}, sort_keys=True).encode()]
                    self.stats["events_forwarded"] += 1
                    return "forward", ""
                return self._drop("target info change not for a tracked scene page")
            return self._drop("unhandled target event")
        if m.split(".", 1)[0] in self.al["event_domains"] and isinstance(o.get("sessionId"), str) and o["sessionId"] in self.sessions:
            self.stats["events_forwarded"] += 1
            return "forward", ""
        return self._drop("event outside the allowed domains or session")

    def _bind(self, sid, ti):
        self.sessions.add(sid)
        self.bound[ti.get("targetId")] = sid
        self.candidates.pop(ti.get("targetId"), None)
        self.last_session = (sid, ti.get("targetId"))

    def _drop(self, why):
        self.stats["events_dropped"] += 1
        return "drop", why


def _write_all(fd, data):
    mv = memoryview(data)
    while mv:
        n = os.write(fd, mv)
        mv = mv[n:]


def _hi(fd):
    if fd >= 10:
        return fd
    n = fcntl.fcntl(fd, fcntl.F_DUPFD_CLOEXEC, 10)
    os.close(fd)
    return n


def _mkfifo(path):
    try:
        st = os.lstat(path)
        if not stat.S_ISFIFO(st.st_mode) or st.st_uid != os.getuid():
            raise OSError("not our FIFO: " + path)
        os.unlink(path)
    except FileNotFoundError:
        pass
    os.mkfifo(path, 0o600)
    os.chmod(path, 0o600)


class Serve:
    def __init__(self, policy, max_frame, out_fd, cw):
        self.pol, self.max, self.out_fd, self.cw = policy, max_frame, out_fd, cw
        self.lock = threading.Lock()
        self.wlock = threading.Lock()
        self.refused = False
        self.stop = threading.Event()
        self.seq = 0

    def row(self, **kw):
        with self.lock:
            self.seq += 1
            d = {"schema": ROW_SCHEMA, "seq": self.seq}
            d.update(kw)
            sys.stdout.write(json.dumps(d, sort_keys=True) + "\n")
            sys.stdout.flush()

    def to_client(self, payload):
        with self.wlock:
            try:
                _write_all(self.out_fd, payload + b"\0")
            except OSError:
                pass

    def fail(self, direction, method, reason, req_id=None, sid=None):
        with self.lock:
            if self.refused:
                return
            self.refused = True
            self.pol.stats["refused"] += 1
        self.row(kind="refusal", dir=direction, method=method if isinstance(method, str) else None, reason=reason[:200])
        if direction == "c2b" and type(req_id) is int:
            e = {"id": req_id, "error": {"code": -32601, "message": "oracle allow-list: refused"}}
            if isinstance(sid, str):
                e["sessionId"] = sid
            self.to_client(json.dumps(e).encode())
        self.stop.set()

    def c2b(self, fifo_in):
        try:
            fd = os.open(fifo_in, os.O_RDONLY)
            fr = Framer(self.max)
            while not self.stop.is_set():
                d = os.read(fd, 1 << 20)
                if not d:
                    break
                for f in fr.feed(d):
                    ok, why = self.pol.check_request(f)
                    if not ok:
                        m = rid = sid = None
                        try:
                            o = json.loads(f.decode("utf-8"))
                            if isinstance(o, dict):
                                m, rid, sid = o.get("method"), o.get("id"), o.get("sessionId")
                        except Exception:
                            pass
                        return self.fail("c2b", m, why, rid, sid)
                    _write_all(self.cw, f + b"\0")
        except Oversize as e:
            self.fail("c2b", None, str(e))
        except OSError:
            pass
        finally:
            try:
                os.close(self.cw)  # chromium sees EOF on fd 3
            except OSError:
                pass

    def b2c(self, br):
        fr = Framer(self.max)
        try:
            while not self.stop.is_set():
                d = os.read(br, 1 << 20)
                if not d:
                    break
                for f in fr.feed(d):
                    act, why = self.pol.inbound(f)
                    if act == "violation":
                        return self.fail("b2c", None, why)
                    if act == "forward":
                        if self.pol.last_session:
                            s, t = self.pol.last_session
                            self.pol.last_session = None
                            self.row(kind="session", sessionId=s, targetId=t)
                        pre, self.pol.pre = self.pol.pre, []
                        for x in pre:
                            self.to_client(x)
                        if not pre:  # a bind via targetInfoChanged is announced by the synthesised attachedToTarget instead
                            self.to_client(f)
        except Oversize as e:
            self.fail("b2c", None, str(e))
        except OSError:
            pass


def _kill_group(pid, status=None, grace=3.0):
    """TERM then KILL our own process group; reap the leader unless already reaped. Returns the leader's wait status."""
    def reap(block):
        r, st = os.waitpid(pid, 0 if block else os.WNOHANG)
        return st if r else None
    for sig, wait in ((signal.SIGTERM, grace), (signal.SIGKILL, 0.0)):
        try:
            os.killpg(pid, sig)
        except OSError:
            break
        t = time.time() + wait
        while time.time() < t:
            if status is None:
                status = reap(False)
            try:
                os.killpg(pid, 0)
            except OSError:
                break
            if status is not None and wait <= 1.0 and time.time() > t - wait + 0.2:
                break
            time.sleep(0.02)
    if status is None:
        status = reap(True)
    return status


def serve(a, cmd):
    os.umask(0o077)
    al = load_allowlist(a.allowlist)
    pol = Policy(al, a.scene_url)
    mx = a.max_frame or al["max_frame_bytes"]
    if not os.path.isdir(a.fifo_dir):
        print("cdp_proxy: fifo dir missing", file=sys.stderr)
        return 2
    fin, fout = os.path.join(a.fifo_dir, "oracle.in"), os.path.join(a.fifo_dir, "oracle.out")
    _mkfifo(fin)
    _mkfifo(fout)
    out_fd = os.open(fout, os.O_RDWR)  # never blocks; the sampler opens it read-only
    cr, cw = (_hi(x) for x in os.pipe())
    br, bw = (_hi(x) for x in os.pipe())
    for fd in (cw, br):
        try:
            fcntl.fcntl(fd, 1031, 1 << 20)  # F_SETPIPE_SZ, best effort
        except OSError:
            pass
    rc = 5
    sv = Serve(pol, mx, out_fd, cw)
    pid = None
    try:
        acts = [(os.POSIX_SPAWN_DUP2, cr, 3), (os.POSIX_SPAWN_DUP2, bw, 4), (os.POSIX_SPAWN_OPEN, 1, os.devnull, os.O_WRONLY, 0)]
        try:
            pid = os.posix_spawnp(cmd[0], cmd, os.environ, file_actions=acts, setpgroup=0)
        except OSError as e:
            print("cdp_proxy: spawn failed: %s" % e, file=sys.stderr)
            return 5
        os.close(cr)
        os.close(bw)
        tb = threading.Thread(target=sv.b2c, args=(br,), daemon=True)
        tc = threading.Thread(target=sv.c2b, args=(fin,), daemon=True)
        tb.start()
        tc.start()
        deadline = time.time() + a.ttl
        status = None
        ttl_hit = False
        while True:
            r, st = os.waitpid(pid, os.WNOHANG)
            if r:
                status = st
                break
            if sv.stop.is_set():
                break
            if time.time() > deadline:
                ttl_hit = True
                break
            time.sleep(0.02)
        status = _kill_group(pid, status, 3.0 if status is None else 1.0)  # our own group only
        tb.join(5)
        if ttl_hit:
            sv.fail("c2b", None, "ttl expired")
        if sv.refused:
            rc = 5
        elif os.WIFEXITED(status):
            c = os.WEXITSTATUS(status)
            rc = 5 if c == 1 else c
        else:
            rc = 128 + os.WTERMSIG(status)
        sv.row(kind="summary", rc=rc, **pol.stats)
        return rc
    finally:
        for p in (fin, fout):
            try:
                os.unlink(p)
            except OSError:
                pass


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    cmd = []
    if "--" in argv:
        k = argv.index("--")
        argv, cmd = argv[:k], argv[k + 1:]
    ap = argparse.ArgumentParser(prog="cdp_proxy.py")
    sub = ap.add_subparsers(dest="verb", required=True)
    s = sub.add_parser("serve")
    s.add_argument("--allowlist", required=True)
    s.add_argument("--scene-url", required=True)
    s.add_argument("--fifo-dir", default="/run/newton-tenant")
    s.add_argument("--max-frame", type=int, default=0)
    s.add_argument("--ttl", type=int, default=3600)
    a = ap.parse_args(argv)  # argparse usage errors exit 2
    if not cmd:
        print("cdp_proxy: serve needs -- <command...>", file=sys.stderr)
        return 2
    try:
        load_allowlist(a.allowlist)
    except (OSError, ValueError):
        print("cdp_proxy: allowlist unreadable or malformed", file=sys.stderr)
        return 2
    try:
        return serve(a, cmd)
    except SystemExit:
        raise
    except BaseException as e:
        print("cdp_proxy: E_PROXY %s: %s" % (type(e).__name__, e), file=sys.stderr)
        return 5


if __name__ == "__main__":
    sys.exit(main())
