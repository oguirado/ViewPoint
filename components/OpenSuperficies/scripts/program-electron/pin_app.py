#!/usr/bin/env python3
"""Pin an unpacked app without executing it. Internal links are materialized."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import sys
import tempfile


class Refused(Exception):
    pass


def inventory(app):
    directories, files, sandbox = [], [], []

    def walk(path, relative, ancestors):
        try:
            resolved = path.resolve(strict=True)
            resolved.relative_to(app)
        except (ValueError, RuntimeError, OSError):
            raise Refused('app_link_escapes_or_is_invalid') from None
        if any(c in relative.as_posix() for c in ('\n', '\r', '\\')):
            raise Refused('invalid_app_filename')
        info = resolved.stat()
        mode = info.st_mode
        if mode & (stat.S_ISUID | stat.S_ISGID):
            if (not stat.S_ISREG(mode) or path.name != 'chrome-sandbox'
                    or mode & stat.S_ISGID or not mode & 0o111):
                raise Refused('app_privileged_mode')
            sandbox.append(relative.as_posix())
        if stat.S_ISDIR(mode):
            if resolved in ancestors:
                raise Refused('app_link_cycle')
            directories.append(relative)
            for child in sorted(resolved.iterdir()):
                walk(child, relative / child.name, ancestors | {resolved})
        elif stat.S_ISREG(mode):
            files.append((relative, resolved, bool(mode & 0o111)))
        else:
            raise Refused('app_requires_regular_files')

    walk(app, Path('.'), set())
    if not files:
        raise Refused('AUSENTE — app_files_missing')
    # Plain string order, as the build printer checks: 'resources.pak' sorts before 'resources/x'.
    return directories, sorted(files, key=lambda f: f[0].as_posix()), sandbox


def pin(app, root):
    app, root = Path(app).resolve(strict=True), Path(root).resolve(strict=True)
    if not app.is_dir():
        raise Refused('app_directory_required')
    destination = root / 'build/electron_app'
    if destination.exists() or destination.is_symlink():
        raise Refused('app_stage_already_exists')
    if app == root or app in destination.parents or destination in app.parents:
        raise Refused('app_stage_overlaps_source')
    directories, files, sandbox = inventory(app)
    destination.parent.mkdir(parents=True, exist_ok=True)
    metadata = root / 'docker/program-electron'
    metadata.mkdir(parents=True, exist_ok=True)
    # Build a private staging tree, publish only after the complete copy succeeds.
    temporary = Path(tempfile.mkdtemp(prefix='.electron-pin-', dir=destination.parent))
    try:
        for relative in directories:
            (temporary / relative).mkdir(parents=True, exist_ok=True)
        lines = []
        for relative, source, executable in files:
            target = temporary / relative
            shutil.copyfile(source, target)
            target.chmod(0o555 if executable else 0o444)
            lines.append(hashlib.sha256(target.read_bytes()).hexdigest() + '  ' + relative.as_posix() + '\n')
        manifest = ''.join(lines)
        for relative in reversed(directories):
            (temporary / relative).chmod(0o555)
        # Refuse an existing stage even if it appeared during the copy.
        if destination.exists() or destination.is_symlink():
            raise Refused('app_stage_already_exists')
        temporary.rename(destination)
        (metadata / 'app.manifest').write_text(manifest)
        # A new app pin invalidates any previous approval through the recipe hash.
        recipe_path = metadata / 'recipe.json'
        if recipe_path.exists():
            recipe = json.loads(recipe_path.read_text())
            recipe['app_manifest_sha256'] = hashlib.sha256(manifest.encode()).hexdigest()
            recipe_path.write_text(json.dumps(recipe, indent=2) + '\n')
        for _ in sandbox:
            print('chrome-sandbox: setuid bit detected and stripped; sandbox remains enabled', file=sys.stderr)
        return manifest
    finally:
        if temporary.exists():
            for directory, _, _ in os.walk(temporary):
                Path(directory).chmod(0o755)
            shutil.rmtree(temporary)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--app', required=True, type=Path)
    args = parser.parse_args()
    try:
        root = Path(__file__).resolve().parents[2]
        manifest = pin(args.app, root)
        print('PINNED app_manifest_sha256=' + hashlib.sha256(manifest.encode()).hexdigest())
        return 0
    except (Refused, OSError, ValueError) as exc:
        detail = str(exc) if isinstance(exc, Refused) else 'app_pin_unavailable'
        print('REFUSED: ' + detail, file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
