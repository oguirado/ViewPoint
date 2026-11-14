# @module {"id":"wa.tests.authoring_guide","layer":0,"owner":"ARCH","deps":["wa.tools.memoryweb_gate"],"book":"HB-WA-a0-arch"}
"""Bounded documentation and public gate controls; no live runtime assertions."""
import copy
import hashlib
import json
from pathlib import Path
import re
import tempfile
import unittest

from tools import memoryweb_gate as gate

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / 'docs/spec'
GUIDE = DOCS / 'AUTHORING_GUIDE.md'
EXAMPLE = DOCS / 'examples/stitch_export_routine.workflow.json'
HEADINGS = ('Intent', 'Starting state', 'Variable slots', 'Ambiguous controls',
            'Expected result', 'Exceptions')
SECTIONS = ('authoring-contract', 'explanation-and-evidence', 'choose-representation',
            'author-workflow', 'author-reply', 'verify-state', 'stitch-export-example',
            'request-focused-demo', 'privacy-and-release', 'budgets-and-checks')
RULES = ('T0', 'T1', 'T2', 'T3', 'capture_before', 'delta_gt', 'set_grew',
         'busy_predicate', 'fallback_verifiers', 'mw.demo_request.v1', 'UNKNOWN',
         'exact_n', 'initial_plus_n', 'right-to-left', 'reconciliation',
         'declarative-only', 'RAM-only', 'AUSENTE', 'current authorized',
         'original deadline', 'null', 'operator_demo', 'fresh unique grounding',
         'site document draft', 'input acceptance', 'submission',
         'generation completion', 'export completion', 'Read retries',
         'delivery', 'verification', 'no raw OCR', 'exact-byte operator review',
         'prompt growth', 'zoom', 'canvas rearrangement', 'tool changes',
         'baseline capture descriptor', 'Existing downloads', 'old checkmarks',
         'ill-typed', 'never send', 'never resend', 'established focus',
         'Chip cleanup is conditional', 'signed displacement', 'never both',
         'lexical miss', 'router reason', 'CAPTCHA', 'human handoff',
         'payment details', 'private messages', 'Byte', 'before decoding/parsing',
         'token accounting', 'audio handling', 'BM25F', 'atomic-switch',
         'envelope', 'workflow', 'site_markdown', 'step_evidence', 'seq',
         'demo_seq', 'frame_sha256', 'mark_id', 'missing_reason', 'rel_box_bps',
         'CONTAINED', 'floor division', 'guidance re-evaluation',
         'already_satisfied', 'non-idempotent')
DEMO_PIN = '4de6b0812f388b66380a574bf66ff2526d18da918b0aff02d541336ad2a33774'
FIXTURES = ['mwl3-example-completion', 'mwl3-example-download', 'mwl3-example-missing-target']
COHORT = dict(fixture_ids=FIXTURES, task_ids=['mwl3-example-export-tail'])
CAPABILITIES = ('mwl3-example-busy', 'mwl3-example-done', 'mwl3-example-downloads',
                'mwl3-example-download-target')


def _budget(raw):
    if len(raw) > 24576:
        raise ValueError('GUIDE_CAPACITY')
    return raw.decode('utf-8', errors='strict')


def _links(raw, protocol):
    refs = re.findall(rb'MEMORYWEB_PROTOCOL\.md#([a-z0-9_-]+)', raw)
    if not refs:
        raise ValueError('NO_PROTOCOL_LINKS')
    for ref in refs:
        if not gate.extract_span(protocol, ref.decode()).strip():
            raise ValueError('EMPTY_SPAN')


def _coverage(raw):
    text = raw.decode('utf-8')
    for section in SECTIONS:
        gate.extract_span(raw, section)
    for rule in RULES:
        if rule.lower() not in ' '.join(text.lower().split()):
            raise ValueError('MISSING_RULE:' + rule)
    if tuple(re.findall(r'^## (.+)$', text, re.M)[2:8]) != HEADINGS:
        raise ValueError('EXPLANATION_TEMPLATE')


def _hygiene(raw, *, workflow=False):
    text = raw.decode('utf-8')
    if re.search(r'(?:/home/|/tmp/|/dev/shm/|/run/user/|[A-Z]:\\)', text):
        raise ValueError('HOST_PATH')
    if re.search(r'(?:password|token|secret|cookie|credential)\s*[=:]\s*\S+', text, re.I):
        raise ValueError('CREDENTIAL')
    if 'data:image' in text.lower():
        raise ValueError('IMAGE')
    if re.search(r'"(?:pixels|pixel_payload|embeddings)"\s*:', text):
        raise ValueError('PIXELS')
    allowed = {('provenance', k) for k in ('quote_sha256', 'receipt_sha256', 'derived_from_sha256')}
    allowed.add(('evaluation_profile', 'cohort_sha256'))
    def walk(value, path=()):
        if isinstance(value, dict):
            for k, v in value.items():
                walk(v, path + (k,))
        elif isinstance(value, list):
            for i, v in enumerate(value):
                walk(v, path + (i,))
        elif isinstance(value, str) and re.search(r'[a-f0-9]{64}', value):
            if path not in allowed or not re.fullmatch(r'[a-f0-9]{64}', value):
                raise ValueError('UNAPPROVED_DIGEST')
    if workflow:
        walk(gate.strict_load(raw))
    elif any(d != DEMO_PIN for d in re.findall(r'[a-f0-9]{64}', text)):
        raise ValueError('UNAPPROVED_DIGEST')


def _capability(obj, predicate):
    accepted = False
    if 'required_capabilities' in obj:
        accepted = (obj['engine'] == 'wender-agent' and
                    obj['version_constraint'] == 'mwl3-example-contract-v1' and
                    obj['required_capabilities'] == list(CAPABILITIES) and predicate is None)
    elif obj.get('rung') == 'T2':
        accepted = obj == dict(rung='T2', mark_kind='button', ocr_label='Download',
                              locale='en', rel_box_bps=[0, 0, 10000, 10000],
                              capability_id=CAPABILITIES[3]) and predicate is None
    elif predicate is obj and obj.get('capability_id') in CAPABILITIES[:3]:
        expected_kind = 'side_effect' if obj['capability_id'] == CAPABILITIES[2] else 'state_signal'
        accepted = (obj['kind'] == expected_kind and obj['bindings'] == [] and
                    obj['evidence_refs'] == [] and obj['comparison'] in ('truth', 'set_grew') and
                    (obj['capability_id'] == CAPABILITIES[2] or obj['comparison'] == 'truth'))
    return gate.GateResult(0, 'PASS') if accepted else gate.GateResult(5, 'UNKNOWN_CAPABILITY')


def _evidence(kind, artifact, related):
    accepted = False
    if kind == 'provenance':
        p = artifact['provenance']
        accepted = (p['source'] == 'import' and p['tier'] == 3 and
                    p['source_ref'] == 'AUTHORING_GUIDE.md#stitch-export-example' and
                    p['derived_from_sha256'] == DEMO_PIN and p['run_id'] is None)
    elif kind == 'ordering':
        v = artifact['verify']
        accepted = (artifact['seq'] == 1 and artifact['action']['kind'] == 'wait_signal' and
                    v['predicate']['capability_id'] == CAPABILITIES[1] and
                    v['busy_predicate']['capability_id'] == CAPABILITIES[0] and
                    v['consecutive_polls'] == 2)
    return gate.GateResult(0, 'PASS') if accepted else gate.GateResult(5, 'UNKNOWN_EVIDENCE')


# @anchor:wa.tests.authoring_guide.TestAuthoringGuide
class TestAuthoringGuide(unittest.TestCase):
    # @anchor:wa.tests.authoring_guide.TestAuthoringGuide.setUp
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.raw = EXAMPLE.read_bytes()
        self.guide = GUIDE.read_bytes()
        self.value = gate.strict_load(self.raw)
        self.bundle = gate.strict_load((DOCS / 'mw.v1.schema.json').read_bytes())
        self.path = self.root / 'stitch-export-routine.json'
        (self.root / 'AUTHORING_GUIDE.md').write_bytes(self.guide)
        self.context = gate.ValidationContext(self.root, capability_check=_capability,
                                               evidence_check=_evidence)

    def _validate(self, value=None, *, raw=None, context=None, name=None):
        path = self.root / name if name else self.path
        if raw is None:
            raw = self.raw if value is None else (gate.canon(value) + '\n').encode()
        path.write_bytes(raw)
        return gate.validate_file(path, self.bundle, context=context or self.context)

    def _pass(self, value=None):
        self.assertEqual(self._validate(value), gate.GateResult(0, 'PASS'))

    def _reject(self, change, category):
        self._pass()
        v = copy.deepcopy(self.value)
        change(v)
        result = self._validate(v)
        self.assertNotEqual(result.rc, 0)
        self.assertEqual(result.category, category)

    # @anchor:wa.tests.authoring_guide.TestAuthoringGuide.test_guide_budget
    def test_guide_budget(self):
        self.assertTrue(_budget(self.guide))
        with self.assertRaisesRegex(ValueError, 'GUIDE_CAPACITY'):
            _budget(self.guide + b' ' * (24577 - len(self.guide)))

    # @anchor:wa.tests.authoring_guide.TestAuthoringGuide.test_protocol_links
    def test_protocol_links(self):
        protocol = (DOCS / 'MEMORYWEB_PROTOCOL.md').read_bytes()
        _links(self.guide, protocol)
        for name, bad, category in (
            ('dead', protocol, 'ANCHOR_MISSING'),
            ('duplicate', protocol + b'\n<a id="end-to-end-procedure"></a>\n## Duplicate\n', 'ANCHOR_DUPLICATE'),
            ('unattached', protocol + b'\n<a id="orphan"></a>\n- list item\n', 'ANCHOR_UNATTACHED'),
            ('crlf', protocol.replace(b'\n', b'\r\n'), 'LINE_ENDINGS')):
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, category):
                _links(self.guide + b' MEMORYWEB_PROTOCOL.md#absent' if name == 'dead' else self.guide, bad)

    # @anchor:wa.tests.authoring_guide.TestAuthoringGuide.test_required_coverage
    def test_required_coverage(self):
        _coverage(self.guide)
        for section in SECTIONS:
            with self.subTest(section=section), self.assertRaises(ValueError):
                _coverage(self.guide.replace(('id="' + section + '"').encode(), b'id="removed"'))
        for rule in RULES:
            with self.subTest(rule=rule), self.assertRaises(ValueError):
                _coverage(re.sub(rb'\s+'.join(re.escape(part).encode() for part in rule.split()), b'REMOVED', self.guide, flags=re.I))
        for heading in HEADINGS:
            with self.subTest(heading=heading), self.assertRaises(ValueError):
                _coverage(self.guide.replace(('## ' + heading + '\n').encode(), b'## Removed\n'))

    # @anchor:wa.tests.authoring_guide.TestAuthoringGuide.test_guidance_recheck_contract
    def test_guidance_recheck_contract(self):
        phrases = (
            'before dispatch, guidance re-evaluation can skip a step only through',
            '`when` or `already_satisfied`',
            'give non-idempotent steps an observable semantic `already_satisfied` predicate',
            'work the operator already completed is not acted on twice',
            '`when` permits an optional condition skip only when proven false',
            '`already_satisfied` permits a satisfied skip only when proven true',
            'admitted by the trusted satisfaction owner',
            'unknown stops',
            'mandatory preconditions still fail closed',
            'protected required steps cannot be condition-skipped',
            'guidance or visual resemblance alone does not prove completion',
            'after input has been sent',
            're-verifies that input',
            'does not itself authorize a resend',
        )

        def check(raw):
            scoped = ' '.join(gate.extract_span(raw, 'author-workflow')
                              .decode('utf-8', errors='strict').lower().split())
            for phrase in phrases:
                self.assertIn(phrase, scoped)

        check(self.guide)
        span = gate.extract_span(self.guide, 'author-workflow')
        paragraph = span[span.index(b'Before dispatch,'):].strip()
        check(self.guide.replace(paragraph, paragraph.upper().replace(b' ', b'  ')))
        misplaced = self.guide.replace(paragraph, b'') + b'\n' + paragraph + b'\n'
        _coverage(misplaced)  # Global keywords cannot establish scoped placement.
        with self.assertRaises(AssertionError):
            check(misplaced)
        with self.assertRaises(UnicodeDecodeError):
            check(self.guide.replace(b'Before dispatch,', b'Before dispatch,\xff'))

        # Each obligation has an independent deletion control, including clauses
        # not affected by the polarity mutants below.
        for phrase in phrases:
            with self.subTest(removed=phrase):
                pattern = rb'\s+'.join(re.escape(part).encode() for part in phrase.split())
                mutant = re.sub(pattern, b'REMOVED', self.guide, flags=re.I)
                self.assertNotEqual(mutant, self.guide)
                with self.assertRaises(AssertionError):
                    check(mutant)

        for original, replacement in (
            (b'Before dispatch, guidance re-evaluation can skip a step only through `when` or `already_satisfied`',
             b'Before dispatch, guidance re-evaluation can skip a step directly on guidance'),
            (b'Give non-idempotent steps', b'Non-idempotent steps do not need'),
            (b'`when` permits an optional condition skip only when proven false',
             b'`when` permits an optional condition skip only when proven true'),
            (b'`already_satisfied` permits a satisfied skip only when proven true',
             b'`already_satisfied` permits a satisfied skip only when proven false'),
            (b' and admitted by the trusted satisfaction owner', b''),
            (b'UNKNOWN stops', b'UNKNOWN may continue'),
            (b'Mandatory preconditions still fail closed', b'Mandatory preconditions may continue'),
            (b'protected required steps cannot be condition-skipped',
             b'protected required steps can be condition-skipped'),
            (b'does not itself authorize a resend', b'authorizes a resend'),
        ):
            with self.subTest(original=original):
                mutant = self.guide.replace(original, replacement)
                self.assertNotEqual(mutant, self.guide)
                with self.assertRaises(AssertionError):
                    check(mutant)

    # @anchor:wa.tests.authoring_guide.TestAuthoringGuide.test_author_reply_contract
    def test_author_reply_contract(self):
        anchor = b'<a id="author-reply"></a>'
        span = gate.extract_span(self.guide, 'author-reply')
        for bad, category in (
            (self.guide.replace(anchor, b''), 'ANCHOR_MISSING'),
            (self.guide + b'\n' + span, 'ANCHOR_DUPLICATE'),
            (self.guide.replace(anchor, anchor + b'\ndetached'), 'ANCHOR_UNATTACHED'),
            (self.guide.replace(b'\n', b'\r\n'), 'LINE_ENDINGS')):
            with self.subTest(category=category), self.assertRaisesRegex(ValueError, category):
                gate.extract_span(bad, 'author-reply')
        phrases = (
            '## Author reply',
            'Normative: [semantic gates](MEMORYWEB_PROTOCOL.md#semantic-gates-beyond-json-schema).',
            'The author reply envelope contains exactly `{workflow, site_markdown, step_evidence}`.',
            'Every association contains exactly `{seq, demo_seq, frame_sha256, mark_id, missing_reason}`.',
            'These association fields belong outside the workflow schema.',
            '| `workflow` | Closed-schema workflow object retaining draft status and frozen constraints. |',
            '| `site_markdown` | Site document draft as Markdown text. |',
            '| `step_evidence` | Ordered associations, exactly one per workflow step [CITED, integrated candidate validator]. |',
            '| `seq` | Corresponding workflow step sequence. |',
            '| `demo_seq` | Selected evidence-pack step sequence, starting at one [CITED, integrated candidate validator]. |',
            "| `frame_sha256` | Selected evidence step's before-frame digest, copied exactly: lowercase hexadecimal of length sixty-four [CITED, author response schema]. |",
            "| `mark_id` | Primary target's OCR mark identifier converted to a string, from the selected frame. |",
            '| `missing_reason` | Explicit missingness for T3-only steps; use `mark_id: null` instead of an invented mark. |',
            "For a primary T2 target, `mark_id` must equal the selected mark's stringified identifier and `missing_reason` must be null.",
            'T3-only authoring should supply a meaningful missing reason.',
            'Enforcement of a closed vocabulary or nonempty reason is AUSENTE — no inventar: the current T3 branch skips mark validation.',
            '`rel_box_bps` is `[left, top, right, bottom]`, with integer basis-point coordinates bounded by zero and ten thousand [CITED, integrated grounding validator].',
            'Convert each pixel edge using integer floor division after multiplying by ten thousand [CITED, integrated grounding validator]: horizontal edges by frame width, vertical edges by frame height.',
            'The resulting mark box must be **CONTAINED** in the region, including its boundary.',
            'Require exactly one same-kind, exact-OCR-label mark fully contained in that region [CITED, integrated grounding validator].',
            'Refuse absent, ambiguous, outside-region or missing-evidence grounding.',
            'Ground each T2 fallback and destination independently by its own kind, label and region.',
            "The association's shared `mark_id` belongs to the primary target.",
        )
        def check(raw):
            scoped = ' '.join(gate.extract_span(raw, 'author-reply').decode().split())
            for phrase in phrases:
                self.assertIn(phrase, scoped)
        check(self.guide)
        for phrase in phrases:
            with self.subTest(phrase=phrase):
                # Preserve the explanation elsewhere: only the scoped span may satisfy it.
                pattern = rb'\s+'.join(re.escape(part).encode() for part in phrase.split())
                replacement = b'## Removed' if phrase.startswith('## ') else b'REMOVED'
                bad_span = re.sub(pattern, replacement, span)
                bad = self.guide.replace(span, bad_span + b'\n\n') + b'\n' + phrase.encode() + b'\n'
                with self.assertRaises(AssertionError):
                    check(bad)
        equality = self.guide.replace(b'**CONTAINED** in the region, including its boundary',
                                      b'EQUAL to the region')
        with self.assertRaises(AssertionError):
            check(equality)

    # @anchor:wa.tests.authoring_guide.TestAuthoringGuide.test_canonical_example
    def test_canonical_example(self):
        self._pass()
        self.assertEqual(self.raw, (gate.canon(self.value) + '\n').encode())
        for name, raw, category in (
            ('pretty', (json.dumps(self.value, indent=2) + '\n').encode(), 'CANONICAL'),
            ('no-lf', self.raw[:-1], 'CANONICAL'),
            ('duplicate', self.raw.replace(b'{', b'{"id":"duplicate",', 1), 'JSON_DUPLICATE_KEY'),
            ('float', self.raw.replace(b'"hi":4', b'"hi":4.0'), 'JSON_NUMBER')):
            with self.subTest(name=name):
                self._pass()
                self.assertEqual(self._validate(raw=raw).category, category)
        self._reject(lambda v: v.update(unknown=True), 'SCHEMA')
        self._pass()
        self.assertEqual(self._validate(raw=self.raw + b' ' * (65537 - len(self.raw))).category, 'CAPACITY')

    # @anchor:wa.tests.authoring_guide.TestAuthoringGuide.test_structural_root
    def test_structural_root(self):
        defs = gate.compile_bundle(self.bundle)
        self.assertTrue(gate.conforms(self.value, defs['workflow'], defs))
        self._reject(lambda v: v.pop('site_id'), 'SCHEMA')
        self._reject(lambda v: v.update(id=v['id'] + '\n'), 'SCHEMA')

    # @anchor:wa.tests.authoring_guide.TestAuthoringGuide.test_gate_owners
    def test_gate_owners(self):
        self._pass()
        missing = gate.ValidationContext(self.root, evidence_check=_evidence)
        self.assertEqual(self._validate(context=missing).category, 'CAPABILITY_MISSING')
        self._reject(lambda v: v['steps'][1]['target'].update(capability_id='unknown'), 'EVIDENCE_DENIED')
        self._reject(lambda v: v['steps'][1]['target'].update(ocr_label='Invented'), 'EVIDENCE_DENIED')
        self._reject(lambda v: v['provenance'].update(derived_from_sha256='a' * 64), 'EVIDENCE_DENIED')
        self._pass()
        invalid_owner = gate.ValidationContext(self.root, capability_check=lambda *a: True, evidence_check=_evidence)
        self.assertEqual(self._validate(context=invalid_owner).category, 'EVIDENCE_DENIED')
        self.assertNotEqual(_evidence('unsupported', {}, ()), gate.GateResult(0, 'PASS'))
        self.assertNotEqual(_capability(dict(capability_id='unsupported'), None), gate.GateResult(0, 'PASS'))

    # @anchor:wa.tests.authoring_guide.TestAuthoringGuide.test_filename_adaptation
    def test_filename_adaptation(self):
        self._pass()
        self.assertEqual(self.path.read_bytes(), self.raw)
        self.assertEqual(self._validate(name=EXAMPLE.name).category, 'IDENTITY')
        self.assertEqual((self.root / EXAMPLE.name).read_bytes(), self.raw)

    # @anchor:wa.tests.authoring_guide.TestAuthoringGuide.test_targets
    def test_targets(self):
        self._pass()
        self.assertEqual(self.value['steps'][1]['target']['rung'], 'T2')
        self._reject(lambda v: v['steps'][1].update(target=None), 'ACTION_TARGET')
        self._reject(lambda v: v['steps'][1].update(target=dict(rung='T3', predicate=v['steps'][0]['verify']['predicate'])), 'ACTION_TARGET')

    # @anchor:wa.tests.authoring_guide.TestAuthoringGuide.test_snapshots
    def test_snapshots(self):
        self._pass()
        step = self.value['steps'][1]
        observe = step['capture_before'][0]['observe']
        predicate = step['verify']['predicate']
        self.assertEqual(predicate['snapshot_ref'], step['capture_before'][0]['name'])
        self.assertEqual(predicate['comparison'], 'set_grew')
        for key in ('kind', 'capability_id', 'bindings'):
            self.assertEqual(observe[key], predicate[key])
        self.assertEqual(self.value['oracle']['postconditions'], [predicate])
        self._reject(lambda v: v['steps'][1].update(capture_before=[]), 'SNAPSHOT')
        self._reject(lambda v: v['steps'][1]['capture_before'][0]['observe'].update(
            kind='state_signal', capability_id=CAPABILITIES[0]), 'SNAPSHOT_COMPATIBILITY')
        self._reject(lambda v: v['steps'][1]['capture_before'][0]['observe'].update(
            bindings=[dict(name='task', type='string', value='foreign')]), 'EVIDENCE_DENIED')

    # @anchor:wa.tests.authoring_guide.TestAuthoringGuide.test_verifiers
    def test_verifiers(self):
        self._pass()
        self.assertEqual(self.value['steps'][0]['verify']['busy_predicate']['capability_id'], CAPABILITIES[0])
        self.assertEqual(self.value['steps'][0]['verify']['consecutive_polls'], 2)
        self._reject(lambda v: v['steps'][1]['verify'].update(predicate=None), 'VERIFIER_PREDICATE')
        self._reject(lambda v: v['steps'][1]['verify'].update(consecutive_polls=31), 'POLLING')
        def fallback(v, depth=1, unequal=None):
            parent = v['steps'][1]['verify']
            child = copy.deepcopy(parent)
            if unequal:
                child[unequal] = 29000 if unequal == 'deadline_ms' else copy.deepcopy(v['steps'][0]['verify']['predicate'])
            parent['fallback_verifiers'] = [child]
            if depth == 2:
                child['fallback_verifiers'] = [copy.deepcopy(child)]
        good = copy.deepcopy(self.value)
        fallback(good)
        self._pass(good)
        self._reject(lambda v: fallback(v, depth=2), 'VERIFIER_DEPTH')
        for field in ('predicate', 'busy_predicate', 'deadline_ms'):
            with self.subTest(field=field):
                self._reject(lambda v: fallback(v, unequal=field), 'VERIFIER_EQUIVALENCE')
        self._reject(lambda v: v['steps'][0]['verify'].update(consecutive_polls=1), 'EVIDENCE_DENIED')
        self._reject(lambda v: v['steps'][1]['verify'].update(action={'kind': 'click'}), 'SCHEMA')
        self._reject(lambda v: v['steps'][1].update(locate_wait_ms=300001), 'SCHEMA')

    # @anchor:wa.tests.authoring_guide.TestAuthoringGuide.test_script_ruling
    def test_script_ruling(self):
        self._pass()
        self.assertIsNone(self.value['script_ref'])
        self.assertIsNone(self.value['script_sha256'])
        self._reject(lambda v: v.update(script_ref='disabled.py'), 'SCRIPT_PAIR')

    # @anchor:wa.tests.authoring_guide.TestAuthoringGuide.test_draft_honesty
    def test_draft_honesty(self):
        self._pass()
        self.assertEqual(self.value['status'], 'draft')
        self.assertTrue(all(x is None for x in self.value['stats'].values()))
        self.assertEqual(self.value['oracle']['mutants_killed'], 0)
        self.assertEqual(self.value['oracle']['receipt_refs'], [])
        self.assertEqual(self.value['evaluation_profile']['fixture_ids'], FIXTURES)
        self.assertEqual(self.value['evaluation_profile']['cohort_sha256'], hashlib.sha256(gate.canon(COHORT).encode()).hexdigest())
        self._reject(lambda v: v.update(status='promoted'), 'ADMISSION')
        self._reject(lambda v: v['stats'].update(support_n=1), 'EVIDENCE_DENIED')
        self._reject(lambda v: v['oracle'].update(mutants_total=4), 'MUTANTS')

    # @anchor:wa.tests.authoring_guide.TestAuthoringGuide.test_provenance_pins
    def test_provenance_pins(self):
        self._pass()
        p = self.value['provenance']
        self.assertEqual(p['receipt_sha256'], hashlib.sha256(self.guide).hexdigest())
        self.assertEqual(p['quote_sha256'], hashlib.sha256(gate.extract_span(self.guide, 'stitch-export-example')).hexdigest())
        self._reject(lambda v: v['provenance'].update(quote_sha256='a' * 64), 'PROVENANCE_PIN')
        self._pass()
        (self.root / 'AUTHORING_GUIDE.md').write_bytes(self.guide + b'\n')
        self.assertEqual(self._validate().category, 'PROVENANCE_PIN')

    # @anchor:wa.tests.authoring_guide.TestAuthoringGuide.test_explanation_gate
    def test_explanation_gate(self):
        path = self.root / 'EXPLANATION.md'
        nominal = ''.join('## ' + h + '\nSynthetic scoped evidence.\n\n' for h in HEADINGS).encode()
        def check(raw):
            path.write_bytes(raw)
            return gate.check_explanation(path)
        for name, bad in (
            ('missing', nominal.replace(b'## Intent\n', b'')),
            ('reordered', nominal.replace(b'## Intent', b'## Starting state', 1)),
            ('empty', nominal.replace(b'## Intent\nSynthetic scoped evidence.', b'## Intent\n', 1)),
            ('fenced', b'```markdown\n' + nominal + b'```\n')):
            with self.subTest(name=name):
                self.assertEqual(check(nominal), gate.GateResult(0, 'PASS'))
                self.assertNotEqual(check(bad).rc, 0)

    # @anchor:wa.tests.authoring_guide.TestAuthoringGuide.test_publication_hygiene
    def test_publication_hygiene(self):
        _hygiene(self.guide)
        _hygiene(self.raw, workflow=True)
        for bad in (b'\ncredential=PLANTED_CANARY', b'\n/home/example/synthetic',
                    b'\n/dev/shm/synthetic', b'\ndata:image/png;base64,canary',
                    b'\n{"pixels":[1,2,3]}', b'\n' + b'a' * 64):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                _hygiene(self.guide + bad)
        for field, value in (('unapproved_digest', 'b' * 64), ('pixels', [1, 2, 3]),
                             ('private', '/tmp/synthetic'), ('credential', 'token=PLANTED_CANARY')):
            with self.subTest(field=field), self.assertRaises(ValueError):
                v = copy.deepcopy(self.value)
                v[field] = value
                _hygiene((gate.canon(v) + '\n').encode(), workflow=True)
