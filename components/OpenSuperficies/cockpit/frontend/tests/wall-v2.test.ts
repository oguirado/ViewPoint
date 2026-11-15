import { expect, it } from 'vitest';
import { legalLayouts, rects, validateTree } from '../src/lib/aiops/splittree';
import type { SplitNode } from '../src/lib/types/hypervisor';
const leaf=(slot:number)=>({type:'leaf' as const,slot_id:`srf-ng-${slot}`,scale_q16:32358});
const row=(a:number,b:number)=>({type:'split' as const,direction:'horizontal' as const,ratio_q16:32768,left:leaf(a),right:leaf(b)});
const tree:SplitNode={type:'split',direction:'vertical',ratio_q16:32768,left:row(0,1),right:row(2,3)};
const canvas={x:0,y:0,w:1264,h:1024};
it('offers a four-capsule wall in every main-window mode',()=>{
 for(const mode of ['tile_1_6','column_1_3_tall','column_1_3_dual'] as const) {
  expect(legalLayouts(mode,false,4)).toContain('2x2');
  expect(legalLayouts(mode,false,3)).not.toContain('2x2');
  expect(validateTree(tree,canvas,mode,'2x2')).toBe(true);
 }
});
it('computes row-major tile geometry and rejects inconsistent scale',()=>{
 expect(rects(tree,canvas)['srf-ng-3']).toEqual({x:632,y:512,w:632,h:512});
 const bad=structuredClone(tree) as any; bad.right.right.scale_q16=65536;
 expect(validateTree(bad,canvas,'tile_1_6','2x2')).toBe(false);
});

import { beforeAll, afterAll } from 'vitest';
import { readFileSync } from 'node:fs';
import { createServer } from 'vite';
import { svelte, vitePreprocess } from '@sveltejs/vite-plugin-svelte';
import { readState } from '../src/lib/aiops/state';
import type { CockpitState, SlotBlock } from '../src/lib/types/hypervisor';
function wall():CockpitState {
 const capsules:SlotBlock[]=Array.from({length:4},(_,slot)=>({archetype:'desktop',cdp:null,geometry:{x:(slot%2)*632,y:Math.floor(slot/2)*512,w:632,h:512},health:'healthy',lease_epoch:0,lease_holder:null,lifecycle:'ready',mode:'read_only',ready:true,rec_state:{phase:'idle',store:null,ttl_ms:null},seq:7,slot,slot_id:`srf-ng-${slot}`,telemetry:{cpu_permille:null,mem_bytes:null},uri:`os://osbox/ws/1/slot/srf-ng-${slot}`,workspace:1,xpra_port:10020+slot}));
 return {schema:'os.cu.cockpit-state/v2',host:'osbox',seq:7,ts_mono_ms:6000,window_mode:'tile_1_6',capsules,workspaces:[{n:1,layout:'2x2',rect:canvas,tree}]};
}
it('passes v2 producer bytes through the shell state reader',async()=>{
 const bytes=JSON.stringify(wall());
 const next=await readState({EstadoCanonico:async()=>bytes,BloqueSlot:async()=>'',StateURL:async()=>''});
 expect(next.bytes).toBe(bytes);expect(next.document.schema).toBe('os.cu.cockpit-state/v2');
 const bad=structuredClone(wall()) as any;bad.workspaces[0].tree.left.left.scale_q16=65536;
 await expect(readState({EstadoCanonico:async()=>JSON.stringify(bad),BloqueSlot:async()=>'',StateURL:async()=>''})).rejects.toThrow();
});
let server:Awaited<ReturnType<typeof createServer>>;
let render:any,Matrix:any,Mirror:any,Frame:any,MatrixFrames:any;
beforeAll(async()=>{
 server=await createServer({configFile:false,root:new URL('..',import.meta.url).pathname,cacheDir:new URL('../../../build/room_WALL/vite-v2',import.meta.url).pathname,plugins:[svelte({preprocess:vitePreprocess(),hot:false})],server:{middlewareMode:true,hmr:false,watch:null},appType:'custom'});
 render=(await server.ssrLoadModule('svelte/server')).render;
 Matrix=(await server.ssrLoadModule('/src/lib/workspace/WorkspaceMatrix.svelte')).default;
 Mirror=(await server.ssrLoadModule('/src/lib/aiops/StateMirror.svelte')).default;
 Frame=(await server.ssrLoadModule('/tests/WallFrameHarness.svelte')).default;
 MatrixFrames=(await server.ssrLoadModule('/tests/WallMatrixHarness.svelte')).default;
});
afterAll(async()=>{await server?.close();});
it('renders all four tiles in the main-window canvas with no staggered row bars',()=>{
 const doc=wall(),html=render(Matrix,{props:{doc,bytes:JSON.stringify(doc)}}).body;
 for(let slot=0;slot<4;slot++)expect(html).toContain(`aria-label="Tile srf-ng-${slot}"`);
 expect(html).toContain('left: 635px');expect(html).toContain('top: 552px');expect(html).not.toContain('DEGRADED');
 expect(html).not.toContain('top: 672px');
});
it('keeps the operator iframe framebuffer exact while the parent scales it',()=>{
 const html=render(Frame).body;
 expect(html).toContain('width="1280"');expect(html).toContain('height="900"');
 expect(html).toContain('Operator view srf-ng-0');expect(html).toContain('clipboard=false');
});
it('mirrors v2 bytes and keeps the main-window layout option under legalLayouts',()=>{
 const bytes=JSON.stringify(wall());const html=render(Mirror,{props:{bytes}}).body;
 expect(html).toContain('os.cu.cockpit-state/v2');
 expect(html).toContain('scale_q16');expect(html).toContain('aria-label="cockpit-state"');
 const app=readFileSync(new URL('../src/App.svelte',import.meta.url),'utf8');
 expect(app).toContain("['1x1','1x2','2x2']");expect(app).toContain('disabled:!legal.some');
});

it('keeps all wall framebuffers exact through an externally supplied surface snippet',()=>{
 const html=render(MatrixFrames,{props:{doc:wall()}}).body;
 expect(html.match(/width="1280"/g)).toHaveLength(4);
 expect(html.match(/height="900"/g)).toHaveLength(4);
});
