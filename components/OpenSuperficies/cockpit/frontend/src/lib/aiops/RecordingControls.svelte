<script module lang="ts">
  import type { Snippet } from 'svelte';
  import type { DiffCtx, StatusBus } from './status';
  import type { LeaseCredential } from './CapsuleBar.svelte';
  export interface RecordingSnapshot {
    fase: 'idle'|'starting'|'recording'|'stopping'|'sealed'|'error';
    slot: number; sessionId: string; recording: boolean;
    sinceMonoMs: number; duracionMs: number; offsetMs: number|null;
    store: 'ram'|'disk'|null; ttl_ms: number|null; dir: string; error?: string;
  }
  export interface RecordingBridge {
    IniciarGrabacionSlot(slot: string, xid: number, epoch: number, cred: LeaseCredential|null): Promise<string>;
    DetenerGrabacionSlot(slot: string, session: string, epoch: number, cred: LeaseCredential|null): Promise<void>;
    EstadoGrabacion(): Promise<RecordingSnapshot>;
    EstadoCanonico(): Promise<string>;
  }
  export const recPrivacy = 'Privacy is structural: only control literals of standard interactive roles are recorded; free text is reduced to role and length.';
  const clock = (ms: number): string => {
    const sec=Math.floor(Math.max(0,ms)/1000);
    return `${String(Math.floor(sec/60)).padStart(2,'0')}:${String(sec%60).padStart(2,'0')}`;
  };
  // nowMs is elapsed since this snapshot was received, never a guessed host clock.
  export function recLocation(rs: RecordingSnapshot, nowMs: number): string {
    if(rs.fase==='idle') return '—';
    if(rs.store==='disk'&&rs.fase==='sealed'&&rs.dir) return `DISK · ${rs.dir}`;
    if(rs.store!=='ram'||rs.ttl_ms===null||rs.ttl_ms<=0) return '—';
    const remaining=rs.ttl_ms-rs.sinceMonoMs-Math.max(0,nowMs);
    return remaining>0 ? `RAM · TTL ${clock(remaining)}` : '—';
  }
  export function recElapsed(rs: RecordingSnapshot): string {
    return clock(rs.fase==='recording' ? rs.sinceMonoMs : rs.duracionMs);
  }
  const clean = (text: string): string => text.replace(/(?:\/[A-Za-z0-9_.~:+-]+)+|[A-Za-z]:\\[^\s]+/g,'<path>');
  export function recDiffCtx(rs: RecordingSnapshot): DiffCtx {
    const scope=`srf-ng-${rs.slot}`;
    return {recElapsedMs:rs.fase==='recording'?{[scope]:rs.sinceMonoMs}:{},literals:rs.error?{[scope]:clean(rs.error)}:{}};
  }
</script>

<script lang="ts">
  import { onMount } from 'svelte';
  import { assertState } from './state';
  interface Props {
    slot_id: string; recording: RecordingSnapshot; epoch?: number; xid?: number;
    credential?: LeaseCredential|null; bridge?: RecordingBridge|null; bus?: StatusBus;
    available?: boolean; unavailableReason?: string;
    children?: Snippet<[Snippet]>;
  }
  let {slot_id,recording,epoch=0,xid=0,credential=null,bridge=null,bus,available=false,
    unavailableReason='AUSENTE: RAM capture',children}: Props=$props();
  let elapsed=$state(0);
  let local=$state<RecordingSnapshot|null>(null);
  let busy=$state(false);
  const rs=$derived(local??recording);
  const owns=$derived(slot_id===`srf-ng-${rs.slot}`);
  const active=$derived(owns&&rs.fase==='recording');
  const changing=$derived(busy||(owns&&(rs.fase==='starting'||rs.fase==='stopping')));
  const retained=$derived(owns&&rs.store==='ram'&&rs.fase!=='recording');
  const reason=$derived(!available?unavailableReason:!bridge?'AUSENTE: recording bridge':changing?'recording operation in progress':retained?'RAM session retained until TTL expiry':undefined);
  const description=$derived([reason,recPrivacy].filter(Boolean).join('. '));
  $effect(()=>{recording;local=null;elapsed=0;});
  // One bounded timer per mounted control; destroy always cancels it. Polling and
  // lifecycle announcements belong to WIRE. Only the location text ticks here.
  onMount(()=>{
    const timer=setInterval(()=>{
      if(owns&&rs.store==='ram'&&rs.ttl_ms!==null&&rs.sinceMonoMs+elapsed<rs.ttl_ms) elapsed+=1000;
    },1000);
    return ()=>clearInterval(timer);
  });
  async function toggle(): Promise<void> {
    if(reason||!bridge){bus?.emit({kind:'refused',scope:slot_id,literal:reason??'AUSENTE: recording bridge'});return;}
    const stopping=active;
    const task=bus?.begin(stopping?'rec-stop':'rec-start',slot_id);busy=true;
    try {
      if(stopping) await bridge.DetenerGrabacionSlot(slot_id,rs.sessionId,epoch,credential);
      else await bridge.IniciarGrabacionSlot(slot_id,xid,epoch,credential);
      const next=await bridge.EstadoGrabacion();
      const doc:unknown=JSON.parse(await bridge.EstadoCanonico());assertState(doc);
      const slot=doc.capsules.find(s=>s.slot_id===slot_id);
      const expected=stopping?'sealed':'recording';
      if(next.slot!==Number(slot_id.slice(7))||next.fase!==expected||slot?.rec_state.phase!==expected||slot.rec_state.store!=='ram') throw new Error('failed: RAM recording not observed');
      local=next;elapsed=0;bus?.ingest(doc,recDiffCtx(next));task?.finish();
    } catch(e:unknown) {
      const literal=clean(e instanceof Error?e.message:String(e));
      bus?.emit({kind:'refused',scope:slot_id,literal});task?.fail(literal);
    } finally {busy=false;}
  }
</script>

{#snippet recButton()}
  <span class="recording-controls">
    <!-- svelte-ignore a11y_role_supports_aria_props_implicit (aria-description is a global ARIA attribute required by B9.) -->
    <button type="button" aria-label={`REC ${slot_id}`} aria-pressed={active}
      aria-disabled={!!reason} aria-description={description} aria-describedby={`rec-location-${slot_id}`}
      title={description} onclick={toggle}>
      <span class:active={active} class:pending={changing} class:sealed={owns&&rs.fase==='sealed'} class:error={owns&&rs.fase==='error'} class="rec-indicator" aria-hidden="true"></span>
      REC {owns ? (rs.fase==='recording'?`recording ${recElapsed(rs)}`:rs.fase==='error'?`error: ${clean(rs.error??'—')}`:rs.fase) : 'idle'}
    </button>
    {#if owns&&rs.fase==='sealed'}<span>{rs.offsetMs===null?'desfase n/a':`desfase ${rs.offsetMs} ms`}</span>{/if}
    <span role="note" id={`rec-location-${slot_id}`} aria-label={`Ubicación grabación ${slot_id}`}>
      {owns?recLocation(rs,elapsed):'—'}
    </span>
  </span>
{/snippet}
{#if children}{@render children(recButton)}{:else}{@render recButton()}{/if}

<style>
  .recording-controls { display: inline-flex; flex-wrap: wrap; align-items: center; gap: var(--space-2, 8px); }
  button { font-size: var(--text-label, 13px); }
  [role="note"] { font-size: var(--text-mono-data, 12px); font-family: var(--font-mono, monospace); }
</style>
