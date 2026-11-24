# SPDX-License-Identifier: AGPL-3.0-only
"""Exercise the checkout-only Medicion entrypoint and its independent witnesses."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
FIXTURES = ROOT / 'slot_host/measure/fixtures'
FACES = (
    'slot.inspect', 'behaviour.record', 'behaviour.replay', 'error.diagnose',
    'journal.verify', 'reconstruction.obligations', 'tape.generate', 'effect.isolate',
    'behaviour.compare', 'timeline.compare', 'visual.compare',
)
TRANSPORTS = ['router', 'mcp', 'cli', 'json']


class MedicionHarnessTests(unittest.TestCase):
    def invoke(self, module, *args, fixture_environment=False):
        env = {key: value for key, value in os.environ.items() if key != 'DISPLAY'}
        env.pop('REA_MEDICION_FIXTURE_MANIFEST', None)
        if fixture_environment:
            env['REA_MEDICION_FIXTURE_MANIFEST'] = str(FIXTURES / 'manifest.json')
        return subprocess.run([sys.executable, '-B', '-m', module, *args], cwd=ROOT, env=env,
                              stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=15)

    def harness(self, face, *args, rc=0):
        process = self.invoke('slot_host.measure', face, *args)
        self.assertEqual(process.returncode, rc, process.stdout + process.stderr)
        data = json.loads(process.stdout)
        self.assertEqual(data['scope'], 'fixture')
        self.assertIs(data['ok'], rc == 0)
        if rc == 0:
            self.assertRegex(data['value_id'], r'^fixture:medicion:sha256:[a-f0-9]{64}$')
        else:
            self.assertIsNone(data['value_id'])
            self.assertEqual(data['code'], 'INSUFFICIENT')
        return data

    def cases(self, face, negative='OK'):
        from slot_host.measure.evidence import PAGE_BYTES
        from slot_host.projections.schema import digest

        nominal = self.harness(face)
        contrast = self.harness(face, '--case', 'negative')
        self.assertNotEqual(nominal['value_id'], contrast['value_id'])
        for report, expected in ((nominal, 'OK'), (contrast, negative)):
            rows = report['runs']
            self.assertEqual([item['transport'] for item in rows], TRANSPORTS)
            self.assertEqual({item['code'] for item in rows}, {expected})
            self.assertEqual(len({item['normalized_intent_sha256'] for item in rows}), 1)
            self.assertEqual(len({item['request_id'] for item in rows}), 4)
            for item in rows:
                self.assertEqual(item['terminal_state'], 'fixture_settled')
                self.assertEqual(item['witness']['assignment'], f"{face}/{report['case']}")
                self.assertEqual(item['witness']['attempt_id'], item['attempt_id'])
                self.assertEqual(item['witness']['index_sha256'], item['readback']['index_sha256'])
                paging = item['readback']
                objects = paging['objects']
                self.assertNotIn('consumer-result', objects)
                self.assertEqual(paging['pages'], sum(max(1, (row['bytes'] + PAGE_BYTES - 1) // PAGE_BYTES)
                                                     for row in objects.values()))
                self.assertEqual(paging['bytes'], sum(row['bytes'] for row in objects.values()))
                digests = {key: row['sha256'] for key, row in objects.items()}
                self.assertEqual(item['witness']['result_sha256'], digest(digests) if digests else None)
                if expected != 'OK':
                    self.assertEqual(item['quality']['observed'], 0)
        return nominal['runs'][0], contrast['runs'][0]

    def test_slot_inspect(self):
        nominal, refused = self.cases('slot.inspect', 'SCHEMA_INVALID')
        self.assertEqual(nominal['result']['readiness']['availability'], 'admitted')
        self.assertEqual(nominal['launches'], 0)
        self.assertEqual(refused['dispatches'], 0)

    def test_behaviour_record(self):
        nominal, refused = self.cases('behaviour.record', 'AVAILABILITY_NOT_INSTALLED')
        changed = self.harness('behaviour.record', '--case', 'changed', '--transport', 'cli')['runs'][0]
        self.assertEqual(nominal['result']['occurrences'], [
            {'occurrence': 'occ-a', 'value': 6}, {'occurrence': 'occ-b', 'value': 16}])
        self.assertEqual(changed['result']['occurrences'][0]['value'], 8)
        self.assertEqual(nominal['launches'], 1)
        self.assertEqual(refused['launches'], 0)
        self.assertEqual(refused['quality']['refused'], 2)

    def test_behaviour_replay(self):
        nominal, refused = self.cases('behaviour.replay', 'SCHEMA_INVALID')
        self.assertIn('retention_index', nominal['readback']['objects'])
        self.assertIn('open-profile', nominal['readback']['objects'])
        self.assertIn('terminal', nominal['readback']['objects'])
        self.assertEqual(nominal['quality']['assigned'], 2)
        self.assertEqual(refused['launches'], 0)

    def test_error_diagnose(self):
        nominal, contrast = self.cases('error.diagnose')
        self.assertEqual(nominal['result']['category'], 'control_refusal')
        self.assertEqual(contrast['result']['category'], 'unknown')

    def test_journal_verify(self):
        nominal, contrast = self.cases('journal.verify')
        self.assertEqual(nominal['quality']['observed'], 3)
        self.assertEqual(contrast['quality']['missing'], 1)
        self.assertEqual(contrast['result']['affected_evidence_ids'], ['e2'])

    def test_reconstruction_obligations(self):
        nominal, contrast = self.cases('reconstruction.obligations')
        self.assertEqual(nominal['result']['summary']['closed'], 2)
        self.assertEqual(contrast['result']['summary']['outstanding_obligation_ids'], ['process.packaged'])

    def test_tape_generate(self):
        nominal, contrast = self.cases('tape.generate', 'AVAILABILITY_PARKED')
        self.assertIn('tape', nominal['readback']['objects'])
        self.assertEqual(nominal['quality']['observed'], 2)
        self.assertNotIn('tape', contrast['readback']['objects'])

    def test_effect_isolate(self):
        nominal, contrast = self.cases('effect.isolate', 'AVAILABILITY_PARKED')
        self.assertIn('reset-original', nominal['readback']['objects'])
        self.assertIn('reset-candidate', nominal['readback']['objects'])
        self.assertNotIn('fixture', contrast['readback']['objects'])
        self.assertEqual(nominal['launches'], 0)

    def test_behaviour_compare(self):
        nominal, contrast = self.cases('behaviour.compare')
        self.assertEqual(nominal['result']['verdict']['result'], 'match')
        self.assertEqual(contrast['result']['verdict']['result'], 'mismatch')
        self.assertEqual(contrast['quality']['wrong'], 1)

    def test_timeline_compare(self):
        nominal, late = self.cases('timeline.compare')
        missing = self.harness('timeline.compare', '--case', 'drop')['runs'][0]
        self.assertEqual(nominal['result']['verdict']['result'], 'match')
        self.assertEqual(late['quality']['late'], 1)
        self.assertEqual(missing['quality']['missing'], 1)

    def test_visual_compare(self):
        nominal, pts = self.cases('visual.compare')
        self.assertEqual(nominal['result']['verdict']['result'], 'match')
        self.assertEqual(pts['quality']['late'], 1)
        for case, expected in (('shift-event', ['mismatch', 'match', 'match']),
                               ('change-pixel', ['match', 'match', 'mismatch'])):
            with self.subTest(case=case):
                report = self.harness('visual.compare', '--case', case)
                for row in report['runs']:
                    self.assertEqual([item['outcome'] for item in row['result']['verdict']['checks']], expected)
                    self.assertEqual((row['leases_acquired'], row['leases_released']), (1, 1))

    def test_comparators_reject_empty_unlisted_and_swapped_refs(self):
        for face in ('behaviour.compare', 'timeline.compare', 'visual.compare'):
            for case, dispatches in (('missing', 0), ('unknown', 1), ('swap', 1)):
                with self.subTest(face=face, case=case):
                    report = self.harness(face, '--case', case)
                    self.assertEqual({(row['code'], row['dispatches'], row['leases_acquired'])
                                      for row in report['runs']}, {('SCHEMA_INVALID', dispatches, 0)})
                    for row in report['runs']:
                        self.assertEqual(row['readback']['objects'], {})
                        self.assertEqual((row['readback']['pages'], row['readback']['bytes']), (0, 0))
                        self.assertIsNone(row['witness']['result_sha256'])

    def test_consumer_readback_clock_excludes_durable_commit(self):
        from slot_host.measure import evidence

        steps = []
        commit = evidence.Store.commit

        def tracked_commit(store):
            steps.append('commit')
            return commit(store)

        def tick():
            steps.append('clock')
            return len(steps)

        with tempfile.TemporaryDirectory() as temp:
            owner = {'request_id': 'req-test-clock', 'attempt_id': 'att-test-clock'}
            store = evidence.Store(Path(temp) / 'evidence', **owner)
            with (patch.object(evidence.Store, 'commit', tracked_commit),
                  patch.object(evidence, 'perf_counter_ns', tick)):
                values, metrics = evidence.readback(store, (), owner)
        self.assertEqual(steps, ['commit', 'clock', 'clock'])
        self.assertEqual(metrics['readback_ns'], 1)
        self.assertEqual((values, metrics['pages'], metrics['bytes']), ({}, 0, 0))

    def assert_fixture_insufficient(self, face, detail):
        from slot_host.measure.__main__ import main

        with redirect_stdout(StringIO()) as output:
            rc = main([face, '--transport', 'router'])
        result = json.loads(output.getvalue())
        self.assertEqual(rc, 16, result)
        self.assertEqual(result['code'], 'INSUFFICIENT')
        self.assertIsNone(result['value_id'])
        self.assertIn(detail, result['detail'])

    def test_paged_comparison_requires_admitted_diagnostics(self):
        from slot_host.measure import harness

        original = harness.readback

        def remove_checks(*args):
            artifacts, paging = original(*args)
            artifacts['report']['diagnostics']['checks'] = []
            return artifacts, paging

        for face in ('behaviour.compare', 'timeline.compare', 'visual.compare'):
            with self.subTest(face=face), patch.object(harness, 'readback', remove_checks):
                self.assert_fixture_insufficient(face, 'Paged comparison check inventory')

    def test_paged_journal_verdict_must_agree_with_journal_bytes(self):
        from slot_host.measure import harness

        original = harness.readback

        def substitute_verdict(*args):
            artifacts, paging = original(*args)
            artifacts['journal']['result'].update(
                completeness='incomplete', violation_steps=[2], affected_evidence_ids=['e2'])
            return artifacts, paging

        with patch.object(harness, 'readback', substitute_verdict):
            self.assert_fixture_insufficient('journal.verify', 'Paged journal verification')

    def test_stateless_faces_verify_the_actual_projected_payload(self):
        from slot_host.measure import harness

        substitutions = {
            'slot.inspect': ('selected_installation_ref', None),
            'error.diagnose': ('retryable', True),
            'reconstruction.obligations': ('closure_digest', '0' * 64),
        }
        original = harness.Session.invoke

        def change_projection(session, transport):
            reply = original(session, transport)
            key, value = substitutions[session.face]
            payload = reply['result']['payload']
            self.assertNotEqual(payload[key], value)
            payload[key] = value
            return reply

        for face in substitutions:
            message = 'Projected slot inspection' if face == 'slot.inspect' else f'Projected {face}'
            with self.subTest(face=face), patch.object(harness.Session, 'invoke', change_projection):
                self.assert_fixture_insufficient(face, message)

    def test_visual_consumer_decodes_recording_bytes(self):
        from slot_host.measure import harness
        from slot_host.measure.seal import Seal, sha

        original = harness.readback
        seal = Seal(FIXTURES / 'manifest.json')

        def substitute_pixels(*args):
            artifacts, paging = original(*args)
            recording = seal.inputs['assets']['rgb:ep:candidate']
            raw = bytes.fromhex(recording['data'])
            altered = raw[:-1] + bytes([raw[-1] ^ 1])
            recording['data'] = altered.hex()
            seal.inputs['assets']['ep:candidate']['data']['media']['recording_sha256'] = sha(altered)
            return artifacts, paging

        with (patch.object(harness, 'Seal', return_value=seal),
              patch.object(harness, 'readback', substitute_pixels)):
            self.assert_fixture_insufficient('visual.compare', 'Independently decoded RGB8 bytes')

    def test_shifted_pts_observes_timing_without_pixel_values(self):
        from slot_host.measure import harness

        with patch.object(harness, 'occurrences', wraps=harness.occurrences) as observed:
            report = harness.run(FIXTURES / 'manifest.json', 'visual.compare', 'negative', ('router',))
        frames = [row for row in observed.call_args.args[1] if row['occurrence'].startswith('frame-')]
        self.assertEqual(frames, [{'occurrence': 'frame-0', 'ticks': 0}, {'occurrence': 'frame-1', 'ticks': 150}])
        run = report['runs'][0]
        self.assertEqual(run['quality']['late'], 1)
        self.assertEqual(run['result']['verdict']['checks'][2]['outcome'], 'abstain')

    def test_retained_parent_must_match_sealed_assignment_and_tape(self):
        from slot_host.measure import harness
        from slot_host.runtime.evidence_projection import record_digest

        original = harness.readback
        profiles = ('open-profile', 'terminal-profile')
        external = ('started', 'terminal', 'retention_index')

        def substituted(*args):
            artifacts, paging = original(*args)
            for key in changed:
                row = artifacts[key]['episode'] if key in profiles else artifacts[key]
                row['parent_episode_id'] = 'ep:wrong-parent'
            artifacts['terminal']['start_sha256'] = record_digest(artifacts['started'])
            artifacts['retention_index']['profile_sha256'] = record_digest(artifacts['terminal-profile'])
            if 'consumer-result' in artifacts:
                artifacts['consumer-result']['receipt'] = artifacts['terminal-profile']
            return artifacts, paging

        for face, parent in (('behaviour.replay', 'ep:reference'), ('behaviour.record', None)):
            nominal = harness.run(FIXTURES / 'manifest.json', face, 'nominal', ('router',))
            for changed in ((profiles[0],), (external[0],), (profiles[1], *external[1:]), (*profiles, *external)):
                with self.subTest(face=face, changed=changed), patch.object(harness, 'readback', substituted):
                    self.assert_fixture_insufficient(face, 'Paged runtime parent')
            with self.subTest(face=face, witness=True):
                self.assertEqual(nominal['runs'][0]['witness'].get('parent_episode_id', 'absent'), parent)

    def test_projection_value_reproduces(self):
        first, second = self.harness('projection'), self.harness('projection')
        self.assertEqual(first['value_id'], second['value_id'])
        self.assertEqual(first['projection_sha256'], second['projection']['regenerated_sha256'])
        self.assertGreaterEqual(first['projection']['verified_rows'], len(FACES))
        self.assertGreater(first['projection']['compile_ns'], 0)

    def test_value_id_binds_transport_workload_and_projection(self):
        from slot_host.measure import harness
        from slot_host.projections.schema import digest

        manifest = FIXTURES / 'manifest.json'
        transport_sets = [('router',), ('cli',), tuple(TRANSPORTS), tuple(reversed(TRANSPORTS))]
        results = [harness.run(manifest, 'error.diagnose', 'nominal', row) for row in transport_sets]
        self.assertEqual(len({row['value_id'] for row in results}), len(transport_sets))
        baseline = results[0]
        self.assertEqual(harness.run(manifest, 'error.diagnose', 'nominal', ('router',))['value_id'],
                         baseline['value_id'])
        self.assertEqual([row['transports'] for row in results], [list(row) for row in transport_sets])
        seal = harness.Seal(manifest)
        assignment = seal.inputs['cases']['error.diagnose/nominal']
        self.assertEqual(baseline['assignment_sha256'], digest(assignment))
        assignment['arguments']['attempt_ref'] = 'fixture:other-attempt'
        seal.truth['assignments']['error.diagnose/nominal']['occurrences'][0]['occurrence'] = 'fixture:other-attempt'
        with patch.object(harness, 'Seal', return_value=seal):
            changed = harness.run(manifest, 'error.diagnose', 'nominal', ('router',))
        self.assertEqual(changed['input_sha256'], baseline['input_sha256'])
        self.assertNotEqual(changed['assignment_sha256'], baseline['assignment_sha256'])
        self.assertNotEqual(changed['value_id'], baseline['value_id'])

        compile_publication = harness.Publication.__init__

        def change_projection(publication, sealed):
            compile_publication(publication, sealed)
            publication.projection_sha256 = '0' * 64

        with patch.object(harness.Publication, '__init__', change_projection):
            changed = harness.run(manifest, 'error.diagnose', 'nominal', ('router',))
        self.assertEqual(changed['build_sha256'], baseline['build_sha256'])
        self.assertEqual(changed['policy_sha256'], baseline['policy_sha256'])
        self.assertNotEqual(changed['projection_sha256'], baseline['projection_sha256'])
        self.assertNotEqual(changed['value_id'], baseline['value_id'])

    def test_value_id_covers_build_sources_and_registry_files(self):
        from slot_host.measure import harness

        manifest = FIXTURES / 'manifest.json'
        paths = (
            'slot_host/__init__.py', 'video_rea/__init__.py', 'episode_provenance.py',
            'contracts/manifest/rea.slot_manifest_v2.schema.json',
            'contracts/evidence/rea.slot_evidence_profile_v1.schema.json',
            '../vp-direct/schemas/vp.control.req_v1.schema.json',
            '../vp-direct/schemas/vp.control.reply_v1.schema.json',
        )
        read = harness.read_regular
        baseline = harness.run(manifest, 'projection', 'nominal', ())

        def alter_bytes(path):
            raw = read(path)
            return raw + b'\n' if path.resolve() == (ROOT / changed).resolve() else raw

        for changed in paths:
            with self.subTest(file=changed), patch.object(harness, 'read_regular', alter_bytes):
                report = harness.run(manifest, 'projection', 'nominal', ())
                self.assertEqual(report['projection_sha256'], baseline['projection_sha256'])
                self.assertNotEqual(report['build_sha256'], baseline['build_sha256'])
                self.assertNotEqual(report['value_id'], baseline['value_id'])

    def test_value_id_binds_effective_runtime_policy(self):
        from slot_host.measure import harness

        manifest = FIXTURES / 'manifest.json'
        runtime = harness.run(manifest, 'behaviour.record', 'nominal', ('router',))
        runtime_policy = harness.Runtime.policy

        def change_grants(*args):
            policy = runtime_policy(*args)
            return policy | {'grants': [*policy['grants'], 'grant.fixture.extra']}

        with patch.object(harness.Runtime, 'policy', change_grants):
            changed = harness.run(manifest, 'behaviour.record', 'nominal', ('router',))
        self.assertEqual(changed['projection_sha256'], runtime['projection_sha256'])
        self.assertNotEqual(changed['policy_sha256'], runtime['policy_sha256'])
        self.assertNotEqual(changed['value_id'], runtime['value_id'])

    def test_value_id_binds_comparison_policy_source_without_references(self):
        from slot_host.measure import harness
        from slot_host.projections.schema import digest

        manifest = FIXTURES / 'manifest.json'
        comparison = harness.run(manifest, 'visual.compare', 'missing', ('router',))
        self.assertEqual(comparison['input_sha256'], {})
        seal = harness.Seal(manifest)
        document = seal.inputs['assets']['policy:fixture']
        document['data']['timeline']['late_tolerance_ms'] += 1
        document['sha256'] = digest(document['data'])
        with patch.object(harness, 'Seal', return_value=seal):
            changed = harness.run(manifest, 'visual.compare', 'missing', ('router',))
        self.assertNotEqual(changed['value_id'], comparison['value_id'])
        self.assertEqual(changed['policy_pins']['comparator_source'], document['sha256'])

    def test_value_id_binds_effective_comparison_policy(self):
        from dataclasses import replace

        from slot_host.measure import harness, host

        manifest = FIXTURES / 'manifest.json'
        comparison = harness.run(manifest, 'visual.compare', 'nominal', ('router',))
        comparison_policy = host.Policy

        def change_effective_policy(**kwargs):
            policy = comparison_policy(**kwargs)
            return replace(policy, spatial=replace(policy.spatial, max_channel_delta=1))

        with patch.object(host, 'Policy', change_effective_policy):
            changed = harness.run(manifest, 'visual.compare', 'nominal', ('router',))
        self.assertNotEqual(changed['value_id'], comparison['value_id'])
        self.assertEqual(changed['policy_pins']['comparator_source'], comparison['policy_pins']['comparator_source'])
        self.assertNotEqual(changed['policy_pins']['comparator'], comparison['policy_pins']['comparator'])

    def test_every_sealed_file_is_verified_before_dispatch(self):
        import hashlib

        baseline = self.harness('projection')
        with tempfile.TemporaryDirectory() as temp:
            mirror = Path(temp) / 'fixture'
            shutil.copytree(FIXTURES, mirror)
            path = mirror / 'manifest.json'
            self.assertEqual(self.harness('projection', '--manifest', str(path))['value_id'], baseline['value_id'])
            for name in ('manifest.json', 'inputs.json', 'truth.json', 'fixture_runner.py'):
                with self.subTest(name=name):
                    source = mirror / name
                    correct = source.read_bytes()
                    source.write_bytes(correct + b' ')
                    denied = self.harness('behaviour.record', '--manifest', str(path), rc=16)
                    self.assertIn('digest', denied['detail'])
                    source.write_bytes(correct)
            inputs = mirror / 'inputs.json'
            inputs.write_bytes(inputs.read_bytes() + b' ')
            forged = json.loads(path.read_bytes())
            forged['files']['inputs.json'] = hashlib.sha256(inputs.read_bytes()).hexdigest()
            path.write_text(json.dumps(forged), encoding='utf-8')
            self.assertIn('manifest digest', self.harness('projection', '--manifest', str(path), rc=16)['detail'])
            path.unlink()
            path.symlink_to(FIXTURES / 'manifest.json')
            self.harness('projection', '--manifest', str(path), rc=16)

    def test_production_cli_remains_closed_with_fixture_material_present(self):
        self.harness('slot.inspect', '--transport', 'cli')
        fixture = json.loads((FIXTURES / 'inputs.json').read_bytes())
        for face in FACES:
            with self.subTest(face=face):
                module = 'slot_host.compare.tools' if face.endswith('.compare') else 'slot_host.runtime.tools'
                payload = fixture['cases'][face + '/nominal']['arguments']
                args = json.dumps({'request_id': 'req-medicion-production-check', 'payload': payload})
                bare = self.invoke(module, 'mini', face, '--args', args)
                present = self.invoke(module, 'mini', face, '--args', args, fixture_environment=True)
                self.assertEqual((bare.returncode, bare.stdout, bare.stderr),
                                 (present.returncode, present.stdout, present.stderr))
                self.assertEqual((present.returncode, present.stdout), (16, ''))
                self.assertIn('authenticated slot control lane is unavailable', present.stderr)
        for face in ('behaviour.compare', 'timeline.compare', 'visual.compare'):
            empty = self.invoke('slot_host.compare.tools', 'mini', face,
                                '--args', '{"request_id":"req-medicion-missing","payload":{}}')
            self.assertEqual(empty.returncode, 14)

    def test_paging_and_readback_reject_incomplete_wrong_parent_and_tampered_bytes(self):
        from slot_host.measure.evidence import PAGE_BYTES, Store, collect_pages, readback
        from slot_host.measure.seal import FixtureError

        raw = b'"' + b'x' * (3 * PAGE_BYTES - 2) + b'"'
        with self.assertRaisesRegex(FixtureError, 'incomplete'):
            collect_pages(len(raw), lambda offset: (raw[offset:offset + PAGE_BYTES], None))
        with tempfile.TemporaryDirectory() as temp:
            owner = {'request_id': 'req-test-ro', 'attempt_id': 'att-test-ro'}
            store = Store(Path(temp) / 'evidence', **owner)
            store.put_json('result', {'value': 6})
            store.put('paged', raw)
            self.assertEqual(collect_pages(len(raw), lambda offset: store.read_page('paged', offset, **owner)), (raw, 3))
            with self.assertRaises(PermissionError):
                store.read_page('result', 0, **(owner | {'attempt_id': 'att-stale-ro'}))
            path = store.root / store.index['result']['sha256']
            path.write_bytes(b'{"value":8}')
            with self.assertRaisesRegex(FixtureError, 'indexed bytes'):
                readback(store, ('result', 'paged'), owner)

    def test_occurrence_oracle_separates_wrong_duplicate_unknown_late_and_missing(self):
        from slot_host.measure.evidence import occurrences

        expected = [{'occurrence': key, 'ticks': 20, 'value': 'good'} for key in ('a', 'b', 'c', 'd')]
        rows = [{'occurrence': 'a', 'ticks': 20, 'value': 'wrong'},
                {'occurrence': 'a', 'ticks': 20, 'value': 'good'},
                {'occurrence': 'b', 'ticks': 21, 'value': 'good'},
                {'occurrence': 'c', 'outcome': 'refused'},
                {'occurrence': 'extra', 'ticks': 20, 'value': 'good'}]
        self.assertEqual(occurrences(expected, rows), {
            'assigned': 4, 'observed': 5, 'wrong': 1, 'duplicate': 1,
            'unknown': 1, 'late': 1, 'refused': 1, 'missing': 1})
