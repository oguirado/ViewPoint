"""RAM-only before/after benchmark. Run pinned to CPUs 5-9, no concurrent benchmarks."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import statistics
import subprocess
import sys
import time
ROOT=Path(__file__).resolve().parents[3]
sys.path.insert(0,str(ROOT))
from visioncore import pipeline, change
from vadapt.synth import synthetic as syn
OUT=ROOT/'eval/results/rt0d02'
CPUS=list(range(5,10))

def main():
    assert set(os.sched_getaffinity(0))==set(CPUS)
    p=ROOT/'build/room_D/benchmark_baseline.py'
    p.write_bytes(subprocess.check_output(['git','show','a4d95ac:visioncore/change.py'],cwd=ROOT))
    spec=importlib.util.spec_from_file_location('visioncore._s2_bench_baseline',p)
    before=importlib.util.module_from_spec(spec);spec.loader.exec_module(before)
    result={'schema':'vqa.rt0d02.timing/v1','claim':'MEASURED','cpus':CPUS,
      'baseline':'a4d95ac S2; same current S0/S1/S3+ pipeline',
      'candidate_change_sha256':hashlib.sha256((ROOT/'visioncore/change.py').read_bytes()).hexdigest(),
      'method':'60 RAM-resident frames; 3 repeats per arm/workers; alternating order; S2 classify_tiles wall timer; S2 total/60 includes zero cost for first frame; whole analyze wall includes S0-S5; affinity CPU5 for W1, CPUs5-9 for W5',
      'rows':[],'medians':[]}
    for w,h in [(1280,720),(1920,1080),(3840,2160)]:
        for kind in ['clean_static','scroll']:
            frames,_,_=syn.render_scenario(kind,1,w,h,60)
            src=syn.SynthSource(frames,syn.build_manifest(frames,kind,1))
            expected=None
            for workers in [1,5]:
                for rep in range(3):
                    for arm in (['before','after'] if rep%2==0 else ['after','before']):
                        module=before if arm=='before' else change
                        actual=module.classify_tiles
                        spent=[]
                        def timed(*args,**kwargs):
                            start=time.perf_counter_ns()
                            try:return actual(*args,**kwargs)
                            finally:spent.append(time.perf_counter_ns()-start)
                        module.classify_tiles=timed
                        pipeline.change=module
                        os.sched_setaffinity(0,set(CPUS[:workers]))
                        load=os.getloadavg()[0]
                        start=time.perf_counter_ns()
                        try:run=pipeline.analyze(src,workers=workers,cpus=CPUS)
                        finally:
                            elapsed=time.perf_counter_ns()-start
                            module.classify_tiles=actual
                            pipeline.change=change
                        digest={k:hashlib.sha256(v).hexdigest() for k,v in run._blobs().items()}
                        if expected is None:expected=digest
                        assert digest==expected,(w,h,kind,workers,rep,arm)
                        result['rows'].append({'resolution':[w,h],'scenario':kind,'workers':workers,'repeat':rep,'arm':arm,'load1':load,'s2_ms_per_frame':sum(spent)/60e6,'whole_ms_per_frame':elapsed/60e6,'output_match':True})
                        print(w,h,kind,workers,rep,arm,round(sum(spent)/60e6,3),round(elapsed/60e6,3),flush=True)
                        (OUT/'TIMINGS.json').write_text(json.dumps(result,indent=2)+'\n')
                for arm in ['before','after']:
                    rows=[r for r in result['rows'] if r['resolution']==[w,h] and r['scenario']==kind and r['workers']==workers and r['arm']==arm]
                    result['medians'].append({'resolution':[w,h],'scenario':kind,'workers':workers,'arm':arm,'s2_ms_per_frame':statistics.median(r['s2_ms_per_frame'] for r in rows),'whole_ms_per_frame':statistics.median(r['whole_ms_per_frame'] for r in rows)})
            os.sched_setaffinity(0,set(CPUS))
            (OUT/'TIMINGS.json').write_text(json.dumps(result,indent=2)+'\n')
    assert result['candidate_change_sha256']==hashlib.sha256((ROOT/'visioncore/change.py').read_bytes()).hexdigest()
    print('PASS benchmark outputs byte-identical',flush=True)
if __name__=='__main__':main()
