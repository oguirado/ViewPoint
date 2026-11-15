import type { CapsuleRect, CockpitState, SplitNode, WindowMode } from '../types/hypervisor';
import { BARRA_H, GUTTER_IZQ, LIENZO_H, LIENZO_H_ALTO, LIENZO_W } from '../geometria';

export type Layout = '1x1' | '1x2' | '2x2';
/** Main-window layouts; Go re-checks all affected leases before a layout write. */
export function legalLayouts(mode: WindowMode, poppedOut: false = false, capsules = Infinity): readonly Layout[] {
  void poppedOut;
  const layouts: Layout[] = mode === 'column_1_3_dual' && capsules >= 2 ? ['1x1', '1x2'] : ['1x1'];
  if (capsules >= 4) layouts.push('2x2');
  return layouts;
}
/** Immutable capsule framebuffer; scale is for operator presentation only. */
export const FRAMEBUFFER = {w:1280,h:900} as const;
export function frameScaleQ16(rect: CapsuleRect): number {
  return Math.min(65536,Math.floor(rect.w*65536/FRAMEBUFFER.w),Math.floor(rect.h*65536/FRAMEBUFFER.h));
}
function integer(value: number, min: number, max = Number.MAX_SAFE_INTEGER): boolean {
  return Number.isSafeInteger(value) && value >= min && value <= max;
}
function areaOK(r: CapsuleRect): boolean {
  return !!r && integer(r.x,0) && integer(r.y,0) && integer(r.w,1,65535) && integer(r.h,1,65535);
}
function same(a: CapsuleRect, b: CapsuleRect): boolean {
  return !!a && !!b && a.x === b.x && a.y === b.y && a.w === b.w && a.h === b.h;
}
/** Independent Q16 mirror, for validation only. Renderers use producer geometry. */
export function rects(tree: SplitNode, rect: CapsuleRect): Readonly<Record<string, CapsuleRect>> {
  const result: Record<string, CapsuleRect> = Object.create(null) as Record<string, CapsuleRect>;
  function visit(node: SplitNode, r: CapsuleRect, depth: number): void {
    if (!node || !areaOK(r)) throw new Error('Invalid split tree');
    if (node.type === 'leaf') {
      if (!/^srf-ng-(?:[0-9]|[1-7][0-9])$/.test(node.slot_id) || result[node.slot_id] || r.w < 600 || r.h < 420 || (node.scale_q16 !== undefined && node.scale_q16 !== frameScaleQ16(r))) throw new Error('Invalid leaf');
      result[node.slot_id] = {...r};
      return;
    }
    if (node.type !== 'split' || depth >= 2 || !integer(node.ratio_q16,4096,61440) || !['vertical','horizontal'].includes(node.direction)) throw new Error('Invalid split');
    const a = {...r}, b = {...r};
    if (node.direction === 'vertical') {
      a.h = Math.floor(r.h * node.ratio_q16 / 65536); b.y += a.h; b.h -= a.h;
    } else {
      a.w = Math.floor(r.w * node.ratio_q16 / 65536); b.x += a.w; b.w -= a.w;
    }
    visit(node.left,a,depth+1); visit(node.right,b,depth+1);
  }
  visit(tree,rect,0);
  return result;
}
export function validateTree(tree: SplitNode, rect: CapsuleRect, mode?: WindowMode, layout?: CockpitState['workspaces'][number]['layout']): boolean {
  try {
    const leaves = Object.keys(rects(tree,rect)).length;
    if (mode !== undefined) {
      if (!layout || !legalLayouts(mode,false,leaves).includes(layout as Layout)) return false;
      const height = layout === '2x2' ? LIENZO_H : mode === 'column_1_3_tall' ? LIENZO_H_ALTO : LIENZO_H * (layout === '1x2' ? 2 : 1);
      if (!same(rect,{x:0,y:0,w:LIENZO_W,h:height})) return false;
      if (leaves !== (layout === '2x2' ? 4 : layout === '1x2' ? 2 : 1)) return false;
    }
    return true;
  } catch { return false; }
}
function scaledLeaves(tree: SplitNode): boolean {
  return tree.type === 'leaf' ? Number.isInteger(tree.scale_q16) && (tree.scale_q16 ?? 0) > 0 : scaledLeaves(tree.left) && scaledLeaves(tree.right);
}
/** Workspace-local refusal plus global leaf/capsule bijection and ordering checks. */
export function workspaceErrors(doc: CockpitState): number[] {
  const bad = new Set<number>();
  const all = (): void => {for (const w of doc.workspaces) bad.add(w.n);};
  const leaves = new Map<string, {n: number; rect: CapsuleRect}>();
  let previous = 0;
  for (const w of doc.workspaces) {
    if (!integer(w.n,1,999) || w.n <= previous) all();
    previous = w.n;
    if (!validateTree(w.tree,w.rect,doc.window_mode,w.layout) || (doc.schema === 'os.cu.cockpit-state/v2' && !scaledLeaves(w.tree))) {bad.add(w.n);continue;}
    for (const [id,r] of Object.entries(rects(w.tree,w.rect))) {
      const other = leaves.get(id);
      if (other) {bad.add(other.n);bad.add(w.n);}
      leaves.set(id,{n:w.n,rect:r});
    }
  }
  const seen = new Map<string,number>();
  for (const c of doc.capsules) {
    const leaf = leaves.get(c.slot_id);
    if (seen.has(c.slot_id)) {bad.add(c.workspace);bad.add(seen.get(c.slot_id)!);}
    seen.set(c.slot_id,c.workspace);
    if (!leaf || leaf.n !== c.workspace || !same(leaf.rect,c.geometry)) {
      bad.add(c.workspace);if(leaf) bad.add(leaf.n);
      if (!doc.workspaces.some(w=>w.n===c.workspace)) all();
    }
  }
  for (const [id,leaf] of leaves) if (!seen.has(id)) bad.add(leaf.n);
  return [...bad].sort((a,b)=>a-b);
}
export function canvasToDomRect(rect: CapsuleRect, k: number): CapsuleRect {
  return {...rect,x:rect.x+GUTTER_IZQ,y:rect.y+BARRA_H+BARRA_H*k};
}
export function offNominalPolicy(domWidth: number): {tier:'exact'|'scaled'|'compact'|'withheld';scale:number;message:string} {
  if (!Number.isFinite(domWidth) || domWidth < 640) return {tier:'withheld',scale:0,message:'widen to operate'};
  if (domWidth >= LIENZO_W) return {tier:'exact',scale:1,message:''};
  const scale=domWidth/LIENZO_W;
  return {tier:domWidth>=960?'scaled':'compact',scale,message:`SCALED ×${scale.toFixed(2)}`};
}
export function separatorModel(a: string,b: string,ratio_q16: number,orientation: 'vertical'|'horizontal') {
  return {role:'separator' as const,name:`Split ${a} | ${b}`,
    'aria-orientation':orientation==='vertical'?'horizontal' as const:'vertical' as const,
    'aria-valuenow':Math.round(ratio_q16*100/65536),'aria-valuemin':6,'aria-valuemax':94,
    'aria-controls':`${a} ${b}`,tabindex:0,step_px:8};
}
export interface LatestWins { (run: ()=>void): void; cancel(): void }
/** WIRE calls this only for explicit mode changes and cancels it on unmount. */
export function latestWins(ms: number): LatestWins {
  if (!Number.isFinite(ms) || ms < 0) throw new RangeError('Invalid debounce duration');
  let pending: ReturnType<typeof setTimeout> | undefined;
  const cancel = (): void => {if(pending!==undefined) clearTimeout(pending);pending=undefined;};
  const queue: LatestWins = (run)=>{cancel();pending=setTimeout(()=>{pending=undefined;run();},ms);};
  queue.cancel=cancel;
  return queue;
}
