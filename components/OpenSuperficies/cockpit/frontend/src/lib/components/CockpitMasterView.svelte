<script module lang="ts">
  // VIEW-01 contracts (docs/design/PAQUETE_CLAUDE_DESIGN_VIEW_01.md section 6.1). The nine TEA states are a closed union;
  // every one of them is reachable from the template below and none of them is a sink (section 8).
  export type ViewState =
    | 'default'
    | 'hover'
    | 'active'
    | 'focus'
    | 'disabled'
    | 'loading'
    | 'empty'
    | 'error'
    | 'recovering';

  export interface CapsuleSlot {
    id: string;
    name: string;
    /** srfd slot number (hypervisor.ts: xpraPort = 10020 + slot, cdpPort = 9220 + slot). */
    slot: number;
    tier: 'DOCKER L2' | 'KVM HARDWARE' | 'PTY JAULA';
    mode: 'OPERATOR-CONTROL' | 'READ-ONLY' | 'AGENT-HELD';
    health: 'HEALTHY' | 'WARN' | 'CRIT' | 'OFFLINE';
    queueDepth: number;
    latencyMs: number;
    cpuPercent: number;
    memoryMb: number;
    cdpPort: number;
    xpraPort: number;
  }

  /** Multimodal status encoding (docs/design/DESIGN.md, Contrast & Multimodal Encoding): colour + glyph + text, never colour alone. */
  export const GLIFO_SALUD: Readonly<Record<CapsuleSlot['health'], string>> = {
    HEALTHY: '●',
    WARN: '▲',
    CRIT: '■',
    OFFLINE: '○',
  };

  /** A2-5: the continuous pointer hold the emergency abort demands, in ms [TARGET]. */
  export const ABORT_HOLD_MS = 1500;
  /** Section 8: recovery is non-absorbing and bounded; attempt k must satisfy k < RECOVERY_MAX. */
  export const RECOVERY_MAX = 3;
  /** Section 7.2: srfd estado is polled on this period. */
  export const SRFD_POLL_MS = 5000;
</script>

<script lang="ts">
  // Cockpit Master View (VIEW-01): multi-capsule viewport and master canvas, Svelte 5 runes only.
  //
  // GEOMETRY. Every figure comes from ../geometria (LIENZO_W x LIENZO_H, BARRA_H, DOM_W, GUTTER_IZQ) and from
  // ../terminal/termguard (ALTURA_CAJON, ALTURA_ASA, ALTURA_CABECERA); this file repeats none of them (rule H-6).
  // The only literals below are the two the audit reads as such: `height: 40px` for the global bar and `240px` for
  // the drawer, both equal to BARRA_H and ALTURA_CAJON.
  //
  // ONE WEBGL2 CONTEXT (DEC-4, BLK-07). In the shell (standalone = false) the context belongs to App.svelte and is
  // reached through MasterWebGL2Broker; this component never calls getContext on a canvas of its own. Its own
  // <canvas> is the LIENZO_W x LIENZO_H coordinate envelope of the framebuffer (pointer -> framebuffer mapping) and gets a
  // context only when standalone = true. The broker's scissor is set to the viewport slot, so the shared context
  // paints exactly the region this view shows.
  //
  // PERMANENT MOUNTING (BLK-04). The viewport, the four Bento slots, the drawer and the terminal are mounted once
  // and never leave the tree: the drawer toggles with display, the slots switch with classes, the state banners are
  // absolutely positioned and sized in advance. CLS = 0.000 [TARGET].
  //
  // TERMINAL (ET4-05). The drawer holds one real xterm.js terminal on the DOM renderer, guarded by protegerTerminal
  // (no addon it did not create), read back with leerRenderer, watched for life with vigilarCapas, and clamped to
  // SCROLLBACK_MAX through limitarScrollback.
  import { onMount, untrack, setContext } from 'svelte';
  import { Terminal } from '@xterm/xterm';
  import '@xterm/xterm/css/xterm.css';
  import { ALINEACION_CODEC, BARRA_H, DOM_W, GUTTER_IZQ, LIENZO_H, LIENZO_W, geometriaDeVentana } from '../geometria';
  import { MasterWebGL2Broker } from './MasterCanvas.svelte';
  import CapsuleCard from '../cards/CapsuleCard.svelte';
  import type { CooperationBridge, LeaseCredential } from '../aiops/CapsuleBar.svelte';
  import type { RecordingBridge, RecordingSnapshot } from '../aiops/RecordingControls.svelte';
  import type { StatusBus } from '../aiops/status';
  import { offNominalPolicy, frameScaleQ16, FRAMEBUFFER } from '../aiops/splittree';
  import { FRAMEBUFFER_CONTEXT, type FramebufferContext } from '../cards/visor';
  import { assertState } from '../aiops/state';
  import type { CockpitState, SlotBlock, SurfaceCapsule } from '../types/hypervisor';
  import type { ClaseSrfd } from '../types/hypervisor';
  import { RC_PARADA, SrfdError, causaDeRC, crearClienteSrfd, transporteWails } from '../srfd';
  import {
    ALTURA_ASA,
    ALTURA_CABECERA,
    ALTURA_CAJON,
    SCROLLBACK_MAX,
    leerRenderer,
    limitarScrollback,
    opcionesDeTerminal,
    protegerTerminal,
    vigilarCapas,
  } from '../terminal/termguard';
  import type { EstadoSrfd } from '../types/hypervisor';

  interface Props {
    /** true only when nothing else owns a WebGL2 context (a bench page); the shell passes false. */
    credentials?: Readonly<Record<string, LeaseCredential>>;
    doc?: CockpitState | null;
    bytes?: string;
    bus?: StatusBus;
    bridge?: (CooperationBridge & { AbortarTodo(): Promise<void> }) | null;
    recBridge?: RecordingBridge | null;
    recAvailable?: boolean;
    recording?: RecordingSnapshot;
    standalone?: boolean;
    sharedCanvas?: HTMLCanvasElement | null;
    sharedGl?: WebGL2RenderingContext | null;
    /** Index into the capsule stack; the selected canonical slot. */
    activeSlotIndex?: number;
    onStateChange?: (state: ViewState) => void;
    /** The live srfd table as cockpit slots (App.svelte, cliente.listar()); null keeps an empty capsule placeholder. */
    capsulas?: readonly CapsuleSlot[] | null;
    /** false when a master shell carries the global bar (App.svelte): this bar is hidden and the valley starts at the top. */
    barraPropia?: boolean;
    /** Initial state of the built-in PTY drawer; the shell docks CajonAgentes instead and starts this one collapsed. */
    cajonInicial?: boolean;

  }

  let {
    credentials = {}, doc = null, bytes = '', bus, bridge = null, recBridge = null, recAvailable = false,
    recording = {fase:'idle',slot:-1,sessionId:'',recording:false,sinceMonoMs:0,duracionMs:0,offsetMs:null,store:null,ttl_ms:null,dir:''},
    standalone = false,
    sharedCanvas = null,
    sharedGl = null,
    activeSlotIndex = 1,
    onStateChange,
    capsulas = null,
    barraPropia = true,
    cajonInicial = true,
  }: Props = $props();

  setContext<FramebufferContext>(FRAMEBUFFER_CONTEXT, () => doc?.schema === 'os.cu.cockpit-state/v2' ? FRAMEBUFFER : null);

  // ---- port contract of hypervisor.ts (SurfaceCapsule.xpraPort, SurfaceCapsule.cdpPort) ------------------------
  const PUERTO_XPRA_BASE = 10020;
  const PUERTO_CDP_BASE = 9220;
  const puertos = (slot: number): { xpraPort: number; cdpPort: number } => ({
    xpraPort: PUERTO_XPRA_BASE + slot,
    cdpPort: PUERTO_CDP_BASE + slot,
  });

  // ---- absent-state placeholder -----------------------------------------------------------------------------
  // Missing sensors and capsules stay absent; the canonical surface is the live authority.
  const SESIONES: readonly string[] = [];
  const slots = $state<CapsuleSlot[]>([
    { id:'srf-ng-0', name:'No capsule', slot:0, tier:'DOCKER L2', mode:'READ-ONLY', health:'OFFLINE', queueDepth:0, latencyMs:0, cpuPercent:0, memoryMb:0, ...puertos(0) },
  ]);
  const cards = new Map<string, CapsuleCard>();
  function registerCard(node: HTMLElement, id: string): { destroy(): void } {
    // The component reference is registered by bind:this; remove it when its tile leaves.
    return { destroy() { cards.delete(id); } };
  }
  function surfaceCapsule(block: SlotBlock): SurfaceCapsule {
    return {
      id:block.slot_id, name:block.slot_id, slot:block.slot, archetype:block.archetype,
      tier:block.archetype==='microvm'?'kvm_hardware':block.archetype==='terminal'?'pty_jaula':'docker_l2',
      mode:block.mode, health:block.health, xpraPort:block.xpra_port, cdpPort:block.cdp,
      // This legacy shape is not a sensor source; TelemetryStrip receives block.telemetry separately.
      telemetry:{cpuPercent:0,memoryBytes:0,memoryLimitBytes:0,networkRxRateBps:0,networkTxRateBps:0},
      gates:{}, recording:{recording:false,sessionId:'',targetXid:'',sinceMonoMs:0,offsetMs:null,verdict:''},
      pbx:null, poppedOut:false, occluded:false, createdAt:'',
    };
  }
  export { superficie };

  // ---- 9-state TEA machine -------------------------------------------------------------------------------------
  let viewState = $state<ViewState>('loading');
  let recoveryAttempt = $state(0);
  let errorMessage = $state('');

  // ---- layout and custody --------------------------------------------------------------------------------------
  let drawerOpen = $state(untrack(() => cajonInicial)); // initial value on purpose: the shell decides once
  let activeSlot = $state(untrack(() => activeSlotIndex)); // initial value on purpose: the effect below follows later changes
  let activeSession = $state(0);
  let operatorCustody = $state(true);

  // ---- emergency abort hold ------------------------------------------------------------------------------------
  /** 0..1 fraction of the hold completed; the bar under the button is this wide. */
  let abortHoldProgress = $state(0);
  let abortHolding = $state(false);

  // ---- srfd and telemetry --------------------------------------------------------------------------------------
  type FaseSrfd = 'esperando' | 'listo' | 'sin_puente' | 'error' | 'parada';
  interface SrfdState {
    fase: FaseSrfd;
    estado: EstadoSrfd | null;
    rc: number;
    aviso: string;
  }
  let srfdState = $state<SrfdState>({ fase: 'esperando', estado: null, rc: 0, aviso: '' });

  interface Telemetry {
    utc: string;
    /** Wall-clock round trip of the last srfd estado call, ms [MEASURED]; null before the first answer. */
    ipcRttMs: number | null;
    /** srfd when the daemon answered, none otherwise. */
    fuente: 'srfd' | 'none';
    memTotalMib: number;
    memAvailMib: number;
    memReservedMib: number;
    /** Pointer position in framebuffer coordinates (the coordinate envelope), null when outside. */
    pointer: { x: number; y: number } | null;
    surfaceFps: number;
    frameJitterMs: number;
    ringMib: number;
    ringUsedPct: number;
  }
  const KIB = 1 << 10;
  let telemetry = $state<Telemetry>({
    utc: '--:--:--',
    ipcRttMs: null,
    fuente: 'none',
    memTotalMib: 0,
    memAvailMib: 0,
    memReservedMib: 0,
    pointer: null,
    surfaceFps: 0,
    frameJitterMs: 0,
    ringMib: 0,
    ringUsedPct: 0,
  });

  // ---- derived -------------------------------------------------------------------------------------------------
  const activeCapsule = $derived(slots[activeSlot] ?? slots[0]);
  const macroblockGrid = $derived({
    cols: LIENZO_W / ALINEACION_CODEC,
    rows: LIENZO_H / ALINEACION_CODEC,
    aligned: LIENZO_W % ALINEACION_CODEC === 0 && LIENZO_H % ALINEACION_CODEC === 0,
  });
  const holdPercentage = $derived(Math.round(abortHoldProgress * 100));
  let viewportWidth = $state(DOM_W);
  const geometryPolicy = $derived(offNominalPolicy(viewportWidth));
  const geometria = $derived(geometriaDeVentana(viewportWidth, BARRA_H + LIENZO_H));
  onMount(() => {
    const update = (): void => { viewportWidth = document.documentElement.clientWidth; };
    update();
    const observer = new ResizeObserver(update);
    observer.observe(document.documentElement);
    return () => observer.disconnect();
  });
  const memUsedPct = $derived(telemetry.memTotalMib > 0 ? ((telemetry.memTotalMib - telemetry.memAvailMib) / telemetry.memTotalMib) * 100 : 0);
  const latenciaMs = $derived(telemetry.ipcRttMs ?? 0);
  const udsSano = $derived(srfdState.fase === 'listo');
  const isCritical = $derived(activeCapsule.health === 'CRIT' || viewState === 'error');


  // ---- elements (bind:this on raw state: the mount effects key on them) ----------------------------------------
  let envelopeEl = $state.raw<HTMLCanvasElement | null>(null);
  let viewportEl = $state.raw<HTMLElement | null>(null);
  let terminalHostEl = $state.raw<HTMLDivElement | null>(null);

  const cliente = crearClienteSrfd(transporteWails());
  const broker = MasterWebGL2Broker.getInstance();

  // ---- state machine -------------------------------------------------------------------------------------------
  export function transitionState(next: ViewState, err = ''): void {
    if (next === 'error') { errorMessage = err === '' ? 'unspecified fault' : err; bus?.emit({kind:'degraded',scope:'cockpit',literal:errorMessage}); }
    else if (next !== 'recovering') errorMessage = '';
    if (viewState === next) return;
    viewState = next;
    onStateChange?.(next);
  }

  /** Where the machine rests when nothing is wrong: empty for an idle slot, disabled without custody, default otherwise. */
  function estadoDeReposo(): ViewState {
    if (activeCapsule.health === 'OFFLINE') return 'empty';
    if (!operatorCustody) return 'disabled';
    return 'default';
  }

  /** Non-absorbing recovery, bounded at k < RECOVERY_MAX: each retry re-polls srfd; success rests, failure returns to error. */
  export function retryRecovery(): void {
    if (recoveryAttempt >= RECOVERY_MAX) {
      transitionState('error', `recovery exhausted (k=${recoveryAttempt} >= ${RECOVERY_MAX}): reset required`);
      return;
    }
    recoveryAttempt += 1;
    transitionState('recovering');
    void refrescarSrfd().then((ok) => {
      if (viewState !== 'recovering') return;
      if (ok) {
        recoveryAttempt = 0;
        transitionState(estadoDeReposo());
      } else {
        transitionState('error', srfdState.aviso === '' ? 'srfd did not answer' : srfdState.aviso);
      }
    });
  }

  export function resetState(): void {
    recoveryAttempt = 0;
    errorMessage = '';
    if (srfdState.fase === 'error' || srfdState.fase === 'parada') srfdState = { ...srfdState, fase: 'esperando', rc: 0, aviso: '' };
    transitionState(estadoDeReposo());
  }

  export function toggleDrawer(): void {
    drawerOpen = !drawerOpen;
  }

  export function selectSlot(i: number): void {
    if (i < 0 || i >= slots.length || i === activeSlot) return;
    activeSlot = i;
    if (viewState !== 'error' && viewState !== 'recovering') transitionState(estadoDeReposo());
  }

  function toggleCustody(): void {
    operatorCustody = !operatorCustody;
    if (viewState !== 'error' && viewState !== 'recovering' && viewState !== 'loading') transitionState(estadoDeReposo());
  }

  // pointer and focus transitions of the viewport slot (section 8, rows 2-4)
  const interactivo = (): boolean => viewState === 'default' || viewState === 'hover' || viewState === 'active' || viewState === 'focus';
  function alEntrar(): void {
    if (viewState === 'default') transitionState('hover');
  }
  function alSalir(): void {
    telemetry.pointer = null;
    if (viewState === 'hover' || viewState === 'active') transitionState('default');
  }
  function alPulsar(): void {
    if (interactivo()) transitionState('active');
  }
  function alSoltar(): void {
    if (viewState === 'active') transitionState('hover');
  }
  function alEnfocar(): void {
    if (interactivo()) transitionState('focus');
  }
  function alDesenfocar(): void {
    if (viewState === 'focus') transitionState('default');
  }

  /** Pointer -> framebuffer coordinates through the coordinate envelope (its CSS box maps onto LIENZO_W x LIENZO_H). */
  function alMover(ev: PointerEvent): void {
    const el = envelopeEl;
    if (el === null) return;
    const r = el.getBoundingClientRect();
    if (r.width === 0 || r.height === 0) return;
    const x = Math.round(((ev.clientX - r.left) / r.width) * LIENZO_W);
    const y = Math.round(((ev.clientY - r.top) / r.height) * LIENZO_H);
    telemetry.pointer = x < 0 || y < 0 || x >= LIENZO_W || y >= LIENZO_H ? null : { x, y };
  }

  // ---- emergency abort: 1.5 s continuous hold (A2-5) ------------------------------------------------------------
  let abortStart = 0;
  let abortFrame = 0;

  function pasoDeAborto(ahora: number): void {
    if (!abortHolding) return;
    const fraccion = Math.min((ahora - abortStart) / ABORT_HOLD_MS, 1);
    abortHoldProgress = fraccion;
    if (fraccion >= 1) {
      abortHolding = false;
      abortFrame = 0;
      triggerEmergencyAbort();
      return;
    }
    abortFrame = requestAnimationFrame(pasoDeAborto);
  }

  export function startAbortHold(ev: Event | null = null): void {
    ev?.preventDefault();
    if (abortHolding) return;
    abortHolding = true;
    abortStart = performance.now();
    abortHoldProgress = 0;
    abortFrame = requestAnimationFrame(pasoDeAborto);
  }

  /** Releasing before ABORT_HOLD_MS resets the progress to 0 and triggers nothing. */
  export function cancelAbortHold(): void {
    if (abortFrame !== 0) cancelAnimationFrame(abortFrame);
    abortFrame = 0;
    abortHolding = false;
    abortHoldProgress = 0;
  }

  function alTeclaAborto(ev: KeyboardEvent): void {
    if (ev.key !== ' ' && ev.key !== 'Enter') return;
    if (ev.type === 'keydown') {
      if (!ev.repeat) startAbortHold(ev);
      else ev.preventDefault();
    } else cancelAbortHold();
  }

  const causaDeFallo = (e: unknown): string => (e instanceof SrfdError ? `${e.codigo}: ${e.message}` : e instanceof Error ? e.message : String(e));

  /** srfd's class for a slot's tier; PTY JAULA has no srfd class (AUSENTE: the daemon routes browser, desktop, microvm). */
  const CLASE_DE_TIER: Readonly<Record<CapsuleSlot['tier'], ClaseSrfd>> = { 'DOCKER L2': 'browser', 'KVM HARDWARE': 'microvm', 'PTY JAULA': 'browser' };

  // ---- lifecycle controls (gap matrix 11.1): every one reaches srfd, none only repaints a banner -------------------
  /** DETACH: srfd detener on the active slot; the banner rests in empty once the daemon has answered. */
  export function detachCapsule(): void {
    const target = doc?.capsules[activeSlot] ?? doc?.capsules[0];
    if (!target || !bridge) { bus?.emit({kind:'refused',scope:'cockpit',literal:'AUSENTE: cooperation bridge or capsule'}); return; }
    const task=bus?.begin('close-capsule',target.slot_id);
    void bridge.CerrarSlot(target.slot_id,credentials[target.slot_id] ? {epoch:target.lease_epoch,credential:credentials[target.slot_id]} : target.lease_epoch).then(async()=>{
      const next:unknown=JSON.parse(await bridge!.EstadoCanonico()); assertState(next); bus?.ingest(next);
      if(next.capsules.some(c=>c.slot_id===target.slot_id&&c.lifecycle!=='stopped')) throw new Error('failed: close not observed');
      task?.finish();
    }).catch((e:unknown)=>task?.fail(causaDeFallo(e)));
  }

  /** ATTACH CAPSULE: srfd iniciar on the active slot with the class of its tier; rests on the daemon's answer. */
  export function attachCapsule(): void {
    const objetivo = activeCapsule;
    const clase = CLASE_DE_TIER[objetivo.tier];
    transitionState('loading');
    void cliente
      .iniciar(clase, objetivo.slot)
      .then((info) => {
        const s = slots.find((x) => x.slot === objetivo.slot);
        if (s !== undefined) {
          s.health = info.estado === 'listo' ? 'HEALTHY' : 'WARN';
          s.mode = info.estado === 'listo' ? 'OPERATOR-CONTROL' : 'READ-ONLY';
          if (info.puerto_xpra !== undefined) s.xpraPort = info.puerto_xpra;
          if (info.puerto_cdp !== undefined) s.cdpPort = info.puerto_cdp;
        }
        if (viewState === 'loading') transitionState(estadoDeReposo());
        void refrescarSrfd();
      })
      .catch((e: unknown) => transitionState('error', `iniciar ${clase} slot ${objetivo.slot}: ${causaDeFallo(e)}`));
  }

  /** STREAM ATTACHED: rests only on a real srfd answer; a daemon that does not answer is an error, not a resting state. */
  export function streamAttached(): void {
    void refrescarSrfd().then((ok) => {
      if (ok) transitionState(estadoDeReposo());
      else transitionState('error', `stream attach: ${srfdState.aviso}`);
    });
  }

  /** The halt: srfd detener on the active slot. RC_PARADA from srfd means the stop signal is effective. The wire
   *  (manager.Request) carries no force flag: `fuerza` is AUSENTE in srfd, the daemon's detener IS the halt. */
  export function triggerEmergencyAbort(): void {
    abortHoldProgress = 1;
    const task = bus?.begin('emergency-abort', 'cockpit');
    const held = (doc?.capsules ?? []).filter(c=>c.lease_holder!==null);
    for (const card of cards.values()) card.clearTakeover();
    void (async()=>{
      if (!bridge) throw new Error('AUSENTE: cooperation bridge');
      await bridge.AbortarTodo();
      const next:unknown=JSON.parse(await bridge.EstadoCanonico()); assertState(next); bus?.ingest(next);
      const pending=held.filter(c=>!next.capsules.some(n=>n.slot_id===c.slot_id&&n.lease_holder===null&&n.lease_epoch>c.lease_epoch));
      if(pending.length) throw new Error('lease not revoked: '+pending.map(c=>c.slot_id).join(', '));
      if(next.capsules.some(c=>c.lifecycle!=='stopped')) throw new Error('failed: slots not stopped');
      task?.finish();
    })().catch((e:unknown)=>task?.fail(causaDeFallo(e))).finally(()=>{abortHoldProgress=0;});
  }

  // ---- srfd polling (section 7.2): every answer is typed or an SrfdError, never guessed ------------------------
  async function refrescarSrfd(): Promise<boolean> {
    const t0 = performance.now();
    try {
      const estado = await cliente.estado();
      const rtt = performance.now() - t0;
      srfdState = { fase: 'listo', estado, rc: 0, aviso: '' };
      telemetry.ipcRttMs = Math.round(rtt * 100) / 100;
      telemetry.fuente = 'srfd';
      telemetry.memAvailMib = estado.disponible_mib;
      telemetry.memReservedMib = estado.reservado_mib;
      telemetry.memTotalMib = Math.max(telemetry.memTotalMib, estado.reservado_mib + estado.disponible_mib);
      return true;
    } catch (e) {
      if (e instanceof SrfdError) {
        const fase: FaseSrfd = e.codigo === 'sin_puente' ? 'sin_puente' : e.rc === RC_PARADA ? 'parada' : 'error';
        const aviso = e.codigo === 'rechazado' ? `srfd rc ${e.rc}: ${causaDeRC(e.rc)}` : e.message;
        srfdState = { fase, estado: null, rc: e.rc, aviso };
      } else {
        srfdState = { fase: 'error', estado: null, rc: 0, aviso: e instanceof Error ? e.message : String(e) };
      }
      telemetry.fuente = 'none';
      return false;
    }
  }

  // ---- WebGL2 through the broker only ---------------------------------------------------------------------------
  /** The context this view paints through: the shell's (shared) or, standalone, the one bound to the envelope. */
  function contextoActual(): WebGL2RenderingContext | null {
    if (standalone) return envelopeEl === null ? null : broker.bindContext(envelopeEl);
    if (sharedGl !== null) return sharedGl;
    if (sharedCanvas !== null) return broker.bindContext(sharedCanvas); // the same canvas: getContext returns the same context
    return broker.getContext();
  }

  /** Scissors the shared context to the viewport slot (framebuffer coordinates, origin bottom-left) and clears it to canvas-void. */
  function recortarAlViewport(): void {
    const gl = contextoActual();
    if (gl === null || viewportEl === null) return;
    if (standalone) {
      broker.setScissor(0, 0, LIENZO_W, LIENZO_H);
    } else {
      const lienzo = (sharedCanvas ?? gl.canvas) as HTMLCanvasElement;
      const marco = lienzo.getBoundingClientRect();
      const r = viewportEl.getBoundingClientRect();
      if (marco.width === 0 || marco.height === 0) return;
      const sx = gl.drawingBufferWidth / marco.width;
      const sy = gl.drawingBufferHeight / marco.height;
      const x = Math.max(0, Math.round((r.left - marco.left) * sx));
      const w = Math.max(0, Math.round(r.width * sx));
      const h = Math.max(0, Math.round(r.height * sy));
      const y = Math.max(0, Math.round((marco.bottom - r.bottom) * sy));
      broker.setScissor(x, y, w, h);
    }
    broker.clearScissor(7 / 255, 9 / 255, 14 / 255, 1); // canvas-void #07090E
  }

  $effect(() => {
    // re-runs when the shared canvas/context arrive from App.svelte or the viewport element mounts
    void sharedCanvas;
    void sharedGl;
    void viewportEl;
    void envelopeEl;
    untrack(recortarAlViewport);
  });

  // ---- the drawer terminal (ET4-05): DOM renderer, guarded, watched, clamped ------------------------------------
  let term: Terminal | null = null;

  function ajustarTerminal(t: Terminal, el: HTMLElement): void {
    const pantalla = t.element?.querySelector('.xterm-screen') as HTMLElement | null | undefined;
    if (!pantalla || t.cols < 1 || t.rows < 1 || el.clientWidth === 0 || el.clientHeight === 0) return;
    const celdaAncho = pantalla.clientWidth / t.cols;
    const celdaAlto = pantalla.clientHeight / t.rows;
    if (!(celdaAncho > 0) || !(celdaAlto > 0)) return;
    const cols = Math.max(2, Math.floor(el.clientWidth / celdaAncho));
    const filas = Math.max(1, Math.floor(el.clientHeight / celdaAlto));
    if (cols !== t.cols || filas !== t.rows) t.resize(cols, filas);
  }

  function montarTerminal(el: HTMLDivElement): () => void {
    let t: Terminal;
    try {
      t = protegerTerminal(new Terminal(opcionesDeTerminal({ scrollback: limitarScrollback(SCROLLBACK_MAX) })));
      t.open(el);
      const leido = leerRenderer(el);
      if (leido !== 'dom') throw new Error(`the terminal renders with "${leido}", not the DOM renderer`);
    } catch (e) {
      transitionState('error', `terminal could not be mounted: ${e instanceof Error ? e.message : String(e)}`);
      return () => {};
    }
    term = t;
    ajustarTerminal(t, el);
    const observador = new ResizeObserver(() => ajustarTerminal(t, el));
    observador.observe(el);
    const dejarDeVigilar = vigilarCapas(el, (motivo) => {
      transitionState('error', `termguard: ${motivo}`);
    });
    return () => {
      dejarDeVigilar();
      observador.disconnect();
      term = null;
      try {
        t.dispose();
      } catch (e) {
        console.error(`master view terminal: dispose failed: ${e instanceof Error ? e.message : String(e)}`);
      }
    };
  }

  // The host element is the only reactive read; the mount itself is untracked (it writes reactive state on failure).
  $effect(() => {
    if (doc !== null) return;
    const el = terminalHostEl;
    if (el === null) return;
    return untrack(() => montarTerminal(el));
  });

  $effect(() => {
    if (!drawerOpen) return;
    requestAnimationFrame(() => {
      if (doc !== null) return;
    const el = terminalHostEl;
      if (term !== null && el !== null) ajustarTerminal(term, el);
    });
  });

  // keep the prop authoritative when the parent changes it
  $effect(() => {
    const i = activeSlotIndex;
    untrack(() => selectSlot(i));
  });

  /** The slot an empty srfd table leaves: OFFLINE, so the machine rests in `empty` (section 8) instead of reading undefined. */
  const ranuraVacia = (): CapsuleSlot => ({ id: 'srf-ng-idle', name: 'No capsule attached', slot: 0, tier: 'DOCKER L2', mode: 'READ-ONLY', health: 'OFFLINE', queueDepth: 0, latencyMs: 0, cpuPercent: 0, memoryMb: 0, ...puertos(0) });

  // Live capsules (BACKEND-SRFD-PIPELINE): the srfd table replaces the fixture in place, the slot tabs follow, the
  // active index is clamped and the machine re-rests. The array identity is the only reactive read: App.svelte
  // hands a new array only when the table changed (capsulas.ts mismaTabla).
  $effect(() => {
    const vivas = capsulas;
    if (vivas === null) return;
    untrack(() => {
      slots.splice(0, slots.length, ...(vivas.length === 0 ? [ranuraVacia()] : vivas.map((c) => ({ ...c }))));
      if (activeSlot >= slots.length) activeSlot = slots.length - 1;
      if (viewState !== 'error' && viewState !== 'recovering' && viewState !== 'loading') transitionState(estadoDeReposo());
    });
  });

  function limpiarTerminal(): void {
    term?.clear();
  }

  // ---- lifecycle ----------------------------------------------------------------------------------------------
  onMount(() => {
    if (doc !== null) return () => cancelAbortHold();
    void refrescarSrfd().then(() => {
      if (viewState === 'loading') transitionState(estadoDeReposo());
    });
    const sondeo = window.setInterval(() => void refrescarSrfd(), SRFD_POLL_MS);

    return () => {
      window.clearInterval(sondeo);
      cancelAbortHold();
    };
  });

  const fmt = (n: number, d = 1): string => n.toFixed(d);
  const gib = (mib: number): string => (mib / KIB).toFixed(1);
</script>

<div
  class="vista"
  class:canonical={doc !== null}
  data-testid="cockpit-master-view"
  data-view-state={viewState}
  data-custody={operatorCustody ? 'operator' : 'agent'}
  data-standalone={standalone ? 'true' : 'false'}
  style:width="{DOM_W}px"
  style:--barra-h="{BARRA_H}px"
  style:--lienzo-h="{LIENZO_H}px"
>
  <!-- 1. GLOBAL STATUS BAR (height: 40px = BARRA_H) -->
  <header class="barra-global" style:height="{BARRA_H}px" style:display={barraPropia ? null : 'none'} aria-label="Global status">
    <div class="grupo">
      <span class="marca-v"><span class="punto cian">●</span> OpenSuperficies <span class="ver">v2.4-STABLE</span></span>
      <span class="separador" aria-hidden="true"></span>
      <span class="chip" data-state={viewState}>
        <span class="glifo" class:sano={viewState === 'default' || viewState === 'hover' || viewState === 'active' || viewState === 'focus'} class:aviso={viewState === 'recovering' || viewState === 'loading'} class:critico={viewState === 'error'} class:apagado={viewState === 'empty' || viewState === 'disabled'}>
          {viewState === 'error' ? '■' : viewState === 'recovering' || viewState === 'loading' ? '▲' : viewState === 'empty' || viewState === 'disabled' ? '○' : '●'}
        </span>
        <span class="mono">{viewState.toUpperCase()}</span>
      </span>
    </div>

    <nav class="grupo pestanas-slot" aria-label="Capsule slots">
      {#each slots as s, i (s.id)}
        <button
          type="button"
          class="slot-tab"
          class:activa={i === activeSlot}
          class:apagada={s.health === 'OFFLINE'}
          aria-pressed={i === activeSlot}
          title="{s.id}: {s.name} · {s.tier} · {s.health}"
          onclick={() => selectSlot(i)}
        >
          <span class="glifo" class:sano={s.health === 'HEALTHY'} class:aviso={s.health === 'WARN'} class:critico={s.health === 'CRIT'} class:apagado={s.health === 'OFFLINE'}>{GLIFO_SALUD[s.health]}</span>
          <span class="mono">{i + 1}</span>
        </button>
      {/each}
      <span class="mono contador">{activeSlot + 1}/{slots.length}</span>
    </nav>

    <div class="grupo">
      <span class="medidor" title="IPC round trip of srfd estado ({telemetry.fuente})">
        <span class="etiqueta">LAT</span>
        <span class="valor mono ancho-lat" class:sano={latenciaMs < 5} class:aviso={latenciaMs >= 5}>—</span>
        <span class="barra-medida"><span class="relleno cian" style:width="{Math.min(latenciaMs / 10, 1) * 100}%"></span></span>
      </span>
      <span class="medidor" title="Host memory ({telemetry.fuente}): {telemetry.memAvailMib} MiB available of {telemetry.memTotalMib} MiB">
        <span class="etiqueta">MEM</span>
        <span class="valor mono ancho-mem">—</span>
        <span class="barra-medida"><span class="relleno" class:cian={memUsedPct < 70} class:ambar={memUsedPct >= 70 && memUsedPct < 90} class:carmesi={memUsedPct >= 90} style:width="{memUsedPct}%"></span></span>
      </span>
      <span class="reloj mono" title="UTC">{telemetry.utc}<span class="etiqueta"> UTC</span></span>
      <button type="button" class="custodia" class:agente={!operatorCustody} onclick={toggleCustody} title="Toggle operator / agent custody">
        <span class="punto" class:cian={operatorCustody} class:ambar={!operatorCustody}>●</span>
        <span class="mono">{operatorCustody ? 'OPERATOR-CONTROL' : 'AGENT-HELD'}</span>
      </button>
      <button type="button" class="boton" onclick={toggleDrawer} aria-expanded={drawerOpen} title="Toggle terminal drawer [Ctrl+`]">PTY</button>
    </div>
  </header>

  <!-- 2. CENTRAL VALLEY: 12-column Bento over the LIENZO_W x LIENZO_H canvas envelope -->
  <div class="valle" style:left="{GUTTER_IZQ}px" style:top="{barraPropia ? BARRA_H : 0}px" style:width="{LIENZO_W}px">
    <div class="bento" hidden={doc !== null} inert={doc !== null}>
      <!-- Slot 1: master viewport. It is focusable on purpose: the TEA focus state (section 8, row 4) is the electric cyan ring on this element. -->
      <!-- svelte-ignore a11y_no_noninteractive_tabindex -->
      <section
        class="ranura viewport"
        bind:this={viewportEl}
        tabindex="0"
        aria-label="Master viewport {activeCapsule.id}"
        data-slot="1"
        onpointerenter={alEntrar}
        onpointerleave={alSalir}
        onpointerdown={alPulsar}
        onpointerup={alSoltar}
        onpointermove={alMover}
        onfocus={alEnfocar}
        onblur={alDesenfocar}
      >
        <header class="cabecera-ranura" style:height="{ALTURA_CABECERA}px">
          <span class="izq">
            <span class="glifo" class:sano={activeCapsule.health === 'HEALTHY'} class:aviso={activeCapsule.health === 'WARN'} class:critico={activeCapsule.health === 'CRIT'} class:apagado={activeCapsule.health === 'OFFLINE'}>{GLIFO_SALUD[activeCapsule.health]}</span>
            <span class="valor">{activeCapsule.id.toUpperCase()} // DISPLAY :{activeCapsule.xpraPort - PUERTO_XPRA_BASE}.0</span>
            <span class="cian mono ancho-ejec">[{activeCapsule.health === 'OFFLINE' ? 'IDLE' : 'RUNNING'}]</span>
            <span class="sep-v" aria-hidden="true"></span>
            <span class="etiqueta">RES</span>
            <span class="valor mono">{LIENZO_W} x {LIENZO_H} · — Hz</span>
            <span class="ambar mono">Xpra VP9 (Zero-Copy memfd)</span>
          </span>
          <span class="der">
            <span class="mono etiqueta">MB {macroblockGrid.cols}x{macroblockGrid.rows} {macroblockGrid.aligned ? 'ALIGNED' : 'MISALIGNED'}</span>
            <button type="button" class="boton mini" onclick={detachCapsule}>DETACH</button>
            <button type="button" class="boton mini" onclick={retryRecovery}>RELOAD (k&lt;{RECOVERY_MAX})</button>
          </span>
        </header>

        <div class="marco">
          <canvas
            class="envolvente"
            bind:this={envelopeEl}
            width={LIENZO_W}
            height={LIENZO_H}
            aria-hidden="true"
          ></canvas>

          <div class="halo" class:visible={operatorCustody && interactivo()} aria-hidden="true"></div>
          <span class="rotulo-custodia" class:visible={operatorCustody && interactivo()}>OPERATOR CUSTODY ACTIVE</span>

          <!-- state banners: always mounted, absolutely positioned, one visible at a time (CLS 0) -->
          <div class="banner error" class:visible={viewState === 'error'} aria-hidden={viewState !== 'error'}>
            <span class="titulo"><span class="glifo critico">■</span> CRIT</span>
            <span class="texto">{errorMessage}</span>
            <span class="acciones">
              <button type="button" class="boton" onclick={retryRecovery} disabled={recoveryAttempt >= RECOVERY_MAX}>RETRY (k={recoveryAttempt}/{RECOVERY_MAX})</button>
              <button type="button" class="boton" onclick={resetState}>RESET</button>
            </span>
          </div>
          <div class="banner recuperando" class:visible={viewState === 'recovering'} aria-hidden={viewState !== 'recovering'}>
            <span class="titulo"><span class="glifo aviso">▲</span> RECOVERING {recoveryAttempt}/{RECOVERY_MAX}</span>
            <span class="texto">re-polling srfd estado…</span>
          </div>
          <div class="banner vacio" class:visible={viewState === 'empty'} aria-hidden={viewState !== 'empty'}>
            <span class="titulo"><span class="glifo apagado">○</span> NO CAPSULE IN THIS SLOT</span>
            <span class="texto">{activeCapsule.id} · {activeCapsule.tier} · xpra :{activeCapsule.xpraPort}</span>
            <span class="acciones"><button type="button" class="boton primario" onclick={attachCapsule}>ATTACH CAPSULE</button></span>
          </div>
          <div class="banner cargando" class:visible={viewState === 'loading'} aria-hidden={viewState !== 'loading'}>
            <span class="titulo"><span class="glifo aviso">▲</span> LOADING</span>
            <span class="esqueleto" aria-hidden="true"><i></i><i></i><i></i></span>
            <span class="acciones"><button type="button" class="boton" onclick={streamAttached}>STREAM ATTACHED</button></span>
          </div>
          <div class="banner deshabilitado" class:visible={viewState === 'disabled'} aria-hidden={viewState !== 'disabled'}>
            <span class="titulo"><span class="glifo apagado">○</span> INPUT DISABLED</span>
            <span class="texto">custody is AGENT-HELD: operator input is not forwarded</span>
          </div>
        </div>

      </section>

      <!-- Slot 2: capsule inspector -->
      <section class="ranura tarjeta inspector" data-slot="2" aria-label="Capsule inspector">
        <header class="cabecera-ranura" style:height="{ALTURA_CABECERA}px">
          <span class="valor">CAPSULE INSPECTOR</span>
          <span class="pill" class:cian={activeCapsule.tier === 'DOCKER L2'} class:ambar={activeCapsule.tier === 'KVM HARDWARE'} class:violeta={activeCapsule.tier === 'PTY JAULA'}>{activeCapsule.tier}</span>
        </header>
        <dl class="campos">
          <dt>ID</dt><dd class="mono">{activeCapsule.id}</dd>
          <dt>NAME</dt><dd>{activeCapsule.name}</dd>
          <dt>SLOT</dt><dd class="mono">{activeCapsule.slot}</dd>
          <dt>HEALTH</dt>
          <dd>
            <span class="pill" class:sano={activeCapsule.health === 'HEALTHY'} class:aviso={activeCapsule.health === 'WARN'} class:critico-solido={activeCapsule.health === 'CRIT'} class:apagado={activeCapsule.health === 'OFFLINE'}>{GLIFO_SALUD[activeCapsule.health]} {activeCapsule.health}</span>
          </dd>
          <dt>MODE</dt>
          <dd>
            <span class="conmutador" role="group" aria-label="Operation mode">
              <span class="opcion" class:activa={activeCapsule.mode === 'OPERATOR-CONTROL'}>OPERATOR</span>
              <span class="opcion" class:activa={activeCapsule.mode === 'READ-ONLY'}>READ-ONLY</span>
              <span class="opcion" class:activa={activeCapsule.mode === 'AGENT-HELD'}>AGENT</span>
            </span>
          </dd>
          <dt>PORTS</dt>
          <dd>
            <span class="pill mono">CDP :{activeCapsule.cdpPort}</span>
            <span class="pill mono">XPRA :{activeCapsule.xpraPort}</span>
          </dd>
        </dl>
      </section>

      <!-- Slot 3: hypervisor telemetry -->
      <section class="ranura tarjeta telem" data-slot="3" aria-label="Hypervisor telemetry">
        <header class="cabecera-ranura" style:height="{ALTURA_CABECERA}px">
          <span class="valor">HYPERVISOR TELEMETRY</span>
          <span class="etiqueta mono ancho-fuente">{telemetry.fuente === 'srfd' ? 'LIVE' : '—'}</span>
        </header>
        <div class="celdas">
          <div class="celda">
            <span class="etiqueta">UDS SOCKET</span>
            <span class="grande mono" class:sano={udsSano} class:aviso={srfdState.fase === 'esperando'} class:apagado={srfdState.fase === 'sin_puente'} class:critico={srfdState.fase === 'error' || srfdState.fase === 'parada'}>
              {udsSano ? '● UP' : srfdState.fase === 'esperando' ? '▲ WAIT' : srfdState.fase === 'sin_puente' ? '○ NO BRIDGE' : srfdState.fase === 'parada' ? '■ PARADA' : '■ DOWN'}
            </span>
            <span class="nota mono">{srfdState.estado === null ? (srfdState.aviso === '' ? 'srfd.sock' : srfdState.aviso) : `${srfdState.estado.estado} · live ${srfdState.estado.vivas}`}</span>
          </div>
          <div class="celda">
            <span class="etiqueta">DMA BUFFER POOL</span>
            <span class="grande mono">—</span>
            <span class="barra-medida"><span class="relleno cian" style:width="{telemetry.ringUsedPct}%"></span></span>
            <span class="nota mono">—</span>
          </div>
          <div class="celda">
            <span class="etiqueta">SURFACE FPS</span>
            <span class="grande mono sano">—</span>
            <span class="nota mono">—</span>
          </div>
          <div class="celda">
            <span class="etiqueta">FRAME JITTER</span>
            <span class="grande mono cian">—</span>
            <span class="nota mono">—</span>
          </div>
        </div>
      </section>

      <!-- Slot 4: stream state -->
      <section class="ranura tarjeta flujo" data-slot="4" aria-label="Stream state">
        <header class="cabecera-ranura" style:height="{ALTURA_CABECERA}px">
          <span class="valor">STREAM STATE</span>
          <span class="punto-estado" class:sano={udsSano} class:aviso={srfdState.fase === 'esperando'} class:apagado={srfdState.fase === 'sin_puente'} class:critico={srfdState.fase === 'error' || srfdState.fase === 'parada'} title="connection {srfdState.fase}"></span>
        </header>
        <div class="filas">
          <span class="etiqueta">SCALE</span>
          <span class="pill mono" class:cian={!geometria.scaled} class:ambar={geometria.scaled}>{geometria.scaled ? `SCALED ×${geometria.scale.toFixed(2)}` : 'PIXEL-EXACT ×1.00'}</span>
          <span class="etiqueta">IPC RTT</span>
          <span class="valor mono">—</span>
          <span class="etiqueta">SRFD</span>
          <span class="valor mono">{srfdState.fase.toUpperCase()}{srfdState.rc !== 0 ? ` rc ${srfdState.rc}` : ''}</span>
          <span class="etiqueta">AVAIL</span>
          <span class="valor mono">—</span>
          <span class="etiqueta">POINTER</span>
          <span class="valor mono">{telemetry.pointer === null ? '—' : `${telemetry.pointer.x}, ${telemetry.pointer.y}`}</span>
          <span class="etiqueta">CAPSULE</span>
          <span class="valor mono">—</span>
          <span class="etiqueta">GATE</span>
          <span class="valor mono"><span class="telemetry-stale">—</span></span>
        </div>
      </section>
    </div>

    <!-- 3. TERMINAL DRAWER (240px = ALTURA_CAJON): permanently mounted, toggled with display -->
    <section
      class="cajon"
      data-cajon
      data-estado={drawerOpen ? 'abierto' : 'plegado'}
      aria-label="PTY terminal drawer"
      style="display: {drawerOpen ? 'flex' : 'none'};"
      style:height="{ALTURA_CAJON}px"
    >
      <button type="button" class="asa" style:height="{ALTURA_ASA}px" onclick={toggleDrawer} aria-label="Collapse the terminal drawer" title="Collapse [Ctrl+`]"><span></span></button>
      <header class="cabecera-cajon" style:height="{ALTURA_CABECERA}px">
        <div class="pestanas" role="tablist" aria-label="PTY sessions">
          {#each SESIONES as sesion, i (sesion)}
            <button type="button" role="tab" class="pestana" aria-selected={i === activeSession} tabindex={i === activeSession ? 0 : -1} onclick={() => (activeSession = i)}>
              <span class="glifo" class:sano={i === activeSession} class:apagado={i !== activeSession}>{i === activeSession ? '●' : '○'}</span>
              {sesion}
            </button>
          {/each}
        </div>
        <div class="acciones-cajon">
          <span class="etiqueta mono">DOM RENDERER · SCROLLBACK {SCROLLBACK_MAX.toLocaleString('en-US')}</span>
          <button type="button" class="boton mini" onclick={limpiarTerminal}>CLEAR</button>
          <button type="button" class="boton mini" onclick={toggleDrawer}>DOCK ▾</button>
        </div>
      </header>
      <div class="cuerpo-cajon" style:height="{ALTURA_CAJON - ALTURA_ASA - ALTURA_CABECERA}px">
        <div class="anfitrion" data-terminal-host bind:this={terminalHostEl}></div>
      </div>
    </section>
    <button type="button" class="asa-plegada" style:height="{ALTURA_ASA}px" hidden={drawerOpen} onclick={toggleDrawer} aria-label="Open the terminal drawer" title="Open [Ctrl+`]"><span></span></button>

    <!-- 4. EMERGENCY ABORT: recency corner, 1.5 s hold-to-confirm -->
    <button
      type="button"
      class="abortar"
      class:sosteniendo={abortHolding}
      onpointerdown={startAbortHold}
      onpointerup={cancelAbortHold}
      onpointerleave={cancelAbortHold}
      onpointercancel={cancelAbortHold}
      onkeydown={alTeclaAborto}
      onkeyup={alTeclaAborto}
      oncontextmenu={(e) => e.preventDefault()}
      aria-label="EMERGENCY ABORT all slots: hold 1.5 s"
      title="Hold {ABORT_HOLD_MS} ms to dispatch the stop signal"
    >
      <span class="glifo">■</span>
      <span class="texto-aborto">SAFETY ABORT</span>
      <span class="pct mono">{holdPercentage}%</span>
      <span class="progreso" style:width="{holdPercentage}%"></span>
    </button>
  </div>
</div>

{#snippet superficie(block: SlotBlock, index: number)}
  <div class="canonical-card" class:scaled-framebuffer={doc?.schema === 'os.cu.cockpit-state/v2'} use:registerCard={block.slot_id}>
    {#if geometryPolicy.tier === 'withheld'}
      <p class="telemetry-stale">{geometryPolicy.message}</p>
    {/if}
    <div class="canonical-surface" class:withheld={geometryPolicy.tier === 'withheld'} inert={geometryPolicy.tier === 'withheld'}>
    <CapsuleCard bind:this={cards.get(block.slot_id), (card) => { if (card) cards.set(block.slot_id, card); else cards.delete(block.slot_id); }}
      capsula={surfaceCapsule(block)} geometria={{...geometria,mode:doc?.window_mode ?? geometria.mode,...(doc?.schema === 'os.cu.cockpit-state/v2' ? {scaled:true,scale:frameScaleQ16(block.geometry)/65536} : {})}}
      metricas={block.telemetry} docTs={doc?.ts_mono_ms ?? 0}
      workspace={block.workspace} workspaceCapsules={doc?.capsules.filter(c=>c.workspace===block.workspace).length ?? 0}
      credential={credentials[block.slot_id] ?? null} epoch={block.lease_epoch} leaseHolder={block.lease_holder} {bridge} {bus} {recBridge} {recording} {recAvailable} />
    </div>
  </div>
{/snippet}

<style>
  .vista.canonical { z-index: 30; pointer-events: none; }
  .canonical .abortar { pointer-events: auto; }
  .canonical .bento { display: none; }
  .canonical .cajon, .canonical .asa-plegada { display: none; }
  .canonical-surface.withheld { visibility: hidden; }
  .scaled-framebuffer :global(.tarjeta) { width: 100% !important; height: 100% !important; }
  .canonical-surface { width: 100%; height: 100%; }
  .canonical-card { position: relative; width: 100%; height: 100%; }
  .telemetry-stale { color: var(--telemetry-stale); }

  /* ---- tokens: docs/design/DESIGN.md Option A, Cyberpunk Obsidian (values [CITED docs/design/DESIGN.md]) ---- */
  .vista {
    --c-void: var(--canvas-void, #07090e);
    --c-base: var(--surface-obsidian, #0e131f);
    --c-raised: var(--surface-cockpit, #161d2e);
    --c-overlay: var(--surface-overlay, #1c263b);
    --c-border: var(--border-subtle, #222d42);
    --c-border-strong: var(--border-strong, #334155);
    --c-focus: var(--border-focus, #00e5ff);
    --c-text: var(--text-polar, #f4f6fc);
    --c-text-2: var(--text-silver, #aab6c8);
    --c-text-off: var(--telemetry-stale, #a5b4ce);
    --c-cyan: var(--accent-cyan, #00e5ff);
    --c-cyan-hover: var(--accent-cyan-hover, #33ebff);
    --c-cyan-active: var(--accent-cyan-active, #00b8cc);
    --c-emerald: var(--accent-emerald, #00e676);
    --c-amber: var(--accent-amber, #ffb000);
    --c-crimson: var(--accent-crimson, #ff3355);
    --c-violet: var(--accent-violet, #b388ff);
    --c-destructive: var(--accent-destructive, #991b1b);
    --c-destructive-hover: #7f1d1d;
    --f-sans: var(--font-sans, 'Inter', system-ui, -apple-system, 'Segoe UI', sans-serif);
    --f-mono: var(--font-mono, 'JetBrains Mono', 'SF Mono', Menlo, Consolas, monospace);
    --r-compact: 2px;
    --r-control: 4px;

    position: absolute;
    top: 0;
    left: 0;
    z-index: 10;
    /* the shell's bar and canvas frame keep their place; in the 1/3 column the frame is taller (app.css --canvas-h-activo) */
    height: calc(var(--bar-h, var(--barra-h)) + var(--canvas-h-activo, var(--lienzo-h)));
    box-sizing: border-box;
    overflow: hidden;
    contain: strict;
    pointer-events: auto;
    /* TRANSPARENT ON PURPOSE: the static frame (surface-base) and the shell's scissored WebGL2 canvas paint the
       viewport; this view paints only its chrome over them. An opaque root would hide the frame the instrument
       reads back (medir-cls.mjs: the bounding box of surface-base pixels must be the frame itself). */
    background: transparent;
    color: var(--c-text);
    font: var(--text-label, 13px/20px var(--f-sans));
    letter-spacing: 0.02em;
    user-select: none;
    -webkit-font-smoothing: antialiased;
  }
  .vista *,
  .vista *::before,
  .vista *::after {
    box-sizing: border-box;
    box-shadow: none !important; /* pure flat invariant */
    font-variant-numeric: tabular-nums;
    font-feature-settings: 'tnum' 1, 'zero' 1;
  }
  .vista button {
    font: inherit;
    letter-spacing: inherit;
    color: inherit;
    cursor: pointer;
  }
  .vista button:focus-visible,
  .vista [tabindex]:focus-visible {
    outline: 2px solid var(--c-focus);
    outline-offset: 2px;
  }

  .mono {
    font-family: var(--f-mono);
    font-weight: 500;
  }
  .etiqueta {
    color: var(--c-text-2);
    font: var(--text-label, 13px/20px var(--f-sans));
    letter-spacing: 0.03em;
  }
  .valor {
    color: var(--c-text);
  }
  .cian { color: var(--c-cyan); }
  .ambar { color: var(--c-amber); }
  .sano { color: var(--c-emerald); }
  .aviso { color: var(--c-amber); }
  .critico { color: var(--c-crimson); }
  .apagado { color: var(--c-text-off); }
  .glifo {
    display: inline-block;
    width: 1em;
    text-align: center;
    font-family: var(--f-mono);
  }
  .punto {
    font-family: var(--f-mono);
    font-size: 12px;
  }

  /* ---- 1. global bar: height 40px (= BARRA_H) ----
     Painted with surface-raised, like the static shell's bar: the measuring instrument (scripts/ng/medir-cls.mjs,
     COLOR_LIENZO) reads the canvas frame back from a screenshot as the bounding box of surface-base #0E131F pixels,
     so nothing outside the LIENZO_W x LIENZO_H frame may carry that exact colour. */
  .barra-global {
    position: absolute;
    top: 0;
    left: 0;
    right: 0;
    height: 40px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    padding: 0 12px;
    background: var(--c-raised);
    border-bottom: 1px solid var(--c-border);
    white-space: nowrap;
    overflow: hidden;
  }
  .grupo {
    display: flex;
    align-items: center;
    gap: 8px;
    height: 100%;
    min-width: 0;
  }
  .marca-v {
    font: 600 14px/1.4 var(--f-sans);
    letter-spacing: -0.01em;
    color: var(--c-text);
  }
  .marca-v .ver {
    margin-left: 4px;
    font: var(--text-mono-data, 12px/16px var(--f-mono));
    letter-spacing: 0.08em;
    color: var(--c-text-off);
  }
  .separador {
    width: 1px;
    height: 24px;
    background: var(--c-border);
  }
  .sep-v {
    display: inline-block;
    width: 1px;
    height: 12px;
    margin: 0 4px;
    background: var(--c-border-strong);
    vertical-align: middle;
  }
  .chip {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    height: 24px;
    padding: 0 8px;
    background: var(--c-raised);
    border: 1px solid var(--c-border);
    border-radius: var(--r-compact);
    font-size: 12px;
    line-height: 1;
    min-width: 108px; /* fits RECOVERING, the longest state name: the chip never resizes */
    justify-content: center;
  }
  .chip[data-state='error'] { border-color: var(--c-crimson); }
  .chip[data-state='recovering'] { border-color: var(--c-amber); border-style: dashed; }
  .pill {
    display: inline-flex;
    align-items: center;
    gap: 4px;
    height: 18px;
    padding: 0 6px;
    background: var(--c-void);
    border: 1px solid var(--c-border);
    border-radius: var(--r-compact);
    color: var(--c-text-2);
    font: var(--text-mono-data, 12px/16px var(--f-mono));
    letter-spacing: 0.03em;
    white-space: nowrap;
  }
  .pill.cian { color: var(--c-cyan); border-color: var(--c-border-strong); }
  .pill.ambar { color: var(--c-amber); border-color: var(--c-border-strong); }
  .pill.violeta { color: var(--c-violet); border-color: var(--c-border-strong); }
  .pill.sano { color: var(--c-emerald); }
  .pill.aviso { color: var(--c-amber); }
  .pill.apagado { color: var(--c-text-off); }
  /* DELTA-04: a critical badge is a solid chip, #F4F6FC on #991B1B (7.69:1 AAA), never #FF3355 text alone */
  .pill.critico-solido { color: var(--c-text); background: var(--c-destructive); border-color: var(--c-destructive); }

  .pestanas-slot {
    gap: 4px;
    padding: 0 4px;
    height: 32px;
    background: var(--c-void);
    border: 1px solid var(--c-border);
    border-radius: var(--r-compact);
  }
  .slot-tab {
    display: inline-flex;
    align-items: center;
    gap: 4px;
    height: 24px;
    padding: 0 8px;
    background: var(--c-void);
    border: 1px solid var(--c-border);
    border-radius: var(--r-compact);
    color: var(--c-text-2);
    font-size: 12px;
  }
  .slot-tab:hover { color: var(--c-text); border-color: var(--c-border-strong); }
  .slot-tab:active { border-color: var(--c-cyan-active); }
  .slot-tab.activa { background: var(--c-void); border-color: var(--c-cyan); color: var(--c-text); }
  .slot-tab.apagada:not(.activa) { opacity: 0.6; }
  .contador {
    padding: 0 4px;
    color: var(--c-text);
    font-size: 12px;
  }

  .medidor {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    height: 24px;
    padding: 0 8px;
    background: var(--c-raised);
    border: 1px solid var(--c-border);
    border-radius: var(--r-compact);
    font-size: 12px;
  }
  .medidor .ancho-lat { display: inline-block; min-width: 58px; text-align: right; }
  .medidor .ancho-mem { display: inline-block; min-width: 96px; text-align: right; }
  .barra-medida {
    display: inline-block;
    width: 40px;
    height: 6px;
    background: var(--c-void);
    border: 1px solid var(--c-border);
    overflow: hidden;
  }
  .relleno {
    display: block;
    height: 100%;
    background: var(--c-cyan);
  }
  .relleno.cian { background: var(--c-cyan); }
  .relleno.ambar { background: var(--c-amber); }
  .relleno.carmesi { background: var(--c-crimson); }
  .reloj {
    min-width: 84px;
    text-align: right;
    font-size: 12px;
    color: var(--c-text);
  }
  .custodia {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    gap: 6px;
    width: 156px; /* OPERATOR-CONTROL and AGENT-HELD share one box */
    height: 32px;
    padding: 0 8px;
    background: var(--c-void);
    border: 1px solid var(--c-focus);
    border-radius: var(--r-compact);
    color: var(--c-cyan);
    font-size: 12px;
    font-weight: 700;
    letter-spacing: -0.01em;
  }
  .custodia.agente { border-color: var(--c-amber); color: var(--c-amber); }
  .boton {
    display: inline-flex;
    align-items: center;
    gap: 4px;
    height: 32px;
    padding: 0 10px;
    background: var(--c-raised);
    border: 1px solid var(--c-border);
    border-radius: var(--r-compact);
    color: var(--c-text-2);
    font: var(--text-mono-data, 12px/16px var(--f-mono));
  }
  .boton:hover { color: var(--c-text); border-color: var(--c-border-strong); }
  .boton:active { border-color: var(--c-cyan-active); color: var(--c-cyan-active); }
  .boton:disabled { opacity: 0.4; pointer-events: none; }
  .boton.mini { height: 20px; padding: 0 6px; font-size: 12px; background: var(--c-base); }
  .boton.primario { background: var(--c-cyan); border-color: var(--c-cyan); color: var(--c-void); font-weight: 700; }
  .boton.primario:hover { background: var(--c-cyan-hover); }
  .boton.primario:active { background: var(--c-cyan-active); }
  .boton.rec { display: inline-flex; align-items: center; gap: 6px; }
  .boton.rec.grabando { border-color: var(--c-crimson, #ff3355); color: var(--c-text); }

  /* ---- 2. valley and Bento (12 columns x 6 rows, 8 px gap, over the LIENZO_W x LIENZO_H envelope) ---- */
  .valle {
    position: absolute;
    height: var(--canvas-h-activo, var(--lienzo-h));
    overflow: hidden;
    contain: strict;
    background: transparent;
  }
  .bento {
    position: absolute;
    inset: 0;
    display: grid;
    grid-template-columns: repeat(12, minmax(0, 1fr));
    grid-template-rows: repeat(12, minmax(0, 1fr));
    gap: 8px;
    padding: 8px;
  }
  .ranura {
    position: relative;
    display: flex;
    flex-direction: column;
    min-width: 0;
    min-height: 0;
    overflow: hidden;
    background: var(--c-base);
    border: 1px solid var(--c-border);
    border-radius: var(--r-compact);
    contain: layout paint;
  }
  .ranura.viewport { grid-column: 1 / 10; grid-row: 1 / 13; background: transparent; }
  .ranura.inspector { grid-column: 10 / 13; grid-row: 1 / 4; }
  .ranura.telem { grid-column: 10 / 13; grid-row: 4 / 7; }
  .ranura.flujo { grid-column: 10 / 13; grid-row: 7 / 10; }
  .tarjeta { background: var(--c-raised); }

  .cabecera-ranura {
    flex: none;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
    padding: 0 8px;
    background: var(--c-raised);
    font: var(--text-mono-data, 12px/16px var(--f-mono));
    letter-spacing: 0.03em;
    white-space: nowrap;
    overflow: hidden;
  }
  .cabecera-ranura { border-bottom: 1px solid var(--c-border); }
  .cabecera-ranura .izq,
  .cabecera-ranura .der {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    min-width: 0;
  }
  .cabecera-ranura .valor { font-weight: 600; }
  .ancho-ejec { display: inline-block; min-width: 60px; }
  .ancho-fuente { display: inline-block; min-width: 48px; text-align: right; }

  /* viewport: hover raises the border, focus is the electric cyan ring, disabled dims */
  .ranura.viewport:hover { border-color: var(--c-border-strong); }
  .vista[data-view-state='focus'] .ranura.viewport { border-color: var(--c-focus); outline: 2px solid var(--c-focus); outline-offset: -3px; }
  .vista[data-view-state='active'] .ranura.viewport { border-color: var(--c-cyan-active); }
  .vista[data-view-state='error'] .ranura.viewport { border-color: var(--c-crimson); }
  .vista[data-view-state='recovering'] .ranura.viewport { border-color: var(--c-amber); border-style: dashed; }
  .vista[data-view-state='disabled'] .ranura.viewport .marco { opacity: 0.4; }
  .vista[data-view-state='disabled'] .ranura.viewport .envolvente { pointer-events: none; }
  .vista[data-view-state='empty'] .ranura.viewport .marco {
    background-image:
      linear-gradient(to right, rgba(34, 45, 66, 0.45) 1px, transparent 1px),
      linear-gradient(to bottom, rgba(34, 45, 66, 0.45) 1px, transparent 1px);
    background-size: 32px 32px;
  }

  .marco {
    position: relative;
    flex: 1;
    min-height: 0;
    overflow: hidden;
    background: transparent; /* the shell's scissored WebGL2 canvas shows through */
  }
  .vista[data-standalone='true'] .marco { background: var(--c-void); }
  .envolvente {
    position: absolute;
    inset: 0;
    width: 100%;
    height: 100%;
    display: block;
    object-fit: contain;
    background: transparent;
  }
  .halo {
    position: absolute;
    inset: 6px;
    border: 2px solid var(--c-cyan);
    opacity: 0;
    pointer-events: none;
  }
  .halo.visible { opacity: 0.75; }
  .rotulo-custodia {
    position: absolute;
    top: 12px;
    right: 12px;
    padding: 2px 8px;
    background: var(--c-base);
    border: 1px solid var(--c-cyan);
    color: var(--c-cyan);
    font: var(--text-mono-data, 12px/16px var(--f-mono));
    letter-spacing: 0.03em;
    visibility: hidden;
    pointer-events: none;
  }
  .rotulo-custodia.visible { visibility: visible; }

  .banner {
    position: absolute;
    left: 32px;
    right: 32px;
    top: 32px;
    display: flex;
    flex-direction: column;
    gap: 8px;
    padding: 12px;
    background: var(--c-raised);
    border: 2px solid var(--c-border-strong);
    border-radius: var(--r-control);
    visibility: hidden;
    pointer-events: none;
  }
  .banner.visible { visibility: visible; pointer-events: auto; }
  .banner .titulo { font: 700 13px/1.3 var(--f-mono); letter-spacing: 0.02em; }
  .banner .texto { color: var(--c-text-2); font: var(--text-mono-data, 12px/16px var(--f-mono)); white-space: pre-wrap; word-break: break-word; }
  .banner .acciones { display: flex; gap: 8px; margin-top: 4px; }
  .banner.error { border-color: var(--c-crimson); }
  .banner.error .titulo { color: var(--c-text); background: var(--c-destructive); align-self: flex-start; padding: 2px 8px; }
  .banner.recuperando { border-color: var(--c-amber); border-style: dashed; }
  .banner.recuperando .titulo { color: var(--c-amber); }
  .banner.vacio { border-color: var(--c-border); background: var(--c-base); }
  .banner.vacio .titulo { color: var(--c-text-off); }
  .banner.cargando { border-color: var(--c-border-strong); }
  .banner.cargando .titulo { color: var(--c-amber); }
  .banner.deshabilitado { border-color: var(--c-border); }
  .banner.deshabilitado .titulo { color: var(--c-text-off); }
  .esqueleto { display: flex; flex-direction: column; gap: 6px; }
  .esqueleto i { display: block; height: 10px; width: 60%; background: var(--c-overlay); }
  .esqueleto i:nth-child(2) { width: 85%; }
  .esqueleto i:nth-child(3) { width: 40%; }

  /* inspector */
  .campos {
    flex: 1;
    display: grid;
    grid-template-columns: 64px minmax(0, 1fr);
    align-content: start;
    row-gap: 8px;
    column-gap: 8px;
    margin: 0;
    padding: 10px;
    overflow: hidden;
  }
  .campos dt { color: var(--c-text-2); font: var(--text-mono-data, 12px/16px var(--f-mono)); letter-spacing: 0.03em; }
  .campos dd { margin: 0; display: flex; align-items: center; gap: 6px; min-width: 0; font: var(--text-label, 13px/20px var(--f-sans)); color: var(--c-text); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .conmutador {
    display: inline-flex;
    height: 20px;
    border: 1px solid var(--c-border-strong);
    border-radius: var(--r-compact);
    overflow: hidden;
  }
  .opcion {
    display: inline-flex;
    align-items: center;
    padding: 0 6px;
    color: var(--c-text-off);
    font: var(--text-mono-data, 12px/16px var(--f-mono));
    letter-spacing: 0.03em;
    border-right: 1px solid var(--c-border);
  }
  .opcion:last-child { border-right: 0; }
  .opcion.activa { background: var(--c-cyan); color: var(--c-void); font-weight: 700; }

  /* telemetry cells */
  .celdas {
    flex: 1;
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    grid-template-rows: repeat(2, minmax(0, 1fr));
    gap: 8px;
    padding: 8px;
    min-height: 0;
  }
  .celda {
    display: flex;
    flex-direction: column;
    justify-content: space-between;
    gap: 2px;
    padding: 8px;
    min-width: 0;
    background: var(--c-base);
    border: 1px solid var(--c-border);
    border-radius: var(--r-compact);
    overflow: hidden;
  }
  .celda .grande { font-size: 15px; font-weight: 700; color: var(--c-text); white-space: nowrap; }
  .celda .nota { color: var(--c-text-off); font-size: 12px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .celda .barra-medida { width: 100%; }
  .punto-estado {
    width: 10px;
    height: 10px;
    border-radius: 50%;
    background: var(--c-text-off);
    border: 1px solid var(--c-border-strong);
  }
  .punto-estado.sano { background: var(--c-emerald); }
  .punto-estado.aviso { background: var(--c-amber); }
  .punto-estado.critico { background: var(--c-crimson); }
  .punto-estado.apagado { background: var(--c-text-off); }

  /* stream state */
  .filas {
    flex: 1;
    display: grid;
    grid-template-columns: 64px minmax(0, 1fr);
    align-content: start;
    row-gap: 6px;
    column-gap: 8px;
    padding: 8px 10px;
    overflow: hidden;
    white-space: nowrap;
  }
  .filas .etiqueta { line-height: 18px; }
  .filas .valor { font-size: 12px; line-height: 18px; overflow: hidden; text-overflow: ellipsis; }

  /* ---- 3. drawer: 240px (= ALTURA_CAJON), 12 px handle + 28 px header + body, overlaid on the valley ---- */
  .cajon {
    position: absolute;
    left: 0;
    right: 0;
    bottom: 0;
    z-index: 30;
    height: 240px;
    flex-direction: column;
    background: var(--c-base);
    border-top: 1px solid var(--c-border-strong);
    overflow: hidden;
    contain: strict;
  }
  .asa,
  .asa-plegada {
    flex: none;
    display: flex;
    align-items: center;
    justify-content: center;
    width: 100%;
    padding: 0;
    border: 0;
    background: var(--c-raised);
  }
  .asa span,
  .asa-plegada span {
    display: block;
    width: 48px;
    height: 2px;
    background: var(--c-border-strong);
  }
  .asa:hover span,
  .asa-plegada:hover span { background: var(--c-cyan); }
  .asa-plegada {
    position: absolute;
    left: 0;
    right: 0;
    bottom: 0;
    z-index: 30;
    border-top: 1px solid var(--c-border-strong);
  }
  .asa-plegada[hidden] { display: none; }
  .cabecera-cajon {
    flex: none;
    display: flex;
    align-items: stretch;
    justify-content: space-between;
    background: var(--c-raised);
    border-bottom: 1px solid var(--c-border);
  }
  .pestanas {
    display: flex;
    min-width: 0;
    overflow: hidden;
  }
  .pestana {
    flex: none;
    display: inline-flex;
    align-items: center;
    gap: 6px;
    padding: 0 10px;
    border: 0;
    border-right: 1px solid var(--c-border);
    border-top: 2px solid transparent;
    background: transparent;
    color: var(--c-text-2);
    font: var(--text-mono-data, 12px/16px var(--f-mono));
    white-space: nowrap;
  }
  .pestana:hover { color: var(--c-text); }
  .pestana[aria-selected='true'] { background: var(--c-base); color: var(--c-text); border-top-color: var(--c-cyan); }
  .acciones-cajon {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 0 8px;
    white-space: nowrap;
  }
  .cuerpo-cajon {
    position: relative;
    flex: none;
    background: var(--c-base);
    overflow: hidden;
  }
  .anfitrion {
    position: absolute;
    inset: 4px 6px;
    overflow: hidden;
  }

  /* ---- 4. emergency abort: recency corner, hold-to-confirm ---- */
  .abortar {
    position: absolute;
    right: 12px;
    bottom: 12px;
    z-index: 40;
    display: inline-flex;
    align-items: center;
    gap: 8px;
    height: 32px;
    padding: 0 12px;
    overflow: hidden;
    background: var(--c-destructive);
    border: 1px solid var(--c-destructive);
    border-radius: var(--r-compact);
    color: var(--c-text);
    font: var(--text-label, 13px/20px var(--f-sans));
    letter-spacing: 0.02em;
    touch-action: none;
    -webkit-user-select: none;
  }
  .abortar:hover { background: var(--c-destructive-hover); }
  .abortar.sosteniendo { background: var(--c-destructive-hover); border-color: var(--c-cyan); }
  .abortar .pct { display: inline-block; min-width: 36px; text-align: right; font-size: 12px; color: var(--c-cyan); }
  .abortar .progreso {
    position: absolute;
    left: 0;
    bottom: 0;
    height: 4px;
    width: 0;
    background: var(--c-cyan);
  }

  @media (prefers-reduced-motion: reduce) {
    .vista * { transition: none !important; animation: none !important; }
  }
</style>
