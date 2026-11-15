<script lang="ts">
  import CajonAgentes from '../src/lib/terminal/CajonAgentes.svelte';
  import type { AgenteCajon } from '../src/lib/terminal/CajonAgentes.svelte';
  let { n, w, h, crearSocket }: { n: number; w: number; h: number; crearSocket: (url: string) => WebSocket } = $props();
  const todos: AgenteCajon[] = [
    { id: 'tab-claude-0', titulo: 'Claude Code', superficie: 'srf-ng-0', url: 'ws://127.0.0.1:10020/pty/claude-0' },
    { id: 'tab-gemini-1', titulo: 'Gemini CLI', superficie: 'srf-ng-1', url: 'ws://127.0.0.1:10021/pty/gemini-1' },
    { id: 'tab-bash-host', titulo: 'bash', superficie: 'host', url: 'ws://127.0.0.1:10022/pty/bash' },
  ];
  let agentes = $state(todos.slice(0, n));
  let abierto = $state(true);
  let activo = $state('tab-claude-0');
  let ultimaAccion = $state('');
</script>

<div data-banco style="position:relative;width:{w}px;height:{h}px;margin:3px 0 0 3px;overflow:hidden;background:#0e131f">
  <div style="position:absolute;inset:0;display:flex;align-items:center;justify-content:center;color:#7f8da3;font:12px monospace">surface behind the drawer · last action: {ultimaAccion || '—'}</div>
  <CajonAgentes {agentes} bind:abierto bind:activo {crearSocket} alAccion={(a, id, t) => (ultimaAccion = `${a} ${id ?? ''} ${t}`.trim())} />
</div>
