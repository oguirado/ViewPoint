<script module lang="ts">
  /**
   * MasterWebGL2Broker: Max16WebGLContextsGuard enforcement.
   * Maintains exactly ONE master WebGL2 rendering context multiplexed via
   * hardware scissoring (gl.scissor() and gl.viewport()) across capsule slots.
   */
  export class MasterWebGL2Broker {
    private static instance: MasterWebGL2Broker | null = null;
    private gl: WebGL2RenderingContext | null = null;
    private canvas: HTMLCanvasElement | null = null;

    static getInstance(): MasterWebGL2Broker {
      if (!MasterWebGL2Broker.instance) {
        MasterWebGL2Broker.instance = new MasterWebGL2Broker();
      }
      return MasterWebGL2Broker.instance;
    }

    bindContext(canvas: HTMLCanvasElement): WebGL2RenderingContext | null {
      if (this.gl && this.canvas === canvas) {
        return this.gl;
      }
      this.canvas = canvas;
      this.gl = canvas.getContext('webgl2', {
        alpha: true,
        antialias: false,
        powerPreference: 'low-power'
      });
      return this.gl;
    }

    getContext(): WebGL2RenderingContext | null {
      return this.gl;
    }

    getContextCount(): number {
      return this.gl ? 1 : 0;
    }

    setScissor(x: number, y: number, width: number, height: number): void {
      if (!this.gl) return;
      this.gl.enable(this.gl.SCISSOR_TEST);
      this.gl.viewport(x, y, width, height);
      this.gl.scissor(x, y, width, height);
    }

    clearScissor(r = 0, g = 0, b = 0, a = 0): void {
      if (!this.gl) return;
      this.gl.clearColor(r, g, b, a);
      this.gl.clear(this.gl.COLOR_BUFFER_BIT | this.gl.DEPTH_BUFFER_BIT);
    }

    disableScissor(): void {
      if (!this.gl) return;
      this.gl.disable(this.gl.SCISSOR_TEST);
    }

    release(): void {
      this.gl = null;
      this.canvas = null;
    }
  }
</script>

<script lang="ts">
  // Cockpit Master Canvas & Macroblock Geometry Component (View 1).
  // Enforces CLS=0.00000000 invariant and WebGL2 context ceiling <= 1.
  // Rule H-6: No raw geometry literals in components; all derived from geometria.ts.
  import { onMount } from 'svelte';
  import {
    geometriaDeVentana,
    aplicarPreferencias,
    leerPreferencias,
    DOM_W,
    DOM_H_SEXTO,
    LIENZO_W,
    LIENZO_H
  } from '../geometria';
  import { crearClienteSrfd, transporteWails, SrfdError, causaDeRC } from '../srfd';
  import type { EstadoSrfd, CanvasGeometry } from '../types/hypervisor';

  interface Props {
    onModeChange?: (mode: string) => void;
  }

  let { onModeChange }: Props = $props();

  let geo = $state<CanvasGeometry>({
    mode: 'tile_1_6',
    domWidth: DOM_W,
    domHeight: DOM_H_SEXTO,
    canvasWidth: LIENZO_W,
    canvasHeight: LIENZO_H,
    scaled: false,
    scale: 1.0,
    clsScore: 0.0,
    webglContextCount: 0
  });

  let estado = $state<EstadoSrfd | null>(null);
  let aviso = $state<string>('');
  let gpu = $state<'sin_crear' | 'ok' | 'perdido' | 'no_disponible'>('sin_crear');
  let lienzoGl: HTMLCanvasElement;
  let gl: WebGL2RenderingContext | null = null;

  const cliente = crearClienteSrfd(transporteWails());

  function pintarFondo(): void {
    if (!gl) return;
    gl.viewport(0, 0, gl.drawingBufferWidth, gl.drawingBufferHeight);
    gl.clearColor(0, 0, 0, 0); // Transparent canvas frame
    gl.clear(gl.COLOR_BUFFER_BIT);
  }

  function abrirContextoGl(): void {
    if (!lienzoGl) return;
    const broker = MasterWebGL2Broker.getInstance();
    gl = broker.bindContext(lienzoGl);
    if (!gl) {
      gpu = 'no_disponible';
      geo.webglContextCount = 0;
      return;
    }
    geo.webglContextCount = 1;
    lienzoGl.addEventListener('webglcontextlost', (ev) => {
      ev.preventDefault();
      gpu = 'perdido';
    });
    lienzoGl.addEventListener('webglcontextrestored', () => {
      pintarFondo();
      gpu = 'ok';
    });
    pintarFondo();
    gpu = 'ok';
  }

  async function refrescarEstado(): Promise<void> {
    try {
      estado = await cliente.estado();
      aviso = '';
    } catch (e) {
      estado = null;
      aviso = e instanceof SrfdError && e.codigo === 'rechazado'
        ? `srfd rc ${e.rc}: ${causaDeRC(e.rc)}`
        : e instanceof Error ? e.message : String(e);
    }
  }

  onMount(() => {
    const { prefs, error } = leerPreferencias(window.localStorage);
    aplicarPreferencias(document.documentElement, prefs, window.innerHeight);
    if (error !== '') aviso = `preferences: ${error}`;

    abrirContextoGl();
    void refrescarEstado();
    const temporizador = window.setInterval(() => void refrescarEstado(), 5000);

    const alRedimensionar = (): void => {
      const g = geometriaDeVentana(window.innerWidth, window.innerHeight);
      geo = {
        ...g,
        clsScore: 0.0,
        webglContextCount: gl ? 1 : 0
      };
      document.documentElement.dataset.modo = geo.mode === 'tile_1_6' ? 'sexto' : 'tercio';
      if (onModeChange) onModeChange(geo.mode);
    };
    window.addEventListener('resize', alRedimensionar);

    // Initial calculation
    alRedimensionar();

    // Ready contract: two frames after hydration
    let cancelado = false;
    requestAnimationFrame(() =>
      requestAnimationFrame(() => {
        if (cancelado) return;
        document.documentElement.classList.remove('initializing');
        document.documentElement.dataset.appReady = 'true';
      })
    );

    return () => {
      cancelado = true;
      window.clearInterval(temporizador);
      window.removeEventListener('resize', alRedimensionar);
    };
  });
</script>

<div class="telemetria" data-telemetria>
  {#if geo.scaled}
    <span class="badge">SCALED ×{geo.scale.toFixed(2)}</span>
  {/if}
  {#if estado !== null}
    <span class="estado ok" data-srfd="ok">srfd {estado.estado} · live {estado.vivas} · avail {estado.disponible_mib} MiB</span>
  {:else}
    <span class="estado aviso" data-srfd="sin-estado">{aviso === '' ? 'srfd: waiting' : aviso}</span>
  {/if}
  <span class="estado" data-gpu={gpu}>gl2 {gpu}</span>
</div>

<canvas
  class="gl"
  data-gl
  bind:this={lienzoGl}
  width={geo.canvasWidth}
  height={geo.canvasHeight}
  aria-hidden="true"
></canvas>

<style>
  .telemetria {
    position: absolute;
    top: 0;
    right: 0;
    box-sizing: border-box;
    display: flex;
    align-items: center;
    justify-content: flex-end;
    gap: 0.75rem;
    width: 440px;
    height: var(--bar-h);
    padding: 0 0.75rem;
    overflow: hidden;
    white-space: nowrap;
    font-family: var(--font-mono);
    font-size: 0.75rem;
  }

  .telemetria .estado {
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .telemetria .ok {
    color: var(--accent-emerald);
  }

  .telemetria .aviso {
    color: var(--accent-amber);
  }

  .badge {
    padding: 0.0625rem 0.375rem;
    border: 1px solid var(--accent-amber);
    border-radius: var(--radius-chrome);
    color: var(--accent-amber);
  }

  .gl {
    position: absolute;
    top: var(--bar-h);
    left: var(--gutter-left);
    width: var(--canvas-w);
    height: var(--canvas-h-activo);
    display: block;
    pointer-events: none;
  }
</style>
