import { readState, stateBridge, type StateBridge } from './state';
import { createStatusBus, type StatusBus } from './status';
import type { CooperationBridge } from './CapsuleBar.svelte';
import type { RecordingBridge } from './RecordingControls.svelte';
import type { CockpitState } from '../types/hypervisor';

/** Preserve bound method receivers and forward credentials unchanged. No authority is minted here. */
function binding<T extends object>(global: unknown, names: readonly string[]): T | null {
  const app = (global as { go?: { main?: { App?: Record<string, unknown> } } } | null)?.go?.main?.App;
  if (!app || names.some(name => typeof app[name] !== 'function')) return null;
  return Object.fromEntries(names.map(name => [name, (app[name] as (...args: unknown[]) => unknown).bind(app)])) as T;
}
export function cooperationBridge(global: unknown = globalThis): (CooperationBridge & { AbortarTodo(): Promise<void> }) | null {
  return binding(global, ['AbortarTodo', 'CopiarSlot', 'CederControl', 'TomarControl', 'CerrarSlot', 'FijarDisposicion', 'EstadoCanonico']);
}
export function recordingBridge(global: unknown = globalThis): RecordingBridge | null {
  return binding(global, ['IniciarGrabacionSlot', 'DetenerGrabacionSlot', 'EstadoGrabacion', 'EstadoCanonico']);
}
export interface ShellWire {
  bus: StatusBus;
  refresh(): Promise<{ bytes: string; document: CockpitState } | null>;
  dispose(): void;
}
/** Polling is owned by App; serialize reads and ignore late completion after unmount. */
export function createShellWire(bridge: StateBridge | null = stateBridge(), bus = createStatusBus()): ShellWire {
  let closed = false;
  let pending: Promise<{ bytes: string; document: CockpitState } | null> | null = null;
  return {
    bus,
    refresh() {
      if (closed) return Promise.resolve(null);
      if (pending) return pending;
      pending = (async () => {
        try {
          if (!bridge) throw new Error('AUSENTE: canonical state bridge');
          const next = await readState(bridge);
          if (closed) return null;
          bus.ingest(next.document);
          return next;
        } catch (error: unknown) {
          if (!closed) bus.emit({kind: 'degraded', scope: 'cockpit', literal: error instanceof Error ? error.message : String(error)});
          return null;
        } finally { pending = null; }
      })();
      return pending;
    },
    dispose() { closed = true; },
  };
}
