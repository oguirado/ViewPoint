"""Exact labels, and a separate pixel verifier. No engine classification imports.

Frame intervals are half-open [f0, f1). Tile IDs are Morton ranks (grid.order), not Morton
codes. Only changed tile-frames are emitted; all others are UNCHANGED. The first
frame has no predecessor. Translation sign is destination minus source.
"""
import numpy as np
from .scenes import motion

UNCHANGED,JITTER,CHANGED,MOVED,FLICKER,FROZEN,DRIFT=range(7)
NAMES=('UNCHANGED','JITTER','CHANGED','MOVED','FLICKER','FROZEN','DRIFT')


def _bounds(grid,t):
    x,y=int(grid.tx[t])*grid.s,int(grid.ty[t])*grid.s
    return x,y,min(x+grid.s,grid.W),min(y+grid.s,grid.H)


def _shift_equal(prev,cur,box,mv):
    x,y,x1,y1=box; dx,dy=mv
    if (dx,dy)==(0,0) or min(x-dx,y-dy)<0 or x1-dx>prev.shape[1] or y1-dy>prev.shape[0]:
        return False
    return np.array_equal(cur[y:y1,x:x1],prev[y-dy:y1-dy,x-dx:x1-dx])


def _expected(scene,ideal,i):
    kind=scene['kind']
    if kind.startswith('scroll_'):
        if kind=='scroll_small' and abs(motion(scene,i)[1])<=8 and ideal==MOVED:
            return MOVED,'HYP'
        return CHANGED,'MEASURED-W3'
    if kind=='caret_blink':
        return CHANGED,('MEASURED-W3' if scene['half_period_ms']==500 else 'ER-014')
    if kind=='caret_move' and abs(scene['dx'])==12:
        return CHANGED,'MEASURED-W3'
    return ideal,'HYP'


def derive(scene, grid):
    """Rasterize graph primitives to determine exact tile coverage and exposed edges."""
    from .gen import render
    if (grid.W,grid.H)!=(scene['w'],scene['h']):
        raise ValueError('E_LABEL_TILE: grid dimensions')
    events=[]; prev=None
    # Blink has exactly two raster states; keep their reference frames, not a movie.
    blink_refs={}
    for i,cur in render(scene):
        if prev is None:
            prev=cur
            if scene['kind']=='caret_blink': blink_refs[True]=cur
            continue
        mv=motion(scene,i); groups={}
        for t in range(grid.N):
            box=_bounds(grid,t); x,y,x1,y1=box
            a,b=prev[y:y1,x:x1],cur[y:y1,x:x1]
            if np.array_equal(a,b): continue
            ideal=CHANGED; vector=None
            if scene['kind']=='aa_jitter' and np.max(np.abs(a.astype(np.int16)-b.astype(np.int16)))<=2:
                ideal=JITTER
            elif _shift_equal(prev,cur,box,mv):
                ideal=MOVED; vector=list(mv)
            elif scene['kind']=='caret_blink' and i>=scene['period']:
                visible=i%scene['period'] < (scene['period']+1)//2
                ref=blink_refs.get(visible)
                if ref is not None and np.array_equal(b,ref[y:y1,x:x1]): ideal=FLICKER
            groups.setdefault((ideal,tuple(vector) if vector else None),[]).append(t)
        for (ideal,vector),tiles in groups.items():
            expected,basis=_expected(scene,ideal,i)
            events.append({'schema':'vqa.gui_synth.label/v1','scene_id':scene['scene_id'],
                'event_id':scene['scene_id']+':'+str(i)+':'+str(ideal),
                'f0':i,'f1':i+1,'tiles':tiles,'ideal':ideal,'v1_expected':expected,
                'v1_basis':basis,'mv':list(vector) if vector else None,'op':scene['kind'],
                'edge_rule':scene['edge_rule']})
        if scene['kind']=='caret_blink':
            visible=i%scene['period'] < (scene['period']+1)//2
            blink_refs.setdefault(visible,cur)
        prev=cur
    return events


def _pixel_translations(prev,cur,known):
    """Discover translation candidates from exact 3x3 pixel patches, no graph motion.

    Reuse previously pixel-proven candidates, then search changed anchor patches.
    Two diagonal pixels filter locations; all nine pixels must subsequently match.
    Solid patches are excluded because their translations are ambiguous.
    """
    h,w=cur.shape[:2]; out=set(known)
    if h<3 or w<3: return out
    # Interior and edge anchors cover both a scrolling plane and a central pane.
    anchors=[(0,0),(w//2,h//2),(w//4,h//4),(3*w//4,3*h//4),(w-3,h-3)]
    # Small translating carriers can miss every fixed viewport anchor. Probe
    # their changed-pixel bounds without consulting graph geometry or labels.
    changed=np.any(prev!=cur,axis=2)
    rows=np.flatnonzero(changed.any(axis=1)); cols=np.flatnonzero(changed.any(axis=0))
    if rows.size and cols.size:
        for fy in (1,2,3,4,5):
            for fx in (1,2,3,4,5):
                anchors.append((int(cols[0]+(cols[-1]-cols[0])*fx//6),
                                int(rows[0]+(rows[-1]-rows[0])*fy//6)))
    packed=None
    for x,y in anchors:
        x,y=min(x,w-3),min(y,h-3)
        patch=cur[y:y+3,x:x+3]
        if np.array_equal(patch,prev[y:y+3,x:x+3]): continue
        if np.unique(patch.reshape(-1,3),axis=0).shape[0]<3: continue
        if any(0<=x-dx<=w-3 and 0<=y-dy<=h-3 and
               np.array_equal(patch,prev[y-dy:y-dy+3,x-dx:x-dx+3]) for dx,dy in out):
            return out
        if packed is None:
            packed=prev[:,:,0].astype(np.uint32)
            packed |= prev[:,:,1].astype(np.uint32)<<8
            packed |= prev[:,:,2].astype(np.uint32)<<16
        pixel=int(patch[0,0,0]) | int(patch[0,0,1])<<8 | int(patch[0,0,2])<<16
        mask=packed[:-2,:-2]==pixel
        pixel2=int(patch[2,2,0]) | int(patch[2,2,1])<<8 | int(patch[2,2,2])<<16
        mask &= packed[2:,2:]==pixel2
        yy,xx=np.nonzero(mask)
        for sy,sx in zip(yy,xx):
            if (sx!=x or sy!=y) and np.array_equal(patch,prev[sy:sy+3,sx:sx+3]):
                out.add((x-int(sx),y-int(sy)))
                return out
    return out


def self_check(scene, frames, events):
    """Verify every changed tile using supplied pixels, not derive() or graph events.

    Pixel-discovered translation precedes generic CHANGED. Period equality reads
    supplied historical pixels. Geometry/graph motion is never consulted. Checks
    operate on visible edge-tile pixels; no fabricated padding samples are used.
    """
    from visioncore.grid import Grid
    grid=Grid(scene['w'],scene['h']); errors=[]; by_frame={}
    valid_classes=set(range(7))
    for event in events:
        if event.get('scene_id')!=scene['scene_id']:
            errors.append(('E_LABEL_TILE','scene_id mismatch')); continue
        try:
            f0,f1=event['f0'],event['f1']; tiles=event['tiles']
            if type(f0) is not int or type(f1) is not int or not 1<=f0<f1<=scene['n']:
                raise ValueError('frame range')
            if event['ideal'] not in valid_classes: raise ValueError('class')
            for i in range(f0,f1):
                row=by_frame.setdefault(i,{})
                for t in tiles:
                    if type(t) is not int or not 0<=t<grid.N or t in row:
                        errors.append(('E_LABEL_TILE',f'invalid/duplicate tile {t} frame {i}')); continue
                    row[t]=event
        except (KeyError,TypeError,ValueError) as exc:
            errors.append(('E_LABEL_TILE',str(exc)))
    prev=None; refs={}; known=set(); count=0; stable_ref=None
    period=2*scene['half_period_ms']*scene['fps']//1000 if scene['kind']=='caret_blink' else 0
    for item in frames:
        if isinstance(item,tuple): i,cur=item
        elif hasattr(item,'rgb'): i,cur=item.i,item.rgb
        else: i,cur=count,item
        if i!=count or cur.shape!=(scene['h'],scene['w'],3) or cur.dtype!=np.uint8:
            errors.append(('E_LABEL_TILE','frame sequence/shape/dtype')); break
        count+=1
        if prev is None:
            prev=cur
            if period: refs[0]=cur; stable_ref=cur
            continue
        row=by_frame.get(i,{})
        if period and i>=period and not np.array_equal(cur,refs[i%period]):
            errors.append(('E_LABEL_CLASS',f'frame {i}: pixels violate canonical blink period'))
        # Discovery uses only changed frames; skip the expensive search for bounded jitter.
        delta=np.abs(cur.astype(np.int16)-prev.astype(np.int16))
        changed=bool(np.any(delta))
        jitter=changed and int(delta.max())<=2
        if changed and not jitter: known=_pixel_translations(prev,cur,known)
        for t in range(grid.N):
            x,y,x1,y1=_bounds(grid,t); a=prev[y:y1,x:x1]; b=cur[y:y1,x:x1]
            differs=not np.array_equal(a,b); event=row.get(t)
            if not differs:
                if event is not None and event['ideal']!=UNCHANGED:
                    errors.append(('E_LABEL_TILE',f'unchanged tile {t} frame {i} labelled'))
                continue
            if event is None:
                errors.append(('E_LABEL_GAP',f'changed tile {t} frame {i} uncovered')); continue
            expected=CHANGED; matches=[]
            if int(np.max(np.abs(a.astype(np.int16)-b.astype(np.int16))))<=2:
                expected=JITTER
            else:
                for dx,dy in known:
                    if min(x-dx,y-dy)>=0 and x1-dx<=grid.W and y1-dy<=grid.H:
                        if np.array_equal(b,prev[y-dy:y1-dy,x-dx:x1-dx]): matches.append((dx,dy))
                if matches: expected=MOVED
                elif period and i>=period and i%period in refs and np.array_equal(b,refs[i%period][y:y1,x:x1]):
                    expected=FLICKER
            if event['ideal']!=expected:
                errors.append(('E_LABEL_CLASS',f'frame {i} tile {t}: {event["ideal"]} != pixel {expected}'))
            if expected==MOVED:
                mv=event.get('mv')
                if not isinstance(mv,(list,tuple)) or len(mv)!=2 or tuple(mv) not in matches:
                    errors.append(('E_LABEL_MV',f'frame {i} tile {t}: shifted bytes disagree'))
            elif event.get('mv') is not None:
                errors.append(('E_LABEL_MV',f'frame {i} tile {t}: non-MOVED mv'))
        if period and i<period:
            if changed: stable_ref=cur
            refs[i]=stable_ref
        prev=cur
    if count!=scene['n']: errors.append(('E_LABEL_GAP','missing frames'))
    return errors


def _main(argv=None):
    """Pixel-check CLI; negative fixtures intentionally return nonzero."""
    import argparse
    import copy
    import json
    from .scenes import build, KINDS
    from .gen import render
    from visioncore.grid import Grid
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--kind',choices=KINDS,default='scroll_small')
    parser.add_argument('--k',type=int,default=0)
    parser.add_argument('--w',type=int,default=320)
    parser.add_argument('--h',type=int,default=180)
    parser.add_argument('--n',type=int,default=24)
    parser.add_argument('--negative',choices=('class','mv','tile','float'))
    args=parser.parse_args(argv)
    kind={'class':'window_drag','mv':'scroll_small','tile':'canvas_anim','float':'scroll_smooth'}.get(args.negative,args.kind)
    scene=build(kind,args.k,w=args.w,h=args.h,n=args.n)
    try:
        if args.negative=='float':
            scene['easing'][0]=0.0
            list(render(scene))
        events=copy.deepcopy(derive(scene,Grid(args.w,args.h)))
        if args.negative=='class': next(e for e in events if e['ideal']==MOVED)['ideal']=CHANGED
        elif args.negative=='mv': next(e for e in events if e['ideal']==MOVED)['mv'][1]+=1
        elif args.negative=='tile': next(e for e in events if e['ideal']==CHANGED)['tiles'].pop()
        errors=self_check(scene,render(scene),events)
    except (ValueError,StopIteration) as exc:
        print(json.dumps({'errors':[str(exc)],'ok':False}))
        return 1
    print(json.dumps({'errors':errors[:20],'error_count':len(errors),'ok':not errors}))
    return int(bool(errors))


if __name__=='__main__':
    raise SystemExit(_main())
