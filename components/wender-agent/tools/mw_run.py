# @module {"id":"wa.tools.mw_run","layer":4,"owner":"ARCH","deps":["wa.tools.memoryweb_gate","wa.learn.mw_workflow","wa.learn.mw_owners_srf","wa.learn.mw_profiles"],"book":"HB-WA-s4-learn"}
"""Declarative validation and private named profiles with operator-created tokens.

Standalone provisioning requires a complete jointly admissible capacity vector,
verified producer contracts, held lease and independent authorization/disclosure
owners. The corrected default capacity vector passes aggregate admission. Named
profiles pin the binary acknowledgement token digest; initialization is inactive
until explicit operator activation: stage with tools/mw_profile_init.py, follow
the printed token-creation/chmod procedure, then use --site <site> --activate.
The standalone default refuses until the producer exposes a capabilities verb
and trusted handlers are registered (N1). Muse is required only when a guarded
workflow requests a T2 target, including fallback targets. The default
image cap is 3 MiB with a separate 8 MiB ceiling [TARGET]; aggregate admission
still applies. Missing semantic detectors and download-completion evidence refuse.
Notices contain metadata only. The output
parent must be user-owned and private (mode 0700 [TARGET]). Explicit injected
services remain supported; unsupported workflow contracts refuse admission.
"""
import argparse
import hashlib
from pathlib import Path
import sys

# Direct tool invocation has the same imports as python -m tools.mw_run.
if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools import memoryweb_gate as gate
from wender.learn.mw_profiles import ProfileError, load_profile, make_invocation, run_profile
from wender.learn.mw_workflow import RunOwners, run_workflow
from wender.learn.mw_owners_srf import LiveInvocation, open_srf_owners, build_live_ports


class _Parser(argparse.ArgumentParser):
    # @anchor:wa.tools.mw_run._Parser.error
    def error(self, message):
        # argparse diagnostics can echo private acknowledgement/argument values.
        raise ValueError('USAGE_SCHEMA') from None


# @anchor:wa.tools.mw_run.trusted_live_dependencies
def trusted_live_dependencies(invocation):
    """Installed trusted provisioning is AUSENTE; refuse without constructing owners.

    An embedding orchestrator may bind this seam to trusted explicit dependencies.
    Workflow JSON, argv and identity.env never supply authority objects.
    """
    raise ValueError('OUT_OF_SUPPORT') from None


# @anchor:wa.tools.mw_run.main
# @anchor:wa.s4.mw_run.main
def main(argv=None, *, owners=None, live_ports=None, profile_services=None, profile_dir=None) -> int:
    parser = _Parser(description=__doc__, add_help=False)
    parser.add_argument('workflow', type=Path)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--mode', choices=('validate', 'replay', 'shadow', 'promoted', 'repair'), required=True)
    for flag in ('identity', 'container', 'window', 'live-ack'):
        parser.add_argument('--' + flag)
    parser.add_argument('--identity-env', type=Path)
    parser.add_argument('--profile')
    try:
        args = parser.parse_args(argv)
    except (SystemExit, ValueError):
        print(gate.canon({'rc': 2, 'verdict_code': 'USAGE_SCHEMA'}))
        return 2
    try:
        if args.profile is not None and (
                any(v is not None for v in (args.identity, args.container, args.window, args.identity_env, args.live_ack))
                or owners is not None or live_ports is not None or args.mode == 'validate' or args.output is None):
            raise ProfileError(2)
        schema = (gate.DOCS / 'mw.v1.schema.json').read_bytes()
        if hashlib.sha256(schema).hexdigest() != gate.SCHEMA_PIN:
            return 3
        bundle = gate.strict_load(schema)
        if args.mode == 'validate':
            result = gate.validate_file(args.workflow, bundle, context=owners.validation_context if type(owners) is RunOwners else None)
            print(gate.canon({'rc': result.rc, 'category': result.category}))
            return result.rc
        if args.profile is not None:
            profile = load_profile(args.profile, profile_dir=profile_dir)
            invocation = make_invocation(profile, workflow=args.workflow, output=args.output, mode=args.mode)
            result = run_profile(profile, invocation, bundle=bundle, services=profile_services)
        elif type(owners) is RunOwners:
            if args.output is None:
                print(gate.canon({'rc': 5, 'verdict_code': 'OUT_OF_SUPPORT'}))
                return 5
            if args.mode != owners.mode or args.output.absolute() != Path(owners.output).absolute():
                print(gate.canon({'rc': 2, 'verdict_code': 'USAGE_SCHEMA'}))
                return 2
            result = run_workflow(args.workflow, bundle=bundle, owners=owners, limits=owners.limits)
        else:
            # Preserve the old unbound programmatic interface. Once factory
            # composition is requested, its explicit argument contract applies.
            if args.mode not in ('replay', 'shadow') or (live_ports is None and not any(
                    (args.identity, args.container, args.window, args.identity_env, args.live_ack))):
                print(gate.canon({'rc': 5, 'verdict_code': 'OUT_OF_SUPPORT'}))
                return 5
            if args.output is None or not all(type(v) is str and v.strip() and not v.startswith('-')
                    for v in (args.identity, args.container, args.window, args.live_ack)) or args.identity_env is None:
                print(gate.canon({'rc': 2, 'verdict_code': 'USAGE_SCHEMA'}))
                return 2
            invocation = LiveInvocation(workflow=args.workflow, output=args.output, mode=args.mode,
                                        identity=args.identity, container=args.container, window=args.window,
                                        identity_env=args.identity_env, live_ack=args.live_ack)
            if live_ports is None:
                live_ports = build_live_ports(invocation, **trusted_live_dependencies(invocation))
            with open_srf_owners(invocation, ports=live_ports) as built_owners:
                result = run_workflow(args.workflow, bundle=bundle, owners=built_owners, limits=built_owners.limits)
        print(gate.canon({'rc': result.rc, 'verdict_code': result.verdict_code}))
        return result.rc
    except ProfileError as exc:
        print(gate.canon({'rc': exc.rc, 'verdict_code': exc.category}))
        return exc.rc
    except (Exception, MemoryError):
        print(gate.canon({'rc': 5, 'verdict_code': 'OUT_OF_SUPPORT'}))
        return 5


if __name__ == '__main__':
    sys.exit(main())
