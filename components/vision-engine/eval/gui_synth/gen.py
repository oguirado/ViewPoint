"""Streaming synthesis; the only frame writer is a validated RAM workspace."""
import hashlib
import numpy as np
from . import raster


def render(scene):
    raster.validate(scene)
    kind=scene['kind']; w,h=scene['w'],scene['h']
    base=None if kind.startswith('scroll_') else raster.background(scene)
    jitter=None
    if kind=='aa_jitter':
        jitter=[raster.frame(scene,i,base) for i in range(2)]
    prev=None; ox=oy=0
    for i in range(scene['n']):
        if jitter is not None:
            cur=jitter[i%2].copy()
        elif kind.startswith('scroll_') and i:
            from .scenes import motion
            dx,dy=motion(scene,i); ox-=dx; oy-=dy
            if abs(dx)>=w or abs(dy)>=h:
                cur=raster.texture(w,h,scene['seed'],ox,oy)
            else:
                cur=np.empty_like(prev)
                x0,x1=max(0,dx),min(w,w+dx)
                y0,y1=max(0,dy),min(h,h+dy)
                cur[y0:y1,x0:x1]=prev[y0-dy:y1-dy,x0-dx:x1-dx]
                if y0: cur[:y0]=raster.texture(w,y0,scene['seed'],ox,oy)
                if y1<h: cur[y1:]=raster.texture(w,h-y1,scene['seed'],ox,oy+y1)
                if x0: cur[y0:y1,:x0]=raster.texture(x0,y1-y0,scene['seed'],ox,oy+y0)
                if x1<w: cur[y0:y1,x1:]=raster.texture(w-x1,y1-y0,scene['seed'],ox+x1,oy+y0)
        else:
            cur=raster.frame(scene,i,base)
        yield i,cur
        prev=cur


def to_ram(scene, ws):
    from vadapt.ramws import RamWorkspace, RamError, is_ram_fs
    if not isinstance(ws,RamWorkspace) or ws.tool!='gui_synth' or not is_ram_fs(ws.root):
        raise RamError('E_RAM_NOT_TMPFS','gui_synth RamWorkspace required')
    raster.validate(scene)
    rel='frames.rgb24'
    size=scene['w']*scene['h']*3*scene['n']
    path=ws.path(rel)
    if path.exists():
        raise FileExistsError(path)
    ws.reserve(size)
    hashes=[]; created=False
    try:
        with open(path,'xb') as out:
            created=True
            for _,rgb in render(scene):
                data=rgb.tobytes(); out.write(data)
                hashes.append(hashlib.sha256(data).hexdigest())
    except Exception:
        if created and path.exists(): ws.release(rel)
        ws.account()
        raise
    return rel,hashes


class Src:
    """Pipeline Frame source. RAM form: Src(scene, ws, rel) or Src((scene,ws,rel))."""
    raw_array=None

    def __init__(self, scene, ws=None, rel='frames.rgb24'):
        if isinstance(scene,tuple): scene,ws,rel=scene
        self.scene,self.ws,self.rel=scene,ws,rel
        self.meta={'width':scene['w'],'height':scene['h'],'storage':'mem','count':scene['n']}

    def __iter__(self):
        from visioncore.frame import Frame
        if self.ws is None:
            frames=render(self.scene)
        else:
            from vadapt.ramws import RamWorkspace, RamError, is_ram_fs
            if not isinstance(self.ws,RamWorkspace) or self.ws.tool!='gui_synth' or not is_ram_fs(self.ws.root):
                raise RamError('E_RAM_NOT_TMPFS','gui_synth workspace required')
            s=self.scene
            a=np.memmap(self.ws.path(self.rel),dtype=np.uint8,mode='r',shape=(s['n'],s['h'],s['w'],3))
            frames=enumerate(a)
        for i,rgb in frames:
            yield Frame(i,i*1000000//self.scene['fps'],i,rgb,hashlib.sha256(rgb.tobytes()).hexdigest())
