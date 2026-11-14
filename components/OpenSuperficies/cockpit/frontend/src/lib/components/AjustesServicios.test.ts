import { describe, expect, it, vi } from 'vitest';
import { render } from 'svelte/server';
import { get } from 'svelte/store';
import { readFileSync } from 'node:fs';
import AjustesServicios from './AjustesServicios.svelte';
import { crearServiciosStore, serviciosBridge, type ServiciosBridge, type ServiciosSnapshot } from '../stores/servicios';

const snapshot = (): ServiciosSnapshot => ({services:['chatgpt','perplexity'],capsules:['srf-ng-1','srf-ng-2'],assignments:{'srf-ng-1':['chatgpt'],'srf-ng-2':[]},pending:[{id:'request-a',ai_id:'ai-a',capsule:'srf-ng-2',service:'perplexity',reason:'Research',state:'pending',revision:2}],revision:2});
function fixture() {
 const value = snapshot();
 const bridge: ServiciosBridge = {ServiciosEstado:vi.fn(async()=>value),ServiciosAsignar:vi.fn(async()=>{}),ServiciosDesasignar:vi.fn(async()=>{}),ServiciosAprobar:vi.fn(async()=>{}),ServiciosDenegar:vi.fn(async()=>{})};
 const store=crearServiciosStore(bridge,value);
 return {store,bridge};
}
describe('Servicios settings',()=>{
 it('renders catalog, slot matrix, revision and distinct request actions',()=>{
  const {store}=fixture();const html=render(AjustesServicios,{props:{store}}).body;
  expect(html).toContain('Revisión 2');expect(html).toContain('Research');
  for(const id of ['srf-ng-1','srf-ng-2']) for(const service of ['chatgpt','perplexity']) expect(html).toContain(`aria-label="Asignar ${service} ${id}"`);
  expect(html).toContain('aria-label="Aprobar perplexity srf-ng-2 request-a"');expect(html).toContain('aria-label="Denegar perplexity srf-ng-2 request-a"');
  const names=[...html.matchAll(/aria-label="([^"]+)"/g)].map(m=>m[1]);expect(new Set(names).size).toBe(names.length);
  expect(html).toMatch(/type="checkbox"[^>]*checked/);
 });
 it('toggles assignment and removal through the corresponding binding, then reads back',async()=>{
  const {store,bridge}=fixture();await store.toggle('srf-ng-2','chatgpt',true);await store.toggle('srf-ng-1','chatgpt',false);
  expect(bridge.ServiciosAsignar).toHaveBeenCalledWith('srf-ng-2','chatgpt');expect(bridge.ServiciosDesasignar).toHaveBeenCalledWith('srf-ng-1','chatgpt');expect(bridge.ServiciosEstado).toHaveBeenCalledTimes(2);
 });
 it('approves and denies the exact request ID',async()=>{
  const {store,bridge}=fixture();await store.approve('request-a');await store.deny('request-b');
  expect(bridge.ServiciosAprobar).toHaveBeenCalledWith('request-a');expect(bridge.ServiciosDenegar).toHaveBeenCalledWith('request-b');
 });
 it('surfaces backend refusal without changing assignments',async()=>{
  const {store,bridge}=fixture();vi.mocked(bridge.ServiciosAsignar).mockRejectedValueOnce(new Error('REFUSED: OPERATOR-AUTHORITY'));
  await store.toggle('srf-ng-2','chatgpt',true);
  expect(get(store).snapshot?.assignments['srf-ng-2']).toEqual([]);
  const html=render(AjustesServicios,{props:{store}}).body;expect(html).toContain('role="alert"');expect(html).toContain('REFUSED: OPERATOR-AUTHORITY');expect(get(store).busy).toBe(false);
 });
 it('shows absent authority and missing data without inventing revision zero',async()=>{
  const store=crearServiciosStore(null);await store.refresh();const html=render(AjustesServicios,{props:{store}}).body;
  expect(html).toContain('AUSENTE: servicios bridge');expect(html).not.toContain('Revisión 0');
 });
 it('preserves binding receivers and refuses incomplete bindings',async()=>{
  const app={...fixture().bridge,ServiciosEstado(){expect(this).toBe(app);return Promise.resolve(snapshot());}};
  const bridge=serviciosBridge({go:{main:{App:app}}});expect(bridge).not.toBeNull();await bridge!.ServiciosEstado();
  expect(serviciosBridge({go:{main:{App:{ServiciosEstado:app.ServiciosEstado}}}})).toBeNull();
 });
 it('wires controls to tested controller actions',()=>{
  const source=readFileSync(new URL('./AjustesServicios.svelte',import.meta.url),'utf8');
  expect(source).toContain('store.toggle(capsule, service, input.checked)');expect(source).toContain('store.approve(request.id)');expect(source).toContain('store.deny(request.id)');
  expect(source).toContain('onchange=');expect(source).not.toMatch(/fetch\(|clipboard|localStorage/);
 });
});
