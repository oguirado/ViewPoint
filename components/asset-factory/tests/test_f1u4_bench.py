"""Synthetic host tests. These do not run models or claim asset acceptance."""
import contextlib
import copy
import hashlib
import importlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
b = importlib.import_module('bench.run_bench')
H = 'a' * 64

def data(value):
    return b.core.canonical_json(value).encode()

def digest(value):
    return hashlib.sha256(value).hexdigest()

def fixture(provider_id='synthetic', case_id='case-a', *, origin_provider='synthetic'):
    policy = {'required_checks':{g:[g + '-check'] for g in b.GATES},
              'vqa3d_profile':{'id':'synthetic-intake','sha256':H}}
    case = {'schema':'af.bench-case/v1','case_id':case_id,'slot':'generate',
            'reference':{'path':'ref.bin','sha256':digest(b'reference'),
                         'provenance':'synthetic host fixture', 'origin_provider':origin_provider,
                         'redistribution_allowed':True},
            'seed':42,'parameters':{'resolution':512},'gate_policy_sha256':b.core.sha256_json(policy)}
    manifest = {'schema':'af.provider/v1','id':provider_id,'revision':'frozen-revision',
                'slot':'generate','kind':'tool','runnable':True,
                'licence':{'code':'AGPL-3.0-only','weights':'none','deps':[]},
                'territory_excluded':[],'commercial':True,'gated':False,'weights':[],
                'runtime':{'container':'synthetic-tool','ram_peak_gb_measured':None,
                           'ram_peak_measurement_ref':None},'known_issues':[]}
    factory = {'schema':'af.factory-config/v1','commercial_use':False,'jurisdiction':'ES',
               'models_path':'models','memory':{'max_resident_models':1,'ceiling_gb_default':8,
                                               'min_free_gb_before_start':1},
               'slots':{slot:{'provider':provider_id if slot=='generate' else 'none'} for slot in b.core.SLOTS},
               'gates':{'appearance':{'floor':4,'target':7},'vqa3d_profile':'synthetic-intake'}}
    territory = {'schema':'af.territory-groups/v1','countries':['ES'],'groups':{'EU':['ES']}}
    decision = {'schema':'af.licence-decision/v1','rc':0,'verdict':'PASS','allowed':True,
                'provider_id':provider_id,'operation':'generate','jurisdiction':'ES','commercial_use':False,
                'monetisable':True,'reason_codes':[],'evaluated_dependencies':[],
                'configuration_sha256':b.core.sha256_json(factory),
                'provider_sha256':b.core.sha256_json(manifest),'policy_sha256':b.core.sha256_json(territory)}
    provider = {'manifest':manifest,'factory':factory,'policy':territory,'decision':decision}
    bindings = {'case_sha256':b.core.sha256_json(case),'provider_sha256':b.core.sha256_json(manifest),
                'gate_policy_sha256':case['gate_policy_sha256'],'toolchain_sha256':H,
                'input_sha256':case['reference']['sha256'],'output_sha256':digest(b'asset')}
    rec = {'schema':'af.bench-evidence/v1','case_id':case_id,'provider_id':provider_id,
           'provider_revision':manifest['revision'],**bindings,'stage_rc':0,
           'outputs':[{'path':'asset.glb','sha256':digest(b'asset')}], 'gate_receipts':[],
           'wall_ms':12,'memory_peak_bytes':2048,'measurement_ref':{'path':'measurement.json','sha256':H},
           'absence_reasons':{}}
    files = {'ref.bin':b'reference','asset.glb':b'asset'}
    previous = None
    for gate in b.GATES:
        body = {'schema':'af.bench-gate/v1','gate':gate,**bindings,'rc':0,'verdict':'PASS',
                'profile':policy['vqa3d_profile'] if gate=='vqa3d' else None,
                'prev_receipt_sha256':previous,'checks':[{'id':gate+'-check','rc':0,'verdict':'PASS'}]}
        content = data(body)
        ref = {'gate':gate,'path':gate+'.json','sha256':digest(content)}
        files[ref['path']] = content
        rec['gate_receipts'].append(ref)
        previous = ref['sha256']
    measurement = {'schema':'af.bench-measurement/v1',**{k:bindings[k] for k in ('case_sha256','provider_sha256','toolchain_sha256','output_sha256')},
                   'scope':'owned-provider-cgroup','wall_ms':12,'memory_peak_bytes':2048,
                   'execution':{'id':'synthetic-run','cgroup_path':'/sys/fs/cgroup/synthetic.scope',
                                'provider_id':provider_id,'provider_revision':manifest['revision']},
                   'observation_ref':{'path':'observation.json','sha256':H}}
    observation = {'schema':'af.bench-observation/v1','execution':measurement['execution'],
                   'source':'cgroup-v2-memory.peak','started_monotonic_ns':1000000,
                   'finished_monotonic_ns':13000000,'memory_peak_bytes':2048,
                   **{k:bindings[k] for k in ('case_sha256','provider_sha256','toolchain_sha256','output_sha256')}}
    files['observation.json'] = data(observation)
    measurement['observation_ref']['sha256'] = digest(files['observation.json'])
    files['measurement.json'] = data(measurement)
    rec['measurement_ref']['sha256'] = digest(files['measurement.json'])
    return case, provider, {'record':rec,'files':files,'gate_policy':policy,'toolchain_sha256':H}

def rewrite_gate(evidence, name, change):
    """Rehash a mutation and all later links: negatives do not rely on stale hashes."""
    previous = None
    for ref in evidence['record']['gate_receipts']:
        body = json.loads(evidence['files'][ref['path']])
        body['prev_receipt_sha256'] = previous
        if ref['gate'] == name:
            change(body)
        content = data(body)
        evidence['files'][ref['path']] = content
        ref['sha256'] = digest(content)
        previous = ref['sha256']

def disk_plan(directory, case, provider, evidence):
    for name, content in evidence['files'].items():
        (directory / name).write_bytes(content)
    content = data(evidence['record'])
    (directory / 'evidence.json').write_bytes(content)
    plan = {'schema':'af.bench-plan/v1','cases':[case],'providers':[provider],
            'gate_policy':evidence['gate_policy'],'toolchain_sha256':H,
            'evidence':[{'case_id':case['case_id'],'provider_id':provider['manifest']['id'],
                         'path':'evidence.json','sha256':digest(content)}]}
    (directory/'plan.json').write_bytes(data(plan))
    return directory/'plan.json'

class BenchTests(unittest.TestCase):
    def setUp(self):
        self.case, self.provider, self.evidence = fixture()
        self.assertEqual(b.evaluate_case(self.case,self.provider,self.evidence)['replay_rc'],0)
        self.assertEqual(b.evaluate_case(self.case,self.provider,self.evidence)['rc'],3)

    def row(self):
        return b.evaluate_case(self.case,self.provider,self.evidence)

    def test_bench_complete_matrix_same_policy(self):
        cases=[]; providers=[]; evidences={}
        for cid in ('case-a','case-b'):
            for pid in ('provider-a','provider-b'):
                c,p,e=fixture(pid,cid)
                if pid=='provider-a': cases.append(c)
                if cid=='case-a': providers.append(p)
                evidences[(cid,pid)]=e
        def evaluate():
            return b.evaluate_matrix(cases,providers,evidences,gate_policy=self.evidence['gate_policy'],toolchain_sha256=H)
        report=evaluate()
        self.assertEqual(report['rc'],3)
        self.assertTrue(all(row['replay_rc']==0 for row in report['rows']))
        self.assertEqual(len(report['rows']),4)
        del evidences[('case-b','provider-b')]
        self.assertEqual(evaluate()['rc'],3)
        self.assertEqual(evaluate()['rows'][-1]['reasons'],['missing_receipt'])
        evidences[('case-b','provider-b')]=fixture('provider-b','case-b')[2]
        evidences[('case-b','provider-b')]['record']['gate_policy_sha256']='b'*64
        self.assertEqual(evaluate()['rows'][-1]['rc'],4)

    def test_bench_missing_receipt_is_cannot_see(self):
        self.evidence['record']=None
        self.assertEqual(self.row()['rc'],3)
        self.assertEqual(self.row()['reasons'],['missing_receipt'])

    def test_bench_missing_required_gate(self):
        self.evidence['record']['gate_receipts'].pop()
        self.assertEqual(self.row()['rc'],3)
        self.assertIn('missing_receipt',self.row()['reasons'])

    def test_bench_receipt_identity_and_chain(self):
        original=copy.deepcopy(self.evidence)
        mutations=[lambda e:e['files'].__setitem__('asset.glb',b'tampered'),
                   lambda e:e['files'].__setitem__('ref.bin',b'tampered'),
                   lambda e:e['record'].__setitem__('provider_revision','other'),
                   lambda e:e['record'].__setitem__('input_sha256','b'*64),
                   lambda e:e['record'].__setitem__('case_sha256','b'*64),
                   lambda e:e['record'].__setitem__('toolchain_sha256','b'*64)]
        for mutation in mutations:
            self.evidence=copy.deepcopy(original)
            mutation(self.evidence)
            self.assertEqual(self.row()['rc'],4)
        self.evidence=copy.deepcopy(original)
        rewrite_gate(self.evidence,'input',lambda g:g.__setitem__('prev_receipt_sha256','b'*64))
        self.assertEqual(self.row()['rc'],4)

    def test_bench_gate_binding_is_rechecked(self):
        for key in ('case_sha256','provider_sha256','gate_policy_sha256','toolchain_sha256','input_sha256','output_sha256'):
            self.evidence=fixture()[2]
            rewrite_gate(self.evidence,'appearance',lambda g:g.__setitem__(key,'b'*64))
            self.assertEqual(self.row()['rc'],4,key)

    def test_bench_incomplete_is_never_pass(self):
        for verdict in ('SKIP','UNCLEAR','ADVISORY','INCOMPLETE'):
            self.evidence=fixture()[2]
            rewrite_gate(self.evidence,'vqa3d',lambda g:g.__setitem__('verdict',verdict))
            self.assertEqual(self.row()['rc'],2)
        self.evidence=fixture()[2]
        rewrite_gate(self.evidence,'vqa3d',lambda g:g.update(rc=3,verdict='CANNOT_SEE',checks=[{'id':'vqa3d-check','rc':3,'verdict':'CANNOT_SEE'}]))
        self.assertEqual(self.row()['rc'],3)
        self.evidence=fixture()[2]
        rewrite_gate(self.evidence,'vqa3d',lambda g:g.__setitem__('checks',[]))
        self.assertEqual(self.row()['rc'],2)

    def test_bench_profile_is_required_and_pinned(self):
        rewrite_gate(self.evidence,'vqa3d',lambda g:g['profile'].__setitem__('sha256','b'*64))
        self.assertEqual(self.row()['rc'],4)
        self.evidence=fixture()[2]
        self.evidence['gate_policy']['vqa3d_profile']=None
        self.case['gate_policy_sha256']=b.core.sha256_json(self.evidence['gate_policy'])
        self.assertEqual(self.row()['rc'],3)

    def test_bench_required_coverage(self):
        rewrite_gate(self.evidence,'vqa3d',lambda g:g['checks'][0].__setitem__('id','other-check'))
        self.assertEqual(self.row()['rc'],3)
        self.assertEqual(self.row()['reasons'],['required_check_missing'])

    def test_bench_summary_cannot_override_failed_check(self):
        rewrite_gate(self.evidence,'vqa3d',lambda g:g['checks'][0].update(rc=5,verdict='FAIL'))
        self.assertEqual(self.row()['rc'],2)

    def test_bench_all_required_gates_block(self):
        for gate in b.GATES:
            self.evidence=fixture()[2]
            rewrite_gate(self.evidence,gate,lambda g:g.update(rc=5,verdict='FAIL',checks=[{'id':gate+'-check','rc':5,'verdict':'FAIL'}]))
            self.assertEqual(self.row()['rc'],5,gate)

    def test_bench_report_rc_is_not_stage_rc(self):
        rewrite_gate(self.evidence,'vqa3d',lambda g:g.update(schema='vqa.report/v1',rc=1,verdict='FAIL'))
        self.assertEqual(self.row()['rc'],2)
        # Raw run/asset/trace families require their own verified readers, never generic rc conversion.
        for family in ('vqa.receipt/v1','bvqa.asset-receipt/v1','vqa3d.profile-trace/v1'):
            self.evidence=fixture()[2]
            rewrite_gate(self.evidence,'vqa3d',lambda g:g.__setitem__('schema',family))
            self.assertEqual(self.row()['rc'],2)

    def test_bench_bool_rc_and_duplicate_checks(self):
        rewrite_gate(self.evidence,'input',lambda g:g.__setitem__('rc',False))
        self.assertEqual(self.row()['rc'],2)
        self.evidence=fixture()[2]
        rewrite_gate(self.evidence,'input',lambda g:g['checks'].append(g['checks'][0].copy()))
        self.assertEqual(self.row()['rc'],2)

    def test_bench_duplicate_gate(self):
        self.evidence['record']['gate_receipts'].append(self.evidence['record']['gate_receipts'][0])
        self.assertEqual(self.row()['rc'],2)

    def test_bench_missing_metrics_are_null(self):
        self.evidence['record'].update(wall_ms=None,memory_peak_bytes=None,measurement_ref=None,
                                      absence_reasons={'wall_ms':'sampler_failed','memory_peak_bytes':'sampler_failed'})
        row=self.row()
        self.assertEqual(row['rc'],3)
        self.assertIsNone(row['memory_peak_bytes'])
        self.assertIsNone(row['wall_ms'])

    def test_bench_unproven_measurement_is_null(self):
        self.evidence['record']['measurement_ref']=None
        row=self.row()
        self.assertEqual(row['rc'],3)
        self.assertEqual(row['replay_rc'],3)
        self.assertIsNone(row['memory_peak_bytes'])
        self.assertIn('memory_peak_bytes',row['absence_reasons'])

    def test_bench_measurement_tamper_and_attribution(self):
        self.evidence['files']['measurement.json']=b'tampered'
        self.assertEqual(self.row()['rc'],4)
        self.evidence=fixture()[2]
        rec=json.loads(self.evidence['files']['measurement.json'])
        rec['scope']='host'
        self.evidence['files']['measurement.json']=data(rec)
        self.evidence['record']['measurement_ref']['sha256']=digest(data(rec))
        row=self.row()
        self.assertEqual(row['rc'],3)
        self.assertIsNone(row['memory_peak_bytes'])

    def test_bench_metric_boolean_and_absence_reason(self):
        self.evidence['record']['wall_ms']=False
        self.assertEqual(self.row()['rc'],2)
        self.evidence=fixture()[2]
        self.evidence['record']['wall_ms']=None
        self.assertEqual(self.row()['rc'],2)

    def test_bench_declarations_and_fixture_publication_authority(self):
        baseline = self.row()
        self.provider['decision'].update(allowed=False,rc=5,verdict='FAIL')
        self.assertEqual(baseline["replay_rc"], 0, baseline)
        self.assertEqual(self.row(), baseline)
        self.case, self.provider, self.evidence = fixture(provider_id="hunyuan3d21", origin_provider="hunyuan3d21")
        row = self.row()
        self.assertEqual(row["replay_rc"], 0, row)
        self.assertEqual(row["case_sha256"], b.core.sha256_json(self.case))
        self.assertEqual(row["reasons"], ["upstream_verification_unavailable"])
        self.case['reference']['redistribution_allowed']=False
        self.assertEqual(self.row()['rc'],5)

    def test_bench_noncommercial_dependency_decision(self):
        self.provider['manifest']['licence']['deps']=[{'name':'synthetic-nc','licence':'synthetic-NC',
            'commercial':False,'territory_excluded':[],'gated':False,'used_by':['generate']}]
        self.provider['decision']['provider_sha256']=b.core.sha256_json(self.provider['manifest'])
        self.provider['decision']['monetisable']=False
        self.assertEqual(b._provider(self.provider,self.case),self.provider['manifest'])
        self.provider['factory']['commercial_use']=True
        self.provider['decision'].update(commercial_use=True,rc=5,verdict='FAIL',allowed=False,
            reason_codes=['dependency_noncommercial'],configuration_sha256=b.core.sha256_json(self.provider['factory']))
        self.assertEqual(b._provider(self.provider,self.case),self.provider['manifest'])

    def test_bench_decision_replay(self):
        self.provider['factory']['models_path']='changed-models'
        self.assertEqual(self.row()['rc'],4)
        self.provider=fixture()[1]
        self.provider['factory']['commercial_use']=True
        self.assertEqual(self.row()['rc'],4)
        self.provider=fixture()[1]
        self.provider['decision']['allowed']=False
        self.assertEqual(self.row()['rc'],2)

    def test_bench_blocked_provider_and_revision(self):
        self.provider['manifest']['runnable']=False
        self.provider['manifest']['blocked_reason']='test_blocked'
        self.provider['decision']['provider_sha256']=b.core.sha256_json(self.provider['manifest'])
        self.assertEqual(self.row()['rc'],5)
        for revision in ('main','HEAD','master','latest'):
            self.provider=fixture()[1]
            self.provider['manifest']['revision']=revision
            self.provider['decision']['provider_sha256']=b.core.sha256_json(self.provider['manifest'])
            self.evidence['record']['provider_revision']=revision
            self.evidence['record']['provider_sha256']=b.core.sha256_json(self.provider['manifest'])
            self.assertEqual(self.row()['rc'],2)

    def test_bench_canonical_and_closed_schema(self):
        row=self.row()
        b.validate_record(row,'results/result')
        encoded=data(row)
        self.assertEqual(encoded,data(json.loads(encoded)))
        self.evidence['record']['accepted']=True
        self.assertEqual(self.row()['rc'],2)

    def test_bench_empty_and_duplicate_matrix(self):
        with self.assertRaises(b.Refusal) as cm:
            b.evaluate_matrix([],[],{},gate_policy=self.evidence['gate_policy'],toolchain_sha256=H)
        self.assertEqual(cm.exception.rc,3)
        with self.assertRaises(b.Refusal) as cm:
            b.evaluate_matrix([self.case,self.case],[self.provider],{},gate_policy=self.evidence['gate_policy'],toolchain_sha256=H)
        self.assertEqual(cm.exception.rc,2)

    def test_bench_aggregate_precedence(self):
        self.assertEqual(b.core.worst_rc([6,5,4]),5)
        self.assertEqual(b.core.worst_rc([5,2]),2)
        self.assertEqual(b.core.worst_rc([4]),3)
        self.assertEqual(b.core.worst_rc([]),3)
        self.assertEqual(b.core.worst_rc([1]),2)

    def test_bench_refusal_preserves_outputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            plan=disk_plan(root,self.case,self.provider,self.evidence)
            output=root/'out'; output.mkdir()
            target=output/'results.json'; target.write_bytes(b'sentinel')
            with contextlib.redirect_stdout(io.StringIO()) as replay:
                self.assertEqual(b.run_bench(plan,('synthetic',),output,timeout_s=10),3)
            self.assertEqual(target.read_bytes(),b'sentinel')
            published=json.loads(replay.getvalue())
            self.assertEqual(published['rows'][0]['replay_rc'],0)
            self.assertEqual(published['verdict'],'CANNOT_SEE')
            self.assertEqual(published['mode'],'evidence-replay')
            source_plan=(output/published['source_plan']['path']).resolve()
            self.assertEqual(source_plan,plan.resolve())
            self.assertEqual(digest(source_plan.read_bytes()),published['source_plan']['sha256'])
            self.assertEqual(published['evidence_records'][0]['path'],'evidence.json')
            target.write_bytes(b'sentinel')
            (root/'asset.glb').write_bytes(b'wrong')
            with contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(b.run_bench(plan,('synthetic',),output,timeout_s=10),3)
            self.assertEqual(json.loads(out.getvalue())['rows'][0]['rc'],4)
            self.assertEqual(target.read_bytes(),b'sentinel')
            (root/'plan.json').write_bytes(b'{"schema":"bad"}')
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(b.run_bench(plan,('synthetic',),output,timeout_s=10),2)
            self.assertEqual(target.read_bytes(),b'sentinel')

    def test_bench_failure_reaches_process_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            plan=disk_plan(root,self.case,self.provider,self.evidence)
            argv=[sys.executable,'-I','-B',str(ROOT/'bench/run_bench.py'),'--cases',str(plan),
                  '--provider','synthetic','--output-dir',str(root/'out'),'--timeout-s','10']
            result=subprocess.run(argv,capture_output=True,timeout=15)
            self.assertEqual(result.returncode,3,result.stderr)
            self.assertEqual(json.loads(result.stdout)['rows'][0]['replay_rc'],0)
            (root/'input.json').unlink()
            result=subprocess.run(argv,capture_output=True,timeout=15)
            self.assertEqual(result.returncode,3,result.stderr)
            self.assertEqual(json.loads(result.stdout)['rows'][0]['rc'],3)
            self.assertEqual(json.loads(result.stdout)['rows'][0]['gate_results'][0]['reason'],'missing_receipt')

    def test_bench_plan_duplicate_float_and_path_escape(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            plan=disk_plan(root,self.case,self.provider,self.evidence)
            output=root/'out'; output.mkdir()
            target=output/'results.json'; target.write_bytes(b'sentinel')
            for content in (b'{"x":1,"x":2}', b'{"x":1.5}', b'{"x":NaN}'):
                plan.write_bytes(content)
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(b.run_bench(plan,('synthetic',),output,timeout_s=10),2)
                self.assertEqual(target.read_bytes(),b'sentinel')
            plan=disk_plan(root,self.case,self.provider,self.evidence)
            payload=json.loads(plan.read_bytes()); payload['evidence'][0]['path']='../outside'
            plan.write_bytes(data(payload))
            with contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(b.run_bench(plan,('synthetic',),output,timeout_s=10),2)
            self.assertEqual(json.loads(out.getvalue())['rows'][0]['reasons'],['path_escape'])
            self.assertEqual(target.read_bytes(),b'sentinel')

    def test_bench_timeout_preserves_outputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            plan=disk_plan(root,self.case,self.provider,self.evidence)
            out=root/'out'; out.mkdir(); target=out/'results.json'; target.write_bytes(b'sentinel')
            with mock.patch.object(b.time,'monotonic',side_effect=[0,11]), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(b.run_bench(plan,('synthetic',),out,timeout_s=10),6)
            self.assertEqual(target.read_bytes(),b'sentinel')

    def test_bench_symlink_and_budget(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as outside:
            root=Path(tmp)
            plan=disk_plan(root,self.case,self.provider,self.evidence)
            (Path(outside)/'input').write_bytes(self.evidence['files']['input.json'])
            (root/'input.json').unlink(); (root/'input.json').symlink_to(Path(outside)/'input')
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(b.run_bench(plan,('synthetic',),root/'out',timeout_s=10),2)
            plan=disk_plan(root,self.case,self.provider,self.evidence)
            with mock.patch.object(b,'MAX_TOTAL_BYTES',1), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(b.run_bench(plan,('synthetic',),root/'out',timeout_s=10),6)

    def test_bench_parser_rejects_duplicates_and_nonintegers(self):
        self.assertEqual(b._json_bytes(b'{"x":1}'),{'x':1})
        for content,reason in [(b'{"x":1,"x":2}','duplicate_key'),
                               (b'{"x":1.5}','non_integer_number'),
                               (b'{"x":NaN}','non_integer_number')]:
            with self.assertRaises(b.Refusal) as cm:
                b._json_bytes(content)
            self.assertEqual(cm.exception.rc,2)
            self.assertEqual(cm.exception.reason,reason)

    def test_bench_json_depth_is_bounded(self):
        self.assertEqual(b._json_bytes(b'[[1]]'),[[1]])
        with self.assertRaises(b.Refusal) as cm:
            b._json_bytes(b'['*2000+b'0'+b']'*2000)
        self.assertEqual(cm.exception.rc,6)
        self.assertEqual(cm.exception.reason,'json_depth_budget')

    def test_bench_all_schema_examples(self):
        report=b.evaluate_matrix([self.case],[self.provider],{('case-a','synthetic'):self.evidence},
                                gate_policy=self.evidence['gate_policy'],toolchain_sha256=H)
        refs=json.loads((ROOT/'bench/references/manifest.json').read_bytes())
        examples=[(self.case,'case'),(self.evidence['record'],'evidence'),
                  (json.loads(self.evidence['files']['input.json']),'gate'),
                  (json.loads(self.evidence['files']['measurement.json']),'measurement'),
                  (self.row(),'results/result'),(report,'results/matrix'),(refs,'references')]
        for value,name in examples:
            b.validate_record(value,name)
            invalid=copy.deepcopy(value); invalid['extra']=True
            with self.assertRaises(b.Refusal):
                b.validate_record(invalid,name)

    def test_bench_reference_copy_integrity(self):
        root=ROOT/'bench/references'
        manifest=json.loads((root/'manifest.json').read_bytes())
        self.assertEqual(len(manifest['references']),3)
        for ref in manifest['references']:
            self.assertEqual(digest((root/ref['path']).read_bytes()),ref['sha256'])

    def test_bench_references_exclude_third_party_render(self):
        # Operator ruling 2026-11-16: the GemDroid render is third-party property and must never ship.
        root=ROOT/'bench/references'
        manifest=json.loads((root/'manifest.json').read_bytes())
        listed={ref['path'] for ref in manifest['references']}
        on_disk={p.name for p in root.iterdir() if p.name!='manifest.json'}
        self.assertEqual(listed,on_disk)
        for name in listed|on_disk:
            self.assertNotIn('gemdroid',name.lower())

if __name__ == '__main__':
    unittest.main()
