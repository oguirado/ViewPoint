<script lang="ts">
  // The master shell (OPENDESIGN stage 7): one 40 px navigation header (BARRA_H), the three master views and the agents
  // drawer, all mounted once. It owns what the views share: the srfd client and its 5 s heartbeat (estado + listar),
  // the shell's single WebGL2 context (DEC-4, through MasterWebGL2Broker) and the global hotkeys.
  //
  // PERMANENT MOUNTING (INV-PANTALLA-01). No view is ever inside {#if}: each one lives in its host <div> and the host
  // toggles `display`. Switching views unmounts nothing, so the WebGL2 context, the Xpra canvases and the PTY streams
  // survive the switch (the 16.2 s reconnect penalty [MEASURED] is what this avoids).
  //
  // GEOMETRY. Every figure comes from lib/geometria.ts (BARRA_H, LIENZO_W, LIENZO_H, DOM_W, GUTTER_IZQ) and from
  // lib/terminal/termguard.ts (ALTURA_CAJON, ALTURA_ASA); this file derives none of them.
  //
  // LAYOUT. The header sits over the static bar of index.html (same 40 px). VIEW-02 and VIEW-04 anchor themselves at
  // the canvas envelope (top BARRA_H, left GUTTER_IZQ) when standalone = false. VIEW-01 carries a global bar of its own;
  // hosted here it hides it (barraPropia = false) and its valley starts at the top of a host that begins at BARRA_H,
  // so the envelope stays LIENZO_W x LIENZO_H, pixel exact, under the master header. The drawer is docked over the
  // envelope's bottom edge, ALTURA_CAJON tall when open and ALTURA_ASA when collapsed, above every view.
  import { onMount, type ComponentExports } from 'svelte';
  import {
    BARRA_H,
    DOM_W,
    GUTTER_IZQ,
    LIENZO_H,
    LIENZO_W,
    aplicarPreferencias,
    geometriaDeVentana,
    leerPreferencias,
  } from './lib/geometria';
  import { RC_PARADA, SrfdError, causaDeRC, crearClienteSrfd, crearPuenteCockpit, transporteWails } from './lib/srfd';
  import { MasterWebGL2Broker } from './lib/components/MasterCanvas.svelte';
  import { ALTURA_ASA, ALTURA_CAJON } from './lib/terminal/termguard';
  import { mismaTabla, ordenarPorSlot, primerSlotLibre, ranuraDeSuperficie } from './lib/capsulas';
  import { REFRESCO_MS, cicloDeRanura, esAtajoCajon, saltoDeRanura, vistaInicial, type VistaMaestra } from './lib/shell';
  import type { CockpitState, EstadoSrfd, InfoSuperficieSrfd } from './lib/types/hypervisor';
  import type { Terminal } from '@xterm/xterm';
  import type { FaseDeFlujo } from './lib/terminal/wsstream';
  import CockpitMasterView from './lib/components/CockpitMasterView.svelte';
  import CajonAgentes, { type AccionDeCajon, type AgenteCajon } from './lib/terminal/CajonAgentes.svelte';

  import StateMirror from './lib/aiops/StateMirror.svelte';
  import OperationStatus from './lib/aiops/OperationStatus.svelte';
  import KeyboardMenu, { escaleraEscape, esDuenoDeTexto } from './lib/aiops/KeyboardMenu.svelte';
  import WorkspaceMatrix from './lib/workspace/WorkspaceMatrix.svelte';
  import AjustesServicios from './lib/components/AjustesServicios.svelte';
  import { createShellWire, cooperationBridge, recordingBridge } from './lib/aiops/wire';

  const wire = createShellWire();
  const bus = wire.bus;
  const bridge = cooperationBridge();
  const recBridge = recordingBridge();
  let doc = $state.raw<CockpitState | null>(null);
  let bytes = $state('');
  let master = $state<ComponentExports<typeof CockpitMasterView> | undefined>();
  async function refreshState(): Promise<void> {
    const next = await wire.refresh();
    if (next) { doc = next.document; bytes = next.bytes; }
  }
  async function setLayout(ws: number, layout: string): Promise<void> {
    const task = bus.begin('set-layout', 'cockpit');
    try {
      if (!bridge || !doc) throw new Error('AUSENTE: cooperation bridge');
      const held = doc.capsules.find(c => c.workspace === ws && c.mode === 'agent_locked' && credentials[c.slot_id]);
      const credential = held ? credentials[held.slot_id] ?? null : null;
      await bridge.FijarDisposicion(ws, layout, credential?.epoch ?? doc.seq, credential);
      await refreshState();
      if (!doc.workspaces.some(w => (w.n === ws || layout === '2x2') && w.layout === layout)) throw new Error('layout not observed');
      task.finish();
    } catch (error: unknown) { task.fail(textoDeError(error)); }
  }

  type FaseSrfd = 'esperando' | 'listo' | 'sin_puente' | 'parada' | 'error';

  let credentials = $state.raw<Record<string, import('./lib/aiops/CapsuleBar.svelte').LeaseCredential>>({});
  const cliente = crearClienteSrfd(transporteWails(), (slot, credential) => {
    credentials = {...credentials, [`srf-ng-${slot}`]:credential};
  });
  /** The rest of the bridge (cockpit/app.go stage 7.2): PTY streams, recording, gates, telemetry, PBX. */
  const puente = crearPuenteCockpit();

  // ---- shell state --------------------------------------------------------------------------------------------
  let vistaActiva = $state<VistaMaestra>(vistaInicial(window.location.search));
  let cajonAbierto = $state(false);
  let serviciosAbierto = $state(false);
  let geo = $state(geometriaDeVentana(window.innerWidth, window.innerHeight));
  let gpu = $state<'sin_crear' | 'ok' | 'perdido' | 'no_disponible'>('sin_crear');
  // Raw state: both are props of CockpitMasterView, which must see them once bind:this and the broker have run.
  let lienzoGl = $state.raw<HTMLCanvasElement | null>(null);
  let gl = $state.raw<WebGL2RenderingContext | null>(null);

  // ---- srfd state: typed answers or the reason there is none, never a guess --------------------------------------
  let fase = $state<FaseSrfd>('esperando');
  let estado = $state<EstadoSrfd | null>(null);
  let aviso = $state('');
  /** The last `listar` answer; null until srfd answers once (the views keep their fixtures until then). */
  let tabla = $state.raw<InfoSuperficieSrfd[] | null>(null);
  /** true when the table on screen is older than the last (failed) poll. */
  let tablaEnvejecida = $state(false);
  let activeSlotIndex = $state(0);
  let ultimaAccion = $state('');

  const ranuras = $derived(tabla === null ? null : ordenarPorSlot(tabla).map(ranuraDeSuperficie));
  /** The slot VIEW-01 shows: the operator's choice, never past the end of the live table. */
  const slotEfectivo = $derived(ranuras === null ? activeSlotIndex : Math.min(activeSlotIndex, Math.max(0, ranuras.length - 1)));
  /** The srfd slot number behind the effective index (the fixture's active slot when srfd has not answered). */
  const slotSrfd = $derived(ranuras === null ? 0 : (ranuras[slotEfectivo]?.slot ?? 0));

  // ---- workload launching ([+ WORKLOAD]) & AI agent delegation ---------------------------------------------


  async function lanzarCarga(clase: 'browser' | 'desktop' | 'microvm'): Promise<void> {
    const slot = primerSlotLibre(tabla ?? []);
    if (slot < 0) {
      ultimaAccion = 'launch refused: every slot is taken';
      bus.emit({kind:'refused',scope:'cockpit',literal:ultimaAccion});
      return;
    }
    const task = bus.begin('start-capsule', 'cockpit');
    ultimaAccion = `iniciar ${clase} slot ${slot}…`;
    try {
      const info = await cliente.iniciar(clase, slot, 'cockpit-ui');
      ultimaAccion = `slot ${info.slot} ${info.estado}`;
      await sondear();
      task.finish();
      if (ranuras) {
        const idx = ranuras.findIndex((r) => r.slot === slot);
        if (idx >= 0) activeSlotIndex = idx;
      }
    } catch (e) {
      ultimaAccion = textoDeError(e);
      task.fail(ultimaAccion);
    }
  }

  function delegarCargaAgente(): void {
    void abrirAgente('Claude Code', 'claude: launch and configure a sandbox container for workload');
  }

  // ---- agents drawer (VIEW-03): one shell per agent through the bridge's PTY broker (PtyUrl) ------------------------
  let agentes = $state<AgenteCajon[]>([]);
  let agenteActivo = $state('');
  let contadorAgentes = 0;
  /** Directives waiting for a shell's stream to open (the quick pills), and what each stream has reached. */
  const directivas = new Map<string, string>();
  const terminales = new Map<string, Terminal>();
  const fasesAgentes = new Map<string, FaseDeFlujo>();

  function enviarDirectivaPendiente(id: string): void {
    const directiva = directivas.get(id);
    const term = terminales.get(id);
    if (directiva === undefined || term === undefined || fasesAgentes.get(id) !== 'abierto') return;
    directivas.delete(id);
    term.input(`${directiva}\r`, true); // through the terminal, like a keystroke: no path around termguard
  }
  function alMontarAgente(id: string, term: Terminal): void {
    terminales.set(id, term);
    enviarDirectivaPendiente(id);
  }
  function alDesmontarAgente(id: string): void {
    terminales.delete(id);
    fasesAgentes.delete(id);
  }
  function alFaseAgente(id: string, fase: FaseDeFlujo): void {
    fasesAgentes.set(id, fase);
    enviarDirectivaPendiente(id);
  }

  /** Opens one agent: a shell of the PTY broker, with the directive typed once the stream is open. */
  async function abrirAgente(titulo: string, directiva: string, reemplaza: string | null = null): Promise<void> {
    contadorAgentes += 1;
    const id = reemplaza === null ? `agente-${contadorAgentes}` : `${reemplaza.replace(/-r\d+$/, '')}-r${contadorAgentes}`;
    let url: string;
    try {
      url = await puente.ptyUrl(id);
    } catch (e) {
      ultimaAccion = `PtyUrl: ${textoDeError(e)}`;
      bus.emit({kind:'refused',scope:'cockpit',literal:ultimaAccion});
      return;
    }
    if (directiva !== '') directivas.set(id, directiva);
    const agente: AgenteCajon = { id, titulo, superficie: `srf-ng-${slotSrfd}`, url };
    agentes = reemplaza === null ? [...agentes, agente] : agentes.map((a) => (a.id === reemplaza ? agente : a));
    agenteActivo = id;
    cajonAbierto = true;
    ultimaAccion = `${titulo}: shell ${id}`;
  }

  const modoVentana = $derived(geo.mode === 'tile_1_6' ? 'TILE 1/6' : 'COLUMN 1/3');

  // ---- WebGL2: the one context of the shell, through the broker (DEC-4) --------------------------------------------
  function pintarFondo(): void {
    if (gl === null) return;
    gl.viewport(0, 0, gl.drawingBufferWidth, gl.drawingBufferHeight);
    gl.clearColor(0, 0, 0, 0); // transparent: the static canvas frame shows through
    gl.clear(gl.COLOR_BUFFER_BIT);
  }

  function abrirContextoGl(): void {
    const lienzo = lienzoGl;
    if (lienzo === null) {
      gpu = 'no_disponible';
      return;
    }
    gl = MasterWebGL2Broker.getInstance().bindContext(lienzo);
    if (gl === null) {
      gpu = 'no_disponible';
      return;
    }
    lienzo.addEventListener('webglcontextlost', (ev) => {
      ev.preventDefault();
      gpu = 'perdido';
    });
    lienzo.addEventListener('webglcontextrestored', () => {
      pintarFondo();
      gpu = 'ok';
    });
    pintarFondo();
    gpu = 'ok';
  }

  // ---- srfd heartbeat: estado and listar together, every REFRESCO_MS ------------------------------------------------
  function faseDeError(e: unknown): FaseSrfd {
    if (e instanceof SrfdError) return e.codigo === 'sin_puente' ? 'sin_puente' : e.rc === RC_PARADA ? 'parada' : 'error';
    return 'error';
  }

  function textoDeError(e: unknown): string {
    if (e instanceof SrfdError) return e.codigo === 'rechazado' ? `srfd rc ${e.rc}: ${causaDeRC(e.rc)}` : e.message;
    return e instanceof Error ? e.message : String(e);
  }

  async function sondear(): Promise<void> {
    void refreshState();
    const [rEstado, rTabla] = await Promise.allSettled([cliente.estado(), cliente.listar()]);
    if (rEstado.status === 'fulfilled') {
      estado = rEstado.value;
      fase = 'listo';
      aviso = '';
    } else {
      estado = null;
      fase = faseDeError(rEstado.reason);
      aviso = textoDeError(rEstado.reason);
      bus.emit({kind:'degraded',scope:'cockpit',literal:aviso});
    }
    if (rTabla.status === 'fulfilled') {
      if (tabla === null || !mismaTabla(tabla, rTabla.value)) tabla = rTabla.value;
      tablaEnvejecida = false;
    } else {
      tablaEnvejecida = tabla !== null;
      if (aviso === '') aviso = textoDeError(rTabla.reason);
    }
  }

  // ---- what the drawer asks for: provisioning goes to srfd; the pills open shells through the PTY broker ------------
  function alAccionDeCajon(accion: AccionDeCajon, id: string | null, texto: string): void {
    if (accion === 'provisionar') {
      cajonAbierto = true;
      void lanzarCarga('browser');
      return;
    }
    if (accion === 'reconectar') {
      if (id === null) return;
      const previo = agentes.find((a) => a.id === id);
      void abrirAgente(previo?.titulo ?? 'shell', '', id); // RESPAWN: a new shell of the broker replaces the failed one
      return;
    }
    // srf conectar / claude / gemini: the pill's own text is the directive, plus whatever the line holds
    const directiva = texto.trim() === '' ? accion : `${accion} ${texto.trim()}`;
    void abrirAgente(accion === 'srf conectar' ? 'srf' : accion === 'claude' ? 'Claude Code' : 'Gemini CLI', directiva);
  }

  // ---- hotkeys: capture phase, so a view's own listener (VIEW-01 also binds Ctrl+`) never sees the shell's keys -----
  function alTeclado(e: KeyboardEvent): void {
    if (e.key === 'Escape') {
      const action = escaleraEscape({target:e.target, hubOpen:cajonAbierto,
        inHubChrome:e.target instanceof Element && !!e.target.closest('[data-anfitrion="cajon"]')});
      if (action?.kind === 'close_hub') { e.preventDefault(); cajonAbierto=false; return; }
    }
    if (esAtajoCajon(e)) {
      e.preventDefault();
      e.stopPropagation();
      cajonAbierto = !cajonAbierto;
      return;
    }
    if (esDuenoDeTexto(e.target)) return;
    const salto = saltoDeRanura(e);
    if (salto !== null) {
      const total = ranuras?.length ?? 0;
      if (total > 0 && salto < total) {
        e.preventDefault();
        e.stopPropagation();
        activeSlotIndex = salto;
      }
      return;
    }
    const delta = cicloDeRanura(e);
    if (delta !== null) {
      const total = ranuras?.length ?? 0;
      if (total > 0) {
        e.preventDefault();
        e.stopPropagation();
        activeSlotIndex = (slotEfectivo + delta + total) % total;
      }
      return;
    }
  }

  onMount(() => {
    // Fallback of the head bootstrap: idempotent when the bootstrap ran (same values, nothing to move),
    // and the only thing that applies the preference when it did not.
    const { prefs, error } = leerPreferencias(window.localStorage);
    aplicarPreferencias(document.documentElement, prefs, window.innerHeight);
    if (error !== '') aviso = `preferences: ${error}`;

    abrirContextoGl();
    void sondear();
    const latido = window.setInterval(() => void sondear(), REFRESCO_MS);

    const alRedimensionar = (): void => {
      geo = geometriaDeVentana(window.innerWidth, window.innerHeight);
      document.documentElement.dataset.modo = geo.mode === 'tile_1_6' ? 'sexto' : 'tercio';
    };
    window.addEventListener('resize', alRedimensionar);
    window.addEventListener('keydown', alTeclado, { capture: true });

    // Ready contract: two frames after hydration, then transitions are allowed and the shell is declared settled.
    let cancelado = false;
    requestAnimationFrame(() =>
      requestAnimationFrame(() => {
        if (cancelado) return;
        document.documentElement.classList.remove('initializing');
        document.documentElement.dataset.appReady = 'true';
      }),
    );

    return () => {
      cancelado = true;
      window.clearInterval(latido);
      wire.dispose();
      window.removeEventListener('resize', alRedimensionar);
      window.removeEventListener('keydown', alTeclado, { capture: true });
    };
  });
</script>

<!-- 1. MASTER NAVIGATION HEADER (height: BARRA_H): single unified cockpit view with container switcher and workload controls -->
<header class="cabecera-maestra" class:canonical-enabled={doc !== null} data-cabecera-maestra style:height="{BARRA_H}px" aria-label="Master navigation">
  <div class="grupo izq">
    <span class="marca">OPENSUPERFICIES <span class="sep" aria-hidden="true">//</span> COCKPIT</span>
    <span class="insignia" data-modo={geo.mode} title="Window mode by DOM height ({geo.domHeight}px)">{modoVentana}</span>
    <span class="insignia vista" data-vista-activa={vistaActiva}>COCKPIT</span>
    {#if geo.scaled}
      <span class="insignia escala">SCALED ×{geo.scale.toFixed(2)}</span>
    {/if}
    <nav class="ranuras" aria-label="Capsule slots" style:visibility={ranuras !== null ? 'visible' : 'hidden'}>
      {#each ranuras ?? [] as r, i (r.id)}
        <button
          type="button"
          class="ranura"
          class:activa={i === slotEfectivo}
          class:sano={r.health === 'HEALTHY'}
          class:aviso={r.health === 'WARN'}
          class:critico={r.health === 'CRIT'}
          aria-pressed={i === slotEfectivo}
          aria-label="Focus slot {r.id}"
          title="{r.id}: {r.name} · {r.tier} · {r.health} [Alt+{i + 1}]"
          onclick={() => (activeSlotIndex = i)}
        >
          <span class="glifo-salud">{r.health === 'HEALTHY' ? '●' : r.health === 'WARN' ? '▲' : r.health === 'CRIT' ? '■' : '○'}</span>
          <span class="slot-num">{r.slot}</span>
          <span class="slot-tier">{r.tier === 'DOCKER L2' ? 'L2' : r.tier === 'KVM HARDWARE' ? 'KVM' : 'PTY'}</span>
        </button>
      {/each}
    </nav>
  </div>

  <div class="grupo centro">
    <KeyboardMenu id="workload" label="Workload menu" items={[
      {id:'browser',label:'Launch Browser',kind:'item'},
      {id:'desktop',label:'Launch Desktop',kind:'item'},
      {id:'microvm',label:'Launch MicroVM',kind:'item'},
      {id:'agent',label:'AI Agent Delegation',kind:'item'},
    ]} onSelect={(id) => id === 'agent' ? delegarCargaAgente() : void lanzarCarga(id as 'browser'|'desktop'|'microvm')} />
  </div>

  <div class="grupo der">
    <OperationStatus {bus} />
    <button
      type="button"
      class="cajon-boton"
      class:activo={cajonAbierto}
      aria-expanded={cajonAbierto}
      aria-label="Agent hub"
      title="Toggle the agents drawer [Ctrl+`]"
      onclick={() => (cajonAbierto = !cajonAbierto)}
    >_ PTY DRAWER</button>
    <button
      type="button"
      class="cajon-boton"
      class:activo={serviciosAbierto}
      aria-expanded={serviciosAbierto}
      aria-controls="panel-servicios"
      onclick={() => (serviciosAbierto = !serviciosAbierto)}
    >SERVICIOS WEB</button>

  </div>
</header>

<StateMirror {bytes} />
{#if serviciosAbierto}
  <!-- Overlay only: it never resizes capsule framebuffers. -->
  <div id="panel-servicios" class="capa-servicios">
    <button type="button" class="cajon-boton" aria-label="Cerrar servicios web" onclick={() => (serviciosAbierto = false)}>×</button>
    <AjustesServicios />
  </div>
{/if}
{#snippet layoutControl(ws: number, layout: string, legal: readonly ('1x1'|'1x2'|'2x2')[])}
  <KeyboardMenu id={`layout-${ws}`} label={`Layout ws ${ws}`} items={(['1x1','1x2','2x2'] as const).map(value => ({
    id:value,label:`Set layout ${value} ws ${ws}`,kind:'radio' as const,checked:value===layout,
    disabled:!legal.some(allowed=>allowed===value),reason:'Layout is not legal on this canvas',
  }))} onSelect={(value)=>void setLayout(ws,value)}
    onDisabledSelect={(_,reason)=>bus.emit({kind:'refused',scope:'cockpit',literal:reason})} />
{/snippet}
{#if doc}
  <div class="workspace-origin">
    <WorkspaceMatrix {doc} {bytes} status={bus} {layoutControl} superficie={master?.superficie} />
  </div>
{/if}

<!-- 2. THE SHELL'S SINGLE WEBGL2 CANVAS (DEC-4): under every view, scissored by VIEW-01 to its viewport -->
<canvas
  class="gl"
  data-gl
  bind:this={lienzoGl}
  width={geo.canvasWidth}
  height={geo.canvasHeight}
  aria-hidden="true"
></canvas>

<!-- 3. MASTER COCKPIT VIEW: permanently mounted under the master header (INV-PANTALLA-01). -->
<div
  class="anfitrion cockpit"
  class:canonical-host={doc !== null}
  data-anfitrion="cockpit"
  style:top="{BARRA_H}px"
  style:width="{DOM_W}px"
>
  <CockpitMasterView sharedCanvas={lienzoGl} sharedGl={gl} capsulas={ranuras} activeSlotIndex={slotEfectivo} barraPropia={false} cajonInicial={false} bind:this={master} {doc} {bytes} {bus} {bridge} {recBridge} {credentials} />
</div>

<!-- 4. AGENTS DRAWER (VIEW-03): docked at the bottom of the canvas envelope, over the active view, in every view -->
<div
  class="anfitrion-cajon"
  data-anfitrion="cajon"
  data-estado={cajonAbierto ? 'abierto' : 'plegado'}
  style:top="{BARRA_H}px"
  style:left="0px"
  style:width="{LIENZO_W}px"
  style:--cajon-h="{ALTURA_CAJON}px"
  style:--asa-h="{ALTURA_ASA}px"
>
  <CajonAgentes lado="left" anunciar={bus.emit} {agentes} bind:abierto={cajonAbierto} bind:activo={agenteActivo} alMontar={alMontarAgente} alDesmontar={alDesmontarAgente} alFase={alFaseAgente} alAccion={alAccionDeCajon} />
</div>

<style>
  .workspace-origin { position: absolute; inset: 0; pointer-events: none; }
  .workspace-origin :global(.vista04) { pointer-events: auto; }
  /* The canonical tabs/layout occupy the header's legacy slot-navigation area.
     Keep the Matrix at the window origin; only its chrome rises over the header. */
  .workspace-origin :global(.workspace-chrome) { z-index: 61; width: 25%; right: auto; }
  .workspace-origin :global([role="tabpanel"]) { position: absolute; top: 0; left: 25%; z-index: 61; }
  .workspace-origin :global([id^="workspace-description-"]) {
    position: absolute; width: 1px; height: 1px; overflow: hidden; clip-path: inset(50%);
  }
  .cabecera-maestra.canonical-enabled .grupo.izq { visibility: hidden; }
  /* The permanently mounted legacy master is transparent above the canonical
     surfaces. Its abort remains reachable; the shell owns the only PTY hub. */
  .canonical-host, .canonical-host :global(.vista), .canonical-host :global(.valle) { pointer-events: none; }
  .canonical-host :global(.bento[hidden]) { display: none; }
  .canonical-host :global(.abortar) { pointer-events: auto; }
  .canonical-host :global(.asa-plegada), .canonical-host :global([data-cajon]) { display: none; }

  /* ---- tokens: app.css (docs/design/DESIGN.md Option A, Cyberpunk Obsidian) with local fallbacks ---- */
  .cabecera-maestra {
    --c-raised: var(--surface-cockpit, #161d2e);
    --c-border: var(--border-subtle, #222d42);
    --c-border-strong: var(--border-bold, #3b4b6e);
    --c-text: var(--text-polar, #f4f6fc);
    --c-text-2: var(--text-silver, #94a3b8);
    --c-text-off: var(--text-muted, #64748b);
    --c-cyan: var(--accent-cyan, #00e5ff);
    --c-emerald: var(--accent-emerald, #00e676);
    --c-amber: var(--accent-amber, #ffb300);
    --c-crimson: var(--accent-crimson, #ff3366);
    --f-mono: var(--font-mono, 'JetBrains Mono', 'SF Mono', Menlo, Consolas, monospace);

    position: absolute;
    top: 0;
    left: 0;
    right: 0;
    z-index: 60;
    box-sizing: border-box;
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto minmax(0, 1fr);
    align-items: center;
    gap: 12px;
    padding: 0 12px;
    background: var(--c-raised);
    border-bottom: 1px solid var(--c-border);
    color: var(--c-text);
    font: 500 11px/16px var(--f-mono);
    letter-spacing: 0.04em;
    white-space: nowrap;
    overflow: visible;
    pointer-events: auto;
    font-variant-numeric: tabular-nums;
  }
  .cabecera-maestra *,
  .cabecera-maestra *::before,
  .cabecera-maestra *::after {
    box-sizing: border-box;
  }
  .grupo {
    display: flex;
    align-items: center;
    gap: 8px;
    min-width: 0;
    height: 100%;
  }
  .grupo.der {
    justify-content: flex-end;
  }
  .marca {
    font-weight: 700;
    font-size: 12px;
    letter-spacing: 0.08em;
    color: var(--c-text);
  }
  .marca .sep {
    color: var(--c-cyan);
  }
  .insignia {
    padding: 1px 6px;
    border: 1px solid var(--c-border-strong);
    border-radius: 2px;
    color: var(--c-text-2);
    font-size: 10px;
  }
  .insignia.vista {
    border-color: var(--c-cyan);
    color: var(--c-cyan);
  }
  .insignia.escala {
    border-color: var(--c-amber);
    color: var(--c-amber);
  }
  .ranuras {
    display: flex;
    align-items: center;
    gap: 4px;
    margin-left: 4px;
    min-width: 0;
    overflow: hidden;
  }
  .ranura {
    display: inline-flex;
    align-items: center;
    gap: 4px;
    min-width: 22px;
    height: 22px;
    padding: 0 6px;
    border: 1px solid var(--c-border);
    border-radius: 2px;
    background: transparent;
    color: var(--c-text-off);
    font: inherit;
    cursor: pointer;
  }
  .ranura.sano { color: var(--c-emerald); }
  .ranura.aviso { color: var(--c-amber); }
  .ranura.critico { color: var(--c-crimson); }
  .ranura.activa { border-color: var(--c-cyan); background: var(--canvas-void, #07090e); color: var(--c-text); }
  .ranura:hover { border-color: var(--c-border-strong); }
  .ranura .glifo-salud { font-size: 8px; line-height: 1; }
  .ranura .slot-num { font-weight: 600; }
  .ranura .slot-tier { font-size: 9px; color: var(--c-text-off); }
  .ranura:focus-visible,
  .cajon-boton:focus-visible {
    outline: 2px solid var(--c-cyan);
    outline-offset: 1px;
  }

  /* workload launcher: center column dropdown */
  .cajon-boton {
    height: 22px;
    padding: 0 8px;
    border: 1px solid var(--c-border-strong);
    border-radius: 2px;
    background: transparent;
    color: var(--c-text-2);
    font: inherit;
    letter-spacing: 0.06em;
    cursor: pointer;
  }
  .cajon-boton:hover { color: var(--c-text); }
  .capa-servicios {
    position: fixed; top: 3rem; right: 1rem; z-index: 40;
    max-width: min(48rem, calc(100vw - 2rem)); max-height: calc(100vh - 4rem); overflow: auto;
    background: var(--c-raised); color: var(--c-text); border: 1px solid var(--c-cyan); padding: 0.75rem;
  }
  .cajon-boton.activo { border-color: var(--c-cyan); color: var(--c-cyan); }


  /* ---- hosts: the views place themselves; a host only grants pointer events and toggles display ---- */
  .anfitrion {
    position: absolute;
    inset: 0;
    pointer-events: auto;
  }
  /* VIEW-01 begins under the header and is clipped to the canvas envelope's height: with barraPropia = false its
     valley starts at the host's top, so the envelope lands at BARRA_H .. BARRA_H + LIENZO_H, exactly where the shell's
     WebGL2 canvas is (app.css .gl). */
  .anfitrion.cockpit {
    inset: auto;
    left: 0;
    height: var(--canvas-h-activo, var(--canvas-h));
    overflow: hidden;
  }
  .anfitrion-cajon {
    position: absolute;
    z-index: 45;
    height: var(--canvas-h-activo, var(--canvas-h));
    pointer-events: none;
  }
  .anfitrion-cajon > :global(.cajon) {
    pointer-events: auto;
  }
  /* Collapsed, the docked handle lies on the frame's bottom edge: it keeps the frame's own colour (surface-obsidian,
     the drawer's base) so the instrument's read-back (medir-cls.mjs cajaDeColor, COLOR_LIENZO) still sees the
     LIENZO_W x LIENZO_H box; the grip glyph alone is raised. */
  .anfitrion-cajon > :global(.cajon[data-estado='plegado'] .asa) {
    background: transparent;
  }
</style>
