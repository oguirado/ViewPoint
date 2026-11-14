// Bench page of VIEW-03 (dev server only, never built): the drawer over a fake canvas, fed by an in-page fake PTY
// socket that greets, echoes and answers directives. `?agentes=0` shows the empty slate.
import { mount } from 'svelte';
import '../src/app.css';
import { LIENZO_H, LIENZO_W } from '../src/lib/geometria';
import { codificarTrama, decodificarTrama } from '../src/lib/terminal/wsstream';
import Banco from './Banco.svelte';

// FlujoWs binds the on* properties, so the fake calls them directly (an EventTarget would not).
class SocketFalso {
  binaryType = 'arraybuffer';
  readyState = 0;
  seq = 0;
  onopen: ((ev: Event) => void) | null = null;
  onmessage: ((ev: MessageEvent) => void) | null = null;
  onclose: ((ev: CloseEvent) => void) | null = null;
  onerror: ((ev: Event) => void) | null = null;
  constructor(public url: string) {
    setTimeout(() => {
      this.readyState = 1;
      this.onopen?.(new Event('open'));
      const id = url.split('/').pop() ?? '?';
      this.decir(`\x1b[90m[2026-09-22T13:07:46Z] Connected to PTY cage ${id} (Claude Code v0.24.0)\x1b[0m\r\n`);
      this.decir(`\x1b[32madmin@open-superficies\x1b[0m:\x1b[36m~\x1b[0m$ claude --dangerously-skip-permissions\r\n`);
      this.decir(`\x1b[90m╭─── Claude Code ─────────────────────────────╮\r\n│ Agent initialized: Subagent 4 active        │\r\n│ Telemetry: UDS stream nominal               │\r\n╰─────────────────────────────────────────────╯\x1b[0m\r\n`);
      this.decir(`\x1b[36mclaude>\x1b[0m Termguard verification passed: DOM renderer active, 0 canvas layers.\r\n\x1b[36mclaude>\x1b[0m Ready for supervisory directives.\r\n`);
    }, 150);
  }
  decir(texto: string): void {
    const cuerpo = new TextEncoder().encode(texto);
    const trama = codificarTrama(cuerpo, this.seq++, 0);
    this.onmessage?.(new MessageEvent('message', { data: trama.buffer.slice(trama.byteOffset, trama.byteOffset + trama.byteLength) }));
  }
  send(datos: ArrayBuffer | Uint8Array): void {
    try {
      const t = decodificarTrama(datos instanceof Uint8Array ? datos : new Uint8Array(datos));
      const texto = new TextDecoder().decode(t.cuerpo);
      if (texto.length > 0 && !texto.startsWith('\x1b')) this.decir(texto.replace(/\r/g, '\r\n'));
    } catch {
      /* control frames */
    }
  }
  close(): void {
    this.readyState = 3;
    this.onclose?.(new CloseEvent('close', { code: 1000 }));
  }
}

const n = Number(new URLSearchParams(location.search).get('agentes') ?? '3');
mount(Banco, { target: document.getElementById('bench')!, props: { n, w: LIENZO_W, h: LIENZO_H, crearSocket: (url: string) => new SocketFalso(url) as unknown as WebSocket } });
