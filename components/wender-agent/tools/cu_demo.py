# @module {"id":"wa.tools.cu_demo","layer":4,"owner":"ARCH","deps":["wa.learn.demonstrations","wa.learn.demo_replay","wa.learn.cu_loop","wa.perceive.srf_identity"],"book":"HB-WA-s4-learn"}
"""Bounded structural summary or explicit episode-private visual replay."""
import argparse
from dataclasses import replace
import time
import json
import os
from pathlib import Path
import sys
import stat

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from wender.learn import demonstrations as d
from wender.learn import demo_replay as r
from wender.perceive.srf_identity import SrfIdentitySurface
from wender.learn.cu_loop import BridgeDescriber


def _select(steps, value):
    if value is None:
        return steps
    try:
        words = value.split(',')
        d._require(all(word.isascii() and word.isdecimal() for word in words), 'SELECTION')
        ids = tuple(int(word) for word in words)
        d._require(ids and len(set(ids)) == len(ids), 'SELECTION')
        selected = tuple(s for s in steps if s.step_id in ids)
        d._require(tuple(s.step_id for s in selected) == ids, 'SELECTION')
        return selected
    except (TypeError, OverflowError):
        raise ValueError('SELECTION') from None


def _texts(path):
    if path is None:
        return {}
    p = Path(path).absolute()
    d._require(p == p.resolve(), 'FILE_CUSTODY')
    fd = d._directory(p.parent)
    try:
        obj = d._json(d._read(fd, p.name, 100_000))
    finally:
        os.close(fd)
    d._require(type(obj) is dict and len(obj) <= 128, 'TEXT_SLOT')
    d._require(all(type(k) is str and k.isascii() and k.isdecimal() and str(int(k)) == k
                   and type(v) is str for k, v in obj.items()), 'TEXT_SLOT')
    d._require(sum(len(d.validate_text_slot(v, expected_length=None, max_bytes=4096))
                   for v in obj.values()) <= 16_384, 'TEXT_BUDGET')
    return {int(k): v for k, v in obj.items()}


# @anchor:wa.tools.cu_demo.summarize
def summarize(steps):
    """Only allowlisted structural metadata; never repr() a descriptor."""
    return {'status': 'SUMMARY', 'steps': [
        {'step': s.step_id, 'kind': s.event.kind, 'target': s.target is not None,
         'small_dimensions': list(s.target.small.pixels.shape[:2][::-1]) if s.target else None,
         'context_dimensions': list(s.target.context.pixels.shape[:2][::-1]) if s.target else None,
         'expected_change': s.expected_change.missing_reason is None,
         'replayable': s.replayable, 'failure_code': s.refusal_code} for s in steps]}


def _ppm(value):
    d._require(value is None or (type(value) is int and -1_000_000 <= value <= 2_000_000), 'NONINTEGER_SCORE')
    return value


# @anchor:wa.tools.cu_demo.receipt_json
def receipt_json(result):
    rows = []
    for x in result.receipts:
        rows.append({'step': x.step_id, 'located_score_ppm': _ppm(x.located_score),
                     'margin_ppm': _ppm(x.margin), 'verify_score_ppm': _ppm(x.verify_score),
                     'elapsed_ms': None if x.elapsed_ns is None else x.elapsed_ns // 1_000_000,
                     'disposition': x.disposition, 'failure_code': x.failure_code,
                     'missing_reason': 'NOT_MEASURED' if any(v is None for v in
                         (x.located_score, x.margin, x.verify_score, x.elapsed_ns)) else None})
        d._require(len(json.dumps(rows[-1]).encode()) <= 4096, 'RECEIPT_BUDGET')
    return {'status': result.status, 'failed_step': result.failed_step,
            'input_uncertain': result.input_uncertain, 'steps': rows}


def _deadline(deadline):
    d._require(time.monotonic_ns() < deadline, 'DEADLINE')


def _atomic_export(path, payload, deadline):
    destination = Path(path).absolute()
    d._require(destination.parent == destination.parent.resolve(), 'FILE_CUSTODY')
    parent = d._directory(destination.parent)
    temporary = '.demo-' + os.urandom(16).hex()
    opened = False
    try:
        def check_target():
            try:
                target = os.stat(destination.name, dir_fd=parent, follow_symlinks=False)
                d._require(stat.S_ISREG(target.st_mode), 'FILE_CUSTODY')
            except FileNotFoundError:
                pass
        check_target()
        _deadline(deadline)
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
        opened = True
        try:
            offset = 0
            while offset < len(payload):
                _deadline(deadline)
                count = os.write(fd, payload[offset:])
                d._require(count > 0, 'EXPORT_WRITE')
                offset += count
            os.fsync(fd)
        finally:
            os.close(fd)
        _deadline(deadline)
        check_target()
        os.replace(temporary, destination.name, src_dir_fd=parent, dst_dir_fd=parent)
        opened = False
        os.fsync(parent)
    finally:
        if opened:
            os.unlink(temporary, dir_fd=parent)
        os.close(parent)


# @anchor:wa.tools.cu_demo._BudgetDescriber
class _BudgetDescriber:
    # @anchor:wa.tools.cu_demo._BudgetDescriber.__init__
    def __init__(self, describer, deadline):
        self.describer, self.deadline = describer, deadline

    # @anchor:wa.tools.cu_demo._BudgetDescriber.describe
    def describe(self, frame, *, prev=None):
        _deadline(self.deadline)
        self.describer.engine.timeout = (self.deadline - time.monotonic_ns()) / 1_000_000_000
        result = self.describer.describe(frame, prev=prev)
        _deadline(self.deadline)
        return result


def _key_policy(path, identity, window):
    p = Path(path).absolute()
    d._require(p == p.resolve(), 'FILE_CUSTODY')
    fd = d._directory(p.parent)
    try:
        value = d._json(d._read(fd, p.name, 100_000))
    finally:
        os.close(fd)
    d._require(type(value) is dict and value.keys() == {'identity', 'window', 'forbidden_keys'}, 'KEY_POLICY')
    policy = r.KeyPolicy(**value)
    d._require((policy.identity, policy.window) == (identity, window), 'KEY_POLICY')
    return policy


# @anchor:wa.tools.cu_demo.main
def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory')
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--summarize', action='store_true')
    mode.add_argument('--replay', action='store_true')
    mode.add_argument('--export-steps')
    for flag in ('demo-id', 'site-id', 'key-policy-file'):
        parser.add_argument('--' + flag)
    for flag in ('identity', 'container', 'window', 'identity-env', 'allow-steps', 'paste-text-file'):
        parser.add_argument('--' + flag)
    parser.add_argument('--at-recorded', help='comma-separated click step ids that target generated content: act at the recorded point')
    parser.add_argument('--final', choices=('strict', 'informative'), default='strict',
                        help='informative: keep a live demo whose final frame differs from the last after frame')
    args = parser.parse_args(argv)
    describer = None
    try:
        if args.export_steps:
            d._export_id(args.demo_id)
            d._export_id(args.site_id)
            d._require(args.paste_text_file is None and args.allow_steps is None
                       and args.key_policy_file is None and args.identity is None
                       and args.container is None and args.window is None and args.identity_env is None, 'SELECTION')
        else:
            d._require(args.demo_id is None and args.site_id is None, 'SELECTION')
        d._require(args.replay or args.key_policy_file is None, 'SELECTION')
        if args.replay:
            d._require(all((args.identity, args.container, args.window)), 'EXPLICIT_IDENTITY_REQUIRED')
        elif args.summarize:
            d._require(args.allow_steps is None and args.paste_text_file is None, 'SELECTION')
        if args.summarize:
            d._require(args.at_recorded is None, 'SELECTION')
        started = time.monotonic_ns()
        limits = d.DemoLimits()
        recording = d.load_demo(args.directory, limits=limits, final_check=args.final)
        remaining = limits.operation_ns - (time.monotonic_ns() - started)
        d._require(remaining > 0, 'DEADLINE')
        recording = replace(recording, limits=replace(limits, operation_ns=remaining))
        deadline = started + limits.operation_ns
        if args.export_steps:
            describer = BridgeDescriber(timeout=remaining / 1_000_000_000)
        steps = d.segment_demo(recording, describer=_BudgetDescriber(describer, deadline) if describer is not None else None)
        _deadline(deadline)
        if args.export_steps:
            selected = _select(steps, args.at_recorded) if args.at_recorded else ()
            at = frozenset(s.step_id for s in selected)
            projection = d.project_demo_steps(steps, demo_id=args.demo_id, site_id=args.site_id, at_recorded=at)
            _deadline(deadline)
            _atomic_export(args.export_steps, projection.payload, deadline)
            result = dict(status='EXPORTED', text_slots=projection.text_slots,
                          at_recorded=projection.at_recorded, omissions=projection.omissions)
        elif args.summarize:
            result = summarize(steps)
            result['final_matches'] = recording.final_matches
        else:
            steps = _select(steps, args.allow_steps)
            texts = _texts(args.paste_text_file)
            kwargs = {'identity': args.identity, 'container': args.container, 'window': args.window}
            if args.identity_env is not None:
                kwargs['identity_env'] = args.identity_env
            identity = SrfIdentitySurface(**kwargs)
            remaining = limits.operation_ns - (time.monotonic_ns() - started)
            d._require(remaining > 0, 'DEADLINE')
            policy = r.ReplayPolicy(limits=replace(limits, operation_ns=remaining))
            key_policy = _key_policy(args.key_policy_file, args.identity, args.window) if args.key_policy_file else None
            surface = r.DemoIdentitySurface(identity, selected_steps=steps, policy=policy, key_policy=key_policy, text_encoding='utf-8',
                                            paste_input=lambda text, timeout=None: identity._call('type', text=text, paste=True))
            at = frozenset(int(w) for w in args.at_recorded.split(',')) if args.at_recorded else frozenset()
            d._require(all(any(st.step_id == k and st.event.kind == 'click' for st in steps) for k in at), 'SELECTION')
            result = receipt_json(r.replay(surface, steps, texts=texts, at_recorded=at))
        print(json.dumps(result, sort_keys=True, separators=(',', ':'), allow_nan=False))
        return 0 if result['status'] in ('SUMMARY', 'EXPORTED', 'VISUAL_TRANSITIONS_VERIFIED') else 2
    except Exception as e:
        codes = r._CODES | {'EXPLICIT_IDENTITY_REQUIRED', 'RECEIPT_BUDGET', 'EVENT_BUDGET', 'RECORDING_ORDER',
                            'EXPORT_ID', 'EXPORT_INTEGER', 'EXPORT_BUDGET', 'EXPORT_WRITE', 'PNG_FORMAT', 'PNG_CRC', 'PNG_UNSUPPORTED', 'PNG_DECOMPRESSION', 'PNG_FILTER', 'FINAL_MISMATCH'}
        code = str(e) if type(e) is ValueError and str(e) in codes else 'DEMO_UNAVAILABLE'
        print(json.dumps({'status': 'REFUSED', 'failure_code': code}, separators=(',', ':')))
        return 2
    finally:
        if describer is not None and describer._marks_dir is not None:
            describer._marks_dir.cleanup()
            describer._marks_dir = None


if __name__ == '__main__':
    raise SystemExit(main())
