<script lang="ts">
  /** The exact Go document from EstadoCanonico (the same producer as GET /state). */
  let { bytes = '' }: { bytes?: string } = $props();
</script>

<section class="state-mirror" role="region" aria-label="cockpit-state" aria-live="off"><pre>{bytes}</pre></section>

<style>
  .state-mirror {
    position: absolute;
    width: 1px;
    height: 1px;
    padding: 0;
    margin: -1px;
    overflow: hidden;
    clip-path: inset(50%);
    white-space: nowrap;
    border: 0;
  }
</style>
