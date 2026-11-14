# @module {"id":"wa.tests.mw_learn_host_unit","layer":4,"owner":"LEARN","deps":["wa.learn.mw_learn_host","wa.learn.mw_learn","wa.tools.mw_learn_host","wa.tests.mw_learn_unit","wa.tests.memoryweb_gate_unit"],"book":"HB-WA-s4-learn"}
"""Synthetic private host intake through public APIs and the real validator."""
import contextlib
import copy
from dataclasses import asdict, replace
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import signal
import tempfile
import unittest
from unittest.mock import patch, Mock

from tools import memoryweb_gate as g, mw_learn_host as cli
from wender.learn import mw_learn as w, mw_learn_host as h
from tests import test_mw_learn_unit as existing
from tests.test_mw_learn_unit import Describer
from tests.test_memoryweb_gate_unit import _explanation, _step, _pred


# @anchor:wa.s4.mw_learn_host_tests.Backend
class Backend:
    # @anchor:wa.s4.mw_learn_host_tests.Backend.__init__
    def __init__(self, fixture):
        self.fixture, self.calls, self.hook = fixture, 0, None

    # @anchor:wa.s4.mw_learn_host_tests.Backend.author
    def author(self, request, *, images, policy, owners, deadline, clock):
        self.calls += 1
        if self.hook:
            self.hook()
        return w._bytes(self.fixture.envelope)


# @anchor:wa.s4.mw_learn_host_tests.HostTests
class HostTests(unittest.TestCase):
    # @anchor:wa.s4.mw_learn_host_tests.HostTests.setUp
    def setUp(self):
        # Reuse only synthetic fixture construction; no existing tests are changed.
        self.fixture = existing.LearnTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        f = self.fixture
        self.root, self.demo, self.output = f.root, f.demo, f.output
        for directory in (self.demo, self.output):
            directory.chmod(0o700)
        rows = []
        for seq, kind in ((7, 'click'), (11, 'text'), (15, 'click')):
            row = dict(f.row, seq=seq, kind=kind, mono_ns=seq * 1000000)
            if kind == 'text':
                row.update(x=None, y=None, button=None, text_len=3)
            rows.append(row)
            for suffix in ('before', 'after'):
                path = self.demo / f'{seq}_{suffix}.png'
                path.write_bytes((self.demo / f'7_{suffix}.png').read_bytes())
                path.chmod(0o600)
        (self.demo / 'events.jsonl').write_text(''.join(json.dumps(r) + '\n' for r in rows))
        for path in self.demo.iterdir():
            path.chmod(0o600)
        self.profile_dir = self.root / 'profiles'
        self.profile_dir.mkdir(mode=0o700)
        self.ack = self.profile_dir / 'ack'
        self.ack.write_bytes(b'synthetic-only-acknowledgement-0000000000')
        self.ack.chmod(0o600)
        identity = self.profile_dir / 'identity.env'
        identity.write_text('SYNTHETIC=1\n')
        identity.chmod(0o600)
        self.profile = dict(capsule='capsule', container='container', window='w', identity_env=str(identity),
            announce_gate='trusted_notice_v1', live_ack_source=dict(kind='file', path=str(self.ack)),
            allowed_modes=['replay'], export_dir=str(self.profile_dir), ack_sha256=hashlib.sha256(self.ack.read_bytes()).hexdigest())
        self.profile_path = self.profile_dir / 'site.json'
        self._save_profile()
        self.slot = dict(slot_id='slot-11', transport='clipboard_paste', clear_first=True, budget_ms=500,
            sensitivity='private_runtime_only', chip_clear_steps=[], empty_len_allowed=[0], post_type_len_allowed=['exact_n'])
        site = g.strict_load(f.inputs.site_metadata.read_bytes())
        provenance = dict(source='human', tier=1, recorded='2026-01-01', source_ref='demos/work/EXPLANATION.md',
            run_id=None, quote_sha256=hashlib.sha256(_explanation()).hexdigest(), receipt_sha256=None, derived_from_sha256=None)
        site['provenance'] = dict(provenance, source_ref='SITE.md')
        site['engine_compat'] = [dict(engine='wender-agent', version_constraint='reviewed-fixture', required_capabilities=['paste'])]
        self.config = dict(schema='mw.learn_host.v1', memoryweb_root=str(self.output), site=site,
            execution_policy=site['execution_policy'], postconditions=[_pred()], protected=[], slot_templates=[self.slot],
            semantic_capabilities={'synthetic-v1': [_pred()]}, recorded_date='2026-01-01',
            policy={k: v for k, v in asdict(f.policy).items() if k not in ('demo_limits', 'effort')},
            demo_limits=asdict(f.policy.demo_limits), capacity_bytes=4 * 1024**3, probe_bytes=65536,
            probe_ns=10**11, store_bytes=2**20, router_root=None, router_query=None, ve_root=None)
        self.config['policy']['images'] = 8
        self.config_path = self.profile_dir / 'site.learn.json'
        self._save_config()
        self.producer = self.root / 'producer'
        self._producer()
        self.envelope = copy.deepcopy(f.envelope)
        workflow = self.envelope['workflow']
        workflow['provenance'] = provenance
        workflow['engine_compat'] = copy.deepcopy(site['engine_compat'])
        workflow['text_slots'] = [dict(self.slot, max_length=4096)]
        workflow['steps'], self.envelope['step_evidence'] = [], []
        target = dict(rung='T2', mark_kind='button', ocr_label='Continue', locale=None,
                      rel_box_bps=[0, 0, 10000, 10000], capability_id='synthetic-v1')
        for n, seq in enumerate((7, 11, 15), 1):
            step = _step()
            step.update(seq=n, target=copy.deepcopy(target))
            step['action'].update(kind='paste' if seq == 11 else 'click', text_slot='slot-11' if seq == 11 else None,
                                  button=None if seq == 11 else 1)
            workflow['steps'].append(step)
            self.envelope['step_evidence'].append(dict(seq=n, demo_seq=n,
                frame_sha256=hashlib.sha256((self.demo / f'{seq}_before.png').read_bytes()).hexdigest(),
                mark_id='9', missing_reason=None))
        self.backend = Backend(self)
        self.patch = patch.object(h, 'BINARY', str(self.producer))
        self.patch.start()
        self.addCleanup(self.patch.stop)
        # Map only the synthetic host workspace parent; never chmod shared host state.
        self.ram_parent = self.root / 'ram-parent'
        self.ram_parent.mkdir(mode=0o700)
        def host_path(value):
            return self.ram_parent if str(value) == '/dev/shm/vqa-ramws' else Path(value)
        host_paths = patch.object(h, 'Path', side_effect=host_path)
        host_paths.start()
        self.addCleanup(host_paths.stop)
        # --map-current-user leaves host-root ancestors unmapped. Translate only
        # these fixed system ancestors in the synthetic profile fixture.
        original_fstat, original_stat = os.fstat, os.stat
        system_ancestors = {(st.st_dev, st.st_ino) for st in
                            (original_stat(p) for p in ('/', '/dev', '/dev/shm'))}
        def normalized(st):
            if st.st_uid == 65534 and (st.st_dev, st.st_ino) in system_ancestors:
                values = list(st)
                values[4] = 0
                return os.stat_result(values)
            return st
        for name, function in (('fstat', lambda fd: normalized(original_fstat(fd))),
                               ('stat', lambda *a, **kw: normalized(original_stat(*a, **kw)))):
            namespace_stats = patch.object(os, name, side_effect=function)
            namespace_stats.start()
            self.addCleanup(namespace_stats.stop)
        self.home_patch = patch.object(Path, 'home', return_value=self.root)
        self.home_patch.start()
        self.addCleanup(self.home_patch.stop)

    def _save_profile(self):
        self.profile_path.write_bytes(w._bytes(self.profile))
        self.profile_path.chmod(0o600)

    def _save_config(self):
        self.config_path.write_bytes(w._bytes(self.config))
        self.config_path.chmod(0o600)

    def _producer(self, raw=b'srf-identity-surface capabilities v1\n--paste --type-stdin --scroll --drag downloads\n', stderr=b'', delay=False, rc=0):
        self.producer.write_text('#!/usr/bin/python3\nimport sys,time\n' +
            ('time.sleep(10)\n' if delay else '') + f'sys.stdout.buffer.write({raw!r})\nsys.stderr.buffer.write({stderr!r})\nsys.exit({rc})\n')
        self.producer.chmod(0o700)

    def _run(self, **kwargs):
        args = dict(profile_name='site', demo_directory=self.demo, explanation=self.fixture.inputs.explanation,
            workflow_id='work', profile_dir=self.profile_dir, configuration_path=self.config_path,
            backend=self.backend, describer=Describer())
        args.update(kwargs)
        return h.run_learn_host(**args)

    def _success(self):
        instances = []
        resource_class = h.HostResources
        def allocated(capacity):
            resource = resource_class(capacity)
            instances.append(resource)
            return resource
        with patch.object(h, 'HostResources', side_effect=allocated):
            result = self._run()
        self.assertTrue(instances)
        self.assertTrue(all(r.reserved == 0 and not r.children and not r.allocations for r in instances))
        self.assertEqual(result.category, 'DRAFT_WRITTEN', result)
        destination = self.output / 'sites/site'
        workflow = g.strict_load((destination / 'workflows/work.json').read_bytes())
        self.assertEqual(workflow['text_slots'][0]['max_length'], self.envelope['workflow']['text_slots'][0]['max_length'])
        self.assertTrue(all(v is None for v in workflow['stats'].values()))
        self.assertEqual(workflow['oracle']['mutants_total'], 0)
        self.assertEqual(workflow['status'], 'draft')
        for path in destination.rglob('*'):
            self.assertEqual(path.stat().st_mode & 0o777, 0o700 if path.is_dir() else 0o600)
            self.assertNotIn(path.suffix, ('.png', '.jpg', '.svg'))
        shutil.rmtree(destination)
        return result

    # @anchor:wa.s4.mw_learn_host_tests.HostTests.test_positive
    def test_positive(self):
        self.assertEqual(self._success().model_calls, 1)
        self.assertIsNone(self._success().usage)

    # @anchor:wa.s4.mw_learn_host_tests.HostTests.test_recorder_completeness
    def test_recorder_completeness(self):
        events = self.demo / 'events.jsonl'
        raw = events.read_bytes()
        events.unlink()
        self.assertEqual(self._run().category, 'INPUT_MISSING')
        self.assertEqual(self.backend.calls, 0)
        events.write_bytes(raw)
        events.chmod(0o600)
        self._success()

    # @anchor:wa.s4.mw_learn_host_tests.HostTests.test_ram_custody
    def test_ram_custody(self):
        with tempfile.TemporaryDirectory() as disk:
            path = Path(disk) / 'recording'
            shutil.copytree(self.demo, path)
            with patch.object(h.demos, 'load_demo', wraps=h.demos.load_demo) as loader:
                self.assertNotEqual(self._run(demo_directory=path).rc, 0)
                self.assertEqual(loader.call_count, 0)
            self.assertEqual(self.backend.calls, 0)
        frame = self.demo / '7_before.png'
        raw = frame.read_bytes()
        frame.unlink()
        frame.symlink_to(self.demo / '11_before.png')
        self.assertNotEqual(self._run().rc, 0)
        self.assertEqual(self.backend.calls, 0)
        frame.unlink()
        frame.write_bytes(raw)
        frame.chmod(0o600)
        self._success()

    # @anchor:wa.s4.mw_learn_host_tests.HostTests.test_frame_sealing
    def test_frame_sealing(self):
        frame = self.demo / '7_before.png'
        raw = frame.read_bytes()
        self.backend.hook = lambda: frame.write_bytes(raw + b'changed')
        self.assertEqual(self._run().category, 'REVISION')
        frame.write_bytes(raw)
        self.backend.hook = None
        self.envelope['step_evidence'][0]['frame_sha256'] = '0' * 64
        self.assertNotEqual(self._run().rc, 0)
        self.envelope['step_evidence'][0]['frame_sha256'] = hashlib.sha256(raw).hexdigest()
        self._success()

    # @anchor:wa.s4.mw_learn_host_tests.HostTests.test_explanation
    def test_explanation(self):
        path = self.fixture.inputs.explanation
        for raw in (b'## Intent\n', _explanation().replace(b'## Intent', b'## Exceptions'),
                    _explanation().replace(b'Operator scoped evidence for intent.', b'')):
            path.write_bytes(raw)
            with patch.object(h.demos, 'load_demo', wraps=h.demos.load_demo) as loader:
                self.assertEqual(self._run().category, 'EXPLANATION')
                self.assertEqual(loader.call_count, 0)
        path.unlink()
        self.assertNotEqual(self._run().rc, 0)
        path.write_bytes(_explanation())
        path.chmod(0o600)
        self._success()

    # @anchor:wa.s4.mw_learn_host_tests.HostTests.test_acknowledgement
    def test_acknowledgement(self):
        raw = self.ack.read_bytes()
        self.ack.unlink()
        self.assertEqual(self._run().category, 'PROFILE_ACKNOWLEDGEMENT')
        self.ack.write_bytes(b'wrong' * 10)
        self.ack.chmod(0o600)
        self.assertEqual(self._run().category, 'PROFILE_ACKNOWLEDGEMENT')
        self.ack.write_bytes(raw)
        self.backend.hook = lambda: self.ack.write_bytes(b'changed' * 8)
        self.assertEqual(self._run().category, 'REVISION')
        self.backend.hook = None
        self.ack.write_bytes(raw)
        self._success()

    # @anchor:wa.s4.mw_learn_host_tests.HostTests.test_producer
    def test_producer(self):
        header = b'srf-identity-surface capabilities v1\n'
        for raw, err, rc in ((b'wrong\n--paste\n', b'', 0), (header + b'--paste --paste\n', b'', 0),
            (header + b'unknown\n', b'', 0), (header + b'--paste', b'', 0), (header + b'--paste\nextra\n', b'', 0),
            (header + b'--paste\n', b'error', 0), (header + b'--paste\n', b'', 1)):
            self._producer(raw, stderr=err, rc=rc)
            self.assertEqual(self._run().category, 'PRODUCER')
        self.config['probe_ns'] = 1000000
        self._save_config()
        self._producer(delay=True)
        self.assertEqual(self._run().category, 'DEADLINE')
        self.config['probe_ns'] = 10**11
        self._save_config()
        self._producer()
        self._success()

    # @anchor:wa.s4.mw_learn_host_tests.HostTests.test_producer_budget
    def test_producer_budget(self):
        self.config['probe_ns'] = 500000000
        self._save_config()
        self.producer.write_text("#!/usr/bin/python3\nimport sys\nwhile True:\n sys.stdout.buffer.write(b'x'*4096)\n sys.stdout.buffer.flush()\n")
        self.producer.chmod(0o700)
        self.assertEqual(self._run().category, 'PRODUCER')
        self.assertEqual(self.backend.calls, 0)
        self.config['probe_ns'] = 10**11
        self._save_config()
        self._producer()
        self._success()

    # @anchor:wa.s4.mw_learn_host_tests.HostTests.test_producer_identity
    def test_producer_identity(self):
        original = self.producer.read_bytes()
        self.backend.hook = lambda: self.producer.write_bytes(original + b'# changed identity\n')
        self.assertEqual(self._run().category, 'REVISION')
        self.backend.hook = None
        self._producer()
        self._success()

    # @anchor:wa.s4.mw_learn_host_tests.HostTests.test_capabilities
    def test_capabilities(self):
        self._producer(b'srf-identity-surface capabilities v1\n--scroll\n')
        self.assertEqual(self._run().category, 'CAPABILITY_MISSING')
        self._producer()
        self.config['semantic_capabilities'] = {}
        self._save_config()
        self.assertNotEqual(self._run().rc, 0)
        self.config['semantic_capabilities'] = {'synthetic-v1': []}
        self._save_config()
        self.assertNotEqual(self._run().rc, 0)
        self.config['semantic_capabilities'] = {'synthetic-v1': [_pred()]}
        self._save_config()
        self._success()

    # @anchor:wa.s4.mw_learn_host_tests.HostTests.test_output_custody
    def test_output_custody(self):
        self.output.chmod(0o755)
        self.assertEqual(self._run().category, 'OUTPUT_CUSTODY')
        self.output.chmod(0o700)
        fstat = os.fstat
        def foreign_root(fd):
            value = fstat(fd)
            if os.readlink(f'/proc/self/fd/{fd}') == str(self.output):
                fields = list(value)
                fields[4] = os.getuid() + 1
                return os.stat_result(fields)
            return value
        with patch.object(w.os, 'fstat', side_effect=foreign_root):
            self.assertEqual(self._run().category, 'OUTPUT_CUSTODY')
        sites = self.output / 'sites'
        sites.mkdir(mode=0o700, exist_ok=True)
        sites.chmod(0o777)
        self.assertEqual(self._run().category, 'OUTPUT_CUSTODY')
        sites.chmod(0o700)
        link = self.output / 'link'
        link.symlink_to(self.root)
        self.assertEqual(self._run().category, 'CONTAINMENT')
        link.unlink()
        self._success()

    # @anchor:wa.s4.mw_learn_host_tests.HostTests.test_host_staging_custody
    def test_host_staging_custody(self):
        validate = g.validate_directory
        def changed(path, *args, **kwargs):
            result = validate(path, *args, **kwargs)
            if Path(path).parent.name.startswith('.mw-staging-'):
                Path(path).chmod(0o755)
            return result
        with patch.object(g, 'validate_directory', side_effect=changed):
            self.assertEqual(self._run().category, 'OUTPUT_CUSTODY')
        self.assertFalse((self.output / 'sites/site').exists())
        self._success()

    # @anchor:wa.s4.mw_learn_host_tests.HostTests.test_ram_parent_custody
    def test_ram_parent_custody(self):
        parent = self.root / 'host-ram-parent'
        parent.mkdir(mode=0o700)
        original_path = Path
        def redirected(value):
            return parent if str(value) == '/dev/shm/vqa-ramws' else original_path(value)
        with patch.object(h, 'Path', side_effect=redirected):
            parent.chmod(0o777)
            self.assertEqual(self._run().category, 'OUTPUT_CUSTODY')
            self.assertEqual(self.backend.calls, 0)
            parent.chmod(0o700)
            fstat = os.fstat
            def foreign(fd):
                st = fstat(fd)
                if os.readlink(f'/proc/self/fd/{fd}') == str(parent):
                    values = list(st)
                    values[4] = os.getuid() + 1
                    return os.stat_result(values)
                return st
            with patch.object(os, 'fstat', side_effect=foreign):
                self.assertEqual(self._run().category, 'OUTPUT_CUSTODY')
                self.assertEqual(self.backend.calls, 0)
            self._success()

    # @anchor:wa.s4.mw_learn_host_tests.HostTests.test_profile_binding
    def test_profile_binding(self):
        profile = h.profiles.load_profile('site', profile_dir=self.profile_dir)
        # Same fields, wrong concrete type: no duck-typed profile admission.
        subclass = type('SyntheticProfile', (h.profiles.Profile,), {})
        for supplied in (subclass(**asdict(profile)), replace(profile, site='other')):
            with self.assertRaises(w._Stop) as raised:
                h.load_host_configuration('site', configuration_path=self.config_path, profile=supplied)
            self.assertEqual(raised.exception.category, 'HOST_CONFIGURATION')
        original = h.build_learn_owners
        def mismatched(**kwargs):
            kwargs['profile'] = replace(kwargs['profile'], window='other')
            return original(**kwargs)
        with patch.object(h, 'build_learn_owners', side_effect=mismatched):
            self.assertEqual(self._run().category, 'HOST_CONFIGURATION')
            self.assertEqual(self.backend.calls, 0)
        self._success()

    # @anchor:wa.s4.mw_learn_host_tests.HostTests.test_visual_guard
    def test_visual_guard(self):
        original = w._artifacts
        def injected(*args):
            return dict(original(*args), **{'frame.png': b'\x89PNG\r\n'})
        with patch.object(w, '_artifacts', side_effect=injected):
            self.assertNotEqual(self._run().rc, 0)
        self.envelope['site_markdown'] = '<svg>visual payload</svg>'
        self.assertNotEqual(self._run().rc, 0)
        self.envelope['site_markdown'] = _explanation().decode()
        self._success()

    # @anchor:wa.s4.mw_learn_host_tests.HostTests.test_sink_guard
    def test_sink_guard(self):
        f = self.fixture
        f.policy = replace(f.policy, images=8)
        projection = h.demos.project_demo_steps(h.demos.segment_demo(h.demos.load_demo(self.demo, limits=f.policy.demo_limits)), demo_id='demo', site_id='site')
        manifest = g.strict_load(f.inputs.demo_manifest.read_bytes())
        manifest['steps_sha256'] = hashlib.sha256(projection.payload).hexdigest()
        f.inputs.demo_manifest.write_bytes(w._bytes(manifest))
        pack = w.build_evidence(f.inputs, policy=f.policy, owners=f.owners, describer=Describer(), clock=lambda: 100)
        reply = w.AuthorReply(**f.envelope)
        original = w._artifacts
        for extra in ({'frame.png': b'PNG'}, {'workflows/extra.json': b'{}'}):
            with tempfile.TemporaryDirectory(dir=self.root) as directory:
                with patch.object(w, '_artifacts', side_effect=lambda *args: dict(original(*args), **extra)), \
                     patch.object(g, 'validate_directory', wraps=g.validate_directory) as validator:
                    result = w.validate_candidate(reply, pack=pack, inputs=f.inputs, policy=f.policy, owners=f.owners, staging=Path(directory))
                    self.assertEqual(result.category, 'OUTPUT_ARTIFACT')
                    self.assertEqual(validator.call_count, 0)
        with tempfile.TemporaryDirectory(dir=self.root) as directory:
            def missing(*args):
                artifacts = original(*args)
                del artifacts['demos/demo/steps.json']
                return artifacts
            with patch.object(w, '_artifacts', side_effect=missing), patch.object(g, 'validate_directory', wraps=g.validate_directory) as validator:
                result = w.validate_candidate(reply, pack=pack, inputs=f.inputs, policy=f.policy, owners=f.owners, staging=Path(directory))
                self.assertEqual(result.category, 'OUTPUT_ARTIFACT')
                self.assertEqual(validator.call_count, 0)
        for content in (b'<svg/>', b'data:image/png;base64,AAAA', b'\x89PNG\r\n'):
            with tempfile.TemporaryDirectory(dir=self.root) as directory:
                def payload(*args):
                    artifacts = original(*args)
                    artifacts['SITE.md'] = content
                    return artifacts
                with patch.object(w, '_artifacts', side_effect=payload), \
                     patch.object(g, 'validate_directory', wraps=g.validate_directory) as validator:
                    result = w.validate_candidate(reply, pack=pack, inputs=f.inputs, policy=f.policy, owners=f.owners, staging=Path(directory))
                    self.assertEqual(result.category, 'OUTPUT_ARTIFACT')
                    self.assertEqual(validator.call_count, 0)
        self._success()

    def _owners(self):
        profile = h.profiles.load_profile('site', profile_dir=self.profile_dir)
        config = h.load_host_configuration('site', configuration_path=self.config_path, profile=profile)
        resource = h.HostResources(config.capacity_bytes)
        self.assertEqual(resource.reserve(1024).rc, 0)
        producer = h._probe(config, resource, lambda: False, 10**30, h.time.monotonic_ns)
        intake = h._intake(self.demo, self.fixture.inputs.explanation, 'work', profile, config, 10**30, lambda: False, h.time.monotonic_ns)
        resource.release()
        return h.build_learn_owners(profile=profile, configuration=config, intake=intake, producer=producer,
            resources=resource, cancelled=lambda: False, clock=h.time.monotonic_ns)

    # @anchor:wa.s4.mw_learn_host_tests.HostTests.test_publication
    def test_publication(self):
        owners = self._owners()
        for dest, purpose in (('https://example.test', 'publish'), ('sites/other/site.json', 'publish'),
            ('sites/site/../site.json', 'publish'), ('sites/site/receipt.json', 'publish'), ('muse', 'publication')):
            self.assertNotEqual(owners.disclosure('0' * 64, owners.scope, owners.authority, dest, purpose, owners.revision).rc, 0)
        self.assertNotEqual(owners.context_factory(self.root).review_check({}, 'publication').rc, 0)
        self.assertNotEqual(owners.context_factory(self.root).review_check({}, 'public_evidence').rc, 0)
        self.assertNotEqual(owners.disclosure('0' * 64, 'foreign', owners.authority, 'sites/site/site.json', 'publish', owners.revision).rc, 0)
        self.assertNotEqual(owners.disclosure('0' * 64, owners.scope, owners.authority, 'muse', 'author', owners.revision).rc, 0)
        self.assertEqual(owners.disclosure('0' * 64, owners.scope, owners.authority, 'sites/site/site.json', 'publish', owners.revision).rc, 0)
        self._success()

    # @anchor:wa.s4.mw_learn_host_tests.HostTests.test_slots
    def test_slots(self):
        for change in ({'max_length': True}, {'max_length': 0}, {'max_length': -1}, {'unknown': 1}):
            template = dict(self.slot, **change)
            with self.assertRaises(w._Stop):
                w.build_text_slots(symbolic_slots=((11, 'slot-11', 3),), templates=[template])
        with self.assertRaises(w._Stop):
            w.build_text_slots(symbolic_slots=((11, 'slot-11', 3),), templates=[self.slot, self.slot])
        self.assertEqual(w.build_text_slots(symbolic_slots=((11, 'slot-11', 3),), templates=[self.slot])[0]['max_length'], 4096)
        self.envelope['workflow']['text_slots'][0]['max_length'] = 3
        self.assertNotEqual(self._run().rc, 0)
        self.config['slot_templates'][0]['max_length'] = 3
        self._save_config()
        self._success()

    # @anchor:wa.s4.mw_learn_host_tests.HostTests.test_evidence_specificity
    def test_evidence_specificity(self):
        self.envelope['step_evidence'][0]['demo_seq'] = 2
        self.envelope['step_evidence'][0]['mark_id'] = 'unrelated'
        self.assertNotEqual(self._run().rc, 0)
        self.envelope['step_evidence'][0].update(demo_seq=1, mark_id='9')
        owners = self._owners()
        context = owners.context_factory(self.root)
        for kind in ('focus', 'drag_start', 'ordering', 'unknown'):
            self.assertNotEqual(context.evidence_check(kind, {}, ()).rc, 0)
        wrong = copy.deepcopy(self.envelope['workflow'])
        wrong['provenance']['quote_sha256'] = '0' * 64
        self.assertNotEqual(context.evidence_check('provenance', wrong, ()).rc, 0)
        self._success()

    # @anchor:wa.s4.mw_learn_host_tests.HostTests.test_resources
    def test_resources(self):
        resource = h.HostResources(100)
        self.assertEqual(resource.reserve(100).rc, 0)
        self.assertNotEqual(resource.reserve(1).rc, 0)
        self.assertEqual(resource.drain(100).rc, 0)
        resource.release()
        self.assertNotEqual(resource.reserve(101).rc, 0)
        self.config['capacity_bytes'] = 1
        self._save_config()
        self.assertEqual(self._run().category, 'CAPACITY')
        self.config['capacity_bytes'] = 4 * 1024**3
        self._save_config()
        self._success()

    # @anchor:wa.s4.mw_learn_host_tests.HostTests.test_drain
    def test_drain(self):
        resource = h.HostResources(100)
        self.assertEqual(resource.reserve(100).rc, 0)
        child = Mock()
        child.poll.return_value = None
        child.wait.side_effect = [h.subprocess.TimeoutExpired('synthetic', 0), 0]
        resource.children.add(child)
        self.assertEqual(resource.drain(1000000).rc, 0)
        child.terminate.assert_called_once()
        child.kill.assert_called_once()
        self.assertFalse(resource.children)
        resource.allocations.add('owned-allocation')
        self.assertEqual(resource.drain(1000).category, 'DRAIN')
        with self.assertRaises(w._Stop):
            resource.release()
        resource.allocations.clear()
        broken = Mock()
        broken.wait.side_effect = h.subprocess.TimeoutExpired('synthetic', 0)
        resource.children.add(broken)
        self.assertEqual(resource.drain(1000).category, 'DRAIN')
        broken.wait.side_effect = None
        broken.wait.return_value = 0
        self.assertEqual(resource.drain(1000).rc, 0)
        resource.release()
        resource.cancelled = True
        self.assertNotEqual(resource.reserve(1).rc, 0)
        self._success()

    # @anchor:wa.s4.mw_learn_host_tests.HostTests.test_deadline_owner_type
    def test_deadline_owner_type(self):
        f = self.fixture
        f.resources.deadline = False
        result = w.learn(f.inputs, policy=f.policy, owners=f.owners, backend=f.backend,
                         describer=Describer(), clock=lambda: 100)
        self.assertEqual(result.category, 'POLICY')
        self.assertEqual(result.model_calls, 0)
        self._success()

    # @anchor:wa.s4.mw_learn_host_tests.HostTests.test_remaining_deadline
    def test_remaining_deadline(self):
        observed = []
        def clock():
            value = h.time.monotonic_ns()
            observed.append(value)
            return value
        author = self.backend.author
        def bounded(request, **kwargs):
            self.assertLessEqual(kwargs['deadline'], observed[0] + self.config['policy']['deadline_ns'])
            self.assertLess(kwargs['policy'].deadline_ns, self.config['policy']['deadline_ns'])
            self.assertEqual(kwargs['policy'].effort, 'medium')
            return author(request, **kwargs)
        with patch.object(self.backend, 'author', side_effect=bounded):
            self.assertEqual(self._run(clock=clock).category, 'DRAFT_WRITTEN')

    # @anchor:wa.s4.mw_learn_host_tests.HostTests.test_phase_reservations
    def test_phase_reservations(self):
        resources = []
        resource_class, popen, loader = h.HostResources, h.subprocess.Popen, h.demos.load_demo
        def allocate(capacity):
            resource = resource_class(capacity)
            resources.append(resource)
            return resource
        def admitted_child(*args, **kwargs):
            self.assertGreater(resources[0].reserved, 0)
            return popen(*args, **kwargs)
        def admitted_decode(*args, **kwargs):
            self.assertGreater(resources[0].reserved, 0)
            return loader(*args, **kwargs)
        with patch.object(h, 'HostResources', side_effect=allocate), \
             patch.object(h.subprocess, 'Popen', side_effect=admitted_child), \
             patch.object(h.demos, 'load_demo', side_effect=admitted_decode):
            self.assertEqual(self._run().category, 'DRAFT_WRITTEN')
        self.assertEqual(resources[0].reserved, 0)
        self.assertFalse(resources[0].children)

    # @anchor:wa.s4.mw_learn_host_tests.HostTests.test_snapshot_bounds
    def test_snapshot_bounds(self):
        directory = self.output / 'empty'
        directory.mkdir(mode=0o700)
        self.config['store_bytes'] = 256
        self._save_config()
        self.assertEqual(self._run().category, 'CAPACITY')
        self.assertEqual(self.backend.calls, 0)
        self.config['store_bytes'] = 4096
        self._save_config()
        self._success()
        added = self.output / 'changed-empty'
        self.backend.hook = lambda: added.mkdir(mode=0o700)
        self.assertEqual(self._run().category, 'REVISION')
        added.rmdir()
        self.backend.hook = None
        self._success()

    # @anchor:wa.s4.mw_learn_host_tests.HostTests.test_revision
    def test_revision(self):
        self.backend.hook = lambda: self.config_path.write_bytes(self.config_path.read_bytes() + b' ')
        self.assertEqual(self._run().category, 'REVISION')
        self._save_config()
        artifact = self.output / 'changed.json'
        def mutate():
            artifact.write_bytes(b'{}')
            artifact.chmod(0o600)
        self.backend.hook = mutate
        self.assertEqual(self._run().category, 'REVISION')
        artifact.unlink()
        self.backend.hook = None
        self._success()

    # @anchor:wa.s4.mw_learn_host_tests.HostTests.test_cancellation
    def test_cancellation(self):
        original = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
        for sig, handler in original.items():
            self.addCleanup(signal.signal, sig, handler)
        segment = h.demos.segment_demo
        def interrupt_intake(*args, **kwargs):
            signal.raise_signal(signal.SIGINT)
            return segment(*args, **kwargs)
        with patch.object(h.demos, 'segment_demo', side_effect=interrupt_intake):
            self.assertEqual(self._run().category, 'CANCELLED')
            self.assertEqual(self.backend.calls, 0)
        for sig in original:
            self.backend.hook = lambda sig=sig: signal.raise_signal(sig)
            self.assertEqual(self._run().category, 'CANCELLED')
            self.assertEqual(signal.getsignal(sig), original[sig])
        self.backend.hook = None
        self._success()

    # @anchor:wa.s4.mw_learn_host_tests.HostTests.test_atomicity
    def test_atomicity(self):
        self._success()
        destination = self.output / 'sites/site'
        destination.mkdir(mode=0o700)
        sentinel = destination / 'sentinel'
        sentinel.write_bytes(b'unchanged')
        sentinel.chmod(0o600)
        self.assertEqual(self._run().category, 'DESTINATION_EXISTS')
        self.assertEqual(sentinel.read_bytes(), b'unchanged')
        shutil.rmtree(destination)
        publish = w._publish
        def collision(staging, target):
            target.mkdir(mode=0o700)
            return publish(staging, target)
        with patch.object(w, '_publish', side_effect=collision):
            self.assertEqual(self._run().category, 'PUBLICATION')
        destination.rmdir()
        validate = g.validate_directory
        def changed(staging, *args, **kwargs):
            result = validate(staging, *args, **kwargs)
            if '.mw-staging-' in str(staging) and result.rc == 0:
                (staging / 'SITE.md').write_bytes(_explanation() + b'changed')
            return result
        with patch.object(g, 'validate_directory', side_effect=changed):
            self.assertEqual(self._run().category, 'STAGING_CHANGED')
        self._success()

    # @anchor:wa.s4.mw_learn_host_tests.HostTests.test_cli
    def test_cli(self):
        args = ['--profile', 'site', '--demo', str(self.demo), '--explanation', str(self.fixture.inputs.explanation),
                '--workflow-id', 'work', '--profile-dir', str(self.profile_dir)]
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main([]), 2)
            self.assertEqual(cli.main(args + ['--effort', 'high']), 2)
            self.assertEqual(cli.main(args + ['--publication']), 2)
        with contextlib.redirect_stdout(io.StringIO()) as invalid:
            self.assertEqual(cli.main([*args[:7], 'bad\n', *args[8:]]), 2)
        self.assertEqual(json.loads(invalid.getvalue())['category'], 'USAGE_SCHEMA')
        actual = h.run_learn_host
        def injected(**kwargs):
            return actual(**kwargs, backend=self.backend, describer=Describer())
        with patch.object(cli, 'run_learn_host', side_effect=injected), contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(cli.main(args), 0, output.getvalue())
        self.assertEqual(json.loads(output.getvalue())['category'], 'DRAFT_WRITTEN')

    # @anchor:wa.s4.mw_learn_host_tests.HostTests.test_hygiene
    def test_hygiene(self):
        original = h.profiles._guard
        seen = []
        @contextlib.contextmanager
        def guarded(path, **kwargs):
            path = Path(path)
            self.assertTrue(path.is_relative_to(self.root), path)
            seen.append(path)
            with original(path, **kwargs) as fd:
                yield fd
        with patch.object(h.profiles, '_guard', side_effect=guarded):
            self._success()
        self.assertIn(self.ack, seen)
        self.assertTrue(all(p.is_relative_to(self.root) for p in seen))

    # @anchor:wa.s4.mw_learn_host_tests.HostTests.test_window_and_final
    def test_window_and_final(self):
        self.profile['window'] = 'foreign'
        self._save_profile()
        self.assertEqual(self._run().category, 'INPUT_BINDING')
        self.assertEqual(self.backend.calls, 0)
        self.profile['window'] = 'w1'
        self._save_profile()
        events = self.demo / 'events.jsonl'
        rows = [json.loads(line) for line in events.read_text().splitlines()]
        for row in rows:
            row['window']['id'] = 'w1'
        events.write_text(''.join(json.dumps(row) + '\n' for row in rows))
        (self.demo / 'final.png').rename(self.demo / 'final_w1.png')
        self._success()

    # @anchor:wa.s4.mw_learn_host_tests.HostTests.test_provisioning_contract
    def test_provisioning_contract(self):
        for key in tuple(self.config):
            value = self.config.pop(key)
            self._save_config()
            self.assertEqual(self._run().category, 'HOST_CONFIGURATION', key)
            self.assertEqual(self.backend.calls, 0)
            self.config[key] = value
        self._save_config()
        self.config_path.unlink()
        self.assertEqual(self._run().category, 'HOST_CONFIGURATION')
        self._save_config()
        other = self.root / 'explicit.learn.json'
        other.write_bytes(self.config_path.read_bytes())
        other.chmod(0o600)
        result = self._run(configuration_path=other)
        self.assertEqual(result.category, 'DRAFT_WRITTEN')
        shutil.rmtree(self.output / 'sites/site')
        self.config_path.unlink()
        self.config_path.symlink_to(other)
        self.assertEqual(self._run().category, 'CONTAINMENT')
        self.config_path.unlink()
        self._save_config()
        repository = self.root / 'configuration-repository'
        repository.mkdir(mode=0o700)
        (repository / '.git').mkdir(mode=0o700)
        contained = repository / 'site.learn.json'
        contained.write_bytes(self.config_path.read_bytes())
        contained.chmod(0o600)
        original_guard = h.profiles._guard
        entered = []
        @contextlib.contextmanager
        def guard(path, **kwargs):
            entered.append(Path(path))
            with original_guard(path, **kwargs) as fd:
                yield fd
        with patch.object(h.profiles, '_guard', side_effect=guard):
            self.assertEqual(self._run(configuration_path=contained).category, 'HOST_CONFIGURATION')
        self.assertNotIn(contained, entered)
        self._success()

    # @anchor:wa.s4.mw_learn_host_tests.HostTests.test_configuration
    def test_configuration(self):
        self.config['command'] = 'never execute'
        self._save_config()
        self.assertEqual(self._run().category, 'HOST_CONFIGURATION')
        del self.config['command']
        self._save_config()
        self.config_path.chmod(0o644)
        self.assertEqual(self._run().category, 'OUTPUT_CUSTODY')
        self.config_path.chmod(0o600)
        fstat = os.fstat
        def foreign_configuration(fd):
            value = fstat(fd)
            if os.readlink(f'/proc/self/fd/{fd}') == str(self.config_path):
                values = list(value)
                values[4] = os.getuid() + 1
                return os.stat_result(values)
            return value
        with patch.object(os, 'fstat', side_effect=foreign_configuration):
            self.assertEqual(self._run().category, 'OUTPUT_CUSTODY')
        self._success()


# Source-only mutants execute in memory; repository files remain byte-identical.
MUTANTS = [
    ('host-output-custody', 'w', "getattr(owners.resources, 'private_output', False) is True", 'False', 'test_host_staging_custody'),
    ('ram-parent-custody', 'h', '        learn._private(parent)\n        learn._ram(parent)', '        learn._ram(parent)', 'test_ram_parent_custody'),
    ('profile-concrete-type', 'h', 'type(profile) is profiles.Profile and profile.site == site', 'profile.site == site', 'test_profile_binding'),
    ('profile-site-binding', 'h', 'type(profile) is profiles.Profile and profile.site == site', 'type(profile) is profiles.Profile', 'test_profile_binding'),
    ('owner-profile-binding', 'h', "learn._need(profile == configuration.profile, 'HOST_CONFIGURATION', 2)", 'pass', 'test_profile_binding'),
    ('configuration-git', 'h', "not any((parent / '.git').exists() for parent in path.parents)", 'True', 'test_provisioning_contract'),
    ('absolute-deadline', 'w', 'deadline = min(deadline, host_deadline)', 'deadline = deadline', 'test_remaining_deadline'),
    ('deadline-owner-type', 'w', "type(host_deadline) is int", 'True', 'test_deadline_owner_type'),
    ('remaining-deadline', 'h', 'policy = replace(configuration.policy, deadline_ns=deadline - clock())', 'policy = configuration.policy', 'test_remaining_deadline'),
    ('intake-admission', 'h', 'learn._pass(resources.reserve(intake_peak))', 'True', 'test_phase_reservations'),
    ('probe-exit', 'h', 'child.wait(timeout=configuration.policy.drain_ns / 1e9) == 0', 'True', 'test_producer'),
    ('probe-bounded', 'h', 'len(stdout) + len(stderr) <= configuration.probe_bytes', 'True', 'test_producer_budget'),
    ('probe-identity', 'h', '_metadata(producer.executable) == producer.executable_identity', 'True', 'test_producer_identity'),
    ('probe-final-newline', 'h', 'raw = bytes(stdout)', "raw = bytes(stdout).rstrip(b'\\n') + b'\\n'", 'test_producer'),
    ('snapshot-metadata-budget', 'h', "total += len(relative.encode('utf-8')) + 256", 'total += 0', 'test_snapshot_bounds'),
    ('snapshot-directories', 'h', 'records.append([relative, _metadata(path)[:4]])', 'pass', 'test_snapshot_bounds'),
    ('recorder-complete', 'h', "(root / 'events.jsonl').is_file() and not (root / 'events.jsonl').is_symlink()", 'True', 'test_recorder_completeness'),
    ('probe-unknown', 'h', 'all(t in known for t in tokens)', 'True', 'test_producer'),
    ('host-deadline', 'h', 'clock() < deadline', 'True', 'test_producer'),
    ('host-cancellation', 'h', '        cancelled = True', '        cancelled = False', 'test_cancellation'),
    ('handler-restore', 'h', '            signal.signal(sig, handler)', '            pass', 'test_cancellation'),
    ('unsupported-evidence', 'h', '    def evidence_check(kind, artifact, related):\n        try:', "    def evidence_check(kind, artifact, related):\n        return gate.GateResult(0, 'PASS')\n        try:", 'test_evidence_specificity'),
    ('host-id', 'h', '        learn._id(workflow_id)', '        pass', 'test_cli'),
    ('profile-root-routing', 'h', 'else Path(profile_dir)\n', 'else Path(profile_dir).parent.parent\n', 'test_hygiene'),
    ('staging-content', 'w', '(adjacent / name).read_bytes() == raw', 'True', 'test_atomicity'),
    ('atomic-no-replace', 'w', 'os.fsencode(destination), 1)', 'os.fsencode(destination), 0)', 'test_atomicity'),
    ('existing-destination', 'w', 'not destination.exists() and not destination.is_symlink()', 'True', 'test_atomicity'),
    ('output-containment', 'w', 'path.resolve() == path', 'True', 'test_output_custody'),
    ('sink-binary', 'w', "text = raw.decode('utf-8')", "text = raw.decode('utf-8', errors='ignore')", 'test_sink_guard'),
    ('ram-before-loader', 'h', '    learn._ram(root)', '    pass', 'test_ram_custody'),
    ('explanation-before-loader', 'h', "learn._pass(gate.check_explanation(Path(explanation)))", 'True', 'test_explanation'),
    ('probe-header', 'h', "lines[0] == b'srf-identity-surface capabilities v1'", 'True', 'test_producer'),
    ('probe-duplicates', 'h', 'len(set(tokens)) == len(tokens)', 'True', 'test_producer'),
    ('probe-stderr', 'h', '== 0 and not stderr', '== 0', 'test_producer'),
    ('probe-lines', 'h', "lines = raw.split(b'\\n')", "lines = raw.split(b'\\n')[:2] + [b'']", 'test_producer'),
    ('native-capabilities', 'h', 'required_ops <= producer.operations', 'True', 'test_capabilities'),
    ('semantic-capabilities', 'h', "predicate in registrations.get(predicate['capability_id'], [])", 'True', 'test_capabilities'),
    ('drain-allocation', 'h', "return gate.GateResult(5, 'DRAIN') if self.allocations else gate.GateResult(0, 'PASS')", "return gate.GateResult(0, 'PASS')", 'test_drain'),
    ('release-owned', 'h', 'not self.children and not self.allocations', 'True', 'test_drain'),
    ('capacity', 'h', 'not 0 < amount <= self.capacity - self.overhead', 'False', 'test_resources'),
    ('concurrent-reservation', 'h', 'self.cancelled or self.reserved or', 'self.cancelled or', 'test_resources'),
    ('provisioning-identity', 'h', '_configuration_file(configuration.path)[1] == configuration.identity', 'True', 'test_revision'),
    ('store-revision', 'h', "_hash(learn._bytes([authority, _snapshot(configuration, resources)])) == revision", 'True', 'test_revision'),
    ('token-revision', 'h', 'profiles._token_digest(profile) == configuration.acknowledgement', 'True', 'test_acknowledgement'),
    ('configuration-closed', 'h', 'set(value) == keys', 'keys <= set(value)', 'test_configuration'),
    ('local-publication', 'h', "(destination in destinations and purpose == 'publish')", "purpose == 'publish'", 'test_publication'),
    ('scope-binding', 'h', '(given_scope, given_authority, given_revision) == (scope, authority, revision)', 'True', 'test_publication'),
    ('author-session', 'h', 'and resources.reserved > 0', '', 'test_publication'),
    ('window-binding', 'h', 'recording.binding[0] == profile.window', 'True', 'test_window_and_final'),
    ('provenance-binding', 'h', "p['quote_sha256'] == _hash(intake.explanation)", 'True', 'test_evidence_specificity'),
    ('promotion-review', 'h', "return gate.GateResult(5, 'REVIEW_DENIED')", "return gate.GateResult(0, 'PASS')", 'test_publication'),
    ('slot-default', 'w', "slot.setdefault('max_length', 4096)", "slot.setdefault('max_length', 3)", 'test_slots'),
    ('slot-schema', 'w', '    _slots(slots)', '    pass', 'test_slots'),
    ('sink-closed', 'w', "set(artifacts) == {'site.json', 'SITE.md', workflows[0],\n                            demo + '/manifest.json', demo + '/steps.json', demo + '/EXPLANATION.md'}", 'True', 'test_sink_guard'),
    ('sink-visual', 'w', "not any(marker in text.lower() for marker in\n                      ('<svg', 'data:image/', 'ivborw0kggo', '/9j/', 'r0lgod'))", 'True', 'test_sink_guard'),
    ('output-owner', 'w', 'st.st_uid == os.getuid()', 'True', 'test_output_custody'),
    ('output-mode', 'w', 'stat.S_IMODE(st.st_mode) == (0o700 if directory else 0o600)', 'True', 'test_output_custody'),
    ('frame-association', 'w', "association['frame_sha256'] == evidence.before[1]", 'True', 'test_frame_sealing'),
    ('mark-association', 'w', "association['mark_id'] == str(mark['id'])", 'True', 'test_evidence_specificity'),
]


# @anchor:wa.s4.mw_learn_host_tests.AuditTests
class AuditTests(unittest.TestCase):
    # @anchor:wa.s4.mw_learn_host_tests.AuditTests.test_gate_removal_mutants
    def test_gate_removal_mutants(self):
        for name, module_name, old, new, test in MUTANTS:
            module = h if module_name == 'h' else w
            source = Path(module.__file__).read_text()
            with self.subTest(mutant=name):
                self.assertEqual(source.count(old), 1, name)
                original = dict(module.__dict__)
                try:
                    exec(compile(source.replace(old, new), module.__file__, 'exec'), module.__dict__)
                    result = unittest.TestResult()
                    with patch.object(cli, 'run_learn_host', h.run_learn_host):
                        HostTests(test).run(result)
                    self.assertFalse(result.errors, (name, result.errors))
                    self.assertTrue(result.failures, name)
                finally:
                    module.__dict__.clear()
                    module.__dict__.update(original)


    # @anchor:wa.s4.mw_learn_host_tests.AuditTests.test_acknowledgement_bypass
    def test_acknowledgement_bypass(self):
        with patch.object(h.profiles, '_token_digest', return_value=('0' * 64, ())):
            result = unittest.TestResult()
            HostTests('test_acknowledgement').run(result)
        self.assertFalse(result.errors)
        self.assertTrue(result.failures)


if __name__ == '__main__':
    unittest.main()
