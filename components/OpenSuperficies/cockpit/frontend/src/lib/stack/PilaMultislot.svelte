<script lang="ts" generics="T extends ElementoDePila">
  // Vertical multislot stack (spec 2.1 and 2.2): every card is mounted for as long as the stack is, one card per
  // viewport at 1/6 and two at 1/3 (dual), snapped per viewport, over a Fenwick height index.
  //
  // PERMANENT MOUNTING. The live surface of a card (an Xpra iframe) is rendered unconditionally: folding a card, moving
  // it to another virtual screen or scrolling it out of view only changes CSS (`display: none`, `content-visibility:
  // hidden`), never the component tree. Unmounting a surface drops its WebSocket and costs a 16.2 s reconnect
  // [MEASURED v1]. The two SRF-SUPERFICIE markers around the render tag below are the only place the instrument's
  // contrast (scripts/ng/medir-scroll.mjs --envolver-if) touches: it compiles a copy of THIS file with the surface
  // wrapped in `{#if visible}` and measures the unmounts that appear. Keep the markers, keep the surface inside them,
  // and never wrap it in a condition.
  //
  // The stack owns no figure: the layout of every mode comes from ./pantallas.ts, which takes it from lib/geometria.ts.
  import { untrack } from 'svelte';
  import type { Snippet } from 'svelte';
  import type { WindowMode } from '../types/hypervisor';
  import { FenwickArbol } from './fenwick';
  import { PANTALLA_PRIMERA, alturasDeItems, disposicion, estaOculta, paradaVecina, ultimaParada } from './pantallas';
  import type { ElementoDePila, Pantalla } from './pantallas';

  interface Props {
    /** The cards, in stack order. A card is mounted for as long as its id is in this list. */
    tarjetas: readonly T[];
    modo: WindowMode;
    /** The live surface of a card. Rendered once per card and never inside a condition. */
    tarjeta: Snippet<[T, number]>;
    /** The card bar of the dual layout: it sits under a card and carries the identity of the NEXT one. */
    barra?: Snippet<[T, number]>;
    pantallas?: readonly Pantalla[];
    pantallaActiva?: string;
    /** The card at the top of the viewport. Setting it scrolls there. */
    indice?: number;
    /** A card left or entered the neighbourhood of the viewport: the surface adapter's `pause(ocluida)`; the socket stays warm. */
    alCambiarOclusion?: (id: string, ocluida: boolean) => void;
  }

  const PANTALLAS_POR_DEFECTO: readonly Pantalla[] = [{ id: PANTALLA_PRIMERA }];

  let {
    tarjetas,
    modo,
    tarjeta,
    barra,
    pantallas = PANTALLAS_POR_DEFECTO,
    pantallaActiva = PANTALLA_PRIMERA,
    indice = $bindable(0),
    alCambiarOclusion,
  }: Props = $props();

  const disp = $derived(disposicion(modo));
  const ocultas = $derived(tarjetas.map((t) => estaOculta(pantallas, t, pantallaActiva)));

  // ---- the height index --------------------------------------------------------------------------------------
  // Non-reactive on purpose: `generacion` is what the template reads to know the index moved. Only the heights that
  // changed are written (O(log n) each), so a fold or a screen swap does not rebuild the tree.
  const arbol = FenwickArbol.desdeAlturas([]);
  let generacion = $state(0);
  $effect.pre(() => {
    arbol.sincronizar(alturasDeItems(modo, ocultas));
    generacion = arbol.generacion;
  });

  const alturas = $derived.by(() => {
    void generacion;
    return tarjetas.map((_, i) => arbol.altura(i));
  });
  const desplazamientos = $derived.by(() => {
    void generacion;
    return tarjetas.map((_, i) => arbol.offsetDe(i));
  });
  /** For the card bar under item i: the next visible card, or -1 when there is none (the last card has no bar). */
  const siguientes = $derived(
    tarjetas.map((_, i) => {
      for (let j = i + 1; j < ocultas.length; j++) if (!ocultas[j]) return j;
      return -1;
    }),
  );

  // ---- occlusion: what is near the viewport is painted, the rest is skipped, all of it stays mounted ------------
  let raiz = $state<HTMLElement | undefined>(undefined);
  let enVista = $state<Record<string, boolean>>({});

  $effect(() => {
    const el = raiz;
    if (el === undefined) return;
    void tarjetas.map((t) => t.id).join('|'); // observe the cards again when the list changes
    const observador = new IntersectionObserver(
      (entradas) => {
        for (const e of entradas) {
          const id = (e.target as HTMLElement).dataset.id;
          if (id === undefined || untrack(() => enVista[id]) === e.isIntersecting) continue;
          enVista[id] = e.isIntersecting;
          alCambiarOclusion?.(id, !e.isIntersecting);
        }
      },
      // One viewport of margin above and below: the neighbours are painted before they scroll in, so paging never shows a blank card.
      { root: el, rootMargin: '100% 0px', threshold: 0 },
    );
    for (const item of el.querySelectorAll('[data-item]')) observador.observe(item);
    return () => observador.disconnect();
  });

  // ---- paging ------------------------------------------------------------------------------------------------
  let desdeDesplazamiento = -1;

  function alDesplazar(): void {
    if (raiz === undefined || arbol.longitud === 0) return;
    // The card at the top is the one that contains the offset half a step under the top edge.
    const i = Math.min(arbol.indicePorOffset(raiz.scrollTop + Math.floor(disp.paso / 2)), arbol.longitud - 1);
    desdeDesplazamiento = i;
    if (i !== indice) indice = i;
  }

  /** Scrolls so that card i is at the top of the viewport (the snap point of that card). */
  export function irA(i: number, suave = true): void {
    if (raiz === undefined) return;
    const destino = Math.max(0, Math.min(i, ultimaParada(arbol, disp.alturaVista)));
    raiz.scrollTo({ top: arbol.offsetDe(destino), behavior: suave ? 'smooth' : 'instant' });
  }

  $effect(() => {
    const pedido = indice;
    if (pedido !== desdeDesplazamiento) untrack(() => irA(pedido, false));
  });

  // A fold or a screen swap moves the offsets under the viewport: keep the card that was on top on top.
  $effect(() => {
    void generacion;
    untrack(() => {
      if (raiz === undefined || arbol.longitud === 0) return;
      const i = Math.min(indice, ultimaParada(arbol, disp.alturaVista));
      const esperado = arbol.offsetDe(i);
      if (raiz.scrollTop !== esperado && !ocultas[i]) raiz.scrollTo({ top: esperado, behavior: 'instant' });
    });
  });

  function alTeclear(ev: KeyboardEvent): void {
    if (ev.target !== ev.currentTarget) return; // Page keys belong to the focused stack, never nested controls.
    const sentido = ev.key === 'PageDown' ? 1 : ev.key === 'PageUp' ? -1 : 0;
    if (sentido === 0 || arbol.total() === 0) return; // nothing visible to page over
    ev.preventDefault();
    const actual = Math.min(arbol.indicePorOffset(raiz?.scrollTop ?? 0), arbol.longitud - 1);
    irA(paradaVecina(arbol, actual, sentido, disp.alturaVista));
  }
</script>

<!-- A scrollable region must take keyboard focus (WCAG 2.1.1), and PageUp/PageDown page it: the two a11y hints below do not apply. -->
<!-- svelte-ignore a11y_no_noninteractive_tabindex, a11y_no_noninteractive_element_interactions -->
<div
  class="pila"
  data-pila
  data-modo={modo}
  data-tarjetas-por-vista={disp.tarjetasPorVista}
  data-indice-actual={indice}
  data-generacion={generacion}
  style:--pila-ancho="{disp.ancho}px"
  style:--pila-vista="{disp.alturaVista}px"
  style:--pila-tarjeta-h="{disp.alturaTarjeta}px"
  style:--pila-tarjeta-w="{disp.anchoTarjeta}px"
  style:--pila-barra-h="{disp.alturaBarra}px"
  style:--pila-gutter="{disp.gutterIzq}px"
  role="region"
  aria-label="Capsule stack"
  tabindex="0"
  bind:this={raiz}
  onscroll={alDesplazar}
  onkeydown={alTeclear}
>
  {#each tarjetas as t, i (t.id)}
    {@const oculta = ocultas[i]}
    {@const visible = enVista[t.id] !== false}
    <div
      class="pila-item"
      data-item
      data-id={t.id}
      data-indice={i}
      data-oculta={oculta}
      data-desplazamiento={desplazamientos[i]}
      style:height="{alturas[i]}px"
      style:display={oculta ? 'none' : null}
    >
      <div
        class="pila-tarjeta"
        data-tarjeta
        data-id={t.id}
        data-visible={visible}
        style:content-visibility={visible ? null : 'hidden'}
      >
        <!-- SRF-SUPERFICIE-INICIO -->
        {@render tarjeta(t, i)}
        <!-- SRF-SUPERFICIE-FIN -->
      </div>
      {#if disp.alturaBarra > 0 && siguientes[i] >= 0}
        <div class="pila-barra" data-barra-tarjeta data-id={tarjetas[siguientes[i]].id}>
          {#if barra}{@render barra(tarjetas[siguientes[i]], siguientes[i])}{/if}
        </div>
      {/if}
    </div>
  {/each}
</div>

<style>
  .pila {
    position: relative;
    box-sizing: border-box;
    width: var(--pila-ancho);
    height: var(--pila-vista);
    overflow-x: hidden;
    overflow-y: auto;
    scroll-snap-type: y mandatory;
    overscroll-behavior: contain;
    scrollbar-width: none; /* a classic scrollbar would take width from the pixel-exact canvas */
    background: var(--canvas-void, #07090e);
    outline: none;
  }

  .pila:focus-visible {
    outline: 2px solid var(--border-focus, #00e5ff);
    outline-offset: -2px;
  }

  .pila-item {
    box-sizing: border-box;
    scroll-snap-align: start;
    scroll-snap-stop: always; /* a fling never skips a card */
    overflow: hidden;
  }

  .pila-tarjeta {
    box-sizing: border-box;
    margin-left: var(--pila-gutter);
    width: var(--pila-tarjeta-w);
    height: var(--pila-tarjeta-h);
    overflow: hidden;
    background: var(--surface-obsidian, #0e131f);
  }

  .pila-barra {
    box-sizing: border-box;
    height: var(--pila-barra-h);
    overflow: hidden;
    background: var(--surface-cockpit, #161d2e);
    border-top: 1px solid var(--border-subtle, #222d42);
  }
</style>
