import { describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { build } from 'esbuild';
import { compile } from 'svelte/compiler';
import { celdaCpu, celdaRam, muestra, slotIdCanonico } from '../src/lib/telemetry/celdas';
import type { Metric } from '../src/lib/types/hypervisor';

const metric = (value=123, ts=1000, source='procfs'): Metric => ({value,ts_mono_ms:ts,source} as Metric);
const root = fileURLToPath(new URL('..',import.meta.url));
const strip = readFileSync(`${root}/src/lib/cards/TelemetryStrip.svelte`,'utf8');

describe('telemetry truth adapters',()=>{
 it('keeps missing metrics unknown and accepts sourced zero CPU only',()=>{
  expect(muestra(null,1000)).toBeNull();
  expect(celdaCpu(null,1000).texto).toBe('CPU —');
  expect(celdaCpu(metric(0),1000).texto).toBe('CPU 0.0%');
  expect(celdaCpu(metric(),1000)).toEqual({texto:'CPU 12.3%',source:'procfs',ts:1000});
  expect(celdaRam(metric(104857600),1000).texto).toBe('RAM 100 MiB');
  expect(celdaRam(metric(0),1000).texto).toBe('RAM —');
  expect(slotIdCanonico(0)).toBe('srf-ng-0');
 });
 it('accepts the inclusive freshness boundary only',()=>{
  expect(muestra(metric(12,1),3001)).toBe(12);
  expect(muestra(metric(12,1),3002)).toBeNull();
  expect(muestra(metric(12,1001),1000)).toBeNull();
 });
 const bad: Array<[string,unknown]> = [
  ['fixture_cpu_42',metric(42,1000,'fixture')],['source_cgroup_v2',metric(1,1000,'cgroup_v2')],
  ['bare_number',42],['float_value',metric(0.14)],['negative',metric(-1)],
  ['zero_ts',metric(12,0)],['metric_field_v',{source:'procfs',ts_mono_ms:1000,v:12}],
  ['unsafe',metric(Number.MAX_SAFE_INTEGER+1)],['nan',metric(NaN)],['infinity',metric(Infinity)],
 ];
 it.each(bad)('rejects %s',(_name,value)=>{
  expect(muestra(value as Metric,1000)).toBeNull();
  expect(celdaCpu(value as Metric,1000).texto).toBe('CPU —');
  expect(celdaRam(value as Metric,1000).texto).toBe('RAM —');
 });
 it('rejects missing or malformed document clocks',()=>{
  for(const ts of [NaN,Infinity,0,1.2,Number.MAX_SAFE_INTEGER+1]) expect(muestra(metric(),ts)).toBeNull();
 });
});

describe('WIRE telemetry relocation and R8 semantics',()=>{
 const matrix=readFileSync(`${root}/src/lib/workspace/WorkspaceMatrix.svelte`,'utf8');
 function metricGroups(source:string): string[] {
  return [...source.matchAll(/<span\b[^>]*aria-label="(?:CPU|RAM) \{c\.slot_id\}"[^>]*>/g)]
   .filter(([tag])=>/role="group"/.test(tag)).map(([tag])=>tag.match(/aria-label="([^"]+)"/)![1]);
 }
 it('celdas_relocated_and_matrix_group_roles',async()=>{
  const cells=await import('../src/lib/telemetry/celdas');
  const legacy=await import('../src/lib/capsulas');
  for(const name of ['muestra','celdaCpu','celdaRam','slotIdCanonico'] as const) expect(legacy[name]).toBe(cells[name]);
  expect(strip).toMatch(/from ['"]\.\.\/telemetry\/celdas['"]/);
 });
 it('matrix_metric_group_roles',()=>{
  expect(metricGroups(matrix)).toEqual(['CPU {c.slot_id}','RAM {c.slot_id}']);
 });
 it('rejects either missing matrix metric role (R8 negative controls)',()=>{
  for(const metric of ['CPU','RAM']) {
   const mutant=matrix.replace(/<span\b[^>]*>/g, tag=>tag.includes(`aria-label="${metric} {c.slot_id}"`) ? tag.replace(/role="group"\s*/,'') : tag);
   expect(metricGroups(mutant)).toHaveLength(1);
  }
 });
});

async function renderStrip(props: Record<string,unknown>): Promise<string> {
 const entry=`import {render} from 'svelte/server'; import Strip from './src/lib/cards/TelemetryStrip.svelte'; console.log(render(Strip,{props:${JSON.stringify(props)}}).body);`;
 const result=await build({stdin:{contents:entry,resolveDir:root,sourcefile:'sensor-ssr.js'},bundle:true,write:false,platform:'node',format:'esm',packages:'external',plugins:[{name:'svelte-ssr',setup(b){b.onLoad({filter:/\.svelte$/},async args=>({contents:compile(readFileSync(args.path,'utf8'),{generate:'server',filename:args.path}).js.code,loader:'js'}));}}]});
 const child=spawnSync(process.execPath,['--input-type=module','-e',result.outputFiles[0].text],{cwd:root,encoding:'utf8',timeout:10000,env:{...process.env,DISPLAY:''}});
 expect(child.status,child.stderr).toBe(0);
 return child.stdout;
}
const capsula={id:'vm-0',slot:0,name:'legacy',archetype:'browser',xpraPort:10020,cdpPort:9220,gates:{cdp:{auth:true,human:true,attach:false,submit:false,harvest:true}},telemetry:{cpuPercent:0,memoryBytes:123}};
const base={capsula,geometria:{},visible:true};

describe('telemetry strip SSR and static contract',()=>{
 it('compiles current mounts with optional canonical inputs and host scope',()=>{
  for(const name of ['cards/CapsuleCard.svelte','components/TelemetryMasterView.svelte','cards/TelemetryStrip.svelte']) {
   const path=`${root}/src/lib/${name}`;
   expect(()=>compile(readFileSync(path,'utf8'),{generate:'client',filename:path})).not.toThrow();
  }
 });
 it('keeps polar text above AAA on the translucent bar including white remote content',()=>{
  const css=readFileSync(`${root}/src/app.css`,'utf8');
  const rgb=(hex:string)=>[0,2,4].map(i=>parseInt(hex.slice(i,i+2),16));
  const luminance=(rgb:number[])=>rgb.map(v=>v/255).map(v=>v<=0.04045?v/12.92:((v+0.055)/1.055)**2.4).reduce((sum,v,i)=>sum+v*[0.2126,0.7152,0.0722][i],0);
  const foreground=rgb(css.match(/--text-polar:\s*#([0-9a-f]{6})/i)![1]);
  for(const match of css.matchAll(/--surface-cockpit:\s*#([0-9a-f]{6})/gi)) {
   const backdrop=rgb(match[1]).map(v=>v*0.88+255*0.12);
   expect((luminance(foreground)+0.05)/(luminance(backdrop)+0.05)).toBeGreaterThanOrEqual(7);
  }
 });
 it('never reads legacy telemetry and renders missing canonical metrics as unknown',async()=>{
  const html=await renderStrip({...base,metricas:null});
  expect(html).toContain('Telemetry srf-ng-0');
  expect(html).toContain('CPU —'); expect(html).toContain('RAM —');
  expect(html).toContain('telemetry-stale'); expect(html).toContain('data-sensor="none"');
  expect(html).not.toContain('CPU 0.0%');
 });
 it('renders CPU and RAM independently with unique host names and sourced timestamps',async()=>{
  const props={...base,metricas:{cpu_permille:null,mem_bytes:metric(104857600)},docTs:1000};
  const html=await renderStrip(props);
  expect(html).toContain('CPU —'); expect(html).toContain('RAM 100 MiB');
  expect(html).toContain('Strip CPU srf-ng-0: no data'); expect(html).toContain('Strip RAM srf-ng-0: 100 MiB · procfs');
  expect(html).toContain('data-ts="1000"');
  const host=await renderStrip({...props,ambito:'host'});
  expect(host).toContain('Telemetry host'); expect(host).toContain('Strip RAM host: 100 MiB · procfs');
  expect(host).not.toContain('Telemetry srf-ng-0');
  for(const tag of [...(html+host).matchAll(/<[^>]+aria-label="[^"]*"[^>]*>/g)]) expect(tag[0]).toContain('role="group"');
 });
 it('dashes stale/future and zero RAM while preserving valid sourced zero CPU',async()=>{
  const html=await renderStrip({...base,metricas:{cpu_permille:metric(0),mem_bytes:metric(0)},docTs:1000});
  expect(html).toContain('CPU 0.0%'); expect(html).toContain('RAM —');
  for(const ts of [1001,1]) {
   const bad=await renderStrip({...base,metricas:{cpu_permille:metric(123,ts),mem_bytes:metric(104857600,ts)},docTs:ts===1?3002:1000});
   expect(bad).toContain('CPU —'); expect(bad).toContain('RAM —');
  }
 });
 it('has no independent motion/live region or legacy source and retains polar contrast',()=>{
  expect(strip).not.toMatch(/capsula\.telemetry|formatoCpu|formatoMemoria|transition|setInterval|setTimeout|requestAnimationFrame|@keyframes|aria-live|role="(?:status|alert)"/);
  expect(strip).toContain('color: var(--text-polar)');
  expect(strip).toContain('role="group"');
  const sampler=readFileSync(`${root}/../telemetria.go`,'utf8');
  expect(sampler).not.toMatch(/setInterval|setTimeout|requestAnimationFrame|@keyframes|transition/);
 });
});
