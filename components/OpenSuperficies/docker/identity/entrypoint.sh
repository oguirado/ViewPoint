#!/bin/sh
# No inherited desktop entrypoint, profile cleanup or browser logs; AX uses a private filtered pipe.
case "${1:-}" in
  xpra-start) exec /usr/local/bin/identity-xpra-layer.sh restart ;;
  xpra-stop) exec /usr/local/bin/identity-xpra-layer.sh stop ;;
  "") ;;
  *) printf '%s\n' 'REFUSED: invalid_identity_command' >&2; exit 2 ;;
esac
exec python3 -I - <<'PYTHON'
import json
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import sys
import time
from urllib.parse import urlsplit


class Refused(Exception):
    def __init__(self, code, reason):
        self.code, self.reason = code, reason


def settings(env):
    try:
        n = int(env['SRF_WINDOWS'])
        tiles = json.loads(env['SRF_TILES'])
        proxy = env['SRF_EGRESS_PROXY']
        url = urlsplit(proxy)
        if (url.scheme != 'http' or not url.hostname or not url.port
                or url.username is not None or url.password is not None
                or url.path not in ('', '/') or url.query or url.fragment
                or any(c.isspace() for c in proxy)):
            raise ValueError()
        if not 1 <= n <= 16 or not isinstance(tiles, list) or len(tiles) != n:
            raise ValueError()
        seen = set()
        for t in tiles:
            if not isinstance(t, dict) or not {'id', 'x', 'y', 'w', 'h'} <= t.keys():
                raise ValueError()
            if not isinstance(t['id'], str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}', t['id']):
                raise ValueError()
            if t['id'] in seen:
                raise ValueError()
            seen.add(t['id'])
            if any(type(t[k]) is not int for k in ('x', 'y', 'w', 'h')):
                raise ValueError()
            if min(t['x'], t['y']) < 0 or min(t['w'], t['h']) < 1:
                raise ValueError()
            if t['x'] + t['w'] > 1280 or t['y'] + t['h'] > 900:
                raise ValueError()
        for i, a in enumerate(tiles):
            for b in tiles[i + 1:]:
                if (a['x'] < b['x'] + b['w'] and b['x'] < a['x'] + a['w']
                        and a['y'] < b['y'] + b['h'] and b['y'] < a['y'] + a['h']):
                    raise ValueError()
        return tiles, proxy
    except (KeyError, TypeError, ValueError):
        raise Refused(2, 'invalid_identity_configuration') from None


def run(argv, check=True):
    return subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                          stderr=subprocess.DEVNULL, timeout=5, check=check, text=True).stdout


def require_tmpfs(path):
    if run(['stat', '-f', '-c', '%T', path]).strip() != 'tmpfs':
        raise Refused(4, 'runtime_not_ram')


def require_view_mount():
    try:
        with open('/proc/self/mountinfo') as mounts:
            mounted = any(line.split()[4] == '/srfview' for line in mounts)
        valid = (mounted and os.path.isdir('/srfview') and not os.path.islink('/srfview')
                 and os.access('/srfview', os.W_OK | os.X_OK))
    except OSError:
        valid = False
    if not valid:
        raise Refused(4, 'view_mount_not_writable')


def windows():
    # The base image has xdotool/xprop but no wmctrl: list Chromium's mapped windows and
    # read each one's geometry (decimal X window ids, as the CLI and the AX shim expect).
    result = []
    for xid in run(['xdotool', 'search', '--onlyvisible', '--class', 'chromium'], check=False).split():
        geo = {}
        for line in run(['xdotool', 'getwindowgeometry', '--shell', xid], check=False).splitlines():
            key, _, value = line.partition('=')
            if value.isdigit():
                geo[key] = int(value)
        if all(k in geo for k in ('X', 'Y', 'WIDTH', 'HEIGHT')):
            result.append((xid, (geo['X'], geo['Y'], geo['WIDTH'], geo['HEIGHT'])))
    return result


def await_value(probe):
    for _ in range(100):
        value = probe()
        if value:
            return value
        time.sleep(0.1)
    raise Refused(4, 'identity_readiness_timeout')


def main():
    children = []
    def stop(signum=None, frame=None):
        try:
            subprocess.run(['/usr/local/bin/identity-xpra-layer.sh', 'stop'],
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=15, check=False)
        except (OSError, subprocess.SubprocessError):
            pass
        for child in reversed(children):
            if child.poll() is None:
                try:
                    os.killpg(child.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
        deadline = time.monotonic() + 3
        for child in reversed(children):
            try:
                child.wait(timeout=max(0.01, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                child.wait(timeout=2)
        if signum is not None:
            raise SystemExit(0)

    def spawn(argv):
        child = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, start_new_session=True)
        children.append(child)
        return child

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        tiles, proxy = settings(os.environ)
        require_view_mount()
        profile = Path('/profile').lstat()
        if (not stat.S_ISDIR(profile.st_mode) or stat.S_IMODE(profile.st_mode) != 0o700
                or profile.st_uid != 10001 or os.getuid() != 10001):
            raise Refused(4, 'private_profile_required')
        for path in ['/run/vqa/fb', '/run/newton-tenant', '/tmp']:
            require_tmpfs(path)
        if not os.access('/usr/local/libexec/srf-fbread', os.X_OK):
            raise Refused(4, 'fbread_missing')
        spawn(['Xvfb', ':100', '-screen', '0', '1280x900x24', '-fbdir', '/run/vqa/fb',
               '-nocursor', '-nolisten', 'tcp', '-noreset'])
        await_value(lambda: subprocess.run(['xdpyinfo', '-display', ':100'], timeout=2,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0)
        config = Path('/run/newton-tenant/openbox.xml')
        config.write_text('<openbox_config xmlns="http://openbox.org/3.4/rc">'
                          '<applications><application class="*"><decor>no</decor>'
                          '</application></applications></openbox_config>')
        spawn(['openbox', '--config-file', str(config)])
        await_value(lambda: 'window id' in run(['xprop', '-display', ':100', '-root', '_NET_SUPPORTING_WM_CHECK'], check=False))
        common = ['chromium', '--user-data-dir=/profile', '--proxy-server=' + proxy,
                  '--proxy-bypass-list=<-loopback>', '--disable-quic',
                  '--no-first-run', '--no-default-browser-check', '--disable-sync',
                  '--force-device-scale-factor=1',
                  '--disable-breakpad', '--disable-session-crashed-bubble',
                  '--disable-features=Translate,MediaRouter,UseXdgDesktopPortal',
                  '--password-store=basic', '--use-mock-keychain',
                  '--disk-cache-dir=/profile/cache', '--new-window']
        browser = None
        mapped = []
        for tile in tiles:
            before = {xid for xid, _ in windows()}
            argv = common + ['--window-position=%d,%d' % (tile['x'], tile['y']),
                             '--window-size=%d,%d' % (tile['w'], tile['h']), 'about:blank']
            if browser is None:
                browser = spawn(['python3', '-I', '/usr/local/bin/ax-shim.py'] + argv)
            else:
                run(argv)
            # A window already placed can be briefly unmapped while it moves: never take it again.
            taken = before | {m['xid'] for m in mapped}
            found = await_value(lambda: [xid for xid, _ in windows() if xid not in taken])
            if len(found) != 1:
                raise Refused(4, 'ambiguous_identity_window')
            xid = found[0]
            run(['xdotool', 'windowmove', xid, str(tile['x']), str(tile['y'])])
            run(['xdotool', 'windowsize', xid, str(tile['w']), str(tile['h'])])
            expected = tuple(tile[k] for k in ('x', 'y', 'w', 'h'))
            await_value(lambda: any(wid == xid and rect == expected for wid, rect in windows()))
            mapped.append(dict(tile, xid=xid))
        Path('/run/newton-tenant/ax-windows.json').write_text(json.dumps(mapped))
        os.chmod('/run/newton-tenant/ax-windows.json', 0o600)
        await_value(lambda: Path('/run/newton-tenant/ax.sock').is_socket())
        subprocess.run(['/usr/local/bin/identity-xpra-layer.sh', 'start'],
                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=120, check=True)
        print('xpra layer up', flush=True)
        print('IDENTITY_READY ' + json.dumps({'windows': mapped}, separators=(',', ':')), flush=True)
        # Container lifetime follows its browser and display, with bounded shutdown.
        while all(child.poll() is None for child in children):
            time.sleep(0.25)
        raise Refused(4, 'identity_child_exited')
    except Refused as exc:
        print('REFUSED: ' + exc.reason, file=sys.stderr)
        return exc.code
    except (OSError, subprocess.SubprocessError, ValueError):
        print('REFUSED: identity_runtime_failure', file=sys.stderr)
        return 4
    finally:
        stop()


sys.exit(main())
PYTHON
