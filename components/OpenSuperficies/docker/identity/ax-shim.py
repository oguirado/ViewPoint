#!/usr/bin/env python3
"""Private, pipe-only Chromium parent. Stdlib only; invoke with python3 -I.

The socket exposes a snapshot operation, never a CDP transport. Diagnostics are
fixed event names and counts only. Protocol violations poison the pipe until
restart; the parent then terminates the browser process group.
"""
import hashlib
import json
import math
import os
import re
from pathlib import Path
import select
import signal
import socket
import subprocess
import sys
import time
from urllib.parse import urlsplit, urlunsplit

MAX = 16 * 1024 * 1024
SOCKET = '/run/newton-tenant/ax.sock'
MAPPING = '/run/newton-tenant/ax-windows.json'
BROWSER_METHODS = {'Target.getTargets', 'Target.attachToTarget', 'Browser.getWindowForTarget'}
SESSION_METHODS = {'Accessibility.enable', 'Accessibility.getFullAXTree',
                   'DOMSnapshot.captureSnapshot', 'Page.getLayoutMetrics'}
LANDMARKS = {'main', 'navigation', 'banner', 'contentinfo', 'complementary', 'form', 'search', 'dialog'}
ACTIONS = {'button', 'link', 'textbox', 'combobox', 'checkbox', 'radio', 'menuitem', 'tab', 'option'}
REPEATED = {'article', 'listitem', 'row'}


class Refused(Exception):
    def __init__(self, code=6):
        self.code = code


def unique(pairs):
    result = {}
    for k, v in pairs:
        if k in result:
            raise Refused(5)
        result[k] = v
    return result


def parse(raw):
    try:
        return json.loads(raw, object_pairs_hook=unique,
                          parse_constant=lambda _: (_ for _ in ()).throw(Refused(5)))
    except (ValueError, UnicodeError, RecursionError):
        raise Refused(5) from None


class Framer:
    def __init__(self, limit=MAX):
        self.limit, self.buf, self.failed = limit, bytearray(), False

    def feed(self, data):
        if self.failed:
            raise Refused(5)
        self.buf.extend(data)
        result = []
        while True:
            i = self.buf.find(b'\0', 0, self.limit + 1)
            if i < 0:
                if len(self.buf) > self.limit:
                    self.failed = True
                    raise Refused(5)
                return result
            result.append(bytes(self.buf[:i]))
            del self.buf[:i + 1]


class Policy:
    def __init__(self, al):
        if (not isinstance(al, dict) or set(al) != {'schema', 'max_frame_bytes', 'browser_methods', 'session_methods'}
                or al['schema'] != 'opensuperficies.identity-ax-policy/v1'
                or type(al['max_frame_bytes']) is not int or al['max_frame_bytes'] != MAX
                or not isinstance(al['browser_methods'], list) or not isinstance(al['session_methods'], list)
                or set(al['browser_methods']) != BROWSER_METHODS
                or set(al['session_methods']) != SESSION_METHODS
                or len(al['browser_methods']) != len(BROWSER_METHODS)
                or len(al['session_methods']) != len(SESSION_METHODS)):
            raise Refused(5)
        self.targets, self.sessions, self.failed = set(), set(), False

    def check(self, method, params, session):
        valid = not self.failed and isinstance(params, dict)
        valid = valid and method in (SESSION_METHODS if session is not None else BROWSER_METHODS)
        valid = valid and (session is None or session in self.sessions)
        if valid:
            if method == 'Target.attachToTarget':
                valid = (set(params) == {'targetId', 'flatten'} and params['flatten'] is True
                         and params['targetId'] in self.targets)
            elif method == 'Browser.getWindowForTarget':
                # Read-only window id + bounds, only for this request's candidate pages.
                valid = set(params) == {'targetId'} and params['targetId'] in self.targets
            elif method == 'DOMSnapshot.captureSnapshot':
                valid = params == {'computedStyles': ['display', 'visibility', 'opacity']}
            else:
                valid = not params
        if not valid:
            self.failed = True
            print('{"event":"refusal","count":1}', file=sys.stderr, flush=True)
            raise Refused(6)


class Pipe:
    def __init__(self, read_fd, write_fd, policy):
        self.read_fd, self.write_fd, self.policy = read_fd, write_fd, policy
        self.framer, self.seq = Framer(), 0
        self.expired = {}

    def call(self, method, params=None, session=None):
        self.policy.check(method, {} if params is None else params, session)
        self.seq += 1
        request = {'id': self.seq, 'method': method, 'params': params or {}}
        if session is not None:
            request['sessionId'] = session
        payload = json.dumps(request, separators=(',', ':')).encode() + b'\0'
        # 2 s per CDP call: a long Perplexity thread needs more than 0.5 s for getFullAXTree (measured
        # live); four calls still fit the client's 10 s window.
        deadline = time.monotonic() + 2.0
        try:
            while payload:
                if not select.select([], [self.write_fd], [], max(0, deadline - time.monotonic()))[1]:
                    raise Refused(4)
                payload = payload[os.write(self.write_fd, payload):]
            while time.monotonic() < deadline:
                if not select.select([self.read_fd], [], [], max(0, deadline - time.monotonic()))[0]:
                    break
                chunk = os.read(self.read_fd, 65536)
                if not chunk:
                    raise Refused(4)
                answer = None
                for raw in self.framer.feed(chunk):
                    row = parse(raw)
                    if not isinstance(row, dict):
                        raise Refused(5)
                    if 'id' not in row:
                        continue  # Events are never exposed or logged.
                    if type(row['id']) is not int:
                        raise Refused(5)
                    if row['id'] in self.expired:
                        if row.get('sessionId') != self.expired.pop(row['id']):
                            raise Refused(5)
                        continue  # Only known timed-out ids may be drained.
                    if row['id'] != self.seq or answer is not None:
                        raise Refused(5)
                    if 'error' in row or not isinstance(row.get('result'), dict):
                        raise Refused(5)
                    if row.get('sessionId') != session:
                        raise Refused(5)
                    answer = row['result']
                if answer is not None:
                    return answer
            if len(self.expired) >= 16:
                raise Refused(5)
            self.expired[self.seq] = session
            raise Refused(4)
        except Refused as exc:
            # A fully written request may reply late; retain its id for draining.
            # Partial writes cannot safely be recovered.
            if exc.code == 4 and self.seq in self.expired and not payload:
                raise
            self.policy.failed = True
            raise Refused(5) from None
        except OSError:
            self.policy.failed = True
            raise Refused(5) from None


def safe_url(raw):
    """Public HTTP(S) location: never userinfo, query or fragment."""
    if not isinstance(raw, str):
        raise Refused(5)
    try:
        u = urlsplit(raw)
        if u.scheme.lower() not in {'http', 'https'} or not u.hostname:
            return ''
        _ = u.port  # Validate malformed ports before emitting metadata.
        return urlunsplit((u.scheme, u.netloc.rsplit('@', 1)[-1], u.path or '/', '', ''))
    except ValueError:
        return ''


def safe_title(raw):
    return re.sub(r'https?://[^\s]+', lambda m: safe_url(m.group()), raw, flags=re.IGNORECASE)


def node_id(window_id, backend):
    return hashlib.sha256((window_id + ':' + str(backend)).encode()).hexdigest()[:24]


def number(value):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise Refused(5)
    return value


def map_snapshot(window, seq, tree, snapshot, metrics, text_container=None):
    """Only top document geometry is admitted; cross-frame offsets are unproven.

    DOMSnapshot indexes are document order. Sensitive input nodes and their AX
    descendants never enter the public projection; value access happens only
    after the sensitivity and visibility checks.
    """
    viewport = metrics['cssVisualViewport']
    vw, vh = number(viewport['clientWidth']), number(viewport['clientHeight'])
    if (not 0 < vw <= window['w'] or not 0 < vh <= window['h'] or viewport.get('scale') != 1
            or viewport.get('offsetX', 0) != 0 or viewport.get('offsetY', 0) != 0):
        raise Refused(6)
    # Chromium's own frame draws an equal border on the left, right and bottom edges
    # (measured live: 4 px at 1280x900, viewport 1272x809); the rest of the height is the toolbar.
    chrome_x = (window['w'] - vw) // 2
    chrome_y = window['h'] - vh - chrome_x
    documents, strings = snapshot['documents'], snapshot['strings']
    if not documents:
        raise Refused(5)
    doc = documents[0]
    sx, sy = number(doc['scrollOffsetX']), number(doc['scrollOffsetY'])
    nodes, layout = doc['nodes'], doc['layout']
    backends, parents = nodes['backendNodeId'], nodes['parentIndex']
    count = len(backends)
    if count > 100000 or len(tree) > 100000:
        raise Refused(6)
    if len(parents) != count or len(set(backends)) != count:
        raise Refused(5)
    by_backend = {n.get('backendDOMNodeId'): n for n in tree if n.get('backendDOMNodeId') is not None}
    def string(i):
        # DOMSnapshot writes -1 for an absent string (e.g. a boolean attribute such as `disabled`).
        if i == -1 and type(i) is int:
            return ''
        if type(i) is not int or not 0 <= i < len(strings) or not isinstance(strings[i], str):
            raise Refused(5)
        return strings[i]
    # DOMSnapshot's first document is the main frame. Backend ids from all
    # other documents are absent from this projection, including AX children.
    embedded = {i for i, name in enumerate(nodes['nodeName'])
                if string(name).upper() in {'IFRAME', 'FRAME'}}
    captured = set(nodes.get('contentDocumentIndex', {}).get('index', []))
    skipped_frames = len(documents) - 1 + len(embedded - captured)
    forbidden = set()
    for i in range(count):
        if type(parents[i]) is not int or not -1 <= parents[i] < i:
            raise Refused(5)
        attrs = nodes['attributes'][i]
        if len(attrs) % 2:
            raise Refused(5)
        attrs = {string(attrs[j]).lower(): string(attrs[j + 1]).lower() for j in range(0, len(attrs), 2)}
        autocomplete = attrs.get('autocomplete', '').split()
        sensitive = (attrs.get('type') == 'password' or any(
            word in {'one-time-code', 'current-password', 'new-password'} or word.startswith('cc-') for word in autocomplete))
        # Also recognize common author field hints; never inspect their content.
        hints = ' '.join(attrs.get(k, '') for k in ('name', 'id', 'aria-label'))
        hints = ''.join(c for c in hints if c.isalnum())
        sensitive = sensitive or any(k in hints for k in ('password', 'creditcard', 'cardnumber', 'ccnumber', 'onetimecode', 'otp', 'cvv', 'cvc'))
        properties = {p['name']: p.get('value', {}).get('value')
                      for p in by_backend.get(backends[i], {}).get('properties', [])}
        if sensitive or properties.get('protected') is True or parents[i] in forbidden:
            forbidden.add(i)
    visible, boxes, hidden = set(), {}, set()
    for j, i in enumerate(layout['nodeIndex']):
        if type(i) is not int or not 0 <= i < count:
            raise Refused(5)
        styles = [string(v) for v in layout['styles'][j]]
        if len(styles) != 3:
            continue  # the document node carries no computed styles; it hides nothing
        try:
            transparent = float(styles[2] or '1') <= 0
        except ValueError:
            raise Refused(5) from None
        if styles[0] == 'none' or styles[1] not in ('visible', '') or transparent:
            hidden.add(i)
            continue
        x, y, w, h = [number(v) for v in layout['bounds'][j]]
        x, y = x - sx, y - sy
        right, bottom = min(vw, x + w), min(vh, y + h)
        x, y = max(0, x), max(0, y)
        if right <= x or bottom <= y:
            continue
        # Round inward to preserve the window bounds contract.
        left, top = math.ceil(x + chrome_x), math.ceil(y + chrome_y)
        box = [left, top, max(0, math.floor(right + chrome_x) - left), max(0, math.floor(bottom + chrome_y) - top)]
        if min(box[2:]) > 0:
            visible.add(i); boxes[i] = box
    def role(i):
        return by_backend.get(backends[i], {}).get('role', {}).get('value', '')
    siblings = {}
    for i in range(count):
        siblings.setdefault((parents[i], role(i)), []).append(i)
    ordinals = {i: ordinal for peers in siblings.values() for ordinal, i in enumerate(peers)}
    def ancestors(i):
        for _ in range(min(count, 256)):
            if i < 0:
                return
            yield i
            i = parents[i]
        if i >= 0:
            raise Refused(6)
    def container(i):
        for ancestor in ancestors(i):
            peers = siblings[(parents[ancestor], role(ancestor))]
            if role(ancestor) in REPEATED or (role(ancestor) and len(peers) >= 2):
                return {'id': node_id(window['id'], backends[ancestor]), 'ordinal': ordinals[ancestor]}
        return None
    # Parent opacity/display also hides descendants even when their own
    # computed opacity is one. Visibility overrides remain conservative.
    # Zero-size ancestors (e.g. a body whose children are absolutely positioned) hide nothing.
    visible = {i for i in visible if not any(a in hidden for a in ancestors(i))}
    elements, text_rows = [], []
    for i in range(count):
        if i not in visible or i in forbidden:
            continue
        n = by_backend.get(backends[i])
        if not n or n.get('ignored'):
            continue
        r = role(i)
        if not isinstance(r, str):
            raise Refused(5)
        name = n.get('name', {}).get('value', '')
        if not isinstance(name, str):
            raise Refused(5)
        props = {p['name']: p.get('value', {}).get('value') for p in n.get('properties', [])}
        focused = props.get('focused') is True
        states = {'enabled': not props['disabled'] if type(props.get('disabled')) is bool else None,
                  'focused': focused if props.get('focusable') is True or 'focused' in props else None}
        for key in ('expanded', 'checked', 'selected'):
            v = props.get(key)
            states[key] = v if type(v) is bool else {'true': True, 'false': False}.get(v) if isinstance(v, str) else None
        length = None
        if r in {'textbox', 'combobox'}:
            value = n.get('value', {}).get('value')
            if isinstance(value, str):
                length = len(value)
        elements.append({'id': node_id(window['id'], backends[i]), 'role': r, 'name': name[:120],
                         'box': boxes[i], 'region': next((role(a) for a in ancestors(i) if role(a) in LANDMARKS), None),
                         'actionable': r in ACTIONS or props.get('focusable') is True,
                         'focused': focused, 'states': states, 'container': container(i), 'value_len': length})
        # StaticText nodes represent visible document text without duplicate parent
        # accessible names; their only children are InlineTextBox line boxes with the
        # same text, so those are never collected. Editable names are labels, never values.
        if r == 'StaticText':
            text_rows.append((i, name))
    truncated = len(elements) > 300
    if truncated:
        elements = sorted(elements, key=lambda e: (not e['actionable'], -e['box'][2] * e['box'][3]))[:300]
    public_window = dict(window, url=safe_url(window['url']), title=safe_title(window['title']))
    result = {'window': public_window, 'frame_seq': seq, 'scroll': {'x': sx, 'y': sy},
              'elements': elements, 'truncated': truncated, 'skipped_frames': skipped_frames}
    if text_container is not None:
        matches = [i for i in visible if node_id(window['id'], backends[i]) == text_container]
        if len(matches) != 1:
            raise Refused(6)
        root = matches[0]
        if any(root in ancestors(i) for i in forbidden):
            raise Refused(6)
        raw = '\n'.join(name for i, name in text_rows if root in ancestors(i)).encode()[:65536]
        text = raw.decode('utf-8', errors='ignore')
        result['text'] = {'container_id': text_container, 'bytes': len(text.encode()), 'text': text}
    return result


def command(argv):
    return subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                          stderr=subprocess.DEVNULL, timeout=3, check=True, text=True).stdout


def resolve_window(selector):
    mapping = parse(Path(MAPPING).read_bytes())
    candidates = []
    for index, row in enumerate(mapping):
        title = command(['xdotool', 'getwindowname', row['xid']]).strip()
        if selector in (row['id'], row['xid'], title, str(index)):
            candidates.append((row, title))
    if len(candidates) != 1:
        raise Refused(6)
    row, title = candidates[0]
    # Check the current native window client rectangle, not cached launch state.
    geometry = command(['xdotool', 'getwindowgeometry', '--shell', row['xid']])
    values = dict(line.split('=', 1) for line in geometry.splitlines() if '=' in line)
    rect = dict(zip(('x', 'y', 'w', 'h'), [int(values[k]) for k in ('X', 'Y', 'WIDTH', 'HEIGHT')]))
    if any(rect[k] != row[k] for k in rect):
        raise Refused(6)
    return dict(rect, id=row['id'], title=title, url='')


def snapshot(pipe, request):
    if (not isinstance(request, dict) or not {'op', 'window_title_or_index', 'frame_seq'} <= set(request)
            or not set(request) <= {'op', 'window_title_or_index', 'frame_seq', 'text_container'}
            or request['op'] != 'ax' or type(request['frame_seq']) is not int
            or not 1 <= request['frame_seq'] <= 999999
            or not isinstance(request['window_title_or_index'], str)
            or len(request['window_title_or_index']) > 4096
            or ('text_container' in request and (not isinstance(request['text_container'], str)
                                                 or len(request['text_container']) > 128))):
        raise Refused(6)
    window = resolve_window(request['window_title_or_index'])
    targets = pipe.call('Target.getTargets')['targetInfos']
    # No target ids are accepted from clients. Match the native identity title
    # exactly, including the browser's fixed title suffix where present.
    matches = [t for t in targets if t.get('type') == 'page' and
               window['title'] in (t.get('title'), t.get('title', '') + ' - Chromium')]
    if len(matches) != 1:
        # Several windows can show pages with the same title (e.g. three Perplexity windows), and a
        # title with non-ASCII text can read differently through X11 and CDP (no match at all): then
        # keep the page whose browser window has exactly this window's measured rectangle.
        if not matches:
            matches = [t for t in targets if t.get('type') == 'page']
        pipe.policy.targets = {t['targetId'] for t in matches}
        rect = (window['x'], window['y'], window['w'], window['h'])
        placed = []
        for t in matches:
            bounds = pipe.call('Browser.getWindowForTarget', {'targetId': t['targetId']}).get('bounds', {})
            if (bounds.get('left'), bounds.get('top'), bounds.get('width'), bounds.get('height')) == rect:
                placed.append(t)
        matches = placed
    if len(matches) != 1:
        raise Refused(6)
    target = matches[0]
    window['url'] = safe_url(target['url'])
    pipe.policy.targets = {target['targetId']}
    # Reuse attachments to avoid creating unbounded sessions.
    if not hasattr(pipe, 'attached'):
        pipe.attached = {}
    live_ids = {t['targetId'] for t in targets if t.get('type') == 'page'}
    pipe.attached = {key: sid for key, sid in pipe.attached.items() if key in live_ids}
    pipe.policy.sessions = set(pipe.attached.values())
    sid = pipe.attached.get(target['targetId'])
    if sid is None:
        if len(pipe.attached) >= 16:
            raise Refused(6)
        sid = pipe.call('Target.attachToTarget', {'targetId': target['targetId'], 'flatten': True})['sessionId']
        pipe.attached[target['targetId']] = sid
        pipe.policy.sessions.add(sid)
    pipe.call('Accessibility.enable', session=sid)
    tree = pipe.call('Accessibility.getFullAXTree', session=sid)['nodes']
    dom = pipe.call('DOMSnapshot.captureSnapshot', {'computedStyles': ['display', 'visibility', 'opacity']}, sid)
    metrics = pipe.call('Page.getLayoutMetrics', session=sid)
    result = map_snapshot(window, request['frame_seq'], tree, dom, metrics, request.get('text_container'))
    if resolve_window(request['window_title_or_index']) != dict(window, url=''):
        raise Refused(6)
    print(json.dumps({'event': 'snapshot', 'count': len(result['elements'])}), file=sys.stderr, flush=True)
    return result


def receive(client):
    data = bytearray()
    while len(data) <= MAX:
        chunk = client.recv(min(65536, MAX + 1 - len(data)))
        if not chunk:
            break
        data.extend(chunk)
        if b'\n' in chunk:
            break
    if len(data) > MAX or not data.endswith(b'\n') or data.count(b'\n') != 1:
        raise Refused(5)
    return parse(bytes(data))


def respond(pipe, client):
    try:
        return snapshot(pipe, receive(client))
    except Refused as exc:
        if exc.code == 5:
            pipe.policy.failed = True
        print('{"event":"refusal","count":1}', file=sys.stderr, flush=True)
        return {'error': {4: 'not_ready', 5: 'malformed', 6: 'refused'}.get(exc.code, 'refused'), 'code': exc.code}
    except Exception:
        pipe.policy.failed = True
        print('{"event":"refusal","count":1}', file=sys.stderr, flush=True)
        return {'error': 'malformed', 'code': 5}


def launch(argv):
    if not argv or any('remote-debugging' in arg.lower() for arg in argv):
        raise Refused(2)
    # fd3 is Chromium's read end; fd4 its write end. Fork avoids preexec and
    # pass_fds/dup2 ordering pitfalls. The exec child inherits exactly stdio+3/4.
    child_read, parent_write = os.pipe()
    parent_read, child_write = os.pipe()
    pid = os.fork()
    if pid == 0:
        try:
            os.setsid()
            a, b = os.dup(child_read), os.dup(child_write)
            os.dup2(a, 3, inheritable=True); os.dup2(b, 4, inheritable=True)
            os.closerange(5, 1048576)
            os.execvp(argv[0], argv + ['--remote-debugging-pipe'])
        except BaseException:
            os._exit(5)
    os.close(child_read); os.close(child_write)
    return pid, parent_read, parent_write


def main(argv):
    pid = None
    server = None
    stopping = False
    def stop(*_):
        nonlocal stopping
        stopping = True
        raise SystemExit(0)
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        if os.getuid() != 10001:
            raise Refused(4)
        os.umask(0o077)
        policy = Policy(parse(Path(__file__).with_name('ax-allowlist.json').read_bytes()))
        pid, read_fd, write_fd = launch(argv)
        pipe = Pipe(read_fd, write_fd, policy)
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        # Never unlink an existing socket/foreign pathname.
        server.bind(SOCKET)
        os.chmod(SOCKET, 0o600)
        server.listen(4); server.settimeout(0.5)
        while not stopping:
            ended, _ = os.waitpid(pid, os.WNOHANG)
            if ended:
                pid = None
                raise Refused(4)
            try:
                client, _ = server.accept()
            except socket.timeout:
                continue
            with client:
                client.settimeout(5)
                response = respond(pipe, client)
                payload = json.dumps(response, ensure_ascii=True, separators=(',', ':')).encode() + b'\n'
                if len(payload) > MAX:
                    policy.failed = True
                    payload = b'{"error":"malformed","code":5}\n'
                try:
                    client.sendall(payload)
                except OSError:
                    pass
            if policy.failed:
                raise Refused(5)
        return 0
    except Refused as exc:
        return exc.code
    except Exception:
        return 5
    finally:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        if server is not None:
            server.close()
        if pid is not None:
            try:
                os.killpg(pid, signal.SIGTERM)
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline:
                    if os.waitpid(pid, os.WNOHANG)[0]:
                        pid = None; break
                    time.sleep(0.05)
                if pid is not None:
                    os.killpg(pid, signal.SIGKILL); os.waitpid(pid, 0)
            except (ProcessLookupError, ChildProcessError):
                pass


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
