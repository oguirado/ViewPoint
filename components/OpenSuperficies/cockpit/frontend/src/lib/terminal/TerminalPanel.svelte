<script lang="ts">
  // One agent terminal of the drawer (spec 6): an xterm.js 5.5.0 terminal on the DOM renderer, fed by ONE binary
  // WebSocket stream (wsstream.ts), or, past the 56th instance, a plain-text pane on the same stream.
  //
  // THE MOUNT IS UNTRACKED. montar() reads and writes reactive state (the mount counter, the phase): run inside an
  // $effect without untrack, the effect subscribes to what it writes, re-runs, tears the terminal down and mounts it
  // again, until Svelte gives up at its update-depth limit (v1 measured 1,001 remounts in one frame that way, and a
  // frozen WebKit). The effect below reads only the host element it needs and hands everything else to untrack().
  //
  // NOTHING HIGH-FREQUENCY IS REACTIVE. Bytes go from the socket to a non-reactive queue and from there to
  // xterm.write, one batch per animation frame while the panel is visible. A panel that is not visible only
  // queues: it schedules no frame. The counters the instrument reads are written straight to data-* attributes.
  import { untrack } from 'svelte';
  import { Terminal } from '@xterm/xterm';
  import type { ITerminalAddon } from '@xterm/xterm';
  import '@xterm/xterm/css/xterm.css';
  import { ALTA_MARCA_BYTES, ColaDeEscritura, FlujoWs, LOTE_MAX_BYTES } from './wsstream';
  import type { FaseDeFlujo } from './wsstream';
  import { SCROLLBACK_MAX, cargarAddon, crearAddonsPropios, leerRenderer, opcionesDeTerminal, programarFoco, protegerTerminal, quitarAnsi, vigilarCapas } from './termguard';
  import type { ModoDePanel } from './termguard';

  interface Props {
    id: string;
    /** The PTY stream of this agent (ws://127.0.0.1:<port>/pty/<id>). */
    url: string;
    modo?: ModoDePanel;
    /** The drawer is open and this is its active tab. */
    visible?: boolean;
    enfocarAlMostrar?: boolean;
    /** Requested scrollback; the guard clamps it to the limit. */
    scrollback?: number;
    /** Addons requested for this terminal, created once per mount. Every one goes through the guard, which loads none it did not create itself. */
    crearAddons?: () => ITerminalAddon[];
    crearSocket?: (url: string) => WebSocket;
    alMontar?: (id: string, term: Terminal) => void;
    alDesmontar?: (id: string) => void;
    alFase?: (id: string, fase: FaseDeFlujo, detalle: string) => void;
  }

  let {
    id,
    url,
    modo = 'interactivo',
    visible = false,
    enfocarAlMostrar = true,
    scrollback = SCROLLBACK_MAX,
    crearAddons,
    crearSocket,
    alMontar,
    alDesmontar,
    alFase,
  }: Props = $props();

  // low-frequency state: what the template shows
  let anfitrion = $state.raw<HTMLElement | null>(null);
  let texto = $state.raw<HTMLElement | null>(null);
  let montajes = $state(0);
  let fase = $state<FaseDeFlujo>('conectando');
  let renderer = $state('');
  let fallo = $state('');

  // everything else is non-reactive on purpose
  let raiz: HTMLElement | undefined;
  let visibleAhora = false;
  let term: Terminal | null = null;
  let cola: ColaDeEscritura | null = null;
  let flujo: FlujoWs | null = null;
  let sumidero: ((lote: Uint8Array, hecho: () => void) => void) | null = null;
  let cuadro = 0;
  let cuadrosProgramados = 0;
  let pasadasDeFoco = 0;
  let cancelarFoco: (() => void) | null = null;
  let ajustePendiente = false;
  let anfitrionActual: HTMLElement | null = null;
  const codificador = new TextEncoder();

  /** The counters the instrument reads, written straight to the DOM (no reactivity, no frame). */
  function publicar(): void {
    const el = raiz;
    if (el === undefined) return;
    const d = el.dataset;
    if (cola !== null) {
      d.bytesEnCola = String(cola.bytesEnCola);
      d.bytesEnVuelo = String(cola.bytesEnVuelo);
      d.pausas = String(cola.pausas);
      d.reanudaciones = String(cola.reanudaciones);
    }
    if (flujo !== null) {
      const c = flujo.contadores;
      d.bytesRecibidos = String(c.bytes);
      d.tramas = String(c.tramas);
      d.tramasInvalidas = String(c.invalidas);
      d.saltosDeSecuencia = String(c.saltosDeSecuencia);
      d.reconexiones = String(c.reconexiones);
      d.aperturas = String(c.aperturas);
      d.enviadas = String(c.enviadas);
    }
    d.cuadros = String(cuadrosProgramados);
    d.pasadasDeFoco = String(pasadasDeFoco);
    if (term !== null) d.scrollback = String(term.options.scrollback ?? '');
  }

  // ---- one batch per animation frame, and only while visible -----------------------------------------------
  function programarDrenaje(): void {
    if (cuadro !== 0 || !visibleAhora || cola === null || cola.bytesEnCola === 0) return;
    cuadro = requestAnimationFrame(drenar);
    cuadrosProgramados++;
  }

  function drenar(): void {
    cuadro = 0;
    if (!visibleAhora || cola === null || sumidero === null) return;
    const propia = cola;
    const lote = propia.tomar(LOTE_MAX_BYTES);
    if (lote === null) {
      publicar();
      return;
    }
    sumidero(lote, () => {
      propia.confirmar(lote.byteLength);
      publicar();
      programarDrenaje();
    });
    publicar();
    programarDrenaje();
  }

  function abrirFlujo(): void {
    const propia = new ColaDeEscritura({
      altaMarca: ALTA_MARCA_BYTES,
      alPausar: () => flujo?.pausar(),
      alReanudar: () => flujo?.reanudar(),
    });
    cola = propia;
    flujo = new FlujoWs({
      url,
      crearSocket,
      alDatos: (cuerpo) => {
        propia.publicar(cuerpo);
        publicar();
        programarDrenaje();
      },
      alFase: (nueva, detalle) => {
        fase = nueva;
        if (nueva === 'fallido') fallo = detalle;
        else if (nueva === 'abierto') fallo = ''; // a stream that came back is no longer failing
        publicar();
        alFase?.(id, nueva, detalle);
      },
      alError: (e) => {
        fallo = e.message;
        publicar(); // an invalid frame moves a counter the instrument reads: it is published like every other one
      },
      alSalida: (codigo) => {
        propia.publicar(codificador.encode(`\r\n[process exited${codigo === null ? '' : `: code ${codigo}`}]\r\n`));
        programarDrenaje();
      },
    });
    flujo.conectar();
  }

  function cerrarFlujo(): void {
    if (cuadro !== 0) cancelAnimationFrame(cuadro);
    cuadro = 0;
    cancelarFoco?.();
    cancelarFoco = null;
    flujo?.cerrar();
    flujo = null;
    cola = null;
    sumidero = null;
  }

  // ---- fit: the drawer body is fixed, the terminal takes the rows and columns that fit ------------------------
  function ajustar(t: Terminal, el: HTMLElement): void {
    const pantalla = t.element?.querySelector('.xterm-screen') as HTMLElement | null | undefined;
    if (!pantalla || t.cols < 1 || t.rows < 1) return;
    const celdaAncho = pantalla.clientWidth / t.cols;
    const celdaAlto = pantalla.clientHeight / t.rows;
    if (!(celdaAncho > 0) || !(celdaAlto > 0) || el.clientWidth === 0 || el.clientHeight === 0) return; // not laid out yet
    const barra = t.element?.querySelector('.xterm-viewport') as HTMLElement | null | undefined;
    const anchoBarra = barra ? barra.offsetWidth - barra.clientWidth : 0;
    const cols = Math.max(2, Math.floor((el.clientWidth - anchoBarra) / celdaAncho));
    const filas = Math.max(1, Math.floor(el.clientHeight / celdaAlto));
    if (cols !== t.cols || filas !== t.rows) t.resize(cols, filas);
  }

  // ---- visibility: what changes when the panel is shown or hidden ---------------------------------------------
  function cambiarVisibilidad(v: boolean): void {
    visibleAhora = v;
    if (!v) {
      if (cuadro !== 0) cancelAnimationFrame(cuadro);
      cuadro = 0;
      cancelarFoco?.();
      cancelarFoco = null;
      publicar();
      return;
    }
    if (ajustePendiente && term !== null && anfitrionActual !== null) {
      ajustePendiente = false;
      ajustar(term, anfitrionActual);
    }
    programarDrenaje();
    if (enfocarAlMostrar && term !== null) {
      const t = term;
      cancelarFoco?.();
      cancelarFoco = programarFoco(
        () => {
          pasadasDeFoco++;
          t.focus();
          publicar();
        },
        () => visibleAhora,
      );
    }
    publicar();
  }

  // ---- input guards: right button at capture phase, IME composition -------------------------------------------
  function instalarGuardas(t: Terminal, el: HTMLElement): () => void {
    const tragarBotonDerecho = (ev: Event): void => {
      if ((ev as MouseEvent).button !== 2) return;
      ev.preventDefault();
      ev.stopPropagation();
    };
    const tipos = ['mousedown', 'mouseup', 'pointerdown', 'pointerup', 'auxclick', 'contextmenu'];
    for (const tipo of tipos) el.addEventListener(tipo, tragarBotonDerecho, true);

    // xterm reads the hidden textarea on a timer after compositionend; the textarea is cleared once that has run,
    // unless a new composition has started meanwhile, so the next input is never read against stale text.
    let componiendo = false;
    const pendientes = new Set<ReturnType<typeof setTimeout>>();
    const alEmpezar = (): void => {
      componiendo = true;
    };
    const alTerminar = (): void => {
      componiendo = false;
      const primero = setTimeout(() => {
        pendientes.delete(primero);
        const segundo = setTimeout(() => {
          pendientes.delete(segundo);
          if (!componiendo && t.textarea) t.textarea.value = '';
        }, 0);
        pendientes.add(segundo);
      }, 0);
      pendientes.add(primero);
    };
    el.addEventListener('compositionstart', alEmpezar);
    el.addEventListener('compositionend', alTerminar);
    return () => {
      for (const tipo of tipos) el.removeEventListener(tipo, tragarBotonDerecho, true);
      el.removeEventListener('compositionstart', alEmpezar);
      el.removeEventListener('compositionend', alTerminar);
      for (const p of pendientes) clearTimeout(p);
      pendientes.clear();
    };
  }

  // ---- the two mounts -----------------------------------------------------------------------------------------
  function montar(el: HTMLElement): () => void {
    montajes += 1; // reads and writes reactive state: this is why the mount runs untracked
    fase = 'conectando';
    fallo = '';
    let t: Terminal | null = null;
    try {
      t = protegerTerminal(new Terminal(opcionesDeTerminal({ scrollback })));
      t.open(el);
      for (const addon of crearAddonsPropios()) t.loadAddon(addon); // the drawer's own addons: none today
      for (const addon of crearAddons?.() ?? []) cargarAddon(t, addon); // requested ones: refused unless the drawer made them
      // line 2 of the guard: whatever got here, what the terminal produced is read back from its DOM
      const leido = leerRenderer(el);
      if (leido !== 'dom') throw new Error(`the terminal renders with "${leido}", not the DOM renderer: a renderer layer got past the guard`);
    } catch (e) {
      const causa = e instanceof Error ? e.message : String(e);
      try {
        t?.dispose();
      } catch (e2) {
        console.error(`terminal ${id}: dispose after a failed mount also failed: ${e2 instanceof Error ? e2.message : String(e2)}`);
      }
      fase = 'fallido';
      fallo = `terminal could not be mounted: ${causa}`;
      console.error(`terminal ${id}: ${fallo}`);
      alFase?.(id, 'fallido', fallo);
      return () => {};
    }
    const viva = t;
    renderer = leerRenderer(el);
    term = viva;
    anfitrionActual = el;
    sumidero = (lote, hecho) => viva.write(lote, hecho);
    viva.onData((d) => flujo?.enviarEntrada(codificador.encode(d)));
    viva.onBinary((d) => flujo?.enviarEntrada(Uint8Array.from(d, (c) => c.charCodeAt(0))));
    viva.onResize(({ cols, rows }) => flujo?.redimensionar(cols, rows));
    abrirFlujo();
    ajustar(viva, el);
    flujo?.redimensionar(viva.cols, viva.rows);
    const quitarGuardas = instalarGuardas(viva, el);
    const observador = new ResizeObserver(() => {
      if (visibleAhora) ajustar(viva, el);
      else ajustePendiente = true;
    });
    observador.observe(el);
    let desmontada = false;
    let dejarDeVigilar = (): void => {};
    const desmontar = (): void => {
      if (desmontada) return;
      desmontada = true;
      dejarDeVigilar();
      observador.disconnect();
      quitarGuardas();
      cerrarFlujo();
      term = null;
      anfitrionActual = null;
      try {
        viva.dispose();
      } catch (e) {
        console.error(`terminal ${id}: dispose failed: ${e instanceof Error ? e.message : String(e)}`);
      }
      alDesmontar?.(id);
    };
    // line 2 of the guard for the rest of the terminal's life: a canvas layer that shows up later fails the panel
    dejarDeVigilar = vigilarCapas(el, (motivo) => {
      desmontar();
      fase = 'fallido';
      fallo = motivo;
      renderer = 'canvas';
      console.error(`terminal ${id}: ${motivo}`);
      alFase?.(id, 'fallido', motivo);
    });
    alMontar?.(id, viva);
    cambiarVisibilidad(visibleAhora);
    publicar();
    return desmontar;
  }

  function montarTexto(el: HTMLElement): () => void {
    montajes += 1;
    fase = 'conectando';
    fallo = '';
    renderer = 'texto';
    const decodificador = new TextDecoder('utf-8');
    const lineas: string[] = [];
    let resto = '';
    sumidero = (lote, hecho) => {
      const partes = (resto + quitarAnsi(decodificador.decode(lote, { stream: true }))).split('\n');
      resto = partes.pop() ?? '';
      for (const p of partes) lineas.push(p);
      if (lineas.length > SCROLLBACK_MAX) lineas.splice(0, lineas.length - SCROLLBACK_MAX);
      el.textContent = lineas.join('\n') + (resto === '' ? '' : `\n${resto}`);
      el.scrollTop = el.scrollHeight;
      hecho();
    };
    abrirFlujo();
    cambiarVisibilidad(visibleAhora);
    publicar();
    return () => {
      cerrarFlujo();
    };
  }

  // The host element is the only reactive read of the mount effect; everything montar() touches is untracked.
  $effect(() => {
    const el = anfitrion;
    if (el === null) return;
    return untrack(() => montar(el));
  });

  $effect(() => {
    const el = texto;
    if (el === null) return;
    return untrack(() => montarTexto(el));
  });

  $effect(() => {
    const v = visible;
    untrack(() => cambiarVisibilidad(v));
  });
</script>

<div
  class="panel"
  bind:this={raiz}
  data-terminal-panel
  data-panel-id={id}
  data-modo={modo}
  data-visible={visible ? 'true' : 'false'}
  data-montajes={montajes}
  data-fase={fase}
  data-renderer={renderer}
>
  {#if modo === 'interactivo'}
    <div class="anfitrion" data-panel-host bind:this={anfitrion}></div>
  {:else}
    <pre class="texto" data-panel-texto bind:this={texto}></pre>
  {/if}
  {#if fallo !== ''}
    <div class="fallo" role="alert" data-panel-fallo>{fallo}</div>
  {/if}
</div>

<style>
  /* Every panel is laid out at all times (so its size is known) and painted only when visible: never display: none. */
  .panel {
    position: absolute;
    inset: 0;
    visibility: hidden;
    background: var(--surface-obsidian);
  }
  .panel[data-visible='true'] {
    visibility: visible;
  }
  .anfitrion {
    position: absolute;
    inset: 4px 6px;
    overflow: hidden;
  }
  .texto {
    box-sizing: border-box;
    height: 100%;
    margin: 0;
    padding: 4px 6px;
    overflow: auto;
    color: var(--text-polar);
    font: var(--text-xs);
    white-space: pre-wrap;
  }
  .fallo {
    position: absolute;
    right: 6px;
    bottom: 4px;
    left: 6px;
    padding: 2px 6px;
    background: var(--surface-cockpit);
    border: 1px solid var(--accent-crimson);
    color: var(--text-polar);
    font: var(--text-2xs);
  }
</style>
