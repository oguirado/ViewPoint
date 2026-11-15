# @module {"id":"wa.tools.mw_learn","layer":4,"owner":"ARCH","deps":["wa.learn.mw_learn"],"book":"HB-WA-s4-learn"}
"""Offline draft authoring with trusted programmatic owner provisioning."""
import argparse
from dataclasses import asdict
from pathlib import Path
import sys

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools import memoryweb_gate as gate
from wender.learn.mw_learn import LearnInputs, LearnPolicy, LearnOwners, AuthorBackend, learn
from wender.learn.demonstrations import DemoLimits


class _Parser(argparse.ArgumentParser):
    # @anchor:wa.tools.mw_learn._Parser.error
    def error(self, message):
        raise ValueError('USAGE_SCHEMA')


# @anchor:wa.tools.mw_learn.main
def main(argv=None, *, owners=None, backend=None, describer=None):
    parser = _Parser(add_help=False)
    for name in ('demo-directory', 'explanation', 'demo-manifest', 'site-metadata', 'guide', 'schema', 'output-root'):
        parser.add_argument('--' + name, type=Path, required=True)
    for name in ('site-id', 'workflow-id'):
        parser.add_argument('--' + name, required=True)
    parser.add_argument('--router-root', type=Path)
    parser.add_argument('--router-query')
    parser.add_argument('--ve-root', type=Path)
    parser.add_argument('--effort', choices=('medium', 'high', 'xhigh'), default='medium')
    # Every resource ceiling is explicit; shell parsing supplies no trusted owners.
    for name in ('request-bytes', 'response-bytes', 'transport-bytes', 'memory-bytes', 'images',
                 'ocr-calls', 'router-calls', 'artifacts', 'deadline-ns', 'poll-ns', 'drain-ns'):
        parser.add_argument('--' + name, type=int, required=True)
    try:
        args = vars(parser.parse_args(argv))
        if type(owners) is not LearnOwners:
            print(gate.canon({'rc': 2, 'category': 'OWNERS_MISSING', 'references': [], 'model_calls': 0}))
            return 2
        policy_fields = set(LearnPolicy.__dataclass_fields__) - {'demo_limits'}
        policy = LearnPolicy(**{k: args[k] for k in policy_fields}, demo_limits=DemoLimits())
        inputs = LearnInputs(**{k: args[k] for k in LearnInputs.__dataclass_fields__ if k != 'guidance_refs'})
        result = learn(inputs, policy=policy, owners=owners, backend=backend or AuthorBackend(effort=policy.effort), describer=describer)
        payload = {**asdict(result), 'references': list(result.references)}
        print(gate.canon(payload))
        return result.rc
    except (ValueError, SystemExit, TypeError):
        print(gate.canon({'rc': 2, 'category': 'USAGE_SCHEMA', 'references': [], 'model_calls': 0}))
        return 2
    except Exception:
        print(gate.canon({'rc': 5, 'category': 'PIPELINE_REFUSED', 'references': [], 'model_calls': 0}))
        return 5


if __name__ == '__main__':
    sys.exit(main())
