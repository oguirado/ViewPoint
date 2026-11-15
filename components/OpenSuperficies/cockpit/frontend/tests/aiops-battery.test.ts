import { describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { compile } from 'svelte/compiler';
import vm from 'node:vm';
import ts from 'typescript';
import { render } from 'svelte/server';
import { get } from 'svelte/store';
import CapsuleCard from '../src/lib/cards/CapsuleCard.svelte';
import { alPointerDown } from '../src/lib/aiops/CapsuleBar.svelte';
import { createStatusBus } from '../src/lib/aiops/status';
import type { SurfaceCapsule, Geometry } from '../src/lib/types/hypervisor';
const read=(path:string)=>readFileSync(new URL(path,import.meta.url),'utf8');
const master=()=>read('../src/lib/components/CockpitMasterView.svelte');
const card=()=>read('../src/lib/cards/CapsuleCard.svelte');
const capsule:SurfaceCapsule={id:'srf-ng-1',name:'Terminal',slot:1,archetype:'terminal',tier:'pty_jaula',mode:'agent_locked',health:'healthy',xpraPort:10021,cdpPort:null,telemetry:{cpuPercent:0,memoryBytes:0,memoryLimitBytes:0,networkRxRateBps:0,networkTxRateBps:0},gates:{},recording:{recording:false,sessionId:'',targetXid:'',sinceMonoMs:0,offsetMs:null,verdict:''},pbx:null,poppedOut:false,occluded:false,createdAt:''};
const geometry:Geometry={mode:'tile_1_6',domWidth:1271,domHeight:1064,canvasWidth:1264,canvasHeight:1024,scaled:false,scale:1};
function mounts(source:string):boolean {return /export\s*\{\s*superficie\s*\}/.test(source)&&/<CapsuleCard\b/.test(source)&&/metricas=\{block.telemetry\}/.test(source)&&/docTs=\{doc\?\.ts_mono_ms/.test(source);}
function browserGated(source:string):boolean {const suite=source.indexOf('describe.skipIf(!process.env.OSCU_BROWSER_TESTS)');const execution=source.search(/chromium\.launch|spawn\(process.execPath,\s*\[INSTRUMENTO/);return suite>=0&&(execution<0||suite<execution);}
// Execute the production event handlers with deterministic platform seams, without a DOM/browser.
function handlers(source:string,names:string[],setup:string,expose:string):any {
 const scripts=[...source.matchAll(/<script[^>]*>([\s\S]*?)<\/script>/g)].map(m=>m[1]).join('\n');
 const ast=ts.createSourceFile('handlers.ts',scripts,ts.ScriptTarget.ES2022,true,ts.ScriptKind.TS);
 const functions=ast.statements.filter(n=>ts.isFunctionDeclaration(n)&&n.name&&names.includes(n.name.text)).map(n=>n.getText(ast).replace(/^export /,''));
 if(functions.length!==names.length) throw new Error('Missing production event handler');
 const js=ts.transpile(setup+'\n'+functions.join('\n')+'\n'+expose,{target:ts.ScriptTarget.ES2022,module:ts.ModuleKind.None});
 return vm.runInNewContext(js);
}
describe('WIRE master battery (node)',()=>{
 it('exports the real card surface with canonical telemetry and rejects missing mounts',()=>{
  expect(mounts(master())).toBe(true);
  expect(mounts(master().replace('<CapsuleCard','<MissingCard'))).toBe(false);
  expect(master()).toContain('class:canonical={doc !== null}');
 });
 it('mounts one RAM recording control through the bar snippet',()=>{
  expect(card()).toContain('<RecordingControls');expect(card()).toContain('rec={recButton}');
  expect(read('../src/lib/aiops/CapsuleBar.svelte')).toContain('{@render rec()}');
  const html=render(CapsuleCard,{props:{capsula:capsule,geometria:geometry}}).body;
  expect(html.match(/aria-label="REC srf-ng-1"/g)).toHaveLength(1);
  expect(html).toContain('AUSENTE: RAM capture');
 });
 it('intercepts held tile pointerdown outside inert content without focusing the frame',()=>{
  expect(alPointerDown('agent_locked',false)).toBe('takeover');
  const source=card();
  expect(source).toContain('onpointerdown={tilePointerDown}');
  expect(source).toContain("inert={modo !== 'operator_controlled'}");
  const handler=source.slice(source.indexOf('function tilePointerDown'),source.indexOf('function viewportKey'));
  expect(handler).toContain('requestTakeover()');expect(handler).not.toContain('.focus(');
  expect(source).toContain('role="alertdialog"');expect(source).toContain('Confirm takeover {capsula.id}');
 });
 it('opens takeover from held tile input and never focuses or silently takes custody',()=>{
  const h=handlers(card(),['tilePointerDown','requestTakeover'],`
   let modo='agent_locked',iaSelected=false,takeoverRequested=false,prevented=0,stopped=0,focused=0;
   const events=[];const capsula={id:'srf-ng-1'};const bus={emit:e=>events.push(e)};
   const alPointerDown=(m,s)=>m==='agent_locked'||m==='read_only'&&s?'takeover':'activate';
   const adaptador={focus:()=>focused++};class Element{closest(){return null;}}
  `,`({click(){tilePointerDown({target:new Element(),preventDefault(){prevented++},stopPropagation(){stopped++}})},read(){return {takeoverRequested,prevented,stopped,focused,modo,events}}})`);
  h.click();expect(h.read()).toMatchObject({takeoverRequested:true,prevented:1,stopped:1,focused:0,modo:'agent_locked'});
  expect(h.read().events[0]).toMatchObject({what:'takeover-requested',scope:'srf-ng-1'});
 });
 it('requires a continuous keyboard hold, ignores repeats, and cancels released holds',()=>{
  const h=handlers(master(),['startAbortHold','cancelAbortHold','pasoDeAborto','alTeclaAborto'],`
   const ABORT_HOLD_MS=1500;let abortHolding=false,abortStart=0,abortFrame=0,abortHoldProgress=0,now=0,fired=0,queued=null;
   const performance={now:()=>now};const requestAnimationFrame=fn=>{queued=fn;return 2};const cancelAnimationFrame=()=>{queued=null};
   const triggerEmergencyAbort=()=>{fired++};
  `,`({key(type,key,repeat=false){alTeclaAborto({type,key,repeat,preventDefault(){}})},advance(ms){now=ms;const fn=queued;queued=null;fn?.(now)},read(){return {fired,abortHolding,abortHoldProgress}}})`);
  h.key('keydown',' ');h.advance(1499);expect(h.read().fired).toBe(0);
  h.key('keyup',' ');h.advance(1500);expect(h.read().fired).toBe(0);
  h.key('keydown','Enter');h.key('keydown','Enter',true);h.advance(2999);expect(h.read().fired).toBe(0);
  h.advance(3000);expect(h.read().fired).toBe(1);expect(h.read().abortHolding).toBe(false);
 });
 it('forwards the held credential in the fixed-arity Copy and Close DTOs',async()=>{
  const h=handlers(read('../src/lib/aiops/CapsuleBar.svelte'),['copy','close'],`
   const credential={holder:'lane-a',epoch:7,token:''};const slot_id='srf-ng-1',epoch=7;
   let copying=false,lastCopy=null;const calls=[];const performance={now:()=>100};
   const copyCycle=()=> 'uri';const perform=async(task,fn)=>{await fn();return true};const refused=()=>{};
   const bridge={CopiarSlot:async(...args)=>calls.push(['copy',...args]),CerrarSlot:async(...args)=>calls.push(['close',...args])};
  `,`({copy,close,calls,credential})`);
  await h.copy();h.close();
  expect(h.calls[0]).toEqual(['copy','srf-ng-1',{kind:'uri',credential:h.credential}]);
  expect(h.calls[1]).toEqual(['close','srf-ng-1',{epoch:7,credential:h.credential}]);
 });
 it('renders only canonical sampled telemetry and hides stale values',()=>{
  const metricas={cpu_permille:{source:'cgroup' as const,ts_mono_ms:100,value:250},mem_bytes:{source:'cgroup' as const,ts_mono_ms:100,value:1048576}};
  const fresh=render(CapsuleCard,{props:{capsula:capsule,geometria:geometry,metricas,docTs:100}}).body;
  expect(fresh).toContain('CPU 25.0%');expect(fresh).toContain('RAM 1 MiB');
  const stale=render(CapsuleCard,{props:{capsula:capsule,geometria:geometry,metricas,docTs:3101}}).body;
  expect(stale).not.toContain('CPU 25.0%');expect(stale).toContain('CPU —');
 });
 it('keeps literal authority failures on the shared status bus',()=>{
  const bus=createStatusBus();const task=bus.begin('switch-mode','srf-ng-1');task.fail('REFUSED: srf-ng-1 OPERATOR-AUTHORITY');
  expect(get(bus.lines).at(-1)?.text).toContain('OPERATOR-AUTHORITY');
  expect(card()).toContain('task?.fail(');expect(master()).not.toContain('role="alert"');
 });
 it('routes keyboard hold and abort through all-slot authority and observed state',()=>{
  const source=master();expect(source).toContain('AbortarTodo()');expect(source).toContain('clearTakeover()');
  expect(source).toContain("bus?.begin('emergency-abort', 'cockpit')");
  expect(source).toContain("ev.key !== ' ' && ev.key !== 'Enter'");
  expect(source).toContain('lease not revoked:');expect(source).toContain('EMERGENCY ABORT all slots: hold 1.5 s');
  expect(source).not.toMatch(/ponerHora|119\.88|0\.14|p99 &lt; 0\.40|role="status"|\.detener\(/);
 });
 it('gates browser launches and instrument spawns, including negative fixtures',()=>{
  for(const path of ['cards','geometria','stack']) expect(browserGated(read(`./${path}.test.ts`)),path).toBe(true);
  expect(browserGated("describe('bad',()=>{chromium.launch()})")).toBe(false);
  expect(browserGated("describe('bad',()=>{spawn(process.execPath, [INSTRUMENTO])})")).toBe(false);
 });
 it('compiles modified Svelte components for client and server',()=>{
  for(const path of ['components/CockpitMasterView','cards/CapsuleCard','aiops/CapsuleBar']) for(const generate of ['client','server'] as const)
   expect(()=>compile(read(`../src/lib/${path}.svelte`),{filename:`${path}.svelte`,generate})).not.toThrow();
 });
});
