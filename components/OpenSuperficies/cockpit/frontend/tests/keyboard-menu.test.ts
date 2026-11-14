import { readFileSync } from 'node:fs';
import { compile } from 'svelte/compiler';
import { render } from 'svelte/server';
import { describe, expect, it } from 'vitest';
import KeyboardMenu, { esDuenoDeTexto, resolverAtajo, escaleraEscape, transicionMenu,
  rovingTecla, revelarPorTeclado, type TeclaK, type MenuItem, type MenuState,
} from '../src/lib/aiops/KeyboardMenu.svelte';
const source = readFileSync(new URL('../src/lib/aiops/KeyboardMenu.svelte', import.meta.url), 'utf8');
const key = (key: string, extra: Partial<TeclaK> = {}): TeclaK =>
  ({key, altKey: false, ctrlKey: false, shiftKey: false, metaKey: false, ...extra});
const items: MenuItem[] = [
  {id: 'blocked', label: 'Unavailable', kind: 'item', disabled: true, reason: 'Lease required'},
  {id: 'browser', label: 'Launch Browser workload', kind: 'item'},
  {id: 'sep', label: '', kind: 'separator'},
  {id: 'desktop', label: 'Launch Desktop workload', kind: 'item'},
];
const initial = (): MenuState => ({items, open: false, index: -1, search: '', searchAt: 0,
  focus: null, selectedId: null, refusedId: null});
const press = (state: MenuState, key: string, time = 1000) => transicionMenu(state, {type: 'key', key, time});

describe('KEYS shortcut ownership and takeover', () => {
  it('shortcut_steals_xterm_alt_digit / structural text owners', () => {
    for (const target of [{tagName: 'INPUT'}, {tagName: 'textarea'}, {tagName: 'select'},
      {isContentEditable: true}, {closest: () => ({})}]) {
      expect(esDuenoDeTexto(target)).toBe(true);
      expect(resolverAtajo(key('1', {altKey: true}), {target})).toBeNull();
      expect(resolverAtajo(key('PageDown', {ctrlKey: true}), {target})).toBeNull();
      expect(resolverAtajo(key('`', {ctrlKey: true}), {target})).toEqual({kind: 'toggle_hub'});
    }
    expect(esDuenoDeTexto(null)).toBe(false);
    expect(esDuenoDeTexto({tagName: 'DIV'})).toBe(false);
    for (const selector of ['.xterm', '[data-terminal-host]', '[contenteditable]']) {
      const target = {closest: (query: string) => query.split(', ').includes(selector) ? {} : null};
      expect(resolverAtajo(key('1', {altKey: true}), {target})).toBeNull();
    }
  });
  it('alt_shift_digit_code / chord_collision', () => {
    for (let n = 1; n <= 9; n++) {
      expect(resolverAtajo(key(String(n), {altKey: true}), {})).toEqual({kind: 'focus_slot', index: n - 1});
      expect(resolverAtajo(key('!', {altKey: true, shiftKey: true, code: `Digit${n}`}), {}))
        .toEqual({kind: 'focus_workspace', index: n - 1});
    }
    expect(resolverAtajo(key('1', {altKey: true, shiftKey: true}), {})).toBeNull();
    expect(resolverAtajo(key('PageUp', {ctrlKey: true}), {})).toEqual({kind: 'cycle_slot', direction: -1});
    expect(resolverAtajo(key('PageDown', {ctrlKey: true}), {})).toEqual({kind: 'cycle_slot', direction: 1});
  });
  it('ctrl_arrow_global / paste_binding / no destructive shortcuts', () => {
    for (const k of ['ArrowLeft', 'ArrowRight', 'n', 'v', 'c', 'Insert', 'Delete', 'Escape'])
      expect(resolverAtajo(key(k, {ctrlKey: true}), {})).toBeNull();
    expect(resolverAtajo(key('V', {ctrlKey: true, shiftKey: true}), {})).toBeNull();
    expect(resolverAtajo(key('Insert', {shiftKey: true}), {})).toBeNull();
    expect(resolverAtajo(key('1', {altKey: true, metaKey: true}), {})).toBeNull();
  });
  it('chord_repeat_oscillation', () => {
    for (const ev of [key('1', {altKey: true}), key('!', {altKey: true, shiftKey: true, code: 'Digit1'}),
      key('`', {ctrlKey: true}), key('PageUp', {ctrlKey: true})])
      expect(resolverAtajo({...ev, repeat: true}, {})).toBeNull();
  });
  it('agent_locked_key_silent_takeover / IA read_only', () => {
    for (const mode of ['agent_locked', 'read_only']) {
      const ctx = {viewportSlotId: 'srf-ng-2', slotMode: () => mode, iaSelected: () => true};
      expect(resolverAtajo(key('x'), ctx)).toEqual({kind: 'prompt_takeover', slot_id: 'srf-ng-2'});
      expect(resolverAtajo(key('1', {altKey: true}), ctx)).toEqual({kind: 'prompt_takeover', slot_id: 'srf-ng-2'});
      expect(resolverAtajo(key('Tab'), ctx)).toBeNull();
      expect(resolverAtajo(key('Tab', {shiftKey: true}), ctx)).toBeNull();
      expect(resolverAtajo(key('`', {ctrlKey: true}), ctx)).toEqual({kind: 'toggle_hub'});
    }
    expect(resolverAtajo(key('x'), {viewportSlotId: 'srf-ng-2', slotMode: () => 'read_only'})).toBeNull();
  });
});

describe('KEYS Escape, reveal and composites', () => {
  it('escape_confirms_dialog / escape_in_dialog_textbox_ignored', () => {
    expect(escaleraEscape({menuOpen: true, dialogOpen: true, hubOpen: true, inHubChrome: true})).toEqual({kind: 'close_menu'});
    expect(escaleraEscape({dialogOpen: true, inDialog: true, target: {tagName: 'INPUT'}})).toEqual({kind: 'cancel_dialog'});
    expect(escaleraEscape({dialogOpen: true, target: {tagName: 'INPUT'}})).toBeNull();
    expect(escaleraEscape({dialogOpen: true, target: {tagName: 'INPUT',
      closest: (selector: string) => selector === '[role="alertdialog"]' ? {} : null}})).toEqual({kind: 'cancel_dialog'});
    expect(escaleraEscape({hubOpen: true, inHubChrome: true})).toEqual({kind: 'close_hub'});
    expect(escaleraEscape({hubOpen: true, inHubChrome: true, target: {tagName: 'TEXTAREA'}})).toBeNull();
    expect(escaleraEscape({})).toBeNull();
  });
  it('header_autohide_while_focused / dialog helper stays true', () => {
    const base = {focusWithin: false, menuOpen: false, dialogOpen: false, pinned: false};
    expect(revelarPorTeclado(base)).toBe(false);
    for (const field of Object.keys(base)) expect(revelarPorTeclado({...base, [field]: true})).toBe(true);
  });
  it('roving arrows wrap and Home/End; unrelated keys and empty composites do nothing', () => {
    expect(rovingTecla('ArrowLeft', 0, 3)).toBe(2);
    expect(rovingTecla('ArrowDown', 2, 3)).toBe(0);
    expect(rovingTecla('Home', 2, 3)).toBe(0);
    expect(rovingTecla('End', 0, 3)).toBe(2);
    expect(rovingTecla('Enter', 0, 3)).toBeNull();
    expect(rovingTecla('Home', 0, 0)).toBeNull();
  });
});

describe('KEYS menu reducer', () => {
  it('hover_only_open / no_escape_close / no_focus_return', () => {
    for (const k of ['Enter', ' ', 'ArrowDown']) {
      const s = press(initial(), k);
      expect([s.open, s.index, s.focus]).toEqual([true, 1, 'item']);
      expect([press(s, 'Escape').open, press(s, 'Escape').focus]).toEqual([false, 'trigger']);
    }
    expect(press(initial(), 'ArrowUp').index).toBe(3);
    expect(press(initial(), 'a').open).toBe(false);
  });
  it('disabled_hidden / separators skipped / typeahead / wrap', () => {
    let s = press(press(initial(), 'ArrowDown'), 'ArrowUp');
    expect(s.index).toBe(0);
    s = press(s, 'ArrowUp'); expect(s.index).toBe(3);
    s = press(s, 'Home'); expect(s.index).toBe(0);
    s = press(s, 'End'); expect(s.index).toBe(3);
    s = press(s, 'u'); expect(s.index).toBe(0);
    s = press(s, 'l', 2000); expect(s.index).toBe(1);
    s = press(s, 'l', 2100); expect(s.index).toBe(3);
  });
  it('disabled_activate_no_status_hook / Tab never selects / repeats never activate', () => {
    const s = press(press(press(initial(), 'ArrowDown'), 'Home'), 'Enter');
    expect([s.selectedId, s.refusedId, s.open, s.focus]).toEqual([null, 'blocked', false, 'trigger']);
    expect(press(press(initial(), 'ArrowDown'), 'Enter').selectedId).toBe('browser');
    expect(press(press(initial(), 'ArrowDown'), 'Tab').selectedId).toBeNull();
    expect(press(press(initial(), 'ArrowDown'), 'Tab').focus).toBeNull();
    expect(transicionMenu(press(initial(), 'Enter'), {type: 'key', key: 'Enter', repeat: true}).selectedId).toBeNull();
    expect(transicionMenu(press(initial(), 'Enter'), {type: 'outside'}).open).toBe(false);
    expect(transicionMenu(initial(), {type: 'activate', index: 2}).selectedId).toBeNull();
  });
  it('empty and entirely disabled menus cannot activate an absent item', () => {
    const empty = {...initial(), items: []};
    expect(press(empty, 'Enter').index).toBe(-1);
    expect(press(press(empty, 'Enter'), 'Enter').selectedId).toBeNull();
    expect(press({...initial(), items: [items[0]]}, 'Enter').index).toBe(0);
  });
});

describe('KEYS node_headless_ssr_and_reducer', () => {
  it('menu_items_plain_buttons / positive_tabindex / submenu_present / DOM dependency', () => {
    const {body} = render(KeyboardMenu, {props: {id: 'workload', label: 'Workload menu', items, onSelect: () => {}}});
    for (const s of ['aria-haspopup="menu"', 'aria-expanded="false"', 'aria-controls="workload-menu"',
      'aria-labelledby="workload-trigger"', 'role="menuitem"', 'role="separator"',
      'aria-description="Lease required"', 'aria-disabled="true"', 'aria-label="Unavailable"']) expect(body).toContain(s);
    expect(body).not.toMatch(/tabindex="[1-9]/);
    expect(body.match(/role="menu"/g)).toHaveLength(1);
    expect(compile(source, {generate: 'client', filename: 'KeyboardMenu.svelte'}).warnings).toEqual([]);
  });
  it('layout_name_drift / layout_2x2_hidden_in_tile / disabled_mutates_accessible_name', () => {
    const layouts: MenuItem[] = ['1x1', '1x2', '2x2'].map(l => ({id: l, label: `Set layout ${l} ws 2`,
      kind: 'radio', checked: l === '1x1', disabled: l === '2x2', reason: l === '2x2' ? 'needs popped-out window' : undefined}));
    const {body} = render(KeyboardMenu, {props: {id: 'layout-2', label: 'Layout ws 2', items: layouts, onSelect: () => {}}});
    expect(body).toContain('aria-label="Layout ws 2"');
    for (const l of ['1x1', '1x2', '2x2']) expect(body).toContain(`aria-label="Set layout ${l} ws 2"`);
    expect(body.match(/role="menuitemradio"/g)).toHaveLength(3);
    expect(body).toContain('aria-checked="true"');
    expect(body).toContain('title="needs popped-out window"');
  });
  it('slot names, no I/O or live regions; rejects disk save ids', () => {
    const {body} = render(KeyboardMenu, {props: {id: 'slot-2', label: 'Actions', slotId: 'srf-ng-2', items, onSelect: () => {}}});
    expect(body).toContain('aria-label="Actions srf-ng-2"');
    expect(body).toContain('aria-label="Launch Browser workload srf-ng-2"');
    expect(source).not.toMatch(/from\s+['"][^'"]*(?:wails|srfd)|role="(?:alert|status)"|aria-live|onpaste|onpointerenter|:hover/);
    expect(source).toContain('onDisabledSelect?.');
    expect(source).toContain('onpointerdown={outside}');
    expect(source).toContain('2px solid var(--border-focus)');
    for (const id of ['save-to-disk', 'guardar-en-disco'])
      expect(() => render(KeyboardMenu, {props: {id: 'bad', label: 'Bad', items: [{id, label: 'Save', kind: 'item'}], onSelect: () => {}}}).body).toThrow();
  });
});
