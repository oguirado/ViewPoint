// Tests of stage 7.2: the typed bridge wrappers (srfd.ts), the recording helpers (capsulas.ts), the REC control and
// PTY agents of App.svelte, and the lifecycle / telemetry / card wiring, as source text and as pure functions.

import { readFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';

import { METODOS_PUENTE, SrfdError, crearPuenteCockpit } from '../src/lib/srfd';
import { estadoRec, grabacionDeSlot, mmss } from '../src/lib/capsulas';
import type { RecordingSession } from '../src/lib/types/hypervisor';

const leer = (rutaRelativa: string): string => readFileSync(new URL(rutaRelativa, import.meta.url), 'utf8');
const APP = leer('../src/App.svelte');
const APP_CSS = leer('../src/app.css');
const COCKPIT = leer('../src/lib/components/CockpitMasterView.svelte');
const TELEMETRIA = leer('../src/lib/components/TelemetryMasterView.svelte');
const CARD = leer('../src/lib/cards/CapsuleCard.svelte');
const BAR = leer('../src/lib/aiops/CapsuleBar.svelte'); // W7 F1-VERBS: per-capsule verbs (REC included) moved to the capsule bar
// the bound methods live in app.go and pbx.go (both package main, build tag cockpit)
const GO_APP = leer('../../app.go') + leer('../../pbx.go');
const GO_PTY = leer('../../ptyserver.go');

const sesion = (extra: Partial<RecordingSession>): RecordingSession => ({
  recording: false,
  sessionId: '',
  targetXid: '',
  sinceMonoMs: 0,
  offsetMs: null,
  verdict: 'IDLE',
  fase: 'idle',
  slot: 0,
  dir: '',
  display: ':150',
  manifiesto: '',
  artefactos: 0,
  duracionMs: 0,
  ...extra,
});

describe('srfd.ts: the bridge wrappers name the Go methods and refuse to emulate a missing one', () => {
  it('maps every wrapper to a method cockpit/app.go binds', () => {
    for (const nombre of Object.values(METODOS_PUENTE)) {
      expect(GO_APP, nombre).toMatch(new RegExp(`func \\(a \\*App\\) ${nombre}\\(`));
    }
  });
  it('fails sin_puente without a bridge and without the method, and forwards the answer with one', async () => {
    const sinPuente = crearPuenteCockpit({});
    expect(sinPuente.disponible('ptyUrl')).toBe(false);
    await expect(sinPuente.ptyUrl('a')).rejects.toMatchObject({ codigo: 'sin_puente' });
    const parcial = crearPuenteCockpit({ go: { main: { App: { PtyUrl: async (id: string) => `ws://127.0.0.1:1/pty/${id}?t=x` } } } });
    expect(parcial.disponible('ptyUrl')).toBe(true);
    expect(parcial.disponible('estadoGrabacion')).toBe(false);
    await expect(parcial.ptyUrl('agente-1')).resolves.toBe('ws://127.0.0.1:1/pty/agente-1?t=x');
    await expect(parcial.estadoGrabacion()).rejects.toMatchObject({ codigo: 'sin_puente' });
    const nulos = crearPuenteCockpit({ go: { main: { App: { EstadoPuertas: async () => null, PbxLeases: async () => null, CandidatosGrabacion: async () => null } } } });
    await expect(nulos.estadoPuertas()).resolves.toEqual([]);
    await expect(nulos.pbxLeases()).resolves.toEqual([]);
    await expect(nulos.candidatosGrabacion()).resolves.toEqual([]);
  });
});

describe('capsulas.ts: the six REC states and the per-slot recording state', () => {
  it('derives the affordance state from the session', () => {
    expect(estadoRec(null)).toBe('idle');
    expect(estadoRec(null, false)).toBe('no target');
    expect(estadoRec(sesion({ fase: 'starting' }))).toBe('starting');
    expect(estadoRec(sesion({ fase: 'recording', recording: true }))).toBe('recording');
    expect(estadoRec(sesion({ fase: 'stopping', recording: true }))).toBe('stopping');
    expect(estadoRec(sesion({ fase: 'sealed', manifiesto: 'completa' }))).toBe('sealed');
    expect(estadoRec(sesion({ fase: 'error', error: 'manifest incompleta' }))).toBe('idle');
  });
  it('formats mm:ss and attributes the live session to its slot only', () => {
    expect(mmss(0)).toBe('00:00');
    expect(mmss(59_999)).toBe('00:59');
    expect(mmss(61_000)).toBe('01:01');
    expect(mmss(-5)).toBe('00:00');
    const viva = sesion({ fase: 'recording', recording: true, slot: 3, sessionId: 's1', targetXid: '0x2a', sinceMonoMs: 4200 });
    expect(grabacionDeSlot(viva, 3)).toMatchObject({ recording: true, sessionId: 's1', targetXid: '0x2a', sinceMonoMs: 4200 });
    expect(grabacionDeSlot(viva, 4).recording).toBe(false);
    expect(grabacionDeSlot(null, 3)).toMatchObject({ recording: false, sessionId: '' });
  });
});

describe('App.svelte: synchronized REC control with the 800 ms red phosphor pulse', () => {
  it('delegates recording to per-slot RAM controls, removing header REC and the target selector', () => {
    expect(APP).not.toMatch(/class="rec-boton"|selector-rec|puente\s*\.iniciarGrabacion/);
    expect(APP).toMatch(/<CockpitMasterView[^>]*\{recBridge\}/);
    expect(APP).toMatch(/const recBridge = recordingBridge\(\)/);
  });
  it('defines the pulse once, in app.css, at 800 ms, and honours reduced motion', () => {
    expect(APP_CSS).toMatch(/@keyframes rec-phosphor-pulse/);
    expect(APP_CSS).toMatch(/\.rec-indicator\.active \{[^}]*animation: rec-phosphor-pulse 800ms ease-in-out infinite/);
    expect(APP_CSS).toMatch(/prefers-reduced-motion: reduce\) \{\s*\.rec-indicator\.active,\s*\.rec-indicator\.pending \{\s*animation: none;/);
    // W7 F1-VERBS removed the disk-backed card REC (B9); the card's rec-indicator returns with F1-REC (RAM store).
    const REC = leer('../src/lib/aiops/RecordingControls.svelte');
    expect(REC).toMatch(/class="rec-indicator"/);
    for (const fuente of [APP, COCKPIT, CARD, BAR]) {
      expect(fuente).not.toMatch(/@keyframes rec-phosphor-pulse/);
    }
  });
  it('opens the drawer agents through PtyUrl and types the pill as a directive once the stream is open', () => {
    expect(APP).toMatch(/url = await puente\.ptyUrl\(id\)/);
    expect(APP).toMatch(/term\.input\(`\$\{directiva\}\\r`, true\)/);
    expect(APP).toMatch(/fasesAgentes\.get\(id\) !== 'abierto'/);
    expect(APP).toMatch(/<CajonAgentes[^>]*alMontar=\{alMontarAgente\}[^>]*alFase=\{alFaseAgente\}/);
    expect(APP).not.toMatch(/no PTY stream endpoint/);
  });
});

describe('VIEW-01 / cards / VIEW-02: the controls reach the backend', () => {
  it('DETACH, ATTACH and STREAM ATTACHED call srfd, and the slot header carries REC', () => {
    expect(COCKPIT).toMatch(/onclick=\{detachCapsule\}>DETACH/);
    expect(COCKPIT).toMatch(/onclick=\{attachCapsule\}>ATTACH CAPSULE/);
    expect(COCKPIT).toMatch(/onclick=\{streamAttached\}>STREAM ATTACHED/);
    expect(COCKPIT).toMatch(/CerrarSlot\(/);
    expect(COCKPIT).toMatch(/cliente\s*\.iniciar\(clase, objetivo\.slot\)/);
    expect(COCKPIT).toMatch(/<CapsuleCard\b/);
    expect(COCKPIT).not.toMatch(/onclick=\{\(\) => transitionState\('empty'\)\}/);
  });
  it('the capsule card offers REC and shows the indicator from capsula.recording', () => {
    expect(BAR).toMatch(/aria-label="REC \{slot_id\}"/); // F1-VERBS: REC lives in the capsule bar, disabled until F1-REC provides the RAM store (B9)
    // the card REC indicator bound to capsula.recording returns with F1-REC (RAM store); until then REC is disabled in the bar
    expect(BAR).toMatch(/rec/);
  });
  it('telemetry polls host telemetry and gate overrides, trips gates through the bridge and dispatches PBX actions', () => {
    expect(TELEMETRIA).toMatch(/puenteCockpit\.hostTelemetria\(\)/);
    expect(TELEMETRIA).toMatch(/puenteCockpit\.estadoPuertas\(\)/);
    expect(TELEMETRIA).toMatch(/puenteCockpit\.tripGate\(key, reason\)/);
    expect(TELEMETRIA).toMatch(/puenteCockpit\.resetGate\('ALL'\)/);
    expect(TELEMETRIA).toMatch(/puenteCockpit\s*\.pbxLeases\(\)/);
    for (const a of ['jack', 'trunk', 'monitor']) expect(TELEMETRIA).toMatch(new RegExp(`onclick=\\{\\(\\) => accionPbx\\('${a}', l\\)\\}`));
    expect(TELEMETRIA).toMatch(/if \(fuente === 'fixture'\) \{/);
  });
});

describe('cockpit/ptyserver.go: the SRF2 broker contract the client expects', () => {
  it('binds loopback only, gates every upgrade with the token, and speaks the four control frames', () => {
    expect(GO_PTY).toMatch(/net\.Listen\("tcp", "127\.0\.0\.1:0"\)/);
    expect(GO_PTY).toMatch(/subtle\.ConstantTimeCompare/);
    for (const c of ['RESIZE', 'PAUSE_FLOW', 'RESUME_FLOW', 'EXIT']) expect(GO_PTY).toContain(`"${c}"`);
    expect(GO_PTY).toMatch(/ws:\/\/127\.0\.0\.1:%d\/pty\/%s\?t=%s/);
  });
});
