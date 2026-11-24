# SPDX-License-Identifier: AGPL-3.0-only
"""Run one source-only REA measurement fixture assignment."""

import argparse
import json
from pathlib import Path

from .harness import FACES, TRANSPORTS, run
from .seal import DEFAULT_MANIFEST, FixtureError


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('face', choices=('projection', *FACES))
    parser.add_argument('--case', default='nominal')
    parser.add_argument('--transport', choices=('all', *TRANSPORTS), default='all')
    parser.add_argument('--manifest', type=Path, default=DEFAULT_MANIFEST)
    options = parser.parse_args(argv)
    try:
        report = run(options.manifest, options.face, options.case,
                     TRANSPORTS if options.transport == 'all' else (options.transport,))
    except (FixtureError, OSError, ValueError, KeyError, IndexError, TypeError, RecursionError) as error:
        refusal = {'schema': 'rea.medicion.fixture_value/v1', 'scope': 'fixture',
                   'ok': False, 'value_id': None, 'code': 'INSUFFICIENT', 'detail': str(error)}
        if isinstance(error, FixtureError) and error.sample is not None:
            refusal['sample'] = error.sample
        print(json.dumps(refusal))
        return 16
    print(json.dumps(report, allow_nan=False, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
