<script lang="ts">
  import { tick } from 'svelte';
  import type { StatusBus } from './status';
  // FRONTEND (os-cu-0 F1-STATUS). One visible B3 region, mounted once by WIRE.
  let { bus }: { bus: StatusBus } = $props();
  const lines = $derived(bus.lines);
  let el: HTMLDivElement | undefined = $state();
  $effect(() => {
    const latest = $lines;
    let active = true;
    void tick().then(() => { if (active && el && latest.length) el.scrollTop = el.scrollHeight; });
    return () => { active = false; };
  });
</script>

<div bind:this={el} role="status" aria-live="polite" aria-atomic="false" aria-relevant="additions" aria-label="Operation status" id="operation-status" class="operation-status">
  {#each $lines as line (line.n)}<p data-n={line.n}>{line.text}</p>{/each}
</div>

<style>
  .operation-status {
    font: 500 13px/16px var(--font-mono);
    color: var(--text-polar, #F4F6FC);
    background: var(--surface-cockpit, #161D2E);
    min-width: 280px;
    max-width: 420px;
    height: 36px;
    overflow-y: auto;
    contain: strict;
  }
  p { margin: 0; }
</style>
