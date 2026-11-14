import { describe, it, expect, vi } from 'vitest';
import { render } from 'svelte/server';
import { readFileSync } from 'node:fs';
import RecordingControls, { recLocation, recDiffCtx, recElapsed, recPrivacy, type RecordingSnapshot } from '../src/lib/aiops/RecordingControls.svelte';
const sample = (extra: Partial<RecordingSnapshot> = {}): RecordingSnapshot => ({fase:'sealed',slot:1,sessionId:'ram-test',recording:false,sinceMonoMs:2000,duracionMs:1000,offsetMs:null,store:'ram',ttl_ms:1800000,dir:'',error:'',...extra});
describe('recording RAM policy', () => {
 it('has unique names and accessible states per slot', () => {
  const html=[1,2].map(n=>render(RecordingControls,{props:{slot_id:`srf-ng-${n}`,recording:sample(),available:false}}).body).join('');
  for(const n of [1,2]) {
   expect(html.match(new RegExp(`aria-label="REC srf-ng-${n}"`,'g'))).toHaveLength(1);
   expect(html).toContain(`id="rec-location-srf-ng-${n}"`);
   expect(html).toContain(`aria-label="Ubicación grabación srf-ng-${n}"`);
  }
  expect(html).toContain('aria-pressed="false"');expect(html).toContain('role="note"');
  expect(html).toContain('AUSENTE: RAM capture');expect(html).toContain(recPrivacy);
  expect(render(RecordingControls,{props:{slot_id:'srf-ng-1',recording:sample({fase:'recording',recording:true})}}).body).toContain('aria-pressed="true"');
 });
 it('counts sealed TTL from t0 while elapsed stays frozen (JRE-02/04)', () => {
  vi.useFakeTimers();vi.setSystemTime(0);
  const rs=sample(); const elapsed=recElapsed(rs);
  expect(recLocation(rs,0)).toBe('RAM · TTL 29:58');
  vi.advanceTimersByTime(10000);
  expect(recLocation(rs,Date.now())).toBe('RAM · TTL 29:48');
  expect(recElapsed(rs)).toBe(elapsed);expect(elapsed).toBe('00:01');
  expect(recLocation(rs,1800000)).toBe('—');vi.useRealTimers();
 });
 it('projects matching slot only and keeps status path-free', () => {
  expect(recLocation(sample({fase:'idle',store:null,ttl_ms:null}),0)).toBe('—');
  expect(recLocation(sample({store:'disk',ttl_ms:null,dir:'chosen'}),0)).toBe('DISK · chosen');
  expect(recLocation(sample({ttl_ms:null}),0)).toBe('—');
  expect(recDiffCtx(sample({fase:'recording',sinceMonoMs:2500}))).toEqual({recElapsedMs:{'srf-ng-1':2500},literals:{}});
  expect(JSON.stringify(recDiffCtx(sample({fase:'error',error:'failed /home/private/file'})))).not.toContain('/home');
  const html=render(RecordingControls,{props:{slot_id:'srf-ng-2',recording:sample()}}).body;
  expect(html).not.toContain('RAM · TTL');expect(html).toContain('—');
  expect(render(RecordingControls,{props:{slot_id:'srf-ng-1',recording:sample()}}).body).toContain('desfase n/a');
 });
 it('has no save bridge, network, extra live region, clipboard or recording pulse', () => {
  const src=readFileSync(new URL('../src/lib/aiops/RecordingControls.svelte',import.meta.url),'utf8');
  expect(src).not.toMatch(/data-pulse|role="status"|aria-live|navigator\.clipboard|\bfetch\s*\(|Guardar|Save|Exportar|Disco/);
  expect(src).toContain('bus?.begin');expect(src).toContain('recDiffCtx');
 });
});
