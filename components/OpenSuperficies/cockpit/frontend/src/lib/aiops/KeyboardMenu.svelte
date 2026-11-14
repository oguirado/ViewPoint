<script module lang="ts">
  export interface TeclaK {
    altKey: boolean;
    ctrlKey: boolean;
    metaKey: boolean;
    shiftKey: boolean;
    key: string;
    code?: string;
    repeat?: boolean;
  }
  export interface MenuItem {
    id: string;
    label: string;
    kind: 'item' | 'radio' | 'separator';
    checked?: boolean;
    disabled?: boolean;
    reason?: string;
  }
  export type Atajo =
    | {kind: 'focus_slot'; index: number}
    | {kind: 'focus_workspace'; index: number}
    | {kind: 'cycle_slot'; direction: -1 | 1}
    | {kind: 'toggle_hub'}
    | {kind: 'prompt_takeover'; slot_id: string};
  export interface ContextoAtajo {
    target?: unknown;
    /** Set by WIRE only when the event target is this slot's outer viewport. */
    viewportSlotId?: string | null;
    slotMode?: (slot: string) => string | undefined;
    /** VERBS' explicit IA selection, including the read_only phase before a lease. */
    iaSelected?: (slot: string) => boolean;
  }
  interface TextTarget {
    closest?: (selector: string) => unknown;
    tagName?: string;
    isContentEditable?: boolean;
  }
  function structuralTarget(target: unknown): TextTarget | null {
    return typeof target === 'object' && target !== null ? target as TextTarget : null;
  }
  export function esDuenoDeTexto(target: unknown): boolean {
    const t = structuralTarget(target);
    if (!t) return false;
    return t.isContentEditable === true || /^(input|textarea|select)$/i.test(t.tagName ?? '') ||
      (typeof t.closest === 'function' && !!t.closest('.xterm, [data-terminal-host], input, textarea, select, [contenteditable]'));
  }
  export function resolverAtajo(ev: TeclaK, ctx: ContextoAtajo): Atajo | null {
    if (ev.repeat) return null;
    if (ev.ctrlKey && !ev.altKey && !ev.metaKey && !ev.shiftKey && ev.key === '`') return {kind: 'toggle_hub'};
    const slot = ctx.viewportSlotId;
    const mode = slot ? ctx.slotMode?.(slot) : undefined;
    if (slot && (mode === 'agent_locked' || (mode === 'read_only' && ctx.iaSelected?.(slot)))) {
      if (ev.key === 'Tab') return null;
      return {kind: 'prompt_takeover', slot_id: slot};
    }
    if (esDuenoDeTexto(ctx.target) || ev.metaKey) return null;
    if (ev.altKey && !ev.ctrlKey) {
      if (ev.shiftKey) {
        const match = /^Digit([1-9])$/.exec(ev.code ?? '');
        return match ? {kind: 'focus_workspace', index: Number(match[1]) - 1} : null;
      }
      if (/^[1-9]$/.test(ev.key)) return {kind: 'focus_slot', index: Number(ev.key) - 1};
    }
    if (ev.ctrlKey && !ev.altKey && !ev.shiftKey) {
      if (ev.key === 'PageUp') return {kind: 'cycle_slot', direction: -1};
      if (ev.key === 'PageDown') return {kind: 'cycle_slot', direction: 1};
    }
    return null;
  }
  export interface ContextoEscape {
    target?: unknown;
    menuOpen?: boolean;
    dialogOpen?: boolean;
    /** WIRE can supply this for structural/non-DOM event adapters. */
    inDialog?: boolean;
    hubOpen?: boolean;
    inHubChrome?: boolean;
  }
  export function escaleraEscape(ctx: ContextoEscape):
    {kind: 'close_menu' | 'cancel_dialog' | 'close_hub'} | null {
    const target = structuralTarget(ctx.target);
    const inDialog = ctx.inDialog || (typeof target?.closest === 'function' && !!target.closest('[role="alertdialog"]'));
    if (esDuenoDeTexto(ctx.target) && !(ctx.dialogOpen && inDialog)) return null;
    if (ctx.menuOpen) return {kind: 'close_menu'};
    if (ctx.dialogOpen) return {kind: 'cancel_dialog'};
    if (ctx.hubOpen && ctx.inHubChrome) return {kind: 'close_hub'};
    return null;
  }
  export function rovingTecla(key: string, idx: number, len: number): number | null {
    if (!Number.isInteger(len) || len <= 0) return null;
    if (key === 'Home') return 0;
    if (key === 'End') return len - 1;
    if (key === 'ArrowLeft' || key === 'ArrowUp') return (idx - 1 + len) % len;
    if (key === 'ArrowRight' || key === 'ArrowDown') return (idx + 1) % len;
    return null;
  }
  export function revelarPorTeclado(ctx: {
    focusWithin: boolean; menuOpen: boolean; dialogOpen: boolean; pinned: boolean;
  }): boolean {
    return ctx.focusWithin || ctx.menuOpen || ctx.dialogOpen || ctx.pinned;
  }
  export interface MenuState {
    items: readonly MenuItem[];
    open: boolean;
    index: number;
    search: string;
    searchAt: number;
    focus: 'trigger' | 'item' | null;
    selectedId: string | null;
    refusedId: string | null;
  }
  export type MenuEvent =
    | {type: 'key'; key: string; repeat?: boolean; time?: number}
    | {type: 'toggle'}
    | {type: 'outside'}
    | {type: 'activate'; index: number};
  function validateItems(items: readonly MenuItem[]): void {
    const ids = new Set<string>();
    for (const item of items) {
      if (/save.*disk|guardar.*disco/i.test(item.id)) throw new Error('Disk save is outside the menu contract');
      if (!item.id || ids.has(item.id)) throw new Error('Menu item ids must be unique and nonempty');
      ids.add(item.id);
    }
  }
  /** Pure reducer: focus and callback effects are explicit outputs, never I/O. */
  export function transicionMenu(state: MenuState, ev: MenuEvent): MenuState {
    validateItems(state.items);
    const s = {...state, focus: null, selectedId: null, refusedId: null};
    const entries = s.items.map((item, index) => ({item, index})).filter(({item}) => item.kind !== 'separator');
    const close = (focus: 'trigger' | null): MenuState => ({...s, open: false, search: '', focus});
    const open = (last: boolean): MenuState => {
      const enabled = entries.filter(({item}) => !item.disabled);
      const choices = enabled.length ? enabled : entries;
      const index = (last ? choices[choices.length - 1] : choices[0])?.index ?? -1;
      return {...s, open: true, index, search: '', focus: index >= 0 ? 'item' : 'trigger'};
    };
    const activate = (index: number): MenuState => {
      const item = s.items[index];
      if (!s.open || !item || item.kind === 'separator') return s;
      return {...close('trigger'), selectedId: item.disabled ? null : item.id, refusedId: item.disabled ? item.id : null};
    };
    if (ev.type === 'outside') return close(null);
    if (ev.type === 'toggle') return s.open ? close('trigger') : open(false);
    if (ev.type === 'activate') return activate(ev.index);
    if (ev.repeat) return s;
    if (!s.open) {
      if (['Enter', ' ', 'ArrowDown', 'ArrowUp'].includes(ev.key)) return open(ev.key === 'ArrowUp');
      return s;
    }
    if (ev.key === 'Escape') return close('trigger');
    if (ev.key === 'Tab') return close(null);
    if (ev.key === 'Enter' || ev.key === ' ') return activate(s.index);
    if (['ArrowUp', 'ArrowDown', 'Home', 'End'].includes(ev.key)) {
      const pos = entries.findIndex(({index}) => index === s.index);
      const next = rovingTecla(ev.key, Math.max(0, pos), entries.length);
      return next === null ? s : {...s, index: entries[next].index, focus: 'item', search: ''};
    }
    if (ev.key.length === 1) {
      const now = ev.time ?? 0;
      const letter = ev.key.toLocaleLowerCase();
      const search = now - s.searchAt < 700 ? s.search + letter : letter;
      const repeated = [...search].every(c => c === letter);
      const query = repeated ? letter : search;
      const pos = entries.findIndex(({index}) => index === s.index);
      // Continue multi-character matches from the current entry; single letters cycle.
      const start = query.length > 1 ? Math.max(0, pos) : pos + 1;
      for (let step = 0; step < entries.length; step++) {
        const candidate = entries[(start + step) % entries.length];
        if (candidate.item.label.toLocaleLowerCase().startsWith(query))
          return {...s, index: candidate.index, search, searchAt: now, focus: 'item'};
      }
      return {...s, search, searchAt: now};
    }
    return s;
  }
</script>

<script lang="ts">
  import { tick } from 'svelte';
  interface Props {
    id: string;
    label: string;
    slotId?: string;
    items: readonly MenuItem[];
    onSelect: (id: string) => void;
    onDisabledSelect?: (id: string, reason: string) => void;
  }
  let {id, label, slotId, items, onSelect, onDisabledSelect}: Props = $props();
  const checkedItems = $derived.by(() => { validateItems(items); return items; });
  let state = $state<MenuState>({items: [], open: false, index: -1, search: '', searchAt: 0,
    focus: null, selectedId: null, refusedId: null});
  let root: HTMLDivElement | undefined;
  let trigger: HTMLButtonElement | undefined;
  let buttons: Record<string, HTMLButtonElement | undefined> = {};
  const name = (text: string): string => slotId && !text.endsWith(` ${slotId}`) ? `${text} ${slotId}` : text;
  async function dispatch(ev: MenuEvent): Promise<void> {
    const next = transicionMenu({...state, items: checkedItems}, ev);
    state = next;
    // Restore focus before notifying consumers which may unmount this menu.
    await tick();
    if (next.focus === 'trigger') trigger?.focus();
    if (next.focus === 'item') {
      const item = checkedItems[next.index];
      if (item) buttons[item.id]?.focus();
    }
    if (next.refusedId !== null) {
      const item = checkedItems.find(item => item.id === next.refusedId);
      onDisabledSelect?.(next.refusedId, item?.reason ?? 'Unavailable');
    }
    if (next.selectedId !== null) onSelect(next.selectedId);
  }
  function keyboard(event: KeyboardEvent): void {
    if (event.altKey || event.ctrlKey || event.metaKey) return;
    const handled = ['Enter', ' ', 'ArrowDown', 'ArrowUp'].includes(event.key) ||
      (state.open && (['Escape', 'Tab', 'Home', 'End'].includes(event.key) || event.key.length === 1));
    if (!handled) return;
    // Tab must retain native focus traversal. All other menu keys are contained here.
    if (event.key !== 'Tab') event.preventDefault();
    event.stopPropagation();
    void dispatch({type: 'key', key: event.key, repeat: event.repeat, time: event.timeStamp});
  }
  function outside(event: PointerEvent): void {
    if (state.open && event.target instanceof Node && !root?.contains(event.target)) void dispatch({type: 'outside'});
  }
  function focusout(event: FocusEvent): void {
    if (state.open && event.relatedTarget instanceof Node && !root?.contains(event.relatedTarget))
      void dispatch({type: 'outside'});
  }
</script>

<svelte:window onpointerdown={outside} />
<div class="keyboard-menu" bind:this={root} onfocusout={focusout}>
  <button type="button" id={`${id}-trigger`} bind:this={trigger}
    aria-label={name(label)} aria-haspopup="menu" aria-expanded={state.open} aria-controls={`${id}-menu`}
    onkeydown={keyboard} onclick={() => dispatch({type: 'toggle'})}>{label}</button>
  <div id={`${id}-menu`} role="menu" aria-labelledby={`${id}-trigger`} hidden={!state.open}>
    {#each checkedItems as item, index (item.id)}
      {#if item.kind === 'separator'}
        <div role="separator" class="menu-separator"></div>
      {:else}
        <button type="button" bind:this={buttons[item.id]}
          role={item.kind === 'radio' ? 'menuitemradio' : 'menuitem'}
          aria-label={name(item.label)} aria-checked={item.kind === 'radio' ? !!item.checked : undefined}
          aria-disabled={!!item.disabled} aria-description={item.disabled ? item.reason ?? 'Unavailable' : undefined}
          title={item.disabled ? item.reason ?? 'Unavailable' : undefined}
          tabindex={state.open && state.index === index ? 0 : -1}
          onfocus={() => { state = {...state, index}; }}
          onkeydown={keyboard} onclick={() => dispatch({type: 'activate', index})}>
          {#if item.kind === 'radio'}<span aria-hidden="true">{item.checked ? '●' : '○'}</span>{/if}
          {item.label}
        </button>
      {/if}
    {/each}
  </div>
</div>

<style>
  .keyboard-menu { position: relative; display: inline-block; font-size: 13px; --border-focus: var(--accent-cyan); }
  button { font: inherit; min-height: 28px; padding: 4px 8px; border: 1px solid var(--border-subtle);
    background: var(--surface-cockpit); color: var(--text-polar); cursor: pointer; }
  button:focus-visible { outline: 2px solid var(--border-focus); outline-offset: 2px; }
  [role='menu'] { position: absolute; z-index: 30; inset-block-start: 100%; inset-inline-start: 0;
    min-width: max-content; padding: 4px; background: var(--surface-cockpit); border: 1px solid var(--border-subtle); }
  [role='menu'][hidden] { display: none; }
  [role='menu'] button { display: flex; gap: 8px; width: 100%; text-align: start; border-color: transparent; }
  [aria-disabled='true'] { cursor: not-allowed; }
  .menu-separator { height: 1px; margin: 4px 0; background: var(--border-subtle); }
</style>
