import { readFileSync } from 'node:fs';
import { spawnSync } from 'node:child_process';
import { compile, parse } from 'svelte/compiler';
import ts from 'typescript';
import { expect, it } from 'vitest';
import * as hub from '../src/lib/terminal/CajonAgentes.svelte';
import { rovingTecla } from '../src/lib/aiops/KeyboardMenu.svelte';
import type { FaseDeFlujo } from '../src/lib/terminal/wsstream';
// Reuse the unmodified legacy assertions; only this lane's test entry is run.
import './view03.test';

const source = readFileSync(new URL('../src/lib/terminal/CajonAgentes.svelte', import.meta.url), 'utf8');
const clean = (s: string): string => s.replace(/<!--[\s\S]*?-->|\/\*[\s\S]*?\*\//g, '').replace(/\/\/.*$/gm, '');
function issues(s: string): string[] {
 const c = clean(s), out: string[] = [];
 const need = (name: string, re: RegExp): void => { if (!re.test(c)) out.push(name); };
 const reject = (name: string, re: RegExp): void => { if (re.test(c)) out.push(name); };
 need('left_overlay_css', /data-lado=\{lado\}/);
 need('left_overlay_css', /\[data-lado='left'\]\s*\{[^}]*right:\s*auto[^}]*height:\s*100% !important[^}]*width:\s*var\(--hub-w\)/);
 need('left_closed_tile_not_full_height', /\[data-lado='left'\]\[data-estado='plegado'\]\s*\{\s*--hub-w:\s*12px/);
 need('left_closed_tile_not_full_height', /\[data-lado='left'\] \.asa\s*\{[^}]*width:\s*100%[^}]*height:\s*100%/);
 need('left_layout_no_flex', /\[data-lado='left'\]\s*\{[^}]*display:\s*flex[^}]*flex-direction:\s*column/);
 need('left_layout_fits_280px', /--hub-w:\s*280px/);
 need('left_layout_fits_280px', /\[data-lado='left'\] \.cabecera\s*\{[^}]*height:\s*auto !important[^}]*flex-wrap:\s*wrap/);
 need('left_layout_fits_280px', /\[data-lado='left'\] \[data-valle\]\s*\{[^}]*flex:\s*1[^}]*height:\s*auto !important/);
 need('left_layout_fits_280px', /\[data-lado='left'\] \.linea\s*\{[^}]*flex-direction:\s*column[^}]*height:\s*auto !important[^}]*align-items:\s*stretch/);
 reject('remount_on_toggle', /\{#if[^}]*(?:abierto|activo|lado)/);
 reject('second_role_status_in_hub', /role="status"/);
 reject('b3_state_in_alert', /role="alert"|aria-live/);
 reject('font_below_12px_in_hub', /(?:font-size:\s*|font:\s*\d+\s+)(?:[0-9]|1[01])px/);
 reject('opacity_dim_state', /opacity:\s*0\./);
 reject('stale_fallback_below_aaa', /#7f8da3/i);
 reject('paste_handler_in_hub', /onpaste|clipboardData|navigator\.clipboard|execCommand/i);
 need('t_short_label_missing', /aria-label="T\{i \+ 1\} \{a.titulo\} \{a.superficie\}"/);
 need('t_short_label_missing', /class="rotulo">T\{i \+ 1\} \{a.titulo\}/);
 need('respawn_unnamed', /aria-label=\{agenteActivo \? `Respawn T/);
 for (const name of ['Launch srf shell', 'Launch Claude Code', 'Launch Gemini CLI']) {
  if (s.split(`aria-label="${name}"`).length !== 2) out.push('duplicate_pill_names');
 }
 if ((s.match(/>\[claude\]<\/button>/g) ?? []).length !== 1) out.push('duplicate_pill_names');
 need('dangling_aria_controls', /id="agent-hub"/);
 if ((s.match(/aria-controls="agent-hub"/g) ?? []).length !== 2) out.push('dangling_aria_controls');
 need('focus_before_tick', /await tick\(\);\s*asa\?\.focus\(\)/);
 need('timer_ticks_when_collapsed', /\$effect\(\(\) => \{\s*if \(!abierto\) return;\s*const id = window.setInterval/);
 need('duplicate_fase_emit', /const anunciarCambio = debeAnunciar\(fases\[id\], f\);\s*fases\[id\] = f/);
 reject('abort_in_hub', /EMERGENCY ABORT|emergency-abort/);
 for (const m of c.matchAll(/([^{}]+)\{[^{}]*display:\s*none[^{}]*\}/g)) {
  if (/panel|anfitrion|cuerpo|valle|xterm|cajon\b/.test(m[1])) out.push('display_none_on_cajon_selector');
 }
 return out;
}

it('implements the complete hub contract, including ca_sub_floor_count_zero', () => {
 expect(issues(source)).toEqual([]);
 expect(() => parse(source, {modern: true})).not.toThrow();
 expect(() => compile(source, {generate: 'server', filename: 'CajonAgentes.svelte'})).not.toThrow();
 expect(source).toContain("lado = 'bottom'");
 expect(source).toContain('abierto = $bindable(true)');
 expect(source).toContain('visible={abierto && a.id === activoEfectivo}');
 expect(source).toMatch(/<header[^>]*data-hub-chrome/);
 expect(source).toMatch(/<footer[^>]*data-hub-chrome/);
 expect(source).not.toMatch(/<div[^>]*data-valle[^>]*data-hub-chrome/);
 expect(source).toContain('rovingTecla(ev.key, actual, agentes.length)');
 const app = readFileSync(new URL('../src/App.svelte', import.meta.url), 'utf8');
 expect(app).toMatch(/cajonAbierto = \$state\(false\)/);
});

const mutants: [string, (s:string)=>string][] = [
 ['left_open_shrinks_framebuffer', s=>s.replace('right: auto;', 'right: 0;')],
 ['remount_on_toggle', s=>s.replace('<TerminalPanel', '{#if abierto}<TerminalPanel')],
 ['second_role_status_in_hub', s=>s.replace('data-aviso-fase', 'role="status" data-aviso-fase')],
 ['b3_state_in_alert', s=>s.replace('data-aviso-fase', 'role="alert" data-aviso-fase')],
 ['font_below_12px_in_hub', s=>s.replace('var(--text-mono-data, 12px)', '11px')],
 ['opacity_dim_state', s=>s.replace('border-style: dashed;', 'opacity: 0.4;')],
 ['stale_fallback_below_aaa', s=>s.replace('#a5b4ce', '#7f8da3')],
 ['paste_handler_in_hub', s=>s.replace('data-directiva', 'onpaste={enviarDirectiva} data-directiva')],
 ['t_short_label_missing', s=>s.replace('class="rotulo">T{i + 1}', 'class="rotulo">')],
 ['respawn_unnamed', s=>s.replace('aria-label={agenteActivo ? `Respawn T', 'data-label={agenteActivo ? `Respawn T')],
 ['duplicate_pill_names', s=>s.replace('Launch Gemini CLI', 'Launch Claude Code')],
 ['dangling_aria_controls', s=>s.replace('id="agent-hub"', 'id="missing-hub"')],
 ['focus_before_tick', s=>s.replace('await tick();\n    asa?.focus();', 'asa?.focus();\n    await tick();')],
 ['timer_ticks_when_collapsed', s=>s.replace('if (!abierto) return;', '')],
 ['duplicate_fase_emit', s=>s.replace('const anunciarCambio = debeAnunciar(fases[id], f);', 'const anunciarCambio = true;')],
 ['left_layout_no_flex', s=>s.replace('display: flex;\n    flex-direction: column;\n    top:', 'display: block;\n    flex-direction: column;\n    top:')],
 ['display_none_on_cajon_selector', s=>s + '\n<style>.cajon { display: none; }</style>'],
 ['abort_in_hub', s=>s.replace('data-mas', 'data-emergency-abort data-mas')],
 ['left_closed_tile_not_full_height', s=>s.replace('.asa { width: 100%; height: 100%; }', '.asa { width: 100%; height: 12px; }')],
];
it.each(mutants)('rejects negative fixture %s', (_name, mutate) => {
 const changed = mutate(source);
 expect(changed).not.toBe(source);
 expect(issues(changed).length).toBeGreaterThan(0);
});

function extractFunction(name: string): string {
 const ast = parse(source, {modern: true});
 const fn = ast.instance!.content.body.find(n => n.type === 'FunctionDeclaration' && n.id?.name === name);
 if (!fn) throw new Error(`missing function ${name}`);
 const positioned = fn as unknown as {start: number; end: number};
 return ts.transpile(source.slice(positioned.start, positioned.end), {target: ts.ScriptTarget.ES2022});
}
it('deduplicates actual phase callbacks, supplies nonempty literals and contains bus failures', () => {
 const h = hub as unknown as {debeAnunciar:(prev:FaseDeFlujo|undefined,f:FaseDeFlujo)=>boolean;literalPty:(n:number,f:FaseDeFlujo,d:string)=>string};
 expect(typeof h.debeAnunciar).toBe('function');
 const events: unknown[] = [], forwarded: unknown[] = [];
 const callback = new Function('debeAnunciar', 'literalPty', 'fases', 'detalles', 'reintentos', 'agentes', 'anunciar', 'alFase', `${extractFunction('fase')}; return fase;`);
 const agentes = [{id:'agent',superficie:'srf-ng-1'}];
 const make = (emit:(e:unknown)=>void): ((id:string,f:FaseDeFlujo,d:string)=>void) => callback(h.debeAnunciar,h.literalPty,{}, {}, {},agentes,emit,(...args:unknown[])=>forwarded.push(args));
 const fase = make(e=>events.push(e));
 fase('agent','fallido',''); fase('agent','fallido',''); fase('agent','abierto',''); fase('agent','conectando','');
 expect(events).toEqual([{kind:'degraded',scope:'cockpit',literal:'PTY T1 fallido: fallido'}]);
 expect(forwarded).toHaveLength(4);
 expect(()=>make(()=>{throw new Error('sink offline');})('agent','cerrado','   ')).not.toThrow();
 expect(h.literalPty(2,'reconectando','  lost  ')).toBe('PTY T2 reconectando: lost');
 expect(h.debeAnunciar('reconectando','reconectando')).toBe(false);
 expect(h.debeAnunciar('abierto','cerrado')).toBe(true);
});

it('schedules no watermark interval while collapsed and clears the open timer', () => {
 const effect = /\$effect\(\(\) => \{([\s\S]*?)\n  \}\);/.exec(source)?.[1];
 expect(effect).toBeDefined();
 const calls: string[] = [];
 const fakeWindow = {setInterval:()=>{calls.push('set');return 7;},clearInterval:(id:number)=>calls.push(`clear ${id}`)};
 const run = new Function('abierto','window','untrack','leerMarcaDeAgua','LECTURA_MS', effect!);
 expect(run(false,fakeWindow,(f:()=>void)=>f(),()=>calls.push('read'),500)).toBeUndefined();
 expect(calls).toEqual([]);
 const cleanup = run(true,fakeWindow,(f:()=>void)=>f(),()=>calls.push('read'),500) as ()=>void;
 cleanup(); expect(calls).toEqual(['set','read','clear 7']);
});

it('renders unique, slot-qualified controls with empty and populated hubs', () => {
 const script = `
 import {compile} from 'svelte/compiler'; import {render} from 'svelte/server'; import {readFileSync} from 'node:fs';
 try {
 let s=readFileSync(0,'utf8');
 s=s.replace(/import TerminalPanel from '[^']+';/, 'const TerminalPanel = () => {};')
 .replace(/import \\{ ALINEACION_CODEC, LIENZO_W \\} from '[^']+';/, 'const ALINEACION_CODEC=16, LIENZO_W=1264;')
 .replace(/import \\{ ALTURA_ASA[^;]+;/, 'const ALTURA_ASA=12, ALTURA_CABECERA=32, ALTURA_CAJON=240, PANELES_INTERACTIVOS_MAX=56, SCROLLBACK_MAX=2000; const modoDeInstancia=()=>"interactive";')
 .replace(/import \\{ rovingTecla \\} from '[^']+';/, 'const rovingTecla=()=>null;');
 let js=compile(s,{generate:'server',filename:'CajonAgentes.svelte'}).js.code;
 for(const name of ['svelte/internal/server','svelte']) js=js.replaceAll("'"+name+"'",JSON.stringify(import.meta.resolve(name)));
 const {default:C}=await import('data:text/javascript;base64,'+Buffer.from(js).toString('base64'));
 console.log(JSON.stringify([0,4].map(n=>render(C,{props:{lado:'left',abierto:true,agentes:Array.from({length:n},(_,i)=>({id:'a'+i,titulo:'Claude Code',superficie:'srf-ng-'+(i+1),url:''}))}}).body)));
 } catch(e){console.error(String(e));process.exitCode=5;}`;
 const result=spawnSync(process.execPath,['--input-type=module','-e',script],{cwd:new URL('..',import.meta.url),input:source,encoding:'utf8',timeout:10000});
 expect(result.status,result.stderr).toBe(0);
 const bodies=JSON.parse(result.stdout) as string[];
 for(const body of bodies){
  expect(body).toContain('id="agent-hub"');
  const labels=Array.from(body.matchAll(/<(?:button|input)\b[^>]*aria-label="([^"]+)"/g),m=>m[1]);
  expect(new Set(labels).size).toBe(labels.length);
  for(const n of ['Launch srf shell','Launch Claude Code','Launch Gemini CLI']) expect(labels).toContain(n);
 }
 for(let i=1;i<=4;i++) expect(bodies[1]).toContain('aria-label="T'+i+' Claude Code srf-ng-'+i+'"');
});


it('delegates wrapping arrows and Home/End to the existing KEYS export', () => {
 expect(rovingTecla('ArrowLeft',0,4)).toBe(3);
 expect(rovingTecla('ArrowRight',3,4)).toBe(0);
 expect(rovingTecla('Home',2,4)).toBe(0);
 expect(rovingTecla('End',1,4)).toBe(3);
 expect(rovingTecla('End',0,0)).toBeNull();
});

it('returns focus only after the collapsed handle has been revealed by tick', async () => {
 const calls: string[]=[];
 const make=new Function('tick','asa', `let abierto = true; ${extractFunction('plegar')}; return {plegar, isOpen:()=>abierto};`);
 const ctrl=make(async()=>{calls.push('tick');await Promise.resolve();calls.push('revealed');},{focus:()=>calls.push('focus')}) as {plegar:()=>Promise<void>;isOpen:()=>boolean};
 const pending=ctrl.plegar();
 expect(ctrl.isOpen()).toBe(false);
 expect(calls).toEqual(['tick']);
 await pending;
 expect(calls).toEqual(['tick','revealed','focus']);
});
