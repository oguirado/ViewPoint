// FRONTEND: F1-TOKENS v2. Node-only; artifacts live exclusively under build/room_BTO.
import { createHash } from 'node:crypto';
import { readFileSync, readdirSync, mkdtempSync, mkdirSync, rmSync } from 'node:fs';
import { resolve, join, relative } from 'node:path';
import { fileURLToPath } from 'node:url';
import { afterAll, describe, expect, it } from 'vitest';
import postcss, { type Rule } from 'postcss';
import { build } from 'vite';
import { parseAst } from 'rollup/parseAst';
import { svelte, vitePreprocess } from '@sveltejs/vite-plugin-svelte';
import { urlXpra } from '../src/lib/cards/visor';
import { FlujoWs } from '../src/lib/terminal/wsstream';
// @ts-expect-error existing JS instrument has no declaration file
import { analizarTokens } from '../../../scripts/ng/medir-contraste.mjs';

const frontend = fileURLToPath(new URL('..', import.meta.url));
const repo = resolve(frontend, '../..');
const scratch = resolve(repo, 'build/room_BTO');
const read = (p: string): string => readFileSync(resolve(frontend, p), 'utf8');
const css = read('src/app.css'), html = read('index.html');
const stripComments = (s: string): string => s.replace(/\/\*[\s\S]*?\*\//g, '');
const rules = (s: string): Rule[] => { const out: Rule[] = []; postcss.parse(s).walkRules(r => { out.push(r); }); return out; };
const decls = (r: Rule): Map<string, string> => { const out = new Map<string, string>(); r.walkDecls(d => { out.set(d.prop, d.value); }); return out; };
const styles = (s: string): string => s.includes('<style') ? [...s.matchAll(/<style[^>]*>([\s\S]*?)<\/style>/g)].map(m => m[1]).join('\n') : s.includes('<script') ? '' : s;

// Root-family cascade: specificity first, then source order. :where() contributes zero specificity.
function tokens(source: string, theme = 'oscuro'): Map<string, string> {
  const result = new Map<string, string>(), priorities = new Map<string, number>();
  for (const r of rules(source)) {
    const sel = r.selector;
    if (!/^(?:html)?:root(?:\[data-tema=['"]?[a-z-]+['"]?\])?$|^:where\(:root(?:\[data-tema=['"]?[a-z-]+['"]?\])?\)$/.test(sel)) continue;
    const named = /data-tema=['"]?([a-z-]+)/.exec(sel)?.[1];
    if (named && named !== theme) continue;
    const rank = sel.startsWith(':where(') ? 0 : (sel.startsWith('html') ? 1 : 0) + (named ? 20 : 10);
    for (const [k, v] of decls(r)) if (rank >= (priorities.get(k) ?? -1)) { priorities.set(k, rank); result.set(k, v); }
  }
  function expand(k: string, seen: string[] = []): string {
    if (seen.includes(k)) throw new Error(`cyclic variable ${k}`);
    const v = result.get(k); if (!v) throw new Error(`missing variable ${k}`);
    return v.replace(/var\((--[\w-]+)(?:,[^)]*)?\)/g, (_, n: string) => expand(n, [...seen, k]));
  }
  return new Map([...result.keys()].map(k => [k, expand(k)]));
}
function luminance(hex: string): number {
  if (!/^#[a-f\d]{6}$/i.test(hex)) throw new Error(`not opaque RGB: ${hex}`);
  const c = [1, 3, 5].map(i => parseInt(hex.slice(i, i + 2), 16) / 255).map(v => v <= .04045 ? v / 12.92 : ((v + .055) / 1.055) ** 2.4);
  return c[0] * .2126 + c[1] * .7152 + c[2] * .0722;
}
const ratio = (a: string, b: string): number => (Math.max(luminance(a), luminance(b)) + .05) / (Math.min(luminance(a), luminance(b)) + .05);
function contrastFaults(source: string): string[] {
  const faults: string[] = [];
  for (const theme of ['oscuro', 'alto-contraste']) {
    const t = tokens(source, theme);
    for (const fg of ['--text-polar', '--text-silver-raised', '--telemetry-stale', '--text-off'])
      for (const bg of ['--canvas-void', '--surface-obsidian', '--surface-cockpit', '--surface-overlay'])
        if (ratio(t.get(fg)!, t.get(bg)!) < 7) faults.push(`${theme} ${fg} ${bg}`);
    const overlay = '#' + [1, 3, 5].map(i => Math.round(parseInt(t.get('--surface-cockpit')!.slice(i, i + 2), 16) * .88 + 255 * .12).toString(16).padStart(2, '0')).join('');
    if (ratio(t.get('--text-polar')!, overlay) < 7) faults.push(`${theme} polar translucent`);
  }
  return faults;
}
const px = (v: string): number | null => { const m = /(?:^|\s)(\d*\.?\d+)(px|rem)(?=[\s/;]|$)/.exec(v); return m ? Number(m[1]) * (m[2] === 'rem' ? 16 : 1) : null; };
// JTO-02: font only, never spacing; locally explicit sans labels have the 13px floor, mono data 12px.
function smallFonts(source: string): number {
  let n = 0;
  for (const r of rules(styles(source))) r.walkDecls(d => {
    if (!['font', 'font-size'].includes(d.prop)) return;
    const size = px(d.value), family = d.prop === 'font' ? d.value : decls(r).get('font-family') ?? '';
    if (size !== null && size < (/sans/.test(family) ? 13 : 12)) n++;
  });
  return n;
}
const filesUnder = (dir: string): string[] => readdirSync(dir, { withFileTypes: true }).flatMap(e => e.isDirectory() ? filesUnder(join(dir, e.name)) : [join(dir, e.name)]);
// The original 20/23 rows counted <12px only. Sealed sans-floor scanning also finds 2/1 existing 12px sans
// declarations. These measured corrections are explicit; owners may only lower them (JTO-02/03/04).
const debt: Record<string, number> = {
  'src/App.svelte': 6, 'src/lib/components/CockpitMasterView.svelte': 22,
  'src/lib/terminal/CajonAgentes.svelte': 11, 'src/lib/components/TelemetryMasterView.svelte': 24,
  'src/lib/components/MasterCanvas.svelte': 0, 'src/app.css': 0,
};
const debtFaults = (files: Record<string, string>): string[] => Object.entries(files).filter(([p, s]) => smallFonts(s) > (debt[p] ?? 0)).map(([p]) => p);
const mutedDebt = (s: string): number => (stripComments(styles(s)).match(/(?:color|--c-text[\w-]*)\s*:\s*var\(--text-muted\b/g) ?? []).length;
function textFaults(source: string): string[] {
  return rules(styles(source)).flatMap(r => {
    const d = decls(r), bg = d.get('background') ?? d.get('background-color') ?? '', fg = d.get('color') ?? '';
    if (/rgba\(|\/\s*0?\.88|88%/.test(bg) && /telemetry-stale|text-silver/.test(fg)) return ['stale_on_translucent_overlay'];
    if (/surface-cockpit/.test(bg) && /var\(--text-silver\s*[,)]/.test(fg)) return ['silver_on_cockpit'];
    return [];
  });
}
function focusFaults(source: string): string[] {
  const rs = rules(styles(source));
  const removed = rs.some(r => /^(?:none|0)$/.test(decls(r).get('outline') ?? ''));
  const replaced = rs.some(r => /:focus-(?:visible|within)/.test(r.selector) && /(?:2px|0\.125rem)\s+solid/.test(decls(r).get('outline') ?? ''));
  return removed && !replaced ? ['outline_none_no_replacement'] : [];
}
function recShapes(source: string): string[] {
  return ['idle', 'pending', 'active', 'sealed', 'error'].map(state => {
    const d = new Map<string, string>();
    for (const r of rules(source)) {
      if (r.parent?.type === 'atrule') continue;
      if (r.selector === '.rec-indicator' || state !== 'idle' && r.selector === `.rec-indicator.${state}`) {
        for (const [k, v] of decls(r)) d.set(k, v);
      }
    }
    const border = d.get('border') ?? '', fill = d.get('background') ?? d.get('background-color') ?? '';
    return [border.match(/\d+px/)?.[0], border.match(/dashed|solid/)?.[0], d.get('border-radius'), fill === 'transparent' ? 'hollow' : 'filled'].join('|');
  });
}
const staticRecording = (s: string): boolean => rules(s).some(r => r.selector === ".rec-indicator.active:not([data-pulse='active'])" && decls(r).get('animation') === 'none');
const reducedRules = (s: string): Rule[] => rules(s).filter(r => r.parent?.type === 'atrule' && r.parent.params === '(prefers-reduced-motion: reduce)');
const motionKilled = (s: string): boolean => reducedRules(s).some(r => r.selector === '*' && /animation:\s*none\s*!important/.test(r.toString()));
const hash = (s: string): string => createHash('sha256').update(s).digest('base64');
function cspFaults(doc: string): string[] {
  const meta = /<meta\s+http-equiv="Content-Security-Policy"\s+content="([^"]+)"\s*\/?\s*>/i.exec(doc)?.[1];
  if (!meta) return ['csp_missing'];
  const p = new Map(meta.split(';').map(v => { const [k, ...xs] = v.trim().split(/\s+/); return [k, xs]; }));
  const faults: string[] = [], script = p.get('script-src');
  if (!script?.includes("'self'") || script.includes("'unsafe-inline'")) faults.push('csp_script_src_missing');
  for (const m of doc.matchAll(/<script([^>]*)>([\s\S]*?)<\/script>/g))
    if (!/\bsrc=|type="module"/.test(m[1]) && !script?.includes(`'sha256-${hash(m[2])}'`)) faults.push('csp_hash_stale');
  for (const key of ['connect-src', 'frame-src']) {
    const values = p.get(key);
    if (!values?.length || values.some(v => !["'self'", 'wails:'].includes(v) && !/^(?:https?|wss?):\/\/(?:127\.0\.0\.1|localhost):\*$/.test(v))) faults.push('csp_connect_wildcard_host');
  }
  if (p.get('worker-src')?.join(' ') !== "'none'") faults.push('csp_worker_blob');
  for (const key of ['object-src', 'base-uri', 'form-action']) if (p.get(key)?.join(' ') !== "'none'") faults.push(key);
  for (const key of ['default-src', 'font-src']) if (p.get(key)?.join(' ') !== "'self'") faults.push(key);
  return faults;
}
function networkFaults(source: string, javascript = false): string[] {
  let s = stripComments(source);
  const faults: string[] = [];
  if (javascript) {
    // Svelte ships diagnostic URLs inside Error messages and console warnings.
    // Only inert diagnostic strings at those two sinks are exempt;
    // a URL at a fetch/socket/import/attribute sink is still rejected, even on svelte.dev.
    const inert: [number, number][] = [];
    type Node = {type: string; start?: number; end?: number; [key: string]: unknown};
    function walk(value: unknown, parent?: Node): void {
      if (!value || typeof value !== 'object') return;
      const n = value as Node;
      if (n.type === 'Literal' || n.type === 'TemplateLiteral') {
        const callee = parent?.callee as Node | undefined;
        const object = callee?.object as Node | undefined, property = callee?.property as Node | undefined;
        const error = parent?.type === 'NewExpression' && callee?.type === 'Identifier' && callee.name === 'Error';
        const warning = parent?.type === 'CallExpression' && callee?.type === 'MemberExpression' && object?.type === 'Identifier' && object.name === 'console' && property?.type === 'Identifier' && property.name === 'warn';
        if (error || warning) {
          const parts = n.type === 'TemplateLiteral' ? n.quasis as Node[] : [n];
          for (const part of parts) {
            const value = part.type === 'TemplateElement' ? (part.value as {raw:string}).raw : part.value;
            if (typeof value === 'string' && /https:\/\/(?:svelte\.dev\/e\/|github\.com\/sveltejs\/svelte)/.test(value) && part.start !== undefined && part.end !== undefined) inert.push([part.start,part.end]);
          }
        }
      }
      for (const [k, v] of Object.entries(n)) if (k !== 'parent') {
        if (Array.isArray(v)) v.forEach(x => walk(x,n));
        else if (v && typeof v === 'object') walk(v,n);
      }
    }
    // Parse original bytes so node offsets remain exact; stripping comments would shift them.
    walk(parseAst(source));
    for (const [a,b] of inert.sort((x,y)=>y[0]-x[0])) source = source.slice(0,a) + '"diagnostic"' + source.slice(b);
    s = stripComments(source);
  }
  if (/@font-face|@import\s+(?:url\()?['"]?https?:/i.test(s)) faults.push('remote_font_face');
  // Namespace identifiers are inert data in Svelte's SVG/MathML runtime, not network requests.
  for (const u of s.match(/(?:https?|wss?):\/\/[^\s'"`<>)}\\]+/g) ?? [])
    if (!/^(?:https?|wss?):\/\/(?:127\.0\.0\.1|localhost)(?=[:/]|$)/.test(u) && !/^http:\/\/www\.w3\.org\/(?:1999\/xhtml|2000\/svg|1998\/Math\/MathML|1999\/xlink|XML\/1998\/namespace)$/.test(u)) faults.push(`remote URL ${u}`);
  return faults;
}
const runtimeAllowed = (u: string): boolean => { const x = new URL(u); return ['http:', 'ws:'].includes(x.protocol) && ['127.0.0.1', 'localhost'].includes(x.hostname) && !x.username && !x.password; };
let built: string | undefined;
afterAll(() => { if (built) rmSync(built, { recursive: true, force: true }); });

describe('FRONTEND (os-cu-0 F1-TOKENS)', () => {
  it('resolves effective cascade, AAA pairs, size floors and the pinned canonical fixture', () => {
    expect(hash(css.split('/* F1-TOKENS additive')[0])).toBe('zZJIVovHc4hK6zbZapVUIWLer4jmlU5s35IWroWXq5M=');
    expect(contrastFaults(css)).toEqual([]);
    for (const theme of ['oscuro', 'alto-contraste']) {
      const t = tokens(css, theme);
      for (const [name, floor] of [['--text-2xs', 12], ['--text-xs', 12], ['--text-sm', 13]] as const) expect(px(t.get(name)!)).toBeGreaterThanOrEqual(floor);
      expect(t.get('--border-focus')).toBe(t.get('--accent-cyan'));
    }
    const app = analizarTokens(css) as Record<string, Map<string, string>>;
    const deg = analizarTokens(readFileSync(resolve(repo, 'fixture/ng/tokens/degradados.css'), 'utf8')) as Record<string, Map<string, string>>;
    const differences = Object.keys(app).flatMap(t => [...app[t]].filter(([k, v]) => deg[t].get(k) !== v).map(([k, v]) => `${t} ${k}: ${v} -> ${deg[t].get(k)}`));
    expect(differences).toEqual(['oscuro --text-silver: #94a3b8 -> #59626e']);
    expect(Object.keys(app)).toEqual(Object.keys(deg));
    for (const t of Object.keys(app)) expect([...app[t].keys()]).toEqual([...deg[t].keys()]);
    const version = decls(rules(css).find(r => r.selector === '.version')!);
    expect(version.get('color')).toBe('var(--text-silver-raised,var(--text-silver))');
    expect(px(version.get('font-size')!)).toBeGreaterThanOrEqual(12);
    expect(px(decls(rules(css).find(r => r.selector === '.rotulo')!).get('font-size')!)).toBeGreaterThanOrEqual(13);
  });
  it('kills legibility/cascade mutants, including a losing :where override', () => {
    expect(smallFonts('.label{font:500 11px/16px var(--font-sans);}')).toBe(1); // label_11px
    expect(smallFonts('.data{font:500 10px/16px var(--font-mono);}')).toBe(1); // mono_10px
    expect(smallFonts('.data{font-family:var(--font-mono);font-size:.75rem;gap:9px;}')).toBe(0);
    expect(smallFonts('.label{font:500 .75rem/1 var(--font-sans);}')).toBe(1);
    for (const [name, mutation] of [
      ['token_pair_below_7', css + 'html:root{--text-polar:#64748b;}'],
      ['stale_7f8da3', css.replace('--telemetry-stale: #a5b4ce', '--telemetry-stale: #7f8da3')],
      ['hc_theme_regresses_pair', css + 'html:root[data-tema="alto-contraste"]{--telemetry-stale:#7f8da3;}'],
    ]) expect(contrastFaults(mutation).length, name).toBeGreaterThan(0);
    expect(px(tokens(':root{--text-xs:11px/16px monospace}:where(:root){--text-xs:12px/16px monospace}').get('--text-xs')!)).toBe(11);
    expect((analizarTokens(css + ':root{--unexpected:#ffffff;}') as Record<string, Map<string, string>>).oscuro.has('--unexpected')).toBe(true);
    expect(textFaults('.s{background:rgba(22,29,46,.88);color:var(--telemetry-stale);}')).toEqual(['stale_on_translucent_overlay']);
    expect(textFaults('.s{background:var(--surface-cockpit);color:var(--text-silver);}')).toEqual(['silver_on_cockpit']);
    expect(mutedDebt('.s{color:var(--text-muted);}')).toBe(1);
  });
  it('ratchets source debt and STATUS, and catches growth and new undersized files', () => {
    const files = Object.fromEntries(filesUnder(resolve(frontend, 'src')).filter(p => /\.(?:svelte|ts|css)$/.test(p)).map(p => [relative(frontend, p), readFileSync(p, 'utf8')]));
    const styled = Object.fromEntries(Object.entries(files).filter(([p]) => /\.(?:svelte|css)$/.test(p)));
    expect(debtFaults(styled)).toEqual([]);
    for (const [p, source] of Object.entries(styled)) {
      expect(mutedDebt(source), p).toBeLessThanOrEqual(p === 'src/App.svelte' ? 1 : 0);
      expect(focusFaults(source), p).toEqual([]);
      expect(textFaults(source), p).toEqual([]);
    }
    for (const [p, source] of Object.entries(files)) expect(networkFaults(source), p).toEqual([]);
    const status = files['src/lib/aiops/OperationStatus.svelte'];
    const d = decls(rules(styles(status)).find(r => r.selector === '.operation-status')!);
    expect(px(d.get('font')!)).toBeGreaterThanOrEqual(13);
    expect(d.get('color')).toMatch(/var\(--text-polar/);
    expect(smallFonts(status.replace('13px/16px', '10px/16px'))).toBe(1);
    expect(debtFaults({'src/App.svelte': '.s{font-size:10px;}'.repeat(7)})).toEqual(['src/App.svelte']);
    expect(debtFaults({'src/new.svelte': '.s{font-size:10px;}'})).toEqual(['src/new.svelte']);
  });
  it('provides the global focus ring and rejects missing or 1px replacements', () => {
    const r = rules(css).find(r => r.selector.startsWith(':where(button,') && r.selector.endsWith(':focus-visible'));
    expect(r).toBeDefined();
    expect(decls(r!).get('outline')).toBe('2px solid var(--border-focus)');
    expect(decls(r!).get('outline-offset')).toBe('2px');
    for (const stop of ['a[href]', 'input', 'select', 'textarea', 'summary', '[tabindex]', '[role=tab]', '[role=menuitem]', '[role=separator]', '[role=option]']) expect(r!.selector).toContain(stop);
    expect(focusFaults('.s{outline:none;}')).toEqual(['outline_none_no_replacement']);
    expect(focusFaults('.s{outline:0;}.s:focus-visible{outline:1px solid cyan;}')).toEqual(['outline_none_no_replacement']);
  });
  it('separates REC shapes in grey scale, stops recording pulse and kills reduced-motion loops', () => {
    expect(new Set(recShapes(css)).size).toBe(5);
    expect(staticRecording(css)).toBe(true);
    expect(motionKilled(css)).toBe(true);
    for (const [state, color] of [['pending', 'amber'], ['active', 'crimson']]) expect(reducedRules(css).some(r => r.selector === `.rec-indicator.${state}` && (decls(r).get('box-shadow') ?? '').includes(`accent-${color}`))).toBe(true);
    expect(new Set(recShapes('.rec-indicator{border:1px solid gray;border-radius:50%;background:transparent}.rec-indicator.active{background:red}.rec-indicator.sealed{background:green}')).size).toBeLessThan(5);
    expect(new Set(recShapes(css.replace('border: 2px dashed', 'border: 1px solid'))).size).toBeLessThan(5);
    expect(staticRecording(css.replace(".rec-indicator.active:not([data-pulse='active'])", '.removed'))).toBe(false);
    expect(motionKilled(css.replaceAll('animation: none !important;', 'animation: rec-phosphor-pulse 800ms infinite;'))).toBe(false);
  });
  it('pins bootstrap CSP and rejects unsafe directives, stale hashes and remote assets', () => {
    expect(cspFaults(html)).toEqual([]);
    expect(html.indexOf('Content-Security-Policy')).toBeLessThan(html.indexOf('<script'));
    expect(networkFaults(html)).toEqual([]);
    for (const [name, mutation] of [
      ['csp_missing', html.replace('Content-Security-Policy', 'removed')],
      ['csp_hash_stale', html.replace('var raiz =', 'var raiz  =')],
      ['csp_script_src_missing', html.replace(/script-src[^;]+;/, '')],
      ['csp_connect_wildcard_host', html.replace("connect-src 'self'", "connect-src * 'self'")],
      ['csp_worker_blob', html.replace("worker-src 'none'", "worker-src blob:")],
    ]) expect(cspFaults(mutation), name).toContain(name);
    for (const [name, source] of [
      ['cdn_script_tag', '<script src="https://cdn.example/a.js"></script>'],
      ['remote_font_face', '@font-face{src:url(https://fonts.example/a.woff2)}'],
      ['font_import_http', '@import "https://fonts.example/a.css";'],
      ['dist_remote_url', 'fetch("https://remote.example/data")'],
    ]) expect(networkFaults(source).length, name).toBeGreaterThan(0);
  });
  it('checks actual Xpra and injected PTY URLs without opening sockets', () => {
    expect(runtimeAllowed(urlXpra(15001))).toBe(true);
    expect(runtimeAllowed('https://remote.example/data')).toBe(false);
    expect(networkFaults('throw new Error("https://svelte.dev/e/invariant_violation")',true)).toEqual([]);
    expect(networkFaults('fetch("https://svelte.dev/e/invariant_violation")',true).length).toBeGreaterThan(0);
    expect(networkFaults('throw new Error(`https://svelte.dev/e/invariant_violation ${fetch("https://remote.example/data")}`)',true).length).toBeGreaterThan(0);
    const attempted: string[] = [];
    new FlujoWs({url:'ws://127.0.0.1:15002/pty/test', alDatos:()=>{}, crearSocket:u=>{attempted.push(u);throw new Error('test: no socket opened');}, programar:()=>0, cancelar:()=>{}, maxReconexiones:0}).conectar();
    expect(attempted).toEqual(['ws://127.0.0.1:15002/pty/test']);
    expect(attempted.every(runtimeAllowed)).toBe(true);
  });
  it('builds into lane scratch and verifies emitted CSP hashes and self-contained artifacts', async () => {
    mkdirSync(scratch, {recursive:true}); built = mkdtempSync(join(scratch, 'dist-'));
    await build({configFile:false, root:frontend, base:'./', cacheDir:join(scratch,'build-cache'), plugins:[svelte({preprocess:vitePreprocess()})], logLevel:'error', build:{outDir:built,emptyOutDir:true,target:'es2022',sourcemap:false}});
    expect(cspFaults(readFileSync(join(built,'index.html'),'utf8'))).toEqual([]);
    for (const p of filesUnder(built)) if (/\.(?:html|css|js)$/.test(p)) expect(networkFaults(readFileSync(p,'utf8'),p.endsWith('.js')),relative(built,p)).toEqual([]);
  });
});
