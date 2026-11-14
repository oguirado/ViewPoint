#!/usr/bin/env python3
"""srf-toolchain-run: run a repo's build/test command inside the toolchain capsule image.

usage: run.py --repo DIR --key KEY [--memory 12g] [--pids 4096] [--tmp 4g] [--timeout 3600]
              [--ro DIR]... [--env KEY=VALUE]... [--full 16m] [--tmpfs ENV=SIZE]... [--ref COMMIT] -- CMD...

- The repo is never written: it is rsync'ed (incremental) into a persistent per-key copy under
  ~/.local/state/opensuperficies/toolchain/<KEY>/repo, excluding k/rust/target, whose copy-side
  directory survives runs as the cargo cache. .git is copied (read-only use).
- Only ~/.cargo/registry and ~/.cargo/git are copied into the per-key CARGO_HOME (never
  credentials.toml or config files). CARGO_NET_OFFLINE=true in the image.
- The container: --network none, --read-only root, --cap-drop ALL, no-new-privileges, the caller's
  uid:gid, cpuset 10-14, --memory/--memory-swap cap, --pids-limit, an exec-capable tmpfs /tmp (tests spawn binaries there), its own PID
  namespace (a kill(-1) inside cannot reach the operator's session).
- --ro DIR mounts an existing host directory read-only at the same path (no /, no dotfiles
  such as ~/.ssh). --env adds a variable; the reserved ones below cannot be overridden.
- --ref COMMIT requires the repo's HEAD to be that commit. Untracked (not ignored) files are left
  out of the copy, and tracked files modified or deleted in the worktree are restored in the copy
  from the commit (git archive), so the run sees exactly the commit plus ignored dependencies such
  as node_modules and k/rust/target. Staged adds, renames and copies are refused.
- --full SIZE mounts a small tmpfs at /work/full (exported as HOLDINGS_TEST_FULL_TMPFS) for
  disk-full tests, so they need no unshare/user namespace.
- --tmpfs ENV=SIZE adds another capped tmpfs (rw, noexec) at /work/tmpfs/<env lowercased> and
  exports ENV with that path; for tests that need several real ENOSPC sizes.
- Login shells (bash -l, e.g. PTY tests) get the image's toolchain dirs back through a read-only
  /etc/profile.d/srf-toolchain.sh, since Debian's /etc/profile resets PATH.
- Env inside: R=/work/repo, SCR=/work/scr (writable scratch), HOME=/work/scr/home, TMPDIR=/tmp.
Output streams through; the exit code is the command's (124 on timeout, 4 on setup failure).
"""
import argparse
import os
import re
import secrets
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

IMAGE = 'opensuperficies-toolchain-arm64:0.2.0'
PROFILE = 'case ":$PATH:" in *:/opt/node/bin:*) ;; *) PATH="/opt/node/bin:/opt/rust/bin:$PATH"; export PATH ;; esac\n'
STATE = Path.home() / '.local/state/opensuperficies/toolchain'
SIZE_RE = re.compile(r'^[1-9][0-9]{0,3}[mg]$')
ENV_RE = re.compile(r'^([A-Z][A-Z0-9_]{0,63})=([^\0\n]{0,4096})$')
RESERVED = {'R', 'SCR', 'HOME', 'TMPDIR', 'CARGO_HOME', 'PATH', 'HOLDINGS_TEST_FULL_TMPFS'}
FULL_RE = re.compile(r'^[1-9][0-9]{0,2}m$')
TMPFS_RE = re.compile(r'^([A-Z][A-Z0-9_]{0,63})=([1-9][0-9]{0,2}m)$')


def ro_dir(raw):
    p = Path(raw)
    if not p.is_absolute() or p.is_symlink() or not p.is_dir():
        return None
    p = p.resolve()
    if p == Path('/') or len(p.parts) < 3 or any(part.startswith('.') and part != '.cache' for part in p.parts[1:]):
        return None
    return p


def sync(src, dst, excludes=()):
    dst.mkdir(parents=True, exist_ok=True)
    argv = ['rsync', '-a', '--delete', '--no-specials', '--no-devices']
    for e in excludes:
        argv += ['--exclude', e]
    argv += [str(src) + '/', str(dst) + '/']
    subprocess.run(argv, check=True)


def clean_ref(repo, ref):
    """(untracked, changed tracked paths, commit), or None when HEAD is not ref or the state is not restorable."""
    def git(*args):
        return subprocess.run(['git', '-C', str(repo), *args], capture_output=True, text=True, check=True).stdout
    try:
        head = git('rev-parse', 'HEAD').strip()
        want = git('rev-parse', '--verify', ref + '^{commit}').strip()
        status = git('status', '--porcelain=v1', '-z', '--untracked-files=normal')
    except (OSError, subprocess.CalledProcessError):
        print('ref check failed', file=sys.stderr)
        return None
    if head != want:
        print(f'HEAD {head[:12]} is not {ref}', file=sys.stderr)
        return None
    untracked, changed = [], []
    for entry in filter(None, status.split('\0')):
        code, path = entry[:2], entry[3:]
        if code == '??':
            untracked.append(path.rstrip('/'))
        elif set(code) <= {' ', 'M', 'D'}:
            changed.append(path)
        else:
            print(f'worktree state {code!r} not restorable; refusing --ref', file=sys.stderr)
            return None
    return untracked, changed, want


def restore(repo, ref, paths, dst):
    """Overwrite paths in the copy with their content at ref."""
    archive = subprocess.run(['git', '-C', str(repo), 'archive', '--format=tar', ref, '--', *paths],
                             capture_output=True, check=True).stdout
    subprocess.run(['tar', '-x', '-C', str(dst), '--no-same-owner'], input=archive, check=True)


def main(argv):
    if '--' not in argv:
        print('usage: run.py --repo DIR --key KEY [opts] -- CMD...', file=sys.stderr)
        return 2
    i = argv.index('--')
    ap = argparse.ArgumentParser(prog='srf-toolchain-run')
    ap.add_argument('--repo', required=True)
    ap.add_argument('--key', required=True)
    ap.add_argument('--memory', default='12g')
    ap.add_argument('--pids', type=int, default=4096)
    ap.add_argument('--tmp', default='4g')
    ap.add_argument('--timeout', type=int, default=3600)
    ap.add_argument('--ro', action='append', default=[])
    ap.add_argument('--env', action='append', default=[])
    ap.add_argument('--full')
    ap.add_argument('--ref')
    ap.add_argument('--tmpfs', action='append', default=[])
    a = ap.parse_args(argv[1:i])
    cmd = argv[i + 1:]
    repo = Path(a.repo).resolve()
    if (not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,39}', a.key) or not cmd or not repo.is_dir()
            or not SIZE_RE.match(a.memory) or not SIZE_RE.match(a.tmp) or not 64 <= a.pids <= 16384
            or not 10 <= a.timeout <= 4 * 3600):
        print('invalid arguments', file=sys.stderr)
        return 2
    ros = [ro_dir(r) for r in a.ro]
    envs = [ENV_RE.match(e) for e in a.env]
    tmpfs = [TMPFS_RE.match(t) for t in a.tmpfs]
    if (None in ros or None in envs or any(m.group(1) in RESERVED for m in envs)
            or (a.full is not None and not FULL_RE.match(a.full))
            or any(m is None or m.group(1) in RESERVED for m in tmpfs)
            or len({m.group(1) for m in tmpfs if m}) != len(tmpfs)
            or {m.group(1) for m in tmpfs if m} & {m.group(1) for m in envs if m}):
        print('invalid --ro or --env', file=sys.stderr)
        return 2
    base = STATE / a.key
    excludes = ['/k/rust/target']
    changed, commit = [], None
    if a.ref is not None:
        checked = clean_ref(repo, a.ref)
        if checked is None:
            return 3
        untracked, changed, commit = checked
        excludes += ['/' + u for u in untracked]
    try:
        sync(repo, base / 'repo', excludes=excludes)
        # rsync --delete keeps excluded paths left by an earlier run; drop them from the copy.
        for u in excludes[1:]:
            stale = base / 'repo' / u.lstrip('/')
            if stale.is_symlink() or stale.is_file():
                stale.unlink()
            elif stale.is_dir():
                shutil.rmtree(stale)
        if changed:
            restore(repo, commit, changed, base / 'repo')
            print(f'--ref: {len(changed)} worktree-modified tracked files restored from {commit[:12]}', file=sys.stderr)
        for part in ('registry', 'git'):
            src = Path.home() / '.cargo' / part
            if src.is_dir():
                sync(src, base / 'cargo' / part)
        (base / 'scr' / 'home').mkdir(parents=True, exist_ok=True)
        profile = base / 'profile.sh'
        profile.write_text(PROFILE)
        profile.chmod(0o644)
    except (OSError, subprocess.CalledProcessError) as e:
        print(f'setup failed: {type(e).__name__}', file=sys.stderr)
        return 4
    name = f'srf-tc-{a.key}-{secrets.token_hex(3)}'
    run = ['docker', 'run', '--rm', '--name', name, '--network', 'none', '--read-only',
           '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges', '--user', f'{os.getuid()}:{os.getgid()}',
           '--cpuset-cpus', '10-14', '--memory', a.memory, '--memory-swap', a.memory, '--pids-limit', str(a.pids),
           '--shm-size', '2g', '--tmpfs', f'/tmp:rw,exec,nosuid,nodev,mode=1777,size={a.tmp}',
           '-v', f'{base / "profile.sh"}:/etc/profile.d/srf-toolchain.sh:ro',
           '-v', f'{base / "repo"}:/work/repo', '-v', f'{base / "scr"}:/work/scr', '-v', f'{base / "cargo"}:/work/cargo',
           '-e', 'R=/work/repo', '-e', 'SCR=/work/scr', '-e', 'HOME=/work/scr/home', '-e', 'TMPDIR=/tmp',
           '-e', 'CARGO_HOME=/work/cargo', '-w', '/work/repo']
    for r in ros:
        run += ['-v', f'{r}:{r}:ro']
    for m in envs:
        run += ['-e', m.group(0)]
    if a.full:
        run += ['--tmpfs', f'/work/full:rw,noexec,nosuid,nodev,mode=1777,size={a.full}',
                '-e', 'HOLDINGS_TEST_FULL_TMPFS=/work/full']
    for m in tmpfs:
        path = '/work/tmpfs/' + m.group(1).lower()
        run += ['--tmpfs', f'{path}:rw,noexec,nosuid,nodev,mode=1777,size={m.group(2)}', '-e', f'{m.group(1)}={path}']
    run += [IMAGE,
           'bash', '-c', shlex.join(cmd) if len(cmd) > 1 else cmd[0]]
    try:
        return subprocess.run(run, timeout=a.timeout).returncode
    except subprocess.TimeoutExpired:
        subprocess.run(['docker', 'kill', name], capture_output=True)
        print(f'timeout after {a.timeout} s; container {name} killed', file=sys.stderr)
        return 124


if __name__ == '__main__':
    sys.exit(main(sys.argv))
