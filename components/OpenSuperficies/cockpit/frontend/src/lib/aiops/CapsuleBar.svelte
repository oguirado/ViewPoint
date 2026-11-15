<script module lang="ts">
  import type { Snippet } from 'svelte';
  import type { AriaAttributes } from 'svelte/elements';
  import type { OperationMode, WindowMode, CockpitState } from '../types/hypervisor';
  import type { StatusBus, TaskName } from './status';
  // ARIA 1.3 global attribute omitted by the pinned Svelte DOM types.
  declare module 'svelte/elements' { interface AriaAttributes { 'aria-description'?: string | null; } }
  export interface LeaseCredential { holder: string; epoch: number; token: string }
  export interface CooperationBridge {
    CopiarSlot(slot: string, request: 'uri' | 'bloque' | {kind: 'uri' | 'bloque'; credential: LeaseCredential}): Promise<{Seq: number; Kind: string; Bytes: string}>;
    CederControl(slot: string, epoch: number): Promise<void>;
    TomarControl(slot: string, epoch: number): Promise<void>;
    CerrarSlot(slot: string, request: number | {epoch: number; credential: LeaseCredential}): Promise<void>;
    FijarDisposicion(ws: number, layout: string, epoch: number, cred: LeaseCredential | null): Promise<void>;
    EstadoCanonico(): Promise<string>;
  }
  export function copyCycle(previous: number | null, now: number, held: number): 'uri' | 'bloque' {
    return held >= 500 || (previous !== null && now >= previous && now - previous <= 10_000) ? 'bloque' : 'uri';
  }
  export function alPointerDown(mode: OperationMode, iaSelected: boolean): 'takeover' | 'activate' {
    return mode === 'agent_locked' || (mode === 'read_only' && iaSelected) ? 'takeover' : 'activate';
  }
  export function alTecla(key: string, dialog: string | null): string | null { return key === 'Escape' ? null : dialog; }
</script>

<script lang="ts">
  import { assertState } from './state';
  import { latestWins, legalLayouts } from './splittree';
  import { rovingTecla } from './KeyboardMenu.svelte';
  interface Props {
    rec?: Snippet;
    slot_id: string;
    mode: OperationMode;
    window_mode: WindowMode;
    workspace?: number;
    workspaceCapsules?: number;
    epoch?: number;
    pinned?: boolean;
    folded?: boolean;
    bridge?: CooperationBridge | null;
    bus?: StatusBus;
    credential?: LeaseCredential | null;
    onpin?: () => void;
    onsplit?: () => void;
    onfold?: () => void;
    onpop?: () => void;
    onmode?: (operator: boolean) => void;
  }
  let { rec, slot_id, mode, window_mode, workspace = 1, workspaceCapsules = 0, epoch = 0, pinned = true, folded = false,
    bridge = null, bus, credential = null, onpin, onfold, onpop, onmode }: Props = $props();
  const queueMode = latestWins(150);
  $effect(() => () => queueMode.cancel());
  let copying = $state(false);
  let lastCopy = $state<number | null>(null);
  let pressedAt: number | null = null;
  let busy = $state(false);
  let modeGroup = $state<HTMLDivElement | undefined>(undefined);
  const splitLegal = $derived(legalLayouts(window_mode,false,workspaceCapsules).includes('1x2'));
  const splitReason = $derived(!splitLegal ? 'Split requires 1/3 dual mode and two capsules' : !bridge ? 'AUSENTE: cooperation bridge' : undefined);
  const nextCopy = $derived(lastCopy === null ? 'uri; activate again within 10 seconds for bloque, or hold 500 ms' : 'bloque within 10 seconds; otherwise uri; hold 500 ms for bloque');
  function refused(reason: string): void { bus?.emit({kind:'refused', scope:slot_id, literal:reason}); }
  async function observed(): Promise<CockpitState> {
    if (!bridge) throw new Error('AUSENTE: cooperation bridge');
    const doc: unknown = JSON.parse(await bridge.EstadoCanonico());
    assertState(doc); bus?.ingest(doc); return doc;
  }
  async function perform(task: TaskName, action: () => Promise<void>, verify: ((doc: CockpitState) => boolean) | undefined = undefined): Promise<boolean> {
    if (busy) { refused('operation in progress'); return false; }
    const handle = bus?.begin(task, slot_id);
    busy = true;
    try {
      await action(); const doc = await observed();
      if (verify && !verify(doc)) throw new Error(task==='switch-mode' ? 'MODE MISMATCH' : 'failed: state change not observed');
      handle?.finish(); return true;
    } catch (e: unknown) {
      const literal = e instanceof Error ? e.message : String(e);
      if (literal==='MODE MISMATCH') bus?.emit({kind:'degraded',scope:slot_id,literal});
      else if (literal.includes('OPERATOR-CONTROL')) bus?.emit({kind:'lease',scope:slot_id,what:'refused'});
      else refused(literal);
      handle?.fail(literal); return false;
    } finally { busy = false; }
  }
  async function copy(held = 0): Promise<void> {
    if (!bridge) { refused('AUSENTE: cooperation bridge'); return; }
    if (copying) return;
    copying = true;
    const now = performance.now(); const kind = copyCycle(lastCopy,now,held);
    await perform('copy-slot', async () => { await bridge!.CopiarSlot(slot_id,credential ? {kind,credential} : kind); lastCopy = kind === 'uri' ? now : null; });
    copying = false;
  }
  async function selectMode(operator: boolean): Promise<void> {
    if (!bridge) { refused('AUSENTE: cooperation bridge'); return; }
    const succeeded = await perform('switch-mode', () => {
      bus?.emit({kind:'mode',scope:slot_id,phase:'switching',mode:'switching'});
      return operator ? bridge!.TomarControl(slot_id,epoch) : bridge!.CederControl(slot_id,epoch);
    },
      d => d.capsules.some(c=>c.slot_id===slot_id && c.mode===(operator?'operator_controlled':'read_only') && c.lease_holder===null));
    if (succeeded) onmode?.(operator);
  }
  function modeKey(event: KeyboardEvent): void {
    const current = mode === 'operator_controlled' ? 0 : 1;
    const next = rovingTecla(event.key,current,2);
    if (next === null) return;
    event.preventDefault(); modeGroup?.querySelectorAll<HTMLButtonElement>('[role="radio"]')[next]?.focus();
    queueMode(() => void selectMode(next === 0));
  }
  function unavailable(task: TaskName): void {
    const h = bus?.begin(task,slot_id); refused('RAM store unavailable'); h?.fail('RAM store unavailable');
  }
  function split(): void {
    if (!splitLegal) { refused('Split requires 1/3 dual mode and two capsules'); return; }
    if (!bridge) { refused('AUSENTE: cooperation bridge'); return; }
    void perform('set-layout',()=>bridge!.FijarDisposicion(workspace,'1x2',epoch,credential),d=>d.workspaces.some(w=>w.n===workspace&&w.layout==='1x2'));
  }
  function close(): void {
    if (!bridge) { refused('AUSENTE: cooperation bridge'); return; }
    void perform('close-capsule',()=>bridge!.CerrarSlot(slot_id,credential ? {epoch,credential} : epoch),d=>!d.capsules.some(c=>c.slot_id===slot_id&&c.lifecycle!=='stopped'));
  }
</script>

<div class="capsule-verbs" role="toolbar" aria-label="Capsule verbs {slot_id}">
    <!-- svelte-ignore a11y_role_supports_aria_props_implicit -->
  {#if rec}{@render rec()}{:else}
  <button type="button" aria-label="REC {slot_id}" aria-pressed="false" aria-disabled="true" aria-description="RAM store unavailable" title="RAM store unavailable" onclick={()=>unavailable('rec-start')}>REC</button>
  {/if}
    <!-- svelte-ignore a11y_role_supports_aria_props_implicit -->
  <button type="button" aria-label="Snapshot {slot_id}" aria-disabled="true" aria-description="RAM store unavailable" title="RAM store unavailable" onclick={()=>unavailable('snapshot')}>Snapshot</button>
    <!-- svelte-ignore a11y_role_supports_aria_props_implicit -->
  <button type="button" aria-label="Copiar slot {slot_id}" aria-description={nextCopy} aria-disabled={!bridge || busy}
    onpointerdown={()=>{pressedAt=performance.now();}} onpointercancel={()=>{pressedAt=null;}}
    onkeydown={(e)=>{if(e.repeat){e.preventDefault();return;}if(e.key==='Enter'||e.key===' ') pressedAt=performance.now();}}
    onclick={()=>{const held=pressedAt===null?0:performance.now()-pressedAt;pressedAt=null;void copy(held);}}>Copy slot</button>
  <div role="radiogroup" tabindex="-1" aria-label="Mode {slot_id}" bind:this={modeGroup} onkeydown={modeKey}>
    <!-- svelte-ignore a11y_role_supports_aria_props -->
    <button type="button" role="radio" data-accion="conmutar" aria-label="Op {slot_id}" aria-checked={mode==='operator_controlled'} tabindex={mode==='operator_controlled'?0:-1} aria-disabled={!bridge||busy} aria-description={!bridge?'AUSENTE: cooperation bridge':busy?'operation in progress':undefined} onclick={()=>queueMode(() => void selectMode(true))}>Op</button>
    <!-- svelte-ignore a11y_role_supports_aria_props -->
    <button type="button" role="radio" aria-label="IA {slot_id}" aria-checked={mode!=='operator_controlled'} tabindex={mode==='operator_controlled'?-1:0} aria-disabled={!bridge||busy} aria-description={!bridge?'AUSENTE: cooperation bridge':busy?'operation in progress':undefined} onclick={()=>queueMode(() => void selectMode(false))}>IA</button>
  </div>
    <!-- svelte-ignore a11y_role_supports_aria_props_implicit -->
  <button type="button" data-accion="fijar" aria-label="Pin {slot_id}" aria-pressed={pinned} aria-disabled={!onpin} aria-description={!onpin?'Pin unavailable':undefined} onclick={()=>onpin?onpin():refused('Pin unavailable')}>Pin</button>
    <!-- svelte-ignore a11y_role_supports_aria_props_implicit -->
  <button type="button" aria-label="Split {slot_id}" aria-disabled={!splitLegal||!bridge} aria-description={splitReason} title={splitReason} onclick={split}>Split</button>
    <!-- svelte-ignore a11y_role_supports_aria_props_implicit -->
  <button type="button" aria-label="Fold {slot_id}" aria-expanded={!folded} aria-disabled={!onfold} aria-description={!onfold?'Fold unavailable':undefined} onclick={()=>onfold?onfold():refused('Fold unavailable')}>Fold</button>
    <!-- svelte-ignore a11y_role_supports_aria_props_implicit -->
  <button type="button" aria-label="Pop out {slot_id}" aria-disabled={!onpop} aria-description={!onpop?'Pop out unavailable':undefined} onclick={()=>onpop?onpop():refused('Pop out unavailable')}>Pop out</button>
    <!-- svelte-ignore a11y_role_supports_aria_props_implicit -->
  <button type="button" aria-label="Close {slot_id}" aria-disabled={!bridge||busy} aria-description={!bridge?'AUSENTE: cooperation bridge':busy?'operation in progress':undefined} onclick={close}>Close</button>
</div>

<style>
  .capsule-verbs, [role='radiogroup'] { display:flex; align-items:center; gap:0.125rem; }
  button { font:var(--text-base); color:var(--text-polar); background:var(--surface-cockpit); border:thin solid var(--border-bold); cursor:pointer; white-space:nowrap; }
  button[aria-disabled='true'] { color:var(--text-polar); }
  button[aria-checked='true'], button[aria-pressed='true'] { border-color:var(--accent-emerald); }
  button:focus-visible { outline:0.125rem solid var(--accent-cyan); outline-offset:0.125rem; }
</style>
