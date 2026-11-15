<script lang="ts">
  // Overlay telemetry strip (spec 3.1): 28 px at the bottom of the canvas, INSIDE it. CPU, RAM, port pair and the
  // gate pills. Plain text on the translucent bar is --text-polar (the only text token that holds 7:1 over the
  // worst backdrop); every coloured statement is a badge on an opaque chip. It never captures a pointer event: it has
  // no control, so a click anywhere on it reaches the remote surface.
  import type { Geometry, SurfaceCapsule, SlotBlock } from '../types/hypervisor';
  import CapsuleBadge from './CapsuleBadge.svelte';
  import { ARQUETIPOS, formatoPuertos, insigniaEscala, pildorasGates } from './visor';

  import { celdaCpu, celdaRam, slotIdCanonico } from '../telemetry/celdas';

  let { capsula, geometria, visible, metricas = null, docTs = 0, ambito = 'capsule' }: {
    capsula: SurfaceCapsule; geometria: Geometry; visible: boolean;
    metricas?: SlotBlock['telemetry'] | null; docTs?: number; ambito?: 'capsule' | 'host';
  } = $props();
  const identidad = $derived(ambito === 'host' ? 'host' : slotIdCanonico(capsula.slot));
  const cpu = $derived(celdaCpu(metricas?.cpu_permille, docTs));
  const ram = $derived(celdaRam(metricas?.mem_bytes, docTs));

  const pildoras = $derived(pildorasGates(capsula.gates));
  const escala = $derived(insigniaEscala(geometria));
</script>

<div
  class="franja"
  data-franja
  data-visible={visible}
  data-primaria={ARQUETIPOS[capsula.archetype].telemetriaPrimaria}
  role="group"
  aria-label="Telemetry {identidad}"
>
  <span class="campo" class:telemetry-stale={cpu.source === 'none'} data-campo="cpu"
    role="group" aria-label="Strip CPU {identidad}: {cpu.source === 'none' ? 'no data' : `${cpu.texto.slice(4)} · ${cpu.source}`}"
    data-sensor={cpu.source} data-ts={cpu.ts ?? undefined}>{cpu.texto}</span>
  <span class="campo" class:telemetry-stale={ram.source === 'none'} data-campo="ram"
    role="group" aria-label="Strip RAM {identidad}: {ram.source === 'none' ? 'no data' : `${ram.texto.slice(4)} · ${ram.source}`}"
    data-sensor={ram.source} data-ts={ram.ts ?? undefined}>{ram.texto}</span>
  <span class="campo" data-campo="puertos">{formatoPuertos(capsula)}</span>
  {#each pildoras as p (p.valor)}
    <span class="pildora" role="group" data-ok={p.ok} aria-label="Strip {identidad} {p.texto} {p.ok ? 'holds' : 'fails'}"><CapsuleBadge insignia={p} /></span>
  {/each}
  {#if escala !== null}
    <CapsuleBadge insignia={escala} />
  {/if}
</div>

<style>
  .franja {
    position: absolute;
    left: 0;
    right: 0;
    bottom: 0;
    box-sizing: border-box;
    display: flex;
    align-items: center;
    gap: 0.75rem;
    height: var(--overlay-foot-h);
    padding: 0 0.75rem;
    overflow: hidden;
    white-space: nowrap;
    background: color-mix(in srgb, var(--surface-cockpit) 88%, transparent);
    border-top: 1px solid var(--border-subtle);
    color: var(--text-polar);
    font: var(--text-xs);
    font-variant-numeric: tabular-nums;
    pointer-events: none;
    opacity: 0;
  }

  .franja[data-visible='true'] {
    opacity: 1;
  }

  .campo.telemetry-stale {
    color: var(--text-polar);
  }

  .pildora {
    display: inline-flex;
  }
</style>
