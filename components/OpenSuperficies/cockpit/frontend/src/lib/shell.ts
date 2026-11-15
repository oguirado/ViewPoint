// The master shell's pure rules (App.svelte): single master cockpit view, container slot cycling hotkeys,
// and the drawer hotkey. No DOM here, so tests/shell.test.ts pins them directly.

export type VistaMaestra = 'cockpit';

/** srfd estado and listar are polled on this period (section 7.2 of VIEW-01, INV BACKEND-SRFD-PIPELINE). */
export const REFRESCO_MS = 5000;

/** In single-view cockpit mode, the initial view is always 'cockpit', regardless of query parameters. */
export function vistaInicial(_search?: string): VistaMaestra {
  return 'cockpit';
}

export type Tecla = Pick<KeyboardEvent, 'altKey' | 'ctrlKey' | 'metaKey' | 'shiftKey' | 'key'>;

/** Alt+1..9 jumps directly to container slot index 0..8. Returns 0-based slot index or null. */
export function saltoDeRanura(ev: Tecla): number | null {
  if (!ev.altKey || ev.ctrlKey || ev.metaKey || ev.shiftKey) return null;
  if (/^[1-9]$/.test(ev.key)) {
    return parseInt(ev.key, 10) - 1;
  }
  return null;
}

/** Ctrl+PageUp / Ctrl+ArrowLeft cycles to previous slot (-1), Ctrl+PageDown / Ctrl+ArrowRight cycles to next slot (+1). */
export function cicloDeRanura(ev: Tecla): -1 | 1 | null {
  if (!ev.ctrlKey || ev.altKey || ev.metaKey || ev.shiftKey) return null;
  if (ev.key === 'PageUp' || ev.key === 'ArrowLeft') return -1;
  if (ev.key === 'PageDown' || ev.key === 'ArrowRight') return 1;
  return null;
}

/** Ctrl+` toggles the agents drawer (VIEW-03) from any state. */
export function esAtajoCajon(ev: Tecla): boolean {
  return ev.ctrlKey && !ev.altKey && !ev.metaKey && ev.key === '`';
}
