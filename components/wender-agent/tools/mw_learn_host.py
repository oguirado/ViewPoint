# @module {"id":"wa.tools.mw_learn_host","layer":4,"owner":"LEARN","deps":["wa.learn.mw_learn_host","wa.learn.mw_learn","wa.tools.memoryweb_gate"],"book":"HB-WA-s4-learn"}
"""Private offline draft authoring from an operator-reviewed host provision."""
import argparse
from pathlib import Path
import sys

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools import memoryweb_gate as gate
from wender.learn.mw_learn_host import run_learn_host


# @anchor:wa.s4.tools.mw_learn_host._Parser
class _Parser(argparse.ArgumentParser):
    # @anchor:wa.s4.tools.mw_learn_host._Parser.error
    def error(self, message):
        raise ValueError('USAGE_SCHEMA')


# @anchor:wa.s4.tools.mw_learn_host.main
def main(argv=None):
    parser = _Parser(add_help=False)
    for name in ('profile', 'demo', 'explanation', 'workflow-id'):
        parser.add_argument('--' + name, required=True)
    for name in ('profile-dir', 'configuration'):
        parser.add_argument('--' + name, type=Path)
    try:
        args = parser.parse_args(argv)
        result = run_learn_host(profile_name=args.profile, demo_directory=Path(args.demo),
            explanation=Path(args.explanation), workflow_id=args.workflow_id, profile_dir=args.profile_dir,
            configuration_path=args.configuration)
        print(gate.canon(dict(rc=result.rc, category=result.category, references=list(result.references),
                             model_calls=result.model_calls, usage=result.usage)))
        return result.rc
    except (ValueError, SystemExit):
        print(gate.canon(dict(rc=2, category='USAGE_SCHEMA', references=[], model_calls=0, usage=None)))
        return 2


if __name__ == '__main__':
    sys.exit(main())
