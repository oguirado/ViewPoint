#!/usr/bin/env python3
"""Assemble the Holdings toolchain image build context (no network, no Docker).

usage: prepare_context.py [--debs DIR] [--go FILE] [--out DIR]
Writes OUT/node.tar (node v24.14.0: bin, include, share and only the npm and corepack global
packages), OUT/rust.tar (the stable-aarch64 1.97.0 toolchain directory) and OUT/debs/*.deb and the operator-
fetched official Go archive (copied as-is, sha256-checked against GO_SHA256), all
deterministic (sorted, uid/gid 0, mtime 0), then prints a JSON manifest with every sha256 for
docker/toolchain/recipe.json. Exit codes: 0 ok, 2 usage, 4 missing input.
"""
import hashlib
import json
import os
import shutil
import sys
import tarfile
from pathlib import Path

HOME = Path.home()
NODE = HOME / '.nvm/versions/node/v24.14.0'
RUST = HOME / '.rustup/toolchains/stable-aarch64-unknown-linux-gnu'
NODE_GLOBALS = {'npm', 'corepack'}
GO_NAME = 'go1.26.10.linux-arm64.tar.gz'
GO_SHA256 = '3f4227ce7b206a513e794f1393adb473a07b46e94d18bd15d1c684bbb7319797'  # go.dev/dl JSON, 2026-10-26


def sha(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def keep_node(rel):
    parts = rel.parts
    if not parts or parts[0] not in ('bin', 'include', 'lib', 'share'):
        return False
    if parts[:2] == ('lib', 'node_modules') and len(parts) > 2 and parts[2] not in NODE_GLOBALS:
        return False
    if parts[0] == 'bin' and len(parts) == 2 and parts[1] not in ('node', 'npm', 'npx', 'corepack'):
        return False
    return True


def write_tar(root, out, keep=lambda rel: True):
    entries = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for name in sorted(dirnames) + sorted(filenames):
            p = Path(dirpath) / name
            rel = p.relative_to(root)
            if keep(rel):
                entries.append((str(rel), p))
    entries.sort()
    with tarfile.open(out, 'w', format=tarfile.PAX_FORMAT) as tar:
        for rel, p in entries:
            info = tar.gettarinfo(str(p), arcname=rel)
            info.uid = info.gid = 0
            info.uname = info.gname = ''
            info.mtime = 0
            info.pax_headers = {}
            if info.isfile():
                with open(p, 'rb') as f:
                    tar.addfile(info, f)
            else:
                tar.addfile(info)


def main(argv):
    debs = Path(HOME / '.local/share/opensuperficies/toolchain/debs')
    go = HOME / '.local/share/opensuperficies/toolchain/go' / GO_NAME
    out = Path(__file__).resolve().parents[2] / 'build/toolchain_ctx'
    args = argv[1:]
    while args:
        if args[0] == '--debs' and len(args) > 1:
            debs, args = Path(args[1]), args[2:]
        elif args[0] == '--go' and len(args) > 1:
            go, args = Path(args[1]), args[2:]
        elif args[0] == '--out' and len(args) > 1:
            out, args = Path(args[1]), args[2:]
        else:
            print('usage: prepare_context.py [--debs DIR] [--go FILE] [--out DIR]', file=sys.stderr)
            return 2
    deb_files = sorted(debs.glob('*.deb')) if debs.is_dir() else []
    if not (NODE / 'bin/node').is_file() or not (RUST / 'bin/rustc').is_file() or not deb_files:
        print('missing input: node, rust toolchain or operator-fetched debs (scripts/toolchain/fetch_debs.sh)', file=sys.stderr)
        return 4
    if not go.is_file() or go.is_symlink() or sha(go) != GO_SHA256:
        print(f'missing input: operator-fetched {GO_NAME} with sha256 {GO_SHA256}', file=sys.stderr)
        return 4
    if out.exists():
        shutil.rmtree(out)
    (out / 'debs').mkdir(parents=True)
    write_tar(NODE, out / 'node.tar', keep_node)
    write_tar(RUST, out / 'rust.tar')
    for d in deb_files:
        shutil.copyfile(d, out / 'debs' / d.name)
    shutil.copyfile(go, out / GO_NAME)
    manifest = {'node.tar': sha(out / 'node.tar'), 'rust.tar': sha(out / 'rust.tar'), GO_NAME: sha(out / GO_NAME)}
    manifest.update({'debs/' + d.name: sha(out / 'debs' / d.name) for d in deb_files})
    print(json.dumps({'context': str(out), 'artifacts': manifest}, indent=1, sort_keys=True))
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
