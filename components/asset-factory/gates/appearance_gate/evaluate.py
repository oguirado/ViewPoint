"""Consume bound independent appearance reviews; never dispatch a reviewer.

Run from the component root with -m gates.appearance_gate.evaluate. Isolated
Python callers may execute this file directly: its bootstrap adds only the
component root. RGB24 evidence is decoded row-major RGB, with no padding.
The caller owns evidence provenance; digests establish identity, not honesty.
"""
import argparse
import contextlib
import hashlib
import json
import os
from pathlib import Path
import stat
import signal
import sys
import time

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from asset_factory import core

SCORES = ('form_and_proportions', 'face_appeal', 'materials_and_lighting',
          'detail_and_craft', 'publishable')
ASSET_TYPES = ('character', 'prop', 'set', 'vehicle_or_machine')
MAX_JSON_BYTES = 2 ** 20
MAX_EVIDENCE_BYTES = 64 * 2 ** 20
MAX_TOTAL_BYTES = 192 * 2 ** 20
MAX_TIMEOUT_MS = 300000


class Refusal(Exception):
    def __init__(self, rc, reason):
        self.rc, self.reason = rc, reason


def _require(condition, rc, reason):
    if not condition:
        raise Refusal(rc, reason)


def _object(value, required, optional=()):
    _require(type(value) is dict, 2, 'object_required')
    _require(not (set(required) - value.keys()), 3, 'missing_evidence')
    _require(not (value.keys() - set(required) - set(optional)), 2, 'unknown_property')


def _text(value):
    _require(type(value) is str and bool(value.strip()), 2, 'text_required')


def _hash(value):
    _require(type(value) is str and len(value) == 64 and
             all(c in '0123456789abcdef' for c in value), 2, 'digest_invalid')


def _identity(value):
    _object(value, ('id', 'family', 'revision'))
    for item in value.values():
        _text(item)
    _require(value['revision'].lower() not in ('main', 'master', 'latest', 'head'), 2, 'unpinned_revision')


def _profile(value):
    _object(value, ('id', 'sha256'))
    _text(value['id'])
    _hash(value['sha256'])


def _policy(policy):
    _object(policy, ('floor', 'target', 'likeness_floor', 'likeness_target', 'timeout_ms'))
    _require(all(type(v) is int for v in policy.values()), 2, 'policy_integer_required')
    _require(0 <= policy['floor'] <= policy['target'] <= 10 and
             0 <= policy['likeness_floor'] <= policy['likeness_target'] <= 10 and
             0 < policy['timeout_ms'] <= MAX_TIMEOUT_MS, 2, 'policy_invalid')


def _score(value):
    _require(type(value) is int, 2, 'score_integer_required')
    _require(0 <= value <= 10, 2, 'score_out_of_range')


def _utf8_tree(value):
    """Reject lone surrogates before retaining any input in diagnostics."""
    if type(value) is str:
        value.encode('utf-8', errors='strict')
    elif type(value) is dict:
        for key, item in value.items():
            _utf8_tree(key)
            _utf8_tree(item)
    elif type(value) in (list, tuple):
        for item in value:
            _utf8_tree(item)


def _schema(value, schema):
    """Validate the complete local schema subset, independently of the stub."""
    _require(not (schema.keys() - {'$id', '$schema', 'type', 'oneOf', 'const', 'enum',
                                  'required', 'properties', 'additionalProperties', 'items'}),
             2, 'unsupported_schema_keyword')
    types = {'object': dict, 'array': list, 'string': str, 'integer': int,
             'boolean': bool, 'null': type(None)}
    if 'oneOf' in schema:
        matches = 0
        for branch in schema['oneOf']:
            try:
                _schema(value, branch)
                matches += 1
            except Refusal:
                pass
        _require(matches == 1, 2, 'schema_mismatch')
    if 'type' in schema:
        _require(type(value) is types[schema['type']], 2, 'schema_mismatch')
    if 'const' in schema:
        _require(type(value) is type(schema['const']) and value == schema['const'], 2, 'schema_mismatch')
    if 'enum' in schema:
        _require(any(type(value) is type(item) and value == item for item in schema['enum']),
                 2, 'schema_mismatch')
    if type(value) is dict:
        _require(not (set(schema.get('required', ())) - value.keys()), 3, 'missing_evidence')
        properties = schema.get('properties', {})
        if schema.get('additionalProperties') is False:
            _require(not (value.keys() - properties.keys()), 2, 'unknown_property')
        for key in value.keys() & properties.keys():
            _schema(value[key], properties[key])
    elif type(value) is list and 'items' in schema:
        for item in value:
            _schema(item, schema['items'])


def _validate_local(value, name, field=None):
    path = Path(__file__).with_name(name + '.schema.json')
    schema = core.load_json(path, max_bytes=MAX_JSON_BYTES)
    _schema(value, schema if field is None else schema['properties'][field])


def _licence_bindings(request):
    context = request.get('licence_context')
    _require(context is not None, 3, 'licence_context_missing')
    _object(context, ('provider_manifest', 'configuration', 'territory_policy', 'operation'))
    decision = request['licence_decision']
    for field, pin in (('provider_manifest', 'provider_sha256'),
                       ('configuration', 'configuration_sha256'),
                       ('territory_policy', 'policy_sha256')):
        _require(type(context[field]) is dict, 2, 'licence_context_invalid')
        _require(core.sha256_json(context[field]) == decision[pin], 4, 'licence_context_stale')
    provider, config = context['provider_manifest'], context['configuration']
    operation = context['operation']
    _require(operation in core.SLOTS, 2, 'licence_context_invalid')
    _require(type(config.get('slots')) is dict and
             type(config['slots'].get(operation)) is dict, 2, 'licence_context_invalid')
    _require(provider.get('id') == request['provider']['id'] and
             provider.get('revision') == request['provider']['revision'] and
             provider.get('slot') == operation and decision['operation'] == operation,
             4, 'licence_context_stale')
    _require(config.get('jurisdiction') == decision['jurisdiction'] and
             config.get('commercial_use') is decision['commercial_use'] and
             config['slots'][operation].get('provider') == provider['id'],
             4, 'licence_context_stale')


class Evidence:
    """Descriptor-relative reads reject symlinks, FIFOs and path escapes."""
    def __init__(self, root, deadline):
        _text(root)
        _require(Path(root).is_absolute(), 2, 'root_not_absolute')
        self.root, self.deadline, self.total = Path(root), deadline, 0

    def tick(self):
        _require(time.monotonic_ns() <= self.deadline, 6, 'deadline_exceeded')

    def read(self, binding, limit=MAX_EVIDENCE_BYTES):
        _object(binding, ('path', 'sha256'))
        _text(binding['path'])
        _hash(binding['sha256'])
        rel = Path(binding['path'])
        _require(not rel.is_absolute() and all(p not in ('..', '.') for p in rel.parts)
                 and bool(rel.parts), 2, 'path_escape')
        self.tick()
        root_fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        fd = root_fd
        try:
            for part in rel.parts[:-1]:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                if fd != root_fd:
                    os.close(fd)
                fd = child
            file_fd = os.open(rel.parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
            with os.fdopen(file_fd, 'rb') as stream:
                _require(stat.S_ISREG(os.fstat(stream.fileno()).st_mode), 2, 'not_regular_file')
                data = stream.read(limit + 1)
        finally:
            if fd != root_fd:
                os.close(fd)
            os.close(root_fd)
        self.total += len(data)
        self.tick()
        _require(len(data) <= limit and self.total <= MAX_TOTAL_BYTES, 6, 'evidence_budget_exceeded')
        _require(hashlib.sha256(data).hexdigest() == binding['sha256'], 4, 'evidence_stale')
        return data


def _json_bytes(data):
    def pairs(items):
        obj = {}
        for key, value in items:
            _require(key not in obj, 2, 'duplicate_key')
            obj[key] = value
        return obj

    def forbidden(_):
        raise Refusal(2, 'non_integer_number')

    return json.loads(data, object_pairs_hook=pairs, parse_float=forbidden, parse_constant=forbidden)


def _rgb(evidence, frame):
    _object(frame, ('path', 'sha256', 'width', 'height'))
    _require(type(frame['width']) is int and type(frame['height']) is int and
             frame['width'] > 0 and frame['height'] > 0, 2, 'rgb_dimensions_invalid')
    size = frame['width'] * frame['height'] * 3
    _require(size <= MAX_EVIDENCE_BYTES, 6, 'rgb_budget_exceeded')
    data = evidence.read({k: frame[k] for k in ('path', 'sha256')})
    _require(len(data) == size, 2, 'rgb_size_invalid')
    return data


def _verify_render(evidence, request, bindings):
    manifest = _json_bytes(evidence.read(request['render_manifest'], MAX_JSON_BYTES))
    _object(manifest, ('schema', 'asset_sha256', 'reference_sha256', 'profile',
                       'render_scope', 'helpers_excluded', 'frames'))
    _require(manifest['schema'] == 'af.appearance-render/v1', 2, 'unsupported_render_family')
    _require(manifest['asset_sha256'] == bindings['asset_sha256'] and
             manifest['reference_sha256'] == bindings['reference_sha256'] and
             manifest['profile'] == request['profile'], 4, 'render_binding_stale')
    _require(manifest['render_scope'] == 'visual_only' and
             manifest['helpers_excluded'] == ['_col', '-occ'], 3, 'visual_only_unproven')
    _object(manifest['frames'], ('reference', 'render', 'comparison'))
    frames = manifest['frames']
    pixels = {name: _rgb(evidence, frame) for name, frame in frames.items()}
    ref, render, sheet = (frames[key] for key in ('reference', 'render', 'comparison'))
    _require(ref['height'] == render['height'] == sheet['height'] and
             sheet['width'] == ref['width'] + render['width'], 2, 'comparison_dimensions_invalid')
    left, right = ref['width'] * 3, render['width'] * 3
    expected = b''.join(pixels['reference'][y*left:(y+1)*left] +
                        pixels['render'][y*right:(y+1)*right] for y in range(ref['height']))
    _require(pixels['comparison'] == expected, 4, 'reference_first_comparison_stale')
    _validate_local(manifest, 'appearance-render')
    bindings.update({name + '_rgb_sha256': frame['sha256'] for name, frame in frames.items()})


def _upstream(upstream, request):
    _object(upstream, ('schema', 'verdict', 'rc', 'reason_codes', 'source_sha256',
                       'profile', 'receipt_file_sha256', 'trace_sha256', 'failed_checks'))
    _require(upstream['schema'] == 'af.bridge-verdict/v1', 2, 'unsupported_upstream_family')
    _require(type(upstream['rc']) is int and upstream['rc'] in core.RC, 2, 'upstream_rc_invalid')
    _require(upstream['verdict'] == core.VERDICT[upstream['rc']], 2, 'upstream_verdict_invalid')
    _require(type(upstream['reason_codes']) is list and type(upstream['failed_checks']) is list,
             2, 'upstream_invalid')
    _require(core.sha256_json(upstream) == request['upstream_sha256'], 4, 'upstream_stale')
    if upstream['rc'] != 0:
        raise Refusal(upstream['rc'], 'upstream_' + upstream['verdict'].lower())
    _require(upstream['source_sha256'] == request['asset']['sha256'] and
             upstream['profile'] == request['profile'], 4, 'profile_or_source_stale')
    for key in ('receipt_file_sha256', 'trace_sha256'):
        _require(upstream[key] is not None, 3, 'upstream_receipt_missing')
        _hash(upstream[key])
    _require(not upstream['failed_checks'], 2, 'upstream_pass_with_failures')


def _licence(decision, provider):
    _object(decision, ('schema', 'rc', 'verdict', 'allowed', 'provider_id', 'operation',
                       'jurisdiction', 'commercial_use', 'monetisable', 'reason_codes',
                       'evaluated_dependencies', 'configuration_sha256', 'provider_sha256', 'policy_sha256'))
    _require(decision['schema'] == 'af.licence-decision/v1' and
             type(decision['rc']) is int and decision['rc'] in core.RC and
             decision['verdict'] == core.VERDICT[decision['rc']] and
             type(decision['allowed']) is bool and
             type(decision['commercial_use']) is bool and
             type(decision['monetisable']) is bool and
             type(decision['reason_codes']) is list and
             type(decision['evaluated_dependencies']) is list and
             decision['operation'] in core.SLOTS, 2, 'licence_decision_invalid')
    _text(decision['jurisdiction'])
    _validate_local(decision, 'appearance-request', 'licence_decision')
    _require(decision['provider_id'] == provider['id'], 4, 'licence_provider_stale')
    _require(decision['allowed'] and decision['rc'] == 0, 5, 'licence_refused')
    _require('hunyuan' not in provider['id'].lower(), 5, 'provider_blocked')
    for key in ('configuration_sha256', 'provider_sha256', 'policy_sha256'):
        _hash(decision[key])


def _evaluate(request, upstream, review, result):
    _object(request, ('schema', 'case_id', 'asset_type', 'evidence_root', 'asset', 'reference',
                      'render_manifest', 'policy', 'policy_sha256', 'profile', 'upstream_sha256',
                      'builder', 'provider', 'licence_decision', 'licence_decision_sha256'), ('licence_context',))
    _require(request['schema'] == 'af.appearance-request/v1', 2, 'unsupported_request_family')
    _text(request['case_id'])
    _require(request['asset_type'] in ASSET_TYPES, 2, 'asset_type_invalid')
    _policy(request['policy'])
    deadline = time.monotonic_ns() + request['policy']['timeout_ms'] * 1000000
    _require(core.sha256_json(request['policy']) == request['policy_sha256'], 4, 'policy_stale')
    _profile(request['profile'])
    _identity(request['builder'])
    _object(request['provider'], ('id', 'revision'))
    _identity({**request['provider'], 'family': 'provider'})
    _require(core.sha256_json(request['licence_decision']) == request['licence_decision_sha256'],
             4, 'licence_decision_stale')
    _licence(request['licence_decision'], request['provider'])
    _licence_bindings(request)
    _validate_local(request, 'appearance-request')
    _require(upstream is not None, 3, 'upstream_missing')
    _upstream(upstream, request)
    evidence = Evidence(request['evidence_root'], deadline)
    bindings = result['bindings']
    for name in ('asset', 'reference'):
        evidence.read(request[name])
        bindings[name + '_sha256'] = request[name]['sha256']
    bindings['render_manifest_sha256'] = request['render_manifest']['sha256']
    _verify_render(evidence, request, bindings)
    _require(review is not None, 3, 'review_missing')
    _object(review, ('schema', 'case_id', 'asset_type', 'request_sha256', 'upstream_sha256',
                     'policy_sha256', 'profile', 'bindings', 'reviewer', 'scores',
                     'likeness_to_reference', 'worst_three_problems', 'fix_advice'), ('min_score', 'verdict'))
    _require(review['schema'] == 'af.appearance-review/v1', 2, 'unsupported_review_family')
    _require(review['case_id'] == request['case_id'] and review['asset_type'] == request['asset_type'] and
             review['request_sha256'] == core.sha256_json(request) and
             review['upstream_sha256'] == request['upstream_sha256'] and
             review['policy_sha256'] == request['policy_sha256'] and
             review['profile'] == request['profile'] and review['bindings'] == bindings,
             4, 'review_binding_stale')
    _identity(review['reviewer'])
    _require(review['reviewer']['id'] != request['builder']['id'] and
             review['reviewer']['family'] != request['builder']['family'], 2, 'reviewer_not_independent')
    result['reviewer'] = review['reviewer']
    _require(review.get('verdict') not in ('SKIP', 'UNCLEAR', 'CANNOT_SEE', 'ADVISORY'), 3, 'review_incomplete')
    _require(review.get('verdict', 'PASS') in ('PASS', 'FAIL'), 2, 'review_verdict_invalid')
    _object(review['scores'], SCORES)
    scores = review['scores']
    values = []
    for key in SCORES:
        value = scores[key]
        if key == 'face_appeal' and request['asset_type'] != 'character' and value is None:
            continue
        _require(value is not None, 3, 'applicable_score_missing')
        _score(value)
        values.append(value)
    _score(review['likeness_to_reference'])
    _require(type(review['worst_three_problems']) is list and len(review['worst_three_problems']) <= 3 and
             all(type(v) is str for v in review['worst_three_problems']) and
             type(review['fix_advice']) is str, 2, 'review_advice_invalid')
    _validate_local(review, 'appearance-review')
    floor, target = request['policy']['floor'], request['policy']['target']
    likeness = review['likeness_to_reference']
    minimum = min(values)
    band = 'GOOD' if minimum >= target else 'ACCEPTABLE' if minimum >= floor else 'FAIL'
    result.update(scores=scores.copy(), min_score=minimum, quality_band=band,
                  likeness_to_reference=likeness)
    evidence.tick()
    _require(likeness >= request['policy']['likeness_floor'], 5, 'likeness_below_floor')
    _require(minimum >= floor, 5, 'craft_below_floor')
    _require(likeness >= request['policy']['likeness_target'], 5, 'likeness_below_target')
    _require(minimum >= target, 5, 'publication_target_not_met')
    _require(review.get('verdict') != 'FAIL', 5, 'reviewer_veto')


def evaluate(request, upstream, review):
    """Return a verdict; missing evidence never becomes an observed failure."""
    result = {'schema': 'af.appearance-result/v1', 'case_id': None, 'verdict': 'ERROR', 'rc': 2,
              'reason_codes': [], 'quality_band': None, 'scores': {}, 'min_score': None,
              'likeness_to_reference': None, 'policy_sha256': None, 'bindings': {},
              'reviewer': None, 'upstream_identity': None}
    try:
        _utf8_tree((request, upstream, review))
        if type(request) is dict:
            result['case_id'] = request.get('case_id') if type(request.get('case_id')) is str else None
            pin = request.get('policy_sha256')
            result['policy_sha256'] = pin if type(pin) is str else None
        if type(upstream) is dict:
            result['upstream_identity'] = {'sha256': core.sha256_json(upstream),
                                           'rc': upstream.get('rc') if type(upstream.get('rc')) is int else None,
                                           'verdict': upstream.get('verdict') if type(upstream.get('verdict')) is str else None}
        _evaluate(request, upstream, review, result)
        rc, reasons = 0, []
    except Refusal as exc:
        rc, reasons = exc.rc, [exc.reason]
    except FileNotFoundError:
        rc, reasons = 3, ['evidence_missing']
    except (OSError, ValueError, TypeError, KeyError, RecursionError, core.ContractError):
        rc, reasons = 2, ['malformed_or_io']
    result.update(rc=rc, verdict=core.VERDICT[rc], reason_codes=reasons)
    return result


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise Refusal(2, 'usage')


def main(argv=None):
    parser = Parser(description=__doc__)
    for name in ('request', 'upstream', 'review', 'out'):
        parser.add_argument('--' + name, required=name in ('request', 'out'))
    with _cli_deadline() as remaining:
        return _main(parser, argv, remaining)


@contextlib.contextmanager
def _cli_deadline():
    """Linux CLI wall deadline covers intake, evaluation and publication."""
    def expired(signum, frame):
        raise Refusal(6, 'deadline_exceeded')

    previous = signal.signal(signal.SIGALRM, expired)
    timer = signal.setitimer(signal.ITIMER_REAL, MAX_TIMEOUT_MS / 1000)
    started = time.monotonic()
    try:
        yield lambda timeout: signal.setitimer(
            signal.ITIMER_REAL, max(0.000001, timeout / 1000 - (time.monotonic() - started)))
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)
        if timer[0] > 0:
            signal.setitimer(signal.ITIMER_REAL, max(0.000001, timer[0] - (time.monotonic() - started)), timer[1])


def _load_input(path):
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    try:
        _require(stat.S_ISREG(os.fstat(fd).st_mode), 2, 'not_regular_file')
        # Load the already checked descriptor, not a second pathname lookup.
        return core.load_json(Path('/proc/self/fd') / str(fd), max_bytes=MAX_JSON_BYTES)
    finally:
        os.close(fd)


def _emit_safe(result):
    try:
        _utf8_tree(result)
        return core.emit(result)
    except (UnicodeError, ValueError, TypeError, RecursionError, core.ContractError):
        return core.emit({'rc': 2, 'status': 'ERROR', 'reason_codes': ['malformed_or_io']})


def _main(parser, argv, remaining):
    try:
        args = parser.parse_args(argv)
        request = _load_input(args.request)
        _utf8_tree(request)
        if type(request) is dict and type(request.get('policy')) is dict:
            _policy(request['policy'])
            remaining(request['policy']['timeout_ms'])
        upstream = _load_input(args.upstream) if args.upstream else None
        review = _load_input(args.review) if args.review else None
        result = evaluate(request, upstream, review)
        if result['rc'] == 0:
            core.atomic_write_json(Path(args.out), result)
    except Refusal as exc:
        return core.emit({'rc': exc.rc, 'status': core.STATUS[exc.rc], 'reason_codes': [exc.reason]})
    except FileNotFoundError:
        return core.emit({'rc': 3, 'status': 'INCOMPLETE', 'reason_codes': ['evidence_missing']})
    except (OSError, ValueError, TypeError, RecursionError, core.ContractError):
        return core.emit({'rc': 2, 'status': 'ERROR', 'reason_codes': ['malformed_or_io']})
    return _emit_safe(result)


if __name__ == '__main__':
    raise SystemExit(main())
