import argparse, cProfile, hashlib, importlib.util, json, os, pstats, subprocess, sys, time
from collections import defaultdict
from pathlib import Path
ROOT=Path(__file__).resolve().parents[3]
sys.path.insert(0,str(ROOT))
from visioncore import pipeline, change
from vadapt.synth import synthetic as syn
OUT=ROOT/'eval/results/rt0d02'
parser=argparse.ArgumentParser()
parser.add_argument('--arm', choices=['before','after'], required=True)
args=parser.parse_args()
assert set(os.sched_getaffinity(0))==set(range(5,10))
if args.arm=='before':
    path=ROOT/'build/room_D/profile_baseline_change.py'
    path.write_bytes(subprocess.check_output(['git','show','a4d95ac:visioncore/change.py'],cwd=ROOT))
    spec=importlib.util.spec_from_file_location('visioncore._profile_baseline_change',path)
    change=importlib.util.module_from_spec(spec);spec.loader.exec_module(change)
    pipeline.change=change

rows=[]
for w,h in [(1920,1080),(3840,2160)]:
    frames,_,_=syn.render_scenario('scroll',1,w,h,16)
    frames=frames[:8]
    src=syn.SynthSource(frames,syn.build_manifest(frames,'profile_scroll',1))
    for rep in range(3):
        prof=cProfile.Profile(); prof.enable()
        start=time.perf_counter(); pipeline.analyze(src,workers=1,cpus=list(range(5,10))); elapsed=time.perf_counter()-start
        prof.disable()
        stats=pstats.Stats(prof)
        funcs=[{'line':k[1],'function':k[2],'calls':v[1],'self_s':v[2],'cumulative_s':v[3]} for k,v in stats.stats.items() if k[0]==change.__file__]
        times=defaultdict(int); counts=defaultdict(int)
        filename=change.__file__
        clocks={}
        def lines(f,event,arg):
            if f.f_code.co_filename!=filename: return None
            key=id(f); now=time.perf_counter_ns()
            if key in clocks:
                line,t0=clocks[key]; times[(f.f_code.co_name,line)]+=now-t0; counts[(f.f_code.co_name,line)]+=1
            if event=='return': clocks.pop(key,None)
            else: clocks[key]=(f.f_lineno,now)
            return lines
        sys.settrace(lines)
        try: pipeline.analyze(src,workers=1,cpus=list(range(5,10)))
        finally: sys.settrace(None)
        rows.append({'resolution':[w,h],'repeat':rep,'frames':8,'load1':os.getloadavg()[0], 'wall_ms_per_frame':elapsed*1000/8,'functions':funcs,'line_timings':[{'function':k[0],'line':k[1],'ms':v/1e6,'hits':counts[k]} for k,v in sorted(times.items(),key=lambda x:-x[1])[:40]]})
        OUT.joinpath('PROFILE_'+args.arm.upper()+'.json').write_text(json.dumps({'claim':'MEASURED','source_sha256':hashlib.sha256(Path(change.__file__).read_bytes()).hexdigest(),'cpus':sorted(os.sched_getaffinity(0)), 'method':'cProfile and separate sys.settrace line timings, eight RAM-resident frames per repeat; line times include nested callees','rows':rows},indent=2)+'\n')
        print(w,h,rep,round(elapsed*1000/8,2),flush=True)
