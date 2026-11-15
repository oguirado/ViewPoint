<script module lang="ts">
  // VIEW-04 contracts (docs/design/PAQUETE_CLAUDE_DESIGN_VIEW_04.md sections 6 and 8) and the pure geometry of the matrix,
  // exported so tests/workspace-matrix.test.ts can check them without a DOM.
  import type { ElementoDePila, Pantalla } from '../stack/pantallas';
  import type { FenwickArbol } from '../stack/fenwick';

  export interface CapsulaWorkspace extends ElementoDePila {
    id: string;
    title: string;
    pantalla: string;
    isolation_tier: 'DOCKER_L2' | 'PTY_JAULA' | 'KVM_HARDWARE';
    plegada?: boolean;
    health_status: 'HEALTHY' | 'WARNING' | 'CRITICAL';
    telemetry: { cpu_pct: number; memory_mb: number };
    ports?: { cdp: number | null; xpra: number };
  }

  /** The eight data states of section 8. `background`, `orphaned`, `empty` and `heir` are per tile; the rest are per view. */
  export type EstadoMatriz = 'default' | 'background' | 'orphaned' | 'empty' | 'heir' | 'loading' | 'error' | 'disabled';

  /** Guarded closure: the continuous hold the actuator demands, in ms [TARGET]. */
  export const HOLD_MS = 1500;
  /** The hold is sampled from the wall clock on this period. */
  export const TICK_MS = 100;
  /** Transient badges (MIGRATING, HEIR COMMITTED) stay this long. */
  export const TRANSITORIO_MS = 1200;
  /** Circumference of the radial timer (r = 13 in a 32-unit box), the dash length the arc fills. */
  export const RADIAL_LARGO = 2 * Math.PI * 13;

  export interface RectMatriz {
    x: number;
    y: number;
    w: number;
    h: number;
  }

  /**
   * Tile of every screen inside a matrix of `ancho` x `alto`: two columns, `pad` around, `gap` between, the last tile
   * spanning both columns when the count is odd (the prototype's arrangement). Pure arithmetic: the slot layer and the
   * tiles agree without measuring the DOM.
   */
  export function baldosas(n: number, ancho: number, alto: number, pad: number, gap: number): RectMatriz[] {
    if (n <= 0) return [];
    const filas = Math.ceil(n / 2);
    const w = (ancho - pad * 2 - gap) / 2;
    const h = (alto - pad * 2 - gap * (filas - 1)) / filas;
    return Array.from({ length: n }, (_, i) => {
      const fila = Math.floor(i / 2);
      const col = i % 2;
      const ultimaImpar = i === n - 1 && n % 2 === 1;
      return { x: pad + (ultimaImpar ? 0 : col * (w + gap)), y: pad + fila * (h + gap), w: ultimaImpar ? ancho - pad * 2 : w, h };
    });
  }

  /** Heights of the chrome inside a tile: header, metrics bar, footer, inner padding and slot gap. */
  export const BALDOSA = { cabecera: 28, metricas: 24, pie: 24, relleno: 8, hueco: 8 } as const;

  /** Slot k of `total` inside a tile: the slots stack vertically in the tile's body. */
  export function ranura(baldosa: RectMatriz, k: number, total: number): RectMatriz {
    const x = baldosa.x + BALDOSA.relleno;
    const y0 = baldosa.y + BALDOSA.cabecera + BALDOSA.metricas + BALDOSA.relleno;
    const alto = baldosa.h - BALDOSA.cabecera - BALDOSA.metricas - BALDOSA.pie - BALDOSA.relleno * 2;
    const n = Math.max(1, total);
    const h = (alto - BALDOSA.hueco * (n - 1)) / n;
    return { x, y: y0 + k * (h + BALDOSA.hueco), w: baldosa.w - BALDOSA.relleno * 2, h };
  }

  export interface NodoFenwick {
    indice: number;
    id: string;
    altura: number;
    offset: number;
  }

  /** What the BIT inspector shows: one row per card, read from the tree (never from the fixtures). */
  export function nodosFenwick(arbol: FenwickArbol, ids: readonly string[]): NodoFenwick[] {
    return ids.map((id, i) => ({ indice: i, id, altura: arbol.longitud > i ? arbol.altura(i) : 0, offset: arbol.longitud > i ? arbol.offsetDe(i) : 0 }));
  }

  /** Hold progress 0..1 after `elapsedMs`; 1 at HOLD_MS. */
  export function progresoDeHold(elapsedMs: number): number {
    if (!(elapsedMs > 0)) return 0;
    return Math.min(elapsedMs / HOLD_MS, 1);
  }

  /** Alt+1..Alt+9 -> screen index 0..8; null for any other key. */
  export function indicePorAtajo(ev: Pick<KeyboardEvent, 'altKey' | 'key'>): number | null {
    if (!ev.altKey || !/^[1-9]$/.test(ev.key)) return null;
    return Number(ev.key) - 1;
  }

  /** Next screen id for `p<n>` provisioning: the smallest unused number. */
  export function siguienteIdDePantalla(pantallas: readonly Pantalla[]): string {
    let n = pantallas.length + 1;
    while (pantallas.some((p) => p.id === `p${n}`)) n++;
    return `p${n}`;
  }
</script>

<script lang="ts">
  import { tick, untrack, setContext } from 'svelte';
  import type { Snippet } from 'svelte';
  import type { CockpitState, Metric, SlotBlock } from '../types/hypervisor';
  import { canvasToDomRect, legalLayouts, workspaceErrors, FRAMEBUFFER } from '../aiops/splittree';

  import { FRAMEBUFFER_CONTEXT, type FramebufferContext } from '../cards/visor';

  type StatusEvent = {kind:'degraded';scope:string;literal:string}
    | {kind:'task';scope:'cockpit';task:'open-workspace';phase:'started'|'finished'};
  interface Props {
    doc: CockpitState;
    bytes: string;
    status?: {emit(ev: StatusEvent): void};
    layoutControl?: Snippet<[number, CockpitState['workspaces'][number]['layout'], readonly ('1x1'|'1x2'|'2x2')[]]>;
    superficie?: Snippet<[SlotBlock, number]>;
  }
  let {doc, bytes, status, layoutControl, superficie}: Props = $props();
  // Producer state events are the only authority that can replace tile data.
  let snapshot = $state.raw(untrack(()=>doc));
  let stateBytes = $state.raw(untrack(()=>bytes));
  // Snippets may be supplied by another component; the rendering parent owns context.
  setContext<FramebufferContext>(FRAMEBUFFER_CONTEXT,()=>snapshot.schema === 'os.cu.cockpit-state/v2' ? FRAMEBUFFER : null);
  $effect.pre(()=>{
    if(doc.seq !== snapshot.seq) {snapshot=doc;stateBytes=untrack(()=>bytes);}
  });
  let selected = $state<number | null>(null);
  const activaEfectiva = $derived(snapshot.workspaces.some(w=>w.n===selected) ? selected : snapshot.workspaces[0]?.n);
  const bad = $derived(workspaceErrors(snapshot));
  // No timers and no parallel STATUS import; the injected owner deduplicates announcements.
  let announcedSeq: number | null = null;
  const announced = new Set<number>();
  $effect(()=>{
    const current = snapshot;
    const emitter = status;
    if (!emitter) return;
    if (announcedSeq !== current.seq) {announced.clear();announcedSeq=current.seq;}
    for (const n of bad) {
      if(announced.has(n)) continue;
      announced.add(n);
      for(const c of current.capsules.filter(c=>c.workspace===n)) emitter.emit({kind:'degraded',scope:c.slot_id,literal:`GEOMETRY MISMATCH ws ${n}`});
    }
  });
  async function select(n: number): Promise<void> {
    status?.emit({kind:'task',scope:'cockpit',task:'open-workspace',phase:'started'});
    selected=n;
    await tick();
    status?.emit({kind:'task',scope:'cockpit',task:'open-workspace',phase:'finished'});
  }
  function tabKey(ev: KeyboardEvent,n: number): void {
    const workspaces=snapshot.workspaces;
    const index=workspaces.findIndex(w=>w.n===n);
    let next=index;
    if(ev.key==='ArrowRight') next=(index+1)%workspaces.length;
    else if(ev.key==='ArrowLeft') next=(index-1+workspaces.length)%workspaces.length;
    else if(ev.key==='Home') next=0;
    else if(ev.key==='End') next=workspaces.length-1;
    else return;
    ev.preventDefault();
    void select(workspaces[next].n);
    const root=(ev.currentTarget as HTMLElement).parentElement;
    root?.querySelector<HTMLButtonElement>(`#workspace-tab-${workspaces[next].n}`)?.focus();
  }
  function leafIndex(c: SlotBlock): number {
    return snapshot.capsules.filter(s=>s.workspace===c.workspace).findIndex(s=>s.slot_id===c.slot_id);
  }
  function sample(metric: Metric | null): number | null {
    if(!metric || !['cgroup','procfs','srfd','sysfs','vadapt'].includes(metric.source) || !Number.isSafeInteger(metric.value) || metric.value<0 || !Number.isSafeInteger(metric.ts_mono_ms) || metric.ts_mono_ms<1 || metric.ts_mono_ms>snapshot.ts_mono_ms || snapshot.ts_mono_ms-metric.ts_mono_ms>3000) return null;
    return metric.value;
  }
</script>

{#snippet previsualizacion(c: SlotBlock, _i: number)}
  <span class="identity">{c.slot_id} · {c.lifecycle}</span>
{/snippet}

<div class="vista04" data-state="default" data-seq={snapshot.seq} data-bytes-present={stateBytes.length > 0}>
  <div class="workspace-chrome">
    <span>Window mode {snapshot.window_mode}</span>
    <div role="tablist" aria-label="Workspaces">
      {#each snapshot.workspaces as w (w.n)}
        <button type="button" role="tab" id="workspace-tab-{w.n}" aria-label="Workspace {w.n}"
          aria-selected={w.n===activaEfectiva} aria-controls="workspace-panel-{w.n}" aria-describedby="workspace-description-{w.n}"
          tabindex={w.n === activaEfectiva ? 0 : -1} onclick={()=>select(w.n)} onkeydown={(ev)=>tabKey(ev,w.n)}>WS {w.n}</button>
      {/each}
    </div>
  </div>
  {#each snapshot.workspaces as w (w.n)}
    <div role="tabpanel" id="workspace-panel-{w.n}" aria-labelledby="workspace-tab-{w.n}" aria-owns={snapshot.capsules.filter(c=>c.workspace===w.n).map(c=>`tile-${c.slot_id}`).join(' ')}
      style:display={w.n===activaEfectiva ? null : 'none'}>
      <p id="workspace-description-{w.n}">Layout {w.layout}, {snapshot.capsules.filter(c=>c.workspace===w.n).length} capsule(s): {snapshot.capsules.filter(c=>c.workspace===w.n).map(c=>c.slot_id).join(', ')}</p>
      {#if bad.includes(w.n)}<p class="degraded">DEGRADED: GEOMETRY MISMATCH ws {w.n}</p>{/if}
      {#if layoutControl}{@render layoutControl(w.n,w.layout,legalLayouts(snapshot.window_mode,false,snapshot.capsules.length))}{/if}
    </div>
  {/each}
  <!-- One stable slot list, independent of tab selection. Failed geometry never paints a surface. -->
  {#each snapshot.capsules as c, i (c.slot_id)}
    {@const dom = canvasToDomRect(c.geometry,snapshot.workspaces.find(w=>w.n===c.workspace)?.layout==='2x2' ? 0 : leafIndex(c))}
    {@const cpu = sample(c.telemetry.cpu_permille)}
    {@const ram = sample(c.telemetry.mem_bytes)}
    <div class="superficie" id="tile-{c.slot_id}" role="group" aria-label="Tile {c.slot_id}" aria-describedby="geometry-{c.slot_id}"
      data-slot-id={c.slot_id} style:display={c.workspace!==activaEfectiva || bad.includes(c.workspace) ? 'none' : null}
      style:left="{dom.x}px" style:top="{dom.y}px" style:width="{dom.w}px" style:height="{dom.h}px">
      <span id="geometry-{c.slot_id}" class="sr-only">x={c.geometry.x},y={c.geometry.y},w={c.geometry.w},h={c.geometry.h}</span>
      <div class="tile-metrics">
        <span role="group" aria-label="CPU {c.slot_id}" class:telemetry-stale={cpu===null}>{cpu===null ? '—' : `${cpu/10}%`}</span>
        <span role="group" aria-label="RAM {c.slot_id}" class:telemetry-stale={ram===null}>{ram===null ? '—' : `${Math.floor(ram/1048576)} MiB`}</span>
      </div>
      {@render (superficie ?? previsualizacion)(c, i)}
    </div>
  {/each}
</div>

<style>
  .vista04 { --surface: #0e131f; --c-cyan: #00e5ff; position: relative; width: 100%; height: 100%; background: #0e131f; color: #e2e8f4; font-size: 13px; font-variant-numeric: tabular-nums; font-feature-settings: 'tnum' 1, 'zero' 1; }
  .workspace-chrome { position: absolute; z-index: 3; top: 0; left: 3px; right: 4px; height: 40px; display: flex; align-items: center; justify-content: space-between; background: #0e131f; }
  [role="tablist"] { display: flex; gap: 4px; }
  button { font: inherit; min-height: 32px; border: 1px solid #697a98; background: #0e131f; color: #e2e8f4; padding: 4px 8px; }
  button[aria-selected="true"] { border-color: var(--c-cyan); text-decoration: underline; }
  button:focus-visible { outline: 2px solid var(--c-cyan); outline-offset: 2px; }
  [role="tabpanel"] { position: relative; z-index: 4; }
  [role="tabpanel"] p { position: absolute; top: 40px; margin: 0; padding: 4px; background: #0e131f; }
  .superficie { position: absolute; overflow: hidden; }
  .tile-metrics { position: absolute; right: 0; bottom: 0; z-index: 2; display: flex; gap: 12px; background: #0e131f; padding: 4px; font: 12px monospace; }
  .telemetry-stale { color: var(--telemetry-stale, #a5b4ce); }
  .degraded { color: #ffb8b8; }
  .identity { font: 12px monospace; }
  .sr-only { position: absolute; width: 1px; height: 1px; overflow: hidden; clip-path: inset(50%); }
  @media (prefers-reduced-motion: reduce) { .vista04 * { transition: none !important; animation: none !important; } }
</style>
