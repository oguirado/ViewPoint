"""Data-only service-manager double. Never creates or signals a process."""
from pathlib import Path
from types import SimpleNamespace

class FakeRunner:
    def __init__(self, *, available=True, auto_exit=None, output=b'', error=b''):
        self.available, self.auto_exit = available, auto_exit
        self.output, self.error = output, error
        self.calls, self.units = [], {}
        self.show_hook = self.stop_hook = None
        self.fail_start = self.fail_stop = False

    def run(self, argv, *, timeout):
        self.calls.append(tuple(argv))
        def reply(rc=0, out='', err=''):
            return SimpleNamespace(returncode=rc, stdout=out, stderr=err)
        if not self.available:
            return reply(1, err='unreachable')
        if argv[0] == 'systemd-run':
            if self.fail_start:
                return reply(1, err='refused')
            unit = next(a.split('=', 1)[1] for a in argv if a.startswith('--unit='))
            if unit in self.units:
                return reply(1, err='exists')
            props = dict(a[len('--property='):].split('=', 1)
                         for a in argv if a.startswith('--property='))
            self.units[unit] = dict(LoadState='loaded', ActiveState='active',
                SubState='running', Result='success', ExecMainCode='0',
                ExecMainStatus='0', MemoryPeak='4096')
            Path(props['StandardOutput'].split(':', 1)[1]).write_bytes(self.output)
            if props['StandardError'] != 'inherit':
                Path(props['StandardError'].split(':', 1)[1]).write_bytes(self.error)
            if self.auto_exit is not None:
                self.finish(unit, self.auto_exit)
            return reply()
        action = argv[2]
        if '--' not in argv:
            return reply(out='255\n')
        unit = argv[-1]
        if action == 'show':
            if self.show_hook:
                self.show_hook(unit)
            state = self.units.get(unit, {'LoadState': 'not-found'})
            return reply(out=''.join(f'{k}={v}\n' for k, v in state.items()))
        if self.fail_stop:
            return reply(1, err='refused')
        if action == 'kill':
            value = next(a.split('=', 1)[1] for a in argv if a.startswith('--signal='))
            self.finish(unit, -int(value))
        elif action == 'stop':
            if self.stop_hook:
                self.stop_hook(unit)
            self.units.pop(unit, None)
        elif action == 'reset-failed':
            self.units.pop(unit, None)
        return reply()

    def finish(self, job, returncode=0):
        unit = job if isinstance(job, str) else job.unit_name
        self.units[unit].update(ActiveState='active' if returncode == 0 else 'failed',
            SubState='exited' if returncode == 0 else 'failed',
            Result='success' if returncode == 0 else 'exit-code',
            ExecMainCode='1' if returncode >= 0 else '2',
            ExecMainStatus=str(abs(returncode)))
