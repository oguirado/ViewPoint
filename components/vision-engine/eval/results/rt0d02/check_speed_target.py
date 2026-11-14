"""Executed performance target and negative control from the measured samples."""
import copy,hashlib,json,statistics
from pathlib import Path
OUT=Path(__file__).resolve().parent

def check(data):
    assert data['candidate_change_sha256']==hashlib.sha256((OUT.parents[2]/'visioncore/change.py').read_bytes()).hexdigest(), 'stale benchmark source'
    assert len(data['rows'])==72, 'incomplete benchmark matrix'
    assert all(r['output_match'] and 'load1' in r for r in data['rows'])
    rows=[]
    for workers in [1,5]:
        arms={}
        for arm in ['before','after']:
            values=[r['s2_ms_per_frame'] for r in data['rows'] if r['resolution']==[3840,2160] and r['scenario']=='scroll' and r['workers']==workers and r['arm']==arm]
            assert len(values)==3
            arms[arm]=statistics.median(values)
        ratio=arms['before']/arms['after']
        assert ratio>=3, (workers,ratio)
        rows.append({'workers':workers,'before_s2_ms':arms['before'],'after_s2_ms':arms['after'],'speedup':ratio})
    return rows

def main():
    data=json.loads((OUT/'TIMINGS.json').read_text())
    rows=check(data)
    negative=copy.deepcopy(data)
    for row in negative['rows']:
        if row['arm']=='after':
            row['s2_ms_per_frame']=next(r['s2_ms_per_frame'] for r in negative['rows'] if r['resolution']==row['resolution'] and r['scenario']==row['scenario'] and r['workers']==row['workers'] and r['repeat']==row['repeat'] and r['arm']=='before')
    try:check(negative)
    except AssertionError:rejected=True
    else:raise AssertionError('unchanged performance passed the target')
    (OUT/'PERFORMANCE_TARGET.json').write_text(json.dumps({'claim':'MEASURED','target':{'claim':'TARGET','min_speedup':3},'rows':rows,'unchanged_performance_negative_rejected':rejected,'verdict':'PASS'},indent=2)+'\n')
    print('PASS: 4K scroll target for both worker counts; unchanged-performance negative rejected')
if __name__=='__main__':main()
