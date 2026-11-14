<script lang="ts">
  // One capsule card (spec section 3): the surface of a capsule at the fixed canvas size, with its chrome laid over it
  // INSIDE that canvas (a 28 px header at the top, a 28 px telemetry strip at the bottom, a mode frame around the
  // edge), so the remote framebuffer is never shrunk by it. Browser, desktop and microvm cards render an Xpra iframe;
  // the terminal card renders an empty surface host that the terminal drawer (xterm.js, another unit) fills through
  // `children`. The Read-Only <-> Operator switch is optimistic: the badge flips at once and the frame is never
  // touched (visor.ts crearConmutacion has no reference to it).
  import { untrack, type Snippet } from 'svelte';
  import type { Geometry, SurfaceCapsule, SlotBlock } from '../types/hypervisor';
  import CapsuleBadge from './CapsuleBadge.svelte';
  import CapsuleBar, { alPointerDown, type CooperationBridge, type LeaseCredential } from '../aiops/CapsuleBar.svelte';
  import { revelarPorTeclado, resolverAtajo } from '../aiops/KeyboardMenu.svelte';
  import { assertState } from '../aiops/state';
  import type { StatusBus } from '../aiops/status';
  import RecordingControls, { type RecordingSnapshot, type RecordingBridge } from '../aiops/RecordingControls.svelte';
  import TelemetryStrip from './TelemetryStrip.svelte';
  import XpraFrame from './XpraFrame.svelte';
  import {
    ARQUETIPOS,
    AUTO_OCULTAR_MS,
    ZONA_HOVER_PX,
    cabeceraVisible,
    crearAdaptadorXpra,
    crearConmutacion,
    debeActivar,
    franjaVisible,
    insigniaAislamiento,
    insigniaModo,
    insigniaSalud,
    tamanoLienzo,
    transferirFoco,
    type ConmutadorCapsula,
    type DestinoConmutacion,
    type EstadoConmutacion,
    type RelojConmutacion,
  } from './visor';

  interface Props {
    metricas?: SlotBlock['telemetry'] | null;
    docTs?: number;
    credential?: LeaseCredential | null;
    recBridge?: RecordingBridge | null;
    recording?: RecordingSnapshot;
    recAvailable?: boolean;
    leaseHolder?: string | null;
    capsula: SurfaceCapsule;
    bridge?: CooperationBridge | null;
    bus?: StatusBus;
    workspace?: number;
    workspaceCapsules?: number;
    epoch?: number;
    geometria: Geometry;
    /** The switch backend; without one the switch control is disabled and says so. */
    conmutador?: ConmutadorCapsula | null;
    reloj?: RelojConmutacion;
    fijado?: boolean;
    autoOcultarMs?: number;
    onactivar?: (id: string) => void;
    onconmutacion?: (estado: EstadoConmutacion) => void;
    onsplit?: (id: string) => void;
    onpop?: (id: string) => void;
    onfold?: (id: string) => void;
    onclose?: (id: string) => void;
    /** REC pressed on this card: the shell starts or stops the forensic recording of the capsule (bridge). */
    onrec?: (id: string) => void;
    /** Content of the surface host of a terminal card. */
    children?: Snippet;
  }

  const RELOJ_NAVEGADOR: RelojConmutacion = {
    ahora: () => performance.now(),
    esperar: (ms) => new Promise((ok) => setTimeout(ok, ms)),
  };

  let {
    capsula,
    credential = null, metricas = null, docTs = 0, recBridge = null, recAvailable = false, leaseHolder = null,
    recording = {fase:'idle',slot:-1,sessionId:'',recording:false,sinceMonoMs:0,duracionMs:0,offsetMs:null,store:null,ttl_ms:null,dir:''},
    bridge = null, bus, workspace = 1, workspaceCapsules = 0, epoch = 0,
    geometria,
    conmutador = null,
    reloj = RELOJ_NAVEGADOR,
    fijado = true,
    autoOcultarMs = AUTO_OCULTAR_MS,
    onactivar,
    onconmutacion,
    onsplit,
    onpop,
    onfold,
    onclose,
    onrec,
    children,
  }: Props = $props();

  const desc = $derived(ARQUETIPOS[capsula.archetype]);
  const usaMarco = $derived(desc.ruta === 'xpra_iframe');
  const lienzo = $derived(tamanoLienzo(geometria.mode));
  const factor = $derived(geometria.scaled ? geometria.scale : 1);

  let takeoverRequested = $state(false);
  let iaSelected = $state(false);
  let iframe = $state<HTMLIFrameElement | null>(null);
  let envoltura = $state<HTMLElement | null>(null);
  let conm = $state<EstadoConmutacion>({ fase: 'reposo', modo: untrack(() => capsula.mode), salud: null, destino: null, ms: null, error: '' });
  let fijadoLocal = $state(untrack(() => fijado));

  // The mode of the card is the capsule's own, except while a switch is under way or after one that settled or failed:
  // then it is the switch's, until the capsule's mode changes again (a barge-in repaints the card at once).
  const modo = $derived(conm.fase === 'reposo' ? capsula.mode : conm.modo);
  const salud = $derived(conm.salud ?? capsula.health);

  $effect.pre(() => {
    void capsula.mode;
    untrack(() => {
      if (conm.fase === 'asentado' || conm.fase === 'degradado') conm = { fase: 'reposo', modo: capsula.mode, salud: null, destino: null, ms: null, error: '' };
    });
  });

  const adaptador = crearAdaptadorXpra({ iframe: () => iframe, envoltura: () => envoltura, geometria: () => geometria });

  $effect(() => {
    if (envoltura === null) return;
    void geometria.scaled;
    void geometria.scale;
    adaptador.fitLocal();
  });

  $effect(() => {
    if (envoltura === null) return;
    adaptador.setViewOnly(modo !== 'operator_controlled');
  });

  // Zero-click-loss: a pointerdown notice from THIS frame activates and focuses the card, except under an agent lease.
  $effect(() => {
    const f = iframe;
    if (f === null) return;
    const alMensaje = (ev: MessageEvent): void => {
      if (ev.origin !== new URL(f.src).origin || ev.source !== f.contentWindow || ev.data?.tipo !== 'superficie:pointerdown') return;
      if (alPointerDown(modo, iaSelected) === 'takeover') {
        requestTakeover();
        return;
      }
      if (!debeActivar({ origin: ev.origin, source: ev.source, data: ev.data }, { origin: new URL(f.src).origin, source: f.contentWindow }, modo)) return;
      adaptador.focus();
      onactivar?.(capsula.id);
    };
    window.addEventListener('message', alMensaje);
    return () => window.removeEventListener('message', alMensaje);
  });

  // ---- overlays: hover on the edge zone, keyboard focus on a control, pinned, or recently used ----
  let sobreCabecera = $state(false);
  let foco = $state(false);
  let actividad = $state(false);
  let temporizador: ReturnType<typeof setTimeout> | undefined;

  function activar(): void {
    actividad = true;
    clearTimeout(temporizador);
    temporizador = setTimeout(() => (actividad = false), autoOcultarMs);
  }
  $effect(() => () => clearTimeout(temporizador));

  const entrada = $derived({ fijado: fijadoLocal, foco, actividadReciente: actividad, salud, modo });
  // The pointer can be tracked only over the header itself (the strip has no control, so it never sees one) and only
  // by the edge zones' entry: over the cross-origin frame no pointer event reaches this document.
  const cabeceraActiva = $derived(cabeceraVisible({ ...entrada, sobreZona: sobreCabecera }) || revelarPorTeclado({focusWithin:foco, menuOpen:false, dialogOpen:takeoverRequested, pinned:fijadoLocal}));
  const franjaActiva = $derived(franjaVisible({ ...entrada, sobreZona: false }));

  // ---- the switch ----
  const destinoSiguiente = $derived<DestinoConmutacion | null>(
    conm.fase === 'degradado' ? conm.destino : modo === 'read_only' ? 'operador' : modo === 'operator_controlled' ? 'solo_lectura' : null,
  );
  const etiquetaConmutar = $derived(destinoSiguiente === 'solo_lectura' ? 'Read-only' : 'Take control');

  async function conmutar(): Promise<void> {
    if (conmutador === null || destinoSiguiente === null || conm.fase === 'conmutando') return;
    await crearConmutacion({
      slot: capsula.slot,
      puertoXpra: capsula.xpraPort,
      modoInicial: modo,
      conmutador,
      reloj,
      alCambiar: (e) => {
        conm = e;
        if (e.error) bus?.emit({kind:'degraded',scope:capsula.id,literal:e.error});
        onconmutacion?.(e);
      },
      alAsentar: (destino) => {
        adaptador.fitLocal();
        if (destino === 'operador') {
          transferirFoco(reloj, () => adaptador.focus()).catch((e: unknown) => {
            conm = { ...conm, error: `settled, but the focus transfer failed: ${e instanceof Error ? e.message : String(e)}` };
          });
        }
      },
    }).conmutar(destinoSiguiente);
  }

  /** WIRE calls this for KEYS K4b on the outer viewport. */
  export function requestTakeover(): void {
    if (takeoverRequested || alPointerDown(modo, iaSelected) !== 'takeover') return;
    takeoverRequested=true;
    bus?.emit({kind:'lease',scope:capsula.id,what:'takeover-requested'});
  }
  export function isIASelected(): boolean { return iaSelected; }
  export function clearTakeover(): void { takeoverRequested=false; iaSelected=false; }
  function cancelTakeover(): void {
    takeoverRequested = false;
    bus?.emit({kind:'lease',scope:capsula.id,what:'takeover-declined'});
  }
  async function takeover(): Promise<void> {
    const task = bus?.begin('switch-mode', capsula.id);
    try {
      if (!bridge) throw new Error('AUSENTE: cooperation bridge');
      await bridge.TomarControl(capsula.id,epoch);
      const doc: unknown = JSON.parse(await bridge.EstadoCanonico());
      assertState(doc); bus?.ingest(doc);
      if (!doc.capsules.some(c=>c.slot_id===capsula.id && c.mode==='operator_controlled' && c.lease_holder===null)) throw new Error('failed: takeover not observed');
      takeoverRequested=false; iaSelected=false; task?.finish();
    } catch(e) { task?.fail(e instanceof Error?e.message:String(e)); }
  }
  let cancelButton = $state<HTMLButtonElement | undefined>(undefined);
  $effect(() => { if (takeoverRequested) cancelButton?.focus(); });
  function tilePointerDown(event: PointerEvent): void {
    const target = event.target;
    if (target instanceof Element && target.closest('button, [role="toolbar"], [role="alertdialog"]')) return;
    if (alPointerDown(modo, iaSelected) === 'takeover') {
      event.preventDefault();
      event.stopPropagation();
      requestTakeover();
    }
  }
  function viewportKey(event: KeyboardEvent): void {
    if (event.key === 'Tab' && takeoverRequested) {
      const dialog = (event.currentTarget as HTMLElement).querySelector('[role="alertdialog"]');
      const buttons = dialog?.querySelectorAll<HTMLButtonElement>('button');
      if (buttons?.length) {
        event.preventDefault();
        const index = Array.from(buttons).indexOf(event.target as HTMLButtonElement);
        buttons[(index + (event.shiftKey ? -1 : 1) + buttons.length) % buttons.length].focus();
      }
      return;
    }
    if (event.key === 'Escape' && takeoverRequested) { event.preventDefault(); cancelTakeover(); return; }
    const target = event.target;
    if (!(target instanceof Element) || !target.closest('[data-superficie-host], [data-viewport]')) return;
    const action = resolverAtajo(event,{target,viewportSlotId:capsula.id,slotMode:()=>modo,iaSelected:()=>iaSelected});
    if (action?.kind === 'prompt_takeover') {
      event.preventDefault(); event.stopPropagation(); requestTakeover();
    }
  }
</script>

<!-- svelte-ignore a11y_no_noninteractive_element_interactions, a11y_no_noninteractive_tabindex (The inert viewport needs a keyboard takeover target.) -->
<article
  onkeydown={viewportKey}
  onpointerdown={tilePointerDown}
  class="tarjeta"
  data-tarjeta
  data-viewport
  role="group"
  tabindex="0"
  data-id={capsula.id}
  data-slot={capsula.slot}
  data-arquetipo={capsula.archetype}
  data-modo={modo}
  data-salud={salud}
  data-fase={conm.fase}
  data-ms={conm.ms ?? ''}
  aria-label="{capsula.name} · {desc.etiqueta} capsule {capsula.id}"
  style:width="{lienzo.w * factor}px"
  style:height="{lienzo.h * factor}px"
  style:--zona-hover="{ZONA_HOVER_PX}px"
>
  <div class="surface-input" data-viewport inert={modo !== 'operator_controlled'}>
  {#if usaMarco}
    <XpraFrame puerto={capsula.xpraPort} tamano={lienzo} titulo={capsula.name} bind:iframe bind:envoltura />
  {:else}
    <div class="anfitrion" data-superficie-host data-arquetipo="terminal" style:width="{lienzo.w * factor}px" style:height="{lienzo.h * factor}px">
      {@render children?.()}
    </div>
  {/if}

  </div>

  <div class="marco-modo" data-marco-modo aria-hidden="true"></div>
  {#if conm.fase === 'conmutando'}
    <div class="barrido" data-barrido aria-hidden="true"></div>
  {/if}

  <!-- Edge zones: armed only while their overlay is hidden, so the first pointer entry reveals it. An element
       that leaves the DOM never gets its pointerleave, so a zone only starts the auto-hide timer and holds no state. -->
  {#if !cabeceraActiva}
    <!-- svelte-ignore a11y_no_static_element_interactions -->
    <div class="zona zona-superior" data-zona="superior" onpointerenter={activar}></div>
  {/if}
  {#if !franjaActiva}
    <!-- svelte-ignore a11y_no_static_element_interactions -->
    <div class="zona zona-inferior" data-zona="inferior" onpointerenter={activar}></div>
  {/if}

  <!-- svelte-ignore a11y_no_static_element_interactions -->
  <header
    class="cabecera"
    data-cabecera
    data-visible={cabeceraActiva}
    onpointerenter={() => { sobreCabecera = true; activar(); }}
    onpointerleave={() => (sobreCabecera = false)}
    onfocusin={() => { foco = true; activar(); }}
    onfocusout={() => (foco = false)}
  >
    <span class="glifo" aria-hidden="true">{desc.glifo}</span>
    <span class="titulo" data-campo="titulo">{capsula.name}</span>
    <CapsuleBadge insignia={insigniaAislamiento(capsula.tier)} />
    <CapsuleBadge insignia={insigniaModo(modo)} />
    <CapsuleBadge insignia={insigniaSalud(salud)} />
    <span class="espacio"></span>
    <RecordingControls slot_id={capsula.id} {credential} {recording} {epoch} bridge={recBridge} {bus} available={recAvailable}>
      {#snippet children(recButton)}
    <CapsuleBar {credential} rec={recButton} slot_id={capsula.id} mode={modo} window_mode={geometria.mode} {workspace} {workspaceCapsules} {epoch} {bridge} {bus}
      pinned={fijadoLocal} onpin={() => (fijadoLocal = !fijadoLocal)}
      onfold={onfold ? () => onfold?.(capsula.id) : undefined}
      onpop={onpop ? () => onpop?.(capsula.id) : undefined}
      onmode={(operator) => { iaSelected = !operator; }} />
      {/snippet}
    </RecordingControls>
  </header>

  {#if takeoverRequested}
    <div class="aviso" data-takeover role="alertdialog" aria-modal="true" aria-label="Take over {capsula.id} from {leaseHolder ?? capsula.pbx?.holder ?? 'IA mode'}">
      <span>Take over {capsula.id} from {leaseHolder ?? capsula.pbx?.holder ?? 'IA mode'}</span>
      <button type="button" aria-label="Confirm takeover {capsula.id}" onclick={takeover}>Take over</button>
      <button type="button" bind:this={cancelButton} aria-label="Cancel takeover {capsula.id}" onclick={cancelTakeover}>Cancel</button>
    </div>
  {/if}
  {#if conm.error !== ''}
    <div class="aviso" data-aviso>{conm.error}</div>
  {/if}

  <TelemetryStrip {capsula} {geometria} {metricas} {docTs} visible={franjaActiva} />
</article>

<style>
  .surface-input { position: absolute; inset: 0; }
  .surface-input[inert] { pointer-events: none; }
  .tarjeta {
    position: relative;
    box-sizing: border-box;
    flex: none;
    overflow: hidden;
    background: var(--surface-obsidian);
  }

  .anfitrion {
    position: absolute;
    top: 0;
    left: 0;
    overflow: hidden;
    background: var(--canvas-void);
  }

  /* Mode frame, drawn over the edge of the canvas and never in the layout: 1 px amber READ-ONLY, 2 px emerald
     OPERATOR-CONTROL, 2 px crimson AGENT-HELD, amber pulse while SWITCHING (spec 4). */
  .marco-modo {
    position: absolute;
    inset: 0;
    box-sizing: border-box;
    pointer-events: none;
    border: 1px solid var(--accent-amber);
  }
  .tarjeta[data-modo='operator_controlled'] .marco-modo {
    border: 2px solid var(--accent-emerald);
  }
  .tarjeta[data-modo='agent_locked'] .marco-modo {
    border: 2px solid var(--accent-crimson);
  }
  .tarjeta[data-modo='switching'] .marco-modo {
    animation: marco-pulso 800ms ease-in-out infinite;
  }

  /* 6 % cyan scanline sweep over the last frame while switching. */
  .barrido {
    position: absolute;
    inset: 0;
    pointer-events: none;
    background: linear-gradient(to bottom, transparent 0%, color-mix(in srgb, var(--accent-cyan) 6%, transparent) 50%, transparent 100%);
    background-size: 100% 96px;
    background-repeat: no-repeat;
    animation: barrido 800ms linear infinite;
  }

  .zona {
    position: absolute;
    left: 0;
    right: 0;
    height: var(--zona-hover);
  }
  .zona-superior {
    top: 0;
  }
  .zona-inferior {
    bottom: 0;
  }

  .cabecera {
    position: absolute;
    top: 0;
    left: 0;
    right: 0;
    box-sizing: border-box;
    display: flex;
    align-items: center;
    gap: 0.5rem;
    height: var(--overlay-head-h);
    padding: 0 0.75rem;
    overflow: hidden;
    white-space: nowrap;
    background: color-mix(in srgb, var(--surface-cockpit) 88%, transparent);
    border-bottom: 1px solid var(--border-subtle);
    color: var(--text-polar);
    font: var(--text-sm);
    pointer-events: none;
    opacity: 0;
    transition: opacity 120ms ease-out;
  }
  .cabecera[data-visible='true'] {
    opacity: 1;
    pointer-events: auto;
  }

  .glifo {
    flex: none;
    color: var(--text-polar);
  }
  .titulo {
    flex: none;
    max-width: 22rem;
    overflow: hidden;
    text-overflow: ellipsis;
    color: var(--text-polar);
    font-weight: 600;
  }
  .espacio {
    flex: 1;
  }

  /* The literal backend error of a failed switch: an opaque chip, so its contrast never depends on the picture. */
  .aviso {
    position: absolute;
    top: var(--overlay-head-h);
    left: 0;
    right: 0;
    box-sizing: border-box;
    padding: 0.25rem 0.75rem;
    background: var(--canvas-void);
    border-left: 3px solid var(--accent-amber);
    color: var(--text-polar);
    font: var(--text-xs);
    overflow-wrap: anywhere;
    pointer-events: none;
  }

  @keyframes marco-pulso {
    0%,
    100% {
      border-color: var(--accent-amber);
    }
    50% {
      border-color: transparent;
    }
  }
  @keyframes barrido {
    from {
      background-position: 0 -96px;
    }
    to {
      background-position: 0 100%;
    }
  }

  /* Reduced motion replaces every pulse, sweep and blink with a static state (spec 9, F-12). */
  @media (prefers-reduced-motion: reduce) {
    .tarjeta[data-modo='switching'] .marco-modo {
      animation: none;
    }
    .barrido {
      animation: none;
      display: none;
    }
    .cabecera {
      transition: none;
    }
  }
</style>
