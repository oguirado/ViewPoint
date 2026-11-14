"""JSON scene graphs. Motion uses destination-minus-source integer pixels.

Velocity catalogues are reference pixels at s=16; every raster resolution scales
those integers with the engine's public grid rule. Split policy lives in splits.
"""
import hashlib
import json
from .rng import SplitMix64

KINDS = ('static_clean','aa_jitter','scroll_small','scroll_large','scroll_page',
         'scroll_h','scroll_smooth','caret_blink','typing','caret_move','window_drag','canvas_anim')
# Public DEV catalogue; split policy has a single owner.
from .splits import parameter_sets
KIND_PARAMS = parameter_sets('DEV')
EDGE_RULE = 'whole destination tile must equal in-bounds translated previous pixels; exposed or moving-edge tiles are CHANGED'


def build(kind, k, *, w=1920, h=1080, fps=None, n=120, split='DEV'):
    from visioncore.layout import grid_params
    if kind not in KINDS:
        raise ValueError('unknown kind')
    if any(type(v) is not int for v in (k,w,h,n)) or min(w,h,n)<1 or k<0:
        raise ValueError('positive integer dimensions/count and nonnegative seed required')
    params=parameter_sets(split).get(kind,{})
    def pick(name, default):
        values=params.get(name,default)
        return values[k % len(values)]
    s=grid_params(w,h)[0]
    def velocity(name, default):
        return max(1,pick(name,default)*s//16)
    rng=SplitMix64((KINDS.index(kind)<<32)^k)
    salt=rng.u64()
    scale=max(1,min(w//160,h//90))
    box_divisor=pick('box_divisor',[4])
    rect=[w//box_divisor,h//box_divisor,max(8,w//2),max(8,h//2)]
    dx=dy=0
    if kind in ('scroll_small','scroll_large'):
        dy=-velocity('dy',KIND_PARAMS[kind]['dy'])
    elif kind=='scroll_page': dy=-h
    elif kind=='scroll_h': dx=-velocity('dx',KIND_PARAMS[kind]['dx'])
    elif kind in ('caret_move','typing'): dx=velocity('dx',list(range(1,25)))
    elif kind=='window_drag': dx=velocity('dx',[4,8,24]); dy=velocity('dy',[2,6,20])
    if fps is None:
        rates=params.get('fps',[60])
        # Cartesian product: each half-period appears at both rates.
        fps=rates[(k//len(params.get('half_period_ms',[1]))) % len(rates)]
    if type(fps) is not int or fps<1:
        raise ValueError('positive integer fps required')
    half_ms=pick('half_period_ms',[250,500,1000])
    period=max(2,2*half_ms*fps//1000)
    distance=velocity('amplitude',[24])
    easing=[(j*j*distance)//64 for j in range(9)]
    # A translating text carrier has interior full tiles at either grid size.
    carrier=[max(0,(w//2-3*s)//s*s),max(0,(h//2-s)//s*s),min(w,6*s),min(h,3*s)]
    variant='carrier' if (k//len(params.get('dx',list(range(1,25)))))%2==0 else 'texture'
    scene={'schema':'vqa.gui_synth.scene/v1','kind':kind,'k':k,'split':split,'w':w,'h':h,
           'fps':fps,'n':n,'seed':salt,'scale':scale,'grid_s':s,'dx':dx,'dy':dy,
           'half_period_ms':half_ms,'period':period,'easing':easing,
           'motion_step':3,'motion_start':1,'motion_end':n,
           'caret_variant':variant,'carrier':carrier,
           'edge_rule':EDGE_RULE,'layers':[{'id':'background','op':'static'},
           {'id':'content','op':kind}], 'boxes':[rect],
           'glyph_runs':[{'text':'GUI SYNTH '+str(salt%1000000),'x':8,'y':8}],
           'caret':{'x':w//3,'y':h//2,'w':2*scale,'h':13*scale},
           'events':[{'f0':1,'f1':n,'op':kind,'mv':[dx,dy],'step':3}]}
    scene['param_bucket']={key:pick(key,[]) for key in params if key!='fps'}
    if kind=='caret_blink': scene['param_bucket']['fps']=fps
    if kind in ('caret_move','typing'): scene['param_bucket']['variant']=variant
    scene['scene_id']=split+':'+kind+':'+str(k)+':'+hashlib.sha256(json.dumps(scene,sort_keys=True).encode()).hexdigest()[:16]
    return scene


def position(scene,i):
    """Bounded ping-pong displacement; no object drifts out of the viewport."""
    if scene['kind'] not in ('caret_move','typing','window_drag') or i<scene['motion_start']:
        return 0,0
    i=min(i,scene['motion_end']-1)
    steps=(i-scene['motion_start'])//scene['motion_step']+1
    # The same displacement table is used by rendering and graph motion.
    if scene['kind']=='window_drag': box=scene['boxes'][0]
    elif scene['caret_variant']=='carrier': box=scene['carrier']
    else:
        c=scene['caret']; box=[c['x'],c['y'],c['w'],c['h']]
    x,y,bw,bh=box
    limits=[4]
    for delta,room in ((scene['dx'],scene['w']-x-bw),(scene['dy'],scene['h']-y-bh)):
        if delta: limits.append(max(1,room//abs(delta)))
    span=min(limits)
    phase=steps%(2*span)
    amount=phase if phase<=span else 2*span-phase
    return amount*scene['dx'],amount*scene['dy']


def motion(scene, i):
    kind=scene['kind']
    if not i: return 0,0
    if kind=='scroll_smooth':
        e=scene['easing']; j=(i-1)%8
        return 0,-(e[j+1]-e[j])
    if kind.startswith('scroll_'): return scene['dx'],scene['dy']
    if kind in ('caret_move','typing','window_drag'):
        x,y=position(scene,i); px,py=position(scene,i-1)
        return x-px,y-py
    return 0,0
