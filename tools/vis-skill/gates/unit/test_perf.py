#!/usr/bin/env python3
"""Performance/runtime regression gates; all process fixtures belong to the test."""
import contextlib
import importlib.machinery
import importlib.util
import os
import hashlib
import json
import struct
import time
import zlib
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[2]
loader=importlib.machinery.SourceFileLoader('vis_perf',os.environ.get('VIS_PERF_SOURCE',str(ROOT/'vis')))
spec=importlib.util.spec_from_loader(loader.name,loader)
core=importlib.util.module_from_spec(spec); loader.exec_module(core)

def png(width=2,height=1,value=0,filter_type=0):
    def chunk(k,b): return struct.pack('>I',len(b))+k+b+struct.pack('>I',zlib.crc32(k+b)&0xffffffff)
    row=bytes([value])*(width*3)
    if filter_type==1: row=row[:3]+b'\0'*(len(row)-3)
    raw=(bytes([filter_type])+row)*height
    return b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('>IIBBBBB',width,height,8,2,0,0,0))+chunk(b'IDAT',zlib.compress(raw))+chunk(b'IEND',b'')

def act_probe(width=2,height=1,settle=800,capture_cost=0,real_clock=False,evict_describe=False,mask=False,sleep_jitter=0):
    """Real do_act/pixels/evidence/receipts; isolated files and scripted target I/O."""
    with tempfile.TemporaryDirectory() as tmp, contextlib.ExitStack() as stack:
        root=Path(tmp); clock=[100.]; captures=[]
        args=core.parser().parse_args(['act','--job','perf','--key','space','--expect-change','none','--settle-ms',str(settle)])
        if mask: args.mask=['0,0,2,1']
        job=dict(job='perf',ownership='started',target_kind='web',display='web:perf',steps={},engines={},pids=[])
        def capture(j):
            index=len(captures); captures.append(clock[0]); clock[0]+=capture_cost if index else 0
            path=root/('frame_'+str(index)+'.png')
            # Unique file bytes, identical visible pixels (ancillary chunk differs).
            data=png(width,height,value=123 if mask and index else 0,filter_type=1)
            body=str(index).encode(); chunk=struct.pack('>I',len(body))+b'tEXt'+body+struct.pack('>I',zlib.crc32(b'tEXt'+body)&0xffffffff)
            path.write_bytes(data[:-12]+chunk+data[-12:]); return path
        for name,value in [('ARGS',args),('RECEIPTED',False),('START',time.monotonic() if real_clock else 100.)]:
            stack.enter_context(patch.object(core,name,value))
        for name,value in [('load',job),('jobpath',root),('save',None),('target',None),('rpc',{'verified':True}),('responsive',True)]:
            stack.enter_context(patch.object(core,name,return_value=value))
        for name in ('lifecycle','actor'):
            stack.enter_context(patch.object(core,name,side_effect=lambda *a,**kw:contextlib.nullcontext()))
        stack.enter_context(patch.dict(os.environ,{'MV_HOSTED':'0'}))
        stack.enter_context(patch.object(core,'capture',side_effect=capture))
        def describe(j,f,*a,**kw):
            result=dict(changed_regions=[],frame_sha256=core.frame_sha(f))
            if evict_describe: core._PIXELS_CACHE.clear()
            return result
        stack.enter_context(patch.object(core,'describe',side_effect=describe))
        if not real_clock:
            stack.enter_context(patch.object(core.time,'monotonic',side_effect=lambda:clock[0]))
            stack.enter_context(patch.object(core.time,'sleep',side_effect=lambda t:clock.__setitem__(0,clock[0]+t+sleep_jitter*len(captures))))
        t0=time.perf_counter(); result=core.do_act(args); elapsed=time.perf_counter()-t0
        return result,captures,elapsed

class Perf(unittest.TestCase):
    def test_p2_fixed_ticks_and_slow_capture(self):
        result,captures,_=act_probe(capture_cost=.01)
        for started in captures[1:]:
            tick=(started-100.)/.04
            self.assertAlmostEqual(tick,round(tick),places=6)
        self.assertAlmostEqual(captures[2]-captures[1],.04,places=6)
        self.assertFalse(result['receipt']['sampling']['settle_degraded'])
        result,captures,_=act_probe(capture_cost=.12)
        sampling=result['receipt']['sampling']
        self.assertGreater(sampling['slow_factor'],2)
        self.assertTrue(sampling['settle_degraded'])
        self.assertLessEqual(len(captures)-1,7)
        self.assertLessEqual(captures[-1]-100.,.8+1e-8)
        result,captures,_=act_probe(settle=0)
        self.assertEqual(len(captures)-1,1)
        self.assertEqual(result['receipt']['sampling']['slow_factor'],1)
        result,_,_=act_probe(settle=0,capture_cost=.0800000001)
        sampling=result['receipt']['sampling']
        self.assertEqual(sampling['settle_degraded'],sampling['slow_factor']>2)

    def test_p1_decode_bound_and_sample_budget(self):
        decode=core.zlib.decompress
        with patch.object(core.zlib,'decompress',wraps=decode) as calls:
            result,captures,_=act_probe()
        self.assertLessEqual(calls.call_count,len(captures))
        sampling=result['receipt']['sampling']
        self.assertEqual(len(sampling['samples']),len(captures)-1)
        for item in sampling['samples']:
            self.assertGreaterEqual(item['decode_ms'],0)
            self.assertEqual(item['budget_violation'],item['decode_ms']>sampling['spacing_ms'])
        # Decoding that exceeds a tick must be visible to the caller.
        decode_pixels=core.pixels
        def slow(path):
            core.time.sleep(.06); return decode_pixels(path)
        with patch.object(core,'pixels',side_effect=slow):
            result,_,_=act_probe(settle=100)
        self.assertTrue(any(s['budget_violation'] for s in result['receipt']['sampling']['samples']))
        def boundary(path):
            core.time.sleep(.0400000001); return decode_pixels(path)
        with patch.object(core,'pixels',side_effect=boundary):
            result,_,_=act_probe(settle=0)
        sampling=result['receipt']['sampling']
        for item in sampling['samples']:
            self.assertEqual(item['budget_violation'],item['decode_ms']>sampling['spacing_ms'])

    def test_final_receipt_reuses_raw_description_hash(self):
        for mask in (False,True):
            with self.subTest(mask=mask), patch.object(core,'frame_sha',wraps=core.frame_sha) as hashes,                  patch.object(core.zlib,'decompress',wraps=core.zlib.decompress) as decodes:
                core._PIXELS_CACHE.clear()
                result,captures,_=act_probe(evict_describe=True,mask=mask)
                self.assertEqual(result['receipt']['frame_sha_after'],result['frame_sha256'])
                expected=hashlib.sha256(bytes([123 if mask else 0])*6).hexdigest()
                self.assertEqual(result['receipt']['frame_sha_after'],expected)
                self.assertEqual(hashes.call_count,1,'only describe needs the raw hash')
                self.assertEqual(decodes.call_count,len(captures))

    def test_slow_factor_excludes_intentional_skipped_ticks(self):
        for cost in (.0401,.045,.079):
            with self.subTest(cost=cost):
                result,_,_=act_probe(capture_cost=cost,sleep_jitter=.000001)
                sampling=result['receipt']['sampling']
                self.assertLess(sampling['slow_factor'],2)
                self.assertFalse(sampling['settle_degraded'])
        # A first sample beyond the deadline must still report slow work.
        result,_,_=act_probe(settle=50,capture_cost=.12)
        self.assertGreater(result['receipt']['sampling']['slow_factor'],2)
        self.assertTrue(result['receipt']['sampling']['settle_degraded'])
        # Unscheduled wake-up lateness is degradation too.
        result,_,_=act_probe(settle=50,sleep_jitter=.12)
        self.assertTrue(result['receipt']['sampling']['settle_degraded'])

    def test_p1_content_lru(self):
        decode=core.zlib.decompress
        with tempfile.TemporaryDirectory() as tmp, patch.object(core.zlib,'decompress',wraps=decode) as calls:
            path=Path(tmp)/'frame.png'; path.write_bytes(png(value=123))
            first=core.pixels(path); n=calls.call_count
            other=Path(tmp)/'copy.png';other.write_bytes(path.read_bytes())
            self.assertEqual(core.pixels(other),first)
            self.assertEqual(calls.call_count,n)
            # A reused pathname must not reuse stale pixels.
            path.write_bytes(png(value=124)); self.assertNotEqual(core.pixels(path),first)
            for value in range(130,136):
                path.write_bytes(png(value=value)); core.pixels(path)
            self.assertLessEqual(len(core._PIXELS_CACHE),4)
            before_reread=calls.call_count
            path.write_bytes(png(value=123)); self.assertEqual(core.pixels(path),first)
            self.assertEqual(calls.call_count,before_reread+1)

    def test_p1_lru_hit_updates_recency(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(core.zlib,'decompress',wraps=core.zlib.decompress) as calls:
            core._PIXELS_CACHE.clear()
            paths=[Path(tmp)/('frame_'+str(i)+'.png') for i in range(5)]
            for i,path in enumerate(paths): path.write_bytes(png(value=200+i))
            for path in paths[:4]: core.pixels(path)
            core.pixels(paths[0])  # A becomes newer than B.
            core.pixels(paths[4])  # E evicts B.
            count=calls.call_count
            core.pixels(paths[0]); self.assertEqual(calls.call_count,count)
            core.pixels(paths[1]); self.assertEqual(calls.call_count,count+1)

    def test_adopt_requires_display_operand(self):
        # Entirely synthetic process metadata: no live PID is read or signalled.
        for command,accepted in ((b'/usr/bin/Xvfb\0:77\0-screen\0' + b'0\0' + b'1280x800x24\0',True),
                                 (b'/usr/bin/Xvfb\0:77.0\0',True),
                                 (b'/usr/bin/Xvfb\0:78\0-auth\0:77\0',False)):
            with self.subTest(command=command), tempfile.TemporaryDirectory() as tmp, \
                 patch.object(core,'jobpath',return_value=Path(tmp)/'job'), \
                 patch.object(core,'identity',return_value='synthetic'), patch.object(core,'alive',return_value=True), \
                 patch.object(Path,'read_bytes',return_value=command), patch.object(core,'engines',return_value={}), patch.object(core,'desktop_env',return_value={}), \
                 patch.object(core,'run',return_value=core.sp.CompletedProcess([],0,b'1280 800',b'')), \
                 patch.object(core,'save'), patch.object(core,'receipt'), patch.object(core,'CURRENT',None):
                args=core.parser().parse_args(['adopt','--job','job','--display',':77.0','--xvfb-pid','12345'])
                if accepted:
                    self.assertEqual(core._dispatch(args)['display'],':77.0')
                else:
                    with self.assertRaises(core.Failure) as error: core._dispatch(args)
                    self.assertEqual(error.exception.rc,5)
                    self.assertFalse((Path(tmp)/'job').exists())

    def test_p0_alias_lock(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(core,'BASE',Path(tmp)):
            for first,second in ((':77',':77.0'),(':77.0',':77'),(':77.1',':77.0')):
                with self.subTest(first=first,second=second), core.actor(dict(display=first,ownership='started')):
                    with self.assertRaises(core.Failure) as error:
                        with core.actor(dict(display=second,ownership='attached')): pass
                    self.assertEqual(error.exception.rc,5)
                    with core.actor(dict(display=':78.0',ownership='started')): pass

if __name__=='__main__':
    if '--benchmark' in sys.argv:
        with patch.object(core.zlib,'decompress',wraps=core.zlib.decompress) as decodes:
            result,captures,elapsed=act_probe(1920,1080,settle=0,real_clock=True)
        print(json.dumps(dict(elapsed_ms=round(elapsed*1000,3),samples=len(captures)-1,decodes=decodes.call_count,frame_sha=result['frame_sha256'])))
    else: unittest.main()
