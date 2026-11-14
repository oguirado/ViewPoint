"""Pure integer NumPy raster primitives; no engine descriptors or system fonts."""
from functools import lru_cache
import numpy as np
from .font import draw


def validate(scene):
    def visit(v):
        if isinstance(v, (float, np.floating)):
            raise ValueError('E_GEN_FLOAT: scene contains floating point')
        if isinstance(v, dict):
            for x in v.values(): visit(x)
        elif isinstance(v, (tuple,list)):
            for x in v: visit(x)
    visit(scene)


@lru_cache(maxsize=128)
def _text_mask(seed):
    tile=np.zeros((39,192,3),np.uint8)
    draw(tile,'GUI TEXT '+str(seed%100000),5,4)
    draw(tile,'WINDOW SCROLL 012345',5,20)
    return tile[:,:,0]!=0


@lru_cache(maxsize=8)
def _tiled_mask(w,h,seed):
    # One bounded tile expansion per scene, including every possible phase.
    tile=_text_mask(seed)
    return np.tile(tile,((h+77)//39,(w+383)//192))


def texture(w,h,seed,ox=0,oy=0):
    # Uint32 hash arithmetic defines an unbounded, seed-specific world plane.
    # A power-of-two noise range avoids full-frame integer division.
    x=(np.arange(w,dtype=np.uint32)+np.uint32(ox & 0xffffffff))[None,:]
    y=(np.arange(h,dtype=np.uint32)+np.uint32(oy & 0xffffffff))[:,None]
    z=(x*np.uint32(374761393)) ^ (y*np.uint32(668265263)) ^ np.uint32(seed & 0xffffffff)
    z=(z ^ (z >> np.uint32(13)))*np.uint32(1274126177)
    z ^= z >> np.uint32(16)
    np.bitwise_and(z,31,out=z)
    grey=z.astype(np.uint8)
    bands=((x//8)%3).astype(np.uint8)+((y//13)%3).astype(np.uint8)
    np.subtract(bands,3,out=bands,where=bands>=3)
    bands*=23
    grey+=bands
    grey+=45
    a=np.empty((h,w,3),np.uint8)
    a[:,:,0]=grey
    np.add(grey,12,out=a[:,:,1]); np.add(grey,24,out=a[:,:,2])
    mx,my=ox%192,oy%39
    mask=_tiled_mask(w,h,seed)[my:my+h,mx:mx+w]
    for c,value in enumerate((225,230,238)):
        np.copyto(a[:,:,c],value,where=mask)
    return a


def rect(rgb, box, color):
    x,y,w,h=box
    x0,y0,x1,y1=max(0,x),max(0,y),min(rgb.shape[1],x+w),min(rgb.shape[0],y+h)
    if x1>x0 and y1>y0: rgb[y0:y1,x0:x1]=color


def background(scene):
    a=texture(scene['w'],scene['h'],scene['seed'])
    for run in scene['glyph_runs']:
        draw(a,run['text'],run['x'],run['y'],scale=scene['scale'])
    return a


@lru_cache(maxsize=8)
def _pane(w,h,seed,scale,carrier=False):
    if carrier:
        # The entire line carrier translates, including its textured backing.
        # This also gives the independent pixel verifier unique patch anchors.
        pane=texture(w,h,seed)
        s=max(16,h//3)
        for y in (s//4,s+s//4):
            draw(pane,'GUI TEXT 0123',2,y,scale=max(1,s//8))
        rect(pane,[max(0,w-s//2),s//4,max(2,s//8),2*s],(250,250,250))
    else:
        pane=texture(w,h,seed)
        draw(pane,'WINDOW',4,4,scale=scale)
    pane.flags.writeable=False
    return pane


def _blit(a,pane,x,y):
    h,w=a.shape[:2]; bh,bw=pane.shape[:2]
    x0,y0,x1,y1=max(0,x),max(0,y),min(w,x+bw),min(h,y+bh)
    if x1>x0 and y1>y0:
        a[y0:y1,x0:x1]=pane[y0-y:y1-y,x0-x:x1-x]


def frame(scene,i,base=None):
    kind=scene['kind']; w,h=scene['w'],scene['h']
    if kind.startswith('scroll_'):
        if kind=='scroll_smooth':
            e=scene['easing']; offset=(i//8)*e[-1]+e[i%8]
            ox,oy=0,offset
        else: ox,oy=-i*scene['dx'],-i*scene['dy']
        return texture(w,h,scene['seed'],ox,oy)
    a=(background(scene) if base is None else base).copy()
    if kind=='aa_jitter':
        # Spatial deterministic AA perturbation; each value stays within one LSB of base.
        yy,xx=np.ogrid[:h,:w]
        sign=(((xx*13+yy*7+(scene['seed'] & 255))&1)*2-1).astype(np.int16)
        if i & 1: sign=-sign
        a=(a.astype(np.int16)+sign[:,:,None]).clip(0,255).astype(np.uint8)
    elif kind in ('caret_blink','caret_move','typing'):
        from .scenes import position
        c=scene['caret']; x,y=c['x'],c['y']
        px,py=position(scene,i)
        if kind!='caret_blink' and scene['caret_variant']=='carrier':
            bx,by,bw,bh=scene['carrier']
            _blit(a,_pane(bw,bh,scene['seed']^0xCACE7,scene['scale'],True),bx+px,by+py)
        else:
            visible=kind!='caret_blink' or (i%scene['period']) < (scene['period']+1)//2
            if visible: rect(a,[x+px,y+py,c['w'],c['h']],(250,250,250))
        if kind=='typing' and i>=scene['motion_start']:
            # Independent glyph insertion outside the rigid translating carrier.
            count=1+((i-scene['motion_start'])//scene['motion_step'])%4
            draw(a,'A'*count,8,max(0,h-9*scene['scale']),scale=scene['scale'])
    elif kind=='window_drag':
        from .scenes import position
        x,y,bw,bh=scene['boxes'][0]
        px,py=position(scene,i)
        _blit(a,_pane(bw,bh,scene['seed']^0xABCDEF,scene['scale']),x+px,y+py)
    elif kind=='canvas_anim':
        x,y,bw,bh=scene['boxes'][0]
        a[y:y+bh,x:x+bw]=texture(bw,bh,scene['seed']^(i*7919))
    return a
