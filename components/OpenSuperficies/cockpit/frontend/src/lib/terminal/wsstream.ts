// Binary WebSocket fabric client of the agents drawer (spec 6, spec 10, SUB_04 1.4). One FlujoWs is one PTY stream.
//
// WIRE. No terminal WebSocket server exists in the tree yet (cmd/srf-terminal has no serving verb), so this file
// defines the client side of the contract and the harness of scripts/ng/medir-terminal.mjs implements the other
// side independently. [TARGET] until a server ships it:
//   binary frames, both directions   an SRF2 frame (spec 10): a fixed little-endian header of 32 bytes, then
//                                    `count` records of `stride` bytes. PTY bytes travel with stride 1.
//                                      0..3   magic "SRF2"      4..5   version (u16)   6..7   stride (u16)
//                                      8..11  count (u32)       12..15 seq (u32)       16..19 ms (u32)
//                                      20..31 reserved, zero
//   text frames, both directions     one JSON object with a "type": PAUSE_FLOW, RESUME_FLOW, RESIZE {cols, rows}
//                                    (client to server), EXIT {code} (server to client). SUB_04 1.4 names the
//                                    first two.
// WHICH DEFINITION OF "SRF2" THIS FILE FOLLOWS. The corpus holds two that cannot both be right. (a) The current
// ESPEC section 10 fixes only the ORDER of the fields (magic, version, stride, count, seq, ms, 12 reserved bytes,
// little-endian) and no widths; the superseded v1.0 of that specification
// (historico/2026-09-16-plan-v3.0/ESPEC-FRONTEND-COCKPIT-CLAUDE-DESIGN.v1.0.md, "Wire format for internal frames")
// gives the widths used here, and only they add up to the 32 bytes that "12 reserved bytes" implies. (b)
// PAQUETE_CONTEXTO_DESTILADO_OPENSUPERFICIES_NEXT_GEN.md, subagent 12, defines a "SurfaceFrameHeader" under the same
// magic (SurfaceID u32, FrameSeq u64, TimestampNs i64, Format u8, geometry and damage rectangle, PayloadLen u32): the
// header of a surface FRAME (RGBA, PNG, Xpra delta), which has no stride, count or reserved bytes. This file follows
// (a): it is the only one consistent with ESPEC section 10, which is the specification in force. (b) is a different
// frame family for the video path, not this fabric. Until a server ships, the widths are the builder's inference.
// A frame that does not decode is counted (contadores.invalidas, published by the panel as data-tramas-invalidas),
// reported through alError and dropped: nothing a server sends can throw out of the socket's message handler. A
// control frame whose type this client does not act on is counted in contadores.control and nothing else.
//
// FLOW CONTROL. The bytes that arrive are not reactive state: they go to a ColaDeEscritura (the non-reactive
// mailbox of spec 10). When the bytes waiting plus the bytes handed to xterm and not yet parsed pass the high-water
// mark the queue asks for PAUSE_FLOW, and it asks for RESUME_FLOW once they fall below a quarter of it.
//
// RECONNECTING. A cut stream is reopened after a delay that doubles with every attempt up to ESPERA_MAX_MS. The
// attempt counter goes back to zero only after a connection has stayed up for ESTABLE_MS: a server that accepts the
// upgrade and drops at once is retried with growing delays and finally given up on, never every 50 ms forever.

export const MAGIA_TRAMA = [0x53, 0x52, 0x46, 0x32] as const; // "SRF2"
export const VERSION_TRAMA = 1;
export const TAM_CABECERA = 32;
/** SUB_04 1.4 [TARGET]: 2 MiB of bytes not yet parsed per terminal. */
export const ALTA_MARCA_BYTES = 2 * 1_024 * 1_024;
/** Bytes handed to xterm per animation frame: bounds the main-thread time of one frame. */
export const LOTE_MAX_BYTES = 256 * 1_024;

export class ErrorDeTrama extends Error {
  constructor(mensaje: string) {
    super(mensaje);
    this.name = 'ErrorDeTrama';
  }
}

export interface Trama {
  version: number;
  stride: number;
  cuenta: number;
  seq: number;
  ms: number;
  cuerpo: Uint8Array;
}

/** Builds one SRF2 frame. */
export function codificarTrama(cuerpo: Uint8Array, seq: number, ms: number, stride = 1): Uint8Array {
  if (!Number.isInteger(stride) || stride < 1 || stride > 0xffff) throw new RangeError(`stride ${String(stride)} is not in 1..65535`);
  if (cuerpo.byteLength % stride !== 0) throw new RangeError(`body of ${cuerpo.byteLength} bytes is not a whole number of ${stride}-byte records`);
  const salida = new Uint8Array(TAM_CABECERA + cuerpo.byteLength);
  const vista = new DataView(salida.buffer);
  salida.set(MAGIA_TRAMA, 0);
  vista.setUint16(4, VERSION_TRAMA, true);
  vista.setUint16(6, stride, true);
  vista.setUint32(8, cuerpo.byteLength / stride, true);
  vista.setUint32(12, seq >>> 0, true);
  vista.setUint32(16, Math.floor(ms) >>> 0, true);
  // bytes 20..31 are reserved and stay zero
  salida.set(cuerpo, TAM_CABECERA);
  return salida;
}

/** Reads one SRF2 frame; anything that does not hold the contract throws ErrorDeTrama. The body is a view, not a copy. */
export function decodificarTrama(datos: ArrayBuffer | Uint8Array): Trama {
  const bytes = datos instanceof Uint8Array ? datos : new Uint8Array(datos);
  if (bytes.byteLength < TAM_CABECERA) throw new ErrorDeTrama(`frame of ${bytes.byteLength} bytes is shorter than the ${TAM_CABECERA}-byte header`);
  for (let i = 0; i < MAGIA_TRAMA.length; i++) {
    if (bytes[i] !== MAGIA_TRAMA[i]) throw new ErrorDeTrama('bad magic: the frame does not start with "SRF2"');
  }
  const vista = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  const version = vista.getUint16(4, true);
  if (version !== VERSION_TRAMA) throw new ErrorDeTrama(`unsupported frame version ${version}`);
  const stride = vista.getUint16(6, true);
  if (stride < 1) throw new ErrorDeTrama('stride 0');
  const cuenta = vista.getUint32(8, true);
  for (let i = 20; i < TAM_CABECERA; i++) {
    if (bytes[i] !== 0) throw new ErrorDeTrama(`reserved byte ${i} is not zero`);
  }
  const largo = bytes.byteLength - TAM_CABECERA;
  if (cuenta * stride !== largo) throw new ErrorDeTrama(`count ${cuenta} x stride ${stride} = ${cuenta * stride} bytes, the frame carries ${largo}`);
  return { version, stride, cuenta, seq: vista.getUint32(12, true), ms: vista.getUint32(16, true), cuerpo: bytes.subarray(TAM_CABECERA) };
}

// ---- the non-reactive mailbox ---------------------------------------------------------------------------------
export interface OpcionesDeCola {
  altaMarca: number;
  alPausar(): void;
  alReanudar(): void;
}

/** Bytes waiting for a frame plus bytes handed to the terminal and not yet parsed, with the two flow-control edges. */
export class ColaDeEscritura {
  private trozos: Uint8Array[] = [];
  private enCola = 0;
  private enVuelo = 0;
  private pausada = false;
  pausas = 0;
  reanudaciones = 0;

  constructor(private readonly opciones: OpcionesDeCola) {}

  get bytesEnCola(): number {
    return this.enCola;
  }
  get bytesEnVuelo(): number {
    return this.enVuelo;
  }
  get pendientes(): number {
    return this.enCola + this.enVuelo;
  }
  get estaPausada(): boolean {
    return this.pausada;
  }

  publicar(bytes: Uint8Array): void {
    if (bytes.byteLength === 0) return;
    this.trozos.push(bytes);
    this.enCola += bytes.byteLength;
    this.evaluar();
  }

  /** Up to `max` bytes for one frame, or null when nothing waits. What it returns counts as in flight until confirmar(). */
  tomar(max: number): Uint8Array | null {
    if (this.trozos.length === 0) return null;
    const primero = this.trozos[0];
    let lote: Uint8Array;
    if (primero.byteLength >= max) {
      lote = primero.subarray(0, max);
      if (primero.byteLength === max) this.trozos.shift();
      else this.trozos[0] = primero.subarray(max);
    } else {
      const partes: Uint8Array[] = [];
      let total = 0;
      while (this.trozos.length > 0 && total < max) {
        const t = this.trozos[0];
        if (total + t.byteLength <= max) {
          partes.push(t);
          total += t.byteLength;
          this.trozos.shift();
        } else {
          const falta = max - total;
          partes.push(t.subarray(0, falta));
          this.trozos[0] = t.subarray(falta);
          total += falta;
        }
      }
      lote = new Uint8Array(total);
      let desplazamiento = 0;
      for (const p of partes) {
        lote.set(p, desplazamiento);
        desplazamiento += p.byteLength;
      }
    }
    this.enCola -= lote.byteLength;
    this.enVuelo += lote.byteLength;
    return lote;
  }

  /** The terminal parsed `n` bytes. */
  confirmar(n: number): void {
    this.enVuelo -= n;
    this.evaluar();
  }

  private evaluar(): void {
    const p = this.pendientes;
    if (!this.pausada && p > this.opciones.altaMarca) {
      this.pausada = true;
      this.pausas++;
      this.opciones.alPausar();
    } else if (this.pausada && p < this.opciones.altaMarca / 4) {
      this.pausada = false;
      this.reanudaciones++;
      this.opciones.alReanudar();
    }
  }
}

// ---- the stream -----------------------------------------------------------------------------------------------
export type FaseDeFlujo = 'conectando' | 'abierto' | 'reconectando' | 'cerrado' | 'fallido';

export interface OpcionesDeFlujo {
  url: string;
  alDatos(cuerpo: Uint8Array, trama: Trama): void;
  alFase?(fase: FaseDeFlujo, detalle: string): void;
  alError?(error: Error): void;
  /** The server said the session ended (EXIT). */
  alSalida?(codigo: number | null): void;
  crearSocket?: (url: string) => WebSocket;
  programar?: (fn: () => void, ms: number) => unknown;
  cancelar?: (manejador: unknown) => void;
  maxReconexiones?: number;
  esperaBaseMs?: number;
  /** How long a connection must stay up before the reconnection backoff restarts (ESTABLE_MS). */
  estableMs?: number;
}

export interface ContadoresDeFlujo {
  aperturas: number;
  tramas: number;
  bytes: number;
  invalidas: number;
  saltosDeSecuencia: number;
  control: number;
  enviadas: number;
  descartadas: number;
  reconexiones: number;
}

/** The longest wait between two attempts to reopen a cut stream. */
export const ESPERA_MAX_MS = 2_000;
/** A connection counts as healthy, and the attempt counter restarts, once it has stayed up this long. */
export const ESTABLE_MS = 2_000;

export class FlujoWs {
  fase: FaseDeFlujo = 'conectando';
  readonly contadores: ContadoresDeFlujo = { aperturas: 0, tramas: 0, bytes: 0, invalidas: 0, saltosDeSecuencia: 0, control: 0, enviadas: 0, descartadas: 0, reconexiones: 0 };
  private ws: WebSocket | null = null;
  private seqSalida = 0;
  private seqEntrada = -1;
  private inicio = 0;
  private dims: { cols: number; filas: number } | null = null;
  private pausado = false;
  private cerradoPorNosotros = false;
  private salidaRecibida = false;
  private intentos = 0;
  private temporizador: unknown = null;
  private estable: unknown = null;
  private readonly codificador = new TextEncoder();

  constructor(private readonly o: OpcionesDeFlujo) {}

  conectar(): void {
    this.abrir();
  }

  private cambiarFase(fase: FaseDeFlujo, detalle = ''): void {
    this.fase = fase;
    this.o.alFase?.(fase, detalle);
  }

  private abrir(): void {
    let ws: WebSocket;
    try {
      ws = (this.o.crearSocket ?? ((u: string) => new WebSocket(u)))(this.o.url);
    } catch (e) {
      const error = e instanceof Error ? e : new Error(String(e));
      this.o.alError?.(error);
      this.cambiarFase('fallido', error.message);
      return;
    }
    ws.binaryType = 'arraybuffer';
    this.ws = ws;
    ws.onopen = () => {
      if (this.ws !== ws) return;
      // the backoff restarts only when this connection has proved itself by staying up (see RECONNECTING above)
      this.cancelarEstable();
      this.estable = this.programar(() => {
        this.estable = null;
        if (this.ws === ws) this.intentos = 0;
      }, this.o.estableMs ?? ESTABLE_MS);
      this.contadores.aperturas++;
      this.seqSalida = 0;
      this.seqEntrada = -1;
      this.inicio = Date.now();
      this.cambiarFase('abierto');
      // a fresh connection knows nothing of what the previous one was told
      if (this.dims !== null) this.enviarControl({ type: 'RESIZE', cols: this.dims.cols, rows: this.dims.filas });
      if (this.pausado) this.enviarControl({ type: 'PAUSE_FLOW' });
    };
    ws.onmessage = (ev: MessageEvent) => {
      if (this.ws === ws) this.alMensaje(ev.data as unknown);
    };
    ws.onerror = () => {
      // the event carries no detail; the close event that follows says how it ended
      if (this.ws === ws) this.o.alError?.(new Error(`websocket error on ${this.o.url}`));
    };
    ws.onclose = (ev: CloseEvent) => {
      if (this.ws !== ws) return;
      this.ws = null;
      this.alCerrar(ev.code, ev.reason);
    };
  }

  private alMensaje(datos: unknown): void {
    if (typeof datos === 'string') {
      this.contadores.control++;
      let leido: unknown;
      try {
        leido = JSON.parse(datos);
      } catch (e) {
        this.contadores.invalidas++;
        this.o.alError?.(new ErrorDeTrama(`control frame is not JSON: ${e instanceof Error ? e.message : String(e)}`));
        return;
      }
      if (leido === null || typeof leido !== 'object' || Array.isArray(leido)) {
        this.contadores.invalidas++;
        this.o.alError?.(new ErrorDeTrama('control frame is JSON but not an object'));
        return;
      }
      const msg = leido as { type?: unknown; code?: unknown };
      if (msg.type === 'EXIT') {
        this.salidaRecibida = true;
        this.o.alSalida?.(typeof msg.code === 'number' ? msg.code : null);
      }
      return;
    }
    if (!(datos instanceof ArrayBuffer)) {
      this.contadores.invalidas++;
      this.o.alError?.(new ErrorDeTrama('binary frame did not arrive as an ArrayBuffer'));
      return;
    }
    let trama: Trama;
    try {
      trama = decodificarTrama(datos);
    } catch (e) {
      this.contadores.invalidas++;
      this.o.alError?.(e instanceof Error ? e : new Error(String(e)));
      return;
    }
    if (trama.seq !== this.seqEntrada + 1) this.contadores.saltosDeSecuencia++;
    this.seqEntrada = trama.seq;
    this.contadores.tramas++;
    this.contadores.bytes += trama.cuerpo.byteLength;
    this.o.alDatos(trama.cuerpo, trama);
  }

  private alCerrar(codigo: number, motivo: string): void {
    this.cancelarEstable();
    if (this.cerradoPorNosotros || this.salidaRecibida) {
      this.cambiarFase('cerrado', `close ${codigo}`);
      return;
    }
    const maximo = this.o.maxReconexiones ?? 5;
    if (this.intentos >= maximo) {
      const error = new Error(`gave up reconnecting to ${this.o.url} after ${this.intentos} attempts (last close ${codigo} ${motivo})`);
      this.o.alError?.(error);
      this.cambiarFase('fallido', error.message);
      return;
    }
    const espera = Math.min((this.o.esperaBaseMs ?? 50) * 2 ** this.intentos, ESPERA_MAX_MS);
    this.intentos++;
    this.contadores.reconexiones++;
    this.cambiarFase('reconectando', `close ${codigo}, attempt ${this.intentos}`);
    this.temporizador = this.programar(() => {
      this.temporizador = null;
      if (!this.cerradoPorNosotros) this.abrir();
    }, espera);
  }

  private programar(fn: () => void, ms: number): unknown {
    return (this.o.programar ?? ((f: () => void, m: number) => setTimeout(f, m)))(fn, ms);
  }

  private cancelar(manejador: unknown): void {
    (this.o.cancelar ?? ((m: unknown) => clearTimeout(m as ReturnType<typeof setTimeout>)))(manejador);
  }

  private cancelarEstable(): void {
    if (this.estable !== null) this.cancelar(this.estable);
    this.estable = null;
  }

  private enviarControl(msg: Record<string, unknown>): boolean {
    if (this.ws === null || this.ws.readyState !== 1) return false;
    this.ws.send(JSON.stringify(msg));
    return true;
  }

  /** Keyboard bytes to the PTY. Returns false, and counts it, when the stream is not open: input is never dropped unseen. */
  enviarEntrada(datos: Uint8Array): boolean {
    if (this.ws === null || this.ws.readyState !== 1) {
      this.contadores.descartadas++;
      this.o.alError?.(new Error(`input dropped: the stream is ${this.fase}`));
      return false;
    }
    this.ws.send(codificarTrama(datos, this.seqSalida++, Date.now() - this.inicio));
    this.contadores.enviadas++;
    return true;
  }

  enviarTexto(texto: string): boolean {
    return this.enviarEntrada(this.codificador.encode(texto));
  }

  redimensionar(cols: number, filas: number): void {
    this.dims = { cols, filas };
    this.enviarControl({ type: 'RESIZE', cols, rows: filas });
  }

  pausar(): void {
    if (this.pausado) return;
    this.pausado = true;
    this.enviarControl({ type: 'PAUSE_FLOW' });
  }

  reanudar(): void {
    if (!this.pausado) return;
    this.pausado = false;
    this.enviarControl({ type: 'RESUME_FLOW' });
  }

  cerrar(): void {
    this.cerradoPorNosotros = true;
    if (this.temporizador !== null) this.cancelar(this.temporizador);
    this.temporizador = null;
    this.cancelarEstable();
    const ws = this.ws;
    this.ws = null;
    if (ws !== null) {
      ws.onopen = ws.onmessage = ws.onerror = ws.onclose = null;
      ws.close(1000, 'client closed');
    }
    this.cambiarFase('cerrado', 'closed by the client');
  }
}
