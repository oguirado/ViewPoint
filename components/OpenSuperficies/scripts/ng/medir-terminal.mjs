#!/usr/bin/env node
// scripts/ng/medir-terminal.mjs — U-21 instrument: the agents drawer, its xterm.js terminals and their stream.
//
//   node scripts/ng/medir-terminal.mjs --paneles N [--renderer dom|canvas|webgl] [--json]
//        [--scrollback-pedido N] [--espera-listo-ms 90000] [--reposo-ms 500]
//        [--plazo-lectura-ms 5000] [--plazo-total-ms 300000]
//        [--inyectar-scrollback ID=N] [--inyectar-congelacion]
//
// WHAT IT DOES. It builds a harness page into a private temporary directory (never cockpit/frontend/dist: U-18's tests
// and medir-cls.mjs read it) that mounts the real cockpit/frontend/src/lib/terminal/CajonAgentes.svelte with N agents,
// each bound to a stream on a STAND-IN PTY server this file starts on 127.0.0.1 port 0 (no terminal WebSocket server
// exists in the tree yet; cmd/srf-terminal has no serving verb). The stand-in speaks the SRF2 binary framing that
// wsstream.ts defines and decodes it with a codec written HERE, independently of the client's. The harness is opened in
// Playwright 1.58.0 Chromium headless (DEC-18) with the viewport fixed BEFORE navigation and these legs run on the live page:
//
//   montaje     the N terminals are mounted and connected; the counters and the CDP DOM tracking start here
//   entrada     keystrokes travel client to server as SRF2 frames and come back as output
//   churn       three collapse/expand cycles, every tab activated, the agents array replaced, two streams cut and reconnected
//   cajon       the drawer's height open and collapsed, and the size of the surface behind it
//   oculto      a collapsed drawer queues bytes and schedules no frame; flow control pauses the server; reopening drains
//   inactivo    the same for a tab that is not the active one
//   texto       past the 56th instance a pane is plain text and still shows its stream once it is shown
//   foco        the three focus passes win the focus back from an element that steals it twice
//   raton       the right button is swallowed at capture phase, the left one still reaches xterm
//   ime         a composition is delivered once and the hidden textarea is cleared
//   scrollback  more lines than the limit are written through one stream, and then straight into EVERY interactive
//               terminal; the lines each one retains are counted (the oracle's field is the largest of them)
//   pestanas    the arrow keys move between tabs, with wrap-around
//   salida      an EXIT frame writes the [process exited] line into the terminal and ends the session for good
//   hostil      a control frame that is JSON but not an object is counted and dropped: no uncaught page error
//   desmontaje  removing an agent unmounts its panel and closes its WebSocket
//   capa_tardia a canvas layer that appears after the mount fails the panel (line 2 of the guard)
//
// EVERY evaluate of the failure path has a deadline (--plazo-lectura-ms) and the whole run has its own bound
// (--plazo-total-ms): a frozen page ends with a report, not with a hang.
//
// --renderer canvas|webgl is the CONTRAST. The harness then asks every terminal for a renderer addon. The addon packages
// are not installed, so the harness supplies STAND-INS that put the canvas layers of the real renderers into
// .xterm-screen, and none of them is named after the thing the guard looks for: the published bundles are minified
// (their classes are called `a`, `l`, `p`), so the stand-ins are a single-letter class, an anonymous class, a subclass of
// an unrelated built-in and a class exported under another name; ONE stand-in named CanvasAddon / WebglAddon is kept
// as a labelled control. The guard (termguard.ts) is deny-by-default and structural, so it refuses all of them for the
// same reason: none is an addon the drawer created. What the report says the renderer IS is read from the DOM of each
// terminal (any canvas anywhere under it), never from the flag, and the run FAILS unless every request was refused.
//
// FAULT INJECTION (test hooks, never part of the oracle or the contrast; a run that used one says so in `inyeccion`):
//   --inyectar-scrollback srf-ng-9=50000   raises ONE live terminal's scrollback option after it was mounted, a defect
//                                          the construction-time clamp cannot see; the report must show it
//   --inyectar-congelacion                 makes the page's main thread spin: the frozen WebKit the unit exists to avoid
//
// PROVENANCE OF EVERY FIELD (published in the report's `procedencia`, A-59):
//   ventana, dom_solicitado      DECLARED   the tiling arithmetic and the viewport this harness asks the browser to emulate
//   dom                          READ-BACK  innerWidth x innerHeight of the emulated viewport (NOT an independent measurement)
//   renderer                     MEASURED   the DOM of every interactive terminal (.xterm-rows vs canvas layers in
//                                           .xterm-screen), read by this instrument; second sources: the component's own
//                                           data-renderer and CDP DOM.querySelectorAll counts of the same structure
//   remontajes_max               MEASURED   .xterm elements inserted into each panel host (MutationObserver in the page,
//                                           minus the first mount); second sources: the component's mount counter and, for the
//                                           window after the mount leg, the CDP DOM.childNodeInserted events
//   scrollback                   MEASURED   the MAXIMUM, over ALL interactive terminals, of the lines the xterm buffer
//                                           retains after more than the limit was written straight into it (buffer length
//                                           minus rows); second sources per terminal: the number of the first retained
//                                           line, the terminal's own option and the component's data-scrollback
//   altura_cajon                 MEASURED   the open drawer's border-box height (getBoundingClientRect once the animations
//                                           finished); second source: CDP DOM.getBoxModel
//   rechazos_de_addon            MEASURED   refusals the guard recorded in the page; second sources: the console warnings
//                                           the browser reported to Playwright, the number of requests the harness made
//                                           (must be equal) and the number of stand-in addons whose activate() ran (0)
// The real Wails/WebKitGTK window and a live PTY fabric are never exercised here (see no_probado).
//
// EVERY run, success or failure, writes ONE report of the SAME shape to stdout (A-53): ok, etapa, error, rc, and null in
// every field that was not measured or on which two sources disagree. A consequence for jq: `null <= 1` is true, so a
// consumer keeps the exit code (bash -o pipefail) and reads the other fields as well.
//
// Exit codes: 0 measured and every source agrees (whatever the values are: the properties are judged by the consumer) ·
// 1 anything else (browser cannot start, protocol failure) · 2 usage or instrument precondition · 3 two sources of one
// field disagree · 4 the page never reached its ready contract, threw, or a leg could not run ·
// 129 / 130 / 143 killed by SIGHUP / SIGINT / SIGTERM (the private temporary root and the browser are removed first).

import { createRequire } from 'node:module';
import { createServer } from 'node:http';
import { createHash } from 'node:crypto';
import { mkdir, readFile, writeFile } from 'node:fs/promises';
import { spawnSync } from 'node:child_process';
import { existsSync, mkdirSync, mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { extname, join, normalize, resolve, dirname, sep } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const RAIZ = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const FRONTEND = join(RAIZ, 'cockpit', 'frontend');
const TERMINAL = join(FRONTEND, 'src', 'lib', 'terminal');
const PLAYWRIGHT_PIN = '1.58.0';
const XTERM_PIN = '5.5.0';

// ---- host facts and declared limits — independent of src/, taken from the specification -----------------------
const PANTALLA = { w: 3840, h: 2160 };
const COLUMNAS = 3;
const FILAS = 2;
const BARRA_GNOME_H = 32;
const MARGEN_WM_W = 9;
/** Spec 6 and --drawer-h (1.5): the open drawer. */
export const ALTURA_CAJON_DECLARADA = 240;
/** Spec 6: the collapsed grab handle. */
export const ALTURA_ASA_DECLARADA = 12;
/** DEC-5: the scrollback limit. */
export const SCROLLBACK_DECLARADO = 2000;
/** DEC-5: the red line of interactive instances. */
export const PANELES_INTERACTIVOS_DECLARADOS = 56;
/** SUB_04 1.4 [TARGET]: the client asks the server to pause when more than this many bytes wait unparsed. */
const ALTA_MARCA_DECLARADA = 2 * 1024 * 1024;
/** Spec 4: the three-pass focus transfer. */
export const PASOS_DE_FOCO_DECLARADOS = [0, 120, 400];
/** The bound on the bytes handed to xterm in one write (one animation frame); wsstream.ts declares its own, this is the instrument's. */
export const LOTE_MAX_DECLARADO = 256 * 1024;
/** How long the browser gets to close before the processes of the run are killed. */
const PLAZO_DE_CIERRE_MS = 15000;
/** The code this harness puts in the EXIT frame it sends. */
const CODIGO_DE_SALIDA = 7;
/** How many numbered lines beyond the limit are written to fill a buffer. */
const MARGEN_DE_LINEAS = 600;
const TAM_CABECERA = 32;
const TROZO_SERVIDOR = 64 * 1024;
/** The stand-in writes PTY output at a bounded rate (a PTY is not infinitely fast): this many bytes, then a pause of RITMO_MS. */
const RITMO_BYTES = 256 * 1024;
const RITMO_MS = 5;

/** Window (outer) and DOM (inner) viewport of the 1/6 tile, from the tiling arithmetic alone. */
export function derivarViewport() {
  const ventana = { w: PANTALLA.w / COLUMNAS, h: (PANTALLA.h - BARRA_GNOME_H) / FILAS };
  return { ventana, dom: { w: ventana.w - MARGEN_WM_W, h: ventana.h } };
}

// ---- the SRF2 codec, written here and not borrowed from wsstream.ts --------------------------------------------
export function codificarTramaSrf2(cuerpo, seq, ms, stride = 1) {
  const b = Buffer.alloc(TAM_CABECERA + cuerpo.length);
  b.write('SRF2', 0, 'latin1');
  b.writeUInt16LE(1, 4);
  b.writeUInt16LE(stride, 6);
  b.writeUInt32LE(cuerpo.length / stride, 8);
  b.writeUInt32LE(seq >>> 0, 12);
  b.writeUInt32LE(Math.floor(ms) >>> 0, 16);
  Buffer.from(cuerpo).copy(b, TAM_CABECERA);
  return b;
}

/** Throws Error on anything that is not a well-formed frame; returns the fields and a copy of the body. */
export function decodificarTramaSrf2(buf) {
  if (buf.length < TAM_CABECERA) throw new Error(`frame of ${buf.length} bytes is shorter than the header`);
  if (buf.toString('latin1', 0, 4) !== 'SRF2') throw new Error('bad magic');
  const version = buf.readUInt16LE(4);
  if (version !== 1) throw new Error(`unsupported version ${version}`);
  const stride = buf.readUInt16LE(6);
  const cuenta = buf.readUInt32LE(8);
  if (stride < 1) throw new Error('stride 0');
  if (!buf.subarray(20, TAM_CABECERA).every((x) => x === 0)) throw new Error('reserved bytes are not zero');
  if (cuenta * stride !== buf.length - TAM_CABECERA) throw new Error(`count ${cuenta} x stride ${stride} does not match ${buf.length - TAM_CABECERA} body bytes`);
  return { version, stride, cuenta, seq: buf.readUInt32LE(12), ms: buf.readUInt32LE(16), cuerpo: Buffer.from(buf.subarray(TAM_CABECERA)) };
}

// ---- the stand-in PTY server: RFC 6455 over node:http, SRF2 frames, PAUSE_FLOW / RESUME_FLOW / RESIZE / EXIT ------
const GUID_WS = '258EAFA5-E914-47DA-95CA-C5AB0DC85B11';

function tramaWs(opcode, payload) {
  const n = payload.length;
  let cab;
  if (n < 126) cab = Buffer.from([0x80 | opcode, n]);
  else if (n < 65536) {
    cab = Buffer.alloc(4);
    cab[0] = 0x80 | opcode;
    cab[1] = 126;
    cab.writeUInt16BE(n, 2);
  } else {
    cab = Buffer.alloc(10);
    cab[0] = 0x80 | opcode;
    cab[1] = 127;
    cab.writeBigUInt64BE(BigInt(n), 2);
  }
  return Buffer.concat([cab, payload]);
}

/** Splits the masked client frames of a byte stream into messages; continuation frames are joined. */
class LectorWs {
  constructor() {
    this.buf = Buffer.alloc(0);
    this.partes = [];
    this.opcodeInicial = 0;
  }
  empujar(chunk) {
    this.buf = Buffer.concat([this.buf, chunk]);
    const mensajes = [];
    for (;;) {
      const b = this.buf;
      if (b.length < 2) break;
      const fin = (b[0] & 0x80) !== 0;
      const opcode = b[0] & 0x0f;
      const enmascarado = (b[1] & 0x80) !== 0;
      let largo = b[1] & 0x7f;
      let pos = 2;
      if (largo === 126) {
        if (b.length < 4) break;
        largo = b.readUInt16BE(2);
        pos = 4;
      } else if (largo === 127) {
        if (b.length < 10) break;
        largo = Number(b.readBigUInt64BE(2));
        pos = 10;
      }
      if (enmascarado) pos += 4;
      if (b.length < pos + largo) break;
      const carga = Buffer.from(b.subarray(pos, pos + largo));
      if (enmascarado) {
        const clave = b.subarray(pos - 4, pos);
        for (let i = 0; i < carga.length; i++) carga[i] ^= clave[i % 4];
      }
      this.buf = b.subarray(pos + largo);
      if (opcode >= 0x8) {
        mensajes.push({ opcode, carga });
      } else if (opcode === 0) {
        this.partes.push(carga);
        if (fin) {
          mensajes.push({ opcode: this.opcodeInicial, carga: Buffer.concat(this.partes) });
          this.partes = [];
        }
      } else if (fin) {
        mensajes.push({ opcode, carga });
      } else {
        this.opcodeInicial = opcode;
        this.partes = [carga];
      }
    }
    return mensajes;
  }
}

class ConexionPty {
  constructor(id, numero, socket) {
    this.id = id;
    this.numero = numero; // 1 for the first connection of this id, 2 after a reconnect, ...
    this.socket = socket;
    this.lector = new LectorWs();
    this.seq = 0;
    this.t0 = Date.now();
    this.pausado = false;
    this.cola = [];
    this.esperandoDrain = false;
    this.programado = null;
    this.cerrada = false;
    this.entradas = [];
    this.seqsEntrada = [];
    this.dims = null;
    this.pausas = 0;
    this.reanudaciones = 0;
    this.bytesEnviados = 0;
    this.tramasEnviadas = 0;
    this.tramasRecibidas = 0;
    this.tramasInvalidasRecibidas = 0;
    this.controlRecibido = [];
    this.errores = [];
    socket.on('data', (chunk) => this.alDatos(chunk));
    socket.on('close', () => {
      this.cerrada = true;
      if (this.programado !== null) clearTimeout(this.programado);
      this.programado = null;
    });
    socket.on('error', (e) => {
      this.errores.push(e.message);
      this.cerrada = true;
    });
  }

  alDatos(chunk) {
    for (const { opcode, carga } of this.lector.empujar(chunk)) {
      if (opcode === 0x2) {
        this.tramasRecibidas++;
        try {
          const t = decodificarTramaSrf2(carga);
          this.entradas.push(t.cuerpo);
          this.seqsEntrada.push(t.seq);
          this.enviar(t.cuerpo); // a PTY echoes what it is typed
        } catch {
          this.tramasInvalidasRecibidas++;
        }
      } else if (opcode === 0x1) {
        let msg = null;
        try {
          msg = JSON.parse(carga.toString('utf8'));
        } catch {
          this.tramasInvalidasRecibidas++;
        }
        if (msg !== null) {
          this.controlRecibido.push(msg);
          if (msg.type === 'PAUSE_FLOW') {
            if (!this.pausado) this.pausas++;
            this.pausado = true;
          } else if (msg.type === 'RESUME_FLOW') {
            if (this.pausado) this.reanudaciones++;
            this.pausado = false;
            this.vaciar();
          } else if (msg.type === 'RESIZE') {
            this.dims = { cols: msg.cols, rows: msg.rows };
          }
        }
      } else if (opcode === 0x9) {
        this.escribir(tramaWs(0xa, carga));
      } else if (opcode === 0x8) {
        this.cerrar(1000);
      }
    }
  }

  escribir(buf) {
    if (this.cerrada || this.socket.destroyed) return true;
    return this.socket.write(buf);
  }

  /** PTY output: cut into frames of at most TROZO_SERVIDOR bytes, held back while the client has paused the flow. */
  enviar(bytes) {
    const b = Buffer.isBuffer(bytes) ? bytes : Buffer.from(bytes);
    for (let o = 0; o < b.length; o += TROZO_SERVIDOR) this.cola.push(b.subarray(o, o + TROZO_SERVIDOR));
    this.vaciar();
  }

  vaciar() {
    if (this.programado !== null) return; // a paced tick is already waiting
    let escritos = 0;
    while (!this.pausado && !this.cerrada && this.cola.length > 0 && !this.esperandoDrain) {
      if (escritos >= RITMO_BYTES) {
        this.programado = setTimeout(() => {
          this.programado = null;
          this.vaciar();
        }, RITMO_MS);
        return;
      }
      const trozo = this.cola.shift();
      escritos += trozo.length;
      const ok = this.escribir(tramaWs(0x2, codificarTramaSrf2(trozo, this.seq++, Date.now() - this.t0)));
      this.bytesEnviados += trozo.length;
      this.tramasEnviadas++;
      if (!ok) {
        this.esperandoDrain = true;
        this.socket.once('drain', () => {
          this.esperandoDrain = false;
          this.vaciar();
        });
      }
    }
  }

  enviarControl(obj) {
    this.escribir(tramaWs(0x1, Buffer.from(JSON.stringify(obj))));
  }

  enviarCrudo(carga) {
    this.escribir(tramaWs(0x2, Buffer.from(carga)));
  }

  cerrar(codigo) {
    if (this.cerrada) return;
    const c = Buffer.alloc(2);
    c.writeUInt16BE(codigo, 0);
    this.escribir(tramaWs(0x8, c));
    this.socket.end();
    this.cerrada = true;
  }

  resumen() {
    return {
      id: this.id,
      numero: this.numero,
      cerrada: this.cerrada,
      pausado: this.pausado,
      pausas: this.pausas,
      reanudaciones: this.reanudaciones,
      bytes_enviados: this.bytesEnviados,
      tramas_enviadas: this.tramasEnviadas,
      tramas_recibidas: this.tramasRecibidas,
      tramas_invalidas_recibidas: this.tramasInvalidasRecibidas,
      bytes_de_entrada: this.entradas.reduce((s, e) => s + e.length, 0),
      seqs_de_entrada: [...this.seqsEntrada],
      errores_de_socket: [...this.errores],
      dims: this.dims,
    };
  }
}

/** Starts the stand-in on 127.0.0.1 port 0 and returns its control surface. `cerrar()` stops it and drops every socket. */
export async function crearServidorPty() {
  const historial = new Map(); // id -> ConexionPty[] (every connection ever made, closed ones included)
  const sockets = new Set();
  const http = createServer((req, res) => {
    res.writeHead(404, { 'content-type': 'text/plain' });
    res.end('not found');
  });
  http.on('upgrade', (req, socket) => {
    const m = /^\/pty\/([A-Za-z0-9_.-]+)$/.exec(new URL(req.url, 'http://127.0.0.1').pathname);
    const clave = req.headers['sec-websocket-key'];
    if (m === null || String(req.headers.upgrade).toLowerCase() !== 'websocket' || typeof clave !== 'string') {
      socket.end('HTTP/1.1 400 Bad Request\r\nconnection: close\r\n\r\n');
      return;
    }
    const aceptado = createHash('sha1').update(clave + GUID_WS).digest('base64');
    socket.write(`HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: ${aceptado}\r\n\r\n`);
    sockets.add(socket);
    socket.on('close', () => sockets.delete(socket));
    const id = m[1];
    const lista = historial.get(id) ?? [];
    historial.set(id, lista);
    const con = new ConexionPty(id, lista.length + 1, socket);
    lista.push(con);
    con.enviar(Buffer.from(`srf stand-in ${id} ready\r\n$ `));
  });
  await new Promise((ok, mal) => {
    http.once('error', mal);
    http.listen(0, '127.0.0.1', ok);
  });
  const viva = (id) => (historial.get(id) ?? []).filter((c) => !c.cerrada);
  const ultima = (id) => {
    const l = historial.get(id) ?? [];
    return l.length > 0 ? l[l.length - 1] : null;
  };
  return {
    puerto: http.address().port,
    /** Connections that are open now (all ids, or one). */
    abiertas: (id) => (id === undefined ? [...historial.values()].flat().filter((c) => !c.cerrada).length : viva(id).length),
    /** Body bytes the server wrote to an id over every connection it ever had (the client counts across reconnects too). */
    bytesEnviadosA: (id) => (historial.get(id) ?? []).reduce((s, c) => s + c.bytesEnviados, 0),
    /** Connections ever accepted for an id, closed ones included. */
    aceptadas: (id) => (historial.get(id) ?? []).length,
    conexion: ultima,
    historial: (id) => [...(historial.get(id) ?? [])],
    enviar(id, bytes) {
      const c = ultima(id);
      if (c === null) throw new Error(`no connection for ${id}`);
      c.enviar(bytes);
    },
    enviarControl(id, obj) {
      const c = ultima(id);
      if (c === null) throw new Error(`no connection for ${id}`);
      c.enviarControl(obj);
    },
    enviarCrudo(id, carga) {
      const c = ultima(id);
      if (c === null) throw new Error(`no connection for ${id}`);
      c.enviarCrudo(carga);
    },
    cerrarConexion(id, codigo = 1012) {
      const c = ultima(id);
      if (c === null) throw new Error(`no connection for ${id}`);
      c.cerrar(codigo);
    },
    async cerrar() {
      for (const s of sockets) s.destroy();
      await new Promise((ok) => http.close(ok));
    },
  };
}

// ---- pure helpers (exported: the tests exercise them with hand-made inputs) -------------------------------------
/** What one terminal renders with, from the structure of its DOM: canvas layers win over the DOM renderer's rows. */
export function decidirRendererLeido(filasDom, lienzos) {
  if (lienzos > 0) return 'canvas';
  return filasDom > 0 ? 'dom' : 'desconocido';
}

/** The renderer of a set of terminals: the one they all use, `mixto` when they differ, `ninguno` when there is none. */
export function rendererDeConjunto(lecturas) {
  const distintos = new Set(lecturas);
  if (distintos.size === 0) return 'ninguno';
  return distintos.size === 1 ? [...distintos][0] : 'mixto';
}

/** Mounts of a panel beyond its first, per panel and the largest. `montajes` maps panel id to the number of mounts. */
export function remontajesDe(montajes) {
  const valores = Object.values(montajes);
  if (valores.length === 0) return { montajes_max: null, remontajes_max: null, remontajes_total: null };
  const max = Math.max(...valores);
  return { montajes_max: max, remontajes_max: max - 1, remontajes_total: valores.reduce((s, v) => s + Math.max(0, v - 1), 0) };
}

/** Compares two per-panel mount maps; returns one text per panel on which they differ. */
export function compararMontajes(a, b, nombreA, nombreB) {
  const salida = [];
  for (const id of [...new Set([...Object.keys(a), ...Object.keys(b)])].sort()) {
    if ((a[id] ?? 0) !== (b[id] ?? 0)) salida.push(`mounts of ${id}: ${nombreA} ${a[id] ?? 0} vs ${nombreB} ${b[id] ?? 0}`);
  }
  return salida;
}

/** Lines the buffer retains beyond the visible rows, and whether the first retained line's number agrees with that count. */
export function retencionDeBuffer({ largo, filas, primeraLinea, escritas }) {
  const retenidas = largo - filas;
  const m = /^linea (\d+)$/.exec(primeraLinea ?? '');
  const numeroPrimera = m === null ? null : Number(m[1]);
  // `escritas` numbered lines plus the blank cursor line fill the buffer from the end: the first retained numbered line
  // is escritas + 2 - (rows + retained), when the buffer is full
  const esperadaPrimera = escritas + 2 - largo;
  return { retenidas, numero_primera: numeroPrimera, esperada_primera: esperadaPrimera, coincide: numeroPrimera === esperadaPrimera };
}

/**
 * The oracle's scrollback. `lecturas` has one entry per interactive terminal ({id, largo, filas, primeraLinea, opcion,
 * atributo}); `escritas` is the number of numbered lines written into each. The field is the MOST lines any terminal
 * retained: one terminal with a larger buffer than the limit is enough to move it.
 */
export function resumirScrollback(lecturas, escritas) {
  const filas = lecturas.map((l) => ({ id: l.id, ...retencionDeBuffer({ largo: l.largo, filas: l.filas, primeraLinea: l.primeraLinea, escritas }), opcion: l.opcion, atributo: l.atributo }));
  const desacuerdos = [];
  for (const f of filas) {
    if (f.opcion <= SCROLLBACK_DECLARADO && f.retenidas !== f.opcion) desacuerdos.push(`scrollback of ${f.id}: retained ${f.retenidas} vs option ${f.opcion}`);
    if (!f.coincide) desacuerdos.push(`scrollback of ${f.id}: ${f.retenidas} lines retained but the first is number ${f.numero_primera}, expected ${f.esperada_primera}`);
    if (f.atributo !== f.opcion) desacuerdos.push(`scrollback of ${f.id}: the component publishes ${f.atributo}, the terminal option is ${f.opcion}`);
  }
  if (filas.length === 0) return { terminales: 0, retenidas_max: null, retenidas_min: null, opcion_max: null, atributo_max: null, coinciden: 0, mayor: null, desacuerdos };
  const mayor = filas.reduce((m, f) => (f.retenidas > m.retenidas ? f : m), filas[0]);
  return {
    terminales: filas.length,
    retenidas_max: mayor.retenidas,
    retenidas_min: Math.min(...filas.map((f) => f.retenidas)),
    opcion_max: Math.max(...filas.map((f) => f.opcion)),
    atributo_max: Math.max(...filas.map((f) => f.atributo)),
    coinciden: filas.filter((f) => f.coincide).length,
    mayor: mayor.id,
    desacuerdos,
  };
}

const contar = (lista) => lista.reduce((m, x) => ({ ...m, [x]: (m[x] || 0) + 1 }), {});

/**
 * Every disagreement about the addon requests of a run. A renderer addon that was requested must have been refused,
 * once per request and by the name it carries; no stand-in may have run; and no canvas may exist anywhere in a terminal.
 * `solicitudes` and `rechazos` are lists of class names as the harness created them and as the guard logged them.
 */
export function desacuerdosDeAddons({ solicitudes, rechazos, activaciones, lienzosFueraDeLaPantalla, lienzosCdp }) {
  const salida = [];
  if (solicitudes.length !== rechazos.length) salida.push(`the harness requested ${solicitudes.length} addon(s) and the guard refused ${rechazos.length}`);
  const pedidos = contar(solicitudes);
  const rechazados = contar(rechazos);
  for (const nombre of new Set([...Object.keys(pedidos), ...Object.keys(rechazados)])) {
    if ((pedidos[nombre] || 0) !== (rechazados[nombre] || 0)) salida.push(`addon "${nombre}": requested ${pedidos[nombre] || 0} time(s), refused ${rechazados[nombre] || 0}`);
  }
  if (activaciones > 0) salida.push(`${activaciones} stand-in addon(s) ran activate(): a request got past the guard`);
  if (lienzosFueraDeLaPantalla > 0) salida.push(`${lienzosFueraDeLaPantalla} canvas element(s) sit in a terminal outside .xterm-screen`);
  if (lienzosCdp > 0) salida.push(`CDP finds ${lienzosCdp} canvas element(s) inside the terminals`);
  return salida;
}

// ---- the report --------------------------------------------------------------------------------------------------
const PROCEDENCIA = {
  ventana: 'DECLARED: tiling arithmetic (3840/3 x (2160-32)/2); a headless page has no window frame, so this is not a browser reading',
  dom_solicitado: 'DECLARED: the viewport this harness asks the browser to emulate (ventana minus the 9 px window-manager border)',
  dom: 'READ-BACK of the emulated viewport (innerWidth x innerHeight after Playwright emulation); NOT an independent measurement',
  renderer: 'MEASURED: the DOM of every interactive terminal, read by this instrument (canvas layers in .xterm-screen, else the DOM renderer rows); second sources: the component data-renderer (its own read) and CDP DOM.querySelectorAll counts of the same structure. Never the --renderer flag',
  remontajes_max: 'MEASURED: .xterm elements inserted into each panel host (MutationObserver installed before any page script) minus the first mount; second sources: the number of alMontar callbacks per panel (survives the re-creation of a panel component), the component mount counter (data-montajes, per component instance: it can never exceed the callbacks), and the CDP DOM.childNodeInserted events of the window that follows the mount leg (a terminal opened in a known host, or a whole interactive panel inserted)',
  scrollback: 'MEASURED: the largest number of lines any of the interactive terminals retains after more than the limit was written straight into it (buffer length minus rows, read from each live terminal); second sources per terminal: the number of its first retained line, its scrollback option (READ-BACK) and the component data-scrollback; the same is also measured through one stream (tramos.scrollback)',
  altura_cajon: 'MEASURED: border-box height of the open drawer (getBoundingClientRect once document.getAnimations() is empty); second source: CDP DOM.getBoxModel; the declared 240 is only what the consumer compares against',
  rechazos_de_addon: 'MEASURED: refusals recorded by the guard in the page; second sources: console warnings reported to Playwright by the browser, the harness\'s own count of the requests it made (must be equal, by class name too) and activaciones_de_addon (0)',
  activaciones_de_addon: 'MEASURED by the harness: how many stand-in addons had activate() called; a stand-in counts itself, so the number does not depend on where a canvas layer lands in the DOM',
  scrollback_opcion_max: 'READ-BACK: the largest scrollback option of the live terminals (term.options.scrollback)',
  plazos: 'DECLARED: the command-line bounds of this run (--espera-listo-ms, --plazo-lectura-ms, --plazo-total-ms)',
  duracion_ms: 'MEASURED: wall-clock time of the run, from the first line of the measurement to the report',
  inyeccion: 'DECLARED: the test hooks this run was started with (--inyectar-scrollback, --inyectar-congelacion); null and false on the oracle and the contrast',
  servidor: 'MEASURED on the far side of the interface: the stand-in PTY server decodes the client frames with a codec of its own and counts connections, pauses and bytes; it writes output at a bounded rate (256 KiB per 5 ms) so that a client pause can be seen to hold bytes back',
  paneles: 'MEASURED: [data-terminal-panel] elements in the page; second sources: CDP DOM.querySelectorAll and the connections the stand-in server accepted',
};

/**
 * Provenance of every value under `tramos`, one label per leaf (H-8): MEASURED (read from the page or from the far side of
 * the interface), READ-BACK (what a component or the emulated browser says about itself), DECLARED (a figure or a step this
 * harness fixes) or HARNESS-COMPUTED (arithmetic the harness does on its own inputs or on measured values).
 */
const PROCEDENCIA_DE_TRAMOS_POR_TRAMO = {
  montaje: 'MEASURED',
  entrada: 'MEASURED',
  churn: 'MEASURED',
  cajon: 'MEASURED',
  oculto: 'MEASURED',
  texto: 'MEASURED',
  inactivo: 'MEASURED',
  foco: 'MEASURED',
  raton: 'MEASURED',
  ime: 'MEASURED',
  scrollback: 'MEASURED',
  pestanas: 'MEASURED',
  ajuste: 'MEASURED',
  salida: 'MEASURED',
  desmontaje: 'MEASURED',
  capa_tardia: 'MEASURED',
  hostil: 'MEASURED',
};
const PROCEDENCIA_DE_TRAMOS_EXCEPCIONES = {
  'entrada.escrito': 'DECLARED: the keystrokes this harness types',
  'churn.ciclos': 'DECLARED: the number of collapse/expand cycles this harness performs',
  'churn.arrays_reemplazados': 'DECLARED: the number of times this harness replaces the agents array',
  'churn.pestanas_activadas': 'HARNESS-COMPUTED: the number of agent ids this harness activated (an input, not a reading)',
  'churn.flujos_cortados': 'HARNESS-COMPUTED: the number of streams this harness cut (an input, not a reading)',
  'churn.reconexiones_por_panel': 'READ-BACK: the panel\'s own reconnection counter (data-reconexiones)',
  'churn.conexiones_aceptadas_por_panel': 'MEASURED on the far side: connections the stand-in server accepted for each cut stream',
  'churn.fases_tras_reconectar': 'READ-BACK: the panel\'s own phase (data-fase)',
  'churn.montajes_max_despues': 'READ-BACK: the panel\'s own mount counter (data-montajes)',
  'oculto.alta_marca': 'DECLARED: the high-water mark this harness expects (SUB_04 1.4 [TARGET])',
  'oculto.bytes_ofrecidos_al_servidor': 'HARNESS-COMPUTED: the bytes this harness handed to the stand-in server to send (an input)',
  'oculto.pausas_cliente': 'READ-BACK: the panel\'s own pause counter (data-pausas); the server\'s is pausas_servidor',
  'oculto.reanudaciones_cliente': 'READ-BACK: the panel\'s own resume counter (data-reanudaciones); the server\'s is reanudaciones_servidor',
  'oculto.pausas_al_final_cliente': 'READ-BACK: the panel\'s own pause counter (data-pausas)',
  'oculto.bytes_recibidos_por_el_cliente': 'READ-BACK: the panel\'s own byte counter (data-bytes-recibidos); the server\'s is bytes_enviados_al_final',
  'oculto.bytes_escritos_a_xterm': 'MEASURED by the harness: the bytes of every write() call the panel made on the terminal (write is wrapped in the page)',
  'oculto.lote_max_declarado': 'DECLARED: the per-write bound the harness expects (independent of wsstream.ts)',
  'foco.pasos_declarados': 'DECLARED: the spec\'s three passes, the figures the harness compares with',
  'foco.pasadas_del_panel': 'READ-BACK: the panel\'s own pass counter (data-pasadas-de-foco); the browser\'s focus log is secuencia',
  'texto.paneles': 'HARNESS-COMPUTED: the number of plain-text panes in the run',
  'scrollback.escritas': 'HARNESS-COMPUTED: the number of numbered lines the harness wrote (rows + limit + margin)',
  'scrollback.esperada_primera_linea': 'HARNESS-COMPUTED: arithmetic on escritas and the measured buffer length',
  'scrollback.coincide': 'HARNESS-COMPUTED: numero_primera_linea equals esperada_primera_linea',
  'scrollback.opcion': 'READ-BACK: term.options.scrollback of the terminal',
  'scrollback.todas.escritas': 'HARNESS-COMPUTED: the number of numbered lines the harness wrote into each terminal',
  'scrollback.todas.opcion_max': 'READ-BACK: the largest term.options.scrollback',
  'scrollback.todas.atributo_max': 'READ-BACK: the largest data-scrollback the components publish',
  'scrollback.todas.coinciden': 'HARNESS-COMPUTED: how many terminals\' first retained line agrees with the arithmetic',
  'pestanas.teclas': 'DECLARED: the keys this harness presses',
  'pestanas.secuencia_esperada': 'DECLARED: the wrap-around rule applied to the ids by the harness',
  'pestanas.coincide': 'HARNESS-COMPUTED: secuencia_dom equals secuencia_esperada',
  'ajuste.paneles_medidos': 'HARNESS-COMPUTED: the number of interactive panels measured',
  'salida.codigo_enviado': 'DECLARED: the exit code this harness sends in the EXIT frame',
  'salida.fase_final': 'READ-BACK: the panel\'s own phase (data-fase)',
  'capa_tardia.fase_final': 'READ-BACK: the panel\'s own phase (data-fase)',
  'hostil.tramas_invalidas_antes': 'READ-BACK: the panel\'s own counter of invalid frames (data-tramas-invalidas)',
  'hostil.tramas_invalidas_despues': 'READ-BACK: the panel\'s own counter of invalid frames (data-tramas-invalidas)',
  'hostil.errores_de_pagina_antes': 'MEASURED: uncaught page errors Playwright had seen before the frame',
  'hostil.errores_de_pagina_despues': 'MEASURED: uncaught page errors Playwright had seen after the frame',
};

/** One label per leaf path of `tramos` (a leaf is anything that is not a plain object). */
export function procedenciaDeTramos(tramos) {
  const salida = {};
  const visitar = (nodo, ruta) => {
    if (nodo !== null && typeof nodo === 'object' && !Array.isArray(nodo) && Object.keys(nodo).length > 0) {
      for (const [k, v] of Object.entries(nodo)) visitar(v, ruta === '' ? k : `${ruta}.${k}`);
      return;
    }
    const tramo = ruta.split('.')[0];
    salida[ruta] = PROCEDENCIA_DE_TRAMOS_EXCEPCIONES[ruta] ?? `${PROCEDENCIA_DE_TRAMOS_POR_TRAMO[tramo] ?? 'UNLABELLED'}: read from the page or from the far side of the interface during the ${tramo} leg`;
  };
  visitar(tramos, '');
  return salida;
}

const NO_PROBADO = [
  'the real Wails/WebKitGTK window and the 9 px window-manager border (F-14: operator smoke check)',
  'WebKitGTK rendering and its compositor: Chromium headless is a measurement proxy (DEC-18)',
  'a live PTY fabric: no terminal WebSocket server exists in the tree, so the stream is a stand-in started by this instrument that speaks the SRF2 framing of wsstream.ts [TARGET]',
  'the real @xterm/addon-canvas and @xterm/addon-webgl bundles: they are NOT installed in this tree and may not be, so no real addon was ever loaded into a live terminal here; the contrast asks for stand-ins (a single-letter class, an anonymous class, a subclass of an unrelated built-in, a renamed export, and one control named CanvasAddon) that put canvas layers into .xterm-screen; what the guard does with the real minified classes is a property of its deny-by-default decision (no name is read), exercised in node against stand-ins shaped like them',
  'a real input method engine: the composition is synthetic events on the hidden textarea',
  'src/App.svelte does not mount the drawer yet (U-18 owns it): the drawer is exercised in a harness page, not in the real shell',
];

/** The one shape of the report. Fields not measured stay null; nested objects keep their keys. */
export function informeVacio(o = {}) {
  const informe = {
    ok: false,
    etapa: 'inicio',
    error: null,
    rc: null,
    esquema: 1,
    renderer: null,
    remontajes_max: null,
    scrollback: null,
    altura_cajon: null,
    paneles: null,
    paneles_interactivos: null,
    paneles_texto: null,
    paneles_texto_con_contenido: null,
    montajes_max: null,
    remontajes_total: null,
    renderer_pedido: o.renderer ?? null,
    addons_solicitados: null,
    rechazos_de_addon: null,
    activaciones_de_addon: null,
    variantes_solicitadas: {},
    variantes_rechazadas: {},
    ventana: null,
    dom_solicitado: null,
    dom: null,
    scrollback_opcion: null,
    scrollback_opcion_max: null,
    scrollback_pedido: o.scrollbackPedido ?? null,
    plazos: { espera_listo_ms: o.esperaListo ?? null, plazo_lectura_ms: o.plazoLectura ?? null, plazo_total_ms: o.plazoTotal ?? null },
    duracion_ms: null,
    inyeccion: { scrollback: null, congelacion: false },
    primera_fuente: {
      renderer: null,
      montajes_por_panel: null,
      altura_cajon: null,
      paneles: null,
      rechazos_de_addon: null,
      retenidas: null,
      lienzos_en_paneles: null,
      lienzos_fuera_de_la_pantalla: null,
    },
    segunda_fuente: {
      renderer_componente: null,
      renderer_cdp: null,
      filas_dom_cdp: null,
      lienzos_cdp: null,
      montajes_por_panel_componente: null,
      montajes_por_panel_callback: null,
      inserciones_xterm_cdp: null,
      inserciones_xterm_pagina_en_la_ventana: null,
      altura_cajon_cdp: null,
      paneles_cdp: null,
      conexiones_servidor: null,
      rechazos_consola: null,
      primera_linea_numero: null,
      lienzos_cdp_en_paneles: null,
    },
    tramos: {
      montaje: { paneles_listos: null, cuadros_de_pagina_antes: null },
      entrada: { escrito: null, recibido_por_el_servidor: null, tramas_invalidas_en_el_servidor: null, eco_en_el_terminal: null, tramas_enviadas_por_el_cliente: null, tramas_recibidas_por_el_servidor: null },
      churn: { ciclos: null, pestanas_activadas: null, arrays_reemplazados: null, flujos_cortados: null, reconexiones_por_panel: null, conexiones_aceptadas_por_panel: null, fases_tras_reconectar: null, montajes_max_despues: null },
      cajon: {
        abierto_altura: null,
        plegado_altura: null,
        reabierto_altura: null,
        plegado_altura_cdp: null,
        superficie_antes: null,
        superficie_plegado: null,
        superficie_despues: null,
        superficie_cdp_antes: null,
        superficie_cdp_plegado: null,
        cuerpo_altura: null,
        estado_plegado: null,
      },
      oculto: {
        cuadros_de_pagina: null,
        cuadros_del_panel: null,
        bytes_en_cola_al_final: null,
        pausas_cliente: null,
        pausas_servidor: null,
        bytes_ofrecidos_al_servidor: null,
        bytes_enviados_por_el_servidor: null,
        bytes_retenidos_por_el_servidor: null,
        lineas_nuevas_en_el_buffer: null,
        cuadros_de_pagina_al_reabrir: null,
        pausas_al_final_cliente: null,
        pausas_al_final_servidor: null,
        reanudaciones_cliente: null,
        reanudaciones_servidor: null,
        bytes_recibidos_por_el_cliente: null,
        bytes_enviados_al_final: null,
        cola_al_final: null,
        alta_marca: null,
        escrituras_al_reabrir: null,
        lote_max_bytes: null,
        lote_max_declarado: null,
        bytes_escritos_a_xterm: null,
      },
      texto: { paneles: null, con_contenido_tras_activar: null, primera_muestra: null, tope: { escritas: null, lineas_conservadas: null, numero_primera: null, esperada_primera: null, coincide: null } },
      inactivo: { cuadros_del_panel: null, bytes_en_cola: null, cuadros_de_pagina: null, cuadros_tras_activar: null, cola_tras_activar: null },
      foco: { pasadas_del_panel: null, secuencia: null, foco_final_en_el_panel: null, intrusos: null, pasos_declarados: null },
      raton: { derecho_llegado_al_destino: null, derecho_cancelado: null, izquierdo_llegado_al_destino: null, derecho_real_llegado_al_destino: null },
      ime: { textarea_tras_la_composicion: null, recibido_por_el_servidor: null },
      scrollback: {
        escritas: null,
        filas: null,
        retenidas: null,
        opcion: null,
        numero_primera_linea: null,
        esperada_primera_linea: null,
        coincide: null,
        bytes_cliente: null,
        bytes_servidor: null,
        todas: { terminales: null, escritas: null, retenidas_max: null, retenidas_min: null, opcion_max: null, atributo_max: null, coinciden: null, mayor: null },
      },
      pestanas: { teclas: null, secuencia_dom: null, secuencia_estado: null, secuencia_esperada: null, coincide: null },
      ajuste: { paneles_medidos: null, filas_min: null, filas_max: null, columnas_min: null, anfitrion_alto: null, fila_alto: null, no_caben: null, no_llenan: null, sin_resize_coherente: null },
      salida: { codigo_enviado: null, linea_en_el_buffer: null, linea_en_el_dom: null, fase_final: null, conexiones_aceptadas_antes: null, conexiones_aceptadas_despues: null },
      desmontaje: { panel: null, panel_fuera_del_dom: null, desmontado_por_callback: null, sockets_abiertos_antes: null, sockets_abiertos_despues: null, conexiones_abiertas_antes: null, conexiones_abiertas_despues: null },
      capa_tardia: { panel: null, fase_final: null, aviso_visible: null, terminal_desmontado: null, conexion_cerrada_en_el_servidor: null },
      hostil: { panel: null, tramas_invalidas_antes: null, tramas_invalidas_despues: null, errores_de_pagina_antes: null, errores_de_pagina_despues: null },
    },
    diagnostico: { montajes_observados_max: null, paneles_por_fase: null, ultimo_error_de_pagina: null, errores_de_pagina_total: null, error_de_lectura: null },
    errores_de_pagina: [],
    errores_de_cierre: [],
    consola_avisos: [],
    servidor: { puerto: null, conexiones_aceptadas: null, pausas: null, reanudaciones: null, tramas_invalidas_recibidas: null, errores_de_socket: null },
    procedencia: { ...PROCEDENCIA, tramos: {} },
    no_probado: [...NO_PROBADO],
    navegador: { chromium: null, playwright: null, xterm: null },
    acuerdo_entre_fuentes: null,
    desacuerdos: [],
    campos_en_desacuerdo: [],
  };
  informe.procedencia.tramos = procedenciaDeTramos(informe.tramos);
  return informe;
}

/** A failure that knows its stage and its exit code. */
class ErrorMedicion extends Error {
  constructor(etapa, rc, mensaje, causa) {
    super(mensaje, causa === undefined ? undefined : { cause: causa });
    this.etapa = etapa;
    this.rc = rc;
  }
}

const primeraLinea = (t) => String(t).split('\n')[0];

/** Races a promise against a deadline and always clears the timer, so a finished run never waits for it. */
function conPlazo(promesa, ms, crearError) {
  let temporizador;
  const limite = new Promise((_, mal) => {
    temporizador = setTimeout(() => mal(crearError()), ms);
  });
  return Promise.race([promesa, limite]).finally(() => clearTimeout(temporizador));
}
/**
 * Closes the browser politely within `ms`; if that does not happen (a renderer that spins never answers) the processes of
 * THIS run, which all carry its private root in their arguments, are killed. Returns the reason it had to be forced, or null.
 */
async function cerrarNavegador(navegador, raiz, ms) {
  try {
    await conPlazo(navegador.close(), ms, () => new Error(`browser.close() did not finish in ${ms} ms`));
    return null;
  } catch (e) {
    if (raiz !== null) spawnSync('pkill', ['-KILL', '-f', raiz], { stdio: 'ignore' });
    return primeraLinea(e.message);
  }
}
const fmt = (w, h) => (w === null || w === undefined || h === null || h === undefined ? null : `${w}x${h}`);

// ---- command line ----------------------------------------------------------------------------------------------------
export function leerArgumentos(argv) {
  const o = { paneles: null, renderer: 'dom', json: false, scrollbackPedido: SCROLLBACK_DECLARADO, esperaListo: 90000, reposo: 500, plazoLectura: 5000, plazoTotal: 300000, inyectarScrollback: null, inyectarCongelacion: false };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    const valor = () => {
      if (i + 1 >= argv.length) throw new ErrorMedicion('argumentos', 2, `${a} needs a value`);
      return argv[++i];
    };
    if (a === '--paneles') o.paneles = Number(valor());
    else if (a === '--renderer') o.renderer = valor();
    else if (a === '--json') o.json = true;
    else if (a === '--scrollback-pedido') o.scrollbackPedido = Number(valor());
    else if (a === '--espera-listo-ms') o.esperaListo = Number(valor());
    else if (a === '--reposo-ms') o.reposo = Number(valor());
    else if (a === '--plazo-lectura-ms') o.plazoLectura = Number(valor());
    else if (a === '--plazo-total-ms') o.plazoTotal = Number(valor());
    else if (a === '--inyectar-scrollback') {
      const m = /^(srf-ng-(\d+))=(\d+)$/.exec(valor());
      if (m === null) throw new ErrorMedicion('argumentos', 2, '--inyectar-scrollback needs ID=N with an id like srf-ng-9 and a non-negative integer');
      o.inyectarScrollback = { id: m[1], indice: Number(m[2]), n: Number(m[3]) };
    } else if (a === '--inyectar-congelacion') o.inyectarCongelacion = true;
    else throw new ErrorMedicion('argumentos', 2, `unknown argument ${a}`);
  }
  if (o.paneles === null) throw new ErrorMedicion('argumentos', 2, '--paneles N is required');
  if (!Number.isInteger(o.paneles) || o.paneles < 2 || o.paneles > 200) throw new ErrorMedicion('argumentos', 2, '--paneles must be an integer in 2..200');
  if (!['dom', 'canvas', 'webgl'].includes(o.renderer)) throw new ErrorMedicion('argumentos', 2, `--renderer must be dom, canvas or webgl, not ${JSON.stringify(o.renderer)}`);
  if (!Number.isInteger(o.scrollbackPedido) || o.scrollbackPedido < 0) throw new ErrorMedicion('argumentos', 2, '--scrollback-pedido must be a non-negative integer');
  for (const [k, v] of [['--espera-listo-ms', o.esperaListo], ['--reposo-ms', o.reposo], ['--plazo-lectura-ms', o.plazoLectura], ['--plazo-total-ms', o.plazoTotal]]) {
    if (!Number.isFinite(v) || v < 0) throw new ErrorMedicion('argumentos', 2, `${k} must be a non-negative number`);
  }
  if (o.inyectarScrollback !== null && o.inyectarScrollback.indice >= Math.min(o.paneles, PANELES_INTERACTIVOS_DECLARADOS)) throw new ErrorMedicion('argumentos', 2, `--inyectar-scrollback ${o.inyectarScrollback.id} is not an interactive panel of a run of ${o.paneles}`);
  return o;
}

// ---- the harness page ----------------------------------------------------------------------------------------------
const FUENTE_ADDONS = `// Stand-ins for @xterm/addon-canvas and @xterm/addon-webgl (NOT installed). What they reproduce is what a guard has to
// survive: the published bundles are minified, so their classes are called \`a\`, \`l\`, \`p\` and no class name is a mark.
// The disguised stand-ins are a single-letter class, an anonymous class, a subclass of an unrelated built-in and a class
// exported under another name; NONE is named CanvasAddon or WebglAddon or extends a class that is. Their observable effect
// is the real renderers': the DOM rows are replaced by canvas layers inside .xterm-screen. The control at the end is
// named as the real class, the only kind of fixture attempt 1 had; it proves nothing about a minified bundle.
// Every activate() counts itself (window.__srfActivaciones): the harness's own record that an addon really ran.
export const CAPAS_CANVAS = ['xterm-text-layer', 'xterm-selection-layer', 'xterm-link-layer', 'xterm-cursor-layer'];
export const CAPAS_WEBGL = ['xterm-webgl-layer'];

function poner(term, clases) {
  window.__srfActivaciones = (window.__srfActivaciones || 0) + 1;
  const pantalla = term.element.querySelector('.xterm-screen');
  const filas = pantalla.querySelector('.xterm-rows');
  if (filas) filas.remove();
  for (const clase of clases) {
    const lienzo = document.createElement('canvas');
    lienzo.className = clase;
    lienzo.width = 16;
    lienzo.height = 16;
    pantalla.appendChild(lienzo);
  }
}

const nombrar = (clase, nombre) => Object.defineProperty(clase, 'name', { value: nombre });

export function crearVariantes(capas) {
  const minificada = nombrar(class { activate(term) { poner(term, capas); } dispose() {} }, 'a');
  const anonima = nombrar(class { activate(term) { poner(term, capas); } dispose() {} }, '');
  const subclase = nombrar(class extends Map { activate(term) { poner(term, capas); } dispose() {} }, 'Marcador');
  const interna = nombrar(class { activate(term) { poner(term, capas); } dispose() {} }, 'Interno');
  const renombrada = interna; // the module exports it under a name of its own: the class keeps the name it was born with
  return { minificada: () => new minificada(), anonima: () => new anonima(), subclase: () => new subclase(), renombrada: () => new renombrada() };
}

export class CanvasAddon {
  activate(term) { poner(term, CAPAS_CANVAS); }
  dispose() {}
}
export class WebglAddon {
  activate(term) { poner(term, CAPAS_WEBGL); }
  dispose() {}
}
`;

function fuenteHarness() {
  return `<script>
  import { onMount } from 'svelte';
  import CajonAgentes from ${JSON.stringify(join(TERMINAL, 'CajonAgentes.svelte'))};
  import { LIENZO_W, LIENZO_H } from ${JSON.stringify(join(FRONTEND, 'src', 'lib', 'geometria.ts'))};
  import { rechazosDeGuarda } from ${JSON.stringify(join(TERMINAL, 'termguard.ts'))};
  import ${JSON.stringify(join(FRONTEND, 'src', 'app.css'))};
  import { CAPAS_CANVAS, CAPAS_WEBGL, CanvasAddon, WebglAddon, crearVariantes } from './addons.js';

  const q = new URLSearchParams(location.search);
  const n = Number(q.get('n'));
  const puerto = q.get('ws');
  const renderer = q.get('renderer') || 'dom';
  const scrollbackPedido = q.has('scrollback') ? Number(q.get('scrollback')) : undefined;
  const inyeccion = q.has('sb') ? { id: q.get('sb').split(':')[0], n: Number(q.get('sb').split(':')[1]) } : null;
  const congelar = q.get('congelar') === '1';
  const TITULOS = ['Claude Code', 'Gemini CLI', 'bash'];
  const construir = () => Array.from({ length: n }, (_, i) => ({ id: 'srf-ng-' + i, titulo: TITULOS[i % 3], superficie: 'srf-ng-' + i, url: 'ws://127.0.0.1:' + puerto + '/pty/srf-ng-' + i }));

  let agentes = $state(construir());
  let abierto = $state(true);
  let activo = $state('srf-ng-0');
  const terminales = {};
  const originales = {};
  const escrituras = {};
  const desmontados = [];
  const montajesPorLlamada = {};
  const solicitudes = [];
  let addonsSolicitados = 0;
  const variantes = crearVariantes(renderer === 'webgl' ? CAPAS_WEBGL : CAPAS_CANVAS);
  const NOMBRES = ['minificada', 'anonima', 'subclase', 'renombrada'];
  // the first request is the labelled control (named as the real class); the rest rotate through the disguises
  const crearAddons = renderer === 'dom' ? undefined : () => {
    const i = addonsSolicitados++;
    const variante = i === 0 ? 'control' : NOMBRES[(i - 1) % NOMBRES.length];
    const addon = variante === 'control' ? (renderer === 'webgl' ? new WebglAddon() : new CanvasAddon()) : variantes[variante]();
    solicitudes.push({ variante, nombre: addon.constructor.name });
    return [addon];
  };
  document.documentElement.dataset.rendererPedido = renderer;

  window.__srfHarness = {
    n,
    abrir: (v) => { abierto = v; },
    activar: (id) => { activo = id; },
    reemplazarAgentes: () => { agentes = agentes.map((a) => ({ ...a })); },
    terminal: (id) => terminales[id] ?? null,
    ids: () => agentes.map((a) => a.id),
    abiertoAhora: () => abierto,
    activoAhora: () => activo,
    rechazos: () => rechazosDeGuarda().map((r) => ({ ...r })),
    addonsSolicitados: () => addonsSolicitados,
    montajesPorLlamada: () => ({ ...montajesPorLlamada }),
    quitar: (id) => { agentes = agentes.filter((a) => a.id !== id); },
    solicitudes: () => solicitudes.map((x) => ({ ...x })),
    activaciones: () => window.__srfActivaciones || 0,
    desmontados: () => [...desmontados],
    escrituras: (id) => ({ ...(escrituras[id] ?? { llamadas: 0, bytes: 0, max: 0 }) }),
    reiniciarEscrituras: (id) => { escrituras[id] = { llamadas: 0, bytes: 0, max: 0 }; },
    // straight into the terminal, past the component's queue and past the counters above
    escribirDirecto: (id, datos) => new Promise((ok) => originales[id](datos, ok)),
    // a canvas layer that no addon made, appended where the published renderer addons append theirs
    inyectarCapaTardia: (id) => { terminales[id].element.querySelector('.xterm-screen').appendChild(document.createElement('canvas')); },
    listo: () => {
      const paneles = [...document.querySelectorAll('[data-terminal-panel]')];
      return paneles.length === n && paneles.every((p) => p.dataset.fase === 'abierto' && Number(p.dataset.montajes) >= 1);
    },
  };

  // the frozen WebKit the unit exists to avoid: the main thread spins from the first line of the harness, so nothing finishes,
  // and the page can neither be read nor closed politely. Deterministic on purpose: a spin that starts later races the reads.
  if (congelar) { const fin = Date.now() + 3600000; let vueltas = 0; while (Date.now() < fin) { vueltas++; } window.__srfVueltas = vueltas; }

  onMount(() => {
    const espera = () => {
      if (window.__srfHarness.listo()) requestAnimationFrame(() => requestAnimationFrame(() => { document.documentElement.dataset.appReady = 'true'; }));
      else window.setTimeout(espera, 20);
    };
    espera();
  });
</script>

<div class="lienzo-falso" data-lienzo-falso style="position:relative;display:flex;flex-direction:column;width:{LIENZO_W}px;height:{LIENZO_H}px;margin:3px 0 0 3px;overflow:hidden;background:#0e131f">
  <input data-intruso aria-label="element that steals the focus" style="position:absolute;top:0;left:0;width:80px;z-index:5" />
  <div data-superficie-falsa style="flex:1 1 auto;min-height:0;background:#161d2e"></div>
  <CajonAgentes
    {agentes}
    bind:abierto
    bind:activo
    scrollback={scrollbackPedido}
    {crearAddons}
    alMontar={(id, t) => {
      terminales[id] = t;
      montajesPorLlamada[id] = (montajesPorLlamada[id] ?? 0) + 1;
      // every write the panel makes on the terminal is measured here, at the call, whatever the component thinks it wrote
      const escribir = t.write.bind(t);
      originales[id] = escribir;
      escrituras[id] = { llamadas: 0, bytes: 0, max: 0 };
      t.write = (datos, cb) => {
        const n = typeof datos === 'string' ? new TextEncoder().encode(datos).length : datos.length;
        const e = escrituras[id];
        e.llamadas++;
        e.bytes += n;
        if (n > e.max) e.max = n;
        return escribir(datos, cb);
      };
      if (inyeccion !== null && inyeccion.id === id) t.options.scrollback = inyeccion.n;
    }}
    alDesmontar={(id) => { delete terminales[id]; desmontados.push(id); }}
  />
</div>
`;
}

const FUENTE_ENTRADA = `import { mount } from 'svelte';
import Harness from './Harness.svelte';
mount(Harness, { target: document.body });
`;

const HTML_HARNESS = `<!doctype html>
<html lang="en">
  <head>
    <meta charset="UTF-8" />
    <title>srf medir-terminal harness</title>
    <link rel="stylesheet" href="/harness.css" />
    <script type="module" src="/harness.js"></script>
  </head>
  <body></body>
</html>
`;

/** Loads a module of the frontend's own node_modules (Vite and the Svelte plugin are ESM-only and not resolvable from here). */
async function importarDelFrontend(...partes) {
  const ruta = join(FRONTEND, 'node_modules', ...partes);
  if (!existsSync(ruta)) throw new ErrorMedicion('precondicion', 2, `${ruta} not found: cockpit/frontend has no installed dependencies`);
  return import(pathToFileURL(ruta).href);
}

/** Builds the harness into `destino/out` with Vite's Node API. Never cockpit/frontend/dist. */
async function construirHarness(destino) {
  if (resolve(destino).startsWith(join(FRONTEND, 'dist'))) throw new ErrorMedicion('precondicion', 2, 'refusing to build into cockpit/frontend/dist');
  for (const f of ['CajonAgentes.svelte', 'TerminalPanel.svelte', 'wsstream.ts', 'termguard.ts']) {
    if (!existsSync(join(TERMINAL, f))) throw new ErrorMedicion('precondicion', 2, `${join(TERMINAL, f)} not found`);
  }
  const vite = await importarDelFrontend('vite', 'dist', 'node', 'index.js');
  const { svelte, vitePreprocess } = await importarDelFrontend('@sveltejs', 'vite-plugin-svelte', 'src', 'index.js');
  const fuentes = join(destino, 'src');
  await mkdir(fuentes, { recursive: true });
  await writeFile(join(fuentes, 'addons.js'), FUENTE_ADDONS);
  await writeFile(join(fuentes, 'Harness.svelte'), fuenteHarness());
  await writeFile(join(fuentes, 'entrada.js'), FUENTE_ENTRADA);
  const plugins = [
    {
      // The harness files live outside the frontend: their bare imports (svelte) resolve as if written inside it.
      name: 'srf-resolver-desde-el-frontend',
      enforce: 'pre',
      async resolveId(fuente, importador, opciones) {
        if (!importador || !importador.startsWith(fuentes) || fuente.startsWith('.') || fuente.startsWith('/') || fuente.startsWith('\0')) return null;
        return this.resolve(fuente, join(FRONTEND, 'src', 'x.js'), { ...opciones, skipSelf: true });
      },
    },
    svelte({ preprocess: vitePreprocess() }),
  ];
  try {
    await vite.build({
      root: FRONTEND,
      configFile: false,
      logLevel: 'silent',
      cacheDir: join(destino, 'cache'),
      plugins,
      build: {
        outDir: join(destino, 'out'),
        emptyOutDir: false,
        target: 'es2022',
        sourcemap: false,
        minify: false, // class names (CanvasAddon, WebglAddon) are what the guard reads
        rollupOptions: { input: join(fuentes, 'entrada.js'), output: { entryFileNames: 'harness.js', assetFileNames: 'harness[extname]' } },
      },
    });
  } catch (e) {
    throw new ErrorMedicion('construccion', 2, `the harness page could not be built: ${primeraLinea(e.message)}`, e);
  }
  return join(destino, 'out');
}

const TIPOS = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8' };

async function servir(dist) {
  const peticiones = [];
  const servidor = createServer(async (req, res) => {
    const ruta = decodeURIComponent(new URL(req.url, 'http://127.0.0.1').pathname);
    peticiones.push(ruta);
    const cabeceras = (tipo) => ({ 'content-type': tipo, 'cache-control': 'no-store' });
    if (ruta === '/' || ruta === '/index.html') {
      res.writeHead(200, cabeceras(TIPOS['.html']));
      res.end(HTML_HARNESS);
      return;
    }
    const destino = normalize(join(dist, ruta));
    if (!destino.startsWith(dist + sep) || !existsSync(destino)) {
      res.writeHead(404, cabeceras('text/plain'));
      res.end('not found');
      return;
    }
    res.writeHead(200, cabeceras(TIPOS[extname(destino)] ?? 'application/octet-stream'));
    res.end(await readFile(destino));
  });
  await new Promise((ok, mal) => {
    servidor.once('error', mal);
    servidor.listen(0, '127.0.0.1', ok);
  });
  return { servidor, puerto: servidor.address().port, peticiones };
}

// ---- page-side instrumentation (a context-wide init script: it runs before any page script) --------------------------
function instrumentarPagina() {
  if (window.top !== window) return;
  const med = { rafs: 0, montajes: {}, montajesTotal: 0, foco: [], observador: null, sockets: [] };
  window.__srfMedicion = med;
  // every WebSocket the page opens is remembered, so the client's own view of a stream can be read at any time
  const WS = window.WebSocket;
  const SocketMedido = function (url, protocolos) {
    const ws = protocolos === undefined ? new WS(url) : new WS(url, protocolos);
    med.sockets.push(ws);
    return ws;
  };
  SocketMedido.prototype = WS.prototype;
  for (const k of ['CONNECTING', 'OPEN', 'CLOSING', 'CLOSED']) Object.defineProperty(SocketMedido, k, { value: WS[k] });
  window.WebSocket = SocketMedido;
  const raf = window.requestAnimationFrame.bind(window);
  window.requestAnimationFrame = (cb) => {
    med.rafs++;
    return raf(cb);
  };
  const idDelPanel = (nodo) => {
    const p = nodo.parentElement && nodo.parentElement.closest('[data-panel-id]');
    return p ? p.getAttribute('data-panel-id') : '(no panel)';
  };
  const contar = (registros) => {
    for (const r of registros) {
      for (const n of r.addedNodes) {
        if (n.nodeType === 1 && n.classList.contains('xterm')) {
          const id = idDelPanel(n);
          med.montajes[id] = (med.montajes[id] || 0) + 1;
          med.montajesTotal++;
        }
      }
    }
  };
  med.observador = new MutationObserver(contar);
  med.observador.observe(document, { childList: true, subtree: true });
  med.drenar = () => contar(med.observador.takeRecords());
  document.addEventListener(
    'focusin',
    (e) => {
      const el = e.target;
      const panel = el && el.closest ? el.closest('[data-panel-id]') : null;
      med.foco.push({ t: performance.now(), panel: panel ? panel.getAttribute('data-panel-id') : null, intruso: !!(el && el.matches && el.matches('[data-intruso]')), textarea: !!(el && el.classList && el.classList.contains('xterm-helper-textarea')) });
    },
    true,
  );
}

/** Runs in the page: what each panel says and what its DOM is. */
function leerPaneles() {
  return [...document.querySelectorAll('[data-terminal-panel]')].map((p) => {
    const pantalla = p.querySelector('.xterm .xterm-screen');
    const d = p.dataset;
    const h = window.__srfHarness;
    const t = h && h.terminal ? h.terminal(d.panelId) : null;
    const anfitrion = p.querySelector('[data-panel-host]');
    const fila = pantalla ? pantalla.querySelector('.xterm-rows > div') : null;
    return {
      id: d.panelId,
      modo: d.modo,
      fase: d.fase,
      visible: d.visible,
      montajes: Number(d.montajes),
      renderer_componente: d.renderer,
      filas_dom: pantalla ? pantalla.querySelectorAll('.xterm-rows').length : 0,
      lienzos: pantalla ? pantalla.querySelectorAll('canvas').length : 0,
      lienzos_panel: p.querySelectorAll('canvas').length,
      columnas: t ? t.cols : null,
      filas: t ? t.rows : null,
      anfitrion_h: anfitrion ? anfitrion.clientHeight : null,
      anfitrion_w: anfitrion ? anfitrion.clientWidth : null,
      pantalla_h: pantalla ? pantalla.getBoundingClientRect().height : null,
      fila_h: fila ? fila.getBoundingClientRect().height : null,
      tiene_xterm: p.querySelector('.xterm') !== null,
      texto_longitud: p.querySelector('[data-panel-texto]') ? p.querySelector('[data-panel-texto]').textContent.length : null,
      cuadros: Number(d.cuadros || 0),
      bytes_en_cola: Number(d.bytesEnCola || 0),
      bytes_en_vuelo: Number(d.bytesEnVuelo || 0),
      pausas: Number(d.pausas || 0),
      reanudaciones: Number(d.reanudaciones || 0),
      bytes_recibidos: Number(d.bytesRecibidos || 0),
      tramas_invalidas: Number(d.tramasInvalidas || 0),
      saltos_de_secuencia: Number(d.saltosDeSecuencia || 0),
      reconexiones: Number(d.reconexiones || 0),
      aperturas: Number(d.aperturas || 0),
      enviadas: Number(d.enviadas || 0),
      pasadas_de_foco: Number(d.pasadasDeFoco || 0),
      scrollback_atributo: d.scrollback === undefined ? null : Number(d.scrollback),
    };
  });
}

/** Runs in the page: the WebSockets of one stream that are not closed, from the page's own record of every socket it opened. */
function contarSocketsAbiertos(id) {
  return window.__srfMedicion.sockets.filter((s) => s.url.endsWith('/pty/' + id) && s.readyState <= 1).length;
}

/** Runs in the page: the mount counters of the observer. */
function leerMontajesPagina() {
  const med = window.__srfMedicion;
  med.drenar();
  return { montajes: { ...med.montajes }, total: med.montajesTotal, rafs: med.rafs };
}

/** Runs in the page: a box read from the layout. */
function leerCaja(selector) {
  const el = document.querySelector(selector);
  if (!el) return null;
  const r = el.getBoundingClientRect();
  return { x: r.x, y: r.y, w: r.width, h: r.height };
}

// ---- measurement -------------------------------------------------------------------------------------------------
const CAJA_A_TEXTO = (c) => (c === null ? null : `${c.w}x${c.h}`);

async function medir(o, informe) {
  informe.etapa = 'precondicion';
  const requerir = createRequire(join(FRONTEND, 'package.json'));
  let pw;
  try {
    pw = requerir('playwright-core');
  } catch (e) {
    throw new ErrorMedicion('precondicion', 2, `playwright-core is not installed under cockpit/frontend: ${primeraLinea(e.message)}`, e);
  }
  const version = requerir('playwright-core/package.json').version;
  informe.navegador.playwright = version;
  if (version !== PLAYWRIGHT_PIN) throw new ErrorMedicion('precondicion', 2, `playwright-core ${version} found, ${PLAYWRIGHT_PIN} is pinned: any other version would try to download a browser`);
  let versionXterm;
  try {
    versionXterm = requerir('@xterm/xterm/package.json').version;
  } catch (e) {
    throw new ErrorMedicion('precondicion', 2, `@xterm/xterm is not installed under cockpit/frontend: ${primeraLinea(e.message)}`, e);
  }
  informe.navegador.xterm = versionXterm;
  if (versionXterm !== XTERM_PIN) throw new ErrorMedicion('precondicion', 2, `@xterm/xterm ${versionXterm} found, ${XTERM_PIN} is pinned by the contract`);

  const { ventana, dom } = derivarViewport();
  informe.ventana = fmt(ventana.w, ventana.h);
  informe.dom_solicitado = fmt(dom.w, dom.h);
  const interactivosEsperados = Math.min(o.paneles, PANELES_INTERACTIVOS_DECLARADOS);
  const textoEsperados = o.paneles - interactivosEsperados;

  // One private root for everything this run writes (harness sources, build output, Vite cache, the browser profile):
  // the run always removes it, so nothing is left behind whatever stage fails. The signal handlers are installed BEFORE
  // the root exists and the root is created synchronously, so no signal can find it without a handler that knows it.
  let raizTemporal = null;
  let web = null;
  let pty = null;
  let navegador = null;
  let lanzamiento = null;
  const inicio = Date.now();
  let vencido = null; // the stage at which the run's own bound expired
  let temporizadorTotal = null;
  const CODIGOS_DE_SENAL = { SIGHUP: 129, SIGINT: 130, SIGTERM: 143 };
  const alSenal = (senal) => {
    const salir = () => {
      try {
        if (raizTemporal !== null) rmSync(raizTemporal, { recursive: true, force: true, maxRetries: 10, retryDelay: 50 });
      } catch (e) {
        process.stderr.write(`medir-terminal: the private root ${raizTemporal} could not be removed after ${senal}: ${primeraLinea(e.message)}\n`);
      }
      process.exit(CODIGOS_DE_SENAL[senal]);
    };
    const pendiente = navegador !== null ? navegador : lanzamiento;
    if (pendiente === null) salir();
    else Promise.resolve(pendiente).then((n) => cerrarNavegador(n, raizTemporal, PLAZO_DE_CIERRE_MS)).then(salir, salir);
  };
  for (const senal of Object.keys(CODIGOS_DE_SENAL)) process.once(senal, alSenal);
  const tmpAnterior = process.env.TMPDIR;
  try {
    raizTemporal = mkdtempSync(join(tmpdir(), 'srf-medir-terminal-'));
    const tmpNavegador = join(raizTemporal, 'tmp');
    mkdirSync(tmpNavegador);
    process.env.TMPDIR = tmpNavegador;
    informe.etapa = 'construccion';
    const dist = await construirHarness(raizTemporal);
    web = await servir(dist);
    pty = await crearServidorPty();
    informe.servidor.puerto = pty.puerto;

    informe.etapa = 'navegador';
    try {
      lanzamiento = pw.chromium.launch({ headless: true, handleSIGINT: false, handleSIGTERM: false, handleSIGHUP: false });
      navegador = await lanzamiento;
    } catch (e) {
      throw new ErrorMedicion('navegador', 1, `the browser could not be launched: ${primeraLinea(e.message)}`, e);
    }
    informe.navegador.chromium = navegador.version();
    // The run's own bound: whatever a leg is waiting for, when it expires the browser is closed, every pending call of
    // the page or of CDP rejects, and the catch below turns that into a report (etapa, rc 4, error).
    temporizadorTotal = setTimeout(() => {
      vencido = informe.etapa;
      cerrarNavegador(navegador, raizTemporal, PLAZO_DE_CIERRE_MS);
    }, o.plazoTotal);
    const contexto = await navegador.newContext({ viewport: { width: dom.w, height: dom.h }, screen: { width: PANTALLA.w, height: PANTALLA.h }, deviceScaleFactor: 1, colorScheme: 'dark', reducedMotion: 'no-preference' });
    await contexto.addInitScript(instrumentarPagina);
    const pagina = await contexto.newPage();
    const erroresDePagina = informe.errores_de_pagina;
    let alFallar = null;
    const falloDePagina = new Promise((_, mal) => {
      alFallar = mal;
    });
    falloDePagina.catch(() => {});
    let erroresTotales = 0;
    pagina.on('pageerror', (e) => {
      erroresTotales++;
      informe.diagnostico.errores_de_pagina_total = erroresTotales;
      if (erroresDePagina.length < 20) erroresDePagina.push(primeraLinea(e.message)); // a remount storm throws thousands: the first 20 are kept, the total is counted
      alFallar(new ErrorMedicion('listo', 4, `the page threw: ${primeraLinea(erroresDePagina[0])}`, e));
    });
    const avisosDeConsola = [];
    pagina.on('console', (m) => {
      if (m.type() === 'warning') avisosDeConsola.push(m.text());
    });
    const cdp = await contexto.newCDPSession(pagina);

    // A leg is a promise raced against the page failing: a page that threw is reported, not waited on.
    const contraFallo = (p) => Promise.race([p, falloDePagina]);
    // Playwright's own timeout of a wait needs the page to answer once more (it disposes its poller there): a frozen page
    // can keep it from ever settling, so every wait is also raced against a deadline of this side of the interface.
    const conPlazoPropio = (promesa, ms) => conPlazo(promesa, ms + o.plazoLectura, () => new Error(`the page did not answer within ${ms + o.plazoLectura} ms`));
    const esperar = async (fn, arg, ms, etapa, que) => {
      try {
        await conPlazoPropio(contraFallo(pagina.waitForFunction(fn, arg, { timeout: ms, polling: 50 })), ms);
      } catch (e) {
        if (e instanceof ErrorMedicion) throw e;
        throw new ErrorMedicion(etapa, 4, `${que}: ${primeraLinea(e.message)}`, e);
      }
    };
    const evaluar = (fn, arg) => contraFallo(pagina.evaluate(fn, arg));
    const asentar = async () => {
      await esperar(() => document.getAnimations().length === 0, null, 5000, 'lectura', 'the drawer animation did not finish');
      await pagina.waitForTimeout(40);
    };
    const enServidor = async (cond, ms, etapa, que) => {
      const limite = Date.now() + ms;
      while (Date.now() < limite) {
        if (cond()) return;
        await pagina.waitForTimeout(25);
      }
      throw new ErrorMedicion(etapa, 4, `${que} (waited ${ms} ms)`);
    };

    informe.etapa = 'carga';
    try {
      const consulta = `n=${o.paneles}&ws=${pty.puerto}&renderer=${o.renderer}${o.scrollbackPedido !== SCROLLBACK_DECLARADO ? `&scrollback=${o.scrollbackPedido}` : ''}${o.inyectarScrollback !== null ? `&sb=${o.inyectarScrollback.id}:${o.inyectarScrollback.n}` : ''}${o.inyectarCongelacion ? '&congelar=1' : ''}`;
      await pagina.goto(`http://127.0.0.1:${web.puerto}/?${consulta}`, { waitUntil: 'commit' });
    } catch (e) {
      throw new ErrorMedicion('carga', 1, `the page could not be loaded: ${primeraLinea(e.message)}`, e);
    }

    informe.etapa = 'listo';
    try {
      await conPlazoPropio(contraFallo(pagina.waitForFunction(() => document.documentElement.dataset.appReady === 'true', null, { timeout: o.esperaListo, polling: 50 })), o.esperaListo);
    } catch (e) {
      if (e instanceof ErrorMedicion) throw e;
      let estado = '';
      try {
        const ps = await conPlazo(pagina.evaluate(leerPaneles), o.plazoLectura, () => new Error(`reading the panels timed out after ${o.plazoLectura} ms`));
        const porFase = {};
        for (const p of ps) porFase[p.fase] = (porFase[p.fase] || 0) + 1;
        informe.diagnostico.paneles_por_fase = porFase;
        estado = ` (panels: ${ps.length}, phases ${JSON.stringify(porFase)}, server connections ${pty.abiertas()})`;
      } catch (e2) {
        estado = ` (the page could not be read either: ${primeraLinea(e2.message)})`;
      }
      throw new ErrorMedicion('listo', 4, `the page never set data-app-ready="true": ${primeraLinea(e.message)}${estado}`, e);
    }
    await pagina.waitForTimeout(o.reposo);
    const datosDom = await evaluar(() => ({ w: window.innerWidth, h: window.innerHeight }));
    informe.dom = fmt(datosDom.w, datosDom.h);

    // ---- montaje --------------------------------------------------------------------------------------------
    informe.etapa = 'montaje';
    const paneles0 = await evaluar(leerPaneles);
    informe.tramos.montaje.paneles_listos = paneles0.filter((p) => p.fase === 'abierto').length;
    informe.tramos.montaje.cuadros_de_pagina_antes = (await evaluar(leerMontajesPagina)).rafs;
    const insertadosCdp = { n: 0 };
    cdp.on('DOM.childNodeInserted', ({ node }) => {
      const a = {};
      const atrs = node.attributes || [];
      for (let i = 0; i + 1 < atrs.length; i += 2) a[atrs[i]] = atrs[i + 1];
      // a terminal opened in a host the frontend already knows...
      if (typeof a.class === 'string' && a.class.split(/\s+/).includes('xterm')) insertadosCdp.n++;
      // ...or a whole interactive panel inserted: its host and its terminal are new, and unknown to the frontend, so
      // their own insertion is not reported
      else if ('data-terminal-panel' in a && a['data-modo'] === 'interactivo') insertadosCdp.n++;
    });
    await cdp.send('DOM.enable');
    // ONE getDocument for the whole run: every later call would discard the frontend's knowledge of the tree, and with it
    // the childNodeInserted events this window depends on. The root id stays valid; queries push the nodes they return.
    const raizCdp = (await cdp.send('DOM.getDocument', { depth: -1 })).root.nodeId;
    const mo0 = await evaluar(leerMontajesPagina);

    // ---- entrada --------------------------------------------------------------------------------------------
    informe.etapa = 'entrada';
    const activoAhora = await evaluar(() => window.__srfHarness.activoAhora());
    const escrito = 'echo hola\r';
    await pagina.locator(`[data-panel-id="${activoAhora}"] .xterm-helper-textarea`).focus();
    await pagina.keyboard.type('echo hola');
    await pagina.keyboard.press('Enter');
    await enServidor(() => Buffer.concat(pty.conexion(activoAhora).entradas).toString('utf8').length >= escrito.length, 5000, 'lectura', 'the server did not receive the typed input');
    const conActivo = pty.conexion(activoAhora);
    await esperar(([id, total]) => { const p = document.querySelector(`[data-panel-id="${id}"]`); return Number(p.dataset.bytesRecibidos) >= total && p.dataset.bytesEnCola === '0' && p.dataset.bytesEnVuelo === '0'; }, [activoAhora, conActivo.bytesEnviados], 5000, 'lectura', 'the echo was not received and parsed');
    const eco = await evaluar((id) => {
      const t = window.__srfHarness.terminal(id);
      const b = t.buffer.active;
      let texto = '';
      for (let i = 0; i < b.length; i++) texto += `${b.getLine(i).translateToString(true)}\n`;
      return texto.includes('$ echo hola');
    }, activoAhora);
    Object.assign(informe.tramos.entrada, {
      escrito,
      recibido_por_el_servidor: Buffer.concat(conActivo.entradas).toString('utf8'),
      tramas_invalidas_en_el_servidor: conActivo.tramasInvalidasRecibidas,
      eco_en_el_terminal: eco,
      tramas_enviadas_por_el_cliente: (await evaluar(leerPaneles)).find((p) => p.id === activoAhora).enviadas,
      tramas_recibidas_por_el_servidor: conActivo.tramasRecibidas,
    });

    // ---- churn ----------------------------------------------------------------------------------------------
    informe.etapa = 'churn';
    const ids = await evaluar(() => window.__srfHarness.ids());
    for (let k = 0; k < 3; k++) {
      await evaluar(() => window.__srfHarness.abrir(false));
      await asentar();
      await evaluar(() => window.__srfHarness.abrir(true));
      await asentar();
    }
    for (const id of ids) await evaluar((x) => window.__srfHarness.activar(x), id);
    await evaluar(() => window.__srfHarness.activar('srf-ng-0'));
    await evaluar(() => window.__srfHarness.reemplazarAgentes());
    const cortados = ids.length > 17 ? ['srf-ng-3', 'srf-ng-17'] : [ids[ids.length - 1]];
    for (const id of cortados) pty.cerrarConexion(id);
    await enServidor(() => cortados.every((id) => pty.aceptadas(id) >= 2), 10000, 'lectura', 'a cut stream did not reconnect');
    await esperar((cs) => cs.every((id) => document.querySelector(`[data-panel-id="${id}"]`).dataset.fase === 'abierto'), cortados, 10000, 'lectura', 'a reconnected panel did not return to the open phase');
    await pagina.waitForTimeout(200);
    const trasChurn = await evaluar(leerPaneles);
    Object.assign(informe.tramos.churn, {
      ciclos: 3,
      pestanas_activadas: ids.length,
      arrays_reemplazados: 1,
      flujos_cortados: cortados.length,
      reconexiones_por_panel: Object.fromEntries(cortados.map((id) => [id, trasChurn.find((p) => p.id === id).reconexiones])),
      conexiones_aceptadas_por_panel: Object.fromEntries(cortados.map((id) => [id, pty.aceptadas(id)])),
      fases_tras_reconectar: Object.fromEntries(cortados.map((id) => [id, trasChurn.find((p) => p.id === id).fase])),
      montajes_max_despues: Math.max(...trasChurn.filter((p) => p.modo === 'interactivo').map((p) => p.montajes), 0),
    });

    // ---- cajon ----------------------------------------------------------------------------------------------
    informe.etapa = 'cajon';
    await evaluar(() => window.__srfHarness.abrir(true));
    await asentar();
    const abierto = await evaluar(leerCaja, '[data-cajon]');
    const lienzoAntes = await evaluar(leerCaja, '[data-superficie-falsa]');
    const cajaCdp = async (selector) => {
      const nodo = await cdp.send('DOM.querySelector', { nodeId: raizCdp, selector });
      if (!nodo.nodeId) throw new ErrorMedicion('lectura', 1, `CDP DOM.querySelector: ${selector} not found`);
      const { model } = await cdp.send('DOM.getBoxModel', { nodeId: nodo.nodeId });
      const xs = [model.border[0], model.border[2], model.border[4], model.border[6]];
      const ys = [model.border[1], model.border[3], model.border[5], model.border[7]];
      return { x: Math.min(...xs), y: Math.min(...ys), w: Math.max(...xs) - Math.min(...xs), h: Math.max(...ys) - Math.min(...ys) };
    };
    const abiertoCdp = await cajaCdp('[data-cajon]');
    const lienzoCdpAntes = await cajaCdp('[data-superficie-falsa]');
    const cuerpo = await evaluar(leerCaja, '[data-cuerpo]');
    await evaluar(() => window.__srfHarness.abrir(false));
    await asentar();
    const plegado = await evaluar(leerCaja, '[data-cajon]');
    const plegadoCdp = await cajaCdp('[data-cajon]');
    const lienzoPlegado = await evaluar(leerCaja, '[data-superficie-falsa]');
    const lienzoCdpPlegado = await cajaCdp('[data-superficie-falsa]');
    const estadoPlegado = await evaluar(() => document.querySelector('[data-cajon]').dataset.estado);
    await evaluar(() => window.__srfHarness.abrir(true));
    await asentar();
    const reabierto = await evaluar(leerCaja, '[data-cajon]');
    const lienzoDespues = await evaluar(leerCaja, '[data-superficie-falsa]');
    Object.assign(informe.tramos.cajon, {
      abierto_altura: abierto === null ? null : abierto.h,
      plegado_altura: plegado === null ? null : plegado.h,
      reabierto_altura: reabierto === null ? null : reabierto.h,
      plegado_altura_cdp: plegadoCdp.h,
      superficie_antes: CAJA_A_TEXTO(lienzoAntes),
      superficie_plegado: CAJA_A_TEXTO(lienzoPlegado),
      superficie_despues: CAJA_A_TEXTO(lienzoDespues),
      superficie_cdp_antes: `${lienzoCdpAntes.w}x${lienzoCdpAntes.h}`,
      superficie_cdp_plegado: `${lienzoCdpPlegado.w}x${lienzoCdpPlegado.h}`,
      cuerpo_altura: cuerpo === null ? null : cuerpo.h,
      estado_plegado: estadoPlegado,
    });

    // ---- oculto: a collapsed drawer queues bytes and schedules no frame -----------------------------------------
    informe.etapa = 'oculto';
    await evaluar(() => window.__srfHarness.activar('srf-ng-0'));
    await evaluar(() => window.__srfHarness.abrir(false));
    await asentar();
    await pagina.waitForTimeout(150);
    const conexion0 = pty.conexion('srf-ng-0');
    const antesOculto = (await evaluar(leerPaneles)).find((p) => p.id === 'srf-ng-0');
    const rafs0 = (await evaluar(leerMontajesPagina)).rafs;
    const bytesTerminalAntes = await evaluar(() => {
      const t = window.__srfHarness.terminal('srf-ng-0');
      return t.buffer.active.length;
    });
    const trozoOculto = Buffer.alloc(80 * 1024, 'x');
    for (let i = 0; i < trozoOculto.length; i += 80) trozoOculto.write('\r\n', i + 78, 'latin1');
    // well over the high-water mark: the client must ask the server to pause while most of it is still on the server
    const totalOculto = Math.ceil((ALTA_MARCA_DECLARADA * 3) / trozoOculto.length) * trozoOculto.length;
    const bytesAntesDelServidor = conexion0.bytesEnviados;
    const clienteAntes = antesOculto.bytes_recibidos; // byte accounting is by difference from here: a panel that was remounted before this leg restarted its counters
    const servidorAntes = pty.bytesEnviadosA('srf-ng-0');
    await evaluar(() => window.__srfHarness.reiniciarEscrituras('srf-ng-0'));
    for (let i = 0; i < totalOculto; i += trozoOculto.length) pty.enviar('srf-ng-0', trozoOculto);
    await esperar((id) => Number(document.querySelector(`[data-panel-id="${id}"]`).dataset.pausas) >= 1, 'srf-ng-0', 15000, 'lectura', 'the client never asked the server to pause a hidden stream');
    await enServidor(() => conexion0.pausas >= 1 && conexion0.pausado, 5000, 'lectura', 'the server never saw PAUSE_FLOW');
    await pagina.waitForTimeout(400);
    const enOculto = (await evaluar(leerPaneles)).find((p) => p.id === 'srf-ng-0');
    const rafs1 = (await evaluar(leerMontajesPagina)).rafs;
    const bytesTerminalDespues = await evaluar(() => window.__srfHarness.terminal('srf-ng-0').buffer.active.length);
    Object.assign(informe.tramos.oculto, {
      cuadros_de_pagina: rafs1 - rafs0,
      cuadros_del_panel: enOculto.cuadros - antesOculto.cuadros,
      bytes_en_cola_al_final: enOculto.bytes_en_cola,
      pausas_cliente: enOculto.pausas,
      pausas_servidor: conexion0.pausas,
      bytes_ofrecidos_al_servidor: totalOculto,
      bytes_enviados_por_el_servidor: conexion0.bytesEnviados - bytesAntesDelServidor,
      bytes_retenidos_por_el_servidor: conexion0.cola.reduce((s, t) => s + t.length, 0),
      lineas_nuevas_en_el_buffer: bytesTerminalDespues - bytesTerminalAntes,
      alta_marca: ALTA_MARCA_DECLARADA,
    });
    const rafsAlReabrir0 = (await evaluar(leerMontajesPagina)).rafs;
    await evaluar(() => window.__srfHarness.abrir(true));
    await asentar();
    await enServidor(() => conexion0.cola.length === 0 && !conexion0.pausado, 60000, 'lectura', 'the server did not finish sending after RESUME_FLOW');
    await esperar(
      ([id, total]) => { const p = document.querySelector(`[data-panel-id="${id}"]`); return p.dataset.bytesEnCola === '0' && p.dataset.bytesEnVuelo === '0' && Number(p.dataset.bytesRecibidos) >= total; },
      ['srf-ng-0', clienteAntes + (pty.bytesEnviadosA('srf-ng-0') - servidorAntes)],
      60000,
      'lectura',
      'the reopened drawer did not drain its queue',
    );
    const tras = (await evaluar(leerPaneles)).find((p) => p.id === 'srf-ng-0');
    const escrituras = await evaluar(() => window.__srfHarness.escrituras('srf-ng-0'));
    Object.assign(informe.tramos.oculto, {
      escrituras_al_reabrir: escrituras.llamadas,
      lote_max_bytes: escrituras.max,
      lote_max_declarado: LOTE_MAX_DECLARADO,
      bytes_escritos_a_xterm: escrituras.bytes,
      cuadros_de_pagina_al_reabrir: (await evaluar(leerMontajesPagina)).rafs - rafsAlReabrir0,
      pausas_al_final_cliente: tras.pausas,
      pausas_al_final_servidor: conexion0.pausas,
      reanudaciones_cliente: tras.reanudaciones,
      reanudaciones_servidor: conexion0.reanudaciones,
      bytes_recibidos_por_el_cliente: tras.bytes_recibidos - clienteAntes,
      bytes_enviados_al_final: pty.bytesEnviadosA('srf-ng-0') - servidorAntes,
      cola_al_final: tras.bytes_en_cola,
    });

    // ---- inactivo: a tab that is not the active one queues too -------------------------------------------------------
    informe.etapa = 'inactivo';
    const inactivoId = ids.length > 5 ? 'srf-ng-5' : ids[ids.length - 1];
    await evaluar(() => window.__srfHarness.activar('srf-ng-0'));
    await pagina.waitForTimeout(150);
    const antesInactivo = (await evaluar(leerPaneles)).find((p) => p.id === inactivoId);
    const rafsI0 = (await evaluar(leerMontajesPagina)).rafs;
    const pequeno = Buffer.from('inactive tab output\r\n'.repeat(500));
    pty.enviar(inactivoId, pequeno);
    await esperar(([id, base]) => Number(document.querySelector(`[data-panel-id="${id}"]`).dataset.bytesEnCola) >= base, [inactivoId, pequeno.length], 5000, 'lectura', 'the inactive panel did not queue the bytes');
    await pagina.waitForTimeout(200);
    const enInactivo = (await evaluar(leerPaneles)).find((p) => p.id === inactivoId);
    const rafsI1 = (await evaluar(leerMontajesPagina)).rafs;
    Object.assign(informe.tramos.inactivo, {
      cuadros_del_panel: enInactivo.cuadros - antesInactivo.cuadros,
      bytes_en_cola: enInactivo.bytes_en_cola,
      cuadros_de_pagina: rafsI1 - rafsI0,
    });
    await evaluar((id) => window.__srfHarness.activar(id), inactivoId);
    await esperar((id) => { const p = document.querySelector(`[data-panel-id="${id}"]`); return p.dataset.bytesEnCola === '0' && p.dataset.bytesEnVuelo === '0'; }, inactivoId, 10000, 'lectura', 'the activated tab did not drain');
    const trasInactivo = (await evaluar(leerPaneles)).find((p) => p.id === inactivoId);
    Object.assign(informe.tramos.inactivo, { cuadros_tras_activar: trasInactivo.cuadros - enInactivo.cuadros, cola_tras_activar: trasInactivo.bytes_en_cola });

    // ---- texto: a plain-text pane past the 56th instance shows its stream once it is shown ---------------------------------
    informe.etapa = 'texto';
    const idsTexto = paneles0.filter((p) => p.modo === 'texto').map((p) => p.id);
    let conContenido = 0;
    let muestra = null;
    for (const id of idsTexto) {
      await evaluar((x) => window.__srfHarness.activar(x), id);
      await esperar(
        (x) => { const p = document.querySelector(`[data-panel-id="${x}"]`); return p.dataset.bytesEnCola === '0' && p.dataset.bytesEnVuelo === '0' && p.querySelector('[data-panel-texto]').textContent.length > 0; },
        id,
        10000,
        'lectura',
        `the plain-text pane ${id} never showed its stream`,
      );
      const t = await evaluar((x) => document.querySelector(`[data-panel-id="${x}"] [data-panel-texto]`).textContent, id);
      if (t.length > 0) conContenido++;
      if (muestra === null) {
        muestra = t;
        // the pane keeps at most the limit's worth of lines: write more than that and count what it shows
        const escritasTexto = SCROLLBACK_DECLARADO + MARGEN_DE_LINEAS;
        let cuerpoTexto = '';
        for (let i = 1; i <= escritasTexto; i++) cuerpoTexto += `linea ${i}\r\n`;
        const clienteTexto = (await evaluar(leerPaneles)).find((p) => p.id === id).bytes_recibidos;
        const servidorTexto = pty.bytesEnviadosA(id);
        pty.enviar(id, cuerpoTexto);
        await esperar(
          ([x, total]) => { const p = document.querySelector(`[data-panel-id="${x}"]`); return p.dataset.bytesEnCola === '0' && p.dataset.bytesEnVuelo === '0' && Number(p.dataset.bytesRecibidos) >= total; },
          [id, clienteTexto + (pty.bytesEnviadosA(id) - servidorTexto)],
          15000,
          'lectura',
          `the plain-text pane ${id} did not take the lines written to it`,
        );
        const lineasTexto = (await evaluar((x) => document.querySelector(`[data-panel-id="${x}"] [data-panel-texto]`).textContent, id)).split('\n');
        const mt = /^linea (\d+)$/.exec(lineasTexto[0]);
        // the greeting line and the partial "$ " prompt are lines too: the last complete line is line number escritas
        const numeroPrimeraTexto = mt === null ? null : Number(mt[1]);
        const esperadaPrimeraTexto = escritasTexto - SCROLLBACK_DECLARADO + 1;
        Object.assign(informe.tramos.texto.tope, { escritas: escritasTexto, lineas_conservadas: lineasTexto.length, numero_primera: numeroPrimeraTexto, esperada_primera: esperadaPrimeraTexto, coincide: numeroPrimeraTexto === esperadaPrimeraTexto });
      }
    }
    if (idsTexto.length > 0) await evaluar(() => window.__srfHarness.activar('srf-ng-0'));
    Object.assign(informe.tramos.texto, { paneles: idsTexto.length, con_contenido_tras_activar: conContenido, primera_muestra: muestra });

    // ---- foco: the three passes win the focus back from an element that steals it twice ----------------------------
    informe.etapa = 'foco';
    const destinoFoco = ids.length > 7 ? 'srf-ng-7' : ids[ids.length - 1];
    await evaluar(() => window.__srfHarness.activar('srf-ng-0'));
    await pagina.waitForTimeout(600);
    const pasadas0 = (await evaluar(leerPaneles)).find((p) => p.id === destinoFoco).pasadas_de_foco;
    const inicioFoco = await evaluar((destino) => {
      document.querySelector('[data-intruso]').focus();
      // everything the browser logs from here on belongs to the leg (an index, not a timestamp: two calls of
      // performance.now() inside one clock tick are equal, and the intruder's own first focus would slip in)
      const desde = window.__srfMedicion.foco.length;
      const t0 = performance.now();
      window.__srfHarness.activar(destino);
      for (const ms of [60, 250]) window.setTimeout(() => document.querySelector('[data-intruso]').focus(), ms);
      return { desde, t0 };
    }, destinoFoco);
    await pagina.waitForTimeout(800);
    const foco = await evaluar(({ desde, t0 }) => ({ log: window.__srfMedicion.foco.slice(desde).map((f) => ({ t: Math.round(f.t - t0), panel: f.panel, intruso: f.intruso, textarea: f.textarea })), final: (() => { const a = document.activeElement; const p = a && a.closest ? a.closest('[data-panel-id]') : null; return p ? p.getAttribute('data-panel-id') : null; })() }), inicioFoco);
    const pasadas1 = (await evaluar(leerPaneles)).find((p) => p.id === destinoFoco).pasadas_de_foco;
    Object.assign(informe.tramos.foco, {
      pasadas_del_panel: pasadas1 - pasadas0,
      secuencia: foco.log.map((f) => (f.intruso ? 'intruso' : f.panel === destinoFoco && f.textarea ? 'panel' : `otro:${f.panel}`)),
      foco_final_en_el_panel: foco.final === destinoFoco,
      intrusos: foco.log.filter((f) => f.intruso).length,
      pasos_declarados: PASOS_DE_FOCO_DECLARADOS,
    });

    // ---- raton --------------------------------------------------------------------------------------------------------
    informe.etapa = 'raton';
    await evaluar(() => window.__srfHarness.activar('srf-ng-0'));
    await pagina.waitForTimeout(600);
    await evaluar(() => {
      const pantalla = document.querySelector('[data-panel-id="srf-ng-0"] .xterm-screen');
      const cuentas = { derecho: 0, izquierdo: 0 };
      window.__srfRaton = { cuentas };
      for (const tipo of ['mousedown', 'mouseup', 'pointerdown', 'pointerup', 'contextmenu', 'auxclick']) {
        pantalla.addEventListener(tipo, (ev) => {
          if (ev.button === 2) cuentas.derecho++;
          if (ev.button === 0) cuentas.izquierdo++;
        });
      }
    });
    const sintetico = await evaluar(() => {
      const pantalla = document.querySelector('[data-panel-id="srf-ng-0"] .xterm-screen');
      const salida = { derecho_cancelado: [], izquierdo_cancelado: [] };
      for (const tipo of ['mousedown', 'mouseup', 'contextmenu']) {
        const ev = new MouseEvent(tipo, { button: 2, bubbles: true, cancelable: true });
        salida.derecho_cancelado.push(!pantalla.dispatchEvent(ev));
      }
      for (const tipo of ['mousedown', 'mouseup']) {
        const ev = new MouseEvent(tipo, { button: 0, bubbles: true, cancelable: true });
        salida.izquierdo_cancelado.push(!pantalla.dispatchEvent(ev));
      }
      return { ...salida, cuentas: { ...window.__srfRaton.cuentas } };
    });
    const cajaPantalla = await evaluar(leerCaja, '[data-panel-id="srf-ng-0"] .xterm-screen');
    const derechoAntesReal = await evaluar(() => window.__srfRaton.cuentas.derecho);
    await pagina.mouse.click(cajaPantalla.x + 40, cajaPantalla.y + 20, { button: 'right' });
    const derechoTrasReal = await evaluar(() => window.__srfRaton.cuentas.derecho);
    Object.assign(informe.tramos.raton, {
      derecho_llegado_al_destino: sintetico.cuentas.derecho,
      derecho_cancelado: sintetico.derecho_cancelado.every((x) => x === true),
      izquierdo_llegado_al_destino: sintetico.cuentas.izquierdo,
      derecho_real_llegado_al_destino: derechoTrasReal - derechoAntesReal,
    });

    // ---- ime ----------------------------------------------------------------------------------------------------------
    informe.etapa = 'ime';
    const antesIme = pty.conexion('srf-ng-0').entradas.length;
    await evaluar(() => {
      const ta = window.__srfHarness.terminal('srf-ng-0').textarea;
      ta.value = '';
      ta.dispatchEvent(new CompositionEvent('compositionstart', { bubbles: true, data: '' }));
      ta.value = 'ñ';
      ta.dispatchEvent(new CompositionEvent('compositionupdate', { bubbles: true, data: 'ñ' }));
      ta.dispatchEvent(new CompositionEvent('compositionend', { bubbles: true, data: 'ñ' }));
    });
    await enServidor(() => pty.conexion('srf-ng-0').entradas.length > antesIme, 5000, 'lectura', 'the composed character never reached the server');
    await pagina.waitForTimeout(150);
    const valorTextarea = await evaluar(() => window.__srfHarness.terminal('srf-ng-0').textarea.value);
    Object.assign(informe.tramos.ime, {
      textarea_tras_la_composicion: valorTextarea,
      recibido_por_el_servidor: Buffer.concat(pty.conexion('srf-ng-0').entradas.slice(antesIme)).toString('utf8'),
    });

    // ---- scrollback ---------------------------------------------------------------------------------------------------
    informe.etapa = 'scrollback';
    const idScroll = 'srf-ng-1';
    await evaluar((id) => window.__srfHarness.activar(id), idScroll);
    await pagina.waitForTimeout(150);
    const filasScroll = await evaluar((id) => window.__srfHarness.terminal(id).rows, idScroll);
    const escritas = filasScroll + Math.max(o.scrollbackPedido, SCROLLBACK_DECLARADO) + 600;
    let lineas = '';
    for (let i = 1; i <= escritas; i++) lineas += `linea ${i}\r\n`;
    const conexionScroll = pty.conexion(idScroll);
    const enviadosAntes = conexionScroll.bytesEnviados;
    const clienteScrollAntes = (await evaluar(leerPaneles)).find((p) => p.id === idScroll).bytes_recibidos;
    const servidorScrollAntes = pty.bytesEnviadosA(idScroll);
    pty.enviar(idScroll, lineas);
    await enServidor(() => conexionScroll.cola.length === 0 && conexionScroll.bytesEnviados - enviadosAntes >= Buffer.byteLength(lineas), 15000, 'lectura', 'the server did not send the scrollback lines');
    await esperar(
      ([id, total]) => { const p = document.querySelector(`[data-panel-id="${id}"]`); return p.dataset.bytesEnCola === '0' && p.dataset.bytesEnVuelo === '0' && Number(p.dataset.bytesRecibidos) >= total; },
      [idScroll, clienteScrollAntes + (pty.bytesEnviadosA(idScroll) - servidorScrollAntes)],
      30000,
      'lectura',
      'the scrollback lines were not parsed',
    );
    const buffer = await evaluar((id) => {
      const t = window.__srfHarness.terminal(id);
      const b = t.buffer.normal;
      return { largo: b.length, filas: t.rows, primera: b.getLine(0).translateToString(true), opcion: t.options.scrollback };
    }, idScroll);
    const ret = retencionDeBuffer({ largo: buffer.largo, filas: buffer.filas, primeraLinea: buffer.primera, escritas });
    Object.assign(informe.tramos.scrollback, {
      escritas,
      filas: buffer.filas,
      retenidas: ret.retenidas,
      opcion: buffer.opcion,
      numero_primera_linea: ret.numero_primera,
      esperada_primera_linea: ret.esperada_primera,
      coincide: ret.coincide,
      bytes_cliente: (await evaluar(leerPaneles)).find((p) => p.id === idScroll).bytes_recibidos - clienteScrollAntes,
      bytes_servidor: pty.bytesEnviadosA(idScroll) - servidorScrollAntes,
    });

    // ---- scrollback of EVERY interactive terminal: the oracle's field is the most lines ANY of them retains -----------------
    informe.etapa = 'scrollback_todas';
    const idsInteractivos = ids.slice(0, Math.min(ids.length, PANELES_INTERACTIVOS_DECLARADOS));
    const filasMax = Math.max(...(await evaluar(leerPaneles)).filter((p) => p.modo === 'interactivo').map((p) => p.filas));
    const escritasTodas = filasMax + SCROLLBACK_DECLARADO + MARGEN_DE_LINEAS;
    const lecturasBuffer = await evaluar(async ({ interactivos, escritas }) => {
      let carga = '';
      for (let i = 1; i <= escritas; i++) carga += `linea ${i}\r\n`;
      for (const id of interactivos) window.__srfHarness.terminal(id).reset(); // a clean buffer, so its first line is numbered
      await Promise.all(interactivos.map((id) => window.__srfHarness.escribirDirecto(id, carga)));
      return interactivos.map((id) => {
        const t = window.__srfHarness.terminal(id);
        const b = t.buffer.normal;
        return { id, largo: b.length, filas: t.rows, primeraLinea: b.getLine(0).translateToString(true), opcion: t.options.scrollback };
      });
    }, { interactivos: idsInteractivos, escritas: escritasTodas });
    const atributos = Object.fromEntries((await evaluar(leerPaneles)).map((p) => [p.id, p.scrollback_atributo]));
    const resumenScroll = resumirScrollback(lecturasBuffer.map((l) => ({ ...l, atributo: atributos[l.id] })), escritasTodas);
    Object.assign(informe.tramos.scrollback.todas, {
      terminales: resumenScroll.terminales,
      escritas: escritasTodas,
      retenidas_max: resumenScroll.retenidas_max,
      retenidas_min: resumenScroll.retenidas_min,
      opcion_max: resumenScroll.opcion_max,
      atributo_max: resumenScroll.atributo_max,
      coinciden: resumenScroll.coinciden,
      mayor: resumenScroll.mayor,
    });

    // ---- final reads: the fields of the oracle and their second sources ---------------------------------------------------
    informe.etapa = 'lectura';
    const panelesFin = await evaluar(leerPaneles);
    const moFin = await evaluar(leerMontajesPagina);
    const montajesCallback = await evaluar(() => window.__srfHarness.montajesPorLlamada());
    const rechazosPagina = await evaluar(() => window.__srfHarness.rechazos());
    const solicitudesPagina = await evaluar(() => window.__srfHarness.solicitudes());
    const activaciones = await evaluar(() => window.__srfHarness.activaciones());
    const solicitados = await evaluar(() => window.__srfHarness.addonsSolicitados());
    const pedidoEnLaPagina = await evaluar(() => document.documentElement.dataset.rendererPedido);
    const cajaAlturaFinal = await evaluar(leerCaja, '[data-cajon]');
    const cajaAlturaCdp = await cajaCdp('[data-cajon]');
    // second source of the panel count and of the structure: CDP node queries
    const contarCdp = async (selector) => (await cdp.send('DOM.querySelectorAll', { nodeId: raizCdp, selector })).nodeIds.length;
    const panelesCdp = await contarCdp('[data-terminal-panel]');
    const interactivosCdp = await contarCdp('[data-terminal-panel][data-modo="interactivo"]');
    const filasCdp = await contarCdp('.xterm-screen .xterm-rows');
    const lienzosCdp = await contarCdp('[data-terminal-panel] canvas');
    const abiertasServidor = pty.abiertas();
    await cdp.send('DOM.disable');
    const avisosDeGuarda = avisosDeConsola.filter((t) => t.startsWith('termguard: refused '));
    informe.consola_avisos = avisosDeConsola.slice(0, 20);

    const interactivos = panelesFin.filter((p) => p.modo === 'interactivo');
    const texto = panelesFin.filter((p) => p.modo === 'texto');
    // a canvas ANYWHERE in a panel is a canvas terminal (the criterion of termguard.leerRenderer, read here by the instrument)
    const lecturasPagina = interactivos.map((p) => decidirRendererLeido(p.filas_dom, p.lienzos_panel));
    const lecturasComponente = interactivos.map((p) => p.renderer_componente);
    const rendererPagina = rendererDeConjunto(lecturasPagina);
    const rendererComponente = rendererDeConjunto(lecturasComponente);
    const rendererCdp = lienzosCdp > 0 ? (filasCdp === 0 ? 'canvas' : 'mixto') : filasCdp === interactivosCdp && interactivosCdp > 0 ? 'dom' : 'desconocido';
    const lienzosEnPaneles = interactivos.reduce((t, p) => t + p.lienzos_panel, 0);
    const lienzosEnPantalla = interactivos.reduce((t, p) => t + p.lienzos, 0);

    const montajesMo = {};
    const montajesComp = {};
    const montajesLlamada = {};
    for (const p of interactivos) {
      montajesMo[p.id] = moFin.montajes[p.id] ?? 0;
      montajesComp[p.id] = p.montajes;
      montajesLlamada[p.id] = montajesCallback[p.id] ?? 0;
    }
    const rem = remontajesDe(montajesMo);
    const insercionesPaginaEnLaVentana = moFin.total - mo0.total;

    // the fit: every interactive terminal fills its host to within one row, and what the server was told is what the terminal is
    const ajuste = interactivos.map((p) => {
      const dims = pty.conexion(p.id)?.dims ?? null;
      return {
        cabe: p.pantalla_h <= p.anfitrion_h + 0.5,
        llena: p.anfitrion_h - p.pantalla_h < p.fila_h + 0.5,
        coherente: dims !== null && dims.cols === p.columnas && dims.rows === p.filas,
      };
    });
    Object.assign(informe.tramos.ajuste, {
      paneles_medidos: interactivos.length,
      filas_min: Math.min(...interactivos.map((p) => p.filas)),
      filas_max: Math.max(...interactivos.map((p) => p.filas)),
      columnas_min: Math.min(...interactivos.map((p) => p.columnas)),
      anfitrion_alto: interactivos[0].anfitrion_h,
      fila_alto: interactivos[0].fila_h,
      no_caben: ajuste.filter((a) => !a.cabe).length,
      no_llenan: ajuste.filter((a) => !a.llena).length,
      sin_resize_coherente: ajuste.filter((a) => !a.coherente).length,
    });

    const primera = {
      renderer: rendererPagina,
      montajes_por_panel: montajesMo,
      altura_cajon: cajaAlturaFinal === null ? null : cajaAlturaFinal.h,
      paneles: panelesFin.length,
      rechazos_de_addon: rechazosPagina.length,
      retenidas: resumenScroll.retenidas_max,
      lienzos_en_paneles: lienzosEnPaneles,
      lienzos_fuera_de_la_pantalla: lienzosEnPaneles - lienzosEnPantalla,
    };
    const segunda = {
      renderer_componente: rendererComponente,
      renderer_cdp: rendererCdp,
      filas_dom_cdp: filasCdp,
      lienzos_cdp: lienzosCdp,
      montajes_por_panel_componente: montajesComp,
      montajes_por_panel_callback: montajesLlamada,
      inserciones_xterm_cdp: insertadosCdp.n,
      inserciones_xterm_pagina_en_la_ventana: insercionesPaginaEnLaVentana,
      altura_cajon_cdp: cajaAlturaCdp.h,
      paneles_cdp: panelesCdp,
      conexiones_servidor: abiertasServidor,
      rechazos_consola: avisosDeGuarda.length,
      primera_linea_numero: ret.numero_primera,
      lienzos_cdp_en_paneles: lienzosCdp,
    };
    Object.assign(informe.primera_fuente, primera);
    Object.assign(informe.segunda_fuente, segunda);
    informe.paneles_interactivos = interactivos.length;
    informe.paneles_texto = texto.length;
    informe.paneles_texto_con_contenido = texto.filter((p) => p.texto_longitud > 0).length;
    informe.addons_solicitados = solicitados;
    informe.scrollback_opcion = buffer.opcion;
    informe.scrollback_opcion_max = resumenScroll.opcion_max;
    Object.assign(informe.servidor, {
      conexiones_aceptadas: ids.reduce((t, id) => t + pty.aceptadas(id), 0),
      pausas: ids.reduce((t, id) => t + pty.historial(id).reduce((u, c) => u + c.pausas, 0), 0),
      reanudaciones: ids.reduce((t, id) => t + pty.historial(id).reduce((u, c) => u + c.reanudaciones, 0), 0),
      tramas_invalidas_recibidas: ids.reduce((t, id) => t + pty.historial(id).reduce((u, c) => u + c.tramasInvalidasRecibidas, 0), 0),
      errores_de_socket: ids.flatMap((id) => pty.historial(id).flatMap((c) => c.errores)),
    });

    // ---- cross-source agreement: a field on which two sources disagree is published as null ----------------------------
    informe.etapa = 'acuerdo_entre_fuentes';
    const anotar = (campo, textoDesacuerdo) => {
      informe.desacuerdos.push(textoDesacuerdo);
      if (!informe.campos_en_desacuerdo.includes(campo)) informe.campos_en_desacuerdo.push(campo);
    };
    if (erroresDePagina.length > 0) throw new ErrorMedicion('lectura', 4, `the page threw ${erroresTotales} uncaught error(s): ${erroresDePagina.join(' | ')}`);
    if (pedidoEnLaPagina !== o.renderer) anotar('renderer', `the page reports it requested renderer ${JSON.stringify(pedidoEnLaPagina)}, the flag said ${JSON.stringify(o.renderer)}`);
    if (o.renderer !== 'dom' && solicitados !== interactivosEsperados) anotar('renderer', `the harness created ${solicitados} addon request(s), ${interactivosEsperados} terminals were expected to make one`);
    // H-7: every renderer addon that was requested must have been refused, one for one, by the name it carries, and none may have run
    for (const t of desacuerdosDeAddons({
      solicitudes: solicitudesPagina.map((x) => (x.nombre === '' ? '(anonymous)' : x.nombre)),
      rechazos: rechazosPagina.map((r) => r.addon),
      activaciones,
      lienzosFueraDeLaPantalla: lienzosEnPaneles - lienzosEnPantalla,
      lienzosCdp,
    })) anotar('rechazos_de_addon', t);
    if (interactivos.length !== interactivosEsperados) anotar('paneles', `interactive panels: page ${interactivos.length} vs expected ${interactivosEsperados}`);
    if (texto.length !== textoEsperados) anotar('paneles', `plain-text panels: page ${texto.length} vs expected ${textoEsperados}`);
    if (panelesFin.length !== panelesCdp) anotar('paneles', `panels: page ${panelesFin.length} vs CDP ${panelesCdp}`);
    if (interactivosCdp !== interactivos.length) anotar('paneles', `interactive panels: page ${interactivos.length} vs CDP ${interactivosCdp}`);
    if (o.paneles !== panelesFin.length) anotar('paneles', `panels: requested ${o.paneles} vs page ${panelesFin.length}`);
    if (abiertasServidor !== o.paneles) anotar('paneles', `panels: requested ${o.paneles} vs server open connections ${abiertasServidor}`);
    if (rendererPagina !== rendererComponente) anotar('renderer', `renderer: instrument ${rendererPagina} vs component ${rendererComponente}`);
    if (rendererPagina !== rendererCdp) anotar('renderer', `renderer: page ${rendererPagina} vs CDP ${rendererCdp}`);
    for (const t of compararMontajes(montajesMo, montajesLlamada, 'observer', 'alMontar callback')) anotar('remontajes_max', t);
    for (const p of interactivos) if (montajesComp[p.id] > montajesLlamada[p.id]) anotar('remontajes_max', `mounts of ${p.id}: the component instance counts ${montajesComp[p.id]}, more than the ${montajesLlamada[p.id]} alMontar callbacks`);
    if (Object.values(montajesMo).some((v) => v < 1)) anotar('remontajes_max', 'an interactive panel was never mounted according to the observer');
    if (insertadosCdp.n !== insercionesPaginaEnLaVentana) anotar('remontajes_max', `.xterm insertions after the mount leg: page observer ${insercionesPaginaEnLaVentana} vs CDP ${insertadosCdp.n}`);
    for (const p of texto) if (p.montajes < 1) anotar('remontajes_max', `plain-text panel ${p.id} was never mounted`);
    if (primera.altura_cajon !== segunda.altura_cajon_cdp) anotar('altura_cajon', `drawer height: page ${primera.altura_cajon} vs CDP ${segunda.altura_cajon_cdp}`);
    if (ret.retenidas !== buffer.opcion && buffer.opcion <= SCROLLBACK_DECLARADO) anotar('scrollback', `scrollback: retained ${ret.retenidas} vs option ${buffer.opcion}`);
    if (!ret.coincide) anotar('scrollback', `scrollback: retained ${ret.retenidas} lines but the first retained line is ${JSON.stringify(buffer.primera)}, expected number ${ret.esperada_primera}`);
    for (const t of resumenScroll.desacuerdos) anotar('scrollback', t);
    if (primera.rechazos_de_addon !== segunda.rechazos_consola) anotar('rechazos_de_addon', `guard refusals: page log ${primera.rechazos_de_addon} vs console warnings ${segunda.rechazos_consola}`);
    if (informe.tramos.entrada.recibido_por_el_servidor !== escrito) anotar('entrada', `input: typed ${JSON.stringify(escrito)} vs decoded by the server ${JSON.stringify(informe.tramos.entrada.recibido_por_el_servidor)}`);
    if (informe.tramos.oculto.bytes_recibidos_por_el_cliente !== informe.tramos.oculto.bytes_enviados_al_final) anotar('oculto', `bytes of the hidden-drawer stream: client ${informe.tramos.oculto.bytes_recibidos_por_el_cliente} vs server ${informe.tramos.oculto.bytes_enviados_al_final}`);
    if (informe.tramos.oculto.bytes_escritos_a_xterm !== informe.tramos.oculto.bytes_recibidos_por_el_cliente) anotar('oculto', `bytes of the hidden-drawer stream: handed to xterm ${informe.tramos.oculto.bytes_escritos_a_xterm} vs received by the client ${informe.tramos.oculto.bytes_recibidos_por_el_cliente}`);
    if (informe.tramos.scrollback.bytes_cliente !== informe.tramos.scrollback.bytes_servidor) anotar('scrollback', `bytes of the scrollback stream: client ${informe.tramos.scrollback.bytes_cliente} vs server ${informe.tramos.scrollback.bytes_servidor}`);
    if (panelesFin.some((p) => p.tramas_invalidas > 0 || p.saltos_de_secuencia > 0)) anotar('flujo', 'a panel counted invalid frames or a sequence gap on a stream the stand-in server wrote correctly');
    if (informe.servidor.tramas_invalidas_recibidas > 0) anotar('flujo', `the server received ${informe.servidor.tramas_invalidas_recibidas} malformed frame(s) from the client`);

    // ---- legs that change the world (a session ends, a panel is removed, a panel fails). They run after every reading
    // above and BEFORE the oracle's fields are set, so a leg that cannot run leaves all four of them null. Their waits do
    // not throw: what a leg could not see is published as false and judged by the consumer.
    const esperarSuave = async (fn, arg, ms) => {
      try {
        await conPlazoPropio(contraFallo(pagina.waitForFunction(fn, arg, { timeout: ms, polling: 50 })), ms);
        return true;
      } catch (e) {
        if (e instanceof ErrorMedicion) throw e;
        return false;
      }
    };
    const enServidorSuave = async (cond, ms) => {
      const limite = Date.now() + ms;
      while (Date.now() < limite) {
        if (cond()) return true;
        await pagina.waitForTimeout(25);
      }
      return false;
    };
    // They only run when every source agreed above: a run whose sources disagree is already a failed run (rc 3), and what these legs
    // would poke at (a terminal that is not the DOM renderer, say) is not worth reading further.
    if (informe.desacuerdos.length === 0) {
      const libres = [...idsInteractivos];
      const tomar = (preferido) => (libres.includes(preferido) ? libres.splice(libres.indexOf(preferido), 1)[0] : libres.pop());
      const idSalida = tomar('srf-ng-2');
      const idSonda = tomar('srf-ng-4');
      const idHostil = tomar('srf-ng-1');
      const idQuitar = tomar(idsInteractivos[idsInteractivos.length - 1]);

      // ---- pestanas: the arrow keys move between tabs, and wrap around
      informe.etapa = 'pestanas';
      const teclas = ['ArrowRight', 'ArrowRight', 'ArrowLeft', 'ArrowLeft', 'ArrowLeft'];
      const secuenciaEsperada = [];
      for (let i = 0, k = 0; k < teclas.length; k++) {
        i = (i + (teclas[k] === 'ArrowRight' ? 1 : -1) + ids.length) % ids.length;
        secuenciaEsperada.push(ids[i]);
      }
      await evaluar(() => window.__srfHarness.activar('srf-ng-0'));
      const secuenciaDom = [];
      const secuenciaEstado = [];
      for (const tecla of teclas) {
        await pagina.waitForTimeout(500); // the three focus passes of the last activation are over
        const actual = await evaluar(() => document.querySelector('[role="tab"][aria-selected="true"]').dataset.pestana);
        await pagina.locator(`[data-pestana="${actual}"]`).focus();
        await pagina.keyboard.press(tecla);
        const leido = await evaluar(() => ({ dom: (document.querySelector('[role="tab"][aria-selected="true"]') || { dataset: {} }).dataset.pestana ?? null, estado: window.__srfHarness.activoAhora() }));
        secuenciaDom.push(leido.dom);
        secuenciaEstado.push(leido.estado);
      }
      Object.assign(informe.tramos.pestanas, {
        teclas,
        secuencia_dom: secuenciaDom,
        secuencia_estado: secuenciaEstado,
        secuencia_esperada: secuenciaEsperada,
        coincide: JSON.stringify(secuenciaDom) === JSON.stringify(secuenciaEsperada),
      });
      await evaluar(() => window.__srfHarness.activar('srf-ng-0'));

      // ---- salida: EXIT writes the [process exited] line into the terminal and ends the session for good
      if (idSalida !== undefined) {
        informe.etapa = 'salida';
        const lineaDeSalida = `[process exited: code ${CODIGO_DE_SALIDA}]`;
        await evaluar((x) => window.__srfHarness.activar(x), idSalida);
        await pagina.waitForTimeout(700);
        const aceptadasAntes = pty.aceptadas(idSalida);
        pty.enviarControl(idSalida, { type: 'EXIT', code: CODIGO_DE_SALIDA });
        pty.cerrarConexion(idSalida, 1000);
        const enBuffer = await esperarSuave(
          ([x, linea]) => {
            const b = window.__srfHarness.terminal(x).buffer.active;
            for (let i = 0; i < b.length; i++) if (b.getLine(i).translateToString(true).includes(linea)) return true;
            return false;
          },
          [idSalida, lineaDeSalida],
          5000,
        );
        await pagina.waitForTimeout(400);
        const enDom = await evaluar(([x, linea]) => { const filas = document.querySelector(`[data-panel-id="${x}"] .xterm-rows`); return filas !== null && filas.textContent.includes(linea); }, [idSalida, lineaDeSalida]);
        Object.assign(informe.tramos.salida, {
          codigo_enviado: CODIGO_DE_SALIDA,
          linea_en_el_buffer: enBuffer,
          linea_en_el_dom: enDom,
          fase_final: (await evaluar(leerPaneles)).find((p) => p.id === idSalida).fase,
          conexiones_aceptadas_antes: aceptadasAntes,
          conexiones_aceptadas_despues: pty.aceptadas(idSalida),
        });
        await evaluar(() => window.__srfHarness.activar('srf-ng-0'));
      }

      // ---- capa_tardia: a canvas layer that shows up after the mount fails the panel (line 2 of the guard)
      if (idSonda !== undefined) {
        informe.etapa = 'capa_tardia';
        await evaluar((x) => window.__srfHarness.inyectarCapaTardia(x), idSonda);
        await esperarSuave((x) => document.querySelector(`[data-panel-id="${x}"]`).dataset.fase === 'fallido', idSonda, 3000);
        const cerradaSonda = await enServidorSuave(() => pty.abiertas(idSonda) === 0, 3000);
        const trasSonda = (await evaluar(leerPaneles)).find((p) => p.id === idSonda);
        const aviso = await evaluar((x) => { const el = document.querySelector(`[data-panel-id="${x}"] [data-panel-fallo]`); return el === null ? null : el.textContent; }, idSonda);
        Object.assign(informe.tramos.capa_tardia, {
          panel: idSonda,
          fase_final: trasSonda.fase,
          aviso_visible: aviso,
          terminal_desmontado: (await evaluar(() => window.__srfHarness.desmontados())).includes(idSonda),
          conexion_cerrada_en_el_servidor: cerradaSonda,
        });
      }

      // ---- hostil: a control frame that is valid JSON but not an object is counted and dropped, never thrown
      if (idHostil !== undefined) {
        informe.etapa = 'hostil';
        const invalidasAntes = (await evaluar(leerPaneles)).find((p) => p.id === idHostil).tramas_invalidas;
        const erroresAntes = erroresTotales;
        pty.enviarControl(idHostil, null); // the text frame "null"
        await esperarSuave(([x, base]) => Number(document.querySelector(`[data-panel-id="${x}"]`).dataset.tramasInvalidas) > base, [idHostil, invalidasAntes], 3000);
        await pagina.waitForTimeout(200);
        Object.assign(informe.tramos.hostil, {
          panel: idHostil,
          tramas_invalidas_antes: invalidasAntes,
          tramas_invalidas_despues: (await evaluar(leerPaneles)).find((p) => p.id === idHostil).tramas_invalidas,
          errores_de_pagina_antes: erroresAntes,
          errores_de_pagina_despues: erroresTotales,
        });
      }

      // ---- desmontaje: removing an agent unmounts its panel and closes its WebSocket
      if (idQuitar !== undefined) {
        informe.etapa = 'desmontaje';
        const socketsAntes = await evaluar(contarSocketsAbiertos, idQuitar);
        const conexionesAntes = pty.abiertas(idQuitar);
        await evaluar((x) => window.__srfHarness.quitar(x), idQuitar);
        const fuera = await esperarSuave((x) => document.querySelector(`[data-panel-id="${x}"]`) === null, idQuitar, 5000);
        await enServidorSuave(() => pty.abiertas(idQuitar) === 0, 5000);
        Object.assign(informe.tramos.desmontaje, {
          panel: idQuitar,
          panel_fuera_del_dom: fuera,
          desmontado_por_callback: (await evaluar(() => window.__srfHarness.desmontados())).includes(idQuitar),
          sockets_abiertos_antes: socketsAntes,
          sockets_abiertos_despues: await evaluar(contarSocketsAbiertos, idQuitar),
          conexiones_abiertas_antes: conexionesAntes,
          conexiones_abiertas_despues: pty.abiertas(idQuitar),
        });
      }
    }
    if (erroresDePagina.length > 0) throw new ErrorMedicion('lectura', 4, `the page threw ${erroresTotales} uncaught error(s): ${erroresDePagina.join(' | ')}`);

    // A run that cannot vouch for one of its fields vouches for none: jq reads `null <= 1` as true, so a report with a
    // single null field must not leave the other three looking measured.
    const vale = (valor) => (informe.desacuerdos.length > 0 ? null : valor);
    informe.renderer = vale(rendererPagina);
    informe.paneles = vale(panelesFin.length);
    informe.remontajes_max = vale(rem.remontajes_max);
    informe.montajes_max = vale(rem.montajes_max);
    informe.remontajes_total = vale(rem.remontajes_total);
    informe.scrollback = vale(resumenScroll.retenidas_max);
    informe.altura_cajon = vale(primera.altura_cajon);
    informe.rechazos_de_addon = vale(primera.rechazos_de_addon);
    informe.activaciones_de_addon = vale(activaciones);
    informe.variantes_solicitadas = contar(solicitudesPagina.map((x) => x.variante));
    informe.variantes_rechazadas = contar(rechazosPagina.map((r) => r.addon));
    informe.acuerdo_entre_fuentes = informe.desacuerdos.length === 0;

    // what was seen of the page is published even on a run that failed later
    informe.diagnostico.montajes_observados_max = rem.montajes_max;
    informe.diagnostico.paneles_por_fase = panelesFin.reduce((m, p) => ({ ...m, [p.fase]: (m[p.fase] || 0) + 1 }), {});

    // a page that failed before the ready contract or during a leg: read what the observer saw, if the page can still be read
  } catch (e) {
    if (vencido !== null && !(e instanceof ErrorMedicion && e.etapa === 'listo')) e = new ErrorMedicion(vencido, 4, `the run exceeded its own bound of ${o.plazoTotal} ms at stage ${vencido}: ${primeraLinea(e.message)}`, e);
    // Read what can still be read (the mount counter of the observer) and hand it to the report, then rethrow.
    try {
      if (navegador !== null) {
        const paginas = navegador.contexts().flatMap((c) => c.pages());
        if (paginas.length > 0) {
          const mo = await conPlazo(paginas[0].evaluate(() => (window.__srfMedicion ? { montajes: { ...window.__srfMedicion.montajes }, total: window.__srfMedicion.montajesTotal } : null)), o.plazoLectura, () => new Error(`reading the page timed out after ${o.plazoLectura} ms`));
          if (mo !== null) {
            const r = remontajesDe(mo.montajes);
            informe.diagnostico.montajes_observados_max = r.montajes_max;
          }
        }
      }
    } catch (e2) {
      informe.diagnostico.error_de_lectura = primeraLinea(e2.message); // the page is gone or frozen: the run's own error is what is reported
    }
    if (informe.errores_de_pagina.length > 0) informe.diagnostico.ultimo_error_de_pagina = informe.errores_de_pagina[informe.errores_de_pagina.length - 1];
    throw e;
  } finally {
    if (temporizadorTotal !== null) clearTimeout(temporizadorTotal);
    informe.duracion_ms = Date.now() - inicio;
    const erroresDeCierre = informe.errores_de_cierre;
    if (navegador !== null) {
      const forzado = await cerrarNavegador(navegador, raizTemporal, PLAZO_DE_CIERRE_MS);
      if (forzado !== null) erroresDeCierre.push(`browser: ${forzado}; its processes were killed`);
    }
    if (pty !== null) await pty.cerrar().catch((e) => erroresDeCierre.push(`stand-in server: ${primeraLinea(e.message)}`));
    if (web !== null) await new Promise((ok) => web.servidor.close((e) => { if (e) erroresDeCierre.push(`static server: ${primeraLinea(e.message)}`); ok(); }));
    if (tmpAnterior === undefined) delete process.env.TMPDIR;
    else process.env.TMPDIR = tmpAnterior;
    for (const senal of Object.keys(CODIGOS_DE_SENAL)) process.off(senal, alSenal);
    if (raizTemporal !== null) rmSync(raizTemporal, { recursive: true, force: true, maxRetries: 10, retryDelay: 50 });
  }
}

/** Runs one measurement; returns the report and the exit code. Never throws. */
export async function ejecutar(argv) {
  const informe = informeVacio();
  informe.etapa = 'argumentos';
  let rc = 1;
  try {
    const o = leerArgumentos(argv);
    informe.renderer_pedido = o.renderer;
    informe.scrollback_pedido = o.scrollbackPedido;
    informe.plazos = { espera_listo_ms: o.esperaListo, plazo_lectura_ms: o.plazoLectura, plazo_total_ms: o.plazoTotal };
    informe.inyeccion = { scrollback: o.inyectarScrollback === null ? null : `${o.inyectarScrollback.id}=${o.inyectarScrollback.n}`, congelacion: o.inyectarCongelacion };
    await medir(o, informe);
    if (informe.acuerdo_entre_fuentes) {
      informe.ok = true;
      informe.etapa = 'completa';
      rc = 0;
    } else {
      informe.ok = false;
      informe.etapa = 'acuerdo_entre_fuentes';
      informe.error = `two sources of one field disagree: ${informe.desacuerdos.join('; ')}`;
      rc = 3;
    }
  } catch (e) {
    informe.ok = false;
    informe.error = e instanceof ErrorMedicion ? e.message : `measurement failed: ${primeraLinea(e && e.message ? e.message : e)}`;
    if (e instanceof ErrorMedicion && e.etapa !== informe.etapa && e.etapa !== undefined) informe.etapa = e.etapa;
    rc = e instanceof ErrorMedicion ? e.rc : 1;
  }
  informe.rc = rc;
  return { informe, rc };
}

async function main() {
  const argv = process.argv.slice(2);
  const { informe, rc } = await ejecutar(argv);
  process.stdout.write(`${argv.includes('--json') ? JSON.stringify(informe) : JSON.stringify(informe, null, 2)}\n`);
  if (rc !== 0) process.stderr.write(`medir-terminal: ${informe.etapa}: ${informe.error}\n`);
  return rc;
}

if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  process.exitCode = await main();
}
