#!/usr/bin/env python3
"""Operator post-build inspection only. Overrides entrypoint; never launches GUI."""
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

sys.dont_write_bytecode = True


def main():
    root = Path(__file__).resolve().parents[2]
    try:
        spec = importlib.util.spec_from_file_location('electron_builder', root / 'scripts/program-electron/print_build.py')
        builder = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(builder)
        recipe = builder.parse((root / 'docker/program-electron/recipe.json').read_bytes())
        builder.command(root, recipe)
        image = recipe['image']
        metadata = json.loads(subprocess.run(['docker', 'image', 'inspect', image], check=True,
                              capture_output=True, text=True, timeout=15).stdout)[0]
        config = metadata['Config']
        if (metadata['Architecture'] != 'arm64' or config['User'] != '10001:0'
                or config['WorkingDir'] != '/profile'
                or config['Entrypoint'] != ['/usr/local/bin/identity-entrypoint.sh']
                or any(e.split('=', 1)[0].startswith(('ELECTRON_', 'NODE_', 'CHROME_'))
                       for e in config['Env'])):
            raise ValueError()
        expected = dict(line.split('  ', 1)[::-1] for line in
                        (root / 'docker/program-electron/app.manifest').read_text().splitlines())
        baked = {target: hashlib.sha256((root / source).read_bytes()).hexdigest() for source, target in [
            ('docker/program/entrypoint.sh', '/usr/local/bin/identity-entrypoint.sh'),
            ('docker/identity/xpra-layer.sh', '/usr/local/bin/identity-xpra-layer.sh'),
            ('docker/program-electron/app.json', '/usr/local/share/srf-app/app.json')]}
        probe = '''import ast, hashlib, json, os, stat, subprocess, sys
from pathlib import Path
assert os.getuid() == 10001
assert os.environ['XDG_RUNTIME_DIR'] == '/run/newton-tenant'
assert not any(k.startswith(('ELECTRON_', 'NODE_', 'CHROME_')) for k in os.environ)
# Exercise the actual program-spawn boundary with a Python stand-in, never Electron.
source = Path('/usr/local/bin/identity-entrypoint.sh').read_text().split("<<'PYTHON'\\n", 1)[1].rsplit('\\nPYTHON', 1)[0]
tree = ast.parse(source)
tree.body = [n for n in tree.body if isinstance(n, ast.FunctionDef)
             and n.name in ('program_env', 'spawn_program')]
scope = dict(os=os, subprocess=subprocess)
exec(compile(tree, '<program-spawn>', 'exec'), scope)
os.environ.update(ELECTRON_RUN_AS_NODE='1', ELECTRON_DISABLE_SANDBOX='1',
                  NODE_OPTIONS='--inspect', NODE_PATH='/untrusted', CHROME_LOG_FILE='/untrusted',
                  DBUS_SESSION_BUS_ADDRESS='unix:path=/untrusted', UNLISTED='drop')
child_probe = ("import os; assert not any(k.startswith(('ELECTRON_', 'NODE_', 'CHROME_')) for k in os.environ); "
               "assert 'UNLISTED' not in os.environ; assert os.environ['DBUS_SESSION_BUS_ADDRESS'] == 'disabled:'; "
               "assert all(k in {'DISPLAY', 'HOME', 'LANG', 'PATH', 'NO_AT_BRIDGE', 'DBUS_SESSION_BUS_ADDRESS', "
               "'GTK_USE_PORTAL', 'GIO_USE_VFS'} or k.startswith(('XDG_', 'LC_')) for k in os.environ)")
assert scope['spawn_program']([sys.executable, '-I', '-c', child_probe]).wait(timeout=5) == 0
expected = json.loads(%r)
baked = json.loads(%r)
root = Path('/opt/srf-app')
actual = set()
for directory, dirs, files in os.walk(root):
    assert stat.S_IMODE(Path(directory).stat().st_mode) == 0o555
    for name in files:
        path = Path(directory) / name
        assert not path.is_symlink() and path.is_file()
        assert stat.S_IMODE(path.stat().st_mode) in (0o444, 0o555)
        rel = path.relative_to(root).as_posix()
        assert hashlib.sha256(path.read_bytes()).hexdigest() == expected[rel]
        actual.add(rel)
assert actual == set(expected)
for name, sha in baked.items():
    assert hashlib.sha256(Path(name).read_bytes()).hexdigest() == sha
assert stat.S_IMODE(Path('/usr/local/share/srf-app/app.json').stat().st_mode) == 0o444
app = json.loads(Path('/usr/local/share/srf-app/app.json').read_text())
assert app['exe'] in expected and os.access(root / app['exe'], os.X_OK)
''' % (json.dumps(expected), json.dumps(baked))
        subprocess.run(['docker', 'run', '--rm', '--network=none', '--read-only', '--cap-drop=ALL',
                        '--entrypoint', 'python3', image, '-I', '-c', probe],
                       check=True, capture_output=True, text=True, timeout=30)
        for package, version in recipe['package_pins'].items():
            result = subprocess.run(['docker', 'run', '--rm', '--network=none', '--read-only',
                                     '--cap-drop=ALL', '--entrypoint', 'dpkg-query', image,
                                     '-W', '-f=${Version}', package], check=True,
                                    capture_output=True, text=True, timeout=15)
            if result.stdout != version:
                raise ValueError()
        print('electron program image contract passed')
        return 0
    except (OSError, ValueError, KeyError, IndexError, TypeError, subprocess.SubprocessError):
        print('REFUSED: electron_image_contract', file=sys.stderr)
        return 6
    except builder.Refused:
        print('REFUSED: electron_image_recipe', file=sys.stderr)
        return 6


if __name__ == '__main__':
    sys.exit(main())
