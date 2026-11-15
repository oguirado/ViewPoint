"""B0 positive and negative contracts; all scratch stays in the SB0 surface."""
import contextlib
import csv
import copy
import importlib.util
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from blender_visual_qa import cli, contracts, judge, report

ROOT = Path(__file__).resolve().parents[1]
SCRATCH = ROOT / 'build/room_SB0'
SCRATCH.mkdir(parents=True, exist_ok=True)


def load_tool(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'tools' / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gates = load_tool('gates')
catalogue = load_tool('refresh_catalogue')


class Fixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=SCRATCH)
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)

    def put(self, path, value):
        p = self.root / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(value)
        return p

    def tool_gate(self, name):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = gates.main([name, '--root', str(self.root)])
        return rc, json.loads(out.getvalue())


class TestB0Gates(Fixture):
    def test_migration_bijection_and_copy_negative_controls(self):
        baseline = json.loads((ROOT/'tools/MIGRATION_SOURCE.json').read_text())
        names = gates.git(baseline['repo'],'ls-tree','-r','--name-only',baseline['commit']).decode().splitlines()
        selected = 'visual_qa/schemas/oracle-result.schema.json'
        content = gates.git(baseline['repo'],'show',baseline['commit']+':'+selected)
        self.put('copied.json',content.decode())
        rows = [{'src':name,'dst':'copied.json' if name==selected else '-', 'action':'COPY' if name==selected else 'DROP','note':'{}'} for name in names]
        self.put('tools/MIGRATION_SOURCE.json',json.dumps(baseline))
        self.put('tools/MIGRATION_POLICY.json',json.dumps({row['src']:{'action':row['action'],'dst':row['dst']} for row in rows}))
        self.put('tools/ADOPTED.json','{"files":[]}')
        self.put('bin/blender-visual-qa','fixture entry')
        self.put('migration/copy_manifest.tsv','fixture location')
        def write_manifest(data):
            with (self.root/'copy_manifest.tsv').open('w') as f:
                writer=csv.DictWriter(f,fieldnames=['src','dst','action','note'],delimiter='\t',lineterminator='\n')
                writer.writeheader();writer.writerows(data)
        write_manifest(rows)
        self.assertEqual(self.tool_gate('g_migration')[0],0)
        write_manifest(rows[1:])
        self.assertEqual(self.tool_gate('g_migration')[0],5)
        write_manifest(rows)
        self.put('copied.json',content.decode()+' ')
        rc, obj = self.tool_gate('g_migration')
        self.assertEqual(rc,5)
        self.assertIn('copy_byte',str(obj))

    def test_vendor_nominal_and_byte_mutant(self):
        shutil.copytree(ROOT / 'tools', self.root / 'tools')
        self.assertEqual(self.tool_gate('g_vendor')[0], 0)
        with (self.root / 'tools/route_code.py').open('a') as f:
            f.write('\n# wrong upstream bytes\n')
        rc, result = self.tool_gate('g_vendor')
        self.assertEqual(rc, 5)
        self.assertIn('vendor_mismatch', str(result))

    def test_pin_nominal_and_wrong_version(self):
        shutil.copytree(ROOT / 'tools', self.root / 'tools')
        cfg = json.loads((ROOT / 'config/vision.json').read_text())
        cfg['engine_path'] = str(ROOT / cfg['engine_path'])
        self.put('config/vision.json', json.dumps(cfg))
        self.assertEqual(self.tool_gate('g_pin')[0], 0)
        cfg['pin']['version'] = '1.0.9'
        self.put('config/vision.json', json.dumps(cfg))
        self.assertEqual(self.tool_gate('g_pin')[0], 5)

    def test_boundary_nominal_and_each_direction(self):
        self.put('bpy_side/q.py', 'import math\n')
        self.put('bpy_side/canon.py', 'import hashlib\n')
        self.put('blender_visual_qa/report.py', 'from bpy_side import q, canon\n')
        self.assertEqual(self.tool_gate('g_bpy_boundary')[0], 0)
        self.put('blender_visual_qa/report.py', 'import bpy\n')
        self.assertEqual(self.tool_gate('g_bpy_boundary')[0], 5)
        self.put('blender_visual_qa/report.py', '')
        self.put('bpy_side/q.py', 'import bpy\n')
        self.assertEqual(self.tool_gate('g_bpy_boundary')[0], 5)
        self.put('bpy_side/q.py', '')
        self.put('bpy_side/other.py', 'from blender_visual_qa import contracts\n')
        self.assertEqual(self.tool_gate('g_bpy_boundary')[0], 5)

    def test_dynamic_boundary_and_numpy(self):
        self.put('bpy_side/q.py', '')
        self.put('bpy_side/canon.py', '')
        self.put('blender_visual_qa/report.py', 'import importlib\nimportlib.import_module("bpy")\n')
        self.assertEqual(self.tool_gate('g_bpy_boundary')[0], 5)
        self.put('blender_visual_qa/report.py', 'import numpy as np\n')
        self.assertEqual(self.tool_gate('g_numpy_scope')[0], 5)
        self.put('blender_visual_qa/report.py', '')
        self.put('blender_visual_qa/truth/labels.py', 'import numpy\n')
        self.assertEqual(self.tool_gate('g_numpy_scope')[0], 0)

    def test_schema_dotted_and_bad_mutant(self):
        self.put('blender_visual_qa/schemas/scene.v1.schema.json', '{"properties":{"schema":{"const":"bvqa.scene/v1"}}}')
        self.assertEqual(self.tool_gate('g_schema_prefix')[0], 0)
        self.put('examples/mutant.json', '{"schema":"bvqa.mutant-manifest/v1","mutant_id":"DM-seed"}')
        self.assertEqual(self.tool_gate('g_schema_prefix')[0], 5)
        self.put('examples/mutant.json', '{"schema":"bvqa.scene.v1"}')
        self.assertEqual(self.tool_gate('g_schema_prefix')[0], 5)

    def test_wave_dependency_cycle_and_absence(self):
        self.assertEqual(self.tool_gate('g_wave_dag')[0], 5)
        deps = {'B0': [], 'B1': ['B0'], 'B2': ['B1'], 'B3': ['B1'], 'B4': ['B1','B2','B3'], 'B5': ['B4']}
        plan = {'schema':'bvqa.wave-plan/v1','waves':[{'id':k,'needs':v} for k,v in deps.items()]}
        self.put('migration/wave_plan.json', json.dumps(plan))
        self.assertEqual(self.tool_gate('g_wave_dag')[0], 0)
        plan['waves'][1]['needs'].append('B4')
        self.put('migration/wave_plan.json', json.dumps(plan))
        self.assertEqual(self.tool_gate('g_wave_dag')[0], 5)

    def test_threshold_denied_citation_and_float(self):
        source = (ROOT / 'catalogue/thresholds.tsv').read_text()
        self.put('catalogue/thresholds.tsv', source)
        self.assertEqual(self.tool_gate('g_threshold')[0], 0)
        self.put('catalogue/thresholds.tsv', source + 'BAD\t1\tppm\tBINT\tCITED\tPM-238.4\n')
        self.assertEqual(self.tool_gate('g_threshold')[0], 5)
        self.put('catalogue/thresholds.tsv', source + 'BAD\t0.5\tppm\tBINT\tTARGET\t-\n')
        self.assertEqual(self.tool_gate('g_threshold')[0], 5)

    def test_clock_mutant(self):
        self.put('tools/gates.py', 'x=1\n')
        self.assertEqual(self.tool_gate('g_pure')[0], 0)
        self.put('tools/gates.py', 'import time\nx=time.time()\n')
        self.assertEqual(self.tool_gate('g_pure')[0], 5)

    def test_pinned_wheel_imports_blender_adapter(self):
        cfg = json.loads((ROOT / 'config/vision.json').read_text())
        wheel = ROOT / cfg['engine_path']
        code = "import sys;sys.path.insert(0,sys.argv[1]);import visioncore;from vadapt.blender import from_render_dir as a;assert visioncore.API_VERSION=='1.1.0';assert callable(a.adapt);assert a.__file__.startswith(sys.argv[1])"
        run = subprocess.run([sys.executable,'-I','-B','-c',code,str(wheel)],capture_output=True,text=True,timeout=30)
        self.assertEqual(run.returncode,0,run.stderr)

    def test_router_projects_thresholds_to_real_coordinates(self):
        for query, expected in [('report reconcile','bv.report.reconcile'),('FRAME_CAP','bv.journal.cycles_seed')]:
            run = subprocess.run([sys.executable,'-B',str(ROOT/'tools/route_code.py'),'--root',str(ROOT/'catalogue/router'),'--q',query],capture_output=True,text=True,timeout=15)
            self.assertEqual(run.returncode,0,run.stderr)
            obj = json.loads(run.stdout)
            self.assertEqual(obj['r'][0][0],expected)
            if query == 'FRAME_CAP':
                # threshold owner BDRIVE projects to the journal anchor (build_router_catalogue.OWNER_ANCHOR), not the BINT fallback
                self.assertEqual(obj['r'][0][1],'blender_visual_qa/drive/journal.py')
            for _,path,line,_ in obj['r']:
                self.assertTrue((ROOT/path).is_file())
                self.assertGreater(line,0)
                self.assertLessEqual(line,len((ROOT/path).read_text().splitlines()))

    def test_missing_precondition_never_passes(self):
        rc, obj = self.tool_gate('g_vendor')
        self.assertEqual(rc, 5)
        self.assertIn('precondition', str(obj))


class TestB0Contracts(unittest.TestCase):
    def test_stage_verdict_mapping(self):
        for stage, verdict in [(0,0),(2,2),(3,3),(4,2),(5,1),(6,2)]:
            self.assertEqual(cli.stage_to_verdict(stage), verdict)
        for reason in ('src_mutated','mutant_noop','argv_forbidden'):
            self.assertEqual(cli.stage_to_verdict(5, reason=reason), 2)
        with self.assertRaises(ValueError): cli.stage_to_verdict(1)

    def test_json_rejects_duplicate_and_nan(self):
        for text in ('{"x":1,"x":2}', '{"x":NaN}'):
            with self.assertRaises(contracts.ContractError): contracts.load_json_str(text)
        self.assertEqual(contracts.canonical_json({'b':2,'a':1}), '{"a":1,"b":2}\n')

    def test_schema_rejects_dotted_id(self):
        with self.assertRaises(contracts.ContractError):
            contracts.validate_schema({'schema':'vqa.report.v1'}, 'report')

    def test_report_empty_is_incomplete(self):
        out = report.reconcile(scope='render')
        self.assertEqual(out['rc'], 3)
        self.assertFalse(out['qualified'])
        self.assertEqual(out['metrics']['frames']['provenance'], 'AUSENTE')

    def test_report_pass_fail_and_error_precedence(self):
        base = {'schema':'vqa.selftest-results/v1','mode':'offline','qualified':False,'checks':1,'fails':0,'clean_runs':[],'mutants':[],'fixtures':[],'fp_counts':{'failing_clean_runs':0,'complete_clean_runs':0,'incomplete_clean_runs':0,'failed_observations':0,'executed_observations':0}}
        out = report.reconcile(scope='offline-selftest', selftest=base)
        self.assertEqual(out['rc'], 0)
        base['fails'] = 1
        out = report.reconcile(scope='offline-selftest', selftest=base, incomplete=['missing'])
        self.assertEqual(out['rc'], 1)
        out = report.reconcile(scope='offline-selftest', selftest=base, errors=[{'code':'fixture.error','stage':'report','message':'fixture'}])
        self.assertEqual(out['rc'], 2)

    def test_error_precedes_incomplete(self):
        out = report.reconcile(scope='render', errors=[{'code':'test.error','stage':'report','message':'fixture'}], incomplete=['missing'])
        self.assertEqual((out['rc'], out['verdict']), (2, 'ERROR'))

    def test_raw_judge_output_not_authority(self):
        with self.assertRaises(contracts.ContractError):
            report.reconcile(judge_results={'schema':'vqa.judge-output/v1'})

    def test_cli_stage_usage_and_incomplete_verdict(self):
        for argv, expected in [([],2),(['validate'],2),(['judge'],2),(['run'],3),(['judge','--arm','A','--from-run','unused'],3)]:
            stream = io.StringIO()
            with contextlib.redirect_stdout(stream), contextlib.redirect_stderr(io.StringIO()):
                rc = cli.main(argv)
            self.assertEqual(rc, expected)
            self.assertEqual(json.loads(stream.getvalue())['rc'], expected)

    def test_drive_dispatch_interface(self):
        with patch.object(cli, '_dispatch', return_value=0) as fn:
            self.assertEqual(cli.main(['drive-check','--run','a','--golden','b']), 0)
            fn.assert_called_once_with('blender_visual_qa.drive.launch','check_main',['--run','a','--golden','b'])


class TestB0Judge(unittest.TestCase):
    def fixture(self):
        inp = {'run_id':'run1','batch_id':'batch1','attempt_id':'attempt1','checks':[{'check_id':'bo.scene.visible','evidence_ids':['frame1'],'evidence_requirement':'scene'}], 'media':[{'evidence_id':'frame1','kind':'png'}]}
        att = {'attention_text':'ACDEFH','attention_absent':'NO','attention_video':'red blue'}
        out = {'schema':'vqa.judge-output/v1','run_id':'run1','batch_id':'batch1','attempt_id':'attempt1','model_id':judge.DEFAULT_MODEL_ID,'input_sha256':judge.compute_canonical_sha256(inp),'prompt_sha256':'a'*64,'media_seen':['frame1'],'attention_answers':att.copy(),'results':[{'check_id':'bo.scene.visible','verdict':'PASS','confidence':1,'reason':'visible object','observations':[{'evidence_id':'frame1','time_ms':None,'text':'object is visible','kind':'scene'}]}]}
        return inp, out, {'attention':att,'prompt_sha256':'a'*64}

    def test_positive_response_and_negative_canary(self):
        inp, out, exp = self.fixture()
        result = judge.validate_response(inp,out,exp)
        self.assertTrue(result['valid'], result)
        out['attention_answers']['attention_absent'] = 'YES'
        result = judge.validate_response(inp,out,exp)
        self.assertFalse(result['valid'])

    def test_binding_and_unseen_evidence(self):
        inp, out, exp = self.fixture()
        out['input_sha256'] = 'b'*64
        self.assertFalse(judge.validate_response(inp,out,exp)['valid'])
        inp, out, exp = self.fixture()
        out['media_seen'] = []
        self.assertFalse(judge.validate_response(inp,out,exp)['valid'])

    def test_low_confidence_abstains(self):
        inp, out, exp = self.fixture()
        out['results'][0]['confidence'] = 0.5
        result = judge.validate_response(inp,out,exp)
        self.assertTrue(result['valid'], result)
        self.assertEqual(result['results'][0]['verdict'],'CANNOT_SEE')

    def test_template_and_versioned_schema(self):
        self.assertTrue(judge.load_judge_prompt_template())
        self.assertEqual(judge.load_schema('judge-output.schema.json')['properties']['schema']['const'],'vqa.judge-output/v1')


if __name__ == '__main__':
    unittest.main()
