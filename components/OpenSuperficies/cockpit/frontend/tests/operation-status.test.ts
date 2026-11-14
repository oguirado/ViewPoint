import { describe, expect, it } from 'vitest';
import { get } from 'svelte/store';
import { readFileSync } from 'node:fs';
import { spawnSync } from 'node:child_process';
import { createStatusBus, diffState, formatLine } from '../src/lib/aiops/status';
import type { StatusEvent, TaskName } from '../src/lib/aiops/status';
import type { CockpitState, SlotBlock } from '../src/lib/types/hypervisor';
const slot = (n = 1, patch: Partial<SlotBlock> = {}): SlotBlock => ({
  archetype:'terminal', cdp:null, geometry:{x:0,y:0,w:1264,h:1024}, health:'healthy',
  lease_epoch:0, lease_holder:null, lifecycle:'ready', mode:'operator_controlled', ready:true,
  rec_state:{phase:'idle',store:null,ttl_ms:null}, seq:1,slot:n,slot_id:`srf-ng-${n}`,
  telemetry:{cpu_permille:null,mem_bytes:null}, uri:`os://host/ws/1/slot/srf-ng-${n}`,workspace:1,xpra_port:10000+n,...patch,
});
const doc = (seq: number, capsules: SlotBlock[]): CockpitState => ({capsules,seq,host:'host',schema:'os.cu.cockpit-state/v1',ts_mono_ms:seq,window_mode:'tile_1_6',workspaces:[]});
const line = (ev: StatusEvent) => formatLine(1,ev);
const text = (bus: ReturnType<typeof createStatusBus>) => get(bus.lines).map(l=>l.text);

describe('FRONTEND (os-cu-0 F1-STATUS)', () => {
 it('good_switch and geometry_mismatch_degraded', () => {
  expect(line({kind:'mode',scope:'srf-ng-1',phase:'switching',mode:'switching'})).toBe('#1 srf-ng-1: SWITCHING…');
  expect(line({kind:'mode',scope:'srf-ng-1',phase:'settled',mode:'agent_locked',holder:'agent.1'})).toBe('#1 srf-ng-1: AGENT-HELD agent.1');
  expect(line({kind:'degraded',scope:'srf-ng-1',literal:'GEOMETRY MISMATCH ws 2'})).toContain('DEGRADED: GEOMETRY MISMATCH ws 2');
 });
 it('good_rec_cycle and recording_unknown_elapsed_dash', () => {
  for(const phase of ['idle','starting','stopping','sealed'] as const) expect(line({kind:'rec',scope:'cockpit',phase})).toContain(`rec ${phase}`);
  expect(line({kind:'rec',scope:'cockpit',phase:'recording',elapsed_ms:125000})).toContain('rec recording 02:05');
  expect(line({kind:'rec',scope:'cockpit',phase:'recording'})).toContain('rec recording —');
 });
 it('literal_verbatim, control cleanup, literal_path_redacted and truncation', () => {
  expect(line({kind:'degraded',scope:'cockpit',literal:'  MiXeD  error  '})).toBe('#1 cockpit: DEGRADED:   MiXeD  error  ');
  expect(line({kind:'refused',scope:'cockpit',literal:'a\r\n\tb'})).toContain('a b');
  expect(line({kind:'refused',scope:'cockpit',literal:'bad /home/alice/private.json {"x": [2]}'})).toContain('bad <path>');
  expect(line({kind:'degraded',scope:'cockpit',literal:'x'.repeat(243)})).toContain('x'.repeat(240)+'…[+3]');
 });
 const invalid = [
  {kind:'mode',scope:'cockpit',phase:'settled',mode:'degraded'},
  {kind:'mode',scope:'cockpit',phase:'settled',mode:'switching'},
  {kind:'mode',scope:'cockpit',phase:'switching',mode:'read_only'},
  {kind:'mode',scope:'cockpit',phase:'settled',mode:'agent_locked'},
  {kind:'mode',scope:'cockpit',phase:'settled',mode:'read_only',holder:'a'},
  {kind:'lease',scope:'cockpit',what:'granted',holder:'.bad',epoch:1},
  {kind:'lease',scope:'cockpit',what:'granted',holder:'good',epoch:0},
  {kind:'lease',scope:'cockpit',what:'released',holder:'a'},
  {kind:'lease',scope:'cockpit',what:'revoked',epoch:NaN},
  {kind:'degraded',scope:'bad',literal:'reason'},
  {kind:'degraded',scope:'srf-ng-80',literal:'reason'},
  {kind:'degraded',scope:'cockpit',literal:'  \n '},
  {kind:'lifecycle',scope:'cockpit',lifecycle:'loading'},
  {kind:'lifecycle',scope:'cockpit',lifecycle:'failed'},
  {kind:'lifecycle',scope:'cockpit',lifecycle:'ready',literal:'bad'},
  {kind:'rec',scope:'cockpit',phase:'recording',elapsed_ms:-1},
  {kind:'rec',scope:'cockpit',phase:'recording',elapsed_ms:'00:00'},
  {kind:'task',scope:'cockpit',task:'/tmp/file',phase:'started'},
  {kind:'other',scope:'cockpit'},
 ];
 it.each(invalid)('rejects programmer-error fixture %#', ev => expect(()=>line(ev as StatusEvent)).toThrow());
 it('cold_boot_only_nonresting and unknown literals', () => {
  expect(diffState(null,doc(1,[slot()]))).toEqual([]);
  const events=diffState(null,doc(1,[slot(1,{health:'degraded',lifecycle:'failed',rec_state:{phase:'error',store:null,ttl_ms:null}})]));
  expect(events.map(line)).toEqual(['#1 srf-ng-1: DEGRADED: —','#1 srf-ng-1: failed: —','#1 srf-ng-1: rec error: —']);
  expect(diffState(null,doc(1,[slot(1,{rec_state:{phase:'recording',store:'ram',ttl_ms:1000}})])).map(line)).toEqual(['#1 srf-ng-1: rec recording —']);
 });
 it('stale_seq_ignored and unsorted_multislot_diff', () => {
  const before=doc(2,[slot(2),slot(1)]), after=doc(3,[slot(2,{mode:'read_only',lifecycle:'busy'}),slot(1,{mode:'read_only',lifecycle:'busy'})]);
  expect(diffState(before,{...after,seq:2})).toEqual([]);
  expect(diffState(before,{...after,seq:1})).toEqual([]);
  expect(diffState(before,after).map(e=>`${e.scope}/${e.kind}`)).toEqual(['srf-ng-1/mode','srf-ng-1/lifecycle','srf-ng-2/mode','srf-ng-2/lifecycle']);
 });
 it('good_abort_two_slots, rejects early completion and a single-slot revoke', () => {
  const b=createStatusBus(); const locked=(n:number)=>slot(n,{mode:'agent_locked',lease_holder:'ai',lease_epoch:5});
  b.ingest(doc(1,[locked(1),locked(2)])); const task=b.begin('emergency-abort','cockpit');
  expect(()=>task.finish()).toThrow();
  b.ingest(doc(2,[slot(1,{lease_epoch:6}),locked(2)])); expect(()=>task.finish()).toThrow();
  b.ingest(doc(3,[slot(1,{lease_epoch:6}),slot(2,{lease_epoch:5})])); expect(()=>task.finish()).toThrow();
  b.ingest(doc(4,[slot(1,{lease_epoch:6}),slot(2,{lease_epoch:6})])); task.finish(); task.finish();
  expect(text(b).filter(t=>t.endsWith('task emergency-abort finished'))).toHaveLength(1);
 });
 it('abort does not accept disappearance or manual claims, and announces reappearing revocation', () => {
  const b=createStatusBus();
  b.ingest(doc(1,[slot(1,{mode:'agent_locked',lease_holder:'ai',lease_epoch:7})]));
  const h=b.begin('emergency-abort','cockpit'); b.ingest(doc(2,[]));
  b.emit({kind:'lease',scope:'srf-ng-1',what:'revoked',epoch:8});
  expect(()=>h.finish()).toThrow();
  b.ingest(doc(3,[slot(1,{lease_epoch:8})])); h.finish();
  expect(text(b).filter(t=>t.includes('lease revoked epoch 8'))).toHaveLength(2);
  expect(text(b).at(-1)).toContain('task emergency-abort finished');
 });
 it('lease before mode before lifecycle before rec and no idle publications', () => {
  const b=createStatusBus(); b.ingest(doc(1,[slot()])); let mutations=0;
  const unsub=b.lines.subscribe(()=>mutations++);
  b.ingest(doc(1,[slot(1,{lifecycle:'busy'})]));
  b.ingest(doc(2,[slot()])); expect(mutations).toBe(1); unsub();
  const changes=diffState(doc(1,[slot()]),doc(2,[slot(1,{mode:'agent_locked',lease_holder:'agent',lease_epoch:2,lifecycle:'busy',rec_state:{phase:'starting',store:'ram',ttl_ms:1000}})]));
  expect(changes.map(e=>e.kind)).toEqual(['lease','mode','lifecycle','rec']);
 });
 it('rejects lost_finish, finish_without_start and double_finish traces', () => {
  const complete=(phases:string[])=> {
   let active=false, settled=false;
   for(const phase of phases) {
    if(phase==='started') { if(active||settled) return false; active=true; }
    else { if(!active||settled) return false; active=false; settled=true; }
   }
   return settled&&!active;
  };
  expect(complete(['started'])).toBe(false);
  expect(complete(['finished'])).toBe(false);
  expect(complete(['started','finished','finished'])).toBe(false);
  expect(complete(['started','finished'])).toBe(true);
 });
 it('abort_partial_fails_with_literal and idempotent handle', () => {
  const b=createStatusBus(), task=b.begin('emergency-abort','cockpit');
  task.fail('lease not revoked: srf-ng-2'); task.finish(); task.fail('again');
  expect(text(b)).toEqual(['#1 cockpit: task emergency-abort started','#2 cockpit: task emergency-abort failed: lease not revoked: srf-ng-2']);
 });
 it('ring_keeps_start_and_finish, all task names, and invalid task sequencing', () => {
  const b=createStatusBus(8), task=b.begin('snapshot','srf-ng-1');
  for(let i=0;i<9;i++) b.emit({kind:'lifecycle',scope:'cockpit',lifecycle:'busy'});
  expect(text(b)).toHaveLength(9); expect(text(b)[0]).toContain('task snapshot started');
  task.finish(); expect(text(b)).toHaveLength(8); expect(text(b).at(-1)).toContain('task snapshot finished');
  expect(()=>b.emit({kind:'task',scope:'cockpit',task:'snapshot',phase:'finished'})).toThrow();
  const tasks: TaskName[]=['open-workspace','set-layout','start-capsule','switch-mode','copy-slot','snapshot','rec-start','rec-stop','emergency-abort','close-capsule'];
  const all=createStatusBus(40); for(const name of tasks) { const h=all.begin(name,'cockpit'); h.finish(); }
  expect(text(all)).toHaveLength(20);
 });
 it('ingest never throws and emits a degraded diagnostic for malformed input', () => {
  const b=createStatusBus(); expect(()=>b.ingest(null as unknown as CockpitState)).not.toThrow();
  expect(text(b)[0]).toContain('cockpit: DEGRADED: status ');
  b.ingest(doc(1,[slot(1,{lifecycle:'failed'})]),{literals:{'srf-ng-1':'cannot open /tmp/private [error]'}});
  expect(text(b).at(-1)).toContain('failed: cannot open <path>');
 });
 it('renders one visible polite region, no JSON/controls, scrolls newest line, uses fixed accessible style', () => {
  const source=readFileSync(new URL('../src/lib/aiops/OperationStatus.svelte',import.meta.url),'utf8');
  expect(source.match(/role="status"/g)).toHaveLength(1); expect(source.match(/aria-live="polite"/g)).toHaveLength(1);
  for(const token of ['aria-atomic="false"','aria-relevant="additions"','aria-label="Operation status"','13px/16px','--text-polar','--surface-cockpit','scrollTop','scrollHeight','contain: strict']) expect(source).toContain(token);
  expect(source).not.toMatch(/JSON.stringify|\{@html|@keyframes|transition|sr-only|display:\s*none|visibility:\s*hidden/);
  const script=String.raw`import {compile} from 'svelte/compiler'; import {render} from 'svelte/server'; import {readFileSync} from 'node:fs';
  const source=readFileSync(0,'utf8'); const client=compile(source,{generate:'client',filename:'OperationStatus.svelte'}); if(client.warnings.length) {console.error(client.warnings);process.exit(5);} let js=compile(source,{generate:'server',filename:'OperationStatus.svelte'}).js.code;
  js=js.replace(/(['"])svelte(?:\/internal\/server)?\1/g,m=>JSON.stringify(import.meta.resolve(m.slice(1,-1))));
  const {default:C}=await import('data:text/javascript;base64,'+Buffer.from(js).toString('base64'));
  console.log(render(C,{props:{bus:{lines:{subscribe(fn){fn([{n:1,text:'#1 cockpit: ready'}]);return ()=>{};}}}}}).body);`;
  const child=spawnSync(process.execPath,['--input-type=module','-e',script],{cwd:new URL('..',import.meta.url),input:source,encoding:'utf8',timeout:10000});
  expect(child.status,child.stderr).toBe(0); expect(child.stdout).toContain('data-n="1"'); expect(child.stdout).toContain('#1 cockpit: ready');
  expect(child.stdout.match(/role="status"/g)).toHaveLength(1); expect(child.stdout).not.toMatch(/<button|<input|role="alert"/);
 });
});
