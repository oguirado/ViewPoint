#!/usr/bin/env python3
"""Production preflight without launching a session or touching a real display."""
import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
import subprocess
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[2]
loader=importlib.machinery.SourceFileLoader('vis_runtime',os.environ.get('VIS_RUNTIME_SOURCE',str(ROOT/'vis')))
spec=importlib.util.spec_from_loader(loader.name,loader)
core=importlib.util.module_from_spec(spec);loader.exec_module(core)

def diagnostic_cli(command, **kwargs):
    # Independent test guard: never let a preflight regression launch production start.
    if 'start' in command:
        raise AssertionError('runtime CLI fixture attempted production vis start')
    return subprocess.run(command, **kwargs)

class Runtime(unittest.TestCase):
    def test_p3_cli_missing_runtime(self):
        env=os.environ.copy()
        home=tempfile.TemporaryDirectory(prefix='vision-runtime-home-')
        self.addCleanup(home.cleanup)
        env['HOME']=home.name
        for name in ('XDG_RUNTIME_DIR','NEWTON_TEST_PIDNS'): env.pop(name,None)
        for args in (['doctor'],['doctor','--skill']):
            with self.subTest(args=args):
                result=diagnostic_cli([sys.executable,'-I',str(ROOT/'vis'),*args],env=env,capture_output=True,timeout=20)
                self.assertEqual(result.returncode,3 if '--skill' in args else 0,result.stderr)
                data=json.loads(result.stdout)
                self.assertIn('XDG_RUNTIME_DIR',' '.join(data['missing']['session_runtime']))
                for key in ('tools','engines','capabilities','responsiveness','ram_available'):
                    self.assertIn(key,data)
                if '--skill' in args: self.assertIn('drift',data)
        with tempfile.TemporaryDirectory() as runtime:
            env.update(XDG_RUNTIME_DIR=runtime,DBUS_SESSION_BUS_ADDRESS='unix:path='+runtime+'/missing-bus')
            result=diagnostic_cli([sys.executable,'-I',str(ROOT/'vis'),'doctor'],env=env,capture_output=True,timeout=20)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertIn('user bus',' '.join(json.loads(result.stdout)['missing']['session_runtime']))

    def test_p3_start_refuses_before_engines_or_job_creation(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(core,'test_pidns_mode',return_value=False), \
             patch.dict(os.environ,{},clear=True), patch.object(core,'jobpath',return_value=Path(tmp)/'job'), \
             patch.object(core,'engines',side_effect=AssertionError('preflight ran too late')), \
             patch.object(core,'CURRENT',None), patch.object(core.sys,'argv',['vis','start','--job','job','--app','true']):
            output=io.StringIO()
            with contextlib.redirect_stdout(output): rc=core.main()
            result=json.loads(output.getvalue())
            self.assertEqual(rc,7)
            self.assertEqual(result['cause'],'session_runtime_absent')
            self.assertIn('XDG_RUNTIME_DIR',result['prerequisite'])
            self.assertFalse((Path(tmp)/'job').exists())

    def test_p3_runtime_and_bus_probe(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ,{'XDG_RUNTIME_DIR':tmp}), \
             patch.object(core.shutil,'which',return_value='/test/systemctl'), \
             patch.object(core,'run',return_value=core.sp.CompletedProcess([],1,b'',b'bus absent')) as probe:
            missing=core.session_runtime_missing()
            self.assertTrue(any('user bus' in item for item in missing))
            self.assertEqual(probe.call_args.args[0],['systemctl','--user','show','--property=Version','--value'])
            probe.return_value=core.sp.CompletedProcess([],0,b'260\n',b'')
            self.assertEqual(core.session_runtime_missing(),[])
        with patch.dict(os.environ,{'XDG_RUNTIME_DIR':'/absent-vis-runtime'}), patch.object(core,'run') as probe:
            self.assertTrue(any('XDG_RUNTIME_DIR' in item for item in core.session_runtime_missing()))
            probe.assert_not_called()

    def test_p3_doctor_reports_runtime_in_missing(self):
        for argv in (['doctor'],['doctor','--skill']):
            args=core.parser().parse_args(argv)
            with self.subTest(argv=argv), tempfile.TemporaryDirectory() as tmp, \
                 patch.object(core,'test_pidns_mode',return_value=False), patch.dict(os.environ,{},clear=True), \
                 patch.object(Path,'home',return_value=Path(tmp)), \
                 patch.object(core,'engines',return_value=dict(ve=tmp,wender=tmp)), \
                 patch.object(core.shutil,'which',return_value='/synthetic/tool'), \
                 patch.object(core,'engine_env',return_value={}), \
                 patch.object(core,'run',return_value=core.sp.CompletedProcess([],0,b'--frame-raw',b'')):
                result=core._dispatch(args)
                self.assertIn('XDG_RUNTIME_DIR',' '.join(result['missing']['session_runtime']))
                self.assertTrue(result['session_runtime']['scope_required'])
                for key in ('tools','engines','capabilities','responsiveness','ram_available','tessdata','state_fields','hosted_judge'):
                    self.assertIn(key,result)
                if args.skill: self.assertIn('drift',result)

if __name__=='__main__': unittest.main()
