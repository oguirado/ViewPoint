<script lang="ts">
  import { onMount } from 'svelte';
  import { get } from 'svelte/store';
  import { crearServiciosStore, type ServiciosStore } from '../stores/servicios';
  let { store = crearServiciosStore() }: { store?: ServiciosStore } = $props();
  onMount(() => { void store.refresh(); });
  async function toggle(capsule: string, service: string, event: Event): Promise<void> {
    const input = event.currentTarget as HTMLInputElement;
    await store.toggle(capsule, service, input.checked);
    // Restore the observed membership on refusal or a failed read after a write.
    input.checked = get(store).snapshot?.assignments[capsule]?.includes(service) ?? false;
  }
</script>

<section class="servicios" aria-label="Servicios web" aria-busy={$store.busy}>
  <header>
    <div><h2>Servicios web</h2><p>Asigna servicios a cada cápsula y revisa las solicitudes de las IA.</p></div>
    <button type="button" aria-label="Actualizar servicios web" aria-disabled={$store.busy} onclick={() => store.refresh()}>Actualizar</button>
  </header>
  <p class="revision">{$store.snapshot ? `Revisión ${$store.snapshot.revision}` : 'Revisión —'}{$store.busy ? ' · Cargando…' : ''}</p>
  {#if $store.error}<p role="alert" class="error">{$store.error}</p>{/if}
  {#if $store.snapshot}
    <h3>Catálogo aprobado</h3>
    {#if $store.snapshot.services.length}
      <ul class="catalogo">{#each $store.snapshot.services as service (service)}<li>{service}</li>{/each}</ul>
    {:else}<p>No hay servicios aprobados en el catálogo.</p>{/if}
    <h3>Asignaciones</h3>
    {#if $store.snapshot.capsules.length && $store.snapshot.services.length}
      <!-- svelte-ignore a11y_no_noninteractive_tabindex (keyboard access to horizontal scrolling) -->
      <div class="matrix" tabindex="0" role="region" aria-label="Matriz de servicios por cápsula">
        <table>
          <caption>Un servicio marcado está asignado a esa cápsula.</caption>
          <thead><tr><th scope="col">Cápsula</th>{#each $store.snapshot.services as service (service)}<th scope="col">{service}</th>{/each}</tr></thead>
          <tbody>{#each $store.snapshot.capsules as capsule (capsule)}
            <tr><th scope="row">{capsule}</th>{#each $store.snapshot.services as service (service)}
              <td><input type="checkbox" aria-label={`Asignar ${service} ${capsule}`} aria-disabled={$store.busy}
                checked={$store.snapshot.assignments[capsule]?.includes(service) ?? false}
                onchange={(event) => toggle(capsule, service, event)} /></td>
            {/each}</tr>
          {/each}</tbody>
        </table>
      </div>
    {:else}<p>No hay asignaciones disponibles. Las cápsulas activas y las solicitudes aparecerán aquí.</p>{/if}
    <h3>Solicitudes pendientes</h3>
    {#if $store.snapshot.pending.length}
      <ul class="pending">{#each $store.snapshot.pending as request (request.id)}
        <li>
          <div><strong>{request.service}</strong> · {request.capsule}<p>{request.ai_id} · {request.reason || 'Sin motivo indicado'}</p></div>
          <div class="actions">
            <button type="button" aria-label={`Aprobar ${request.service} ${request.capsule} ${request.id}`} aria-disabled={$store.busy} onclick={() => store.approve(request.id)}>Aprobar</button>
            <button type="button" aria-label={`Denegar ${request.service} ${request.capsule} ${request.id}`} aria-disabled={$store.busy} onclick={() => store.deny(request.id)}>Denegar</button>
          </div>
        </li>
      {/each}</ul>
    {:else}<p>No hay solicitudes pendientes.</p>{/if}
  {:else if !$store.busy && !$store.error}<p>Actualiza para consultar los servicios.</p>{/if}
</section>

<style>
  .servicios { padding: 16px; color: var(--text-polar); background: var(--surface-cockpit); font: var(--text-base); border: 1px solid var(--border-bold); border-radius: var(--radius-chrome); }
  header, .pending li { display: flex; align-items: start; justify-content: space-between; gap: 16px; }
  h2 { font: var(--text-lg); margin: 0; }
  h3 { font: var(--text-base); margin: 20px 0 8px; }
  p { margin: 4px 0 8px; }
  .revision { color: var(--text-silver); font: var(--text-xs); font-variant-numeric: tabular-nums; }
  .catalogo { display: flex; flex-wrap: wrap; gap: 8px; padding: 0; list-style: none; }
  .catalogo li { padding: 4px 8px; border: 1px solid var(--border-bold); border-radius: var(--r-control); font-family: var(--font-mono); }
  .matrix { overflow: auto; }
  table { width: 100%; border-collapse: collapse; }
  caption { text-align: left; padding: 0 0 8px; color: var(--text-silver); }
  th, td { padding: 8px; border-bottom: 1px solid var(--border-bold); text-align: center; white-space: nowrap; }
  th:first-child { text-align: left; font-family: var(--font-mono); }
  input { width: 20px; height: 20px; accent-color: var(--accent-cyan); cursor: pointer; }
  button { color: var(--text-polar); background: var(--surface-obsidian); border: 1px solid var(--border-bold); border-radius: var(--r-control); padding: 6px 10px; min-height: 32px; cursor: pointer; font: inherit; }
  button:hover { border-color: var(--accent-cyan); }
  :is(button, input, .matrix):focus-visible { outline: 2px solid var(--accent-cyan); outline-offset: 3px; }
  [aria-disabled="true"] { cursor: progress; opacity: .65; }
  .pending { padding: 0; list-style: none; }
  .pending li { padding: 12px 0; border-bottom: 1px solid var(--border-bold); overflow-wrap: anywhere; }
  .actions { display: flex; gap: 8px; flex-shrink: 0; }
  .error { border-left: 3px solid var(--accent-crimson); padding: 8px; overflow-wrap: anywhere; }
  @media (max-width: 600px) { header, .pending li { flex-direction: column; gap: 8px; } }
</style>
