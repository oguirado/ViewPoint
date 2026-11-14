import { get, writable, type Readable } from 'svelte/store';

export interface ServicioRequest {
  id: string;
  ai_id: string;
  capsule: string;
  service: string;
  reason: string;
  state: string;
  revision: number;
}
export interface ServiciosSnapshot {
  services: string[];
  capsules: string[];
  assignments: Record<string, string[]>;
  pending: ServicioRequest[];
  revision: number;
}
export interface ServiciosBridge {
  ServiciosEstado(): Promise<ServiciosSnapshot>;
  ServiciosAsignar(capsule: string, service: string): Promise<void>;
  ServiciosDesasignar(capsule: string, service: string): Promise<void>;
  ServiciosAprobar(requestID: string): Promise<void>;
  ServiciosDenegar(requestID: string): Promise<void>;
}
/** Read the runtime Wails bindings, preserving receivers. This creates no authority. */
export function serviciosBridge(global: unknown = globalThis): ServiciosBridge | null {
  const app = (global as { go?: { main?: { App?: Record<string, unknown> } } } | null)?.go?.main?.App;
  const names = ['ServiciosEstado', 'ServiciosAsignar', 'ServiciosDesasignar', 'ServiciosAprobar', 'ServiciosDenegar'] as const;
  if (!app || names.some(name => typeof app[name] !== 'function')) return null;
  return Object.fromEntries(names.map(name => [name, (app[name] as (...args: unknown[]) => unknown).bind(app)])) as unknown as ServiciosBridge;
}
export interface ServiciosView {
  snapshot: ServiciosSnapshot | null;
  busy: boolean;
  error: string | null;
}
export interface ServiciosStore extends Readable<ServiciosView> {
  refresh(): Promise<void>;
  toggle(capsule: string, service: string, assigned: boolean): Promise<void>;
  approve(requestID: string): Promise<void>;
  deny(requestID: string): Promise<void>;
}
/** Serialize actions and show only backend observations. Never optimistically grant access. */
export function crearServiciosStore(bridge: ServiciosBridge | null = serviciosBridge(), initial: ServiciosSnapshot | null = null): ServiciosStore {
  const state = writable<ServiciosView>({ snapshot: initial, busy: false, error: null });
  async function perform(action?: (bridge: ServiciosBridge) => Promise<void>): Promise<void> {
    if (get(state).busy) return;
    state.update(s => ({ ...s, busy: true, error: null }));
    try {
      if (!bridge) throw new Error('AUSENTE: servicios bridge');
      if (action) await action(bridge);
      const snapshot = await bridge.ServiciosEstado();
      state.set({ snapshot, busy: true, error: null });
    } catch (error: unknown) {
      state.update(s => ({ ...s, error: error instanceof Error ? error.message : String(error) }));
    } finally {
      state.update(s => ({ ...s, busy: false }));
    }
  }
  return {
    subscribe: state.subscribe,
    refresh: () => perform(),
    toggle: (capsule, service, assigned) => perform(b => assigned ? b.ServiciosAsignar(capsule, service) : b.ServiciosDesasignar(capsule, service)),
    approve: id => perform(b => b.ServiciosAprobar(id)),
    deny: id => perform(b => b.ServiciosDenegar(id)),
  };
}
