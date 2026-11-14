#!/usr/bin/env python3
"""Offline recipe oracle; scratch-only mutation controls. Exits 0/2/5."""
import argparse
import ast
import json
from pathlib import Path
import shutil
import tempfile


def check(root):
    errors = []
    try:
        docker = (root / 'docker/identity/Dockerfile').read_text()
        entry = (root / 'docker/identity/entrypoint.sh').read_text()
        xpra = (root / 'docker/identity/xpra-layer.sh').read_text()
        for token in ['XPRA_SOCKET=/srfview/xpra.sock', 'XPRA_READONLY=yes',
                      'if [ "${SRF_LOGIN:-0}" = 1 ]; then XPRA_READONLY=no; fi',
                      '--bind="${XPRA_SOCKET}"', '--socket-permissions=666', 'XPRA_USER_CONF_DIRS=/run/user/10001/xpra-conf', "start = /bin/true",
                      'socket.AF_UNIX', 'GET / HTTP/1.0', 'view_mount_not_writable',
                      '--readonly="${XPRA_READONLY}"',
                      '--clipboard=no', '--file-transfer=no', '--printing=no',
                      '--speaker=no', '--microphone=no', '--daemon=no',
                      '--start-new-commands=no', '--sharing=no']:
            if token not in xpra:
                errors.append('xpra missing ' + token)
        for flag in ['--bind-tcp', '--encodings=', '--encoding=', '--min-quality=',
                     '--min-speed=', '--speed=', '--compress=', '--compression_level=',
                     '--compressors=', '--auto-refresh-delay=', '--video-scaling=']:
            if flag in xpra:
                errors.append('forced xpra transport/codec ' + flag)
        if '0.0.0.0' in xpra or 'XPRA_READONLY="${XPRA_READONLY' in xpra:
            errors.append('unsafe xpra override or wildcard bind')
        for token in ['opensuperficies-desktop-arm64:1.1.0', '152.0.7977.82',
                      "dpkg-query -W -f='${Version}' chromium", '152.0.7977.82-1~deb12u1',
                      'USER 10001:0']:
            if token not in docker:
                errors.append('Dockerfile missing ' + token)
        for token in ['--proxy-server=', '--user-data-dir=/profile', '-fbdir', '/run/vqa/fb',
                      '-nocursor', 'IDENTITY_READY', 'SRF_TILES', 'SRF_WINDOWS',
                      'SRF_EGRESS_PROXY', 'require_view_mount()', 'view_mount_not_writable', '--disable-quic', '--proxy-bypass-list=<-loopback>']:
            if token not in entry:
                errors.append('entrypoint missing ' + token)
        if 'apt-get' in docker:
            errors.append('package installation forbidden')
        if 'xpra layer up' not in entry:
            errors.append('xpra readiness absent')
        for token in ['--no-sandbox', '--remote-debugging', 'dbus-daemon', 'pulseaudio --', 'xpra screenshot']:
            if token in entry or (token.startswith('--') and token in docker):
                errors.append('forbidden recipe token ' + token)
        shim = (root / 'docker/identity/ax-shim.py').read_text()
        policy = json.loads((root / 'docker/identity/ax-allowlist.json').read_text())
        browser = {'Target.getTargets', 'Target.attachToTarget', 'Browser.getWindowForTarget'}
        session = {'Accessibility.enable', 'Accessibility.getFullAXTree',
                   'DOMSnapshot.captureSnapshot', 'Page.getLayoutMetrics'}
        if (set(policy['browser_methods']) != browser or set(policy['session_methods']) != session
                or policy['max_frame_bytes'] != 16777216):
            errors.append('AX policy not closed')
        if shim.count('--remote-debugging-pipe') != 1:
            errors.append('AX pipe flag absent or duplicated')
        if '--remote-debugging-port' in shim or '--no-sandbox' in shim:
            errors.append('AX unsafe browser flag')
        for token in ['ax-shim.py', 'ax-allowlist.json']:
            if token not in docker:
                errors.append('AX copy absent')
        for token in ["spawn(['python3', '-I', '/usr/local/bin/ax-shim.py'] + argv)",
                      '--force-device-scale-factor=1', 'ax-windows.json', 'ax.sock']:
            if token not in entry:
                errors.append('AX launch wiring absent')
        parsed = ast.parse(shim)
        element_keys = {'id', 'role', 'name', 'box', 'region', 'actionable', 'focused',
                        'states', 'container', 'value_len'}
        element_dicts = [node for node in ast.walk(parsed) if isinstance(node, ast.Dict)
                         and any(isinstance(k, ast.Constant) and k.value == 'actionable' for k in node.keys)]
        if len(element_dicts) != 1 or {k.value for k in element_dicts[0].keys} != element_keys:
            errors.append('AX projection leaks value or has wrong keys')
        elif ast.unparse(element_dicts[0].values[list(k.value for k in element_dicts[0].keys).index('value_len')]) != 'length':
            errors.append('AX value length expression unsafe')
        # Logging is a closed AST surface: fixed refusal literals or an event
        # dictionary whose only dynamic field is len(elements).
        for node in ast.walk(parsed):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == 'print':
                expression = ast.unparse(node.args[0]) if len(node.args) == 1 else ''
                if expression not in {
                    "'{\"event\":\"refusal\",\"count\":1}'",
                    "json.dumps({'event': 'snapshot', 'count': len(result['elements'])})"}:
                    errors.append('AX unsafe logging')
        program = entry.split("<<'PYTHON'\n", 1)[1].rsplit('\nPYTHON', 1)[0]
        entry_ast = ast.parse(program)
        calls = [node for node in ast.walk(entry_ast) if isinstance(node, ast.Call)
                 and isinstance(node.func, ast.Name) and node.func.id == 'require_view_mount']
        if len(calls) != 1:
            errors.append('view mount preflight absent')
    except (OSError, ValueError, IndexError, SyntaxError) as exc:
        errors.append(type(exc).__name__)
    return errors


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument('--selftest', action='store_true')
    args = parser.parse_args()
    errors = check(args.root)
    killed = []
    if not errors and args.selftest:
        scratch = args.root / 'build/room_IMAGE'
        scratch.mkdir(parents=True, exist_ok=True)
        mutations = [('Dockerfile', '152.0.7977.82', '0.0.0.0'),
                     ('Dockerfile', 'RUN ', 'RUN apt-get update; '),
                     ('Dockerfile', 'dpkg-query', 'absent-query'),
                     ('xpra-layer.sh', 'XPRA_READONLY=yes', 'XPRA_READONLY=no'),
                     ('xpra-layer.sh', '--bind="${XPRA_SOCKET}"', '--bind-tcp=127.0.0.1:10000'),
                     ('xpra-layer.sh', '--socket-permissions=666', '--socket-permissions=600'),
                     ('xpra-layer.sh', 'start = /bin/true', 'start = /etc/X11/Xsession true'),
                     ('xpra-layer.sh', '--html=on', '--html=on --encodings=rgb,jpeg,scroll'),
                     ('xpra-layer.sh', '--html=on', '--html=on --min-quality=80'),
                     ('xpra-layer.sh', 'GET / HTTP/1.0', 'HEAD / HTTP/1.0'),
                     ('entrypoint.sh', '        require_view_mount()', '        pass'),
                     ('entrypoint.sh', '--proxy-server=', '--proxy-absent='),
                     ('entrypoint.sh', '-fbdir', '-absent'),
                     ('entrypoint.sh', '--disable-quic', '--no-sandbox'),
                     ('entrypoint.sh', '--disable-quic', '--remote-debugging-port=9222'),
                     ('ax-allowlist.json', 'Accessibility.enable', 'Runtime.evaluate'),
                     ('ax-allowlist.json', 'Accessibility.enable', 'Network.getCookies'),
                     ('ax-shim.py', "'value_len': length", "'value': length"),
                     ('ax-shim.py', "'value_len': length", "'value_len': value"),
                     ('ax-shim.py', "'count': len(result['elements'])", "'name': window['title']")]
        mutations += [('xpra-layer.sh', '--html=on', '--html=on ' + flag)
                      for flag in ['--encoding=rgb', '--min-speed=90', '--speed=100',
                                   '--compress=1', '--compression_level=1', '--compressors=lz4',
                                   '--auto-refresh-delay=0.15', '--video-scaling=off']]
        for filename, old, new in mutations:
            with tempfile.TemporaryDirectory(dir=scratch) as temp:
                clone = Path(temp)
                shutil.copytree(args.root / 'docker/identity', clone / 'docker/identity')
                target = clone / 'docker/identity' / filename
                original = target.read_text()
                if old not in original:
                    errors.append('dead mutation ' + old)
                    continue
                target.write_text(original.replace(old, new))
                if check(clone):
                    killed.append(old + ' -> ' + new)
                else:
                    errors.append('surviving mutation ' + old)
    print(json.dumps({'ok': not errors, 'errors': errors, 'mutants_killed': killed}))
    return 5 if errors else 0


if __name__ == '__main__':
    raise SystemExit(main())
