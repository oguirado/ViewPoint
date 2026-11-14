/** FRONTEND (os-cu-0 F1-STATUS): pure B3 announcement channel. No transport or I/O. */
import { writable, type Readable } from 'svelte/store';
import type { CockpitState, OperationMode, SlotBlock } from '../types/hypervisor';
export type TaskName = 'open-workspace'|'set-layout'|'start-capsule'|'switch-mode'|'copy-slot'|'snapshot'|'rec-start'|'rec-stop'|'emergency-abort'|'close-capsule';
export type StatusEvent =
 | {kind:'mode'; scope:string; phase:'switching'|'settled'; mode:OperationMode; holder?:string}
 | {kind:'degraded'; scope:string; literal:string}
 | {kind:'lease'; scope:string; what:'granted'|'released'|'revoked'|'takeover-requested'|'takeover-granted'|'takeover-declined'|'refused'; holder?:string; epoch?:number}
 | {kind:'refused'; scope:string; literal:string}
 | {kind:'rec'; scope:string; phase:SlotBlock['rec_state']['phase']; elapsed_ms?:number; literal?:string}
 | {kind:'lifecycle'; scope:string; lifecycle:SlotBlock['lifecycle']; literal?:string}
 | {kind:'task'; scope:string; task:TaskName; phase:'started'|'finished'|'failed'; literal?:string};
export type Announce = (ev: StatusEvent) => void;
export interface TaskHandle { finish():void; fail(literal:string):void }
export interface StatusBus {
 emit:Announce; begin(task:TaskName,scope:string):TaskHandle;
 ingest(doc:CockpitState,ctx?:DiffCtx):void;
 lines:Readable<readonly {n:number;text:string}[]>;
}
export type DiffCtx = {recElapsedMs?:Readonly<Record<string,number>>; literals?:Readonly<Record<string,string>>};
const tasks: readonly TaskName[] = ['open-workspace','set-layout','start-capsule','switch-mode','copy-slot','snapshot','rec-start','rec-stop','emergency-abort','close-capsule'];
const scopePattern=/^(?:cockpit|srf-ng-(?:[0-9]|[1-7][0-9]))$/;
const holderPattern=/^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$/;
const pathPattern=/(^|[\s"'=])(?:\/(?:home|tmp|var|etc|opt|proc|sys|dev|run)\b|[A-Za-z]:\\)[^\s"'<>]*/g;
function requireStatus(ok:unknown,reason:string):asserts ok { if(!ok) throw new Error(reason); }
function literal(value:unknown):string {
 requireStatus(typeof value==='string','missing literal');
 const clean=value.replace(/[\u0000-\u001F\u007F-\u009F]+/g,' ').replace(pathPattern,'$1<path>').replace(/[{}\[\]"]/g,' ');
 requireStatus(clean.trim().length>0,'empty literal');
 return clean.length>240 ? clean.slice(0,240)+`…[+${clean.length-240}]` : clean;
}
function epoch(value:unknown):asserts value is number { requireStatus(Number.isSafeInteger(value)&&Number(value)>=1,'invalid epoch'); }
export function formatLine(n:number,ev:StatusEvent):string {
 requireStatus(Number.isSafeInteger(n)&&n>=1,'invalid line number');
 requireStatus(ev && scopePattern.test(ev.scope),'invalid scope');
 const fields=ev as unknown as Record<string,unknown>;
 const needsHolder=(ev.kind==='mode'&&ev.mode==='agent_locked')||(ev.kind==='lease'&&ev.what==='granted');
 requireStatus(needsHolder ? typeof fields.holder==='string'&&holderPattern.test(fields.holder) : fields.holder===undefined,'invalid holder');
 const needsLiteral=ev.kind==='degraded'||ev.kind==='refused'||(ev.kind==='rec'&&ev.phase==='error')||(ev.kind==='lifecycle'&&ev.lifecycle==='failed')||(ev.kind==='task'&&ev.phase==='failed');
 const error=needsLiteral ? literal(fields.literal) : '';
 requireStatus(needsLiteral||fields.literal===undefined,'unexpected literal');
 const needsEpoch=ev.kind==='lease'&&(ev.what==='granted'||ev.what==='revoked');
 if(needsEpoch) epoch(fields.epoch); else requireStatus(fields.epoch===undefined,'unexpected epoch');
 requireStatus((ev.kind==='rec'&&ev.phase==='recording')||fields.elapsed_ms===undefined,'unexpected elapsed');
 let text:string;
 switch(ev.kind) {
 case 'mode': {
  requireStatus(['switching','settled'].includes(ev.phase),'invalid mode phase');
  requireStatus((ev.phase==='switching')===(ev.mode==='switching'),'inconsistent switching');
  const names: Record<OperationMode,string>={switching:'SWITCHING…',operator_controlled:'OPERATOR-CONTROL',read_only:'READ-ONLY',agent_locked:`AGENT-HELD ${ev.holder}`};
  requireStatus(Object.hasOwn(names,ev.mode),'invalid mode'); text=names[ev.mode]; break;
 }
 case 'degraded': text=`DEGRADED: ${error}`; break;
 case 'refused': text=`refused: ${error}`; break;
 case 'lease': {
  const names={granted:`lease granted ${ev.holder} epoch ${ev.epoch}`,released:'lease released',revoked:`lease revoked epoch ${ev.epoch}`,'takeover-requested':'takeover requested','takeover-granted':'takeover granted','takeover-declined':'takeover declined',refused:'agent refused: OPERATOR-CONTROL'};
  requireStatus(Object.hasOwn(names,ev.what),'invalid lease'); text=names[ev.what]; break;
 }
 case 'rec': {
  requireStatus(['idle','starting','recording','stopping','sealed','error'].includes(ev.phase),'invalid rec phase');
  let elapsed='—';
  if(ev.elapsed_ms!==undefined) {
   requireStatus(Number.isSafeInteger(ev.elapsed_ms)&&ev.elapsed_ms>=0,'invalid elapsed');
   const seconds=Math.floor(ev.elapsed_ms/1000); elapsed=`${String(Math.floor(seconds/60)).padStart(2,'0')}:${String(seconds%60).padStart(2,'0')}`;
  }
  text=ev.phase==='error'?`rec error: ${error}`:ev.phase==='recording'?`rec recording ${elapsed}`:`rec ${ev.phase}`; break;
 }
 case 'lifecycle':
  requireStatus(['booting','ready','busy','stopping','stopped','failed'].includes(ev.lifecycle),'invalid lifecycle');
  text=ev.lifecycle==='failed'?`failed: ${error}`:ev.lifecycle; break;
 case 'task':
  requireStatus(tasks.includes(ev.task)&&['started','finished','failed'].includes(ev.phase),'invalid task');
  text=`task ${ev.task} ${ev.phase}${ev.phase==='failed'?`: ${error}`:''}`; break;
 default: throw new Error('invalid event kind');
 }
 return `#${n} ${ev.scope}: ${text}`;
}
export function diffState(prev:CockpitState|null,next:CockpitState,ctx:DiffCtx={}):StatusEvent[] {
 if(prev&&next.seq<=prev.seq) return [];
 const events:StatusEvent[]=[];
 const previous=new Map(prev?.capsules.map(s=>[s.slot_id,s]));
 for(const slot of [...next.capsules].sort((a,b)=>a.slot-b.slot)) {
  const old=previous.get(slot.slot_id), scope=slot.slot_id, reason=ctx.literals?.[scope]??'—';
  if(prev) {
   if(slot.mode==='agent_locked'&&(!old||old.lease_holder!==slot.lease_holder||old.lease_epoch!==slot.lease_epoch)) events.push({kind:'lease',scope,what:'granted',holder:slot.lease_holder??undefined,epoch:slot.lease_epoch});
   else if(old&&slot.lease_holder===null&&slot.lease_epoch>old.lease_epoch) events.push({kind:'lease',scope,what:'revoked',epoch:slot.lease_epoch});
   else if(old?.lease_holder&&slot.lease_holder===null) events.push({kind:'lease',scope,what:'released'});
  }
  if(prev ? !old||old.mode!==slot.mode : slot.mode==='switching') events.push({kind:'mode',scope,phase:slot.mode==='switching'?'switching':'settled',mode:slot.mode,...(slot.mode==='agent_locked'?{holder:slot.lease_holder??undefined}:{})});
  if(slot.health==='degraded'&&(!old||old.health!=='degraded')) events.push({kind:'degraded',scope,literal:reason});
  if(prev ? !old||old.lifecycle!==slot.lifecycle : ['booting','stopping','failed'].includes(slot.lifecycle)) events.push({kind:'lifecycle',scope,lifecycle:slot.lifecycle,...(slot.lifecycle==='failed'?{literal:reason}:{})});
  if(prev ? !old||old.rec_state.phase!==slot.rec_state.phase : ['starting','recording','stopping','error'].includes(slot.rec_state.phase)) events.push({kind:'rec',scope,phase:slot.rec_state.phase,...(slot.rec_state.phase==='error'?{literal:reason}:{}),...(slot.rec_state.phase==='recording'?{elapsed_ms:ctx.recElapsedMs?.[scope]}:{})});
 }
 return events;
}
export function createStatusBus(ring=8):StatusBus {
 requireStatus(Number.isSafeInteger(ring)&&ring>=1,'invalid ring');
 type Line={n:number;text:string};
 const store=writable<readonly Line[]>([]);
 let lines:Line[]=[], sequence=0, previous:CockpitState|null=null;
 const pinned=new Map<string,number>();
 // Retain evidence until a strictly newer, holder-null epoch is observed. Disappearance is not revocation.
 const locked=new Map<string,number>();
 const key=(ev:{scope:string;task:TaskName})=>`${ev.scope}/${ev.task}`;
 const emit:Announce=ev=> {
  const formatted=formatLine(sequence+1,ev);
  if(ev.kind==='task') {
   const id=key(ev);
   if(ev.phase==='started') { requireStatus(!pinned.has(id),'task already started'); pinned.set(id,sequence+1); }
   else {
    requireStatus(pinned.has(id),'task finish without start');
    if(ev.task==='emergency-abort'&&ev.phase==='finished') requireStatus(locked.size===0,'lease not revoked: '+[...locked.keys()].join(', '));
    pinned.delete(id);
   }
  }
  lines=[...lines,{n:++sequence,text:formatted}];
  const pins=new Set(pinned.values());
  while(lines.length-pins.size>ring) { const index=lines.findIndex(l=>!pins.has(l.n)); if(index<0) break; lines.splice(index,1); }
  store.set(lines);
 };
 return {
  emit, lines:{subscribe:store.subscribe},
  begin(task,scope) {
   emit({kind:'task',scope,task,phase:'started'}); let done=false;
   return { finish() { if(!done) { emit({kind:'task',scope,task,phase:'finished'}); done=true; } }, fail(reason) { if(!done) { emit({kind:'task',scope,task,phase:'failed',literal:reason}); done=true; } } };
  },
  ingest(doc,ctx) {
   try {
    if(previous&&doc.seq<=previous.seq) return;
    const events=diffState(previous,doc,ctx);
    // A missing slot may reappear after an abort. Announce its witnessed revocation too.
    for(const slot of doc.capsules) {
     const heldEpoch=locked.get(slot.slot_id);
     if(heldEpoch!==undefined&&slot.lease_holder===null&&slot.lease_epoch>heldEpoch&&!events.some(ev=>ev.kind==='lease'&&ev.scope===slot.slot_id&&ev.what==='revoked')) {
      events.push({kind:'lease',scope:slot.slot_id,what:'revoked',epoch:slot.lease_epoch});
     }
    }
    events.sort((a,b)=> {
     const bySlot=Number(a.scope.slice(7))-Number(b.scope.slice(7));
     const rank={lease:0,mode:1,degraded:2,lifecycle:3,rec:4,refused:5,task:6};
     return bySlot||rank[a.kind]-rank[b.kind];
    });
    // Validate all before updating evidence or the baseline; malformed input cannot open the abort barrier.
    for(const ev of events) formatLine(sequence+1,ev);
    for(const slot of doc.capsules) {
     if(slot.mode==='agent_locked') locked.set(slot.slot_id,Math.max(locked.get(slot.slot_id)??0,slot.lease_epoch));
     else if(slot.lease_holder===null&&slot.lease_epoch>(locked.get(slot.slot_id)??Infinity)) locked.delete(slot.slot_id);
    }
    for(const ev of events) emit(ev);
    previous=doc;
   } catch(error) { emit({kind:'degraded',scope:'cockpit',literal:`status ${error instanceof Error?error.message:String(error)}`}); }
  },
 };
}
