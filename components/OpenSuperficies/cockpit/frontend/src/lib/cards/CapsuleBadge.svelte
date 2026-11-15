<script lang="ts">
  // One badge of section 3.3. The chip is opaque (--canvas-void) so its label contrast never depends on the remote
  // picture behind a translucent overlay; the accent colours that do not reach 7:1 as text (crimson, violet, muted)
  // only paint the border and the dot, and visor.ts keeps the label in a text token.
  import type { Insignia } from './visor';

  let { insignia }: { insignia: Insignia } = $props();
</script>

<span
  class="insignia"
  data-insignia
  data-clase={insignia.clase}
  data-valor={insignia.valor}
  data-animacion={insignia.animacion ?? 'ninguna'}
  style:--indicador="var({insignia.indicador})"
  style:--rotulo="var({insignia.textoToken})"
>
  <span class="punto" aria-hidden="true"></span>
  <span class="texto">{insignia.texto}</span>
</span>

<style>
  .insignia {
    box-sizing: border-box;
    display: inline-flex;
    align-items: center;
    gap: 0.375rem;
    height: 20px;
    padding: 0 0.5rem;
    background: var(--canvas-void);
    border: 1px solid var(--indicador);
    border-radius: var(--radius-chrome);
    color: var(--rotulo);
    font: var(--text-2xs);
    font-variant-numeric: tabular-nums;
    letter-spacing: 0.04em;
    white-space: nowrap;
  }

  .punto {
    flex: none;
    width: 6px;
    height: 6px;
    border-radius: 50%;
    background: var(--indicador);
  }

  /* 800 ms pulse loop until the state is confirmed (spec 9). The ring animates, never the label: contrast holds. */
  .insignia[data-animacion='pulso'] {
    animation: pulso 800ms ease-in-out infinite;
  }

  /* 600 ms x 3, then steady (spec 9). */
  .insignia[data-animacion='parpadeo'] {
    animation: parpadeo 600ms steps(2) 3;
  }

  @keyframes pulso {
    0%,
    100% {
      box-shadow: 0 0 0 0 transparent;
    }
    50% {
      box-shadow: 0 0 0 3px var(--indicador);
    }
  }

  @keyframes parpadeo {
    0% {
      box-shadow: 0 0 0 3px var(--indicador);
    }
    100% {
      box-shadow: 0 0 0 0 transparent;
    }
  }

  /* Reduced motion replaces every pulse and blink with a static state (spec 9, F-12): a solid ring. */
  @media (prefers-reduced-motion: reduce) {
    .insignia[data-animacion='pulso'],
    .insignia[data-animacion='parpadeo'] {
      animation: none;
      box-shadow: 0 0 0 2px var(--indicador);
    }
  }
</style>
