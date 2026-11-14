#!/usr/bin/env node
// scripts/ng/medir-cockpit.mjs — U-23 aggregator behind gate B-31: cockpit geometry, CLS, one GL context, scroll p95,
// unmounts, DOM renderer and AAA, measured LIVE by the four unit instruments and compared with the six sealed files of those four units.
//
//   node scripts/ng/medir-cockpit.mjs [--json] [--desde-disco] [--evidencia <dir>]
//        [--mutante envolver-if|sin-arranque-sincrono|tokens-degradados] [--plazo-leg-ms 240000]
//
// WHAT IT DOES. It runs, one after the other (Chromium timing must not be perturbed by a sibling), the instruments of
// U-18 (medir-cls.mjs, both modes), U-19 (medir-scroll.mjs, 24 cards, both modes), U-20 (medir-contraste.mjs --tokens) and
// U-21 (medir-terminal.mjs, 56 panels) as child processes, parses the ONE JSON report each prints, and reads the sealed
// unit files (evidencia/v2/u18/cls-16.json, cls-13.json, u19/scroll-16.json, scroll-13.json, u20/contraste.json,
// u21/terminal.json). Every property of the gate is then evaluated on BOTH observations, made at different times by
// different processes, and the gate holds only when both observations satisfy it. Nothing is measured in this file: it
// only reads what the instruments and the sealed files say and judges it against the contract figures below.
//
// CONTRACT FIGURES (DECLARED here, taken from the plan and never derived from the measurements they judge):
//   DEC-13   window 1280x1064 / DOM 1271x1064 / canvas 1264x1024 (1/6); window 1280x2128 / DOM 1271x2128 / canvas 1264x2080 (1/3)
//   DEC-4    exactly one WebGL2 context (the U-18 oracle accepts 0 as well: `<= 1`; opus-356 item 4 asks the certifier for `== 1`)
//   B-31     CLS 0; p95 frame work <= 16.6 ms; 0 unmounts; cards per view 1 (1/6) and 2 (1/3); DOM renderer; AAA failures 0
//   U-21     scrollback <= 2000 lines, drawer height 240, 56 panels, remounts <= 1
//
// PROVENANCE OF EVERY FIELD OF THE REPORT (published in `procedencia`, A-59):
//   viva          MEASURED   what the instrument reported in this run (each instrument carries its own second sources and
//                            publishes `acuerdo_entre_fuentes`; a report on which two sources disagree has null fields)
//   sellada       READ-BACK  the unit's sealed file, produced by an earlier run of the same instrument
//   cumple_*      JUDGED     the contract figure above compared with the observation; the comparison is made HERE
//   coinciden     JUDGED     the live and the sealed observation of every discrete field (equal values), the p95 excepted
//                            (a timing: only its threshold is compared)
// --mutante forwards ONE flag of the instrument's own contrast to the instrument that owns it (--envolver-if to medir-scroll,
// --sin-arranque-sincrono to medir-cls, --tokens fixture/ng/tokens/degradados.css to medir-contraste) and evaluates only the
// properties that instrument feeds. The counters are the instrument's; nothing is edited afterwards. The sealed observation is
// not compared in that mode (the run is a mutant on purpose).
//
// EVERY run, success or failure, writes ONE report of the SAME shape to stdout: ok, etapa, error, and null in every field
// that was not measured. jq: `null == 0` is false but `null <= 16.6` is true: a consumer keeps the exit code.
//
// Exit codes: 0 every property holds live and sealed · 1 measured and at least one property is violated ·
// 2 usage or precondition (bad argument, instrument or sealed file missing) · 3 an instrument did not produce a report.

import { execFile } from 'node:child_process';
import { readFile } from 'node:fs/promises';
import { existsSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const RAIZ = resolve(process.env.SRF_REPO || resolve(dirname(fileURLToPath(import.meta.url)), '..', '..'));
const ESQUEMA = 1;

// ---- contract figures ---------------------------------------------------------------------------------------------
const GEOMETRIA = {
  '1/6': { ventana: '1280x1064', dom: '1271x1064', lienzo: '1264x1024' },
  '1/3': { ventana: '1280x2128', dom: '1271x2128', lienzo: '1264x2080' },
};
const PRESUPUESTO_FRAME_MS = 16.6;
const TARJETAS_POR_VISTA = { '1/6': 1, '1/3': 2 };
const SCROLLBACK_MAX = 2000;
const ALTURA_CAJON = 240;
const PANELES = 56;
const REMONTAJES_MAX = 1;

// ---- the legs: instrument, fixed arguments, sealed file --------------------------------------------------------------
const HOJAS = {
  cls16: 'u18/cls-16.json',
  cls13: 'u18/cls-13.json',
  scroll16: 'u19/scroll-16.json',
  scroll13: 'u19/scroll-13.json',
  contraste: 'u20/contraste.json',
  terminal: 'u21/terminal.json',
};

const MUTANTES = {
  'envolver-if': { legs: ['scroll16', 'scroll13'], bandera: '--envolver-if', instrumento: 'medir-scroll.mjs' },
  'sin-arranque-sincrono': { legs: ['cls16', 'cls13'], bandera: '--sin-arranque-sincrono', instrumento: 'medir-cls.mjs' },
  'tokens-degradados': { legs: ['contraste'], bandera: '--tokens fixture/ng/tokens/degradados.css', instrumento: 'medir-contraste.mjs' },
};

function argumentosDeLeg(id, mutante) {
  const m = mutante ? MUTANTES[mutante] : null;
  const con = (banderas) => (m && m.legs.includes(id) ? [...banderas, ...m.bandera.split(' ')] : banderas);
  switch (id) {
    case 'cls16': return { script: 'medir-cls.mjs', args: con(['--modo', '1/6', '--json']) };
    case 'cls13': return { script: 'medir-cls.mjs', args: con(['--modo', '1/3', '--json']) };
    case 'scroll16': return { script: 'medir-scroll.mjs', args: con(['--tarjetas', '24', '--modo', '1/6', '--json']) };
    case 'scroll13': return { script: 'medir-scroll.mjs', args: con(['--tarjetas', '24', '--modo', '1/3', '--json']) };
    case 'contraste': {
      if (m && m.legs.includes(id)) return { script: 'medir-contraste.mjs', args: ['--tokens', 'fixture/ng/tokens/degradados.css', '--json'] };
      return { script: 'medir-contraste.mjs', args: ['--tokens', 'cockpit/frontend/src/app.css', '--json'] };
    }
    case 'terminal': return { script: 'medir-terminal.mjs', args: ['--paneles', String(PANELES), '--json'] };
    default: throw new RangeError(`unknown leg ${id}`);
  }
}

function uso(msg) {
  const cuerpo = 'usage: node scripts/ng/medir-cockpit.mjs [--json] [--desde-disco] [--evidencia <dir>] [--mutante envolver-if|sin-arranque-sincrono|tokens-degradados] [--plazo-leg-ms N]';
  process.stderr.write(`${msg ? `medir-cockpit: ${msg}\n` : ''}${cuerpo}\n`);
  process.exit(2);
}

function leerArgumentos(argv) {
  const o = { json: false, desdeDisco: false, evidencia: join(RAIZ, 'evidencia', 'v2'), mutante: null, plazoLegMs: 240000 };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    const valor = () => { if (i + 1 >= argv.length) uso(`${a} needs a value`); return argv[++i]; };
    if (a === '--json') o.json = true;
    else if (a === '--desde-disco') o.desdeDisco = true;
    else if (a === '--evidencia') o.evidencia = resolve(valor());
    else if (a === '--mutante') o.mutante = valor();
    else if (a === '--plazo-leg-ms') o.plazoLegMs = Number.parseInt(valor(), 10);
    else if (a === '-h' || a === '--help') uso('');
    else uso(`unknown argument ${JSON.stringify(a)}`);
  }
  if (o.mutante !== null && !(o.mutante in MUTANTES)) uso(`unknown mutant ${JSON.stringify(o.mutante)}`);
  if (o.mutante !== null && o.desdeDisco) uso('--mutante measures live: it cannot be combined with --desde-disco');
  if (!Number.isInteger(o.plazoLegMs) || o.plazoLegMs < 1000) uso('--plazo-leg-ms must be an integer >= 1000');
  return o;
}

// ---- running one instrument --------------------------------------------------------------------------------------------
function correrLeg(id, mutante, plazoMs) {
  const { script, args } = argumentosDeLeg(id, mutante);
  const ruta = join(RAIZ, 'scripts', 'ng', script);
  const t0 = Date.now();
  return new Promise((resolver) => {
    if (!existsSync(ruta)) {
      resolver({ id, script, args, rc: null, informe: null, duracion_ms: 0, error: `instrument missing: ${ruta}`, stderr: '' });
      return;
    }
    execFile(process.execPath, [ruta, ...args], { cwd: RAIZ, timeout: plazoMs, maxBuffer: 256 * 1024 * 1024, encoding: 'utf8' }, (err, stdout, stderr) => {
      let rc = 0;
      let error = null;
      if (err) {
        rc = typeof err.code === 'number' ? err.code : null;
        error = err.killed ? `killed after ${plazoMs} ms` : (typeof err.code === 'number' ? null : String(err.message).slice(0, 300));
      }
      let informe = null;
      try { informe = JSON.parse(stdout); } catch (e) { error = error ?? `stdout is not one JSON report: ${String(e.message).slice(0, 160)}`; }
      resolver({ id, script, args, rc, informe, duracion_ms: Date.now() - t0, error, stderr: String(stderr).slice(-400) });
    });
  });
}

async function leerHoja(dir, id) {
  const ruta = join(dir, HOJAS[id]);
  try {
    const cuerpo = await readFile(ruta, 'utf8');
    return { id, ruta, informe: JSON.parse(cuerpo), error: null };
  } catch (e) {
    return { id, ruta, informe: null, error: `${e.code || 'error'}: ${String(e.message).slice(0, 200)}` };
  }
}

// ---- judging -----------------------------------------------------------------------------------------------------------
const igual = (a, b) => a !== null && a !== undefined && a === b;
const numero = (v) => typeof v === 'number' && Number.isFinite(v);

/** Property table: each entry reads only the reports named in `necesita` and returns {cumple, valor}. cumple is null when a report is absent. */
const PROPIEDADES = [
  {
    id: 'instrumentos-de-acuerdo',
    gate: 'each instrument: ok, rc 0 and its own sources agree',
    necesita: ['cls16', 'cls13', 'scroll16', 'scroll13', 'contraste', 'terminal'],
    juzga: (r) => {
      const valor = {};
      for (const [k, inf] of Object.entries(r)) valor[k] = inf ? { ok: inf.ok, rc: inf.rc, acuerdo_entre_fuentes: inf.acuerdo_entre_fuentes } : null;
      return { cumple: Object.values(r).every((inf) => inf && inf.ok === true && inf.rc === 0 && inf.acuerdo_entre_fuentes !== false), valor };
    },
    discretos: () => ({}),
  },
  {
    id: 'geometria-1/6',
    gate: 'window 1280x1064, DOM 1271x1064, canvas 1264x1024 (DEC-13)',
    necesita: ['cls16', 'scroll16', 'terminal'],
    juzga: (r) => {
      const e = GEOMETRIA['1/6'];
      const valor = {
        cls: { ventana: r.cls16.ventana, dom: r.cls16.dom, lienzo: r.cls16.lienzo },
        scroll: { ventana: r.scroll16.ventana, dom: r.scroll16.dom, lienzo_de_tarjeta: r.scroll16.esperado ? r.scroll16.esperado.lienzo : null },
        terminal: { ventana: r.terminal.ventana, dom: r.terminal.dom },
      };
      const cumple = igual(valor.cls.ventana, e.ventana) && igual(valor.cls.dom, e.dom) && igual(valor.cls.lienzo, e.lienzo)
        && igual(valor.scroll.ventana, e.ventana) && igual(valor.scroll.dom, e.dom) && igual(valor.scroll.lienzo_de_tarjeta, e.lienzo)
        && igual(valor.terminal.ventana, e.ventana) && igual(valor.terminal.dom, e.dom);
      return { cumple, valor };
    },
    discretos: (r) => ({ cls: [r.cls16.ventana, r.cls16.dom, r.cls16.lienzo], scroll: [r.scroll16.ventana, r.scroll16.dom], terminal: [r.terminal.ventana, r.terminal.dom] }),
  },
  {
    id: 'geometria-1/3',
    gate: 'window 1280x2128, DOM 1271x2128, canvas 1264x2080 (DEC-13)',
    necesita: ['cls13', 'scroll13'],
    juzga: (r) => {
      const e = GEOMETRIA['1/3'];
      const valor = { cls: { ventana: r.cls13.ventana, dom: r.cls13.dom, lienzo: r.cls13.lienzo }, scroll: { ventana: r.scroll13.ventana, dom: r.scroll13.dom } };
      const cumple = igual(valor.cls.ventana, e.ventana) && igual(valor.cls.dom, e.dom) && igual(valor.cls.lienzo, e.lienzo)
        && igual(valor.scroll.ventana, e.ventana) && igual(valor.scroll.dom, e.dom);
      return { cumple, valor };
    },
    discretos: (r) => ({ cls: [r.cls13.ventana, r.cls13.dom, r.cls13.lienzo], scroll: [r.scroll13.ventana, r.scroll13.dom] }),
  },
  {
    id: 'cls-0',
    gate: 'cumulative layout shift 0 in both modes (B-31)',
    necesita: ['cls16', 'cls13'],
    juzga: (r) => ({ cumple: r.cls16.cls === 0 && r.cls13.cls === 0, valor: { '1/6': r.cls16.cls, '1/3': r.cls13.cls } }),
    discretos: (r) => ({ cls: [r.cls16.cls, r.cls13.cls] }),
  },
  {
    id: 'un-contexto-gl',
    gate: 'exactly one WebGL2 context in both modes (DEC-4; the U-18 oracle only asks <= 1)',
    necesita: ['cls16', 'cls13'],
    juzga: (r) => ({ cumple: r.cls16.contextos_webgl === 1 && r.cls13.contextos_webgl === 1, valor: { '1/6': r.cls16.contextos_webgl, '1/3': r.cls13.contextos_webgl } }),
    discretos: (r) => ({ contextos: [r.cls16.contextos_webgl, r.cls13.contextos_webgl] }),
  },
  {
    id: 'p95-frame',
    gate: `p95 of the frame's main-thread work <= ${PRESUPUESTO_FRAME_MS} ms in both modes (B-31)`,
    necesita: ['scroll16', 'scroll13'],
    juzga: (r) => ({
      cumple: numero(r.scroll16.p95_frame_ms) && r.scroll16.p95_frame_ms <= PRESUPUESTO_FRAME_MS && numero(r.scroll13.p95_frame_ms) && r.scroll13.p95_frame_ms <= PRESUPUESTO_FRAME_MS,
      valor: { '1/6': r.scroll16.p95_frame_ms, '1/3': r.scroll13.p95_frame_ms },
    }),
    discretos: () => ({}),
  },
  {
    id: 'cero-desmontajes',
    gate: '0 unmounts of a live surface across paging, keys, fold and screen swap (B-31, INV-12)',
    necesita: ['scroll16', 'scroll13'],
    juzga: (r) => ({ cumple: r.scroll16.desmontajes === 0 && r.scroll13.desmontajes === 0, valor: { '1/6': r.scroll16.desmontajes, '1/3': r.scroll13.desmontajes } }),
    discretos: (r) => ({ desmontajes: [r.scroll16.desmontajes, r.scroll13.desmontajes] }),
  },
  {
    id: 'tarjetas-por-vista',
    gate: 'cards per view 1 in 1/6 and 2 in 1/3 (U-19)',
    necesita: ['scroll16', 'scroll13'],
    juzga: (r) => ({
      cumple: r.scroll16.tarjetas_por_vista === TARJETAS_POR_VISTA['1/6'] && r.scroll13.tarjetas_por_vista === TARJETAS_POR_VISTA['1/3'],
      valor: { '1/6': r.scroll16.tarjetas_por_vista, '1/3': r.scroll13.tarjetas_por_vista },
    }),
    discretos: (r) => ({ tarjetas: [r.scroll16.tarjetas_por_vista, r.scroll13.tarjetas_por_vista] }),
  },
  {
    id: 'renderer-dom',
    gate: `xterm DOM renderer, scrollback <= ${SCROLLBACK_MAX}, drawer ${ALTURA_CAJON}, ${PANELES} panels, remounts <= ${REMONTAJES_MAX} (DEC-5, U-21)`,
    necesita: ['terminal'],
    juzga: (r) => {
      const t = r.terminal;
      const valor = { renderer: t.renderer, scrollback: t.scrollback, altura_cajon: t.altura_cajon, paneles: t.paneles, remontajes_max: t.remontajes_max };
      const cumple = t.renderer === 'dom' && numero(t.scrollback) && t.scrollback <= SCROLLBACK_MAX && t.altura_cajon === ALTURA_CAJON && t.paneles === PANELES
        && numero(t.remontajes_max) && t.remontajes_max <= REMONTAJES_MAX;
      return { cumple, valor };
    },
    discretos: (r) => ({ terminal: [r.terminal.renderer, r.terminal.scrollback, r.terminal.altura_cajon, r.terminal.paneles, r.terminal.remontajes_max] }),
  },
  {
    id: 'aaa',
    gate: 'AAA failures 0 over the declared foreground/background pairs of the tokens (B-31, U-20)',
    necesita: ['contraste'],
    juzga: (r) => ({ cumple: numero(r.contraste.aaa_fallos) && r.contraste.aaa_fallos === 0, valor: { aaa_fallos: r.contraste.aaa_fallos } }),
    discretos: (r) => ({ aaa: [r.contraste.aaa_fallos] }),
  },
];

/** Evaluates every property whose reports are all present; the others come back with cumple null and the names missing. */
function juzgar(informes) {
  const salida = {};
  for (const p of PROPIEDADES) {
    const faltan = p.necesita.filter((k) => !informes[k]);
    if (faltan.length > 0) { salida[p.id] = { cumple: null, valor: null, faltan, discretos: null }; continue; }
    const r = Object.fromEntries(p.necesita.map((k) => [k, informes[k]]));
    const { cumple, valor } = p.juzga(r);
    salida[p.id] = { cumple, valor, faltan: [], discretos: p.discretos(r) };
  }
  return salida;
}

const mismo = (a, b) => JSON.stringify(a) === JSON.stringify(b);

function informeVacio(o, etapa) {
  return {
    ok: false, etapa, error: null, esquema: ESQUEMA, modo: o.desdeDisco ? 'disco' : 'vivo', mutante: o.mutante,
    cumple_todo: null, cumple_vivo: null, cumple_sellado: null, propiedades: [], tramos: [], hojas_selladas: [], no_probado: [], duracion_ms: 0,
  };
}

const NO_PROBADO = [
  'the real Wails/WebKitGTK window at 1280x1064 is the F-14 operator smoke check, not measured here (DEC-18: Playwright Chromium headless is a measurement proxy)',
  'no live terminal WebSocket fabric: the U-21 leg talks to a stand-in server, cmd/srf-terminal has no serving verb',
  'the U-20 hot switch (medir-contraste --conmutar-slot) needs a live capsule and is not part of this aggregate',
  'p95 is the main-thread work of a frame in a 60 Hz headless Chromium, not the cadence of the 119.88 Hz panel',
];

async function principal() {
  const t0 = Date.now();
  const o = leerArgumentos(process.argv.slice(2));
  const informe = informeVacio(o, 'inicio');
  informe.no_probado = NO_PROBADO;
  const imprimir = (rc) => {
    informe.duracion_ms = Date.now() - t0;
    if (o.json) process.stdout.write(`${JSON.stringify(informe)}\n`);
    else process.stdout.write(textoHumano(informe));
    process.exit(rc);
  };

  // sealed observation
  informe.etapa = 'lectura-de-hojas';
  const ids = Object.keys(HOJAS);
  const hojas = await Promise.all(ids.map((id) => leerHoja(o.evidencia, id)));
  informe.hojas_selladas = hojas.map((h) => ({ id: h.id, ruta: h.ruta, leida: h.informe !== null, error: h.error }));
  const sellado = Object.fromEntries(hojas.map((h) => [h.id, h.informe]));
  if (!o.mutante && hojas.some((h) => h.informe === null)) {
    informe.error = `sealed unit file unreadable: ${hojas.filter((h) => h.informe === null).map((h) => `${h.ruta} (${h.error})`).join('; ')}`;
    imprimir(2);
    return;
  }

  // live observation
  let vivo = null;
  const legsVivos = o.desdeDisco ? [] : (o.mutante ? MUTANTES[o.mutante].legs : ids);
  if (legsVivos.length > 0) {
    informe.etapa = 'medicion-viva';
    const tramos = [];
    for (const id of legsVivos) tramos.push(await correrLeg(id, o.mutante, o.plazoLegMs));
    informe.tramos = tramos.map((t) => ({ id: t.id, instrumento: t.script, argumentos: t.args, rc: t.rc, duracion_ms: t.duracion_ms, error: t.error, informe_leido: t.informe !== null }));
    vivo = Object.fromEntries(tramos.map((t) => [t.id, t.informe]));
    const rotos = tramos.filter((t) => t.informe === null);
    if (rotos.length > 0) {
      informe.error = `instrument without a report: ${rotos.map((t) => `${t.id} rc=${t.rc} ${t.error || ''} ${t.stderr.trim()}`).join(' | ').slice(0, 900)}`;
      imprimir(3);
      return;
    }
  }

  informe.etapa = 'juicio';
  const propViva = vivo ? juzgar(vivo) : null;
  const propSellada = o.mutante ? null : juzgar(sellado);
  const propiedades = [];
  for (const p of PROPIEDADES) {
    const v = propViva ? propViva[p.id] : null;
    const s = propSellada ? propSellada[p.id] : null;
    const medidaViva = v && v.cumple !== null;
    const coinciden = v && s && v.discretos && s.discretos ? mismo(v.discretos, s.discretos) : null;
    propiedades.push({
      id: p.id, gate: p.gate,
      valor_vivo: v ? v.valor : null, cumple_vivo: v ? v.cumple : null,
      valor_sellado: s ? s.valor : null, cumple_sellado: s ? s.cumple : null,
      coinciden,
      medida: medidaViva || (s && s.cumple !== null) ? true : false,
    });
  }
  informe.propiedades = propiedades;
  const evaluadas = propiedades.filter((p) => p.medida);
  if (o.mutante) {
    informe.cumple_vivo = evaluadas.length > 0 && evaluadas.every((p) => p.cumple_vivo === true);
    informe.cumple_sellado = null;
    informe.cumple_todo = informe.cumple_vivo;
  } else {
    const paraJuzgar = o.desdeDisco ? propiedades : propiedades.filter((p) => p.medida);
    informe.cumple_vivo = o.desdeDisco ? null : (paraJuzgar.length === PROPIEDADES.length && paraJuzgar.every((p) => p.cumple_vivo === true));
    informe.cumple_sellado = paraJuzgar.length === PROPIEDADES.length && paraJuzgar.every((p) => p.cumple_sellado === true);
    informe.cumple_todo = (o.desdeDisco ? informe.cumple_sellado : (informe.cumple_vivo && informe.cumple_sellado && paraJuzgar.every((p) => p.coinciden !== false)));
  }
  informe.ok = true;
  informe.etapa = 'completa';
  imprimir(informe.cumple_todo === true ? 0 : 1);
}

function textoHumano(inf) {
  const filas = inf.propiedades.map((p) => `${p.id.padEnd(24)} viva=${String(p.cumple_vivo).padEnd(5)} sellada=${String(p.cumple_sellado).padEnd(5)} coinciden=${p.coinciden}`);
  return `medir-cockpit modo=${inf.modo} mutante=${inf.mutante} etapa=${inf.etapa} cumple_todo=${inf.cumple_todo}${inf.error ? `\nerror: ${inf.error}` : ''}\n${filas.join('\n')}\n`;
}

principal().catch((e) => {
  const informe = {
    ok: false, etapa: 'excepcion', error: String(e && e.stack ? e.stack : e).slice(0, 600), esquema: ESQUEMA, cumple_todo: null,
    cumple_vivo: null, cumple_sellado: null, propiedades: [], tramos: [], hojas_selladas: [], no_probado: NO_PROBADO,
  };
  process.stdout.write(`${JSON.stringify(informe)}\n`);
  process.exit(3);
});
