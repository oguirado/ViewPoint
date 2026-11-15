"""Render, sync and inspect the four vision skill copies; stdlib only."""
import hashlib
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent


class Refused(Exception):
    pass


def targets():
    return [ROOT / 'dist' / host / 'vision' for host in ('claude', 'codex')] + [
        Path.home() / host / 'skills/vision' for host in ('.claude', '.agents')]


def expected():
    template = (ROOT / 'skill/SKILL.md.tmpl').read_bytes()
    sha = hashlib.sha256(template).hexdigest()

    def render(data):
        return (data.replace(b'@VISION_PATH@', os.fsencode(ROOT / 'vision'))
                .replace(b'@VIS_PATH@', os.fsencode(ROOT / 'vis'))
                .replace(b'@SOURCE_SHA@', sha.encode()))

    files = {'SKILL.md': render(template)}
    for p in sorted((ROOT / 'skill/references').rglob('*')):
        if p.is_symlink():
            raise Refused('symlink in skill source: ' + str(p))
        if p.is_file():
            files['references/' + p.relative_to(ROOT / 'skill/references').as_posix()] = render(p.read_bytes())
    return files


def directories(files):
    return {str(parent) for name in files for parent in Path(name).parents if str(parent) != '.'}


def guard(dest, *, home=False):
    if home and dest.resolve().is_relative_to(ROOT):
        raise Refused('home target inside checkout: ' + str(dest))
    for p in [dest, *dest.parents]:
        if p.is_symlink():
            raise Refused('symlink in target path: ' + str(p))
        if p.exists() and not p.is_dir():
            raise Refused('target ancestor is not a directory: ' + str(p))
    if dest.exists():
        for p in dest.rglob('*'):
            if p.is_symlink() or not (p.is_file() or p.is_dir()):
                raise Refused('unsafe target entry: ' + str(p))


def plan(dest, files):
    dirs = directories(files)
    deletes = []
    for p in dest.rglob('*'):
        name = p.relative_to(dest).as_posix()
        if (p.is_dir() and name not in dirs) or (p.is_file() and name not in files):
            deletes.append(p)
        elif (p.is_dir() and name in files) or (p.is_file() and name in dirs):
            deletes.append(p)
    operations = [('DELETE', p, None) for p in sorted(deletes, key=lambda p: (-len(p.parts), str(p)))]
    needed = {dest, *(dest / name for name in dirs)}
    for p in list(needed):
        needed.update(q for q in p.parents if not q.exists())
    operations += [('MKDIR', p, None) for p in sorted(needed, key=lambda p: (len(p.parts), str(p)))
                   if not p.is_dir()]
    operations += [('WRITE', dest / name, data) for name, data in sorted(files.items())
                   if not (dest / name).is_file() or (dest / name).read_bytes() != data]
    return operations


def inspect(files):
    copies = []
    for i, dest in enumerate(targets()):
        copy = dict(path=str(dest), missing=[], changed=[], orphan=[])
        try:
            guard(dest, home=i >= 2)
            dirs = directories(files)
            copy['missing'] = sorted(name for name in files if not (dest / name).is_file())
            copy['changed'] = sorted(name for name, data in files.items()
                                     if (dest / name).is_file() and (dest / name).read_bytes() != data)
            copy['orphan'] = sorted(p.relative_to(dest).as_posix() for p in dest.rglob('*')
                                    if p.relative_to(dest).as_posix() not in (dirs if p.is_dir() else files))
        except (OSError, Refused) as exc:
            copy['error'] = str(exc)
        copies.append(copy)
    legacy = [str(p.parent / 'vis') for p in targets()
              if (p.parent / 'vis').exists() or (p.parent / 'vis').is_symlink()]
    return dict(drift=any(c['missing'] or c['changed'] or c['orphan'] or c.get('error') for c in copies),
                skill_copies=copies, legacy_copies=legacy,
                warnings=['legacy_skill_copy_present'] if legacy else [])


def main():
    if len(sys.argv) != 2 or sys.argv[1] not in ('--apply', '--dry-run', '--check'):
        print('usage: skill_copies.py [--apply|--dry-run|--check]', file=sys.stderr)
        return 2
    mode = sys.argv[1]
    if mode == '--apply' and os.environ.get('VISION_INSTALL_OK') != '1' and os.environ.get('VIS_INSTALL_OK') != '1':
        raise Refused('--apply requires VISION_INSTALL_OK=1 (VIS_INSTALL_OK=1 also accepted)')
    files = expected()
    if mode == '--check':
        print(json.dumps(inspect(files), sort_keys=True))
        return 0
    # Validate all destinations and build the entire plan before creating any directory.
    destinations = targets()
    for i, dest in enumerate(destinations):
        guard(dest, home=i >= 2)
    operations = [op for dest in destinations for op in plan(dest, files)]
    for action, path, data in operations:
        print(action + ' ' + str(path))
        if mode == '--dry-run':
            continue
        if action == 'DELETE':
            path.rmdir() if path.is_dir() else path.unlink()
        elif action == 'MKDIR':
            path.mkdir(exist_ok=True)
        else:
            path.write_bytes(data)
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (Refused, OSError) as exc:
        print('E_RULE: ' + str(exc), file=sys.stderr)
        sys.exit(5)
