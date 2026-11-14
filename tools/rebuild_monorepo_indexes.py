#!/usr/bin/env python3
"""Publish root-owned indexes with the repository's existing builders."""
import hashlib
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def main():
    def run(*args):
        subprocess.run([sys.executable, *args], cwd=ROOT, check=True)
    run('tools/route_docs.py', 'build')
    run('tools/router_index.py')
    with tempfile.TemporaryDirectory(prefix='vp-index-') as directory:
        built = Path(directory) / 'index'
        args = ('router/kit/tools/build_index_v2.py', '--nodes', 'catalogue/nodes.jsonl',
                '--thesaurus', 'router/kit/standards/thesaurus.json',
                '--corpus', 'metaviewer', '--out', str(built))
        run(*args)
        run(*args, '--check')
        target = ROOT / 'catalogue/router'
        # Replace generated data only, preserving historical bootstrap receipts.
        shutil.rmtree(target)
        shutil.copytree(built, target)
    sums = ROOT / 'router/index/SHA256SUMS'
    prior = {line.split('  ', 1)[1] for line in sums.read_text().splitlines() if line}
    paths = {rel for rel in prior if not rel.startswith('catalogue/router/')}
    paths.update(p.relative_to(ROOT).as_posix() for p in target.rglob('*') if p.is_file())
    sums.write_text(''.join(hashlib.sha256((ROOT / rel).read_bytes()).hexdigest() +
                            '  ' + rel + '\n' for rel in sorted(paths)))
    run('route/route_query.py', 'check')
    run('tools/router_index.py', '--check')


if __name__ == '__main__':
    main()
