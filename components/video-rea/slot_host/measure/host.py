# SPDX-License-Identifier: AGPL-3.0-only
"""Host-owned ports for one sealed fixture assignment and attempt."""

from copy import deepcopy

from slot_host.compare import tools as compare
from slot_host.compare._model import Behaviour, Comparison, Episode, Media, Policy, SpatialPolicy, Timeline, TimelinePolicy
from slot_host.compare._visual import Vision
from slot_host.projections.schema import jcs_bytes
from slot_host.runtime.journal_verify import TrustedSeal
from video_rea.expose.wire import loads

from .seal import FixtureError, FixtureReferenceError, sha
from .visual import VisualFixture


class Host:
    def __init__(self, seal, face, assignment, store):
        self.seal = seal
        self.face = face
        self.assignment = assignment
        self.store = store
        self.visual = VisualFixture({})
        self.comparison_witness = None

    def authorize(self, request_id):
        if request_id != self.store.request_id:
            raise FixtureError('Fixture authority is scoped to one request.')

    def asset(self, ref, role):
        row = self.seal.inputs['assets'].get(ref)
        if ref not in self.assignment['refs'] or row is None or row['role'] != role:
            raise FixtureReferenceError('Fixture reference is unlisted or has the wrong role.')
        return row

    def asset_bytes(self, ref, role, cap):
        raw = jcs_bytes(self.asset(ref, role)['data'])
        if len(raw) > cap:
            raise FixtureError('Fixture object exceeds its authorized size.')
        return raw

    def resolve_journal(self, journal_ref, trusted_seal_ref, *, request_id, max_bytes):
        self.authorize(request_id)
        seal = self.asset(trusted_seal_ref, 'seal')['data']
        if journal_ref not in seal['journal_refs']:
            return None
        raw = self.asset(journal_ref, 'journal')['data'].encode()
        if len(raw) > max_bytes:
            return None
        return raw, TrustedSeal(journal_ref, seal['grammar_profile'], seal['root_sha256'], tuple(seal['evidence_ids']))

    def retain_journal_verification(self, request_id, receipt_ref, receipt_bytes):
        self.authorize(request_id)
        self.store.put('journal', receipt_bytes, ref=receipt_ref)
        return self.store.read('journal', request_id=request_id, attempt_id=self.store.attempt_id)

    def resolve_reconstruction_evidence(self, evidence_bundle_ref, *, boundary_id, request_id, max_bytes):
        self.authorize(request_id)
        if self.asset(evidence_bundle_ref, 'reconstruction')['identity'] != boundary_id:
            return None
        return self.asset_bytes(evidence_bundle_ref, 'reconstruction', max_bytes)

    def resolve_operation_schema(self, ref, *, request_id, max_bytes):
        self.authorize(request_id)
        return self.asset_bytes(ref, 'operation-schema', max_bytes)

    def resolve_test_capture(self, ref, *, request_id, max_bytes):
        self.authorize(request_id)
        return self.asset_bytes(ref, 'test-capture', max_bytes)

    def resolve_effect_fixture(self, request, *, request_id, max_bytes):
        self.authorize(request_id)
        return self.asset_bytes(request['run_ref'], 'effect-plan', max_bytes)

    def retain_generated_tape(self, request_id, artifacts):
        return self._retain(request_id, artifacts, lambda parts, _: parts[-3])

    def retain_effect_isolation(self, request_id, artifacts):

        def label(parts, raw):
            kind = parts[-3]
            return kind if kind == 'fixture' else kind + '-' + loads(raw)['side']

        return self._retain(request_id, artifacts, label)

    def _retain(self, request_id, artifacts, label):
        self.authorize(request_id)
        readback = {}
        for ref, raw in artifacts.items():
            key = label(ref.split(':'), raw)
            self.store.put(key, raw, ref=ref)
            readback[ref] = self.store.read(key, request_id=request_id, attempt_id=self.store.attempt_id)
        return readback

    def episode(self, ref, role, checks):
        row = self.asset(ref, role)
        doc = row['data']
        if row['identity'] != doc['episode_id'] or doc['episode_id'] != ref:
            raise FixtureReferenceError('Fixture reference identity differs from the episode.')
        timeline = None
        if 'timeline' in checks:
            timeline = Timeline(record=deepcopy(doc['timeline']), clock_verified=True, complete=True,
                                ordinal_prefix_verified=True, clock_domain=doc['clock_domain'])
        behaviour = None
        if 'structured' in checks:
            behaviour = Behaviour(cases=tuple(deepcopy(doc['behaviour'])), complete=True)
        media = None
        if 'spatial' in checks:
            recording_ref = doc['recording_ref']
            recording = self.seal.inputs['assets'][recording_ref]
            raw = bytes.fromhex(recording['data'])
            if (recording['role'] != 'recording' or recording['identity'] != ref
                    or sha(raw) != doc['media']['recording_sha256']):
                raise FixtureError('Fixture recording differs from its episode or digest.')
            self.visual.recordings[recording_ref] = raw
            media = Media(recording_ref=recording_ref, record=deepcopy(doc['media']))
        return Episode(episode_id=ref, tape_sha256=doc['tape_sha256'],
                       timeline=timeline, behaviour=behaviour, media=media)

    def resolve_comparison(self, tool_id, request_id, *, reference_ref, candidate_ref, policy_ref):
        self.authorize(request_id)
        row = self.asset(policy_ref, 'policy')
        if tool_id != self.face or row['identity'] != row['data']['id'] or reference_ref == candidate_ref:
            raise FixtureReferenceError('Fixture comparison scope, policy or episode pair differs.')
        policy = self.comparison_policy(tool_id, row)
        return Comparison(policy=policy, reference=self.episode(reference_ref, 'reference', policy.checks),
                          candidate=self.episode(candidate_ref, 'candidate', policy.checks),
                          exchange_valid=True, evidence_bound=True, custody_verified=True, lineage_verified=True,
                          fresh=True, determinism_mode='uncontrolled', effects_repeatable=True,
                          perception_ceiling='vision' if policy.spatial else 'none',
                          vision_authorized=policy.spatial is not None,
                          producer={'kind': 'host_mini_tool', 'id': tool_id, 'process_bound': True},
                          oracle_independent=False, contrast_outcomes={}, negative_controls=(),
                          attempt_id=self.store.attempt_id)

    @staticmethod
    def comparison_policy(tool_id, row):
        doc = row['data']
        checks = tuple(doc['checks'][tool_id])
        kinds = frozenset(doc['timeline']['kinds'])
        timeline = TimelinePolicy(kinds=kinds, required_kinds=kinds,
                                  late_tolerance_ms=doc['timeline']['late_tolerance_ms'], integer_milliseconds=False)
        spatial = SpatialPolicy(decoder_ref=doc['decoder_ref'], max_mismatch_ppm=0, max_channel_delta=0,
                                region=(0, 0, 2, 1), frame_indices=None) if 'spatial' in checks else None
        return Policy(id=doc['id'], version=doc['version'], sha256=row['sha256'],
                      comparator_id=tool_id, comparator_version='1', checks=checks,
                      determinism_modes=frozenset({'uncontrolled'}), repeatable_effects_required=False,
                      contrast_ids=(), negative_control_ids=(), timeline=timeline, spatial=spatial)

    def vision_for(self, tool_id, request_id):
        self.authorize(request_id)
        if tool_id != self.face or tool_id != 'visual.compare':
            raise FixtureError('CPU vision admission is scoped to visual.compare.')
        return Vision(pool=self.visual, source=self.visual, front='ViewPoint_REA',
                      timeout=2, ttl=10, attempt_id=self.store.attempt_id, decoded_byte_ceiling=256)

    def retain_comparison(self, tool_id, request_id, report):
        self.authorize(request_id)
        if tool_id != self.face:
            raise FixtureError('Comparison differs from the authorized fixture face.')
        raw = jcs_bytes(report)
        checksum = sha(raw)
        artifact = 'fixture:comparison:sha256:' + checksum
        self.store.put('report', raw, ref=artifact)
        self.store.put_json('comparison-receipt', {
            'request_id': request_id, 'attempt_id': self.store.attempt_id,
            'artifact_ref': artifact, 'artifact_sha256': checksum})
        owner = {'request_id': request_id, 'attempt_id': self.store.attempt_id}
        receipt_raw, receipt_sha256 = self.store.read_verified('comparison-receipt', **owner)
        readback = loads(receipt_raw)
        _, report_sha256 = self.store.read_verified('report', **owner)
        if report_sha256 != readback['artifact_sha256']:
            raise FixtureError('Readback did not resolve the comparison receipt and report.')
        receipt = compare.VerifiedComparisonReceipt(
            'fixture:comparison-receipt:sha256:' + receipt_sha256,
            readback['attempt_id'], readback['request_id'], readback['artifact_ref'], readback['artifact_sha256'])
        return compare.RetainedComparison(artifact, checksum, receipt)

    def receive_comparison_witness(self, witness):
        self.comparison_witness = witness
