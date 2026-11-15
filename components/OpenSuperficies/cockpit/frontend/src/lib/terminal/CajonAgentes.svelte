<script module lang="ts">
  export interface AgenteCajon {
    id: string;
    /** The tab's title (`Claude Code`, `Gemini CLI`, `bash`). */
    titulo: string;
    /** The surface the agent is bound to (`srf-ng-N`). */
    superficie: string;
    /** The PTY stream of the agent. */
    url: string;
  }

  /** What the operator asked the shell for from the drawer's chrome; the shell (not the drawer) knows how to do it. */
  export type AccionDeCajon = 'provisionar' | 'srf conectar' | 'claude' | 'gemini' | 'reconectar';

  /** The drawer's own TEA state (spec 8): what the beacon and the chrome show for the active tab. */
  export type EstadoDeCajon = 'default' | 'hover' | 'active' | 'focus' | 'disabled' | 'loading' | 'empty' | 'error' | 'recovering';

  /** Section 8: recovery is non-absorbing and bounded; a RESPAWN is offered while k < RECONEXIONES_MAX. */
  export const RECONEXIONES_MAX = 3;

  export function debeAnunciar(prev: import('./wsstream').FaseDeFlujo | undefined, f: import('./wsstream').FaseDeFlujo): boolean {
    return prev !== f && (f === 'fallido' || f === 'cerrado' || f === 'reconectando');
  }

  export function literalPty(n: number, f: import('./wsstream').FaseDeFlujo, detalle: string): string {
    return `PTY T${n} ${f}: ${(detalle || f).trim() || f}`;
  }
</script>

<script lang="ts">
  // The agents drawer (spec 6, VIEW-03): an overlay of fixed height anchored to the bottom of the active canvas. It is
  // absolutely positioned, so opening, collapsing or resizing it never changes the size of the surface behind it.
  // Open it is ALTURA_CAJON (240 px) tall; collapsed it shrinks to the ALTURA_ASA (12 px) grab handle. Three bands:
  // the header (ALTURA_CABECERA: grip, system tag, one tab per agent, telemetry cluster, collapse), the valley
  // (the terminals, the empty slate, the diagnostic watermark) and the prompt line (2 macroblocks: directive input
  // and quick actions). The valley and the prompt line share the body, [data-cuerpo], ALTURA_CAJON - ALTURA_CABECERA tall.
  //
  // EVERY PANEL STAYS MOUNTED. Collapsing the drawer or switching tabs only changes what is visible, never the
  // component tree: a terminal that is unmounted loses its scrollback and its stream. The first 56 agents get an
  // interactive xterm.js terminal (DOM renderer), the rest a plain-text pane (DEC-5). The panels enforce termguard
  // (both lines), the scrollback ceiling, the three focus passes and the input guards; this file adds no path around
  // any of them: the directive line reaches the PTY through the terminal's own input, like a keystroke.
  //
  // The chrome is styled here, completely (no external utility runtime); the colours are the docs/design/DESIGN.md tokens.
  import { tick, untrack } from 'svelte';
  import type { Announce } from '../aiops/status';
  import { rovingTecla } from '../aiops/KeyboardMenu.svelte';
  import type { ITerminalAddon, Terminal } from '@xterm/xterm';
  import TerminalPanel from './TerminalPanel.svelte';
  import { ALINEACION_CODEC, LIENZO_W } from '../geometria';
  import { ALTURA_ASA, ALTURA_CABECERA, ALTURA_CAJON, PANELES_INTERACTIVOS_MAX, SCROLLBACK_MAX, modoDeInstancia } from './termguard';
  import type { FaseDeFlujo } from './wsstream';

  interface Props {
    agentes: readonly AgenteCajon[];
    abierto?: boolean;
    lado?: 'bottom' | 'left';
    anunciar?: Announce;
    /** The id of the active tab; the first agent when it is empty or unknown. */
    activo?: string;
    scrollback?: number;
    crearAddons?: () => ITerminalAddon[];
    crearSocket?: (url: string) => WebSocket;
    alMontar?: (id: string, term: Terminal) => void;
    alDesmontar?: (id: string) => void;
    alFase?: (id: string, fase: FaseDeFlujo, detalle: string) => void;
    /** A chrome action (provision, quick pill, respawn) for the shell to carry out; the drawer only reports it. */
    alAccion?: (accion: AccionDeCajon, id: string | null, texto: string) => void;
  }

  let {
    agentes,
    lado = 'bottom',
    anunciar,
    abierto = $bindable(true),
    activo = $bindable(''),
    scrollback = SCROLLBACK_MAX,
    crearAddons,
    crearSocket,
    alMontar,
    alDesmontar,
    alFase,
    alAccion,
  }: Props = $props();

  /** Band 3, the prompt line: two macroblocks (spec 4.1 of VIEW-03). */
  const ALTURA_PROMPT = 2 * ALINEACION_CODEC;
  const ALTURA_CUERPO = ALTURA_CAJON - ALTURA_CABECERA;
  const ALTURA_VALLE = ALTURA_CUERPO - ALTURA_PROMPT;
  /** How often the watermark re-reads the active terminal's buffer and counts canvas layers. */
  const LECTURA_MS = 500;

  const activoEfectivo = $derived(agentes.some((a) => a.id === activo) ? activo : (agentes[0]?.id ?? ''));
  const agenteActivo = $derived(agentes.find((a) => a.id === activoEfectivo) ?? null);

  // ---- what the panels report, kept per agent (low frequency: a phase change) ----------------------------------
  let fases = $state<Record<string, FaseDeFlujo>>({});
  let detalles = $state<Record<string, string>>({});
  let reintentos = $state<Record<string, number>>({});
  /** The live terminals, by agent id: non-reactive on purpose (the watermark reads them on a timer). */
  const terminales: Record<string, Terminal> = {};

  let scrollbackActivo = $state(0);
  let capasCanvas = $state(0);
  let directiva = $state('');
  let raiz = $state.raw<HTMLElement | null>(null);
  let asa = $state.raw<HTMLButtonElement | null>(null);

  const faseActiva = $derived<FaseDeFlujo | null>(activoEfectivo === '' ? null : (fases[activoEfectivo] ?? null));
  const estado = $derived<EstadoDeCajon>(
    agentes.length === 0
      ? 'empty'
      : faseActiva === null || faseActiva === 'conectando'
        ? 'loading'
        : faseActiva === 'reconectando'
          ? 'recovering'
          : faseActiva === 'fallido'
            ? 'error'
            : faseActiva === 'cerrado'
              ? 'disabled'
              : 'default',
  );
  const beacon = $derived(
    estado === 'default'
      ? { glifo: '●', texto: 'CONN', clase: 'sano' }
      : estado === 'loading'
        ? { glifo: '▲', texto: 'CONNECTING', clase: 'aviso' }
        : estado === 'recovering'
          ? { glifo: '▲', texto: 'RECON', clase: 'aviso' }
          : estado === 'error'
            ? { glifo: '■', texto: 'FAULT', clase: 'critico' }
            : estado === 'disabled'
              ? { glifo: '○', texto: 'CLOSED', clase: 'apagado' }
              : { glifo: '○', texto: 'IDLE', clase: 'apagado' },
  );
  const prompt = $derived(agenteActivo === null ? 'srf' : agenteActivo.titulo.split(' ')[0].toLowerCase());
  const puedeReintentar = $derived(activoEfectivo !== '' && (reintentos[activoEfectivo] ?? 0) < RECONEXIONES_MAX);

  function alTeclearPestana(ev: KeyboardEvent): void {
    const actual = agentes.findIndex((a) => a.id === activoEfectivo);
    const indice = rovingTecla(ev.key, actual, agentes.length);
    if (indice === null) return;
    ev.preventDefault();
    const siguiente = agentes[indice];
    activo = siguiente.id;
    (ev.currentTarget as HTMLElement).querySelector<HTMLElement>(`[data-pestana="${CSS.escape(siguiente.id)}"]`)?.focus();
  }

  // ---- the panel callbacks, observed here and forwarded untouched -----------------------------------------------
  function montado(id: string, term: Terminal): void {
    terminales[id] = term;
    alMontar?.(id, term);
  }
  function desmontado(id: string): void {
    delete terminales[id];
    alDesmontar?.(id);
  }
  function fase(id: string, f: FaseDeFlujo, detalle: string): void {
    const anunciarCambio = debeAnunciar(fases[id], f);
    fases[id] = f;
    detalles[id] = detalle;
    if (f === 'abierto') reintentos[id] = 0;
    if (anunciarCambio) {
      const n = agentes.findIndex((a) => a.id === id) + 1;
      try {
        anunciar?.({ kind: 'degraded', scope: 'cockpit', literal: literalPty(n, f, detalle) });
      } catch {
        // A failed status sink must not interrupt PTY lifecycle handling.
      }
    }
    alFase?.(id, f, detalle);
  }

  /** The directive line: the text reaches the PTY through the terminal itself, as if it had been typed. */
  function enviarDirectiva(): void {
    const texto = directiva.trim();
    const term = terminales[activoEfectivo];
    if (texto === '' || term === undefined) return;
    term.input(`${texto}\r`, true);
    directiva = '';
    term.focus();
  }

  function alTeclearDirectiva(ev: KeyboardEvent): void {
    if (ev.key === 'Enter') {
      ev.preventDefault();
      enviarDirectiva();
    } else if (ev.ctrlKey && ev.key === '`') {
      ev.preventDefault();
      void plegar(); // stash
    }
  }

  async function plegar(): Promise<void> {
    abierto = false;
    await tick();
    asa?.focus();
  }

  function accion(a: AccionDeCajon): void {
    if (a === 'reconectar') {
      if (!puedeReintentar) return;
      reintentos[activoEfectivo] = (reintentos[activoEfectivo] ?? 0) + 1;
    }
    if (alAccion !== undefined) alAccion(a, activoEfectivo === '' ? null : activoEfectivo, directiva);
    else if (a !== 'provisionar' && a !== 'reconectar') directiva = a; // no shell listening: the pill fills the line
  }

  /** The watermark: scrollback retained by the active terminal and canvas layers under the drawer, both read, never assumed. */
  function leerMarcaDeAgua(): void {
    const t = terminales[activoEfectivo];
    const scrollbackLeido = t === undefined ? 0 : Math.max(0, t.buffer.active.length - t.rows);
    const capasLeidas = raiz === null ? 0 : raiz.querySelectorAll('canvas').length;
    if (scrollbackActivo !== scrollbackLeido) scrollbackActivo = scrollbackLeido;
    if (capasCanvas !== capasLeidas) capasCanvas = capasLeidas;
  }

  $effect(() => {
    if (!abierto) return;
    const id = window.setInterval(() => untrack(leerMarcaDeAgua), LECTURA_MS);
    untrack(leerMarcaDeAgua);
    return () => window.clearInterval(id);
  });
</script>

<section
  id="agent-hub"
  class="cajon"
  data-lado={lado}
  data-cajon
  data-estado={abierto ? 'abierto' : 'plegado'}
  data-tea={estado}
  aria-label="Agent hub"
  bind:this={raiz}
  style:height="{abierto ? ALTURA_CAJON : ALTURA_ASA}px"
  style:max-width="{LIENZO_W}px"
>
  <button
    type="button"
    class="asa"
    data-asa
    bind:this={asa}
    aria-controls="agent-hub"
    aria-label="Open agent hub"
    aria-expanded={abierto}
    hidden={abierto}
    onclick={() => (abierto = true)}
  ><span class="agarre-asa" aria-hidden="true"></span></button>

  <!-- Band 1: header (ALTURA_CABECERA) -->
  <header data-hub-chrome class="cabecera" style:height="{ALTURA_CABECERA}px" style:visibility={abierto ? 'visible' : 'hidden'}>
    <span class="agarre" aria-hidden="true">⋮⋮</span>
    <span class="etiqueta-sistema">OPENSUPERFICIES // VIEW-03</span>
    <div class="pestanas" role="tablist" aria-label="Agents" tabindex="-1" onkeydown={alTeclearPestana}>
      {#each agentes as a, i (a.id)}
        <button
          type="button"
          role="tab"
          class="pestana"
          aria-label="T{i + 1} {a.titulo} {a.superficie}"
          data-pestana={a.id}
          data-fase={fases[a.id] ?? 'conectando'}
          aria-selected={a.id === activoEfectivo}
          aria-disabled={fases[a.id] === 'cerrado'}
          tabindex={a.id === activoEfectivo ? 0 : -1}
          onclick={() => (activo = a.id)}
        >
          <span class="pip" aria-hidden="true"></span>
          <span class="rotulo">T{i + 1} {a.titulo} ▸ {a.superficie}</span>
        </button>
      {/each}
    </div>
    <button type="button" class="mas" data-mas aria-label="Provision agent surface" title="Provision agent surface" onclick={() => accion('provisionar')}>+</button>
    <div class="telemetria-cajon">
      <span class="chip beacon" data-beacon={estado}><span class="glifo {beacon.clase}">{beacon.glifo}</span><span class="v {beacon.clase} ancho-beacon">{beacon.texto}</span></span>
      <span class="chip" title="Round-trip probe: AUSENTE (wsstream carries no ping; the frame's ms is the sender's own clock)"><span class="k">RTT</span><span class="v ancho-rtt">—</span></span>
      <span class="chip cian"><span class="v ancho-pty">PTY: {abierto && activoEfectivo !== '' ? 'ACTIVE' : 'IDLE'}</span></span>
      <span class="divisor" aria-hidden="true"></span>
      <button type="button" class="plegar" data-plegar aria-label="Collapse agent hub" aria-expanded={abierto} aria-controls="agent-hub" title="Collapse [Ctrl+`]" onclick={plegar}>[ _ ]</button>
    </div>
  </header>

  <!-- Bands 2 and 3 share the body: ALTURA_CAJON - ALTURA_CABECERA -->
  <div class="cuerpo" data-cuerpo style:height="{ALTURA_CUERPO}px">
    <div class="valle" data-valle data-terminal-host role="group" aria-label="Terminal buffer" style:height="{ALTURA_VALLE}px" oncontextmenu={(e) => e.preventDefault()}>
      {#each agentes as a, i (a.id)}
        <TerminalPanel
          id={a.id}
          url={a.url}
          modo={modoDeInstancia(i)}
          visible={abierto && a.id === activoEfectivo}
          {scrollback}
          {crearAddons}
          {crearSocket}
          alMontar={montado}
          alDesmontar={desmontado}
          alFase={fase}
        />
      {/each}
      <div class="vacio" data-vacio hidden={agentes.length > 0}>
        <span class="vacio-titulo">NO AGENT SESSION</span>
        <span class="vacio-texto">provision a surface or launch an agent from the prompt line</span>
      </div>
      <div class="aviso-fase" data-aviso-fase hidden={estado !== 'error' && estado !== 'disabled' && estado !== 'recovering'}>
        <span class="glifo {beacon.clase}">{beacon.glifo}</span>
        <span class="texto-aviso">{estado === 'recovering' ? 'reconnecting to the PTY stream…' : estado === 'disabled' ? `process ended${detalles[activoEfectivo] ? `: ${detalles[activoEfectivo]}` : ''}` : detalles[activoEfectivo] || 'terminal fault'}</span>
        <button type="button" class="pildora" aria-label={agenteActivo ? `Respawn T${agentes.findIndex((a) => a.id === activoEfectivo) + 1} ${agenteActivo.superficie}` : 'Respawn agent'} onclick={() => accion('reconectar')} disabled={estado === 'recovering' || !puedeReintentar}>{puedeReintentar ? `[RESPAWN ${reintentos[activoEfectivo] ?? 0}/${RECONEXIONES_MAX}]` : '[RESET REQUIRED]'}</button>
      </div>
      <div class="marca-agua" aria-hidden="true">
        <span class="ancho-scroll">SCROLLBACK: {scrollbackActivo}/{scrollback}</span>
        <span>TERMGUARD: DOM-RENDERER [{capasCanvas} CANVAS] · {Math.min(agentes.length, PANELES_INTERACTIVOS_MAX)}/{PANELES_INTERACTIVOS_MAX} INTERACTIVE</span>
      </div>
    </div>
    <footer data-hub-chrome class="linea" style:height="{ALTURA_PROMPT}px">
      <label class="entrada">
        <span class="prompt">{prompt}&gt;</span>
        <span class="cursor" aria-hidden="true">|</span>
        <input
          type="text"
          class="directiva"
          data-directiva
          bind:value={directiva}
          placeholder="input agent directive (Ctrl+` to stash)..."
          autocomplete="off"
          spellcheck="false"
          aria-label="Agent directive"
          disabled={agentes.length === 0 || estado === 'disabled'}
          onkeydown={alTeclearDirectiva}
        />
      </label>
      <span class="pildoras">
        <button type="button" class="pildora" aria-label="Launch srf shell" onclick={() => accion('srf conectar')}>[srf conectar]</button>
        <button type="button" class="pildora" aria-label="Launch Claude Code" onclick={() => accion('claude')}>[claude]</button>
        <button type="button" class="pildora" aria-label="Launch Gemini CLI" onclick={() => accion('gemini')}>[gemini]</button>
      </span>
    </footer>
  </div>
</section>

<style>
  /* ---- tokens: docs/design/DESIGN.md Option A, Cyberpunk Obsidian [CITED]; app.css tokens win when the shell provides them ---- */
  .cajon {
    --c-void: var(--canvas-void, #07090e);
    --c-base: var(--surface-obsidian, #0e131f);
    --c-raised: var(--surface-cockpit, #161d2e);
    --c-overlay: var(--surface-overlay, #1c263b);
    --c-border: var(--border-subtle, #222d42);
    --c-border-strong: var(--border-strong, #334155);
    --c-cyan: var(--accent-cyan, #00e5ff);
    --c-emerald: var(--accent-emerald, #00e676);
    --c-amber: var(--accent-amber, #ffb000);
    --c-crimson: var(--accent-crimson, #ff3355);
    --c-text: var(--text-polar, #f4f6fc);
    --c-text-2: var(--text-silver, #aab6c8);
    --c-text-off: var(--telemetry-stale, #a5b4ce);
    --f-sans: var(--font-sans, 'Inter', system-ui, -apple-system, 'Segoe UI', sans-serif);
    --f-mono: var(--font-mono, 'JetBrains Mono', 'SF Mono', Menlo, Consolas, monospace);

    position: absolute;
    right: 0;
    bottom: 0;
    left: 0;
    z-index: 30;
    box-sizing: border-box;
    overflow: hidden;
    background: var(--c-base);
    border-top: 1px solid var(--c-border);
    color: var(--c-text);
    font: 500 12px/16px var(--f-mono);
    transition: height 160ms cubic-bezier(0.4, 0, 0.2, 1);
  }
  /* Left overlay: its dimensions never participate in the surface layout. */
  [data-lado='left'] {
    --hub-w: 280px;
    display: flex;
    flex-direction: column;
    top: 0;
    bottom: 0;
    left: 0;
    right: auto;
    height: 100% !important;
    width: var(--hub-w);
    border-right: 1px solid var(--c-border);
  }
  [data-lado='left'][data-estado='plegado'] { --hub-w: 12px; }
  [data-lado='left'] .asa { width: 100%; height: 100%; }
  [data-lado='left'] .cabecera {
    height: auto !important;
    flex-wrap: wrap;
    flex: none;
    padding: 8px;
  }
  [data-lado='left'] .etiqueta-sistema,
  [data-lado='left'] .agarre { display: none; }
  [data-lado='left'] .pestanas { flex-basis: 100%; min-height: 32px; }
  [data-lado='left'] .telemetria-cajon { flex-wrap: wrap; min-width: 0; max-width: 100%; gap: 4px; }
  [data-lado='left'] [data-cuerpo],
  [data-lado='left'] [data-valle] {
    flex: 1;
    height: auto !important;
    min-height: 0;
  }
  [data-lado='left'] .linea {
    flex-direction: column;
    height: auto !important;
    align-items: stretch;
    padding: 8px;
    gap: 8px;
  }
  [data-lado='left'] .entrada { min-height: 32px; }
  [data-lado='left'] .pildoras { flex-wrap: wrap; }
  [data-lado='left'] .marca-agua { flex-wrap: wrap; white-space: normal; }
  [data-lado='left'] .vacio-texto { text-align: center; }

  @media (prefers-reduced-motion: reduce) {
    .cajon {
      transition: none;
    }
  }
  .cajon *,
  .cajon *::before,
  .cajon *::after {
    box-sizing: border-box;
    font-variant-numeric: tabular-nums;
    font-feature-settings: 'tnum' 1, 'zero' 1;
  }
  .cajon button {
    font: inherit;
    color: inherit;
    cursor: pointer;
  }
  .cajon button:focus-visible,
  .cajon input:focus-visible {
    outline: 2px solid var(--c-cyan);
    outline-offset: 2px;
  }
  .glifo { display: inline-block; width: 1em; text-align: center; }
  .sano { color: var(--c-emerald); }
  .aviso { color: var(--c-amber); }
  .critico { color: var(--c-crimson); }
  .apagado { color: var(--c-text-off); }

  /* ---- collapsed grab handle (ALTURA_ASA) ---- */
  .asa {
    position: absolute;
    inset: 0;
    z-index: 1;
    display: flex;
    align-items: center;
    justify-content: center;
    padding: 0;
    border: 0;
    background: var(--c-raised);
  }
  .asa[hidden] {
    display: none;
  }
  .agarre-asa {
    display: block;
    width: 48px;
    height: 2px;
    background: var(--c-border-strong);
  }
  .asa:hover .agarre-asa {
    background: var(--c-cyan);
  }

  /* ---- band 1: header (ALTURA_CABECERA) ---- */
  .cabecera {
    display: flex;
    align-items: stretch;
    gap: 4px;
    padding: 0 10px 0 6px;
    background: var(--c-raised);
    border-bottom: 1px solid var(--c-border);
    white-space: nowrap;
    overflow: hidden;
  }
  .agarre {
    flex: none;
    display: inline-flex;
    align-items: center;
    width: 16px;
    color: var(--c-text-off);
    font-size: var(--text-mono-data, 12px);
    letter-spacing: -2px;
    cursor: grab;
    user-select: none;
  }
  .etiqueta-sistema {
    flex: none;
    display: inline-flex;
    align-items: center;
    margin-right: 8px;
    color: var(--c-text);
    font: 700 var(--text-label, 13px)/1 var(--f-sans);
    letter-spacing: -0.01em;
  }
  .pestanas {
    display: flex;
    flex: 1;
    min-width: 0;
    gap: 2px;
    overflow-x: auto;
    scrollbar-width: none;
  }
  .pestanas::-webkit-scrollbar {
    display: none;
  }
  .pestana {
    flex: none;
    display: inline-flex;
    align-items: center;
    gap: 6px;
    padding: 0 12px;
    border: 0;
    border-bottom: 2px solid transparent;
    background: transparent;
    color: var(--c-text-2);
    font: 500 var(--text-mono-data, 12px)/1 var(--f-mono);
    letter-spacing: -0.01em;
    white-space: nowrap;
  }
  .pestana:hover {
    background: var(--c-overlay);
    color: var(--c-text);
  }
  .pestana:active {
    box-shadow: inset 0 0 0 1px var(--c-cyan);
  }
  .pestana .pip {
    display: none;
    width: 6px;
    height: 6px;
    background: var(--c-cyan);
  }
  .pestana[aria-selected='true'] {
    background: var(--c-base);
    color: var(--c-text);
    border-bottom-color: var(--c-cyan);
  }
  .pestana[aria-selected='true'] .pip {
    display: inline-block;
  }
  .pestana[data-fase='cerrado'] {
    color: var(--c-text-off);
    border-style: dashed;
  }
  .pestana[data-fase='fallido'] {
    border-bottom-color: var(--c-crimson);
  }
  .pestana[data-fase='reconectando'] .pip,
  .pestana[data-fase='conectando'] .pip {
    background: var(--c-amber);
  }
  .mas {
    flex: none;
    width: 28px;
    border: 0;
    background: transparent;
    color: var(--c-text-2);
    font: 500 14px/1 var(--f-mono);
  }
  .mas:hover {
    background: var(--c-overlay);
    color: var(--c-text);
  }
  .telemetria-cajon {
    flex: none;
    display: inline-flex;
    align-items: center;
    gap: 8px;
  }
  .chip {
    display: inline-flex;
    align-items: center;
    gap: 4px;
    height: 20px;
    padding: 0 6px;
    background: var(--c-base);
    border: 1px solid var(--c-border);
    color: var(--c-text-2);
    font: 500 var(--text-mono-data, 12px)/1 var(--f-mono);
    letter-spacing: 0.03em;
  }
  .chip .k {
    color: var(--c-text-off);
    font: 500 var(--text-label, 13px)/1 var(--f-sans);
    letter-spacing: 0.06em;
  }
  .chip .v {
    color: var(--c-text);
    font-weight: 600;
  }
  .chip.cian .v {
    color: var(--c-cyan);
    font-weight: 700;
  }
  .chip.beacon .v {
    letter-spacing: 0.06em;
  }
  .ancho-beacon { display: inline-block; min-width: 10ch; }
  .ancho-rtt { display: inline-block; min-width: 6ch; }
  .ancho-pty { display: inline-block; min-width: 11ch; }
  .divisor {
    width: 1px;
    height: 16px;
    background: var(--c-border);
  }
  .plegar {
    flex: none;
    height: 22px;
    padding: 0 4px;
    border: 0;
    background: transparent;
    color: var(--c-text-2);
    font: 700 var(--text-mono-data, 12px)/1 var(--f-mono);
  }
  .plegar:hover {
    background: var(--c-overlay);
    color: var(--c-text);
  }

  /* ---- body: the valley (band 2) and the prompt line (band 3) ---- */
  .cuerpo {
    display: flex;
    flex-direction: column;
    overflow: hidden;
  }
  .valle {
    position: relative;
    flex: none;
    overflow: hidden;
    background: var(--c-void);
    border-bottom: 1px solid var(--c-border);
  }
  .vacio {
    position: absolute;
    inset: 0;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    gap: 8px;
    border: 1px dashed var(--c-border);
    margin: 8px;
  }
  .vacio[hidden] {
    display: none;
  }
  .vacio-titulo {
    color: var(--c-text-off);
    font: 700 var(--text-mono-data, 12px)/1 var(--f-mono);
    letter-spacing: 0.08em;
  }
  .vacio-texto {
    color: var(--c-text-2);
    font: 400 var(--text-mono-data, 12px)/1 var(--f-mono);
  }
  .aviso-fase {
    position: absolute;
    top: 8px;
    right: 8px;
    left: 8px;
    z-index: 2;
    display: flex;
    align-items: center;
    gap: 8px;
    height: 28px;
    padding: 0 8px;
    background: var(--c-raised);
    border: 1px solid var(--c-crimson);
    color: var(--c-text);
    font: 500 var(--text-mono-data, 12px)/1 var(--f-mono);
    white-space: nowrap;
    overflow: hidden;
  }
  .aviso-fase[hidden] {
    display: none;
  }
  .cajon[data-tea='recovering'] .aviso-fase,
  .cajon[data-tea='disabled'] .aviso-fase {
    border-color: var(--c-amber);
  }
  .aviso-fase .texto-aviso {
    flex: 1;
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .marca-agua {
    position: absolute;
    right: 8px;
    bottom: 4px;
    left: 8px;
    z-index: 2;
    display: flex;
    justify-content: space-between;
    color: var(--c-text-off);
    font: 500 var(--text-mono-data, 12px)/1 var(--f-mono);
    letter-spacing: 0.08em;
    white-space: nowrap;
    pointer-events: none;
    user-select: none;
  }
  .ancho-scroll { display: inline-block; min-width: 20ch; }
  .linea {
    flex: none;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 16px;
    padding: 0 12px;
    background: var(--c-base);
  }
  .entrada {
    display: flex;
    flex: 1;
    align-items: center;
    gap: 4px;
    min-width: 0;
    height: 100%;
  }
  .prompt {
    flex: none;
    color: var(--c-cyan);
    font: 700 12px/1 var(--f-mono);
    letter-spacing: -0.01em;
  }
  .cursor {
    flex: none;
    margin-left: -2px;
    color: var(--c-cyan);
    font: 700 12px/1 var(--f-mono);
  }
  .directiva {
    flex: 1;
    min-width: 0;
    height: 100%;
    margin: 0;
    padding: 0;
    border: 0;
    background: transparent;
    color: var(--c-text);
    font: 500 12px/1 var(--f-mono);
    letter-spacing: -0.01em;
    outline: none;
  }
  .directiva::placeholder {
    color: var(--c-text-off);
  }
  .directiva:disabled {
    color: var(--c-text-off);
    border-style: dashed;
  }
  .pildoras {
    flex: none;
    display: inline-flex;
    gap: 6px;
  }
  .pildora {
    height: 20px;
    padding: 0 8px;
    background: var(--c-raised);
    border: 1px solid var(--c-border);
    border-radius: 2px;
    color: var(--c-text-2);
    font: 500 var(--text-mono-data, 12px)/1 var(--f-mono);
    white-space: nowrap;
  }
  .pildora:hover {
    color: var(--c-text);
    border-color: var(--c-border-strong);
  }
  .pildora:disabled {
    color: var(--c-text-off);
    border-style: dashed;
    pointer-events: none;
  }
</style>
