<script lang="ts">
  // The Xpra HTML5 surface of a capsule: ONE sandboxed iframe at the fixed CSS size of the canvas (spec 3.2). The
  // frame is never wrapped in {#if} and its src is derived from the port alone, so nothing a switch changes (mode,
  // health, badges, overlays) can navigate, replace or unmount it (spec 2.1, 4). Only an explicit disconnect()
  // of the adapter navigates it. The aspect-fit scale (--escala-local) is written by the adapter's fitLocal() alone.
  import { getContext } from 'svelte';
  import { SANDBOX_XPRA, urlXpra, FRAMEBUFFER_CONTEXT, type FramebufferContext, type TamanoLienzo } from './visor';

  let {
    puerto,
    tamano,
    titulo,
    iframe = $bindable(null),
    envoltura = $bindable(null),
  }: {
    puerto: number;
    tamano: TamanoLienzo;
    titulo: string;
    iframe?: HTMLIFrameElement | null;
    envoltura?: HTMLElement | null;
  } = $props();

  const framebuffer = getContext<FramebufferContext | undefined>(FRAMEBUFFER_CONTEXT);
  const size = $derived(framebuffer?.() ?? tamano);
  const url = $derived(urlXpra(puerto));
</script>

<div class="envoltura" data-xpra-envoltura data-superficie-host bind:this={envoltura} style:width="{size.w}px" style:height="{size.h}px">
  <iframe
    bind:this={iframe}
    class="xpra"
    data-xpra
    title={titulo}
    src={url}
    sandbox={SANDBOX_XPRA}
    referrerpolicy="no-referrer"
    width={size.w}
    height={size.h}
    style:width="{size.w}px"
    style:height="{size.h}px"
  ></iframe>
</div>

<style>
  .envoltura {
    position: absolute;
    top: 0;
    left: 0;
    overflow: hidden;
    background: var(--surface-obsidian);
    transform-origin: 0 0;
    transform: scale(var(--escala-local, 1));
  }

  /* Off the viewport the socket stays warm and painting stops (spec 2.1): never an unmount. */
  /* data-pausado is written by the adapter's pause(), outside the template. */
  .envoltura:global([data-pausado='true']) {
    content-visibility: hidden;
  }

  .xpra {
    display: block;
    border: 0;
    background: var(--surface-obsidian);
  }
</style>
