#!/bin/sh
# No inherited desktop entrypoint, profile cleanup or browser logs; AX uses a private filtered pipe.
case "${1:-}" in
  xpra-start) exec /usr/local/bin/identity-xpra-layer.sh restart ;;
  xpra-stop) exec /usr/local/bin/identity-xpra-layer.sh stop ;;
  "") ;;
  *) printf '%s\n' 'REFUSED: invalid_program_command' >&2; exit 2 ;;
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


PROGRAMS = {'notepad': {'argv': ['python3', '-I', '/usr/local/libexec/srf-notepad.py'],
                        'wm_class': 'srf-notepad'},
            'metaviewer-nested': None}

class Refused(Exception):
    def __init__(self, code, reason):
        self.code, self.reason = code, reason


def load_electron_app(config=Path('/usr/local/share/srf-app/app.json'), root=Path('/opt/srf-app')):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError()
            result[key] = value
        return result

    try:
        with config.open('rb') as source:
            raw = source.read(65537)
        if len(raw) > 65536:
            raise ValueError()
        app = json.loads(raw, object_pairs_hook=unique)
        if not isinstance(app, dict) or set(app) != {'exe', 'args', 'wm_class'}:
            raise ValueError()
        exe, args, wm_class = app['exe'], app['args'], app['wm_class']
        if (not isinstance(exe, str) or not exe or exe.startswith('/') or '..' in exe
                or any(part in ('', '.', '..') for part in exe.split('/'))
                or any(c in exe for c in ('\x00', '\n', '\r'))):
            raise ValueError()
        base = root.resolve(strict=True)
        target = (base / exe).resolve(strict=True)
        target.relative_to(base)
        if not target.is_file() or not os.access(target, os.X_OK):
            raise ValueError()
        if (not isinstance(args, list) or any(not isinstance(a, str)
                or any(c in a for c in ('\x00', '\n', '\r')) for a in args)
                or not isinstance(wm_class, str)
                or not re.fullmatch(r'[A-Za-z0-9._-]{1,64}', wm_class)):
            raise ValueError()
        blocked_names = ('sandbox', 'zygote', 'single-process', 'in-process',
                         'remote-debugging', 'inspect', 'dev-shm')
        if '--' in args or any(
                any(part in a.split('=', 1)[0] for part in blocked_names)
                or a.split('=', 1)[0] in ('--js-flags', '--disable-web-security',
                                          '--allow-file-access-from-files') for a in args):
            raise ValueError()
        args = list(args)
        for flag, value in [('--user-data-dir', '/profile/app'), ('--ozone-platform', 'x11')]:
            seen = 0
            for i, arg in enumerate(args):
                if arg == flag or arg.startswith(flag + '='):
                    actual = args[i + 1] if arg == flag and i + 1 < len(args) else arg[len(flag) + 1:]
                    if actual != value:
                        raise ValueError()
                    seen += 1
            if seen > 1:
                raise ValueError()
            if not seen:
                args.append(flag + '=' + value)
        return {'argv': [str(target)] + args, 'wm_class': wm_class}
    except (OSError, ValueError, TypeError, RuntimeError):
        raise Refused(2, 'invalid_app_manifest') from None


def program_argv(program_id, program, tile_id):
    return program['argv'] + (['--tile', tile_id] if program_id == 'notepad' else [])


def program_env(env):
    allowed = {'DISPLAY', 'HOME', 'LANG', 'PATH', 'NO_AT_BRIDGE',
               'GTK_USE_PORTAL', 'GIO_USE_VFS'}
    result = {key: value for key, value in env.items()
              if key in allowed or key.startswith(('XDG_', 'LC_'))}
    result['DBUS_SESSION_BUS_ADDRESS'] = 'disabled:'
    return result


def spawn_program(argv):
    return subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, start_new_session=True,
                            env=program_env(os.environ))


def settings(env):
    try:
        n = int(env['SRF_WINDOWS'])
        tiles = json.loads(env['SRF_TILES'])
        if not 1 <= n <= 16 or not isinstance(tiles, list) or len(tiles) != n:
            raise ValueError()
        seen = set()
        for t in tiles:
            if not isinstance(t, dict) or set(t) != {'id', 'x', 'y', 'w', 'h'}:
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
        return tiles
    except (KeyError, TypeError, ValueError):
        raise Refused(2, 'invalid_program_configuration') from None


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
    # The base image has xdotool/xprop but no wmctrl: list the program's mapped windows and
    # read each one's geometry (decimal X window ids, as the CLI and the AX shim expect).
    result = []
    for xid in run(['xdotool', 'search', '--onlyvisible', '--class', PROGRAMS[os.environ['SRF_PROGRAM']]['wm_class']], check=False).split():
        if os.environ['SRF_PROGRAM'] == 'metaviewer-nested':
            props = run(['xprop', '-id', xid, 'WM_STATE', '_NET_WM_WINDOW_TYPE'], check=False)
            # ICCCM WM_STATE marks managed top-level client windows, excluding child widgets.
            if not re.search(r'^WM_STATE\(WM_STATE\):', props, re.MULTILINE):
                continue
            types = re.findall(r'_NET_WM_WINDOW_TYPE_[A-Z_]+', props)
            absent = re.search(r'^_NET_WM_WINDOW_TYPE: +(not found\.|no such atom on any window\.)$',
                               props, re.MULTILINE)
            if types != ['_NET_WM_WINDOW_TYPE_NORMAL'] and not (not types and absent):
                continue
        geo = {}
        for line in run(['xdotool', 'getwindowgeometry', '--shell', xid], check=False).splitlines():
            key, _, value = line.partition('=')
            if value.isdigit():
                geo[key] = int(value)
        if all(k in geo for k in ('X', 'Y', 'WIDTH', 'HEIGHT')):
            result.append((xid, (geo['X'], geo['Y'], geo['WIDTH'], geo['HEIGHT'])))
    return result


def await_value(probe, attempts):
    for _ in range(attempts):
        value = probe()
        if value:
            return value
        time.sleep(0.1)
    raise Refused(4, 'program_readiness_timeout')


def await_program_window(taken):
    def probe():
        found = [xid for xid, _ in windows() if xid not in taken]
        if len(found) > 1:
            raise Refused(4, 'ambiguous_program_window')
        return found
    return await_value(probe, 300)


def main():
    children = []
    runtime_started = False
    def stop(signum=None, frame=None):
        try:
            if runtime_started:
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

    def spawn(argv, program=False):
        child = (spawn_program(argv) if program else
                 subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL, start_new_session=True))
        children.append(child)
        return child

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        if os.environ.get('SRF_PROGRAM') not in PROGRAMS:
            raise Refused(2, 'unknown_program')
        if os.environ['SRF_PROGRAM'] == 'metaviewer-nested':
            if 'ELECTRON_DISABLE_SANDBOX' in os.environ:
                raise Refused(2, 'electron_sandbox_required')
            PROGRAMS['metaviewer-nested'] = load_electron_app()
            os.environ['XDG_RUNTIME_DIR'] = '/run/newton-tenant'
        tiles = settings(os.environ)
        if os.environ['SRF_PROGRAM'] == 'metaviewer-nested' and len(tiles) != 1:
            raise Refused(2, 'electron_requires_one_window')
        runtime_started = True
        require_view_mount()
        profile = Path('/profile').lstat()
        if (not stat.S_ISDIR(profile.st_mode) or stat.S_IMODE(profile.st_mode) != 0o700
                or profile.st_uid != 10001 or os.getuid() != 10001):
            raise Refused(4, 'private_profile_required')
        exchange = Path('/profile/exchange')
        exchange.mkdir(mode=0o700, exist_ok=True)
        exchange_stat = exchange.lstat()
        if not stat.S_ISDIR(exchange_stat.st_mode) or exchange_stat.st_uid != 10001:
            raise Refused(4, 'private_exchange_required')
        exchange.chmod(0o700)
        for path in ['/run/vqa/fb', '/run/newton-tenant', '/tmp']:
            require_tmpfs(path)
        if not os.access('/usr/local/libexec/srf-fbread', os.X_OK):
            raise Refused(4, 'AUSENTE — fbread_missing')
        spawn(['Xvfb', ':100', '-screen', '0', '1280x900x24', '-fbdir', '/run/vqa/fb',
               '-nocursor', '-nolisten', 'tcp', '-noreset'])
        await_value(lambda: subprocess.run(['xdpyinfo', '-display', ':100'], timeout=2,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0, 100)
        config = Path('/run/newton-tenant/openbox.xml')
        config.write_text('<openbox_config xmlns="http://openbox.org/3.4/rc">'
                          '<theme><keepBorder>no</keepBorder></theme>'
                          '<applications><application class="*"><decor>no</decor>'
                          '</application></applications></openbox_config>')
        spawn(['openbox', '--config-file', str(config)])
        await_value(lambda: 'window id' in run(['xprop', '-display', ':100', '-root', '_NET_SUPPORTING_WM_CHECK'], check=False), 100)
        mapped = []
        for tile in tiles:
            before = {xid for xid, _ in windows()}
            spawn(program_argv(os.environ['SRF_PROGRAM'], PROGRAMS[os.environ['SRF_PROGRAM']], tile['id']), program=True)
            # A window already placed can be briefly unmapped while it moves: never take it again.
            taken = before | {m['xid'] for m in mapped}
            found = await_program_window(taken)
            if len(found) != 1:
                raise Refused(4, 'ambiguous_program_window')
            xid = found[0]
            run(['xdotool', 'windowmove', xid, str(tile['x']), str(tile['y'])])
            run(['xdotool', 'windowsize', xid, str(tile['w']), str(tile['h'])])
            expected = tuple(tile[k] for k in ('x', 'y', 'w', 'h'))
            await_value(lambda: any(wid == xid and rect == expected for wid, rect in windows()), 300)
            mapped.append(dict(tile, xid=xid))
        subprocess.run(['/usr/local/bin/identity-xpra-layer.sh', 'start'],
                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=120, check=True)
        print('xpra layer up', flush=True)
        print('PROGRAM_READY ' + json.dumps({'windows': [dict(m, xid='0x%x' % int(m['xid'])) for m in mapped]}, separators=(',', ':')), flush=True)
        # Container lifetime follows its programs and display, with bounded shutdown.
        while all(child.poll() is None for child in children):
            time.sleep(0.25)
        raise Refused(4, 'program_child_exited')
    except Refused as exc:
        print('REFUSED: ' + exc.reason, file=sys.stderr)
        return exc.code
    except (OSError, subprocess.SubprocessError, ValueError):
        print('REFUSED: program_runtime_failure', file=sys.stderr)
        return 4
    finally:
        stop()


sys.exit(main())
PYTHON
