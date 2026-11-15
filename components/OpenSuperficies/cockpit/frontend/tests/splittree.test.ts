import { describe, expect, it, vi } from 'vitest';
import { canvasToDomRect, latestWins, legalLayouts, offNominalPolicy, rects, separatorModel, validateTree, workspaceErrors } from '../src/lib/aiops/splittree';
import type { CockpitState, SlotBlock, WindowMode } from '../src/lib/types/hypervisor';
function fixture(mode: WindowMode = 'column_1_3_dual', count = 2): CockpitState {
  const per = mode === 'column_1_3_dual' ? 2 : 1;
  const height = mode === 'column_1_3_tall' ? 2080 : 1024;
  const capsules: SlotBlock[] = Array.from({length:count}, (_, slot) => ({
    archetype:'browser', cdp:9220+slot, geometry:{x:0,y:(slot%per)*height,w:1264,h:height}, health:'healthy', lease_epoch:0, lease_holder:null,
    lifecycle:'ready', mode:'read_only', ready:true, rec_state:{phase:'idle',store:null,ttl_ms:null}, seq:7, slot,
    slot_id:`srf-ng-${slot}`, telemetry:{cpu_permille:null,mem_bytes:null}, uri:`os://osbox/ws/${Math.floor(slot/per)+1}/slot/srf-ng-${slot}`,workspace:Math.floor(slot/per)+1,xpra_port:10020+slot,
  }));
  return {capsules,host:'osbox',schema:'os.cu.cockpit-state/v1',seq:7,ts_mono_ms:6000,window_mode:mode,
    workspaces:Array.from({length:Math.ceil(count/per)},(_, i)=>{
      const a=capsules[i*per], b=capsules[i*per+1];
      const dual=per===2 && !!b;
      return {n:i+1,layout:dual?'1x2':'1x1',rect:{x:0,y:0,w:1264,h:height*(dual?2:1)},tree:dual?{type:'split',direction:'vertical',ratio_q16:32768,left:{type:'leaf',slot_id:a.slot_id},right:{type:'leaf',slot_id:b.slot_id}}:{type:'leaf',slot_id:a.slot_id}};
    })};
}


describe('F1-TILING canonical geometry oracle', () => {
  it.each([
    ['ok_1x1_tile','tile_1_6',1],['ok_tall_2080','column_1_3_tall',1],
    ['ok_dual_1x2_from_bytes','column_1_3_dual',2],['ok_overflow_ws2','column_1_3_dual',3],['ok_cap1_dual','column_1_3_dual',1],
  ] as const)('%s', (_, mode, count) => {
    const doc = fixture(mode,count);
    expect(workspaceErrors(doc)).toEqual([]);
    for (const w of doc.workspaces) {
      expect(validateTree(w.tree,w.rect,mode,w.layout)).toBe(true);
      const actual=rects(w.tree,w.rect);
      for(const c of doc.capsules.filter(c=>c.workspace===w.n)) expect(actual[c.slot_id]).toEqual(c.geometry);
    }
  });
  const bad: [string,(d:any)=>void][] = [
    ['split_in_tile_1_6',d=>d.window_mode='tile_1_6'],
    ['leaf_below_useful_width',d=>d.workspaces[0].rect.w=1263],
    ['side_by_side_2528',d=>{d.workspaces[0].rect.w=2528;d.workspaces[0].rect.h=1024;d.workspaces[0].tree.direction='horizontal';}],
    ['extreme_q16_ratio',d=>d.workspaces[0].tree.ratio_q16=1],
    ['tree_depth_3',d=>{const t=d.workspaces[0].tree; t.left=structuredClone(t);t.left.left=structuredClone(t);}],
    ['dup_tree_leaf',d=>d.workspaces[0].tree.right.slot_id='srf-ng-0'],
    ['ghost_tree_leaf',d=>d.workspaces[0].tree.right.slot_id='srf-ng-9'],
    ['layout_leaf_mismatch',d=>d.workspaces[0].layout='1x1'],
    ['unsorted_workspaces',d=>d.workspaces.reverse()],
    ['orphan_capsule',d=>d.workspaces.pop()],
    ['ui_rect_mismatch',d=>d.capsules[0].geometry.y=1],
  ];
  it.each(bad)('rejects %s',(_,mutate)=>{
    const d=structuredClone(fixture('column_1_3_dual',3)); mutate(d);
    expect(workspaceErrors(d as CockpitState).length).toBeGreaterThan(0);
  });
  it('legal layout parity includes the four-capsule main-window wall',()=>{
    expect(legalLayouts('tile_1_6',false,1)).toEqual(['1x1']);
    expect(legalLayouts('column_1_3_tall',false,1)).toEqual(['1x1']);
    expect(legalLayouts('column_1_3_dual',false,1)).toEqual(['1x1']);
    expect(legalLayouts('column_1_3_dual',false,2)).toEqual(['1x1','1x2']);
    expect(legalLayouts('column_1_3_dual',false,4)).toContain('2x2');
    expect(legalLayouts('column_1_3_dual',true as false,4)).toContain('2x2');
  });
  it('ok_dom_rect_leaf1_1104',()=>expect(canvasToDomRect({x:0,y:1024,w:1264,h:1024},1)).toEqual({x:3,y:1104,w:1264,h:1024}));
  it.each([[639,'withheld'],[640,'compact'],[959,'compact'],[960,'scaled'],[1263,'scaled'],[1264,'exact']] as const)('off nominal %i', (w,tier)=>{
    const p=offNominalPolicy(w);expect(p.tier).toBe(tier);
    if(w===639) expect(p.message).toBe('widen to operate');
    else expect(p.scale).toBe(Math.min(1,w/1264));
  });
  it('separator_missing_role negative: complete inert AX model',()=>{
    expect(separatorModel('srf-ng-0','srf-ng-1',32768,'vertical')).toEqual({role:'separator',name:'Split srf-ng-0 | srf-ng-1','aria-orientation':'horizontal','aria-valuenow':50,'aria-valuemin':6,'aria-valuemax':94,'aria-controls':'srf-ng-0 srf-ng-1',tabindex:0,step_px:8});
  });
  it('latest-wins only runs the last explicit request and can cancel pending work',()=>{
    vi.useFakeTimers();try {
      const q=latestWins(150), calls=vi.fn();q(()=>calls('old'));vi.advanceTimersByTime(149);q(()=>calls('new'));
      vi.advanceTimersByTime(149);expect(calls).not.toHaveBeenCalled();vi.advanceTimersByTime(1);expect(calls.mock.calls).toEqual([['new']]);
      q(()=>calls('cancelled'));q.cancel();vi.advanceTimersByTime(150);expect(calls).toHaveBeenCalledTimes(1);
    } finally {vi.useRealTimers();}
  });
});
