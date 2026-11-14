"""Kill installer guard mutants in disposable trees. Invoke only under safe_run."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def replace(path, old, new):
    text = path.read_text()
    if text.count(old) != 1:
        raise AssertionError('mutation anchor is not unique: ' + old)
    path.write_text(text.replace(old, new))


def main():
    if os.environ.get('NEWTON_TEST_PIDNS') != '1':
        raise SystemExit('requires campaign g1/safe_run.sh')
    killed = 0
    mutants = [('variable_gate', 'test_variable_guard'),
               ('inside_checkout', 'test_inside_checkout_guard'),
               ('orphan_scope', 'test_orphan_scope'),
               ('legacy_untouched', 'test_legacy_untouched')]
    for name, test in mutants:
        with tempfile.TemporaryDirectory(prefix='vision-mutant-') as tmp:
            repo = Path(tmp) / 'repo'
            repo.mkdir()
            for p in ('vis', 'vis.json', 'install.sh', 'skill_copies.py'):
                shutil.copy2(ROOT / p, repo / p)
            for p in ('skill', 'dist'):
                shutil.copytree(ROOT / p, repo / p)
            shell, module = repo / 'install.sh', repo / 'skill_copies.py'
            if name == 'variable_gate':
                replace(shell, 'if [ "$VISION_INSTALL_MODE" = --apply ]', 'if false && [ "$VISION_INSTALL_MODE" = --apply ]')
                replace(module, "if mode == '--apply' and os.environ", "if False and mode == '--apply' and os.environ")
            elif name == 'inside_checkout':
                replace(module, 'if home and dest.resolve().is_relative_to(ROOT):',
                        'if False and home and dest.resolve().is_relative_to(ROOT):')
            elif name == 'orphan_scope':
                replace(module, '    return operations\n',
                        "    return operations + [('DELETE', dest.parent / 'outside', None)] if (dest.parent / 'outside').is_file() else operations\n")
            else:
                replace(module, 'for dest in destinations for op in plan(dest, files)',
                        "for dest in destinations + [p.parent / 'vis' for p in destinations] for op in plan(dest, files)")
            r = subprocess.run([sys.executable, '-I', str(ROOT / 'tests/test_vision.py'),
                                'InstallerTest.' + test, '-v'], capture_output=True, text=True,
                               env=dict(os.environ, VISION_TEST_SOURCE=str(repo)), timeout=90)
            if r.returncode != 1 or 'FAIL:' not in r.stderr or 'ERROR:' in r.stderr:
                print(name + ': SURVIVED or invalid failure\n' + r.stdout + r.stderr)
                return 1
            killed += 1
            print(name + ': KILLED by ' + test + ' [MEASURED]')
            # Print the assertion evidence, never a complete subprocess dump.
            print(next(line for line in r.stderr.splitlines() if line.startswith('AssertionError:'))[:500])
    print(str(killed) + '/' + str(len(mutants)) + ' installer mutants killed [MEASURED]')
    return 0


if __name__ == '__main__':
    sys.exit(main())
