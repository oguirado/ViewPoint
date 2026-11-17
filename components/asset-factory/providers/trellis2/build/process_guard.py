#!/usr/bin/env python3
"""Legacy supervisor entry point using the explicitly selected job backend."""
import json
import os
from pathlib import Path
import subprocess
import sys
try:
    from asset_factory import proc as af_proc
except ModuleNotFoundError:
    sys.path.insert(0, os.environ.get('AF_PROCESS_KERNEL_ROOT', str(Path(__file__).resolve().parents[3])))
    from asset_factory import proc as af_proc


def supervise(argv, timeout_s):
    job = None
    try:
        job = af_proc.popen(argv, start_new_session=True)
        returncode = job.wait(timeout_s)
        reason = job.result.reason
        budget = reason in ('job_oom_kill', 'job_resources', 'job_timeout')
        result = {'rc': 0 if returncode == 0 else 6 if budget else 5,
                  'reason': None if returncode == 0 else reason or 'job_payload_failed',
                  'returncode': returncode}
    except subprocess.TimeoutExpired:
        result = {'rc': 6, 'reason': 'deadline_exceeded', 'returncode': None}
    except af_proc.SignalRefusal as exc:
        result = {'rc': 3, 'reason': exc.code, 'returncode': None}
    finally:
        errors = af_proc.cleanup(job) if job is not None else []
    if errors:
        result['cleanup_errors'] = errors
        if result['rc'] == 0:
            result.update(rc=3, reason=errors[0])
    return result


def main():
    try:
        if len(sys.argv) < 4 or not 0 < int(sys.argv[1]) <= 300 or int(sys.argv[2]) < 0:
            raise ValueError
        timeout, output_fd = int(sys.argv[1]), int(sys.argv[2])
    except (ValueError, IndexError):
        print(json.dumps({'rc': 2, 'reason': 'invocation_invalid', 'returncode': None},
                         sort_keys=True, separators=(',', ':')))
        return 2
    result = supervise(sys.argv[3:], timeout)
    with os.fdopen(output_fd, 'w') as output:
        output.write(json.dumps(result, sort_keys=True, separators=(',', ':'))+'\n')
    return result['rc']

if __name__ == '__main__':
    raise SystemExit(main())
