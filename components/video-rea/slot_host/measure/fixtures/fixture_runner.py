#!/usr/bin/python3 -I
# SPDX-License-Identifier: AGPL-3.0-only
"""Disposable REA input-tape fixture. The only output is one result on stdout."""

import hashlib
import json
import sys


def main():
    operation, request, tape = sys.argv[1:]
    rows = json.loads(tape)
    if operation == 'replay':
        lineage = json.load(sys.stdin)
        if lineage['input_tape_sha256'] != hashlib.sha256(tape.encode()).hexdigest():
            return 2
    elif operation != 'record':
        return 2
    result = json.dumps(
        [{'occurrence': row['occurrence'], 'value': row['value'] * 2} for row in rows],
        separators=(',', ':'), sort_keys=True,
    )
    print(json.dumps({'schema': 'rea.slot_result/v1', 'status': 'OK', 'rc': 0,
                      'request_id': request, 'role': 'replay',
                      'sha256': hashlib.sha256(result.encode()).hexdigest(),
                      'result_payload': result}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
