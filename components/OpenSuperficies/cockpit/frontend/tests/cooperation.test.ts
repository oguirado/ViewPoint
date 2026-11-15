import { describe, expect, it } from 'vitest';
import { render } from 'svelte/server';
import { readFileSync } from 'node:fs';
import CapsuleCard from '../src/lib/cards/CapsuleCard.svelte';
import type { SurfaceCapsule, Geometry } from '../src/lib/types/hypervisor';
import CapsuleBar, { alPointerDown, alTecla, copyCycle } from '../src/lib/aiops/CapsuleBar.svelte';

const read = (p: string) => readFileSync(new URL(p, import.meta.url), 'utf8');
describe('cooperation verbs', () => {
 it('uses one ordered, uniquely named accessible bar per slot', () => {
  const html = [1,2,3,4].map(n => render(CapsuleBar, {props:{slot_id:`srf-ng-${n}`,mode:'operator_controlled',window_mode:'tile_1_6'}}).body).join('');
  const labels = [...html.matchAll(/aria-label="([^"]+)"/g)].map(m=>m[1]);
  expect(new Set(labels).size).toBe(labels.length);
  for(const n of [1,2,3,4]) {
   const names = ['REC','Snapshot','Copiar slot','Mode','Pin','Split','Fold','Pop out','Close'].map(s=>html.indexOf(`aria-label="${s} srf-ng-${n}"`));
   expect(names.every(x=>x>=0)).toBe(true);expect(names).toEqual([...names].sort((a,b)=>a-b));
  }
  expect(html).toContain('RAM store unavailable');expect(html).toContain('aria-disabled="true"');
  expect(html).not.toMatch(/\sdisabled(?:=|\s|>)/);
 });
 it('cycles copy with bounded repeat and hold', () => {
  expect(copyCycle(null,0,0)).toBe('uri');
  expect(copyCycle(0,9999,0)).toBe('bloque');
  expect(copyCycle(0,10001,0)).toBe('uri');
  expect(copyCycle(null,0,500)).toBe('bloque');
  expect(copyCycle(100,99,0)).toBe('uri');
 });
 it('never silently takes over and Escape cancels', () => {
  expect(alPointerDown('agent_locked',false)).toBe('takeover');
  expect(alPointerDown('read_only',true)).toBe('takeover');
  expect(alPointerDown('operator_controlled',false)).toBe('activate');
  expect(alTecla('Escape','takeover')).toBe(null);
  expect(alTecla('Enter','takeover')).toBe('takeover');
 });
 it('keeps server copy, status ownership and RAM boundary', () => {
  const bar=read('../src/lib/aiops/CapsuleBar.svelte');
  const card=read('../src/lib/cards/CapsuleCard.svelte');
  expect(bar).not.toMatch(/navigator\.clipboard|IniciarGrabacion|role="(?:alert|status)"|aria-live|\bpaste\b/);
  expect(bar).toContain('bus?.begin');expect(bar).toContain('CopiarSlot');expect(bar).toContain('bus?.ingest');
  expect(card).toContain('fijado = true');expect(card).toContain('<CapsuleBar');
  expect(card).not.toContain('role="alert"');
 });
});

it('mounts the guarded bar in the existing capsule chrome, pinned at rest', () => {
 const capsula: SurfaceCapsule = {
  id:'srf-ng-1',name:'terminal',archetype:'terminal',tier:'pty_jaula',mode:'read_only',health:'healthy',slot:1,xpraPort:10021,cdpPort:null,
  telemetry:{cpuPercent:0,memoryBytes:0,memoryLimitBytes:0,networkRxRateBps:0,networkTxRateBps:0},gates:{},
  recording:{recording:false,sessionId:'',targetXid:'',sinceMonoMs:0,offsetMs:null,verdict:''},pbx:null,poppedOut:false,occluded:false,createdAt:''
 };
 const geometria: Geometry = {mode:'tile_1_6',domWidth:1271,domHeight:1064,canvasWidth:1264,canvasHeight:1024,scaled:false,scale:1};
 const html=render(CapsuleCard,{props:{capsula,geometria}}).body;
 expect(html).toContain('Capsule verbs srf-ng-1');
 expect(html).toMatch(/data-cabecera[^>]*data-visible="true"/);
 expect(html).toContain('aria-label="REC srf-ng-1"');
 expect(html).not.toContain('role="alert"');
});
