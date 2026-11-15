#!/usr/bin/env python3
"""Local, offline monorepo gate (arm64 and x86_64); retain commands, logs and receipts in ctl.

The root gate and each component suite receive separate memory-capped scopes.
Component commands run in disposable pinned checkouts, never components/.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
LEGS = ('manifests', 'laws', 'contracts', 'unit', 'agents', 'js', 'rust', 'go',
        'ui', 'ui-smoke', 'perf')
# Suites that use the same fixed host path must never overlap, even in --parallel.
SHARED_RESOURCES = {
    'vision-ramws': ('vision-engine/python', 'vision-engine/ci-health', 'vision-engine/gates',
                     'vision-engine/mutants', 'OpenSuperficies/cu-gates', 'OpenSuperficies/cu-suite'),
}
RESOURCE_LOCKS = {name: threading.Lock() for name in SHARED_RESOURCES}
# systemd 259 expands ${...} in the command unless told not to; component suites pass bash -c scripts.
CAP = ['systemd-run', '--user', '--scope', '--expand-environment=no', '-p', 'MemoryHigh=6G',
       '-p', 'MemoryMax=8G', '-p', 'MemorySwapMax=0', '--']


def git(root, *args):
    return subprocess.check_output(['git', '-C', str(root), *args],
                                   stderr=subprocess.PIPE, text=True).strip()


def local_checkout(source, target, pin, owner=None):
    """No network: the pinned object must already exist in the local source.

    Component checkouts receive only their own tags, kept in the monorepo under
    refs/component-tags/<owner>/ so they never collide with root release tags."""
    if git(source, 'rev-parse', '--verify', pin + '^{commit}') != pin:
        raise ValueError('local pin cannot be resolved')
    if not target.exists():
        subprocess.run(['git', 'clone', '--shared', '--no-checkout',
                        *(['--no-tags'] if owner else []), '--',
                        str(source), str(target)], check=True, capture_output=True)
        if owner:
            subprocess.run(['git', '-C', str(target), 'fetch', '-q', '--no-tags', str(source),
                            'refs/component-tags/' + owner + '/*:refs/tags/*'],
                           check=True, capture_output=True)
        subprocess.run(['git', '-C', str(target), 'checkout', '--detach', pin],
                       check=True, capture_output=True)
    if git(target, 'rev-parse', 'HEAD') != pin or git(target, 'status', '--porcelain'):
        raise ValueError('pinned checkout is dirty or at another revision: ' + str(target))
    return target


def component_workdirs(ctl):
    """Physical short paths preserve the component's repository-local sockets."""
    parent = ctl / 'w'
    parent.mkdir(parents=True, exist_ok=True)
    if parent.is_symlink():
        raise ValueError('component work parent must not be a symlink')
    base = Path(tempfile.mkdtemp(prefix='vp-', dir=parent))
    work = {owner: base / short for owner, short in (
        ('wender-agent', 'w'), ('vision-engine', 'v'), ('OpenSuperficies', 'o'))}
    socket = work['OpenSuperficies'] / 'build/room_FB3/tmp/test-1451585838/peer.sock'
    if len(os.fsencode(socket)) >= 108:
        raise ValueError('component workspace exceeds Unix socket path budget')
    return work


def environment(ctl, scratch):
    if os.uname().machine not in ('aarch64', 'arm64', 'x86_64'):
        raise ValueError('unsupported architecture: ' + os.uname().machine)
    scratch.mkdir(parents=True, exist_ok=True, mode=0o700)
    if scratch.is_symlink():
        raise ValueError('scratch must not be a symlink')
    filesystem = subprocess.check_output(['stat', '-f', '-c', '%T', str(scratch)], text=True).strip()
    if filesystem not in ('ext2/ext3', 'xfs', 'btrfs'):
        raise ValueError('unqualified scratch filesystem: ' + filesystem)
    # Go needs a short disk-backed path; hosts whose /tmp is tmpfs name another.
    go_scratch = Path(os.environ.get('VP_GO_TMPDIR', '/tmp'))
    go_filesystem = subprocess.check_output(
        ['stat', '-f', '-c', '%T', str(go_scratch)], text=True).strip()
    if go_filesystem not in ('ext2/ext3', 'xfs', 'btrfs'):
        raise ValueError('Go requires a short qualified disk-backed temporary directory')
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1', TMPDIR=str(scratch),
               CARGO_BUILD_JOBS='2', CARGO_NET_OFFLINE='true', GOPROXY='off',
               GOSUMDB='off', GOTOOLCHAIN='local', PIP_NO_INDEX='1',
               npm_config_offline='true', npm_config_audit='false', npm_config_fund='false',
               ELECTRON_SKIP_BINARY_DOWNLOAD='1', MV_GATE_COMMAND_SECONDS='1200',
               MV_GO_TMPDIR=str(go_scratch),
               MV_MW_WENDER_ROOT=str(ROOT / 'components/wender-agent'))
    # Owner fixtures map their explicit root locally. A global planner root
    # would change unrelated legacy planner tests and production selection.
    env.pop('MV_WENDER_ROOT', None)
    jobs = os.environ.get('VP_CARGO_BUILD_JOBS', '2')
    if not jobs.isdigit() or not 1 <= int(jobs) <= 2:
        raise ValueError('VP_CARGO_BUILD_JOBS must be a positive integer at most two')
    env['CARGO_BUILD_JOBS'] = jobs
    # A reusable cold-build target does not bypass formatter, Clippy or tests.
    env['CARGO_TARGET_DIR'] = str(ctl / 'cargo-target')
    lock = json.loads((ROOT / 'deps.lock').read_text())
    wender = next(c for c in lock['components'] if c['name'] == 'wender-agent')
    if git(ROOT, 'rev-parse', 'HEAD:components/wender-agent') != git(
            ROOT, 'rev-parse', wender['commit'] + '^{tree}'):
        raise ValueError('Wender subtree differs from deps.lock')
    # Provision the protected reconciler artifact required by the unchanged test
    # fixture. Git does not track group/other write bits; its bytes stay intact.
    artifact = ROOT / 'tools/arm64_capsule_reconcile.py'
    info = artifact.lstat()
    if artifact.is_symlink() or info.st_uid != os.getuid() or info.st_nlink != 1:
        raise ValueError('reconciler artifact custody is not private')
    artifact.chmod(info.st_mode & ~0o022)
    return env, {'architecture': os.uname().machine, 'filesystem': filesystem,
                 'scratch': str(scratch), 'go_scratch': str(go_scratch),
                 'go_filesystem': go_filesystem, 'wender_pin': wender['commit'],
                 'cargo_jobs': int(jobs), 'revision': git(ROOT, 'rev-parse', 'HEAD'),
                 'branch': git(ROOT, 'branch', '--show-current')}


def provision_kit(ctl, env):
    lock = json.loads((ROOT / 'ui/kit.lock.json').read_text())
    source = Path(os.environ.get('VP_KIT_SOURCE', str(ROOT.parents[1] / 'capsule-ui-kit')))
    if git(source, 'config', '--get', 'remote.origin.url') != lock['repo']:
        raise ValueError('UI kit source origin differs from lock')
    target = ctl / 'deps' / ('capsule-ui-kit-' + lock['commit'])
    target.parent.mkdir(parents=True, exist_ok=True)
    local_checkout(source, target, lock['commit'])
    # The sync tool checks origin, HEAD and contentHash independently.
    origin = git(target, 'config', '--get', 'remote.origin.url')
    if origin == str(source):
        subprocess.run(['git', '-C', str(target), 'config', 'remote.origin.url', lock['repo']], check=True)
    elif origin != lock['repo']:
        raise ValueError('pinned kit checkout origin differs from lock')
    env['MV_KIT_CHECKOUT'] = str(target)
    return {'checkout': str(target), 'HEAD': git(target, 'rev-parse', 'HEAD'), **lock}


def run_case(name, cwd, argv, env, out, seconds=1200, blocked=None):
    log = out / (name.replace('/', '__') + '.log')
    started = time.monotonic()
    command = [*CAP, 'timeout', '--signal=TERM', '--kill-after=10s', str(seconds) + 's', *argv]
    with log.open('w') as stream:
        stream.write('CWD: ' + str(cwd) + '\nCOMMAND: ' + shlex.join(command) + '\n')
        stream.flush()
        if blocked:
            stream.write('BLOCKED: ' + blocked + '\n')
            rc = 2
        else:
            try:
                rc = subprocess.run(command, cwd=cwd, env=env, stdout=stream,
                                    stderr=subprocess.STDOUT, timeout=seconds + 30).returncode
            except (OSError, subprocess.TimeoutExpired) as error:
                stream.write(str(error) + '\n')
                rc = 124 if isinstance(error, subprocess.TimeoutExpired) else 2
        stream.write('\nVP_RC: ' + str(rc) + '\n')
    status = 'PASS' if rc == 0 else 'BLOCKED' if rc == 2 else 'FAIL'
    row = dict(name=name, status=status, rc=rc, seconds=round(time.monotonic()-started, 3),
               cwd=str(cwd), command=command, log=str(log),
               sha256=hashlib.sha256(log.read_bytes()).hexdigest())
    print(status + ' ' + name + ' rc=' + str(rc) + ' log=' + str(log), flush=True)
    return row


def component_cases(work):
    """Explicit TRIAGE suite inventory; keep existing upstream exclusions visible."""
    cases = []
    def add(owner, label, command, relative='.', note=None):
        name = owner + '/' + label
        cases.append(dict(name=name, owner=owner, cwd=work[owner] / relative, command=command,
                          note=note, resources=sorted(r for r, users in SHARED_RESOURCES.items()
                                                      if name in users)))
    py = ['python3', '-B']
    for owner in ('wender-agent', 'vision-engine'):
        add(owner, 'python', py + ['-m', 'unittest', 'discover', '-s', 'tests', '-t', '.'])
        # Health output is data, not a change to the pinned checkout. The
        # component's scope controls must see only the intended source tree.
        add(owner, 'ci-health', ['bash', 'tools/ci_health.sh',
                                str(work[owner].parent / ('health-' + owner))],
            note='Upstream per-file health excludes its declared heavy file; full discovery is separate.')
    add('wender-agent', 'javascript', ['bash', '-c', 'set -e; files=(js/tests/*.test.js); test -f "${files[0]}"; node --test "${files[@]}"'])
    for label, script, args in (
        ('reseal', 'reseal.py', ['--check']), ('route-code', 'route_code.py', ['--check']),
        ('code-index', 'build_code_index.py', ['--check-index']), ('anchors', 'anchors.py', []),
        ('layers', 'layer_check.py', []), ('state-budget', 'state_budget.py', []),
        ('memoryweb', 'memoryweb_gate.py', ['entry-check'])):
        command = py + (['-m', 'tools.state_budget'] if label == 'state-budget'
                        else ['tools/' + script, *args])
        add('wender-agent', label, command)
    for label, script, args in (
        ('gates', 'gates.py', ['--all']), ('mutants', 'gates.py', ['--mutants']),
        ('catalogue', 'build_catalogue.py', ['--check']),
        ('code-index', 'build_code_index.py', ['--check']),
        ('native', 'build_native.py', []), ('wheel', 'build_wheel.py', [])):
        add('vision-engine', label, py + ['tools/' + script, *args])
    for manifest in sorted(work['vision-engine'].glob('rust/*/Cargo.toml')):
        relative = manifest.parent.relative_to(work['vision-engine']).as_posix()
        add('vision-engine', 'release-' + manifest.parent.name,
            ['cargo', 'build', '--release', '--offline', '-j', '2'], relative)
    add('vision-engine', 'hyperbook-reseal', py + ['hyperbooks/_tools/reseal.py'])
    for relative in ('langx/x1', 'langx/x1/host/adapters'):
        for index in range(1, 6):
            add('vision-engine', relative.replace('/', '-') + '-rust-w' + str(index),
                ['cargo', 'test', '--offline'], relative + '/rust_w' + str(index))
            add('vision-engine', relative.replace('/', '-') + '-go-w' + str(index),
                ['go', 'test', './...'], relative + '/go_w' + str(index))
    for index in range(1, 4):
        relative = 'langx/x2/ts_w' + str(index)
        # W2's child CLI is resolved from the component root by the test itself.
        add('vision-engine', 'typescript-w' + str(index),
            ['node', '--test', relative + '/x2.test.ts' if index == 2 else 'x2.test.ts'],
            '.' if index == 2 else relative)
    # Peer wheel is built from the pinned component. The upstream wheel witness
    # retains its equal:5e2395d5 assertion; an incompatible peer remains a FAIL.
    add('vision-engine', 'peer-wheel', py + ['tools/build_wheel.py', '--out', 'build/peer'])
    for label, command in (
        ('cu-gates', ['bash', 'scripts/cu/gates.sh']),
        ('cu-suite', ['bash', 'scripts/cu/run_suite.sh']),
        ('hyperbook', ['bash', 'hyperbooks/_tools/os/hb_os_gates.sh', 'HB-OS-cubook']),
        ('oracle', ['bash', 'scripts/cu/oracle_gates.sh']), ('build', ['make', 'build']),
        ('go-vet', ['go', 'vet', './...']),
        ('go-race', ['bash', '-c', 'set -euo pipefail; mapfile -t pkgs < <(go list ./... | grep -Ev "src/kernelbook/cgroup$|src/kernelbook/escalera$|src/surfacebook/pbx$|src/surfacebook/grabador$"); test "${#pkgs[@]}" -gt 0; go test -count=1 -race -skip "^(TestEstadoPresupuestoDeTokens)$" "${pkgs[@]}"']),
        ('worker-contracts', py + ['-m', 'unittest', 'discover', '-s', 'contracts/worker/tests', '-t', '.'])):
        add('OpenSuperficies', label, command)
    add('OpenSuperficies', 'cockpit', ['env', 'CGO_ENABLED=0', 'go', 'test', '-count=1', '-tags', 'cockpit', '.'], 'cockpit')
    for label, command in (
        ('frontend-install', ['npm', 'ci', '--offline', '--no-audit', '--no-fund']),
        ('frontend-tests', ['npm', 'exec', '--offline', '--', 'vitest', 'run', '--exclude', 'tests/terminal.test.ts']),
        ('frontend-build', ['npm', 'exec', '--offline', '--', 'vite', 'build'])):
        add('OpenSuperficies', label, command, 'cockpit/frontend')
    # The upstream final mutant gate alone allows 1500 seconds. Its parent
    # must leave room for the earlier gates instead of cutting that suite short.
    for case in cases:
        if case['name'] == 'OpenSuperficies/cu-gates':
            case['seconds'] = 3600
        if case['name'] in ('vision-engine/python', 'vision-engine/ci-health'):
            # Full discovery measured above 1200 seconds on the reference host.
            case['seconds'] = 3600
        if case['name'] == 'OpenSuperficies/go-race':
            # This CI selection includes TestXTestWithLiveDisplay, which falls
            # back to :1 when DISPLAY is empty. Run it on a dedicated display.
            case['command'] = ['xvfb-run', '--auto-servernum', *case['command']]
    # Native artifacts are prerequisites of the component tests, not evidence
    # that can be supplied after those tests have already run.
    builders = [case for case in cases if case['name'].startswith('vision-engine/release-')
                or case['name'] in ('vision-engine/native', 'vision-engine/wheel',
                                    'vision-engine/peer-wheel')]
    return builders + [case for case in cases if case not in builders]


def guarded(name, step, record):
    """A step that raises still leaves a FAIL row, so a crashed thread can never pass."""
    def run():
        try:
            step()
        except Exception as error:  # noqa: BLE001 - recorded as a failed step
            print('FAIL ' + name + ' error=' + repr(error), flush=True)
            record(dict(name=name, status='FAIL', rc=1, seconds=0.0, error=repr(error)))
    return run


def component_lanes(cases):
    """Split the ordered plan into native builders and independent per-owner lanes.

    Builders run first; afterwards each owner keeps its own order in its own
    checkout, so owners never share a working tree or a build directory."""
    builders = [case for case in cases if case['name'].startswith('vision-engine/release-')
                or case['name'] in ('vision-engine/native', 'vision-engine/wheel',
                                    'vision-engine/peer-wheel')]
    lanes = {}
    for case in cases:
        if case not in builders:
            lanes.setdefault(case['owner'], []).append(case)
    return builders, list(lanes.values())


def component_environment(environment, ctl):
    child = dict(environment, DISPLAY='', WAYLAND_DISPLAY='',
                 CU_CPUSET=','.join(map(str, sorted(os.sched_getaffinity(0))[-5:])))
    # Component consumers use their crate-local target/release paths.
    child.pop('CARGO_TARGET_DIR', None)
    child.pop('MV_WENDER_ROOT', None)
    child.pop('MV_MW_WENDER_ROOT', None)
    return child


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--leg', choices=LEGS)
    parser.add_argument('--components', action='store_true', help='run only the component step')
    parser.add_argument('--parallel', action='store_true',
                        help='run the root legs and each component owner concurrently')
    args = parser.parse_args()
    os.umask(0o022)
    ctl = ROOT.parent / 'ctl'
    scratch = ROOT.parent / 'scratch'
    ctl.mkdir(parents=True, exist_ok=True)
    out = ctl / 'runs' / ('gate-' + str(time.time_ns()))
    out.mkdir(parents=True)
    rows, evidence = [], {}
    try:
        env, evidence = environment(ctl, scratch)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print('BLOCKED environment: ' + str(error), flush=True)
        return 2
    lock_rows = threading.Lock()

    def record(row, note=None):
        if note:
            row['coverage_note'] = note
        with lock_rows:
            rows.append(row)
            (out / 'results.json').write_text(json.dumps(dict(evidence=evidence, results=rows), indent=2) + '\n')

    # Shared state is prepared before any thread starts; threads only add rows.
    kit_error = None
    if not args.components:
        try:
            evidence['kit'] = provision_kit(ctl, env)
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            kit_error = str(error)

    def root_lane():
        for leg in (args.leg,) if args.leg else LEGS:
            if leg == 'ui' and kit_error is None:
                # UI-only invocations also satisfy the existing kit prerequisite.
                record(run_case('root/ui-kit-preflight', ROOT,
                                ['node', 'tools/sync_kit.mjs'], env, out))
                record(run_case('root/ui-kit-check', ROOT,
                                ['node', 'tools/sync_kit.mjs', '--check'], env, out))
            record(run_case('root/' + leg, ROOT, ['bash', 'tools/gate.sh', '--leg', leg],
                            env, out, seconds=1800,
                            blocked=kit_error if leg in ('js', 'ui') else None))

    def run_component(case, child):
        child = dict(child)
        if case['name'] == 'OpenSuperficies/cu-gates':
            wheels = sorted((work['vision-engine'] / 'build/peer').glob('*.whl'))
            # Supplying a missing peer forces the upstream refusal; it never
            # falls through to an unrelated absolute host checkout.
            child['VE_PEER_WHL'] = str(wheels[0] if len(wheels) == 1 else out / 'missing-peer.whl')
        locks = [RESOURCE_LOCKS[name] for name in case['resources']]
        for lock in locks:
            lock.acquire()
        try:
            row = run_case(case['name'], case['cwd'], case['command'], child, out,
                           seconds=case.get('seconds', 1200))
        finally:
            for lock in reversed(locks):
                lock.release()
        record(row, case['note'])

    def component_step():
        lock = json.loads((ROOT / 'deps.lock').read_text())
        pins = {c['name']: c['commit'] for c in lock['components']}
        work.update(component_workdirs(ctl))
        for owner in ('wender-agent', 'vision-engine', 'OpenSuperficies'):
            pin = pins['opensuperficies' if owner == 'OpenSuperficies' else owner]
            local_checkout(ROOT, work[owner], pin, owner)
        with lock_rows:
            evidence['component_workdirs'] = {owner: str(path) for owner, path in work.items()}
        child = component_environment(env, ctl)
        # OpenSuperficies hyperbook gates read frozen params from vision-engine history.
        child['VE_REPO'] = str(work['vision-engine'])
        cases = component_cases(work)
        if args.parallel:
            builders, lanes = component_lanes(cases)
            for case in builders:
                run_component(case, child)
            threads = [threading.Thread(target=guarded(
                'gate/lane-' + lane[0]['owner'],
                lambda lane=lane: [run_component(c, child) for c in lane], record)) for lane in lanes]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
        else:
            for case in cases:
                run_component(case, child)
        # TRIAGE inventories these conditional/task-dependent lanes without an
        # active task selection or live prerequisite. Never imply they ran.
        for name, reason in (
            ('wender-agent/live-cdp', 'CHROME_BIN and a dedicated live fixture are not provisioned'),
            ('vision-engine/live-chromium', 'optional live Chromium fixture is not provisioned'),
            ('OpenSuperficies/frontend-terminal', 'Playwright Chromium and a dedicated terminal fixture are not provisioned'),
            ('components/task-acceptance', 'active bridge task selection and reviewed acceptance commands require the operator')):
            record(run_case(name, ROOT, [], child, out, blocked=reason))

    work = {}
    steps = []
    if not args.components:
        steps.append(guarded('gate/root', root_lane, record))
    if args.components or args.leg is None:
        steps.append(guarded('gate/components', component_step, record))
    if args.parallel:
        threads = [threading.Thread(target=step) for step in steps]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
    else:
        for step in steps:
            step()
    evidence['component_tree_unchanged'] = not bool(git(ROOT, 'status', '--porcelain', '--', 'components'))
    evidence['final_revision'] = git(ROOT, 'rev-parse', 'HEAD')
    evidence['source_revision_unchanged'] = evidence['revision'] == evidence['final_revision']
    receipt = out / 'results.json'
    receipt.write_text(json.dumps(dict(evidence=evidence, results=rows), indent=2) + '\n')
    print('RECEIPT ' + str(receipt), flush=True)
    return 0 if rows and all(r['status'] == 'PASS' for r in rows) and evidence['component_tree_unchanged'] and evidence['source_revision_unchanged'] else 1


if __name__ == '__main__':
    sys.exit(main())
